"""Activity: list active DevLoopWorkflow IDs via Temporal visibility.

ReconcileWorkflow's orphan detection needs the set of currently-running
DevLoop workflow IDs to compare against actionable proposals. This lives in
its own class because it needs the connected Temporal client, which plain
function activities don't have — the worker constructs one instance with
the client it already holds and registers the bound method.
"""
from __future__ import annotations

from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError

from orchestrator.temporal.implement_outcome import PRE_START_ERROR_TYPE

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

    @activity.defn
    async def count_swept_prestart_failures(self, workflow_ids: list[str]) -> dict[str, int]:
        """Per child id, how many prior executions died BEFORE the implementer ran.

        mctl-agents#412 review: `ImplementSweepWorkflow` is a fresh execution
        every 15-minute tick and keeps no state of its own, so a `pre_start`
        outcome — the one class that touches no `.status.yaml` field, because
        nothing ever ran — would otherwise be resubmitted forever. The child's
        workflow id is deterministic per (service, slug) and reused
        (`id_reuse_policy=ALLOW_DUPLICATE`), so Temporal's own visibility store
        is the only durable record of how many times a given proposal has
        already failed to start; `ImplementSweepWorkflow` uses this to stop
        resubmitting past a bound, the same way `MAX_PRESTART_REQUEUES` bounds
        `dev_loop._implement`.

        BULK, and that matters. The per-id version fanned out one visibility
        query per candidate — 71 on today's backlog — and the cap added to
        bound that starved the tail of the candidate list permanently, because
        the list is rebuilt in stable order every tick and an over-budget
        candidate never leaves it. One `IN (...)` query per tick has neither
        problem.

        PRE-START ONLY (review P2). Counting every Failed execution also
        charged the budget for `execution` and `finalization` outcomes — runs
        where the implementer DID run and left needs-triage/blocked behind, so
        the proposal drops out of the candidate set on its own. A proposal
        triaged and re-accepted then arrived with strikes already charged and
        became permanently unsweepable after two real pre-start kills.
        `ExecutionStatus = 'Failed'` alone cannot say which, and visibility
        cannot filter on an `ApplicationError.type`, so each failed execution's
        own terminal error is read back and only `PRE_START_ERROR_TYPE` counts.
        A `submit_and_wait` that exhausted its retries against an Argo/mctl-api
        outage carries a different shape and is left out, as is any cause that
        cannot be read at all: of the two ways to be wrong there,
        under-charging costs one extra resubmit while over-charging can make a
        proposal permanently unsweepable.

        Raises on a visibility failure — the caller treats an unknown budget as
        a reason to skip the tick, not to submit on a count of zero.
        """
        counts = {wf_id: 0 for wf_id in workflow_ids}
        if not workflow_ids:
            return counts

        # Quoted for the visibility filter's own string syntax. These ids are
        # built from gitops path segments, not from user input, but an
        # unescaped quote would still turn a malformed slug into an
        # InvalidArgument that reads as "no prior failures" to anything that
        # swallowed it (review P3).
        quoted = ", ".join("'" + wf_id.replace("'", "''") + "'" for wf_id in workflow_ids)
        async for wf in self._client.list_workflows(
            f"WorkflowId IN ({quoted}) AND ExecutionStatus = 'Failed'"
        ):
            if wf.id not in counts:
                continue
            handle = self._client.get_workflow_handle(wf.id, run_id=wf.run_id)
            try:
                await handle.result()
            except WorkflowFailureError as exc:
                cause = exc.cause
                if isinstance(cause, ApplicationError) and cause.type == PRE_START_ERROR_TYPE:
                    counts[wf.id] += 1
            except Exception:  # noqa: BLE001 — an unreadable cause is not counted, not raised
                activity.logger.warning(
                    "count_swept_prestart_failures: could not read the failure "
                    "cause for %s run_id=%s; not counted as a pre-start loss",
                    wf.id,
                    wf.run_id,
                )
        activity.logger.info(
            "visibility: pre-start failures across %d candidate id(s): %s",
            len(workflow_ids),
            {k: v for k, v in counts.items() if v},
        )
        return counts
