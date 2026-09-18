"""Activities: acquire, progress and release lifecycle ownership.

**This module exists so that workflow code never performs network I/O.**

`DevLoopWorkflow` must know who owns the PR it is watching, and the answer
lives in mctl-api. A workflow that called the API directly would be
non-deterministic and would not replay — and an ownership record that cannot
survive a replay is worse than no record at all, because it would be trusted.
So the workflow schedules these activities and consumes a typed result
(ADR-010 §9).

Every activity here fails SOFT. Ownership is a coordination signal, not the
work: a workflow that died because it could not reach the ownership store
would trade a bookkeeping outage for a delivery outage. The caller receives an
`unknown` verdict and decides — and the decision is always the conservative
one, because the cron sweeper keeps any PR the loop does not positively claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from temporalio import activity

from orchestrator.lifecycle import rollout
from orchestrator.lifecycle.contract import (
    CLAIM_HELD_BY_ME,
    CLAIM_UNKNOWN,
    OWNED_BY_ME,
    UNKNOWN,
    ClaimAnswer,
    Executor,
    Owner,
    OwnershipAnswer,
    answer_from,
    claim_answer_from,
)
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

REQUEST_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class OwnershipRequest:
    """One ownership operation, flattened for the activity boundary.

    Flat scalars rather than nested dataclasses: Temporal serializes activity
    arguments into history, and a nested shape is harder to extend without
    breaking the deserialization of an execution recorded before the change.
    Every field is defaulted for the same reason.
    """

    op: str = ""  # acquire | progress | release | terminal | handoff-start
    kind: str = ""
    entity_id: str = ""
    phase: str = ""
    version: str = ""
    owner_type: str = ""
    owner_id: str = ""
    epoch: int = 0
    evidence: str = ""
    reason: str = ""
    proposal_ref: str = ""
    policy_ref: str = ""
    temporal_workflow_id: str = ""
    to_owner_type: str = ""
    to_owner_id: str = ""


@dataclass(frozen=True)
class OwnershipResult:
    """What the workflow gets back.

    `verdict` mirrors `OwnershipAnswer.verdict`, and `unknown` is its default
    — so a result recorded before a field existed, or one built from a failed
    call, is conservative rather than permissive.
    """

    verdict: str = UNKNOWN
    epoch: int = 0
    owner_type: str = ""
    owner_id: str = ""
    state: str = ""
    healthy: bool = False
    reason: str = ""
    # Whether a MUTATING call was accepted by the server, independent of what
    # the resulting record says about ownership. A body-less 2xx is neither
    # owned_by_caller nor owned-by-other, so without this the workflow's
    # acquire branch takes neither path: it never claims, never backs off, and
    # logs nothing. The contract added WROTE_NO_RECORD for this caller and the
    # result type was dropping it.
    accepted: bool = False

    # See _result_from: these are NOT derivable from `healthy`, which is their
    # conjunction, and ADR-010 §4 acts on them differently.
    dead: bool = False
    stuck: bool = False

    @property
    def owned_by_caller(self) -> bool:
        return self.verdict == OWNED_BY_ME


_PATHS = {
    "acquire": "/api/v1/lifecycle/ownership/acquire",
    "progress": "/api/v1/lifecycle/ownership/progress",
    "release": "/api/v1/lifecycle/ownership/release",
    "terminal": "/api/v1/lifecycle/ownership/terminal",
    "handoff-start": "/api/v1/lifecycle/ownership/handoff/start",
}


def _payload(req: OwnershipRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": req.kind,
        "id": req.entity_id,
        "phase": req.phase,
        "owner_type": req.owner_type,
        "owner_id": req.owner_id,
        # Unconditional, matching LifecycleClient._write, which puts
        # entity.version in the base dict and never filters it. Dropping it
        # when empty meant the release and terminal this workflow issues from
        # its finally — where head_sha is not carried — sent no `version` key
        # at all, while the sync transport sent `"version": ""`. Same axis as
        # the epoch below: answer_from normalises the RESPONSE and cannot see a
        # divergence in the REQUEST.
        "version": req.version,
    }
    for key, value in (
        ("evidence", req.evidence),
        ("reason", req.reason),
        ("proposal_ref", req.proposal_ref),
        ("policy_ref", req.policy_ref),
        ("temporal_workflow_id", req.temporal_workflow_id),
        ("to_owner_type", req.to_owner_type),
        ("to_owner_id", req.to_owner_id),
    ):
        if value:
            body[key] = value
    if req.op != "acquire":
        # NOT on acquire, matching LifecycleClient.acquire, which takes no
        # epoch at all. The epoch is a fencing precondition on a write against
        # an existing claim; an acquire asserting one is asking the server to
        # refuse unless the caller's belief about the generation still holds.
        #
        # That is exactly wrong on the path DevLoopWorkflow now recovers by:
        # an UNOWNED progress result falls through to the heartbeat acquire,
        # which re-establishes the claim — asserting the epoch the reconciler
        # already superseded turns that into a 412, no re-acquire happens, and
        # because _owned_entity_id is not cleared on UNOWNED the loop stays on
        # the claimed path, where the heartbeat acquire is the only liveness
        # write. It then stops refreshing last_seen_at for the rest of the
        # watch.
        #
        # This was the last place the two transports built the same request
        # differently. `answer_from` normalises the RESPONSE and cannot see a
        # divergence in the REQUEST, so it is the one axis the shared contract
        # does not protect, and it needs a test comparing bodies.
        #
        # Zero is SENT, not dropped. `_write`'s filter is `v not in ("", None)`,
        # so the client sends `"epoch": 0` — an explicit "I hold no generation
        # yet" — and a truthiness test here turned that into a missing key,
        # which is a different request for the server to interpret.
        body["epoch"] = req.epoch
    return body


def _result_from(answer: OwnershipAnswer) -> OwnershipResult:
    """Flatten a shared OwnershipAnswer into the activity's wire type.

    The CLASSIFICATION is not done here. It used to be, and this module's copy
    drifted from the client's twice over — an unrecognised state fell into the
    free bucket after the client had been fixed to fail closed, and only an
    exact 200 counted as success where the client accepts the 2xx range, so a
    201 acquire would have silently no-opped the whole writer half.

    Two implementations of one safety decision is how that decision becomes a
    coin flip, so there is one: contract.answer_from.
    """
    own = answer.ownership
    return OwnershipResult(
        verdict=answer.verdict,
        epoch=own.epoch if own else 0,
        owner_type=own.owner.type if own else "",
        owner_id=own.owner.id if own else "",
        state=own.state if own else "",
        healthy=own.healthy if own else False,
        # dead and stuck SEPARATELY, not collapsed into healthy. ADR-010 §4
        # gives them different consequences — `dead` licenses a takeover,
        # `stuck` licenses an escalation and ownership does NOT move — and the
        # workflow's give-up argues a takeover while its heartbeat arm argues
        # an escalation. A caller that can only see `healthy` cannot tell the
        # two apart, which is the one distinction this wire type has to carry.
        dead=own.dead if own else False,
        stuck=own.stuck if own else False,
        reason=answer.reason,
        accepted=answer.accepted,
    )


@activity.defn
async def lifecycle_ownership(req: OwnershipRequest) -> OwnershipResult:
    """Perform one ownership operation against mctl-api.

    Returns an `unknown` verdict rather than raising on any failure. A
    persistent failure must still leave the loop running, because the cron
    sweeper is the fallback owner and it only stands down for a positive claim.
    """
    path = _PATHS.get(req.op)
    if path is None:
        return OwnershipResult(verdict=UNKNOWN, reason=f"unknown op {req.op!r}")

    if not rollout.records_writes():
        # Break-glass OFF (ADR-010 §12): nothing is written and nothing is
        # read. Gated on records_writes() rather than on mode() == OFF because
        # every op this activity performs is a mutation, and rollout.py's
        # three-switch table gives each question exactly one reader.
        #
        # This belongs in ACTIVITY code and nowhere else. Activities run
        # outside the workflow sandbox, so reading the environment here is
        # legal; the same read inside @workflow.defn is not. More importantly,
        # the command sequence is identical in every mode — the workflow still
        # schedules this activity and still records its completion, and only
        # the PAYLOAD differs. Payloads are replayed from history rather than
        # recomputed, so a history recorded under observe replays byte-for-byte
        # on a worker configured off. Gating track_ownership in dev_loop.py
        # instead would REMOVE commands from history and break that replay.
        mode = rollout.mode()
        activity.logger.info("lifecycle %s skipped: %s=%s", req.op, rollout.ENV_VAR, mode)
        return OwnershipResult(
            verdict=UNKNOWN,
            accepted=False,
            reason=f"rollout mode {mode}: ownership not consulted",
        )

    if req.op == "progress" and not req.evidence:
        # The same guard LifecycleClient.progress raises on, in the form this
        # transport is allowed to take. Empty evidence is filtered out of the
        # body, so the server answers 400 with a message that dies in the
        # activity's own logs; answered here it reaches the caller as a reason,
        # and the caller's counter treats it like any other write that did not
        # land. Raising is not an option: this activity's contract is that it
        # never does.
        return OwnershipResult(
            verdict=UNKNOWN, reason="progress requires evidence: say what changed"
        )

    try:
        headers = auth_headers()
    except Exception as exc:  # noqa: BLE001 — auth_headers raises on a missing token
        # The only failure here that was silent. A missing token looks exactly
        # like an unreachable store to the caller, and an operator reading the
        # logs would find nothing at all.
        activity.logger.warning("lifecycle %s has no usable credentials: %s", req.op, exc)
        return OwnershipResult(verdict=UNKNOWN, reason=f"auth: {exc}")

    try:
        async with httpx.AsyncClient(
            base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(path, json=_payload(req), headers=headers)
    except Exception as exc:  # noqa: BLE001 — httpx raises a wide family
        activity.logger.warning("lifecycle %s unreachable: %s", req.op, exc)
        return OwnershipResult(verdict=UNKNOWN, reason=str(exc))

    raw = resp.content
    body: dict[str, Any] = {}
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:  # noqa: BLE001 — a non-JSON body is still a response
        body = {}

    # Every call this activity makes is a MUTATION, so is_read stays False:
    # a 404 here is a missing route or a wrong base path, never "no such row".
    return _result_from(
        answer_from(
            resp.status_code,
            body,
            Owner(type=req.owner_type, id=req.owner_id),
            is_read=False,
            path=path,
            # Only a genuinely empty body can be a no-content success. An HTML
            # error page from a gateway parses to {} as well, and reading that
            # as a completed write would drop the caller's claim on a write
            # that never reached the store.
            body_empty=not raw,
        )
    )


# ---------------------------------------------------------------------------
# ExecutionClaim (ADR-010 phase 2, #352).
#
# A SECOND activity next to lifecycle_ownership, not an overload of it: the
# workflow already exposes a `lifecycle_claim` query reporting the workflow's
# OWNERSHIP claim state (dev_loop.py, LifecycleClaim / `:674-693`). Reusing
# that name for the EXECUTION claim this activity handles would put two
# different concepts behind one word in the one file where telling them apart
# matters, so this one is `execution_claim` / `ExecutionClaim*` throughout.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionClaimRequest:
    """One claim operation, flattened for the activity boundary.

    Same rationale as OwnershipRequest: flat scalars, every field defaulted,
    so history recorded before a field existed still deserializes.
    """

    op: str = ""  # acquire | renew | check | record | release
    kind: str = ""
    entity_id: str = ""
    phase: str = ""
    owner_epoch: int = 0
    entity_version: str = ""
    executor_type: str = ""
    executor_id: str = ""
    attempt: str = ""
    claim_id: str = ""
    lease_seconds: int = 0
    idempotency_key: str = ""
    action: str = ""
    outcome: str = ""
    proposal_ref: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ExecutionClaimResult:
    """What the workflow gets back. `unknown` is the default verdict, same
    conservative-by-default rule as OwnershipResult."""

    verdict: str = CLAIM_UNKNOWN
    claim_id: str = ""
    owner_epoch: int = 0
    entity_version: str = ""
    executor_type: str = ""
    executor_id: str = ""
    state: str = ""
    lease_until: str = ""
    reason: str = ""
    accepted: bool = False
    # Carried for the same reason `accepted` is: the field answers a question
    # the verdict cannot. CLAIM_HELD_BY_ME reaches a workflow identically
    # whether the store GRANTED the acquire or REFUSED it with a record naming
    # this attempt, and only the second obliges the caller to renew before
    # leaning on the lease. Dropping it here is how the wire type would make
    # that distinction unrepresentable — the omission `OwnershipResult.accepted`
    # already shipped once (claude P3 on `0af3b38`).
    retaken: bool = False
    # Whether a claim RECORD came back, as opposed to a bare acknowledgement.
    # A 2xx renew with no record answers `claim-held-by-me` with `claim_id`,
    # `state` and `lease_until` all empty — indistinguishable, from the fields
    # alone, from a record that arrived empty. Same argument as `retaken` one
    # field up: the wire type must not make the distinction unrepresentable
    # (claude P3 on `b362b5e`).
    has_record: bool = False

    @property
    def may_execute(self) -> bool:
        return self.verdict == CLAIM_HELD_BY_ME


_CLAIM_PATHS = {
    "acquire": "/api/v1/lifecycle/claims/acquire",
    "renew": "/api/v1/lifecycle/claims/renew",
    "check": "/api/v1/lifecycle/claims/check",
    "record": "/api/v1/lifecycle/claims/record",
    "release": "/api/v1/lifecycle/claims/release",
}


def _claim_payload(req: ExecutionClaimRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": req.kind,
        "id": req.entity_id,
        "phase": req.phase,
        "owner_epoch": req.owner_epoch,
        "entity_version": req.entity_version,
        "executor_type": req.executor_type,
        "executor_id": req.executor_id,
        "attempt": req.attempt,
    }
    for key, value in (
        ("claim_id", req.claim_id),
        ("lease_seconds", req.lease_seconds),
        ("idempotency_key", req.idempotency_key),
        ("action", req.action),
        ("outcome", req.outcome),
        ("proposal_ref", req.proposal_ref),
        ("reason", req.reason),
    ):
        if value:
            body[key] = value
    return body


def _claim_result_from(answer: ClaimAnswer) -> ExecutionClaimResult:
    """Flatten a shared ClaimAnswer into the activity's wire type.

    Classification stays in contract.claim_answer_from — this function only
    flattens what it already decided, the same discipline _result_from keeps
    for ownership.
    """
    claim = answer.claim
    return ExecutionClaimResult(
        verdict=answer.verdict,
        claim_id=claim.claim_id if claim else "",
        owner_epoch=claim.owner_epoch if claim else 0,
        entity_version=claim.entity_version if claim else "",
        executor_type=claim.executor.type if claim else "",
        executor_id=claim.executor.id if claim else "",
        state=claim.state if claim else "",
        lease_until=claim.lease_until if claim else "",
        reason=answer.reason,
        accepted=answer.accepted,
        retaken=answer.retaken,
        has_record=claim is not None,
    )


@activity.defn
async def execution_claim(req: ExecutionClaimRequest) -> ExecutionClaimResult:
    """Perform one execution-claim operation against mctl-api.

    Returns an `unknown` verdict rather than raising on any failure — a claim
    call must never fail the workflow; it declines the claim and lets the
    existing fallback owner keep the entity.
    """
    path = _CLAIM_PATHS.get(req.op)
    if path is None:
        return ExecutionClaimResult(verdict=CLAIM_UNKNOWN, reason=f"unknown op {req.op!r}")

    if not rollout.records_writes():
        # Same short-circuit lifecycle_ownership applies (lines above), and
        # for the same reason: it must live in the ACTIVITY, not in
        # dev_loop.py, or a history recorded under a different rollout mode
        # would replay a different command sequence.
        mode = rollout.mode()
        activity.logger.info("lifecycle claim %s skipped: %s=%s", req.op, rollout.ENV_VAR, mode)
        return ExecutionClaimResult(
            verdict=CLAIM_UNKNOWN,
            accepted=False,
            reason=f"rollout mode {mode}: claims not consulted",
        )

    if req.op in ("acquire", "renew", "check", "record", "release") and not req.attempt:
        return ExecutionClaimResult(verdict=CLAIM_UNKNOWN, reason="attempt id is required")

    try:
        headers = auth_headers()
    except Exception as exc:  # noqa: BLE001 — auth_headers raises on a missing token
        activity.logger.warning("lifecycle claim %s has no usable credentials: %s", req.op, exc)
        return ExecutionClaimResult(verdict=CLAIM_UNKNOWN, reason=f"auth: {exc}")

    try:
        async with httpx.AsyncClient(
            base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(path, json=_claim_payload(req), headers=headers)
    except Exception as exc:  # noqa: BLE001 — httpx raises a wide family
        activity.logger.warning("lifecycle claim %s unreachable: %s", req.op, exc)
        return ExecutionClaimResult(verdict=CLAIM_UNKNOWN, reason=str(exc))

    raw = resp.content
    body: dict[str, Any] = {}
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:  # noqa: BLE001 — a non-JSON body is still a response
        body = {}

    return _claim_result_from(
        claim_answer_from(
            resp.status_code,
            body,
            Executor(type=req.executor_type, id=req.executor_id),
            path=path,
            body_empty=not raw,
        )
    )
