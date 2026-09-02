"""ReconcileWorkflow orchestration tests (#151): the active DevLoop set
comes from the list_active_dev_loop_ids visibility activity and feeds
detect_orphans; a failing visibility query skips orphan detection for the
tick instead of reporting every actionable proposal as an orphan.

Runs the real workflow definition against temporalio's time-skipping test
environment with fake activities, same harness as test_dev_loop_workflow.py.
"""
from __future__ import annotations

import logging
import uuid

import pytest
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.discovery import ProposalProjection, ReconcileDiscoveryResult
from orchestrator.temporal.activities.orphans import OrphanDetectionResult, OrphanSignal
from orchestrator.temporal.workflows.reconcile import ReconcileWorkflow, ReconcileWorkflowInput
from tests.temporal_harness import Worker  # polls the execution queue too — see #251

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-reconcile"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _fake_activities(
    *,
    active_ids: list[str] | None = None,
    visibility_fails: bool = False,
    projections: list[ProposalProjection] | None = None,
    submit_fails: bool = False,
    submit_phase: str = "Succeeded",
):
    received: dict = {}
    received["submits"] = []

    @activity.defn(name="discover_and_project")
    async def fake_discover_and_project(state_dir_path: str) -> ReconcileDiscoveryResult:
        return ReconcileDiscoveryResult(total_inspected=1, projections=projections or [])

    @activity.defn(name="submit_and_wait")
    async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
        received["submits"].append(input)
        if submit_fails:
            raise ApplicationError("argo unavailable", non_retryable=True)
        return WorkflowResult(workflow_name=f"{input.operation}-fake", phase=submit_phase)

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

    return [
        fake_discover_and_project,
        fake_list_active_dev_loop_ids,
        fake_detect_orphans,
        fake_submit_and_wait,
    ], received


async def _run(env, activities):
    """Run one tick against fake activities. Mirrors the inline setup the
    older tests use; the newer ones share it rather than repeat it."""
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ReconcileWorkflow],
        activities=activities,
    ):
        return await env.client.execute_workflow(
            ReconcileWorkflow.run,
            ReconcileWorkflowInput(state_dir_path="/tmp/state"),
            id=f"reconcile-test-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


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


DRIFT = [
    ProposalProjection(
        service="mctl-telegram",
        slug="issue-412-read-only-mcp-jwt",
        current_status="implemented",
        projected_status="merged",
        pr_url="https://github.com/mctlhq/mctl-telegram/pull/414",
        notes="PR mctlhq/mctl-telegram#414 merged on GitHub",
    )
]


class TestReconcileApplies:
    """The write half (#270): discovery found drift, something has to write it.

    Discovery is read-only and the worker has no gitops checkout, so the
    only way a projection becomes a commit is the mctl-agents-reconcile
    CWFT. Before this, ReconcileWorkflow logged the drift and dropped it.
    """

    async def test_drift_is_submitted_to_the_reconcile_cwft(self, env):
        acts, received = _fake_activities(projections=DRIFT)

        result = await _run(env, acts)

        assert [i.operation for i in received["submits"]] == ["mctl-agents-reconcile"]
        assert result.applied is not None
        assert result.applied.workflow_name == "mctl-agents-reconcile-fake"

    async def test_the_submit_is_never_a_dry_run(self, env):
        """A dry run would read everything, report success and write nothing
        — #270 exactly, and invisible from this side."""
        acts, received = _fake_activities(projections=DRIFT)

        await _run(env, acts)

        assert received["submits"][0].params["dry_run"] == "false"

    async def test_a_clean_tick_submits_nothing(self, env):
        """No drift means no clone, no ~200 GitHub reads, and no turn at the
        shared write mutex — every 15 minutes, to write nothing."""
        acts, received = _fake_activities(projections=[])

        result = await _run(env, acts)

        assert received["submits"] == []
        assert result.applied is None

    async def test_a_failed_submit_does_not_fail_the_tick(self, env):
        """The drift is still there and the next tick re-derives it; failing
        here would turn a busy mutex into a red schedule."""
        acts, received = _fake_activities(projections=DRIFT, submit_fails=True)

        result = await _run(env, acts)

        assert result.applied is None
        assert result.discovery.projections, "the finding must survive the failed write"
        assert len(received["submits"]) >= 1

    async def test_a_visibility_failure_still_writes_the_drift(self, env):
        """Orphan detection needs the active DevLoop set; projecting merged
        PRs onto .status.yaml does not. Dropping the write here would let a
        Temporal blip silently cost a tick of drift correction."""
        acts, received = _fake_activities(projections=DRIFT, visibility_fails=True)

        result = await _run(env, acts)

        assert result.orphans.skipped_reason is not None
        assert [i.operation for i in received["submits"]] == ["mctl-agents-reconcile"]
        assert result.applied is not None

    async def test_a_cwft_that_ran_and_failed_is_not_read_as_written(self, env, caplog):
        """submit_and_wait RETURNS for every terminal phase, including
        Failed and Error — it only raises when submission or polling itself
        blows up. A CWFT that ran and ended Failed (staging guard rejected
        the write, push lost the race) therefore arrives as an ordinary
        value, and without a check it reads exactly like a successful write
        (claude P2). The tick still succeeds; the phase must survive on the
        result so the failure is visible."""
        acts, received = _fake_activities(projections=DRIFT, submit_phase="Failed")

        with caplog.at_level(logging.WARNING, logger="temporalio.workflow"):
            result = await _run(env, acts)

        # The log line IS the fix: the returned value looks the same either
        # way, so asserting only on it passes against the unguarded version.
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("ended Failed" in m for m in warnings), warnings
        assert any("not written" in m for m in warnings), warnings

        assert len(received["submits"]) == 1
        assert result.applied is not None, "the Argo run must stay visible"
        assert result.applied.succeeded is False
        assert result.applied.phase == "Failed"
        assert result.discovery.projections, "the finding must survive the failed write"
