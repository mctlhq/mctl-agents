"""The cron-driven merge-approval wait (mctlhq/mctl-agents#198,
docs/adr/016-human-approval-checkpoints.md): `run_shepherd.merge_pr` parking
and resuming a `REQUIRE_APPROVAL` merge in `.status.yaml` instead of only
blocking.

The Temporal wait (#479) is reused vocabulary and is not touched here; these
tests exercise only the cron driver: `ApprovalTicket` persistence, the
outcome table in design.md §3, and that `merge_pr`'s `(False, None)` refusal
contract and its default (`ref=None`) behaviour are unchanged.
"""
from __future__ import annotations

import functools
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator import policy_checkpoint as pc
from orchestrator import proposal_state, run_shepherd
from orchestrator.action_approvals import ApprovalAnswer, ApprovalRecord
from orchestrator.approval_ticket import ticket_from
from tests.test_run_shepherd import HEAD_SHA, PR_URL, make_pr, make_ref, read_status

GATE_MERGE_POLICY = pc.Policy("test/gate-merge", (
    pc.Rule("github-pr-merge", pc.GITHUB_PR_MERGE, "merge", pc.REQUIRE_APPROVAL),
))
REF_ID = "aar_1"


def _ticket(**kw):
    decision = pc.Decision(
        pc.REQUIRE_APPROVAL, pc.CODE_APPROVAL_PENDING, "x", "v1", "github-pr-merge",
        "sha256:" + "0" * 64, approval_ref=REF_ID,
    )
    request = pc.ActionRequest(pc.GITHUB_PR_MERGE, "merge", PR_URL, "sha256:" + "1" * 64)
    record = ApprovalRecord(
        id=REF_ID, state="pending", intent_hash="sha256:" + "a" * 64, expires_at="2026-09-24T00:00:00Z",
    )
    return ticket_from(decision, request, record, artifact_ref=kw.pop("artifact_ref", HEAD_SHA))


class _ScriptedLookup:
    """An `ApprovalLookup` whose answer depends only on whether a receipt is
    named: a fresh ask (`approval_ref=""`) always opens `PENDING` naming
    `ref`; a revalidation (`approval_ref=ref`) answers `outcome`."""

    def __init__(self, ref: str, outcome: pc.ApprovalOutcome) -> None:
        self.ref = ref
        self.outcome = outcome
        self.calls: list[str] = []

    def redeem(self, request, *, rule_id, policy_version, approval_ref=""):
        self.calls.append(approval_ref)
        if approval_ref:
            return self.outcome
        return pc.ApprovalOutcome(pc.APPROVAL_PENDING, approval_ref=self.ref, reason="pending")


def _gate(monkeypatch: pytest.MonkeyPatch, lookup) -> None:
    real = pc.checkpoint
    monkeypatch.setattr(pc, "checkpoint", functools.partial(real, policy=GATE_MERGE_POLICY, approvals=lookup))


def _no_op_transport(monkeypatch: pytest.MonkeyPatch, *, merged: bool = True, merge_commit: str | None = "m" * 40):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_snapshot(*_a, **_kw):
        return SimpleNamespace(merge_commit=merge_commit, merged=merged)

    monkeypatch.setattr(run_shepherd.subprocess, "run", fake_run)
    monkeypatch.setattr(run_shepherd, "refresh_github_token", lambda: None)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", fake_snapshot)
    return calls


@pytest.fixture(autouse=True)
def _no_execution_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MCTL_EXECUTION_CONTEXT_FILE", "MCTL_REQUIRE_EXECUTION_CONTEXT", pc.APPROVALS_ENV):
        monkeypatch.delenv(name, raising=False)


def _stub_get_client(monkeypatch: pytest.MonkeyPatch, answer: ApprovalAnswer) -> None:
    class _Client:
        def get(self, approval_id):
            return answer

    monkeypatch.setattr(run_shepherd, "ActionApprovalClient", lambda *a, **kw: _Client())


def test_park_persists_ticket_logs_and_does_not_merge(tmp_path, monkeypatch, capsys):
    """T4 tick 1: a REQUIRE_APPROVAL decision parks instead of blocking."""
    ref = make_ref(tmp_path)
    pr = make_pr()
    calls = _no_op_transport(monkeypatch)
    _gate(monkeypatch, _ScriptedLookup(REF_ID, pc.ApprovalOutcome(pc.APPROVAL_PENDING)))
    record = ApprovalRecord(
        id=REF_ID, state="pending", intent_hash="sha256:" + "a" * 64, expires_at="2026-09-24T00:00:00Z",
    )
    _stub_get_client(monkeypatch, ApprovalAnswer("pending", record=record))

    assert run_shepherd.merge_pr(pr, ref) == (False, None)

    assert calls == []
    out = capsys.readouterr().out
    assert "APPROVAL_PARKED" in out and REF_ID in out
    ticket, denials, attempt = proposal_state.read_approval(read_status(ref))
    assert ticket is not None and ticket.approval_ref == REF_ID and denials == 0 and attempt == 0
    assert ticket.target == PR_URL


def test_resume_after_approval_merges_once_and_clears_ticket(tmp_path, monkeypatch, capsys):
    """T4 tick 2: a granted decision merges exactly once and clears the ticket."""
    ref = make_ref(tmp_path)
    pr = make_pr()
    calls = _no_op_transport(monkeypatch)
    outcome = pc.ApprovalOutcome(pc.APPROVAL_GRANTED, approval_ref=REF_ID, decided_by="github:root")
    lookup = _ScriptedLookup(REF_ID, outcome)
    _gate(monkeypatch, lookup)
    run_shepherd.update_status(ref, ref.status, approval=proposal_state.approval_payload(_ticket()))

    assert run_shepherd.merge_pr(pr, ref) == (True, "m" * 40)

    assert len(calls) == 1 and calls[0][:3] == ["gh", "pr", "merge"]
    assert lookup.calls == [REF_ID]  # revalidated the SAME receipt, never a fresh ask
    assert "APPROVAL_RESUMED" in capsys.readouterr().out
    ticket_after, _denials, _attempt = proposal_state.read_approval(read_status(ref))
    assert ticket_after is None


@pytest.mark.parametrize("cap_hit", [False, True])
def test_denied_increments_denials_and_caps_at_needs_triage(tmp_path, monkeypatch, capsys, cap_hit):
    ref = make_ref(tmp_path)
    pr = make_pr()
    _no_op_transport(monkeypatch)
    outcome = pc.ApprovalOutcome(pc.APPROVAL_DENIED, approval_ref=REF_ID, decided_by="github:root")
    _gate(monkeypatch, _ScriptedLookup(REF_ID, outcome))
    starting_denials = run_shepherd.MERGE_APPROVAL_DENIAL_LIMIT - 1 if cap_hit else 0
    payload = proposal_state.approval_payload(_ticket(), denials=starting_denials)
    run_shepherd.update_status(ref, ref.status, approval=payload)

    assert run_shepherd.merge_pr(pr, ref) == (False, None)

    data = read_status(ref)
    ticket_after, denials, _attempt = proposal_state.read_approval(data)
    if cap_hit:
        assert data["status"] == "needs-triage"
        assert data["failure"]["code"] == "approval-denied"
        assert ticket_after is None
        assert "action=needs-triage" in capsys.readouterr().out
    else:
        assert data["status"] == ref.status
        assert ticket_after is not None and ticket_after.approval_ref == REF_ID
        assert denials == starting_denials + 1


def test_expired_clears_ticket_and_bumps_the_attempt(tmp_path, monkeypatch):
    ref = make_ref(tmp_path)
    pr = make_pr()
    _no_op_transport(monkeypatch)
    outcome = pc.ApprovalOutcome(pc.APPROVAL_EXPIRED, approval_ref=REF_ID)
    _gate(monkeypatch, _ScriptedLookup(REF_ID, outcome))
    run_shepherd.update_status(ref, ref.status, approval=proposal_state.approval_payload(_ticket(), attempt=0))

    assert run_shepherd.merge_pr(pr, ref) == (False, None)

    ticket_after, denials, attempt = proposal_state.read_approval(read_status(ref))
    assert ticket_after is None and denials == 0 and attempt == 1


def test_intent_mismatch_clears_the_ticket_without_bumping_attempt(tmp_path, monkeypatch):
    ref = make_ref(tmp_path)
    pr = make_pr()
    _no_op_transport(monkeypatch)
    outcome = pc.ApprovalOutcome(pc.APPROVAL_MISMATCH, approval_ref=REF_ID)
    _gate(monkeypatch, _ScriptedLookup(REF_ID, outcome))
    run_shepherd.update_status(ref, ref.status, approval=proposal_state.approval_payload(_ticket(), attempt=2))

    assert run_shepherd.merge_pr(pr, ref) == (False, None)

    ticket_after, denials, attempt = proposal_state.read_approval(read_status(ref))
    assert ticket_after is None and denials == 0 and attempt == 0


def test_consumed_reconciles_an_already_merged_pr_without_remerging(tmp_path, monkeypatch, capsys):
    """A crash between consume and the side effect: the receipt is spent,
    but GitHub shows the merge landed anyway. `gh pr merge` must never run
    a second time."""
    ref = make_ref(tmp_path)
    pr = make_pr()
    calls = _no_op_transport(monkeypatch, merged=True, merge_commit="m" * 40)
    outcome = pc.ApprovalOutcome(pc.APPROVAL_CONSUMED, approval_ref=REF_ID)
    _gate(monkeypatch, _ScriptedLookup(REF_ID, outcome))
    run_shepherd.update_status(ref, ref.status, approval=proposal_state.approval_payload(_ticket()))

    assert run_shepherd.merge_pr(pr, ref) == (True, "m" * 40)

    assert calls == []  # never re-ran `gh pr merge`
    ticket_after, _denials, _attempt = proposal_state.read_approval(read_status(ref))
    assert ticket_after is None
    assert "reconciled=merged" in capsys.readouterr().out


def test_consumed_but_not_merged_clears_the_ticket_and_does_not_merge(tmp_path, monkeypatch):
    """The rare, accepted safe failure (ADR 014 §6): the approval was spent
    but the effect never ran. The ticket clears so a later tick can request
    a fresh approval; `gh pr merge` never runs on the spent receipt."""
    ref = make_ref(tmp_path)
    pr = make_pr()
    calls = _no_op_transport(monkeypatch, merged=False, merge_commit=None)
    outcome = pc.ApprovalOutcome(pc.APPROVAL_CONSUMED, approval_ref=REF_ID)
    _gate(monkeypatch, _ScriptedLookup(REF_ID, outcome))
    run_shepherd.update_status(ref, ref.status, approval=proposal_state.approval_payload(_ticket()))

    assert run_shepherd.merge_pr(pr, ref) == (False, None)

    assert calls == []
    ticket_after, _denials, _attempt = proposal_state.read_approval(read_status(ref))
    assert ticket_after is None


def test_lookup_error_leaves_the_ticket_untouched(tmp_path, monkeypatch):
    """A store outage is undecided, not the item's failure: the ticket stays
    exactly as it was, so the receipt's own deadline decides its fate."""
    ref = make_ref(tmp_path)
    pr = make_pr()
    _no_op_transport(monkeypatch)

    class _RaisingLookup:
        def redeem(self, *_a, **_kw):
            raise RuntimeError("mctl-api unreachable")

    _gate(monkeypatch, _RaisingLookup())
    before = proposal_state.approval_payload(_ticket(), denials=1, attempt=1)
    run_shepherd.update_status(ref, ref.status, approval=before)

    assert run_shepherd.merge_pr(pr, ref) == (False, None)

    assert read_status(ref)["approval"] == before


def test_default_off_with_a_ref_still_makes_no_http_call_and_parks_nothing(tmp_path, monkeypatch, capsys):
    """T7: `MCTL_POLICY_APPROVALS` unset — even with a `ref` in hand — is
    byte-identical to pre-#198 behaviour: `REQUIRE_APPROVAL` just blocks."""
    ref = make_ref(tmp_path)
    pr = make_pr()
    calls = _no_op_transport(monkeypatch)
    real = pc.checkpoint
    monkeypatch.setattr(pc, "checkpoint", functools.partial(real, policy=GATE_MERGE_POLICY))

    with patch.object(run_shepherd, "ActionApprovalClient") as client_cls:
        assert run_shepherd.merge_pr(pr, ref) == (False, None)
        client_cls.assert_not_called()

    assert calls == []
    assert "APPROVAL_PARKED" not in capsys.readouterr().out
    ticket, denials, attempt = proposal_state.read_approval(read_status(ref))
    assert ticket is None and denials == 0 and attempt == 0


def test_approvals_for_attempt_returns_none_when_the_store_cannot_be_built(monkeypatch):
    """`_approvals_for_attempt` never raises: an unbuildable store falls
    through to `None`, which leaves `checkpoint()` to fall back to its own
    default handling."""
    def _boom():
        raise RuntimeError("misconfigured")

    monkeypatch.setattr(pc, "configured_approvals", _boom)

    assert run_shepherd._approvals_for_attempt(3) is None


def test_approvals_for_attempt_passes_through_a_store_without_the_hook(monkeypatch):
    """A store with no `for_attempt` method (e.g. `NO_APPROVALS`) is
    returned unchanged -- attempt-scoping is opt-in per store, not assumed."""
    monkeypatch.setattr(pc, "configured_approvals", lambda: pc.NO_APPROVALS)

    assert run_shepherd._approvals_for_attempt(5) is pc.NO_APPROVALS


def test_approvals_for_attempt_isolates_a_denied_prior_attempt_from_the_next(monkeypatch):
    """mctl-agents#198: the whole point of the attempt bump. mctl-api answers
    a replayed idempotency key with whatever is stored under it
    (`action_approvals.idempotency_key`'s documented behaviour), so attempt
    0's denied receipt must never be echoed back to attempt 1 -- attempt 1
    needs, and gets, its own fresh, pending request bound to a new key.
    Replaying the SAME attempt then finds that SAME fresh receipt rather
    than opening a second one."""
    from orchestrator import action_approvals

    class _KeyedClient:
        """One receipt per idempotency key -- exactly mctl-api's replay rule
        that make the attempt bump necessary in the first place."""

        def __init__(self) -> None:
            self._by_key: dict[str, ApprovalRecord] = {}

        def create(self, intent, *, key, expires_at):
            rec = self._by_key.get(key)
            if rec is None:
                rec = ApprovalRecord(
                    id=f"aar_{key.rsplit('/', 1)[-1]}",
                    state="pending",
                    intent_hash=action_approvals.intent_hash(intent),
                    expires_at="2099-01-01T00:00:00Z",
                )
                self._by_key[key] = rec
            return ApprovalAnswer(rec.state, record=rec)

    client = _KeyedClient()
    monkeypatch.setenv(pc.APPROVALS_ENV, pc.APPROVALS_MCTL_API)
    monkeypatch.setattr(action_approvals, "ActionApprovalClient", lambda *a, **kw: client)

    request = pc.ActionRequest(
        pc.GITHUB_PR_MERGE, "merge", PR_URL, "sha256:" + "1" * 64,
        execution_id="exec1", trace_id="trace1", actor="human:x",
    )
    intent = action_approvals.intent_for(request, rule_id="github-pr-merge", policy_version="v1")
    denied_key = action_approvals.idempotency_key(intent, 0)
    client._by_key[denied_key] = ApprovalRecord(
        id="aar_denied0", state="denied", intent_hash=action_approvals.intent_hash(intent),
        expires_at="2099-01-01T00:00:00Z", decided_by="github:root",
    )

    # Attempt 0 finds the prior (denied) receipt stored under its key.
    outcome0 = run_shepherd._approvals_for_attempt(0).redeem(
        request, rule_id="github-pr-merge", policy_version="v1",
    )
    assert outcome0.status == pc.APPROVAL_DENIED and outcome0.approval_ref == "aar_denied0"

    # Attempt 1 must NOT see attempt 0's denial -- it gets a fresh, pending
    # receipt under a different key.
    outcome1 = run_shepherd._approvals_for_attempt(1).redeem(
        request, rule_id="github-pr-merge", policy_version="v1",
    )
    assert outcome1.status == pc.APPROVAL_PENDING
    assert outcome1.approval_ref != "aar_denied0"

    # A second ask at the SAME attempt finds the SAME fresh receipt it just
    # opened, never a third one.
    outcome1_again = run_shepherd._approvals_for_attempt(1).redeem(
        request, rule_id="github-pr-merge", policy_version="v1",
    )
    assert outcome1_again.approval_ref == outcome1.approval_ref


def test_no_raw_arguments_leak_into_the_persisted_ticket(tmp_path, monkeypatch):
    """T11: nothing in the persisted ticket is a raw action argument."""
    ref = make_ref(tmp_path)
    pr = make_pr()
    _no_op_transport(monkeypatch)
    _gate(monkeypatch, _ScriptedLookup(REF_ID, pc.ApprovalOutcome(pc.APPROVAL_PENDING)))
    record = ApprovalRecord(
        id=REF_ID, state="pending", intent_hash="sha256:" + "a" * 64, expires_at="2026-09-24T00:00:00Z",
    )
    _stub_get_client(monkeypatch, ApprovalAnswer("pending", record=record))

    run_shepherd.merge_pr(pr, ref)

    payload = read_status(ref)["approval"]["ticket"]
    assert "delete_branch" not in payload and "match_head_commit" not in payload and "method" not in payload
