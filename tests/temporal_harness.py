"""A test Worker that polls both queues, as production does (#251, ADR-008).

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
on the execution queue. Deliberately not "all activities on both" — that
would make a misrouted activity work in tests and fail in production, which
is precisely the class of bug this harness exists to expose.
"""
from __future__ import annotations

from typing import Any

from temporalio.worker import Worker as _TemporalWorker

from orchestrator.temporal.constants import EXECUTION_TASK_QUEUE

# The one activity the execution worker registers. Kept as a literal rather
# than imported from the workflow modules: the point is to mirror what
# worker.py's execution_plan registers, and a shared import would let both
# sides move together without anything noticing.
EXECUTION_ACTIVITY = "submit_and_wait"


def _activity_name(fn: Any) -> str | None:
    definition = getattr(fn, "__temporal_activity_definition", None)
    return getattr(definition, "name", None)


class Worker:
    """`temporalio.worker.Worker`, plus an execution-queue worker beside it."""

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

    async def __aenter__(self) -> Worker:
        await self._control.__aenter__()
        if self._execution is not None:
            await self._execution.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self._execution is not None:
                await self._execution.__aexit__(*exc)
        finally:
            await self._control.__aexit__(*exc)
