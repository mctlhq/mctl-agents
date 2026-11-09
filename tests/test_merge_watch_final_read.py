"""#1021 M2b-1d-2b-i (#1055): the merge watch's deadline decision is wired.

A fresh (patched) run that rides to the 14-day deadline reads the PR once more
before it lets go of the claim. These tests run on the time-skipping server
with a `get_pr_state` fake that answers OPEN until the watch deadline and a
chosen answer after it. The committed-fixture half of the slice (recorder,
D15b, D16) is 1d-2b-ii and is not here.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.workflows.dev_loop import (
    FINAL_READ_ATTEMPTS,
    MERGE_WATCH_RECONCILE_UNRECORDED,
    DevLoopWorkflow,
    IssueRef,
)
from tests.temporal_harness import Worker
from tests.test_dev_loop_workflow import MERGED_PR, _fake_activities

pytestmark = pytest.mark.anyio

HEAD = "abc1234def5678"
OPEN_PR = dataclasses.replace(MERGED_PR, state="OPEN", merged=False, head_sha=HEAD)


# The workflow sandbox re-imports the module, so the 14-day deadline cannot be
# patched; the time-skipping server fast-forwards the ~1344 polls instead.
DEADLINE = timedelta(days=14)


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@dataclasses.dataclass
class _Run:
    handle: Any
    events: list[Any]
    ops: list[Any]
    reads: list[datetime]
    cutoff: datetime
    result: Any = None
    failure: BaseException | None = None


async def _ride_to_deadline(env: WorkflowEnvironment, after_deadline: Any) -> _Run:
    """Run a fresh watch to its deadline. `after_deadline` is a PRState, or an
    exception type name string (raised as a non-retryable ApplicationError)
    for every read at or after the deadline."""
    base, _calls, investigate_ran, ops = _fake_activities(released=True)
    reads: list[datetime] = []
    cutoff_holder: dict[str, datetime] = {}

    @activity.defn(name="get_pr_state")
    async def get_pr_state(service: str, slug: str) -> PRState:
        now = await env.get_current_time()
        cutoff_holder.setdefault("start", now)
        # Polls land every 15 min from `start` (the first poll is ~43 s after
        # the watch began); the last in-loop poll is at about start+13d23h45m
        # and the final read at about start+14d. A cutoff between them keeps
        # every in-loop poll OPEN.
        if now < cutoff_holder["start"] + DEADLINE - timedelta(minutes=5):
            return OPEN_PR
        reads.append(now)
        if isinstance(after_deadline, str):
            raise ApplicationError("github unreachable", type=after_deadline, non_retryable=True)
        return after_deadline

    activities = [a for a in base if a.__temporal_activity_definition.name != "get_pr_state"] + [get_pr_state]
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/1055"),
            id=f"final-read-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        with anyio.fail_after(10):
            await investigate_ran.wait()
        await handle.signal(DevLoopWorkflow.approve, {"approval_ref": "aar_test"})
        run = _Run(handle=handle, events=[], ops=ops, reads=reads, cutoff=datetime.now(UTC))
        try:
            run.result = await handle.result()
        except WorkflowFailureError as exc:
            run.failure = exc
    run.events = list((await handle.fetch_history()).events)
    return run


def _scheduled(run: _Run, name: str) -> int:
    return sum(
        1
        for e in run.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == name
    )


async def test_final_read_merged_writes_terminal(env):
    """D3: a PR that merged near the deadline is reported MERGED, `terminal` only."""
    run = await _ride_to_deadline(env, dataclasses.replace(MERGED_PR, head_sha=HEAD))
    assert run.failure is None
    assert len(run.reads) == 1
    assert run.result.pr is not None and run.result.pr.state == "MERGED"
    assert run.result.watch_end == "final-read"
    ops = [o.op for o in run.ops]
    assert ops.count("terminal") == 1
    assert "release" not in ops


async def test_final_read_open_releases_with_fresh_state(env):
    """D4: still OPEN at the deadline: `final-read`, release on the fresh head."""
    fresh = dataclasses.replace(OPEN_PR, head_sha="fresh999")
    run = await _ride_to_deadline(env, fresh)
    assert run.failure is None
    assert run.result.pr is not None and run.result.pr.head_sha == "fresh999"
    assert run.result.watch_end == "final-read"
    releases = [o for o in run.ops if o.op == "release"]
    assert len(releases) == 1 and releases[0].version == "fresh999"
    assert "terminal" not in [o.op for o in run.ops]


async def test_final_read_closed_writes_terminal(env):
    """D5: CLOSED at the deadline: `terminal`, `final-read`."""
    run = await _ride_to_deadline(env, dataclasses.replace(OPEN_PR, state="CLOSED"))
    assert run.failure is None
    assert run.result.pr is not None and run.result.pr.state == "CLOSED"
    assert run.result.watch_end == "final-read"
    ops = [o.op for o in run.ops]
    assert ops.count("terminal") == 1 and "release" not in ops


@pytest.mark.parametrize(
    "after_deadline",
    ["ProposalListingError", "SomethingElse", PRState(found=False)],
    ids=["transient", "non-transient", "not-found"],
)
async def test_unreadable_final_read_fails_closed(env, after_deadline, caplog):
    """D6: an unreadable final read ends Failed with the claim retained."""
    run = await _ride_to_deadline(env, after_deadline)
    assert run.failure is not None
    cause = run.failure.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == MERGE_WATCH_RECONCILE_UNRECORDED
    assert len(run.reads) == FINAL_READ_ATTEMPTS
    ops = [o.op for o in run.ops]
    assert "release" not in ops and "terminal" not in ops
    assert _scheduled(run, "record_merge_watch_reconcile") == 0
    assert _scheduled(run, "confirm_no_pr") == 0


# --- resume-record harness (#1055 D6b, D8-D10, D11 carry) -----------------------
#
# A run started from a `MergeWatchResume` whose absolute deadline is a few
# poll intervals away reaches the same `_watch_pr` deadline arm as a fresh
# 14-day run, without the ~1344 polls. The fake `get_pr_state` answers OPEN
# until that deadline and the scripted `after` answers from it on.

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import logging  # noqa: E402

from temporalio.worker import UnsandboxedWorkflowRunner  # noqa: E402

from orchestrator import policy_checkpoint as pc  # noqa: E402
from orchestrator.temporal.activities.action_approval import ApprovalPoll, GatedActionResult  # noqa: E402
from orchestrator.temporal.workflows import dev_loop  # noqa: E402
from orchestrator.temporal.workflows.action_approval import (  # noqa: E402
    GATED_ACTIVITY_TIMEOUT,
    ActionApprovalWaitWorkflow,
    ApprovalWaitInput,
    ApprovalWaitResult,
)
from tests.test_merge_watch_worker_gap import (  # noqa: E402
    HEAD_SHA,
    ISSUE_URL,
    _resume_record,
)
from tests.test_merge_watch_worker_gap import OPEN_PR as GAP_OPEN_PR  # noqa: E402

# The gate's wait child lives ~20 virtual minutes: a deadline 10 minutes out
# falls while the attempt started by poll 1 is still in flight.
GATE_OFFSET = timedelta(minutes=10)


@dataclasses.dataclass
class _Resumed:
    handle: Any
    events: list[Any]
    ops: list[Any]
    post: list[datetime]
    polls: int
    result: Any = None
    failure: BaseException | None = None
    first_run_id: str = ""


async def _resume_run(
    env: WorkflowEnvironment,
    *,
    after: list[Any],
    offset: timedelta = timedelta(minutes=20),
    claim_abandoned: bool = False,
    gate_ran_head: str = "",
    on_read: Any = None,
    on_poll: Any = None,
    unsandboxed: bool = False,
    holder_out: dict[str, Any] | None = None,
    skip_off: bool = False,
    gate: bool = False,
    wait_cls: type | None = None,
) -> _Resumed:
    """`after` is the scripted answer for the nth read at or after the
    deadline (the last one repeats): a PRState, or an exception type name."""
    base, _calls, _investigate_ran, ops = _fake_activities(
        released=True,
        pr_states=[GAP_OPEN_PR],
        gate_results=[GatedActionResult(code=pc.CODE_APPROVAL_PENDING, approval_ref="aar_pending")] if gate else None,
    )
    t0 = await env.get_current_time()
    deadline = t0 + offset
    post: list[datetime] = []
    polls = {"n": 0}
    holder: dict[str, Any] = {}

    @activity.defn(name="get_pr_state")
    async def get_pr_state(service: str, slug: str) -> PRState:
        polls["n"] += 1
        now = await env.get_current_time()
        if now < deadline:
            if on_poll is not None:
                await on_poll(holder["handle"], polls["n"])
            return GAP_OPEN_PR
        post.append(now)
        answer = after[min(len(post) - 1, len(after) - 1)]
        if on_read is not None:
            await on_read(holder["handle"], len(post))
        if isinstance(answer, str):
            raise ApplicationError("github unreachable", type=answer, non_retryable=True)
        return answer

    @activity.defn(name="read_action_approval")
    async def read_action_approval(approval_id: str) -> ApprovalPoll:
        # Nobody ever decides: the wait stays pending until cancelled.
        return ApprovalPoll(approval_id=approval_id, state="pending", expires_at="2099-01-01T00:00:00+00:00")

    activities = [a for a in base if a.__temporal_activity_definition.name != "get_pr_state"] + [
        get_pr_state,
        read_action_approval,
    ]
    record = dataclasses.replace(
        _resume_record(t0, offset, terminal_reconcile=True),
        claim_abandoned=claim_abandoned,
        gate_ran_head=gate_ran_head,
        # a record with no settled head lets poll 1 start a gate attempt
        merge_gate_settled_head="" if gate else HEAD_SHA,
    )
    kwargs: dict[str, Any] = {"workflow_runner": UnsandboxedWorkflowRunner()} if unsandboxed else {}
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[DevLoopWorkflow, wait_cls or ActionApprovalWaitWorkflow],
        activities=activities,
        **kwargs,
    ):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=ISSUE_URL, resume=record),
            id=f"resume-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        holder["handle"] = handle
        if holder_out is not None:
            holder_out["handle"] = handle
            holder_out["post"] = post
        run = _Resumed(handle=handle, events=[], ops=ops, post=post, polls=0, first_run_id=handle.result_run_id or "")
        try:
            with anyio.fail_after(60), env.auto_time_skipping_disabled() if skip_off else contextlib.nullcontext():
                run.result = await handle.result()
        except WorkflowFailureError as exc:
            run.failure = exc
    run.polls = polls["n"]
    run.events = list((await handle.fetch_history()).events)
    return run


def _unrecorded(run: _Resumed) -> None:
    assert run.failure is not None, run.result
    cause = run.failure.cause
    assert isinstance(cause, ApplicationError) and cause.type == MERGE_WATCH_RECONCILE_UNRECORDED


async def test_operator_abandon_before_the_deadline_skips_the_decision(env):
    """D8 (R6, mutation v): an ordinary abandon never reaches the decision:
    no read at or after the deadline, `watch_end` stays "", the claim is released."""

    async def abandon_on_first_poll(handle: Any, n: int) -> None:
        if n == 1:
            await handle.signal(DevLoopWorkflow.abandon, {"reason": "operator cleanup"})

    run = await _resume_run(env, after=[GAP_OPEN_PR], on_poll=abandon_on_first_poll)
    assert run.failure is None
    assert run.post == []
    assert run.result.watch_end == ""
    assert run.result.ended.startswith("abandoned: ")
    ops = [o.op for o in run.ops]
    assert "release" in ops and "terminal" not in ops


async def test_abandon_during_a_read_that_returns_open(env):
    """D9: abandon landing during a read that returns OPEN ends
    `abandoned-unverified` with that fresh state, released, and no second read."""
    fresh = dataclasses.replace(GAP_OPEN_PR, head_sha="fresh999")

    async def abandon(handle: Any, n: int) -> None:
        await handle.signal(DevLoopWorkflow.abandon, {"reason": "operator"})

    run = await _resume_run(env, after=[fresh], on_read=abandon)
    assert run.failure is None
    assert run.result.watch_end == "abandoned-unverified"
    assert run.result.pr is not None and run.result.pr.head_sha == "fresh999"
    assert len(run.post) == 1
    releases = [o for o in run.ops if o.op == "release"]
    assert len(releases) == 1 and "terminal" not in [o.op for o in run.ops]


async def test_abandon_during_a_read_that_returns_merged_still_lands_terminal(env):
    """D9: a MERGED answer wins over the abandon that arrived with it."""
    merged = dataclasses.replace(MERGED_PR, head_sha=HEAD_SHA)

    async def abandon(handle: Any, n: int) -> None:
        await handle.signal(DevLoopWorkflow.abandon, {"reason": "operator"})

    run = await _resume_run(env, after=[merged], on_read=abandon)
    assert run.failure is None
    assert run.result.watch_end == "final-read"
    assert run.result.pr is not None and run.result.pr.state == "MERGED"
    ops = [o.op for o in run.ops]
    assert ops.count("terminal") == 1 and "release" not in ops


async def test_abandon_during_a_final_read_backoff_wakes_at_once(env):
    """D9: abandon during the backoff after an unreadable first read wakes the
    wait: no further read, `abandoned-unverified`, released."""

    async def abandon_in_the_backoff(handle: Any, n: int) -> None:
        async def later() -> None:
            await env.sleep(timedelta(seconds=30))
            await handle.signal(DevLoopWorkflow.abandon, {"reason": "operator"})

        asyncio.get_running_loop().create_task(later())

    run = await _resume_run(env, after=["ProposalListingError"], on_read=abandon_in_the_backoff)
    assert run.failure is None
    assert len(run.post) == 1
    assert run.result.watch_end == "abandoned-unverified"
    assert "release" in [o.op for o in run.ops]


async def test_unrecorded_end_still_counts_an_already_abandoned_claim_once(env, caplog):
    """D6b (R10, mutation finally-2): a claim whose last relinquishing write
    already failed is counted once even when the watch ends unrecorded."""
    caplog.set_level(logging.WARNING)
    run = await _resume_run(env, after=["SomethingElse"], claim_abandoned=True)
    _unrecorded(run)
    assert sum("LIFECYCLE-CLAIM-ABANDONED" in r.getMessage() for r in caplog.records) == 1
    ops = [o.op for o in run.ops]
    assert "release" not in ops and "terminal" not in ops


async def test_hopped_or_resumed_watch_keeps_the_carried_deadline(env):
    """D10 (mutation e): the continued run ends on the carried deadline; a
    recomputed 14-day deadline would poll ~1344 times before the final read."""
    fresh = dataclasses.replace(GAP_OPEN_PR, head_sha="fresh999")
    run = await _resume_run(env, after=[fresh])
    assert run.failure is None
    assert run.polls <= 6
    assert run.result.watch_end == "final-read"
    assert [o.version for o in run.ops if o.op == "release"] == ["fresh999"]


async def test_gate_ran_head_is_rehydrated_from_the_record(env):
    """D11 (mutation carry-rehydrate): a record whose gate already ran at the
    OPEN head cannot decide `open`: the end is the Failed interim end."""
    run = await _resume_run(env, after=[GAP_OPEN_PR], gate_ran_head=HEAD_SHA)
    _unrecorded(run)
    ops = [o.op for o in run.ops]
    assert "release" not in ops and "terminal" not in ops


async def test_gate_ran_head_survives_a_hop(env, monkeypatch):
    """D11 (mutation carry-hop): the same, after the watch continued-as-new
    before the deadline (history floor forced to one event)."""
    monkeypatch.setattr(dev_loop, "MERGE_WATCH_HISTORY_FLOOR", 1)
    run = await _resume_run(env, after=[GAP_OPEN_PR], gate_ran_head=HEAD_SHA, unsandboxed=True)
    first = env.client.get_workflow_handle(run.handle.id, run_id=run.first_run_id)
    kinds = {e.event_type for e in (await first.fetch_history()).events}
    assert EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW in kinds, "the watch never hopped"
    _unrecorded(run)
    ops = [o.op for o in run.ops]
    assert "release" not in ops


_STUBBORN: dict[str, Any] = {}


@workflow.defn(name="ActionApprovalWaitWorkflow")
class _StubbornWait:
    """Test double for the gate's wait child. It never ends by itself, so the
    own merge attempt is still in flight at the deadline. It swallows the
    cancel its parent sends and stays open until the test sets `release`."""

    @workflow.run
    async def run(self, inp: ApprovalWaitInput) -> ApprovalWaitResult:
        try:
            await workflow.wait_condition(lambda: False)
        except asyncio.CancelledError:
            _STUBBORN["cancel_seen"] = True
            # A plain dict flip raises no workflow activation: poll it on a timer.
            while not _STUBBORN.get("release"):
                await workflow.sleep(timedelta(seconds=2))
        return ApprovalWaitResult(outcome="timed_out", approval_id=inp.approval_id, attempt=0, code="")


async def _released_gate_run(env: WorkflowEnvironment, after: list[Any]) -> _Resumed:
    """A gate attempt that is in flight at the deadline and drains as soon as
    the watch cancels it."""
    _STUBBORN.clear()
    _STUBBORN["release"] = True
    return await _resume_run(env, after=after, gate=True, offset=GATE_OFFSET, wait_cls=_StubbornWait, unsandboxed=True)


async def test_a_gate_in_flight_at_the_deadline_is_cancelled_and_the_window_waited_out(env):
    """D11: an own merge attempt in flight at the deadline is cancelled and
    drained; a read that is still OPEN afterwards waits out
    GATED_ACTIVITY_TIMEOUT, re-reads once, and a PR still OPEN ends Failed."""
    run = await _released_gate_run(env, [GAP_OPEN_PR])
    _unrecorded(run)
    assert len(run.post) == 2
    # the window is measured from the settle, a hair before the first read
    assert run.post[1] - run.post[0] >= GATED_ACTIVITY_TIMEOUT - timedelta(seconds=30)
    ops = [o.op for o in run.ops]
    assert "release" not in ops and "terminal" not in ops


async def test_a_merge_landing_in_the_window_gives_a_second_read_of_merged(env):
    """D11: the merge the cancelled attempt started lands in the window:
    the second read sees MERGED and writes `terminal`."""
    merged = dataclasses.replace(MERGED_PR, head_sha=HEAD_SHA)
    run = await _released_gate_run(env, [GAP_OPEN_PR, merged])
    assert run.failure is None
    assert len(run.post) == 2
    assert run.result.pr is not None and run.result.pr.state == "MERGED"
    assert run.result.watch_end == "final-read"
    ops = [o.op for o in run.ops]
    assert ops.count("terminal") == 1 and "release" not in ops


async def test_workflow_cancel_while_the_settle_waits_on_a_gate_that_ignores_cancel(env, caplog):
    """D13 (mutation ai): a workflow cancel that lands while the decision
    waits for an own merge attempt that ignores its cancel must end the run
    Cancelled, with no read after the cancel and no `release` (S2: a swallowed
    cancel used to fall through to a final read and a stale write)."""
    caplog.set_level(logging.WARNING)
    _STUBBORN.clear()
    reads_at_cancel: dict[str, int] = {}

    async def canceller(holder: dict[str, Any]) -> None:
        for _ in range(400):
            if "handle" in holder:
                break
            await asyncio.sleep(0.01)
        for _ in range(400):
            if _STUBBORN.get("cancel_seen"):
                break
            await env.sleep(timedelta(minutes=1))
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the gate's child never saw the cancel")
        reads_at_cancel["n"] = len(holder["post"])
        await holder["handle"].cancel()
        await asyncio.sleep(0.5)
        _STUBBORN["release"] = True
        await env.sleep(timedelta(seconds=10))

    holder: dict[str, Any] = {}
    task = asyncio.get_running_loop().create_task(canceller(holder))

    try:
        run = await _resume_run(
            env,
            after=[GAP_OPEN_PR],
            gate=True,
            offset=GATE_OFFSET,
            wait_cls=_StubbornWait,
            unsandboxed=True,
            skip_off=True,
            holder_out=holder,
        )
    finally:
        _STUBBORN["release"] = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert run.failure is not None
    from temporalio.exceptions import CancelledError as TemporalCancelled

    assert isinstance(run.failure.cause, TemporalCancelled)
    assert run.post == [] and reads_at_cancel["n"] == 0
    assert "release" not in [o.op for o in run.ops]
    assert any(dev_loop.MERGE_WATCH_RECONCILE_UNRECORDED in r.getMessage() for r in caplog.records)


# --- D18: the delivery guard (R11, R12; mutations deliver-wait, deliver-decided) --
#
# These two drive the guard with a test-only probe that raises the flag by
# signal, exactly as the 1d-1 `_FlaggedLoop` does for `_reconcile_required`:
# the flag is what the guard reads, and it is true for the whole of a real
# decision window, which a 14-day watch cannot reach on the dispatch fakes.

from orchestrator.temporal.activities.argo import WorkflowResult  # noqa: E402
from orchestrator.temporal.issue_ref import workflow_id_for  # noqa: E402
from orchestrator.temporal.workflows.dev_loop import MergeWatchResume, OpenDelivery  # noqa: E402
from tests.test_execution_request_dispatch import (  # noqa: E402
    _dispatcher,
    _ledger_entry,
    _loop_activities,
    _wait_for,
)
from tests.test_execution_request_resume import (  # noqa: E402
    OPEN_PR as DELIVERY_OPEN_PR,
)
from tests.test_execution_request_resume import (  # noqa: E402
    _delivery,
    _row,
    _stop,
    _submit_log,
    api,  # noqa: F401  (fixture)
)
from tests.test_work_context_resume_acceptance import URL, WID  # noqa: E402


@workflow.defn(name="DevLoopWorkflow", sandboxed=False)
class _UnrecordedLoop(DevLoopWorkflow):
    """Test-only: `_watch_unrecorded` is raised and lowered by signal."""

    @workflow.run
    async def run(self, issue: IssueRef) -> Any:
        return await super().run(issue)

    @workflow.signal
    def raise_flag(self) -> None:
        self._watch_unrecorded = True

    @workflow.signal
    def lower_flag(self) -> None:
        self._watch_unrecorded = False


async def test_a_delivery_is_held_while_a_decision_is_undecided(api, env):  # noqa: F811
    """D18 (mutation deliver-wait): while `_watch_unrecorded` is true the
    delivery's wait does not wake on approval; it is fulfilled only after the
    flag drops, and then `Succeeded`."""
    submit, _ = _submit_log()
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    ref = f"{loop}#{rid}"
    _ledger_entry(api, "temporal", loop, "Succeeded")
    carried = _delivery_record(rid)
    activities = _loop_activities(submit, pr_states=[DELIVERY_OPEN_PR])
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[_UnrecordedLoop], activities=activities):
        await env.client.start_workflow(
            _UnrecordedLoop.run,
            IssueRef(issue_url=URL, work_item_id=WID, resume=carried),
            id=loop,
            task_queue=TASK_QUEUE,
            start_signal="raise_flag",
        )
        await _dispatcher(env).dispatch_once()
        # The first bind attempt is refused while the request is still
        # claimed; the test server skips that retry backoff only while the test
        # sleeps. Without this the delivery has not reached the wait yet and
        # the hold below would pass vacuously.
        await anyio.sleep(1.0)
        await env.sleep(timedelta(minutes=2))
        await anyio.sleep(1.5)
        assert _row(api, ref)["phase"] not in ("Succeeded", "Failed"), "the delivery was fulfilled while held"
        await env.client.get_workflow_handle(loop).signal(_UnrecordedLoop.lower_flag)
        await _wait_for(lambda: _row(api, ref)["phase"] in ("Succeeded", "Failed"))
        assert _row(api, ref)["phase"] == "Succeeded"
        await _stop(env, loop)


async def test_a_delivery_woken_on_an_undecided_end_lands_failed(api, env):  # noqa: F811
    """D18 (mutation deliver-decided): the watch ends while the flag is still
    true (the `unrecorded` end): the exit wakes the delivery, which lands
    `Failed`, never `Succeeded`."""
    submit, _ = _submit_log()
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    ref = f"{loop}#{rid}"
    _ledger_entry(api, "temporal", loop, "Succeeded")
    merged = dataclasses.replace(DELIVERY_OPEN_PR, state="MERGED", merged=True)
    activities = _loop_activities(submit, pr_states=[DELIVERY_OPEN_PR, merged])
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[_UnrecordedLoop], activities=activities):
        await env.client.start_workflow(
            _UnrecordedLoop.run,
            IssueRef(issue_url=URL, work_item_id=WID, resume=_delivery_record(rid)),
            id=loop,
            task_queue=TASK_QUEUE,
            start_signal="raise_flag",
        )
        await _dispatcher(env).dispatch_once()
        # let poll 1 and its timer land before the test moves time
        await anyio.sleep(1.5)
        await env.sleep(timedelta(minutes=20))
        await anyio.sleep(1.5)
        # the watch ended MERGED: an unfulfilled delivery is woken by the exit
        # after DELIVERY_EXIT_GRACE, which the test server skips only while the
        # test sleeps
        # the merged PR is followed by the deploy observation, then the exit
        # grace; move time in steps until the delivery lands
        for _ in range(40):
            if _row(api, ref)["phase"] in ("Succeeded", "Failed"):
                break
            await env.sleep(timedelta(minutes=30))
            await anyio.sleep(0.3)
        assert _row(api, ref)["phase"] == "Failed"
        # the loop has already completed: nothing to stop


def _delivery_record(rid: str) -> MergeWatchResume:
    return MergeWatchResume(
        service="mctl-telegram",
        slug="issue-431-dispatch-acceptance",
        deadline="2099-01-01T00:00:00Z",
        investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
        implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
        work_item_id=WID,
        accepted_request_ids=(rid,),
        deferred_resume_contract=True,
        open_deliveries=(OpenDelivery(delivery=_delivery(rid)),),
    )
