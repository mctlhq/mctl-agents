"""Activity: submit an mctl-api operation (which maps 1:1 to an Argo
ClusterWorkflowTemplate, e.g. "mctl-agents-investigate") and poll it to a
terminal status.

Retry ownership (plan phase 4's explicit rule): the CWFTs already retry
*within* a run — on a failed attempt they re-run on a second OAuth account
(claude-code-oauth-token-2), which exists precisely because the first
account hits the five-hour 429. A Temporal RetryPolicy that re-submitted
would multiply real SDK runs (3 attempts x 2 accounts = 6), burning quota
hardest exactly when it's already exhausted. To get resumability without
that multiplication, this activity distinguishes "retry before submission
happened" (must re-submit) from "retry after submission happened" (must
NOT re-submit, only resume polling): the first successful submission is
recorded via `activity.heartbeat(workflow_name)` before the poll loop
starts, and a new attempt reads it back via
`activity.info().heartbeat_details` — if present, submission is skipped
entirely and polling resumes against the same Argo workflow name.
DevLoopWorkflow's retry policy for this activity may therefore allow a
small number of attempts (see SDK_STEP_RETRY_POLICY in workflows/dev_loop.py)
without ever causing a second real SDK run for the same step.

Do not re-read Argo for a result: mctl-agents-investigate carries
ttlStrategy.secondsAfterCompletion=86400, so a workflow that resumes after
that finds the Argo Workflow gone. This activity returns the terminal
result at completion time; callers must persist whatever they need (see
activities/state.py's record_execution) rather than re-querying Argo later.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
from temporalio import activity

from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

REQUEST_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 15.0
# Argo Workflow status.phase terminal values. "" / "Pending" / "Running" all
# mean "keep polling" — anything not in this set is treated as non-terminal
# rather than guessed at, since a not-yet-populated status block also reads
# as an empty phase right after submission.
TERMINAL_PHASES = frozenset({"Succeeded", "Failed", "Error"})
# How many consecutive poll failures (mctl-api 5xx, network blip) to ride
# out before giving up. Because this activity runs with maximum_attempts=1
# (see the module docstring), letting any single poll failure propagate
# would kill the whole activity hours into an already-submitted Argo run —
# losing track of it entirely, since the workflow_name isn't persisted
# anywhere else. At POLL_INTERVAL_SECONDS=15s, this rides out ~5 minutes of
# a down mctl-api before finally raising.
MAX_CONSECUTIVE_POLL_ERRORS = 20


@dataclass(frozen=True)
class SubmitAndWaitInput:
    # mctl-api operation name — matches the target ClusterWorkflowTemplate
    # name 1:1 for every mctl-agents-* operation (see
    # internal/operations/registry.go and AgentManifest.cluster_workflow_template).
    operation: str
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowResult:
    workflow_name: str
    phase: str  # Succeeded | Failed | Error
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.phase == "Succeeded"


@activity.defn
async def submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
    headers = auth_headers()
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        # Resume, don't resubmit: a prior attempt of THIS activity execution
        # already heartbeated a workflow_name once submission succeeded (see
        # below). If this attempt has that detail, the Argo run already
        # exists — go straight to polling instead of POSTing a second one.
        heartbeat_details = activity.info().heartbeat_details
        workflow_name = heartbeat_details[0] if heartbeat_details else None

        if workflow_name:
            activity.logger.info(
                "resuming poll for %s -> %s (retry after crash/heartbeat gap, no re-submit)",
                input.operation,
                workflow_name,
            )
        else:
            submit_resp = await client.post(
                f"/api/v1/operations/{input.operation}/execute",
                json=input.params,
                headers=headers,
            )
            submit_resp.raise_for_status()
            workflow_name = submit_resp.json()["workflow"]["workflowName"]
            activity.logger.info("submitted %s -> %s", input.operation, workflow_name)
            # Record the submission immediately, before the first poll, so
            # even a crash on the very next line leaves a retry able to find
            # workflow_name via heartbeat_details above instead of resubmitting.
            activity.heartbeat(workflow_name)

        consecutive_errors = 0
        while True:
            # Heartbeat before every poll, not just on change: a stuck
            # mctl-api / cluster makes this loop spin on the `continue`
            # below without ever reaching a terminal phase, and the
            # heartbeat is what lets Temporal notice a genuinely wedged
            # activity (worker crash, network partition) instead of the
            # activity looking alive forever because polling itself is
            # still succeeding.
            activity.heartbeat(workflow_name)

            try:
                status_resp = await client.get(f"/api/v1/workflows/{workflow_name}", headers=headers)
                status_resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                consecutive_errors += 1
                if consecutive_errors > MAX_CONSECUTIVE_POLL_ERRORS:
                    activity.logger.error(
                        "giving up polling %s after %d consecutive failures: %s",
                        workflow_name,
                        consecutive_errors,
                        exc,
                    )
                    raise
                activity.logger.warning(
                    "poll failed for %s (%d/%d consecutive): %s",
                    workflow_name,
                    consecutive_errors,
                    MAX_CONSECUTIVE_POLL_ERRORS,
                    exc,
                )
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            consecutive_errors = 0
            body = status_resp.json()
            live = body.get("live") or {}
            status_block = live.get("status") or {}
            phase = status_block.get("phase", "")

            if phase in TERMINAL_PHASES:
                return WorkflowResult(
                    workflow_name=workflow_name,
                    phase=phase,
                    started_at=status_block.get("startedAt"),
                    finished_at=status_block.get("finishedAt"),
                )

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
