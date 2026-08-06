"""ReconcileWorkflow: durable single-pass reconcile sweep on Temporal.

Executes read-only discovery & projection followed by orphan detection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.discovery import ReconcileDiscoveryResult, discover_and_project
    from orchestrator.temporal.activities.orphans import OrphanDetectionResult, detect_orphans

ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass(frozen=True)
class ReconcileWorkflowInput:
    state_dir_path: str = ""


@dataclass(frozen=True)
class ReconcileWorkflowResult:
    discovery: ReconcileDiscoveryResult
    orphans: OrphanDetectionResult


@workflow.defn
class ReconcileWorkflow:
    @workflow.run
    async def run(self, input_data: ReconcileWorkflowInput | None = None) -> ReconcileWorkflowResult:
        state_dir_path = input_data.state_dir_path if input_data else ""

        discovery_result: ReconcileDiscoveryResult = await workflow.execute_activity(
            discover_and_project,
            args=[state_dir_path],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )

        orphans_result: OrphanDetectionResult = await workflow.execute_activity(
            detect_orphans,
            args=[state_dir_path],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )

        return ReconcileWorkflowResult(
            discovery=discovery_result,
            orphans=orphans_result,
        )
