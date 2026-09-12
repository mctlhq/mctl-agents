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
        reason=answer.reason,
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

    try:
        headers = auth_headers()
    except Exception as exc:  # noqa: BLE001 — auth_headers raises on a missing token
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
