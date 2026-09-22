"""`ExecutionContext` — the versioned, hashed contract for "who and what is
executing, on whose behalf, in which scope" (mctlhq/mctl-agents#196, ADR 011:
docs/adr/011-execution-identity-contract.md).

ADR 007 (`orchestrator/resolver.py`) answers "what contract ran". ADR 009
(`orchestrator/context_snapshot.py`) answers "what did the agent see". Neither
says who is running it, on whose behalf, or in which environment — today that
answer is six disjoint fragments scattered across `IssueRef`, the CWFT
`params` dict, `ExecutionRecord`, `ExecutionPlan`, argv/env, and a single
static shared `MCTL_TOKEN`. This module is the missing answer: a frozen-
dataclass schema, a `sha256:`-prefixed content-hash rule identical to ADR
009's, and a validator. It contains **no retrieval, no minting side effects,
no I/O beyond reading a local file path already named by the caller** — the
control-plane mint activity that POSTs a sealed context to mctl-api lives in
`orchestrator/temporal/activities/identity.py`, not here.

Stdlib only, deliberately, mirroring `orchestrator/context_snapshot.py`: so
both the long-lived Temporal worker and the short-lived agent sandbox can
import this module without pulling in `claude_agent_sdk`, `temporalio`,
`httpx`, `yaml`, `anyio` or `mcp` (`tests/test_execution_identity.py` enforces
this the same way `tests/test_worker_isolation.py` enforces it for the
worker).

No field here is ever consumed by an authorization decision. Nothing in this
module records allow/deny/permit/grant/authorized/role. A workload's own copy
of a context is evidence for logging and prompting only; the authoritative
copy lives in mctl-api, keyed by `context_id`, written only by the control
plane. Authorization belongs to policy checkpoints (#197); this module answers
identity, never permission. See ADR 011 sec. 5 for the full boundary table.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

API_VERSION = "identity.mctl.ai/v1alpha1"
KIND = "ExecutionContext"

# The complete allow-list, mirroring orchestrator/context_snapshot.py's and
# orchestrator/manifest.py's SUPPORTED_API_VERSIONS: a document declaring
# anything else fails loudly in from_dict, never falls back to a default
# shape.
SUPPORTED_API_VERSIONS = {API_VERSION: KIND}

# Closed vocabularies (ADR 011 sec. 6).
ACTOR_TYPES = frozenset({"github_user", "operator", "cron", "temporal-schedule", "system"})
ACTOR_VERIFICATIONS = frozenset({"control-plane-verified", "signal-asserted", "unverified"})
TRIGGER_TYPES = frozenset({
    "github_issue", "github_issue_comment", "pull_request", "incident", "schedule", "manual",
})
WORKFLOW_TYPES = frozenset({
    "investigate", "approve", "implement", "review-fix", "incident", "reconcile",
})
# dev_loop.py:80 / resolver.py:110 — the same two-value vocabulary those
# modules already use as plain strings, given a closed home here.
ENVIRONMENTS = frozenset({"production", "shadow"})
# Reuses ADR 010's Executor vocabulary verbatim (orchestrator/lifecycle/
# contract.py's Executor.type docstring: "shepherd | pr-steward |
# devloop-workflow | reconciler | implementer") so the two contracts never
# name the same concept two different ways, EXTENDED with the SDK-backed
# agents docs/agent-inventory.yaml lists that ADR 010's narrower
# lifecycle-executor vocabulary has no reason to know about (issue-poller
# never claims a PR). A superset, not a divergence: every ADR 010 value is
# still a valid ExecutionContext executor.type.
EXECUTOR_TYPES = frozenset({
    "shepherd", "pr-steward", "devloop-workflow", "reconciler", "implementer",
    "issue-investigator", "issue-poller", "service-agent", "incident-responder", "mentor",
})

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ExecutionIdentityError(ValueError):
    """Fail-closed schema/validation failure. Every raise site below is
    either a structural problem (`from_dict`: wrong type, unknown key,
    unsupported `api_version`/`kind`) or a semantic one (`validate`: closed
    vocabulary violation, broken step chain, malformed `trace_id`).
    Non-retryable: callers fix the document, never catch this to fall back to
    a default shape."""


class ExecutionContextRequiredError(RuntimeError):
    """`MCTL_REQUIRE_EXECUTION_CONTEXT` demands a control-plane-minted
    context and none could be produced — the file is missing, unreadable or
    fails tamper evidence. Deliberately NOT an `ExecutionIdentityError`
    subclass: the drivers' degrade-to-local handlers catch that one and mint
    an `asserted_by="local"` context, which is exactly what require mode
    must never allow (ADR 011). This error passes through those handlers
    and kills the run."""


def _hash_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_json(payload: Any) -> bytes:
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutionIdentityError(f"payload is not JSON-serializable: {exc}") from exc


def _reject_unknown_keys(data: Mapping[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ExecutionIdentityError(f"{where}: unknown key(s) {sorted(unknown)!r}")


def _require_mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionIdentityError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _require_str(value: Any, *, where: str, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ExecutionIdentityError(f"{where} must be a {'string' if allow_empty else 'non-empty string'}")
    return value


def _require_int(value: Any, *, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ExecutionIdentityError(f"{where} must be an int")
    return value


def _optional_str(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where=where, allow_empty=True)


def _require_str_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExecutionIdentityError(f"{where} must be a list of strings")
    return tuple(value)


def _require_trace_id(value: Any, *, where: str) -> str:
    text = _require_str(value, where=where, allow_empty=False)
    if not _TRACE_ID_RE.match(text):
        raise ExecutionIdentityError(f"{where} must be 32 lowercase hex characters, got {text!r}")
    return text


# ---------------------------------------------------------------------------
# Nested blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Who asked. `verification` is the trust label a policy author (#197)
    reads to decide how much weight `id` carries — an approve signal with no
    payload records `id=""`, `verification="unverified"` rather than the
    literal string `"unknown"` (dev_loop.py:897-905)."""

    type: str
    id: str = ""
    verification: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "verification": self.verification}

    @classmethod
    def from_dict(cls, data: Any) -> Actor:
        mapping = _require_mapping(data, where="actor")
        _reject_unknown_keys(mapping, frozenset({"type", "id", "verification"}), where="actor")
        return cls(
            type=_require_str(mapping.get("type"), where="actor.type", allow_empty=False),
            id=_require_str(mapping.get("id", ""), where="actor.id"),
            verification=_require_str(mapping.get("verification", "unverified"), where="actor.verification"),
        )


@dataclass(frozen=True)
class Executor:
    """Which agent/service identity runs. `binding` names what a stolen
    `context_id` is narrowed to (ADR 011 sec. "Trust model"): the Argo
    workflow name this context was issued to, plus the agent — a
    tamper-evident pin, not yet a tamper-proof one (every caller still shares
    one admin `MCTL_TOKEN`)."""

    type: str
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
            type=_require_str(mapping.get("type"), where="executor.type", allow_empty=False),
            id=_require_str(mapping.get("id", ""), where="executor.id"),
            agent=_require_str(mapping.get("agent", ""), where="executor.agent"),
            version=_require_str(mapping.get("version", ""), where="executor.version"),
            image_ref=_require_str(mapping.get("image_ref", ""), where="executor.image_ref"),
            binding=_require_str(mapping.get("binding", ""), where="executor.binding"),
        )


@dataclass(frozen=True)
class Scope:
    """Tenant, repository, environment, service, slug — where this execution
    is allowed to leave marks. `tenant` defaults to `"mctlhq"`: this repo runs
    a single tenant today and mctl-api's team-scoping model is not represented
    anywhere else in this codebase (ADR 011 open question)."""

    environment: str
    tenant: str = "mctlhq"
    repository: str = ""
    target_repository_sha: str = ""
    service: str = ""
    slug: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "repository": self.repository,
            "target_repository_sha": self.target_repository_sha,
            "environment": self.environment,
            "service": self.service,
            "slug": self.slug,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Scope:
        mapping = _require_mapping(data, where="scope")
        _reject_unknown_keys(
            mapping,
            frozenset({"tenant", "repository", "target_repository_sha", "environment", "service", "slug"}),
            where="scope",
        )
        return cls(
            tenant=_require_str(mapping.get("tenant", "mctlhq"), where="scope.tenant", allow_empty=False),
            repository=_require_str(mapping.get("repository", ""), where="scope.repository"),
            target_repository_sha=_require_str(
                mapping.get("target_repository_sha", ""), where="scope.target_repository_sha"
            ),
            environment=_require_str(mapping.get("environment"), where="scope.environment", allow_empty=False),
            service=_require_str(mapping.get("service", ""), where="scope.service"),
            slug=_require_str(mapping.get("slug", ""), where="scope.slug"),
        )


@dataclass(frozen=True)
class Trigger:
    """What event carried the ask. `ref` is a bounded, structural pointer
    (an issue URL, a comment URL) — never a payload; see ADR 011 sec. 7."""

    type: str
    ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ref": self.ref}

    @classmethod
    def from_dict(cls, data: Any) -> Trigger:
        mapping = _require_mapping(data, where="trigger")
        _reject_unknown_keys(mapping, frozenset({"type", "ref"}), where="trigger")
        return cls(
            type=_require_str(mapping.get("type"), where="trigger.type", allow_empty=False),
            ref=_require_str(mapping.get("ref", ""), where="trigger.ref"),
        )


@dataclass(frozen=True)
class Correlation:
    """The control-plane run ids both the ledger and #195 traces already
    have: `temporal_workflow_id` matches `issue_ref.workflow_id_for`,
    `argo_workflow_name` matches `ExecutionRecord.argo_workflow_name`."""

    temporal_workflow_id: str = ""
    temporal_run_id: str | None = None
    argo_workflow_name: str | None = None
    attempt: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "temporal_workflow_id": self.temporal_workflow_id,
            "temporal_run_id": self.temporal_run_id,
            "argo_workflow_name": self.argo_workflow_name,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Correlation:
        mapping = _require_mapping(data, where="correlation")
        _reject_unknown_keys(
            mapping,
            frozenset({"temporal_workflow_id", "temporal_run_id", "argo_workflow_name", "attempt"}),
            where="correlation",
        )
        return cls(
            temporal_workflow_id=_require_str(
                mapping.get("temporal_workflow_id", ""), where="correlation.temporal_workflow_id"
            ),
            temporal_run_id=_optional_str(mapping.get("temporal_run_id"), where="correlation.temporal_run_id"),
            argo_workflow_name=_optional_str(
                mapping.get("argo_workflow_name"), where="correlation.argo_workflow_name"
            ),
            attempt=_require_int(mapping.get("attempt", 0), where="correlation.attempt"),
        )


@dataclass(frozen=True)
class Assertions:
    """The trust model made machine-readable (ADR 011 sec. "Trust model").
    `asserted_by` is `"control-plane"` for a control-plane-minted context and
    `"local"` for the degraded fallback `mint_local()` produces.
    `asserted_fields`/`declared_fields` are dotted paths: a consumer that
    needs a trustworthy field checks membership rather than trusting the
    document wholesale."""

    asserted_by: str
    asserted_fields: tuple[str, ...] = ()
    declared_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "asserted_by": self.asserted_by,
            "asserted_fields": list(self.asserted_fields),
            "declared_fields": list(self.declared_fields),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Assertions:
        mapping = _require_mapping(data, where="assertions")
        _reject_unknown_keys(
            mapping, frozenset({"asserted_by", "asserted_fields", "declared_fields"}), where="assertions"
        )
        return cls(
            asserted_by=_require_str(mapping.get("asserted_by"), where="assertions.asserted_by", allow_empty=False),
            asserted_fields=_require_str_tuple(
                mapping.get("asserted_fields", []), where="assertions.asserted_fields"
            ),
            declared_fields=_require_str_tuple(
                mapping.get("declared_fields", []), where="assertions.declared_fields"
            ),
        )


# ---------------------------------------------------------------------------
# ExecutionContext — the top-level document
# ---------------------------------------------------------------------------

_CONTEXT_KEYS = frozenset({
    "api_version", "kind", "context_id", "content_hash", "issued_at", "trace_id",
    "parent_context_id", "workflow_type", "step_sequence", "actor", "executor",
    "scope", "trigger", "correlation", "assertions",
})


@dataclass(frozen=True)
class ExecutionContext:
    """One immutable, content-addressed statement of who/what is executing.
    Only ever produced by `seal()`; `from_dict` reconstructs an already-sealed
    document and re-validates its shape, but never recomputes the hash — use
    `recompute_content_hash` to verify one (T8 in
    tests/test_execution_identity.py)."""

    api_version: str
    kind: str
    context_id: str
    content_hash: str
    issued_at: str
    trace_id: str
    workflow_type: str
    actor: Actor
    executor: Executor
    scope: Scope
    trigger: Trigger
    correlation: Correlation
    assertions: Assertions
    parent_context_id: str | None = None
    step_sequence: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_version": self.api_version,
            "kind": self.kind,
            "context_id": self.context_id,
            "content_hash": self.content_hash,
            "issued_at": self.issued_at,
            "trace_id": self.trace_id,
            "parent_context_id": self.parent_context_id,
            "workflow_type": self.workflow_type,
            "step_sequence": self.step_sequence,
            "actor": self.actor.to_dict(),
            "executor": self.executor.to_dict(),
            "scope": self.scope.to_dict(),
            "trigger": self.trigger.to_dict(),
            "correlation": self.correlation.to_dict(),
            "assertions": self.assertions.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionContext:
        mapping = _require_mapping(data, where="ExecutionContext")
        _reject_unknown_keys(mapping, _CONTEXT_KEYS, where="ExecutionContext")

        api_version_raw = mapping.get("api_version")
        expected_kind = SUPPORTED_API_VERSIONS.get(api_version_raw) if isinstance(api_version_raw, str) else None
        if not isinstance(api_version_raw, str) or expected_kind is None:
            raise ExecutionIdentityError(
                f"unsupported api_version {api_version_raw!r}, expected one of {sorted(SUPPORTED_API_VERSIONS)!r}"
            )
        api_version = api_version_raw
        kind_raw = mapping.get("kind")
        if kind_raw != expected_kind:
            raise ExecutionIdentityError(
                f"kind must be {expected_kind!r} for api_version {api_version!r}, got {kind_raw!r}"
            )
        kind = kind_raw

        context_id = _require_str(mapping.get("context_id"), where="context_id", allow_empty=False)
        content_hash = _require_str(mapping.get("content_hash"), where="content_hash", allow_empty=False)
        if not content_hash.startswith("sha256:"):
            raise ExecutionIdentityError(f"content_hash must carry the 'sha256:' prefix, got {content_hash!r}")
        issued_at = _require_str(mapping.get("issued_at"), where="issued_at", allow_empty=False)
        trace_id = _require_trace_id(mapping.get("trace_id"), where="trace_id")
        parent_context_id = _optional_str(mapping.get("parent_context_id"), where="parent_context_id")
        workflow_type = _require_str(mapping.get("workflow_type"), where="workflow_type", allow_empty=False)
        step_sequence = _require_int(mapping.get("step_sequence", 0), where="step_sequence")

        actor = Actor.from_dict(mapping.get("actor"))
        executor = Executor.from_dict(mapping.get("executor"))
        scope = Scope.from_dict(mapping.get("scope"))
        trigger = Trigger.from_dict(mapping.get("trigger"))
        correlation = Correlation.from_dict(mapping.get("correlation"))
        assertions = Assertions.from_dict(mapping.get("assertions"))

        context = cls(
            api_version=api_version,
            kind=kind,
            context_id=context_id,
            content_hash=content_hash,
            issued_at=issued_at,
            trace_id=trace_id,
            parent_context_id=parent_context_id,
            workflow_type=workflow_type,
            step_sequence=step_sequence,
            actor=actor,
            executor=executor,
            scope=scope,
            trigger=trigger,
            correlation=correlation,
            assertions=assertions,
        )
        context.validate()
        return context

    def validate(self, *, parent: ExecutionContext | None = None) -> None:
        """Enforce everything `from_dict`'s shape checks and `seal()`'s
        construction cannot: closed vocabularies and, when `parent` is
        supplied, the step-chaining rule (T5)."""
        if self.api_version != API_VERSION:
            raise ExecutionIdentityError(f"api_version must be {API_VERSION!r}, got {self.api_version!r}")
        if self.kind != KIND:
            raise ExecutionIdentityError(f"kind must be {KIND!r}, got {self.kind!r}")
        if not self.content_hash.startswith("sha256:"):
            raise ExecutionIdentityError(f"content_hash must carry the 'sha256:' prefix, got {self.content_hash!r}")
        _require_trace_id(self.trace_id, where="trace_id")
        if self.actor.type not in ACTOR_TYPES:
            raise ExecutionIdentityError(f"actor.type {self.actor.type!r} is not one of {sorted(ACTOR_TYPES)!r}")
        if self.actor.verification not in ACTOR_VERIFICATIONS:
            raise ExecutionIdentityError(
                f"actor.verification {self.actor.verification!r} is not one of {sorted(ACTOR_VERIFICATIONS)!r}"
            )
        if self.trigger.type not in TRIGGER_TYPES:
            raise ExecutionIdentityError(f"trigger.type {self.trigger.type!r} is not one of {sorted(TRIGGER_TYPES)!r}")
        if self.workflow_type not in WORKFLOW_TYPES:
            raise ExecutionIdentityError(
                f"workflow_type {self.workflow_type!r} is not one of {sorted(WORKFLOW_TYPES)!r}"
            )
        if self.scope.environment not in ENVIRONMENTS:
            raise ExecutionIdentityError(
                f"scope.environment {self.scope.environment!r} is not one of {sorted(ENVIRONMENTS)!r}"
            )
        if self.executor.type not in EXECUTOR_TYPES:
            raise ExecutionIdentityError(
                f"executor.type {self.executor.type!r} is not one of {sorted(EXECUTOR_TYPES)!r}"
            )

        if parent is not None:
            if self.parent_context_id != parent.context_id:
                raise ExecutionIdentityError(
                    f"parent_context_id {self.parent_context_id!r} does not match "
                    f"parent.context_id {parent.context_id!r}"
                )
            if self.trace_id != parent.trace_id:
                raise ExecutionIdentityError("a child context's trace_id must equal its parent's")
            if self.step_sequence <= parent.step_sequence:
                raise ExecutionIdentityError(
                    f"step_sequence {self.step_sequence} is not strictly greater than "
                    f"parent.step_sequence {parent.step_sequence}"
                )

    def to_log_dict(self) -> dict[str, Any]:
        """Trace/telemetry-export shape (#195 owns traces). Every field this
        schema declares is an identifier, a version, a hash or a bounded
        structural pointer — there is no payload field anywhere in
        `ExecutionContext` for `to_log_dict` to exclude, unlike
        `ContextSnapshot.to_log_dict` (which drops `sources`). Returning the
        full `to_dict()` is therefore the correct, not merely convenient,
        answer: any future field that could not be logged safely would not
        belong in this schema at all (ADR 011 sec. 7)."""
        return self.to_dict()

    def to_execution_correlation(
        self,
        *,
        definition_version: str,
        definition_content_hash: str,
        profile_version: str,
        profile_content_hash: str,
        release_revision: int,
    ) -> Any:
        """Project this context into `context_snapshot.ExecutionCorrelation`
        (ADR 009), so that block is a projection rather than a second
        hand-built copy (task 3). `orchestrator/context_snapshot.py` is not
        imported at module scope — that would give a stdlib-only module a
        same-repo dependency it does not otherwise need — so the import
        happens here, at call time, in the one method that actually needs
        the type.

        The identity-owned fields (`agent`, `environment`,
        `temporal_workflow_id`, `temporal_run_id`, `argo_workflow_name`,
        `target_repository_sha`) come from `self`. The plan-owned fields
        (`definition_version`, `definition_content_hash`, `profile_version`,
        `profile_content_hash`, `release_revision`) are NOT carried on
        `ExecutionContext` at all — ADR 009 sec. 4 already sources them from
        `ExecutionPlan`, which is resolved before submission and pinned by
        publish-time hashes, a different lifecycle than an execution's
        runtime identity — so a caller holding both a context and a plan
        supplies them here rather than this module duplicating
        `ExecutionPlan`'s fields.
        """
        from orchestrator.context_snapshot import ExecutionCorrelation

        return ExecutionCorrelation(
            agent=self.executor.agent,
            environment=self.scope.environment,
            temporal_workflow_id=self.correlation.temporal_workflow_id,
            temporal_run_id=self.correlation.temporal_run_id,
            argo_workflow_name=self.correlation.argo_workflow_name,
            target_repository_sha=self.scope.target_repository_sha,
            definition_version=definition_version,
            definition_content_hash=definition_content_hash,
            profile_version=profile_version,
            profile_content_hash=profile_content_hash,
            release_revision=release_revision,
        )


def _content_payload(
    *,
    trace_id: str,
    parent_context_id: str | None,
    workflow_type: str,
    step_sequence: int,
    actor: Actor,
    executor: Executor,
    scope: Scope,
    trigger: Trigger,
    correlation: Correlation,
    assertions: Assertions,
) -> dict[str, Any]:
    """Every field that participates in `content_hash` — everything except
    `content_hash`, `context_id` and `issued_at` (ADR 011 sec. 2)."""
    return {
        "api_version": API_VERSION,
        "kind": KIND,
        "trace_id": trace_id,
        "parent_context_id": parent_context_id,
        "workflow_type": workflow_type,
        "step_sequence": step_sequence,
        "actor": actor.to_dict(),
        "executor": executor.to_dict(),
        "scope": scope.to_dict(),
        "trigger": trigger.to_dict(),
        "correlation": correlation.to_dict(),
        "assertions": assertions.to_dict(),
    }


def seal(
    *,
    trace_id: str,
    workflow_type: str,
    actor: Actor,
    executor: Executor,
    scope: Scope,
    trigger: Trigger,
    correlation: Correlation,
    assertions: Assertions,
    issued_at: str,
    parent_context_id: str | None = None,
    step_sequence: int = 0,
) -> ExecutionContext:
    """The only constructor that produces a sealed `ExecutionContext`.
    Computes `content_hash = "sha256:" + sha256(canonical JSON of every field
    except content_hash, context_id, issued_at)` and
    `context_id = "ex-" + content_hash[7:23]`, mirroring
    `orchestrator/context_snapshot.py`'s `seal()` and
    `orchestrator/resolver.py`'s `_hash_bytes` convention. `issued_at` is
    caller-supplied and excluded from the hash, so sealing the same inputs
    twice at different times yields the same identity. Raises
    `ExecutionIdentityError` via `validate()` if the assembled document is not
    internally consistent; never returns a partially-sealed context."""
    payload = _content_payload(
        trace_id=trace_id,
        parent_context_id=parent_context_id,
        workflow_type=workflow_type,
        step_sequence=step_sequence,
        actor=actor,
        executor=executor,
        scope=scope,
        trigger=trigger,
        correlation=correlation,
        assertions=assertions,
    )
    content_hash = _hash_bytes(_canonical_json(payload))
    context_id = "ex-" + content_hash[7:23]
    context = ExecutionContext(
        api_version=API_VERSION,
        kind=KIND,
        context_id=context_id,
        content_hash=content_hash,
        issued_at=issued_at,
        trace_id=trace_id,
        parent_context_id=parent_context_id,
        workflow_type=workflow_type,
        step_sequence=step_sequence,
        actor=actor,
        executor=executor,
        scope=scope,
        trigger=trigger,
        correlation=correlation,
        assertions=assertions,
    )
    context.validate()
    return context


def recompute_content_hash(context: ExecutionContext) -> str:
    """Recompute the `content_hash` a fresh `seal()` of `context`'s fields
    (excluding `content_hash`, `context_id`, `issued_at`) would produce. Used
    to verify golden fixtures and tamper evidence (T8) without mutating the
    context under test."""
    payload = _content_payload(
        trace_id=context.trace_id,
        parent_context_id=context.parent_context_id,
        workflow_type=context.workflow_type,
        step_sequence=context.step_sequence,
        actor=context.actor,
        executor=context.executor,
        scope=context.scope,
        trigger=context.trigger,
        correlation=context.correlation,
        assertions=context.assertions,
    )
    return _hash_bytes(_canonical_json(payload))


# ---------------------------------------------------------------------------
# Local fallback — no control plane involved
# ---------------------------------------------------------------------------

#: File path env var the CWFT is expected to export (design.md sec. 2): a
#: file, not an inline env value, so the sealed document never lands in an
#: Argo parameter dump or a `printenv` in agent logs.
MCTL_EXECUTION_CONTEXT_FILE_ENV = "MCTL_EXECUTION_CONTEXT_FILE"
#: Fail-closed switch for cluster runs that must never proceed without a
#: control-plane-minted context (requirements.md "Trust model").
MCTL_REQUIRE_EXECUTION_CONTEXT_ENV = "MCTL_REQUIRE_EXECUTION_CONTEXT"


def mint_local(
    *,
    executor_type: str,
    workflow_type: str = "implement",
    agent: str = "",
    version: str = "",
    scope: Scope | None = None,
    trigger: Trigger | None = None,
    actor: Actor | None = None,
    correlation: Correlation | None = None,
    trace_id: str | None = None,
    issued_at: str | None = None,
) -> ExecutionContext:
    """Degrade to an explicitly `unverified`, locally-minted context — the
    fallback `load_from_environment()` uses outside the cluster (design.md
    sec. 2), and the one `ExecutionIdentityError`-free path through this
    module. `actor.verification` is always `"unverified"` and
    `assertions.asserted_by` is always `"local"`, regardless of what a caller
    passes for `actor`, so a local run can never be mistaken for a
    control-plane assertion."""
    resolved_actor = actor or Actor(type="system", id="", verification="unverified")
    resolved_actor = Actor(type=resolved_actor.type, id=resolved_actor.id, verification="unverified")
    resolved_scope = scope or Scope(environment="production")
    resolved_trigger = trigger or Trigger(type="manual")
    resolved_correlation = correlation or Correlation()
    resolved_trace_id = trace_id or hashlib.sha256(os.urandom(16)).hexdigest()[:32]
    resolved_issued_at = issued_at or _now_iso()
    executor = Executor(type=executor_type, agent=agent, version=version)
    return seal(
        trace_id=resolved_trace_id,
        workflow_type=workflow_type,
        actor=resolved_actor,
        executor=executor,
        scope=resolved_scope,
        trigger=resolved_trigger,
        correlation=resolved_correlation,
        assertions=Assertions(asserted_by="local", asserted_fields=(), declared_fields=("actor", "executor", "scope")),
        issued_at=resolved_issued_at,
    )


def _now_iso() -> str:
    # Local import: datetime is stdlib, but kept inside the function so the
    # module-level surface stays exactly the names ADR 011 documents.
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_from_environment(
    *,
    executor_type: str,
    workflow_type: str = "implement",
    agent: str = "",
    version: str = "",
) -> ExecutionContext:
    """Read the sealed context the CWFT wrote to
    `MCTL_EXECUTION_CONTEXT_FILE`, or degrade to a locally-minted, explicitly
    `unverified` context (requirements.md "Trust model"). When
    `MCTL_REQUIRE_EXECUTION_CONTEXT` is set, fails closed instead of
    degrading — for a cluster run that must never proceed without a
    control-plane-minted identity. Never raises on the local-mint
    path: a driver calling this with a bare `executor_type` always gets back
    an ExecutionContext, so local development and tests keep working.

    Error contract: every failure to produce a verified context from a
    PRESENT file — unreadable path, bad encoding, truncated JSON, schema
    violation, tamper-evidence mismatch — surfaces as
    `ExecutionIdentityError`, so a degrading caller needs exactly one narrow
    catch and nothing (e.g. `UnicodeDecodeError`, an OSError) escapes it.
    When `MCTL_REQUIRE_EXECUTION_CONTEXT` is set, the same failures and the
    missing-file case raise `ExecutionContextRequiredError` instead, which
    degrading callers must NOT catch: require mode fails closed even when
    the file is present but broken."""
    path = os.environ.get(MCTL_EXECUTION_CONTEXT_FILE_ENV, "").strip()
    required = bool(os.environ.get(MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, "").strip())
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
            data = json.loads(text)
            context = ExecutionContext.from_dict(data)
            # from_dict() only checks shape and vocabulary (see its docstring);
            # this is the one production call site that reads an untrusted
            # document, so verify the tamper evidence ADR 011 promises here.
            expected_content_hash = recompute_content_hash(context)
            # compare_digest is hygiene, not a proven timing oracle: both
            # sides derive from the same caller-supplied document.
            if not hmac.compare_digest(expected_content_hash, context.content_hash):
                raise ExecutionIdentityError(
                    f"content_hash mismatch for context_id={context.context_id!r} loaded from "
                    f"{MCTL_EXECUTION_CONTEXT_FILE_ENV}={path!r}: document content does not match "
                    "its declared content_hash"
                )
            # content_hash is computed over every field except content_hash,
            # context_id and issued_at itself (_content_payload), so a matching
            # content_hash alone does not prove context_id was not swapped for
            # some other (even legitimately sealed) context's id. seal() derives
            # context_id deterministically as "ex-" + content_hash[7:23]; recheck
            # that binding explicitly so a tampered context_id is caught too.
            expected_context_id = "ex-" + expected_content_hash[7:23]
            if not hmac.compare_digest(context.context_id, expected_context_id):
                raise ExecutionIdentityError(
                    f"context_id mismatch loaded from {MCTL_EXECUTION_CONTEXT_FILE_ENV}={path!r}: "
                    f"declared context_id={context.context_id!r} does not match {expected_context_id!r} "
                    "derived from its own content_hash"
                )
            return context
        except (OSError, ValueError) as exc:
            # ValueError covers ExecutionIdentityError itself plus its
            # parse-stage siblings: json.JSONDecodeError and
            # UnicodeDecodeError are ValueError subclasses that are NOT
            # ExecutionIdentityError subclasses.
            if required:
                raise ExecutionContextRequiredError(
                    f"{MCTL_REQUIRE_EXECUTION_CONTEXT_ENV} demands a control-plane-minted context, "
                    f"but {MCTL_EXECUTION_CONTEXT_FILE_ENV}={path!r} did not yield one: {exc}"
                ) from exc
            if isinstance(exc, ExecutionIdentityError):
                raise
            raise ExecutionIdentityError(
                f"{MCTL_EXECUTION_CONTEXT_FILE_ENV}={path!r} is unreadable or unparseable: {exc}"
            ) from exc
    if required:
        raise ExecutionContextRequiredError(
            f"{MCTL_EXECUTION_CONTEXT_FILE_ENV} is not set and {MCTL_REQUIRE_EXECUTION_CONTEXT_ENV} "
            "demands a control-plane-minted context"
        )
    return mint_local(executor_type=executor_type, workflow_type=workflow_type, agent=agent, version=version)
