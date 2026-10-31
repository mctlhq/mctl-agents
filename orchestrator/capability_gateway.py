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

Provider types in this slice: `mcp-remote` providers are connected through
the connector; `mcp-local` and `sdk-builtin` providers must be registered in
`local_sessions` by `provider.id`. There is no built-in listing of the SDK's
own tools yet, so an `sdk-builtin` provider without a registered session is
a `GatewayError`, not an empty provider.

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
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from orchestrator.capability import (
    MAX_ANNOTATIONS_JSON_LENGTH,
    MAX_KEYWORD_LENGTH,
    MAX_KEYWORDS,
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
    CapabilityDescriptor,
    CapabilitySet,
    CapabilityStrategy,
    CheckpointVerdict,
    CommerceSummary,
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
from orchestrator.context_snapshot import ContextSnapshotError, canonical_json, hash_bytes
from orchestrator.resolver import ExecutionPlan

if TYPE_CHECKING:
    from orchestrator.commerce_gateway import CommerceRuntime

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
    #: Set by `_McpProviderSession.list_tools()` when this tool's raw
    #: `ToolAnnotations` was present but could not be coerced to a dict
    #: (`_annotations_to_dict` returned `None`) — a per-TOOL flag, not a
    #: per-session count (#509 review follow-up), so `_discover` can fold it
    #: into `annotations_dropped` AFTER the eligibility `continue`s, sharing
    #: one denominator with the oversized-annotations half of that count
    #: instead of counting excluded tools' drops that half never sees.
    annotations_unparseable: bool = False
    #: MCP `Tool.execution.taskSupport` (`forbidden`, `optional`,
    #: `required`), empty when the provider does not say. A paid capability
    #: whose tool REQUIRES task-based execution is refused with
    #: `unsupported-execution` (ADR 020 sec. 7): the milestone has no paid
    #: long-running execution.
    task_support: str = ""


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
    import httpx2
    import mcp
    from mcp.client.streamable_http import streamable_http_client

    # Only a failure BEFORE the session is handed out is a connection
    # failure. An exception raised inside the caller's `async with` body is
    # thrown into this generator at `yield`; it must propagate unchanged,
    # or every call_tool timeout/error would be relabelled
    # provider-unavailable.
    connected = False
    try:
        # mcp 2.x takes headers and timeouts on the HTTP client instead of on
        # the transport. These are the values mcp 1.x's
        # `streamablehttp_client` applied: 30 s to connect, write and wait
        # for a pooled connection, 300 s to read, since a response stream
        # may be held open. `max_sse_event_size=None` keeps 1.x's unbounded
        # SSE events; 2.x would otherwise fail any single event over 1 MiB,
        # turning a large but well-formed tool result into provider-error.
        async with httpx2.AsyncClient(
            headers=dict(headers), timeout=httpx2.Timeout(30.0, read=300.0),
        ) as http_client:
            async with streamable_http_client(
                provider.endpoint_ref, http_client=http_client, max_sse_event_size=None,
            ) as (read_stream, write_stream):
                async with mcp.ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    connected = True
                    yield _McpProviderSession(provider, session)
    except TimeoutError as exc:
        if connected:
            raise
        raise ProviderTimeoutError(f"provider {provider.id!r}: timed out connecting: {exc}") from exc
    except Exception as exc:
        if connected:
            raise
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
        tools: list[ProviderTool] = []
        for t in result.tools:
            converted = _annotations_to_dict(getattr(t, "annotations", None))
            tools.append(ProviderTool(
                name=t.name,
                description=t.description or "",
                input_schema=t.input_schema or {},
                annotations=converted or {},
                # Only a genuine coercion failure counts here. An empty
                # `annotations: {}` (or every hint unset) converts to {} and
                # is not a drop — see ProviderTool.annotations_unparseable.
                annotations_unparseable=converted is None,
                task_support=_task_support(t),
            ))
        return tools

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolCallResult:
        try:
            result = await self.session.call_tool(tool, dict(arguments))
        except TimeoutError as exc:
            raise ProviderTimeoutError(f"provider {self.provider.id!r}: call_tool timed out: {exc}") from exc
        except Exception as exc:
            raise GatewayError(f"provider {self.provider.id!r}: call_tool failed: {exc}") from exc
        text = "\n".join(str(getattr(block, "text", "")) for block in (result.content or []))
        return ToolCallResult(is_error=bool(result.is_error), text=text)


def _task_support(tool: Any) -> str:
    execution = getattr(tool, "execution", None)
    value = getattr(execution, "task_support", None) if execution is not None else None
    return value if isinstance(value, str) else ""


def _annotations_to_dict(annotations: Any) -> dict[str, Any] | None:
    """MCP `ToolAnnotations` -> a plain dict, advisory-only (ADR 017
    sec. 8), tolerant of every shape a provider might actually hand back.
    Leaf values are coerced to JSON (`default=str`) so a `datetime`/`Enum`/
    URL from `model_dump()` cannot fail sealing.

    `{}` means "nothing there" (absent, or present with every hint unset);
    `None` means "present but could not be coerced", which is the only case
    counted as a drop."""
    import json

    if annotations is None:
        return {}
    if hasattr(annotations, "model_dump"):
        # `by_alias`: mcp 2.x models name their fields in snake_case and keep
        # the wire's camelCase only as aliases. The keys sealed into a
        # descriptor stay the protocol's own (`readOnlyHint`, ...), as on 1.x.
        raw: Any = annotations.model_dump(by_alias=True, exclude_none=True)
    elif isinstance(annotations, Mapping):
        raw = dict(annotations)
    else:
        return None
    try:
        coerced = json.loads(json.dumps(raw, default=str))
    except (TypeError, ValueError):
        return None
    return coerced if isinstance(coerced, dict) else None


def _bounded_annotations(annotations: Mapping[str, Any]) -> dict[str, Any]:
    """Annotations are advisory-only (ADR 017 sec. 8), so an oversized or
    unserializable blob degrades ITS OWN descriptor to no annotations
    instead of failing `CapabilityDescriptor` validation and with it the
    whole discovery for every provider."""
    candidate = dict(annotations)
    try:
        size = len(canonical_json(candidate))
    except ContextSnapshotError:
        return {}
    return candidate if size <= MAX_ANNOTATIONS_JSON_LENGTH else {}


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
    for the CLI's own mctl connection: `Authorization: Bearer` with the
    token `run_token.mcp_auth()` picks (the run token in enforce mode,
    otherwise `$MCTL_TOKEN`, with the shadow scope header in observe mode)
    plus the `#196` correlation headers `_execution_context_headers()`
    computes (fails closed under `MCTL_REQUIRE_EXECUTION_CONTEXT`, inherited
    rather than reimplemented). Reused verbatim so the gateway's own
    connection carries the exact identity the CLI's direct connection
    would have."""
    import os

    from orchestrator import run_token
    from orchestrator.options import _execution_context_headers

    headers = dict(_execution_context_headers())
    # The same credential choice as mctl_mcp_config(): a run-token enforce
    # run reaches mctl through the gateway with its run token, never with
    # the static one (mctlhq/mctl-agents#717).
    token, scope_headers = run_token.mcp_auth(os.environ.get("MCTL_TOKEN", "").strip())
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers.update(scope_headers)
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


def _usable_tool_name(name: Any) -> bool:
    """A bare tool name `capability_id` can carry: non-empty, no `/`.
    MCP does not forbid namespaced names like `github/create_issue`, but
    ADR 017 sec. 4's `mctl://<type>/<id>/<tool>` cannot hold one, and one
    such name must not fail discovery for every provider."""
    return isinstance(name, str) and bool(name) and "/" not in name


def _reject_unusable_provider_ref(provider: ProviderRef) -> None:
    """A `provider.type` or `provider.id` carrying `/` would corrupt every
    capability_id this provider mints (`mctl://<type>/<id>/<tool>`;
    `_parse_capability_id` splits on exactly three `/`-delimited segments).
    Unlike a single bad tool name (`_usable_tool_name`, per-tool and merely
    excluded), a bad provider identity poisons the whole provider, so it
    fails closed at discovery time rather than being silently skipped."""
    if "/" in provider.type:
        raise GatewayError(
            f"provider.type {provider.type!r} must not contain '/' — it appears verbatim in every "
            "capability_id this provider mints (mctl://<provider_type>/<provider_id>/<tool>)"
        )
    if "/" in provider.id:
        raise GatewayError(
            f"provider.id {provider.id!r} must not contain '/' — it appears verbatim in every "
            "capability_id this provider mints (mctl://<provider_type>/<provider_id>/<tool>)"
        )


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _discover(
    plan: ExecutionPlan,
    correlation: ExecutionCorrelation,
    providers: Sequence[ProviderRef],
    *,
    local_sessions: Mapping[str, ProviderSession],
    connector: ProviderConnector | None,
    headers: Mapping[str, str] | None,
    consequence_table: Mapping[str, str] | None,
    commerce: CommerceRuntime | None = None,
    task_support_out: dict[str, str] | None = None,
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

    With a `commerce` runtime, each eligible capability's current terms are
    read from mctl-api (ADR 020 sec. 12): terms in force become the
    descriptor's `commerce` summary, mctl-api's typed "no terms" answer
    leaves it legacy, and any other answer fails discovery, for the same
    reason a provider failure does. Without one, every capability is legacy.
    """
    resolved_connector = connector if connector is not None else _default_remote_connector
    resolved_headers = dict(headers) if headers is not None else _default_remote_headers()
    # The listing round trip carries the same #196 correlation fields an
    # invocation does, minus the capability_set_id that cannot exist before
    # the set is sealed (`_correlation_headers` drops empty values).
    discovery_headers = {**resolved_headers, **_correlation_headers(correlation, "")}
    table = consequence_table if consequence_table is not None else load_consequence_table()

    descriptors: list[CapabilityDescriptor] = []
    schemas_by_id: dict[str, Mapping[str, Any]] = {}
    dispatch_by_id: dict[str, tuple[ProviderRef, str]] = {}
    excluded_count = 0
    # (descriptor index, capability_id, schema_hash, provider) for every
    # matched tool whose terms are read once all providers are listed.
    terms_reads: list[tuple[int, str, str, ProviderRef]] = []

    for provider in providers:
        _reject_unusable_provider_ref(provider)
        if provider.type == "mcp-remote":
            session_cm = resolved_connector(provider, discovery_headers)
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

        unusable_names = 0
        # Both halves of this count are folded in AFTER the eligibility
        # `continue`s below (#509 review follow-up), so they share one
        # denominator: a tool excluded by `plan.tools` was never eligible to
        # begin with, and counting its annotation drop here would inflate
        # `annotations_dropped` relative to what the sealed set actually
        # holds.
        annotations_dropped = 0
        for info in tools:
            if not _usable_tool_name(info.name):
                unusable_names += 1
                continue
            sdk_name = _sdk_visible_name(provider, info.name)
            matched_pattern = _first_matching_pattern(sdk_name, plan.tools)
            if matched_pattern is None:
                excluded_count += 1
                continue
            capability_id = f"mctl://{provider.type}/{provider.id}/{info.name}"
            schema_bytes = canonical_json(dict(info.input_schema))
            schema_hash = hash_bytes(schema_bytes)
            annotations = _bounded_annotations(info.annotations)
            # Either a coercion failure at the session (ProviderTool.
            # annotations_unparseable, set per-tool by
            # `_McpProviderSession.list_tools()`) or an oversized/
            # unserializable blob dropped here by `_bounded_annotations`.
            if info.annotations_unparseable or (info.annotations and not annotations):
                annotations_dropped += 1
            descriptor = CapabilityDescriptor(
                capability_id=capability_id,
                tool_name=sdk_name,
                provider=provider,
                title=_bounded_title(info.name),
                summary=_one_line(info.description),
                keywords=_derive_keywords(info.name),
                input_schema_hash=schema_hash,
                input_schema_bytes=len(schema_bytes),
                consequence=classify_consequence(info.name, table, provider_id=provider.id),
                matched_tool_pattern=matched_pattern,
                annotations=annotations,
            )
            if commerce is not None:
                terms_reads.append((len(descriptors), capability_id, schema_hash, provider))
            descriptors.append(descriptor)
            if task_support_out is not None:
                task_support_out[capability_id] = info.task_support
            schemas_by_id[capability_id] = info.input_schema
            dispatch_by_id[capability_id] = (provider, info.name)

        # Degradation is visible, never silent: counts and the provider id
        # only, no tool names and no annotation content.
        if unusable_names or annotations_dropped:
            _emit_trace({
                "provider_id": provider.id,
                "unusable_tool_names": unusable_names,
                "annotations_dropped": annotations_dropped,
            }, event="provider_degraded")

    if commerce is not None and terms_reads:
        summaries = await _commerce_summaries(commerce, terms_reads)
        for (index, _, _, _), summary in zip(terms_reads, summaries, strict=True):
            if summary is not None:
                descriptors[index] = replace(descriptors[index], commerce=summary)

    return descriptors, excluded_count, schemas_by_id, dispatch_by_id


#: How many terms reads discovery keeps in flight at once.
_TERMS_READ_CONCURRENCY = 8


async def _commerce_summaries(
    commerce: CommerceRuntime, reads: Sequence[tuple[int, str, str, ProviderRef]],
) -> list[CommerceSummary | None]:
    """Every matched tool's terms, read concurrently (at most
    `_TERMS_READ_CONCURRENCY` at a time) so discovery waits about one
    client timeout, not one per matched tool. Every read is awaited before
    anything is raised, and the failure raised is the first in discovery
    order, so which capability fails discovery does not depend on timing."""
    import asyncio

    gate = asyncio.Semaphore(_TERMS_READ_CONCURRENCY)

    async def one(capability_id: str, schema_hash: str, provider: ProviderRef) -> CommerceSummary | None:
        async with gate:
            return await _commerce_summary(commerce, capability_id, schema_hash, provider)

    answers = await asyncio.gather(*(one(c, h, p) for _, c, h, p in reads), return_exceptions=True)
    for answer in answers:
        if isinstance(answer, BaseException):
            raise answer
    return list(answers)  # type: ignore[arg-type]


async def _commerce_summary(
    commerce: CommerceRuntime, capability_id: str, schema_hash: str, provider: ProviderRef,
) -> CommerceSummary | None:
    """The descriptor's `commerce` field: a read-only summary of the terms
    in force. `None` only on mctl-api's typed `commerce-terms-unknown`.
    One mctl-api read; `_commerce_summaries` runs them concurrently."""
    import asyncio

    answer = await asyncio.to_thread(commerce.client.current_terms, capability_id, schema_hash)
    if not answer.ok:
        from orchestrator import commerce as cm

        if answer.status == cm.REFUSED and answer.code == "commerce-terms-unknown":
            return None
        raise GatewayError(
            f"capability {capability_id!r}: commerce terms could not be read ({answer.reason or answer.status})"
        )
    doc: Any = answer.body
    if (doc.get("capability_id") != capability_id or doc.get("input_schema_hash") != schema_hash
            or doc.get("provider_id") != provider.id):
        raise GatewayError(f"capability {capability_id!r}: the terms in force name another capability version")
    price: Any = doc.get("price") if isinstance(doc.get("price"), Mapping) else {}
    try:
        return CommerceSummary(
            terms_id=doc.get("terms_id"), terms_version=doc.get("terms_version"),
            price_mode=doc.get("price_mode"), currency=price.get("currency"),
            amount_minor=price.get("amount_minor"), funds_mode=doc.get("funds_mode"),
            meter=doc.get("meter") or "invocation",
        )
    except Exception as exc:
        raise GatewayError(f"capability {capability_id!r}: the terms in force are malformed: {exc}") from exc


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
    commerce: CommerceRuntime | None = None,
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
        plan, correlation, providers,
        local_sessions=local_sessions, connector=connector, headers=headers, consequence_table=consequence_table,
        commerce=commerce,
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


def _commerce_view(summary: Any) -> dict[str, Any]:
    """The sealed `commerce` summary plus a display price. Display only
    (ADR 020 sec. 2): `price.amount_minor` is the integer that counts. Taken
    from the sealed descriptor, never from mctl-api."""
    from orchestrator.commerce import price_display

    view = summary.to_dict()
    view["price_display"] = price_display(summary.currency, summary.amount_minor)
    return view


def _commerce_search_view(summary: Any) -> dict[str, Any]:
    full = _commerce_view(summary)
    return {key: full[key] for key in (
        "price_mode", "price", "price_display", "funds_mode", "meter", "quote_required", "billable_on",
        "terms_id", "terms_version",
    )}


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
    #: The policy `decide()` evaluates; None is `BUILTIN_POLICY`. The paid
    #: path (ADR 020 sec. 8) evaluates the same policy, over the call's
    #: arguments plus the terms binding.
    policy: Any = None

    def resolved_policy(self) -> Any:
        from orchestrator import policy_checkpoint

        return self.policy if self.policy is not None else policy_checkpoint.BUILTIN_POLICY

    def check(self, descriptor: CapabilityDescriptor, correlation: ExecutionCorrelation) -> CheckpointVerdict:
        from orchestrator import policy_checkpoint

        decision = policy_checkpoint.checkpoint(
            policy_checkpoint.MCP_TOOL_CALL,
            descriptor.tool_name,
            descriptor.provider.alias,
            {},
            grants=self.grants,
            metadata={"capability_id": descriptor.capability_id, "agent": correlation.agent},
            policy=self.resolved_policy(),
        )
        return CheckpointVerdict(
            decision="allowed" if decision.permitted else "denied",
            reason=decision.reason,
        )


# ---------------------------------------------------------------------------
# CapabilityGateway — serves capability_search/describe/invoke over one
# sealed CapabilitySet.
# ---------------------------------------------------------------------------

#: Bound on a model-invented id carried into a `not-eligible` trace line.
#: Eligible ids come from the sealed set and are never truncated.
_MAX_TRACED_ID_LENGTH = 256

#: Bound on a raising PolicyCheckpoint's message in its
#: `CAPABILITY_POLICY_CHECKPOINT_FAILED` trace line.
_MAX_CHECKPOINT_ERROR_LENGTH = 300

#: A caller's own idempotency key for `capability_invoke` (ADR 020 sec. 6).
#: The gateway derives the ledger key from it, so it never reaches mctl-api.
_CLIENT_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

#: What the model is told when a dispatch fails. Provider/transport exception
#: text can carry URLs or upstream response fragments, so it never reaches the
#: model; the reason code is the whole answer, and the trace line records it.
_FIXED_ERRORS = MappingProxyType({
    "timeout": "the provider did not answer in time",
    "provider-unavailable": "the provider could not be reached",
    "provider-error": "the provider failed to execute this capability",
})


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
    # Required, never defaulted: on the gateway path this is the only #197
    # enforcement for the capabilities behind capability_invoke, so a caller
    # must choose it explicitly (AbsentPolicyCheckpoint included).
    checkpoint: PolicyCheckpoint
    descriptors_by_id: Mapping[str, CapabilityDescriptor] = field(default_factory=dict)
    schemas_by_id: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    dispatch_by_id: Mapping[str, tuple[ProviderRef, str]] = field(default_factory=dict)
    local_sessions: Mapping[str, ProviderSession] = field(default_factory=dict)
    connector: ProviderConnector | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    #: ADR 020: None serves every capability as legacy and refuses a paid
    #: descriptor with `commerce-terms-unknown`.
    commerce: CommerceRuntime | None = None
    task_support_by_id: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    async def build(
        cls,
        plan: ExecutionPlan,
        correlation: ExecutionCorrelation,
        providers: Sequence[ProviderRef],
        *,
        checkpoint: PolicyCheckpoint,
        local_sessions: Mapping[str, ProviderSession] = MappingProxyType({}),
        connector: ProviderConnector | None = None,
        headers: Mapping[str, str] | None = None,
        consequence_table: Mapping[str, str] | None = None,
        strategy: CapabilityStrategy | None = None,
        retention: RetentionPolicy | None = None,
        created_at: str | None = None,
        commerce: CommerceRuntime | None = None,
    ) -> CapabilityGateway:
        """Discover, seal, and emit the set-sealing trace line — one
        provider round trip, shared with the private schema/dispatch
        caches this gateway serves `capability_describe`/`capability_invoke`
        from."""
        resolved_headers = dict(headers) if headers is not None else _default_remote_headers()
        task_support: dict[str, str] = {}
        descriptors, excluded_count, schemas_by_id, dispatch_by_id = await _discover(
            plan, correlation, providers,
            local_sessions=local_sessions, connector=connector, headers=resolved_headers,
            consequence_table=consequence_table, commerce=commerce, task_support_out=task_support,
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
            checkpoint=checkpoint,
            descriptors_by_id={d.capability_id: d for d in capability_set.capabilities},
            schemas_by_id=schemas_by_id,
            dispatch_by_id=dispatch_by_id,
            local_sessions=dict(local_sessions),
            connector=connector,
            headers=resolved_headers,
            commerce=commerce,
            task_support_by_id=task_support,
        )

    # -- capability_search --------------------------------------------------

    def search(self, query: Any = "", limit: Any = None) -> dict[str, Any]:
        """Compact rows only — `capability_id`, `title`, one-line `summary`,
        `consequence` — drawn only from the sealed set. Never a schema
        (ADR 017 sec. 5). Arguments come from the model, so they are
        validated here and a bad one is `invalid-arguments`, never an
        exception."""
        if query is None:
            query = ""
        if not isinstance(query, str):
            return {"results": [], "reason_code": "invalid-arguments", "error": "query must be a string"}
        if limit is None:
            row_limit = DEFAULT_SEARCH_LIMIT
        elif isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            return {"results": [], "reason_code": "invalid-arguments", "error": "limit must be a positive integer"}
        else:
            row_limit = min(limit, MAX_SEARCH_LIMIT)
        needle = query.strip().lower()
        rows: list[dict[str, Any]] = []
        for descriptor in self.capability_set.capabilities:
            haystack = " ".join((descriptor.title, descriptor.summary, *descriptor.keywords)).lower()
            if needle and needle not in haystack:
                continue
            row: dict[str, Any] = {
                "capability_id": descriptor.capability_id,
                "title": descriptor.title,
                "summary": descriptor.summary,
                "consequence": descriptor.consequence,
            }
            summary = self._offered_commerce(descriptor)
            if summary is not None:
                row["commerce"] = _commerce_search_view(summary)
            rows.append(row)
        return {"results": rows[:row_limit], "reason_code": "ok"}

    def _paid_unavailable(self, capability_id: str) -> tuple[str, str] | None:
        """Why this execution cannot charge `capability_id`, or None when it
        can. Discovery and `capability_invoke` share it, so a price is shown
        only where a paid call could actually be made."""
        if self.commerce is None:
            return "commerce-terms-unknown", "paid capabilities are not available in this execution"
        from orchestrator import commerce_gateway as cg

        if not isinstance(self.checkpoint, PolicyDecidePolicyCheckpoint):
            return "policy-denied", "a paid capability requires the policy checkpoint"
        dispatch = self.dispatch_by_id.get(capability_id)
        if dispatch is None:
            return "provider-error", f"{capability_id!r} has no dispatch target"
        provider, _ = dispatch
        session = None if provider.type == "mcp-remote" else self.local_sessions.get(provider.id)
        if (provider.id not in self.commerce.admitted_providers or session is None
                or not isinstance(session, cg.PaidProviderSession)):
            return "unsupported-execution", "this provider is not admitted for paid execution"
        if self.task_support_by_id.get(capability_id) == "required":
            return "unsupported-execution", "paid long-running execution is not supported"
        return None

    def _offered_commerce(self, descriptor: CapabilityDescriptor) -> CommerceSummary | None:
        """The summary discovery may show: None for a legacy descriptor and
        for a paid one this execution could not charge. A free summary is
        shown even when the paid path is unavailable (provider not admitted,
        no policy adapter): it is advisory. A free descriptor is invoked down
        the legacy path, so whether that call succeeds depends on the
        provider session; a provider behind the ingress fence refuses an
        unsigned legacy dispatch (`provider-error`)."""
        if descriptor.commerce is None:
            return None
        if descriptor.commerce.price_mode == "free":
            return descriptor.commerce
        if self._paid_unavailable(descriptor.capability_id) is not None:
            return None
        return descriptor.commerce

    def _approval_expected(self, descriptor: CapabilityDescriptor) -> str:
        """Advisory (mctlhq/mctl-agents#620): `required` | `not_required` |
        `unknown`, from the configured policy's rule for this tool. Reads no
        arguments and contacts nothing; `capability_invoke` re-decides."""
        from orchestrator import policy_checkpoint

        if not isinstance(self.checkpoint, PolicyDecidePolicyCheckpoint):
            return policy_checkpoint.EXPECTED_UNKNOWN
        try:
            return policy_checkpoint.expected_verdict(
                self.checkpoint.resolved_policy(), policy_checkpoint.MCP_TOOL_CALL,
                descriptor.tool_name, self.checkpoint.grants,
            )
        except Exception:  # noqa: BLE001 — a hint must never fail discovery
            return policy_checkpoint.EXPECTED_UNKNOWN

    # -- capability_describe -------------------------------------------------

    def describe(self, capability_ids: Any) -> dict[str, Any]:
        """Full input schemas for at most `MAX_DESCRIBE_IDS` eligible ids.
        An id outside the sealed set answers `not-found` — indistinguishable
        from a genuinely nonexistent id, so this leaks nothing about what
        was withheld (ADR 017 sec. 5). Model-supplied input: anything but a
        list of strings is `invalid-arguments`, never an exception."""
        if not isinstance(capability_ids, (list, tuple)) or not all(
            isinstance(capability_id, str) for capability_id in capability_ids
        ):
            return {
                "results": {},
                "reason_code": "invalid-arguments",
                "error": "capability_ids must be a list of strings",
            }
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
            entry: dict[str, Any] = {
                "reason_code": "ok",
                "title": descriptor.title,
                "summary": descriptor.summary,
                "consequence": descriptor.consequence,
                "input_schema": dict(self.schemas_by_id.get(capability_id, {})),
            }
            summary = self._offered_commerce(descriptor)
            if summary is not None:
                entry["commerce"] = _commerce_view(summary)
                entry["approval_expected"] = self._approval_expected(descriptor)
            results[capability_id] = entry
        return {"results": results, "reason_code": "ok"}

    # -- capability_invoke ---------------------------------------------------

    async def invoke(self, capability_id: Any, arguments: Any, idempotency_key: Any = None) -> dict[str, Any]:
        """Membership check against the sealed set, then the
        `PolicyCheckpoint` (every capability, whatever its consequence tier
        — ADR 017 sec. 8, option B), then dispatch — remote via the provider
        connector, local in process, with zero network hop (ADR 017 sec. 5).
        Returns exactly one reason code from
        `orchestrator.capability.REASON_CODES` on every path, and emits one
        invocation trace line regardless of outcome.

        A capability whose descriptor carries a paid `commerce` summary goes
        through `orchestrator.commerce_gateway.PaidInvocation` instead (ADR
        020 secs. 7-9). `idempotency_key` matters only there: the same call
        with the same arguments in the same execution is one invocation, and
        a caller that really wants a second one passes a new key."""
        start = time.monotonic()

        if not isinstance(capability_id, str):
            # A fixed placeholder: the trace never carries model-supplied
            # content (the no-payload rule above).
            record = self._record("<non-string>", "refused", "invalid-arguments", start, {},
                                  policy_checkpoint="absent")
            self._trace(record)
            return {"reason_code": "invalid-arguments", "error": "capability_id must be a string"}

        if not isinstance(arguments, Mapping):
            record = self._record(
                capability_id[:_MAX_TRACED_ID_LENGTH], "refused", "invalid-arguments", start, {},
                policy_checkpoint="absent",
            )
            self._trace(record)
            return {"reason_code": "invalid-arguments", "error": "arguments must be a JSON object"}

        descriptor = self.descriptors_by_id.get(capability_id)
        if descriptor is None:
            record = self._record(
                capability_id[:_MAX_TRACED_ID_LENGTH], "refused", "not-eligible", start, arguments,
                policy_checkpoint="absent",
            )
            self._trace(record)
            return {"reason_code": "not-eligible", "error": f"{capability_id!r} is not eligible for this execution"}

        if idempotency_key is not None and not (
            isinstance(idempotency_key, str) and _CLIENT_IDEMPOTENCY_KEY.match(idempotency_key)
        ):
            record = self._record(capability_id, "refused", "invalid-arguments", start, arguments,
                                  policy_checkpoint="absent")
            self._trace(record)
            return {"reason_code": "invalid-arguments", "error": "idempotency_key must be 1..64 of [A-Za-z0-9._:-]"}

        if descriptor.commerce is not None and descriptor.commerce.price_mode != "free":
            return await self._invoke_paid(descriptor, arguments, idempotency_key or "", start)

        # ADR 017 sec. 8, option B (dated owner decision, 2026-09-26): every
        # capability goes through the PolicyCheckpoint before dispatch,
        # whatever its consequence tier. BUILTIN_POLICY still allows reads,
        # so behaviour is unchanged for read-only tools; what changes is that
        # the decision — ALLOW included — is now recorded, matching the
        # direct mcp__mctl__* path's audit trail (_PolicyCheckpointHook
        # sends every call through the checkpoint, not only mutating/
        # consequential ones).
        # #509 review follow-up (design.md "5."): a `PolicyCheckpoint`
        # implementation that raises must fail closed, exactly like every
        # other refusal path here — never let an exception fall through to
        # dispatch, which would turn a broken checkpoint into an unchecked
        # capability_invoke.
        try:
            verdict = self.checkpoint.check(descriptor, self.capability_set.execution)
            policy_status = policy_checkpoint_status(self.checkpoint, verdict)
        except Exception as exc:  # noqa: BLE001 - any checkpoint failure is a fail-closed deny
            # The record alone would read exactly like a real deny verdict, so
            # the cause goes out on its own trace line: a checkpoint backend
            # outage must not look like policy working as intended. The status
            # still goes through policy_checkpoint_status, so an
            # AbsentPolicyCheckpoint that raised never claims a decision.
            _emit_trace({
                "capability_id": capability_id,
                "checkpoint": type(self.checkpoint).__name__,
                "error_type": type(exc).__name__,
                "error": str(exc)[:_MAX_CHECKPOINT_ERROR_LENGTH],
            }, event="policy_checkpoint_failed")
            failed_status = policy_checkpoint_status(
                self.checkpoint, CheckpointVerdict(decision="denied", reason="the policy checkpoint failed"),
            )
            record = self._record(
                capability_id, "refused", "policy-denied", start, arguments, policy_checkpoint=failed_status,
            )
            self._trace(record)
            return {"reason_code": "policy-denied", "error": "the policy checkpoint failed"}

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
        except ProviderTimeoutError:
            record = self._record(
                capability_id, "error", "timeout", start, arguments, policy_checkpoint=policy_status,
            )
            self._trace(record)
            return {"reason_code": "timeout", "error": _FIXED_ERRORS["timeout"]}
        except ProviderUnavailableError:
            record = self._record(
                capability_id, "error", "provider-unavailable", start, arguments, policy_checkpoint=policy_status,
            )
            self._trace(record)
            return {"reason_code": "provider-unavailable", "error": _FIXED_ERRORS["provider-unavailable"]}
        except Exception:  # noqa: BLE001 — any other dispatch failure is provider-error
            record = self._record(
                capability_id, "error", "provider-error", start, arguments, policy_checkpoint=policy_status,
            )
            self._trace(record)
            return {"reason_code": "provider-error", "error": _FIXED_ERRORS["provider-error"]}

        result_hash = hash_bytes(canonical_json({"text": call_result.text}))
        outcome = "error" if call_result.is_error else "ok"
        reason_code = "provider-error" if call_result.is_error else "ok"
        record = self._record(
            capability_id, outcome, reason_code, start, arguments,
            policy_checkpoint=policy_status, result_hash=result_hash,
        )
        self._trace(record)
        return {"reason_code": reason_code, "text": call_result.text}

    async def _invoke_paid(
        self, descriptor: CapabilityDescriptor, arguments: Mapping[str, Any], client_key: str, start: float,
    ) -> dict[str, Any]:
        """ADR 020: the paid path. Everything that would make the call
        unsafe to charge is refused here, before any quote or ledger row
        exists."""
        from orchestrator import commerce_gateway as cg

        capability_id = descriptor.capability_id

        def refuse(code: str, error: str) -> dict[str, Any]:
            self._trace(self._record(capability_id, "refused", code, start, arguments, policy_checkpoint="absent"))
            return {"reason_code": code, "error": error}

        if not isinstance(arguments, Mapping):
            # `invoke` already refuses this before routing here; kept so the
            # paid path never depends on its caller for the `dict()` below.
            self._trace(self._record(capability_id, "refused", "invalid-arguments", start, {},
                                     policy_checkpoint="absent"))
            return {"reason_code": "invalid-arguments", "error": "arguments must be a JSON object"}
        unavailable = self._paid_unavailable(capability_id)
        if unavailable is not None:
            return refuse(*unavailable)
        runtime, checkpoint = self.commerce, self.checkpoint
        provider, bare_tool_name = self.dispatch_by_id[capability_id]
        session = self.local_sessions.get(provider.id)
        if (runtime is None or not isinstance(checkpoint, PolicyDecidePolicyCheckpoint)
                or not isinstance(session, cg.PaidProviderSession)):
            # Unreachable after `_paid_unavailable`; restated so the types narrow.
            return refuse("unsupported-execution", "this provider is not admitted for paid execution")

        paid = cg.PaidInvocation(
            runtime=runtime, descriptor=descriptor, session=session, tool=bare_tool_name,
            arguments=dict(arguments), grants=tuple(checkpoint.grants),
            policy=checkpoint.resolved_policy(), client_key=client_key,
            emit=lambda payload, event: _emit_trace(payload, event=event),
        )
        try:
            result = await paid.run()
        except Exception as exc:  # noqa: BLE001 — a bug here must not look like a definite answer
            _emit_trace({"capability_id": capability_id, "error_type": type(exc).__name__}, event="commerce_failed")
            # The checkpoint verdict reached before the failure, not a
            # blanket "denied": the audit must say what policy decided.
            result = cg.PaidResult("outcome-unknown", error="the paid call's state is unknown",
                                   policy_checkpoint=paid.policy_status())
        outcome = "ok" if result.reason_code == "ok" else (
            "error" if result.reason_code in ("provider-error", "outcome-unknown", "timeout") else "refused")
        result_hash = (
            hash_bytes(canonical_json({"text": result.result_hash_source}))
            if result.result_hash_source is not None else None
        )
        self._trace(self._record(capability_id, outcome, result.reason_code, start, arguments,
                                 policy_checkpoint=result.policy_checkpoint, result_hash=result_hash))
        return result.to_answer()

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

    def sdk_tools(self) -> list[Any]:
        """The three gateway tools as `claude_agent_sdk.SdkMcpTool`s, each
        with a full JSON schema. The SDK's `{"key": type}` shorthand marks
        every key required, which would make `capability_search` uncallable
        without a `limit`; the explicit schemas below require only what the
        handler actually needs. Handlers pass model input to `search`/
        `describe`/`invoke` unchanged, and those three validate it."""
        import json

        from claude_agent_sdk import tool as sdk_tool

        def _as_tool_result(payload: Mapping[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}]}

        async def _search(args: dict[str, Any]) -> dict[str, Any]:
            return _as_tool_result(self.search(args.get("query", ""), args.get("limit")))

        async def _describe(args: dict[str, Any]) -> dict[str, Any]:
            return _as_tool_result(self.describe(args.get("capability_ids")))

        async def _invoke(args: dict[str, Any]) -> dict[str, Any]:
            arguments = args.get("arguments")
            result = await self.invoke(args.get("capability_id"), arguments if arguments is not None else {},
                                       args.get("idempotency_key"))
            return _as_tool_result(result)

        return [
            sdk_tool(
                "capability_search",
                "Search the capabilities eligible for this execution. A paid capability's row carries a "
                "`commerce` block (price, mode, quote requirement). The price is advisory: "
                "capability_invoke re-checks identity, policy, quote and budget.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
                    },
                },
            )(_search),
            sdk_tool(
                "capability_describe",
                "Return full input schemas for eligible capability ids. A paid capability also shows its "
                "full `commerce` terms and an advisory `approval_expected` (required, not_required or "
                "unknown); capability_invoke re-checks identity, policy, quote and budget.",
                {
                    "type": "object",
                    "properties": {
                        "capability_ids": {
                            "type": "array", "items": {"type": "string"}, "maxItems": MAX_DESCRIBE_IDS,
                        },
                    },
                    "required": ["capability_ids"],
                },
            )(_describe),
            sdk_tool(
                "capability_invoke", "Invoke one eligible capability.",
                {
                    "type": "object",
                    "properties": {
                        "capability_id": {"type": "string"},
                        "arguments": {"type": "object"},
                        "idempotency_key": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{1,64}$"},
                    },
                    "required": ["capability_id"],
                },
            )(_invoke),
        ]

    def sdk_server(self) -> Any:
        """The `create_sdk_mcp_server` config exposing exactly the three
        gateway tools (ADR 017 sec. 5) — the only MCP server
        `build_issue_investigator_options_from_plan(..., gateway=...)`
        (task 8) connects into the model's tool set. Imports
        `claude_agent_sdk` lazily, inside this method, so constructing or
        testing a `CapabilityGateway` never requires the SDK to be
        importable — only actually serving it as an MCP server does."""
        from claude_agent_sdk import create_sdk_mcp_server

        return create_sdk_mcp_server(name="capability", tools=self.sdk_tools())
