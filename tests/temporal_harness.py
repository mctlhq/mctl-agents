"""A test Worker that polls every queue, as production does (#251, ADR-008, #395).

Once `submit_and_wait` is routed to `EXECUTION_TASK_QUEUE`, a test harness
that runs a single Worker on its own queue schedules that activity onto a
queue nobody polls. Nothing fails: the activity simply never starts, the
workflow waits on it, and the test hangs until the suite is killed. That is
the same failure ADR-008's rollout order exists to prevent in production —
"a worker polling the new queue must exist BEFORE any workflow routes work
to it" — showing up in the tests, which is the right place for it to show up.

Rather than adding a second `Worker(...)` at all 45 construction sites, this
is a drop-in replacement: the workflow test modules import `Worker` from here
instead of from `temporalio.worker`, and every existing `async with
Worker(...)` keeps working unchanged.

The split mirrors `worker_plans("all")` in orchestrator/temporal/worker.py:
workflows and every activity on the control queue, and ONLY submit_and_wait
on the execution queue and on the implementation (admission) queue.
Deliberately not "all activities on every queue" — that would make a
misrouted activity work in tests and fail in production, which is precisely
the class of bug this harness exists to expose.

The implementation worker carries the SAME slot limit production does, so a
test that approves more loops than N can observe the admission it is
testing rather than a pool that admits everything.

The second half of this module is the teardown every SDK worker in the
suite gets when it stops (#912): see `evict_left_over_runs`.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from temporalio.bridge.proto import workflow_activation
from temporalio.worker import Worker as _TemporalWorker

from orchestrator.temporal.constants import (
    EXECUTION_TASK_QUEUE,
    IMPLEMENTATION_TASK_QUEUE,
    implementation_max_concurrent_activities,
)

# The one activity the execution worker registers. Kept as a literal rather
# than imported from the workflow modules: the point is to mirror what
# worker.py's execution_plan registers, and a shared import would let both
# sides move together without anything noticing.
EXECUTION_ACTIVITY = "submit_and_wait"
# #636: the delegated approve flip runs beside it on the execution queue only
# (it mints a credential, so it is not offered to the admission pool).
EXECUTION_ONLY_ACTIVITIES = frozenset({"submit_delegated_approve"})


def _activity_name(fn: Any) -> str | None:
    definition = getattr(fn, "__temporal_activity_definition", None)
    return getattr(definition, "name", None)


class Worker:
    """`temporalio.worker.Worker`, plus the execution- and implementation-queue
    workers beside it."""

    def __init__(
        self,
        client: Any,
        *,
        task_queue: str,
        workflows: list | None = None,
        activities: list | None = None,
        **kwargs: Any,
    ) -> None:
        activities = list(activities or [])
        self._control = _TemporalWorker(
            client,
            task_queue=task_queue,
            workflows=list(workflows or []),
            activities=activities,
            **kwargs,
        )
        routed = [fn for fn in activities if _activity_name(fn) == EXECUTION_ACTIVITY]
        execution_routed = routed + [fn for fn in activities if _activity_name(fn) in EXECUTION_ONLY_ACTIVITIES]
        # Nothing registered for the execution queue means this test never
        # routes anything there, so a second worker would only slow it down.
        # Decided by the execution queue's own list (claude P3 on #651): a
        # test that fakes only the delegated flip still needs its poller.
        self._execution = (
            _TemporalWorker(
                client,
                task_queue=EXECUTION_TASK_QUEUE,
                activities=execution_routed,
                **kwargs,
            )
            if execution_routed
            else None
        )
        # The admission limit is a default a caller may override, not a
        # second value for the same keyword: a test that passes
        # max_concurrent_activities through Worker(...) already reaches the
        # control and execution workers via **kwargs, and must reach this
        # one the same way rather than raise TypeError (agy P2 on #397).
        implementation_kwargs = {
            "max_concurrent_activities": implementation_max_concurrent_activities(),
            **kwargs,
        }
        self._implementation = (
            _TemporalWorker(
                client,
                task_queue=IMPLEMENTATION_TASK_QUEUE,
                activities=routed,
                **implementation_kwargs,
            )
            if routed
            else None
        )
        self._entered: list[_TemporalWorker] = []

    @property
    def workers(self) -> list[_TemporalWorker]:
        """The real SDK workers this harness drives, in start order."""
        return [w for w in (self._control, self._execution, self._implementation) if w is not None]

    async def __aenter__(self) -> Worker:
        # Start them in order and tear down whatever already started if a
        # later one fails. `async with` never binds the target on a raising
        # __aenter__, so __aexit__ would never run and the started workers
        # would keep polling for the rest of the process — which in this
        # suite shows up as an unrelated test hanging on a task some ghost
        # worker already took. Cheap insurance against an expensive symptom
        # (claude P3 on #282).
        for worker in self.workers:
            try:
                await worker.__aenter__()
            except BaseException as startup_error:
                # Same unwind as __aexit__: every started worker, even if
                # one of them refuses to stop (agy P3 on #397). The startup
                # error is the one worth seeing; a teardown error must not
                # replace it, so it rides along as context.
                try:
                    await self._exit_entered(None, None, None)
                except BaseException as teardown_error:
                    raise startup_error from teardown_error
                raise
            self._entered.append(worker)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._exit_entered(*exc)

    async def _exit_entered(self, *exc: Any) -> None:
        # Reverse order, and every one of them even if an earlier exit
        # raises — a worker left polling is the hang described above.
        first_error: BaseException | None = None
        for worker in reversed(self._entered):
            try:
                await worker.__aexit__(*exc)
            except BaseException as error:  # noqa: BLE001 - re-raised below
                first_error = first_error or error
        self._entered.clear()
        if first_error is not None:
            raise first_error


# --- What a stopped worker leaves cached (mctlhq/mctl-agents#912) -----------
#
# Neither Core nor the SDK evicts a cached run when a worker shuts down. Core
# reports shutdown once no run has pending work, and a run parked on a timer
# has none; `_WorkflowWorker.run` then leaves its poll loop with the instance
# still in `_running_workflows`. Production never notices: a process runs at
# most one worker with workflows (`worker_plans` in
# orchestrator/temporal/worker.py) and exits once it stops. The suite stops
# worker after worker in one process, and each leftover -- the instance, its
# event loop, every coroutine suspended in it -- becomes cyclic garbage that
# whichever thread the collector next runs in finalizes.
#
# A finalized coroutine runs its `finally` blocks, and the SDK finds "the
# current workflow" through `asyncio.get_running_loop()`. When the collector
# runs inside another workflow's activation -- the thread that activation
# runs in has that workflow installed as its running loop -- the dead run's
# cleanup schedules its commands on THAT workflow. The SDK's own eviction
# exists to rule this out ("no GC'ing of the tasks occurs (which can cause
# them to wake up in different threads)", temporalio/worker/_workflow.py);
# a worker that shuts down drops its cache without one.
#
# #912 is this firing. test_execution_request_dispatch.py, which runs just
# before the replay tests, terminates DevLoopWorkflows in their merge watch
# with a lifecycle claim held and stops their worker. Once a full collection
# lands inside a Replayer activation, `_watch_pr`'s `finally` schedules
# `lifecycle_ownership` (op=release) as the replayed run's first activity --
# reproduced verbatim by forcing that collection:
#
#   [TMPRL1100] Nondeterminism error: Activity type of scheduled event
#   'ensure_issue_execution' does not match activity type of activity
#   command 'lifecycle_ownership'
#
# So the harness finishes what shutdown skips: once a worker's poll loop is
# over, every run it still caches gets the RemoveFromCache activation a
# normal eviction applies. That cancels the run's tasks inside its OWN loop,
# where every command is refused, and leaves nothing for the collector.

#: Runs `evict_left_over_runs` could not tear down, as "<run id>: <error>".
#: conftest fails the test that stopped the worker on a non-empty list: a run
#: that survives its eviction is the #912 hazard still armed, and a teardown
#: that failed quietly is how it went unseen in the first place.
EVICTION_FAILURES: list[str] = []


async def evict_left_over_runs(workflow_worker: Any) -> None:
    """Evict every run an SDK `_WorkflowWorker` whose poll loop has ended
    still holds, the way its own cache eviction would."""
    try:
        running = workflow_worker._running_workflows
    except AttributeError as error:
        # An SDK that moved its run cache must not turn this into a no-op.
        EVICTION_FAILURES.append(f"cannot see the stopped worker's cached runs: {error!r}")
        return
    while running:
        run_id, run = running.popitem()
        eviction = workflow_activation.WorkflowActivation(
            run_id=run_id,
            is_replaying=True,
            jobs=[
                workflow_activation.WorkflowActivationJob(
                    remove_from_cache=workflow_activation.RemoveFromCache(
                        message="test worker stopped with this run still cached (#912)",
                        reason=workflow_activation.RemoveFromCache.EvictionReason.LANG_REQUESTED,
                    )
                )
            ],
        )
        try:
            # On an executor thread, as the SDK runs every activation: the
            # instance installs itself as that thread's running loop.
            await asyncio.get_running_loop().run_in_executor(None, run.activate, eviction)
        except Exception as error:  # noqa: BLE001 - reported through EVICTION_FAILURES
            # The SDK raises "Eviction processed, but N tasks remain" for a
            # run whose coroutines outlive the cancellation.
            EVICTION_FAILURES.append(f"{run_id}: {error!r}")


def evicting_after_shutdown(sdk_run: Callable[[Any], Awaitable[None]]) -> Callable[[Any], Awaitable[None]]:
    """`_WorkflowWorker.run`, followed by `evict_left_over_runs` however the
    poll loop ended. Installed for the whole session by conftest."""

    async def run(self: Any) -> None:
        try:
            await sdk_run(self)
        finally:
            await evict_left_over_runs(self)

    return run
