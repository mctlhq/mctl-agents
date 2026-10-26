"""Client-side mirror of the mctl-api SecurityFinding v1 contract (4.124.0).

The vocabularies are closed and come from `vocabulary.json`, a copy of
`tests/fixtures/security_finding_v1/vocabulary.json` captured from mctl-api
`docs/security-findings.md` and `internal/openapi/openapi.yaml` at ref
`4.124.0` (a test pins the two copies together). A value outside a
vocabulary parses as `UNKNOWN`, never as a valid member.

The parsers read only structural fields. Provider free text
(`untrusted.*`) is dropped here, so no downstream code can read it: the
parsed types have no `untrusted` attribute.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_VOCAB = json.loads((Path(__file__).with_name("vocabulary.json")).read_text())

UNKNOWN = "unknown"

STATES = frozenset(_VOCAB["states"])
ATTEMPT_OUTCOMES = frozenset(_VOCAB["attempt_outcomes"])
MECHANISMS = frozenset(_VOCAB["mechanisms"])
RESULTS = frozenset(_VOCAB["results"])
CLAIMED_ACTIONS = frozenset(_VOCAB["claimed_actions"])
ERROR_CODES = frozenset(_VOCAB["error_codes"])
START_STATES = frozenset(_VOCAB["start_states"])
#: Strongest first.
STRENGTH_ORDER: tuple[str, ...] = tuple(_VOCAB["strength_order"])
DEFAULT_MINIMUM_STRENGTH: str = _VOCAB["default_minimum_strength"]

STATE_FIXING = "fixing"
STATE_AWAITING = "awaiting_verification"

#: Error codes that mean "the store could not answer", not "the answer is no".
UNAVAILABLE_CODES = frozenset({"security_findings_store_unavailable", "work_items_unavailable", "internal_error"})

EVIDENCE_ASSURANCE_FINDING = "assurance_finding"
EVIDENCE_ASSURANCE_RESULT = "assurance_result"

#: Mechanisms this agent may record. `provider_claim` and `manual` never.
AGENT_MECHANISMS = frozenset({"deterministic_regression_test", "assurance_replay", "scanner_rerun_exact_revision"})


def _s(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _member(value: Any, vocabulary: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in vocabulary else UNKNOWN


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    ref: str
    digest: str = ""


@dataclass(frozen=True)
class Observation:
    id: str
    observation_hash: str
    provider_ref: str
    claimed_action: str
    observed_revision: str = ""
    observed_artifact_digest: str = ""
    observed_at: str = ""
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class Attempt:
    id: str
    seq: int
    work_item_id: str = ""
    devloop_workflow_id: str = ""
    landed_revision: str = ""
    landed_artifact_digest: str = ""
    landed_ref: str = ""
    landed_at: str = ""
    outcome: str = ""

    @property
    def active(self) -> bool:
        return not self.outcome


@dataclass(frozen=True)
class Verification:
    id: str
    attempt_id: str
    mechanism: str
    result: str
    revision: str = ""
    artifact_digest: str = ""
    recorded_by: str = ""
    recorded_at: str = ""


@dataclass(frozen=True)
class Occurrence:
    id: str
    state: str
    state_version: int
    finding_id: str = ""
    tenant: str = ""
    repository: str = ""
    active_attempt_id: str = ""
    attempts: tuple[Attempt, ...] = ()
    verifications: tuple[Verification, ...] = ()
    observations: tuple[Observation, ...] = ()
    #: True when the body carried the history arrays (a full read, not a list summary).
    full: bool = field(default=False, compare=False)

    @property
    def active_attempt(self) -> Attempt | None:
        for attempt in self.attempts:
            if attempt.id == self.active_attempt_id and attempt.active:
                return attempt
        return None


def _list(value: Any) -> list[dict[str, Any]]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def parse_evidence(raw: dict[str, Any]) -> EvidenceRef | None:
    kind, ref = _s(raw.get("kind")), _s(raw.get("ref"))
    return EvidenceRef(kind, ref, _s(raw.get("digest"))) if kind and ref else None


def parse_observation(raw: dict[str, Any]) -> Observation | None:
    oid = _s(raw.get("id"))
    if not oid:
        return None
    refs = tuple(e for e in (parse_evidence(r) for r in _list(raw.get("evidence_refs"))) if e)
    return Observation(
        id=oid,
        observation_hash=_s(raw.get("observation_hash")),
        provider_ref=_s(raw.get("provider_ref")),
        claimed_action=_member(raw.get("claimed_action"), CLAIMED_ACTIONS),
        observed_revision=_s(raw.get("observed_revision")),
        observed_artifact_digest=_s(raw.get("observed_artifact_digest")),
        observed_at=_s(raw.get("observed_at")),
        evidence_refs=refs,
    )


def parse_attempt(raw: dict[str, Any]) -> Attempt | None:
    aid = _s(raw.get("id"))
    if not aid:
        return None
    seq = raw.get("seq")
    outcome = raw.get("outcome")
    return Attempt(
        id=aid,
        seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else 0,
        work_item_id=_s(raw.get("work_item_id")),
        devloop_workflow_id=_s(raw.get("devloop_workflow_id")),
        landed_revision=_s(raw.get("landed_revision")),
        landed_artifact_digest=_s(raw.get("landed_artifact_digest")),
        landed_ref=_s(raw.get("landed_ref")),
        landed_at=_s(raw.get("landed_at")),
        # An outcome outside the vocabulary is not "active": it is unknown.
        outcome=_member(outcome, ATTEMPT_OUTCOMES) if outcome else "",
    )


def parse_verification(raw: dict[str, Any]) -> Verification | None:
    vid = _s(raw.get("id"))
    if not vid:
        return None
    return Verification(
        id=vid,
        attempt_id=_s(raw.get("attempt_id")),
        mechanism=_member(raw.get("mechanism"), MECHANISMS),
        result=_member(raw.get("result"), RESULTS),
        revision=_s(raw.get("revision")),
        artifact_digest=_s(raw.get("artifact_digest")),
        recorded_by=_s(raw.get("recorded_by")),
        recorded_at=_s(raw.get("recorded_at")),
    )


def parse_occurrence(raw: Any) -> Occurrence | None:
    """None for a body that is not an occurrence. `untrusted` is never read."""
    if not isinstance(raw, dict):
        return None
    oid = _s(raw.get("id"))
    version = raw.get("state_version")
    if not oid or not isinstance(version, int) or isinstance(version, bool):
        return None
    subject = raw.get("subject")
    repository = _s(subject.get("repository")) if isinstance(subject, dict) else ""
    return Occurrence(
        id=oid,
        state=_member(raw.get("state"), STATES),
        state_version=version,
        finding_id=_s(raw.get("finding_id")),
        tenant=_s(raw.get("tenant")),
        repository=repository,
        active_attempt_id=_s(raw.get("active_attempt_id")),
        attempts=tuple(a for a in (parse_attempt(r) for r in _list(raw.get("remediation_attempts"))) if a),
        verifications=tuple(v for v in (parse_verification(r) for r in _list(raw.get("verifications"))) if v),
        observations=tuple(o for o in (parse_observation(r) for r in _list(raw.get("observations"))) if o),
        full="remediation_attempts" in raw or "verifications" in raw,
    )


def parse_finding_identity(raw: Any) -> str:
    """The `identity_key` of a finding body, "" when unreadable."""
    return _s(raw.get("identity_key")) if isinstance(raw, dict) else ""


def strength_rank(mechanism: str) -> int:
    """0 is strongest; an unknown mechanism ranks below every known one."""
    return STRENGTH_ORDER.index(mechanism) if mechanism in STRENGTH_ORDER else len(STRENGTH_ORDER)
