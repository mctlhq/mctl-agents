"""Activity: mint one control-plane-asserted `ExecutionContext` and register
it with mctl-api (mctlhq/mctl-agents#196, ADR 011:
docs/adr/011-execution-identity-contract.md).

Mirrors `orchestrator/temporal/activities/state.py`'s shape: a flat,
Temporal-serializable request dataclass in, an HTTP POST under the same
`httpx` + `auth_headers()` pattern, best-effort against mctl-api rather than
fatal. The activity itself does the sealing (`orchestrator.execution_identity
.seal()`) rather than the workflow, for the same reason `resolve_agent_release`
computes its own image reference inside the activity: a Temporal workflow may
not call non-deterministic code (a wall clock read, `os.urandom`) directly,
and `seal()`'s `issued_at` and the fallback `trace_id` a caller might omit
both need one.

A non-2xx (or unreachable mctl-api, or a missing `MCTL_TOKEN`) never raises:
it degrades to a locally-minted, explicitly `unverified` context, matching
`_record`'s existing best-effort rule in
`orchestrator/temporal/workflows/dev_loop.py`. The caller (once wired into
`DevLoopWorkflow` — see ADR 011's Implementation map for why that wiring is
deferred) reads `MintedContext.stored` to know which happened; either way it
always gets back a usable `context_id`/`trace_id`.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from temporalio import activity

from orchestrator.execution_identity import (
    Actor,
    Assertions,
    Correlation,
    ExecutionContext,
    ExecutionIdentityError,
    Executor,
    Scope,
    Trigger,
    mint_local,
    seal,
)
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

REQUEST_TIMEOUT_SECONDS = 30.0
CONTEXT_ENDPOINT = "/api/v1/agents/executions/context"


@dataclass(frozen=True)
class MintRequest:
    """Everything `seal()` needs, flattened to primitives so Temporal's
    default dataclass converter never has to reconstruct a nested frozen
    dataclass from `execution_identity` on the wire."""

    trace_id: str
    workflow_type: str
    actor_type: str
    actor_id: str
    actor_verification: str
    executor_type: str
    trigger_type: str
    temporal_workflow_id: str
    scope_environment: str = "production"
    executor_id: str = ""
    executor_agent: str = ""
    executor_version: str = ""
    executor_image_ref: str = ""
    executor_binding: str = ""
    scope_tenant: str = "mctlhq"
    scope_repository: str = ""
    scope_target_repository_sha: str = ""
    scope_service: str = ""
    scope_slug: str = ""
    trigger_ref: str = ""
    temporal_run_id: str | None = None
    argo_workflow_name: str | None = None
    attempt: int = 0
    parent_context_id: str | None = None
    step_sequence: int = 0


@dataclass(frozen=True)
class MintedContext:
    context_id: str
    trace_id: str
    content_hash: str
    #: False when mctl-api rejected the write (or MCTL_TOKEN is unset, or the
    #: request timed out) — the returned context is then a locally-minted,
    #: `unverified` one rather than the control-plane-asserted document this
    #: activity tried to store.
    stored: bool


def _seal_control_plane_context(req: MintRequest, *, issued_at: str) -> ExecutionContext:
    return seal(
        trace_id=req.trace_id,
        workflow_type=req.workflow_type,
        actor=Actor(type=req.actor_type, id=req.actor_id, verification=req.actor_verification),
        executor=Executor(
            type=req.executor_type,
            id=req.executor_id,
            agent=req.executor_agent,
            version=req.executor_version,
            image_ref=req.executor_image_ref,
            binding=req.executor_binding,
        ),
        scope=Scope(
            environment=req.scope_environment,
            tenant=req.scope_tenant,
            repository=req.scope_repository,
            target_repository_sha=req.scope_target_repository_sha,
            service=req.scope_service,
            slug=req.scope_slug,
        ),
        trigger=Trigger(type=req.trigger_type, ref=req.trigger_ref),
        correlation=Correlation(
            temporal_workflow_id=req.temporal_workflow_id,
            temporal_run_id=req.temporal_run_id,
            argo_workflow_name=req.argo_workflow_name,
            attempt=req.attempt,
        ),
        assertions=Assertions(
            asserted_by="control-plane",
            asserted_fields=("actor", "executor.type", "executor.agent", "correlation", "trace_id"),
            declared_fields=(),
        ),
        issued_at=issued_at,
        parent_context_id=req.parent_context_id,
        step_sequence=req.step_sequence,
    )


@activity.defn
async def mint_execution_context(req: MintRequest) -> MintedContext:
    from datetime import UTC, datetime

    issued_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        context = _seal_control_plane_context(req, issued_at=issued_at)
    except ExecutionIdentityError:
        # A caller-supplied vocabulary field (actor_type, trigger_type,
        # workflow_type, executor_type, scope_environment) or a malformed
        # trace_id failed seal()'s validate() call. Same best-effort degrade
        # as the auth/network branches below, per this module's docstring:
        # this activity never raises.
        try:
            # mint_local() always replaces actor.type/actor.verification,
            # trigger.type and scope.environment with its own hardcoded,
            # always-valid defaults -- but it forwards executor_type,
            # workflow_type and trace_id to seal() unchanged. If one of
            # those three was the field that failed validate() above, this
            # first attempt re-triggers the identical ExecutionIdentityError
            # instead of degrading.
            context = mint_local(
                executor_type=req.executor_type,
                workflow_type=req.workflow_type,
                agent=req.executor_agent,
                version=req.executor_version,
                trace_id=req.trace_id,
            )
        except ExecutionIdentityError:
            # Retry with mint_local()'s own known-valid values for the three
            # fields it does not sanitize, so the degrade path always
            # succeeds regardless of which caller-supplied field was bad.
            context = mint_local(executor_type="service-agent", agent=req.executor_agent, version=req.executor_version)
        return MintedContext(
            context_id=context.context_id, trace_id=context.trace_id, content_hash=context.content_hash,
            stored=False,
        )

    try:
        headers = auth_headers()
    except RuntimeError:
        # MCTL_TOKEN unset — same degrade as every other consumer of
        # auth_headers() in this package: a missing token must not fail the
        # step this mint is decorating.
        context = mint_local(
            executor_type=req.executor_type,
            workflow_type=req.workflow_type,
            agent=req.executor_agent,
            version=req.executor_version,
            trace_id=req.trace_id,
        )
        return MintedContext(
            context_id=context.context_id, trace_id=context.trace_id, content_hash=context.content_hash,
            stored=False,
        )

    try:
        async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.post(CONTEXT_ENDPOINT, json=context.to_dict(), headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError:
        context = mint_local(
            executor_type=req.executor_type,
            workflow_type=req.workflow_type,
            agent=req.executor_agent,
            version=req.executor_version,
            trace_id=req.trace_id,
        )
        return MintedContext(
            context_id=context.context_id, trace_id=context.trace_id, content_hash=context.content_hash,
            stored=False,
        )

    return MintedContext(
        context_id=context.context_id, trace_id=context.trace_id, content_hash=context.content_hash, stored=True,
    )
