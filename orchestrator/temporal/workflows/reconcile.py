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
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.discovery import ReconcileDiscoveryResult, discover_and_project
    from orchestrator.temporal.activities.orphans import OrphanDetectionResult, detect_orphans

ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)

# mctl-api operation wrapping cwft-mctl-agents-reconcile: clone-gitops ->
# run_shepherd --reconcile -> commit-and-push under the
# mctl-gitops-main-writes mutex. The worker has no gitops checkout and no
# deploy key by design, so this is the only way its findings become writes.
APPLY_OPERATION = "mctl-agents-reconcile"

# The CWFT's own activeDeadlineSeconds is 1800 and a full sweep takes ~4
# minutes. 35 minutes leaves room for the mutex to be held by a sibling
# write (shepherd, implement, approve) without this activity timing out
# first and re-submitting on top of a run that is merely queued.
APPLY_STEP_TIMEOUT = timedelta(minutes=35)
APPLY_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=2)
# Matches every other CWFT submission: the retry exists so an attempt can
# RESUME polling after a worker crash — submit_and_wait refuses to re-POST
# once it has heartbeated a workflow name.
APPLY_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass(frozen=True)
class ReconcileWorkflowInput:
    state_dir_path: str = ""


@dataclass(frozen=True)
class ReconcileWorkflowResult:
    discovery: ReconcileDiscoveryResult
    orphans: OrphanDetectionResult
    # The Argo run that wrote the projections back, when there were any to
    # write. None means the tick found no drift — or that the submit failed
    # and the next tick will try again. Defaulted so results recorded before
    # this stage existed still deserialize.
    applied: WorkflowResult | None = None


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

        if workflow.patched("orphan-active-ids"):
            try:
                active_ids: list[str] = await workflow.execute_activity(
                    "list_active_dev_loop_ids",
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY_POLICY,
                )
            except Exception as exc:  # noqa: BLE001 — ActivityError after retries
                # Active set unknown: skip orphan detection for this tick
                # instead of reporting every actionable proposal as an
                # orphan. The next scheduled run retries from scratch.
                workflow.logger.warning(
                    "list_active_dev_loop_ids failed; skipping orphan detection "
                    "this tick: %s",
                    exc,
                )
                # The write still goes out. Orphan detection needs the active
                # set; projecting merged/closed PRs onto .status.yaml does
                # not, and dropping it here would mean a Temporal visibility
                # blip silently costs a tick of drift correction — the same
                # class of quiet loss #270 was.
                return ReconcileWorkflowResult(
                    discovery=discovery_result,
                    orphans=OrphanDetectionResult(
                        total_actionable=0,
                        orphans=[],
                        skipped_reason=f"active-DevLoop visibility query failed: {exc}",
                    ),
                    applied=(
                        await self._apply(discovery_result)
                        if workflow.patched("reconcile-apply")
                        else None
                    ),
                )

            orphans_result: OrphanDetectionResult = await workflow.execute_activity(
                detect_orphans,
                args=[state_dir_path, active_ids],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
        else:
            # Unpatched replay branch: schedule detect_orphans with exactly
            # the one argument the old history recorded.
            #
            # The original reason given here (agy P1 on that PR) was that a
            # second argument "would be a payload mismatch on replay". That
            # is not true, and tests/test_workflow_replay.py now measures it:
            # Replayer compares the shape of the command stream, never a
            # command's payload against the event it matched. An extra
            # argument replays clean.
            #
            # Keeping the branch anyway, for the reason that does hold: this
            # activity is re-executed for real when a resumed execution
            # reaches it, and detect_orphans' signature is what bounds what
            # may be passed. Same code, honest justification.
            orphans_result = await workflow.execute_activity(
                detect_orphans,
                args=[state_dir_path],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )

        applied: WorkflowResult | None = None
        if workflow.patched("reconcile-apply"):
            applied = await self._apply(discovery_result)

        return ReconcileWorkflowResult(
            discovery=discovery_result,
            orphans=orphans_result,
            applied=applied,
        )

    async def _apply(self, discovery: ReconcileDiscoveryResult) -> WorkflowResult | None:
        """Submit the reconcile CWFT so the projections actually get written.

        Discovery is read-only and stays that way: the worker has no gitops
        checkout and no deploy key (the invariant whose violation OOMKilled
        the incident responder, #179), so the write has to happen in Argo,
        under the mctl-gitops-main-writes mutex the CWFT already takes.

        No findings are passed. The CWFT re-reads state in the pod that has
        the clone, which is both simpler and safer than trusting a list this
        workflow assembled minutes earlier: by the time the mutex is free,
        a PR may have merged, and the pod sees that where a payload could
        not.

        Submitting only when there is drift is deliberate. An unconditional
        submit would run a full sweep — a clone, ~200 GitHub reads, and a
        turn at the shared write mutex — every 15 minutes to write nothing.
        """
        if not discovery.projections:
            return None

        workflow.logger.info(
            "reconcile: %d projection(s) to write; submitting %s",
            len(discovery.projections),
            APPLY_OPERATION,
        )
        try:
            result: WorkflowResult = await workflow.execute_activity(
                submit_and_wait,
                SubmitAndWaitInput(
                    operation=APPLY_OPERATION,
                    # Explicit rather than relying on the operation's default:
                    # a dry run here would read everything, report success and
                    # write nothing — the exact shape of #270, and invisible
                    # from this side.
                    params={"dry_run": "false"},
                ),
                start_to_close_timeout=APPLY_STEP_TIMEOUT,
                heartbeat_timeout=APPLY_STEP_HEARTBEAT_TIMEOUT,
                retry_policy=APPLY_STEP_RETRY_POLICY,
            )
        except ActivityError as exc:
            # A failed write is not a failed tick. The drift is still there,
            # discovery still reported it, and the next tick in 15 minutes
            # re-derives and re-submits — whereas failing here would turn a
            # busy mutex into a red schedule. Logged loudly because "no
            # projections" and "could not write them" must not look alike.
            workflow.logger.warning(
                "reconcile: %d projection(s) found but %s failed after retries "
                "(%s) — leaving them for the next tick",
                len(discovery.projections),
                APPLY_OPERATION,
                exc,
            )
            return None

        if not result.succeeded:
            # The other half of the same failure, and the quieter one:
            # submit_and_wait RETURNS for every terminal phase, so a CWFT
            # that ran and ended Failed/Error arrives here as an ordinary
            # value. Without this branch a rejected staging guard or a lost
            # push would read exactly like a successful write — the same
            # indistinguishability this whole change exists to remove
            # (claude P2 on #273). The result is still returned rather than
            # dropped: it carries the Argo workflow name, which is what an
            # operator needs to go read the failure.
            workflow.logger.warning(
                "reconcile: %s ran as %s and ended %s — %d projection(s) not "
                "written, leaving them for the next tick",
                APPLY_OPERATION,
                result.workflow_name,
                result.phase,
                len(discovery.projections),
            )

        return result
