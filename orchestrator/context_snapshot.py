"""`ContextSnapshot` — the versioned, hashed contract for "what did the agent
see" (mctlhq/mctl-agents#264, ADR 009:
docs/adr/009-context-snapshot-contract.md).

ADR 007 (docs/adr/007-agent-definition-execution-profile-contract.md) and
`orchestrator/resolver.py` answer "what contract ran": one immutable
`ExecutionPlan` per run, content-hashed and pinned. Neither says anything
about the other half of reproducibility — the issue body, the target-repo
tree, the log window, the incident record actually placed in front of the
model. This module is that other half: a frozen-dataclass schema, a
`sha256:`-prefixed content-hash rule, and a validator. It contains **no
retrieval, no ranking, and no I/O** — nothing here fetches a source, hashes
a live file, or calls a network. `seal()` takes already-fetched, already-
hashed inputs and produces one immutable, content-addressed document.

Stdlib only, deliberately, mirroring `orchestrator/temporal/issue_ref.py`:
so both the long-lived Temporal worker and the short-lived agent sandbox can
import this module without pulling in `claude_agent_sdk`
(`tests/test_worker_isolation.py` enforces the worker side of that line; this
module is not yet imported by production code, only by tests and fixture
generation — see ADR 009's "Follow-ups" section).

No field here is ever consumed by an authorization decision. Nothing in this
module records allow/deny/permit/grant. Capability eligibility belongs to
`ExecutionProfile` (mctlhq/mctl-agents#242); authorization belongs to policy
checkpoints (#197); evidence belongs to #199; traces belong to #195. See ADR
009 sec. 5 for the full boundary table.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

API_VERSION = "context.mctl.ai/v1alpha1"
KIND = "ContextSnapshot"

# The complete allow-list, mirroring orchestrator/manifest.py's
# SUPPORTED_API_VERSIONS: a document declaring anything else fails loudly in
# from_dict, never falls back to a default shape.
SUPPORTED_API_VERSIONS = {API_VERSION: KIND}

# Closed vocabularies (ADR 009 sec. 6). `unknown` is freshness's fail-safe
# default — see Freshness.from_dict.
FRESHNESS_VALUES = frozenset({"fresh", "aging", "stale", "unknown"})
TRUST_TIERS = frozenset({"authoritative", "corroborated", "reported", "untrusted"})
SOURCE_KINDS = frozenset({
    "github-issue",
    "github-issue-comment",
    "github-pr",
    "target-repo",
    "gitops-file",
    "proposal-dir",
    "loki-logs",
    "incident",
    "inline-template",
})
RETENTION_CLASSES = frozenset({"telemetry", "execution-record", "gitops"})

# Bounded-length rule (ADR 009 sec. 7): locator/selector are addresses and
# slice descriptors, never a place to smuggle a payload. These are sanity
# ceilings, the same spirit as resolver.py's MAX_BUDGET_USD/MAX_TIMEOUT_SECONDS.
MAX_LOCATOR_LENGTH = 2048
MAX_SELECTOR_JSON_LENGTH = 2048


class ContextSnapshotError(ValueError):
    """Fail-closed schema/validation failure. Every raise site below is
    either a structural problem (`from_dict`: wrong type, unknown key,
    unsupported `api_version`/`kind`) or a semantic one (`validate`: closed
    vocabulary violation, budget overrun, broken step chain). Non-retryable:
    callers fix the document, never catch this to fall back to a default
    shape."""


def _hash_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _reject_unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ContextSnapshotError(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _require_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextSnapshotError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _require_str(value: Any, *, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ContextSnapshotError(f"{where} must be a non-empty string")
    return value


def _require_int(value: Any, *, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContextSnapshotError(f"{where} must be an int")
    return value


def _require_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ContextSnapshotError(f"{where} must be a bool")
    return value


def _optional_str(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where=where)


def _require_sha256(value: Any, *, where: str) -> str:
    text = _require_str(value, where=where)
    if not text.startswith("sha256:"):
        raise ContextSnapshotError(f"{where} must carry the 'sha256:' prefix, got {text!r}")
    return text


# ---------------------------------------------------------------------------
# Leaf value objects (nested inside ContextSource)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Freshness:
    """How current a source was when retrieved. `staleness` is a closed
    vocabulary (`fresh|aging|stale|unknown`) whose fail-safe default is
    `unknown` — a source with no declared `max_age_seconds` never defaults to
    `fresh` (ADR 009 sec. 6)."""

    observed_at: str
    staleness: str = "unknown"
    max_age_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "staleness": self.staleness,
            "max_age_seconds": self.max_age_seconds,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Freshness:
        mapping = _require_mapping(data, where="freshness")
        _reject_unknown_keys(
            mapping, frozenset({"observed_at", "staleness", "max_age_seconds"}), where="freshness"
        )
        observed_at = _require_str(mapping.get("observed_at"), where="freshness.observed_at")
        staleness = mapping.get("staleness", "unknown")
        max_age_raw = mapping.get("max_age_seconds")
        max_age_seconds = None if max_age_raw is None else _require_int(max_age_raw, where="freshness.max_age_seconds")
        return cls(observed_at=observed_at, staleness=staleness, max_age_seconds=max_age_seconds)


@dataclass(frozen=True)
class Trust:
    """Origin-only provenance tier (`authoritative|corroborated|reported|
    untrusted`). Grants nothing — see ADR 009 sec. 5/6: raising a tier never
    widens what a caller may do with the source."""

    tier: str
    rationale_code: str

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "rationale_code": self.rationale_code}

    @classmethod
    def from_dict(cls, data: Any) -> Trust:
        mapping = _require_mapping(data, where="trust")
        _reject_unknown_keys(mapping, frozenset({"tier", "rationale_code"}), where="trust")
        tier = _require_str(mapping.get("tier"), where="trust.tier")
        rationale_code = _require_str(mapping.get("rationale_code"), where="trust.rationale_code")
        return cls(tier=tier, rationale_code=rationale_code)


@dataclass(frozen=True)
class Selection:
    """Why a candidate source was (or was not) included. `rank` is an
    ordinal an assembler can fill deterministically today; `score` stays
    optional because no ranker exists yet (ADR 009 open question)."""

    rank: int
    reason_code: str
    included: bool
    score: float | None = None
    strategy_step: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "reason_code": self.reason_code,
            "included": self.included,
            "score": self.score,
            "strategy_step": self.strategy_step,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Selection:
        mapping = _require_mapping(data, where="selection")
        _reject_unknown_keys(
            mapping,
            frozenset({"rank", "reason_code", "included", "score", "strategy_step"}),
            where="selection",
        )
        rank = _require_int(mapping.get("rank"), where="selection.rank")
        reason_code = _require_str(mapping.get("reason_code"), where="selection.reason_code")
        included = _require_bool(mapping.get("included"), where="selection.included")
        score_raw = mapping.get("score")
        if score_raw is not None and not isinstance(score_raw, (int, float)):
            raise ContextSnapshotError("selection.score must be a number or null")
        score = None if score_raw is None else float(score_raw)
        strategy_step = _optional_str(mapping.get("strategy_step"), where="selection.strategy_step")
        return cls(rank=rank, reason_code=reason_code, included=included, score=score, strategy_step=strategy_step)


@dataclass(frozen=True)
class Redaction:
    """Whether the bytes hashed into `ContextSource.content_hash` were
    redacted before hashing. `rules` records rule ids, never matched text;
    `dropped_bytes` records volume only. No redaction helper exists in this
    repository yet (ADR 009 sec. 8) — this is a contract for one, not a
    claim about current behaviour."""

    applied: bool = False
    rules: tuple[str, ...] = ()
    dropped_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rules": list(self.rules),
            "dropped_bytes": self.dropped_bytes,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Redaction:
        mapping = _require_mapping(data, where="redaction")
        _reject_unknown_keys(mapping, frozenset({"applied", "rules", "dropped_bytes"}), where="redaction")
        applied = _require_bool(mapping.get("applied", False), where="redaction.applied")
        rules_raw = mapping.get("rules", [])
        if not isinstance(rules_raw, list) or not all(isinstance(r, str) for r in rules_raw):
            raise ContextSnapshotError("redaction.rules must be a list of strings")
        dropped_bytes = _require_int(mapping.get("dropped_bytes", 0), where="redaction.dropped_bytes")
        return cls(applied=applied, rules=tuple(rules_raw), dropped_bytes=dropped_bytes)


# ---------------------------------------------------------------------------
# ContextSource — the provenance descriptor, the heart of the contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextSource:
    """One source considered for a snapshot. Never a payload field — there
    is no place to put one, and `from_dict` rejects unknown keys so a future
    caller cannot smuggle one in (ADR 009 sec. 1)."""

    source_id: str
    kind: str
    locator: str
    selector: Mapping[str, Any]
    content_hash: str
    byte_count: int
    retrieved_at: str
    freshness: Freshness
    trust: Trust
    selection: Selection
    redaction: Redaction = field(default_factory=Redaction)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "locator": self.locator,
            "selector": dict(self.selector),
            "content_hash": self.content_hash,
            "byte_count": self.byte_count,
            "retrieved_at": self.retrieved_at,
            "freshness": self.freshness.to_dict(),
            "trust": self.trust.to_dict(),
            "selection": self.selection.to_dict(),
            "redaction": self.redaction.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ContextSource:
        mapping = _require_mapping(data, where="source")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "source_id", "kind", "locator", "selector", "content_hash", "byte_count",
                "retrieved_at", "freshness", "trust", "selection", "redaction",
            }),
            where="source",
        )
        source_id = _require_str(mapping.get("source_id"), where="source.source_id")
        kind = _require_str(mapping.get("kind"), where="source.kind")
        locator = _require_str(mapping.get("locator"), where="source.locator")
        selector = _require_mapping(mapping.get("selector", {}), where="source.selector")
        content_hash = _require_str(mapping.get("content_hash"), where="source.content_hash")
        byte_count = _require_int(mapping.get("byte_count"), where="source.byte_count")
        retrieved_at = _require_str(mapping.get("retrieved_at"), where="source.retrieved_at")
        freshness = Freshness.from_dict(mapping.get("freshness"))
        trust = Trust.from_dict(mapping.get("trust"))
        selection = Selection.from_dict(mapping.get("selection"))
        redaction_raw = mapping.get("redaction")
        redaction = Redaction.from_dict(redaction_raw) if redaction_raw is not None else Redaction()
        return cls(
            source_id=source_id,
            kind=kind,
            locator=locator,
            selector=dict(selector),
            content_hash=content_hash,
            byte_count=byte_count,
            retrieved_at=retrieved_at,
            freshness=freshness,
            trust=trust,
            selection=selection,
            redaction=redaction,
        )


# ---------------------------------------------------------------------------
# Execution correlation, step chaining, strategy, budget, evidence, retention
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionCorrelation:
    """Joins a snapshot to the execution that produced it, entirely from
    fields both sides already have (ADR 009 sec. 4): `temporal_workflow_id`
    matches `issue_ref.workflow_id_for`, `argo_workflow_name` matches
    `ExecutionRecord.argo_workflow_name`, and the four version/hash pins plus
    `target_repository_sha` are copied straight off `ExecutionPlan`."""

    agent: str
    environment: str
    temporal_workflow_id: str
    target_repository_sha: str
    definition_version: str
    definition_content_hash: str
    profile_version: str
    profile_content_hash: str
    release_revision: int
    temporal_run_id: str | None = None
    argo_workflow_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "environment": self.environment,
            "temporal_workflow_id": self.temporal_workflow_id,
            "temporal_run_id": self.temporal_run_id,
            "argo_workflow_name": self.argo_workflow_name,
            "target_repository_sha": self.target_repository_sha,
            "definition_version": self.definition_version,
            "definition_content_hash": self.definition_content_hash,
            "profile_version": self.profile_version,
            "profile_content_hash": self.profile_content_hash,
            "release_revision": self.release_revision,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ExecutionCorrelation:
        mapping = _require_mapping(data, where="execution")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "agent", "environment", "temporal_workflow_id", "temporal_run_id",
                "argo_workflow_name", "target_repository_sha", "definition_version",
                "definition_content_hash", "profile_version", "profile_content_hash",
                "release_revision",
            }),
            where="execution",
        )
        return cls(
            agent=_require_str(mapping.get("agent"), where="execution.agent"),
            environment=_require_str(mapping.get("environment"), where="execution.environment"),
            temporal_workflow_id=_require_str(
                mapping.get("temporal_workflow_id"), where="execution.temporal_workflow_id"
            ),
            temporal_run_id=_optional_str(mapping.get("temporal_run_id"), where="execution.temporal_run_id"),
            argo_workflow_name=_optional_str(
                mapping.get("argo_workflow_name"), where="execution.argo_workflow_name"
            ),
            target_repository_sha=_require_str(
                mapping.get("target_repository_sha"), where="execution.target_repository_sha"
            ),
            definition_version=_require_str(mapping.get("definition_version"), where="execution.definition_version"),
            definition_content_hash=_require_sha256(
                mapping.get("definition_content_hash"), where="execution.definition_content_hash"
            ),
            profile_version=_require_str(mapping.get("profile_version"), where="execution.profile_version"),
            profile_content_hash=_require_sha256(
                mapping.get("profile_content_hash"), where="execution.profile_content_hash"
            ),
            release_revision=_require_int(mapping.get("release_revision"), where="execution.release_revision"),
        )


@dataclass(frozen=True)
class StepRef:
    """Chains a per-step snapshot to its per-execution root. `sequence` must
    be strictly increasing among siblings sharing one `parent_snapshot_id`
    (enforced by `validate_step_sequence`, not by this shape alone)."""

    parent_snapshot_id: str
    step: str
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {"parent_snapshot_id": self.parent_snapshot_id, "step": self.step, "sequence": self.sequence}

    @classmethod
    def from_dict(cls, data: Any) -> StepRef:
        mapping = _require_mapping(data, where="step")
        _reject_unknown_keys(mapping, frozenset({"parent_snapshot_id", "step", "sequence"}), where="step")
        return cls(
            parent_snapshot_id=_require_str(mapping.get("parent_snapshot_id"), where="step.parent_snapshot_id"),
            step=_require_str(mapping.get("step"), where="step.step"),
            sequence=_require_int(mapping.get("sequence"), where="step.sequence"),
        )


@dataclass(frozen=True)
class ContextStrategy:
    """Which assembler/ranker produced this snapshot. `ranker_name`/
    `ranker_version` stay optional because no ranker exists yet; a future
    one is identified here without an `apiVersion` bump (ADR 009 sec. 6)."""

    name: str
    version: str
    ranker_name: str | None = None
    ranker_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "ranker_name": self.ranker_name,
            "ranker_version": self.ranker_version,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ContextStrategy:
        mapping = _require_mapping(data, where="strategy")
        _reject_unknown_keys(
            mapping, frozenset({"name", "version", "ranker_name", "ranker_version"}), where="strategy"
        )
        return cls(
            name=_require_str(mapping.get("name"), where="strategy.name"),
            version=_require_str(mapping.get("version"), where="strategy.version"),
            ranker_name=_optional_str(mapping.get("ranker_name"), where="strategy.ranker_name"),
            ranker_version=_optional_str(mapping.get("ranker_version"), where="strategy.ranker_version"),
        )


@dataclass(frozen=True)
class ContextBudget:
    """Assembly budget in sources/bytes — deliberately NOT the model's
    context window, and deliberately not token-denominated (ADR 009 sec. 6:
    token budgets are deferred with written rationale). No field here may
    ever be named after a token or context-window concept; `from_dict`'s
    unknown-key rejection is what keeps that true for documents, and no
    field is declared here for code."""

    max_sources: int
    max_bytes: int
    max_bytes_per_source: int
    used_sources: int
    used_bytes: int
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_sources": self.max_sources,
            "max_bytes": self.max_bytes,
            "max_bytes_per_source": self.max_bytes_per_source,
            "used_sources": self.used_sources,
            "used_bytes": self.used_bytes,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ContextBudget:
        mapping = _require_mapping(data, where="budget")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "max_sources", "max_bytes", "max_bytes_per_source", "used_sources", "used_bytes", "truncated",
            }),
            where="budget",
        )
        return cls(
            max_sources=_require_int(mapping.get("max_sources"), where="budget.max_sources"),
            max_bytes=_require_int(mapping.get("max_bytes"), where="budget.max_bytes"),
            max_bytes_per_source=_require_int(mapping.get("max_bytes_per_source"), where="budget.max_bytes_per_source"),
            used_sources=_require_int(mapping.get("used_sources"), where="budget.used_sources"),
            used_bytes=_require_int(mapping.get("used_bytes"), where="budget.used_bytes"),
            truncated=_require_bool(mapping.get("truncated", False), where="budget.truncated"),
        )


@dataclass(frozen=True)
class EvidenceRef:
    """A pointer into the #199 evidence store — `{evidence_id, kind}` only.
    No payload field exists; `from_dict` rejects any other key."""

    evidence_id: str
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, "kind": self.kind}

    @classmethod
    def from_dict(cls, data: Any) -> EvidenceRef:
        mapping = _require_mapping(data, where="evidence_ref")
        _reject_unknown_keys(mapping, frozenset({"evidence_id", "kind"}), where="evidence_ref")
        return cls(
            evidence_id=_require_str(mapping.get("evidence_id"), where="evidence_ref.evidence_id"),
            kind=_require_str(mapping.get("kind"), where="evidence_ref.kind"),
        )


@dataclass(frozen=True)
class RetentionPolicy:
    """Which store honours this snapshot and for how long (ADR 009 sec. 7).
    The Python attribute is `class_` — `class` is a reserved word — but the
    wire/JSON key and the `retention.class` name used throughout the ADR and
    requirements.md is `class`."""

    class_: str
    expires_after_days: int

    def to_dict(self) -> dict[str, Any]:
        return {"class": self.class_, "expires_after_days": self.expires_after_days}

    @classmethod
    def from_dict(cls, data: Any) -> RetentionPolicy:
        mapping = _require_mapping(data, where="retention")
        _reject_unknown_keys(mapping, frozenset({"class", "expires_after_days"}), where="retention")
        return cls(
            class_=_require_str(mapping.get("class"), where="retention.class"),
            expires_after_days=_require_int(mapping.get("expires_after_days"), where="retention.expires_after_days"),
        )


# ---------------------------------------------------------------------------
# ContextSnapshot — the top-level document
# ---------------------------------------------------------------------------


_SNAPSHOT_KEYS = frozenset({
    "api_version", "kind", "snapshot_id", "content_hash", "created_at",
    "execution", "step", "strategy", "budget", "sources", "evidence_refs", "retention",
})


@dataclass(frozen=True)
class ContextSnapshot:
    """One immutable, content-addressed statement of what was placed in
    front of a model for one execution (or one step of one execution). Only
    ever produced by `seal()`; `from_dict` reconstructs an already-sealed
    document and re-validates its shape, but never recomputes the hash —
    use `recompute_content_hash` to verify one (T2/T3 in
    tests/test_context_snapshot.py)."""

    api_version: str
    kind: str
    snapshot_id: str
    content_hash: str
    created_at: str
    execution: ExecutionCorrelation
    strategy: ContextStrategy
    budget: ContextBudget
    retention: RetentionPolicy
    step: StepRef | None = None
    sources: tuple[ContextSource, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "execution": self.execution.to_dict(),
            "step": self.step.to_dict() if self.step is not None else None,
            "strategy": self.strategy.to_dict(),
            "budget": self.budget.to_dict(),
            "sources": [s.to_dict() for s in self.sources],
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
            "retention": self.retention.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ContextSnapshot:
        mapping = _require_mapping(data, where="ContextSnapshot")
        _reject_unknown_keys(mapping, _SNAPSHOT_KEYS, where="ContextSnapshot")

        api_version_raw = mapping.get("api_version")
        expected_kind = (
            SUPPORTED_API_VERSIONS.get(api_version_raw) if isinstance(api_version_raw, str) else None
        )
        if not isinstance(api_version_raw, str) or expected_kind is None:
            raise ContextSnapshotError(
                f"unsupported api_version {api_version_raw!r}, expected one of {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        api_version = api_version_raw
        kind_raw = mapping.get("kind")
        if kind_raw != expected_kind:
            raise ContextSnapshotError(
                f"kind must be {expected_kind!r} for api_version {api_version!r}, got {kind_raw!r}"
            )
        kind = kind_raw

        snapshot_id = _require_str(mapping.get("snapshot_id"), where="snapshot_id")
        content_hash = _require_sha256(mapping.get("content_hash"), where="content_hash")
        created_at = _require_str(mapping.get("created_at"), where="created_at")

        execution = ExecutionCorrelation.from_dict(mapping.get("execution"))
        step_raw = mapping.get("step")
        step = StepRef.from_dict(step_raw) if step_raw is not None else None
        strategy = ContextStrategy.from_dict(mapping.get("strategy"))
        budget = ContextBudget.from_dict(mapping.get("budget"))
        retention = RetentionPolicy.from_dict(mapping.get("retention"))

        sources_raw = mapping.get("sources", [])
        if not isinstance(sources_raw, list):
            raise ContextSnapshotError("sources must be a list")
        sources = tuple(ContextSource.from_dict(s) for s in sources_raw)

        evidence_raw = mapping.get("evidence_refs", [])
        if not isinstance(evidence_raw, list):
            raise ContextSnapshotError("evidence_refs must be a list")
        evidence_refs = tuple(EvidenceRef.from_dict(e) for e in evidence_raw)

        snapshot = cls(
            api_version=api_version,
            kind=kind,
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            created_at=created_at,
            execution=execution,
            step=step,
            strategy=strategy,
            budget=budget,
            sources=sources,
            evidence_refs=evidence_refs,
            retention=retention,
        )
        snapshot.validate()
        return snapshot

    def validate(self, *, parent: ContextSnapshot | None = None) -> None:
        """Enforce everything `from_dict`'s shape checks and `seal()`'s
        construction cannot: closed vocabularies, budget semantics, and (when
        `parent` is supplied) the step-chaining rule that a child's
        `execution` block must equal its parent's. Raises
        `ContextSnapshotError`; never silently coerces or drops a field."""
        if self.api_version != API_VERSION:
            raise ContextSnapshotError(f"api_version must be {API_VERSION!r}, got {self.api_version!r}")
        if self.kind != KIND:
            raise ContextSnapshotError(f"kind must be {KIND!r}, got {self.kind!r}")
        if not self.content_hash.startswith("sha256:"):
            raise ContextSnapshotError(f"content_hash must carry the 'sha256:' prefix, got {self.content_hash!r}")
        if self.retention.class_ not in RETENTION_CLASSES:
            raise ContextSnapshotError(
                f"retention.class {self.retention.class_!r} is not one of {sorted(RETENTION_CLASSES)!r}"
            )

        for source in self.sources:
            _check_source(source)

        budget = self.budget
        if not budget.truncated:
            if budget.used_sources > budget.max_sources:
                raise ContextSnapshotError(
                    f"budget.used_sources ({budget.used_sources}) exceeds max_sources "
                    f"({budget.max_sources}) without truncated=true"
                )
            if budget.used_bytes > budget.max_bytes:
                raise ContextSnapshotError(
                    f"budget.used_bytes ({budget.used_bytes}) exceeds max_bytes "
                    f"({budget.max_bytes}) without truncated=true"
                )

        if parent is not None:
            if self.step is None:
                raise ContextSnapshotError("a child snapshot must carry a step block to reference a parent")
            if self.step.parent_snapshot_id != parent.snapshot_id:
                raise ContextSnapshotError(
                    f"step.parent_snapshot_id {self.step.parent_snapshot_id!r} does not match "
                    f"parent.snapshot_id {parent.snapshot_id!r}"
                )
            if self.execution != parent.execution:
                raise ContextSnapshotError("a child snapshot's execution block must equal its parent's")

    def to_log_dict(self) -> dict[str, Any]:
        """Trace/telemetry-export shape (#195 owns traces): `snapshot_id`,
        `content_hash`, strategy/ranker name+version, counts and byte
        totals only. Never a `locator`, `selector`, or anything derived from
        a retrieved payload."""
        return {
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "strategy_name": self.strategy.name,
            "strategy_version": self.strategy.version,
            "ranker_name": self.strategy.ranker_name,
            "ranker_version": self.strategy.ranker_version,
            "source_count": len(self.sources),
            "included_source_count": sum(1 for s in self.sources if s.selection.included),
            "evidence_ref_count": len(self.evidence_refs),
            "max_sources": self.budget.max_sources,
            "max_bytes": self.budget.max_bytes,
            "used_sources": self.budget.used_sources,
            "used_bytes": self.budget.used_bytes,
            "truncated": self.budget.truncated,
        }


def _check_source(source: ContextSource) -> None:
    if source.kind not in SOURCE_KINDS:
        raise ContextSnapshotError(
            f"source {source.source_id!r}: kind {source.kind!r} is not one of {sorted(SOURCE_KINDS)!r}"
        )
    if source.freshness.staleness not in FRESHNESS_VALUES:
        raise ContextSnapshotError(
            f"source {source.source_id!r}: freshness.staleness {source.freshness.staleness!r} is not one "
            f"of {sorted(FRESHNESS_VALUES)!r}"
        )
    if source.trust.tier not in TRUST_TIERS:
        raise ContextSnapshotError(
            f"source {source.source_id!r}: trust.tier {source.trust.tier!r} is not one of {sorted(TRUST_TIERS)!r}"
        )
    if not source.content_hash.startswith("sha256:"):
        raise ContextSnapshotError(
            f"source {source.source_id!r}: content_hash must carry the 'sha256:' prefix, got "
            f"{source.content_hash!r}"
        )
    if len(source.locator) > MAX_LOCATOR_LENGTH:
        raise ContextSnapshotError(
            f"source {source.source_id!r}: locator exceeds {MAX_LOCATOR_LENGTH} characters"
        )
    selector_len = len(_canonical_json(dict(source.selector)))
    if selector_len > MAX_SELECTOR_JSON_LENGTH:
        raise ContextSnapshotError(
            f"source {source.source_id!r}: selector JSON exceeds {MAX_SELECTOR_JSON_LENGTH} bytes"
        )


def _content_payload(
    *,
    execution: ExecutionCorrelation,
    step: StepRef | None,
    strategy: ContextStrategy,
    budget: ContextBudget,
    sources: Sequence[ContextSource],
    evidence_refs: Sequence[EvidenceRef],
    retention: RetentionPolicy,
) -> dict[str, Any]:
    """Every field that participates in `content_hash` — everything except
    `content_hash`, `snapshot_id` and `created_at` (ADR 009 sec. 2)."""
    return {
        "api_version": API_VERSION,
        "kind": KIND,
        "execution": execution.to_dict(),
        "step": step.to_dict() if step is not None else None,
        "strategy": strategy.to_dict(),
        "budget": budget.to_dict(),
        "sources": [s.to_dict() for s in sources],
        "evidence_refs": [e.to_dict() for e in evidence_refs],
        "retention": retention.to_dict(),
    }


def seal(
    *,
    execution: ExecutionCorrelation,
    strategy: ContextStrategy,
    budget: ContextBudget,
    retention: RetentionPolicy,
    created_at: str,
    step: StepRef | None = None,
    sources: Sequence[ContextSource] = (),
    evidence_refs: Sequence[EvidenceRef] = (),
) -> ContextSnapshot:
    """The only constructor that produces a sealed `ContextSnapshot`.
    Computes `content_hash = "sha256:" + sha256(canonical JSON of every
    field except content_hash, snapshot_id, created_at)` and
    `snapshot_id = "cs-" + content_hash[7:23]`, mirroring
    `orchestrator/resolver.py`'s `_hash_bytes` convention. `created_at` is
    caller-supplied and excluded from the hash, so sealing the same inputs
    twice at different times yields the same identity (ADR 009 sec. 2).
    Raises `ContextSnapshotError` via `validate()` if the assembled document
    is not internally consistent; never returns a partially-sealed
    snapshot."""
    payload = _content_payload(
        execution=execution,
        step=step,
        strategy=strategy,
        budget=budget,
        sources=sources,
        evidence_refs=evidence_refs,
        retention=retention,
    )
    content_hash = _hash_bytes(_canonical_json(payload))
    snapshot_id = "cs-" + content_hash[7:23]
    snapshot = ContextSnapshot(
        api_version=API_VERSION,
        kind=KIND,
        snapshot_id=snapshot_id,
        content_hash=content_hash,
        created_at=created_at,
        execution=execution,
        step=step,
        strategy=strategy,
        budget=budget,
        sources=tuple(sources),
        evidence_refs=tuple(evidence_refs),
        retention=retention,
    )
    snapshot.validate()
    return snapshot


def recompute_content_hash(snapshot: ContextSnapshot) -> str:
    """Recompute the `content_hash` a fresh `seal()` of `snapshot`'s fields
    (excluding `content_hash`, `snapshot_id`, `created_at`) would produce.
    Used to verify golden fixtures (T3) and reseal-stability (T2) without
    mutating the snapshot under test."""
    payload = _content_payload(
        execution=snapshot.execution,
        step=snapshot.step,
        strategy=snapshot.strategy,
        budget=snapshot.budget,
        sources=snapshot.sources,
        evidence_refs=snapshot.evidence_refs,
        retention=snapshot.retention,
    )
    return _hash_bytes(_canonical_json(payload))


def validate_step_sequence(children: Sequence[ContextSnapshot]) -> None:
    """`sequence` must be strictly increasing across `children`, given in
    the order the caller wants to assert (ADR 009 sec. 4). Every child must
    carry a `step` block; use `ContextSnapshot.validate(parent=...)`
    separately to check each child's `execution` block against its parent."""
    previous: int | None = None
    for child in children:
        if child.step is None:
            raise ContextSnapshotError(f"snapshot {child.snapshot_id!r} has no step block to sequence")
        if previous is not None and child.step.sequence <= previous:
            raise ContextSnapshotError(
                f"snapshot {child.snapshot_id!r} has sequence {child.step.sequence}, which is not "
                f"strictly greater than the previous {previous}"
            )
        previous = child.step.sequence
