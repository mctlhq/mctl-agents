"""IncidentLoopWorkflow: durable incident responder loop on Temporal.

This workflow does NOT run the incident responder itself — it submits the
Argo `mctl-agents-incidents` operation and waits, exactly like
`DevLoopWorkflow` does for investigate/implement.

Why (2026-08-15 incident, mctl-agents#179): the previous version called
`respond_incidents_activity`, which ran `run_incident_responder()` — a full
Claude SDK session — inside the worker pod. The responder writes its
proposals to `{STATE_DIR}/<service>/proposals/<slug>/` and expects a git
checkout to commit them from, both of which only exist in the Argo pod (the
CWFT's `clone-gitops` step creates /workdir/mctl-gitops, and its
`commit-and-push` step is what actually lands the proposals on main). In the
worker there is no checkout, no STATE_DIR and no commit path, so every tick
searched the filesystem for a directory that was never going to be there
and died OOMKilled against the worker's deliberately small 256Mi limit.

That was invisible for as long as the SDK had no OAuth token — it returned
"Not logged in" in a fraction of a second, spending neither memory nor
quota. Giving the worker a token (gitops #850) exposed both holes at once.

Keeping the worker a thin orchestrator is the fix: the SDK work belongs in
Argo, which already has the clone, the commit step, a 1Gi workdir and the
second-OAuth-account fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait

# mctl-api operation name; maps 1:1 to the `mctl-agents-run` CWFT with
# mode=incident-responder (see mctl-api internal/operations/registry.go).
INCIDENTS_OPERATION = "mctl-agents-incidents"
# Passed explicitly rather than relying on the registry's default: the
# operation's Enum locks this value, so a mismatch fails loudly at submit
# time instead of silently running some other mode.
INCIDENTS_MODE = "incident-responder"

# Same three values, for the same reasons, as DevLoopWorkflow's SDK steps:
# the CWFT already retries within a run (account-2 fallback), so this retry
# policy exists to let an attempt RESUME polling after a worker crash —
# submit_and_wait refuses to re-POST once it has heartbeated a workflow name.
SDK_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
SDK_STEP_TIMEOUT = timedelta(hours=2)
SDK_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=2)


@dataclass(frozen=True)
class IncidentLoopWorkflowResult:
    responder_result: WorkflowResult


@workflow.defn
class IncidentLoopWorkflow:
    @workflow.run
    async def run(self) -> IncidentLoopWorkflowResult:
        # A Failed/Error phase is returned, not raised: a failed responder
        # run is an ordinary outcome of a scheduled tick (the next tick
        # retries from scratch), and raising would only add noise to the
        # schedule's failure count without changing anything.
        #
        # Note the phase is a WEAK success signal in the other direction
        # too: run_all.py's _safe_run_incident_responder swallows transient
        # responder failures, so Argo can report Succeeded for a run that
        # diagnosed nothing. That predates this workflow (it is what the
        # Argo cron did before it was suspended) and is tracked separately.
        responder_result: WorkflowResult = await workflow.execute_activity(
            submit_and_wait,
            SubmitAndWaitInput(operation=INCIDENTS_OPERATION, params={"mode": INCIDENTS_MODE}),
            start_to_close_timeout=SDK_STEP_TIMEOUT,
            heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
            retry_policy=SDK_STEP_RETRY_POLICY,
        )

        return IncidentLoopWorkflowResult(responder_result=responder_result)
