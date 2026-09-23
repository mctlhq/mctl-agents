"""Persisting sealed `ContextSnapshot`s to mctl-api (mctlhq/mctl-agents#431).

mctl-api keeps one immutable snapshot per execution (mctl-api#362): the
execution id is the retry identity, the same bytes again are a replay, and
different bytes for the same execution are refused as a divergence. This
module is the mctl-agents side of that contract, stdlib-only like the rest
of `orchestrator.work_context`:

- `resumed_from` looks up the snapshot the prior execution sealed, so a
  resumed execution's `WorkContextRef.resumed_from_snapshot_id` names it
  BEFORE this execution's snapshot is sealed (the pointer is part of the
  sealed content).
- `persist` sends the sealed snapshot's canonical bytes after `seal()`.

Both act only when the rollout is at least `observe` AND the execution id
is a store execution (`we_...`). Any other id is a local correlation id
the store has never seen (see mctlhq/mctl-agents#455), so nothing is sent.

Failure is a value (`SnapshotAnswer`), never an exception. The caller
decides what an unfavourable answer costs: at `observe` it is logged, at
`enforce` and above `blocks_on_unknown()` decides, and a divergence is
always loud — the store will never hold a second version of what this
execution was given.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from orchestrator.context_snapshot import canonical_json, hash_bytes

if TYPE_CHECKING:
    from orchestrator.context_snapshot import ContextSnapshot, WorkContextRef

EXECUTION_ID_PREFIX = "we_"
#: The policy checkpoint's operation for a seal (#197).
SEAL_SNAPSHOT_OPERATION = "seal:context-snapshot"
SNAPSHOT_ID_PREFIX = "cs_"

SNAPSHOT_SEALED = "snapshot-sealed"
SNAPSHOT_REPLAYED = "snapshot-replayed"
SNAPSHOT_DIVERGED = "snapshot-diverged"
SNAPSHOT_REFUSED = "snapshot-refused"
SNAPSHOT_ABSENT = "snapshot-absent"
SNAPSHOT_UNKNOWN = "snapshot-unknown"
#: Nothing was sent: rollout off, or not a store execution.
SNAPSHOT_SKIPPED = "snapshot-skipped"

#: mctl-api's typed codes (internal/api/handlers_work_item_snapshots.go).
DIVERGENCE_CODE = "snapshot_divergence"
NOT_FOUND_CODE = "snapshot_not_found"


@dataclass(frozen=True)
class SnapshotAnswer:
    verdict: str
    snapshot_id: str = ""
    content_hash: str = ""
    reason: str = ""
    #: On a read: the hash of the stored document without `created_at`
    #: (`timeless_hash`), if it decodes; "" otherwise.
    timeless_hash: str = ""

    @property
    def stored(self) -> bool:
        """The store holds exactly these bytes for this execution."""
        return self.verdict in (SNAPSHOT_SEALED, SNAPSHOT_REPLAYED)


def is_store_execution(execution_id: str | None) -> bool:
    return isinstance(execution_id, str) and execution_id.startswith(EXECUTION_ID_PREFIX)


def canonical_bytes(snapshot: ContextSnapshot) -> bytes:
    """The bytes the store keeps: the whole sealed document, canonical.
    It carries the snapshot's strategy name and version, so a retry from a
    newer build diverges only if it really built a different snapshot."""
    return canonical_json(snapshot.to_dict())


def seal_body(snapshot: ContextSnapshot, work_context: WorkContextRef) -> dict[str, Any]:
    raw = canonical_bytes(snapshot)
    body: dict[str, Any] = {
        "execution_sequence": work_context.execution_sequence,
        "canonical_b64": base64.b64encode(raw).decode("ascii"),
        "content_hash": hash_bytes(raw),
        "strategy": snapshot.strategy.name,
        "strategy_version": snapshot.strategy.version,
    }
    prior = _latest_store_prior(work_context)
    if prior:
        body["prior_execution_id"] = prior
    resumed = work_context.resumed_from_snapshot_id
    if isinstance(resumed, str) and resumed.startswith(SNAPSHOT_ID_PREFIX):
        body["prior_snapshot_id"] = resumed
    return body


def _latest_store_prior(work_context: WorkContextRef) -> str:
    priors = [p for p in work_context.prior_execution_ids if is_store_execution(p)]
    return priors[-1] if priors else ""


def _snapshot_of(payload: dict[str, Any]) -> dict[str, Any]:
    snap = payload.get("snapshot")
    return snap if isinstance(snap, dict) else {}


def answer_from_seal(status: int, payload: dict[str, Any], *, content_hash: str, execution_id: str) -> SnapshotAnswer:
    """Classify mctl-api's answer to a seal. A 2xx counts only when it
    describes exactly the bytes and execution that were sent."""
    code = payload.get("code")
    if status in (200, 201):
        snap = _snapshot_of(payload)
        sid = snap.get("id")
        if (
            not isinstance(sid, str)
            or not sid.startswith(SNAPSHOT_ID_PREFIX)
            or snap.get("content_hash") != content_hash
            or snap.get("execution_id") != execution_id
        ):
            return SnapshotAnswer(SNAPSHOT_UNKNOWN, reason=f"HTTP {status} does not describe the sealed bytes")
        return SnapshotAnswer(
            SNAPSHOT_SEALED if status == 201 else SNAPSHOT_REPLAYED, snapshot_id=sid, content_hash=content_hash
        )
    reason = f"HTTP {status} {code or ''}: {payload.get('error', '')}".strip()
    if status == 409 and code == DIVERGENCE_CODE:
        return SnapshotAnswer(SNAPSHOT_DIVERGED, content_hash=content_hash, reason=reason)
    if 400 <= status < 500:
        return SnapshotAnswer(SNAPSHOT_REFUSED, content_hash=content_hash, reason=reason)
    return SnapshotAnswer(SNAPSHOT_UNKNOWN, content_hash=content_hash, reason=reason)


def answer_from_read(status: int, payload: dict[str, Any], *, execution_id: str) -> SnapshotAnswer:
    """Classify a read of one execution's snapshot."""
    if status == 200:
        snap = _snapshot_of(payload)
        sid, digest = snap.get("id"), snap.get("content_hash")
        if (
            not isinstance(sid, str)
            or not sid.startswith(SNAPSHOT_ID_PREFIX)
            or not isinstance(digest, str)
            or snap.get("execution_id") != execution_id
        ):
            return SnapshotAnswer(SNAPSHOT_UNKNOWN, reason="HTTP 200 does not describe that execution's snapshot")
        return SnapshotAnswer(
            SNAPSHOT_REPLAYED, snapshot_id=sid, content_hash=digest, timeless_hash=_stored_timeless_hash(snap)
        )
    if status == 404 and payload.get("code") == NOT_FOUND_CODE:
        return SnapshotAnswer(SNAPSHOT_ABSENT, reason="the prior execution sealed no snapshot")
    return SnapshotAnswer(SNAPSHOT_UNKNOWN, reason=f"HTTP {status} {payload.get('code') or ''}".strip())


def timeless_hash(document: dict[str, Any]) -> str:
    """The hash of a snapshot document without `created_at`: what a retry
    of the same execution reproduces exactly when it assembles the same
    context. Computed from the document itself, never taken from a field
    the document declares."""
    return hash_bytes(canonical_json({k: v for k, v in document.items() if k != "created_at"}))


def _stored_timeless_hash(snap: dict[str, Any]) -> str:
    raw = snap.get("canonical_b64")
    if not isinstance(raw, str):
        return ""
    try:
        doc = json.loads(base64.b64decode(raw, validate=True))
    except (binascii.Error, ValueError):
        return ""
    return timeless_hash(doc) if isinstance(doc, dict) else ""


def resumed_from(work_context: WorkContextRef, client: Any) -> tuple[WorkContextRef, SnapshotAnswer]:
    """`work_context` with `resumed_from_snapshot_id` naming the snapshot the
    latest prior store execution sealed. Unchanged unless that snapshot is
    found; the answer says why."""
    prior = _latest_store_prior(work_context)
    if not is_store_execution(work_context.execution_id) or not prior:
        return work_context, SnapshotAnswer(SNAPSHOT_SKIPPED, reason="no prior store execution")
    answer = client.execution_snapshot(work_context.work_item_id, prior)
    if answer.verdict != SNAPSHOT_REPLAYED:
        return work_context, answer
    return replace(work_context, resumed_from_snapshot_id=answer.snapshot_id), answer


def persist(snapshot: ContextSnapshot, client: Any) -> SnapshotAnswer:
    """Seal `snapshot` in mctl-api under its execution. Skipped unless the
    snapshot carries a store execution.

    A retry of the same execution (an Argo pod retry) re-assembles the same
    context with a new `created_at`, so its bytes differ from the stored
    ones. That is not a divergence: ADR 009's content identity excludes
    `created_at`. So a 409 is checked against the stored document, and
    only a document that differs in anything but `created_at` is a real
    divergence. The stored snapshot is never replaced."""
    work_context = snapshot.work_context
    if work_context is None or not is_store_execution(work_context.execution_id):
        return SnapshotAnswer(SNAPSHOT_SKIPPED, reason="not a store execution")
    wid, eid = work_context.work_item_id, work_context.execution_id
    answer = client.seal_snapshot(wid, eid, seal_body(snapshot, work_context))
    if answer.verdict != SNAPSHOT_DIVERGED:
        return answer
    stored = client.execution_snapshot(wid, eid)
    if stored.verdict == SNAPSHOT_REPLAYED and stored.timeless_hash and (
        stored.timeless_hash == timeless_hash(snapshot.to_dict())
    ):
        return SnapshotAnswer(
            SNAPSHOT_REPLAYED, snapshot_id=stored.snapshot_id, content_hash=stored.content_hash,
            reason="same content as the stored snapshot, sealed at another time",
        )
    return answer
