"""DevLoopWorkflow: the durable orchestrator for one GitHub issue's path
through investigate -> human approval -> implement.

This is the phase-4 "first vertical slice" from the plan, deliberately
narrower than the full issue -> ... -> deploy -> monitor pipeline: it covers
exactly the acceptance slice the plan defines ("mctl_trigger_issue ->
Temporal -> registry resolve -> Argo investigate -> proposal in gitops ->
approval signal -> implement -> PR"). The review/fix loop (shepherd) and
deploy/monitor stages are phase 5/6 work, layered on top of this workflow
once the slice is proven, not reimplemented here.

Known simplification: approval is signal-only in this version (no
.status.yaml-flip polling fallback) — an operator (or mctl_trigger_implementer
today) approves by calling the `approve` signal on this workflow's ID. A
human editing .status.yaml directly in gitops (the pre-Temporal affordance)
still works for the legacy cron-driven pipeline, but does not signal a
Temporal-tracked run; wiring that up is left to phase 5's cutover.

Also a known simplification: the implement step runs with no
service/slug filter, matching today's cwft-mctl-agents-implement default
(picks up whatever is accepted, max_proposals=1) — sufficient for a
single-issue run since nothing else should be queued ahead of it in a smoke
test, not yet a scoped "implement exactly this issue's proposal" call.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.registry import ResolvedRelease, resolve_agent_release
    from orchestrator.temporal.activities.state import ExecutionRecord, record_execution

ENVIRONMENT = "production"

# The Argo CWFTs already retry within a run (second-OAuth-account fallback on
# a 429/five_hour limit) — see activities/argo.py's module docstring. Never
# stack a Temporal retry on top of an SDK-backed run.
SDK_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=1)
SDK_STEP_TIMEOUT = timedelta(hours=2)
SDK_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=2)

FAST_ACTIVITY_TIMEOUT = timedelta(seconds=30)


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
    )


async def _run_cwft(operation: str, params: dict[str, str]) -> WorkflowResult:
    return await workflow.execute_activity(
        submit_and_wait,
        SubmitAndWaitInput(operation=operation, params=params),
        start_to_close_timeout=SDK_STEP_TIMEOUT,
        heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
        retry_policy=SDK_STEP_RETRY_POLICY,
    )


async def _record(agent: str, release: ResolvedRelease | None, result: WorkflowResult) -> None:
    await workflow.execute_activity(
        record_execution,
        ExecutionRecord(
            temporal_workflow_id=workflow.info().workflow_id,
            agent=agent,
            environment=ENVIRONMENT,
            version=release.version if release else "",
            image_ref=release.image_ref if release else "",
            argo_workflow_name=result.workflow_name,
            phase=result.phase,
        ),
        start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
    )


@workflow.defn
class DevLoopWorkflow:
    def __init__(self) -> None:
        self._approved = False

    @workflow.signal
    def approve(self) -> None:
        self._approved = True

    @workflow.run
    async def run(self, issue: IssueRef) -> DevLoopResult:
        # Pin the investigator version ONCE, at the start of this step. A
        # later promote/rollback in the registry must not retroactively
        # change what an in-flight (or replayed) workflow already ran.
        investigator_release = await _resolve("issue-investigator")
        investigate_params = {"issue_url": issue.issue_url}
        if investigator_release and investigator_release.image_ref:
            investigate_params["agent_image"] = investigator_release.image_ref
            investigate_params["agent_version"] = f"issue-investigator@{investigator_release.version}"

        investigate_result = await _run_cwft("mctl-agents-investigate", investigate_params)
        await _record("issue-investigator", investigator_release, investigate_result)

        if not investigate_result.succeeded:
            return DevLoopResult(investigate=investigate_result, implement=None)

        # Durable wait: this workflow can sit here for days without costing
        # anything beyond Temporal's own history storage — exactly the
        # "durable per-issue state" the plan's problem statement calls out
        # as missing from the current polling-cron pipeline.
        await workflow.wait_condition(lambda: self._approved)

        implementer_release = await _resolve("implementer")
        implement_params: dict[str, str] = {}
        if implementer_release and implementer_release.image_ref:
            implement_params["agent_image"] = implementer_release.image_ref
            implement_params["agent_version"] = f"implementer@{implementer_release.version}"

        implement_result = await _run_cwft("mctl-agents-implement", implement_params)
        await _record("implementer", implementer_release, implement_result)

        return DevLoopResult(investigate=investigate_result, implement=implement_result)
