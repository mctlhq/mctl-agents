"""`ImplementationCompletion` -- the typed, sealed, versioned record of how one
implementer attempt step ended (mctlhq/mctl-agents#428, ADR 021:
docs/adr/021-implementation-completion-contract.md).

This is an implementer-phase completion contract -- not an independent review
verdict, not an approval, and not another DevLoop. It describes how the
existing implement / review-remediation phase ended, bound to the identities
that make the statement checkable: the Argo run (`run_id`) and step
(`primary` | `fallback`), the attempt, the execution and claim, the proposal
content, and the pushed head.

What `complete` means in v1alpha1: **the attempt pushed a head that passed the
orchestrator gates** (`commits_present`, `chart_major_guard`,
`push_succeeded`, `claim_valid_at_push`). It does NOT mean "the approved scope
is implemented": no check yet compares the pushed head with the approved
`tasks.md`. That task-coverage verification is named follow-up F1 (a future
`task_coverage` check, `required` in a later api version). The stop-reason
code for `complete` is therefore `gates_passed`, never `scope_done`.

The module is inert: stdlib plus the repo's one hashing rule
(`orchestrator.context_snapshot`) and credential screen
(`orchestrator.redaction`). It imports nothing from `run_implementer` or
`run_shepherd`, does no I/O, and defines no field that could express approval,
a review verdict, merge, release, an ownership change, or a claim / budget
extension; `from_dict` rejects such keys like any other unknown key.

Primary / fallback authority (`select_authoritative`): within one Argo run the
primary step's record is authoritative unless the fallback step is `complete`.
A failing fallback never replaces the primary's outcome.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from orchestrator.context_snapshot import ContextSnapshotError, canonical_json, hash_bytes
from orchestrator.redaction import contains_credential

API_VERSION = "completion.mctl.ai/v1alpha1"
KIND = "ImplementationCompletion"
SUPPORTED_API_VERSIONS = {API_VERSION: KIND}

COMPLETION_ID_PREFIX = "ic-"

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------
OUTCOME_COMPLETE = "complete"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_BOUNDED_REFUSAL = "bounded_refusal"
OUTCOME_BUDGET_EXHAUSTED = "budget_exhausted"
OUTCOME_DETERMINISTIC_FAILURE = "deterministic_failure"
OUTCOME_HARNESS_FAILURE = "harness_failure"
OUTCOMES = frozenset(
    {
        OUTCOME_COMPLETE,
        OUTCOME_INCOMPLETE,
        OUTCOME_BOUNDED_REFUSAL,
        OUTCOME_BUDGET_EXHAUSTED,
        OUTCOME_DETERMINISTIC_FAILURE,
        OUTCOME_HARNESS_FAILURE,
    }
)

#: Every stop-reason code and the one outcome it belongs to. A record whose
#: code does not belong to its outcome is refused at construction.
STOP_REASON_OUTCOMES: Mapping[str, str] = {
    "gates_passed": OUTCOME_COMPLETE,
    "known_work_remains": OUTCOME_INCOMPLETE,
    "deliberate_no_op": OUTCOME_BOUNDED_REFUSAL,
    "evidence_insufficient": OUTCOME_BOUNDED_REFUSAL,
    "policy_refused": OUTCOME_BOUNDED_REFUSAL,
    "verification_budget": OUTCOME_BUDGET_EXHAUSTED,
    "operation_timeout": OUTCOME_BUDGET_EXHAUSTED,
    "rate_limited": OUTCOME_BUDGET_EXHAUSTED,
    "no_commits": OUTCOME_DETERMINISTIC_FAILURE,
    "branch_missing": OUTCOME_DETERMINISTIC_FAILURE,
    "policy_gate": OUTCOME_DETERMINISTIC_FAILURE,
    "stale_source": OUTCOME_DETERMINISTIC_FAILURE,
    "orphaned_subagent": OUTCOME_HARNESS_FAILURE,
    "fenced": OUTCOME_HARNESS_FAILURE,
    "claim_refused": OUTCOME_HARNESS_FAILURE,
    "policy_undecided": OUTCOME_HARNESS_FAILURE,
    "unexpected_error": OUTCOME_HARNESS_FAILURE,
}
STOP_REASON_CODES = frozenset(STOP_REASON_OUTCOMES)

STEPS = frozenset({"primary", "fallback"})
STEP_PRIMARY = "primary"
STEP_FALLBACK = "fallback"
PHASES = frozenset({"implement", "review-remediation"})

VERIFICATION_RESULTS = frozenset({"pass", "fail", "unknown", "not_run"})
VERIFICATION_CLASSIFICATIONS = frozenset(
    {"exit_zero", "exit_nonzero", "timeout", "budget_denied", "not_applicable", "observation_failed"}
)

#: The orchestrator-side gates. In v1alpha1 these are the ONLY `required`
#: checks; nothing compares the result with the approved `tasks.md` (F1).
REQUIRED_CHECKS_V1ALPHA1 = frozenset(
    {"commits_present", "chart_major_guard", "push_succeeded", "claim_valid_at_push"}
)

RELATION_FALLBACK = "fallback"
RELATION_SUPERSEDED = "superseded"
RELATIONS = frozenset({RELATION_FALLBACK, RELATION_SUPERSEDED})

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------
MAX_LIST_ITEMS = 32
MAX_ID_CHARS = 128
MAX_REF_CHARS = 256
MAX_DETAIL_CHARS = 600
MAX_SUMMARY_CHARS = 256
MAX_ITEM_ID_CHARS = 64

_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_COMPLETION_ID_RE = re.compile(r"ic-[0-9a-f]{16}")
_CHECK_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ITEM_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_CONTEXT_ID_RE = re.compile(r"ex-[0-9a-f]{16}")
_CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

#: Excluded from `content_hash`: identity is derived from them or they are a
#: fact about when, not what.
_UNHASHED = frozenset({"created_at", "completion_id", "content_hash"})
_REDACTED = "[redacted]"


class ImplementationCompletionError(ValueError):
    """A completion record violates the v1alpha1 contract."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def _reject_unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ImplementationCompletionError(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _g(data: Mapping[str, Any], key: str) -> Any:
    """`data.get(key)` typed `Any`: values are validated by the leaf types."""
    return data.get(key)


def _mapping(data: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ImplementationCompletionError(f"{where}: expected an object, got {type(data).__name__}")
    return data


def _str(value: Any, *, where: str, max_chars: int, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > max_chars:
        raise ImplementationCompletionError(f"{where}: expected a non-empty string of at most {max_chars} chars")
    if contains_credential(value):
        raise ImplementationCompletionError(f"{where}: value contains a credential shape")
    if pattern is not None and not pattern.fullmatch(value):
        raise ImplementationCompletionError(f"{where}: value does not match {pattern.pattern!r}")
    return value


def _opt_str(value: Any, *, where: str, max_chars: int, pattern: re.Pattern[str] | None = None) -> str | None:
    if value is None:
        return None
    return _str(value, where=where, max_chars=max_chars, pattern=pattern)


def _opt_int(value: Any, *, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ImplementationCompletionError(f"{where}: expected a non-negative integer")
    return value


def _one_of(value: Any, allowed: frozenset[str], *, where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ImplementationCompletionError(f"{where}: {value!r} is not one of {sorted(allowed)!r}")
    return value


def _list(value: Any, *, where: str) -> Sequence[Any]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise ImplementationCompletionError(f"{where}: expected a list")
    if len(value) > MAX_LIST_ITEMS:
        raise ImplementationCompletionError(f"{where}: at most {MAX_LIST_ITEMS} entries, got {len(value)}")
    return value


def redact_text(text: str, *, max_chars: int) -> str:
    """Normalise whitespace, bound, and screen free text before sealing.

    Same policy as `orchestrator.redaction`: a value that carries a credential
    shape is dropped (replaced by a marker), never partially masked."""
    flat = " ".join(str(text).split())
    if not flat or contains_credential(flat):
        return _REDACTED
    return flat[:max_chars]


# ---------------------------------------------------------------------------
# Leaf dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CompletionSubject:
    service: str
    slug: str
    work_item_id: str | None = None

    _KEYS = frozenset({"service", "slug", "work_item_id"})

    def __post_init__(self) -> None:
        _str(self.service, where="subject.service", max_chars=MAX_ID_CHARS)
        _str(self.slug, where="subject.slug", max_chars=MAX_ID_CHARS)
        _opt_str(self.work_item_id, where="subject.work_item_id", max_chars=MAX_ID_CHARS)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"service": self.service, "slug": self.slug}
        if self.work_item_id is not None:
            out["work_item_id"] = self.work_item_id
        return out

    @classmethod
    def from_dict(cls, data: Any) -> CompletionSubject:
        d = _mapping(data, where="subject")
        _reject_unknown_keys(d, cls._KEYS, where="subject")
        return cls(
            service=_g(d, "service"),
            slug=_g(d, "slug"),
            work_item_id=_g(d, "work_item_id"),
        )


@dataclass(frozen=True)
class AttemptBinding:
    attempt_id: str
    run_id: str
    step: str
    phase: str
    ordinal: int | None = None
    execution_id: str | None = None
    context_id: str | None = None
    claim_id: str | None = None
    owner_epoch: int | None = None

    _KEYS = frozenset(
        {"attempt_id", "run_id", "step", "phase", "ordinal", "execution_id", "context_id", "claim_id", "owner_epoch"}
    )

    def __post_init__(self) -> None:
        _str(self.attempt_id, where="attempt.attempt_id", max_chars=MAX_ID_CHARS)
        _str(self.run_id, where="attempt.run_id", max_chars=MAX_ID_CHARS)
        _one_of(self.step, STEPS, where="attempt.step")
        _one_of(self.phase, PHASES, where="attempt.phase")
        _opt_int(self.ordinal, where="attempt.ordinal")
        _opt_int(self.owner_epoch, where="attempt.owner_epoch")
        if self.execution_id is not None:
            _str(self.execution_id, where="attempt.execution_id", max_chars=MAX_ID_CHARS)
            if not self.execution_id.startswith("we_"):
                raise ImplementationCompletionError("attempt.execution_id: expected a `we_` work execution id")
        _opt_str(self.context_id, where="attempt.context_id", max_chars=MAX_ID_CHARS, pattern=_CONTEXT_ID_RE)
        _opt_str(self.claim_id, where="attempt.claim_id", max_chars=MAX_ID_CHARS)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "step": self.step,
            "phase": self.phase,
        }
        for key in ("ordinal", "execution_id", "context_id", "claim_id", "owner_epoch"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @classmethod
    def from_dict(cls, data: Any) -> AttemptBinding:
        d = _mapping(data, where="attempt")
        _reject_unknown_keys(d, cls._KEYS, where="attempt")
        return cls(**{k: _g(d, k) for k in cls._KEYS})


@dataclass(frozen=True)
class SourceBinding:
    repository: str
    base_ref: str
    branch: str
    base_sha: str | None = None
    head_sha: str | None = None

    _KEYS = frozenset({"repository", "base_ref", "branch", "base_sha", "head_sha"})

    def __post_init__(self) -> None:
        _str(self.repository, where="source.repository", max_chars=MAX_ID_CHARS)
        _str(self.base_ref, where="source.base_ref", max_chars=MAX_REF_CHARS)
        _str(self.branch, where="source.branch", max_chars=MAX_REF_CHARS)
        _opt_str(self.base_sha, where="source.base_sha", max_chars=64, pattern=_SHA_RE)
        _opt_str(self.head_sha, where="source.head_sha", max_chars=64, pattern=_SHA_RE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "head_sha": self.head_sha,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SourceBinding:
        d = _mapping(data, where="source")
        _reject_unknown_keys(d, cls._KEYS, where="source")
        return cls(**{k: _g(d, k) for k in cls._KEYS})


@dataclass(frozen=True)
class ProposalBinding:
    proposal_digest: str
    review_bundle_digest: str | None = None

    _KEYS = frozenset({"proposal_digest", "review_bundle_digest"})

    def __post_init__(self) -> None:
        _str(self.proposal_digest, where="proposal.proposal_digest", max_chars=71, pattern=_DIGEST_RE)
        _opt_str(
            self.review_bundle_digest, where="proposal.review_bundle_digest", max_chars=71, pattern=_DIGEST_RE
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"proposal_digest": self.proposal_digest}
        if self.review_bundle_digest is not None:
            out["review_bundle_digest"] = self.review_bundle_digest
        return out

    @classmethod
    def from_dict(cls, data: Any) -> ProposalBinding:
        d = _mapping(data, where="proposal")
        _reject_unknown_keys(d, cls._KEYS, where="proposal")
        return cls(
            proposal_digest=_g(d, "proposal_digest"),
            review_bundle_digest=_g(d, "review_bundle_digest"),
        )


@dataclass(frozen=True)
class EvidenceRef:
    """`{evidence_id, kind}` only: an id, never a payload (ADR 018)."""

    evidence_id: str
    kind: str

    _KEYS = frozenset({"evidence_id", "kind"})

    def __post_init__(self) -> None:
        _str(self.evidence_id, where="evidence_ref.evidence_id", max_chars=MAX_ID_CHARS)
        _str(self.kind, where="evidence_ref.kind", max_chars=MAX_ITEM_ID_CHARS)

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, "kind": self.kind}

    @classmethod
    def from_dict(cls, data: Any) -> EvidenceRef:
        d = _mapping(data, where="evidence_ref")
        _reject_unknown_keys(d, cls._KEYS, where="evidence_ref")
        return cls(evidence_id=_g(d, "evidence_id"), kind=_g(d, "kind"))


@dataclass(frozen=True)
class VerificationRecord:
    """One check. No stdout / stderr field by design."""

    check_id: str
    result: str
    classification: str
    required: bool
    evidence_ref: EvidenceRef | None = None

    _KEYS = frozenset({"check_id", "result", "classification", "required", "evidence_ref"})

    def __post_init__(self) -> None:
        _str(self.check_id, where="verification.check_id", max_chars=64, pattern=_CHECK_ID_RE)
        _one_of(self.result, VERIFICATION_RESULTS, where="verification.result")
        _one_of(self.classification, VERIFICATION_CLASSIFICATIONS, where="verification.classification")
        if not isinstance(self.required, bool):
            raise ImplementationCompletionError("verification.required: expected a boolean")
        # `required` is a property of the contract version, not of the caller.
        if self.required != (self.check_id in REQUIRED_CHECKS_V1ALPHA1):
            raise ImplementationCompletionError(
                f"verification.required: in v1alpha1 exactly {sorted(REQUIRED_CHECKS_V1ALPHA1)!r} are required "
                f"(check {self.check_id!r} has required={self.required})"
            )
        if self.evidence_ref is not None and not isinstance(self.evidence_ref, EvidenceRef):
            raise ImplementationCompletionError("verification.evidence_ref: expected an EvidenceRef")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "check_id": self.check_id,
            "result": self.result,
            "classification": self.classification,
            "required": self.required,
        }
        if self.evidence_ref is not None:
            out["evidence_ref"] = self.evidence_ref.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: Any) -> VerificationRecord:
        d = _mapping(data, where="verification")
        _reject_unknown_keys(d, cls._KEYS, where="verification")
        ref = _g(d, "evidence_ref")
        return cls(
            check_id=_g(d, "check_id"),
            result=_g(d, "result"),
            classification=_g(d, "classification"),
            required=_g(d, "required"),
            evidence_ref=None if ref is None else EvidenceRef.from_dict(ref),
        )


@dataclass(frozen=True)
class RemainingWorkItem:
    item_id: str
    summary: str

    _KEYS = frozenset({"item_id", "summary"})

    def __post_init__(self) -> None:
        _str(self.item_id, where="remaining_work.item_id", max_chars=MAX_ITEM_ID_CHARS, pattern=_ITEM_ID_RE)
        _str(self.summary, where="remaining_work.summary", max_chars=MAX_SUMMARY_CHARS)

    def to_dict(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "summary": self.summary}

    @classmethod
    def from_dict(cls, data: Any) -> RemainingWorkItem:
        d = _mapping(data, where="remaining_work")
        _reject_unknown_keys(d, cls._KEYS, where="remaining_work")
        return cls(item_id=_g(d, "item_id"), summary=_g(d, "summary"))


@dataclass(frozen=True)
class StopReason:
    code: str
    detail: str | None = None

    _KEYS = frozenset({"code", "detail"})

    def __post_init__(self) -> None:
        _one_of(self.code, STOP_REASON_CODES, where="stop_reason.code")
        _opt_str(self.detail, where="stop_reason.detail", max_chars=MAX_DETAIL_CHARS)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code}
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, data: Any) -> StopReason:
        d = _mapping(data, where="stop_reason")
        _reject_unknown_keys(d, cls._KEYS, where="stop_reason")
        return cls(code=_g(d, "code"), detail=_g(d, "detail"))


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
_TOP_KEYS = frozenset(
    {
        "api_version",
        "kind",
        "completion_id",
        "content_hash",
        "created_at",
        "subject",
        "attempt",
        "source",
        "proposal",
        "outcome",
        "stop_reason",
        "verification",
        "remaining_work",
        "evidence_refs",
    }
)


def _hash_body(body: Mapping[str, Any]) -> str:
    hashed = {k: v for k, v in body.items() if k not in _UNHASHED}
    try:
        return hash_bytes(canonical_json(hashed))
    except ContextSnapshotError as exc:
        raise ImplementationCompletionError(str(exc)) from exc


def _id_for(content_hash: str) -> str:
    return COMPLETION_ID_PREFIX + content_hash[len("sha256:") : len("sha256:") + 16]


@dataclass(frozen=True)
class ImplementationCompletion:
    """One sealed record. Build with `seal()`; `from_dict` re-validates."""

    completion_id: str
    content_hash: str
    created_at: str
    subject: CompletionSubject
    attempt: AttemptBinding
    source: SourceBinding
    proposal: ProposalBinding
    outcome: str
    stop_reason: StopReason
    verification: tuple[VerificationRecord, ...] = ()
    remaining_work: tuple[RemainingWorkItem, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    api_version: str = API_VERSION
    kind: str = KIND

    def __post_init__(self) -> None:
        if SUPPORTED_API_VERSIONS.get(self.api_version) != self.kind:
            raise ImplementationCompletionError(
                f"unsupported api_version/kind {self.api_version!r}/{self.kind!r}; "
                f"supported: {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        _one_of(self.outcome, OUTCOMES, where="outcome")
        if STOP_REASON_OUTCOMES[self.stop_reason.code] != self.outcome:
            raise ImplementationCompletionError(
                f"stop_reason.code {self.stop_reason.code!r} does not belong to outcome {self.outcome!r}"
            )
        _str(self.created_at, where="created_at", max_chars=20, pattern=_CREATED_AT_RE)
        for name, items, cls in (
            ("verification", self.verification, VerificationRecord),
            ("remaining_work", self.remaining_work, RemainingWorkItem),
            ("evidence_refs", self.evidence_refs, EvidenceRef),
        ):
            if not isinstance(items, tuple) or len(items) > MAX_LIST_ITEMS:
                raise ImplementationCompletionError(f"{name}: expected a tuple of at most {MAX_LIST_ITEMS} entries")
            if not all(isinstance(i, cls) for i in items):
                raise ImplementationCompletionError(f"{name}: entries must be {cls.__name__}")
        self._check_invariants()
        expected_hash = _hash_body(self._body())
        if self.content_hash != expected_hash:
            raise ImplementationCompletionError("content_hash does not match the record content")
        if self.completion_id != _id_for(expected_hash) or not _COMPLETION_ID_RE.fullmatch(self.completion_id):
            raise ImplementationCompletionError("completion_id is not derived from content_hash")

    def _check_invariants(self) -> None:
        if self.outcome == OUTCOME_COMPLETE:
            bad = [v.check_id for v in self.verification if v.required and v.result != "pass"]
            if bad:
                raise ImplementationCompletionError(f"complete requires every required check to pass; failing: {bad!r}")
            present = {v.check_id for v in self.verification if v.required}
            if present != REQUIRED_CHECKS_V1ALPHA1:
                raise ImplementationCompletionError(
                    f"complete requires every required check to be recorded; missing: "
                    f"{sorted(REQUIRED_CHECKS_V1ALPHA1 - present)!r}"
                )
            if self.remaining_work:
                raise ImplementationCompletionError("complete requires empty remaining_work")
            if self.source.head_sha is None:
                raise ImplementationCompletionError("complete requires source.head_sha")
        if self.outcome == OUTCOME_INCOMPLETE and not self.remaining_work:
            raise ImplementationCompletionError("incomplete requires non-empty remaining_work")

    def _body(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "completion_id": self.completion_id,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "subject": self.subject.to_dict(),
            "attempt": self.attempt.to_dict(),
            "source": self.source.to_dict(),
            "proposal": self.proposal.to_dict(),
            "outcome": self.outcome,
            "stop_reason": self.stop_reason.to_dict(),
            "verification": [v.to_dict() for v in self.verification],
            "remaining_work": [r.to_dict() for r in self.remaining_work],
            "evidence_refs": [e.to_dict() for e in self.evidence_refs],
        }

    def to_dict(self) -> dict[str, Any]:
        return self._body()

    def to_json(self) -> str:
        """Canonical JSON form (sorted keys, no whitespace)."""
        try:
            return canonical_json(self.to_dict()).decode("utf-8")
        except ContextSnapshotError as exc:
            raise ImplementationCompletionError(str(exc)) from exc

    @classmethod
    def from_dict(cls, data: Any) -> ImplementationCompletion:
        d = _mapping(data, where="completion")
        _reject_unknown_keys(d, _TOP_KEYS, where="completion")
        version = _g(d, "api_version")
        if version not in SUPPORTED_API_VERSIONS:
            raise ImplementationCompletionError(
                f"unsupported api_version {version!r}; supported: {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        for required in ("completion_id", "content_hash", "created_at", "subject", "attempt", "source",
                         "proposal", "outcome", "stop_reason"):
            if required not in d:
                raise ImplementationCompletionError(f"completion: missing required key {required!r}")
        return cls(
            api_version=version,
            kind=_g(d, "kind"),
            completion_id=d["completion_id"],
            content_hash=d["content_hash"],
            created_at=d["created_at"],
            subject=CompletionSubject.from_dict(d["subject"]),
            attempt=AttemptBinding.from_dict(d["attempt"]),
            source=SourceBinding.from_dict(d["source"]),
            proposal=ProposalBinding.from_dict(d["proposal"]),
            outcome=d["outcome"],
            stop_reason=StopReason.from_dict(d["stop_reason"]),
            verification=tuple(
                VerificationRecord.from_dict(v) for v in _list(_g(d, "verification"), where="verification")
            ),
            remaining_work=tuple(
                RemainingWorkItem.from_dict(r) for r in _list(_g(d, "remaining_work"), where="remaining_work")
            ),
            evidence_refs=tuple(EvidenceRef.from_dict(e) for e in _list(_g(d, "evidence_refs"), where="evidence_refs")),
        )


def stop_reason(code: str, detail: str | None = None) -> StopReason:
    """Build a `StopReason`, redacting and bounding `detail` first."""
    return StopReason(code, None if detail is None else redact_text(detail, max_chars=MAX_DETAIL_CHARS))


def remaining_item(item_id: str, summary: str) -> RemainingWorkItem:
    """Build a `RemainingWorkItem`, redacting and bounding `summary` first."""
    return RemainingWorkItem(item_id, redact_text(summary, max_chars=MAX_SUMMARY_CHARS))


def _now_z() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def seal(
    *,
    subject: CompletionSubject,
    attempt: AttemptBinding,
    source: SourceBinding,
    proposal: ProposalBinding,
    outcome: str,
    stop_reason: StopReason,
    verification: Sequence[VerificationRecord] = (),
    remaining_work: Sequence[RemainingWorkItem] = (),
    evidence_refs: Sequence[EvidenceRef] = (),
    created_at: str | None = None,
) -> ImplementationCompletion:
    """The only constructor that fills identity.

    Free text (`stop_reason.detail`, `remaining_work[].summary`) arrives
    already redacted and bounded through `stop_reason()` / `remaining_item()`;
    the leaf types refuse anything else, so nothing unscreened reaches the
    hash. `created_at` is excluded from the hash, so re-sealing identical
    content yields the same `completion_id`.
    """
    remaining = tuple(remaining_work)
    verification_t = tuple(verification)
    evidence_t = tuple(evidence_refs)
    for name, items in (("verification", verification_t), ("remaining_work", remaining), ("evidence_refs", evidence_t)):
        if len(items) > MAX_LIST_ITEMS:
            raise ImplementationCompletionError(f"{name}: at most {MAX_LIST_ITEMS} entries, got {len(items)}")
    body = {
        "api_version": API_VERSION,
        "kind": KIND,
        "subject": subject.to_dict(),
        "attempt": attempt.to_dict(),
        "source": source.to_dict(),
        "proposal": proposal.to_dict(),
        "outcome": outcome,
        "stop_reason": stop_reason.to_dict(),
        "verification": [v.to_dict() for v in verification_t],
        "remaining_work": [r.to_dict() for r in remaining],
        "evidence_refs": [e.to_dict() for e in evidence_t],
    }
    content_hash = _hash_body(body)
    return ImplementationCompletion(
        completion_id=_id_for(content_hash),
        content_hash=content_hash,
        created_at=created_at or _now_z(),
        subject=subject,
        attempt=attempt,
        source=source,
        proposal=proposal,
        outcome=outcome,
        stop_reason=stop_reason,
        verification=verification_t,
        remaining_work=remaining,
        evidence_refs=evidence_t,
    )


# ---------------------------------------------------------------------------
# Projection (what `.status.yaml` keeps) and primary / fallback authority
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CompletionSummary:
    """A bounded summary of one sealed record: no verification details, no
    runtime telemetry. `relation` is set only on the non-authoritative one."""

    completion_id: str
    content_hash: str
    attempt_id: str
    step: str
    outcome: str
    stop_reason: str
    head_sha: str | None
    required_passed: int
    required_total: int
    optional_total: int
    relation: str | None = None

    _KEYS = frozenset(
        {"completion_id", "content_hash", "attempt_id", "step", "outcome", "stop_reason", "head_sha",
         "verification", "relation"}
    )

    def __post_init__(self) -> None:
        _str(self.completion_id, where="summary.completion_id", max_chars=19, pattern=_COMPLETION_ID_RE)
        _str(self.content_hash, where="summary.content_hash", max_chars=71, pattern=_DIGEST_RE)
        _str(self.attempt_id, where="summary.attempt_id", max_chars=MAX_ID_CHARS)
        _one_of(self.step, STEPS, where="summary.step")
        _one_of(self.outcome, OUTCOMES, where="summary.outcome")
        _one_of(self.stop_reason, STOP_REASON_CODES, where="summary.stop_reason")
        _opt_str(self.head_sha, where="summary.head_sha", max_chars=64, pattern=_SHA_RE)
        for name in ("required_passed", "required_total", "optional_total"):
            if getattr(self, name) is None:
                raise ImplementationCompletionError(f"summary.verification.{name}: required")
            _opt_int(getattr(self, name), where=f"summary.verification.{name}")
        if self.relation is not None:
            _one_of(self.relation, RELATIONS, where="summary.relation")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "completion_id": self.completion_id,
            "content_hash": self.content_hash,
            "attempt_id": self.attempt_id,
            "step": self.step,
            "outcome": self.outcome,
            "stop_reason": self.stop_reason,
            "head_sha": self.head_sha,
            "verification": {
                "required_passed": self.required_passed,
                "required_total": self.required_total,
                "optional_total": self.optional_total,
            },
        }
        if self.relation is not None:
            out["relation"] = self.relation
        return out

    @classmethod
    def from_dict(cls, data: Any) -> CompletionSummary:
        d = _mapping(data, where="summary")
        _reject_unknown_keys(d, cls._KEYS, where="summary")
        v = _mapping(_g(d, "verification"), where="summary.verification")
        _reject_unknown_keys(
            v, frozenset({"required_passed", "required_total", "optional_total"}), where="summary.verification"
        )
        return cls(
            completion_id=_g(d, "completion_id"),
            content_hash=_g(d, "content_hash"),
            attempt_id=_g(d, "attempt_id"),
            step=_g(d, "step"),
            outcome=_g(d, "outcome"),
            stop_reason=_g(d, "stop_reason"),
            head_sha=_g(d, "head_sha"),
            required_passed=_g(v, "required_passed"),
            required_total=_g(v, "required_total"),
            optional_total=_g(v, "optional_total"),
            relation=_g(d, "relation"),
        )

    def with_relation(self, relation: str | None) -> CompletionSummary:
        return replace(self, relation=relation)


def summarize(completion: ImplementationCompletion) -> CompletionSummary:
    required = [v for v in completion.verification if v.required]
    return CompletionSummary(
        completion_id=completion.completion_id,
        content_hash=completion.content_hash,
        attempt_id=completion.attempt.attempt_id,
        step=completion.attempt.step,
        outcome=completion.outcome,
        stop_reason=completion.stop_reason.code,
        head_sha=completion.source.head_sha,
        required_passed=sum(1 for v in required if v.result == "pass"),
        required_total=len(required),
        optional_total=len(completion.verification) - len(required),
    )


@dataclass(frozen=True)
class CompletionProjection:
    """The `completion:` block of `.status.yaml`."""

    run_id: str
    authoritative: CompletionSummary
    primary_observed: bool
    other: CompletionSummary | None = None

    _KEYS = frozenset({"api_version", "run_id", "authoritative", "other", "primary_observed"})

    def __post_init__(self) -> None:
        _str(self.run_id, where="projection.run_id", max_chars=MAX_ID_CHARS)
        if self.authoritative.relation is not None:
            raise ImplementationCompletionError("projection.authoritative carries no relation")
        if self.other is not None and self.other.relation is None:
            raise ImplementationCompletionError("projection.other requires a relation")
        if not isinstance(self.primary_observed, bool):
            raise ImplementationCompletionError("projection.primary_observed: expected a boolean")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "api_version": API_VERSION,
            "run_id": self.run_id,
            "authoritative": self.authoritative.to_dict(),
        }
        if self.other is not None:
            out["other"] = self.other.to_dict()
        out["primary_observed"] = self.primary_observed
        return out

    @classmethod
    def from_dict(cls, data: Any) -> CompletionProjection:
        d = _mapping(data, where="projection")
        _reject_unknown_keys(d, cls._KEYS, where="projection")
        if _g(d, "api_version") not in SUPPORTED_API_VERSIONS:
            raise ImplementationCompletionError(f"unsupported api_version {_g(d, 'api_version')!r}")
        other = _g(d, "other")
        return cls(
            run_id=_g(d, "run_id"),
            authoritative=CompletionSummary.from_dict(_g(d, "authoritative")),
            other=None if other is None else CompletionSummary.from_dict(other),
            primary_observed=_g(d, "primary_observed"),
        )

    def summaries(self) -> list[CompletionSummary]:
        return [self.authoritative] + ([self.other] if self.other is not None else [])


def project_primary(completion: ImplementationCompletion) -> CompletionProjection:
    """The primary step runs first and never sees a fallback: its own record
    is authoritative."""
    return CompletionProjection(
        run_id=completion.attempt.run_id, authoritative=summarize(completion), primary_observed=True
    )


def select_authoritative(
    primary: CompletionSummary | None, fallback: ImplementationCompletion
) -> CompletionProjection:
    """Which record speaks for one Argo run, given the fallback step's own.

    | primary handoff | fallback outcome | authoritative | other |
    | present         | `complete`       | fallback      | primary as `superseded` |
    | present         | not `complete`   | primary       | fallback as `fallback`  |
    | absent          | any              | fallback, `primary_observed: false` | none |

    A failing fallback (typically a harness failure on the second account)
    never replaces the primary's real outcome; a succeeding one does, which
    mirrors `assert-produced` counting the run as produced if either step
    succeeded. The row "fallback never reached the claim" has no fallback
    record, so it does not reach this function: the primary's artifact stays.
    """
    if fallback.attempt.step != STEP_FALLBACK:
        raise ImplementationCompletionError("select_authoritative: the second argument must be a fallback record")
    mine = summarize(fallback)
    run_id = fallback.attempt.run_id
    if primary is None:
        return CompletionProjection(run_id=run_id, authoritative=mine, primary_observed=False)
    if primary.step != STEP_PRIMARY:
        raise ImplementationCompletionError("select_authoritative: the first argument must be a primary summary")
    if fallback.outcome == OUTCOME_COMPLETE:
        return CompletionProjection(
            run_id=run_id,
            authoritative=mine.with_relation(None),
            other=primary.with_relation(RELATION_SUPERSEDED),
            primary_observed=True,
        )
    return CompletionProjection(
        run_id=run_id,
        authoritative=primary.with_relation(None),
        other=mine.with_relation(RELATION_FALLBACK),
        primary_observed=True,
    )
