"""Worker role selection — the first slice of the queue split (ADR-008, #152).

Nothing routes to the execution queue yet: the flip is a later step behind
`workflow.patched("exec-queue")`, because a worker has to be POLLING a
queue before a workflow may schedule onto it. What these tests pin is the
half that ships now — that `--role all` is byte-for-byte the old
behaviour, and that the two new roles register the right things on the
right queues.

These assert on `worker_plans`, the pure function that decides the layout,
rather than on a constructed `Worker`. That is not a convenience: a real
Worker insists on a live bridge client and dials on construction, so the
routing decision is only unit-testable once it is separated from the
object that acts on it.
"""
from __future__ import annotations

import asyncio
import inspect
import signal
from unittest.mock import MagicMock

import pytest

from orchestrator.temporal.constants import (
    CONTROL_MAX_CONCURRENT_ACTIVITIES,
    CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS,
    EXECUTION_MAX_CONCURRENT_ACTIVITIES,
    EXECUTION_TASK_QUEUE,
    TASK_QUEUE,
)
from orchestrator.temporal.worker import owns_schedules, run_until_signalled, worker_plans


@pytest.fixture
def visibility():
    stub = MagicMock()
    stub.list_active_dev_loop_ids = _named_activity("list_active_dev_loop_ids")
    return stub


def _named_activity(name):
    from temporalio import activity

    @activity.defn(name=name)
    async def _fn() -> None:
        return None

    return _fn


def test_all_keeps_the_original_control_queue_shape(visibility):
    """`all` must still be today's worker — step 1 is a no-op in production."""
    control = next(p for p in worker_plans("all", visibility) if p.task_queue == TASK_QUEUE)

    assert "submit_and_wait" in control.activity_names
    assert "find_proposal_slug" in control.activity_names
    assert control.max_concurrent_activities is None


def test_all_also_polls_the_execution_queue(visibility):
    """`all` is the documented rollback target, so it has to work as one.

    After step 3, patched histories schedule submit_and_wait onto the
    execution queue. Collapsing the split deployments back to a process
    that listens only on the control queue would leave those activities
    with no poller until they time out — a rollback that strands work is
    not a rollback (codex P1 on #249).
    """
    queues = {p.task_queue for p in worker_plans("all", visibility)}

    assert queues == {TASK_QUEUE, EXECUTION_TASK_QUEUE}


def test_the_execution_worker_polls_only_the_new_queue(visibility):
    """It services activities scheduled by workflows it does not run."""
    plans = worker_plans("execution", visibility)

    assert [p.task_queue for p in plans] == [EXECUTION_TASK_QUEUE]
    assert plans[0].activity_names == {"submit_and_wait"}


def test_the_control_worker_still_serves_submit_and_wait(visibility):
    """The ordering constraint that makes step 1 releasable on its own.

    Nothing routes to the execution queue until the patched flip lands, so
    a control worker that dropped submit_and_wait would strand every Argo
    submit the moment it rolled out — a self-inflicted outage in the gap
    between two PRs.
    """
    plans = worker_plans("control", visibility)

    assert [p.task_queue for p in plans] == [TASK_QUEUE]
    assert "submit_and_wait" in plans[0].activity_names


def test_slot_limits_are_set_for_the_split_roles(visibility):
    """Explicit limits are the point: an unbounded pool is what turns
    exhaustion into an invisible stall instead of a visible backlog."""
    control = worker_plans("control", visibility)[0]
    execution = worker_plans("execution", visibility)[0]

    assert control.max_concurrent_activities == CONTROL_MAX_CONCURRENT_ACTIVITIES
    assert control.max_concurrent_workflow_tasks == CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS
    assert execution.max_concurrent_activities == EXECUTION_MAX_CONCURRENT_ACTIVITIES
    # The execution worker runs no workflows, so a workflow-task limit there
    # would be a number with nothing to bound.
    assert execution.max_concurrent_workflow_tasks is None


def test_an_unknown_role_is_refused(visibility):
    """argparse guards the CLI, but worker_plans is also called directly."""
    with pytest.raises(SystemExit):
        worker_plans("orchestration", visibility)


def test_only_workflow_running_roles_own_the_schedules():
    """An execution worker must not assert a spec for workflows it does not run.

    Not merely redundant: `_ensure_schedule` converges an existing spec in
    place, so a role declaring a cadence it does not serve would actively
    overwrite the real one.
    """
    assert owns_schedules("all") is True
    assert owns_schedules("control") is True
    assert owns_schedules("execution") is False


def test_no_control_ceiling_is_lowered_before_the_routing_flip():
    """Between step 2 and step 3 the control worker still carries every long
    Argo poll AND all five workflow types. Any ceiling below current
    behaviour in that window reintroduces the starvation the split removes
    (claude P2 on #249, twice — once per limit). Step 3 tightens them in
    the same change that takes the workload away.

    Both limits, because fixing one and leaving the sibling is exactly how
    this was got wrong the first time. Note the workflow-task default is
    not 100: unset, the SDK builds a 500-thread pool, so any number there
    is a much bigger step down than it looks.
    """
    assert CONTROL_MAX_CONCURRENT_ACTIVITIES >= 100
    assert CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS is None


# ---------------------------------------------------------------------------
# Graceful shutdown across every worker in the process (agy P1 on #249)
# ---------------------------------------------------------------------------
class _FakeWorker:
    """Mimics the SDK pair this design uses: run() blocks, shutdown() ends it."""

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self._stop = asyncio.Event()
        self._fail_with = fail_with
        self.ran = False
        self.shut_down = False

    async def run(self) -> None:
        self.ran = True
        if self._fail_with is not None:
            raise self._fail_with
        await self._stop.wait()

    async def shutdown(self) -> None:
        self.shut_down = True
        self._stop.set()


@pytest.mark.anyio
async def test_every_worker_is_drained_on_shutdown():
    """Both workers must drain, not just one.

    `--role all` runs two workers in one process. The SDK installs no
    signal handlers of its own, so without this the pod's SIGTERM ended
    the process outright and in-flight activities were cut mid-flight.
    """
    workers = [_FakeWorker(), _FakeWorker()]

    task = asyncio.create_task(run_until_signalled(workers))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    assert all(w.ran for w in workers)
    assert all(w.shut_down for w in workers)


@pytest.mark.anyio
async def test_a_worker_dying_alone_takes_the_process_down():
    """A dead poller must not leave the process looking healthy.

    The pre-split code got this from a bare `await worker.run()`. With two
    workers it has to be arranged: if only the control worker's loop dies,
    the execution worker keeps the process alive while reconcile, intake
    and every DevLoop go dark — and nothing restarts, because nothing
    crashed (claude P1 / codex P1 on #249).
    """
    healthy = _FakeWorker()
    doomed = _FakeWorker(fail_with=RuntimeError("connection lost"))

    with pytest.raises(RuntimeError, match="connection lost"):
        await asyncio.wait_for(
            run_until_signalled([healthy, doomed]),  # type: ignore[arg-type]
            timeout=5,
        )

    # ...and the survivor is drained rather than abandoned mid-flight.
    assert healthy.shut_down


def test_the_sdk_context_manager_really_starts_and_drains_a_worker():
    """Pin the SDK contract run_until_signalled depends on.

    `async with worker:` is what starts polling here — `Worker.__aenter__`
    is documented as "a wrapper around run()" and schedules `self.run()`,
    and `__aexit__` awaits `shutdown()`. Reviewed as a P1 on the claim
    that Worker implements no context-manager protocol and that the
    process would therefore start and do nothing; it does, and it would
    not. But the fake above cannot tell the difference, so if a future SDK
    upgrade changed this the fake would keep passing while production
    silently stopped polling.

    Asserting on source is deliberate. The behaviour needs a live Temporal
    to exercise, and the thing worth catching is the SDK changing under us
    — at which point this fails and says to go re-read it.
    """
    from temporalio.worker import Worker

    assert "__aenter__" in vars(Worker), "Worker no longer defines its own __aenter__"
    assert "await self.run()" in inspect.getsource(Worker.__aenter__)
    assert "await self.shutdown()" in inspect.getsource(Worker.__aexit__)
