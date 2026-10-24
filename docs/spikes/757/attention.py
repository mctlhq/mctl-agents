"""AttentionDecision v1 and a pure, fixture-only decide() prototype (spike #757).

Attention is not authorization and not approval. This module decides whether a
human should be interrupted about a watched resource. `approval_ref` is a
pointer to an existing `aar_` record and nothing here creates, decides,
consumes or writes one. No I/O, no network, no approval client import.
`REQUIRE_APPROVAL` here means "surface to a human who may use the existing
approval path"; it is not the policy_checkpoint verdict of the same name.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _load_sibling(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


envelope_v1 = _load_sibling("envelope_v1")
EnvelopeRefused = envelope_v1.EnvelopeRefused

SCHEMA = "attentiondecision/v1"
DECISIONS = ("IGNORE", "CONTINUE", "NOTIFY", "REQUIRE_APPROVAL")
BASES = ("rule", "model")
_SEVERITY = {d: i for i, d in enumerate(DECISIONS)}
# Same pattern string as orchestrator/approval_evidence.py GRANT_REF_RE (a test asserts equality).
APPROVAL_REF_PATTERN = r"aar_[A-Za-z0-9_-]{1,124}"
_APPROVAL_REF_RE = re.compile(APPROVAL_REF_PATTERN)
UNMATCHED = "unmatched"

CHECKS = ("success", "failure", "pending", "unknown")
MERGEABLE = ("clean", "blocked", "dirty", "unknown")
SEVERITY = ("P1", "P2", "P3", "none", "unknown")
_MATCH_KEYS = {"type", "digest_unchanged", "checks", "mergeable", "max_review_severity"}


class AttentionError(ValueError):
    """An AttentionDecision, ObservedState or rule is outside its closed vocabulary."""


@dataclass(frozen=True)
class ObservedState:
    """Re-observed state (stands in for what channel.py would read via GitHub MCP tools)."""

    checks: str
    mergeable: str
    max_review_severity: str
    head_sha: str

    def __post_init__(self) -> None:
        for name, allowed in (
            ("checks", CHECKS),
            ("mergeable", MERGEABLE),
            ("max_review_severity", SEVERITY),
        ):
            if getattr(self, name) not in allowed:
                raise AttentionError(f"invalid {name}: {getattr(self, name)!r}")
        if not isinstance(self.head_sha, str) or not self.head_sha:
            raise AttentionError("head_sha is required")

    def fully_known(self) -> bool:
        return "unknown" not in (self.checks, self.mergeable, self.max_review_severity)


@dataclass(frozen=True)
class AttentionDecision:
    decision_id: str
    subject_ref: str
    session_or_workitem_ref: str
    decision: str
    basis: str
    rule_id: str
    observed_state_digest: str
    coalesced_event_ids: tuple[str, ...]
    approval_ref: str | None
    decided_at: str
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise AttentionError(f"invalid decision: {self.decision!r}")
        if self.basis not in BASES:
            raise AttentionError(f"invalid basis: {self.basis!r}")
        if self.approval_ref is not None and not _APPROVAL_REF_RE.fullmatch(self.approval_ref):
            raise AttentionError("approval_ref must match the aar_ pattern")
        if self.schema != SCHEMA:
            raise AttentionError("unsupported schema")


def load_rules(path: str | Path) -> list[dict[str, Any]]:
    doc = yaml.safe_load(Path(path).read_text())
    rules = doc["rules"]
    for rule in rules:
        if rule["decision"] not in DECISIONS:
            raise AttentionError(f"rule {rule['id']}: invalid decision")
        unknown = set(rule["match"]) - _MATCH_KEYS
        if unknown:
            raise AttentionError(f"rule {rule['id']}: unknown match keys {sorted(unknown)}")
    return list(rules)


def type_matches(pattern: str, value: str) -> bool:
    """Exact match, or a trailing `*` glob (prefix match; `*` is only meaningful last)."""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return pattern == value


def resource_key(envelope: Mapping[str, Any]) -> str:
    s = envelope["subject"]
    return f"github:pr:{s.get('repository', '')}#{s.get('number', '')}"


def observed_digest(key: str, observed: ObservedState | None) -> str:
    """Digest of the resource key plus canonical ObservedState; never envelope id or time."""
    state = (
        {"checks": "unknown", "mergeable": "unknown", "max_review_severity": "unknown", "head_sha": ""}
        if observed is None
        else {
            "checks": observed.checks,
            "mergeable": observed.mergeable,
            "max_review_severity": observed.max_review_severity,
            "head_sha": observed.head_sha,
        }
    )
    raw = json.dumps({"resource": key, "state": state}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _rule_matches(rule: Mapping[str, Any], etype: str, observed: ObservedState | None, unchanged: bool) -> bool:
    for k, want in rule["match"].items():
        if k == "type":
            if not type_matches(want, etype):
                return False
        elif k == "digest_unchanged":
            # Unknown is never "unchanged" (amendment A2).
            if bool(want) != (unchanged and observed is not None and observed.fully_known()):
                return False
        else:
            if observed is None or getattr(observed, k) != want:
                return False
    return True


def _decision_id(subject_ref: str, ids: tuple[str, ...], digest: str, rule_id: str) -> str:
    raw = json.dumps([subject_ref, sorted(ids), digest, rule_id], separators=(",", ":"))
    return "atd_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _build(
    *,
    subject_ref: str,
    key: str,
    decision: str,
    basis: str,
    rule_id: str,
    digest: str,
    ids: Iterable[str],
    approval_ref: str | None,
    decided_at: str,
) -> AttentionDecision:
    coalesced = tuple(sorted(set(ids)))
    return AttentionDecision(
        decision_id=_decision_id(subject_ref, coalesced, digest, rule_id),
        subject_ref=subject_ref,
        session_or_workitem_ref=key,
        decision=decision,
        basis=basis,
        rule_id=rule_id,
        observed_state_digest=digest,
        coalesced_event_ids=coalesced,
        approval_ref=approval_ref if decision == "REQUIRE_APPROVAL" else None,
        decided_at=decided_at,
    )


Classifier = Callable[[Mapping[str, Any], "ObservedState | None"], str]


def decide(
    envelope: Mapping[str, Any],
    last_observed_digest: str | None,
    rules: list[dict[str, Any]],
    *,
    observed: ObservedState | None = None,
    classifier: Classifier | None = None,
    clock: Callable[[], str] | None = None,
    approval_ref: str | None = None,
) -> AttentionDecision:
    """Pure decision for one envelope. Raises EnvelopeRefused; a refusal is never IGNORE."""
    return _decide_one(envelope, last_observed_digest, rules, observed, classifier, clock, approval_ref)[0]


def _decide_one(
    envelope: Mapping[str, Any],
    last: str | None,
    rules: list[dict[str, Any]],
    observed: ObservedState | None,
    classifier: Classifier | None,
    clock: Callable[[], str] | None,
    approval_ref: str | None,
) -> tuple[AttentionDecision, int]:
    envelope_v1.validate(envelope)
    if approval_ref is not None and not _APPROVAL_REF_RE.fullmatch(approval_ref):
        raise AttentionError("approval_ref must match the aar_ pattern")
    key = resource_key(envelope)
    digest = observed_digest(key, observed)
    unchanged = last is not None and digest == last
    decided_at = clock() if clock else envelope["occurred_at"]
    common = dict(
        subject_ref=envelope["id"],
        key=key,
        digest=digest,
        ids=[envelope["id"]],
        approval_ref=approval_ref,
        decided_at=decided_at,
    )
    for index, rule in enumerate(rules):
        if _rule_matches(rule, envelope["type"], observed, unchanged):
            d = _build(decision=rule["decision"], basis="rule", rule_id=rule["id"], **common)  # type: ignore[arg-type]
            return d, index
    if classifier is None:
        return _build(decision="NOTIFY", basis="model", rule_id=UNMATCHED, **common), len(rules)  # type: ignore[arg-type]
    answer = classifier(envelope, observed)
    if answer not in DECISIONS:
        raise AttentionError(f"classifier answer outside closed set: {answer!r}")
    return _build(decision=answer, basis="model", rule_id=UNMATCHED, **common), len(rules)  # type: ignore[arg-type]


def decide_wake_cycle(
    envelopes: Iterable[Mapping[str, Any]],
    last_observed_digests: Mapping[str, str],
    rules: list[dict[str, Any]],
    *,
    observed_by_resource: Mapping[str, ObservedState] | None = None,
    classifier: Classifier | None = None,
    clock: Callable[[], str] | None = None,
    approval_refs: Mapping[str, str] | None = None,
) -> list[AttentionDecision]:
    """At most one decision per watched resource; independent of input order.

    A refused envelope raises (fail closed) instead of being dropped.
    """
    observed_by_resource = observed_by_resource or {}
    approval_refs = approval_refs or {}
    unique: dict[str, Mapping[str, Any]] = {}
    for env in envelopes:
        envelope_v1.validate(env)
        unique.setdefault(env["id"], env)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for env in unique.values():
        groups.setdefault(resource_key(env), []).append(env)
    out: list[AttentionDecision] = []
    for key in sorted(groups):
        events = sorted(groups[key], key=lambda e: (e["occurred_at"], e["id"]))
        observed = observed_by_resource.get(key)
        scored = [
            _decide_one(
                e,
                last_observed_digests.get(key),
                rules,
                observed,
                classifier,
                clock,
                approval_refs.get(key),
            )
            for e in events
        ]
        best = min(scored, key=lambda t: (-_SEVERITY[t[0].decision], t[1], t[0].subject_ref))[0]
        ids = [e["id"] for e in events]
        subject_ref = ids[0] if len(ids) == 1 else events[-1]["correlation_id"]
        out.append(
            _build(
                subject_ref=subject_ref,
                key=key,
                decision=best.decision,
                basis=best.basis,
                rule_id=best.rule_id,
                digest=best.observed_state_digest,
                ids=ids,
                approval_ref=best.approval_ref,
                decided_at=clock() if clock else events[-1]["occurred_at"],
            )
        )
    return out
