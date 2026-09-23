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

Both act only for a store execution (`we_...`): any other id is a local
correlation id the store has never seen (see mctlhq/mctl-agents#455), so
nothing is sent. The rollout gate (`observe` or above) is NOT enforced
here: it lives in the one caller, `context_assembly._work_context_active`.

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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypeGuard

from orchestrator.context_snapshot import MAX_WORK_CONTEXT_ID_LENGTH, canonical_json, hash_bytes

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

#: The label mctl-api puts on every work-item response; another label is a
#: breaking change this mirror does not understand (as `contract.envelope_of`).
SCHEMA_VERSION = "workitem/v1"

#: mctl-api's typed codes (internal/api/handlers_work_item_snapshots.go).
DIVERGENCE_CODE = "snapshot_divergence"
NOT_FOUND_CODE = "snapshot_not_found"


@dataclass(frozen=True)
class SnapshotAnswer:
    verdict: str
    snapshot_id: str = ""
    content_hash: str = ""
    reason: str = ""
    #: On a read: the stored document, decoded, or None when it does not
    #: decode. Compared by `persist` after a 409; never logged.
    stored_document: dict[str, Any] | None = field(default=None, compare=False, repr=False)

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
    """The `snapshot` of a `workitem/v1` answer, or {} for anything else."""
    if payload.get("schema_version") != SCHEMA_VERSION:
        return {}
    snap = payload.get("snapshot")
    if not isinstance(snap, dict) or snap.get("schema_version") != SCHEMA_VERSION:
        return {}
    return snap


def _is_snapshot_id(value: Any) -> TypeGuard[str]:
    """A store snapshot id this side can carry: `cs_`-prefixed and within
    the length `ContextSnapshot.validate()` accepts for sealed ids."""
    return (
        isinstance(value, str)
        and value.startswith(SNAPSHOT_ID_PREFIX)
        and len(value) <= MAX_WORK_CONTEXT_ID_LENGTH
    )


def answer_from_seal(status: int, payload: dict[str, Any], *, content_hash: str, execution_id: str) -> SnapshotAnswer:
    """Classify mctl-api's answer to a seal. A 2xx counts only when it
    describes exactly the bytes and execution that were sent."""
    code = payload.get("code")
    if status in (200, 201):
        snap = _snapshot_of(payload)
        sid = snap.get("id")
        describes_ours = snap.get("content_hash") == content_hash and snap.get("execution_id") == execution_id
        if not _is_snapshot_id(sid) or not describes_ours:
            return SnapshotAnswer(
                SNAPSHOT_UNKNOWN, content_hash=content_hash,
                reason=f"HTTP {status} does not describe the sealed bytes {content_hash} of {execution_id}",
            )
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
        if not _is_snapshot_id(sid) or not isinstance(digest, str) or snap.get("execution_id") != execution_id:
            return SnapshotAnswer(SNAPSHOT_UNKNOWN, reason="HTTP 200 does not describe that execution's snapshot")
        return SnapshotAnswer(
            SNAPSHOT_REPLAYED, snapshot_id=sid, content_hash=digest, stored_document=_stored_document(snap)
        )
    if status == 404 and payload.get("code") == NOT_FOUND_CODE:
        return SnapshotAnswer(SNAPSHOT_ABSENT, reason="the prior execution sealed no snapshot")
    return SnapshotAnswer(SNAPSHOT_UNKNOWN, reason=f"HTTP {status} {payload.get('code') or ''}".strip())


#: Top-level fields a retry of the same execution may legitimately change
#: without having assembled a different context: `created_at`, and the
#: snapshot's own id/hash, which cover it.
_RETRY_VOLATILE = frozenset({"created_at", "snapshot_id", "content_hash"})


def _retry_stable(document: dict[str, Any]) -> dict[str, Any]:
    """`document` without what a retry may change: the volatile top-level
    fields, and `work_context.resumed_from_snapshot_id` — a best-effort
    convenience pointer (ADR 011 §2) whose lookup can fail on one attempt
    and succeed on the next."""
    stable = {k: v for k, v in document.items() if k not in _RETRY_VOLATILE}
    wc = stable.get("work_context")
    if isinstance(wc, dict):
        stable["work_context"] = {k: v for k, v in wc.items() if k != "resumed_from_snapshot_id"}
    return stable


def differing_fields(stored: dict[str, Any], ours: dict[str, Any]) -> list[str]:
    """The top-level fields (and `work_context.<field>`) in which two
    snapshot documents differ once what a retry may change is removed.
    Empty means a retry of the same context."""
    a, b = _retry_stable(stored), _retry_stable(ours)
    out = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) == b.get(key):
            continue
        if key == "work_context" and isinstance(a.get(key), dict) and isinstance(b.get(key), dict):
            wa, wb = a[key], b[key]
            out += [f"work_context.{k}" for k in sorted(set(wa) | set(wb)) if wa.get(k) != wb.get(k)]
        else:
            out.append(key)
    return out


def _stored_document(snap: dict[str, Any]) -> dict[str, Any] | None:
    raw = snap.get("canonical_b64")
    if not isinstance(raw, str):
        return None
    try:
        doc = json.loads(base64.b64decode(raw, validate=True))
    except (binascii.Error, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


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
    # A divergence is reported only once the stored document was read and
    # really differs; a read that fails or cannot be decoded leaves it
    # unverified, which is UNKNOWN (governed by `blocks_on_unknown()`),
    # never a divergence.
    stored = client.execution_snapshot(wid, eid)
    if stored.verdict != SNAPSHOT_REPLAYED or stored.stored_document is None:
        return SnapshotAnswer(
            SNAPSHOT_UNKNOWN, content_hash=answer.content_hash,
            reason=f"409 {DIVERGENCE_CODE}, but the stored snapshot could not be verified: "
            f"{stored.verdict} {stored.reason}".strip(),
        )
    differs = differing_fields(stored.stored_document, snapshot.to_dict())
    if not differs:
        return SnapshotAnswer(
            SNAPSHOT_REPLAYED, snapshot_id=stored.snapshot_id, content_hash=stored.content_hash,
            reason="same context as the stored snapshot, assembled on another attempt",
        )
    # Most often the live inputs changed under a retry (e.g. a new issue
    # comment): the reason names what differs, so this reads as that, not
    # as corruption.
    return SnapshotAnswer(
        SNAPSHOT_DIVERGED, content_hash=answer.content_hash,
        reason=f"this execution already sealed a different context; differs in: {', '.join(differs)}",
    )
