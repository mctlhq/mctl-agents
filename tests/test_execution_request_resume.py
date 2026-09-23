"""A `resume` execution request delivered onto a LIVE DevLoop, offline
(mctlhq/mctl-agents#461, gap 1; ADR 011 §8).

The dispatcher hands the live loop L the request through the
`accept_execution_request` Update (update id = the request id) BEFORE it
fulfils, then fulfils with `engine_ref = "<L>#<request id>"`; L binds the
`we_` that fulfil mints and ends it at its next approval decision, or on any
exit. Everything runs against `DispatchFakeApi` (mctl-api#368's rules) and
the real `DevLoopWorkflow` in the time-skipping test server; only the Argo
submit is faked. What each test pins:

- the resume is delivered and fulfilled, the loop runs on under the resumed
  `we_`, and the approval it had is never inherited by the new actor;
- a crash between the Update and the fulfil converges, with one delivery;
- a crash after the fulfil loses nothing;
- a repeated delivery is a no-op, in the same run and across continue-as-new;
- a loop that ends around the delivery never leaves the request lost, nor an
  execution nobody will end;
- the resumed execution is terminal on every exit;
- a refused resume is rejected with a typed reason, before any fulfil;
- the reconciliation understands `#xr_` engine refs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import anyio
import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal import dispatcher as dx
from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.constants import IMPLEMENTATION_OPERATION, TASK_QUEUE
from orchestrator.temporal.issue_ref import (
    is_resume_engine_ref,
    loop_id_of_engine_ref,
    resume_engine_ref,
    workflow_id_for,
)
from orchestrator.temporal.start import dispatched_workflow_id
from orchestrator.temporal.workflows.dev_loop import (
    ACCEPT_EXECUTION_REQUEST_UPDATE,
    DELIVERY_ACCEPTED,
    DELIVERY_EXIT_GRACE,
    FULFILMENT_WAIT,
    RESUME_DEFERRED_ERROR_TYPE,
    RESUME_REFUSED_ERROR_TYPE,
    DevLoopWorkflow,
    IssueRef,
    MergeWatchResume,
    OpenDelivery,
    ResumeDelivery,
)
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context.client import WorkItemClient, _HTTPResult
from orchestrator.work_context.contract import ActorRef
from tests.temporal_harness import Worker
from tests.test_execution_request_dispatch import (
    Crash,
    CrashBeforeFulfil,
    DispatchFakeApi,
    FakeTemporal,
    _audit,
    _dispatcher,
    _end,
    _ledger_entry,
    _loop_activities,
    _wait_for,
)
from tests.test_work_context_resume_acceptance import URL, WID

pytestmark = pytest.mark.anyio


@pytest.fixture
def api(monkeypatch) -> DispatchFakeApi:
    fake = DispatchFakeApi()
    monkeypatch.setattr(
        WorkItemClient, "_request", lambda self, method, path, payload=None: fake.request(method, path, payload)
    )
    monkeypatch.delenv(dx.ENABLED_ENV_VAR, raising=False)
    return fake


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env

APPROVE_OPERATION = "mctl-agents-approve"
OPEN_PR = PRState(
    found=True, pr_url="https://github.com/mctlhq/mctl-telegram/pull/9", repo="mctl-telegram", number=9, state="OPEN"
)


def _submit_log(*, hold_approve: asyncio.Event | None = None) -> tuple[Any, list[tuple[str, dict]]]:
    """A fake Argo submit that records every operation; with `hold_approve`
    the approve flip does not return until the event is set."""
    ops: list[tuple[str, dict]] = []

    @activity.defn(name="submit_and_wait")
    async def submit(input: SubmitAndWaitInput) -> WorkflowResult:
        ops.append((input.operation, dict(input.params)))
        if input.operation == APPROVE_OPERATION and hold_approve is not None:
            await hold_approve.wait()
        return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

    return submit, ops


def _worker(env: Any, submit: Any) -> Worker:
    # An open PR keeps a loop that reaches its merge watch alive until `_end`.
    return Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[DevLoopWorkflow],
        activities=_loop_activities(submit, pr_states=[OPEN_PR]),
    )


def _ops(ops: list[tuple[str, dict]], operation: str) -> list[dict]:
    return [params for op, params in ops if op == operation]


async def _park(api: DispatchFakeApi, env: Any) -> str:
    """A dispatched loop, its first execution ended, parked at approval."""
    first = api.create_request("start")
    outcome = await _dispatcher(env).dispatch_once()
    assert outcome.action == dx.FULFILLED
    await _wait_for(lambda: bool(api.executions) and api.executions[0]["phase"] == "Succeeded")
    return dispatched_workflow_id(first)


async def _events(env: Any, workflow_id: str) -> list[Any]:
    return list((await env.client.get_workflow_handle(workflow_id).fetch_history()).events)


def _accepted_updates(events: list[Any]) -> list[str]:
    """The update ids this loop accepted, in history order."""
    return [
        e.workflow_execution_update_accepted_event_attributes.accepted_request.meta.update_id
        for e in events
        if e.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
    ]


def _binds_for(events: list[Any], request_id: str) -> int:
    count = 0
    for e in events:
        if e.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            continue
        attrs = e.activity_task_scheduled_event_attributes
        if attrs.activity_type.name != "bind_dispatched_execution":
            continue
        if json.loads(attrs.input.payloads[0].data)["execution_request_id"] == request_id:
            count += 1
    return count


async def _stop(env: Any, workflow_id: str) -> None:
    """Clean up a loop that is past its approval gates, once the test has
    asserted everything: `_end`'s graceful abandon is observed by the merge
    watch only at its next poll, which the test server does not reach
    within one result long-poll (true of today's loop without any
    delivery)."""
    await env.client.get_workflow_handle(workflow_id).terminate("test over")


async def _end_through_the_grace(env: Any, workflow_id: str) -> Any:
    """`_end` for a loop that must first wait out DELIVERY_EXIT_GRACE for an
    unfulfilled delivery: the test server skips an activity's retry backoff
    only while the test sleeps, not while it waits for a result."""
    handle = env.client.get_workflow_handle_for(DevLoopWorkflow.run, workflow_id)
    await handle.signal(DevLoopWorkflow.abandon, {"reason": "test over"})
    await env.sleep(DELIVERY_EXIT_GRACE + timedelta(minutes=1))
    return await handle.result()


def _row(api: DispatchFakeApi, engine_ref: str) -> dict:
    return next(e for e in api.executions if e["engine_ref"] == engine_ref)


def _delivery(rid: str, *, surface: str = "telegram") -> ResumeDelivery:
    return ResumeDelivery(
        execution_request_id=rid, work_item_id=WID, surface=surface, actor_kind="human", actor_id="user:alice"
    )


# -- delivered, fulfilled, and the loop runs on under it ----------------------


async def test_a_resume_onto_a_live_loop_is_delivered_and_the_loop_continues_under_it(api, env, capsys):
    """bob's approval is spent on the approve flip; alice resumes from
    Telegram while that flip runs. The loop binds alice's `we_`, and the gate
    before the implement step waits for a FRESH approval: the resume never
    inherits bob's. alice approves, her execution succeeds, implement runs."""
    held = asyncio.Event()
    submit, ops = _submit_log(hold_approve=held)
    async with _worker(env, submit):
        loop = await _park(api, env)
        handle = env.client.get_workflow_handle(loop)
        await handle.signal(DevLoopWorkflow.approve, {"approver": "bob"})
        await _wait_for(lambda: bool(_ops(ops, APPROVE_OPERATION)))

        rid = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()

        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        resumed = api.executions[1]
        assert (resumed["id"], resumed["engine_ref"], resumed["attempt"]) == (outcome.execution_id, f"{loop}#{rid}", 2)
        ctx = await handle.query(DevLoopWorkflow.work_context)
        assert ctx.execution_id == resumed["id"] and ctx.execution_sequence == 2
        assert ctx.last_surface.kind == "telegram"
        assert ctx.last_actor == ActorRef(kind="human", actor_id="user:alice")
        assert ctx.executions[-1].surface_transition and ctx.executions[-1].temporal_workflow_id == f"{loop}#{rid}"

        held.set()
        await anyio.sleep(1.5)
        # The flip ran on bob's approval, as it was already under way; the
        # implement step did not, and alice's execution is still open.
        assert [p["approver"] for p in _ops(ops, APPROVE_OPERATION)] == ["bob"]
        assert _ops(ops, IMPLEMENTATION_OPERATION) == []
        assert api.executions[1]["phase"] == "Running"

        await handle.signal(DevLoopWorkflow.approve, {"approver": "alice"})
        await _wait_for(lambda: api.executions[1]["phase"] == "Succeeded")
        await _wait_for(lambda: bool(_ops(ops, IMPLEMENTATION_OPERATION)))
        await _stop(env, loop)

    # One loop, one investigation, no continuation started for the resume.
    assert len(_ops(ops, "mctl-agents-investigate")) == 1
    assert [e["engine_ref"] for e in api.executions] == [loop, f"{loop}#{rid}"]
    assert api.request_state(rid)["state"] == "fulfilled"
    audit = [a for a in _audit(capsys.readouterr().out) if a["execution_request_id"] == rid]
    assert [a["event"] for a in audit] == ["claim", "deliver", "fulfil"]
    assert audit[1]["live_loop"] == loop and audit[1]["verdict"] == dx.DELIVERED


# -- crash windows -------------------------------------------------------------


async def test_a_crash_between_the_update_and_the_fulfil_converges_with_one_delivery(api, env):
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        with pytest.raises(Crash):
            await _dispatcher(env, CrashBeforeFulfil(WorkItemClient())).dispatch_once()

        # The loop already holds the request, although nothing was
        # fulfilled: its own history is the record, not the dead process.
        assert _accepted_updates(await _events(env, loop)) == [rid]
        assert len(api.executions) == 1 and api.request_state(rid)["state"] == "claimed"

        api.now += 61  # the crashed holder's lease lapses
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        events = await _events(env, loop)
        await _end(env, loop)

    # The re-sent Update (same update id) was answered from the registry: no
    # second acceptance, one bind, one execution.
    assert _accepted_updates(events) == [rid]
    assert _binds_for(events, rid) == 1
    assert [e["engine_ref"] for e in api.executions] == [loop, f"{loop}#{rid}"]


async def test_an_accepted_delivery_outwaits_a_whole_fulfilment_wait(api, env):
    """The dispatcher died after the Update and no claim came back for longer
    than FULFILMENT_WAIT. The loop must still be waiting when one does: the
    re-sent Update is answered from Temporal's registry, not by the loop, so
    a loop that had given up would leave the fulfil it triggers unbound."""
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        with pytest.raises(Crash):
            await _dispatcher(env, CrashBeforeFulfil(WorkItemClient())).dispatch_once()
        await env.sleep(FULFILMENT_WAIT + timedelta(minutes=15))
        assert _binds_for(await _events(env, loop), rid) >= 2  # a second wait began

        api.now += 3600
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        await _end(env, loop)

    assert api.executions[1]["phase"] == "Failed"


class CrashAfterFulfil(CrashBeforeFulfil):
    """A `WorkItemClient` whose process dies right after the fulfil
    committed, before anything else the dispatcher does."""

    def fulfil_execution_request(self, *args: Any, **kwargs: Any) -> Any:
        self._inner.fulfil_execution_request(*args, **kwargs)
        raise Crash("dispatcher died right after the fulfil committed")


async def test_a_crash_after_the_fulfil_loses_nothing(api, env):
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        with pytest.raises(Crash):
            await _dispatcher(env, CrashAfterFulfil(WorkItemClient())).dispatch_once()
        # The request is closed: no later claim will ever see it again...
        assert api.request_state(rid)["state"] == "fulfilled"
        assert (await _dispatcher(env).dispatch_once()).action == dx.NOTHING
        # ...and none is needed: the loop accepted it first, and reads the
        # `we_` from the store itself.
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        await env.client.get_workflow_handle(loop).signal(DevLoopWorkflow.approve, {"approver": "alice"})
        await _wait_for(lambda: api.executions[1]["phase"] == "Succeeded")
        await _stop(env, loop)


# -- duplicates ---------------------------------------------------------------


async def test_a_repeated_delivery_is_a_no_op_under_either_update_id(api, env):
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        handle = env.client.get_workflow_handle(loop)

        # The same update id: Temporal's registry answers, the handler never runs.
        again = await dx.TemporalClientPort(env.client).deliver_resume(loop, _delivery(rid))
        assert again.verdict == dx.DELIVERED
        # The same request under ANOTHER update id (what a continued run sees,
        # whose registry is fresh): the loop's own set answers.
        other = await handle.execute_update(
            ACCEPT_EXECUTION_REQUEST_UPDATE, _delivery(rid), id=f"{rid}-again", result_type=str
        )
        assert other == DELIVERY_ACCEPTED
        await anyio.sleep(1.0)
        ctx = await handle.query(DevLoopWorkflow.work_context)
        events = await _events(env, loop)
        await _end(env, loop)

    assert [e.execution_id for e in ctx.executions].count(api.executions[1]["id"]) == 1
    assert _accepted_updates(events) == [rid, f"{rid}-again"]
    assert _binds_for(events, rid) == 1
    assert len(api.executions) == 2


async def test_a_delivery_carried_across_continue_as_new_is_bound_once_by_the_next_run(api, env):
    """The run a merge watch hopped into: it carries one accepted, unbound
    delivery. It binds it itself; the next claim's re-sent Update reaches
    this NEW run, whose Temporal registry is empty, and is a no-op because
    the accepted ids were carried. No approval gate is left in a merge
    watch, so the resumed execution succeeds as soon as it is bound. The
    loop is the issue-keyed one the label path starts."""
    submit, _ = _submit_log()
    loop = workflow_id_for(URL)
    _ledger_entry(api, "temporal", loop, "Succeeded")
    rid = api.create_request("resume")
    carried = MergeWatchResume(
        service="mctl-telegram",
        slug="issue-431-dispatch-acceptance",
        deadline="2099-01-01T00:00:00Z",
        investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
        implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
        work_item_id=WID,
        resume_pending=True,
        accepted_request_ids=(rid,),
        open_deliveries=(OpenDelivery(delivery=_delivery(rid), surface_transition=True),),
    )
    async with _worker(env, submit):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=URL, work_item_id=WID, resume=carried),
            id=loop,
            task_queue=TASK_QUEUE,
        )
        await handle.query(DevLoopWorkflow.work_context)  # the run has its state

        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
        await _wait_for(lambda: _row(api, f"{loop}#{rid}")["phase"] == "Succeeded")
        events = await _events(env, loop)
        ctx = await handle.query(DevLoopWorkflow.work_context)
        await _stop(env, loop)

    assert _accepted_updates(events) == [rid]
    assert _binds_for(events, rid) == 1
    assert [e.execution_id for e in ctx.executions] == [_row(api, f"{loop}#{rid}")["id"]]


async def test_a_delivery_carried_with_a_pending_terminal_phase_is_landed_by_the_next_run(api, env):
    """The previous run decided the resumed execution's phase but its
    advance never landed, and it hopped: the next run lands the carried
    phase, binds nothing again, and closes the delivery."""
    submit, _ = _submit_log()
    loop = workflow_id_for(URL)
    _ledger_entry(api, "temporal", loop, "Succeeded")
    rid = "xr_00000009-0000-4000-8000-000000000461"
    ref = f"{loop}#{rid}"
    _ledger_entry(api, "temporal", ref, "Running")
    carried = MergeWatchResume(
        service="mctl-telegram",
        slug="issue-431-dispatch-acceptance",
        deadline="2099-01-01T00:00:00Z",
        investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
        implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
        work_item_id=WID,
        accepted_request_ids=(rid,),
        open_deliveries=(OpenDelivery(delivery=_delivery(rid), pending_phase="Succeeded"),),
    )
    async with _worker(env, submit):
        await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=URL, work_item_id=WID, resume=carried),
            id=loop,
            task_queue=TASK_QUEUE,
        )
        await _wait_for(lambda: _row(api, ref)["phase"] == "Succeeded")
        events = await _events(env, loop)
        await _stop(env, loop)

    assert _binds_for(events, rid) == 0


async def test_a_resume_delivered_after_the_implement_step_succeeds_at_once(api, env):
    """No approval gate is left in a merge watch: a fresh resume that
    changes the actor clears the approval, but its execution has no decision
    to wait for, and succeeds as soon as it is bound."""
    submit, _ = _submit_log()
    loop = workflow_id_for(URL)
    _ledger_entry(api, "temporal", loop, "Succeeded")
    watching = MergeWatchResume(
        service="mctl-telegram",
        slug="issue-431-dispatch-acceptance",
        deadline="2099-01-01T00:00:00Z",
        investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
        implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
        work_item_id=WID,
    )
    async with _worker(env, submit):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=URL, work_item_id=WID, resume=watching),
            id=loop,
            task_queue=TASK_QUEUE,
        )
        await handle.query(DevLoopWorkflow.work_context)  # the run has its state
        rid = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
        await _wait_for(lambda: _row(api, f"{loop}#{rid}")["phase"] == "Succeeded")
        await _stop(env, loop)


# -- the loop ends around the delivery ------------------------------------------


class _Port(dx.TemporalClientPort):
    """The real port, with a hook on `loop_state` and `deliver_resume`."""

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self.state_calls: list[str] = []

    async def loop_state(self, workflow_id: str) -> str:
        self.state_calls.append(workflow_id)
        return await super().loop_state(workflow_id)


async def test_a_loop_gone_before_the_update_turns_the_resume_into_a_continuation(api, env):
    """The liveness check saw the loop running; it ended before the Update
    arrived. The Update finds nothing, and the request starts a continuation
    exactly as a resume onto a finished loop does."""
    submit, ops = _submit_log()

    class StaleCheck(_Port):
        async def loop_state(self, workflow_id: str) -> str:
            answer = await super().loop_state(workflow_id)
            return dx.LOOP_RUNNING if workflow_id == loop and len(self.state_calls) == 1 else answer

    async with _worker(env, submit):
        loop = await _park(api, env)
        await _end(env, loop)
        rid = api.create_request("resume")
        outcome = await dx.Dispatcher(WorkItemClient(), StaleCheck(env.client), lease=60).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.workflow_id == dispatched_workflow_id(rid)
        assert outcome.engine_ref == ""
        await _wait_for(lambda: len(_ops(ops, "mctl-agents-investigate")) == 2)
        await _end(env, outcome.workflow_id)

    assert [e["engine_ref"] for e in api.executions] == [loop, dispatched_workflow_id(rid)]


async def test_a_loop_that_accepted_and_then_ended_before_the_fulfil_gets_nothing_bound(api, env, capsys):
    """The loop said yes, then ended (abandoned) before the fulfil. It gives
    the fulfil its exit grace, then lets go; the dispatcher re-checks the
    loop before fulfilling, finds it closed, and starts a continuation: no
    execution is ever minted for the loop that is gone."""
    submit, ops = _submit_log()

    class EndsAfterAccepting(_Port):
        async def deliver_resume(self, workflow_id: str, delivery: Any) -> dx.DeliveryAnswer:
            answer = await super().deliver_resume(workflow_id, delivery)
            await _end_through_the_grace(env, workflow_id)
            return answer

    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        outcome = await dx.Dispatcher(WorkItemClient(), EndsAfterAccepting(env.client), lease=60).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.workflow_id == dispatched_workflow_id(rid)
        ctx = await env.client.get_workflow_handle(loop).query(DevLoopWorkflow.work_context)
        await _wait_for(lambda: len(_ops(ops, "mctl-agents-investigate")) == 2)
        await _end(env, outcome.workflow_id)

    assert not any(is_resume_engine_ref(e["engine_ref"]) for e in api.executions)
    assert [e["engine_ref"] for e in api.executions] == [loop, dispatched_workflow_id(rid)]
    assert [(r.execution_request_id, r.reason) for r in ctx.resume_rejections] == [
        (rid, "delivery-request-not-fulfilled")
    ]
    events = [a["event"] for a in _audit(capsys.readouterr().out) if a["execution_request_id"] == rid]
    assert events == ["claim", "deliver", "deliver_stale", "start", "fulfil"]


async def test_a_fulfil_landing_while_the_loop_is_ending_is_bound_and_ended_by_the_loop(api, env, capsys):
    """The loop said yes, then was abandoned; the fulfil lands while it is
    ending. The exit grace lets the loop bind that execution and end it
    itself, `Failed` — nothing is left for the dispatcher to clean up."""
    submit, _ = _submit_log()

    class AbandonsAfterAccepting(_Port):
        async def deliver_resume(self, workflow_id: str, delivery: Any) -> dx.DeliveryAnswer:
            answer = await super().deliver_resume(workflow_id, delivery)
            handle = env.client.get_workflow_handle(workflow_id)
            await handle.signal(DevLoopWorkflow.abandon, {"reason": "test over"})
            await anyio.sleep(0.5)  # the loop is now in its exit, still running
            return answer

    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        outcome = await dx.Dispatcher(WorkItemClient(), AbandonsAfterAccepting(env.client), lease=60).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
        result = await env.client.get_workflow_handle_for(DevLoopWorkflow.run, loop).result()

    assert result.ended.startswith("abandoned")
    assert _row(api, f"{loop}#{rid}")["phase"] == "Failed"
    events = [a["event"] for a in _audit(capsys.readouterr().out) if a["execution_request_id"] == rid]
    assert "orphan_failed" not in events


async def test_a_fulfil_that_lands_after_the_loop_closed_is_ended_by_the_dispatcher(api, env, capsys):
    """The narrowest window: the loop closes between the dispatcher's
    re-check and its fulfil. The execution the fulfil mints would stay
    `Pending` for ever — mctl-api refuses every new request for the item
    while it is, so no reconciliation could ever run — so the dispatcher
    checks once more after the fulfil and ends it itself."""
    submit, _ = _submit_log()

    class ClosesBeforeTheFulfil(_Port):
        async def loop_state(self, workflow_id: str) -> str:
            answer = await super().loop_state(workflow_id)
            if workflow_id == loop and len(self.state_calls) == 2:
                await _end_through_the_grace(env, loop)  # closes right after answering "running"
            return answer

    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        outcome = await dx.Dispatcher(WorkItemClient(), ClosesBeforeTheFulfil(env.client), lease=60).dispatch_once()

    assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
    assert _row(api, f"{loop}#{rid}")["phase"] == "Failed"
    audit = [a for a in _audit(capsys.readouterr().out) if a["execution_request_id"] == rid]
    assert [a["event"] for a in audit] == ["claim", "deliver", "fulfil", "orphan_failed"]
    assert audit[-1]["execution_id"] == outcome.execution_id


# -- terminal on every exit ----------------------------------------------------


async def test_the_resumed_execution_fails_when_the_re_approval_wait_expires(api, env):
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        api.create_request("resume")
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        await env.sleep(timedelta(days=15))
        result = await env.client.get_workflow_handle_for(DevLoopWorkflow.run, loop).result()

    assert result.ended == "approval wait expired"
    assert api.executions[1]["phase"] == "Failed"


async def test_the_resumed_execution_fails_when_the_loop_is_cancelled(api, env):
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        api.create_request("resume")
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        handle = env.client.get_workflow_handle(loop)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    assert api.executions[1]["phase"] == "Failed"


async def test_a_delivery_whose_request_is_rejected_at_fulfil_binds_nothing_and_frees_the_loop(api, env):
    """Accepted, then mctl-api re-decided the item (a stale version) and the
    dispatcher rejected the request. The loop binds nothing, records why, and
    a later resume is accepted again (the pending window closed with it).
    The approval the accepted resume cleared stays cleared: fail closed."""
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        stale = api.create_request("resume")
        api.state_version += 1  # the item moved after the surface asked
        refused = await _dispatcher(env).dispatch_once()
        assert refused.action == dx.REJECTED and refused.reason == f"{xr.FULFIL_REFUSED}:state_version_conflict"
        handle = env.client.get_workflow_handle(loop)

        async def rejected() -> bool:
            ctx = await handle.query(DevLoopWorkflow.work_context)
            return any(r.execution_request_id == stale for r in ctx.resume_rejections)

        for _ in range(200):
            if await rejected():
                break
            await anyio.sleep(0.05)
        ctx = await handle.query(DevLoopWorkflow.work_context)
        assert [(r.execution_request_id, r.reason) for r in ctx.resume_rejections] == [(stale, "delivery-rejected")]
        assert len(api.executions) == 1

        fresh = api.create_request("resume")
        accepted = await _dispatcher(env).dispatch_once()
        assert accepted.action == dx.FULFILLED and accepted.engine_ref == f"{loop}#{fresh}"
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        await _end(env, loop)

    assert api.executions[1]["phase"] == "Failed"


# -- refused at the bind, although the fulfil minted an execution -------------


async def _rejected_delivery(env: Any, loop: str, rid: str) -> list[tuple[str, str]]:
    handle = env.client.get_workflow_handle(loop)
    for _ in range(200):
        ctx = await handle.query(DevLoopWorkflow.work_context)
        if any(r.execution_request_id == rid for r in ctx.resume_rejections):
            break
        await anyio.sleep(0.05)
    ctx = await handle.query(DevLoopWorkflow.work_context)
    return [(r.execution_request_id, r.reason) for r in ctx.resume_rejections]


async def test_a_delivery_whose_item_is_about_another_issue_ends_its_minted_execution_failed(api, env):
    """The fulfil minted the `we_` under `<L>#<xr>`, then the bind finds the
    item re-pointed at another issue: the loop must not run it, and must END
    it. Left `Pending` it would wedge the item (mctl-api refuses every other
    request while it is non-terminal, and the reconciliation never touches
    the execution of a RUNNING loop)."""
    submit, ops = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        api.external_key = "https://github.com/mctlhq/mctl-telegram/issues/9999"
        rid = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"

        assert await _rejected_delivery(env, loop, rid) == [(rid, "delivery-work-item-mismatch")]
        await _wait_for(lambda: _row(api, f"{loop}#{rid}")["phase"] == "Failed")
        ctx = await env.client.get_workflow_handle(loop).query(DevLoopWorkflow.work_context)
        assert outcome.execution_id not in [e.execution_id for e in ctx.executions]  # never adopted
        await _end(env, loop)

    assert _ops(ops, IMPLEMENTATION_OPERATION) == []


async def test_a_delivery_whose_advance_to_running_is_refused_ends_its_minted_execution_failed(api, env):
    """The ledger proves the `we_` is this delivery's, but mctl-api (or the
    policy checkpoint) refuses its advance to Running: a definite no, so it
    must not run, and it is ended `Failed` rather than left `Pending`."""
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        ref = f"{loop}#{rid}"
        serve = api.request

        def refuse_running(method: str, path: str, payload: dict | None = None) -> _HTTPResult:
            body = payload or {}
            if method == "POST" and path == f"/api/v1/work-items/{WID}/executions" and (
                body.get("engine_ref"),
                body.get("phase"),
            ) == (ref, "Running"):
                return _HTTPResult(422, {"code": "policy_denied", "error": "not this one"})
            return serve(method, path, payload)

        api.request = refuse_running  # type: ignore[method-assign]
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == ref

        assert await _rejected_delivery(env, loop, rid) == [(rid, "delivery-execution-refused")]
        await _wait_for(lambda: _row(api, ref)["phase"] == "Failed")
        await _end(env, loop)


async def test_a_terminal_advance_that_outlasts_its_retries_is_kept_open_and_lands_later(api, env):
    """alice re-approves, but mctl-api does not answer the resumed
    execution's advance to Succeeded for longer than the whole patient retry
    policy. The delivery stays open with the phase pending and lands it once
    the store answers again: never popped with its `we_` still Running."""
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        ref = f"{loop}#{rid}"
        await _wait_for(lambda: _row(api, ref)["phase"] == "Running")
        # One more than DISPATCHED_ADVANCE_RETRY_POLICY's 10 attempts, and then some.
        api.unavailable_advances["Succeeded"] = 13
        handle = env.client.get_workflow_handle(loop)
        await handle.signal(DevLoopWorkflow.approve, {"approver": "alice"})
        for _ in range(12):
            if _row(api, ref)["phase"] == "Succeeded":
                break
            await env.sleep(timedelta(minutes=30))
        assert api.unavailable_advances["Succeeded"] == 0
        assert _row(api, ref)["phase"] == "Succeeded"
        ctx = await handle.query(DevLoopWorkflow.work_context)
        assert all(r.execution_request_id != rid for r in ctx.resume_rejections)
        await _stop(env, loop)


# -- refused before any fulfil -------------------------------------------------


async def test_a_resume_the_loop_refuses_is_rejected_typed_and_never_fulfilled(api, env):
    """A request whose surface is outside the loop's closed surface kinds
    (here `slack`) is refused by the loop's validator, and the request is
    rejected before any `we_`. (mctl-api's own default surface `api` is in
    the vocabulary since #481: see the validator test below.)"""
    submit, _ = _submit_log()
    async with _worker(env, submit):
        loop = await _park(api, env)
        rid = api.create_request("resume")
        api.xrs[rid]["surface"] = "slack"
        outcome = await _dispatcher(env).dispatch_once()
        events = await _events(env, loop)
        await _end(env, loop)

    assert outcome.action == dx.REJECTED
    assert api.request_state(rid)["reason"] == f"{xr.RESUME_REFUSED}:surface-or-actor-unrecognised"
    assert len(api.executions) == 1 and [f for f in api.fulfils() if f.get("engine_ref", "").endswith(rid)] == []
    assert _accepted_updates(events) == []  # a refused Update writes nothing


def _validator(**state: Any) -> DevLoopWorkflow:
    wf = DevLoopWorkflow()
    wf._initialized = True
    for name, value in state.items():
        setattr(wf, name, value)
    return wf


@pytest.mark.parametrize(
    ("state", "delivery", "error_type", "reason"),
    [
        ({"_initialized": False}, _delivery("xr_1"), RESUME_DEFERRED_ERROR_TYPE, "loop-not-ready"),
        ({"_exiting": True}, _delivery("xr_1"), RESUME_DEFERRED_ERROR_TYPE, "loop-ending"),
        ({"_hopping": True}, _delivery("xr_1"), RESUME_DEFERRED_ERROR_TYPE, "loop-ending"),
        ({"_abandoned": True}, _delivery("xr_1"), RESUME_DEFERRED_ERROR_TYPE, "loop-ending"),
        ({"_work_item_id": "wi_other"}, _delivery("xr_1"), RESUME_REFUSED_ERROR_TYPE, "work-item-mismatch"),
        ({"_resume_pending": True}, _delivery("xr_1"), RESUME_REFUSED_ERROR_TYPE, "resume-already-pending"),
        (
            {"_open_deliveries": {"xr_0": OpenDelivery(delivery=_delivery("xr_0"))}},
            _delivery("xr_1"),
            RESUME_REFUSED_ERROR_TYPE,
            "resume-already-pending",
        ),
        ({}, _delivery("xr_1", surface=""), RESUME_REFUSED_ERROR_TYPE, "surface-or-actor-missing"),
        ({}, _delivery("xr_1", surface="slack"), RESUME_REFUSED_ERROR_TYPE, "surface-or-actor-unrecognised"),
        ({}, _delivery("not-a-request"), RESUME_REFUSED_ERROR_TYPE, "malformed-delivery"),
    ],
    ids=[
        "not-ready",
        "exiting",
        "hopping",
        "abandoned",
        "mismatch",
        "signal-pending",
        "delivery-open",
        "no-surface",
        "unknown-surface",
        "malformed",
    ],
)
def test_the_validator_answers_with_the_resume_rules(state, delivery, error_type, reason):
    with pytest.raises(ApplicationError) as exc:
        _validator(**state)._validate_execution_request(delivery)
    assert exc.value.type == error_type and exc.value.details == (reason,)
    if error_type == RESUME_REFUSED_ERROR_TYPE:
        assert reason in xr.RESUME_REFUSAL_REASONS  # the documented vocabulary


def test_the_validator_accepts_mctl_apis_default_surface_api():
    """A resume made straight on mctl-api carries its default surface `api`
    (`defaultWorkItemSurface`); since #481 that is in the closed vocabulary,
    so the loop accepts the delivery rather than refusing it."""
    _validator()._validate_execution_request(_delivery("xr_1", surface="api"))


def test_the_resume_signal_is_refused_while_a_delivered_request_is_open():
    """Never two pending resumes, whichever way each arrived: the validator
    refuses an Update while a signal's resume is pending, and the signal
    refuses while a delivery is open, even one that changed nothing (so
    `_resume_pending` is not set)."""
    wf = _validator(_work_item_id=WID, _open_deliveries={"xr_1": OpenDelivery(delivery=_delivery("xr_1"))})
    wf.resume(
        {"work_item_id": WID, "execution_id": "we_2", "surface": "slack", "actor_kind": "human", "actor_id": "u:b"}
    )
    assert [(r.execution_id, r.reason) for r in wf._resume_rejections] == [("we_2", "resume-already-pending")]
    assert wf._executions == [] and "we_2" not in wf._seen_execution_ids


def test_the_validator_takes_a_repeated_request_even_while_it_is_the_open_one():
    open_one = {"xr_1": OpenDelivery(delivery=_delivery("xr_1"))}
    _validator(_accepted_request_ids={"xr_1"}, _open_deliveries=open_one)._validate_execution_request(
        _delivery("xr_1")
    )


# -- reconciliation and candidates understand `#xr_` refs ----------------------


def test_a_resume_engine_ref_names_its_loop_and_is_never_a_workflow_id():
    loop, rid = "dev-loop-mctlhq-mctl-telegram-431", "xr_00000002-0000-4000-8000-000000000461"
    ref = resume_engine_ref(loop, rid)
    assert ref == f"{loop}#{rid}" and is_resume_engine_ref(ref) and loop_id_of_engine_ref(ref) == loop
    assert not is_resume_engine_ref(loop) and loop_id_of_engine_ref(loop) == loop
    assert not is_resume_engine_ref("dev-loop-xr_1#not-a-request")


async def test_the_reconciliation_fails_a_delivered_execution_whose_loop_closed(api, capsys):
    """Behind the issue-keyed loop the label path starts: its id has no
    `dev-loop-xr_` prefix, so only the `#xr_` suffix proves the dispatcher
    wrote this row."""
    loop = workflow_id_for(URL)
    _ledger_entry(api, "temporal", loop, "Succeeded")
    orphan = resume_engine_ref(loop, "xr_00000010-0000-4000-8000-000000000461")
    _ledger_entry(api, "temporal", orphan, "Pending")
    api.create_request("resume")
    temporal = FakeTemporal({loop: dx.LOOP_CLOSED})

    await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert _row(api, orphan)["phase"] == "Failed"
    reconciled = [a for a in _audit(capsys.readouterr().out) if a["event"] == "reconcile"]
    assert [a["workflow_id"] for a in reconciled] == [orphan]


async def test_the_reconciliation_leaves_a_delivered_execution_of_a_running_loop_alone(api):
    loop = workflow_id_for(URL)
    live = resume_engine_ref(loop, "xr_00000010-0000-4000-8000-000000000461")
    _ledger_entry(api, "temporal", live, "Running")
    api.create_request("resume")
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING})

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert _row(api, live)["phase"] == "Running"
    # The candidate is the loop behind the ref, never the ref itself.
    assert [wid for wid, _ in temporal.delivered] == [loop]
    assert outcome.action == dx.DEFERRED and "execution_active" in outcome.reason


async def test_a_resume_goes_to_the_dispatched_loop_behind_a_delivered_ref(api):
    """The ledger's only trace of a live dispatched loop is a delivered
    resume's `<loop>#xr_...` ref: the candidate is the loop before the `#`,
    never the ref itself (which Temporal does not know)."""
    loop = dispatched_workflow_id("xr_00000009-0000-4000-8000-000000000461")
    _ledger_entry(api, "temporal", resume_engine_ref(loop, "xr_00000010-0000-4000-8000-000000000461"), "Succeeded")
    rid = api.create_request("resume")
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING})

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert [wid for wid, _ in temporal.delivered] == [loop] and temporal.started == []
    assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"


async def test_a_refusal_from_the_loop_is_a_typed_reject_and_a_deferral_defers(api):
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    refusal = dx.DeliveryAnswer(dx.DELIVERY_REFUSED, "work-item-mismatch")
    refusing = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=refusal)
    outcome = await dx.Dispatcher(WorkItemClient(), refusing, lease=60).dispatch_once()
    assert outcome.action == dx.REJECTED
    assert api.request_state(rid)["reason"] == f"{xr.RESUME_REFUSED}:work-item-mismatch"
    assert api.fulfils() == [] and refusing.started == []

    later = api.create_request("resume")
    deferring = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=dx.DeliveryAnswer(dx.DELIVERY_DEFERRED, "loop-ending"))
    outcome = await dx.Dispatcher(WorkItemClient(), deferring, lease=60).dispatch_once()
    assert outcome.action == dx.DEFERRED and api.request_state(later)["state"] == "claimed"
    assert api.fulfils() == [] and deferring.started == []


# -- what the port answers the dispatcher --------------------------------------


class _UpdateClient:
    """A Temporal client whose only handle answers `execute_update` with
    `behaviour` (raise it, or await it)."""

    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour

    def get_workflow_handle(self, workflow_id: str) -> Any:
        client = self

        class _Handle:
            async def execute_update(self, *args: Any, **kwargs: Any) -> str:
                if isinstance(client.behaviour, BaseException):
                    raise client.behaviour
                return await client.behaviour()

        return _Handle()


class _RunningPort(dx.TemporalClientPort):
    def __init__(self, client: Any, running: str) -> None:
        super().__init__(client)
        self.running = running

    async def loop_state(self, workflow_id: str) -> str:
        return dx.LOOP_RUNNING if workflow_id == self.running else dx.LOOP_ABSENT


@pytest.mark.parametrize(
    ("details", "reason"),
    [
        ((), xr.RESUME_REFUSAL_UNSPECIFIED),
        (("a-reason-from-a-newer-loop",), xr.RESUME_REFUSAL_UNSPECIFIED),
        (("work-item-mismatch",), "work-item-mismatch"),
    ],
    ids=["no-details", "unknown-reason", "known-reason"],
)
async def test_a_loop_refusal_reaches_a_surface_only_in_the_closed_vocabulary(api, capsys, details, reason):
    """A `ResumeRefused` without details would otherwise surface `str(cause)`,
    free text a surface cannot branch on."""
    from temporalio.client import WorkflowUpdateFailedError

    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    cause = ApplicationError("execution request refused: a sentence", *details, type=RESUME_REFUSED_ERROR_TYPE)
    port = _RunningPort(_UpdateClient(WorkflowUpdateFailedError(cause)), loop)
    outcome = await dx.Dispatcher(WorkItemClient(), port, lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED
    assert api.request_state(rid)["reason"] == f"{xr.RESUME_REFUSED}:{reason}"
    assert api.fulfils() == []


async def test_an_update_that_times_out_defers_with_a_fixed_reason_and_an_audit_line(api, capsys, monkeypatch):
    """The Update round trip outlasts DELIVERY_TIMEOUT_SECONDS: nothing
    permanent is known (the loop may even have accepted it; the next claim
    re-sends the same update id), so the request is deferred, with its
    `deliver` audit line and a fixed reason, never a free-text crash."""
    monkeypatch.setattr(dx, "DELIVERY_TIMEOUT_SECONDS", 0.05)

    async def hang() -> str:
        await asyncio.sleep(5)
        return DELIVERY_ACCEPTED

    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    outcome = await dx.Dispatcher(WorkItemClient(), _RunningPort(_UpdateClient(hang), loop), lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED
    assert outcome.reason == f"delivery to {loop}: {dx.DELIVERY_TIMED_OUT}"
    assert api.request_state(rid)["state"] == "claimed" and api.fulfils() == []
    deliver = [a for a in _audit(capsys.readouterr().out) if a["event"] == "deliver"]
    assert [(a["verdict"], a["reason"]) for a in deliver] == [(dx.DELIVERY_DEFERRED, dx.DELIVERY_TIMED_OUT)]
