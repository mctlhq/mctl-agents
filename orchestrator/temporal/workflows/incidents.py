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
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.registry import ResolvedRelease, resolve_agent_release
    from orchestrator.temporal.activities.state import ExecutionRecord, record_execution
    from orchestrator.temporal.constants import EXECUTION_TASK_QUEUE

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

# Registry lookup and audit write: short calls against mctl-api, not agent
# work. Same values DevLoopWorkflow uses for the same two activities.
FAST_ACTIVITY_TIMEOUT = timedelta(seconds=30)
FAST_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=5)

ENVIRONMENT = "production"

# The registry name of the agent this loop runs — agents/_manifests/
# incident-responder/agent.yaml, published and promoted by the release
# pipeline like every other agent.
RESPONDER_AGENT = "incident-responder"


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
        params = {"mode": INCIDENTS_MODE}
        release: ResolvedRelease | None = None

        if workflow.patched("incident-registry-and-record"):
            # Pin the released responder image, exactly as the dev loop
            # pins investigate/implement/shepherd. Without this the run
            # took whatever image `cwft-mctl-agents-run` had baked in, so
            # a promotion or a rollback in the agent registry reached
            # every agent EXCEPT the one that runs unattended on a
            # schedule and writes auto-accepted proposals — the one where
            # nobody is watching the version it used.
            #
            # mctl-api passes parameters it does not declare straight
            # through (ValidateInput only walks declared ones), and
            # cwft-mctl-agents-run already declares agent_image and uses
            # it as the container image, so this needs no change in the
            # sibling repos — checked, rather than assumed, before
            # writing it.
            try:
                release = await workflow.execute_activity(
                    resolve_agent_release,
                    args=[RESPONDER_AGENT, ENVIRONMENT],
                    start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
                    retry_policy=FAST_ACTIVITY_RETRY_POLICY,
                )
            except ActivityError:
                # An mctl-api outage is a lookup failure, not an incident
                # response failure. Letting it propagate would abandon the
                # tick before submit_and_wait — the exact fail-closed
                # behaviour the branch below exists to avoid, arrived at
                # through the registry being down rather than empty
                # (codex P2 + agy P2 on #254).
                workflow.logger.warning(
                    "registry lookup for %s failed after retries — falling back to the "
                    "CWFT's baked-in default image for this tick",
                    RESPONDER_AGENT,
                )
                release = None

            if release and release.image_ref:
                params["agent_image"] = release.image_ref
                params["agent_version"] = f"{RESPONDER_AGENT}@{release.version}"
            else:
                # Deliberately NOT the dev loop's fail-closed gate. A
                # scheduled tick has no operator waiting to read the error
                # and republish; failing here would silently stop incident
                # response until someone noticed the schedule going red.
                # Running the CWFT default and saying so keeps the loop
                # working while the missing pin stays visible in the log
                # and in the execution record's empty version.
                workflow.logger.warning(
                    "no released image for %s in %s — running the CWFT's baked-in "
                    "default; publish and promote it to pin this loop",
                    RESPONDER_AGENT,
                    ENVIRONMENT,
                )

        # ADR-008 step 4, same marker as the dev loop's funnel: one patch id
        # covers the whole routing change, so an execution either routes all
        # of its submits to exec or none of them.  Checked before the call,
        # with the old branch kept verbatim below.
        if workflow.patched("exec-queue"):
            responder_result: WorkflowResult = await workflow.execute_activity(
                submit_and_wait,
                SubmitAndWaitInput(operation=INCIDENTS_OPERATION, params=params),
                task_queue=EXECUTION_TASK_QUEUE,
                start_to_close_timeout=SDK_STEP_TIMEOUT,
                heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
                retry_policy=SDK_STEP_RETRY_POLICY,
            )
        else:
            responder_result = await workflow.execute_activity(
                submit_and_wait,
                SubmitAndWaitInput(operation=INCIDENTS_OPERATION, params=params),
                start_to_close_timeout=SDK_STEP_TIMEOUT,
                heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
                retry_policy=SDK_STEP_RETRY_POLICY,
            )

        if workflow.patched("incident-registry-and-record"):
            await self._record(release, responder_result)

        return IncidentLoopWorkflowResult(responder_result=responder_result)

    async def _record(self, release: ResolvedRelease | None, result: WorkflowResult) -> None:
        """Link this Temporal workflow to the Argo run it submitted.

        #149 asks for incident runs to be traceable from a Temporal
        workflow ID to an Argo workflow name. Until now they were not:
        DevLoopWorkflow writes an execution record for every CWFT it
        submits, and this loop — the one that runs unattended and resolves
        incidents on its own — wrote none, so the only trace was a log
        line in a pod that has since been recycled.

        The workflow id is enough to tell one tick from the next, even
        though `worker.py`'s INCIDENTS_WORKFLOW_ID is a constant: that
        constant is the schedule ACTION's id, and Temporal appends the
        action's nominal time when the schedule fires ("The Action's
        timestamp is appended to the Workflow Id" —
        docs.temporal.io/schedule). Each tick therefore records
        `incidents-mctl-agents-<nominal-time>`, one id per Argo run, which
        is what #149 asks for. Recording `run_id` alongside it would not
        help today either: mctl-api's RecordAgentExecution decodes into a
        fixed request struct with a plain json.Decoder, so an undeclared
        field is dropped rather than persisted.

        Best-effort for the same reason it is in the dev loop: the CWFT
        being recorded has already finished, and letting an mctl-api blip
        fail the tick would turn a missing audit row into a missed
        incident response.
        """
        pinned = release if release and release.image_ref else None
        try:
            await workflow.execute_activity(
                record_execution,
                ExecutionRecord(
                    temporal_workflow_id=workflow.info().workflow_id,
                    agent=RESPONDER_AGENT,
                    environment=ENVIRONMENT,
                    version=pinned.version if pinned else "",
                    image_ref=pinned.image_ref if pinned else "",
                    # No single target repo: one run diagnoses incidents
                    # across every service, and the proposals it writes
                    # name their own. Empty rather than a guess.
                    target_repo="",
                    argo_workflow_name=result.workflow_name,
                    phase=result.phase,
                ),
                start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
                retry_policy=FAST_ACTIVITY_RETRY_POLICY,
            )
        except ActivityError:
            workflow.logger.warning(
                "record_execution failed after retries for %s argo_workflow=%s — "
                "continuing without a durable execution record for this tick",
                RESPONDER_AGENT,
                result.workflow_name,
            )
