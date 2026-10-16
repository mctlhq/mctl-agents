"""The test Worker's queue split (tests/temporal_harness.py)."""
from __future__ import annotations

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.delegated_approval import DelegatedApproveInput, DelegatedApproveResult
from orchestrator.temporal.constants import TASK_QUEUE
from tests.temporal_harness import Worker

pytestmark = pytest.mark.anyio


async def test_a_worker_with_only_the_delegated_flip_still_polls_the_execution_queue():
    """claude P3 on #651: the delegated approve flip runs on the execution
    queue only, so a test that fakes it without `submit_and_wait` must still
    get that queue's poller; otherwise the activity is never picked up and
    the test times out instead of failing."""

    @activity.defn(name="submit_delegated_approve")
    async def delegated(inp: DelegatedApproveInput) -> DelegatedApproveResult:
        raise AssertionError("not run")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker = Worker(env.client, task_queue=TASK_QUEUE, activities=[delegated])

    assert worker._execution is not None
    assert worker._implementation is None  # never offered to the admission pool
