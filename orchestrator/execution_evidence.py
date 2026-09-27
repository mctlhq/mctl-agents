"""`ExecutionEvidence` — the immutable, content-addressed audit record that
joins execution identity, policy decisions, approvals and artifacts into one
document per governed execution (mctlhq/mctl-agents#199, ADR 015:
docs/adr/015-execution-evidence-contract.md).

Four inputs already exist and none of them is joined: execution traces
(`orchestrator/tracing.py`, #195 — not exported in production, lossy by
design), execution identity (`orchestrator/execution_identity.py`, ADR 011 —
sealed, content-addressed), policy decisions (`orchestrator/policy_checkpoint.py`,
ADR 014 — fail-closed, one `POLICY_DECISION <json>` line per decision) and
durable approvals (`orchestrator/action_approvals.py`, mctl-api#366). ADR 009
already reserved the seam this module fills: `ContextSnapshot.evidence_refs`
carries `{evidence_id, kind}` and nothing else (ADR 009 sec. 5: "Evidence
(#199) owns the referent").

Stdlib only, deliberately, mirroring `orchestrator/context_snapshot.py`,
`orchestrator/execution_identity.py` and `orchestrator/policy_checkpoint.py`:
a frozen-dataclass schema, the same `sha256:`-prefixed content-hash rule
(`context_snapshot.canonical_json` / `hash_bytes`, never reinvented), and a
validator. This module imports nothing from `orchestrator.policy_checkpoint`,
`orchestrator.execution_identity` or `orchestrator.tracing` — those modules
call INTO this one (via the optional recorder sink), never the other way,
so importing this module can never pull in `httpx`, `claude_agent_sdk` or
the OTel SDK.

No field here ever re-decides anything: `ActionRecord.permitted`/`code`/
`decision` are copies of a decision `policy_checkpoint.decide()` already
made, never a fresh evaluation. Authorization belongs to policy checkpoints
(#197); this module answers "what happened, under which policy, who
approved it, and what did it produce" — after the fact, never before it.
See ADR 015 sec. 6 for the full boundary table.

Redaction: `_safe()` mirrors `orchestrator/tracing_sdk.py`'s `GuardedExporter`
rule exactly — drop (never mask or truncate) a string over 256 characters or
matching a credential shape. Evidence is not exported through
`GuardedExporter` (it never touches the trace pipeline), so this module
carries its own copy of that one rule rather than importing the OTel-SDK-
dependent `tracing_sdk` module.
"""
from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestrator.context_snapshot import canonical_json, hash_bytes

API_VERSION = "evidence.mctl.ai/v1alpha1"
KIND = "ExecutionEvidence"
ID_PREFIX = "ev-"
EVIDENCE_LINE_PREFIX = "EXECUTION_EVIDENCE"

# The complete allow-list, mirroring context_snapshot.py's and
# execution_identity.py's SUPPORTED_API_VERSIONS: a document declaring
# anything else fails loudly in from_dict, never falls back to a default
# shape.
SUPPORTED_API_VERSIONS = {API_VERSION: KIND}

# Closed vocabularies.
CONTEXT_TRUST_VALUES = frozenset({"control-plane", "unverified"})
RETENTION_CLASSES = frozenset({"telemetry", "execution-record", "gitops"})  # ADR 009 sec. 6, reused verbatim
COMPLETENESS_STATUSES = frozenset({"COMPLETE", "INCOMPLETE"})
OUTCOME_STATUSES = frozenset({"SUCCESS", "FAILURE", "REFUSED", "UNDECIDED", "SKIPPED"})
EVALUATION_RESULTS = frozenset({"PASS", "FAIL", "SKIP"})

# Completeness gap vocabulary (ADR 015 sec. 4 / design.md).
GAP_MUTATION_WITHOUT_DECISION = "mutation_without_decision"
GAP_APPROVAL_UNRESOLVED = "approval_unresolved"
GAP_IDENTITY_UNAVAILABLE = "identity_unavailable"
GAP_UNGOVERNED_TRANSPORT = "ungoverned_transport"
GAP_DECISION_WITHOUT_OUTCOME = "decision_without_outcome"
GAP_CODES = frozenset({
    GAP_MUTATION_WITHOUT_DECISION, GAP_APPROVAL_UNRESOLVED, GAP_IDENTITY_UNAVAILABLE,
    GAP_UNGOVERNED_TRANSPORT, GAP_DECISION_WITHOUT_OUTCOME,
})

# Built-in evaluation names.
EVAL_POLICY_COMPLIANCE = "policy_compliance"
EVAL_EVIDENCE_COMPLETENESS = "evidence_completeness"

# Undecided policy-checkpoint codes (orchestrator/policy_checkpoint.py's
# UNDECIDED_CODES) that must never, by themselves, classify a run as
# REFUSED. Duplicated as string literals rather than imported — this module
# stays stdlib-only and import-direction-clean (policy_checkpoint calls into
# this module, not the reverse); a vocabulary mismatch between the two is a
# one-line fix in whichever module drifted, the same trade
# context_snapshot.py already accepts for its WORK_CONTEXT_* duplication.
UNDECIDED_CODES = frozenset({"evaluator_error", "identity_unavailable", "approval_lookup_error"})
#: The one code meaning "permitted, and REQUIRE_APPROVAL's receipt was spent
#: for it" (policy_checkpoint.CODE_APPROVED). Duplicated for the same reason.
DECISION_CODE_APPROVED = "approved"

# Artifact kinds whose presence implies a mutation happened (design.md:
# "A recorded mutation is any ActionRecord with mutation=True ... or any
# artifact of kind branch/pull_request/merge_commit").
MUTATING_ARTIFACT_KINDS = frozenset({"branch", "pull_request", "merge_commit"})

# Action kinds classified as mutating when a caller does not say so itself
# (EvidenceRecorder.offer_decision's `mutation` parameter). Mirrors
# orchestrator/policy_checkpoint.py's own action_kind constants plus the
# "comment" mutation classification orchestrator/tracing.py's `_GH_MUTATING`
# already encodes for `gh ... comment`. String literals only, never
# imported, for the same import-direction reason as UNDECIDED_CODES above.
_MUTATING_ACTION_KINDS = frozenset({
    "github.issue.comment",
    "github.git.push",
    "github.pull_request.create",
    "github.pull_request.merge",
    "github.pull_request.comment",
    "github.actions.run.rerun",
    "github.issue.label",
    "mctl.work_item.write",
})

_MAX_SAFE_STRING_CHARS = 256

# Credential SHAPES, copied verbatim from orchestrator/tracing_sdk.py's
# `_CREDENTIAL_VALUE` (GitHub tokens, fine-grained PATs, sk- keys, Vault
# tokens, JWTs, PEM private keys, bearer headers, basic-auth URLs). Kept as
# a literal copy, not an import: tracing_sdk.py imports the OTel SDK at
# module scope, and this module must stay importable without it.
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|hv[sbr]\.[A-Za-z0-9_-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:bearer\s+[A-Za-z0-9._~+/-]{12,})"
    r"|://[^/\s:@]+:[^/\s@]+@)"
)

# The closed target_ref allowlist (design.md / requirements.md): a target
# is admitted as a bounded, already-public reference only when it matches
# one of these shapes; anything else is reduced to target_digest.
_GITHUB_ISSUE_PR_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[0-9]+/?$"
)
_OWNER_REPO_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[A-Za-z0-9._/-]+$")
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PREFIXED_ID_RE = re.compile(r"^(?:aar_|we_|ex-|cs-)[A-Za-z0-9_-]+$")
# An mctl-api "operation id" shape, e.g. policy_checkpoint's own
# "execute:mctl-agents-investigate" (BUILTIN_POLICY's mctl-investigate rule).
_MCTL_OPERATION_ID_RE = re.compile(r"^[a-z][a-z0-9_]*:[A-Za-z0-9][A-Za-z0-9._-]*$")


class ExecutionEvidenceError(ValueError):
    """Fail-closed schema/validation failure. Every raise site below is
    either a structural problem (`from_dict`: wrong type, unknown key,
    unsupported `api_version`/`kind`) or a semantic one (`validate`: closed
    vocabulary violation). Non-retryable: callers fix the document, never
    catch this to fall back to a default shape.

    Never raised by `EvidenceRecorder` or by anything reachable from
    `seal()`'s normal construction path with well-formed inputs — see
    `EvidenceRecorder`'s docstring for the total, never-raises contract the
    recorder itself keeps."""


def _reject_unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ExecutionEvidenceError(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _require_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionEvidenceError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _require_str(value: Any, *, where: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ExecutionEvidenceError(f"{where} must be a {'string' if allow_empty else 'non-empty string'}")
    return value


def _require_int(value: Any, *, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ExecutionEvidenceError(f"{where} must be an int")
    return value


def _require_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ExecutionEvidenceError(f"{where} must be a bool")
    return value


def _optional_str(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where=where)


def _require_sha256(value: Any, *, where: str) -> str:
    text = _require_str(value, where=where, allow_empty=False)
    if not text.startswith("sha256:"):
        raise ExecutionEvidenceError(f"{where} must carry the 'sha256:' prefix, got {text!r}")
    return text


# ---------------------------------------------------------------------------
# Redaction guard (task 4)
# ---------------------------------------------------------------------------


def _safe(value: Any) -> Any:
    """Drop (never mask or truncate) a string over 256 characters or
    matching a credential shape, mirroring `tracing_sdk.GuardedExporter`'s
    rule. Non-string values pass through unchanged. Returns `None` for a
    dropped string, so a caller building an optional field gets an absent
    field rather than a present-but-wrong one."""
    if not isinstance(value, str):
        return value
    if len(value) > _MAX_SAFE_STRING_CHARS or _CREDENTIAL_VALUE_RE.search(value):
        return None
    return value


def _safe_str(value: Any) -> str | None:
    """`_safe`, narrowed to strings: a non-string input is dropped too
    (never coerced), matching every offer_* method's "safe or absent"
    contract."""
    return _safe(value) if isinstance(value, str) else None


def _target_ref_allowed(target: str) -> bool:
    return bool(
        _GITHUB_ISSUE_PR_URL_RE.match(target)
        or _OWNER_REPO_REF_RE.match(target)
        or _OWNER_REPO_RE.match(target)
        or _PREFIXED_ID_RE.match(target)
        or _MCTL_OPERATION_ID_RE.match(target)
    )


def _split_target(raw: Any) -> tuple[str | None, str | None]:
    """(target_ref, target_digest) — the split design.md and requirements.md
    both pin: `target_ref` only for a closed, already-public shape,
    `target_digest` (a `sha256:`-prefixed hash of the raw string, always
    safe regardless of length or content) otherwise. A digest never leaks
    the string it was computed from, so no length/credential check applies
    to the digest branch — only to the target_ref branch, where the
    allowlisted shapes are already tightly bounded, but the check stays
    here as defense in depth."""
    if not isinstance(raw, str) or not raw:
        return None, None
    if _target_ref_allowed(raw) and len(raw) <= _MAX_SAFE_STRING_CHARS and not _CREDENTIAL_VALUE_RE.search(raw):
        return raw, None
    return None, hash_bytes(raw.encode("utf-8", errors="replace"))


def _infer_mutation(action_kind: Any) -> bool:
    return isinstance(action_kind, str) and action_kind in _MUTATING_ACTION_KINDS


# ---------------------------------------------------------------------------
# Nested blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Who asked — the same shape as `execution_identity.Actor`, projected
    rather than imported (see module docstring on import direction)."""

    type: str = ""
    id: str = ""
    verification: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "verification": self.verification}

    @classmethod
    def from_dict(cls, data: Any) -> Actor:
        mapping = _require_mapping(data, where="actor")
        _reject_unknown_keys(mapping, frozenset({"type", "id", "verification"}), where="actor")
        return cls(
            type=_require_str(mapping.get("type", ""), where="actor.type"),
            id=_require_str(mapping.get("id", ""), where="actor.id"),
            verification=_require_str(mapping.get("verification", "unverified"), where="actor.verification"),
        )


@dataclass(frozen=True)
class Executor:
    """Which agent/service identity ran — the same shape as
    `execution_identity.Executor`, projected rather than imported."""

    type: str = ""
    id: str = ""
    agent: str = ""
    version: str = ""
    image_ref: str = ""
    binding: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "agent": self.agent,
            "version": self.version,
            "image_ref": self.image_ref,
            "binding": self.binding,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Executor:
        mapping = _require_mapping(data, where="executor")
        _reject_unknown_keys(
            mapping, frozenset({"type", "id", "agent", "version", "image_ref", "binding"}), where="executor"
        )
        return cls(
            type=_require_str(mapping.get("type", ""), where="executor.type"),
            id=_require_str(mapping.get("id", ""), where="executor.id"),
            agent=_require_str(mapping.get("agent", ""), where="executor.agent"),
            version=_require_str(mapping.get("version", ""), where="executor.version"),
            image_ref=_require_str(mapping.get("image_ref", ""), where="executor.image_ref"),
            binding=_require_str(mapping.get("binding", ""), where="executor.binding"),
        )


@dataclass(frozen=True)
class ExecutionBlock:
    """Correlation ids for the execution this evidence covers — a superset
    of `execution_identity.Correlation`/`ExecutionContext`'s own fields plus
    the work-item and wall-clock fields evidence alone needs."""

    trace_id: str = ""
    context_id: str = ""
    parent_context_id: str | None = None
    workflow_type: str = ""
    step_sequence: int = 0
    temporal_workflow_id: str = ""
    temporal_run_id: str | None = None
    argo_workflow_name: str | None = None
    attempt: int = 0
    work_item_id: str = ""
    store_execution_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "context_id": self.context_id,
            "parent_context_id": self.parent_context_id,
            "workflow_type": self.workflow_type,
            "step_sequence": self.step_sequence,
            "temporal_workflow_id": self.temporal_workflow_id,
            "temporal_run_id": self.temporal_run_id,
            "argo_workflow_name": self.argo_workflow_name,
            "attempt": self.attempt,
            "work_item_id": self.work_item_id,
            "store_execution_id": self.store_execution_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ExecutionBlock:
        mapping = _require_mapping(data, where="execution")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "trace_id", "context_id", "parent_context_id", "workflow_type", "step_sequence",
                "temporal_workflow_id", "temporal_run_id", "argo_workflow_name", "attempt", "work_item_id",
                "store_execution_id", "started_at", "completed_at", "duration_ms",
            }),
            where="execution",
        )
        return cls(
            trace_id=_require_str(mapping.get("trace_id", ""), where="execution.trace_id"),
            context_id=_require_str(mapping.get("context_id", ""), where="execution.context_id"),
            parent_context_id=_optional_str(mapping.get("parent_context_id"), where="execution.parent_context_id"),
            workflow_type=_require_str(mapping.get("workflow_type", ""), where="execution.workflow_type"),
            step_sequence=_require_int(mapping.get("step_sequence", 0), where="execution.step_sequence"),
            temporal_workflow_id=_require_str(
                mapping.get("temporal_workflow_id", ""), where="execution.temporal_workflow_id"
            ),
            temporal_run_id=_optional_str(mapping.get("temporal_run_id"), where="execution.temporal_run_id"),
            argo_workflow_name=_optional_str(
                mapping.get("argo_workflow_name"), where="execution.argo_workflow_name"
            ),
            attempt=_require_int(mapping.get("attempt", 0), where="execution.attempt"),
            work_item_id=_require_str(mapping.get("work_item_id", ""), where="execution.work_item_id"),
            store_execution_id=_require_str(
                mapping.get("store_execution_id", ""), where="execution.store_execution_id"
            ),
            started_at=_require_str(mapping.get("started_at", ""), where="execution.started_at"),
            completed_at=_require_str(mapping.get("completed_at", ""), where="execution.completed_at"),
            duration_ms=_require_int(mapping.get("duration_ms", 0), where="execution.duration_ms"),
        )


@dataclass(frozen=True)
class IdentityBlock:
    """Who/what ran, projected from `execution_identity.ExecutionContext`.
    `context_trust` is `control-plane` when a verified context was
    available, `unverified` otherwise (requirements.md: "IF no control-plane
    context is available THEN ... mark identity.context_trust as
    unverified")."""

    actor: Actor
    executor: Executor
    environment: str = ""
    tenant: str = ""
    repository: str = ""
    target_repository_sha: str = ""
    context_trust: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor.to_dict(),
            "executor": self.executor.to_dict(),
            "environment": self.environment,
            "tenant": self.tenant,
            "repository": self.repository,
            "target_repository_sha": self.target_repository_sha,
            "context_trust": self.context_trust,
        }

    @classmethod
    def from_dict(cls, data: Any) -> IdentityBlock:
        mapping = _require_mapping(data, where="identity")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "actor", "executor", "environment", "tenant", "repository", "target_repository_sha",
                "context_trust",
            }),
            where="identity",
        )
        return cls(
            actor=Actor.from_dict(mapping.get("actor", {})),
            executor=Executor.from_dict(mapping.get("executor", {})),
            environment=_require_str(mapping.get("environment", ""), where="identity.environment"),
            tenant=_require_str(mapping.get("tenant", ""), where="identity.tenant"),
            repository=_require_str(mapping.get("repository", ""), where="identity.repository"),
            target_repository_sha=_require_str(
                mapping.get("target_repository_sha", ""), where="identity.target_repository_sha"
            ),
            context_trust=_require_str(mapping.get("context_trust", "unverified"), where="identity.context_trust"),
        )


@dataclass(frozen=True)
class ModelUse:
    """Per-`(provider, model)` usage, summed across turns. `usage_record_ref`
    stays empty until ADR 012's usage ledger exists — it is this field's
    reserved slot, not a computed cost."""

    provider: str
    model: str
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_record_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_record_ref": self.usage_record_ref,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ModelUse:
        mapping = _require_mapping(data, where="model_use")
        _reject_unknown_keys(
            mapping,
            frozenset({"provider", "model", "turns", "input_tokens", "output_tokens", "usage_record_ref"}),
            where="model_use",
        )
        return cls(
            provider=_require_str(mapping.get("provider", ""), where="model_use.provider"),
            model=_require_str(mapping.get("model", ""), where="model_use.model"),
            turns=_require_int(mapping.get("turns", 0), where="model_use.turns"),
            input_tokens=_require_int(mapping.get("input_tokens", 0), where="model_use.input_tokens"),
            output_tokens=_require_int(mapping.get("output_tokens", 0), where="model_use.output_tokens"),
            usage_record_ref=_require_str(mapping.get("usage_record_ref", ""), where="model_use.usage_record_ref"),
        )


@dataclass(frozen=True)
class ActionRecord:
    """One governed action, copied from a `policy_checkpoint.Decision` and
    its `ActionRequest` — never re-evaluated. `target_ref`/`target_digest`
    are mutually exclusive (see `_split_target`); `mutation` says whether
    this action's kind is treated as a mutation for completeness purposes."""

    sequence: int
    action_kind: str
    operation: str
    args_digest: str
    action_digest: str
    policy_version: str
    rule_id: str
    decision: str
    code: str
    permitted: bool
    undecided: bool
    mutation: bool
    target_ref: str | None = None
    target_digest: str | None = None
    approval_ref: str = ""
    at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "action_kind": self.action_kind,
            "operation": self.operation,
            "target_ref": self.target_ref,
            "target_digest": self.target_digest,
            "args_digest": self.args_digest,
            "action_digest": self.action_digest,
            "policy_version": self.policy_version,
            "rule_id": self.rule_id,
            "decision": self.decision,
            "code": self.code,
            "permitted": self.permitted,
            "undecided": self.undecided,
            "mutation": self.mutation,
            "approval_ref": self.approval_ref,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ActionRecord:
        mapping = _require_mapping(data, where="action")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "sequence", "action_kind", "operation", "target_ref", "target_digest", "args_digest",
                "action_digest", "policy_version", "rule_id", "decision", "code", "permitted", "undecided",
                "mutation", "approval_ref", "at",
            }),
            where="action",
        )
        return cls(
            sequence=_require_int(mapping.get("sequence", 0), where="action.sequence"),
            action_kind=_require_str(mapping.get("action_kind", ""), where="action.action_kind"),
            operation=_require_str(mapping.get("operation", ""), where="action.operation"),
            target_ref=_optional_str(mapping.get("target_ref"), where="action.target_ref"),
            target_digest=_optional_str(mapping.get("target_digest"), where="action.target_digest"),
            args_digest=_require_str(mapping.get("args_digest", ""), where="action.args_digest"),
            action_digest=_require_str(mapping.get("action_digest", ""), where="action.action_digest"),
            policy_version=_require_str(mapping.get("policy_version", ""), where="action.policy_version"),
            rule_id=_require_str(mapping.get("rule_id", ""), where="action.rule_id"),
            decision=_require_str(mapping.get("decision", ""), where="action.decision"),
            code=_require_str(mapping.get("code", ""), where="action.code"),
            permitted=_require_bool(mapping.get("permitted", False), where="action.permitted"),
            undecided=_require_bool(mapping.get("undecided", False), where="action.undecided"),
            mutation=_require_bool(mapping.get("mutation", False), where="action.mutation"),
            approval_ref=_require_str(mapping.get("approval_ref", ""), where="action.approval_ref"),
            at=_require_str(mapping.get("at", ""), where="action.at"),
        )


@dataclass(frozen=True)
class ApprovalRecordRef:
    """One durable approval receipt (mctl-api#366), copied from
    `GET /action-approvals/{id}` — never re-decided."""

    receipt_id: str
    intent_hash: str = ""
    state: str = ""
    approver: str = ""
    requested_at: str = ""
    decided_at: str = ""
    consumed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "intent_hash": self.intent_hash,
            "state": self.state,
            "approver": self.approver,
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ApprovalRecordRef:
        mapping = _require_mapping(data, where="approval")
        _reject_unknown_keys(
            mapping,
            frozenset({
                "receipt_id", "intent_hash", "state", "approver", "requested_at", "decided_at", "consumed_at",
            }),
            where="approval",
        )
        return cls(
            receipt_id=_require_str(mapping.get("receipt_id", ""), where="approval.receipt_id"),
            intent_hash=_require_str(mapping.get("intent_hash", ""), where="approval.intent_hash"),
            state=_require_str(mapping.get("state", ""), where="approval.state"),
            approver=_require_str(mapping.get("approver", ""), where="approval.approver"),
            requested_at=_require_str(mapping.get("requested_at", ""), where="approval.requested_at"),
            decided_at=_require_str(mapping.get("decided_at", ""), where="approval.decided_at"),
            consumed_at=_require_str(mapping.get("consumed_at", ""), where="approval.consumed_at"),
        )


@dataclass(frozen=True)
class ArtifactRecord:
    """Something this execution produced — never its bytes: `content_hash`
    over what was written, a bounded `locator`, never the content."""

    name: str
    kind: str
    content_hash: str
    byte_count: int = 0
    locator: str = ""
    immutable_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "content_hash": self.content_hash,
            "byte_count": self.byte_count,
            "locator": self.locator,
            "immutable_ref": self.immutable_ref,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ArtifactRecord:
        mapping = _require_mapping(data, where="artifact")
        _reject_unknown_keys(
            mapping,
            frozenset({"name", "kind", "content_hash", "byte_count", "locator", "immutable_ref"}),
            where="artifact",
        )
        return cls(
            name=_require_str(mapping.get("name", ""), where="artifact.name"),
            kind=_require_str(mapping.get("kind", ""), where="artifact.kind"),
            content_hash=_require_str(mapping.get("content_hash", ""), where="artifact.content_hash"),
            byte_count=_require_int(mapping.get("byte_count", 0), where="artifact.byte_count"),
            locator=_require_str(mapping.get("locator", ""), where="artifact.locator"),
            immutable_ref=_require_str(mapping.get("immutable_ref", ""), where="artifact.immutable_ref"),
        )


@dataclass(frozen=True)
class Evaluation:
    """One named check over the sealed record — `policy_compliance` and
    `evidence_completeness` are built in; #60 may add more without a schema
    change."""

    name: str
    result: str
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "result": self.result, "code": self.code}

    @classmethod
    def from_dict(cls, data: Any) -> Evaluation:
        mapping = _require_mapping(data, where="evaluation")
        _reject_unknown_keys(mapping, frozenset({"name", "result", "code"}), where="evaluation")
        return cls(
            name=_require_str(mapping.get("name", ""), where="evaluation.name"),
            result=_require_str(mapping.get("result", ""), where="evaluation.result"),
            code=_require_str(mapping.get("code", ""), where="evaluation.code"),
        )


@dataclass(frozen=True)
class Gap:
    """One reason `completeness.status` is `INCOMPLETE` — a code from the
    closed `GAP_CODES` vocabulary plus the action it names, when there is
    one."""

    code: str
    action_kind: str = ""
    operation: str = ""
    detail_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "action_kind": self.action_kind,
            "operation": self.operation,
            "detail_code": self.detail_code,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Gap:
        mapping = _require_mapping(data, where="gap")
        _reject_unknown_keys(mapping, frozenset({"code", "action_kind", "operation", "detail_code"}), where="gap")
        return cls(
            code=_require_str(mapping.get("code", ""), where="gap.code", allow_empty=False),
            action_kind=_require_str(mapping.get("action_kind", ""), where="gap.action_kind"),
            operation=_require_str(mapping.get("operation", ""), where="gap.operation"),
            detail_code=_require_str(mapping.get("detail_code", ""), where="gap.detail_code"),
        )


@dataclass(frozen=True)
class Completeness:
    """`COMPLETE` iff `gaps` is empty (requirements.md: "WHEN
    completeness.gaps is empty THE SYSTEM SHALL set completeness.status to
    COMPLETE, and otherwise to INCOMPLETE")."""

    status: str = "INCOMPLETE"
    gaps: tuple[Gap, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "gaps": [g.to_dict() for g in self.gaps]}

    @classmethod
    def from_dict(cls, data: Any) -> Completeness:
        mapping = _require_mapping(data, where="completeness")
        _reject_unknown_keys(mapping, frozenset({"status", "gaps"}), where="completeness")
        gaps_raw = mapping.get("gaps", [])
        if not isinstance(gaps_raw, list):
            raise ExecutionEvidenceError("completeness.gaps must be a list")
        return cls(
            status=_require_str(mapping.get("status", "INCOMPLETE"), where="completeness.status"),
            gaps=tuple(Gap.from_dict(g) for g in gaps_raw),
        )


@dataclass(frozen=True)
class Outcome:
    """The run's own result — `status` is closed, `code` is bounded, and
    there is no free-text message field (requirements.md: "no free-text
    message")."""

    status: str
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "code": self.code}

    @classmethod
    def from_dict(cls, data: Any) -> Outcome:
        mapping = _require_mapping(data, where="outcome")
        _reject_unknown_keys(mapping, frozenset({"status", "code"}), where="outcome")
        return cls(
            status=_require_str(mapping.get("status", ""), where="outcome.status", allow_empty=False),
            code=_require_str(mapping.get("code", ""), where="outcome.code"),
        )


@dataclass(frozen=True)
class Retention:
    """Which store honours this document and for how long — ADR 009's
    vocabulary reused verbatim. The Python attribute is `class_` (`class` is
    a reserved word); the wire/JSON key is `class`."""

    class_: str
    expires_after_days: int

    def to_dict(self) -> dict[str, Any]:
        return {"class": self.class_, "expires_after_days": self.expires_after_days}

    @classmethod
    def from_dict(cls, data: Any) -> Retention:
        mapping = _require_mapping(data, where="retention")
        _reject_unknown_keys(mapping, frozenset({"class", "expires_after_days"}), where="retention")
        return cls(
            class_=_require_str(mapping.get("class", ""), where="retention.class", allow_empty=False),
            expires_after_days=_require_int(
                mapping.get("expires_after_days", 0), where="retention.expires_after_days"
            ),
        )


# ---------------------------------------------------------------------------
# ExecutionEvidence — the top-level document
# ---------------------------------------------------------------------------

_EVIDENCE_KEYS = frozenset({
    "api_version", "kind", "evidence_id", "content_hash", "created_at", "execution", "identity", "models",
    "actions", "approvals", "artifacts", "evaluations", "completeness", "outcome", "retention",
})


@dataclass(frozen=True)
class ExecutionEvidence:
    """One immutable, content-addressed statement of what happened in one
    governed execution. Only ever produced by `seal()`; `from_dict`
    reconstructs an already-sealed document and re-validates its shape, but
    never recomputes the hash — use `recompute_content_hash` (or
    `is_trustworthy`) to verify one."""

    api_version: str
    kind: str
    evidence_id: str
    content_hash: str
    created_at: str
    execution: ExecutionBlock
    identity: IdentityBlock
    models: tuple[ModelUse, ...]
    actions: tuple[ActionRecord, ...]
    approvals: tuple[ApprovalRecordRef, ...]
    artifacts: tuple[ArtifactRecord, ...]
    evaluations: tuple[Evaluation, ...]
    completeness: Completeness
    outcome: Outcome
    retention: Retention

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "evidence_id": self.evidence_id,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "execution": self.execution.to_dict(),
            "identity": self.identity.to_dict(),
            "models": [m.to_dict() for m in self.models],
            "actions": [a.to_dict() for a in self.actions],
            "approvals": [a.to_dict() for a in self.approvals],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "evaluations": [e.to_dict() for e in self.evaluations],
            "completeness": self.completeness.to_dict(),
            "outcome": self.outcome.to_dict(),
            "retention": self.retention.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionEvidence:
        mapping = _require_mapping(data, where="ExecutionEvidence")
        _reject_unknown_keys(mapping, _EVIDENCE_KEYS, where="ExecutionEvidence")

        api_version_raw = mapping.get("api_version")
        expected_kind = SUPPORTED_API_VERSIONS.get(api_version_raw) if isinstance(api_version_raw, str) else None
        if not isinstance(api_version_raw, str) or expected_kind is None:
            raise ExecutionEvidenceError(
                f"unsupported api_version {api_version_raw!r}, expected one of {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        api_version = api_version_raw
        kind_raw = mapping.get("kind")
        if kind_raw != expected_kind:
            raise ExecutionEvidenceError(
                f"kind must be {expected_kind!r} for api_version {api_version!r}, got {kind_raw!r}"
            )
        kind = kind_raw

        evidence_id = _require_str(mapping.get("evidence_id"), where="evidence_id", allow_empty=False)
        content_hash = _require_sha256(mapping.get("content_hash"), where="content_hash")
        created_at = _require_str(mapping.get("created_at", ""), where="created_at")

        execution = ExecutionBlock.from_dict(mapping.get("execution", {}))
        identity = IdentityBlock.from_dict(mapping.get("identity", {}))

        models_raw = mapping.get("models", [])
        if not isinstance(models_raw, list):
            raise ExecutionEvidenceError("models must be a list")
        models = tuple(ModelUse.from_dict(m) for m in models_raw)

        actions_raw = mapping.get("actions", [])
        if not isinstance(actions_raw, list):
            raise ExecutionEvidenceError("actions must be a list")
        actions = tuple(ActionRecord.from_dict(a) for a in actions_raw)

        approvals_raw = mapping.get("approvals", [])
        if not isinstance(approvals_raw, list):
            raise ExecutionEvidenceError("approvals must be a list")
        approvals = tuple(ApprovalRecordRef.from_dict(a) for a in approvals_raw)

        artifacts_raw = mapping.get("artifacts", [])
        if not isinstance(artifacts_raw, list):
            raise ExecutionEvidenceError("artifacts must be a list")
        artifacts = tuple(ArtifactRecord.from_dict(a) for a in artifacts_raw)

        evaluations_raw = mapping.get("evaluations", [])
        if not isinstance(evaluations_raw, list):
            raise ExecutionEvidenceError("evaluations must be a list")
        evaluations = tuple(Evaluation.from_dict(e) for e in evaluations_raw)

        completeness = Completeness.from_dict(mapping.get("completeness", {}))
        outcome = Outcome.from_dict(mapping.get("outcome", {}))
        retention = Retention.from_dict(mapping.get("retention", {}))

        record = cls(
            api_version=api_version,
            kind=kind,
            evidence_id=evidence_id,
            content_hash=content_hash,
            created_at=created_at,
            execution=execution,
            identity=identity,
            models=models,
            actions=actions,
            approvals=approvals,
            artifacts=artifacts,
            evaluations=evaluations,
            completeness=completeness,
            outcome=outcome,
            retention=retention,
        )
        record.validate()
        return record

    def validate(self) -> None:
        """Everything `from_dict`'s shape checks and `seal()`'s construction
        cannot: closed vocabularies. Raises `ExecutionEvidenceError`; never
        silently coerces or drops a field."""
        if self.api_version != API_VERSION:
            raise ExecutionEvidenceError(f"api_version must be {API_VERSION!r}, got {self.api_version!r}")
        if self.kind != KIND:
            raise ExecutionEvidenceError(f"kind must be {KIND!r}, got {self.kind!r}")
        if not self.content_hash.startswith("sha256:"):
            raise ExecutionEvidenceError(f"content_hash must carry the 'sha256:' prefix, got {self.content_hash!r}")
        if self.identity.context_trust not in CONTEXT_TRUST_VALUES:
            raise ExecutionEvidenceError(
                f"identity.context_trust {self.identity.context_trust!r} is not one of "
                f"{sorted(CONTEXT_TRUST_VALUES)!r}"
            )
        if self.retention.class_ not in RETENTION_CLASSES:
            raise ExecutionEvidenceError(
                f"retention.class {self.retention.class_!r} is not one of {sorted(RETENTION_CLASSES)!r}"
            )
        if self.completeness.status not in COMPLETENESS_STATUSES:
            raise ExecutionEvidenceError(
                f"completeness.status {self.completeness.status!r} is not one of "
                f"{sorted(COMPLETENESS_STATUSES)!r}"
            )
        if (self.completeness.status == "COMPLETE") != (len(self.completeness.gaps) == 0):
            raise ExecutionEvidenceError("completeness.status must be COMPLETE if and only if gaps is empty")
        for gap in self.completeness.gaps:
            if gap.code not in GAP_CODES:
                raise ExecutionEvidenceError(f"gap code {gap.code!r} is not one of {sorted(GAP_CODES)!r}")
        if self.outcome.status not in OUTCOME_STATUSES:
            raise ExecutionEvidenceError(
                f"outcome.status {self.outcome.status!r} is not one of {sorted(OUTCOME_STATUSES)!r}"
            )
        for evaluation in self.evaluations:
            if evaluation.result not in EVALUATION_RESULTS:
                raise ExecutionEvidenceError(
                    f"evaluation {evaluation.name!r}: result {evaluation.result!r} is not one of "
                    f"{sorted(EVALUATION_RESULTS)!r}"
                )
        for action in self.actions:
            if action.target_ref is not None and action.target_digest is not None:
                raise ExecutionEvidenceError(
                    f"action {action.sequence}: target_ref and target_digest are mutually exclusive"
                )


def _content_payload(
    *,
    execution: ExecutionBlock,
    identity: IdentityBlock,
    models: Sequence[ModelUse],
    actions: Sequence[ActionRecord],
    approvals: Sequence[ApprovalRecordRef],
    artifacts: Sequence[ArtifactRecord],
    evaluations: Sequence[Evaluation],
    completeness: Completeness,
    outcome: Outcome,
    retention: Retention,
) -> dict[str, Any]:
    """Every field that participates in `content_hash` — everything except
    `content_hash`, `evidence_id` and `created_at` (requirements.md)."""
    return {
        "api_version": API_VERSION,
        "kind": KIND,
        "execution": execution.to_dict(),
        "identity": identity.to_dict(),
        "models": [m.to_dict() for m in models],
        "actions": [a.to_dict() for a in actions],
        "approvals": [a.to_dict() for a in approvals],
        "artifacts": [a.to_dict() for a in artifacts],
        "evaluations": [e.to_dict() for e in evaluations],
        "completeness": completeness.to_dict(),
        "outcome": outcome.to_dict(),
        "retention": retention.to_dict(),
    }


def seal(
    *,
    execution: ExecutionBlock,
    identity: IdentityBlock,
    completeness: Completeness,
    outcome: Outcome,
    retention: Retention,
    created_at: str,
    models: Sequence[ModelUse] = (),
    actions: Sequence[ActionRecord] = (),
    approvals: Sequence[ApprovalRecordRef] = (),
    artifacts: Sequence[ArtifactRecord] = (),
    evaluations: Sequence[Evaluation] = (),
) -> ExecutionEvidence:
    """The only constructor that produces a sealed `ExecutionEvidence`.
    Computes `content_hash = "sha256:" + sha256(canonical JSON of every
    field except content_hash, evidence_id, created_at)` and
    `evidence_id = "ev-" + content_hash[7:23]`, mirroring
    `context_snapshot.seal()` and `execution_identity.seal()`. `created_at`
    is caller-supplied and excluded from the hash, so sealing the same
    inputs twice at different clock times yields the same identity. Raises
    `ExecutionEvidenceError` via `validate()` if the assembled document is
    not internally consistent; never returns a partially-sealed document."""
    payload = _content_payload(
        execution=execution, identity=identity, models=models, actions=actions, approvals=approvals,
        artifacts=artifacts, evaluations=evaluations, completeness=completeness, outcome=outcome,
        retention=retention,
    )
    content_hash = hash_bytes(canonical_json(payload))
    evidence_id = ID_PREFIX + content_hash[7:23]
    record = ExecutionEvidence(
        api_version=API_VERSION,
        kind=KIND,
        evidence_id=evidence_id,
        content_hash=content_hash,
        created_at=created_at,
        execution=execution,
        identity=identity,
        models=tuple(models),
        actions=tuple(actions),
        approvals=tuple(approvals),
        artifacts=tuple(artifacts),
        evaluations=tuple(evaluations),
        completeness=completeness,
        outcome=outcome,
        retention=retention,
    )
    record.validate()
    return record


def recompute_content_hash(record: ExecutionEvidence) -> str:
    """Recompute the `content_hash` a fresh `seal()` of `record`'s fields
    (excluding `content_hash`, `evidence_id`, `created_at`) would produce.
    Used to verify a loaded document without mutating it."""
    payload = _content_payload(
        execution=record.execution, identity=record.identity, models=record.models, actions=record.actions,
        approvals=record.approvals, artifacts=record.artifacts, evaluations=record.evaluations,
        completeness=record.completeness, outcome=record.outcome, retention=record.retention,
    )
    return hash_bytes(canonical_json(payload))


def is_trustworthy(record: ExecutionEvidence) -> bool:
    """`True` only if `record.content_hash` matches a fresh recompute AND
    `record.evidence_id` is the id that hash derives — the same double check
    `execution_identity.load_from_environment` makes for `context_id`
    (requirements.md: "IF recompute_content_hash disagrees ... THEN THE
    SYSTEM SHALL treat the document as untrusted"). Never raises: any
    failure while reconstructing the payload (e.g. a value that turned out
    not to be JSON-serializable after tampering) counts as untrustworthy."""
    try:
        expected_content_hash = recompute_content_hash(record)
        expected_evidence_id = ID_PREFIX + expected_content_hash[7:23]
        return expected_content_hash == record.content_hash and expected_evidence_id == record.evidence_id
    except Exception:  # noqa: BLE001 — a document that cannot even be rehashed is untrusted, not an error
        return False


def standing_gap(code: str, *, action_kind: str = "", operation: str = "", detail_code: str = "") -> Gap:
    """Build a caller-supplied standing gap (e.g. `GAP_UNGOVERNED_TRANSPORT`
    for a builder that grants `Bash`) to pass into `check_completeness`'s
    `standing_gaps`."""
    return Gap(code=code, action_kind=action_kind, operation=operation, detail_code=detail_code)


def check_completeness(
    *,
    actions: Sequence[ActionRecord],
    artifacts: Sequence[ArtifactRecord] = (),
    approvals: Sequence[ApprovalRecordRef] = (),
    identity: IdentityBlock | None = None,
    standing_gaps: Sequence[Gap] = (),
) -> Completeness:
    """The issue's "detect incomplete evidence" (mctlhq/mctl-agents#199).

    `standing_gaps` carries what this function cannot detect from the
    record alone — `GAP_UNGOVERNED_TRANSPORT` for a builder that grants
    `Bash` (tasks.md: "a standing/static flag the recorder is told about,
    not something it detects from traces") — and is always included
    unchanged, so `COMPLETE` stays unreachable for such a run.

    Detected gaps:

    - `GAP_MUTATION_WITHOUT_DECISION`: an artifact of a mutating kind
      (`MUTATING_ARTIFACT_KINDS`) exists but no recorded action is both
      `mutation=True` and `permitted`.
    - `GAP_APPROVAL_UNRESOLVED`: an action carries `code == "approved"` and
      an `approval_ref`, but no `ApprovalRecordRef` with that `receipt_id`
      was recorded (the receipt could not be resolved).
    - `GAP_DECISION_WITHOUT_OUTCOME`: an approved action's receipt WAS
      resolved, but its `consumed_at` is empty — approved, but not
      confirmed spent, so the side effect's own outcome is unconfirmed.
    - `GAP_IDENTITY_UNAVAILABLE`: `identity.context_trust != "control-plane"`
      (no control-plane context was loadable)."""
    gaps: list[Gap] = list(standing_gaps)

    mutating_artifacts = [a for a in artifacts if a.kind in MUTATING_ARTIFACT_KINDS]
    permitted_mutation_exists = any(a.mutation and a.permitted for a in actions)
    if mutating_artifacts and not permitted_mutation_exists:
        gaps.append(Gap(code=GAP_MUTATION_WITHOUT_DECISION, detail_code="artifact_without_permitted_decision"))

    approvals_by_receipt = {a.receipt_id: a for a in approvals if a.receipt_id}
    for action in actions:
        if action.code != DECISION_CODE_APPROVED or not action.approval_ref:
            continue
        matched = approvals_by_receipt.get(action.approval_ref)
        if matched is None:
            gaps.append(Gap(code=GAP_APPROVAL_UNRESOLVED, action_kind=action.action_kind, operation=action.operation))
        elif not matched.consumed_at:
            gaps.append(
                Gap(code=GAP_DECISION_WITHOUT_OUTCOME, action_kind=action.action_kind, operation=action.operation)
            )

    if identity is not None and identity.context_trust != "control-plane":
        gaps.append(Gap(code=GAP_IDENTITY_UNAVAILABLE))

    status = "COMPLETE" if not gaps else "INCOMPLETE"
    return Completeness(status=status, gaps=tuple(gaps))


def built_in_evaluations(
    *, actions: Sequence[ActionRecord], completeness: Completeness,
) -> tuple[Evaluation, Evaluation]:
    """`policy_compliance` — `PASS` only if every recorded mutation is
    permitted AND `completeness.status == COMPLETE`; `evidence_completeness`
    — `PASS` iff `completeness.status == COMPLETE`."""
    all_mutations_permitted = all(a.permitted for a in actions if a.mutation)
    complete = completeness.status == "COMPLETE"
    first_gap_code = completeness.gaps[0].code if completeness.gaps else ""
    policy_eval = Evaluation(
        name=EVAL_POLICY_COMPLIANCE,
        result="PASS" if (all_mutations_permitted and complete) else "FAIL",
        code="" if (all_mutations_permitted and complete) else (first_gap_code or "unauthorized_mutation"),
    )
    completeness_eval = Evaluation(
        name=EVAL_EVIDENCE_COMPLETENESS,
        result="PASS" if complete else "FAIL",
        code="" if complete else first_gap_code,
    )
    return policy_eval, completeness_eval


# ---------------------------------------------------------------------------
# EvidenceRecorder — the in-process collector (task 5)
# ---------------------------------------------------------------------------


class EvidenceRecorder:
    """Collects offered decisions, artifacts, model turns and approvals for
    one execution, then `finish()`s them into one sealed `ExecutionEvidence`.

    Every public method is total: it never raises, regardless of what it is
    given. A caller cannot make evidence assembly the reason a governed
    action fails ("evidence never fails a run", requirements.md), so every
    method body is wrapped in a swallow-and-drop guard. With no recorder
    installed (`get_recorder()` returns `None`), a call site's offer is
    skipped entirely — one attribute read, no allocation.

    Thread-safe: the MCP `PreToolUse` hook that would call `offer_decision`
    runs in a worker thread (ADR 014's "Consequences"), so every mutation of
    internal state is taken under one lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._actions: list[ActionRecord] = []
        self._artifacts: list[ArtifactRecord] = []
        self._models: dict[tuple[str, str], ModelUse] = {}
        self._approvals: list[ApprovalRecordRef] = []
        self._next_sequence = 0

    def offer_decision(
        self,
        *,
        action_kind: str,
        operation: str,
        target: str = "",
        args_digest: str = "",
        action_digest: str = "",
        policy_version: str = "",
        rule_id: str = "",
        decision: str = "",
        code: str = "",
        permitted: bool = False,
        undecided: bool = False,
        approval_ref: str = "",
        mutation: bool | None = None,
        at: str = "",
    ) -> None:
        """Offer one `policy_checkpoint.Decision` (and its `ActionRequest`)
        — the fields `policy_checkpoint.decision_record()` already builds,
        so a call site can pass that dict's values through unchanged."""
        try:
            target_ref, target_digest = _split_target(target)
            with self._lock:
                sequence = self._next_sequence
                self._next_sequence += 1
                self._actions.append(ActionRecord(
                    sequence=sequence,
                    action_kind=_safe_str(action_kind) or "",
                    operation=_safe_str(operation) or "",
                    target_ref=target_ref,
                    target_digest=target_digest,
                    args_digest=_safe_str(args_digest) or "",
                    action_digest=_safe_str(action_digest) or "",
                    policy_version=_safe_str(policy_version) or "",
                    rule_id=_safe_str(rule_id) or "",
                    decision=_safe_str(decision) or "",
                    code=_safe_str(code) or "",
                    permitted=bool(permitted),
                    undecided=bool(undecided),
                    mutation=bool(mutation) if mutation is not None else _infer_mutation(action_kind),
                    approval_ref=_safe_str(approval_ref) or "",
                    at=_safe_str(at) or "",
                ))
        except Exception:  # noqa: BLE001, S110 — recording must never fail the action it describes
            pass

    def offer_artifact(
        self, *, name: str, kind: str, content_hash: str = "", byte_count: int = 0, locator: str = "",
        immutable_ref: str = "",
    ) -> None:
        try:
            safe_byte_count = byte_count if isinstance(byte_count, int) and not isinstance(byte_count, bool) else 0
            with self._lock:
                self._artifacts.append(ArtifactRecord(
                    name=_safe_str(name) or "",
                    kind=_safe_str(kind) or "",
                    content_hash=_safe_str(content_hash) or "",
                    byte_count=safe_byte_count,
                    locator=_safe_str(locator) or "",
                    immutable_ref=_safe_str(immutable_ref) or "",
                ))
        except Exception:  # noqa: BLE001, S110 — recording must never fail the write it describes
            pass

    def offer_model_turn(
        self, *, provider: str, model: str, input_tokens: int = 0, output_tokens: int = 0,
    ) -> None:
        try:
            key = (_safe_str(provider) or "", _safe_str(model) or "")
            in_tok = input_tokens if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) else 0
            out_tok = output_tokens if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) else 0
            with self._lock:
                existing = self._models.get(key)
                self._models[key] = ModelUse(
                    provider=key[0],
                    model=key[1],
                    turns=(existing.turns if existing else 0) + 1,
                    input_tokens=(existing.input_tokens if existing else 0) + in_tok,
                    output_tokens=(existing.output_tokens if existing else 0) + out_tok,
                    usage_record_ref=existing.usage_record_ref if existing else "",
                )
        except Exception:  # noqa: BLE001, S110 — recording must never fail the turn it describes
            pass

    def offer_approval(
        self, *, receipt_id: str, intent_hash: str = "", state: str = "", approver: str = "",
        requested_at: str = "", decided_at: str = "", consumed_at: str = "",
    ) -> None:
        try:
            with self._lock:
                self._approvals.append(ApprovalRecordRef(
                    receipt_id=_safe_str(receipt_id) or "",
                    intent_hash=_safe_str(intent_hash) or "",
                    state=_safe_str(state) or "",
                    approver=_safe_str(approver) or "",
                    requested_at=_safe_str(requested_at) or "",
                    decided_at=_safe_str(decided_at) or "",
                    consumed_at=_safe_str(consumed_at) or "",
                ))
        except Exception:  # noqa: BLE001, S110 — recording must never fail the resolution it describes
            pass

    def finish(
        self,
        *,
        execution: ExecutionBlock,
        identity: IdentityBlock,
        retention: Retention,
        outcome: Outcome,
        created_at: str,
        standing_gaps: Sequence[Gap] = (),
    ) -> ExecutionEvidence | None:
        """Seal everything offered so far into one `ExecutionEvidence`.
        Returns `None` (never raises) if sealing could not complete — a
        caller must treat `None` exactly like a failed `seal()`: log once,
        keep the run's own result and exit code unchanged."""
        try:
            with self._lock:
                actions = tuple(self._actions)
                artifacts = tuple(self._artifacts)
                approvals = tuple(self._approvals)
                models = tuple(self._models.values())
            completeness = check_completeness(
                actions=actions, artifacts=artifacts, approvals=approvals, identity=identity,
                standing_gaps=standing_gaps,
            )
            evaluations = built_in_evaluations(actions=actions, completeness=completeness)
            return seal(
                execution=execution, identity=identity, models=models, actions=actions, approvals=approvals,
                artifacts=artifacts, evaluations=evaluations, completeness=completeness, outcome=outcome,
                retention=retention, created_at=created_at,
            )
        except Exception:  # noqa: BLE001 — evidence assembly must never fail the run it describes
            return None


_ACTIVE_RECORDER: EvidenceRecorder | None = None


def set_recorder(recorder: EvidenceRecorder | None) -> None:
    """Install (or clear) the module-level sink. Unset by default, so an
    unwired caller's `get_recorder()` returns `None` and every offer is a
    no-op costing one attribute read."""
    global _ACTIVE_RECORDER
    _ACTIVE_RECORDER = recorder


def get_recorder() -> EvidenceRecorder | None:
    return _ACTIVE_RECORDER


# ---------------------------------------------------------------------------
# Summary line (task 13's shape, kept here so it is testable without wiring
# it into a run entrypoint yet)
# ---------------------------------------------------------------------------


def summary_dict(record: ExecutionEvidence) -> dict[str, Any]:
    """Ids, counts, completeness status and outcome only — no target, no
    digest of arguments (requirements.md: "the line carries ids, counts,
    completeness status and outcome only")."""
    return {
        "evidence_id": record.evidence_id,
        "trace_id": record.execution.trace_id,
        "context_id": record.execution.context_id,
        "temporal_workflow_id": record.execution.temporal_workflow_id,
        "action_count": len(record.actions),
        "artifact_count": len(record.artifacts),
        "approval_count": len(record.approvals),
        "model_count": len(record.models),
        "completeness_status": record.completeness.status,
        "gap_count": len(record.completeness.gaps),
        "outcome_status": record.outcome.status,
        "outcome_code": record.outcome.code,
    }


def emit_summary(record: ExecutionEvidence) -> None:
    """Print one `EXECUTION_EVIDENCE <json>` line, the `POLICY_DECISION` /
    `lifecycle/claim.py:_emit` structured-log convention. Never raises."""
    try:
        import json

        print(f"{EVIDENCE_LINE_PREFIX} {json.dumps(summary_dict(record), sort_keys=True)}", flush=True)
    except Exception:  # noqa: BLE001, S110 — recording must never become the reason a run fails
        pass
