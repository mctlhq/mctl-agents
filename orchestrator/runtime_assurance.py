"""Runtime assurance contracts: `AssuranceScenario`, `AssuranceRun`,
`AssuranceResult`, `AssuranceFinding` (+ `AssuranceTargetAllowlist`), all
`assurance.mctl.ai/v1alpha1` (mctlhq/mctl-agents#694, ADR 022:
docs/adr/022-runtime-assurance-contract.md).

Scope: the versioned, validated data contracts a later deterministic bounded
probe harness consumes. Nothing here executes a probe, performs I/O, persists
a record, or is wired into a runner.

Trust boundaries enforced as validator rules (see the ADR for the mapping):
target scope, mutation mode and budgets are explicit data, never prompt
content; a model can only author a `FollowupProposal` (a template id plus
bounded parameters) or the text of a finding; a model-reported finding stays
`candidate` until a replay run confirms it; a failed or partial observation is
never recorded as observed absence; every run, result and finding is bound to
the exact scenario content hash, runtime release pins and target revision;
production mutation is unrepresentable.

Reuse map -- referenced, never copied or redefined: `ExecutionJoin` (identity,
#196 / ADR 011), `PolicyDecisionRef` (#197 / ADR 014), `ApprovalRef` (#198),
`ev-` evidence ids and `ArtifactRef` (#199 / ADR 018), `VersionPins` (ADR 007),
`context_snapshot.hash_bytes` / `canonical_json` (the one hashing rule) and
`redaction.contains_credential` (the one credential screen).

Authority: this module defines no field that expresses allow, deny, grant,
approval or credential material. Authorization stays with `policy_checkpoint`
and `action_approvals`. `check_target_allowlisted` only narrows scope; it never
authorizes an action. (The few field names that merely contain such a word --
`allowlist_entry`, `permitted_*`, `granted_budgets` -- name scope or budget
data, not a decision; the tests pin the exact set.)
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from typing import Any

from orchestrator import execution_evidence as _ee
from orchestrator import policy_checkpoint as _pc
from orchestrator.context_snapshot import canonical_json, hash_bytes
from orchestrator.execution_evidence import (
    AUTHORITIES,
    MAX_TOOL_CALLS,
    ApprovalRef,
    ArtifactRef,
    ExecutionEvidenceError,
    ExecutionJoin,
    PolicyDecisionRef,
    VersionPins,
)
from orchestrator.redaction import contains_credential

API_VERSION = "assurance.mctl.ai/v1alpha1"
KIND_SCENARIO = "AssuranceScenario"
KIND_ALLOWLIST = "AssuranceTargetAllowlist"
KIND_RUN = "AssuranceRun"
KIND_RESULT = "AssuranceResult"
KIND_FINDING = "AssuranceFinding"
SUPPORTED_API_VERSIONS: Mapping[str, Mapping[str, str]] = {
    kind: {API_VERSION: kind} for kind in (KIND_SCENARIO, KIND_ALLOWLIST, KIND_RUN, KIND_RESULT, KIND_FINDING)
}

SCENARIO_ID_PREFIX = "asc-"
ALLOWLIST_ID_PREFIX = "atl-"
RUN_ID_PREFIX = "arn-"
RESULT_ID_PREFIX = "ars-"
FINDING_ID_PREFIX = "afd-"

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------
TARGET_KINDS = frozenset({"repository", "mctl_service", "mcp_capability", "fixture"})
ENV_FIXTURE, ENV_SHADOW, ENV_PRODUCTION = "fixture", "shadow", "production"
ENVIRONMENTS = frozenset({ENV_FIXTURE, ENV_SHADOW, ENV_PRODUCTION})
ALLOWLISTED_ENVIRONMENTS = frozenset({ENV_SHADOW, ENV_PRODUCTION})
MUTATION_NONE, MUTATION_FIXTURE_ONLY = "none", "fixture_only"
MUTATION_MODES = frozenset({MUTATION_NONE, MUTATION_FIXTURE_ONLY})
EXEC_DETERMINISTIC, EXEC_ADAPTIVE = "deterministic", "bounded_adaptive"
EXECUTION_MODES = frozenset({EXEC_DETERMINISTIC, EXEC_ADAPTIVE})
SETUP_KINDS = frozenset({"fixture_load", "state_seed", "noop"})
INPUT_KINDS = frozenset({"literal", "fixture_ref"})
CHECK_KINDS = frozenset({"deterministic_check", "policy_decision_expected", "evidence_present"})
PARAM_KINDS = frozenset({"enum", "int_range", "fixture_ref"})
OBS_OBSERVED, OBS_PARTIAL, OBS_FAILED, OBS_NOT_ATTEMPTED = (
    "observed",
    "partial",
    "observation_failed",
    "not_attempted",
)
OBSERVATION_STATUSES = frozenset({OBS_OBSERVED, OBS_PARTIAL, OBS_FAILED, OBS_NOT_ATTEMPTED})
OUTCOME_HELD, OUTCOME_VIOLATED = "invariant_held", "invariant_violated"
OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"
OUTCOMES = frozenset(
    {
        OUTCOME_HELD,
        OUTCOME_VIOLATED,
        "inconclusive",
        OUTCOME_BUDGET_EXHAUSTED,
        "refused",
        "harness_failure",
    }
)
VERDICT_HELD, VERDICT_VIOLATED, VERDICT_UNKNOWN = "held", "violated", "unknown"
INVARIANT_VERDICTS = frozenset({VERDICT_HELD, VERDICT_VIOLATED, VERDICT_UNKNOWN})
STATE_CANDIDATE, STATE_CONFIRMED, STATE_REJECTED = "candidate", "confirmed", "rejected"
FINDING_STATES = frozenset({STATE_CANDIDATE, STATE_CONFIRMED, STATE_REJECTED})
DISPOSITIONS = frozenset({"false_positive", "not_reproducible", "duplicate", "out_of_scope"})
RELATION_FOLLOWUP, RELATION_REPLAY = "followup", "replay"
RELATIONS = frozenset({RELATION_FOLLOWUP, RELATION_REPLAY})
VALIDATION_AUTHORITIES = frozenset({"observed", "derived"})

#: Every governed action kind except a read-only tool call counts as mutating.
MUTATING_ACTION_KINDS = frozenset(_pc.ACTION_KINDS - {_pc.MCP_TOOL_CALL})


def is_read_only_tool(name: str) -> bool:
    """Whether `policy_checkpoint` classes the MCP tool as a read (verb prefix
    or named read tool, bare, `mctl_`- or `mcp__mctl__`-prefixed). Anything
    else - a known mutation or a tool the policy does not know - is not."""
    bare = name.removeprefix("mcp__mctl__").removeprefix("mctl_")
    return bare.startswith(_pc._READ_VERBS) or bare in _pc._READ_TOOLS


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------
MAX_ATTEMPTS_CAP = 10
MAX_TOOL_CALLS_CAP = MAX_TOOL_CALLS
MAX_WALL_CLOCK_CAP = 3600
MAX_FOLLOWUPS_CAP = 16
MAX_INPUTS = 32
MAX_SETUP_STEPS = 16
MAX_INVARIANTS = 16
MAX_TOOLS = 64
MAX_TEMPLATES = 8
MAX_PARAMS = 8
MAX_ENUM_VALUES = 16
MAX_ALLOWLIST_ENTRIES = 64
MAX_OBSERVATIONS = 256
MAX_POLICY_DECISIONS = 256
MAX_APPROVALS = 64
MAX_EVIDENCE_IDS = 64
MAX_INVARIANT_REFS = 16
MAX_STRING_CHARS = 256
MAX_DESCRIPTION_CHARS = 1000
MAX_STATEMENT_CHARS = 600
MAX_SUMMARY_CHARS = 600
MAX_NOTE_CHARS = 300
MAX_INT_BOUND = 10**9

_REDACTED = "[redacted]"
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_ITEM_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}")
_TOOL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
#: Same shape as `execution_evidence._GIT_SHA_PATTERN` (a test asserts equality).
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_EVIDENCE_ID_RE = re.compile(r"ev-[0-9a-f]{16}")
_RUNTIME_ID_RE = re.compile(r"ex-[0-9a-f]{16}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SEALED_ID_RES = {
    SCENARIO_ID_PREFIX: re.compile(r"asc-[0-9a-f]{16}"),
    ALLOWLIST_ID_PREFIX: re.compile(r"atl-[0-9a-f]{16}"),
    RUN_ID_PREFIX: re.compile(r"arn-[0-9a-f]{16}"),
    RESULT_ID_PREFIX: re.compile(r"ars-[0-9a-f]{16}"),
    FINDING_ID_PREFIX: re.compile(r"afd-[0-9a-f]{16}"),
}

#: Excluded from every `content_hash`: derived identity, and facts about when
#: rather than what.
_UNHASHED = frozenset({"content_hash", "started_at", "ended_at"})
_ID_FIELDS = {
    SCENARIO_ID_PREFIX: "scenario_id",
    ALLOWLIST_ID_PREFIX: "allowlist_id",
    RUN_ID_PREFIX: "run_id",
    RESULT_ID_PREFIX: "result_id",
    FINDING_ID_PREFIX: "finding_id",
}


class RuntimeAssuranceError(ValueError):
    """A runtime assurance record violates the v1alpha1 contract."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _fail(message: str) -> RuntimeAssuranceError:
    return RuntimeAssuranceError(message)


def _g(data: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """`data.get(key)` typed `Any`: values are validated by the leaf types."""
    return data.get(key, default)


def _mapping(data: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise _fail(f"{where}: expected an object, got {type(data).__name__}")
    return data


def _reject_unknown(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise _fail(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _str(value: Any, *, where: str, max_chars: int = MAX_STRING_CHARS, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise _fail(f"{where}: expected a non-empty string of at most {max_chars} chars")
    if contains_credential(value):
        raise _fail(f"{where}: value contains a credential shape")
    if pattern is not None and not pattern.fullmatch(value):
        raise _fail(f"{where}: value does not match {pattern.pattern!r}")
    return value


def _opt_str(
    value: Any, *, where: str, max_chars: int = MAX_STRING_CHARS, pattern: re.Pattern[str] | None = None
) -> str | None:
    return None if value is None else _str(value, where=where, max_chars=max_chars, pattern=pattern)


def _int(value: Any, *, where: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _fail(f"{where}: expected an integer within {lo}..{hi}")
    return value


def _one_of(value: Any, allowed: frozenset[str], *, where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _fail(f"{where}: {value!r} is not one of {sorted(allowed)!r}")
    return value


def _instance(value: Any, cls: type, *, where: str) -> None:
    if not isinstance(value, cls):
        raise _fail(f"{where}: expected {cls.__name__}, got {type(value).__name__}")


def _seq(value: Any, *, where: str, max_items: int, min_items: int = 0) -> tuple[Any, ...]:
    if value is None:
        value = ()
    if not isinstance(value, list | tuple):
        raise _fail(f"{where}: expected a list")
    if not min_items <= len(value) <= max_items:
        raise _fail(f"{where}: expected {min_items}..{max_items} entries, got {len(value)}")
    return tuple(value)


def _typed_seq(owner: Any, name: str, cls: type, *, max_items: int, min_items: int = 0) -> tuple[Any, ...]:
    """Coerce `owner.<name>` to a tuple of `cls` instances (frozen-safe)."""
    items = _seq(getattr(owner, name), where=name, max_items=max_items, min_items=min_items)
    for item in items:
        _instance(item, cls, where=name)
    object.__setattr__(owner, name, items)
    return items


def _str_seq(
    owner: Any,
    name: str,
    *,
    max_items: int,
    pattern: re.Pattern[str],
    min_items: int = 0,
    max_chars: int = MAX_STRING_CHARS,
    sort: bool = False,
) -> tuple[str, ...]:
    items = _seq(getattr(owner, name), where=name, max_items=max_items, min_items=min_items)
    for item in items:
        _str(item, where=name, max_chars=max_chars, pattern=pattern)
    if len(set(items)) != len(items):
        raise _fail(f"{name}: duplicate entries")
    result = tuple(sorted(items)) if sort else items
    object.__setattr__(owner, name, result)
    return result


def _unique(values: Sequence[str], *, where: str) -> None:
    if len(set(values)) != len(values):
        raise _fail(f"{where}: duplicate ids")


def _screen(value: Any, *, where: str) -> None:
    """Credential-screen every string reachable in a reused block's dict."""
    if isinstance(value, str):
        if contains_credential(value):
            raise _fail(f"{where}: value contains a credential shape")
    elif isinstance(value, Mapping):
        for key, sub in value.items():
            _screen(sub, where=f"{where}.{key}")
    elif isinstance(value, list | tuple):
        for sub in value:
            _screen(sub, where=where)


def _enc(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list | tuple):
        return [_enc(v) for v in value]
    return value


def _encode(obj: Any, skip: frozenset[str] = frozenset()) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(obj):
        if f.name in skip:
            continue
        value = getattr(obj, f.name)
        if value is not None:
            out[f.name] = _enc(value)
    return out


def _timestamp(value: Any, *, where: str) -> str:
    return _str(value, where=where, max_chars=20, pattern=_TIMESTAMP_RE)


def redact_text(text: str, *, max_chars: int) -> str:
    """Normalise whitespace, bound, and screen model-authored text. A value
    with a credential shape (or no content) is replaced, never masked."""
    flat = " ".join(str(text).split())
    if not flat or contains_credential(flat):
        return _REDACTED
    return flat[:max_chars].rstrip()


def _wrap(call: Any, *args: Any) -> Any:
    try:
        return call(*args)
    except ExecutionEvidenceError as exc:
        raise _fail(str(exc)) from exc


# ---------------------------------------------------------------------------
# Sealing helpers (shared by the five sealed kinds)
# ---------------------------------------------------------------------------
def _derive_id(prefix: str, content_hash: str) -> str:
    return prefix + content_hash[len("sha256:") :][:16]


def recompute_content_hash(record: Any) -> str:
    """The hash of the record's hashed fields, ignoring any stored hash/id."""
    own_id = _ID_FIELDS[record._ID_PREFIX]
    payload = {k: v for k, v in record.to_dict().items() if k not in _UNHASHED and k != own_id}
    return hash_bytes(canonical_json(payload))


def _seal(draft: Any, id_field: str, prefix: str) -> Any:
    content_hash = recompute_content_hash(draft)
    return replace(draft, **{id_field: _derive_id(prefix, content_hash), "content_hash": content_hash})


def _verify_seal(record: Any, id_field: str, prefix: str) -> None:
    stored_id = getattr(record, id_field)
    if not stored_id and not record.content_hash:
        return  # an unsealed draft
    _str(stored_id, where=id_field, max_chars=32, pattern=_SEALED_ID_RES[prefix])
    _str(record.content_hash, where="content_hash", max_chars=71, pattern=_DIGEST_RE)
    expected = recompute_content_hash(record)
    if record.content_hash != expected:
        raise _fail(f"content_hash mismatch: stored {record.content_hash!r}, recomputed {expected!r}")
    if stored_id != _derive_id(prefix, expected):
        raise _fail(f"{id_field} {stored_id!r} does not derive from the content hash")


def _envelope(data: Any, kind: str) -> Mapping[str, Any]:
    d = _mapping(data, where=kind)
    version = _g(d, "api_version")
    if not isinstance(version, str) or version not in SUPPORTED_API_VERSIONS[kind]:
        raise _fail(f"{kind}: unsupported api_version {version!r}")
    if _g(d, "kind") != kind:
        raise _fail(f"{kind}: kind is {_g(d, 'kind')!r}")
    return d


def _sealed_dict(record: Any, kind: str) -> dict[str, Any]:
    return {"api_version": API_VERSION, "kind": kind, **_encode(record)}


def _from_dict_keys(cls: type) -> frozenset[str]:
    return frozenset(f.name for f in fields(cls))


# ---------------------------------------------------------------------------
# Shared blocks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AllowlistEntryRef:
    allowlist_id: str
    entry_id: str
    content_hash: str

    def __post_init__(self) -> None:
        _str(
            self.allowlist_id,
            where="allowlist_entry.allowlist_id",
            max_chars=32,
            pattern=_SEALED_ID_RES[ALLOWLIST_ID_PREFIX],
        )
        _str(self.entry_id, where="allowlist_entry.entry_id", pattern=_ITEM_ID_RE)
        _str(self.content_hash, where="allowlist_entry.content_hash", max_chars=71, pattern=_DIGEST_RE)

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> AllowlistEntryRef:
        d = _mapping(data, where="allowlist_entry")
        _reject_unknown(d, _from_dict_keys(cls), where="allowlist_entry")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


def _check_ref(value: Any, *, where: str) -> None:
    _str(value, where=where, pattern=_REF_RE)  # excludes wildcard / glob characters


@dataclass(frozen=True)
class TargetBoundary:
    kind: str
    ref: str
    environment: str
    allowlist_entry: AllowlistEntryRef | None = None

    def __post_init__(self) -> None:
        _one_of(self.kind, TARGET_KINDS, where="target.kind")
        _check_ref(self.ref, where="target.ref")
        _one_of(self.environment, ENVIRONMENTS, where="target.environment")
        if self.allowlist_entry is not None:
            _instance(self.allowlist_entry, AllowlistEntryRef, where="target.allowlist_entry")
        if self.environment in ALLOWLISTED_ENVIRONMENTS and self.allowlist_entry is None:
            raise _fail(f"target: a {self.environment} target requires an allowlist_entry")
        if self.environment == ENV_FIXTURE and self.allowlist_entry is not None:
            raise _fail("target: a fixture target forbids an allowlist_entry")

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> TargetBoundary:
        d = _mapping(data, where="target")
        _reject_unknown(d, _from_dict_keys(cls), where="target")
        entry = _g(d, "allowlist_entry")
        return cls(
            kind=_g(d, "kind"),
            ref=_g(d, "ref"),
            environment=_g(d, "environment"),
            allowlist_entry=None if entry is None else AllowlistEntryRef.from_dict(entry),
        )


@dataclass(frozen=True)
class Budgets:
    """Every axis is required and positive within a hard cap
    (`max_followups` may be zero). No "unlimited"."""

    max_attempts: int
    max_tool_calls: int
    max_wall_clock_seconds: int
    max_followups: int

    def __post_init__(self) -> None:
        _int(self.max_attempts, where="budgets.max_attempts", lo=1, hi=MAX_ATTEMPTS_CAP)
        _int(self.max_tool_calls, where="budgets.max_tool_calls", lo=1, hi=MAX_TOOL_CALLS_CAP)
        _int(self.max_wall_clock_seconds, where="budgets.max_wall_clock_seconds", lo=1, hi=MAX_WALL_CLOCK_CAP)
        _int(self.max_followups, where="budgets.max_followups", lo=0, hi=MAX_FOLLOWUPS_CAP)

    def within(self, other: Budgets) -> bool:
        return all(getattr(self, f.name) <= getattr(other, f.name) for f in fields(self))

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> Budgets:
        d = _mapping(data, where="budgets")
        _reject_unknown(d, _from_dict_keys(cls), where="budgets")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class BudgetUsage:
    attempts: int
    tool_calls: int
    wall_clock_seconds: int
    followups: int

    def __post_init__(self) -> None:
        _int(self.attempts, where="consumed.attempts", lo=0, hi=MAX_ATTEMPTS_CAP * 100)
        _int(self.tool_calls, where="consumed.tool_calls", lo=0, hi=MAX_TOOL_CALLS_CAP * 100)
        _int(self.wall_clock_seconds, where="consumed.wall_clock_seconds", lo=0, hi=MAX_WALL_CLOCK_CAP * 100)
        _int(self.followups, where="consumed.followups", lo=0, hi=MAX_FOLLOWUPS_CAP * 100)

    def within(self, budgets: Budgets) -> bool:
        return (
            self.attempts <= budgets.max_attempts
            and self.tool_calls <= budgets.max_tool_calls
            and self.wall_clock_seconds <= budgets.max_wall_clock_seconds
            and self.followups <= budgets.max_followups
        )

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> BudgetUsage:
        d = _mapping(data, where="consumed")
        _reject_unknown(d, _from_dict_keys(cls), where="consumed")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class InputSpec:
    name: str
    kind: str
    value: str | None = None  # literal
    content_hash: str | None = None  # fixture_ref

    def __post_init__(self) -> None:
        _str(self.name, where="input.name", pattern=_ITEM_ID_RE)
        _one_of(self.kind, INPUT_KINDS, where="input.kind")
        if self.kind == "literal":
            _str(self.value, where="input.value", max_chars=MAX_STRING_CHARS)
            if self.content_hash is not None:
                raise _fail("input: a literal carries no content_hash")
        else:
            _str(self.content_hash, where="input.content_hash", max_chars=71, pattern=_DIGEST_RE)
            if self.value is not None:
                raise _fail("input: a fixture_ref carries no inline value")

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> InputSpec:
        d = _mapping(data, where="input")
        _reject_unknown(d, _from_dict_keys(cls), where="input")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class SetupStep:
    step_id: str
    kind: str
    fixture_ref: str | None = None

    def __post_init__(self) -> None:
        _str(self.step_id, where="setup.step_id", pattern=_ITEM_ID_RE)
        _one_of(self.kind, SETUP_KINDS, where="setup.kind")
        if self.kind == "noop":
            if self.fixture_ref is not None:
                raise _fail("setup: a noop step carries no fixture_ref")
        else:
            _str(self.fixture_ref, where="setup.fixture_ref", max_chars=71, pattern=_DIGEST_RE)

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> SetupStep:
        d = _mapping(data, where="setup")
        _reject_unknown(d, _from_dict_keys(cls), where="setup")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class InvariantSpec:
    invariant_id: str
    statement: str
    check_kind: str

    def __post_init__(self) -> None:
        _str(self.invariant_id, where="invariant.invariant_id", pattern=_ITEM_ID_RE)
        _str(self.statement, where="invariant.statement", max_chars=MAX_STATEMENT_CHARS)
        _one_of(self.check_kind, CHECK_KINDS, where="invariant.check_kind")

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> InvariantSpec:
        d = _mapping(data, where="invariant")
        _reject_unknown(d, _from_dict_keys(cls), where="invariant")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class ParamBound:
    name: str
    kind: str
    values: tuple[str, ...] = ()  # enum
    minimum: int | None = None  # int_range
    maximum: int | None = None  # int_range

    def __post_init__(self) -> None:
        _str(self.name, where="parameter.name", pattern=_ITEM_ID_RE)
        _one_of(self.kind, PARAM_KINDS, where="parameter.kind")
        if self.kind == "enum":
            _str_seq(self, "values", max_items=MAX_ENUM_VALUES, min_items=1, pattern=_ITEM_ID_RE)
            if self.minimum is not None or self.maximum is not None:
                raise _fail("parameter: an enum carries no numeric bounds")
        elif self.kind == "int_range":
            _int(self.minimum, where="parameter.minimum", lo=-MAX_INT_BOUND, hi=MAX_INT_BOUND)
            _int(self.maximum, where="parameter.maximum", lo=-MAX_INT_BOUND, hi=MAX_INT_BOUND)
            if self.minimum > self.maximum:  # type: ignore[operator]
                raise _fail("parameter: minimum exceeds maximum")
            if self.values:
                raise _fail("parameter: an int_range carries no enum values")
        elif self.values or self.minimum is not None or self.maximum is not None:
            raise _fail("parameter: a fixture_ref carries no values or numeric bounds")

    def to_dict(self) -> dict[str, Any]:
        out = _encode(self, skip=frozenset({"name"}))
        if not self.values:
            out.pop("values", None)
        return out

    @classmethod
    def from_dict(cls, name: str, data: Any) -> ParamBound:
        d = _mapping(data, where=f"parameter {name}")
        _reject_unknown(d, frozenset({"kind", "values", "minimum", "maximum"}), where=f"parameter {name}")
        return cls(
            name=name,
            kind=_g(d, "kind"),
            values=_g(d, "values") or (),
            minimum=_g(d, "minimum"),
            maximum=_g(d, "maximum"),
        )

    def check_value(self, value: Any, fixture_refs: frozenset[str]) -> None:
        where = f"followup parameter {self.name}"
        if self.kind == "enum":
            if not isinstance(value, str) or value not in self.values:
                raise _fail(f"{where}: {value!r} is not one of {sorted(self.values)!r}")
        elif self.kind == "int_range":
            _int(value, where=where, lo=self.minimum, hi=self.maximum)  # type: ignore[arg-type]
        else:
            if not isinstance(value, str) or value not in fixture_refs:
                raise _fail(f"{where}: not a fixture ref declared by the scenario")


@dataclass(frozen=True)
class FollowupTemplate:
    template_id: str
    parameters: tuple[ParamBound, ...] = ()

    def __post_init__(self) -> None:
        _str(self.template_id, where="followup_template.template_id", pattern=_ITEM_ID_RE)
        params = _typed_seq(self, "parameters", ParamBound, max_items=MAX_PARAMS)
        _unique([p.name for p in params], where="followup_template.parameters")
        object.__setattr__(self, "parameters", tuple(sorted(params, key=lambda p: p.name)))

    def to_dict(self) -> dict[str, Any]:
        return {"template_id": self.template_id, "parameters": {p.name: p.to_dict() for p in self.parameters}}

    @classmethod
    def from_dict(cls, data: Any) -> FollowupTemplate:
        d = _mapping(data, where="followup_template")
        _reject_unknown(d, frozenset({"template_id", "parameters"}), where="followup_template")
        params = _mapping(_g(d, "parameters") or {}, where="followup_template.parameters")
        if len(params) > MAX_PARAMS:
            raise _fail("followup_template.parameters: too many parameters")
        return cls(
            template_id=_g(d, "template_id"), parameters=tuple(ParamBound.from_dict(n, b) for n, b in params.items())
        )


@dataclass(frozen=True)
class FollowupProposal:
    """The only shape a model may author for a follow-up. It has no field for
    a target, environment, tenant, credential, tool or action kind, so none
    can be expressed (`from_dict` rejects such keys as unknown)."""

    template_id: str
    parameters: tuple[tuple[str, str | int], ...] = ()

    def __post_init__(self) -> None:
        _str(self.template_id, where="followup.template_id", pattern=_ITEM_ID_RE)
        pairs = _seq(self.parameters, where="followup.parameters", max_items=MAX_PARAMS)
        clean = []
        for pair in pairs:
            if not isinstance(pair, tuple | list) or len(pair) != 2:
                raise _fail("followup.parameters: expected (name, value) pairs")
            name, value = pair
            _str(name, where="followup.parameters name", pattern=_ITEM_ID_RE)
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise _fail(f"followup parameter {name}: expected a string or integer")
            if isinstance(value, str):
                _str(value, where=f"followup parameter {name}")
            clean.append((name, value))
        _unique([n for n, _ in clean], where="followup.parameters")
        object.__setattr__(self, "parameters", tuple(sorted(clean)))

    def to_dict(self) -> dict[str, Any]:
        return {"template_id": self.template_id, "parameters": dict(self.parameters)}

    @classmethod
    def from_dict(cls, data: Any) -> FollowupProposal:
        d = _mapping(data, where="followup")
        _reject_unknown(d, frozenset({"template_id", "parameters"}), where="followup")
        params = _mapping(_g(d, "parameters") or {}, where="followup.parameters")
        return cls(template_id=_g(d, "template_id"), parameters=tuple(params.items()))


@dataclass(frozen=True)
class ScenarioRef:
    scenario_id: str
    content_hash: str
    name: str
    version: str

    def __post_init__(self) -> None:
        _str(self.scenario_id, where="scenario.scenario_id", max_chars=32, pattern=_SEALED_ID_RES[SCENARIO_ID_PREFIX])
        _str(self.content_hash, where="scenario.content_hash", max_chars=71, pattern=_DIGEST_RE)
        _str(self.name, where="scenario.name", pattern=_SLUG_RE)
        _str(self.version, where="scenario.version", max_chars=128, pattern=_VERSION_RE)

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> ScenarioRef:
        d = _mapping(data, where="scenario")
        _reject_unknown(d, _from_dict_keys(cls), where="scenario")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class TargetRevision:
    kind: str
    ref: str
    revision: str

    def __post_init__(self) -> None:
        _one_of(self.kind, TARGET_KINDS, where="target.kind")
        _check_ref(self.ref, where="target.ref")
        _str(self.revision, where="target.revision", max_chars=128)
        if not (_GIT_SHA_RE.fullmatch(self.revision) or _VERSION_RE.fullmatch(self.revision)):
            raise _fail("target.revision: expected a full git SHA or a bounded version token")

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> TargetRevision:
        d = _mapping(data, where="target")
        _reject_unknown(d, _from_dict_keys(cls), where="target")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


def _check_runtime_pins(pins: VersionPins) -> None:
    _instance(pins, VersionPins, where="runtime")
    for name in ("agent", "environment"):
        _str(getattr(pins, name), where=f"runtime.{name}", max_chars=128, pattern=_SLUG_RE)
    _str(pins.definition_version, where="runtime.definition_version", max_chars=128, pattern=_VERSION_RE)
    _str(pins.definition_content_hash, where="runtime.definition_content_hash", max_chars=71, pattern=_DIGEST_RE)
    if bool(pins.profile_name) != bool(pins.profile_version):
        raise _fail("runtime: profile_name and profile_version are required together")
    if pins.profile_name:
        _str(pins.profile_name, where="runtime.profile_name", max_chars=128, pattern=_SLUG_RE)
        _str(pins.profile_version, where="runtime.profile_version", max_chars=128, pattern=_VERSION_RE)
    if pins.profile_content_hash:
        _str(pins.profile_content_hash, where="runtime.profile_content_hash", max_chars=71, pattern=_DIGEST_RE)
    if pins.release_revision is not None:
        _int(pins.release_revision, where="runtime.release_revision", lo=0, hi=_ee.MAX_RELEASE_REVISION)


@dataclass(frozen=True)
class VersionBinding:
    scenario: ScenarioRef
    runtime: VersionPins
    target: TargetRevision

    def __post_init__(self) -> None:
        _instance(self.scenario, ScenarioRef, where="binding.scenario")
        _instance(self.target, TargetRevision, where="binding.target")
        _check_runtime_pins(self.runtime)

    def to_dict(self) -> dict[str, Any]:
        return {"scenario": self.scenario.to_dict(), "runtime": self.runtime.to_dict(), "target": self.target.to_dict()}

    @classmethod
    def from_dict(cls, data: Any) -> VersionBinding:
        d = _mapping(data, where="binding")
        _reject_unknown(d, frozenset({"scenario", "runtime", "target"}), where="binding")
        for key in ("scenario", "runtime", "target"):
            if _g(d, key) is None:
                raise _fail(f"binding.{key}: required")
        return cls(
            scenario=ScenarioRef.from_dict(d["scenario"]),
            runtime=_wrap(VersionPins.from_dict, d["runtime"]),
            target=TargetRevision.from_dict(d["target"]),
        )


@dataclass(frozen=True)
class ObservationRef:
    observation_id: str
    invariant_ids: tuple[str, ...]
    status: str
    authority: str
    evidence_id: str | None = None
    artifact: ArtifactRef | None = None

    def __post_init__(self) -> None:
        _str(self.observation_id, where="observation.observation_id", pattern=_ITEM_ID_RE)
        _str_seq(self, "invariant_ids", max_items=MAX_INVARIANT_REFS, min_items=1, pattern=_ITEM_ID_RE)
        _one_of(self.status, OBSERVATION_STATUSES, where="observation.status")
        _one_of(self.authority, frozenset(AUTHORITIES), where="observation.authority")
        _opt_str(self.evidence_id, where="observation.evidence_id", max_chars=19, pattern=_EVIDENCE_ID_RE)
        if self.artifact is not None:
            _instance(self.artifact, ArtifactRef, where="observation.artifact")
            _screen(self.artifact.to_dict(), where="observation.artifact")
            _str(self.artifact.name, where="observation.artifact.name", pattern=_ee._ARTIFACT_NAME_PATTERN)
            _str(self.artifact.kind, where="observation.artifact.kind", max_chars=64, pattern=_SLUG_RE)
            _str(
                self.artifact.content_hash, where="observation.artifact.content_hash", max_chars=71, pattern=_DIGEST_RE
            )
        if self.status == OBS_OBSERVED and self.evidence_id is None and self.artifact is None:
            raise _fail("observation: an observed status requires an evidence_id or artifact reference")

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> ObservationRef:
        d = _mapping(data, where="observation")
        _reject_unknown(d, _from_dict_keys(cls), where="observation")
        artifact = _g(d, "artifact")
        return cls(
            observation_id=_g(d, "observation_id"),
            invariant_ids=_g(d, "invariant_ids"),
            status=_g(d, "status"),
            authority=_g(d, "authority"),
            evidence_id=_g(d, "evidence_id"),
            artifact=None if artifact is None else _wrap(ArtifactRef.from_dict, artifact),
        )


def _check_policy_ref(ref: PolicyDecisionRef) -> None:
    _instance(ref, PolicyDecisionRef, where="policy_decision")
    _screen(ref.to_dict(), where="policy_decision")
    _one_of(ref.verdict, frozenset(_pc.VERDICTS), where="policy_decision.verdict")
    for name in ("action_digest", "code", "policy_version"):
        _str(getattr(ref, name), where=f"policy_decision.{name}", max_chars=128)
    for name in ("rule_id", "approval_ref"):
        if getattr(ref, name):
            _str(getattr(ref, name), where=f"policy_decision.{name}", max_chars=128)


def _check_approval_ref(ref: ApprovalRef) -> None:
    _instance(ref, ApprovalRef, where="approval")
    _screen(ref.to_dict(), where="approval")
    _str(ref.approval_id, where="approval.approval_id", max_chars=128)
    if not ref.approval_id.startswith(_ee.APPROVAL_ID_PREFIX):
        raise _fail(f"approval.approval_id must start with {_ee.APPROVAL_ID_PREFIX!r}")
    _str(ref.intent_hash, where="approval.intent_hash", max_chars=71, pattern=_DIGEST_RE)
    _one_of(ref.state, _ee.APPROVAL_STATES, where="approval.state")


def _check_execution_join(join: ExecutionJoin) -> None:
    _instance(join, ExecutionJoin, where="execution")
    _screen(join.to_dict(), where="execution")
    if not join.runtime_execution_id:
        raise _fail("execution.runtime_execution_id is required")
    _str(join.runtime_execution_id, where="execution.runtime_execution_id", max_chars=19, pattern=_RUNTIME_ID_RE)
    if join.execution_id:
        _str(join.execution_id, where="execution.execution_id", max_chars=128)
        if not join.execution_id.startswith(_ee.EXECUTION_ID_PREFIX):
            raise _fail(f"execution.execution_id must start with {_ee.EXECUTION_ID_PREFIX!r}")
    for name in ("work_item_id", "trace_id"):
        if getattr(join, name):
            _str(getattr(join, name), where=f"execution.{name}", max_chars=128)


# ---------------------------------------------------------------------------
# AssuranceTargetAllowlist
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AllowlistEntry:
    entry_id: str
    target_kind: str
    ref: str
    environments: tuple[str, ...]

    def __post_init__(self) -> None:
        _str(self.entry_id, where="entry.entry_id", pattern=_ITEM_ID_RE)
        _one_of(self.target_kind, TARGET_KINDS, where="entry.target_kind")
        _check_ref(self.ref, where="entry.ref")
        envs = _seq(self.environments, where="entry.environments", max_items=len(ALLOWLISTED_ENVIRONMENTS), min_items=1)
        for env in envs:
            _one_of(env, ALLOWLISTED_ENVIRONMENTS, where="entry.environments")
        _unique(envs, where="entry.environments")
        object.__setattr__(self, "environments", tuple(sorted(envs)))

    @property
    def entry_hash(self) -> str:
        return hash_bytes(canonical_json(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "target_kind": self.target_kind,
            "ref": self.ref,
            "environments": list(self.environments),
        }

    @classmethod
    def from_dict(cls, data: Any) -> AllowlistEntry:
        d = _mapping(data, where="entry")
        _reject_unknown(d, _from_dict_keys(cls), where="entry")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class AssuranceTargetAllowlist:
    _ID_PREFIX = ALLOWLIST_ID_PREFIX

    """A versioned, hashed list of exact targets scenarios may name. Data a
    reviewed change controls; a model never writes it. It narrows scope only."""

    name: str
    version: str
    entries: tuple[AllowlistEntry, ...]
    allowlist_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        _str(self.name, where="allowlist.name", pattern=_SLUG_RE)
        _str(self.version, where="allowlist.version", max_chars=128, pattern=_VERSION_RE)
        entries = _typed_seq(self, "entries", AllowlistEntry, max_items=MAX_ALLOWLIST_ENTRIES, min_items=1)
        _unique([e.entry_id for e in entries], where="allowlist.entries")
        _verify_seal(self, "allowlist_id", ALLOWLIST_ID_PREFIX)

    def to_dict(self) -> dict[str, Any]:
        return _sealed_dict(self, KIND_ALLOWLIST)

    @classmethod
    def from_dict(cls, data: Any) -> AssuranceTargetAllowlist:
        d = _envelope(data, KIND_ALLOWLIST)
        _reject_unknown(d, _from_dict_keys(cls) | {"api_version", "kind"}, where=KIND_ALLOWLIST)
        _require_sealed(d, "allowlist_id", KIND_ALLOWLIST)
        return cls(
            name=_g(d, "name"),
            version=_g(d, "version"),
            entries=tuple(
                AllowlistEntry.from_dict(e)
                for e in _seq(_g(d, "entries"), where="entries", max_items=MAX_ALLOWLIST_ENTRIES)
            ),
            allowlist_id=_g(d, "allowlist_id"),
            content_hash=_g(d, "content_hash"),
        )

    def entry(self, entry_id: str) -> AllowlistEntry | None:
        return next((e for e in self.entries if e.entry_id == entry_id), None)


def _require_sealed(d: Mapping[str, Any], id_field: str, kind: str) -> None:
    if not _g(d, id_field) or not _g(d, "content_hash"):
        raise _fail(f"{kind}: a stored document must carry {id_field} and content_hash")


def seal_allowlist(**fields_: Any) -> AssuranceTargetAllowlist:
    return _seal(AssuranceTargetAllowlist(**fields_), "allowlist_id", ALLOWLIST_ID_PREFIX)


# ---------------------------------------------------------------------------
# AssuranceScenario
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AssuranceScenario:
    _ID_PREFIX = SCENARIO_ID_PREFIX

    name: str
    version: str
    description: str
    target: TargetBoundary
    invariants: tuple[InvariantSpec, ...]
    budgets: Budgets
    execution_mode: str
    setup: tuple[SetupStep, ...] = ()
    inputs: tuple[InputSpec, ...] = ()
    mutation_mode: str = MUTATION_NONE
    permitted_action_kinds: tuple[str, ...] = ()
    permitted_tools: tuple[str, ...] = ()
    followup_templates: tuple[FollowupTemplate, ...] = ()
    scenario_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        _str(self.name, where="scenario.name", pattern=_SLUG_RE)
        _str(self.version, where="scenario.version", max_chars=128, pattern=_VERSION_RE)
        _str(self.description, where="scenario.description", max_chars=MAX_DESCRIPTION_CHARS)
        _instance(self.target, TargetBoundary, where="scenario.target")
        _instance(self.budgets, Budgets, where="scenario.budgets")
        _one_of(self.execution_mode, EXECUTION_MODES, where="scenario.execution_mode")
        _one_of(self.mutation_mode, MUTATION_MODES, where="scenario.mutation_mode")
        invariants = _typed_seq(self, "invariants", InvariantSpec, max_items=MAX_INVARIANTS, min_items=1)
        _unique([i.invariant_id for i in invariants], where="scenario.invariants")
        setup = _typed_seq(self, "setup", SetupStep, max_items=MAX_SETUP_STEPS)
        _unique([s.step_id for s in setup], where="scenario.setup")
        inputs = _typed_seq(self, "inputs", InputSpec, max_items=MAX_INPUTS)
        _unique([i.name for i in inputs], where="scenario.inputs")
        kinds = _str_seq(self, "permitted_action_kinds", max_items=len(_pc.ACTION_KINDS), pattern=_TOOL_RE, sort=True)
        for kind in kinds:
            _one_of(kind, frozenset(_pc.ACTION_KINDS), where="scenario.permitted_action_kinds")
        tools = _str_seq(self, "permitted_tools", max_items=MAX_TOOLS, pattern=_TOOL_RE, max_chars=128, sort=True)
        templates = _typed_seq(self, "followup_templates", FollowupTemplate, max_items=MAX_TEMPLATES)
        _unique([t.template_id for t in templates], where="scenario.followup_templates")
        if self.target.environment == ENV_PRODUCTION and self.mutation_mode != MUTATION_NONE:
            raise _fail("scenario: production mutation cannot be represented in v1alpha1")
        if self.mutation_mode == MUTATION_NONE and MUTATING_ACTION_KINDS.intersection(kinds):
            raise _fail("scenario: a mutating action kind requires mutation_mode other than none")
        if self.mutation_mode == MUTATION_NONE:
            for tool in tools:
                if not is_read_only_tool(tool):
                    raise _fail(f"scenario: tool {tool!r} is not read-only; mutation_mode none permits reads only")
        if self.execution_mode == EXEC_DETERMINISTIC and (self.budgets.max_followups != 0 or templates):
            raise _fail("scenario: a deterministic scenario allows no follow-ups")
        _verify_seal(self, "scenario_id", SCENARIO_ID_PREFIX)

    @property
    def fixture_refs(self) -> frozenset[str]:
        return frozenset(
            [s.fixture_ref for s in self.setup if s.fixture_ref]
            + [i.content_hash for i in self.inputs if i.content_hash]
        )

    def to_dict(self) -> dict[str, Any]:
        return _sealed_dict(self, KIND_SCENARIO)

    @classmethod
    def from_dict(cls, data: Any) -> AssuranceScenario:
        d = _envelope(data, KIND_SCENARIO)
        _reject_unknown(d, _from_dict_keys(cls) | {"api_version", "kind"}, where=KIND_SCENARIO)
        _require_sealed(d, "scenario_id", KIND_SCENARIO)
        if _g(d, "target") is None or _g(d, "budgets") is None:
            raise _fail("scenario: target and budgets are required")
        return cls(
            name=_g(d, "name"),
            version=_g(d, "version"),
            description=_g(d, "description"),
            target=TargetBoundary.from_dict(d["target"]),
            invariants=tuple(
                InvariantSpec.from_dict(i)
                for i in _seq(_g(d, "invariants"), where="invariants", max_items=MAX_INVARIANTS)
            ),
            budgets=Budgets.from_dict(d["budgets"]),
            execution_mode=_g(d, "execution_mode"),
            setup=tuple(SetupStep.from_dict(s) for s in _seq(_g(d, "setup"), where="setup", max_items=MAX_SETUP_STEPS)),
            inputs=tuple(InputSpec.from_dict(i) for i in _seq(_g(d, "inputs"), where="inputs", max_items=MAX_INPUTS)),
            mutation_mode=_g(d, "mutation_mode", MUTATION_NONE),
            permitted_action_kinds=_g(d, "permitted_action_kinds"),
            permitted_tools=_g(d, "permitted_tools"),
            followup_templates=tuple(
                FollowupTemplate.from_dict(t)
                for t in _seq(_g(d, "followup_templates"), where="followup_templates", max_items=MAX_TEMPLATES)
            ),
            scenario_id=_g(d, "scenario_id"),
            content_hash=_g(d, "content_hash"),
        )


def seal_scenario(**fields_: Any) -> AssuranceScenario:
    return _seal(AssuranceScenario(**fields_), "scenario_id", SCENARIO_ID_PREFIX)


def check_scenario_catalog(scenarios: Sequence[AssuranceScenario]) -> None:
    """Refuse a catalog where one `(name, version)` has two different hashes."""
    seen: dict[tuple[str, str], str] = {}
    for scenario in scenarios:
        key = (scenario.name, scenario.version)
        if seen.setdefault(key, scenario.content_hash) != scenario.content_hash:
            raise _fail(f"scenario catalog: version collision for {key[0]}@{key[1]}")


def check_target_allowlisted(scenario: AssuranceScenario, allowlist: AssuranceTargetAllowlist) -> None:
    """Succeed (return None) only when the scenario's exact allowlist entry,
    in this exact allowlist version, admits the target's kind, ref and
    environment. Narrows scope; never authorizes an action."""
    target = scenario.target
    ref = target.allowlist_entry
    if ref is None:
        raise _fail("target: no allowlist_entry to check")
    if not allowlist.allowlist_id or ref.allowlist_id != allowlist.allowlist_id:
        raise _fail("target: allowlist_entry names a different allowlist version")
    entry = allowlist.entry(ref.entry_id)
    if entry is None:
        raise _fail(f"target: allowlist has no entry {ref.entry_id!r}")
    if entry.entry_hash != ref.content_hash:
        raise _fail("target: allowlist_entry content_hash does not match the entry")
    if (entry.target_kind, entry.ref) != (target.kind, target.ref):
        raise _fail("target: kind or ref differs from the allowlist entry")
    if target.environment not in entry.environments:
        raise _fail("target: environment is not listed by the allowlist entry")


def validate_followup(scenario: AssuranceScenario, proposal: FollowupProposal) -> FollowupTemplate:
    """The only path by which a model-proposed follow-up is accepted. Every
    parameter of the named template must be supplied, declared and in bounds."""
    _instance(proposal, FollowupProposal, where="followup")
    template = next((t for t in scenario.followup_templates if t.template_id == proposal.template_id), None)
    if template is None:
        raise _fail(f"followup: unknown template {proposal.template_id!r}")
    supplied = dict(proposal.parameters)
    declared = {p.name: p for p in template.parameters}
    extra = sorted(set(supplied) - set(declared))
    if extra:
        raise _fail(f"followup: undeclared parameter(s) {extra!r}")
    missing = sorted(set(declared) - set(supplied))
    if missing:
        raise _fail(f"followup: missing parameter(s) {missing!r}")
    for name, bound in declared.items():
        bound.check_value(supplied[name], scenario.fixture_refs)
    return template


# ---------------------------------------------------------------------------
# AssuranceRun
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AssuranceRun:
    _ID_PREFIX = RUN_ID_PREFIX

    binding: VersionBinding
    execution: ExecutionJoin
    mutation_mode: str
    granted_budgets: Budgets
    attempt: int
    started_at: str
    ended_at: str | None = None
    observations: tuple[ObservationRef, ...] = ()
    policy_decisions: tuple[PolicyDecisionRef, ...] = ()
    approvals: tuple[ApprovalRef, ...] = ()
    relation: str | None = None
    parent_run_id: str | None = None
    followup: FollowupProposal | None = None
    run_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        _instance(self.binding, VersionBinding, where="run.binding")
        _check_execution_join(self.execution)
        _one_of(self.mutation_mode, MUTATION_MODES, where="run.mutation_mode")
        _instance(self.granted_budgets, Budgets, where="run.granted_budgets")
        _int(self.attempt, where="run.attempt", lo=1, hi=MAX_ATTEMPTS_CAP)
        if self.attempt > self.granted_budgets.max_attempts:
            raise _fail("run: attempt exceeds max_attempts")
        _timestamp(self.started_at, where="run.started_at")
        if self.ended_at is not None:
            _timestamp(self.ended_at, where="run.ended_at")
        obs = _typed_seq(self, "observations", ObservationRef, max_items=MAX_OBSERVATIONS)
        _unique([o.observation_id for o in obs], where="run.observations")
        for ref in _typed_seq(self, "policy_decisions", PolicyDecisionRef, max_items=MAX_POLICY_DECISIONS):
            _check_policy_ref(ref)
        for ref in _typed_seq(self, "approvals", ApprovalRef, max_items=MAX_APPROVALS):
            _check_approval_ref(ref)
        _opt_str(self.relation, where="run.relation")
        if self.relation is not None:
            _one_of(self.relation, RELATIONS, where="run.relation")
        _opt_str(self.parent_run_id, where="run.parent_run_id", max_chars=32, pattern=_SEALED_ID_RES[RUN_ID_PREFIX])
        if self.followup is not None:
            _instance(self.followup, FollowupProposal, where="run.followup")
        if self.relation is None and (self.parent_run_id is not None or self.followup is not None):
            raise _fail("run: parent_run_id and followup require a relation")
        if self.relation is not None and self.parent_run_id is None:
            raise _fail("run: a followup or replay run requires parent_run_id")
        if self.relation == RELATION_FOLLOWUP and self.followup is None:
            raise _fail("run: a followup run requires a followup proposal")
        if self.relation == RELATION_REPLAY and self.followup is not None:
            raise _fail("run: a replay run carries no followup proposal")
        _verify_seal(self, "run_id", RUN_ID_PREFIX)

    def to_dict(self) -> dict[str, Any]:
        return _sealed_dict(self, KIND_RUN)

    @classmethod
    def from_dict(cls, data: Any) -> AssuranceRun:
        d = _envelope(data, KIND_RUN)
        _reject_unknown(d, _from_dict_keys(cls) | {"api_version", "kind"}, where=KIND_RUN)
        _require_sealed(d, "run_id", KIND_RUN)
        for key in ("binding", "execution", "granted_budgets"):
            if _g(d, key) is None:
                raise _fail(f"run.{key}: required")
        followup = _g(d, "followup")
        return cls(
            binding=VersionBinding.from_dict(d["binding"]),
            execution=_wrap(ExecutionJoin.from_dict, d["execution"]),
            mutation_mode=_g(d, "mutation_mode"),
            granted_budgets=Budgets.from_dict(d["granted_budgets"]),
            attempt=_g(d, "attempt"),
            started_at=_g(d, "started_at"),
            ended_at=_g(d, "ended_at"),
            observations=tuple(
                ObservationRef.from_dict(o)
                for o in _seq(_g(d, "observations"), where="observations", max_items=MAX_OBSERVATIONS)
            ),
            policy_decisions=tuple(
                _wrap(PolicyDecisionRef.from_dict, p)
                for p in _seq(_g(d, "policy_decisions"), where="policy_decisions", max_items=MAX_POLICY_DECISIONS)
            ),
            approvals=tuple(
                _wrap(ApprovalRef.from_dict, a)
                for a in _seq(_g(d, "approvals"), where="approvals", max_items=MAX_APPROVALS)
            ),
            relation=_g(d, "relation"),
            parent_run_id=_g(d, "parent_run_id"),
            followup=None if followup is None else FollowupProposal.from_dict(followup),
            run_id=_g(d, "run_id"),
            content_hash=_g(d, "content_hash"),
        )


def seal_run(**fields_: Any) -> AssuranceRun:
    return _seal(AssuranceRun(**fields_), "run_id", RUN_ID_PREFIX)


def _check_binding_matches_scenario(binding: VersionBinding, scenario: AssuranceScenario) -> None:
    ref = binding.scenario
    if (ref.scenario_id, ref.content_hash, ref.name, ref.version) != (
        scenario.scenario_id,
        scenario.content_hash,
        scenario.name,
        scenario.version,
    ):
        raise _fail("binding: scenario reference differs from the scenario")
    if (binding.target.kind, binding.target.ref) != (scenario.target.kind, scenario.target.ref):
        raise _fail("binding: target differs from the scenario target")


def check_run_against_scenario(run: AssuranceRun, scenario: AssuranceScenario) -> None:
    _check_binding_matches_scenario(run.binding, scenario)
    if run.mutation_mode != scenario.mutation_mode:
        raise _fail("run: mutation_mode differs from the scenario")
    if not run.granted_budgets.within(scenario.budgets):
        raise _fail("run: granted budget exceeds the scenario budget")
    if run.attempt > scenario.budgets.max_attempts:
        raise _fail("run: attempt exceeds the scenario max_attempts")
    known = {i.invariant_id for i in scenario.invariants}
    for obs in run.observations:
        if not set(obs.invariant_ids) <= known:
            raise _fail(f"run: observation {obs.observation_id!r} names an unknown invariant")
    if run.relation == RELATION_FOLLOWUP:
        if run.followup is None:
            raise _fail("run: a followup run requires a followup proposal")
        validate_followup(scenario, run.followup)


# ---------------------------------------------------------------------------
# AssuranceResult
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InvariantVerdict:
    invariant_id: str
    verdict: str
    observation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _str(self.invariant_id, where="verdict.invariant_id", pattern=_ITEM_ID_RE)
        _one_of(self.verdict, INVARIANT_VERDICTS, where="verdict.verdict")
        _str_seq(self, "observation_ids", max_items=MAX_OBSERVATIONS, pattern=_ITEM_ID_RE)
        if self.verdict != VERDICT_UNKNOWN and not self.observation_ids:
            raise _fail(f"verdict {self.invariant_id!r}: a {self.verdict} verdict needs observations")

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> InvariantVerdict:
        d = _mapping(data, where="verdict")
        _reject_unknown(d, _from_dict_keys(cls), where="verdict")
        return cls(
            invariant_id=_g(d, "invariant_id"), verdict=_g(d, "verdict"), observation_ids=_g(d, "observation_ids")
        )


@dataclass(frozen=True)
class AssuranceResult:
    _ID_PREFIX = RESULT_ID_PREFIX

    run_id: str
    binding: VersionBinding
    outcome: str
    invariant_verdicts: tuple[InvariantVerdict, ...]
    consumed: BudgetUsage
    evidence_ids: tuple[str, ...] = ()
    result_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        _str(self.run_id, where="result.run_id", max_chars=32, pattern=_SEALED_ID_RES[RUN_ID_PREFIX])
        _instance(self.binding, VersionBinding, where="result.binding")
        _one_of(self.outcome, OUTCOMES, where="result.outcome")
        _instance(self.consumed, BudgetUsage, where="result.consumed")
        verdicts = _typed_seq(self, "invariant_verdicts", InvariantVerdict, max_items=MAX_INVARIANTS, min_items=1)
        _unique([v.invariant_id for v in verdicts], where="result.invariant_verdicts")
        _str_seq(self, "evidence_ids", max_items=MAX_EVIDENCE_IDS, pattern=_EVIDENCE_ID_RE, max_chars=19)
        kinds = {v.verdict for v in verdicts}
        if self.outcome == OUTCOME_HELD and kinds != {VERDICT_HELD}:
            raise _fail("result: invariant_held requires every verdict to be held")
        if self.outcome == OUTCOME_VIOLATED and VERDICT_VIOLATED not in kinds:
            raise _fail("result: invariant_violated requires a violated verdict")
        if self.outcome != OUTCOME_VIOLATED and VERDICT_VIOLATED in kinds and self.outcome != "inconclusive":
            raise _fail("result: a violated verdict requires outcome invariant_violated or inconclusive")
        _verify_seal(self, "result_id", RESULT_ID_PREFIX)

    def to_dict(self) -> dict[str, Any]:
        return _sealed_dict(self, KIND_RESULT)

    @classmethod
    def from_dict(cls, data: Any) -> AssuranceResult:
        d = _envelope(data, KIND_RESULT)
        _reject_unknown(d, _from_dict_keys(cls) | {"api_version", "kind"}, where=KIND_RESULT)
        _require_sealed(d, "result_id", KIND_RESULT)
        for key in ("binding", "consumed"):
            if _g(d, key) is None:
                raise _fail(f"result.{key}: required")
        return cls(
            run_id=_g(d, "run_id"),
            binding=VersionBinding.from_dict(d["binding"]),
            outcome=_g(d, "outcome"),
            invariant_verdicts=tuple(
                InvariantVerdict.from_dict(v)
                for v in _seq(_g(d, "invariant_verdicts"), where="invariant_verdicts", max_items=MAX_INVARIANTS)
            ),
            consumed=BudgetUsage.from_dict(d["consumed"]),
            evidence_ids=_g(d, "evidence_ids"),
            result_id=_g(d, "result_id"),
            content_hash=_g(d, "content_hash"),
        )


def seal_result(**fields_: Any) -> AssuranceResult:
    return _seal(AssuranceResult(**fields_), "result_id", RESULT_ID_PREFIX)


def check_result_against_run(result: AssuranceResult, run: AssuranceRun, scenario: AssuranceScenario) -> None:
    """Observation honesty: only an `observed` observation may support
    `held` or `violated`; anything else forces `unknown`."""
    if result.run_id != run.run_id:
        raise _fail("result: run_id differs from the run")
    if result.binding != run.binding:
        raise _fail("result: binding differs from the run binding")
    _check_binding_matches_scenario(result.binding, scenario)
    if {v.invariant_id for v in result.invariant_verdicts} != {i.invariant_id for i in scenario.invariants}:
        raise _fail("result: verdicts must cover exactly the scenario invariants")
    by_id = {o.observation_id: o for o in run.observations}
    for verdict in result.invariant_verdicts:
        if verdict.verdict == VERDICT_UNKNOWN:
            continue
        observed = []
        for oid in verdict.observation_ids:
            obs = by_id.get(oid)
            if obs is None or verdict.invariant_id not in obs.invariant_ids:
                raise _fail(f"result: verdict {verdict.invariant_id!r} cites unknown observation {oid!r}")
            if obs.status == OBS_OBSERVED:
                observed.append(obs)
            elif verdict.verdict == VERDICT_HELD:
                raise _fail(f"result: {verdict.invariant_id!r} cannot be held on a {obs.status} observation")
        if not observed:
            raise _fail(f"result: {verdict.invariant_id!r} has no observed support")
    if not result.consumed.within(run.granted_budgets) and result.outcome != OUTCOME_BUDGET_EXHAUSTED:
        raise _fail("result: consumed exceeds the granted budget but the outcome is not budget_exhausted")
    known_evidence = {o.evidence_id for o in run.observations if o.evidence_id}
    if not set(result.evidence_ids) <= known_evidence:
        raise _fail("result: evidence_ids cite evidence the run did not observe")


# ---------------------------------------------------------------------------
# AssuranceFinding
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FindingReporter:
    authority: str
    producer: str

    def __post_init__(self) -> None:
        _one_of(self.authority, frozenset(AUTHORITIES), where="reported_by.authority")
        _str(self.producer, where="reported_by.producer", pattern=_SLUG_RE)

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> FindingReporter:
        d = _mapping(data, where="reported_by")
        _reject_unknown(d, _from_dict_keys(cls), where="reported_by")
        return cls(authority=_g(d, "authority"), producer=_g(d, "producer"))


@dataclass(frozen=True)
class FindingValidation:
    replay_run_id: str
    replay_result_id: str
    authority: str

    def __post_init__(self) -> None:
        _str(self.replay_run_id, where="validation.replay_run_id", max_chars=32, pattern=_SEALED_ID_RES[RUN_ID_PREFIX])
        _str(
            self.replay_result_id,
            where="validation.replay_result_id",
            max_chars=32,
            pattern=_SEALED_ID_RES[RESULT_ID_PREFIX],
        )
        _one_of(self.authority, VALIDATION_AUTHORITIES, where="validation.authority")

    def to_dict(self) -> dict[str, Any]:
        return _encode(self)

    @classmethod
    def from_dict(cls, data: Any) -> FindingValidation:
        d = _mapping(data, where="validation")
        _reject_unknown(d, _from_dict_keys(cls), where="validation")
        return cls(**{k: _g(d, k) for k in _from_dict_keys(cls)})


@dataclass(frozen=True)
class AssuranceFinding:
    _ID_PREFIX = FINDING_ID_PREFIX

    binding: VersionBinding
    origin_run_id: str
    origin_result_id: str
    invariant_id: str
    state: str
    reported_by: FindingReporter
    summary: str
    evidence_ids: tuple[str, ...] = ()
    validation: FindingValidation | None = None
    disposition: str | None = None
    disposition_note: str | None = None
    supersedes: str | None = None
    finding_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        _instance(self.binding, VersionBinding, where="finding.binding")
        _str(self.origin_run_id, where="finding.origin_run_id", max_chars=32, pattern=_SEALED_ID_RES[RUN_ID_PREFIX])
        _str(
            self.origin_result_id,
            where="finding.origin_result_id",
            max_chars=32,
            pattern=_SEALED_ID_RES[RESULT_ID_PREFIX],
        )
        _str(self.invariant_id, where="finding.invariant_id", pattern=_ITEM_ID_RE)
        _one_of(self.state, FINDING_STATES, where="finding.state")
        _instance(self.reported_by, FindingReporter, where="finding.reported_by")
        _str(self.summary, where="finding.summary", max_chars=MAX_SUMMARY_CHARS)
        if self.summary != redact_text(self.summary, max_chars=MAX_SUMMARY_CHARS):
            raise _fail("finding.summary: not normalised; seal through seal_finding")
        _str_seq(self, "evidence_ids", max_items=MAX_EVIDENCE_IDS, pattern=_EVIDENCE_ID_RE, max_chars=19)
        if self.validation is not None:
            _instance(self.validation, FindingValidation, where="finding.validation")
        if self.disposition is not None:
            _one_of(self.disposition, DISPOSITIONS, where="finding.disposition")
        if self.disposition_note is not None:
            _str(self.disposition_note, where="finding.disposition_note", max_chars=MAX_NOTE_CHARS)
            if self.disposition_note != redact_text(self.disposition_note, max_chars=MAX_NOTE_CHARS):
                raise _fail("finding.disposition_note: not normalised; seal through seal_finding")
        _opt_str(self.supersedes, where="finding.supersedes", max_chars=32, pattern=_SEALED_ID_RES[FINDING_ID_PREFIX])
        if self.state == STATE_CANDIDATE:
            if self.validation is not None or self.disposition is not None or self.disposition_note is not None:
                raise _fail("finding: a candidate carries no validation or disposition")
        else:
            if self.supersedes is None:
                raise _fail("finding: a confirmed or rejected revision must supersede the candidate")
        if self.state == STATE_CONFIRMED:
            if self.validation is None:
                raise _fail("finding: a confirmed finding requires a validation block")
            if self.validation.replay_run_id == self.origin_run_id:
                raise _fail("finding: the replay run must differ from the originating run")
            if self.validation.replay_result_id == self.origin_result_id:
                raise _fail("finding: the replay result must differ from the originating result")
            if self.disposition is not None or self.disposition_note is not None:
                raise _fail("finding: a confirmed finding carries no disposition")
        if self.state == STATE_REJECTED:
            if self.disposition is None:
                raise _fail("finding: a rejected finding requires a disposition")
            if self.validation is not None:
                raise _fail("finding: a rejected finding carries no validation block")
        _verify_seal(self, "finding_id", FINDING_ID_PREFIX)

    def to_dict(self) -> dict[str, Any]:
        return _sealed_dict(self, KIND_FINDING)

    @classmethod
    def from_dict(cls, data: Any) -> AssuranceFinding:
        d = _envelope(data, KIND_FINDING)
        _reject_unknown(d, _from_dict_keys(cls) | {"api_version", "kind"}, where=KIND_FINDING)
        _require_sealed(d, "finding_id", KIND_FINDING)
        for key in ("binding", "reported_by"):
            if _g(d, key) is None:
                raise _fail(f"finding.{key}: required")
        validation = _g(d, "validation")
        return cls(
            binding=VersionBinding.from_dict(d["binding"]),
            origin_run_id=_g(d, "origin_run_id"),
            origin_result_id=_g(d, "origin_result_id"),
            invariant_id=_g(d, "invariant_id"),
            state=_g(d, "state"),
            reported_by=FindingReporter.from_dict(d["reported_by"]),
            summary=_g(d, "summary"),
            evidence_ids=_g(d, "evidence_ids"),
            validation=None if validation is None else FindingValidation.from_dict(validation),
            disposition=_g(d, "disposition"),
            disposition_note=_g(d, "disposition_note"),
            supersedes=_g(d, "supersedes"),
            finding_id=_g(d, "finding_id"),
            content_hash=_g(d, "content_hash"),
        )


def seal_finding(**fields_: Any) -> AssuranceFinding:
    """Seal a finding. Model-authored text is redacted and bounded first."""
    if "summary" in fields_:
        fields_["summary"] = redact_text(fields_["summary"], max_chars=MAX_SUMMARY_CHARS)
    if fields_.get("disposition_note") is not None:
        fields_["disposition_note"] = redact_text(fields_["disposition_note"], max_chars=MAX_NOTE_CHARS)
    return _seal(AssuranceFinding(**fields_), "finding_id", FINDING_ID_PREFIX)


def check_finding_confirmation(
    finding: AssuranceFinding, replay_run: AssuranceRun | None, replay_result: AssuranceResult | None
) -> str:
    """Check that a replay confirms `finding`; return the validation authority
    (`observed` or `derived`). Raises `RuntimeAssuranceError` otherwise."""
    if replay_run is None or replay_result is None:
        raise _fail("finding: confirmation requires a replay run and result")
    if replay_result.result_id == finding.origin_result_id:
        raise _fail("finding: the replay result must differ from the originating result")
    if replay_run.run_id == finding.origin_run_id:
        raise _fail("finding: the replay run must differ from the originating run")
    if replay_run.relation != RELATION_REPLAY or replay_run.parent_run_id != finding.origin_run_id:
        raise _fail("finding: the replay run must be a replay of the originating run")
    if replay_run.binding != finding.binding or replay_result.binding != finding.binding:
        raise _fail("finding: the replay is bound to a different scenario, runtime or target revision")
    if replay_result.run_id != replay_run.run_id:
        raise _fail("finding: the replay result belongs to a different run")
    verdict = next((v for v in replay_result.invariant_verdicts if v.invariant_id == finding.invariant_id), None)
    if replay_result.outcome != OUTCOME_VIOLATED or verdict is None or verdict.verdict != VERDICT_VIOLATED:
        raise _fail("finding: the replay did not violate the same invariant")
    # The same run-consistency rule `check_result_against_run` applies: every
    # observation the verdict cites exists in the replay run and is about the
    # finding's invariant. Evidence about another invariant never confirms.
    by_id = {o.observation_id: o for o in replay_run.observations}
    for oid in verdict.observation_ids:
        obs = by_id.get(oid)
        if obs is None or finding.invariant_id not in obs.invariant_ids:
            raise _fail(f"finding: the replay verdict cites observation {oid!r}, which is not about the invariant")
    known_evidence = {o.evidence_id for o in replay_run.observations if o.evidence_id}
    if not set(replay_result.evidence_ids) <= known_evidence:
        raise _fail("finding: the replay result cites evidence the replay run did not observe")
    backing = [
        by_id[i]
        for i in verdict.observation_ids
        if by_id[i].status == OBS_OBSERVED and by_id[i].authority != "asserted"
    ]
    if not backing:
        raise _fail("finding: the replay has no observed, non-asserted support")
    return "observed" if any(o.authority == "observed" for o in backing) else "derived"


def check_finding_against_result(
    finding: AssuranceFinding, result: AssuranceResult, scenario: AssuranceScenario
) -> None:
    """Cross-record check of a finding against its originating result: the
    invariant is one of the scenario's, and the result and evidence are the
    origin's."""
    if finding.binding != result.binding:
        raise _fail("finding: binding differs from the originating result")
    if finding.origin_result_id != result.result_id or finding.origin_run_id != result.run_id:
        raise _fail("finding: origin ids differ from the originating result")
    if finding.invariant_id not in {i.invariant_id for i in scenario.invariants}:
        raise _fail("finding: invariant_id is not an invariant of the scenario")
    if not set(finding.evidence_ids) <= set(result.evidence_ids):
        raise _fail("finding: evidence_ids are not a subset of the originating result's")


def transition_finding(
    finding: AssuranceFinding,
    to_state: str,
    *,
    replay_run: AssuranceRun | None = None,
    replay_result: AssuranceResult | None = None,
    disposition: str | None = None,
    disposition_note: str | None = None,
) -> AssuranceFinding:
    """The only lifecycle API: `candidate -> confirmed | rejected`. The new
    revision's `supersedes` names the previous `finding_id`; both target states
    are terminal."""
    if finding.state != STATE_CANDIDATE:
        raise _fail(f"finding: {finding.state} is terminal and cannot transition")
    if not finding.finding_id:
        raise _fail("finding: only a sealed finding can transition")
    common = {f.name: getattr(finding, f.name) for f in fields(finding) if f.name not in ("finding_id", "content_hash")}
    common["supersedes"] = finding.finding_id
    if to_state == STATE_REJECTED:
        if disposition is None:
            raise _fail("finding: rejection requires a disposition")
        return seal_finding(
            **{**common, "state": STATE_REJECTED, "disposition": disposition, "disposition_note": disposition_note}
        )
    if to_state != STATE_CONFIRMED:
        raise _fail(f"finding: cannot transition to {to_state!r}")
    authority = check_finding_confirmation(finding, replay_run, replay_result)
    return seal_finding(
        **{
            **common,
            "state": STATE_CONFIRMED,
            "validation": FindingValidation(
                replay_run_id=replay_run.run_id,  # type: ignore[union-attr]
                replay_result_id=replay_result.result_id,  # type: ignore[union-attr]
                authority=authority,
            ),
        }
    )
