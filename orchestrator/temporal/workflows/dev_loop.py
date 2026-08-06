"""DevLoopWorkflow: the durable orchestrator for one GitHub issue's path
through investigate -> human approval -> implement.

This is the phase-4 "first vertical slice" from the plan, deliberately
narrower than the full issue -> ... -> deploy -> monitor pipeline: it covers
exactly the acceptance slice the plan defines ("mctl_trigger_issue ->
Temporal -> registry resolve -> Argo investigate -> proposal in gitops ->
approval signal -> implement -> PR"). The review/fix loop (shepherd) and
deploy/monitor stages are phase 5/6 work, layered on top of this workflow
once the slice is proven, not reimplemented here.

Known simplification: approval is signal-only in this version, and does NOT
itself flip the proposal's `.status.yaml` from `proposed` to `accepted` — an
operator still does that by editing gitops directly (the pre-Temporal
affordance), same as the legacy cron-driven pipeline. `approve()` only
unblocks this workflow's wait_condition; the implement step below still
requires the proposal to already be `accepted` by the time it runs, or the
CWFT skips it with `skipped_reason` (a safe no-op, not an error). Automating
that flip requires a new gitops-committing step, which per this plan's
"gitops commit step" constraint must run inside Argo (the only place holding
the `mctl-gitops-main-writes` mutex) — i.e. a new CWFT parameter, a
cross-repo change deferred to phase 5's cutover rather than done here.

To keep that gap from ever implementing the WRONG proposal in the meantime,
the implement step is scoped to this issue's own repo via the CWFT's existing
`service` parameter (see `_target_repo` below) — so an approve() on this
workflow can at worst no-op (nothing accepted yet in this repo) or implement
a different already-accepted proposal in the SAME repo, never a different
repo's issue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.registry import ResolvedRelease, resolve_agent_release
    from orchestrator.temporal.activities.state import ExecutionRecord, record_execution
    from orchestrator.temporal.issue_ref import parse_issue_url

ENVIRONMENT = "production"

# The Argo CWFTs already retry within a run (second-OAuth-account fallback on
# a 429/five_hour limit) — see activities/argo.py's module docstring. A
# Temporal retry that re-submitted on top of that would multiply real SDK
# runs, so this bound only exists to let a retried attempt RESUME polling
# (via activity.info().heartbeat_details, see submit_and_wait) after a worker
# crash or missed heartbeat — submit_and_wait detects "already submitted" and
# refuses to re-POST once it has a workflow_name (including the "submitted
# but name unparseable" sentinel, which fails loudly instead of guessing).
# The only gap this doesn't close: a crash strictly between the Argo POST
# succeeding and the first activity.heartbeat() call actually landing at the
# Temporal server — vanishingly small (no I/O in between), but real. 3, not
# unlimited: still fail loudly on a genuinely wedged activity rather than
# retry forever.
SDK_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
SDK_STEP_TIMEOUT = timedelta(hours=2)
SDK_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=2)

FAST_ACTIVITY_TIMEOUT = timedelta(seconds=30)
# Bounded, not the Temporal default of unlimited attempts: resolve/record are
# plain HTTP round trips to mctl-api, so a handful of retries covers a real
# transient blip. Unlimited retries on _record in particular would otherwise
# wedge wait_condition forever if mctl-api's executions endpoint were ever
# down — the real SDK work (submit_and_wait) already succeeded by the time
# _record runs, so its own failure must never block workflow progress (see
# _record's try/except below).
FAST_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=5)


@dataclass(frozen=True)
class IssueRef:
    issue_url: str


@dataclass(frozen=True)
class DevLoopResult:
    investigate: WorkflowResult
    implement: WorkflowResult | None  # None if approval was never signalled


async def _resolve(agent: str) -> ResolvedRelease | None:
    return await workflow.execute_activity(
        resolve_agent_release,
        args=[agent, ENVIRONMENT],
        start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
        retry_policy=FAST_ACTIVITY_RETRY_POLICY,
    )


async def _run_cwft(operation: str, params: dict[str, str]) -> WorkflowResult:
    return await workflow.execute_activity(
        submit_and_wait,
        SubmitAndWaitInput(operation=operation, params=params),
        start_to_close_timeout=SDK_STEP_TIMEOUT,
        heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
        retry_policy=SDK_STEP_RETRY_POLICY,
    )


def _target_repo(issue: IssueRef) -> str:
    return parse_issue_url(issue.issue_url).repo


async def _record(
    agent: str, release: ResolvedRelease | None, result: WorkflowResult, target_repo: str
) -> None:
    # A release with no image_ref means resolve_agent_release found nothing
    # to pin, so the CWFT ran its own baked-in default image instead (see
    # activities/registry.py) — record that as "no pinned version", not as
    # release.version, or the audit trail would claim a specific registry
    # version produced this result when it was never actually passed to the
    # run.
    pinned = release if release and release.image_ref else None

    # Best-effort: this is an audit-trail write, not the real work (the CWFT
    # this records already ran to completion by the time this is called).
    # Letting a persistent failure here fail the whole workflow would make
    # an mctl-api outage on this one endpoint block wait_condition/approval
    # indefinitely for work that already succeeded — worse than a missing
    # execution record.
    try:
        await workflow.execute_activity(
            record_execution,
            ExecutionRecord(
                temporal_workflow_id=workflow.info().workflow_id,
                agent=agent,
                environment=ENVIRONMENT,
                version=pinned.version if pinned else "",
                image_ref=pinned.image_ref if pinned else "",
                target_repo=target_repo,
                argo_workflow_name=result.workflow_name,
                phase=result.phase,
            ),
            start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
            retry_policy=FAST_ACTIVITY_RETRY_POLICY,
        )
    except ActivityError:
        workflow.logger.warning(
            "record_execution failed after retries for agent=%s argo_workflow=%s — "
            "continuing without a durable execution record for this step",
            agent,
            result.workflow_name,
        )


@workflow.defn
class DevLoopWorkflow:
    def __init__(self) -> None:
        self._approved = False

    @workflow.signal
    def approve(self, *args: object) -> None:
        self._approved = True

    @workflow.run
    async def run(self, issue: IssueRef) -> DevLoopResult:
        target_repo = _target_repo(issue)

        # Pin the investigator version ONCE, at the start of this step. A
        # later promote/rollback in the registry must not retroactively
        # change what an in-flight (or replayed) workflow already ran.
        investigator_release = await _resolve("issue-investigator")
        investigate_params = {"issue_url": issue.issue_url}
        if investigator_release and investigator_release.image_ref:
            investigate_params["agent_image"] = investigator_release.image_ref
            investigate_params["agent_version"] = f"issue-investigator@{investigator_release.version}"

        investigate_result = await _run_cwft("mctl-agents-investigate", investigate_params)
        await _record("issue-investigator", investigator_release, investigate_result, target_repo)

        if not investigate_result.succeeded:
            return DevLoopResult(investigate=investigate_result, implement=None)

        # Durable wait: this workflow can sit here for days without costing
        # anything beyond Temporal's own history storage — exactly the
        # "durable per-issue state" the plan's problem statement calls out
        # as missing from the current polling-cron pipeline.
        await workflow.wait_condition(lambda: self._approved)

        implementer_release = await _resolve("implementer")
        # Scoped to this issue's own repo (see the module docstring): the
        # implement CWFT only considers proposals under agents-state/<service>/,
        # so an approve() here can at worst implement a different
        # already-accepted proposal in the SAME repo, never a different one.
        #
        # Depends on mctl-gitops's cwft-mctl-agents-implement.yaml already
        # declaring this `service` parameter and threading it to
        # `run_implementer.py --service <value>` — verified directly
        # (not assumed) as of this comment: see
        # cwft-mctl-agents-implement.yaml's `arguments.parameters` (name:
        # service) and its `implement-proposals` step, which does
        # `[ -n "$WORKFLOW_SERVICE" ] && set -- "$@" --service "$WORKFLOW_SERVICE"`
        # before invoking run_implementer.py. mctl-agents' CI can't check
        # this out to assert it directly (mctl-gitops is a sibling repo), so
        # if that CWFT is ever changed to drop/rename the parameter, this
        # scoping silently reverts to today's unscoped behavior with no
        # error on this side.
        implement_params: dict[str, str] = {"service": target_repo}
        if implementer_release and implementer_release.image_ref:
            implement_params["agent_image"] = implementer_release.image_ref
            implement_params["agent_version"] = f"implementer@{implementer_release.version}"

        implement_result = await _run_cwft("mctl-agents-implement", implement_params)
        await _record("implementer", implementer_release, implement_result, target_repo)

        return DevLoopResult(investigate=investigate_result, implement=implement_result)
