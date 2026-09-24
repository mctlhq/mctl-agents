"""`CapabilitySet` — the versioned, hashed contract for "what could the model
actually see and call" (mctlhq/mctl-agents#242, ADR 017:
docs/adr/017-capability-discovery-and-gateway-contract.md).

ADR 007 (`docs/adr/007-agent-definition-execution-profile-contract.md`) and
`orchestrator/resolver.py` answer "what contract ran": one immutable
`ExecutionPlan` per run, carrying `tools: tuple[str, ...]` as a *permission*
filter. Nothing in the repository narrows that into an *exposure* set until
this module: every provider-advertised capability that matches
`ExecutionPlan.tools` is turned into a `CapabilityDescriptor`, and the
per-execution set of them is sealed exactly once, the same way
`orchestrator/context_snapshot.py` seals what the model saw of its context.

This module is the contract only: frozen dataclasses, a `seal()` that fills
identity/hash fields, a `validate()` that enforces the narrowing invariant
("discovery narrows, it never grants" — ADR 009 sec. 5, restated here in the
opposite direction), and the `PolicyCheckpoint` seam `#197` will eventually
implement for real. It contains **no discovery, no ranking, no provider I/O,
no gateway**: nothing here connects an MCP server, lists tools, or dispatches
an invocation. `orchestrator/capability_gateway.py` (a later slice) is the
runtime that does that.

Stdlib only, deliberately, mirroring `orchestrator/context_snapshot.py`: so
both the long-lived Temporal worker (256Mi, ADR 008) and the short-lived
agent sandbox can import this module without pulling in `claude_agent_sdk`
or the `mcp` client package (`tests/test_worker_isolation.py` enforces the
worker side of that line). `load_consequence_table` below is the one
function that reads a file (`config/capability-consequence.yaml`, YAML) — it
lazily imports `yaml` *inside the function body*, not at module scope, so
merely importing this module stays stdlib-only; only calling the loader
pulls in PyYAML (mirroring `orchestrator/policy_checkpoint.py`'s
`configured_approvals()`/`emit()`, which defer their own non-stdlib imports
the same way).

This module is additive and unwired: nothing in `orchestrator/options.py`,
`orchestrator/run_issue_investigator.py` or `orchestrator/validate_manifest.py`
imports it yet. Slice 2 (`orchestrator/capability_gateway.py`) and slice 3
(the mode flag, the catalog field, the benchmark) wire it in later.

No field here is ever consumed by an authorization decision. Nothing in this
module records allow/deny/permit/grant. Capability *eligibility* stays with
`ExecutionProfile.tools`; *authorization* stays with policy checkpoints
(`#197`, see `PolicyCheckpoint` below) and provider-side enforcement. See
ADR 017 for the full boundary table.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from orchestrator.context_snapshot import ContextSnapshotError, ExecutionCorrelation, canonical_json, hash_bytes

# Re-exported so a caller of this module never needs to import
# orchestrator.context_snapshot itself just to build the join key both
# documents share (ADR 017, mirroring ADR 009 sec. 4's "the fields both
# sides already have" join).
__all__ = [
    "API_VERSION",
    "KIND",
    "AbsentPolicyCheckpoint",
    "CapabilityDescriptor",
    "CapabilityError",
    "CapabilitySet",
    "CapabilityStrategy",
    "CheckpointVerdict",
    "DiscoveryDecision",
    "ExecutionCorrelation",
    "InvocationRecord",
    "PolicyCheckpoint",
    "ProviderRef",
    "RetentionPolicy",
    "classify_consequence",
    "load_consequence_table",
    "policy_checkpoint_status",
    "reason_code_for_verdict",
    "recompute_content_hash",
    "seal",
]

API_VERSION = "capability.mctl.ai/v1alpha1"
KIND = "CapabilitySet"
SUPPORTED_API_VERSIONS = {API_VERSION: KIND}

# Closed vocabularies (ADR 017).
PROVIDER_TYPES = frozenset({"mcp-remote", "mcp-local", "sdk-builtin"})
CONSEQUENCE_VALUES = frozenset({"read-only", "mutating", "consequential"})
#: Fail-safe default for an unclassified capability (design.md open
#: question "Consequence classification source"): never read-only, never
#: mutating, always the tier that requires a checkpoint.
DEFAULT_CONSEQUENCE = "consequential"
RETENTION_CLASSES = frozenset({"telemetry", "execution-record", "gitops"})
#: `ProviderRef.id` for the one provider `config/capability-consequence.yaml`
#: classifies (ADR 017 sec. 8): mctl-api's own advertised tool set.
#: `classify_consequence` only consults the table for this provider id; any
#: other provider_id falls straight through to `DEFAULT_CONSEQUENCE`, so the
#: table can only narrow which tools skip the checkpoint, never widen it by
#: a bare-name collision with an unrelated provider.
MCTL_API_PROVIDER_ID = "mctl-api"
#: The closed reason-code vocabulary discovery and invocation share.
#: `collision` is a sealing-time failure (two providers resolving to one
#: SDK-visible name); the rest are per-call outcomes.
REASON_CODES = frozenset({
    "ok",
    "not-eligible",
    "not-found",
    "policy-denied",
    "invalid-arguments",
    "provider-unavailable",
    "provider-error",
    "timeout",
    "collision",
})
POLICY_CHECKPOINT_STATUSES = frozenset({"absent", "allowed", "denied"})
CHECKPOINT_DECISIONS = frozenset({"allowed", "denied"})
OUTCOME_VALUES = frozenset({"ok", "refused", "error"})

# Bounded-length rules (ADR 017, same spirit as context_snapshot.py's
# MAX_LOCATOR_LENGTH): a search-index field is an address or a short label,
# never a place to smuggle a payload.
MAX_TITLE_LENGTH = 200
MAX_SUMMARY_LENGTH = 500
MAX_KEYWORDS = 32
MAX_KEYWORD_LENGTH = 64
MAX_ANNOTATIONS_JSON_LENGTH = 1024
MAX_ENDPOINT_REF_LENGTH = 2048


class CapabilityError(ValueError):
    """Fail-closed schema/validation failure. Every raise site below is
    either structural (`from_dict`: wrong type, unknown key, unsupported
    `api_version`/`kind`) or semantic (`validate`: closed vocabulary
    violation, the narrowing invariant). Non-retryable: callers fix the
    document, never catch this to fall back to a default shape."""


def _hash_bytes(raw: bytes) -> str:
    return hash_bytes(raw)


def _canonical_json(payload: Any) -> bytes:
    # canonical_json() raises ContextSnapshotError; re-wrap as CapabilityError
    # so callers of this module only ever see one exception type.
    try:
        return canonical_json(payload)
    except ContextSnapshotError as exc:
        raise CapabilityError(f"payload is not JSON-serializable: {exc}") from exc


def _reject_unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise CapabilityError(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _require_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _require_str(value: Any, *, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise CapabilityError(f"{where} must be a non-empty string")
    return value


def _require_int(value: Any, *, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CapabilityError(f"{where} must be an int")
    return value


def _require_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise CapabilityError(f"{where} must be a bool")
    return value


def _optional_str(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where=where)


def _optional_float(value: Any, *, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CapabilityError(f"{where} must be a number or null")
    return float(value)


def _execution_from_dict(data: Any) -> ExecutionCorrelation:
    """Wraps `ExecutionCorrelation.from_dict` so every exception raised
    while parsing a `CapabilitySet` document is a `CapabilityError`, never
    the `context_snapshot.ContextSnapshotError` the imported class itself
    raises — this module's callers should only ever need to catch one
    exception type."""
    try:
        return ExecutionCorrelation.from_dict(data)
    except ContextSnapshotError as exc:
        raise CapabilityError(str(exc)) from exc


def _require_sha256(value: Any, *, where: str) -> str:
    text = _require_str(value, where=where)
    if not text.startswith("sha256:"):
        raise CapabilityError(f"{where} must carry the 'sha256:' prefix, got {text!r}")
    return text


# ---------------------------------------------------------------------------
# ProviderRef
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderRef:
    """One capability provider: a remote MCP server, an in-process local
    registry, or the SDK's own built-ins. `alias` is execution-scoped and
    assigned deterministically from the profile's provider declaration
    order (a later slice's job); this module only carries the shape."""

    type: str
    id: str
    alias: str
    endpoint_ref: str = ""

    def __post_init__(self) -> None:
        if len(self.endpoint_ref) > MAX_ENDPOINT_REF_LENGTH:
            raise CapabilityError(f"provider.endpoint_ref exceeds {MAX_ENDPOINT_REF_LENGTH} characters")

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "alias": self.alias, "endpoint_ref": self.endpoint_ref}

    @classmethod
    def from_dict(cls, data: Any) -> ProviderRef:
        mapping = _require_mapping(data, where="provider")
        _reject_unknown_keys(mapping, frozenset({"type", "id", "alias", "endpoint_ref"}), where="provider")
        return cls(
            type=_require_str(mapping.get("type"), where="provider.type"),
            id=_require_str(mapping.get("id"), where="provider.id"),
            alias=_require_str(mapping.get("alias"), where="provider.alias"),
            endpoint_ref=_require_str(mapping.get("endpoint_ref", ""), where="provider.endpoint_ref", allow_empty=True),
        )


# ---------------------------------------------------------------------------
# CapabilityDescriptor — the canonical shape shared by every provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityDescriptor:
    """One capability eligible for one execution: the canonical
    `capability_id` (`mctl://<provider_type>/<provider_id>/<tool>`), the
    SDK-visible `tool_name` (`mcp__<alias>__<tool>` or a built-in name), the
    provider it came from, search-index text (`title`/`summary`/`keywords`),
    the input schema's hash and byte count (never the schema bytes
    themselves — this is a descriptor, not a payload carrier),
    `consequence` (closed vocabulary), and `matched_tool_pattern` — the
    exact `ExecutionPlan.tools` entry that made this capability eligible,
    the field `CapabilitySet.validate()`'s narrowing invariant checks."""

    capability_id: str
    tool_name: str
    provider: ProviderRef
    title: str
    summary: str
    keywords: tuple[str, ...]
    input_schema_hash: str
    input_schema_bytes: int
    consequence: str
    matched_tool_pattern: str
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "keywords", tuple(self.keywords))
        # Defensive copy + read-only wrap, mirroring ContextSource.selector
        # in context_snapshot.py: a caller mutating the dict it passed in
        # must never change this descriptor's effective content after
        # construction.
        object.__setattr__(self, "annotations", MappingProxyType(dict(self.annotations)))
        if len(self.title) > MAX_TITLE_LENGTH:
            raise CapabilityError(f"capability {self.capability_id!r}: title exceeds {MAX_TITLE_LENGTH} characters")
        if len(self.summary) > MAX_SUMMARY_LENGTH:
            raise CapabilityError(f"capability {self.capability_id!r}: summary exceeds {MAX_SUMMARY_LENGTH} characters")
        # Mirrors what CapabilityDescriptor.from_dict enforces via
        # _require_sha256 — checked here too so seal() (which constructs
        # this class directly, never through from_dict) can never produce a
        # descriptor that from_dict itself would reject on reload.
        _require_sha256(self.input_schema_hash, where=f"capability {self.capability_id!r}: input_schema_hash")
        if len(self.keywords) > MAX_KEYWORDS:
            raise CapabilityError(f"capability {self.capability_id!r}: more than {MAX_KEYWORDS} keywords")
        for kw in self.keywords:
            if not isinstance(kw, str) or len(kw) > MAX_KEYWORD_LENGTH:
                raise CapabilityError(
                    f"capability {self.capability_id!r}: a keyword exceeds {MAX_KEYWORD_LENGTH} characters"
                )
        annotations_len = len(_canonical_json(dict(self.annotations)))
        if annotations_len > MAX_ANNOTATIONS_JSON_LENGTH:
            raise CapabilityError(
                f"capability {self.capability_id!r}: annotations JSON exceeds {MAX_ANNOTATIONS_JSON_LENGTH} bytes"
            )

    def __eq__(self, other: object) -> bool:
        # Same reasoning as ContextSource.__eq__/__hash__ in context_snapshot.py:
        # comparing `annotations` via plain mapping equality would drift from
        # __hash__ hashing its canonical JSON (e.g. {"x": 1} vs {"x": 1.0}).
        if not isinstance(other, CapabilityDescriptor):
            return NotImplemented
        return (
            self.capability_id == other.capability_id
            and self.tool_name == other.tool_name
            and self.provider == other.provider
            and self.title == other.title
            and self.summary == other.summary
            and self.keywords == other.keywords
            and self.input_schema_hash == other.input_schema_hash
            and self.input_schema_bytes == other.input_schema_bytes
            and self.consequence == other.consequence
            and self.matched_tool_pattern == other.matched_tool_pattern
            and _canonical_json(dict(self.annotations)) == _canonical_json(dict(other.annotations))
        )

    def __hash__(self) -> int:
        return hash((
            self.capability_id,
            self.tool_name,
            self.provider,
            self.title,
            self.summary,
            self.keywords,
            self.input_schema_hash,
            self.input_schema_bytes,
            self.consequence,
            self.matched_tool_pattern,
            _canonical_json(dict(self.annotations)),
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "tool_name": self.tool_name,
            "provider": self.provider.to_dict(),
            "title": self.title,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "input_schema_hash": self.input_schema_hash,
            "input_schema_bytes": self.input_schema_bytes,
            "consequence": self.consequence,
            "matched_tool_pattern": self.matched_tool_pattern,
            "annotations": dict(self.annotations),
        }

    @classmethod
    def from_dict(cls, data: Any) -> CapabilityDescriptor:
        mapping = _require_mapping(data, where="capability")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "capability_id", "tool_name", "provider", "title", "summary", "keywords",
                "input_schema_hash", "input_schema_bytes", "consequence", "matched_tool_pattern",
                "annotations",
            }),
            where="capability",
        )
        keywords_raw = mapping.get("keywords", [])
        if not isinstance(keywords_raw, list) or not all(isinstance(k, str) for k in keywords_raw):
            raise CapabilityError("capability.keywords must be a list of strings")
        annotations_raw = mapping.get("annotations", {})
        return cls(
            capability_id=_require_str(mapping.get("capability_id"), where="capability.capability_id"),
            tool_name=_require_str(mapping.get("tool_name"), where="capability.tool_name"),
            provider=ProviderRef.from_dict(mapping.get("provider")),
            title=_require_str(mapping.get("title"), where="capability.title"),
            summary=_require_str(mapping.get("summary", ""), where="capability.summary", allow_empty=True),
            keywords=tuple(keywords_raw),
            input_schema_hash=_require_sha256(
                mapping.get("input_schema_hash"), where="capability.input_schema_hash"
            ),
            input_schema_bytes=_require_int(
                mapping.get("input_schema_bytes"), where="capability.input_schema_bytes"
            ),
            consequence=_require_str(mapping.get("consequence"), where="capability.consequence"),
            matched_tool_pattern=_require_str(
                mapping.get("matched_tool_pattern"), where="capability.matched_tool_pattern"
            ),
            annotations=_require_mapping(annotations_raw, where="capability.annotations"),
        )


# ---------------------------------------------------------------------------
# CapabilityStrategy, RetentionPolicy — the small owned-shape blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityStrategy:
    """Which discovery/ranking logic produced this set. `ranker_name`/
    `ranker_version` stay optional because the pilot ranker (a later slice)
    is lexical and may not exist yet at sealing time (ADR 017, mirroring
    ADR 009 sec. 6's ContextStrategy)."""

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
    def from_dict(cls, data: Any) -> CapabilityStrategy:
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
class RetentionPolicy:
    """Which store honours this sealed set and for how long — the same
    open question ADR 009 left for `ContextSnapshot`, answered the same way
    here (requirements.md "Where a sealed CapabilitySet durably lives"):
    structured logs plus this class field now, mctl-api persistence
    tracked separately. The Python attribute is `class_` — `class` is a
    reserved word — but the wire/JSON key is `class`."""

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
# CapabilitySet — the top-level sealed document
# ---------------------------------------------------------------------------


_CAPABILITY_SET_KEYS = frozenset({
    "api_version", "kind", "capability_set_id", "content_hash", "created_at",
    "execution", "plan_tools", "providers", "capabilities", "excluded_count",
    "strategy", "retention",
})


@dataclass(frozen=True)
class CapabilitySet:
    """One immutable, content-addressed statement of every capability one
    execution was eligible to discover. Only ever produced by `seal()`;
    `from_dict` reconstructs an already-sealed document and re-validates its
    shape, but never recomputes the hash — use `recompute_content_hash` to
    verify one.

    `validate()` enforces the narrowing invariant this whole contract exists
    for (ADR 017, restating ADR 009 sec. 5 in the opposite direction):
    every member's `matched_tool_pattern` must be a verbatim element of
    `plan_tools` (`ExecutionPlan.tools`), and every member's `tool_name`
    must match that pattern under `fnmatch.fnmatchcase` — the same
    case-sensitive semantics `orchestrator/policy_checkpoint.py` already
    uses for its own operation-pattern matching. A capability that fails
    either check can never have been sealed as eligible; discovery may
    record that a capability exists and rank it, but it may never widen
    `ExecutionPlan.tools`, and this check is not a policy decision — it is
    the shape-level proof that nothing did."""

    api_version: str
    kind: str
    capability_set_id: str
    content_hash: str
    created_at: str
    execution: ExecutionCorrelation
    plan_tools: tuple[str, ...]
    providers: tuple[ProviderRef, ...]
    capabilities: tuple[CapabilityDescriptor, ...]
    excluded_count: int
    strategy: CapabilityStrategy
    retention: RetentionPolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "capability_set_id": self.capability_set_id,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "execution": self.execution.to_dict(),
            "plan_tools": list(self.plan_tools),
            "providers": [p.to_dict() for p in self.providers],
            "capabilities": [c.to_dict() for c in self.capabilities],
            "excluded_count": self.excluded_count,
            "strategy": self.strategy.to_dict(),
            "retention": self.retention.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CapabilitySet:
        mapping = _require_mapping(data, where="CapabilitySet")
        _reject_unknown_keys(mapping, _CAPABILITY_SET_KEYS, where="CapabilitySet")

        api_version_raw = mapping.get("api_version")
        expected_kind = SUPPORTED_API_VERSIONS.get(api_version_raw) if isinstance(api_version_raw, str) else None
        if not isinstance(api_version_raw, str) or expected_kind is None:
            raise CapabilityError(
                f"unsupported api_version {api_version_raw!r}, expected one of {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        kind_raw = mapping.get("kind")
        if kind_raw != expected_kind:
            raise CapabilityError(
                f"kind must be {expected_kind!r} for api_version {api_version_raw!r}, got {kind_raw!r}"
            )

        capability_set_id = _require_str(mapping.get("capability_set_id"), where="capability_set_id")
        content_hash = _require_sha256(mapping.get("content_hash"), where="content_hash")
        created_at = _require_str(mapping.get("created_at"), where="created_at")
        execution = _execution_from_dict(mapping.get("execution"))

        plan_tools_raw = mapping.get("plan_tools", [])
        if not isinstance(plan_tools_raw, list) or not all(isinstance(t, str) for t in plan_tools_raw):
            raise CapabilityError("plan_tools must be a list of strings")

        providers_raw = mapping.get("providers", [])
        if not isinstance(providers_raw, list):
            raise CapabilityError("providers must be a list")
        providers = tuple(ProviderRef.from_dict(p) for p in providers_raw)

        capabilities_raw = mapping.get("capabilities", [])
        if not isinstance(capabilities_raw, list):
            raise CapabilityError("capabilities must be a list")
        capabilities = tuple(CapabilityDescriptor.from_dict(c) for c in capabilities_raw)

        excluded_count = _require_int(mapping.get("excluded_count"), where="excluded_count")
        strategy = CapabilityStrategy.from_dict(mapping.get("strategy"))
        retention = RetentionPolicy.from_dict(mapping.get("retention"))

        capability_set = cls(
            api_version=api_version_raw,
            kind=kind_raw,
            capability_set_id=capability_set_id,
            content_hash=content_hash,
            created_at=created_at,
            execution=execution,
            plan_tools=tuple(plan_tools_raw),
            providers=providers,
            capabilities=capabilities,
            excluded_count=excluded_count,
            strategy=strategy,
            retention=retention,
        )
        capability_set.validate()
        return capability_set

    def validate(self) -> None:
        """Raises `CapabilityError`; never silently coerces or drops a
        field. See the class docstring for the narrowing invariant this
        enforces."""
        if self.api_version != API_VERSION:
            raise CapabilityError(f"api_version must be {API_VERSION!r}, got {self.api_version!r}")
        if self.kind != KIND:
            raise CapabilityError(f"kind must be {KIND!r}, got {self.kind!r}")
        # Mirrors what CapabilitySet.from_dict enforces via _require_str
        # (allow_empty defaults to False) — checked here too so seal() can
        # never produce a document that its own from_dict rejects on reload.
        if not self.capability_set_id:
            raise CapabilityError("capability_set_id must be a non-empty string")
        if not self.created_at:
            raise CapabilityError("created_at must be a non-empty string")
        if not self.content_hash.startswith("sha256:"):
            raise CapabilityError(f"content_hash must carry the 'sha256:' prefix, got {self.content_hash!r}")
        if self.retention.class_ not in RETENTION_CLASSES:
            raise CapabilityError(
                f"retention.class {self.retention.class_!r} is not one of {sorted(RETENTION_CLASSES)!r}"
            )
        if self.excluded_count < 0:
            raise CapabilityError(f"excluded_count must be >= 0, got {self.excluded_count}")

        for provider in self.providers:
            if provider.type not in PROVIDER_TYPES:
                raise CapabilityError(
                    f"provider {provider.id!r}: type {provider.type!r} is not one of {sorted(PROVIDER_TYPES)!r}"
                )

        plan_tools_set = set(self.plan_tools)
        for capability in self.capabilities:
            if capability.consequence not in CONSEQUENCE_VALUES:
                raise CapabilityError(
                    f"capability {capability.capability_id!r}: consequence {capability.consequence!r} is not "
                    f"one of {sorted(CONSEQUENCE_VALUES)!r}"
                )
            if capability.provider.type not in PROVIDER_TYPES:
                raise CapabilityError(
                    f"capability {capability.capability_id!r}: provider.type {capability.provider.type!r} is "
                    f"not one of {sorted(PROVIDER_TYPES)!r}"
                )
            # The narrowing invariant (ADR 017): a capability's eligibility
            # is provable from the sealed document alone, not merely
            # asserted by whoever built it.
            if capability.matched_tool_pattern not in plan_tools_set:
                raise CapabilityError(
                    f"capability {capability.capability_id!r}: matched_tool_pattern "
                    f"{capability.matched_tool_pattern!r} is not a member of plan_tools"
                )
            if not fnmatch.fnmatchcase(capability.tool_name, capability.matched_tool_pattern):
                raise CapabilityError(
                    f"capability {capability.capability_id!r}: tool_name {capability.tool_name!r} does not "
                    f"match matched_tool_pattern {capability.matched_tool_pattern!r}"
                )

    def to_log_dict(self) -> dict[str, Any]:
        """Trace/telemetry-export shape (`#195` owns traces): ids, hashes,
        strategy name+version, and counts only. Never a `tool_name`, a
        `title`, a `summary`, or anything else that names what was
        discoverable — matching ADR 009 sec. 5's trace row and this
        module's own no-payload rule."""
        return {
            "capability_set_id": self.capability_set_id,
            "content_hash": self.content_hash,
            "strategy_name": self.strategy.name,
            "strategy_version": self.strategy.version,
            "ranker_name": self.strategy.ranker_name,
            "ranker_version": self.strategy.ranker_version,
            "provider_count": len(self.providers),
            "capability_count": len(self.capabilities),
            "excluded_count": self.excluded_count,
        }


def _content_payload(
    *,
    execution: ExecutionCorrelation,
    plan_tools: Sequence[str],
    providers: Sequence[ProviderRef],
    capabilities: Sequence[CapabilityDescriptor],
    excluded_count: int,
    strategy: CapabilityStrategy,
    retention: RetentionPolicy,
) -> dict[str, Any]:
    """Every field that participates in `content_hash` — everything except
    `content_hash`, `capability_set_id` and `created_at` (mirroring
    `orchestrator/context_snapshot.py`'s `_content_payload`)."""
    return {
        "api_version": API_VERSION,
        "kind": KIND,
        "execution": execution.to_dict(),
        "plan_tools": list(plan_tools),
        "providers": [p.to_dict() for p in providers],
        "capabilities": [c.to_dict() for c in capabilities],
        "excluded_count": excluded_count,
        "strategy": strategy.to_dict(),
        "retention": retention.to_dict(),
    }


def seal(
    *,
    execution: ExecutionCorrelation,
    plan_tools: Sequence[str],
    providers: Sequence[ProviderRef],
    capabilities: Sequence[CapabilityDescriptor],
    excluded_count: int,
    strategy: CapabilityStrategy,
    retention: RetentionPolicy,
    created_at: str,
) -> CapabilitySet:
    """The only constructor that produces a sealed `CapabilitySet`.
    Computes `content_hash = "sha256:" + sha256(canonical JSON of every
    field except content_hash, capability_set_id, created_at)` and
    `capability_set_id = "cap-" + content_hash[7:23]`, the same convention
    `orchestrator/context_snapshot.py`'s `seal()` uses for `ContextSnapshot`.
    `created_at` is caller-supplied and excluded from the hash, so sealing
    the same logical input twice at different times yields the same
    identity. Raises `CapabilityError` via `validate()` if the assembled
    document is not internally consistent (in particular, if the narrowing
    invariant does not hold); never returns a partially-sealed set."""
    payload = _content_payload(
        execution=execution,
        plan_tools=plan_tools,
        providers=providers,
        capabilities=capabilities,
        excluded_count=excluded_count,
        strategy=strategy,
        retention=retention,
    )
    content_hash = _hash_bytes(_canonical_json(payload))
    capability_set_id = "cap-" + content_hash[7:23]
    capability_set = CapabilitySet(
        api_version=API_VERSION,
        kind=KIND,
        capability_set_id=capability_set_id,
        content_hash=content_hash,
        created_at=created_at,
        execution=execution,
        plan_tools=tuple(plan_tools),
        providers=tuple(providers),
        capabilities=tuple(capabilities),
        excluded_count=excluded_count,
        strategy=strategy,
        retention=retention,
    )
    capability_set.validate()
    return capability_set


def recompute_content_hash(capability_set: CapabilitySet) -> str:
    """Recompute the `content_hash` a fresh `seal()` of `capability_set`'s
    fields (excluding `content_hash`, `capability_set_id`, `created_at`)
    would produce. Used to verify golden fixtures without mutating the set
    under test."""
    payload = _content_payload(
        execution=capability_set.execution,
        plan_tools=capability_set.plan_tools,
        providers=capability_set.providers,
        capabilities=capability_set.capabilities,
        excluded_count=capability_set.excluded_count,
        strategy=capability_set.strategy,
        retention=capability_set.retention,
    )
    return _hash_bytes(_canonical_json(payload))


# ---------------------------------------------------------------------------
# DiscoveryDecision, InvocationRecord — per-call telemetry shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryDecision:
    """One capability's place in one `capability_search` result (a later
    slice's job to produce): `rank`, an optional `score` (no ranker beyond
    a lexical pilot exists yet), a closed `reason_code`, and whether it was
    `included` in the returned rows. Never a title, summary or schema —
    those live on `CapabilityDescriptor`; this is a decision record, not a
    second copy of the descriptor."""

    capability_id: str
    rank: int
    reason_code: str
    included: bool
    score: float | None = None

    def __post_init__(self) -> None:
        # Same normalization Selection.score uses in context_snapshot.py:
        # an int score coerces to float on every construction path, and a
        # bool (an int subclass) is rejected rather than silently becoming
        # 1.0/0.0.
        object.__setattr__(self, "score", _optional_float(self.score, where="discovery_decision.score"))
        if self.reason_code not in REASON_CODES:
            raise CapabilityError(
                f"discovery_decision.reason_code {self.reason_code!r} is not one of {sorted(REASON_CODES)!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "rank": self.rank,
            "score": self.score,
            "reason_code": self.reason_code,
            "included": self.included,
        }

    @classmethod
    def from_dict(cls, data: Any) -> DiscoveryDecision:
        mapping = _require_mapping(data, where="discovery_decision")
        _reject_unknown_keys(
            mapping,
            frozenset({"capability_id", "rank", "score", "reason_code", "included"}),
            where="discovery_decision",
        )
        return cls(
            capability_id=_require_str(mapping.get("capability_id"), where="discovery_decision.capability_id"),
            rank=_require_int(mapping.get("rank"), where="discovery_decision.rank"),
            score=mapping.get("score"),
            reason_code=_require_str(mapping.get("reason_code"), where="discovery_decision.reason_code"),
            included=_require_bool(mapping.get("included"), where="discovery_decision.included"),
        )


@dataclass(frozen=True)
class InvocationRecord:
    """One `capability_invoke` attempt (a later slice's job to produce),
    described without its payload: `arguments_hash`/`result_hash` are
    `sha256:`-prefixed digests of the serialized bytes, never the bytes
    themselves — the same rule `orchestrator/policy_checkpoint.py`'s
    `args_digest_of` already applies to policy decisions. `from_dict`
    rejects any key outside this shape, so no argument, result or free-text
    payload field can ever be smuggled onto a record of this kind.

    `policy_checkpoint` records whether a real `#197` checkpoint ran
    (`allowed`/`denied`) or none did (`absent` — see `AbsentPolicyCheckpoint`
    below). It is never itself an authorization claim; `outcome` and
    `reason_code` are what actually happened."""

    capability_id: str
    capability_set_id: str
    outcome: str
    reason_code: str
    duration_ms: int
    policy_checkpoint: str
    arguments_hash: str
    result_hash: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOME_VALUES:
            raise CapabilityError(
                f"invocation_record.outcome {self.outcome!r} is not one of {sorted(OUTCOME_VALUES)!r}"
            )
        if self.reason_code not in REASON_CODES:
            raise CapabilityError(
                f"invocation_record.reason_code {self.reason_code!r} is not one of {sorted(REASON_CODES)!r}"
            )
        if self.policy_checkpoint not in POLICY_CHECKPOINT_STATUSES:
            raise CapabilityError(
                f"invocation_record.policy_checkpoint {self.policy_checkpoint!r} is not one of "
                f"{sorted(POLICY_CHECKPOINT_STATUSES)!r}"
            )
        if self.duration_ms < 0:
            raise CapabilityError("invocation_record.duration_ms must be >= 0")
        # Mirrors what InvocationRecord.from_dict enforces via
        # _require_sha256 — checked here too so a directly-constructed
        # record can never round-trip through to_dict/from_dict and be
        # rejected by the same class that produced it.
        _require_sha256(self.arguments_hash, where="invocation_record.arguments_hash")
        if self.result_hash is not None:
            _require_sha256(self.result_hash, where="invocation_record.result_hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "capability_set_id": self.capability_set_id,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "duration_ms": self.duration_ms,
            "policy_checkpoint": self.policy_checkpoint,
            "arguments_hash": self.arguments_hash,
            "result_hash": self.result_hash,
        }

    @classmethod
    def from_dict(cls, data: Any) -> InvocationRecord:
        mapping = _require_mapping(data, where="invocation_record")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "capability_id", "capability_set_id", "outcome", "reason_code", "duration_ms",
                "policy_checkpoint", "arguments_hash", "result_hash",
            }),
            where="invocation_record",
        )
        return cls(
            capability_id=_require_str(mapping.get("capability_id"), where="invocation_record.capability_id"),
            capability_set_id=_require_str(
                mapping.get("capability_set_id"), where="invocation_record.capability_set_id"
            ),
            outcome=_require_str(mapping.get("outcome"), where="invocation_record.outcome"),
            reason_code=_require_str(mapping.get("reason_code"), where="invocation_record.reason_code"),
            duration_ms=_require_int(mapping.get("duration_ms"), where="invocation_record.duration_ms"),
            policy_checkpoint=_require_str(
                mapping.get("policy_checkpoint"), where="invocation_record.policy_checkpoint"
            ),
            arguments_hash=_require_sha256(mapping.get("arguments_hash"), where="invocation_record.arguments_hash"),
            result_hash=(
                _require_sha256(mapping.get("result_hash"), where="invocation_record.result_hash")
                if mapping.get("result_hash") is not None else None
            ),
        )


# ---------------------------------------------------------------------------
# PolicyCheckpoint — the #197 seam, plus the pass-through pilot adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointVerdict:
    """What a `PolicyCheckpoint` answers for one capability invocation:
    `allowed` or `denied`, plus a free-text `reason` for logs only (never
    hashed, never part of any content-addressed document)."""

    decision: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.decision not in CHECKPOINT_DECISIONS:
            raise CapabilityError(
                f"checkpoint_verdict.decision {self.decision!r} is not one of {sorted(CHECKPOINT_DECISIONS)!r}"
            )


class PolicyCheckpoint(Protocol):
    """The narrow, synchronous seam `#197` will implement for real
    (design.md open question "Whether `#197` will expose a synchronous
    in-process check or an out-of-process call" — proceeding with this
    shape so the invocation path has one form regardless of which lands).
    A single method: given the capability being invoked and the execution
    that wants to invoke it, decide. Nothing here is called anywhere in
    this slice; `orchestrator/capability_gateway.py` (a later slice) is the
    only intended caller, immediately before dispatch."""

    def check(self, descriptor: CapabilityDescriptor, correlation: ExecutionCorrelation) -> CheckpointVerdict: ...


@dataclass(frozen=True)
class AbsentPolicyCheckpoint:
    """The named, logged pass-through adapter used while `#197` does not
    exist yet (requirements.md: "WHILE the `#197` checkpoint implementation
    is absent THE SYSTEM SHALL use a named, logged pass-through adapter
    that reproduces today's behaviour exactly ... and SHALL NOT claim a
    policy decision was made"). Always returns `allowed`; the caller is
    responsible for recording `policy_checkpoint: "absent"` on the
    resulting `InvocationRecord` via `policy_checkpoint_status` below —
    never `"allowed"`, which would misrepresent a real checkpoint having
    run.

    Swapping in a real `#197` implementation touches exactly one
    construction site: whichever call currently builds
    `AbsentPolicyCheckpoint()` starts building the real adapter instead;
    nothing else in this module, or in a caller using
    `policy_checkpoint_status`/`reason_code_for_verdict`, changes."""

    def check(self, descriptor: CapabilityDescriptor, correlation: ExecutionCorrelation) -> CheckpointVerdict:
        return CheckpointVerdict(decision="allowed", reason="no #197 checkpoint is wired yet")


def reason_code_for_verdict(verdict: CheckpointVerdict) -> str:
    """Pure: the closed `reason_code` a `CheckpointVerdict` maps to. `ok`
    for `allowed`, `policy-denied` for `denied` — the only two values
    `CHECKPOINT_DECISIONS` admits."""
    return "ok" if verdict.decision == "allowed" else "policy-denied"


def policy_checkpoint_status(checkpoint: PolicyCheckpoint, verdict: CheckpointVerdict) -> str:
    """Pure: the `InvocationRecord.policy_checkpoint` value for one
    `(checkpoint, verdict)` pair. `AbsentPolicyCheckpoint` always answers
    `absent`, regardless of the verdict it returned — the pilot must never
    claim a policy decision was made (see `AbsentPolicyCheckpoint`'s
    docstring). Any other checkpoint reports the verdict's own decision."""
    if isinstance(checkpoint, AbsentPolicyCheckpoint):
        return "absent"
    return verdict.decision


# ---------------------------------------------------------------------------
# Consequence classification table — config/capability-consequence.yaml
# ---------------------------------------------------------------------------

#: config/capability-consequence.yaml, relative to the repo root (this file
#: lives at orchestrator/capability.py).
DEFAULT_CONSEQUENCE_TABLE_PATH = Path(__file__).resolve().parent.parent / "config" / "capability-consequence.yaml"


def load_consequence_table(path: Path | str | None = None) -> Mapping[str, str]:
    """Parse `config/capability-consequence.yaml` into an immutable
    `{tool_name: consequence}` mapping. Pure aside from the one file read:
    no network, no caching, no default-classification decision — that is
    `classify_consequence`'s job. Every value must be a member of
    `CONSEQUENCE_VALUES`; anything else raises `CapabilityError` rather
    than silently accepting an unrecognised tier.

    Imports `yaml` lazily, inside this function, specifically so that
    merely `import orchestrator.capability` stays stdlib-only (see this
    module's docstring) — only calling this function pulls in PyYAML."""
    import yaml

    target = Path(path) if path is not None else DEFAULT_CONSEQUENCE_TABLE_PATH
    raw = target.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, Mapping):
        raise CapabilityError(f"{target}: expected a top-level mapping, got {type(data).__name__}")
    tools_raw = data.get("tools", {})
    if not isinstance(tools_raw, Mapping):
        raise CapabilityError(f"{target}: 'tools' must be a mapping")
    table: dict[str, str] = {}
    for name, consequence in tools_raw.items():
        if not isinstance(name, str) or not isinstance(consequence, str):
            raise CapabilityError(f"{target}: tool entries must be string:string, got {name!r}: {consequence!r}")
        if consequence not in CONSEQUENCE_VALUES:
            raise CapabilityError(
                f"{target}: tool {name!r} has consequence {consequence!r}, not one of {sorted(CONSEQUENCE_VALUES)!r}"
            )
        table[name] = consequence
    return MappingProxyType(table)


def classify_consequence(tool_name: str, table: Mapping[str, str], *, provider_id: str = MCTL_API_PROVIDER_ID) -> str:
    """Pure. `tool_name` may be the bare mctl-api tool name
    (`mctl_deploy_service`) or the SDK-visible name
    (`mcp__mctl__mctl_deploy_service`) — a leading `mcp__<alias>__` is
    stripped before lookup, so both forms classify identically. Any name
    absent from `table` defaults to `DEFAULT_CONSEQUENCE` (`consequential`)
    — the fail-safe rule design.md names explicitly: an unclassified
    capability is never allowed to skip the policy checkpoint by omission.

    `table` (`config/capability-consequence.yaml`) is mctl-api's own
    advertised tool set (ADR 017 sec. 8) — it says nothing about any other
    provider. `provider_id` (`ProviderRef.id`, default `MCTL_API_PROVIDER_ID`)
    scopes the lookup to that one provider: any other provider_id bypasses
    the table entirely and returns `DEFAULT_CONSEQUENCE`, so a bare-name
    collision with an unrelated provider's tool (e.g. a second `mcp-remote`
    server that happens to expose its own `mctl_whoami`) can never borrow
    mctl-api's classification and widen what skips the checkpoint."""
    if provider_id != MCTL_API_PROVIDER_ID:
        return DEFAULT_CONSEQUENCE
    bare = tool_name
    if bare.startswith("mcp__"):
        parts = bare.split("__", 2)
        if len(parts) == 3:
            bare = parts[2]
    return table.get(bare, DEFAULT_CONSEQUENCE)


