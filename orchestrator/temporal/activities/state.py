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
    # implement submit from before the outcome/reason fields existed).
    # Optional on the wire — see record_execution for what mctl-api does
    # with it today.
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
    # The two #418 fields are OPTIONAL on the wire, and today mctl-api does
    # not store them: its recordExecutionRequest (mctl-api
    # internal/api/handlers_agent_registry.go) has no outcome /
    # pre_start_reason fields and decodes with a plain json.Decoder, so
    # unknown keys are accepted and dropped, not rejected. Persisting them
    # is a cross-repo follow-up (ADR-008's #418 amendment).
    #
    # Every implement record carries a non-empty `outcome`, so if mctl-api
    # ever tightens that decoder (DisallowUnknownFields) ahead of growing
    # the columns, the whole POST would 400 and — since callers swallow a
    # failed record_execution — the ENTIRE execution row would be lost, not
    # just these two fields (#459 review). So a 4xx on a body carrying them
    # is retried once without them: a schema mismatch costs the new fields,
    # never the record.
    optional: dict[str, str] = {}
    if record.outcome:
        optional["outcome"] = record.outcome
    if record.pre_start_reason:
        optional["pre_start_reason"] = record.pre_start_reason
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            "/api/v1/agents/executions",
            json={**body, **optional},
            headers=headers,
        )
        if optional and 400 <= resp.status_code < 500:
            activity.logger.warning(
                "mctl-api rejected the %s execution record carrying optional keys %s (HTTP %d: %s); "
                "retrying once without them so the row itself is not lost",
                record.agent,
                sorted(optional),
                resp.status_code,
                resp.text[:200],
            )
            resp = await client.post(
                "/api/v1/agents/executions",
                json=body,
                headers=headers,
            )
        resp.raise_for_status()
