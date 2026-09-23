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
activity returns it after the JSON round trip through the workflow: a bare id
per loop without an alias, a `{workflow_id, issue_workflow_id}` dict per loop
with one.
"""
from __future__ import annotations

import dataclasses
import logging
import typing
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio.api.workflow.v1 import WorkflowExecutionInfo
from temporalio.client import WorkflowExecution
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
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


def _loop(workflow_id: str, alias: str) -> dict[str, str]:
    return {"workflow_id": workflow_id, "issue_workflow_id": alias}


DISPATCHED_LOOP = _loop(DISPATCHED, ISSUE_KEYED)
#: A second execution request for the same issue.
DISPATCHED_2 = dispatched_workflow_id("xr_47400000-0000-4000-8000-000000000475")


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

        assert loops == [DISPATCHED_LOOP, OTHER_ISSUE]
        assert listing.queries == [ACTIVE_DEV_LOOPS_QUERY]

    async def test_a_dispatched_loop_without_the_memo_is_listed_and_reported(self, env, caplog):
        """A loop started before the memo existed: listed by its real id, no
        alias, and said out loud rather than guessed at."""
        listing = _ListClient([await _memo_for(DISPATCHED, None)])

        with caplog.at_level(logging.WARNING):
            loops = await env.run(VisibilityActivities(listing).list_active_dev_loop_ids)  # type: ignore[arg-type]

        assert loops == [DISPATCHED]
        assert any(DISPATCHED in r.getMessage() for r in caplog.records)

    async def test_the_listing_is_plain_strings_without_a_memo(self, env):
        """The rolling-deploy guarantee: with no dispatched loop running, the
        payload is exactly the pre-#474 `list[str]`."""
        listing = _ListClient([await _memo_for(ISSUE_KEYED, None), await _memo_for(OTHER_ISSUE, None)])

        loops = await env.run(VisibilityActivities(listing).list_active_dev_loop_ids)  # type: ignore[arg-type]

        assert loops == [ISSUE_KEYED, OTHER_ISSUE]
        assert all(isinstance(e, str) for e in loops)

    async def test_an_unreadable_memo_is_logged_with_its_reason(self, env, caplog):
        row = await _memo_for(DISPATCHED, None)

        async def _boom(*_a, **_k):
            raise ApplicationError("codec exploded")

        row.memo_value = _boom  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING):
            loops = await env.run(VisibilityActivities(_ListClient([row])).list_active_dev_loop_ids)  # type: ignore[arg-type]

        assert loops == [DISPATCHED]
        assert any("codec exploded" in r.getMessage() for r in caplog.records)


def _hint(fn) -> Any:
    return typing.get_type_hints(fn)["active_workflow_ids"]


_CONSUMERS = [
    stranded_act.find_stranded_accepted,
    orphans_act.detect_orphans,
    lr_act.reconcile_lifecycle_ownership,
]


class TestTheWireShape:
    """Decoded by temporalio's own payload converter against the declared
    parameter types, which is what a worker does before an activity body runs."""

    def _roundtrip(self, value: Any, hint: Any) -> Any:
        pc = DataConverter.default.payload_converter
        return pc.from_payloads(pc.to_payloads([value]), [hint])[0]

    @pytest.mark.parametrize("consumer", _CONSUMERS, ids=lambda f: f.__name__)
    def test_a_mixed_list_decodes_under_the_new_consumer_signatures(self, consumer):
        value = [ISSUE_KEYED, DISPATCHED_LOOP]
        assert self._roundtrip(value, _hint(consumer)) == value

    def test_a_plain_string_payload_decodes_against_the_old_annotation(self):
        """An old worker's consumer declares `list[str]`."""
        assert self._roundtrip([ISSUE_KEYED, OTHER_ISSUE], list[str]) == [ISSUE_KEYED, OTHER_ISSUE]

    def test_a_dict_does_not_decode_against_the_old_annotation(self):
        """Why the listing emits a dict ONLY for a loop with an alias."""
        with pytest.raises(TypeError):
            self._roundtrip([DISPATCHED_LOOP], list[str])


class TestTheIndex:
    def test_the_issue_keyed_loop_answers_for_itself(self):
        assert active_loops.index([ISSUE_KEYED]).owners_of(ISSUE_KEYED) == (ISSUE_KEYED,)

    def test_a_dispatched_loop_answers_by_its_real_id(self):
        assert active_loops.index([DISPATCHED_LOOP]).owners_of(ISSUE_KEYED) == (DISPATCHED,)

    def test_the_alias_is_not_an_id(self):
        """The whole point: the alias must not appear where real ids do."""
        active = active_loops.index([DISPATCHED_LOOP])
        assert ISSUE_KEYED not in active.ids

    def test_both_running_lists_the_issue_keyed_loop_first(self):
        active = active_loops.index([DISPATCHED_LOOP, ISSUE_KEYED])
        assert active.owners_of(ISSUE_KEYED) == (ISSUE_KEYED, DISPATCHED)

    def test_empty_entries_name_no_loop(self):
        active = active_loops.index([ISSUE_KEYED, "", None])
        assert active.owners_of(ISSUE_KEYED) == (ISSUE_KEYED,)
        assert active.ids == frozenset({ISSUE_KEYED})
        assert active.unreadable == 0

    @pytest.mark.parametrize(
        "bad",
        [{"workflow_id": ""}, {"issue_workflow_id": ISSUE_KEYED}, {"workflow_id": 7}, 42, ["x"]],
    )
    def test_an_unrecognised_entry_is_counted_not_dropped(self, bad):
        active = active_loops.index([ISSUE_KEYED, bad])
        assert active.unreadable == 1
        assert active.owners_of(ISSUE_KEYED) == (ISSUE_KEYED,)

    def test_the_index_is_frozen_all_the_way_down(self):
        active = active_loops.index([DISPATCHED_LOOP, ISSUE_KEYED])
        assert hash(active) == hash(active_loops.index([ISSUE_KEYED, DISPATCHED_LOOP]))

    def test_nothing_owns_a_slug_without_an_issue(self):
        assert active_loops.index([DISPATCHED_LOOP]).owners_of(None) == ()

    @pytest.mark.parametrize("entry", [DISPATCHED, {"workflow_id": DISPATCHED, "issue_workflow_id": ""}])
    def test_a_dispatched_loop_without_an_alias_is_unattributable(self, entry):
        """What the listing emits when the memo is missing or unreadable: a
        real id (still found by id) that may own any proposal."""
        active = active_loops.index([entry])
        assert DISPATCHED in active.ids
        assert active.unreadable == 1

    def test_an_issue_keyed_bare_id_is_not_unattributable(self):
        assert active_loops.index([ISSUE_KEYED, OTHER_ISSUE]).unreadable == 0

    def test_two_dispatched_loops_on_one_issue_are_both_owners(self):
        active = active_loops.index([_loop(DISPATCHED_2, ISSUE_KEYED), DISPATCHED_LOOP])
        assert active.owners_of(ISSUE_KEYED) == tuple(sorted((DISPATCHED, DISPATCHED_2)))


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
        result = await _sweep(env, monkeypatch, [ISSUE_KEYED])
        assert result.stranded == []
        assert ISSUE_KEYED in result.skipped[0][1]

    async def test_a_dispatched_loop_with_no_readable_memo_fails_the_sweep_closed(
        self, env, monkeypatch, caplog
    ):
        """The one unattributable input the listing really produces: a bare
        `dev-loop-xr_*` id. It may be this proposal's loop."""
        with caplog.at_level(logging.WARNING):
            result = await _sweep(env, monkeypatch, [DISPATCHED])
        assert result.stranded == []
        assert "ownership unknown" in result.skipped[0][1]
        # Said in the log, not only inside `skipped`.
        assert any("could not be attributed" in r.getMessage() for r in caplog.records)

    async def test_an_issueless_slug_is_not_held_back_by_an_unattributed_loop(self, env, monkeypatch):
        """A slug with no `issue-<N>-` prefix can never have a loop, so an
        unattributed dispatched loop cannot own it: it stays a candidate."""
        async def _refs():
            return [dataclasses.replace(_accepted(), slug="incident-2026-09-23-outage")]

        monkeypatch.setattr(stranded_act, "list_proposal_refs", _refs)
        result = await env.run(stranded_act.find_stranded_accepted, [DISPATCHED], 20)
        assert [p.slug for p in result.stranded] == ["incident-2026-09-23-outage"]

    async def test_two_loops_on_one_issue_are_both_named(self, env, monkeypatch):
        result = await _sweep(env, monkeypatch, [_loop(DISPATCHED_2, ISSUE_KEYED), DISPATCHED_LOOP])
        assert result.stranded == []
        reason = result.skipped[0][1]
        assert DISPATCHED in reason and DISPATCHED_2 in reason

    async def test_an_unreadable_entry_fails_the_sweep_closed(self, env, monkeypatch):
        """It may be the loop that owns this proposal."""
        result = await _sweep(env, monkeypatch, [{"workflow_id": DISPATCHED, "issue_workflow_id": 7}])
        assert result.stranded == []
        assert "ownership unknown" in result.skipped[0][1]

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
        result = await _orphans_from_github(env, monkeypatch, [ISSUE_KEYED])
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


async def _reconcile(monkeypatch, owner_id: str | None, active: list[Any]):
    import json

    import httpx

    from orchestrator.temporal.activities import lifecycle as ownership_act

    real_client = httpx.AsyncClient
    ownership = {PR_ID: _record(owner_id)} if owner_id else {}
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
        finding, _ = await _reconcile(monkeypatch, ISSUE_KEYED, [ISSUE_KEYED])
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

    async def test_a_dispatched_only_live_loop_needs_an_owner(self, monkeypatch):
        """`_live_id` also feeds `needs_owner`: a dispatched loop running with
        no ownership record is zero-owner live work, reported exactly as an
        issue-keyed loop's is (escalated, never adopted), not `no-work`."""
        finding, writes = await _reconcile(monkeypatch, None, [DISPATCHED_LOOP])
        assert finding.reason == "zero-owner-live-worker"
        assert DISPATCHED in finding.evidence
        assert writes == []

    async def test_two_live_loops_escalate_naming_both(self, monkeypatch):
        """The record names one of two dispatched loops for the issue. Two
        loops on one entity is a conflict, and the evidence must name every
        candidate, not whichever id sorts first (which could be the owner)."""
        finding, writes = await _reconcile(
            monkeypatch, DISPATCHED, [DISPATCHED_LOOP, _loop(DISPATCHED_2, ISSUE_KEYED)]
        )
        assert finding.reason == "conflicting-owner"
        assert DISPATCHED in finding.evidence and DISPATCHED_2 in finding.evidence
        assert writes == []

    async def test_with_no_live_loop_it_stays_no_work(self, monkeypatch):
        finding, _ = await _reconcile(monkeypatch, None, [])
        assert finding.reason == "no-work"


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
        async def listing() -> list[active_loops.ActiveLoopEntry]:
            # Mixed, as a real listing with one dispatched loop is: decoded by
            # the worker against the real scan's declared signature.
            return [OTHER_ISSUE, DISPATCHED_LOOP]

        fakes, received = _fake_activities()
        # Keep the fake counter, submit and record; swap in the new listing
        # shape and the REAL scan.
        activities = [listing, fakes[1], stranded_act.find_stranded_accepted, fakes[3], fakes[4]]

        async with await WorkflowEnvironment.start_time_skipping() as env:
            result = await _run(env, activities)

        assert received["submits"] == []
        assert result.submitted == 0


# --------------------------------------------------------------------------
# The alias across a merge-watch continue_as_new.
# --------------------------------------------------------------------------


class TestTheAliasSurvivesContinueAsNew:
    """`_watch_pr` hops via continue_as_new under the same workflow id. The
    memo is carried explicitly at both hop sites rather than trusted to the
    server; without it the post-hop run would list as a bare `dev-loop-xr_*`
    id, which `index` counts as unattributable and which then holds back the
    whole implement sweep for as long as the loop watches its PR.

    Driven the way `TestMergeWatchContinueAsNew` in test_dev_loop_workflow.py
    forces a hop: `MERGE_WATCH_HISTORY_FLOOR` low, an unsandboxed worker.

    What this pins is the end-to-end guarantee, not the `memo=` line alone:
    temporalio 1.31's core also re-uses the current memo when a
    continue_as_new command leaves it unset ("If unset, re-uses the current
    workflow's memo", workflow_commands.proto), so dropping the explicit
    `memo=` still passes here. The explicit carry is kept so the guarantee
    does not rest on that default alone; this test is what fails if both
    ever stop carrying it."""

    async def _run_with_hops(self, monkeypatch, memo: dict[str, str] | None):
        import uuid

        import anyio
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import UnsandboxedWorkflowRunner

        from orchestrator.temporal.activities.pr_state import PRState
        from orchestrator.temporal.workflows import dev_loop
        from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef
        from tests.temporal_harness import Worker
        from tests.test_dev_loop_workflow import MERGED_PR, TASK_QUEUE, _fake_activities

        monkeypatch.setattr(dev_loop, "MERGE_WATCH_HISTORY_FLOOR", 1)
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
            head_sha="deadbeef",
        )
        activities, _calls, investigate_ran, _ops = _fake_activities(
            released=True, pr_states=[open_pr, open_pr, MERGED_PR]
        )
        # A `dev-loop-xr_*` id, as the dispatcher starts it; the unique suffix
        # keeps the test server's id reuse policy out of the picture.
        workflow_id = f"{DISPATCHED}-{uuid.uuid4().hex[:8]}"
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[DevLoopWorkflow],
                activities=activities,
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = await env.client.start_workflow(
                    DevLoopWorkflow.run,
                    IssueRef(issue_url=ISSUE_URL),
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    memo=memo,
                )
                first_run = handle.result_run_id
                with anyio.fail_after(30):
                    await investigate_ran.wait()
                await handle.signal(DevLoopWorkflow.approve, {"approver": "alice"})
                with anyio.fail_after(30):
                    result = await handle.result()
                # The LATEST run of the chain, as visibility lists it.
                latest = await env.client.get_workflow_handle(workflow_id).describe()
        assert result.pr is not None and result.pr.state == "MERGED"
        assert latest.run_id != first_run, "no merge-watch hop happened; the test proves nothing"
        return workflow_id, latest

    async def test_a_dispatched_loop_keeps_its_alias_after_a_hop(self, monkeypatch):
        memo = {active_loops.ISSUE_WORKFLOW_ID_MEMO: ISSUE_KEYED}
        workflow_id, latest = await self._run_with_hops(monkeypatch, memo)

        assert await latest.memo_value(active_loops.ISSUE_WORKFLOW_ID_MEMO, "") == ISSUE_KEYED
        # And the listing, fed the continued run, still emits the alias.
        loops = await ActivityEnvironment().run(
            VisibilityActivities(_ListClient([latest])).list_active_dev_loop_ids  # type: ignore[arg-type]
        )
        assert loops == [{"workflow_id": workflow_id, "issue_workflow_id": ISSUE_KEYED}]

    async def test_a_loop_without_a_memo_gains_none_across_a_hop(self, monkeypatch):
        """The unchanged-command half: an issue-keyed loop carries nothing."""
        _workflow_id, latest = await self._run_with_hops(monkeypatch, None)
        assert await latest.memo() == {}
