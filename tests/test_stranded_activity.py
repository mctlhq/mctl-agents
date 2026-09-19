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

from orchestrator.proposal_state import (
    AUTHORIZATION_HUMAN_APPROVAL,
    UNAUTHORIZED_LEGACY_AUTO_ACCEPTED,
)
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
        # Default fixture models a proposal a human actually approved —
        # `approval.approved_by: <a real login>`, which is the only thing that
        # authorizes execution today. The unauthorized path is exercised
        # explicitly by TestExecutionAuthorization below.
        execution_authorization=AUTHORIZATION_HUMAN_APPROVAL,
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
    async def test_a_legacy_incident_record_is_quarantined_not_swept(self, env, monkeypatch):
        """The 69 legacy `incident-*` proposals, as they actually exist.

        `incident-*` never had a DevLoop (`expected_dev_loop_id` is None), so
        every other filter passes it straight through — it IS the permanently
        stranded shape. What stops it is authorization: the incident
        responder's template writes `status: accepted` with no `control` block
        and no `approval`, so nothing ever authorized executing it.

        This test asserted the opposite one commit ago. The 2026-09-19 product
        decision on #412 settled it: provenance of who created an
        auto-accepted record is not human authorization to execute it, these
        69 are diagnostic artifacts, and they belong in human triage rather
        than in 69 implementer runs on the first tick.
        """
        result = await _run(
            env,
            monkeypatch,
            [
                _ref(
                    service="mctl-web",
                    slug="incident-2026-09-19-outage",
                    execution_authorization=None,
                )
            ],
            active=["dev-loop-mctlhq-mctl-web-10"],
        )

        assert result.stranded == [], "a legacy incident record must never be submitted"
        assert result.unauthorized == [
            ("mctl-web/incident-2026-09-19-outage", UNAUTHORIZED_LEGACY_AUTO_ACCEPTED)
        ]

    async def test_an_incident_record_a_human_approved_is_swept(self, env, monkeypatch):
        """Quarantine is about authorization, not about the slug.

        An `incident-*` record that a human HAS approved carries real
        authorization and must still be swept — otherwise the fix for the 69
        would silently become a permanent ban on the whole incident path.
        """
        result = await _run(
            env,
            monkeypatch,
            [_ref(slug="incident-2026-09-19-outage",
                  execution_authorization=AUTHORIZATION_HUMAN_APPROVAL)],
        )

        assert len(result.stranded) == 1
        assert result.unauthorized == []


class TestExecutionAuthorization:
    """mctl-agents#412, the 2026-09-19 product decision.

    The sweep submits execution on records nobody is watching, so it fails
    closed: missing `control`/`approval` metadata must never read as "approval
    not required" here, and a writer allowlist is not an acceptable stand-in
    for authorization.
    """

    async def test_no_authorization_at_all_is_quarantined(self, env, monkeypatch):
        result = await _run(env, monkeypatch, [_ref(execution_authorization=None)])

        assert result.stranded == []
        key, reason = result.skipped[0]
        assert key == "mctl-web/issue-10-widget"
        assert UNAUTHORIZED_LEGACY_AUTO_ACCEPTED in reason
        assert result.unauthorized == [
            ("mctl-web/issue-10-widget", UNAUTHORIZED_LEGACY_AUTO_ACCEPTED)
        ]

    async def test_the_scan_has_no_field_to_build_a_writer_allowlist_from(self):
        """The rejected mechanism is gone, not merely unused.

        `updated_by` was threaded all the way from `_parse_status_yaml` into
        `ProposalStateRef` for the sole purpose of trusting one writer. Keeping
        the field would leave the allowlist one line away from coming back, so
        the field itself is removed and this pins that.
        """
        from dataclasses import fields

        names = {f.name for f in fields(ProposalStateRef)}
        assert "updated_by" not in names
        assert "has_control_block" not in names
        assert "execution_authorization" in names

    async def test_a_quarantined_record_does_not_block_an_authorized_one(
        self, env, monkeypatch
    ):
        """The quarantine skips its own record and nothing else."""
        result = await _run(
            env,
            monkeypatch,
            [
                _ref(service="mctl-web", slug="incident-a", execution_authorization=None),
                _ref(service="mctl-web", slug="issue-2-b"),
                _ref(service="mctl-api", slug="incident-c", execution_authorization=None),
            ],
        )

        assert [p.slug for p in result.stranded] == ["issue-2-b"]
        assert len(result.unauthorized) == 2
        assert result.total_accepted == 3


class TestTimestampParsing:
    async def test_an_offset_less_timestamp_is_read_as_utc_not_a_crash(
        self, env, monkeypatch
    ):
        """`_parse_iso`'s naive branch: every other fixture goes through
        `_iso()`, which always emits a `Z`, so the branch that assumes UTC for
        a hand-edited offset-less timestamp was never executed (review P3).
        Comparing a naive datetime against the aware `now` raises TypeError,
        which would take the whole scan down over one `.status.yaml`."""
        naive_recent = (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
        result = await _run(env, monkeypatch, [_ref(updated_at=naive_recent)])

        assert result.stranded == []
        assert "grace period" in result.skipped[0][1], (
            "read as UTC, this timestamp is inside the grace window"
        )

    async def test_an_unparseable_timestamp_is_treated_as_absent(self, env, monkeypatch):
        result = await _run(env, monkeypatch, [_ref(updated_at="yesterday-ish")])

        assert len(result.stranded) == 1, (
            "an unparseable timestamp must not take the scan down, and must "
            "not silently hold a proposal in the grace period forever"
        )


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
