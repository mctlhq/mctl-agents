"""Dispatched `dev-loop-xr_*` loops are visible to the proposal sweeps
(mctlhq/mctl-agents#474).

The implement sweep, the orphan sweep and the lifecycle reconcile each rebuild
the issue-keyed `dev-loop-mctlhq-<service>-<n>` from a proposal slug and look
it up among running loops. A loop the execution-request dispatcher started is
`dev-loop-xr_<request id>`, so none of them saw it. The fix records the
issue-keyed id as a memo at the dispatcher's start, returns it from the
listing as an alias, and has every consumer match on the alias while reporting
the real id.

Every consumer test below builds the active set exactly as the listing
activity returns it after the JSON round trip through the workflow: a list of
`{workflow_id, issue_workflow_id}` dicts.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio.api.workflow.v1 import WorkflowExecutionInfo
from temporalio.client import WorkflowExecution
from temporalio.converter import DataConverter
from temporalio.testing import ActivityEnvironment

from orchestrator.lifecycle import rollout
from orchestrator.proposal_state import AUTHORIZATION_HUMAN_APPROVAL
from orchestrator.temporal import active_loops
from orchestrator.temporal.activities import lifecycle_reconcile as lr_act
from orchestrator.temporal.activities import orphans as orphans_act
from orchestrator.temporal.activities import stranded as stranded_act
from orchestrator.temporal.activities.gitops_state import ProposalStateRef, PRSnapshot
from orchestrator.temporal.activities.visibility import ACTIVE_DEV_LOOPS_QUERY, VisibilityActivities
from orchestrator.temporal.issue_ref import dispatched_workflow_id

pytestmark = pytest.mark.anyio

ISSUE_URL = "https://github.com/mctlhq/mctl-web/issues/10"
SLUG = "issue-10-widget"
ISSUE_KEYED = "dev-loop-mctlhq-mctl-web-10"
XR = "xr_47400000-0000-4000-8000-000000000474"
DISPATCHED = dispatched_workflow_id(XR)
OTHER_ISSUE = "dev-loop-mctlhq-mctl-web-99"
REPO = "mctlhq/mctl-web"
PR_ID = f"{REPO}#42"


def _loop(workflow_id: str, alias: str = "") -> dict[str, str]:
    return {"workflow_id": workflow_id, "issue_workflow_id": alias}


DISPATCHED_LOOP = _loop(DISPATCHED, ISSUE_KEYED)


@pytest.fixture
def env():
    return ActivityEnvironment()


# --------------------------------------------------------------------------
# The producer: the memo at start, read back by the listing.
# --------------------------------------------------------------------------


class _StartClient:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def start_workflow(self, *args: Any, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return "handle"


async def _memo_for(workflow_id: str, memo: dict[str, Any] | None) -> WorkflowExecution:
    """A listing row exactly as the SDK builds one from the visibility store."""
    info = WorkflowExecutionInfo()
    info.execution.workflow_id = workflow_id
    info.execution.run_id = "run-1"
    info.type.name = "DevLoopWorkflow"
    if memo is not None:
        info.memo.CopyFrom(await DataConverter.default._encode_memo(memo))
    return WorkflowExecution._from_raw_info(info, "mctl-agents", DataConverter.default)


class _ListClient:
    def __init__(self, rows: list[WorkflowExecution]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def list_workflows(self, query: str):
        self.queries.append(query)

        async def _gen():
            for row in self.rows:
                yield row

        return _gen()


class TestTheMemoRoundTrip:
    async def test_the_dispatched_start_records_the_issue_keyed_id_as_a_memo(self):
        from orchestrator.temporal.start import start_dispatched_dev_loop
        from orchestrator.temporal.workflows.dev_loop import IssueRef

        client = _StartClient()
        await start_dispatched_dev_loop(
            client,  # type: ignore[arg-type]
            IssueRef(issue_url=ISSUE_URL, work_item_id="wi_1", execution_request_id=XR),
        )

        assert client.kwargs["id"] == DISPATCHED
        assert client.kwargs["memo"] == {active_loops.ISSUE_WORKFLOW_ID_MEMO: ISSUE_KEYED}

    async def test_the_listing_reads_the_memo_the_start_writes(self, env):
        """Encoded and decoded by the SDK's own converter, not a mock: the
        key and the value shape the start writes are the ones the listing
        reads."""
        client = _StartClient()
        from orchestrator.temporal.start import start_dispatched_dev_loop
        from orchestrator.temporal.workflows.dev_loop import IssueRef

        await start_dispatched_dev_loop(
            client,  # type: ignore[arg-type]
            IssueRef(issue_url=ISSUE_URL, work_item_id="wi_1", execution_request_id=XR),
        )
        rows = [
            await _memo_for(DISPATCHED, client.kwargs["memo"]),
            await _memo_for(OTHER_ISSUE, None),
        ]
        listing = _ListClient(rows)

        loops = await env.run(VisibilityActivities(listing).list_active_dev_loop_ids)  # type: ignore[arg-type]

        assert loops == [DISPATCHED_LOOP, _loop(OTHER_ISSUE)]
        assert listing.queries == [ACTIVE_DEV_LOOPS_QUERY]

    async def test_a_dispatched_loop_without_the_memo_is_listed_and_reported(self, env, caplog):
        """A loop started before the memo existed: listed by its real id, no
        alias, and said out loud rather than guessed at."""
        listing = _ListClient([await _memo_for(DISPATCHED, None)])

        with caplog.at_level(logging.WARNING):
            loops = await env.run(VisibilityActivities(listing).list_active_dev_loop_ids)  # type: ignore[arg-type]

        assert loops == [_loop(DISPATCHED)]
        assert any(DISPATCHED in r.getMessage() for r in caplog.records)


class TestTheIndex:
    def test_the_issue_keyed_loop_answers_for_itself(self):
        assert active_loops.index([_loop(ISSUE_KEYED)]).owner_of(ISSUE_KEYED) == ISSUE_KEYED

    def test_a_dispatched_loop_answers_by_its_real_id(self):
        assert active_loops.index([DISPATCHED_LOOP]).owner_of(ISSUE_KEYED) == DISPATCHED

    def test_the_alias_is_not_an_id(self):
        """The whole point: the alias must not appear where real ids do."""
        active = active_loops.index([DISPATCHED_LOOP])
        assert ISSUE_KEYED not in active.ids

    def test_both_running_lists_the_issue_keyed_loop_first(self):
        active = active_loops.index([DISPATCHED_LOOP, _loop(ISSUE_KEYED)])
        assert active.owners_of(ISSUE_KEYED) == (ISSUE_KEYED, DISPATCHED)

    def test_bare_ids_from_a_result_recorded_before_474_still_index(self):
        active = active_loops.index([ISSUE_KEYED, "", None, {"workflow_id": ""}])
        assert active.owner_of(ISSUE_KEYED) == ISSUE_KEYED
        assert active.ids == frozenset({ISSUE_KEYED})

    def test_nothing_owns_a_slug_without_an_issue(self):
        assert active_loops.index([DISPATCHED_LOOP]).owner_of(None) == ""


# --------------------------------------------------------------------------
# Consumer 1: the implement sweep (the real hazard).
# --------------------------------------------------------------------------


def _accepted() -> ProposalStateRef:
    return ProposalStateRef(
        service="mctl-web",
        slug=SLUG,
        status="accepted",
        pr_url=None,
        updated_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        execution_authorization=AUTHORIZATION_HUMAN_APPROVAL,
    )


async def _sweep(env, monkeypatch, active: list[Any]):
    async def _refs():
        return [_accepted()]

    monkeypatch.setattr(stranded_act, "list_proposal_refs", _refs)
    return await env.run(stranded_act.find_stranded_accepted, active, 20)


class TestImplementSweep:
    async def test_a_running_dispatched_loop_blocks_the_sweep_for_its_proposal(self, env, monkeypatch):
        result = await _sweep(env, monkeypatch, [DISPATCHED_LOOP])

        assert result.stranded == [], "a second implementer run on a queued dispatched loop"
        key, reason = result.skipped[0]
        assert key == f"mctl-web/{SLUG}"
        # Reported by the id an operator can look up in Temporal.
        assert DISPATCHED in reason

    async def test_an_issue_keyed_loop_still_blocks_it(self, env, monkeypatch):
        result = await _sweep(env, monkeypatch, [_loop(ISSUE_KEYED)])
        assert result.stranded == []
        assert ISSUE_KEYED in result.skipped[0][1]

    async def test_a_bare_issue_keyed_id_still_blocks_it(self, env, monkeypatch):
        """A tick that recorded the old listing result before the deploy."""
        result = await _sweep(env, monkeypatch, [ISSUE_KEYED])
        assert result.stranded == []

    async def test_a_dispatched_loop_for_another_issue_does_not(self, env, monkeypatch):
        result = await _sweep(env, monkeypatch, [_loop(DISPATCHED, OTHER_ISSUE)])
        assert len(result.stranded) == 1


# --------------------------------------------------------------------------
# Consumer 2: the orphan sweep, both read paths.
# --------------------------------------------------------------------------


def _implemented() -> ProposalStateRef:
    return ProposalStateRef(
        service="mctl-web",
        slug=SLUG,
        status="implemented",
        pr_url=f"https://github.com/{REPO}/pull/42",
    )


def _open_pr() -> PRSnapshot:
    return PRSnapshot(repo=REPO, number=42, merged=False, closed_unmerged=False, head_sha="abc1234")


async def _orphans_from_github(env, monkeypatch, active: list[Any]):
    async def _refs():
        return [_implemented()]

    async def _snapshots(_refs_arg):
        return {("mctl-web", SLUG): _open_pr()}

    monkeypatch.setattr(orphans_act, "list_proposal_refs", _refs)
    monkeypatch.setattr(orphans_act, "fetch_pr_snapshots", _snapshots)
    return await env.run(orphans_act.detect_orphans, "", active)


async def _orphans_from_disk(env, monkeypatch, tmp_path, active: list[Any]):
    monkeypatch.setattr(orphans_act, "_discover_refs", lambda _dir, reconcile: [_implemented()])
    monkeypatch.setattr(orphans_act, "find_pr_for_proposal", lambda *_a, **_k: _open_pr())
    return await env.run(orphans_act.detect_orphans, str(tmp_path), active)


class TestOrphans:
    async def test_a_running_dispatched_loop_is_not_an_orphan(self, env, monkeypatch):
        result = await _orphans_from_github(env, monkeypatch, [DISPATCHED_LOOP])
        assert result.orphans == []

    async def test_nor_on_the_local_checkout_path(self, env, monkeypatch, tmp_path):
        result = await _orphans_from_disk(env, monkeypatch, tmp_path, [DISPATCHED_LOOP])
        assert result.orphans == []

    async def test_an_issue_keyed_loop_is_still_not_an_orphan(self, env, monkeypatch):
        result = await _orphans_from_github(env, monkeypatch, [_loop(ISSUE_KEYED)])
        assert result.orphans == []

    async def test_a_dispatched_loop_for_another_issue_leaves_it_an_orphan(self, env, monkeypatch):
        result = await _orphans_from_github(env, monkeypatch, [_loop(DISPATCHED, OTHER_ISSUE)])
        assert [o.slug for o in result.orphans] == [SLUG]


# --------------------------------------------------------------------------
# Consumer 3: the lifecycle reconcile's live id.
# --------------------------------------------------------------------------


def _record(owner_id: str) -> dict[str, Any]:
    return {
        "entity": {"kind": "pull-request", "id": PR_ID, "version": "abc1234"},
        "phase": "review-remediation",
        "owner": {"type": "devloop-workflow", "id": owner_id},
        # What a DevLoop writes on its rows: its own workflow id
        # (`info.workflow_id`), so a dispatched loop's rows name `dev-loop-xr_*`.
        "temporal_workflow_id": owner_id,
        "epoch": 3,
        "state": "active",
        "healthy": True,
        "derived": {"status": "healthy", "held": True},
    }


async def _reconcile(monkeypatch, owner_id: str, active: list[Any]):
    import json

    import httpx

    from orchestrator.temporal.activities import lifecycle as ownership_act

    real_client = httpx.AsyncClient
    ownership = {PR_ID: _record(owner_id)}
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            ids = request.url.params.get_list("id")
            found = {i: ownership[i] for i in ids if i in ownership}
            return httpx.Response(200, content=json.dumps({"ownership": found}).encode())
        writes.append(request.url.path)
        return httpx.Response(500)

    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setattr(lr_act, "auth_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(ownership_act, "auth_headers", lambda: {"Authorization": "Bearer test"})

    async def _refs():
        return [
            ProposalStateRef(
                service="mctl-web", slug=SLUG, status="in-progress", pr_url=f"https://github.com/{REPO}/pull/42"
            )
        ]

    async def _snapshots(_refs_arg):
        return {("mctl-web", SLUG): _open_pr()}

    monkeypatch.setattr(lr_act, "list_proposal_refs", _refs)
    monkeypatch.setattr(lr_act, "fetch_pr_snapshots", _snapshots)

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(lr_act.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(ownership_act.httpx, "AsyncClient", _factory)
    result = await ActivityEnvironment().run(lr_act.reconcile_lifecycle_ownership, active)
    finding = next(f for f in result.findings if f.entity_id == PR_ID)
    return finding, writes


class TestLifecycleReconcile:
    async def test_a_dispatched_owner_is_not_a_conflicting_owner(self, monkeypatch):
        """The record names `dev-loop-xr_*` and so must the live id: had the
        alias been put into the running set, this would escalate."""
        finding, writes = await _reconcile(monkeypatch, DISPATCHED, [DISPATCHED_LOOP])
        assert finding.action == "none", finding.evidence
        assert writes == []

    async def test_an_issue_keyed_owner_is_still_left_alone(self, monkeypatch):
        finding, _ = await _reconcile(monkeypatch, ISSUE_KEYED, [_loop(ISSUE_KEYED)])
        assert finding.action == "none"

    async def test_the_live_id_is_the_real_id(self):
        ref = ProposalStateRef(service="mctl-web", slug=SLUG, status="in-progress", pr_url=None)
        assert lr_act._live_id(ref, REPO, active_loops.index([DISPATCHED_LOOP])) == DISPATCHED

    async def test_a_record_naming_a_different_loop_still_conflicts(self, monkeypatch):
        """The alias match finds the dispatched loop; a record that names the
        issue-keyed loop while only the dispatched one runs is still two
        actors on one entity."""
        finding, _ = await _reconcile(monkeypatch, ISSUE_KEYED, [DISPATCHED_LOOP])
        assert finding.reason == "conflicting-owner"
        assert DISPATCHED in finding.evidence


# --------------------------------------------------------------------------
# End to end through the real sweep workflow: the listing's dict entries
# survive the workflow's by-name hand-off into the real scan activity.
# --------------------------------------------------------------------------


class TestThroughTheSweepWorkflow:
    async def test_a_dispatched_loop_starts_no_second_implementer(self, monkeypatch):
        from temporalio import activity
        from temporalio.testing import WorkflowEnvironment

        from tests.test_implement_sweep_workflow import _fake_activities, _run

        async def _refs():
            return [_accepted()]

        monkeypatch.setattr(stranded_act, "list_proposal_refs", _refs)

        @activity.defn(name="list_active_dev_loop_ids")
        async def listing() -> list[dict[str, str]]:
            return [DISPATCHED_LOOP]

        fakes, received = _fake_activities()
        # Keep the fake counter, submit and record; swap in the new listing
        # shape and the REAL scan.
        activities = [listing, fakes[1], stranded_act.find_stranded_accepted, fakes[3], fakes[4]]

        async with await WorkflowEnvironment.start_time_skipping() as env:
            result = await _run(env, activities)

        assert received["submits"] == []
        assert result.submitted == 0
