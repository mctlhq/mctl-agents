"""`context_eval` — the retrieval-quality evaluator for a sealed
`ContextSnapshot` (mctlhq/mctl-agents#526, ADR 015: docs/adr/015-context-
evaluation-contract.md).

`orchestrator/context_assembly.py` counts what the pipeline DID
(`AssemblyMetrics.to_log_dict()`): candidates, drops, bytes, latency. This
module answers a different question: was the evidence the model needed
actually selected? It is deliberately independent of final model-output
scoring (mctlhq/mctl-agents#60) — a retrieval score orders and measures what
was placed in front of the model, never what the model then did with it.

Stdlib only, plus exactly two intra-repo imports, mirroring
`orchestrator/context_snapshot.py`'s own discipline: `orchestrator
.context_snapshot` (the one hash/canonicalization rule and the sealed
document shape) and `orchestrator.work_context.snapshots` (`StoreRef`, the
store's identity for a persisted snapshot). No `os.getenv`, no network, no
filesystem, no subprocess, and no clock read anywhere in this module —
`evaluate()`/`assess_evidence()` take every timestamp (`observed_at`, `now`)
as an argument. `orchestrator.context_release` (mctlhq/mctl-agents#472's
catalog, ADR 019) and `orchestrator.context_assembly` are never imported here
at all, not even under `TYPE_CHECKING`: a caller that needs the catalog's
`contentHash`/`implementationHash` for a strategy version loads it itself
(`context_release.load_version`, imported inside the function body, exactly
where `context_assembly` already imports `orchestrator.work_context` lazily)
and passes the two hashes in as plain strings; a caller that has an
`AssemblyMetrics` reads it as a duck-typed object (`assembly_counters_from`)
rather than this module importing that type.

**Non-authorization (ADR 009 sec. 5/6, ADR 014, restated here).** A
retrieval-quality record orders and measures what a model was shown. It
grants nothing, blocks nothing, and no policy, capability-eligibility or
authorization decision may ever read one. `tests/test_context_eval.py`
scans `orchestrator/`'s imports and asserts that only
`run_issue_investigator` and `run_context_eval` import this module at all.

Vocabularies (`WORK_ITEM_STATES`, `EXECUTION_PHASES`) that this module needs
for outcome linking are duplicated from `orchestrator/work_context/
contract.py` rather than imported, the same deliberate-duplication
discipline `context_snapshot.py:84-94` documents for its own
`WORK_CONTEXT_SURFACE_KINDS`/`WORK_CONTEXT_ACTOR_KINDS`: importing
`work_context.contract` would pull this module toward the mctl-api client
mirror, and a divergence between the two copies is a one-line fix in
whichever module is wrong — a test pins them equal.
"""
from __future__ import annotations

import inspect
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orchestrator.context_snapshot import (
    ContextSnapshot,
    ContextSource,
    canonical_json,
    hash_bytes,
    recompute_content_hash,
)
from orchestrator.work_context.snapshots import StoreRef

# ---------------------------------------------------------------------------
# Identity: this record's own name.
# ---------------------------------------------------------------------------

RECORD_KIND = "context-eval"
EVALUATOR_NAME = "issue-investigator-context-eval"
EVALUATOR_VERSION = "1.0.0"
#: ADR 015 sec. 2's metric definitions. Bumped only when the definitions
#: change, so a metric-definition change is attributable rather than a
#: number silently compared against a new rule.
METRICS_CONTRACT_VERSION = "adr-015/1"

VERDICT_EVALUATED = "evaluated"
VERDICT_HASH_MISMATCH = "hash-mismatch"
VERDICTS = frozenset({VERDICT_EVALUATED, VERDICT_HASH_MISMATCH})

OUTCOME_SOURCE_LEDGER = "work-item-ledger"
OUTCOME_SOURCE_STATUS_YAML = "status-yaml"
OUTCOME_SOURCES = frozenset({OUTCOME_SOURCE_LEDGER, OUTCOME_SOURCE_STATUS_YAML})
OUTCOMES = frozenset({"succeeded", "failed", "abandoned", "in-progress", "unknown"})

EVIDENCE_KINDS = frozenset({"none", "fixture-baseline", "stored-replay", "live"})
FRESHNESS_STATUSES = frozenset({"fresh", "missing", "stale", "mismatched", "insufficient-observations"})

#: Duplicated from `context_assembly._SAFE_SOURCE_ID` deliberately (see the
#: module docstring): a test pins the two `.pattern` strings equal.
SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: Duplicated from `orchestrator/work_context/contract.py` (see the module
#: docstring's rationale). A test pins both frozensets equal to their owner.
WORK_ITEM_STATES = frozenset({"active", "waiting", "completed", "superseded", "archived"})
EXECUTION_PHASES = frozenset({"Pending", "Running", "Succeeded", "Failed", "Error"})

# ---------------------------------------------------------------------------
# Identity verification (ADR 015 sec. 1)
# ---------------------------------------------------------------------------


STORE_MATCH_STORED = "stored"
STORE_MATCH_RETRY_EQUIVALENT = "retry-equivalent"


@dataclass(frozen=True)
class IdentityCheck:
    """The result of verifying a snapshot's (and, when present, its store's)
    identity. `store_ok` is `None` when no `StoreRef` was supplied — the
    store identity simply does not apply, which is not the same as it
    holding. `store_match` says which hash held: `stored` (mctl-api's own
    digest) or `retry-equivalent` (the ref's `local_content_hash` — the store
    kept another attempt's bytes, which `persist` already found to differ
    only in retry-volatile fields). `mismatch_fields` names every
    disagreeing field, never the values themselves."""

    document_ok: bool
    store_ok: bool | None
    mismatch_fields: tuple[str, ...] = ()
    store_match: str | None = None

    @property
    def ok(self) -> bool:
        return self.document_ok and self.store_ok is not False


def verify_identity(snapshot: ContextSnapshot, store_ref: StoreRef | None = None) -> IdentityCheck:
    """Document identity first — `recompute_content_hash(snapshot) ==
    snapshot.content_hash` and `snapshot.snapshot_id == "cs-" +
    content_hash[7:23]` — then, when `store_ref` is supplied, the store
    identity: `hash_bytes(canonical_json(snapshot.to_dict())) ==
    store_ref.store_content_hash` (the same bytes
    `work_context.snapshots.canonical_bytes` hashes; a test pins the two
    equal for a real snapshot), or, on a cross-attempt replay, `==
    store_ref.local_content_hash`, reported as `store_match:
    retry-equivalent`. `store_ref.store_snapshot_id` is never recomputed or
    compared here — it is opaque, carried through only."""
    mismatch_fields: list[str] = []
    document_ok = True
    if recompute_content_hash(snapshot) != snapshot.content_hash:
        document_ok = False
        mismatch_fields.append("content_hash")
    expected_snapshot_id = "cs-" + snapshot.content_hash[7:23]
    if snapshot.snapshot_id != expected_snapshot_id:
        document_ok = False
        mismatch_fields.append("snapshot_id")

    store_ok: bool | None = None
    store_match: str | None = None
    if store_ref is not None:
        store_hash = hash_bytes(canonical_json(snapshot.to_dict()))
        if store_hash == store_ref.store_content_hash:
            store_ok, store_match = True, STORE_MATCH_STORED
        elif store_ref.local_content_hash and store_hash == store_ref.local_content_hash:
            store_ok, store_match = True, STORE_MATCH_RETRY_EQUIVALENT
        else:
            store_ok = False
            mismatch_fields.append("store_content_hash")

    return IdentityCheck(
        document_ok=document_ok, store_ok=store_ok, mismatch_fields=tuple(mismatch_fields), store_match=store_match
    )


def _safe_or_kind(source_id: str, by_id: Mapping[str, ContextSource]) -> str:
    """`source_id` when it is a plain token (`SAFE_SOURCE_ID`); otherwise the
    matching source's `kind` (always closed-vocabulary, hence always safe);
    otherwise the literal `"unknown"` — an id this unsafe naming a source
    this module has never seen at all (mctlhq/mctl-agents#526, ADR 015
    sec. 3)."""
    if SAFE_SOURCE_ID.match(source_id):
        return source_id
    source = by_id.get(source_id)
    return source.kind if source is not None else "unknown"


# ---------------------------------------------------------------------------
# Metrics layer (ADR 015 sec. 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseLabels:
    """Ground truth for one fixture case (mctlhq/mctl-agents#526's "Label
    storage" open question: labels live only in fixture JSON, never in the
    production path). `None` on `evaluate(labels=...)` is what makes a live
    or unlabelled record report `null` for the three ratio metrics."""

    useful_source_ids: tuple[str, ...] = ()
    noise_source_ids: tuple[str, ...] = ()
    expected_conflict_source_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "useful_source_ids": list(self.useful_source_ids),
            "noise_source_ids": list(self.noise_source_ids),
            "expected_conflict_source_ids": list(self.expected_conflict_source_ids),
        }

    @classmethod
    def from_dict(cls, data: Any) -> CaseLabels:
        mapping = data if isinstance(data, dict) else {}
        return cls(
            useful_source_ids=tuple(mapping.get("useful_source_ids", []) or []),
            noise_source_ids=tuple(mapping.get("noise_source_ids", []) or []),
            expected_conflict_source_ids=tuple(mapping.get("expected_conflict_source_ids", []) or []),
        )


@dataclass(frozen=True)
class CoverageEntry:
    """One `coverage_by_kind` row: how many candidates of `kind` were seen,
    how many were included, and how many bytes the included ones cost."""

    kind: str
    candidates: int
    included: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "candidates": self.candidates, "included": self.included, "bytes": self.bytes}


@dataclass(frozen=True)
class AssemblyCounters:
    """The pipeline counters `evaluate()` reads without importing the
    pipeline (mctlhq/mctl-agents#526's alternative 2) — a small, duck-typed
    value object a caller builds from `context_assembly.AssemblyMetrics`
    (`assembly_counters_from`, below) or from a fixture harness's own
    `PipelineOutcome.counters`. Every field is optional: `evaluate()` falls
    back to what the sealed snapshot itself carries when a field is absent,
    except `assembly_latency_ms` and `capability_calls`, which this module
    has no other way to know (`null` when absent)."""

    used_bytes: int | None = None
    candidates_total: int | None = None
    dropped_duplicate: int | None = None
    assembly_latency_ms: float | None = None
    capability_calls: int | None = None
    #: Not part of the sealed document (mctlhq/mctl-agents#526's design
    #: gap: `detect_conflicts`'s cap excludes a later comment from the
    #: conflict entirely, leaving no trace on the snapshot) — `0` unless the
    #: caller supplies it from `PipelineCounters`/`AssemblyMetrics`.
    conflict_sources_capped: int = 0


@dataclass(frozen=True)
class EvalMetrics:
    """Exactly the metrics ADR 015 sec. 2 names, computed from the sealed
    snapshot plus (optionally) the pipeline's own counters — never from
    re-derived text. `selected_precision`/`useful_recall`/`f1` are `null`
    for an unlabelled case, never `0.0` or `1.0`."""

    selected_precision: float | None
    useful_recall: float | None
    f1: float | None
    #: Safe-substituted ids of declared-useful sources this snapshot did not
    #: select (`_safe_or_kind`) — "declared useful ids that no candidate
    #: carried" (ADR 015 sec. 2), rendered as the ids themselves rather than
    #: a bare count because that is the more faithful representation of the
    #: definition, and every id here is already telemetry-safe.
    missing_expected: tuple[str, ...]
    stale_rate: float
    duplicate_rate: float
    noise_rate: float
    context_bytes: int
    context_tokens_estimate: int
    assembly_latency_ms: float | None
    capability_calls: int | None
    coverage_by_kind: tuple[CoverageEntry, ...]
    conflicts_detected: int
    conflicts_expected_detected: int
    conflict_sources_capped: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_precision": self.selected_precision,
            "useful_recall": self.useful_recall,
            "f1": self.f1,
            "missing_expected": list(self.missing_expected),
            "stale_rate": self.stale_rate,
            "duplicate_rate": self.duplicate_rate,
            "noise_rate": self.noise_rate,
            "context_bytes": self.context_bytes,
            "context_tokens_estimate": self.context_tokens_estimate,
            "assembly_latency_ms": self.assembly_latency_ms,
            "capability_calls": self.capability_calls,
            "coverage_by_kind": [c.to_dict() for c in self.coverage_by_kind],
            "conflicts_detected": self.conflicts_detected,
            "conflicts_expected_detected": self.conflicts_expected_detected,
            "conflict_sources_capped": self.conflict_sources_capped,
        }


def _compute_metrics(
    snapshot: ContextSnapshot, *, labels: CaseLabels | None, assembly: AssemblyCounters | None
) -> EvalMetrics:
    sources = snapshot.sources
    by_id = {s.source_id: s for s in sources}
    selected = [s for s in sources if s.selection.included]
    selected_ids = {s.source_id for s in selected}
    total = len(sources)

    if labels is None:
        selected_precision: float | None = None
        useful_recall: float | None = None
        f1: float | None = None
        missing_expected: tuple[str, ...] = ()
        noise_rate = 0.0
        conflicts_expected_detected = 0
    else:
        useful = set(labels.useful_source_ids)
        noise = set(labels.noise_source_ids)
        intersection = selected_ids & useful
        selected_precision = (len(intersection) / len(selected_ids)) if selected_ids else 0.0
        useful_recall = (len(intersection) / len(useful)) if useful else 0.0
        f1 = (
            2 * selected_precision * useful_recall / (selected_precision + useful_recall)
            if (selected_precision + useful_recall) > 0
            else 0.0
        )
        missing_expected = tuple(_safe_or_kind(i, by_id) for i in sorted(useful - selected_ids))
        noise_selected = selected_ids & noise
        noise_rate = (len(noise_selected) / len(selected_ids)) if selected_ids else 0.0
        expected_conflict = set(labels.expected_conflict_source_ids)
        conflict_members = {sid for c in snapshot.conflicts for sid in c.source_ids}
        conflicts_expected_detected = len(expected_conflict & conflict_members)

    # ADR 015 sec. 2: "over selected sources" — the default strategy DROPS a
    # stale candidate (excluded from `selected`), while the ranked strategy
    # DEMOTES it (stays selected, `reason_code == "stale-demoted"`). Counting
    # over all `sources` instead of `selected` would report the same rate for
    # both strategies and hide exactly the distinction this metric exists to
    # surface.
    stale_matches = sum(
        1 for s in selected if s.freshness.staleness == "stale" or s.selection.reason_code == "stale-demoted"
    )
    stale_rate = (stale_matches / len(selected)) if selected else 0.0

    dropped_duplicate = (
        assembly.dropped_duplicate
        if assembly is not None and assembly.dropped_duplicate is not None
        else sum(1 for s in sources if s.selection.reason_code == "duplicate-content")
    )
    candidates_total = (
        assembly.candidates_total if assembly is not None and assembly.candidates_total is not None else total
    )
    duplicate_rate = (dropped_duplicate / candidates_total) if candidates_total else 0.0

    used_bytes = (
        assembly.used_bytes
        if assembly is not None and assembly.used_bytes is not None
        else snapshot.budget.used_bytes
    )
    context_tokens_estimate = math.ceil(used_bytes / 4) if used_bytes else 0

    coverage: dict[str, list[int]] = {}
    for s in sources:
        row = coverage.setdefault(s.kind, [0, 0, 0])
        row[0] += 1
        if s.selection.included:
            row[1] += 1
            row[2] += s.byte_count
    coverage_by_kind = tuple(
        CoverageEntry(kind=kind, candidates=row[0], included=row[1], bytes=row[2])
        for kind, row in sorted(coverage.items())
    )

    return EvalMetrics(
        selected_precision=selected_precision,
        useful_recall=useful_recall,
        f1=f1,
        missing_expected=missing_expected,
        stale_rate=stale_rate,
        duplicate_rate=duplicate_rate,
        noise_rate=noise_rate,
        context_bytes=used_bytes,
        context_tokens_estimate=context_tokens_estimate,
        assembly_latency_ms=assembly.assembly_latency_ms if assembly is not None else None,
        capability_calls=assembly.capability_calls if assembly is not None else None,
        coverage_by_kind=coverage_by_kind,
        conflicts_detected=len(snapshot.conflicts),
        conflicts_expected_detected=conflicts_expected_detected,
        conflict_sources_capped=assembly.conflict_sources_capped if assembly is not None else 0,
    )


def assembly_counters_from(metrics: Any) -> AssemblyCounters:
    """`AssemblyCounters` built from an `AssemblyMetrics`-shaped object
    (`orchestrator.context_assembly.AssemblyMetrics`) by attribute name only
    — this module never imports that type, even under `TYPE_CHECKING`, so
    `metrics` is duck-typed (`Any`). The one call site is
    `run_issue_investigator._emit_context_eval`, which already holds a real
    `AssemblyMetrics`."""
    return AssemblyCounters(
        used_bytes=getattr(metrics, "used_bytes", None),
        candidates_total=getattr(metrics, "candidates_total", None),
        dropped_duplicate=getattr(metrics, "dropped_duplicate", None),
        assembly_latency_ms=getattr(metrics, "assembly_latency_ms", None),
        capability_calls=getattr(metrics, "collector_calls", None),
        conflict_sources_capped=getattr(metrics, "conflict_sources_capped", 0) or 0,
    )


# ---------------------------------------------------------------------------
# Evidence identity, outcome linking, and the record itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceIdentity:
    """Names the strategy, the strategy version, the ranker (when any), the
    strategy version's catalog identity (#472's `contentHash`/
    `implementationHash`, supplied by the caller — this module never reads
    the catalog), and the evaluator's own identity. `pipeline_source_hash` is
    a diagnostic only (mctlhq/mctl-agents#526's alternative 5): it explains
    which function moved, but never gates an assessment."""

    strategy_name: str
    strategy_version: str
    ranker_name: str | None
    ranker_version: str | None
    strategy_content_hash: str
    strategy_implementation_hash: str
    evaluator_name: str
    evaluator_version: str
    metrics_contract_version: str
    pipeline_source_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "ranker_name": self.ranker_name,
            "ranker_version": self.ranker_version,
            "strategy_content_hash": self.strategy_content_hash,
            "strategy_implementation_hash": self.strategy_implementation_hash,
            "evaluator_name": self.evaluator_name,
            "evaluator_version": self.evaluator_version,
            "metrics_contract_version": self.metrics_contract_version,
            "pipeline_source_hash": self.pipeline_source_hash,
        }

    @classmethod
    def from_dict(cls, data: Any) -> EvidenceIdentity:
        mapping = data if isinstance(data, dict) else {}
        return cls(
            strategy_name=str(mapping.get("strategy_name", "")),
            strategy_version=str(mapping.get("strategy_version", "")),
            ranker_name=mapping.get("ranker_name"),
            ranker_version=mapping.get("ranker_version"),
            strategy_content_hash=str(mapping.get("strategy_content_hash", "")),
            strategy_implementation_hash=str(mapping.get("strategy_implementation_hash", "")),
            evaluator_name=str(mapping.get("evaluator_name", "")),
            evaluator_version=str(mapping.get("evaluator_version", "")),
            metrics_contract_version=str(mapping.get("metrics_contract_version", "")),
            pipeline_source_hash=str(mapping.get("pipeline_source_hash", "") or ""),
        )


@dataclass(frozen=True)
class OutcomeLink:
    """The result of `link_outcome`: a value from `OUTCOMES`, which of the
    two sources produced it, and (when known) the raw ledger fields it was
    derived from — never an absent key; an unrecognised value is
    `"unknown"` plus a machine-readable `reason_code`."""

    outcome: str
    outcome_source: str
    work_item_state: str
    execution_phase: str
    reason_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "outcome_source": self.outcome_source,
            "work_item_state": self.work_item_state,
            "execution_phase": self.execution_phase,
            "reason_code": self.reason_code,
        }


#: mctl-api's `ExecutionRef.phase` (`work_context/contract.py:191`) mapped to
#: this module's outcome vocabulary. `Pending`/`Running` are provisional.
_PHASE_TO_OUTCOME: Mapping[str, str] = {
    "Succeeded": "succeeded",
    "Failed": "failed",
    "Error": "failed",
    "Pending": "in-progress",
    "Running": "in-progress",
}

#: `WorkItem.state` overrides the phase-derived answer only for the two
#: terminal-but-not-completed states; `completed` keeps the phase's answer,
#: and `active`/`waiting` are always still in progress regardless of phase.
_STATE_OVERRIDE: Mapping[str, str] = {
    "superseded": "abandoned",
    "archived": "abandoned",
    "active": "in-progress",
    "waiting": "in-progress",
}

#: The published proposal's `.status.yaml` `status` field, reachable only
#: when no store execution exists (ADR 015 sec. 5).
_STATUS_YAML_TO_OUTCOME: Mapping[str, str] = {
    "merged": "succeeded",
    "rejected": "failed",
    "needs-triage": "failed",
    "proposed": "in-progress",
    "accepted": "in-progress",
    "implementing": "in-progress",
    "review-fixing": "in-progress",
}


def link_outcome(
    *,
    work_item_state: str | None = None,
    execution_phase: str | None = None,
    status_yaml_status: str | None = None,
    store_execution: bool = True,
) -> OutcomeLink:
    """Pure mapping, no I/O (ADR 015 sec. 5). WHEN a store execution exists,
    resolve from the canonical ledger (`work_item_state` + `execution_phase`);
    otherwise fall back to `status_yaml_status`, recording the weaker link.
    Every branch that cannot resolve names a reason code rather than
    guessing; `outcome` is a member of `OUTCOMES` in every case, including
    `"unknown"`."""
    if store_execution:
        if work_item_state is None or execution_phase is None:
            return OutcomeLink(
                outcome="unknown", outcome_source=OUTCOME_SOURCE_LEDGER,
                work_item_state=work_item_state or "", execution_phase=execution_phase or "",
                reason_code="missing-ledger-fields",
            )
        if execution_phase not in EXECUTION_PHASES:
            return OutcomeLink(
                outcome="unknown", outcome_source=OUTCOME_SOURCE_LEDGER,
                work_item_state=work_item_state, execution_phase=execution_phase,
                reason_code="unrecognised-execution-phase",
            )
        if work_item_state not in WORK_ITEM_STATES:
            return OutcomeLink(
                outcome="unknown", outcome_source=OUTCOME_SOURCE_LEDGER,
                work_item_state=work_item_state, execution_phase=execution_phase,
                reason_code="unrecognised-work-item-state",
            )
        phase_outcome = _PHASE_TO_OUTCOME[execution_phase]
        outcome = phase_outcome if work_item_state == "completed" else _STATE_OVERRIDE.get(
            work_item_state, phase_outcome
        )
        return OutcomeLink(
            outcome=outcome, outcome_source=OUTCOME_SOURCE_LEDGER,
            work_item_state=work_item_state, execution_phase=execution_phase, reason_code="",
        )

    if status_yaml_status is None:
        return OutcomeLink(
            outcome="unknown", outcome_source=OUTCOME_SOURCE_STATUS_YAML,
            work_item_state="", execution_phase="", reason_code="missing-status-yaml",
        )
    mapped = _STATUS_YAML_TO_OUTCOME.get(status_yaml_status)
    if mapped is None:
        return OutcomeLink(
            outcome="unknown", outcome_source=OUTCOME_SOURCE_STATUS_YAML,
            work_item_state="", execution_phase="",
            reason_code="unrecognised-status-yaml-status",
        )
    return OutcomeLink(
        outcome=mapped, outcome_source=OUTCOME_SOURCE_STATUS_YAML,
        work_item_state="", execution_phase="", reason_code="",
    )


@dataclass(frozen=True)
class EvalRecord:
    """One evaluation, exactly the shape ADR 015 fixes: `to_log_dict()` is
    the only printing path, so a locator, a selector, a payload byte or
    rendered text can never reach a log line through this type — every
    field here is already an id, a kind, a closed-vocabulary code, a count
    or a ratio."""

    record_kind: str
    evaluator_name: str
    evaluator_version: str
    verdict: str
    identity: EvidenceIdentity
    evidence_kind: str
    context_snapshot_id: str
    content_hash: str
    store_ref: StoreRef | None
    metrics: EvalMetrics | None
    outcome: OutcomeLink | None
    observed_at: str
    mismatch_fields: tuple[str, ...] = ()

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "record_kind": self.record_kind,
            "evaluator_name": self.evaluator_name,
            "evaluator_version": self.evaluator_version,
            "verdict": self.verdict,
            "identity": self.identity.to_dict(),
            "evidence_kind": self.evidence_kind,
            "context_snapshot_id": self.context_snapshot_id,
            "content_hash": self.content_hash,
            "store_ref": self.store_ref.to_dict() if self.store_ref is not None else None,
            "metrics": self.metrics.to_dict() if self.metrics is not None else None,
            "outcome": self.outcome.to_dict() if self.outcome is not None else None,
            "observed_at": self.observed_at,
            "mismatch_fields": list(self.mismatch_fields),
        }

    # `to_dict()` is the same shape as `to_log_dict()` — nothing here is ever
    # NOT safe to log, so there is no second, richer representation.
    to_dict = to_log_dict


def evaluate(
    snapshot: ContextSnapshot,
    *,
    observed_at: str,
    evidence_kind: str = "live",
    labels: CaseLabels | None = None,
    assembly: AssemblyCounters | None = None,
    store_ref: StoreRef | None = None,
    outcome: OutcomeLink | None = None,
    strategy_content_hash: str = "",
    strategy_implementation_hash: str = "",
    pipeline_source_hash: str = "",
) -> EvalRecord:
    """Verify identity first (ADR 015 sec. 1); on a mismatch, return a
    `verdict: "hash-mismatch"` record with `metrics=None` and no metrics
    computed at all. Otherwise compute exactly ADR 015 sec. 2's metrics and
    return `verdict: "evaluated"`. `strategy_content_hash`/
    `strategy_implementation_hash` are the caller's (#472's catalog
    `contentHash`/`implementationHash`, loaded via
    `context_release.load_version` at the call site) — this function never
    reads the catalog itself."""
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(f"evidence_kind must be one of {sorted(EVIDENCE_KINDS)!r}, got {evidence_kind!r}")

    strategy = snapshot.strategy
    identity = EvidenceIdentity(
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        ranker_name=strategy.ranker_name,
        ranker_version=strategy.ranker_version,
        strategy_content_hash=strategy_content_hash,
        strategy_implementation_hash=strategy_implementation_hash,
        evaluator_name=EVALUATOR_NAME,
        evaluator_version=EVALUATOR_VERSION,
        metrics_contract_version=METRICS_CONTRACT_VERSION,
        pipeline_source_hash=pipeline_source_hash,
    )

    identity_check = verify_identity(snapshot, store_ref)
    if not identity_check.ok:
        return EvalRecord(
            record_kind=RECORD_KIND,
            evaluator_name=EVALUATOR_NAME,
            evaluator_version=EVALUATOR_VERSION,
            verdict=VERDICT_HASH_MISMATCH,
            identity=identity,
            evidence_kind=evidence_kind,
            context_snapshot_id=snapshot.snapshot_id,
            content_hash=snapshot.content_hash,
            store_ref=store_ref,
            metrics=None,
            outcome=outcome,
            observed_at=observed_at,
            mismatch_fields=identity_check.mismatch_fields,
        )

    metrics = _compute_metrics(snapshot, labels=labels, assembly=assembly)
    return EvalRecord(
        record_kind=RECORD_KIND,
        evaluator_name=EVALUATOR_NAME,
        evaluator_version=EVALUATOR_VERSION,
        verdict=VERDICT_EVALUATED,
        identity=identity,
        evidence_kind=evidence_kind,
        context_snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        store_ref=store_ref,
        metrics=metrics,
        outcome=outcome,
        observed_at=observed_at,
        mismatch_fields=(),
    )


def pipeline_source_hash(*functions: Callable[..., Any]) -> str:
    """sha256 (via `context_snapshot.hash_bytes`) over the concatenated
    `inspect.getsource` of `functions`, in the order given — the same
    `inspect.getsource`-as-identity-input precedent
    `run_issue_investigator.py:1998` already uses for `prompt_template`. A
    diagnostic only (mctlhq/mctl-agents#526's alternative 5): it explains
    which function moved, but the enforced implementation identity is #472's
    catalog `implementationHash`."""
    joined = "\n".join(inspect.getsource(fn) for fn in functions)
    return hash_bytes(joined.encode("utf-8"))


# ---------------------------------------------------------------------------
# Freshness / promotion-readiness semantics (mctlhq/mctl-agents#472's seam)
# ---------------------------------------------------------------------------

#: ADR 019's v1 promotion policy constants (mctlhq/mctl-agents#472 Slice A).
#: Changed only by amending ADR 019 — this module reads no environment
#: variable for either value, and never infers one from observation history.
ADR019_V1_FRESHNESS_WINDOW_SECONDS = 604_800
ADR019_V1_MIN_CONSECUTIVE_OBSERVATIONS = 3


@dataclass(frozen=True)
class FreshnessPolicy:
    """The freshness window and minimum consecutive-observation count, as
    explicit caller-supplied parameters — no defaults, no `from_env()`. The
    caller (#472 Slice C, mctlhq/mctl-agents#528) passes ADR 019's constants
    (or a future amendment's) explicitly."""

    window_seconds: int
    min_consecutive_observations: int

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.min_consecutive_observations <= 0:
            raise ValueError("min_consecutive_observations must be positive")


@dataclass(frozen=True)
class EvidenceAssessment:
    """`assess_evidence`'s answer: a status from `FRESHNESS_STATUSES` and a
    machine-readable reason code. A status only — deciding that `fresh`
    permits a production promotion is #528's, reading ADR 019."""

    status: str
    reason_code: str
    evidence_kind: str
    observations: int
    newest_age_seconds: int | None


def _parse_observed_at(value: str) -> datetime | None:
    """Best-effort ISO-8601 parse of a record's `observed_at`. Returns `None`
    — never raises — for a malformed string (`datetime.fromisoformat` would
    raise `ValueError`) or a timezone-naive one (subtracting it from the
    caller's tz-aware `now` would raise `TypeError`): `assess_evidence` has
    no way to know a naive timestamp's offset, so it cannot be trusted for a
    freshness comparison either way."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _declared_identity_matches(record: EvalRecord, expected: EvidenceIdentity) -> bool:
    identity = record.identity
    return (
        identity.strategy_name == expected.strategy_name
        and identity.strategy_version == expected.strategy_version
        and identity.ranker_name == expected.ranker_name
        and identity.ranker_version == expected.ranker_version
    )


def _catalog_identity_matches(record: EvalRecord, expected: EvidenceIdentity) -> bool:
    identity = record.identity
    return (
        identity.strategy_content_hash == expected.strategy_content_hash
        and identity.strategy_implementation_hash == expected.strategy_implementation_hash
    )


def _observation_key(record: EvalRecord) -> tuple[str, str] | None:
    """One promotion observation per store execution, or `None` for a record
    with no store backing: nothing identifies its execution across retries
    (a retry restamps its snapshot id, `content_hash` and `observed_at`), so
    it cannot be told apart from another attempt and is never counted."""
    if record.store_ref is not None and record.store_ref.execution_id:
        return (record.store_ref.work_item_id, record.store_ref.execution_id)
    return None


def assess_evidence(
    records: Sequence[EvalRecord], *, expected: EvidenceIdentity, now: datetime, policy: FreshnessPolicy
) -> EvidenceAssessment:
    """Fixed precedence (mctlhq/mctl-agents#526 design.md sec. 3), so
    `missing` and `mismatched` are never order-dependent: empty, or the
    newest record's `evidence_kind == "none"` -> `missing`; a declared-field
    difference -> `mismatched`; an empty or differing catalog identity
    (`strategy_content_hash`/`strategy_implementation_hash`) -> `mismatched`
    (`catalog-identity-unavailable` when empty); the newest observation older
    than `policy.window_seconds` -> `stale`; fewer than
    `policy.min_consecutive_observations` newest-first observations agreeing
    on the full identity -> `insufficient-observations`; else `fresh`.
    Observations, not records, are counted (`_observation_key`): one per
    store execution; a record with no `store_ref` is never an observation;
    the run ends at the first `evidence_kind == "none"` record. A record
    whose own `verdict != "evaluated"` (a hash-mismatch) never counts as an
    observation at all. `fresh` is unreachable for `evidence_kind == "none"`
    by construction. `now` is an argument: this function reads no clock."""
    usable = [r for r in records if r.verdict == VERDICT_EVALUATED]
    if not usable:
        fallback_kind = max(records, key=lambda r: r.observed_at).evidence_kind if records else "none"
        return EvidenceAssessment(
            status="missing", reason_code="no-evidence", evidence_kind=fallback_kind, observations=0,
            newest_age_seconds=None,
        )

    ordered = sorted(usable, key=lambda r: r.observed_at, reverse=True)
    newest = ordered[0]
    observations = len({key for r in ordered if (key := _observation_key(r)) is not None})

    if newest.evidence_kind == "none":
        return EvidenceAssessment(
            status="missing", reason_code="evidence-kind-none", evidence_kind=newest.evidence_kind,
            observations=observations, newest_age_seconds=None,
        )

    if not _declared_identity_matches(newest, expected):
        return EvidenceAssessment(
            status="mismatched", reason_code="declared-identity-mismatch", evidence_kind=newest.evidence_kind,
            observations=observations, newest_age_seconds=None,
        )

    if not expected.strategy_content_hash or not expected.strategy_implementation_hash:
        return EvidenceAssessment(
            status="mismatched", reason_code="catalog-identity-unavailable", evidence_kind=newest.evidence_kind,
            observations=observations, newest_age_seconds=None,
        )
    if not _catalog_identity_matches(newest, expected):
        return EvidenceAssessment(
            status="mismatched", reason_code="catalog-identity-mismatch", evidence_kind=newest.evidence_kind,
            observations=observations, newest_age_seconds=None,
        )

    # The freshness window is measured from the newest OBSERVATION, never from
    # a record that is not one (a `store_ref: null` record, ADR 015 sec. 7
    # step 5): otherwise one fresh unbacked record would carry three stale
    # observations inside the window.
    anchor = next((r for r in ordered if _observation_key(r) is not None), None)
    if anchor is None:
        return EvidenceAssessment(
            status="insufficient-observations", reason_code="no-store-backed-observation",
            evidence_kind=newest.evidence_kind, observations=0, newest_age_seconds=None,
        )
    newest_observed_at = _parse_observed_at(anchor.observed_at)
    if newest_observed_at is None:
        # A malformed or timezone-naive `observed_at` cannot be trusted for a
        # freshness comparison; fail closed rather than raise or silently
        # assume freshness (mctlhq/mctl-agents#526, ADR 015 sec. 7).
        return EvidenceAssessment(
            status="stale", reason_code="observed-at-unparseable", evidence_kind=newest.evidence_kind,
            observations=observations, newest_age_seconds=None,
        )
    newest_age_seconds = int((now - newest_observed_at).total_seconds())
    if newest_age_seconds > policy.window_seconds:
        return EvidenceAssessment(
            status="stale", reason_code="observation-older-than-window", evidence_kind=newest.evidence_kind,
            observations=observations, newest_age_seconds=newest_age_seconds,
        )

    # Observations are counted, not records (mctlhq/mctl-agents#526, ADR 015
    # sec. 7 step 5): one per store execution, keyed by
    # `store_ref.execution_id`, the store's own retry identity, so the
    # attempts of one execution count once. A record with no store backing
    # is not an observation at all (see `_observation_key`). An
    # `evidence_kind: none` record ends the run.
    counted: set[tuple[str, str]] = set()
    for record in ordered:
        if record.evidence_kind == "none":
            break
        if not (_declared_identity_matches(record, expected) and _catalog_identity_matches(record, expected)):
            break
        key = _observation_key(record)
        if key is not None:
            counted.add(key)
    consecutive = len(counted)
    if consecutive < policy.min_consecutive_observations:
        return EvidenceAssessment(
            status="insufficient-observations", reason_code="fewer-than-minimum-consecutive-observations",
            evidence_kind=newest.evidence_kind, observations=consecutive, newest_age_seconds=newest_age_seconds,
        )

    return EvidenceAssessment(
        status="fresh", reason_code="ok", evidence_kind=newest.evidence_kind, observations=consecutive,
        newest_age_seconds=newest_age_seconds,
    )
