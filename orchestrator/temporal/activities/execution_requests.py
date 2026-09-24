"""Activities: bind a dispatched DevLoop to the execution its request was
fulfilled with, and advance that execution's phase (mctlhq/mctl-agents#461).

The dispatcher (`orchestrator.temporal.dispatcher`) starts the DevLoop FIRST
and fulfils the execution request SECOND, so a crash between the two is
recovered by the next claim converging on the same workflow and the same
`(engine, engine_ref)`. The loop therefore cannot be handed its `we_` id in
its start input: it learns it here, by reading the request until mctl-api
says it is fulfilled. That also covers the crash the other way round (the
request fulfilled, the dispatcher gone): the loop reads the answer from the
store, not from the process that asked for it.

The binding is refused, never guessed, when the store's answer does not
describe THIS loop: the request belongs to another work item, the item is
about another issue, or the execution the request was fulfilled with is not
this workflow's own engine run. Refusals are values (`BoundExecution`);
"not fulfilled yet" and an unreadable store raise, so the workflow's retry
policy turns them into a bounded wait.

A refusal still names an execution (`BoundExecution.stranded`) when the
ledger proves one exists under THIS loop's engine ref: the fulfil minted it
for this loop, and nothing but this loop will ever end it. The item turned
out to be about another issue, or the advance to `Running` was refused, or
the engine ref answered with an execution the request does not name: the
loop must not run it (`work-item-mismatch`, or `execution-refused` for the
refused advance), but it must end it (`Failed`), or it stays
non-terminal and mctl-api refuses every later request for the item. A
refusal with no execution id proved nothing of this loop's exists (a
rejected request, another item's request, a fulfil for another engine run).

The same two activities serve a `resume` delivered onto a LIVE loop
(`DevLoopWorkflow.accept_execution_request`). Every execution the dispatcher
fulfils has the engine ref `<loop id>#<request id>`
(`issue_ref.request_engine_ref`): a loop's own dispatched request (since #461
option A; a history recorded before bound under the bare `dev-loop-xr_<id>`)
and a delivered resume alike, and the rules are the same — the store's
execution must be exactly that engine run.

Workflow code never performs this I/O itself (ADR-010 §9, as
`activities/lifecycle.py`): these activities are the only place it happens.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context.client import WorkItemClient
from orchestrator.work_context.contract import WORK_ITEM_FOUND
from orchestrator.work_context.executions import EXECUTION_REFUSED, EngineRun

ENGINE_TEMPORAL = "temporal"

BOUND = "bound"
#: The platform rejected the request: the loop must not run.
REQUEST_REJECTED = "rejected"
#: The store's answer does not describe this loop.
MISMATCH = "work-item-mismatch"
#: The execution is this loop's, but mctl-api (or the policy checkpoint)
#: refused its advance to `Running`: it exists and may not run (stranded).
EXECUTION_REFUSED_OUTCOME = "execution-refused"

#: Raised while the request is still pending/claimed, so the retry policy
#: waits for the dispatcher's fulfil.
NOT_FULFILLED_TYPE = "ExecutionRequestNotFulfilled"
#: Raised when the store cannot be read (or answers ambiguously).
UNREADABLE_TYPE = "WorkItemStoreUnreadable"


@dataclass(frozen=True)
class BindInput:
    work_item_id: str
    execution_request_id: str
    #: The workflow's own id: the engine ref the dispatcher fulfils with.
    engine_ref: str
    #: The issue the loop was started for. The item must still be about it.
    issue_url: str


@dataclass(frozen=True)
class BoundExecution:
    outcome: str
    #: BOUND: the execution this loop is. Any other outcome: the execution
    #: provably attached under this loop's engine ref that the loop must
    #: end without running (`stranded`), or "" when none is.
    execution_id: str = ""
    #: The execution's attempt number in the item's ledger.
    sequence: int = 0
    reason: str = ""

    @property
    def bound(self) -> bool:
        return self.outcome == BOUND

    @property
    def stranded(self) -> bool:
        """Refused, but an execution of this loop's engine ref exists: the
        caller ends it `Failed` (module docstring)."""
        return not self.bound and bool(self.execution_id)


@dataclass(frozen=True)
class AdvanceInput:
    work_item_id: str
    engine_ref: str
    phase: str


def _client() -> WorkItemClient:
    return WorkItemClient()


@activity.defn
async def bind_dispatched_execution(input: BindInput) -> BoundExecution:
    """The `we_` execution this loop is, once its request is fulfilled.

    Advances the execution to `Running` (a `resume` fulfilment attaches it
    `Pending`), only after the ledger shows it is this loop's own engine
    run: an attach under an engine ref the store does not already hold would
    CREATE an execution, which is exactly what this loop must never do."""
    client = _client()
    read = await asyncio.to_thread(client.execution_request, input.work_item_id, input.execution_request_id)
    if read.verdict == xr.REFUSED:
        # A definite no from mctl-api (e.g. 404 on the item-scoped route: no
        # such request under THIS work item). Retrying it for FULFILMENT_WAIT
        # would only end in a misleading "not fulfilled"; it is a mismatch.
        return BoundExecution(
            MISMATCH,
            reason=f"execution request {input.execution_request_id} of {input.work_item_id}: {read.reason}",
        )
    if read.verdict != xr.FOUND or read.request is None:
        raise ApplicationError(
            f"execution request {input.execution_request_id}: {read.verdict} {read.reason}".strip(),
            type=UNREADABLE_TYPE,
        )
    request = read.request
    if request.work_item_id != input.work_item_id:
        return BoundExecution(
            MISMATCH,
            reason=f"request {request.request_id} belongs to {request.work_item_id}, not {input.work_item_id}",
        )
    if request.state == xr.STATE_REJECTED:
        return BoundExecution(REQUEST_REJECTED, reason=request.reason or "rejected")
    if request.state != xr.STATE_FULFILLED or not request.execution_id:
        raise ApplicationError(f"execution request {request.request_id} is {request.state}", type=NOT_FULFILLED_TYPE)

    answer = await asyncio.to_thread(client.get, input.work_item_id)
    if answer.verdict != WORK_ITEM_FOUND or answer.item is None:
        raise ApplicationError(
            f"work item {input.work_item_id}: {answer.verdict} {answer.reason}", type=UNREADABLE_TYPE
        )
    item = answer.item
    own = next((e for e in item.executions if e.execution_id == request.execution_id), None)
    ours = own is not None and own.temporal_workflow_id == input.engine_ref
    if item.issue_url != input.issue_url:
        return BoundExecution(
            MISMATCH,
            # The fulfil minted it under this loop's engine ref: stranded.
            execution_id=request.execution_id if ours else "",
            reason=f"work item {item.work_item_id} is about {item.issue_url!r}, not {input.issue_url!r}",
        )
    if not ours:
        return BoundExecution(
            MISMATCH,
            reason=(
                f"request {request.request_id} was fulfilled with {request.execution_id}, which is not "
                f"{ENGINE_TEMPORAL}/{input.engine_ref} of {input.work_item_id}"
            ),
        )

    run = EngineRun(engine=ENGINE_TEMPORAL, engine_ref=input.engine_ref)
    attached = await asyncio.to_thread(client.attach_execution, input.work_item_id, run, "Running")
    if attached.verdict == EXECUTION_REFUSED:
        # A definite no (an ended execution, a policy DENY): the execution
        # exists (proven above) but cannot run. Refused, not retried, and
        # stranded: ending an already ended one is a harmless refusal.
        return BoundExecution(
            EXECUTION_REFUSED_OUTCOME,
            execution_id=request.execution_id,
            reason=f"advance {request.execution_id} to Running: {attached.reason}",
        )
    if not attached.usable:
        raise ApplicationError(
            f"advance {request.execution_id}: {attached.verdict} {attached.reason}", type=UNREADABLE_TYPE
        )
    if attached.execution_id != request.execution_id:
        # The advance just moved `attached.execution_id`, the execution under
        # this loop's engine ref, to Running: stranded, whatever it is.
        return BoundExecution(
            MISMATCH,
            execution_id=attached.execution_id,
            reason=f"{ENGINE_TEMPORAL}/{input.engine_ref} is {attached.execution_id}, "
            f"the request names {request.execution_id}",
        )
    return BoundExecution(BOUND, execution_id=attached.execution_id, sequence=attached.attempt)


@activity.defn
async def advance_dispatched_execution(input: AdvanceInput) -> str:
    """Advance this loop's execution to `input.phase`. A definite refusal is
    returned (the caller logs it); an unanswered one raises for a retry."""
    run = EngineRun(engine=ENGINE_TEMPORAL, engine_ref=input.engine_ref)
    answer = await asyncio.to_thread(_client().attach_execution, input.work_item_id, run, input.phase)
    if answer.usable or answer.verdict == EXECUTION_REFUSED:
        return f"{answer.verdict} {answer.execution_id or answer.reason}".strip()
    raise ApplicationError(f"advance to {input.phase}: {answer.verdict} {answer.reason}", type=UNREADABLE_TYPE)
