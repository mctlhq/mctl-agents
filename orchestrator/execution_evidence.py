"""`ExecutionEvidence` — the versioned, hashed, tamper-evident envelope that
joins the canonical governance records of one governed execution (mctlhq/
mctl-agents#520, parent #199, ADR 018:
docs/adr/018-execution-evidence-envelope-contract.md, amended by
mctlhq/mctl-agents#539 to add a second typed execution identity).

`ExecutionJoin` carries two distinct, typed identities rather than one
overloaded field: `execution_id` (`we_`, the #196 work execution mctl-api
mints) and `runtime_execution_id` (`ex-`, the ADR 011
`ExecutionContext.context_id` every governed mutation is stamped with).
Each is validated to belong to its own namespace and never the other's;
`ExecutionJoin.primary_execution_ref` derives a single typed `(kind, id)`
retrieval pair from whichever is set, `work` taking precedence when both
are. Three golden fixtures under `tests/fixtures/evidence/` pin the three
join shapes: `investigator-evidence.json` (`we_` only, unchanged),
`implementer-evidence.json` (`ex-` only) and `shepherd-evidence.json`
(both). See ADR 018 Amendment 1 for the full rationale.

ADR 018 Amendment 2 (mctlhq/mctl-agents#199) adds four optional blocks —
`versions` (the resolved ADR 007 release pins), `subject` (what the
evidence is about, bound to the exact revision observed, so PR@SHA1 and
PR@SHA2 never share an identity), `tool_calls` (consequential calls by kind,
name and action digest) and `provenance` (`authority`, `observed_at`,
`supersedes`) — plus the `observation_failed` gap code and
`resolve_current`, the one rule for telling current evidence from
historical. Every new block is hash-neutral when absent: the three
pre-amendment golden fixtures keep their exact `content_hash`.

mctl-agents already has five sealed, canonical governance contracts, each
owned by exactly one store: execution identity (`we_`,
`orchestrator/work_context/`, `orchestrator/execution_identity.py`), context
snapshots (`cs_`/`cs-`, `orchestrator/context_snapshot.py`, ADR 009),
execution requests (`xr_`, `orchestrator/work_context/execution_requests.py`),
human approvals (`aar_`, `orchestrator/action_approvals.py`), policy
decisions (`orchestrator/policy_checkpoint.py`, ADR 014) and the model usage
ledger (`orchestrator/usage_ledger.py`, ADR 012). This module is the one
document that says, for a single governed execution, *which* of those
canonical records applied: a frozen-dataclass schema, a `sha256:`-prefixed
content-hash rule, redaction and a validator. It contains **no persistence,
no store, no retrieval and no I/O of any kind** — Tier B (durable storage
and the retrieval API) is a separate, later mctl-api issue. This module
ships inert and additive: nothing in `run_issue_investigator.py`,
`run_implementer.py`, `run_shepherd.py` or `temporal/workflows/dev_loop.py`
imports it.

PR #483 tried to build the referent as `orchestrator/evidence_store.py`,
writing durable `_evidence/` trees into the public `mctl-gitops` repository
with 3650-day retention. That design is superseded and must not be
continued (ADR 018): governance evidence does not belong in a public repo,
persistence is Tier B and owned by mctl-api, and this module defines no
second store for work items, executions, snapshots, execution requests,
approvals or usage — every block below is a reference, never a copy.

Stdlib only, deliberately, mirroring `orchestrator/context_snapshot.py` and
`orchestrator/policy_checkpoint.py`: only `re`, `dataclasses`,
`collections.abc` and `typing` at module scope, plus three intra-repo
imports, each avoiding a duplicated rule: `hash_bytes`/`canonical_json` from
`orchestrator.context_snapshot` (the one hashing/canonicalization rule),
`UNDECIDED_CODES`/`VERDICTS` from `orchestrator.policy_checkpoint` (the one
undecided-code and verdict vocabulary), and `contains_credential`/
`safe_scalar` from `orchestrator.redaction` (the one credential-shape
screen). No `pathlib`, `os`, `open`, `httpx`, `urllib` or `subprocess`
anywhere, and no second hashing or ad hoc JSON-dump convention.

No field here is ever consumed by an authorization decision. Nothing in
this module records allow/deny/permit/grant. Every reference block carries
ids and hashes only, never a payload, a path, or free text — every
human-facing reason is a slug from a closed vocabulary.
"""
from __future__ import annotations

import re
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestrator import policy_checkpoint as pc
from orchestrator.context_snapshot import canonical_json, hash_bytes
from orchestrator.policy_checkpoint import UNDECIDED_CODES, VERDICTS
from orchestrator.redaction import contains_credential, safe_scalar

API_VERSION = "evidence.mctl.ai/v1alpha1"
KIND = "ExecutionEvidence"

# The complete allow-list, mirroring context_snapshot.SUPPORTED_API_VERSIONS:
# a document declaring anything else fails loudly in from_dict, never falls
# back to a default shape.
SUPPORTED_API_VERSIONS = {API_VERSION: KIND}

EVIDENCE_ID_PREFIX = "ev-"

# ---------------------------------------------------------------------------
# Owning-module prefixes and vocabularies, duplicated on purpose.
#
# Importing orchestrator.work_context.execution_requests, action_approvals or
# orchestrator.usage_ledger at module scope would pull this module into
# their import graphs (usage_ledger imports httpx directly; action_approvals
# and work_context are stdlib-only today but are the client mirrors of a
# store, not this contract's business). context_snapshot.py:84-94 states the
# same rationale for WORK_CONTEXT_SURFACE_KINDS: duplicating a tiny prefix or
# closed vocabulary is deliberate — a divergence is a one-line fix in
# whichever module is wrong — and tests/test_execution_evidence.py's T11
# asserts every one of these equals its owner, so a rename there breaks this
# test rather than silently diverging here.
# ---------------------------------------------------------------------------

#: orchestrator/work_context/snapshots.py:39 EXECUTION_ID_PREFIX
EXECUTION_ID_PREFIX = "we_"
#: orchestrator/work_context/snapshots.py:42 SNAPSHOT_ID_PREFIX (the mctl-api
#: store id)
SNAPSHOT_ID_PREFIX = "cs_"
#: orchestrator/context_snapshot.py's seal() local id ("cs-" + hash[7:23]);
#: not exported as a symbol there, so no equality test is possible — see the
#: "Store id vs local id" open question this proposal resolved.
SNAPSHOT_LOCAL_ID_PREFIX = "cs-"
SNAPSHOT_ID_PREFIXES = (SNAPSHOT_ID_PREFIX, SNAPSHOT_LOCAL_ID_PREFIX)
#: orchestrator/work_context/execution_requests.py:32 REQUEST_ID_PREFIX
REQUEST_ID_PREFIX = "xr_"
#: orchestrator/action_approvals.py:47 ID_PREFIX
APPROVAL_ID_PREFIX = "aar_"
#: orchestrator/execution_identity.py's CONTEXT_ID_PREFIX — the ADR 011
#: ExecutionContext.context_id prefix every governed mutation (policy
#: decisions, aar_ approvals, implementer/shepherd runs) is stamped with,
#: never the #196 work execution's we_.
RUNTIME_EXECUTION_ID_PREFIX = "ex-"
#: The exact shape execution_identity.seal() derives: "ex-" + 16 lowercase
#: hex characters (content_hash[7:23]). A prefix-only check would let
#: arbitrary text through into to_dict(); execution_id keeps a prefix-only
#: check instead because we_ ids are ULIDs minted by mctl-api whose body
#: shape this repository does not own.
_RUNTIME_EXECUTION_ID_PATTERN = re.compile(r"ex-[0-9a-f]{16}")

#: orchestrator/work_context/execution_requests.py KINDS
EXECUTION_REQUEST_KINDS = frozenset({"start", "resume"})
#: orchestrator/work_context/execution_requests.py STATES
EXECUTION_REQUEST_STATES = frozenset({"pending", "claimed", "fulfilled", "rejected"})
#: orchestrator/action_approvals.py's typed answers (PENDING/APPROVED/DENIED/
#: EXPIRED/CONSUMED)
APPROVAL_STATES = frozenset({"pending", "approved", "denied", "expired", "consumed"})
#: orchestrator/usage_ledger.py DEVLOOP_STAGES
USAGE_DEVLOOP_STAGES = frozenset({"investigator", "implementer", "reviewer", "shepherd"})

# Closed vocabularies for this contract's own fields (requirements.md's
# resolved open questions).
OUTCOME_CODES = frozenset({"succeeded", "failed", "refused", "abandoned", "superseded"})
#: `observation_failed` (ADR 018 Amendment 2): the producer tried to observe
#: the referent and the read failed, was partial or was malformed — an
#: unknown, never an observed absence. Unlike the legacy codes it must always
#: be `required=True` (see `_check_gap`), so an unknown can never leave an
#: envelope `COMPLETE`.
GAP_CODES = frozenset({
    "not_produced", "store_unavailable", "not_applicable", "redacted_out", "undecided",
    "observation_failed",
})
#: Gap codes that state "could not observe", which must therefore always be
#: required. Only the Amendment 2 code: the legacy codes keep their
#: caller-chosen flag so no already-sealed envelope is invalidated.
UNKNOWN_GAP_CODES = frozenset({"observation_failed"})
#: Which identity ExecutionJoin.primary_execution_ref is retrieved by:
#: "work" for execution_id (we_), "runtime" for runtime_execution_id (ex-).
EXECUTION_REF_KINDS = frozenset({"work", "runtime"})
BLOCK_NAMES = frozenset({
    "execution", "outcome", "policy_decisions", "snapshot_refs",
    "execution_request", "usage", "approvals", "artifacts",
    # ADR 018 Amendment 2 — every one optional and hash-neutral when absent.
    "versions", "subject", "tool_calls", "provenance",
})

# ---------------------------------------------------------------------------
# ADR 018 Amendment 2 vocabularies (mctlhq/mctl-agents#199).
# ---------------------------------------------------------------------------

#: What an envelope's evidence is about. `pull_request`, `branch` and
#: `release` name a moving pointer, so their `revision` (the git SHA it
#: pointed at when observed) is required: evidence for PR@SHA1 must never be
#: read as evidence for PR@SHA2.
SUBJECT_KINDS = frozenset({"pull_request", "issue", "branch", "release", "work_item"})
#: Subject kinds whose `revision` is a required, full git object id.
SHA_BOUND_SUBJECT_KINDS = frozenset({"pull_request", "branch", "release"})
#: Subject kinds whose `repository` (`owner/name`) is required.
REPOSITORY_SUBJECT_KINDS = frozenset({"pull_request", "issue", "branch", "release"})
#: Subject kinds whose `ref` is a GitHub issue/PR number.
NUMBERED_SUBJECT_KINDS = frozenset({"pull_request", "issue"})

#: Who stands behind the envelope's statements, strongest first. `observed`:
#: read from the system of record (GitHub API, mctl-api, the cluster) by the
#: producer itself. `derived`: computed deterministically from other
#: records. `asserted`: a model's or agent's own claim, not independently
#: verified. Order is the precedence `resolve_current` applies.
AUTHORITIES = ("observed", "derived", "asserted")
AUTHORITY_RANK = {name: len(AUTHORITIES) - index for index, name in enumerate(AUTHORITIES)}

#: The consequential action classes a `ToolCallRef.kind` may name — the
#: governed `ActionRequest.action_kind` values — `policy_checkpoint.
#: ACTION_KINDS` itself, the same way `VERDICTS`/`UNDECIDED_CODES` are reused.
TOOL_CALL_KINDS = pc.ACTION_KINDS
#: A tool call's observed result. `unknown` is the explicit "the producer
#: could not observe the result" state — never folded into `failed` or
#: `succeeded`.
TOOL_CALL_STATUSES = frozenset({"succeeded", "failed", "refused", "unknown"})
MAX_TOOL_CALLS = 256
#: `versions.release_revision` upper bound: Tier B stores it as a signed
#: 64-bit integer, so anything larger must fail here, not at ingest.
MAX_RELEASE_REVISION = 2**63 - 1

#: `resolve_current` result states. `unknown_revision` and `ambiguous` are
#: unknowns, never a substitute for `no_evidence`: `ambiguous` covers both a
#: top-rank tie and a fully superseded pool. `stale_revision` means
#: evidence exists for the subject, only at other revisions.
CURRENT_STATES = frozenset({"current", "no_evidence", "stale_revision", "unknown_revision", "ambiguous"})

_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_NUMBER_REF_PATTERN = re.compile(r"[1-9][0-9]{0,9}")
MAX_SUBJECT_REF_LENGTH = 256
_SUBJECT_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]*")
_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
MAX_VERSION_LENGTH = 128
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]*")
MAX_TOOL_NAME_LENGTH = 128
_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")

COMPLETE = "COMPLETE"
INCOMPLETE = "INCOMPLETE"

# `reason_code`/`outcome.reason_code` are machine-readable slugs, never
# prose: same shape as context_snapshot's MAX_CONFLICT_SUBJECT_LENGTH /
# _CONFLICT_SUBJECT_PATTERN.
MAX_SLUG_LENGTH = 128
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# ArtifactRef.name: bounded, and never a place to smuggle a path — no `/`,
# `\`, `..` or leading `~`.
MAX_ARTIFACT_NAME_LENGTH = 256
_ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# `created_at` is excluded from `content_hash` by design (requirements.md:
# "sealing the same inputs twice with different created_at values SHALL
# produce the identical evidence_id and content_hash" — the same rule
# `context_snapshot.py` and `capability.py` already state for their own
# `created_at`). Exclusion from the hash is not exemption from being a
# bounded, closed-shape field: it is an ISO8601 UTC timestamp
# (`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`), the shape every existing caller,
# fixture and test already uses, and it is bounded and pattern-checked here
# the same way every other schema field is.
MAX_CREATED_AT_LENGTH = 40
# The exact shapes `seal()` produces (`hash_bytes` → "sha256:" + 64 lowercase
# hex; the id is "ev-" + 16 of those hex chars). A prefix check alone let a
# forged, self-consistent pair carry arbitrary text -- a credential, or an
# unbounded blob -- through `from_dict` into `to_dict()` and `to_log_dict()`.
_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_EVIDENCE_ID_PATTERN = re.compile(r"ev-[0-9a-f]{16}")
_CREATED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

# The generic redaction-safety-net cap `_safe()` applies to every leaf
# regardless of which field it came from — mirrors
# `orchestrator/redaction.py`'s MAX_ATTRIBUTE_CHARS. Per-field schema bounds
# (MAX_SLUG_LENGTH, MAX_ARTIFACT_NAME_LENGTH, ...) are enforced separately by
# `validate()`.
MAX_LEAF_CHARS = 256

#: A sentinel distinguishing "this leaf was dropped" from a legitimate None.
_DROPPED = object()

#: The stable value written in place of a dropped leaf inside a mapping.
#: Chosen to equal exactly what every reference block's own `from_dict`
#: already defaults an absent string field to (`mapping.get(field, "")`), so
#: a leaf redacted before hashing at `seal()` time hashes identically when
#: `recompute_content_hash()` later rebuilds the payload from the
#: reconstructed, already-redacted dataclass via its `to_dict()` (which
#: always emits every key -- with one deliberate exception:
#: `ExecutionJoin.to_dict()`'s `runtime_execution_id`, mctlhq/
#: mctl-agents#539. `seal()`'s hash-neutral prune, above, already pops that
#: key from the payload whenever it is blank or redacted before
#: reconstruction; `to_dict()` mirrors the same omission so a rebuilt
#: payload stays byte-identical to the pruned one. Every other block, and
#: every other field of this one, still always emits every key). Omitting
#: a key outright is otherwise unsafe -- it would make a redacted
#: envelope's `content_hash` unreproducible -- which is exactly why this
#: exception is narrow and this comment calls it out by name.
_REDACTED_LEAF = ""


class ExecutionEvidenceError(ValueError):
    """Fail-closed schema/validation failure. Every raise site below is
    either a structural problem (`from_dict`: wrong type, unknown key,
    unsupported `api_version`/`kind`) or a semantic one (`validate`/`seal`:
    closed vocabulary violation, a required block absent and ungapped).
    Non-retryable: callers fix the document, never catch this to fall back
    to a default shape."""


# ---------------------------------------------------------------------------
# Parse helpers — copied and retyped from context_snapshot.py (deliberate
# duplication, context_snapshot.py:84-94's rationale: these are tiny and a
# divergence is a one-line fix, unlike the credential screen or the hash
# rule, which are imported instead).
# ---------------------------------------------------------------------------


def _reject_unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ExecutionEvidenceError(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _require_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionEvidenceError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _require_str(value: Any, *, where: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ExecutionEvidenceError(f"{where} must be a non-empty string")
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
    return _require_str(value, where=where, allow_empty=True)


def _require_sha256(value: Any, *, where: str) -> str:
    text = _require_str(value, where=where)
    if not _SHA256_PATTERN.fullmatch(text):
        raise ExecutionEvidenceError(
            f"{where} must be 'sha256:' followed by 64 lowercase hex characters, got {text[:80]!r}"
        )
    return text


# ---------------------------------------------------------------------------
# Reference blocks — ids and hashes only, never a copy of the referent.
#
# Every leaf string field below allows an empty value at parse time: a blank
# leaf means "absent" (never supplied, or dropped by `_safe()` before
# hashing). Whether an absent, required leaf was properly accounted for by a
# `Gap` is `seal()`'s job (it holds the `Requirements` profile); `validate()`
# checks vocabulary/shape only when a value is present, and skips a blank
# one rather than re-deriving what `seal()` already decided.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionJoin:
    """Joins this envelope to the canonical execution(s) it is evidence for
    — two distinct, typed identities, never one overloaded field (ADR 018
    Amendment 1, mctlhq/mctl-agents#539): `execution_id` is the #196
    identity contract's `we_...` work execution; `runtime_execution_id` is
    the ADR 011 `ExecutionContext.context_id` (`ex-...`) every governed
    mutation — implementer/shepherd runs, policy decisions, `aar_`
    approvals — is stamped with. A reference only: no execution state, no
    `ExecutionContext` field, is copied here. Either may be blank; `seal()`
    requires at least one. Use `primary_execution_ref` for the single typed
    retrieval identity this join implies."""

    execution_id: str = ""
    work_item_id: str = ""
    trace_id: str = ""
    runtime_execution_id: str = ""

    @property
    def primary_execution_ref(self) -> tuple[str, str]:
        """`(kind, id)` with `kind` in `EXECUTION_REF_KINDS`, or `("", "")`
        when neither identity is set. `work` wins when both are present.
        Derived, never stored, never hashed, never accepted by `from_dict`
        or `__init__` as a field — the same rule `ExecutionEvidence.
        completeness` follows."""
        if self.execution_id:
            return ("work", self.execution_id)
        if self.runtime_execution_id:
            return ("runtime", self.runtime_execution_id)
        return ("", "")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "execution_id": self.execution_id,
            "work_item_id": self.work_item_id,
            "trace_id": self.trace_id,
        }
        if self.runtime_execution_id:
            result["runtime_execution_id"] = self.runtime_execution_id
        return result

    @classmethod
    def from_dict(cls, data: Any) -> ExecutionJoin:
        mapping = _require_mapping(data, where="execution")
        _reject_unknown_keys(
            mapping,
            frozenset({"execution_id", "work_item_id", "trace_id", "runtime_execution_id"}),
            where="execution",
        )
        return cls(
            execution_id=_require_str(
                mapping.get("execution_id", ""), where="execution.execution_id", allow_empty=True
            ),
            work_item_id=_require_str(
                mapping.get("work_item_id", ""), where="execution.work_item_id", allow_empty=True
            ),
            trace_id=_require_str(mapping.get("trace_id", ""), where="execution.trace_id", allow_empty=True),
            runtime_execution_id=_require_str(
                mapping.get("runtime_execution_id", ""), where="execution.runtime_execution_id", allow_empty=True
            ),
        )


@dataclass(frozen=True)
class SnapshotRef:
    """One `ContextSnapshot` this execution sealed or consulted — an id plus
    its `sha256:`-prefixed `content_hash`. No source, selector, locator or
    body: `context_snapshot.EvidenceRef` already forbids a payload field on
    the other side of this same seam."""

    snapshot_id: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, data: Any) -> SnapshotRef:
        mapping = _require_mapping(data, where="snapshot_ref")
        _reject_unknown_keys(mapping, frozenset({"snapshot_id", "content_hash"}), where="snapshot_ref")
        return cls(
            snapshot_id=_require_str(
                mapping.get("snapshot_id", ""), where="snapshot_ref.snapshot_id", allow_empty=True
            ),
            content_hash=_require_str(
                mapping.get("content_hash", ""), where="snapshot_ref.content_hash", allow_empty=True
            ),
        )


@dataclass(frozen=True)
class ExecutionRequestRef:
    """The `xr_` execution request that dispatched this execution, if any."""

    request_id: str = ""
    kind: str = ""
    state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "kind": self.kind, "state": self.state}

    @classmethod
    def from_dict(cls, data: Any) -> ExecutionRequestRef:
        mapping = _require_mapping(data, where="execution_request")
        _reject_unknown_keys(mapping, frozenset({"request_id", "kind", "state"}), where="execution_request")
        return cls(
            request_id=_require_str(
                mapping.get("request_id", ""), where="execution_request.request_id", allow_empty=True
            ),
            kind=_require_str(mapping.get("kind", ""), where="execution_request.kind", allow_empty=True),
            state=_require_str(mapping.get("state", ""), where="execution_request.state", allow_empty=True),
        )


@dataclass(frozen=True)
class UsageRef:
    """Join keys into the canonical model usage ledger only — `session_id`,
    optional `result_uuid`, `model_key` and optional `devloop_stage`. No
    token count, no cost figure, no ledger row content: this mirrors the
    ledger's own `(session_id, result_uuid, model_key)` idempotency key, so
    a row is locatable and never copied."""

    session_id: str = ""
    model_key: str = ""
    result_uuid: str | None = None
    devloop_stage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model_key": self.model_key,
            "result_uuid": self.result_uuid,
            "devloop_stage": self.devloop_stage,
        }

    @classmethod
    def from_dict(cls, data: Any) -> UsageRef:
        mapping = _require_mapping(data, where="usage")
        _reject_unknown_keys(
            mapping, frozenset({"session_id", "model_key", "result_uuid", "devloop_stage"}), where="usage"
        )
        return cls(
            session_id=_require_str(mapping.get("session_id", ""), where="usage.session_id", allow_empty=True),
            model_key=_require_str(mapping.get("model_key", ""), where="usage.model_key", allow_empty=True),
            result_uuid=_optional_str(mapping.get("result_uuid"), where="usage.result_uuid"),
            devloop_stage=_optional_str(mapping.get("devloop_stage"), where="usage.devloop_stage"),
        )


@dataclass(frozen=True)
class ApprovalRef:
    """The `aar_` human approval this execution's action relied on, and the
    `intent_hash` it was bound to — never the intent's own fields."""

    approval_id: str = ""
    intent_hash: str = ""
    state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"approval_id": self.approval_id, "intent_hash": self.intent_hash, "state": self.state}

    @classmethod
    def from_dict(cls, data: Any) -> ApprovalRef:
        mapping = _require_mapping(data, where="approval")
        _reject_unknown_keys(mapping, frozenset({"approval_id", "intent_hash", "state"}), where="approval")
        return cls(
            approval_id=_require_str(mapping.get("approval_id", ""), where="approval.approval_id", allow_empty=True),
            intent_hash=_require_str(mapping.get("intent_hash", ""), where="approval.intent_hash", allow_empty=True),
            state=_require_str(mapping.get("state", ""), where="approval.state", allow_empty=True),
        )


@dataclass(frozen=True)
class PolicyDecisionRef:
    """One `policy_checkpoint.Decision` this execution's action produced.
    `undecided` is `self.code in UNDECIDED_CODES` — the same expression
    `Decision.undecided` uses, against the same imported frozenset; there is
    no second list."""

    action_digest: str = ""
    verdict: str = ""
    code: str = ""
    policy_version: str = ""
    rule_id: str = ""
    approval_ref: str = ""

    @property
    def undecided(self) -> bool:
        return self.code in UNDECIDED_CODES

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_digest": self.action_digest,
            "verdict": self.verdict,
            "code": self.code,
            "policy_version": self.policy_version,
            "rule_id": self.rule_id,
            "approval_ref": self.approval_ref,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PolicyDecisionRef:
        mapping = _require_mapping(data, where="policy_decision")
        _reject_unknown_keys(
            mapping,
            frozenset({"action_digest", "verdict", "code", "policy_version", "rule_id", "approval_ref"}),
            where="policy_decision",
        )
        return cls(
            action_digest=_require_str(
                mapping.get("action_digest", ""), where="policy_decision.action_digest", allow_empty=True
            ),
            verdict=_require_str(mapping.get("verdict", ""), where="policy_decision.verdict", allow_empty=True),
            code=_require_str(mapping.get("code", ""), where="policy_decision.code", allow_empty=True),
            policy_version=_require_str(
                mapping.get("policy_version", ""), where="policy_decision.policy_version", allow_empty=True
            ),
            rule_id=_require_str(mapping.get("rule_id", ""), where="policy_decision.rule_id", allow_empty=True),
            approval_ref=_require_str(
                mapping.get("approval_ref", ""), where="policy_decision.approval_ref", allow_empty=True
            ),
        )


@dataclass(frozen=True)
class ArtifactRef:
    """An immutable ref to a generated artifact: a bounded `name`, a `kind`
    and a `sha256:`-prefixed `content_hash`. Never the artifact's bytes."""

    name: str = ""
    kind: str = ""
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, data: Any) -> ArtifactRef:
        mapping = _require_mapping(data, where="artifact")
        _reject_unknown_keys(mapping, frozenset({"name", "kind", "content_hash"}), where="artifact")
        return cls(
            name=_require_str(mapping.get("name", ""), where="artifact.name", allow_empty=True),
            kind=_require_str(mapping.get("kind", ""), where="artifact.kind", allow_empty=True),
            content_hash=_require_str(
                mapping.get("content_hash", ""), where="artifact.content_hash", allow_empty=True
            ),
        )


@dataclass(frozen=True)
class Outcome:
    """The final outcome of the governed execution: a `code` from the closed
    `OUTCOME_CODES` set and a machine-readable `reason_code` slug."""

    code: str = ""
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "reason_code": self.reason_code}

    @classmethod
    def from_dict(cls, data: Any) -> Outcome:
        mapping = _require_mapping(data, where="outcome")
        _reject_unknown_keys(mapping, frozenset({"code", "reason_code"}), where="outcome")
        return cls(
            code=_require_str(mapping.get("code", ""), where="outcome.code", allow_empty=True),
            reason_code=_require_str(mapping.get("reason_code", ""), where="outcome.reason_code", allow_empty=True),
        )


# ---------------------------------------------------------------------------
# ADR 018 Amendment 2 blocks (mctlhq/mctl-agents#199). Each is optional at
# the envelope level and enters the hashed payload only when present, so an
# envelope sealed without them hashes exactly as before this amendment.
# Inside a present block every key is always emitted (blank as ""), the
# rule every pre-amendment block follows.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VersionPins:
    """The resolved release this execution ran under — ADR 007 sec. 4/5's
    `ExecutionPlan` pins, copied field-for-field from the names
    `context_snapshot.ExecutionCorrelation` already uses. References only:
    which definition bytes, which profile, which binding revision, never
    the definition or profile content. `definition_content_hash` is the
    load-bearing pin (ADR 007: `definition_version` names no bytes), so it
    is required whenever the block is present."""

    agent: str = ""
    environment: str = ""
    definition_version: str = ""
    definition_content_hash: str = ""
    profile_name: str = ""
    profile_version: str = ""
    profile_content_hash: str = ""
    release_revision: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "environment": self.environment,
            "definition_version": self.definition_version,
            "definition_content_hash": self.definition_content_hash,
            "profile_name": self.profile_name,
            "profile_version": self.profile_version,
            "profile_content_hash": self.profile_content_hash,
            "release_revision": self.release_revision,
        }

    @classmethod
    def from_dict(cls, data: Any) -> VersionPins:
        mapping = _require_mapping(data, where="versions")
        keys = (
            "agent", "environment", "definition_version", "definition_content_hash",
            "profile_name", "profile_version", "profile_content_hash",
        )
        _reject_unknown_keys(mapping, frozenset(keys) | {"release_revision"}, where="versions")
        values = {
            key: _require_str(mapping.get(key, ""), where=f"versions.{key}", allow_empty=True) for key in keys
        }
        revision_raw = mapping.get("release_revision")
        release_revision = (
            None if revision_raw is None else _require_int(revision_raw, where="versions.release_revision")
        )
        return cls(release_revision=release_revision, **values)


@dataclass(frozen=True)
class SubjectRef:
    """What this evidence is about, bound to the exact version observed:
    `kind` (closed `SUBJECT_KINDS`), `repository` (`owner/name`), `ref`
    (the PR/issue number, branch, tag or work item id) and `revision` (the
    git SHA the pointer resolved to, or a version token). `revision` is
    part of the hashed payload, so PR@SHA1 and PR@SHA2 can never share an
    `evidence_id`, and it is required for every `SHA_BOUND_SUBJECT_KINDS`
    kind — evidence about a moving pointer without the version it pointed
    at is not bound to anything."""

    kind: str = ""
    repository: str = ""
    ref: str = ""
    revision: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """`(kind, repository, ref)` — the version-independent identity
        `resolve_current` groups by. Derived, never stored or hashed."""
        return (self.kind, self.repository, self.ref)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "repository": self.repository, "ref": self.ref, "revision": self.revision}

    @classmethod
    def from_dict(cls, data: Any) -> SubjectRef:
        mapping = _require_mapping(data, where="subject")
        _reject_unknown_keys(mapping, frozenset({"kind", "repository", "ref", "revision"}), where="subject")
        return cls(
            kind=_require_str(mapping.get("kind", ""), where="subject.kind", allow_empty=True),
            repository=_require_str(mapping.get("repository", ""), where="subject.repository", allow_empty=True),
            ref=_require_str(mapping.get("ref", ""), where="subject.ref", allow_empty=True),
            revision=_require_str(mapping.get("revision", ""), where="subject.revision", allow_empty=True),
        )


@dataclass(frozen=True)
class ToolCallRef:
    """One consequential tool call this execution made: `kind` (a governed
    `policy_checkpoint` action kind), the tool/operation `name`, the
    `action_digest` that call's `ActionRequest` hashes to — the same value a
    `PolicyDecisionRef.action_digest` and an `aar_` `intent_hash` bind, so
    the three join without either carrying the arguments — and the observed
    `status`. Never the arguments, the target text or the result."""

    kind: str = ""
    name: str = ""
    action_digest: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "action_digest": self.action_digest, "status": self.status}

    @classmethod
    def from_dict(cls, data: Any) -> ToolCallRef:
        mapping = _require_mapping(data, where="tool_call")
        _reject_unknown_keys(mapping, frozenset({"kind", "name", "action_digest", "status"}), where="tool_call")
        return cls(
            kind=_require_str(mapping.get("kind", ""), where="tool_call.kind", allow_empty=True),
            name=_require_str(mapping.get("name", ""), where="tool_call.name", allow_empty=True),
            action_digest=_require_str(
                mapping.get("action_digest", ""), where="tool_call.action_digest", allow_empty=True
            ),
            status=_require_str(mapping.get("status", ""), where="tool_call.status", allow_empty=True),
        )


@dataclass(frozen=True)
class Provenance:
    """Who stands behind this envelope and when it was true: `authority`
    (closed `AUTHORITIES`, `observed` > `derived` > `asserted`),
    `observed_at` (when the producer observed the state it records — part
    of the hash, unlike `created_at`, because it is a fact about the
    evidence rather than about sealing) and `supersedes` (the `ev-` id of
    the earlier envelope this one explicitly replaces, blank when none).
    Required whenever `subject` is present: `resolve_current` cannot rank
    subject-bound evidence without it."""

    authority: str = ""
    observed_at: str = ""
    supersedes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"authority": self.authority, "observed_at": self.observed_at, "supersedes": self.supersedes}

    @classmethod
    def from_dict(cls, data: Any) -> Provenance:
        mapping = _require_mapping(data, where="provenance")
        _reject_unknown_keys(mapping, frozenset({"authority", "observed_at", "supersedes"}), where="provenance")
        return cls(
            authority=_require_str(mapping.get("authority", ""), where="provenance.authority", allow_empty=True),
            observed_at=_require_str(
                mapping.get("observed_at", ""), where="provenance.observed_at", allow_empty=True
            ),
            supersedes=_require_str(mapping.get("supersedes", ""), where="provenance.supersedes", allow_empty=True),
        )


@dataclass(frozen=True)
class Gap:
    """An explicit statement that a block of evidence is missing: `block`
    names which one (closed set `BLOCK_NAMES`), `code` says why (closed set
    `GAP_CODES`), and `required` says whether that absence makes the
    envelope `INCOMPLETE`. Never silent: "no record" is always a `Gap`, not
    an absent key."""

    block: str
    code: str
    required: bool

    def to_dict(self) -> dict[str, Any]:
        return {"block": self.block, "code": self.code, "required": self.required}

    @classmethod
    def from_dict(cls, data: Any) -> Gap:
        mapping = _require_mapping(data, where="gap")
        _reject_unknown_keys(mapping, frozenset({"block", "code", "required"}), where="gap")
        return cls(
            block=_require_str(mapping.get("block"), where="gap.block"),
            code=_require_str(mapping.get("code"), where="gap.code"),
            required=_require_bool(mapping.get("required"), where="gap.required"),
        )


@dataclass(frozen=True)
class Requirements:
    """Which optional evidence blocks this execution was supposed to
    produce (design.md's resolution of requirements.md's "which blocks are
    required" open question). `execution` and `outcome` are always
    required — `seal()`'s signature already makes them mandatory arguments,
    so they carry no flag here. `policy_decisions` defaults to required,
    matching "at least one policy-decision reference" in the default
    profile; every other block defaults to not-required, since it applies
    only when the execution's own design says it does."""

    policy_decisions: bool = True
    snapshot_refs: bool = False
    execution_request: bool = False
    usage: bool = False
    approvals: bool = False
    artifacts: bool = False
    # ADR 018 Amendment 2: not required by default, so the default profile
    # — and every envelope sealed under it — is unchanged.
    versions: bool = False
    subject: bool = False
    tool_calls: bool = False
    provenance: bool = False


#: The default profile: execution join, outcome and at least one policy
#: decision are required; every other block is required-if-applicable.
DEFAULT_REQUIREMENTS = Requirements()


# ---------------------------------------------------------------------------
# Redaction — every block, no exceptions, before a single byte is hashed.
# ---------------------------------------------------------------------------


#: The ADR 018 Amendment 2 blocks. A redaction inside one of them always
#: yields a required gap: each binds identity or trust (which version,
#: which subject, which call, how authoritative), so a partly dropped one
#: must leave the envelope INCOMPLETE rather than sealed COMPLETE.
AMENDMENT_2_BLOCKS = frozenset({"versions", "subject", "tool_calls", "provenance"})
#: The only required Amendment 2 leaves a legitimate value can lose to
#: `_safe()`: free-form, pattern-bounded strings that may contain a
#: credential shape (a branch named after a token, say). `subject.ref` is
#: redactable ONLY for kinds outside `NUMBERED_SUBJECT_KINDS`: a
#: `pull_request`/`issue` ref is a short decimal `_safe()` can never drop.
#: Every other required leaf is a closed vocabulary, a hash, a git SHA or a
#: timestamp, and is never excused. `_required_leaves` derives each row's
#: `redactable` flag from this set, so the set is the rule.
REDACTABLE_REQUIRED_LEAVES = frozenset({"versions.agent", "subject.ref", "subject.repository"})


@dataclass(frozen=True)
class _RequiredLeaf:
    """One row of the required-leaf table: the contract leaf name, the gap
    block `_safe()` would name for it, its value, whether a redaction gap
    on that block may excuse a blank, and the error message."""

    leaf: str
    gap_block: str
    value: Any
    redactable: bool
    message: str


def _required_leaves(
    *,
    versions: VersionPins | None = None,
    subject: SubjectRef | None = None,
    tool_calls: Sequence[ToolCallRef] = (),
    provenance: Provenance | None = None,
) -> list[_RequiredLeaf]:
    """The single table of required Amendment 2 leaves for the blocks
    present. Drives both seal()'s pre-redaction presence check and
    validate()'s post-redaction one, so the two can never disagree."""
    rows: list[_RequiredLeaf] = []

    def add(leaf: str, gap_block: str, value: Any, message: str, *, redactable: bool = True) -> None:
        rows.append(_RequiredLeaf(
            leaf=leaf, gap_block=gap_block, value=value,
            redactable=redactable and leaf in REDACTABLE_REQUIRED_LEAVES, message=message,
        ))

    if versions is not None:
        add("versions.agent", "versions", versions.agent,
            "versions.agent is required when the versions block is present")
        add("versions.definition_content_hash", "versions", versions.definition_content_hash,
            "versions.definition_content_hash is required when the versions block is present: "
            "definition_version alone names no bytes (ADR 007 sec. 4)")
    if subject is not None:
        add("subject.kind", "subject", subject.kind, "subject.kind is required when the subject block is present")
        add("subject.ref", "subject", subject.ref, "subject.ref is required when the subject block is present",
            redactable=not (isinstance(subject.kind, str) and subject.kind in NUMBERED_SUBJECT_KINDS))
        if isinstance(subject.kind, str) and subject.kind in REPOSITORY_SUBJECT_KINDS:
            add("subject.repository", "subject", subject.repository,
                f"subject.repository is required for a {subject.kind} subject")
        if isinstance(subject.kind, str) and subject.kind in SHA_BOUND_SUBJECT_KINDS:
            add("subject.revision", "subject", subject.revision,
                f"subject.revision is required for a {subject.kind} subject: evidence about a moving "
                "pointer must name the exact git SHA it observed")
    for call in tool_calls:
        add("tool_call.kind", "tool_calls", call.kind, "tool_call.kind is required")
        add("tool_call.action_digest", "tool_calls", call.action_digest, "tool_call.action_digest is required")
        add("tool_call.status", "tool_calls", call.status, "tool_call.status is required")
    if provenance is not None:
        add("provenance.authority", "provenance", provenance.authority,
            "provenance.authority is required when the provenance block is present")
        add("provenance.observed_at", "provenance", provenance.observed_at,
            "provenance.observed_at is required when the provenance block is present")
    return rows


def _check_required_leaves(
    *,
    versions: VersionPins | None = None,
    subject: SubjectRef | None = None,
    tool_calls: Sequence[ToolCallRef] = (),
    provenance: Provenance | None = None,
    redacted_blocks: Container[str] = frozenset(),
    skip: Container[str] = frozenset(),
) -> None:
    """Raise `ExecutionEvidenceError` for the first non-string or blank
    required leaf, unless the row is `redactable` and a required
    `redacted_out` gap names its `gap_block`. seal() calls this on the
    caller's blocks before `_safe()` with no redacted blocks, so a leaf the
    caller never supplied is never excused by a sibling's redaction. The
    type check comes first, so an unhashable value (a list `kind` out of
    YAML) fails as this error, not as a `TypeError` from a set lookup."""
    for row in _required_leaves(versions=versions, subject=subject, tool_calls=tool_calls, provenance=provenance):
        if not isinstance(row.value, str):
            raise ExecutionEvidenceError(f"{row.leaf} must be a string, got {type(row.value).__name__}")
        if row.value or row.leaf in skip:
            continue
        if row.redactable and row.gap_block in redacted_blocks:
            continue
        raise ExecutionEvidenceError(row.message)


def _is_block_required(block: str, requirements: Requirements) -> bool:
    if block in ("execution", "outcome") or block in AMENDMENT_2_BLOCKS:
        return True
    return bool(getattr(requirements, block, False))


def _safe_node(value: Any, *, block: str, required: bool) -> tuple[Any, list[Gap]]:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        gaps: list[Gap] = []
        for key, sub in value.items():
            cleaned, sub_gaps = _safe_node(sub, block=block, required=required)
            gaps.extend(sub_gaps)
            if cleaned is _DROPPED:
                gaps.append(Gap(block=block, code="redacted_out", required=required))
                # Keep the key, with the same value its owning dataclass's
                # from_dict already defaults an absent field to, rather than
                # omitting it — see _REDACTED_LEAF.
                out[key] = _REDACTED_LEAF
                continue
            out[key] = cleaned
        return out, gaps
    if isinstance(value, list):
        out_list: list[Any] = []
        list_gaps: list[Gap] = []
        for item in value:
            cleaned, sub_gaps = _safe_node(item, block=block, required=required)
            list_gaps.extend(sub_gaps)
            if cleaned is _DROPPED:
                list_gaps.append(Gap(block=block, code="redacted_out", required=required))
                continue
            out_list.append(cleaned)
        return out_list, list_gaps
    if value is None or isinstance(value, bool):
        return value, []
    if isinstance(value, int | float):
        return value, []
    if isinstance(value, str):
        # An empty string is a legitimate "not applicable" leaf, not a
        # payload to screen: `safe_scalar` (mirroring the OTel attribute
        # guard it was extracted from) rejects zero-length strings outright,
        # which would turn every blank optional field into a spurious
        # redaction. Only a non-empty string goes through the bounded,
        # credential-shape check.
        if value == "" or (safe_scalar(value, max_chars=MAX_LEAF_CHARS) and not contains_credential(value)):
            return value, []
        return _DROPPED, []
    # Not a declared scalar or structure at all: reject rather than guess.
    return _DROPPED, []


def _safe(payload: Mapping[str, Any], *, requirements: Requirements) -> tuple[dict[str, Any], tuple[Gap, ...]]:
    """Walk every block of `payload` (`_content_payload`'s output, before
    hashing) and drop — never mask — any leaf that is not a bounded, safe
    scalar or that matches a credential shape
    (`orchestrator.redaction.contains_credential`). Every drop becomes an
    explicit `Gap(block=..., code="redacted_out", required=<per
    requirements>)`, following `context_snapshot.Redaction`'s "rule ids and
    volume only, never matched text" accounting — the gap records THAT a
    drop happened and where, never what was dropped. `api_version`/`kind`
    are fixed constants and are never walked."""
    clean: dict[str, Any] = {}
    gaps: list[Gap] = []
    for block, value in payload.items():
        if block in ("api_version", "kind"):
            clean[block] = value
            continue
        required = _is_block_required(block, requirements)
        cleaned_value, block_gaps = _safe_node(value, block=block, required=required)
        clean[block] = cleaned_value
        gaps.extend(block_gaps)
    return clean, tuple(gaps)


# ---------------------------------------------------------------------------
# ExecutionEvidence — the top-level envelope.
# ---------------------------------------------------------------------------


_EVIDENCE_KEYS = frozenset({
    "api_version", "kind", "evidence_id", "content_hash", "created_at",
    "execution", "outcome", "policy_decisions", "snapshot_refs",
    "execution_request", "usage", "approvals", "artifacts", "gaps",
    # ADR 018 Amendment 2.
    "versions", "subject", "tool_calls", "provenance",
})


@dataclass(frozen=True)
class ExecutionEvidence:
    """One immutable, content-addressed, redacted statement of which
    canonical governance records applied to one governed execution. Only
    ever produced by `seal()`; `from_dict` reconstructs an already-sealed
    document and re-validates its shape, but never recomputes the hash —
    use `recompute_content_hash` to verify one. `from_dict` also runs every
    block through the same `_safe()` redaction/bounds check `seal()` applies
    before hashing, but fails closed on a hit: a document that needs
    redaction was never legitimately produced by `seal()` (which redacts
    before hashing), so `from_dict` raises `ExecutionEvidenceError` rather
    than rewriting the document — that rewrite would otherwise leave
    `content_hash`/`evidence_id` certifying content the returned envelope no
    longer carries."""

    api_version: str
    kind: str
    evidence_id: str
    content_hash: str
    created_at: str
    execution: ExecutionJoin
    outcome: Outcome
    policy_decisions: tuple[PolicyDecisionRef, ...] = ()
    snapshot_refs: tuple[SnapshotRef, ...] = ()
    execution_request: ExecutionRequestRef | None = None
    usage: UsageRef | None = None
    approvals: tuple[ApprovalRef, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    gaps: tuple[Gap, ...] = ()
    # ADR 018 Amendment 2 — absent (None/empty) on every envelope sealed
    # before it, and then omitted from to_dict() and from the hash.
    versions: VersionPins | None = None
    subject: SubjectRef | None = None
    tool_calls: tuple[ToolCallRef, ...] = ()
    provenance: Provenance | None = None

    @property
    def completeness(self) -> str:
        """Derived from `gaps`, never a field: `INCOMPLETE` iff at least one
        gap is `required`. Not in `__init__`, not accepted by `from_dict` —
        a caller cannot construct a `COMPLETE` envelope while evidence is
        missing."""
        return INCOMPLETE if any(g.required for g in self.gaps) else COMPLETE

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "api_version": self.api_version,
            "kind": self.kind,
            "evidence_id": self.evidence_id,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "execution": self.execution.to_dict(),
            "outcome": self.outcome.to_dict(),
            "policy_decisions": [p.to_dict() for p in self.policy_decisions],
            "snapshot_refs": [s.to_dict() for s in self.snapshot_refs],
            "execution_request": self.execution_request.to_dict() if self.execution_request is not None else None,
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "approvals": [a.to_dict() for a in self.approvals],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "gaps": [g.to_dict() for g in self.gaps],
        }
        # ADR 018 Amendment 2: emitted only when present, so every envelope
        # sealed before the amendment round-trips byte-identically (its
        # golden fixture carries none of these keys).
        if self.versions is not None:
            result["versions"] = self.versions.to_dict()
        if self.subject is not None:
            result["subject"] = self.subject.to_dict()
        if self.tool_calls:
            result["tool_calls"] = [t.to_dict() for t in self.tool_calls]
        if self.provenance is not None:
            result["provenance"] = self.provenance.to_dict()
        return result

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

        evidence_id = _require_str(mapping.get("evidence_id"), where="evidence_id")
        content_hash = _require_sha256(mapping.get("content_hash"), where="content_hash")
        created_at = _require_str(mapping.get("created_at"), where="created_at")

        execution_raw = mapping.get("execution")
        outcome_raw = mapping.get("outcome")

        policy_decisions_raw = mapping.get("policy_decisions", [])
        if not isinstance(policy_decisions_raw, list):
            raise ExecutionEvidenceError("policy_decisions must be a list")

        snapshot_refs_raw = mapping.get("snapshot_refs", [])
        if not isinstance(snapshot_refs_raw, list):
            raise ExecutionEvidenceError("snapshot_refs must be a list")

        execution_request_raw = mapping.get("execution_request")

        usage_raw = mapping.get("usage")

        approvals_raw = mapping.get("approvals", [])
        if not isinstance(approvals_raw, list):
            raise ExecutionEvidenceError("approvals must be a list")

        artifacts_raw = mapping.get("artifacts", [])
        if not isinstance(artifacts_raw, list):
            raise ExecutionEvidenceError("artifacts must be a list")

        versions_raw = mapping.get("versions")
        subject_raw = mapping.get("subject")
        tool_calls_raw = mapping.get("tool_calls", [])
        if not isinstance(tool_calls_raw, list):
            raise ExecutionEvidenceError("tool_calls must be a list")
        provenance_raw = mapping.get("provenance")

        # Read-path parity with seal(): a document handed to from_dict is
        # documented as "already sealed", meaning seal() already redacted
        # every block before computing content_hash/evidence_id. Route every
        # block through the same _safe() redaction and per-leaf bounds check
        # seal() runs before hashing, but only to detect a hit, never to
        # rewrite the document: content_hash/evidence_id below are the raw,
        # caller-supplied values, so silently accepting a redacted rewrite
        # here would return an envelope whose identity fields certify
        # content it no longer carries. A hit means this document was never
        # legitimately produced by seal() — reject it instead.
        raw_payload: dict[str, Any] = {"execution": execution_raw, "outcome": outcome_raw}
        if policy_decisions_raw:
            raw_payload["policy_decisions"] = policy_decisions_raw
        if snapshot_refs_raw:
            raw_payload["snapshot_refs"] = snapshot_refs_raw
        if execution_request_raw is not None:
            raw_payload["execution_request"] = execution_request_raw
        if usage_raw is not None:
            raw_payload["usage"] = usage_raw
        if approvals_raw:
            raw_payload["approvals"] = approvals_raw
        if artifacts_raw:
            raw_payload["artifacts"] = artifacts_raw
        if versions_raw is not None:
            raw_payload["versions"] = versions_raw
        if subject_raw is not None:
            raw_payload["subject"] = subject_raw
        if tool_calls_raw:
            raw_payload["tool_calls"] = tool_calls_raw
        if provenance_raw is not None:
            raw_payload["provenance"] = provenance_raw
        safe_payload, redaction_gaps = _safe(raw_payload, requirements=DEFAULT_REQUIREMENTS)
        if redaction_gaps:
            offending = sorted({gap.block for gap in redaction_gaps})
            raise ExecutionEvidenceError(
                "from_dict rejects a document that requires redaction on read: "
                f"block(s) {offending!r} contain a credential-shaped or over-cap leaf. "
                "A document produced by seal() is redacted before hashing and would "
                "never trigger this; fix the document rather than relying on from_dict "
                "to silently rewrite it, which would leave content_hash/evidence_id "
                "certifying content the returned envelope no longer carries."
            )

        execution = ExecutionJoin.from_dict(safe_payload["execution"])
        outcome = Outcome.from_dict(safe_payload["outcome"])
        policy_decisions = tuple(PolicyDecisionRef.from_dict(p) for p in safe_payload.get("policy_decisions", []))
        snapshot_refs = tuple(SnapshotRef.from_dict(s) for s in safe_payload.get("snapshot_refs", []))
        execution_request = (
            ExecutionRequestRef.from_dict(safe_payload["execution_request"])
            if "execution_request" in safe_payload
            else None
        )
        usage = UsageRef.from_dict(safe_payload["usage"]) if "usage" in safe_payload else None
        approvals = tuple(ApprovalRef.from_dict(a) for a in safe_payload.get("approvals", []))
        artifacts = tuple(ArtifactRef.from_dict(a) for a in safe_payload.get("artifacts", []))
        versions = VersionPins.from_dict(safe_payload["versions"]) if "versions" in safe_payload else None
        subject = SubjectRef.from_dict(safe_payload["subject"]) if "subject" in safe_payload else None
        tool_calls = tuple(ToolCallRef.from_dict(t) for t in safe_payload.get("tool_calls", []))
        provenance = Provenance.from_dict(safe_payload["provenance"]) if "provenance" in safe_payload else None

        gaps_raw = mapping.get("gaps", [])
        if not isinstance(gaps_raw, list):
            raise ExecutionEvidenceError("gaps must be a list")
        # redaction_gaps is always empty here: a non-empty result raises above.
        gaps = tuple(Gap.from_dict(g) for g in gaps_raw) + redaction_gaps

        evidence = cls(
            api_version=api_version,
            kind=kind,
            evidence_id=evidence_id,
            content_hash=content_hash,
            created_at=created_at,
            execution=execution,
            outcome=outcome,
            policy_decisions=policy_decisions,
            snapshot_refs=snapshot_refs,
            execution_request=execution_request,
            usage=usage,
            approvals=approvals,
            artifacts=artifacts,
            gaps=gaps,
            versions=versions,
            subject=subject,
            tool_calls=tool_calls,
            provenance=provenance,
        )
        evidence.validate()
        return evidence

    def validate(self) -> None:
        """Enforce the vocabulary/shape rules `from_dict`'s type checks and
        `seal()`'s construction cannot. Checks are skipped when a value is
        blank: a blank leaf means "absent" (never supplied, or dropped by
        `_safe()`), and whether an absent required block was properly
        gapped is `seal()`'s job — the only place a `Requirements` profile
        is ever in hand. Raises `ExecutionEvidenceError`; never silently
        coerces or drops a field."""
        if self.api_version != API_VERSION:
            raise ExecutionEvidenceError(f"api_version must be {API_VERSION!r}, got {self.api_version!r}")
        if self.kind != KIND:
            raise ExecutionEvidenceError(f"kind must be {KIND!r}, got {self.kind!r}")
        if not _SHA256_PATTERN.fullmatch(self.content_hash):
            raise ExecutionEvidenceError(
                "content_hash must be 'sha256:' followed by 64 lowercase hex characters, "
                f"got {self.content_hash[:80]!r}"
            )
        if not _EVIDENCE_ID_PATTERN.fullmatch(self.evidence_id):
            raise ExecutionEvidenceError(
                f"evidence_id must be {EVIDENCE_ID_PREFIX!r} followed by 16 lowercase hex characters, "
                f"got {self.evidence_id[:80]!r}"
            )
        expected_evidence_id = EVIDENCE_ID_PREFIX + self.content_hash[7:23]
        if self.evidence_id != expected_evidence_id:
            raise ExecutionEvidenceError(
                f"evidence_id {self.evidence_id!r} does not match the id derived from content_hash "
                f"({expected_evidence_id!r})"
            )
        _check_created_at(self.created_at)

        _check_execution_join(self.execution)
        _check_outcome(self.outcome)
        for decision in self.policy_decisions:
            _check_policy_decision(decision)
        for ref in self.snapshot_refs:
            _check_snapshot_ref(ref)
        if self.execution_request is not None:
            _check_execution_request(self.execution_request)
        if self.usage is not None:
            _check_usage(self.usage)
        for approval in self.approvals:
            _check_approval(approval)
        for artifact in self.artifacts:
            _check_artifact(artifact)
        for gap in self.gaps:
            _check_gap(gap)
        # A blank required FREE-FORM leaf (REDACTABLE_REQUIRED_LEAVES) is
        # tolerated only when a redacted_out gap names its block: _safe()
        # dropped it, _is_block_required made that gap required, and the
        # envelope is INCOMPLETE rather than lost. The excusal is declarative
        # (ADR 018 Amendment 2): from_dict trusts the gap as written. seal()
        # additionally checks every required leaf is present BEFORE
        # redaction (_check_required_leaves), so a leaf the caller
        # never supplied is never excused by a sibling's redaction.
        redacted = {g.block for g in self.gaps if g.code == "redacted_out"}
        for gap in self.gaps:
            if gap.code == "redacted_out" and gap.block in AMENDMENT_2_BLOCKS and not gap.required:
                raise ExecutionEvidenceError(
                    f"a redacted_out gap on Amendment 2 block {gap.block!r} must be required: "
                    "seal() never writes it otherwise"
                )
        _check_required_leaves(
            versions=self.versions, subject=self.subject, tool_calls=self.tool_calls,
            provenance=self.provenance, redacted_blocks=redacted,
        )
        if self.versions is not None:
            _check_versions(self.versions)
        if self.subject is not None:
            _check_subject(self.subject)
            if self.provenance is None:
                raise ExecutionEvidenceError(
                    "subject-bound evidence must carry a provenance block (authority, observed_at): "
                    "without it resolve_current cannot rank it against other evidence for the same subject"
                )
        if len(self.tool_calls) > MAX_TOOL_CALLS:
            raise ExecutionEvidenceError(f"tool_calls holds {len(self.tool_calls)} entries, max {MAX_TOOL_CALLS}")
        for call in self.tool_calls:
            _check_tool_call(call)
        if self.provenance is not None:
            _check_provenance(self.provenance, own_evidence_id=self.evidence_id)

    def to_log_dict(self) -> dict[str, Any]:
        """Trace/telemetry-export shape (#195 owns traces): `evidence_id`,
        `content_hash`, derived `completeness`, the outcome code and integer
        `*_count` values per block, plus the closed-vocabulary
        `subject_kind` and `authority` codes — nothing else, the same count-not-list
        idiom as `ContextSnapshot.to_log_dict`'s `evidence_ref_count`."""
        return {
            "evidence_id": self.evidence_id,
            "content_hash": self.content_hash,
            "completeness": self.completeness,
            "outcome_code": self.outcome.code,
            "primary_execution_kind": self.execution.primary_execution_ref[0],
            "policy_decision_count": len(self.policy_decisions),
            "snapshot_ref_count": len(self.snapshot_refs),
            "approval_count": len(self.approvals),
            "artifact_count": len(self.artifacts),
            "gap_count": len(self.gaps),
            # ADR 018 Amendment 2: a count and two closed-vocabulary codes.
            "tool_call_count": len(self.tool_calls),
            "subject_kind": self.subject.kind if self.subject is not None else "",
            "authority": self.provenance.authority if self.provenance is not None else "",
        }


def _check_created_at(created_at: str) -> None:
    if len(created_at) > MAX_CREATED_AT_LENGTH or not _CREATED_AT_PATTERN.match(created_at):
        raise ExecutionEvidenceError(
            "created_at must be an ISO8601 UTC timestamp of the form "
            f"YYYY-MM-DDTHH:MM:SS[.ffffff]Z, got {created_at!r}"
        )


def _check_execution_join(join: ExecutionJoin) -> None:
    """Symmetric and cross-rejecting (ADR 018 Amendment 1): each identity
    must belong to its own namespace and must never carry the other's
    shape, so `we_` and `ex-` can never silently merge into one untagged
    field the way `usage_ledger.execution_id` already has."""
    if join.execution_id:
        if join.execution_id.startswith(RUNTIME_EXECUTION_ID_PREFIX):
            raise ExecutionEvidenceError(
                f"execution.execution_id {join.execution_id!r} carries a runtime ExecutionContext id "
                f"({RUNTIME_EXECUTION_ID_PREFIX!r}); put it in execution.runtime_execution_id instead"
            )
        if not join.execution_id.startswith(EXECUTION_ID_PREFIX):
            raise ExecutionEvidenceError(
                f"execution.execution_id must start with {EXECUTION_ID_PREFIX!r}, got {join.execution_id!r}"
            )
    if join.runtime_execution_id:
        if join.runtime_execution_id.startswith(EXECUTION_ID_PREFIX):
            raise ExecutionEvidenceError(
                f"execution.runtime_execution_id {join.runtime_execution_id!r} carries a work execution id "
                f"({EXECUTION_ID_PREFIX!r}); put it in execution.execution_id instead"
            )
        if not _RUNTIME_EXECUTION_ID_PATTERN.fullmatch(join.runtime_execution_id):
            raise ExecutionEvidenceError(
                f"execution.runtime_execution_id must be {RUNTIME_EXECUTION_ID_PREFIX!r} followed by 16 "
                f"lowercase hex characters, got {join.runtime_execution_id!r}"
            )


def _check_outcome(outcome: Outcome) -> None:
    if outcome.code and outcome.code not in OUTCOME_CODES:
        raise ExecutionEvidenceError(f"outcome.code {outcome.code!r} is not one of {sorted(OUTCOME_CODES)!r}")
    if outcome.reason_code and (
        len(outcome.reason_code) > MAX_SLUG_LENGTH or not _SLUG_PATTERN.match(outcome.reason_code)
    ):
        raise ExecutionEvidenceError(
            f"outcome.reason_code must be a 1..{MAX_SLUG_LENGTH} character code of [a-z0-9._-]"
        )


def _check_policy_decision(decision: PolicyDecisionRef) -> None:
    if decision.action_digest and not decision.action_digest.startswith("sha256:"):
        raise ExecutionEvidenceError(
            f"policy_decision.action_digest must carry the 'sha256:' prefix, got {decision.action_digest!r}"
        )
    if decision.verdict and decision.verdict not in VERDICTS:
        raise ExecutionEvidenceError(f"policy_decision.verdict {decision.verdict!r} is not one of {sorted(VERDICTS)!r}")


def _check_snapshot_ref(ref: SnapshotRef) -> None:
    if ref.snapshot_id and not ref.snapshot_id.startswith(SNAPSHOT_ID_PREFIXES):
        raise ExecutionEvidenceError(
            f"snapshot_ref.snapshot_id must start with one of {SNAPSHOT_ID_PREFIXES!r}, got {ref.snapshot_id!r}"
        )
    if ref.content_hash and not ref.content_hash.startswith("sha256:"):
        raise ExecutionEvidenceError(
            f"snapshot_ref.content_hash must carry the 'sha256:' prefix, got {ref.content_hash!r}"
        )


def _check_execution_request(ref: ExecutionRequestRef) -> None:
    if ref.request_id and not ref.request_id.startswith(REQUEST_ID_PREFIX):
        raise ExecutionEvidenceError(
            f"execution_request.request_id must start with {REQUEST_ID_PREFIX!r}, got {ref.request_id!r}"
        )
    if ref.kind and ref.kind not in EXECUTION_REQUEST_KINDS:
        raise ExecutionEvidenceError(
            f"execution_request.kind {ref.kind!r} is not one of {sorted(EXECUTION_REQUEST_KINDS)!r}"
        )
    if ref.state and ref.state not in EXECUTION_REQUEST_STATES:
        raise ExecutionEvidenceError(
            f"execution_request.state {ref.state!r} is not one of {sorted(EXECUTION_REQUEST_STATES)!r}"
        )


def _check_usage(ref: UsageRef) -> None:
    if ref.devloop_stage and ref.devloop_stage not in USAGE_DEVLOOP_STAGES:
        raise ExecutionEvidenceError(
            f"usage.devloop_stage {ref.devloop_stage!r} is not one of {sorted(USAGE_DEVLOOP_STAGES)!r}"
        )


def _check_approval(ref: ApprovalRef) -> None:
    if ref.approval_id and not ref.approval_id.startswith(APPROVAL_ID_PREFIX):
        raise ExecutionEvidenceError(
            f"approval.approval_id must start with {APPROVAL_ID_PREFIX!r}, got {ref.approval_id!r}"
        )
    if ref.intent_hash and not ref.intent_hash.startswith("sha256:"):
        raise ExecutionEvidenceError(f"approval.intent_hash must carry the 'sha256:' prefix, got {ref.intent_hash!r}")
    if ref.state and ref.state not in APPROVAL_STATES:
        raise ExecutionEvidenceError(f"approval.state {ref.state!r} is not one of {sorted(APPROVAL_STATES)!r}")


def _check_artifact(ref: ArtifactRef) -> None:
    if ref.name:
        _check_artifact_name(ref.name)
    if ref.content_hash and not ref.content_hash.startswith("sha256:"):
        raise ExecutionEvidenceError(f"artifact.content_hash must carry the 'sha256:' prefix, got {ref.content_hash!r}")


def _check_artifact_name(name: str) -> None:
    if (
        len(name) > MAX_ARTIFACT_NAME_LENGTH
        or not _ARTIFACT_NAME_PATTERN.match(name)
        or "/" in name
        or "\\" in name
        or ".." in name
        or name.startswith("~")
    ):
        raise ExecutionEvidenceError(
            f"artifact.name {name!r} must be a bounded, separator-free name with no path fragment"
        )


def _check_gap(gap: Gap) -> None:
    if gap.block not in BLOCK_NAMES:
        raise ExecutionEvidenceError(f"gap.block {gap.block!r} is not one of {sorted(BLOCK_NAMES)!r}")
    if gap.code not in GAP_CODES:
        raise ExecutionEvidenceError(f"gap.code {gap.code!r} is not one of {sorted(GAP_CODES)!r}")
    if gap.code in UNKNOWN_GAP_CODES and not gap.required:
        raise ExecutionEvidenceError(
            f"gap {gap.block!r}/{gap.code!r} states an unknown and must be required: "
            "could-not-observe never leaves an envelope COMPLETE"
        )


def _check_bounded(value: str, pattern: re.Pattern[str], max_length: int, *, where: str) -> None:
    if len(value) > max_length or not pattern.fullmatch(value):
        raise ExecutionEvidenceError(f"{where} must match {pattern.pattern!r} within {max_length} characters")


def _check_string_fields(
    block: VersionPins | SubjectRef | ToolCallRef | Provenance, names: Sequence[str], *, where: str
) -> None:
    for name in names:
        if not isinstance(getattr(block, name), str):
            raise ExecutionEvidenceError(f"{where}.{name} must be a string")


def _check_versions(pins: VersionPins) -> None:
    """Shape only: requiredness is `_check_required_leaves`'s job."""
    _check_string_fields(pins, (
        "agent", "environment", "definition_version", "definition_content_hash",
        "profile_name", "profile_version", "profile_content_hash",
    ), where="versions")
    if pins.release_revision is not None and (
        not isinstance(pins.release_revision, int) or isinstance(pins.release_revision, bool)
    ):
        raise ExecutionEvidenceError("versions.release_revision must be an int or null")
    for name in ("agent", "environment", "profile_name"):
        value = getattr(pins, name)
        if value:
            _check_bounded(value, _SLUG_PATTERN, MAX_SLUG_LENGTH, where=f"versions.{name}")
    for name in ("definition_version", "profile_version"):
        value = getattr(pins, name)
        if value:
            _check_bounded(value, _VERSION_PATTERN, MAX_VERSION_LENGTH, where=f"versions.{name}")
    for name in ("definition_content_hash", "profile_content_hash"):
        value = getattr(pins, name)
        if value and not _SHA256_PATTERN.fullmatch(value):
            raise ExecutionEvidenceError(
                f"versions.{name} must be 'sha256:' followed by 64 lowercase hex characters, got {value[:80]!r}"
            )
    if pins.release_revision is not None and not 0 <= pins.release_revision <= MAX_RELEASE_REVISION:
        raise ExecutionEvidenceError(
            f"versions.release_revision must be within 0..{MAX_RELEASE_REVISION} (a signed 64-bit integer), "
            f"got {pins.release_revision}"
        )


def _check_repository(repository: str) -> None:
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise ExecutionEvidenceError(f"subject.repository must be 'owner/name', got {repository[:80]!r}")
    name = repository.split("/", 1)[1]
    if ".." in name or not name.strip("."):
        raise ExecutionEvidenceError(f"subject.repository {repository[:80]!r} must not carry a path fragment")


def _check_subject(subject: SubjectRef) -> None:
    """Shape only: requiredness is `_check_required_leaves`'s job."""
    _check_string_fields(subject, ("kind", "repository", "ref", "revision"), where="subject")
    if subject.kind and subject.kind not in SUBJECT_KINDS:
        raise ExecutionEvidenceError(f"subject.kind {subject.kind!r} is not one of {sorted(SUBJECT_KINDS)!r}")
    if subject.ref:
        if subject.kind in NUMBERED_SUBJECT_KINDS:
            if not _NUMBER_REF_PATTERN.fullmatch(subject.ref):
                raise ExecutionEvidenceError(
                    f"subject.ref for a {subject.kind} must be its number, got {subject.ref[:80]!r}"
                )
        else:
            _check_bounded(subject.ref, _SUBJECT_REF_PATTERN, MAX_SUBJECT_REF_LENGTH, where="subject.ref")
            if ".." in subject.ref or "//" in subject.ref or subject.ref.endswith("/"):
                raise ExecutionEvidenceError(f"subject.ref {subject.ref[:80]!r} must not carry a path fragment")
    if subject.repository:
        _check_repository(subject.repository)
    if subject.revision:
        if subject.kind in SHA_BOUND_SUBJECT_KINDS:
            if not _GIT_SHA_PATTERN.fullmatch(subject.revision):
                raise ExecutionEvidenceError(
                    f"subject.revision for a {subject.kind} must be a full lowercase git SHA (40 or 64 hex), "
                    f"got {subject.revision[:80]!r}"
                )
        else:
            _check_bounded(subject.revision, _VERSION_PATTERN, MAX_VERSION_LENGTH, where="subject.revision")


def _check_tool_call(call: ToolCallRef) -> None:
    """Shape only: requiredness is `_check_required_leaves`'s job."""
    _check_string_fields(call, ("kind", "name", "action_digest", "status"), where="tool_call")
    if call.kind and call.kind not in TOOL_CALL_KINDS:
        raise ExecutionEvidenceError(f"tool_call.kind {call.kind!r} is not one of {sorted(TOOL_CALL_KINDS)!r}")
    if call.name:
        _check_bounded(call.name, _TOOL_NAME_PATTERN, MAX_TOOL_NAME_LENGTH, where="tool_call.name")
    if call.action_digest and not _SHA256_PATTERN.fullmatch(call.action_digest):
        raise ExecutionEvidenceError(
            "tool_call.action_digest must be 'sha256:' followed by 64 lowercase hex characters, "
            f"got {call.action_digest[:80]!r}"
        )
    if call.status and call.status not in TOOL_CALL_STATUSES:
        raise ExecutionEvidenceError(
            f"tool_call.status {call.status!r} is not one of {sorted(TOOL_CALL_STATUSES)!r}"
        )


def _check_provenance(provenance: Provenance, *, own_evidence_id: str) -> None:
    """Shape only: requiredness is `_check_required_leaves`'s job."""
    _check_string_fields(provenance, ("authority", "observed_at", "supersedes"), where="provenance")
    if provenance.authority and provenance.authority not in AUTHORITY_RANK:
        raise ExecutionEvidenceError(
            f"provenance.authority {provenance.authority!r} is not one of {list(AUTHORITIES)!r}"
        )
    if provenance.observed_at and (
        len(provenance.observed_at) > MAX_CREATED_AT_LENGTH or not _CREATED_AT_PATTERN.match(provenance.observed_at)
    ):
        raise ExecutionEvidenceError(
            "provenance.observed_at must be an ISO8601 UTC timestamp of the form "
            f"YYYY-MM-DDTHH:MM:SS[.ffffff]Z, got {provenance.observed_at[:80]!r}"
        )
    if provenance.supersedes:
        if not _EVIDENCE_ID_PATTERN.fullmatch(provenance.supersedes):
            raise ExecutionEvidenceError(
                f"provenance.supersedes must be an evidence id ({EVIDENCE_ID_PREFIX!r} followed by 16 "
                f"lowercase hex characters), got {provenance.supersedes[:80]!r}"
            )
        if provenance.supersedes == own_evidence_id:
            raise ExecutionEvidenceError("provenance.supersedes must not name the envelope itself")


# ---------------------------------------------------------------------------
# Sealing.
# ---------------------------------------------------------------------------


def _content_payload(
    *,
    execution: ExecutionJoin,
    outcome: Outcome,
    policy_decisions: Sequence[PolicyDecisionRef] = (),
    snapshot_refs: Sequence[SnapshotRef] = (),
    execution_request: ExecutionRequestRef | None = None,
    usage: UsageRef | None = None,
    approvals: Sequence[ApprovalRef] = (),
    artifacts: Sequence[ArtifactRef] = (),
    versions: VersionPins | None = None,
    subject: SubjectRef | None = None,
    tool_calls: Sequence[ToolCallRef] = (),
    provenance: Provenance | None = None,
) -> dict[str, Any]:
    """Every field that participates in `content_hash` — everything except
    `content_hash`, `evidence_id` and `created_at`. Every optional block
    (`policy_decisions`, `snapshot_refs`, `execution_request`, `usage`,
    `approvals`, `artifacts`) enters the payload ONLY when non-empty/present,
    mirroring `context_snapshot.py:1174-1207`'s rule: a future optional
    block added to this schema must not re-identify an already-sealed
    envelope, and an envelope sealed without one today hashes exactly as if
    the key were never declared."""
    payload: dict[str, Any] = {
        "api_version": API_VERSION,
        "kind": KIND,
        "execution": execution.to_dict(),
        "outcome": outcome.to_dict(),
    }
    if policy_decisions:
        payload["policy_decisions"] = [p.to_dict() for p in policy_decisions]
    if snapshot_refs:
        payload["snapshot_refs"] = [s.to_dict() for s in snapshot_refs]
    if execution_request is not None:
        payload["execution_request"] = execution_request.to_dict()
    if usage is not None:
        payload["usage"] = usage.to_dict()
    if approvals:
        payload["approvals"] = [a.to_dict() for a in approvals]
    if artifacts:
        payload["artifacts"] = [a.to_dict() for a in artifacts]
    # ADR 018 Amendment 2 blocks: the same absent-when-empty rule, which is
    # what keeps every pre-amendment envelope's content_hash unchanged.
    if versions is not None:
        payload["versions"] = versions.to_dict()
    if subject is not None:
        payload["subject"] = subject.to_dict()
    if tool_calls:
        payload["tool_calls"] = [t.to_dict() for t in tool_calls]
    if provenance is not None:
        payload["provenance"] = provenance.to_dict()
    return payload


def _check_required_blocks(
    *,
    requirements: Requirements,
    execution: ExecutionJoin,
    outcome: Outcome,
    policy_decisions: tuple[PolicyDecisionRef, ...],
    snapshot_refs: tuple[SnapshotRef, ...],
    execution_request: ExecutionRequestRef | None,
    usage: UsageRef | None,
    approvals: tuple[ApprovalRef, ...],
    artifacts: tuple[ArtifactRef, ...],
    gaps: tuple[Gap, ...],
    versions: VersionPins | None = None,
    subject: SubjectRef | None = None,
    tool_calls: tuple[ToolCallRef, ...] = (),
    provenance: Provenance | None = None,
) -> None:
    gaps_by_block: dict[str, list[Gap]] = {}
    for g in gaps:
        gaps_by_block.setdefault(g.block, []).append(g)

    def _check(block: str, required: bool, absent: bool) -> None:
        if not (required and absent):
            return
        block_gaps = gaps_by_block.get(block, [])
        if not block_gaps:
            raise ExecutionEvidenceError(f"block {block!r} is required but absent and carries no Gap")
        if not any(g.required for g in block_gaps):
            raise ExecutionEvidenceError(
                f"block {block!r} is required but absent and its Gap is not marked required"
            )

    _check("execution", True, not (execution.execution_id or execution.runtime_execution_id))
    _check("outcome", True, not outcome.code)
    _check("policy_decisions", requirements.policy_decisions, len(policy_decisions) == 0)
    _check("snapshot_refs", requirements.snapshot_refs, len(snapshot_refs) == 0)
    _check("execution_request", requirements.execution_request, execution_request is None)
    _check("usage", requirements.usage, usage is None)
    _check("approvals", requirements.approvals, len(approvals) == 0)
    _check("artifacts", requirements.artifacts, len(artifacts) == 0)
    _check("versions", requirements.versions, versions is None)
    _check("subject", requirements.subject, subject is None)
    _check("tool_calls", requirements.tool_calls, len(tool_calls) == 0)
    _check("provenance", requirements.provenance, provenance is None)


def seal(
    *,
    execution: ExecutionJoin,
    outcome: Outcome,
    created_at: str,
    policy_decisions: Sequence[PolicyDecisionRef] = (),
    snapshot_refs: Sequence[SnapshotRef] = (),
    execution_request: ExecutionRequestRef | None = None,
    usage: UsageRef | None = None,
    approvals: Sequence[ApprovalRef] = (),
    artifacts: Sequence[ArtifactRef] = (),
    gaps: Sequence[Gap] = (),
    requirements: Requirements = DEFAULT_REQUIREMENTS,
    versions: VersionPins | None = None,
    subject: SubjectRef | None = None,
    tool_calls: Sequence[ToolCallRef] = (),
    provenance: Provenance | None = None,
) -> ExecutionEvidence:
    """The only constructor that produces a sealed `ExecutionEvidence`.

    Order: assemble `_content_payload()` -> `_safe()` redacts every block
    (never masks; each drop becomes a `redacted_out` Gap) -> reconstruct
    each block from the redacted payload, so the returned envelope never
    carries a value `_safe()` dropped -> `_check_required_blocks()` raises
    `ExecutionEvidenceError` if `requirements` marks a block required and it
    is both absent and ungapped -> `content_hash = hash_bytes(canonical_json(
    <redacted payload, plus gaps when non-empty>))` -> `evidence_id = "ev-" +
    content_hash[7:23]` -> `validate()` before returning. `created_at` is
    caller-supplied and excluded from the hash, so sealing identical inputs
    twice at different times yields the same identity."""
    # Pre-redaction presence check over the caller's own blocks, from the
    # same table validate() uses after redaction.
    _check_required_leaves(versions=versions, subject=subject, tool_calls=tool_calls, provenance=provenance)
    raw_payload = _content_payload(
        execution=execution,
        outcome=outcome,
        policy_decisions=policy_decisions,
        snapshot_refs=snapshot_refs,
        execution_request=execution_request,
        usage=usage,
        approvals=approvals,
        artifacts=artifacts,
        versions=versions,
        subject=subject,
        tool_calls=tool_calls,
        provenance=provenance,
    )
    safe_payload, redaction_gaps = _safe(raw_payload, requirements=requirements)
    all_gaps = tuple(gaps) + redaction_gaps

    # Hash-neutral prune (ADR 018 Amendment 1): _safe() rewrites a dropped
    # leaf to _REDACTED_LEAF ("") rather than omitting it, and ExecutionJoin
    # itself is constructed from this same safe_payload below, so a blank
    # runtime_execution_id — never supplied, or dropped by redaction — must
    # be pruned here before clean_execution/hashing, mirroring
    # _content_payload's block-level absent-when-empty rule. Without this,
    # a we_-only join would hash a
    # {"execution_id": ..., "runtime_execution_id": "", ...} block instead
    # of the byte-identical block every already-sealed we_-only envelope
    # (including the investigator fixture) was hashed with.
    if not safe_payload["execution"].get("runtime_execution_id"):
        safe_payload["execution"].pop("runtime_execution_id", None)

    clean_execution = ExecutionJoin.from_dict(safe_payload["execution"])
    clean_outcome = Outcome.from_dict(safe_payload["outcome"])
    clean_policy_decisions = tuple(PolicyDecisionRef.from_dict(p) for p in safe_payload.get("policy_decisions", []))
    clean_snapshot_refs = tuple(SnapshotRef.from_dict(s) for s in safe_payload.get("snapshot_refs", []))
    clean_execution_request = (
        ExecutionRequestRef.from_dict(safe_payload["execution_request"])
        if "execution_request" in safe_payload
        else None
    )
    clean_usage = UsageRef.from_dict(safe_payload["usage"]) if "usage" in safe_payload else None
    clean_approvals = tuple(ApprovalRef.from_dict(a) for a in safe_payload.get("approvals", []))
    clean_artifacts = tuple(ArtifactRef.from_dict(a) for a in safe_payload.get("artifacts", []))
    clean_versions = VersionPins.from_dict(safe_payload["versions"]) if "versions" in safe_payload else None
    clean_subject = SubjectRef.from_dict(safe_payload["subject"]) if "subject" in safe_payload else None
    clean_tool_calls = tuple(ToolCallRef.from_dict(t) for t in safe_payload.get("tool_calls", []))
    clean_provenance = Provenance.from_dict(safe_payload["provenance"]) if "provenance" in safe_payload else None

    _check_required_blocks(
        requirements=requirements,
        execution=clean_execution,
        outcome=clean_outcome,
        policy_decisions=clean_policy_decisions,
        snapshot_refs=clean_snapshot_refs,
        execution_request=clean_execution_request,
        usage=clean_usage,
        approvals=clean_approvals,
        artifacts=clean_artifacts,
        gaps=all_gaps,
        versions=clean_versions,
        subject=clean_subject,
        tool_calls=clean_tool_calls,
        provenance=clean_provenance,
    )

    hash_payload = dict(safe_payload)
    if all_gaps:
        hash_payload["gaps"] = [g.to_dict() for g in all_gaps]
    content_hash = hash_bytes(canonical_json(hash_payload))
    evidence_id = EVIDENCE_ID_PREFIX + content_hash[7:23]

    evidence = ExecutionEvidence(
        api_version=API_VERSION,
        kind=KIND,
        evidence_id=evidence_id,
        content_hash=content_hash,
        created_at=created_at,
        execution=clean_execution,
        outcome=clean_outcome,
        policy_decisions=clean_policy_decisions,
        snapshot_refs=clean_snapshot_refs,
        execution_request=clean_execution_request,
        usage=clean_usage,
        approvals=clean_approvals,
        artifacts=clean_artifacts,
        gaps=all_gaps,
        versions=clean_versions,
        subject=clean_subject,
        tool_calls=clean_tool_calls,
        provenance=clean_provenance,
    )
    evidence.validate()
    return evidence


def recompute_content_hash(evidence: ExecutionEvidence) -> str:
    """Recompute the `content_hash` a fresh `seal()` of `evidence`'s fields
    would produce, without mutating `evidence`. Used to verify golden
    fixtures and reseal-stability. Pure: `evidence` is already sealed and
    redacted, so this never re-runs `_safe()`."""
    payload = _content_payload(
        execution=evidence.execution,
        outcome=evidence.outcome,
        policy_decisions=evidence.policy_decisions,
        snapshot_refs=evidence.snapshot_refs,
        execution_request=evidence.execution_request,
        usage=evidence.usage,
        approvals=evidence.approvals,
        artifacts=evidence.artifacts,
        versions=evidence.versions,
        subject=evidence.subject,
        tool_calls=evidence.tool_calls,
        provenance=evidence.provenance,
    )
    if evidence.gaps:
        payload["gaps"] = [g.to_dict() for g in evidence.gaps]
    return hash_bytes(canonical_json(payload))


def evidence_ref(evidence: ExecutionEvidence, kind: str) -> dict[str, str]:
    """`{"evidence_id": ..., "kind": ...}` — exactly the two keys
    `context_snapshot.EvidenceRef.from_dict` accepts, so a caller sealing
    this envelope can attach it to a `ContextSnapshot.evidence_refs` entry
    without either module importing the other. The same `evidence_id` is
    also valid as a `human_input` `"evidence:"` context ref
    (`orchestrator/human_input.py:46`), which already reserves that prefix."""
    return {"evidence_id": evidence.evidence_id, "kind": kind}


# ---------------------------------------------------------------------------
# Current vs historical (ADR 018 Amendment 2) — the one resolution rule,
# pure and in-memory. Tier B must implement the same rule server-side; this
# function is its reference and its conformance oracle.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentEvidence:
    """`resolve_current`'s answer: a `state` from `CURRENT_STATES` and, only
    for `current`, the one envelope that is current. `unknown_revision` and
    `ambiguous` are unknowns and must never be read as `no_evidence`."""

    state: str
    evidence: ExecutionEvidence | None = None


def _observed_at_key(observed_at: str) -> str:
    """A lexically comparable form of a `_CREATED_AT_PATTERN` timestamp:
    the fraction is padded to six digits, so `...:00Z` sorts before
    `...:00.5Z` (a raw string comparison gets that backwards: '.' < 'Z')."""
    seconds, _, rest = observed_at.partition(".")
    if rest:
        return seconds + "." + rest.rstrip("Z").ljust(6, "0")
    return observed_at.rstrip("Z") + ".000000"


def resolve_current(
    candidates: Sequence[ExecutionEvidence],
    *,
    kind: str,
    repository: str,
    ref: str,
    revision: str,
) -> CurrentEvidence:
    """Which envelope is current for subject `(kind, repository, ref)` at
    the `revision` the caller has just observed it at.

    1. `revision` blank for a `SHA_BOUND_SUBJECT_KINDS` kind ->
       `unknown_revision`: without the live SHA no envelope can be called
       current, and "none" would be a lie. For `issue`/`work_item` the
       revision is an optional, producer-chosen version token: a blank one
       is itself the revision ("unversioned"), pooled with other blank ones.
    2. Pool = candidates whose `subject` has exactly this key AND this
       `revision`, excluding any envelope with a `redacted_out` gap on
       `subject` (it is not bound to a known subject). Evidence for any other revision is historical by
       definition — PR@SHA1 evidence is never current for PR@SHA2.
    3. Drop every pool member another pool member names in
       `provenance.supersedes` with equal or stronger authority.
       Supersession only acts inside the pool, and never downward: an
       `asserted` envelope naming an `observed` one retires nothing.
    4. Empty pool -> `stale_revision` when the subject key has
       (unredacted) evidence at other revisions, else `no_evidence` — which
       means no USABLE evidence, not that no run happened. A non-empty pool
       whose every member is superseded (only a forged `supersedes` cycle)
       -> `ambiguous`. Otherwise rank by authority
       (`AUTHORITY_RANK`: observed > derived > asserted), then by
       `observed_at` (later wins). A newer assertion never displaces an
       older observation.
    5. More than one distinct envelope sharing the top rank -> `ambiguous`
       (fail closed, never an arbitrary pick); exactly one -> `current`.

    `kind`/`repository`/`ref`/`revision` are validated with the same rules
    as a sealed `subject` (a blank `revision` excepted); a malformed one
    raises `ExecutionEvidenceError`.

    `candidates` must be the complete set for this subject key: a caller
    whose listing failed or was partial must not call this at all — that is
    an unknown, and passing a short list would turn it into `no_evidence`."""
    # A malformed argument is a caller bug, never `no_evidence`: an
    # abbreviated or uppercase SHA, or a kind outside SUBJECT_KINDS, would
    # otherwise match nothing and read as "there is no evidence".
    query = SubjectRef(kind=kind, repository=repository, ref=ref, revision=revision)
    _check_subject(query)
    _check_required_leaves(subject=query, skip={"subject.revision"})
    if not revision and kind in SHA_BOUND_SUBJECT_KINDS:
        return CurrentEvidence(state="unknown_revision")
    key = (kind, repository, ref)
    # An envelope whose subject block lost a leaf to redaction is, by
    # construction, not bound to a known subject: it is never a candidate,
    # whatever its remaining leaves happen to match.
    same_subject = [
        e for e in candidates
        if e.subject is not None and e.subject.key == key
        and not any(g.block == "subject" and g.code == "redacted_out" for g in e.gaps)
    ]
    pool = [e for e in same_subject if e.subject is not None and e.subject.revision == revision]
    by_id = {e.evidence_id: e for e in pool}

    def authority_rank(e: ExecutionEvidence) -> int:
        return AUTHORITY_RANK.get(e.provenance.authority, 0) if e.provenance is not None else 0

    superseded: set[str] = set()
    for e in pool:
        target_id = e.provenance.supersedes if e.provenance is not None else ""
        target = by_id.get(target_id)
        # A supersedes link counts only inside the pool (same subject key
        # and revision) and only from equal or stronger authority: a model
        # assertion can never retire an observation by naming it.
        if target is not None and authority_rank(e) >= authority_rank(target):
            superseded.add(target_id)
    live = [e for e in pool if e.evidence_id not in superseded]
    if not live:
        if pool:
            # Every pool member is superseded by another: only a forged
            # supersedes cycle can do that (supersedes is hashed). An
            # unknown, never resolved arbitrarily.
            return CurrentEvidence(state="ambiguous")
        # Evidence for this subject at another revision is stale, not absent:
        # "re-run for this SHA" and "the pipeline never ran" are different
        # decisions for the reader. no_evidence means no USABLE evidence: a
        # redacted-subject envelope is excluded above and answers it too.
        # With the pool empty, every same-subject candidate is at another
        # revision.
        return CurrentEvidence(state="stale_revision" if same_subject else "no_evidence")

    def rank(e: ExecutionEvidence) -> tuple[int, str]:
        # validate() guarantees provenance on every subject-bound envelope;
        # an unvalidated one ranks below every authority rather than raising.
        if e.provenance is None:
            return (0, "")
        return (authority_rank(e), _observed_at_key(e.provenance.observed_at))

    top = max(rank(e) for e in live)
    winners = {e.evidence_id: e for e in live if rank(e) == top}
    if len(winners) != 1:
        return CurrentEvidence(state="ambiguous")
    return CurrentEvidence(state="current", evidence=next(iter(winners.values())))


__all__ = [
    "API_VERSION",
    "APPROVAL_ID_PREFIX",
    "APPROVAL_STATES",
    "AUTHORITIES",
    "AUTHORITY_RANK",
    "BLOCK_NAMES",
    "COMPLETE",
    "CURRENT_STATES",
    "DEFAULT_REQUIREMENTS",
    "EVIDENCE_ID_PREFIX",
    "EXECUTION_ID_PREFIX",
    "EXECUTION_REF_KINDS",
    "EXECUTION_REQUEST_KINDS",
    "EXECUTION_REQUEST_STATES",
    "GAP_CODES",
    "INCOMPLETE",
    "KIND",
    "OUTCOME_CODES",
    "REQUEST_ID_PREFIX",
    "RUNTIME_EXECUTION_ID_PREFIX",
    "SHA_BOUND_SUBJECT_KINDS",
    "SNAPSHOT_ID_PREFIX",
    "SNAPSHOT_ID_PREFIXES",
    "SNAPSHOT_LOCAL_ID_PREFIX",
    "SUBJECT_KINDS",
    "SUPPORTED_API_VERSIONS",
    "TOOL_CALL_KINDS",
    "TOOL_CALL_STATUSES",
    "UNKNOWN_GAP_CODES",
    "USAGE_DEVLOOP_STAGES",
    "ApprovalRef",
    "ArtifactRef",
    "CurrentEvidence",
    "ExecutionEvidence",
    "ExecutionEvidenceError",
    "ExecutionJoin",
    "ExecutionRequestRef",
    "Gap",
    "Outcome",
    "PolicyDecisionRef",
    "Provenance",
    "Requirements",
    "SnapshotRef",
    "SubjectRef",
    "ToolCallRef",
    "UsageRef",
    "VersionPins",
    "evidence_ref",
    "recompute_content_hash",
    "resolve_current",
    "seal",
]
