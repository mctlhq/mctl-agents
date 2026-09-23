"""The execution-request dispatcher (mctlhq/mctl-agents#461).

Turns "a surface asked for this work item to run" (an mctl-api execution
request, mctl-api#368) into a DevLoop run that owns the canonical execution
identity. The execution platform is this process, acting as the service
principal; `surface:telegram` and every other surface only ever create the
request.

One dispatch, in order:

1. **Claim** the oldest claimable request under a lease (mctl-api's CAS; a
   lapsed lease is claimable again, under a new claim token).
2. **Decide** what to run, from the store, never guessed:
   - v1 runs the investigator for an item bound to a mctlhq GitHub issue
     (`issue_url`). Anything else is rejected `no_runnable_target`.
   - A DevLoop already live for the item (behind any Temporal execution in
     its ledger, or the issue's own `dev-loop-<owner>-<repo>-<n>` loop)
     refuses a `start` (`loop_active`) and a `resume`
     (`resume_onto_live_loop_unsupported`, see below). No live loop: a
     `resume` starts a continuation exactly as `start` starts the first run;
     mctl-api's fulfil applies the `/resume` rule and re-decides the item's
     state, and the new run's approval is its own, never inherited.
3. **Start** the DevLoop under `dispatched_workflow_id(request id)` with a
   reuse policy that refuses a second run (`start_dispatched_dev_loop`). A
   run that already ended under that id rejects the request
   (`engine_run_ended`).
4. **Fulfil** with `(temporal, <that workflow id>)`: mctl-api attaches the
   `we_` execution. The engine ref is the workflow id, so it is as
   deterministic as the workflow id.

Start comes BEFORE fulfil on purpose. A crash between the two leaves a
claimed request whose lease lapses; the next claim (new token) derives the
same workflow id, attaches to the same run (USE_EXISTING), and fulfils the
same `(engine, engine_ref)`. A crash after the fulfil leaves nothing to do:
the loop reads its `we_` from the fulfilled request itself
(`activities/execution_requests.py`). No order of crashes produces a second
run or a second execution.

**Resume onto a live loop is refused, not delivered, in v1.** The existing
`resume` signal (#267) carries the resumed execution's id, which exists only
after the fulfil. A fulfilled request is never claimable again, so a crash
between that fulfil and the signal would lose the resume with nothing left to
retry it; and nothing would advance that resumed execution out of `Pending`,
which then blocks every later request for the item (`execution_active`). A
durable delivery needs a place to keep "fulfilled but not yet delivered" that
is not this process, which ADR 011 has not decided; the refusal is typed so a
surface can say so and ask again once the loop ends.

Each claim, start, fulfil and reject prints one structured audit line
(`EXECUTION_REQUEST_DISPATCH {...}`) carrying the request, work item,
workflow and execution ids; mctl-api records its own events and audit rows.
Every mctl-api mutation goes through the policy checkpoint (#197) inside
`WorkItemClient`.

**Off by default.** Nothing here runs unless `EXECUTION_REQUEST_DISPATCHER`
is set to a truthy value: the worker's loop (`worker.main`) and the operator
CLI (`cli.py dispatch-once`) both check `enabled()` first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator.temporal.issue_ref import (
    dispatched_workflow_id,
    is_dispatched_workflow_id,
    parse_issue_url,
    workflow_id_for,
)
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context.contract import WORK_ITEM_FOUND

logger = logging.getLogger(__name__)

ENABLED_ENV_VAR = "EXECUTION_REQUEST_DISPATCHER"
LEASE_ENV_VAR = "EXECUTION_REQUEST_LEASE_SECONDS"
INTERVAL_ENV_VAR = "EXECUTION_REQUEST_POLL_SECONDS"

#: Long enough to cover a Temporal start and a fulfil with room for a slow
#: mctl-api; short enough that a crashed dispatcher's request is picked up
#: again within minutes. Well under the loop's FULFILMENT_WAIT.
DEFAULT_LEASE_SECONDS = 120
DEFAULT_INTERVAL_SECONDS = 15
#: Claims per tick at most, so one tick cannot monopolise the loop.
MAX_CLAIMS_PER_TICK = 10

ENGINE = "temporal"
AUDIT_PREFIX = "EXECUTION_REQUEST_DISPATCH"

# DevLoop liveness, as `TemporalPort.loop_state` answers it.
LOOP_RUNNING = "running"
LOOP_CLOSED = "closed"
LOOP_ABSENT = "absent"

# `TemporalPort.start` answers.
START_STARTED = "started"
#: A run under that id already ended; the reuse policy refused a second.
START_CLOSED = "closed"

# Dispatch outcomes.
NOTHING = "nothing"
FULFILLED = "fulfilled"
REJECTED = "rejected"
#: Left claimed on purpose: the store or Temporal did not answer. The lease
#: lapses and a later claim retries.
DEFERRED = "deferred"
#: mctl-api fenced this holder (its claim lapsed or was superseded).
FENCED = "fenced"
#: The request closed under us (fulfilled with another run, or rejected).
CLOSED = "closed"
CLAIM_FAILED = "claim-failed"

_TRUTHY = {"1", "true", "yes", "on"}

#: mctl-api's terminal execution phases (`workitems.IsTerminalPhase`).
TERMINAL_PHASES = frozenset({"Succeeded", "Failed", "Error"})

#: The ONLY fulfil refusals that reject the request. Both are mctl-api's
#: typed re-decision of the item at fulfilment (`checkStart`/`checkRunnable`/
#: `resumeTx` in internal/workitems), and both are permanent for THIS request,
#: because its `expected_state_version` and kind never change:
#:
#: - `state_version_conflict`: the item moved past the version the surface
#:   asked about. No later fulfil of this request can match it again.
#: - `invalid_transition`: the item is terminal, or cannot take this kind
#:   any more (a `start` for an item that has run, or is waiting).
#:
#: Everything else DEFERS (the lease lapses, a later claim retries) because it
#: says nothing permanent about the request:
#:
#: - `execution_active`: another execution is non-terminal. That ends — the
#:   live run finishes, or `_reconcile_closed_loops` fails one whose
#:   dispatched loop is gone — and until then a retry is the right answer.
#:   It cannot defer forever: this request's own loop gives up after
#:   FULFILMENT_WAIT, and the next claim then rejects `engine_run_ended`.
#: - `invalid_request` and any other untyped or unknown 4xx: most likely a
#:   schema skew between this build and mctl-api; rejecting would destroy
#:   every request that a fixed build could still serve.
#: - a local policy-checkpoint DENY (no code at all): a statement about this
#:   worker's identity or configuration, not about the request.
TERMINAL_FULFIL_CODES = frozenset({"state_version_conflict", "invalid_transition"})


def enabled() -> bool:
    """Is the dispatcher switched on? Default: no. Anything but an explicit
    truthy value is off: a typo must not start claiming users' requests."""
    return os.environ.get(ENABLED_ENV_VAR, "").strip().lower() in _TRUTHY


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return min(max(value, lo), hi)


def lease_seconds() -> int:
    return _int_env(LEASE_ENV_VAR, DEFAULT_LEASE_SECONDS, xr.MIN_LEASE_SECONDS, xr.MAX_LEASE_SECONDS)


def interval_seconds() -> int:
    return _int_env(INTERVAL_ENV_VAR, DEFAULT_INTERVAL_SECONDS, 1, 3600)


def audit(event: str, **fields: Any) -> None:
    """One structured audit line. Ids only: never the claim token."""
    record = {"event": event, **{k: v for k, v in fields.items() if v not in (None, "")}}
    print(f"{AUDIT_PREFIX} {json.dumps(record, sort_keys=True)}", flush=True)


class TemporalPort(Protocol):
    """The two things the dispatcher needs from Temporal."""

    async def start(self, issue: Any) -> str:
        """START_STARTED (new or already running), or START_CLOSED."""
        ...

    async def loop_state(self, workflow_id: str) -> str:
        """LOOP_RUNNING, LOOP_CLOSED or LOOP_ABSENT. Raises when Temporal
        cannot answer."""
        ...


class TemporalClientPort:
    """`TemporalPort` over a live `temporalio` client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def start(self, issue: Any) -> str:
        from temporalio.exceptions import WorkflowAlreadyStartedError

        from orchestrator.temporal.start import start_dispatched_dev_loop

        try:
            await start_dispatched_dev_loop(self._client, issue)
        except WorkflowAlreadyStartedError:
            return START_CLOSED
        return START_STARTED

    async def loop_state(self, workflow_id: str) -> str:
        from temporalio.client import WorkflowExecutionStatus
        from temporalio.service import RPCError, RPCStatusCode

        try:
            desc = await self._client.get_workflow_handle(workflow_id).describe()
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                return LOOP_ABSENT
            raise
        return LOOP_RUNNING if desc.status == WorkflowExecutionStatus.RUNNING else LOOP_CLOSED


@dataclass(frozen=True)
class DispatchOutcome:
    action: str
    execution_request_id: str = ""
    work_item_id: str = ""
    workflow_id: str = ""
    execution_id: str = ""
    reason: str = ""


def runnable_issue_url(issue_url: str) -> str:
    """The item's issue URL when v1 can run it (a mctlhq GitHub issue, the
    only shape `DevLoopWorkflow` accepts), else ""."""
    if not issue_url:
        return ""
    try:
        parse_issue_url(issue_url)
    except ValueError:
        return ""
    return issue_url


class Dispatcher:
    """Claims and dispatches execution requests. `api` is a `WorkItemClient`
    (synchronous; called off the event loop), `temporal` a `TemporalPort`."""

    def __init__(self, api: Any, temporal: TemporalPort, *, lease: int | None = None) -> None:
        self._api = api
        self._temporal = temporal
        self._lease = lease if lease is not None else lease_seconds()

    async def dispatch_once(self) -> DispatchOutcome:
        """Claim one request and take it as far as it can go."""
        claim = await asyncio.to_thread(self._api.claim_execution_request, self._lease)
        if claim.verdict == xr.NONE_CLAIMABLE:
            return DispatchOutcome(NOTHING)
        if claim.verdict != xr.CLAIMED or claim.request is None:
            audit("claim_failed", verdict=claim.verdict, reason=claim.reason)
            return DispatchOutcome(CLAIM_FAILED, reason=claim.reason)
        request, token = claim.request, claim.claim_token
        own_id = dispatched_workflow_id(request.request_id)
        audit(
            "claim",
            execution_request_id=request.request_id,
            work_item_id=request.work_item_id,
            kind=request.kind,
            workflow_id=own_id,
            claim_expires_at=request.claim_expires_at,
        )
        try:
            return await self._dispatch(request, token, own_id)
        except Exception as exc:
            logger.exception("dispatch of %s failed", request.request_id)
            return self._defer(request, own_id, f"{type(exc).__name__}: {exc}")

    async def _dispatch(self, request: xr.ExecutionRequest, token: str, own_id: str) -> DispatchOutcome:
        if request.kind not in xr.KINDS:
            return await self._reject(request, token, own_id, xr.UNSUPPORTED_KIND)

        answer = await asyncio.to_thread(self._api.get, request.work_item_id)
        if answer.verdict != WORK_ITEM_FOUND or answer.item is None:
            # Not a reason to reject: the store did not answer. Guessing
            # either way would be wrong, so the lease decides when to retry.
            return self._defer(request, own_id, f"work item {answer.verdict}: {answer.reason}")
        item = answer.item
        issue_url = runnable_issue_url(item.issue_url)
        if not issue_url:
            return await self._reject(request, token, own_id, xr.NO_RUNNABLE_TARGET)

        await self._reconcile_closed_loops(request, item, own_id)

        for candidate in _live_loop_candidates(item, issue_url, own_id):
            if await self._temporal.loop_state(candidate) == LOOP_RUNNING:
                reason = xr.LOOP_ACTIVE if request.kind == xr.KIND_START else xr.RESUME_ONTO_LIVE_LOOP_UNSUPPORTED
                return await self._reject(request, token, own_id, reason, live_loop=candidate)

        from orchestrator.temporal.workflows.dev_loop import IssueRef

        started = await self._temporal.start(
            IssueRef(issue_url=issue_url, work_item_id=item.work_item_id, execution_request_id=request.request_id)
        )
        if started == START_CLOSED:
            return await self._reject(request, token, own_id, xr.ENGINE_RUN_ENDED)
        audit(
            "start",
            execution_request_id=request.request_id,
            work_item_id=request.work_item_id,
            kind=request.kind,
            workflow_id=own_id,
            issue_url=issue_url,
        )
        return await self._fulfil(request, token, own_id)

    async def _fulfil(self, request: xr.ExecutionRequest, token: str, own_id: str) -> DispatchOutcome:
        answer = await asyncio.to_thread(self._api.fulfil_execution_request, request, token, ENGINE, own_id)
        if answer.verdict == xr.UNKNOWN:
            # A lost answer: the store may already have committed. The same
            # holder repeating the same engine run gets the same execution,
            # so one immediate retry is safe; after that the lease decides.
            answer = await asyncio.to_thread(self._api.fulfil_execution_request, request, token, ENGINE, own_id)
        ids = {"execution_request_id": request.request_id, "work_item_id": request.work_item_id, "workflow_id": own_id}
        if answer.verdict == xr.FULFILLED:
            audit("fulfil", **ids, engine=ENGINE, engine_ref=own_id, execution_id=answer.execution_id)
            return DispatchOutcome(FULFILLED, **ids, execution_id=answer.execution_id)
        if answer.verdict == xr.NOT_CLAIMED:
            audit("fenced", **ids, reason=answer.reason)
            return DispatchOutcome(FENCED, **ids, reason=answer.reason)
        if answer.verdict == xr.CLOSED:
            audit("closed", **ids, execution_id=answer.execution_id, reason=answer.reason)
            return DispatchOutcome(CLOSED, **ids, execution_id=answer.execution_id, reason=answer.reason)
        if answer.verdict == xr.REFUSED and answer.code in TERMINAL_FULFIL_CODES:
            # mctl-api re-decided the item and said a permanent no (see
            # TERMINAL_FULFIL_CODES). The request stays claimed for us to
            # reject; the started loop reads the rejection and ends.
            return await self._reject(request, token, own_id, f"{xr.FULFIL_REFUSED}:{answer.code}")
        return self._defer(request, own_id, f"fulfil {answer.verdict}: {answer.reason}")

    async def _reconcile_closed_loops(self, request: xr.ExecutionRequest, item: Any, own_id: str) -> None:
        """Fail every execution that a dispatched loop left non-terminal and
        can no longer end itself (mctlhq/mctl-agents#461).

        A dispatched loop ends its own `we_` on every exit it can run code
        on, but a TERMINATED workflow runs none, and its execution would then
        block every later request for the item (`execution_active`). Narrow
        on purpose: only a Temporal execution whose engine ref is a
        dispatched loop id (this dispatcher's own runs), only while it is
        non-terminal, and only once Temporal says that run is CLOSED. An
        execution of another engine, of an issue-keyed loop, of a run that
        is still RUNNING or that Temporal does not know (ABSENT) is never
        touched. The write is the same attach-or-advance the loop itself
        uses, through the policy checkpoint; its answer is logged and never
        stops the dispatch (mctl-api's fulfil re-decides regardless)."""
        from orchestrator.work_context.executions import EngineRun

        for execution in item.executions:
            ref = execution.temporal_workflow_id
            if not ref or ref == own_id or not is_dispatched_workflow_id(ref):
                continue
            if not execution.phase or execution.phase in TERMINAL_PHASES:
                continue
            if await self._temporal.loop_state(ref) != LOOP_CLOSED:
                continue
            answer = await asyncio.to_thread(
                self._api.attach_execution, item.work_item_id, EngineRun(engine=ENGINE, engine_ref=ref), "Failed"
            )
            audit(
                "reconcile",
                execution_request_id=request.request_id,
                work_item_id=item.work_item_id,
                workflow_id=ref,
                execution_id=execution.execution_id,
                phase_was=execution.phase,
                verdict=answer.verdict,
                reason=answer.reason,
            )

    async def _reject(
        self, request: xr.ExecutionRequest, token: str, own_id: str, reason: str, *, live_loop: str = ""
    ) -> DispatchOutcome:
        answer = await asyncio.to_thread(self._api.reject_execution_request, request, token, reason)
        ids = {"execution_request_id": request.request_id, "work_item_id": request.work_item_id, "workflow_id": own_id}
        audit("reject", **ids, reason=reason, verdict=answer.verdict, live_loop=live_loop)
        if answer.verdict == xr.REJECTED:
            return DispatchOutcome(REJECTED, **ids, reason=reason)
        if answer.verdict == xr.NOT_CLAIMED:
            return DispatchOutcome(FENCED, **ids, reason=answer.reason)
        return DispatchOutcome(DEFERRED, **ids, reason=f"reject {answer.verdict}: {answer.reason}")

    def _defer(self, request: xr.ExecutionRequest, own_id: str, reason: str) -> DispatchOutcome:
        audit(
            "defer",
            execution_request_id=request.request_id,
            work_item_id=request.work_item_id,
            workflow_id=own_id,
            reason=reason,
        )
        return DispatchOutcome(
            DEFERRED,
            execution_request_id=request.request_id,
            work_item_id=request.work_item_id,
            workflow_id=own_id,
            reason=reason,
        )


def _live_loop_candidates(item: Any, issue_url: str, own_id: str) -> list[str]:
    """DevLoops that may be live for this item, other than this request's
    own run (which is a convergence, never a conflict): the loop behind
    EVERY Temporal execution in its ledger, and the issue-keyed loop the
    `agents:intake` label path starts.

    Every one, not the latest: a dispatched loop ends its execution after
    the investigator run and then stays RUNNING for days at the approval
    gate or in the merge watch, so a newer execution from another loop says
    nothing about whether an older loop is still alive."""
    candidates = [e.temporal_workflow_id for e in item.executions if e.temporal_workflow_id]
    candidates.append(workflow_id_for(issue_url))
    return [c for c in dict.fromkeys(candidates) if c != own_id]


async def run_dispatcher(dispatcher: Dispatcher, stop: asyncio.Event, *, interval: float | None = None) -> None:
    """Claim until nothing is claimable, then sleep; until `stop` is set.
    Never raises: a failed tick is logged and the next one runs."""
    wait = float(interval if interval is not None else interval_seconds())
    while not stop.is_set():
        for _ in range(MAX_CLAIMS_PER_TICK):
            try:
                outcome = await dispatcher.dispatch_once()
            except Exception:
                logger.exception("execution-request dispatch tick failed")
                break
            if outcome.action in (NOTHING, CLAIM_FAILED):
                break
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except TimeoutError:
            pass
