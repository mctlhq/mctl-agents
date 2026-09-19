"""find_stranded_accepted — the implement-sweep's stranding predicate (#412).

Mirrors test_lifecycle_reconcile_activity.py's approach: monkeypatch
`list_proposal_refs` directly rather than faking the GitHub transport, since
the thing under test is the filter logic, not the HTTP layer gitops_state.py
already owns tests for.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from temporalio.testing import ActivityEnvironment

from orchestrator.temporal.activities import stranded as act
from orchestrator.temporal.activities.gitops_state import ProposalStateRef

pytestmark = pytest.mark.anyio

# find_stranded_accepted reads the real clock (datetime.now(UTC)), not an
# injected one, so every fixture timestamp below is relative to it at call
# time rather than to a fixed constant — a hardcoded date would drift out of
# the grace/lease windows depending on when the suite happens to run.
NOW = datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _ref(service="mctl-web", slug="issue-10-widget", **overrides) -> ProposalStateRef:
    base = dict(
        service=service,
        slug=slug,
        status="accepted",
        pr_url=None,
        updated_at=_iso(NOW - timedelta(hours=1)),
        attempt_expires_at=None,
        unrunnable=False,
        blocked=False,
    )
    base.update(overrides)
    return ProposalStateRef(**base)


@pytest.fixture
def env():
    return ActivityEnvironment()


async def _run(env, monkeypatch, refs, active=None, grace_minutes=20):
    async def _list_refs():
        return refs

    monkeypatch.setattr(act, "list_proposal_refs", _list_refs)
    return await env.run(act.find_stranded_accepted, active or [], grace_minutes)


class TestStranding:
    async def test_an_accepted_proposal_with_no_owner_is_stranded(self, env, monkeypatch):
        result = await _run(env, monkeypatch, [_ref()])

        assert result.total_accepted == 1
        assert len(result.stranded) == 1
        candidate = result.stranded[0]
        assert candidate.service == "mctl-web"
        assert candidate.slug == "issue-10-widget"
        assert candidate.reason == "accepted, no PR, no live DevLoopWorkflow"
        assert result.skipped == []
        assert result.skipped_reason is None

    async def test_a_non_accepted_proposal_is_not_a_candidate(self, env, monkeypatch):
        result = await _run(env, monkeypatch, [_ref(status="proposed")])

        assert result.total_accepted == 0
        assert result.stranded == []


class TestSkipFilters:
    async def test_a_pr_url_is_skipped(self, env, monkeypatch):
        result = await _run(
            env, monkeypatch, [_ref(pr_url="https://github.com/mctlhq/mctl-web/pull/42")]
        )

        assert result.stranded == []
        key, reason = result.skipped[0]
        assert key == "mctl-web/issue-10-widget"
        assert "pr:" in reason

    async def test_an_unexpired_attempt_lease_is_skipped(self, env, monkeypatch):
        result = await _run(
            env,
            monkeypatch,
            [_ref(attempt_expires_at=_iso(NOW + timedelta(minutes=30)))],
        )

        assert result.stranded == []
        key, reason = result.skipped[0]
        assert key == "mctl-web/issue-10-widget"
        assert "attempt lease" in reason

    async def test_an_expired_attempt_lease_does_not_skip(self, env, monkeypatch):
        """A lease that already ran out is not a live holder — the sweep
        must still consider the proposal."""
        result = await _run(
            env,
            monkeypatch,
            [_ref(attempt_expires_at=_iso(NOW - timedelta(minutes=30)))],
        )

        assert len(result.stranded) == 1

    async def test_unrunnable_as_written_is_skipped(self, env, monkeypatch):
        """`control.requires_human_approval` with no verified approver
        (mctl-agents#349) — a submit could only refuse."""
        result = await _run(env, monkeypatch, [_ref(unrunnable=True)])

        assert result.stranded == []
        key, reason = result.skipped[0]
        assert key == "mctl-web/issue-10-widget"
        assert "unrunnable" in reason

    async def test_a_blocked_marker_is_skipped(self, env, monkeypatch):
        result = await _run(env, monkeypatch, [_ref(blocked=True)])

        assert result.stranded == []
        key, reason = result.skipped[0]
        assert key == "mctl-web/issue-10-widget"
        assert "blocked" in reason

    async def test_updated_within_the_grace_period_is_skipped(self, env, monkeypatch):
        """A DevLoopWorkflow may be between its approve flip and its own
        implement submit — never race it."""
        result = await _run(
            env,
            monkeypatch,
            [_ref(updated_at=_iso(NOW - timedelta(minutes=5)))],
            grace_minutes=20,
        )

        assert result.stranded == []
        key, reason = result.skipped[0]
        assert key == "mctl-web/issue-10-widget"
        assert "grace period" in reason

    async def test_updated_outside_the_grace_period_does_not_skip(self, env, monkeypatch):
        result = await _run(
            env,
            monkeypatch,
            [_ref(updated_at=_iso(NOW - timedelta(minutes=25)))],
            grace_minutes=20,
        )

        assert len(result.stranded) == 1

    async def test_an_active_dev_loop_owning_the_proposal_is_skipped(self, env, monkeypatch):
        """issue-10-widget on mctl-web derives dev-loop-mctlhq-mctl-web-10 —
        orphans._expected_workflow_id's own reconstruction."""
        result = await _run(
            env,
            monkeypatch,
            [_ref(service="mctl-web", slug="issue-10-widget")],
            active=["dev-loop-mctlhq-mctl-web-10"],
        )

        assert result.stranded == []
        key, reason = result.skipped[0]
        assert key == "mctl-web/issue-10-widget"
        assert "dev-loop-mctlhq-mctl-web-10" in reason

    async def test_a_different_active_dev_loop_does_not_skip(self, env, monkeypatch):
        result = await _run(
            env,
            monkeypatch,
            [_ref(service="mctl-web", slug="issue-10-widget")],
            active=["dev-loop-mctlhq-mctl-web-99"],
        )

        assert len(result.stranded) == 1


class TestIncidentSlugs:
    async def test_an_incident_slug_is_swept(self, env, monkeypatch):
        """incident-* never had a DevLoop (expected_dev_loop_id is None), so
        it is exactly the permanently-stranded case the incident responder's
        auto-accepted proposals fall into."""
        result = await _run(
            env,
            monkeypatch,
            [_ref(service="mctl-web", slug="incident-2026-09-19-outage")],
            active=["dev-loop-mctlhq-mctl-web-10"],
        )

        assert len(result.stranded) == 1
        assert result.stranded[0].slug == "incident-2026-09-19-outage"


class TestManyProposals:
    async def test_only_stranded_ones_are_reported_and_the_rest_are_skipped(self, env, monkeypatch):
        refs = [
            _ref(service="mctl-web", slug="issue-1-a"),
            _ref(service="mctl-web", slug="issue-2-b", pr_url="https://github.com/mctlhq/mctl-web/pull/1"),
            _ref(service="mctl-api", slug="issue-3-c", status="proposed"),
        ]
        result = await _run(env, monkeypatch, refs)

        assert result.total_accepted == 2
        assert [c.slug for c in result.stranded] == ["issue-1-a"]
        assert len(result.skipped) == 1
