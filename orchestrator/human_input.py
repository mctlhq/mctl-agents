"""`HumanInputRequest`/`HumanInputResponse` — the versioned, hashed contract
for the durable agent-clarification primitive (mctlhq/mctl-agents#333, ADR
011: docs/adr/011-human-input-contract.md).

Modelled line for line on `orchestrator/context_snapshot.py`'s hashing,
versioning and unknown-key-rejection conventions, so the two validate and
hash the same way and a reader who knows one already knows the other.

Stdlib only, deliberately: this module is imported both by the short-lived
agent sandbox (`run_issue_investigator.py`) and by the long-lived Temporal
worker (`orchestrator/temporal/workflows/dev_loop.py`, where
`validate_response` runs as pure workflow code, never as an activity — ADR
010 sec. 9's rule that I/O lives in activities, not workflows). No network
call, no filesystem access, no SDK import.

A response is data with provenance, never a capability. Nothing in this
module grants, authorizes or approves anything — that is `approve()`
(`orchestrator/temporal/workflows/dev_loop.py`) and the profile's
`approval.requiredBefore` gate, entirely untouched by this contract.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from orchestrator.context_snapshot import ExecutionCorrelation

API_VERSION = "humaninput.mctl.ai/v1alpha1"
REQUEST_KIND = "HumanInputRequest"
RESPONSE_KIND = "HumanInputResponse"

# The complete allow-list, mirroring context_snapshot.SUPPORTED_API_VERSIONS:
# a document declaring anything else fails loudly, never falls back to a
# default shape.
SUPPORTED_API_VERSIONS = {API_VERSION: (REQUEST_KIND, RESPONSE_KIND)}

RESPONSE_TYPES = frozenset({"free_text", "single_choice", "multi_choice", "structured"})
AUDIENCES = frozenset({"work_item_owner", "repo_operators", "tenant_operators"})
# A context_refs entry must start with one of these — a locator, never a
# place to smuggle a raw payload into the request.
CONTEXT_REF_PREFIXES = ("github:", "gitops-file:", "context_snapshot:", "evidence:")

DEFAULT_REQUEST_TTL_SECONDS = 86400
MAX_REQUEST_TTL_SECONDS = 604800
MAX_CLARIFICATION_ROUNDS = 3
MAX_OUTSTANDING_REQUESTS_PER_EXECUTION = 1


class HumanInputError(ValueError):
    """Fail-closed schema/validation failure. Every raise site is either a
    structural problem (`from_dict`: wrong type, unknown key, unsupported
    `api_version`/`kind`) or a semantic one (`seal_request`/`validate_response`:
    closed vocabulary violation, expiry, cardinality, authorization).
    Non-retryable: callers fix the document, never catch this to fall back to
    a default shape."""


def _hash_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_json(payload: Any) -> bytes:
    try:
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HumanInputError(f"payload is not JSON-serializable: {exc}") from exc


def _reject_unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise HumanInputError(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _require_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HumanInputError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _require_str(value: Any, *, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise HumanInputError(f"{where} must be a non-empty string")
    return value


def _require_int(value: Any, *, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise HumanInputError(f"{where} must be an int")
    return value


def _optional_str(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where=where)


def _require_str_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise HumanInputError(f"{where} must be a list of strings")
    return tuple(value)


# ---------------------------------------------------------------------------
# Leaf value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponseSpec:
    """What shape of answer a request expects. `type` is a closed vocabulary
    (`RESPONSE_TYPES`); `options` is required and non-empty for the two
    choice types, and ignored (but preserved) otherwise."""

    type: str
    options: tuple[str, ...] = ()
    schema_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "options": list(self.options), "schema_ref": self.schema_ref}

    @classmethod
    def from_dict(cls, data: Any) -> ResponseSpec:
        mapping = _require_mapping(data, where="response")
        _reject_unknown_keys(mapping, frozenset({"type", "options", "schema_ref"}), where="response")
        type_ = _require_str(mapping.get("type"), where="response.type")
        options = _require_str_tuple(mapping.get("options", []), where="response.options")
        schema_ref = _optional_str(mapping.get("schema_ref"), where="response.schema_ref")
        spec = cls(type=type_, options=options, schema_ref=schema_ref)
        spec.validate()
        return spec

    def validate(self) -> None:
        if self.type not in RESPONSE_TYPES:
            raise HumanInputError(f"response.type {self.type!r} is not one of {sorted(RESPONSE_TYPES)!r}")
        if self.type in ("single_choice", "multi_choice") and not self.options:
            raise HumanInputError(f"response.type {self.type!r} requires a non-empty options list")


@dataclass(frozen=True)
class RequestedFrom:
    """Who may answer. `audience` is a closed vocabulary (`AUDIENCES`);
    `actor_refs` is the explicit allow-list `validate_response` checks a
    respondent against. Richer role resolution belongs to mctl-api#261."""

    audience: str
    actor_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"audience": self.audience, "actor_refs": list(self.actor_refs)}

    @classmethod
    def from_dict(cls, data: Any) -> RequestedFrom:
        mapping = _require_mapping(data, where="requested_from")
        _reject_unknown_keys(mapping, frozenset({"audience", "actor_refs"}), where="requested_from")
        audience = _require_str(mapping.get("audience"), where="requested_from.audience")
        actor_refs = _require_str_tuple(mapping.get("actor_refs", []), where="requested_from.actor_refs")
        requested_from = cls(audience=audience, actor_refs=actor_refs)
        requested_from.validate()
        return requested_from

    def validate(self) -> None:
        if self.audience not in AUDIENCES:
            raise HumanInputError(f"requested_from.audience {self.audience!r} is not one of {sorted(AUDIENCES)!r}")


@dataclass(frozen=True)
class Respondent:
    """Identity of whoever answered. A reference, never a transcript or a
    surface-specific payload."""

    actor_type: str
    actor_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"actor_type": self.actor_type, "actor_id": self.actor_id}

    @classmethod
    def from_dict(cls, data: Any) -> Respondent:
        mapping = _require_mapping(data, where="respondent")
        _reject_unknown_keys(mapping, frozenset({"actor_type", "actor_id"}), where="respondent")
        return cls(
            actor_type=_require_str(mapping.get("actor_type"), where="respondent.actor_type"),
            actor_id=_require_str(mapping.get("actor_id"), where="respondent.actor_id"),
        )

    def reference(self) -> str:
        """`actor_type:actor_id` — the form `requested_from.actor_refs`
        entries and `question_hash_for`'s audience keys are compared in."""
        return f"{self.actor_type}:{self.actor_id}"


# ---------------------------------------------------------------------------
# HumanInputRequest / HumanInputResponse — the top-level documents
# ---------------------------------------------------------------------------

_REQUEST_KEYS = frozenset({
    "api_version", "kind", "request_id", "request_hash", "question_hash",
    "request_version", "created_at", "expires_at", "work_item_id", "execution",
    "question", "reason", "response", "requested_from", "context_refs", "round",
})


@dataclass(frozen=True)
class HumanInputRequest:
    """One immutable, content-addressed clarification question. Only ever
    produced by `seal_request()`; `from_dict` reconstructs an already-sealed
    document and re-validates its shape, but never recomputes the hash."""

    api_version: str
    kind: str
    request_id: str
    request_hash: str
    question_hash: str
    request_version: int
    created_at: str
    expires_at: str
    work_item_id: str
    execution: ExecutionCorrelation
    question: str
    reason: str
    response: ResponseSpec
    requested_from: RequestedFrom
    context_refs: tuple[str, ...] = ()
    round: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "question_hash": self.question_hash,
            "request_version": self.request_version,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "work_item_id": self.work_item_id,
            "execution": self.execution.to_dict(),
            "question": self.question,
            "reason": self.reason,
            "response": self.response.to_dict(),
            "requested_from": self.requested_from.to_dict(),
            "context_refs": list(self.context_refs),
            "round": self.round,
        }

    @classmethod
    def from_dict(cls, data: Any) -> HumanInputRequest:
        mapping = _require_mapping(data, where="HumanInputRequest")
        _reject_unknown_keys(mapping, _REQUEST_KEYS, where="HumanInputRequest")

        api_version_raw = mapping.get("api_version")
        kinds = SUPPORTED_API_VERSIONS.get(api_version_raw) if isinstance(api_version_raw, str) else None
        if not isinstance(api_version_raw, str) or kinds is None:
            raise HumanInputError(
                f"unsupported api_version {api_version_raw!r}, expected one of {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        kind_raw = mapping.get("kind")
        if kind_raw != REQUEST_KIND:
            raise HumanInputError(f"kind must be {REQUEST_KIND!r}, got {kind_raw!r}")

        context_refs = _require_str_tuple(mapping.get("context_refs", []), where="context_refs")
        for ref in context_refs:
            if not ref.startswith(CONTEXT_REF_PREFIXES):
                raise HumanInputError(
                    f"context_refs entry {ref!r} does not start with one of {CONTEXT_REF_PREFIXES!r}"
                )

        request = cls(
            api_version=api_version_raw,
            kind=kind_raw,
            request_id=_require_str(mapping.get("request_id"), where="request_id"),
            request_hash=_require_str(mapping.get("request_hash"), where="request_hash"),
            question_hash=_require_str(mapping.get("question_hash"), where="question_hash"),
            request_version=_require_int(mapping.get("request_version"), where="request_version"),
            created_at=_require_str(mapping.get("created_at"), where="created_at"),
            expires_at=_require_str(mapping.get("expires_at"), where="expires_at"),
            work_item_id=_require_str(mapping.get("work_item_id"), where="work_item_id"),
            execution=ExecutionCorrelation.from_dict(mapping.get("execution")),
            question=_require_str(mapping.get("question"), where="question"),
            reason=_require_str(mapping.get("reason"), where="reason"),
            response=ResponseSpec.from_dict(mapping.get("response")),
            requested_from=RequestedFrom.from_dict(mapping.get("requested_from")),
            context_refs=context_refs,
            round=_require_int(mapping.get("round", 1), where="round"),
        )
        request.validate()
        return request

    def validate(self) -> None:
        if self.api_version != API_VERSION:
            raise HumanInputError(f"api_version must be {API_VERSION!r}, got {self.api_version!r}")
        if self.kind != REQUEST_KIND:
            raise HumanInputError(f"kind must be {REQUEST_KIND!r}, got {self.kind!r}")
        if not self.request_hash.startswith("sha256:"):
            raise HumanInputError(f"request_hash must carry the 'sha256:' prefix, got {self.request_hash!r}")
        if self.round < 1:
            raise HumanInputError(f"round must be >= 1, got {self.round}")
        self.response.validate()
        self.requested_from.validate()
        for ref in self.context_refs:
            if not ref.startswith(CONTEXT_REF_PREFIXES):
                raise HumanInputError(
                    f"context_refs entry {ref!r} does not start with one of {CONTEXT_REF_PREFIXES!r}"
                )


_RESPONSE_KEYS = frozenset({
    "api_version", "kind", "request_id", "request_hash", "respondent",
    "surface", "value", "received_at",
})


@dataclass(frozen=True)
class HumanInputResponse:
    """One answer to a `HumanInputRequest`. Data with provenance — never a
    capability, never an approval (see this module's docstring)."""

    api_version: str
    kind: str
    request_id: str
    request_hash: str
    respondent: Respondent
    surface: str
    value: Any
    received_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "respondent": self.respondent.to_dict(),
            "surface": self.surface,
            "value": self.value,
            "received_at": self.received_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> HumanInputResponse:
        mapping = _require_mapping(data, where="HumanInputResponse")
        _reject_unknown_keys(mapping, _RESPONSE_KEYS, where="HumanInputResponse")

        api_version_raw = mapping.get("api_version")
        kinds = SUPPORTED_API_VERSIONS.get(api_version_raw) if isinstance(api_version_raw, str) else None
        if not isinstance(api_version_raw, str) or kinds is None:
            raise HumanInputError(
                f"unsupported api_version {api_version_raw!r}, expected one of {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        kind_raw = mapping.get("kind")
        if kind_raw != RESPONSE_KIND:
            raise HumanInputError(f"kind must be {RESPONSE_KIND!r}, got {kind_raw!r}")
        if "value" not in mapping:
            raise HumanInputError("value is required")

        return cls(
            api_version=api_version_raw,
            kind=kind_raw,
            request_id=_require_str(mapping.get("request_id"), where="request_id"),
            request_hash=_require_str(mapping.get("request_hash"), where="request_hash"),
            respondent=Respondent.from_dict(mapping.get("respondent")),
            surface=_require_str(mapping.get("surface"), where="surface"),
            value=mapping.get("value"),
            received_at=_require_str(mapping.get("received_at"), where="received_at"),
        )


# ---------------------------------------------------------------------------
# seal_request / question_hash_for / validate_response
# ---------------------------------------------------------------------------


def _content_payload(
    *,
    work_item_id: str,
    execution: ExecutionCorrelation,
    expires_at: str,
    question: str,
    reason: str,
    response: ResponseSpec,
    requested_from: RequestedFrom,
    context_refs: tuple[str, ...],
    round: int,
    request_version: int,
) -> dict[str, Any]:
    """Every field that participates in `request_hash` — everything except
    `request_id`, `request_hash` and `created_at`."""
    return {
        "api_version": API_VERSION,
        "kind": REQUEST_KIND,
        "request_version": request_version,
        "expires_at": expires_at,
        "work_item_id": work_item_id,
        "execution": execution.to_dict(),
        "question": question,
        "reason": reason,
        "response": response.to_dict(),
        "requested_from": requested_from.to_dict(),
        "context_refs": list(context_refs),
        "round": round,
    }


_WHITESPACE_RE = re.compile(r"\s+")


def question_hash_for(question: str, response: ResponseSpec) -> str:
    """The dedupe key: `sha256` over the whitespace-collapsed, case-folded
    question plus the canonical JSON of the response spec. Deliberately NOT
    `request_hash` — that includes round- and correlation-specific fields, so
    two retries of the same ambiguity share a `question_hash` even when
    `round` differs."""
    normalized_question = _WHITESPACE_RE.sub(" ", question).strip().casefold()
    payload = {"question": normalized_question, "response": response.to_dict()}
    return _hash_bytes(_canonical_json(payload))


def seal_request(
    *,
    work_item_id: str,
    execution: ExecutionCorrelation,
    question: str,
    reason: str,
    response: ResponseSpec,
    requested_from: RequestedFrom,
    created_at: str,
    expires_at: str,
    context_refs: tuple[str, ...] = (),
    round: int = 1,
    request_version: int = 1,
) -> HumanInputRequest:
    """The only constructor that produces a sealed `HumanInputRequest`.

    Computes `request_hash = "sha256:" + sha256(canonical JSON of every
    field except request_id, request_hash and created_at)`, then
    `request_id = "hir-" + request_hash[7:23]` — the same rule as
    `context_snapshot.seal()`. `created_at` is caller-supplied and excluded
    from the hash, so sealing identical inputs twice at different wall-clock
    times yields the same identity: a retried Argo step is idempotent
    instead of duplicative.

    Raises `HumanInputError` if the assembled document is not internally
    consistent (empty question/reason, bad response spec, expiry outside
    (created_at, created_at + MAX_REQUEST_TTL_SECONDS], a context_refs entry
    without an allowed prefix); never returns a partially-sealed request.
    """
    question = _require_str(question, where="question")
    reason = _require_str(reason, where="reason")
    response.validate()
    requested_from.validate()
    for ref in context_refs:
        if not ref.startswith(CONTEXT_REF_PREFIXES):
            raise HumanInputError(f"context_refs entry {ref!r} does not start with one of {CONTEXT_REF_PREFIXES!r}")
    if round < 1:
        raise HumanInputError(f"round must be >= 1, got {round}")

    created_dt = _parse_iso(created_at, where="created_at")
    expires_dt = _parse_iso(expires_at, where="expires_at")
    if expires_dt <= created_dt:
        raise HumanInputError("expires_at must be strictly after created_at")
    if (expires_dt - created_dt).total_seconds() > MAX_REQUEST_TTL_SECONDS:
        raise HumanInputError(
            f"expires_at is more than MAX_REQUEST_TTL_SECONDS ({MAX_REQUEST_TTL_SECONDS}s) after created_at"
        )

    question_hash = question_hash_for(question, response)
    payload = _content_payload(
        work_item_id=work_item_id,
        execution=execution,
        expires_at=expires_at,
        question=question,
        reason=reason,
        response=response,
        requested_from=requested_from,
        context_refs=context_refs,
        round=round,
        request_version=request_version,
    )
    request_hash = _hash_bytes(_canonical_json(payload))
    request_id = "hir-" + request_hash[7:23]
    request = HumanInputRequest(
        api_version=API_VERSION,
        kind=REQUEST_KIND,
        request_id=request_id,
        request_hash=request_hash,
        question_hash=question_hash,
        request_version=request_version,
        created_at=created_at,
        expires_at=expires_at,
        work_item_id=work_item_id,
        execution=execution,
        question=question,
        reason=reason,
        response=response,
        requested_from=requested_from,
        context_refs=tuple(context_refs),
        round=round,
    )
    request.validate()
    return request


def _parse_iso(value: str, *, where: str) -> datetime:
    if not isinstance(value, str):
        raise HumanInputError(f"{where} must be an ISO-8601 string, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HumanInputError(f"{where} is not a valid ISO-8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def validate_response(
    request: HumanInputRequest, response: HumanInputResponse, *, now: datetime | str
) -> None:
    """Raise `HumanInputError` unless `response` may resume `request` right
    now. Pure — no I/O, no clock read (`now` is caller-supplied) — so this
    is legal to call from Temporal workflow code (ADR 010 sec. 9).

    Checks, in order: `request_id` match, `request_hash` match (exact),
    `now < expires_at`, `respondent` inside `requested_from.actor_refs`, and
    `value` satisfies `request.response` (declared options for a
    choice-typed request, non-empty for free_text, list cardinality for
    multi_choice).
    """
    if response.request_id != request.request_id:
        raise HumanInputError(
            f"response.request_id {response.request_id!r} does not match request_id {request.request_id!r}"
        )
    if response.request_hash != request.request_hash:
        raise HumanInputError("response.request_hash does not match the request's hash")

    expires_dt = _parse_iso(request.expires_at, where="expires_at")
    now_dt = now if getattr(now, "tzinfo", None) is not None else _parse_iso(str(now), where="now")
    if now_dt >= expires_dt:
        raise HumanInputError(f"request {request.request_id} expired at {request.expires_at}")

    if response.respondent.reference() not in request.requested_from.actor_refs:
        raise HumanInputError(
            f"respondent {response.respondent.reference()!r} is not in the requested_from audience "
            f"{list(request.requested_from.actor_refs)!r}"
        )

    _validate_value(request.response, response.value)


def _validate_value(spec: ResponseSpec, value: Any) -> None:
    if spec.type == "free_text":
        if not isinstance(value, str) or not value.strip():
            raise HumanInputError("free_text response value must be a non-empty string")
    elif spec.type == "single_choice":
        if value not in spec.options:
            raise HumanInputError(f"single_choice value {value!r} is not one of {list(spec.options)!r}")
    elif spec.type == "multi_choice":
        if not isinstance(value, (list, tuple)) or not value:
            raise HumanInputError("multi_choice response value must be a non-empty list")
        unknown = [v for v in value if v not in spec.options]
        if unknown:
            raise HumanInputError(f"multi_choice value contains options not in {list(spec.options)!r}: {unknown!r}")
    elif spec.type == "structured":
        if not isinstance(value, Mapping):
            raise HumanInputError("structured response value must be an object")
    else:  # pragma: no cover - closed by ResponseSpec.validate at seal/parse time
        raise HumanInputError(f"unknown response.type {spec.type!r}")


# ---------------------------------------------------------------------------
# Safe telemetry projections
# ---------------------------------------------------------------------------


def request_log_dict(request: HumanInputRequest) -> dict[str, Any]:
    """Trace/telemetry-export shape: ids, hashes, versions, correlation,
    audience, round, expiry — and **never** `question` or `reason`."""
    return {
        "request_id": request.request_id,
        "request_hash": request.request_hash,
        "question_hash": request.question_hash,
        "request_version": request.request_version,
        "work_item_id": request.work_item_id,
        "temporal_workflow_id": request.execution.temporal_workflow_id,
        "temporal_run_id": request.execution.temporal_run_id,
        "argo_workflow_name": request.execution.argo_workflow_name,
        "agent": request.execution.agent,
        "profile_version": request.execution.profile_version,
        "audience": request.requested_from.audience,
        "response_type": request.response.type,
        "round": request.round,
        "expires_at": request.expires_at,
        "created_at": request.created_at,
    }


def response_log_dict(response: HumanInputResponse) -> dict[str, Any]:
    """Trace/telemetry-export shape for a response: ids, hashes, surface,
    respondent reference, timestamp — and **never** `value`."""
    return {
        "request_id": response.request_id,
        "request_hash": response.request_hash,
        "respondent": response.respondent.reference(),
        "surface": response.surface,
        "received_at": response.received_at,
    }
