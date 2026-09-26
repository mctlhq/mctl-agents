"""The capability discovery/invocation runtime (mctlhq/mctl-agents#242 slice
2, ADR 017: docs/adr/017-capability-discovery-and-gateway-contract.md).

`orchestrator/capability.py` (slice 1) is the CONTRACT: frozen dataclasses,
`seal()`, `validate()`, the `PolicyCheckpoint` seam. Nothing in that module
connects a provider, lists a tool, or dispatches an invocation. This module
is the RUNTIME that does: `resolve_eligible()` turns a resolved
`ExecutionPlan` plus an ordered provider list into one sealed
`CapabilitySet` (ADR 017 sec. 5), and `CapabilityGateway` serves the three
SDK tools (`capability_search`, `capability_describe`, `capability_invoke`)
a model actually calls, over that one sealed set.

Imports `claude_agent_sdk` and `mcp` — both process-heavy, network-capable
packages the long-lived Temporal worker (256Mi, ADR 008) must never load
(tests/test_worker_isolation.py). This module is therefore NEVER imported at
module scope by any worker-reachable module; a future caller (slice 3) must
import it lazily inside the function that actually runs an agent, exactly as
`orchestrator/options.py` defers `orchestrator.execution_identity` and
`orchestrator/run_implementer.py` defers `orchestrator.auth`.
`orchestrator/capability.py` stays stdlib-only and worker-importable —
nothing in this module changes that.

No production code constructs a `CapabilityGateway` in this slice
(design.md "The #197 adapter"): slice 3 chooses between
`orchestrator.capability.AbsentPolicyCheckpoint` and
`PolicyDecidePolicyCheckpoint` at the one construction site that wires the
investigator's discovery mode.

Discovery narrows, it never grants (ADR 009 sec. 5, ADR 017 sec. 3): this
module never widens `ExecutionPlan.tools`. A capability outside
`plan.tools` never becomes a `CapabilityDescriptor`
(`_first_matching_pattern` returns `None` for it, and it is only ever
counted, in `excluded_count`, never named). `capability_invoke` refuses
anything outside the sealed set with `not-eligible` before it ever reaches
a provider.
"""
from __future__ import annotations

import fnmatch
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol

from orchestrator.capability import (
    MAX_KEYWORD_LENGTH,
    MAX_KEYWORDS,
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
    AbsentPolicyCheckpoint,
    CapabilityDescriptor,
    CapabilitySet,
    CapabilityStrategy,
    CheckpointVerdict,
    ExecutionCorrelation,
    InvocationRecord,
    PolicyCheckpoint,
    ProviderRef,
    RetentionPolicy,
    classify_consequence,
    load_consequence_table,
    policy_checkpoint_status,
    seal,
)
from orchestrator.context_snapshot import canonical_json, hash_bytes
from orchestrator.resolver import ExecutionPlan

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Cap on one `capability_describe` request (ADR 017 sec. 5; design.md:
#: "MAX_DESCRIBE_IDS does not exist on main ... neither ADR 017 nor the
#: requirements fix a number; 10 is a reviewable default").
MAX_DESCRIBE_IDS = 10
#: Row cap `capability_search` falls back to when the caller omits `limit`.
DEFAULT_SEARCH_LIMIT = 20
#: Hard ceiling on `capability_search`'s `limit`, whatever the caller asks for.
MAX_SEARCH_LIMIT = 50

#: `CapabilityStrategy` identity for this slice's ranker: lexical, fixed
#: order (matches by pattern order, ranks by set order) — "no ranker beyond
#: a lexical pilot exists yet" (ADR 017 sec. 1). A future ranker changes
#: this name/version, not the contract.
STRATEGY_NAME = "lexical-fixed-order"
STRATEGY_VERSION = "1.0.0"
#: Default `RetentionPolicy` for a set this module seals (requirements.md
#: "Where a sealed CapabilitySet durably lives": structured logs plus this
#: class field now, mctl-api persistence tracked separately).
DEFAULT_RETENTION_CLASS = "execution-record"
DEFAULT_RETENTION_EXPIRES_AFTER_DAYS = 90


class GatewayError(RuntimeError):
    """A provider/runtime failure that is not itself a shape violation
    (`orchestrator.capability.CapabilityError` stays that module's own error
    type for schema/invariant failures). Every subclass below fixes a
    `reason_code` from `orchestrator.capability.REASON_CODES`, so a caller
    can report the closed vocabulary without parsing this exception's
    message."""

    reason_code = "provider-error"


class ProviderUnavailableError(GatewayError):
    """A provider could not be reached or listed at all. Raised, never
    swallowed into a shorter `CapabilitySet` — the
    `orchestrator/mcp_guard.py` lesson restated at set level (ADR 017
    sec. 5): a short set and a broken provider must never look alike."""

    reason_code = "provider-unavailable"


class ProviderTimeoutError(GatewayError):
    reason_code = "timeout"


# ---------------------------------------------------------------------------
# Provider session seam — what discovery/invocation need from a connected
# provider, remote or execution-local. Deliberately not `mcp`'s own
# ClientSession type: a test substitutes a fake implementing this Protocol
# with no network, and no claude_agent_sdk/mcp import in the test module.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderTool:
    """One tool as advertised by a provider, translated into the one shape
    this module needs regardless of transport (remote MCP, execution-local
    registry). `CapabilityDescriptor` never carries the schema itself (a
    descriptor, not a payload carrier — orchestrator/capability.py's own
    rule); the actual schema lives here, held privately by
    `CapabilityGateway`, and is never serialized into the sealed
    `CapabilitySet`."""

    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    annotations: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallResult:
    """The result of one provider tool call, already reduced to text. The
    gateway hashes this for its `InvocationRecord`; it never logs the text
    itself (ADR 017 sec. 5's no-payload trace rule)."""

    is_error: bool
    text: str


class ProviderSession(Protocol):
    """A connected provider, ready to list and call its tools. Remote
    (`mcp-remote`) sessions are real `mcp.ClientSession`s wrapped by
    `_McpProviderSession` below; execution-local (`mcp-local`) sessions are
    whatever a caller's in-process registry supplies — both satisfy this
    same Protocol, so discovery/invocation never branch on transport beyond
    picking which session to open."""

    async def list_tools(self) -> list[ProviderTool]: ...

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolCallResult: ...


#: One provider connection, opened for the duration of one `async with`
#: block. `_discover` opens one per remote provider to list tools;
#: `CapabilityGateway.invoke` opens one more, per call, to dispatch —
#: reconnecting rather than holding a session open across SDK tool-call
#: boundaries. ADR 017's "one list_tools round trip per execution" governs
#: discovery; invocation is inherently its own round trip regardless.
ProviderConnector = Callable[[ProviderRef, Mapping[str, str]], AbstractAsyncContextManager[ProviderSession]]


@asynccontextmanager
async def _default_remote_connector(
    provider: ProviderRef, headers: Mapping[str, str],
) -> AsyncIterator[ProviderSession]:
    """The real `mcp-remote` connector: `mcp.client.streamable_http` with
    whatever headers the caller supplies (`_default_remote_headers()`
    reuses `orchestrator/options.py::_execution_context_headers()` and the
    same bearer-token convention `mctl_mcp_config()` uses today — this
    function adds no header logic of its own, per design.md's "reuse the
    helper; do not copy the header logic").

    `mcp` is imported lazily, inside this function, so importing this
    module's other names (the dataclasses, `resolve_eligible`'s signature)
    never requires the package to be installed in a context that only
    needs the shapes — mirrors `orchestrator/capability.py`'s own lazy
    `yaml` import in `load_consequence_table`.
    """
    import mcp
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(provider.endpoint_ref, headers=dict(headers)) as (
            read_stream, write_stream, _get_session_id,
        ):
            async with mcp.ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield _McpProviderSession(provider, session)
    except TimeoutError as exc:
        raise ProviderTimeoutError(f"provider {provider.id!r}: timed out connecting: {exc}") from exc
    except Exception as exc:
        raise ProviderUnavailableError(f"provider {provider.id!r}: connection failed: {exc}") from exc


@dataclass
class _McpProviderSession:
    """Adapts a real, already-initialized `mcp.ClientSession` to the
    `ProviderSession` Protocol. `session` is typed `Any` so this dataclass
    carries no top-level `mcp` type annotation — the import stays confined
    to `_default_remote_connector`'s function body."""

    provider: ProviderRef
    session: Any

    async def list_tools(self) -> list[ProviderTool]:
        try:
            result = await self.session.list_tools()
        except TimeoutError as exc:
            raise ProviderTimeoutError(f"provider {self.provider.id!r}: list_tools timed out: {exc}") from exc
        except Exception as exc:
            raise GatewayError(f"provider {self.provider.id!r}: list_tools failed: {exc}") from exc
        return [
            ProviderTool(
                name=t.name,
                description=t.description or "",
                input_schema=t.inputSchema or {},
                annotations=_annotations_to_dict(getattr(t, "annotations", None)),
            )
            for t in result.tools
        ]

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolCallResult:
        try:
            result = await self.session.call_tool(tool, dict(arguments))
        except TimeoutError as exc:
            raise ProviderTimeoutError(f"provider {self.provider.id!r}: call_tool timed out: {exc}") from exc
        except Exception as exc:
            raise GatewayError(f"provider {self.provider.id!r}: call_tool failed: {exc}") from exc
        text = "\n".join(str(getattr(block, "text", "")) for block in (result.content or []))
        return ToolCallResult(is_error=bool(result.isError), text=text)


def _annotations_to_dict(annotations: Any) -> dict[str, Any]:
    """MCP `ToolAnnotations` -> a plain dict, advisory-only (ADR 017
    sec. 8), tolerant of every shape a provider might actually hand back."""
    if annotations is None:
        return {}
    if hasattr(annotations, "model_dump"):
        return dict(annotations.model_dump(exclude_none=True))
    if isinstance(annotations, Mapping):
        return dict(annotations)
    return {}


@asynccontextmanager
async def _static_session(session: ProviderSession) -> AsyncIterator[ProviderSession]:
    """Wraps an already-constructed execution-local `ProviderSession` (the
    caller's own in-process registry) in the same async-context-manager
    shape `_default_remote_connector` uses, so `_discover`/`invoke` never
    branch on transport beyond which connector they call."""
    yield session


def _correlation_headers(execution: ExecutionCorrelation, capability_set_id: str) -> dict[str, str]:
    """`#196` correlation fields as provider request metadata, ADDITIVE on
    top of the identity headers `_default_remote_headers()` already carries
    and the `MCTL_TOKEN` bearer credential (requirements.md: "propagate the
    execution identity/correlation fields ... as provider request metadata
    alongside the existing MCTL_TOKEN bearer credential"). Correlation is
    for tracing only, never an authorization claim: the bearer credential
    remains what the provider actually authorizes on (ADR 017 sec. 5)."""
    fields = {
        "X-Mctl-Agent": execution.agent,
        "X-Mctl-Environment": execution.environment,
        "X-Mctl-Temporal-Workflow-Id": execution.temporal_workflow_id,
        "X-Mctl-Target-Repository-Sha": execution.target_repository_sha,
        "X-Mctl-Definition-Version": execution.definition_version,
        "X-Mctl-Definition-Content-Hash": execution.definition_content_hash,
        "X-Mctl-Profile-Version": execution.profile_version,
        "X-Mctl-Profile-Content-Hash": execution.profile_content_hash,
        "X-Mctl-Capability-Set-Id": capability_set_id,
        "X-Mctl-Argo-Workflow-Name": execution.argo_workflow_name or "",
    }
    return {key: value for key, value in fields.items() if value}


def _default_remote_headers() -> dict[str, str]:
    """The header set `orchestrator/options.py::mctl_mcp_config()` builds
    for the CLI's own mctl connection: `Authorization: Bearer $MCTL_TOKEN`
    plus the `#196` correlation headers `_execution_context_headers()`
    computes (fails closed under `MCTL_REQUIRE_EXECUTION_CONTEXT`, inherited
    rather than reimplemented). Reused verbatim so the gateway's own
    connection carries the exact identity the CLI's direct connection
    would have."""
    import os

    from orchestrator.options import _execution_context_headers

    headers = dict(_execution_context_headers())
    token = os.environ.get("MCTL_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------------
# Discovery — matching, descriptor construction, the shared traversal both
# resolve_eligible() and CapabilityGateway.build() use.
# ---------------------------------------------------------------------------


def _sdk_visible_name(provider: ProviderRef, bare_tool_name: str) -> str:
    """`mcp__<alias>__<tool>` for a remote/local MCP tool, or the bare name
    for `sdk-builtin` (ADR 017 sec. 1's `CapabilityDescriptor.tool_name`)."""
    if provider.type == "sdk-builtin":
        return bare_tool_name
    return f"mcp__{provider.alias}__{bare_tool_name}"


def _first_matching_pattern(sdk_visible_name: str, plan_tools: Sequence[str]) -> str | None:
    """The first `plan.tools` entry `sdk_visible_name` matches under
    `fnmatch.fnmatchcase` — the same case-sensitive semantics
    `CapabilitySet.validate()`'s narrowing invariant checks, and
    `orchestrator/policy_checkpoint.py`'s own operation matching. `None`
    means: no entry matches, so this tool is EXCLUDED (counted, never
    named) — the narrowing invariant made testable at discovery time."""
    for pattern in plan_tools:
        if fnmatch.fnmatchcase(sdk_visible_name, pattern):
            return pattern
    return None


def _one_line(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    return stripped.splitlines()[0][:MAX_SUMMARY_LENGTH]


def _bounded_title(name: str) -> str:
    return name[:MAX_TITLE_LENGTH] if name else name


def _derive_keywords(bare_tool_name: str) -> tuple[str, ...]:
    """The lexical pilot's search-index terms: `bare_tool_name` split on
    `_`, bounded to what `CapabilityDescriptor` accepts. No ranking beyond
    this — `STRATEGY_NAME`/`STRATEGY_VERSION` name this pilot so a better
    ranker can replace it without a contract change (ADR 017 sec. 1)."""
    parts = [p[:MAX_KEYWORD_LENGTH] for p in bare_tool_name.split("_") if p]
    return tuple(parts[:MAX_KEYWORDS])


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _discover(
    plan: ExecutionPlan,
    providers: Sequence[ProviderRef],
    *,
    local_sessions: Mapping[str, ProviderSession],
    connector: ProviderConnector | None,
    headers: Mapping[str, str] | None,
    consequence_table: Mapping[str, str] | None,
) -> tuple[list[CapabilityDescriptor], int, dict[str, Mapping[str, Any]], dict[str, tuple[ProviderRef, str]]]:
    """Connect every declared provider once, in order, list its tools,
    match each against `plan.tools`, and build one `CapabilityDescriptor`
    per match. Returns `(descriptors, excluded_count, schemas_by_id,
    dispatch_by_id)` — the last two are private caches (actual input
    schema, and `(provider, bare_tool_name)` to call) that never enter the
    sealed `CapabilitySet` (ADR 017 sec. 1: a descriptor is not a payload
    carrier).

    A provider that raises while connecting or listing (`ProviderUnavailableError`,
    `ProviderTimeoutError`, or any other exception `_McpProviderSession`/a
    caller's local session raises) propagates unchanged: this function
    never catches a provider failure to seal a shorter set instead (ADR 017
    sec. 5, the `orchestrator/mcp_guard.py` lesson restated at set level).
    """
    resolved_connector = connector if connector is not None else _default_remote_connector
    resolved_headers = dict(headers) if headers is not None else _default_remote_headers()
    table = consequence_table if consequence_table is not None else load_consequence_table()

    descriptors: list[CapabilityDescriptor] = []
    schemas_by_id: dict[str, Mapping[str, Any]] = {}
    dispatch_by_id: dict[str, tuple[ProviderRef, str]] = {}
    excluded_count = 0

    for provider in providers:
        if provider.type == "mcp-remote":
            session_cm = resolved_connector(provider, resolved_headers)
        elif provider.id in local_sessions:
            session_cm = _static_session(local_sessions[provider.id])
        else:
            raise GatewayError(
                f"provider {provider.id!r}: no session available for type {provider.type!r} "
                "(mcp-remote providers use the connector; anything else must be registered in "
                "local_sessions by provider.id)"
            )
        async with session_cm as session:
            tools = await session.list_tools()

        for info in tools:
            sdk_name = _sdk_visible_name(provider, info.name)
            matched_pattern = _first_matching_pattern(sdk_name, plan.tools)
            if matched_pattern is None:
                excluded_count += 1
                continue
            capability_id = f"mctl://{provider.type}/{provider.id}/{info.name}"
            schema_bytes = canonical_json(dict(info.input_schema))
            descriptor = CapabilityDescriptor(
                capability_id=capability_id,
                tool_name=sdk_name,
                provider=provider,
                title=_bounded_title(info.name),
                summary=_one_line(info.description),
                keywords=_derive_keywords(info.name),
                input_schema_hash=hash_bytes(schema_bytes),
                input_schema_bytes=len(schema_bytes),
                consequence=classify_consequence(info.name, table, provider_id=provider.id),
                matched_tool_pattern=matched_pattern,
                annotations=dict(info.annotations),
            )
            descriptors.append(descriptor)
            schemas_by_id[capability_id] = info.input_schema
            dispatch_by_id[capability_id] = (provider, info.name)

    return descriptors, excluded_count, schemas_by_id, dispatch_by_id


async def resolve_eligible(
    plan: ExecutionPlan,
    correlation: ExecutionCorrelation,
    providers: Sequence[ProviderRef],
    *,
    local_sessions: Mapping[str, ProviderSession] = MappingProxyType({}),
    connector: ProviderConnector | None = None,
    headers: Mapping[str, str] | None = None,
    consequence_table: Mapping[str, str] | None = None,
    strategy: CapabilityStrategy | None = None,
    retention: RetentionPolicy | None = None,
    created_at: str | None = None,
) -> CapabilitySet:
    """Resolve one sealed `CapabilitySet` for one execution (ADR 017
    sec. 5, task 5). `providers` is an explicit, ordered list — alias
    assignment is by that order (the profile field that declares providers,
    `spec.capabilityDiscovery.providers`, is slice 3's job); this slice
    takes the list as a parameter.

    Remote (`mcp-remote`) providers are connected via
    `mcp.client.streamable_http`, using `connector`/`headers` if supplied,
    else the real default (`_default_remote_connector`/
    `_default_remote_headers`). Execution-local providers are looked up in
    `local_sessions` by `provider.id` — the "in-process registry" design.md
    names; there is no default for it, since no execution-local capability
    ships in this slice.

    Raises whatever `_discover` raises on a provider failure (never seals a
    shorter set instead), and raises `orchestrator.capability.CapabilityError`
    (via `seal()`/`validate()`) if the assembled document is not internally
    consistent — in particular `CapabilityCollisionError` if two providers
    resolve to one SDK-visible name or claim one alias (ADR 017 sec. 4).
    """
    descriptors, excluded_count, _schemas, _dispatch = await _discover(
        plan, providers,
        local_sessions=local_sessions, connector=connector, headers=headers, consequence_table=consequence_table,
    )
    return seal(
        execution=correlation,
        plan_tools=plan.tools,
        providers=tuple(providers),
        capabilities=descriptors,
        excluded_count=excluded_count,
        strategy=strategy if strategy is not None else CapabilityStrategy(name=STRATEGY_NAME, version=STRATEGY_VERSION),
        retention=retention if retention is not None else RetentionPolicy(
            class_=DEFAULT_RETENTION_CLASS, expires_after_days=DEFAULT_RETENTION_EXPIRES_AFTER_DAYS,
        ),
        created_at=created_at if created_at is not None else _utcnow_iso(),
    )


# ---------------------------------------------------------------------------
# Tracing (#195-shaped) — one line per sealed set and per invocation. Ids,
# hashes, counts, durations, reason codes only: `payload` must already be
# one of CapabilitySet.to_log_dict() or InvocationRecord.to_dict(), both of
# which carry no argument/result field by construction.
# ---------------------------------------------------------------------------


def _emit_trace(payload: Mapping[str, Any], *, event: str) -> None:
    """Mirrors `orchestrator/policy_checkpoint.py`'s `emit()`: print one
    structured line; never raise. `json` is imported here, not at module
    scope, purely to keep this function's one job visually self-contained —
    it costs nothing beyond that, `json` being stdlib."""
    import json

    try:
        print(f"CAPABILITY_{event.upper()} {json.dumps(payload, sort_keys=True)}", flush=True)
    except Exception:  # noqa: BLE001, S110 — tracing must never fail the caller
        pass


# ---------------------------------------------------------------------------
# The #197 adapter (task 6b) — wraps the general policy checkpoint
# (orchestrator/policy_checkpoint.py, ADR 014) instead of reinventing policy
# evaluation (ADR 017 sec. 6). No production code constructs the gateway in
# this slice, so no construction site chooses between this and
# AbsentPolicyCheckpoint yet — that is slice 3's job.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecidePolicyCheckpoint:
    """A `PolicyCheckpoint` (orchestrator/capability.py) adapter wrapping
    `orchestrator.policy_checkpoint.decide` — the same general `#197`
    checkpoint `orchestrator/options.py`'s `_PolicyCheckpointHook` already
    puts every direct `mcp__mctl__*` call through. `grants` is the same
    allow-list that hook evaluates against: the resolved `ExecutionPlan`'s
    tool entries, so the identical `mctl-mcp-default-approval` rule governs
    a capability invocation exactly as it governs a directly-connected
    call today — routing through the gateway changes WHERE a call is
    checked, never the policy that decides it.

    The `PolicyCheckpoint.check(descriptor, correlation)` seam (ADR 017
    sec. 6) carries no `arguments` parameter — fixed by slice 1, not
    reopened here — so this adapter's `decide()` call always digests an
    empty argument set; the checkpoint's decision is about WHICH tool a
    correlation may call, not the arguments of one particular call.
    """

    grants: tuple[str, ...] = ()

    def check(self, descriptor: CapabilityDescriptor, correlation: ExecutionCorrelation) -> CheckpointVerdict:
        from orchestrator import policy_checkpoint

        decision = policy_checkpoint.checkpoint(
            policy_checkpoint.MCP_TOOL_CALL,
            descriptor.tool_name,
            descriptor.provider.alias,
            {},
            grants=self.grants,
            metadata={"capability_id": descriptor.capability_id, "agent": correlation.agent},
        )
        return CheckpointVerdict(
            decision="allowed" if decision.permitted else "denied",
            reason=decision.reason,
        )


# ---------------------------------------------------------------------------
# CapabilityGateway — serves capability_search/describe/invoke over one
# sealed CapabilitySet.
# ---------------------------------------------------------------------------

#: Consequence tiers that must clear the PolicyCheckpoint before dispatch
#: (ADR 017 sec. 8): "'mutating' and 'consequential' capabilities are the
#: ones capability_invoke submits to the PolicyCheckpoint before dispatch."
#: `read-only` never reaches it, even one that discloses sensitive data —
#: an open product question ADR 017 sec. 8 leaves for a later slice, not
#: reopened here.
_CHECKPOINT_CONSEQUENCES = frozenset({"mutating", "consequential"})


@dataclass
class CapabilityGateway:
    """Serves the three SDK tools over one sealed `CapabilitySet`. Normally
    built by `CapabilityGateway.build()` (the async factory that actually
    talks to providers); every field below can also be supplied directly,
    which is how tests exercise `search`/`describe`/`invoke` against a
    fixed sealed set and a recording fake provider with no network and no
    `claude_agent_sdk`/`mcp` import at all.

    `descriptors_by_id`/`schemas_by_id`/`dispatch_by_id` are private caches
    the SAME provider round trip that sealed `capability_set` produced —
    never a second one, and never serialized into the sealed document
    itself (a descriptor is not a payload carrier)."""

    capability_set: CapabilitySet
    checkpoint: PolicyCheckpoint = field(default_factory=AbsentPolicyCheckpoint)
    descriptors_by_id: Mapping[str, CapabilityDescriptor] = field(default_factory=dict)
    schemas_by_id: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    dispatch_by_id: Mapping[str, tuple[ProviderRef, str]] = field(default_factory=dict)
    local_sessions: Mapping[str, ProviderSession] = field(default_factory=dict)
    connector: ProviderConnector | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    async def build(
        cls,
        plan: ExecutionPlan,
        correlation: ExecutionCorrelation,
        providers: Sequence[ProviderRef],
        *,
        local_sessions: Mapping[str, ProviderSession] = MappingProxyType({}),
        connector: ProviderConnector | None = None,
        headers: Mapping[str, str] | None = None,
        consequence_table: Mapping[str, str] | None = None,
        checkpoint: PolicyCheckpoint | None = None,
        strategy: CapabilityStrategy | None = None,
        retention: RetentionPolicy | None = None,
        created_at: str | None = None,
    ) -> CapabilityGateway:
        """Discover, seal, and emit the set-sealing trace line — one
        provider round trip, shared with the private schema/dispatch
        caches this gateway serves `capability_describe`/`capability_invoke`
        from."""
        resolved_headers = dict(headers) if headers is not None else _default_remote_headers()
        descriptors, excluded_count, schemas_by_id, dispatch_by_id = await _discover(
            plan, providers,
            local_sessions=local_sessions, connector=connector, headers=resolved_headers,
            consequence_table=consequence_table,
        )
        capability_set = seal(
            execution=correlation,
            plan_tools=plan.tools,
            providers=tuple(providers),
            capabilities=descriptors,
            excluded_count=excluded_count,
            strategy=strategy if strategy is not None else CapabilityStrategy(
                name=STRATEGY_NAME, version=STRATEGY_VERSION,
            ),
            retention=retention if retention is not None else RetentionPolicy(
                class_=DEFAULT_RETENTION_CLASS, expires_after_days=DEFAULT_RETENTION_EXPIRES_AFTER_DAYS,
            ),
            created_at=created_at if created_at is not None else _utcnow_iso(),
        )
        _emit_trace(capability_set.to_log_dict(), event="set_sealed")
        return cls(
            capability_set=capability_set,
            checkpoint=checkpoint if checkpoint is not None else AbsentPolicyCheckpoint(),
            descriptors_by_id={d.capability_id: d for d in capability_set.capabilities},
            schemas_by_id=schemas_by_id,
            dispatch_by_id=dispatch_by_id,
            local_sessions=dict(local_sessions),
            connector=connector,
            headers=resolved_headers,
        )

    # -- capability_search --------------------------------------------------

    def search(self, query: str = "", limit: int | None = None) -> dict[str, Any]:
        """Compact rows only — `capability_id`, `title`, one-line `summary`,
        `consequence` — drawn only from the sealed set. Never a schema
        (ADR 017 sec. 5)."""
        row_limit = DEFAULT_SEARCH_LIMIT if limit is None else max(0, min(int(limit), MAX_SEARCH_LIMIT))
        needle = (query or "").strip().lower()
        rows: list[dict[str, Any]] = []
        for descriptor in self.capability_set.capabilities:
            haystack = " ".join((descriptor.title, descriptor.summary, *descriptor.keywords)).lower()
            if needle and needle not in haystack:
                continue
            rows.append({
                "capability_id": descriptor.capability_id,
                "title": descriptor.title,
                "summary": descriptor.summary,
                "consequence": descriptor.consequence,
            })
        return {"results": rows[:row_limit]}

    # -- capability_describe -------------------------------------------------

    def describe(self, capability_ids: Sequence[str]) -> dict[str, Any]:
        """Full input schemas for at most `MAX_DESCRIBE_IDS` eligible ids.
        An id outside the sealed set answers `not-found` — indistinguishable
        from a genuinely nonexistent id, so this leaks nothing about what
        was withheld (ADR 017 sec. 5)."""
        if len(capability_ids) > MAX_DESCRIBE_IDS:
            return {
                "results": {},
                "reason_code": "invalid-arguments",
                "error": f"at most {MAX_DESCRIBE_IDS} capability_ids may be requested at once, got "
                         f"{len(capability_ids)}",
            }
        results: dict[str, Any] = {}
        for capability_id in capability_ids:
            descriptor = self.descriptors_by_id.get(capability_id)
            if descriptor is None:
                results[capability_id] = {"reason_code": "not-found"}
                continue
            results[capability_id] = {
                "reason_code": "ok",
                "title": descriptor.title,
                "summary": descriptor.summary,
                "consequence": descriptor.consequence,
                "input_schema": dict(self.schemas_by_id.get(capability_id, {})),
            }
        return {"results": results}

    # -- capability_invoke ---------------------------------------------------

    async def invoke(self, capability_id: str, arguments: Any) -> dict[str, Any]:
        """Membership check against the sealed set, then the
        `PolicyCheckpoint` (for `mutating`/`consequential` capabilities
        only), then dispatch — remote via the provider connector, local in
        process, with zero network hop (ADR 017 sec. 5). Returns exactly one
        reason code from `orchestrator.capability.REASON_CODES` on every
        path, and emits one invocation trace line regardless of outcome."""
        start = time.monotonic()

        if not isinstance(arguments, Mapping):
            record = self._record(capability_id, "refused", "invalid-arguments", start, {}, policy_checkpoint="absent")
            self._trace(record)
            return {"reason_code": "invalid-arguments", "error": "arguments must be a JSON object"}

        descriptor = self.descriptors_by_id.get(capability_id)
        if descriptor is None:
            record = self._record(
                capability_id, "refused", "not-eligible", start, arguments, policy_checkpoint="absent",
            )
            self._trace(record)
            return {"reason_code": "not-eligible", "error": f"{capability_id!r} is not eligible for this execution"}

        policy_status = "absent"
        if descriptor.consequence in _CHECKPOINT_CONSEQUENCES:
            verdict = self.checkpoint.check(descriptor, self.capability_set.execution)
            policy_status = policy_checkpoint_status(self.checkpoint, verdict)
            if verdict.decision != "allowed":
                record = self._record(
                    capability_id, "refused", "policy-denied", start, arguments, policy_checkpoint=policy_status,
                )
                self._trace(record)
                return {"reason_code": "policy-denied", "error": verdict.reason}

        dispatch = self.dispatch_by_id.get(capability_id)
        if dispatch is None:
            # Membership in descriptors_by_id and dispatch_by_id is
            # established together, in _discover — reachable only if a
            # caller hand-builds a CapabilityGateway inconsistently.
            record = self._record(
                capability_id, "error", "provider-error", start, arguments, policy_checkpoint=policy_status,
            )
            self._trace(record)
            return {"reason_code": "provider-error", "error": f"{capability_id!r} has no dispatch target"}
        provider, bare_tool_name = dispatch

        try:
            if provider.type == "mcp-remote":
                connector = self.connector if self.connector is not None else _default_remote_connector
                invocation_headers = {
                    **self.headers,
                    **_correlation_headers(self.capability_set.execution, self.capability_set.capability_set_id),
                }
                async with connector(provider, invocation_headers) as session:
                    call_result = await session.call_tool(bare_tool_name, arguments)
            else:
                local = self.local_sessions.get(provider.id)
                if local is None:
                    raise GatewayError(f"provider {provider.id!r}: no local session registered")
                call_result = await local.call_tool(bare_tool_name, arguments)
        except ProviderTimeoutError as exc:
            record = self._record(
                capability_id, "error", "timeout", start, arguments, policy_checkpoint=policy_status,
            )
            self._trace(record)
            return {"reason_code": "timeout", "error": str(exc)}
        except ProviderUnavailableError as exc:
            record = self._record(
                capability_id, "error", "provider-unavailable", start, arguments, policy_checkpoint=policy_status,
            )
            self._trace(record)
            return {"reason_code": "provider-unavailable", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — any other dispatch failure is provider-error
            record = self._record(
                capability_id, "error", "provider-error", start, arguments, policy_checkpoint=policy_status,
            )
            self._trace(record)
            return {"reason_code": "provider-error", "error": str(exc)}

        result_hash = hash_bytes(canonical_json({"text": call_result.text}))
        outcome = "error" if call_result.is_error else "ok"
        reason_code = "provider-error" if call_result.is_error else "ok"
        record = self._record(
            capability_id, outcome, reason_code, start, arguments,
            policy_checkpoint=policy_status, result_hash=result_hash,
        )
        self._trace(record)
        return {"reason_code": reason_code, "text": call_result.text}

    def _record(
        self,
        capability_id: str,
        outcome: str,
        reason_code: str,
        start: float,
        arguments: Mapping[str, Any],
        *,
        policy_checkpoint: str,
        result_hash: str | None = None,
    ) -> InvocationRecord:
        duration_ms = max(0, round((time.monotonic() - start) * 1000))
        arguments_hash = hash_bytes(canonical_json(dict(arguments)))
        return InvocationRecord(
            capability_id=capability_id,
            capability_set_id=self.capability_set.capability_set_id,
            outcome=outcome,
            reason_code=reason_code,
            duration_ms=duration_ms,
            policy_checkpoint=policy_checkpoint,
            arguments_hash=arguments_hash,
            result_hash=result_hash,
        )

    def _trace(self, record: InvocationRecord) -> None:
        _emit_trace(record.to_dict(), event="invocation")

    # -- SDK tool registration -----------------------------------------------

    def sdk_server(self) -> Any:
        """The `create_sdk_mcp_server` config exposing exactly the three
        gateway tools (ADR 017 sec. 5) — the only MCP server
        `build_issue_investigator_options_from_plan(..., gateway=...)`
        (task 8) connects into the model's tool set. Imports
        `claude_agent_sdk` lazily, inside this method, so constructing or
        testing a `CapabilityGateway` never requires the SDK to be
        importable — only actually serving it as an MCP server does."""
        import json

        from claude_agent_sdk import create_sdk_mcp_server
        from claude_agent_sdk import tool as sdk_tool

        def _as_tool_result(payload: Mapping[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}]}

        async def _search(args: dict[str, Any]) -> dict[str, Any]:
            return _as_tool_result(self.search(str(args.get("query") or ""), args.get("limit")))

        async def _describe(args: dict[str, Any]) -> dict[str, Any]:
            ids = args.get("capability_ids")
            return _as_tool_result(self.describe(ids if isinstance(ids, list) else []))

        async def _invoke(args: dict[str, Any]) -> dict[str, Any]:
            capability_id = str(args.get("capability_id") or "")
            arguments = args.get("arguments")
            result = await self.invoke(capability_id, arguments if arguments is not None else {})
            return _as_tool_result(result)

        tools = [
            sdk_tool(
                "capability_search", "Search the capabilities eligible for this execution.",
                {"query": str, "limit": int},
            )(_search),
            sdk_tool(
                "capability_describe", "Return full input schemas for eligible capability ids.",
                {"capability_ids": list},
            )(_describe),
            sdk_tool(
                "capability_invoke", "Invoke one eligible capability.",
                {"capability_id": str, "arguments": dict},
            )(_invoke),
        ]
        return create_sdk_mcp_server(name="capability", tools=tools)
