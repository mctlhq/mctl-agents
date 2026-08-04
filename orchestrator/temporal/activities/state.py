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
    argo_workflow_name: str
    phase: str  # Succeeded | Failed | Error


@activity.defn
async def record_execution(record: ExecutionRecord) -> None:
    headers = auth_headers()
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "/api/v1/agents/executions",
            json={
                "temporal_workflow_id": record.temporal_workflow_id,
                "agent": record.agent,
                "environment": record.environment,
                "version": record.version,
                "image_ref": record.image_ref,
                "argo_workflow_name": record.argo_workflow_name,
                "phase": record.phase,
            },
            headers=headers,
        )
        resp.raise_for_status()
