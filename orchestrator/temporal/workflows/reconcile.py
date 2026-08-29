"""ReconcileWorkflow: durable single-pass reconcile sweep on Temporal.

Executes read-only discovery & projection followed by orphan detection.
The active DevLoopWorkflow set for orphan comparison comes from a Temporal
visibility query (list_active_dev_loop_ids); if that query fails, orphan
detection is skipped for the tick — an unknown active set must not turn
every actionable proposal into a false orphan (#151).
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

        active_ids: list[str] | None = None
        if workflow.patched("orphan-active-ids"):
            try:
                active_ids = await workflow.execute_activity(
                    "list_active_dev_loop_ids",
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY_POLICY,
                )
            except Exception:  # noqa: BLE001 — ActivityError after retries
                # Active set unknown: skip orphan detection for this tick
                # instead of reporting every actionable proposal as an
                # orphan. The next scheduled run retries from scratch.
                workflow.logger.warning(
                    "list_active_dev_loop_ids failed; skipping orphan detection this tick"
                )
                return ReconcileWorkflowResult(
                    discovery=discovery_result,
                    orphans=OrphanDetectionResult(total_actionable=0, orphans=[]),
                )

        orphans_result: OrphanDetectionResult = await workflow.execute_activity(
            detect_orphans,
            args=[state_dir_path, active_ids],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )

        return ReconcileWorkflowResult(
            discovery=discovery_result,
            orphans=orphans_result,
        )
