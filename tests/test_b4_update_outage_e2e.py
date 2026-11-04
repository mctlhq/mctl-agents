"""B4 / P-9 (mctlhq/mctl-agents#1007): a delivery Update across a worker outage.

Real `DevLoopWorkflow` + real `Dispatcher`/`TemporalClientPort` against a real
local Temporal dev server (no time skipping, unversioned workers). Only the
Argo submit and the mctl-api store are fakes (the same ones the dispatch tests
use).

What it pins, in the order a rollout would hit it:

* positive control: with a worker up, the delivery is accepted at once (so the
  outage cases below are not vacuous);
* outage: the Update is admitted but nobody runs it; the dispatcher's bounded
  wait expires and it DEFERS (never rejects, never mints an execution);
* restore: the late Update executes exactly once on the SAME run, a re-dispatch
  of the same request converges on one execution, and a further dispatch finds
  nothing;
* repeated delivery of one request id under another update id is absorbed by
  `_accepted_request_ids` (the mutation target: with the guard removed this
  module goes red).

Skips (loudly) when the dev server cannot be started, e.g. offline CI.
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.client import WorkflowUpdateFailedError

from orchestrator.temporal import dispatcher as dx
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.issue_ref import request_engine_ref, workflow_id_for
from orchestrator.temporal.start import start_dev_loop_workflow
from orchestrator.temporal.workflows.dev_loop import (
    ACCEPT_EXECUTION_REQUEST_UPDATE,
    DELIVERY_ACCEPTED,
    DevLoopWorkflow,
)
from orchestrator.work_context.client import WorkItemClient
from tests.temporal_harness import Worker
from tests.test_execution_request_dispatch import (
    _delivery,
    _investigate_log,
    _loop_activities,
    _wait_for,
)
from tests.test_execution_request_dispatch import (
    api as api,
)
from tests.test_work_context_resume_acceptance import URL, WID

pytestmark = pytest.mark.anyio

#: Stands in for the production 60 s wait; the outage cases only need it to be
#: shorter than the test, not equal to the real value.
SHORT_WAIT = 3.0
#: Where the test needs the delivery to succeed, not to time out: generous so a
#: slow CI runner cannot turn a positive case red.
LONG_WAIT = 30.0


@pytest.fixture
async def live():
    from temporalio.testing import WorkflowEnvironment

    try:
        env = await WorkflowEnvironment.start_local(dev_server_download_ttl=None)
    except Exception as exc:  # noqa: BLE001 - no dev server binary / offline
        pytest.skip(f"local Temporal dev server unavailable: {exc!r}")
    try:
        yield env
    finally:
        await env.shutdown()


def _worker(client, activities) -> Worker:
    return Worker(client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities)


async def _accepted_updates(client, wid: str) -> int:
    hist = await client.get_workflow_handle(wid).fetch_history()
    return sum(1 for e in hist.events if e.HasField("workflow_execution_update_accepted_event_attributes"))


async def _scheduled(client, wid: str, activity_type: str) -> int:
    hist = await client.get_workflow_handle(wid).fetch_history()
    return sum(
        1
        for e in hist.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name == activity_type
    )


async def _wait_for_async(read, want: int, limit: float) -> None:
    import anyio

    for _ in range(int(limit / 0.25)):
        if await read() >= want:
            return
        await anyio.sleep(0.25)
    raise AssertionError(f"{want} not reached within {limit}s")


async def _park_loop(client, acts, seen):
    """Start the loop and wait until it parks at its approval gate."""
    handle = await start_dev_loop_workflow(URL, client=client)
    await _wait_for(lambda: len(seen) == 1, 40)
    return handle, (await handle.describe()).run_id


async def test_positive_control_delivery_is_accepted_with_a_live_worker(api: Any, live, monkeypatch):
    monkeypatch.setattr(dx, "DELIVERY_TIMEOUT_SECONDS", LONG_WAIT)  # no timeout wanted here
    client = live.client
    submit, seen = _investigate_log()
    acts = _loop_activities(submit)
    wid = workflow_id_for(URL)
    async with _worker(client, acts):
        _, run_id = await _park_loop(client, acts, seen)
        rid = api.create_request("resume")
        disp = dx.Dispatcher(WorkItemClient(), dx.TemporalClientPort(client), lease=60)
        out = await disp.dispatch_once()
        assert out.action == dx.FULFILLED, (out.action, getattr(out, "reason", None))
        assert out.engine_ref == request_engine_ref(wid, rid)
        assert await _accepted_updates(client, wid) == 1
        assert (await client.get_workflow_handle(wid).describe()).run_id == run_id
        assert len(api.executions) == 1 and len(seen) == 1
        handle = client.get_workflow_handle(wid)
        await handle.signal(DevLoopWorkflow.abandon, {"reason": "test over"})
        await handle.result()


async def test_late_update_after_outage_is_deferred_then_executes_exactly_once(api: Any, live, monkeypatch):
    monkeypatch.setattr(dx, "DELIVERY_TIMEOUT_SECONDS", SHORT_WAIT)
    client = live.client
    submit, seen = _investigate_log()
    acts = _loop_activities(submit)
    wid = workflow_id_for(URL)
    async with _worker(client, acts):
        _, run_id = await _park_loop(client, acts, seen)
    # -- outage: no worker polls the queue ---------------------------------
    rid = api.create_request("resume")
    disp = dx.Dispatcher(WorkItemClient(), dx.TemporalClientPort(client), lease=60)
    out = await disp.dispatch_once()
    assert out.action == dx.DEFERRED, (out.action, getattr(out, "reason", None))
    # The documented deferral (fixed reason), not the generic crash path: the
    # SDK reports the cancelled wait as WorkflowUpdateRPCTimeoutOrCancelledError.
    assert out.reason == f"delivery to {wid}: {dx.DELIVERY_TIMED_OUT}", out.reason
    assert api.executions == []  # nothing minted for an undelivered request
    assert api.request_state(rid).get("state") == "claimed"
    api.now += 500  # the store's clock: the deferred claim's lease lapses
    # -- restore ------------------------------------------------------------
    async with _worker(client, acts):
        # The Update admitted during the outage is accepted by the restored
        # worker on its own, before any re-dispatch sends it again.
        await _wait_for_async(lambda: _accepted_updates(client, wid), 1, 30)
        outcomes = []
        for _ in range(6):
            o = await disp.dispatch_once()
            outcomes.append(o.action)
            if o.action == dx.FULFILLED:
                break
            api.now += 500
        assert outcomes[-1] == dx.FULFILLED, outcomes
        await _wait_for(lambda: bool(api.executions), 30)
        # exactly-once: one accepted Update on the SAME run, one execution,
        # one investigation, and the request is not served again.
        assert await _accepted_updates(client, wid) == 1
        assert (await client.get_workflow_handle(wid).describe()).run_id == run_id
        assert len(api.executions) == 1 and len(seen) == 1
        assert api.executions[0]["engine_ref"] == request_engine_ref(wid, rid)
        # Running, not just not-Succeeded: the phase is Pending right after
        # the fulfil, where "not Succeeded" cannot fail.
        await _wait_for(lambda: api.executions[0]["phase"] == "Running", 30)
        assert api.executions[0]["phase"] != "Succeeded"  # never a false success
        api.now += 500
        third = await disp.dispatch_once()
        assert third.action == dx.NOTHING
        assert len(api.executions) == 1
        handle = client.get_workflow_handle(wid)
        await handle.signal(DevLoopWorkflow.abandon, {"reason": "test over"})
        await handle.result()


async def test_repeated_delivery_of_one_request_is_absorbed(api: Any, live, monkeypatch):
    """Same request id, a DIFFERENT update id: Temporal's own update-id dedup
    cannot help, only `_accepted_request_ids` does (the mutation target).
    Both guards (validator and handler) are on this path; removing either one
    turns it red, though the validator guard is caught through the
    `resume-already-pending` refusal rather than in isolation. The same set
    carried across continue-as-new is NOT exercised here."""
    monkeypatch.setattr(dx, "DELIVERY_TIMEOUT_SECONDS", LONG_WAIT)
    client = live.client
    submit, seen = _investigate_log()
    acts = _loop_activities(submit)
    wid = workflow_id_for(URL)
    async with _worker(client, acts):
        await _park_loop(client, acts, seen)
        rid = api.create_request("resume")
        disp = dx.Dispatcher(WorkItemClient(), dx.TemporalClientPort(client), lease=60)
        out = await disp.dispatch_once()
        assert out.action == dx.FULFILLED
        await _wait_for(lambda: bool(api.executions), 30)
        handle = client.get_workflow_handle(wid)
        again = _delivery(rid, "resume", WID)
        try:
            answer = await handle.execute_update(
                ACCEPT_EXECUTION_REQUEST_UPDATE, again, id="another-update-id", result_type=str
            )
        except WorkflowUpdateFailedError as exc:  # guard gone: refused as pending
            pytest.fail(f"repeated delivery of {rid} was not absorbed: {exc.cause!r}")
        assert answer == DELIVERY_ACCEPTED
        assert len(api.executions) == 1 and len(seen) == 1
        # The execution is idempotent downstream, so also pin the loop itself:
        # one delivery task, hence ONE bind, however many times it was sent.
        assert await _scheduled(client, wid, "bind_dispatched_execution") == 1
        await handle.signal(DevLoopWorkflow.abandon, {"reason": "test over"})
        await handle.result()
