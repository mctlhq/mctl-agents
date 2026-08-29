"""ReconcileWorkflow orchestration tests (#151): the active DevLoop set
comes from the list_active_dev_loop_ids visibility activity and feeds
detect_orphans; a failing visibility query skips orphan detection for the
tick instead of reporting every actionable proposal as an orphan.

Runs the real workflow definition against temporalio's time-skipping test
environment with fake activities, same harness as test_dev_loop_workflow.py.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal.activities.discovery import ReconcileDiscoveryResult
from orchestrator.temporal.activities.orphans import OrphanDetectionResult, OrphanSignal
from orchestrator.temporal.workflows.reconcile import ReconcileWorkflow, ReconcileWorkflowInput

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-reconcile"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _fake_activities(*, active_ids: list[str] | None = None, visibility_fails: bool = False):
    received: dict = {}

    @activity.defn(name="discover_and_project")
    async def fake_discover_and_project(state_dir_path: str) -> ReconcileDiscoveryResult:
        return ReconcileDiscoveryResult(total_inspected=1, projections=[])

    @activity.defn(name="list_active_dev_loop_ids")
    async def fake_list_active_dev_loop_ids() -> list[str]:
        if visibility_fails:
            raise ApplicationError("visibility unavailable", non_retryable=True)
        return active_ids or []

    @activity.defn(name="detect_orphans")
    async def fake_detect_orphans(
        state_dir_path: str = "", active_workflow_ids: list[str] | None = None
    ) -> OrphanDetectionResult:
        received["active_workflow_ids"] = active_workflow_ids
        return OrphanDetectionResult(
            total_actionable=1,
            orphans=[
                OrphanSignal(
                    service="mctl-web",
                    slug="issue-10-test",
                    status="accepted",
                    pr_url=None,
                    reason="No active DevLoopWorkflow found for open PR proposal",
                )
            ],
        )

    return [fake_discover_and_project, fake_list_active_dev_loop_ids, fake_detect_orphans], received


class TestReconcileWorkflow:
    async def test_active_ids_flow_into_detect_orphans(self, env):
        activities, received = _fake_activities(active_ids=["dev-loop-mctlhq-mctl-web-10"])
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ReconcileWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                ReconcileWorkflow.run,
                ReconcileWorkflowInput(state_dir_path="/tmp/state"),
                id=f"reconcile-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

        assert received["active_workflow_ids"] == ["dev-loop-mctlhq-mctl-web-10"]
        assert result.discovery.total_inspected == 1
        assert len(result.orphans.orphans) == 1
        # A tick that actually ran detection carries no skipped marker.
        assert result.orphans.skipped_reason is None

    async def test_visibility_failure_skips_orphan_detection(self, env):
        """Unknown active set → no orphan report this tick, not a page for
        every actionable proposal. detect_orphans must not run at all."""
        activities, received = _fake_activities(visibility_fails=True)
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ReconcileWorkflow],
            activities=activities,
        ):
            result = await env.client.execute_workflow(
                ReconcileWorkflow.run,
                ReconcileWorkflowInput(state_dir_path="/tmp/state"),
                id=f"reconcile-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

        assert "active_workflow_ids" not in received
        assert result.orphans.orphans == []
        assert result.orphans.total_actionable == 0
        # A skipped tick is distinguishable from a genuinely clean one, and
        # the reason carries the underlying error for the on-call engineer.
        assert result.orphans.skipped_reason is not None
        assert "visibility" in result.orphans.skipped_reason
        # Discovery still ran and is reported.
        assert result.discovery.total_inspected == 1
