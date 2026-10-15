"""Activity: the DevLoop's own canonical WorkItem and `we_`
(mctlhq/mctl-agents#636, mctl-api#571 design section 0).

Every new DevLoop is an execution of a WorkItem in mctl-api, because the
approval chain (`aar_` -> `art_` -> delegated operation) is bound to a `we_`.
A DevLoop dispatched from an execution request already has its `we_` (the
dispatcher fulfilled the request with this workflow's engine run) and never
comes here: creating or attaching another would split the run in two. Any
other DevLoop (issue trigger, cron) resolves its issue's WorkItem through
`POST /api/v1/work-items` (the create route is the resolver: it answers the
open item with the same `(tenant, external_key)`), then attaches
`(temporal, <workflow id>)`. Both routes are idempotent, so a retry of this
activity, or of the whole workflow under the same id, lands on the same
`wi_` and `we_`.

Refusals are values; uncertainty raises so the workflow's retry policy bounds
it. Neither is ever answered with a second work item or a local identity:
the caller fails the start.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from config.settings import tenant_for_service
from orchestrator.temporal.issue_ref import parse_issue_url
from orchestrator.work_context import creation
from orchestrator.work_context.client import WorkItemClient
from orchestrator.work_context.contract import WORK_ITEM_ABSENT, WORK_ITEM_FOUND
from orchestrator.work_context.executions import EXECUTION_ACTIVE, EXECUTION_REFUSED, EngineRun

ENGINE_TEMPORAL = "temporal"
PHASE_RUNNING = "Running"

#: Raised when the store (or the policy checkpoint) gave no usable answer.
UNAVAILABLE_TYPE = "CanonicalExecutionUnavailable"


@dataclass(frozen=True)
class EnsureExecutionInput:
    issue_url: str
    #: This run's own engine ref (`issue_ref.run_engine_ref`), which also
    #: scopes the create's idempotency key to the run.
    engine_ref: str
    #: A work item the caller already holds for this issue (`IssueRef`
    #: carries one when a surface started the loop). Empty: resolve by issue.
    work_item_id: str = ""


@dataclass(frozen=True)
class EnsuredExecution:
    ok: bool
    work_item_id: str = ""
    execution_id: str = ""
    sequence: int = 0
    #: Why not (ok is False): a definite refusal. Never free text from the
    #: store's body, only the status/typed code and our own wording.
    reason: str = ""


def _client() -> WorkItemClient:
    return WorkItemClient()


@activity.defn
async def ensure_issue_execution(input: EnsureExecutionInput) -> EnsuredExecution:
    """Resolve (or create) the issue's WorkItem and attach this workflow's
    engine run to it as `Running`."""
    client = _client()
    work_item_id = input.work_item_id
    try:
        service = parse_issue_url(input.issue_url).repo
    except ValueError as exc:
        return EnsuredExecution(ok=False, reason=str(exc))
    tenant = tenant_for_service(service)
    if tenant is None:
        # An unmapped service is a hard failure, never a default tenant:
        # the item must land in the tenant that owns the target.
        return EnsuredExecution(ok=False, reason=f"no tenant is configured for service {service!r}")
    if work_item_id:
        # A supplied item is held to what a created one would be: this
        # issue's, in this service's tenant. Otherwise the grant would be
        # decided under another issue's or another tenant's item while the
        # flip targets this issue's proposal.
        answer = await asyncio.to_thread(client.get, work_item_id)
        if answer.verdict == WORK_ITEM_ABSENT:
            return EnsuredExecution(ok=False, reason=f"work item {work_item_id} does not exist")
        if answer.verdict != WORK_ITEM_FOUND or answer.item is None:
            raise ApplicationError(
                f"work item {work_item_id}: {answer.verdict} {answer.reason}".strip(), type=UNAVAILABLE_TYPE
            )
        item = answer.item
        if item.external_key != creation.canonical_issue_url(input.issue_url) or item.tenant != tenant:
            return EnsuredExecution(
                ok=False,
                work_item_id=work_item_id,
                reason=(
                    f"work item {work_item_id} is {item.external_key!r} in tenant {item.tenant!r}, "
                    f"not {creation.canonical_issue_url(input.issue_url)!r} in {tenant!r}"
                ),
            )
    else:
        created = await asyncio.to_thread(client.create_work_item, tenant, input.issue_url, input.engine_ref)
        if created.verdict == creation.UNKNOWN:
            raise ApplicationError(f"work item for {input.issue_url}: {created.reason}", type=UNAVAILABLE_TYPE)
        if not created.usable:
            return EnsuredExecution(ok=False, reason=f"work item for {input.issue_url}: {created.reason}")
        work_item_id = created.work_item_id

    run = EngineRun(engine=ENGINE_TEMPORAL, engine_ref=input.engine_ref)
    attached = await asyncio.to_thread(client.attach_execution, work_item_id, run, PHASE_RUNNING)
    if attached.usable:
        return EnsuredExecution(
            ok=True, work_item_id=work_item_id, execution_id=attached.execution_id, sequence=attached.attempt,
        )
    if attached.verdict in (EXECUTION_REFUSED, EXECUTION_ACTIVE):
        # `execution_active`: another run holds the item. A definite no;
        # waiting would not change it, and this loop must not run beside it.
        return EnsuredExecution(
            ok=False, work_item_id=work_item_id,
            reason=f"attach {ENGINE_TEMPORAL}/{input.engine_ref} to {work_item_id}: {attached.reason}",
        )
    raise ApplicationError(
        f"attach {ENGINE_TEMPORAL}/{input.engine_ref} to {work_item_id}: {attached.verdict} {attached.reason}",
        type=UNAVAILABLE_TYPE,
    )
