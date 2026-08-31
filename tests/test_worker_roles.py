"""Worker role selection — the first slice of the queue split (ADR-008, #152).

Nothing routes to the execution queue yet: the flip is a later step behind
`workflow.patched("exec-queue")`, because a worker has to be POLLING a
queue before a workflow may schedule onto it. What these tests pin is the
half that ships now — that `--role all` is byte-for-byte the old
behaviour, and that the two new roles register the right things on the
right queues.

These assert on `worker_plan`, the pure function that decides the layout,
rather than on a constructed `Worker`. That is not a convenience: a real
Worker insists on a live bridge client and dials on construction, so the
routing decision is only unit-testable once it is separated from the
object that acts on it.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from orchestrator.temporal.constants import (
    CONTROL_MAX_CONCURRENT_ACTIVITIES,
    CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS,
    EXECUTION_MAX_CONCURRENT_ACTIVITIES,
    EXECUTION_TASK_QUEUE,
    TASK_QUEUE,
)
from orchestrator.temporal.worker import worker_plan


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


def test_all_is_the_default_shape_and_keeps_one_queue(visibility):
    """The rollback target. `all` must not change under this PR — the whole
    point of shipping the flag before the routing is that step 1 is a
    no-op in production."""
    worker = worker_plan("all", visibility)

    assert worker.task_queue == TASK_QUEUE
    assert "submit_and_wait" in worker.activity_names
    assert "find_proposal_slug" in worker.activity_names


def test_the_execution_worker_polls_only_the_new_queue(visibility):
    """It services activities scheduled by workflows it does not run."""
    worker = worker_plan("execution", visibility)

    assert worker.task_queue == EXECUTION_TASK_QUEUE
    assert worker.activity_names == {"submit_and_wait"}


def test_the_control_worker_still_serves_submit_and_wait(visibility):
    """The ordering constraint that makes step 1 releasable on its own.

    Nothing routes to the execution queue until the patched flip lands, so
    a control worker that dropped submit_and_wait would strand every Argo
    submit the moment it rolled out — a self-inflicted outage in the gap
    between two PRs.
    """
    worker = worker_plan("control", visibility)

    assert worker.task_queue == TASK_QUEUE
    assert "submit_and_wait" in worker.activity_names


def test_slot_limits_are_set_for_the_split_roles(visibility):
    """Explicit limits are the point: an unbounded pool is what turns
    exhaustion into an invisible stall instead of a visible backlog."""
    control = worker_plan("control", visibility)
    execution = worker_plan("execution", visibility)

    assert control.max_concurrent_activities == CONTROL_MAX_CONCURRENT_ACTIVITIES
    assert control.max_concurrent_workflow_tasks == CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS
    assert execution.max_concurrent_activities == EXECUTION_MAX_CONCURRENT_ACTIVITIES
    # The execution worker runs no workflows, so a workflow-task limit there
    # would be a number with nothing to bound.
    assert execution.max_concurrent_workflow_tasks is None


def test_an_unknown_role_is_refused(visibility):
    """argparse guards the CLI, but worker_plan is also called directly."""
    with pytest.raises(SystemExit):
        worker_plan("orchestration", visibility)
