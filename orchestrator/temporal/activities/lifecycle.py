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

from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    UNKNOWN,
    Owner,
    OwnershipAnswer,
    answer_from,
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
