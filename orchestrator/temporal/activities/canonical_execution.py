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
`(temporal, <workflow id>#run_<run id>)` (`issue_ref.run_engine_ref`). Both
routes are idempotent per run: a retry of this activity lands on the same
`wi_` and `we_`, while a new run of the same workflow id gets its own `we_`
on the same open `wi_` (and a new `wi_` if that one has ended), because
mctl-api never reopens an ended execution.

A run that died without ending its `we_` (a terminate runs no code) would
hold the item for ever: at most one run of a workflow id is open at a time,
so a non-terminal `we_` of ANOTHER run of this loop id is a dead one, and
the new run fails it before it attaches its own.

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
from orchestrator.temporal.issue_ref import is_run_engine_ref, loop_id_of_engine_ref, parse_issue_url
from orchestrator.work_context import creation
from orchestrator.work_context.client import WorkItemClient
from orchestrator.work_context.contract import WORK_ITEM_ABSENT, WORK_ITEM_FOUND
from orchestrator.work_context.executions import EXECUTION_ACTIVE, EXECUTION_REFUSED, EngineRun

ENGINE_TEMPORAL = "temporal"
PHASE_RUNNING = "Running"
PHASE_FAILED = "Failed"
_TERMINAL_PHASES = frozenset({"Succeeded", "Failed", "Error"})

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
    # Lower-cased like the canonical issue URL: GitHub repo names are
    # case-insensitive, the tenant map's keys are lower case.
    tenant = tenant_for_service(service.lower())
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
        # The run suffix alone scopes the key to this run; the issue is
        # already in it (claude P3 on #651: the whole ref repeated it).
        run_scope = input.engine_ref.rpartition("#")[2]
        created = await asyncio.to_thread(client.create_work_item, tenant, input.issue_url, run_scope)
        if created.verdict == creation.UNKNOWN:
            raise ApplicationError(f"work item for {input.issue_url}: {created.reason}", type=UNAVAILABLE_TYPE)
        if not created.usable:
            return EnsuredExecution(ok=False, reason=f"work item for {input.issue_url}: {created.reason}")
        work_item_id = created.work_item_id

    run = EngineRun(engine=ENGINE_TEMPORAL, engine_ref=input.engine_ref)
    attached = await asyncio.to_thread(client.attach_execution, work_item_id, run, PHASE_RUNNING)
    if attached.verdict == EXECUTION_ACTIVE and await _fail_dead_runs(client, work_item_id, input.engine_ref):
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


async def _fail_dead_runs(client: WorkItemClient, work_item_id: str, engine_ref: str) -> bool:
    """Fail every non-terminal `we_` of another run of this loop id (claude
    P2 on #651). Returns whether any was failed, so the attach is worth
    one more try. Only `#run_` refs of exactly this loop id are touched:
    another loop's, a dispatched request's (`#xr_`) or another engine's
    execution is never this run's to end. Uncertainty raises."""
    if not is_run_engine_ref(engine_ref):
        return False
    answer = await asyncio.to_thread(client.get, work_item_id)
    if answer.verdict != WORK_ITEM_FOUND or answer.item is None:
        raise ApplicationError(
            f"work item {work_item_id}: {answer.verdict} {answer.reason}".strip(), type=UNAVAILABLE_TYPE
        )
    loop = loop_id_of_engine_ref(engine_ref)
    failed = False
    for execution in answer.item.executions:
        ref = execution.temporal_workflow_id
        if (
            ref == engine_ref
            or not is_run_engine_ref(ref)
            or loop_id_of_engine_ref(ref) != loop
            or not execution.phase
            or execution.phase in _TERMINAL_PHASES
        ):
            continue
        ended = await asyncio.to_thread(
            client.attach_execution, work_item_id, EngineRun(engine=ENGINE_TEMPORAL, engine_ref=ref), PHASE_FAILED
        )
        if not ended.usable:
            raise ApplicationError(
                f"end the dead run {ref} of {work_item_id}: {ended.verdict} {ended.reason}".strip(),
                type=UNAVAILABLE_TYPE,
            )
        activity.logger.info("failed the dead run's execution %s (%s) of %s", execution.execution_id, ref, work_item_id)
        failed = True
    return failed
