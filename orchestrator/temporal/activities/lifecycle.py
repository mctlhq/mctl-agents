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

from dataclasses import dataclass, field
from typing import Any

import httpx
from temporalio import activity

from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    STATE_ACTIVE,
    STATE_HANDING_OFF,
    UNKNOWN,
    UNOWNED,
    EntityRef,
    Owner,
    Ownership,
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
    raw: dict[str, Any] = field(default_factory=dict)

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
    }
    for key, value in (
        ("version", req.version),
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
    if req.epoch:
        body["epoch"] = req.epoch
    return body


def _result_from(payload: dict[str, Any], req: OwnershipRequest) -> OwnershipResult:
    own = Ownership.from_payload(payload)
    if own is None:
        # A 200 whose body is not an ownership record. Reading it as one would
        # produce a confident OWNED_BY_OTHER built from an empty record — a
        # wrong answer stated as firmly as a right one.
        return OwnershipResult(verdict=UNKNOWN, reason="unrecognised payload")
    asking = Owner(type=req.owner_type, id=req.owner_id)
    if own.state and own.state not in (STATE_ACTIVE, STATE_HANDING_OFF):
        # Released or terminal: the row still names an owner, but nobody holds
        # the entity. Answering OWNED_BY_OTHER here would make the next actor
        # stand down on a PR that was explicitly handed back.
        return OwnershipResult(
            verdict=UNOWNED,
            epoch=own.epoch,
            owner_type=own.owner.type,
            owner_id=own.owner.id,
            state=own.state,
            raw=payload,
        )
    verdict = OWNED_BY_ME if (own.owner == asking and own.healthy) else OWNED_BY_OTHER
    return OwnershipResult(
        verdict=verdict,
        epoch=own.epoch,
        owner_type=own.owner.type,
        owner_id=own.owner.id,
        state=own.state,
        healthy=own.healthy,
        raw=payload,
    )


@activity.defn
async def lifecycle_ownership(req: OwnershipRequest) -> OwnershipResult:
    """Perform one ownership operation against mctl-api.

    Returns an `unknown` verdict rather than raising on any failure. The
    activity is registered with a retry policy by the workflow; a persistent
    failure must still leave the loop running, because the cron sweeper is
    the fallback owner and it only stands down for a positive claim.
    """
    path = _PATHS.get(req.op)
    if path is None:
        return OwnershipResult(verdict=UNKNOWN, reason=f"unknown op {req.op!r}")

    try:
        headers = auth_headers()
    except Exception as exc:  # noqa: BLE001 — auth_headers raises on missing token
        return OwnershipResult(verdict=UNKNOWN, reason=f"auth: {exc}")

    try:
        async with httpx.AsyncClient(
            base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(path, json=_payload(req), headers=headers)
    except Exception as exc:  # noqa: BLE001 — httpx raises a wide family
        activity.logger.warning("lifecycle %s unreachable: %s", req.op, exc)
        return OwnershipResult(verdict=UNKNOWN, reason=str(exc))

    # Parse ONCE, before branching on status. A 200 carrying an HTML error page
    # from a gateway or proxy is not rarer than a malformed error body, and
    # leaving the success path unguarded made the one status that matters most
    # the only one that could crash the activity.
    body: dict[str, Any] = {}
    try:
        parsed = resp.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:  # noqa: BLE001 — a non-JSON body is still a response
        body = {}

    if resp.status_code == 200:
        if not body:
            return OwnershipResult(verdict=UNKNOWN, reason="non-JSON 200 body")
        return _result_from(body, req)

    if resp.status_code == 409:
        raw = body.get("ownership")
        own = Ownership.from_payload(raw) if isinstance(raw, dict) else None
        if own is not None:
            return OwnershipResult(
                verdict=OWNED_BY_OTHER,
                epoch=own.epoch,
                owner_type=own.owner.type,
                owner_id=own.owner.id,
                state=own.state,
                healthy=own.healthy,
                reason=str(body.get("error") or "owned by another actor"),
                raw=raw,
            )
        return OwnershipResult(verdict=OWNED_BY_OTHER, reason=str(body.get("error") or "owned"))

    if resp.status_code == 404:
        return OwnershipResult(verdict=UNOWNED, reason="no record")

    # 412, 503, 5xx, 401/403 — none of these establish ownership, and none of
    # them may read as "free". 412 in particular means the record moved
    # underneath the caller, which is the strongest reason not to act.
    return OwnershipResult(
        verdict=UNKNOWN, reason=str(body.get("error") or f"HTTP {resp.status_code}")
    )


def entity_for_pr(repo: str, number: int, head_sha: str = "") -> EntityRef:
    """Convenience mirror of `EntityRef.for_pull_request` for callers that
    already import this module."""
    return EntityRef.for_pull_request(repo, number, head_sha)
