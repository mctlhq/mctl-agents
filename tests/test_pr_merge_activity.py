"""orchestrator/temporal/activities/pr_merge.py — the gated merge activity
(mctlhq/mctl-agents#519, docs/adr/017-shepherd-merge-approval.md).

Every read is `run_shepherd`'s own (monkeypatched here, exercised for real in
tests/test_run_shepherd.py); the approval flow itself runs against the same
fake mctl-api tests/test_action_approvals.py and
tests/test_action_approval_wait.py use, so `merge_pull_request_gated` is
exercised through the real `run_gated`/checkpoint/store path, not a stub of
it.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from temporalio.testing import ActivityEnvironment

from orchestrator import action_approvals as aa
from orchestrator import policy_checkpoint as pc
from orchestrator import run_shepherd
from orchestrator.temporal.activities import pr_merge as act
from orchestrator.temporal.activities.action_approval import GatedActionInput
from tests.test_action_approvals import FakeMctlApi
from tests.test_run_shepherd import HEAD_SHA, make_finding, make_pr

pytestmark = pytest.mark.anyio

APPROVED_REVIEW = run_shepherd.CodexReview(has_responded=True, findings=[], head_verdict="APPROVED")


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _action(
    *, repo: str = "mctlhq/mctl-web", number: int = 42, head_sha: str = HEAD_SHA, service: str = "mctl-web",
) -> GatedActionInput:
    return GatedActionInput(
        payload={"repo": repo, "pr_number": number, "head_sha": head_sha, "service": service},
        execution_id="ctx-519", actor="system:dev-loop-workflow", trace_id="tr-519",
    )


@pytest.fixture
def env() -> ActivityEnvironment:
    return ActivityEnvironment()


@pytest.fixture(autouse=True)
def _gate_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pc.MERGE_APPROVAL_ENV, raising=False)
    monkeypatch.delenv(pc.APPROVALS_ENV, raising=False)
    monkeypatch.setattr(run_shepherd, "SHEPHERD_MERGE_APPROVAL_SERVICES", frozenset())


def _enable_gate(monkeypatch: pytest.MonkeyPatch, service: str = "mctl-web") -> None:
    monkeypatch.setenv(pc.MERGE_APPROVAL_ENV, pc.MERGE_APPROVAL_REQUIRE)
    monkeypatch.setattr(run_shepherd, "SHEPHERD_MERGE_APPROVAL_SERVICES", frozenset({service}))


async def test_gate_off_performs_zero_network_calls(env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a, **_kw):
        raise AssertionError("must not read the PR when the gate is off")

    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", _boom)

    result = await env.run(act.merge_pull_request_gated, _action())

    assert result.code == act.CODE_MERGE_GATE_DISABLED
    assert not result.ran
    assert not result.approval_ref


async def test_never_merge_service_is_forbidden_before_any_read(
    env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_gate(monkeypatch, service="mctl-academy")

    def _boom(*_a, **_kw):
        raise AssertionError("must not read the PR for a NEVER_MERGE_SERVICES repo")

    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", _boom)

    result = await env.run(
        act.merge_pull_request_gated, _action(repo="mctlhq/mctl-academy", service="mctl-academy"),
    )

    assert result.code == act.CODE_MERGE_FORBIDDEN
    assert not result.ran
    assert not result.approval_ref


@pytest.mark.parametrize(("kwargs", "label"), [
    ({"merged": True, "merge_commit": "m" * 40}, "already-merged"),
    ({"closed_unmerged": True}, "closed-unmerged"),
    ({"is_draft": True}, "draft"),
])
async def test_precondition_unmet_creates_no_request(
    env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch, kwargs: dict, label: str,
) -> None:
    _enable_gate(monkeypatch)
    pr = make_pr(**kwargs)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: pr)
    monkeypatch.setattr(run_shepherd, "read_codex_review", lambda _pr: APPROVED_REVIEW)

    result = await env.run(act.merge_pull_request_gated, _action())

    assert result.code == act.CODE_MERGE_PRECONDITION_UNMET, label
    assert not result.ran
    assert not result.approval_ref


async def test_head_moved_is_a_precondition_miss(env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_gate(monkeypatch)
    pr = make_pr(head_sha="b" * 40)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: pr)
    monkeypatch.setattr(run_shepherd, "read_codex_review", lambda _pr: APPROVED_REVIEW)

    result = await env.run(act.merge_pull_request_gated, _action(head_sha=HEAD_SHA))

    assert result.code == act.CODE_MERGE_PRECONDITION_UNMET
    assert not result.ran
    assert not result.approval_ref


async def test_unreadable_snapshot_is_a_precondition_miss(
    env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_gate(monkeypatch)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: None)

    result = await env.run(act.merge_pull_request_gated, _action())

    assert result.code == act.CODE_MERGE_PRECONDITION_UNMET
    assert not result.ran


async def test_decide_wait_is_a_precondition_miss(env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex has not responded yet: decide() returns "wait", not "merge"."""
    _enable_gate(monkeypatch)
    pr = make_pr()
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: pr)
    monkeypatch.setattr(run_shepherd, "read_codex_review",
                        lambda _pr: run_shepherd.CodexReview(has_responded=False, findings=[]))

    result = await env.run(act.merge_pull_request_gated, _action())

    assert result.code == act.CODE_MERGE_PRECONDITION_UNMET
    assert not result.ran


async def test_decide_address_review_is_a_precondition_miss(
    env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh P1 finding on this head: decide() returns "address-review"."""
    _enable_gate(monkeypatch)
    pr = make_pr()
    finding = make_finding(severity="P1", commit_id=HEAD_SHA)
    review = run_shepherd.CodexReview(has_responded=True, findings=[finding], head_verdict="APPROVED")
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: pr)
    monkeypatch.setattr(run_shepherd, "read_codex_review", lambda _pr: review)

    result = await env.run(act.merge_pull_request_gated, _action())

    assert result.code == act.CODE_MERGE_PRECONDITION_UNMET
    assert not result.ran


async def test_decide_merge_opens_a_pending_request_and_does_not_merge_yet(
    env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """decide() says merge, so the checkpoint asks mctl-api for an approval:
    a receipt is created and the call ends `approval_pending` — the merge
    itself must wait for a human, never happening in this same call."""
    _enable_gate(monkeypatch)
    monkeypatch.setenv(pc.APPROVALS_ENV, pc.APPROVALS_MCTL_API)
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    fake = FakeMctlApi(_Clock())  # type: ignore[arg-type]
    monkeypatch.setattr(aa, "_no_redirect_opener", lambda: fake)

    pr = make_pr()
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: pr)
    monkeypatch.setattr(run_shepherd, "read_codex_review", lambda _pr: APPROVED_REVIEW)

    def _boom(_pr):
        raise AssertionError("must not merge before an approval is granted")

    monkeypatch.setattr(run_shepherd, "merge_pr_unchecked", _boom)

    result = await env.run(act.merge_pull_request_gated, _action())

    assert result.code == pc.CODE_APPROVAL_PENDING
    assert result.approval_ref
    assert not result.ran
    assert len(fake.records) == 1


async def test_a_granted_approval_merges_exactly_once(
    env: ActivityEnvironment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_gate(monkeypatch)
    monkeypatch.setenv(pc.APPROVALS_ENV, pc.APPROVALS_MCTL_API)
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    fake = FakeMctlApi(_Clock())  # type: ignore[arg-type]
    monkeypatch.setattr(aa, "_no_redirect_opener", lambda: fake)

    pr = make_pr()
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: pr)
    monkeypatch.setattr(run_shepherd, "read_codex_review", lambda _pr: APPROVED_REVIEW)
    merges: list[str] = []
    monkeypatch.setattr(run_shepherd, "merge_pr_unchecked", lambda _pr: (merges.append(_pr.repo), (True, "m" * 40))[1])

    first = await env.run(act.merge_pull_request_gated, _action())
    assert first.code == pc.CODE_APPROVAL_PENDING
    fake.decide(first.approval_ref, "approve")

    second = await env.run(act.merge_pull_request_gated, GatedActionInput(
        payload=_action().payload, execution_id="ctx-519", actor="system:dev-loop-workflow", trace_id="tr-519",
        approval_ref=first.approval_ref,
    ))

    assert second.code == pc.CODE_APPROVED
    assert second.ran
    assert merges == ["mctlhq/mctl-web"]
    assert second.approver == "github:root"
