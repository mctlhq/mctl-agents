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
"""
from __future__ import annotations

from typing import Any

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
        # No submit_and_wait fake means this test never routes anything, so
        # a second worker would only slow it down.
        self._execution = (
            _TemporalWorker(
                client,
                task_queue=EXECUTION_TASK_QUEUE,
                activities=routed,
                **kwargs,
            )
            if routed
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
