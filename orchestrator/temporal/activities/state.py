"""Activity: persist a durable execution record for one DevLoopWorkflow step.

This is NOT the gitops `.status.yaml` commit — that stays inside the Argo
CWFT's own commit-and-push step (see cwft-mctl-agents-investigate.yaml etc.),
which already holds the `mctl-gitops-main-writes` mutex correctly alongside
every other workflow that pushes to gitops main. Re-implementing that commit
here, in the worker, would need its own copy of that serialization against a
lock Temporal knows nothing about (see the plan's "gitops commit step" note).

What this activity records is the phase-4 execution audit trail: which agent
version actually ran, on which Argo workflow, with what result — the record
`mctl_list_recent_agent_runs` needs to answer "which agent version produced
this PR" without re-querying Argo (whose workflow objects expire after
ttlStrategy.secondsAfterCompletion).
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from temporalio import activity

from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ExecutionRecord:
    temporal_workflow_id: str
    agent: str
    environment: str
    version: str  # "" when no registry release existed yet (CWFT default image used)
    image_ref: str  # "" when no registry release existed yet
    # The sibling repo (e.g. "mctl-telegram") this run's target-repo inputs
    # came from — see docs/agent-inventory.yaml's runtimeContextInputs note:
    # an agent version is only reproducible against a fixed target SHA, and
    # this is the first half of that (which repo). The exact SHA isn't
    # captured here yet: no CWFT exposes it as a workflow output today, and
    # adding one is a cross-repo gitops change deferred to a follow-up rather
    # than folded into this slice.
    target_repo: str
    argo_workflow_name: str
    phase: str  # Succeeded | Failed | Error
    # The implement_outcome.py taxonomy (#395, #418): "" when this step's
    # outcome was never classified (every non-implement agent, and an
    # implement submit from before the outcome/reason fields existed),
    # posted only when non-empty so an mctl-api that hasn't grown these
    # columns yet just doesn't get them.
    outcome: str = ""
    # WHY a pre_start outcome never started: lock_wait, unscheduled or
    # unknown. "" when outcome isn't pre_start at all.
    pre_start_reason: str = ""


@activity.defn
async def record_execution(record: ExecutionRecord) -> None:
    headers = auth_headers()
    body: dict[str, str] = {
        "temporal_workflow_id": record.temporal_workflow_id,
        "agent": record.agent,
        "environment": record.environment,
        "version": record.version,
        "image_ref": record.image_ref,
        "target_repo": record.target_repo,
        "argo_workflow_name": record.argo_workflow_name,
        "phase": record.phase,
    }
    # Sent only when non-empty (#418): whether mctl-api's executions
    # endpoint has grown these two columns is an open question this repo
    # cannot answer (see the proposal's open questions), and this call is
    # already best-effort — record_execution's caller swallows a failure —
    # so the worst case of sending a field mctl-api rejects is a missing
    # audit field, not a broken loop.
    if record.outcome:
        body["outcome"] = record.outcome
    if record.pre_start_reason:
        body["pre_start_reason"] = record.pre_start_reason
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "/api/v1/agents/executions",
            json=body,
            headers=headers,
        )
        resp.raise_for_status()
