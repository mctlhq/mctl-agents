"""Execution requests: a surface asks the platform to run a work item
(mctl-api#368, mctlhq/mctl-agents#461).

The owner rule mctl-api encodes: **a surface requests execution; a surface
does not declare execution identity.** A surface creates a `pending` request
naming only the item, the kind (`start` or `resume`) and its provenance. The
execution platform, the service principal acting directly, claims it under a
lease, starts the run, and fulfils it with `(engine, engine_ref)`; mctl-api
attaches the canonical execution (`we_...`) in the same transaction. Or the
platform rejects it with a reason.

This module is the client-side mirror of that contract, stdlib-only like the
rest of `orchestrator.work_context`: the request's shape, and the
classification of each answer into a verdict. The HTTP calls live in
`WorkItemClient` (`client.py`), each behind the policy checkpoint (#197). The
dispatch decisions live in `orchestrator.temporal.dispatcher`.

Failure is a value, never an exception, as everywhere in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The policy checkpoint's operations (#197, ADR 014). One per mutation.
CLAIM_OPERATION = "claim:execution-request"
FULFIL_OPERATION = "fulfil:execution-request"
REJECT_OPERATION = "reject:execution-request"

SCHEMA_VERSION = "workitem/v1"
REQUEST_ID_PREFIX = "xr_"

KIND_START = "start"
KIND_RESUME = "resume"
KINDS = frozenset({KIND_START, KIND_RESUME})

STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_FULFILLED = "fulfilled"
STATE_REJECTED = "rejected"
STATES = frozenset({STATE_PENDING, STATE_CLAIMED, STATE_FULFILLED, STATE_REJECTED})

#: mctl-api's `workitems.MinClaimLease` / `MaxClaimLease` (seconds).
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 900

#: mctl-api's typed codes (internal/api/handlers_execution_requests.go).
NOT_CLAIMED_CODE = "execution_request_not_claimed"
CLOSED_CODE = "execution_request_closed"

# Reject reasons this platform gives. Typed, short, and never free text a
# surface would have to parse: the surface branches on them.
#: The item names nothing this platform can run. v1 runs the investigator
#: for an item bound to a mctlhq GitHub issue; anything else is refused,
#: never guessed.
NO_RUNNABLE_TARGET = "no_runnable_target"
#: The request's kind is not one this platform knows.
UNSUPPORTED_KIND = "unsupported_kind"
#: Another DevLoop is already live for the item's issue; a second
#: investigation of the same issue would race it for the same proposal.
LOOP_ACTIVE = "loop_active"
#: A resume the live DevLoop refused to accept, or one this platform refused
#: before delivering it. One of RESUME_REFUSAL_REASONS is appended after a
#: colon, never anything else. Refused before any fulfil, so no execution was
#: minted for it.
RESUME_REFUSED = "resume_refused"
#: The closed vocabulary after `resume_refused:`. The loop's own (its
#: `accept_execution_request` validator, the `resume` signal's rules):
#: `malformed-delivery`, `work-item-mismatch`, `resume-already-pending`,
#: `surface-or-actor-missing`, `surface-or-actor-unrecognised`. The
#: dispatcher's: `engine-ref-too-long` (the engine ref `<loop>#<request id>`
#: exceeds mctl-api's limit). And `unspecified`: a refusal whose reason is
#: missing or outside this list (a loop built with a reason this build does
#: not know), normalised so a surface never sees free text.
RESUME_REFUSAL_UNSPECIFIED = "unspecified"
RESUME_REFUSAL_REASONS = frozenset(
    {
        "malformed-delivery",
        "work-item-mismatch",
        "resume-already-pending",
        "surface-or-actor-missing",
        "surface-or-actor-unrecognised",
        "engine-ref-too-long",
        RESUME_REFUSAL_UNSPECIFIED,
    }
)
#: The run this request's workflow id names already ended without the
#: request being fulfilled (it waited for fulfilment and gave up). Starting
#: it again is refused by the reuse policy, and fulfilling the request with
#: an ended run would bind the execution to nothing.
ENGINE_RUN_ENDED = "engine_run_ended"
#: mctl-api refused the fulfilment (the item moved since the request was
#: made: a stale version, a terminal item, another active execution). The
#: request stays claimed for the platform to reject; the store's code is
#: appended after a colon.
FULFIL_REFUSED = "fulfil_refused"

CLAIMED = "request-claimed"
#: 204: nothing is claimable.
NONE_CLAIMABLE = "request-none"
FULFILLED = "request-fulfilled"
REJECTED = "request-rejected"
#: 409 execution_request_not_claimed: this holder's claim lapsed or was
#: superseded. The fence worked; another claim (maybe this platform's own,
#: later) will converge.
NOT_CLAIMED = "request-not-claimed"
#: 409 execution_request_closed: fulfilled with another run, or rejected.
CLOSED = "request-closed"
#: Any other definite 4xx.
REFUSED = "request-refused"
#: The store did not answer, or answered something this mirror cannot read.
UNKNOWN = "request-unknown"
#: A read of one request.
FOUND = "request-found"


@dataclass(frozen=True)
class ExecutionRequest:
    """mctl-api's `workitems.ExecutionRequest`, as a read or claim returns it.

    The claim token is NOT a field of the request (mctl-api never serializes
    it in a read); a claim carries it beside the request, in `RequestAnswer`."""

    request_id: str
    work_item_id: str
    kind: str
    state: str
    expected_state_version: int = 0
    resumed_from_execution_id: str = ""
    surface: str = ""
    requested_by: str = ""
    execution_id: str = ""
    reason: str = ""
    claim_expires_at: str = ""

    @staticmethod
    def from_payload(data: Any) -> ExecutionRequest | None:
        if not isinstance(data, dict) or data.get("schema_version") not in (None, SCHEMA_VERSION):
            return None
        rid, wid, kind, state = data.get("id"), data.get("work_item_id"), data.get("kind"), data.get("state")
        if not isinstance(rid, str) or not rid.startswith(REQUEST_ID_PREFIX):
            return None
        if not isinstance(wid, str) or not wid:
            return None
        # An unknown kind still parses: the dispatcher rejects it with a typed
        # reason rather than leaving a request it cannot read claimed forever.
        if not isinstance(kind, str) or state not in STATES:
            return None
        version = data.get("expected_state_version", 0)
        if not isinstance(version, int) or isinstance(version, bool):
            return None
        return ExecutionRequest(
            request_id=rid,
            work_item_id=wid,
            kind=kind,
            state=str(state),
            expected_state_version=version,
            resumed_from_execution_id=_str(data.get("resumed_from_execution_id")),
            surface=_str(data.get("surface")),
            requested_by=_str(data.get("requested_by")),
            execution_id=_str(data.get("execution_id")),
            reason=_str(data.get("reason")),
            claim_expires_at=_str(data.get("claim_expires_at")),
        )


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class RequestAnswer:
    """The answer to a claim, fulfil, reject or read.

    `claim_token` is set only on a claim; it is a bearer credential for this
    one claim, so it is never logged (see `__repr__`) and never sent to the
    policy checkpoint."""

    verdict: str
    request: ExecutionRequest | None = None
    claim_token: str = ""
    execution_id: str = ""
    #: mctl-api's typed error code, when it gave one.
    code: str = ""
    reason: str = ""

    def __repr__(self) -> str:  # the token must not reach a log line by accident
        rid = self.request.request_id if self.request else ""
        return (
            f"RequestAnswer(verdict={self.verdict!r}, request={rid!r}, execution_id={self.execution_id!r}, "
            f"code={self.code!r}, reason={self.reason!r})"
        )


def _envelope_request(payload: dict[str, Any]) -> ExecutionRequest | None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    return ExecutionRequest.from_payload(payload.get("execution_request"))


def _refusal(status: int, payload: dict[str, Any]) -> RequestAnswer:
    code = _str(payload.get("code"))
    reason = f"HTTP {status} {code}: {_str(payload.get('error'))}".strip()
    if status == 409 and code == NOT_CLAIMED_CODE:
        return RequestAnswer(NOT_CLAIMED, code=code, reason=reason)
    if status == 409 and code == CLOSED_CODE:
        details = payload.get("details")
        eid = _str(details.get("execution_id")) if isinstance(details, dict) else ""
        return RequestAnswer(CLOSED, execution_id=eid, code=code, reason=reason)
    # 401/403/408/429 say nothing definite about the request itself, as in
    # `executions.answer_from_attach`.
    if 400 <= status < 500 and status not in (401, 403, 408, 429):
        return RequestAnswer(REFUSED, code=code, reason=reason)
    return RequestAnswer(UNKNOWN, code=code, reason=reason)


def answer_from_claim(status: int, payload: dict[str, Any], *, body_empty: bool = False) -> RequestAnswer:
    """204 is nothing claimable; 200 counts only with a request this mirror
    can read, in state `claimed`, and a claim token."""
    if status == 204:
        return RequestAnswer(NONE_CLAIMABLE)
    if status == 200 and not body_empty:
        request = _envelope_request(payload)
        token = payload.get("claim_token")
        if request is None or request.state != STATE_CLAIMED or not isinstance(token, str) or not token:
            return RequestAnswer(UNKNOWN, reason="HTTP 200 does not describe a claimed execution request")
        return RequestAnswer(CLAIMED, request=request, claim_token=token)
    return _refusal(status, payload)


def answer_from_fulfil(
    status: int, payload: dict[str, Any], *, request_id: str, engine: str, engine_ref: str
) -> RequestAnswer:
    """A 2xx counts only when it describes exactly this request, fulfilled,
    with an execution of exactly this engine run."""
    if status in (200, 201):
        request = _envelope_request(payload)
        ex = payload.get("execution")
        ex = ex if isinstance(ex, dict) else {}
        eid = _str(ex.get("id"))
        describes_ours = (
            request is not None
            and request.request_id == request_id
            and request.state == STATE_FULFILLED
            and request.execution_id == eid
            and eid.startswith("we_")
            and ex.get("engine") == engine
            and ex.get("engine_ref") == engine_ref
            and ex.get("work_item_id") == request.work_item_id
        )
        if not describes_ours:
            return RequestAnswer(
                UNKNOWN, reason=f"HTTP {status} does not describe {request_id} fulfilled by {engine}/{engine_ref}"
            )
        return RequestAnswer(FULFILLED, request=request, execution_id=eid)
    return _refusal(status, payload)


def answer_from_reject(status: int, payload: dict[str, Any], *, request_id: str) -> RequestAnswer:
    if status == 200:
        request = _envelope_request(payload)
        if request is None or request.request_id != request_id or request.state != STATE_REJECTED:
            return RequestAnswer(UNKNOWN, reason=f"HTTP 200 does not describe {request_id} rejected")
        return RequestAnswer(REJECTED, request=request)
    return _refusal(status, payload)


def answer_from_read(status: int, payload: dict[str, Any], *, request_id: str) -> RequestAnswer:
    """A read of one request. The request's `work_item_id` is returned as
    the store gave it, NOT checked here: a request that belongs to another
    work item is a definite mismatch the caller refuses by name, never an
    UNKNOWN it would retry."""
    if status == 200:
        request = _envelope_request(payload)
        if request is None or request.request_id != request_id:
            return RequestAnswer(UNKNOWN, reason=f"HTTP 200 does not describe {request_id}")
        return RequestAnswer(FOUND, request=request, execution_id=request.execution_id)
    return _refusal(status, payload)
