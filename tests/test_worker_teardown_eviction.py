"""A worker that stops with a run still cached must not leave that run's
coroutines to the garbage collector (mctlhq/mctl-agents#912).

`test_todays_canonical_loop_replays_its_own_history[issue_started]` failed
once in CI with

    [TMPRL1100] Nondeterminism error: Activity type of scheduled event
    'ensure_issue_execution' does not match activity type of activity
    command 'lifecycle_ownership'

on code that replays its own history cleanly every other time. The command
was not the replayed run's. test_execution_request_dispatch.py, which runs
just before, terminates DevLoopWorkflows in their merge watch and stops
their worker; nothing ever evicts those runs; and a full collection that
finalizes one inside the Replayer's activation makes `_watch_pr`'s
`finally` schedule its ownership release on the workflow that thread is
replaying -- the CI error, verbatim, once that collection is forced. The
mechanism and the harness's answer to it are written up in
tests/temporal_harness.py, and conftest installs that answer for every SDK
worker the suite stops.

These tests pin it down without depending on when the collector happens to
run: `gc.disable()` keeps the leftover alive, and the collection that
finalizes it is one the replayed workflow runs itself, from inside its own
activation -- the place where finalizing it turns the dead run's cleanup
into the replayed run's commands.
"""
from __future__ import annotations

import asyncio
import gc
import uuid
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from tests import temporal_harness

pytestmark = pytest.mark.anyio

TIMEOUT = timedelta(seconds=10)


@activity.defn
async def release_claim() -> None:
    """Stands in for `lifecycle_ownership(release)`."""


@activity.defn
async def first_step() -> None:
    """Stands in for `ensure_issue_execution`, the replayed run's first activity."""


@workflow.defn(name="ParkedWithCleanup", sandboxed=False)
class ParkedWithCleanup:
    """The run its worker leaves behind: parked on a timer, with a `finally`
    that schedules an activity -- `_watch_pr` between two polls."""

    def __init__(self) -> None:
        self._parked = False

    @workflow.run
    async def run(self) -> None:
        try:
            self._parked = True
            await workflow.sleep(timedelta(hours=1))
        finally:
            await workflow.execute_activity(release_claim, start_to_close_timeout=TIMEOUT)

    @workflow.query
    def parked(self) -> bool:
        return self._parked


@workflow.defn(name="CollectsThenSteps", sandboxed=False)
class CollectsThenSteps:
    """The replayed run: a full collection inside its own activation, then
    its first activity. With nothing left over, the collection reaches no
    workflow API, so the workflow stays deterministic."""

    @workflow.run
    async def run(self) -> None:
        gc.collect()
        await workflow.execute_activity(first_step, start_to_close_timeout=TIMEOUT)


async def _until_parked(handle: WorkflowHandle) -> None:
    for _ in range(200):
        if await handle.query(ParkedWithCleanup.parked):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the run never reached its timer")


async def test_a_run_left_cached_by_a_stopped_worker_cannot_write_into_another_workflow():
    # Whatever an earlier test left is finalized here, outside any workflow.
    gc.collect()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"teardown-eviction-{uuid.uuid4()}"
        async with Worker(env.client, task_queue=queue, workflows=[CollectsThenSteps], activities=[first_step]):
            replayed = await env.client.start_workflow(
                CollectsThenSteps.run, id=f"replayed-{uuid.uuid4()}", task_queue=queue
            )
            await replayed.result()
        history = await replayed.fetch_history()

        gc.disable()
        try:
            async with Worker(env.client, task_queue=queue, workflows=[ParkedWithCleanup], activities=[release_claim]):
                parked = await env.client.start_workflow(
                    ParkedWithCleanup.run, id=f"parked-{uuid.uuid4()}", task_queue=queue
                )
                await _until_parked(parked)
                await parked.terminate("test over")
            # Nothing evicted the parked run when its worker stopped. Left to
            # the collector, its suspended coroutines are finalized by the
            # replayed run's own gc.collect(), inside the replay, and their
            # `finally` schedules `release_claim` as that run's first activity:
            # "scheduled event 'first_step' does not match ... 'release_claim'".
            await Replayer(workflows=[CollectsThenSteps]).replay_workflow(history)
        finally:
            gc.enable()


async def test_a_run_its_eviction_cannot_tear_down_is_reported_not_dropped():
    class Unevictable:
        def activate(self, _activation: object) -> None:
            raise RuntimeError("Eviction processed, but 1 tasks remain.")

    class StoppedWorker:
        def __init__(self) -> None:
            self._running_workflows = {"run-1": Unevictable()}

    stopped = StoppedWorker()
    try:
        await temporal_harness.evict_left_over_runs(stopped)
        # An SDK that keeps its run cache somewhere else.
        await temporal_harness.evict_left_over_runs(object())

        failures = list(temporal_harness.EVICTION_FAILURES)
        assert stopped._running_workflows == {}
        assert failures[0] == "run-1: RuntimeError('Eviction processed, but 1 tasks remain.')"
        assert failures[1].startswith("cannot see the stopped worker's cached runs: AttributeError(")
        assert len(failures) == 2
    finally:
        # Recorded on purpose; `_unevicted_runs_fail_the_test` would fail this test for them.
        temporal_harness.EVICTION_FAILURES.clear()
