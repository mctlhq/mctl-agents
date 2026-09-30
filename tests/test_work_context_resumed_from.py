"""Which prior snapshot a resume points at (mctlhq/mctl-agents#542, found by
the #431 live proof): the execution the request resumes from, else the latest
prior execution that sealed one, walking past a prior only when the store
documents that it sealed nothing and its ledger records it."""
from __future__ import annotations

import pytest

from orchestrator import context_assembly as ca
from orchestrator import context_snapshot as cs
from orchestrator.run_issue_investigator import IssueData, IssueRef
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context import snapshots as ws
from orchestrator.work_context.contract import (
    WORK_ITEM_FOUND,
    WORK_ITEM_UNKNOWN,
    ExecutionRef,
    WorkItem,
    WorkItemAnswer,
)

WID = "wi_5c479148-7ffd-43bb-9c98-236431b6e97b"
E1 = "we_11111111-1111-4111-8111-111111111111"
E2 = "we_22222222-2222-4222-8222-222222222222"
E3 = "we_33333333-3333-4333-8333-333333333333"
UNRECORDED = "we_99999999-9999-4999-8999-999999999999"
C1 = "cs_7580bb2246ee3c81c76b74c0648e3436"
C0 = "cs_00000000000000000000000000000000"
XR = "xr_d3a5d338ccaa4d05a8514860d17a4805"


def _env(snap: dict) -> dict:
    return {"schema_version": "workitem/v1",
            "snapshot": {"schema_version": "workitem/v1", "work_item_id": WID, **snap}}


class _Store:
    """mctl-api's snapshot read, ledger read and execution-request read.

    `snapshots` maps an execution to its snapshot id; any other execution
    answers the store's own `snapshot_not_found`, recorded or not, which is
    exactly the ambiguity `resumed_from` must not resolve by guessing."""

    def __init__(self, snapshots, *, ledger=(E1, E2, E3), down=(), ledger_down=False, request=None):
        self.snapshots = dict(snapshots)
        self.ledger = tuple(ledger)
        self.down = set(down)
        self.ledger_down = ledger_down
        self.request = request
        self.asked: list[str] = []
        self.sealed: list[dict] = []

    def execution_snapshot(self, work_item_id, execution_id):
        self.asked.append(execution_id)
        if execution_id in self.down:
            return ws.answer_from_read(503, {"error": "down"}, work_item_id=WID, execution_id=execution_id)
        sid = self.snapshots.get(execution_id)
        if sid is None:
            return ws.answer_from_read(404, {"code": "snapshot_not_found"}, work_item_id=WID,
                                       execution_id=execution_id)
        snap = {"id": sid, "execution_id": execution_id, "content_hash": "sha256:" + "a" * 64}
        return ws.answer_from_read(200, _env(snap), work_item_id=WID, execution_id=execution_id)

    def get(self, work_item_id):
        if self.ledger_down:
            return WorkItemAnswer(verdict=WORK_ITEM_UNKNOWN, reason="down")
        executions = tuple(ExecutionRef(execution_id=e, sequence=i + 1) for i, e in enumerate(self.ledger))
        return WorkItemAnswer(verdict=WORK_ITEM_FOUND, item=WorkItem(work_item_id=WID, executions=executions))

    def execution_request(self, work_item_id, request_id):
        if self.request is None:
            return xr.RequestAnswer(xr.UNKNOWN, reason="down")
        return xr.RequestAnswer(xr.FOUND, request=self.request)

    def seal_snapshot(self, work_item_id, execution_id, body):
        self.sealed.append(body)
        snap = {"id": "cs_new", "execution_id": execution_id, "content_hash": body["content_hash"]}
        return ws.answer_from_seal(201, _env(snap), work_item_id=WID, content_hash=body["content_hash"],
                                   execution_id=execution_id)


def _wc(priors=(E1, E2), current=E3, sequence=3) -> cs.WorkContextRef:
    return cs.WorkContextRef(work_item_id=WID, work_item_revision="3", execution_id=current,
                             execution_sequence=sequence, prior_execution_ids=tuple(priors))


def _request(resumed_from: str, work_item_id: str = WID) -> xr.ExecutionRequest:
    return xr.ExecutionRequest(request_id=XR, work_item_id=work_item_id, kind="resume", state="claimed",
                               resumed_from_execution_id=resumed_from)


# -- resumed_from itself ----------------------------------------------------


def test_the_production_shape_resolves_c1_past_a_snapshotless_e2():
    """#431 proof v2: E1 sealed C1, E2 ran and sealed nothing, E3 resumes."""
    store = _Store({E1: C1})
    linked, answer = ws.resumed_from(_wc(), store)
    assert (answer.verdict, linked.resumed_from_snapshot_id) == (ws.SNAPSHOT_REPLAYED, C1)
    assert store.asked == [E2, E1]
    assert E1 in answer.reason


def test_the_requested_execution_wins_over_a_newer_sealed_one():
    store = _Store({E1: C1, E2: C0})
    linked, answer = ws.resumed_from(_wc(), store, requested_execution_id=E1)
    assert (answer.verdict, linked.resumed_from_snapshot_id) == (ws.SNAPSHOT_REPLAYED, C1)
    assert store.asked == [E1] and "requested execution" in answer.reason


def test_a_requested_execution_that_sealed_nothing_is_not_replaced_by_another():
    store = _Store({E1: C1})
    linked, answer = ws.resumed_from(_wc(), store, requested_execution_id=E2)
    assert (answer.verdict, linked.resumed_from_snapshot_id) == (ws.SNAPSHOT_ABSENT, None)
    assert store.asked == [E2]


@pytest.mark.parametrize("requested", [UNRECORDED, E3, "sha-local", ""])
def test_a_requested_execution_that_is_not_a_prior_is_ignored(requested):
    store = _Store({E1: C1, E2: C0})
    linked, _ = ws.resumed_from(_wc(), store, requested_execution_id=requested)
    assert linked.resumed_from_snapshot_id == C0 and store.asked == [E2]


def test_a_failed_lookup_on_e2_does_not_skip_to_e1():
    store = _Store({E1: C1}, down={E2})
    linked, answer = ws.resumed_from(_wc(), store)
    assert (answer.verdict, linked.resumed_from_snapshot_id) == (ws.SNAPSHOT_UNKNOWN, None)
    assert store.asked == [E2]


def test_an_unrecorded_prior_is_never_walked_past():
    """The store answers `snapshot_not_found` for an execution it never
    recorded too; walking past one would seal C2 naming C1 and the unknown
    prior would never reach the store's continuity check."""
    store = _Store({E1: C1}, ledger=(E1, E3))
    linked, answer = ws.resumed_from(_wc(priors=(E1, UNRECORDED)), store)
    assert (answer.verdict, linked.resumed_from_snapshot_id) == (ws.SNAPSHOT_ABSENT, None)
    assert store.asked == [UNRECORDED] and "not a recorded execution" in answer.reason


def test_an_unreadable_ledger_confirms_nothing():
    store = _Store({E1: C1}, ledger_down=True)
    linked, answer = ws.resumed_from(_wc(), store)
    assert (answer.verdict, linked.resumed_from_snapshot_id) == (ws.SNAPSHOT_ABSENT, None)
    assert store.asked == [E2]


def test_no_prior_sealed_anything_is_absent():
    store = _Store({})
    linked, answer = ws.resumed_from(_wc(), store)
    assert (answer.verdict, linked.resumed_from_snapshot_id) == (ws.SNAPSHOT_ABSENT, None)
    assert store.asked == [E2, E1]


def test_the_walk_is_bounded():
    priors = tuple(f"we_{i:08d}-0000-4000-8000-000000000000" for i in range(40))
    store = _Store({}, ledger=(*priors, E3))
    ws.resumed_from(_wc(priors=priors, sequence=41), store)
    assert len(store.asked) == ws.MAX_RESUME_LOOKUPS


# -- through the assembly entry point ----------------------------------------


def _assemble(tmp_path, store, **kw):
    issue = IssueData(
        ref=IssueRef(owner="mctlhq", repo="mctl-agents", number=494,
                     url="https://github.com/mctlhq/mctl-agents/issues/494"),
        title="t", body="b", state="CLOSED",
    )
    return ca.assemble_investigator_context(
        mode="shadow", issue=issue, issue_url=issue.ref.url, full_repo="mctlhq/mctl-agents",
        repo_dir=tmp_path / "repo", target_repo_sha="a" * 40, proposal_dir=tmp_path / "proposal",
        service="mctl-agents", slug="issue-494-x", prompt_template="PROMPT", resolver_mode="legacy",
        legacy_model="claude-sonnet-5-5", legacy_allowed_tools=("Read",), legacy_budget_usd=8.0,
        work_context=_wc(), work_item_client=store, **kw,
    )


@pytest.fixture
def observe(monkeypatch):
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "observe")
    monkeypatch.delenv("WORK_ITEM_INTENT_SOURCE", raising=False)


def test_the_request_names_the_resumed_execution_and_the_seal_names_its_snapshot(tmp_path, observe):
    store = _Store({E1: C1, E2: C0}, request=_request(E1))
    result = _assemble(tmp_path, store, execution_request_id=XR)
    assert result.snapshot.work_context.resumed_from_snapshot_id == C1
    [body] = store.sealed
    # Named alone, so the store resolves it to E1 instead of refusing an
    # (E2, C1) pair (mctl-api `checkPrior`).
    assert body["prior_snapshot_id"] == C1 and "prior_execution_id" not in body


def test_the_production_shape_through_assembly_with_no_request(tmp_path, observe):
    store = _Store({E1: C1})
    result = _assemble(tmp_path, store)
    assert result.snapshot.work_context.resumed_from_snapshot_id == C1
    [body] = store.sealed
    assert body["prior_snapshot_id"] == C1 and "prior_execution_id" not in body


@pytest.mark.parametrize("request_obj", [None, "foreign"])
def test_an_unreadable_or_foreign_request_falls_back_to_the_walk(tmp_path, observe, request_obj):
    request = _request(E1, work_item_id="wi_other") if request_obj == "foreign" else None
    store = _Store({E1: C1, E2: C0}, request=request)
    result = _assemble(tmp_path, store, execution_request_id=XR)
    assert result.snapshot.work_context.resumed_from_snapshot_id == C0


def test_a_failed_lookup_leaves_the_seal_naming_the_latest_prior(tmp_path, observe):
    store = _Store({E1: C1}, down={E2})
    result = _assemble(tmp_path, store)
    assert result.snapshot.work_context.resumed_from_snapshot_id is None
    [body] = store.sealed
    assert body["prior_execution_id"] == E2 and "prior_snapshot_id" not in body
