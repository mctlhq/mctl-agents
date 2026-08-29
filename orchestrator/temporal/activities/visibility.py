"""Activity: list active DevLoopWorkflow IDs via Temporal visibility.

ReconcileWorkflow's orphan detection needs the set of currently-running
DevLoop workflow IDs to compare against actionable proposals. This lives in
its own class because it needs the connected Temporal client, which plain
function activities don't have — the worker constructs one instance with
the client it already holds and registers the bound method.
"""
from __future__ import annotations

from temporalio import activity
from temporalio.client import Client

# Visibility query for the active DevLoop set. WorkflowType is the
# @workflow.defn class name; ExecutionStatus 'Running' deliberately excludes
# terminal and continued-as-new-completed runs — a proposal whose workflow
# closed IS the orphan case detect_orphans exists to catch.
ACTIVE_DEV_LOOPS_QUERY = "WorkflowType = 'DevLoopWorkflow' AND ExecutionStatus = 'Running'"


class VisibilityActivities:
    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn
    async def list_active_dev_loop_ids(self) -> list[str]:
        """Return the workflow IDs of all running DevLoopWorkflow executions.

        Raises on visibility errors — the caller (ReconcileWorkflow) treats a
        failure as "active set unknown" and skips orphan detection for the
        tick rather than reporting every proposal as an orphan.
        """
        ids: list[str] = []
        async for wf in self._client.list_workflows(ACTIVE_DEV_LOOPS_QUERY):
            ids.append(wf.id)
        activity.logger.info("visibility: %d active DevLoopWorkflow run(s)", len(ids))
        return ids
