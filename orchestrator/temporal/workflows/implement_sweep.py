"""ImplementSweepWorkflow: turn a stranded `accepted` proposal back into a run.

mctl-agents#412. Runs on its own 15-minute schedule
(`worker.IMPLEMENT_SWEEP_SCHEDULE_ID`) and does three things, fast, then
returns:

    list_active_dev_loop_ids  (fail-closed: query failure -> submit nothing)
    find_stranded_accepted(active_ids, grace_minutes)
    start_child_workflow(SweptImplementWorkflow, ...) for each stranded
        proposal, up to the per-tick cap, ABANDONed so the tick itself stays
        short — an implement run can occupy its child for hours, and
        `overlap=SKIP` on the schedule must cost nothing while it does.

The child, SweptImplementWorkflow, is deliberately tiny: one
`submit_and_wait` of the implement operation, scoped to `{service, slug}`
exactly the way DevLoopWorkflow's own implement step is (an unscoped submit
would let one sweep implement a DIFFERENT proposal — the hazard dev_loop.py
already documents for #203).

Dedup without a lock. The 1.31.0 SDK's `start_child_workflow` has no
`id_conflict_policy` (that parameter exists only on `Client.start_workflow`,
for top-level workflows) — so a second start against a still-RUNNING child id
raises `WorkflowAlreadyStartedError` rather than transparently attaching to
it. That exception is caught and treated as the no-op it would have been
under `USE_EXISTING`: the existing run keeps going, nothing new starts.
`id_reuse_policy=ALLOW_DUPLICATE` (not `ALLOW_DUPLICATE_FAILED_ONLY`) keeps a
proposal re-sweepable after a completed-but-ineffective run — the filters in
`stranded.py`, not the reuse policy, are what stop a pointless resubmit.

A brand-new workflow type, so no `workflow.patched()` marker guards any of
this: there is no existing history whose replay could disagree.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.stranded import StrandedScanResult, find_stranded_accepted
    from orchestrator.temporal.constants import (
        DEFAULT_IMPLEMENT_SWEEP_GRACE_MINUTES,
        DEFAULT_IMPLEMENT_SWEEP_MAX_SUBMITS,
        IMPLEMENTATION_OPERATION,
        IMPLEMENTATION_TASK_QUEUE,
    )

ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)

# Same shape as every other CWFT submit (dev_loop.SDK_STEP_TIMEOUT /
# SDK_STEP_HEARTBEAT_TIMEOUT / SDK_STEP_RETRY_POLICY), declared locally
# rather than imported: this module owns its own submit and reconcile.py
# sets the same precedent of not sharing dev_loop's constants.
SWEEP_STEP_TIMEOUT = timedelta(hours=2)
SWEEP_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=2)
SWEEP_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass(frozen=True)
class SweptImplementInput:
    service: str
    slug: str


@workflow.defn
class SweptImplementWorkflow:
    """One implement submit, scoped, on the admission queue. Nothing else."""

    @workflow.run
    async def run(self, input_data: SweptImplementInput) -> WorkflowResult:
        return await workflow.execute_activity(
            submit_and_wait,
            SubmitAndWaitInput(
                operation=IMPLEMENTATION_OPERATION,
                params={"service": input_data.service, "slug": input_data.slug},
            ),
            task_queue=IMPLEMENTATION_TASK_QUEUE,
            start_to_close_timeout=SWEEP_STEP_TIMEOUT,
            heartbeat_timeout=SWEEP_STEP_HEARTBEAT_TIMEOUT,
            retry_policy=SWEEP_STEP_RETRY_POLICY,
        )


@dataclass(frozen=True)
class ImplementSweepWorkflowInput:
    grace_minutes: int = DEFAULT_IMPLEMENT_SWEEP_GRACE_MINUTES
    max_submits: int = DEFAULT_IMPLEMENT_SWEEP_MAX_SUBMITS


@dataclass(frozen=True)
class ImplementSweepResult:
    # How many `accepted` proposals survived every skip filter this tick —
    # zero here, distinctly from a `skipped_reason`, means "found nothing
    # stranded", not "did not look".
    candidates: int
    submitted: int
    skipped: int
    # Set when the tick declined to act at all (the visibility query failed
    # after its retries) — see the module docstring's fail-closed stance.
    skipped_reason: str | None = None


@workflow.defn
class ImplementSweepWorkflow:
    @workflow.run
    async def run(self, input_data: ImplementSweepWorkflowInput | None = None) -> ImplementSweepResult:
        cfg = input_data or ImplementSweepWorkflowInput()

        try:
            active_ids: list[str] = await workflow.execute_activity(
                "list_active_dev_loop_ids",
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
        except Exception as exc:  # noqa: BLE001 — ActivityError after retries
            # The active set IS the safety argument here (unlike reconcile's
            # projection write, which is harmless without it): an unknown
            # active set must never read as "no live loops anywhere", or
            # every accepted proposal becomes a false candidate for a second
            # implementer run. Submit nothing; the next tick retries fresh.
            workflow.logger.warning(
                "implement-sweep: list_active_dev_loop_ids failed; skipping "
                "this tick rather than submitting against an unknown active "
                "set: %s",
                exc,
            )
            return ImplementSweepResult(
                candidates=0,
                submitted=0,
                skipped=0,
                skipped_reason=f"active-DevLoop visibility query failed: {exc}",
            )

        scan: StrandedScanResult = await workflow.execute_activity(
            find_stranded_accepted,
            args=[active_ids, cfg.grace_minutes],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )

        submitted = 0
        for candidate in scan.stranded:
            if submitted >= cfg.max_submits:
                workflow.logger.info(
                    "STRANDED service=%s slug=%s reason=%s (over the "
                    "%d-per-tick cap; not submitted this tick)",
                    candidate.service,
                    candidate.slug,
                    candidate.reason,
                    cfg.max_submits,
                )
                continue

            child_id = f"implement-sweep-{candidate.service}-{candidate.slug}"
            try:
                await workflow.start_child_workflow(
                    SweptImplementWorkflow.run,
                    SweptImplementInput(service=candidate.service, slug=candidate.slug),
                    id=child_id,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                # A prior tick's child for this (service, slug) is still
                # running — the no-lock dedup the module docstring describes.
                workflow.logger.info(
                    "STRANDED service=%s slug=%s reason=%s (already being "
                    "swept; not resubmitted)",
                    candidate.service,
                    candidate.slug,
                    candidate.reason,
                )
                continue

            workflow.logger.info(
                "STRANDED service=%s slug=%s reason=%s (submitted as %s)",
                candidate.service,
                candidate.slug,
                candidate.reason,
                child_id,
            )
            submitted += 1

        return ImplementSweepResult(
            candidates=len(scan.stranded),
            submitted=submitted,
            skipped=len(scan.stranded) - submitted,
            skipped_reason=scan.skipped_reason,
        )
