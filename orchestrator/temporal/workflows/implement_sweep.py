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
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.state import ExecutionRecord, record_execution
    from orchestrator.temporal.activities.stranded import StrandedScanResult, find_stranded_accepted
    from orchestrator.temporal.constants import (
        DEFAULT_IMPLEMENT_SWEEP_GRACE_MINUTES,
        DEFAULT_IMPLEMENT_SWEEP_MAX_SUBMITS,
        IMPLEMENTATION_OPERATION,
        IMPLEMENTATION_TASK_QUEUE,
    )
    from orchestrator.temporal.implement_outcome import (
        PRE_START_ERROR_TYPE,
        Outcome,
        classify,
        finalization_evidence,
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

# Bounds how many times a tick will resubmit the SAME (service, slug) child
# workflow id after prior executions under it ended Failed — mirrors
# dev_loop.MAX_PRESTART_REQUEUES. Unlike dev_loop's in-loop requeue, this
# bound is enforced ACROSS ticks (via VisibilityActivities.
# count_swept_prestart_failures), because a `pre_start` outcome touches no
# `.status.yaml` field, so nothing else removes the proposal from next
# tick's candidate set (mctl-agents#412 review).
MAX_SWEEP_PRESTART_ATTEMPTS = 3


# The pre-start budget is read for every candidate in ONE visibility query per
# tick, not one query per candidate.
#
# A per-candidate query fanned out with the size of the backlog (71 today)
# before landing its 5 submits. Capping the number of queries instead was
# worse, and was the fix a review round caught: `scan.stranded` is rebuilt in
# stable tree order every tick and an over-budget candidate never leaves it, so
# once N over-budget proposals precede candidate N+1, that candidate is never
# queried or submitted again on ANY tick — a permanent starvation of the tail
# dressed up as "reconsidered next tick".

# dev_loop.py's ENVIRONMENT / FAST_ACTIVITY_TIMEOUT / FAST_ACTIVITY_RETRY_POLICY,
# duplicated rather than imported — incidents.py sets the same precedent for
# its own best-effort record_execution call, for the reason given above.
ENVIRONMENT = "production"
RECORD_EXECUTION_TIMEOUT = timedelta(seconds=30)
RECORD_EXECUTION_RETRY_POLICY = RetryPolicy(maximum_attempts=5)


@dataclass(frozen=True)
class SweptImplementInput:
    service: str
    slug: str


async def _record_swept_execution(input_data: SweptImplementInput, result: WorkflowResult) -> None:
    """Best-effort audit-trail write for a swept implement run (mctl-agents#412).

    Mirrors dev_loop.py's `_record` / incidents.py's `_record`: without this,
    a run submitted by this workflow is invisible to the executions ledger
    `mctl_list_recent_agent_runs` reads, even though DevLoopWorkflow's own
    implement step writes one for the exact same operation. No release is
    resolved before a swept submit (there is no `resolve_agent_release` call
    here), so `version`/`image_ref` are always empty — the same "no pinned
    version" fallback `_record` uses when a release exists but carries no
    `image_ref`.

    Best-effort for the same reason as its siblings: the CWFT being recorded
    has already finished, and failing this workflow over a missing audit row
    would turn an mctl-api blip into a false ImplementationFailed.
    """
    try:
        await workflow.execute_activity(
            record_execution,
            ExecutionRecord(
                temporal_workflow_id=workflow.info().workflow_id,
                agent="implementer",
                environment=ENVIRONMENT,
                version="",
                image_ref="",
                target_repo=input_data.service,
                argo_workflow_name=result.workflow_name,
                phase=result.phase,
            ),
            start_to_close_timeout=RECORD_EXECUTION_TIMEOUT,
            retry_policy=RECORD_EXECUTION_RETRY_POLICY,
        )
    except ActivityError:
        workflow.logger.warning(
            "record_execution failed after retries for swept implement of "
            "%s/%s argo_workflow=%s — continuing without a durable execution "
            "record for this run",
            input_data.service,
            input_data.slug,
            result.workflow_name,
        )


@workflow.defn
class SweptImplementWorkflow:
    """One implement submit, scoped, on the admission queue. Nothing else."""

    @workflow.run
    async def run(self, input_data: SweptImplementInput) -> WorkflowResult:
        result: WorkflowResult = await workflow.execute_activity(
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

        await _record_swept_execution(input_data, result)

        # Classified the same way dev_loop._implement classifies its own
        # implement submit (implement_outcome.py) — collapsing straight to
        # `result.phase != "Succeeded"` here would be exactly the reduction
        # to a bare phase implement_outcome.py exists to reject: `Failed`
        # alone cannot say whether the implementer ever ran.
        outcome: Outcome = classify(
            result.phase,
            implementer_ran=result.implementer_ran,
            implementer_phase=result.implementer_phase,
            finalization_phase=result.finalization_phase,
        )
        if outcome == "success":
            return result

        # No pre-start requeue loop here, unlike dev_loop._implement: a sweep
        # tick submits each stranded proposal at most once, and the next
        # 15-minute tick naturally reconsiders a proposal that is still
        # `accepted` — there is no in-workflow retry budget to spend.
        #
        error_type = {
            "pre_start": PRE_START_ERROR_TYPE,
            "execution": "ImplementationFailed",
            "finalization": "ImplementationFinalizationFailed",
        }[outcome]
        raise ApplicationError(
            f"swept implement of {input_data.service}/{input_data.slug} ended "
            f"{result.phase} ({outcome}) in Argo workflow {result.workflow_name}"
            + (
                f": {finalization_evidence(result.finalization_phase)}"
                if outcome == "finalization"
                else ""
            ),
            result,
            type=error_type,
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
    # Set when the tick declined to act at all (a fail-closed read failed
    # after its retries) — see the module docstring's fail-closed stance.
    skipped_reason: str | None = None
    # `accepted` proposals carrying no execution authorization at all,
    # quarantined by `stranded._scan` and awaiting human triage. Surfaced on
    # the RESULT, not only in a log line, because the 2026-09-19 product
    # decision on #412 requires these reach a human: a log-only count is
    # indistinguishable from a clean tick to everything that reads a sweep.
    unauthorized: int = 0
    # Candidates that had a live claim to a submit but were held back because
    # they have already burned MAX_SWEEP_PRESTART_ATTEMPTS pre-start attempts.
    # On the result because "needs a human" that reaches no human is not a
    # gate. This is the ONLY report of that state: an earlier round wrote a
    # `record_execution` row per over-budget candidate per tick instead, which
    # put 96 rows/day/proposal into the executions ledger carrying
    # `agent="implementer"`, `phase="Failed"` and a Temporal child id in
    # `argo_workflow_name` — indistinguishable from a real implementer failure
    # and naming no Argo workflow, which is exactly the opaque handle this
    # PR's acceptance criterion was written against.
    #
    # KNOWN LIMIT, stated rather than hidden: this budget is counted out of
    # Temporal visibility, which ADR-007 and ADR-009 both name as the thing
    # `ExecutionRecord` exists to OUTLIVE. It resets with the retention
    # window, so the loop resumes at three attempts per window. Making it
    # durable needs a `.status.yaml` write the Temporal worker has no path for
    # today (every needs-triage write lives in run_shepherd, CWFT-side); until
    # then this field is what makes the exhaustion visible.
    over_budget: int = 0


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

        try:
            scan: StrandedScanResult = await workflow.execute_activity(
                find_stranded_accepted,
                args=[active_ids, cfg.grace_minutes],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
        except Exception as exc:  # noqa: BLE001 — ActivityError after retries
            # The sibling call above is wrapped and this one was not, so a
            # GitHub 5xx or a malformed listing failed the whole SCHEDULED
            # workflow every 15 minutes instead of reporting a skipped tick
            # (review P2). `skipped_reason` is the field that exists to say
            # "did not look", as distinct from "looked and found nothing" —
            # it was documented on two dataclasses and assigned on neither.
            workflow.logger.warning(
                "implement-sweep: find_stranded_accepted failed; skipping this "
                "tick rather than failing the schedule: %s",
                exc,
            )
            return ImplementSweepResult(
                candidates=0,
                submitted=0,
                skipped=0,
                skipped_reason=f"stranded scan failed: {exc}",
            )

        # One query for the whole candidate set, before the loop. Fails the
        # tick closed rather than per candidate: an unknown prior-failure count
        # must never read as zero, or the bound it enforces is gone exactly
        # when visibility is unhealthy — and unlike the per-candidate version,
        # there is no partial answer to carry on with.
        child_ids = [
            f"implement-sweep-{c.service}-{c.slug}" for c in scan.stranded
        ]
        prestart_failures: dict[str, int] = {}
        if child_ids:
            try:
                prestart_failures = await workflow.execute_activity(
                    "count_swept_prestart_failures",
                    child_ids,
                    start_to_close_timeout=ACTIVITY_TIMEOUT,
                    retry_policy=ACTIVITY_RETRY_POLICY,
                )
            except Exception as exc:  # noqa: BLE001 — ActivityError after retries
                workflow.logger.warning(
                    "implement-sweep: count_swept_prestart_failures failed; "
                    "skipping this tick rather than submitting on an unknown "
                    "retry budget: %s",
                    exc,
                )
                return ImplementSweepResult(
                    candidates=len(scan.stranded),
                    submitted=0,
                    skipped=len(scan.stranded),
                    skipped_reason=f"pre-start budget query failed: {exc}",
                    unauthorized=len(scan.unauthorized),
                )

        submitted = 0
        over_budget = 0
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
            prior_failures = prestart_failures.get(child_id, 0)
            if prior_failures >= MAX_SWEEP_PRESTART_ATTEMPTS:
                over_budget += 1
                workflow.logger.warning(
                    "STRANDED service=%s slug=%s reason=%s (%d prior pre-start "
                    "failure(s) under %s; exceeded the retry budget, needs a human, "
                    "not resubmitted)",
                    candidate.service,
                    candidate.slug,
                    candidate.reason,
                    prior_failures,
                    child_id,
                )
                continue

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

        if scan.unauthorized:
            workflow.logger.warning(
                "implement-sweep: %d accepted proposal(s) carry no execution "
                "authorization and were quarantined from execution; they need "
                "human triage: %s",
                len(scan.unauthorized),
                ", ".join(key for key, _ in scan.unauthorized[:20]),
            )

        return ImplementSweepResult(
            candidates=len(scan.stranded),
            submitted=submitted,
            skipped=len(scan.stranded) - submitted,
            skipped_reason=scan.skipped_reason,
            unauthorized=len(scan.unauthorized),
            over_budget=over_budget,
        )
