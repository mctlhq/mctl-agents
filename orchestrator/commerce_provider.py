"""A paid capability provider's own side of ADR 020 sec. 9
(mctlhq/mctl-agents#619, C4).

`ProviderIngress` is the protected entry point a paid provider puts in front
of its tools. It is the payment boundary; a client-side wrapper is not
(sec. 9, requirement 1). It:

1. verifies the dispatch authorization mctl-api signed for exactly this call
   (pinned Ed25519 keys) and that it names this provider, this tool, this
   tool's own input schema, these exact arguments and, when the provider is
   bound to one, this tenant;
2. refuses a key whose `dispatch_deadline` has passed on its own clock,
   unless that key was already accepted (requirement 4);
3. durably records the acceptance BEFORE any side effect (requirement 3);
4. answers a second dispatch of the same provider operation key with the
   first one's outcome, never executing again (requirement 2);
5. answers `outcome(key)` with `succeeded`, `failed`, `in_progress` or
   `not_found` (requirement 5).

A plain `call_tool` with no authorization is refused, so a direct call, an
eager-mode call or an alternate adapter cannot reach a paid tool.

`PaidLocalProviderSession` serves an ingress as an execution-local
(`mcp-local`) provider. `pilot_lookup_status_ingress` builds the milestone's
read-only pilot capability (`lookup_status`), which is what C4's fault tests
drive and what C5 later exposes.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from orchestrator.capability_gateway import ProviderTool, ToolCallResult
from orchestrator.commerce import (
    DISPATCH_SIGNED_FIELDS,
    FAILED,
    IN_PROGRESS,
    NOT_FOUND,
    SUCCEEDED,
    AuthorizationRejected,
    DispatchAuthorization,
    DispatchRefused,
    OutcomeUnavailable,
    ProviderOutcome,
    args_digest,
    verify_dispatch_authorization,
)
from orchestrator.context_snapshot import canonical_json, hash_bytes

#: What a handler that raised is recorded as. Its exception text never
#: leaves the provider.
_HANDLER_FAILED_TEXT = "the capability failed"


Handler = Callable[[Mapping[str, Any]], Awaitable[ToolCallResult]]


@dataclass(frozen=True)
class PaidTool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Handler
    annotations: Mapping[str, Any] = field(default_factory=dict)
    #: MCP `Tool.execution.taskSupport`, listed as `ProviderTool.task_support`;
    #: the gateway refuses a paid call to a tool that requires it.
    task_support: str = ""

    @property
    def input_schema_hash(self) -> str:
        """The same digest `capability_gateway._discover` puts on the
        descriptor, so the ingress checks the schema the caller saw."""
        return hash_bytes(canonical_json(dict(self.input_schema)))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_operations (
    provider_operation_key TEXT PRIMARY KEY,
    invocation_id          TEXT NOT NULL,
    capability_id          TEXT NOT NULL,
    args_digest            TEXT NOT NULL,
    tenant_id              TEXT NOT NULL,
    state                  TEXT NOT NULL CHECK (state IN ('in_progress', 'succeeded', 'failed')),
    is_error               INTEGER NOT NULL DEFAULT 0,
    result_text            TEXT NOT NULL DEFAULT '',
    accepted_at            TEXT NOT NULL,
    completed_at           TEXT
)
"""


class ProviderIngress:
    """See the module docstring. `db_path` is the durable dedupe store; a
    restarted provider opened on the same path keeps every accepted key."""

    def __init__(
        self,
        *,
        provider_type: str,
        provider_id: str,
        public_keys: Mapping[str, bytes],
        tools: Mapping[str, PaidTool],
        db_path: str,
        tenant_id: str = "",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not public_keys:
            raise ValueError("a paid provider needs at least one pinned dispatch key")
        self.provider_type = provider_type
        self.provider_id = provider_id
        self.tenant_id = tenant_id
        self._keys = dict(public_keys)
        self._tools = dict(tools)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._running: set[asyncio.Future[ToolCallResult]] = set()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def tools(self) -> list[ProviderTool]:
        return [
            ProviderTool(name=t.name, description=t.description, input_schema=t.input_schema,
                         annotations=t.annotations, task_support=t.task_support)
            for t in self._tools.values()
        ]

    def capability_id(self, tool: str) -> str:
        return f"mctl://{self.provider_type}/{self.provider_id}/{tool}"

    # -- the protected entry point -------------------------------------------

    def _verify(self, tool: str, arguments: Mapping[str, Any], authorization: DispatchAuthorization) -> PaidTool:
        try:
            verify_dispatch_authorization(self._keys, authorization)
        except AuthorizationRejected as exc:
            raise DispatchRefused(f"authorization rejected: {exc}") from exc
        spec = self._tools.get(tool)
        if spec is None:
            raise DispatchRefused(f"unknown tool {tool!r}")
        if authorization.provider_id != self.provider_id:
            raise DispatchRefused("authorization names another provider")
        if authorization.capability_id != self.capability_id(tool):
            raise DispatchRefused("authorization names another capability")
        if authorization.input_schema_hash != spec.input_schema_hash:
            raise DispatchRefused("authorization names another input schema")
        try:
            presented = args_digest(arguments)
        except Exception as exc:
            raise DispatchRefused("arguments cannot be digested") from exc
        if authorization.args_digest != presented:
            raise DispatchRefused("authorization was signed for other arguments")
        if self.tenant_id and authorization.tenant_id != self.tenant_id:
            raise DispatchRefused("authorization names another tenant")
        return spec

    def _row(self, key: str) -> tuple[Any, ...] | None:
        cur = self._db.execute(
            "SELECT invocation_id, args_digest, tenant_id, state, is_error, result_text FROM paid_operations "
            "WHERE provider_operation_key = ?", (key,))
        return cur.fetchone()

    def _accept(self, authorization: DispatchAuthorization) -> tuple[Any, ...] | None:
        """Insert the acceptance record, or return the existing row for the
        key. The deadline is checked only for a key not yet accepted."""
        key = authorization.provider_operation_key
        with self._lock:
            row = self._row(key)
            if row is not None:
                return row
            if self._now() > authorization.deadline():
                raise DispatchRefused("dispatch deadline has passed")
            # ON CONFLICT keeps the dedupe correct across processes sharing
            # the store: the lock only serializes this process.
            inserted = self._db.execute(
                "INSERT INTO paid_operations (provider_operation_key, invocation_id, capability_id, args_digest, "
                "tenant_id, state, accepted_at) VALUES (?, ?, ?, ?, ?, 'in_progress', ?) "
                "ON CONFLICT(provider_operation_key) DO NOTHING",
                (key, authorization.invocation_id, authorization.capability_id, authorization.args_digest,
                 authorization.tenant_id, self._now().isoformat()))
            if inserted.rowcount == 0:
                return self._row(key)
            return None

    def _complete(self, key: str, result: ToolCallResult) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE paid_operations SET state = ?, is_error = ?, result_text = ?, completed_at = ? "
                "WHERE provider_operation_key = ? AND state = 'in_progress'",
                (FAILED if result.is_error else SUCCEEDED, int(result.is_error), result.text,
                 self._now().isoformat(), key))

    async def call(
        self, tool: str, arguments: Mapping[str, Any], authorization: DispatchAuthorization,
    ) -> ToolCallResult:
        spec = self._verify(tool, arguments, authorization)
        existing = self._accept(authorization)
        if existing is not None:
            invocation_id, digest, tenant, state, is_error, text = existing
            # Invocation ids are unique and signed for one tenant, so a
            # mismatch here is never legitimate; the tenant is compared too
            # so a stored outcome is never handed across tenants.
            if (invocation_id != authorization.invocation_id or digest != authorization.args_digest
                    or tenant != authorization.tenant_id):
                raise DispatchRefused("provider operation key is bound to another invocation")
            if state == IN_PROGRESS:
                # A concurrent or restarted duplicate: never a second run.
                raise OutcomeUnavailable("this operation is still in progress")
            return ToolCallResult(is_error=bool(is_error), text=text)
        # The operation is accepted: it runs to its outcome even if the
        # caller goes away (timeout, disconnect), so the record can never be
        # stranded half-done by the caller's cancellation.
        task = asyncio.ensure_future(self._execute(spec, authorization.provider_operation_key, arguments))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return await asyncio.shield(task)

    async def _execute(self, spec: PaidTool, key: str, arguments: Mapping[str, Any]) -> ToolCallResult:
        try:
            result = await spec.handler(arguments)
        except Exception:  # noqa: BLE001 — a failing handler is a recorded failure
            result = ToolCallResult(is_error=True, text=_HANDLER_FAILED_TEXT)
        # A cancelled handler (the process shutting down) is deliberately NOT
        # recorded: whether its side effect happened is unknown, and a
        # `failed` record would let per_successful_call release the charge
        # for an effect that may have taken place. The row stays in_progress,
        # the gateway reports outcome-unknown, and only a human or the
        # reconciler resolves it (ADR 020 sec. 11: time never releases
        # unknown liability).
        self._complete(key, result)
        return result

    def outcome(self, provider_operation_key: str) -> ProviderOutcome:
        with self._lock:
            try:
                row = self._row(provider_operation_key)
            except sqlite3.Error as exc:
                raise OutcomeUnavailable(f"outcome store unreadable: {type(exc).__name__}") from exc
        if row is None:
            return ProviderOutcome(NOT_FOUND)
        _inv, _digest, _tenant, state, is_error, text = row
        if state == IN_PROGRESS:
            return ProviderOutcome(IN_PROGRESS)
        return ProviderOutcome(state, is_error=bool(is_error), text=text)


@dataclass
class PaidLocalProviderSession:
    """An execution-local provider whose every tool sits behind a
    `ProviderIngress`. Satisfies both `ProviderSession` (discovery) and
    `capability_gateway.PaidProviderSession` (paid dispatch)."""

    ingress: ProviderIngress

    async def list_tools(self) -> list[ProviderTool]:
        return self.ingress.tools()

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolCallResult:
        raise DispatchRefused("a paid capability requires a dispatch authorization")

    async def call_paid_tool(
        self, tool: str, arguments: Mapping[str, Any], authorization: DispatchAuthorization,
    ) -> ToolCallResult:
        return await self.ingress.call(tool, arguments, authorization)

    async def outcome(self, provider_operation_key: str) -> ProviderOutcome:
        return self.ingress.outcome(provider_operation_key)


# ---------------------------------------------------------------------------
# The pilot: one read-only capability (ADR 020 Implementation map, C4/C5)
# ---------------------------------------------------------------------------

PILOT_PROVIDER_ID = "pilot-provider"
PILOT_TOOL = "lookup_status"
PILOT_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {"ticket": {"type": "string", "pattern": "^[A-Z]+-[0-9]+$"}},
    "required": ["ticket"],
    "additionalProperties": False,
}


def pilot_lookup_status_ingress(
    *,
    public_keys: Mapping[str, bytes],
    db_path: str,
    statuses: Mapping[str, str] | None = None,
    tenant_id: str = "",
    now: Callable[[], datetime] | None = None,
) -> ProviderIngress:
    """The milestone's paid pilot: reads a ticket's status from a fixed
    table. Read-only, so a duplicate execution would be harmless; the
    ingress still guarantees there is none, which is what the fault tests
    prove. An unknown ticket is an `isError` result: a failure the provider
    recorded, which `per_successful_call` does not charge."""
    table = dict(statuses or {})

    async def lookup_status(arguments: Mapping[str, Any]) -> ToolCallResult:
        ticket = arguments.get("ticket")
        if not isinstance(ticket, str) or ticket not in table:
            return ToolCallResult(is_error=True, text="unknown ticket")
        return ToolCallResult(is_error=False, text=f"{ticket}: {table[ticket]}")

    return ProviderIngress(
        provider_type="mcp-local",
        provider_id=PILOT_PROVIDER_ID,
        public_keys=public_keys,
        tools={PILOT_TOOL: PaidTool(
            name=PILOT_TOOL,
            description="Look up the status of a ticket (paid pilot, simulated credits).",
            input_schema=PILOT_INPUT_SCHEMA,
            handler=lookup_status,
            annotations={"readOnlyHint": True},
        )},
        db_path=db_path,
        tenant_id=tenant_id,
        now=now,
    )


# ---------------------------------------------------------------------------
# projects-mcp: Paid Capability Alpha (ADR 020 sec. 9, projects_search)
# ---------------------------------------------------------------------------

PROJECTS_MCP_PROVIDER_ID = "projects-mcp"
PROJECTS_SEARCH_TOOL = "projects_search"
PROJECTS_SEARCH_CAPABILITY_ID = "mctl://mcp-remote/projects-mcp/projects_search"
PROJECTS_SEARCH_INPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "description": "The project slug, as returned by projects_list", "minLength": 1},
        "query": {"type": "string", "description": "Words to look for", "minLength": 1},
        "limit": {
            "type": "integer",
            "default": 8,
            "description": "How many passages to return",
            "minimum": 1,
            "maximum": 25,
        },
    },
    "required": ["project", "query"],
}
PROJECTS_SEARCH_SCHEMA_HASH: str = hash_bytes(canonical_json(dict(PROJECTS_SEARCH_INPUT_SCHEMA)))


def _parse_mcp_response(raw_text: str) -> dict[str, Any]:
    def _is_response_obj(o: Any) -> bool:
        return isinstance(o, dict) and ("result" in o or "error" in o) and "method" not in o

    text = raw_text.strip()
    if text.startswith(("{", "[")):
        try:
            obj = json.loads(text)
            if _is_response_obj(obj):
                return obj
            if isinstance(obj, list):
                for item in reversed(obj):
                    if _is_response_obj(item):
                        return item
            raise OutcomeUnavailable("malformed JSON-RPC response: neither 'result' nor 'error' present")
        except json.JSONDecodeError as exc:
            raise OutcomeUnavailable(f"malformed JSON response: {exc}") from exc

    text_norm = text.replace("\r\n", "\n").replace("\r", "\n")
    if "data:" in text_norm:
        # Split into SSE frames by double newline to handle multiline events and pings
        frames = [f.strip() for f in text_norm.split("\n\n") if f.strip()]
        for frame in reversed(frames):
            frame_data_lines = []
            for line in frame.splitlines():
                line_str = line.strip()
                if line_str.startswith("data:"):
                    payload = line_str[len("data:"):].lstrip()
                    frame_data_lines.append(payload)
            if frame_data_lines:
                combined_payload = "\n".join(frame_data_lines).strip()
                try:
                    obj = json.loads(combined_payload)
                    if _is_response_obj(obj):
                        return obj
                    if isinstance(obj, list):
                        for item in reversed(obj):
                            if _is_response_obj(item):
                                return item
                except json.JSONDecodeError:
                    continue

        # If no frame matched with double newline, try all data: lines together
        all_data_lines = []
        for line in text_norm.splitlines():
            line_str = line.strip()
            if line_str.startswith("data:"):
                all_data_lines.append(line_str[len("data:"):].lstrip())
        if all_data_lines:
            try:
                obj = json.loads("\n".join(all_data_lines).strip())
                if _is_response_obj(obj):
                    return obj
                if isinstance(obj, list):
                    for item in reversed(obj):
                        if _is_response_obj(item):
                            return item
            except json.JSONDecodeError as exc:
                raise OutcomeUnavailable(f"malformed SSE data payload: {exc}") from exc
        raise OutcomeUnavailable("no JSON-RPC response found in SSE stream")

    try:
        obj = json.loads(text)
        if _is_response_obj(obj):
            return obj
        raise OutcomeUnavailable("malformed JSON-RPC response: neither 'result' nor 'error' present")
    except json.JSONDecodeError as exc:
        raise OutcomeUnavailable(f"malformed JSON response: {exc}") from exc


PROVIDER_OPERATION_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")

POST_ACCEPTANCE_HINTS: tuple[str, ...] = (
    "still in progress",
    "failed on previous attempt",
    "already accepted",
    "concurrent dispatch",
)

PRE_DISPATCH_REFUSAL_HINTS: tuple[str, ...] = (
    # 1. Authorization & ingress verification phrases (commerce.py & ProviderIngress)
    "authorization rejected:",
    *(f"authorization.{name} is missing" for name in ("signature", *DISPATCH_SIGNED_FIELDS)),
    "authorization must be an object",
    "authorization.schema_version must be an integer",
    "unknown authorization schema_version",
    "unknown authorization version",
    "authorization carries unknown fields",
    "names another provider",
    "names another capability",
    "names another input schema",
    "signed for other arguments",
    "names another tenant",
    "requires a dispatch authorization",
    "requires a valid dispatch authorization",
    "invalid authorization structure",
    "invalid authorization json",
    "signature does not verify",
    "signature is not base64url",
    "unknown signing key",
    "provider_operation_key does not derive",
    "invalid dispatch_deadline",
    "invalid signature length",
    "public key must be 32 bytes",
    "dispatch deadline has passed",
    "is not a yyyy-mm-ddthh:mm:ssz time",
    "a hashed time must be timezone-aware",
    # 2. JSON-RPC tool dispatch errors (pre-execution protocol rejections)
    "unknown tool '",
    'unknown tool "',
    "unknown tool: ",
    "arguments cannot be digested",
    "provider operation key is bound to another invocation",
)

# Provenance: Fastify projects-mcp domain non-visibility refusals (pre-acceptance 404).
# Only observed, live-verified patterns are included. Matching opening and closing
# delimiters are strictly required.
DOMAIN_REFUSAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'no project\s+(["\'])[^"\']+\1\s+is available to you', re.IGNORECASE),
    re.compile(r'project\s+(["\'])[^"\']+\1\s+is not configured for tenant', re.IGNORECASE),
)


def is_pre_dispatch_refusal(text: str) -> bool:
    """True if text indicates a pre-acceptance refusal where execution never ran."""
    text_low = text.lower()
    if any(hint in text_low for hint in PRE_DISPATCH_REFUSAL_HINTS):
        return True
    return any(pat.search(text) for pat in DOMAIN_REFUSAL_PATTERNS)


@dataclass
class HttpProjectsMcpPaidSession:
    """A paid provider session connecting to `projects-mcp` over HTTP/MCP.
    Implements PaidProviderSession protocol (ADR 020 sec. 9).

    Note: CapabilityGateway currently registers local_sessions for in-process
    providers; remote HTTP session dispatch will be wired once dynamic remote
    discovery registration is configured.
    """

    base_url: str
    access_token: str = field(default="", repr=False)
    timeout_s: float = 30.0
    transport: httpx.AsyncBaseTransport | None = None
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False, compare=False)
    _client_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        loopback_hosts = ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and parsed.hostname not in loopback_hosts:
            raise ValueError(
                f"base_url must use https scheme for remote providers to protect access tokens, got: {self.base_url}"
            )

    def _get_client(self) -> httpx.AsyncClient:
        current_loop = None
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        if (
            self._client is None
            or self._client.is_closed
            or (self._client_loop is not None and self._client_loop is not current_loop)
        ):
            # A session is bound to a single event loop. An AsyncClient cannot be safely
            # awaited or closed from a foreign event loop (its transport pool belongs to the
            # loop that created it). If this session is invoked across different event loops,
            # we discard the stale client handle and instantiate a fresh client on the active loop.
            self._client = httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport)
            self._client_loop = current_loop
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client if open on the active event loop.

        An AsyncClient cannot be cleanly closed from an event loop different from the one
        that created its transport; in that cross-loop case, drop the reference directly.
        """
        try:
            if self._client is not None and not self._client.is_closed:
                current_loop = None
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if self._client_loop is None or self._client_loop is current_loop:
                    await self._client.aclose()
        finally:
            self._client = None
            self._client_loop = None

    async def __aenter__(self) -> HttpProjectsMcpPaidSession:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def list_tools(self) -> list[ProviderTool]:
        url = f"{self.base_url.rstrip('/')}/mcp"
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if self.access_token:
            headers["cf-access-jwt-assertion"] = self.access_token
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

        try:
            client = self._get_client()
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                raise OutcomeUnavailable(f"tools/list failed: HTTP {resp.status_code}")
            data = _parse_mcp_response(resp.text)
            # Discovery does not carry money liability, so inspect JSON-RPC error first
            # to preserve upstream/middleware diagnostics even if response id is null.
            if "error" in data:
                raise OutcomeUnavailable(f"tools/list JSON-RPC error: {data['error']}")
            raw_id = data.get("id")
            if raw_id is True or raw_id not in (1, "1"):
                raise OutcomeUnavailable(f"tools/list response id mismatch: expected 1, got {raw_id}")
            if "result" not in data:
                raise OutcomeUnavailable(f"tools/list response missing 'result': {data}")
            result = data.get("result")
            if not isinstance(result, dict):
                raise OutcomeUnavailable(f"tools/list result is not a mapping: {result}")
            raw_tools = result.get("tools")
            if not isinstance(raw_tools, list):
                raise OutcomeUnavailable(f"tools/list result 'tools' is not a list: {raw_tools}")
            tools = []
            for t in raw_tools:
                if isinstance(t, dict):
                    name = t.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    execution = t.get("execution")
                    task_support = ""
                    if isinstance(execution, dict):
                        raw_ts = execution.get("taskSupport")
                        if isinstance(raw_ts, str):
                            task_support = raw_ts
                    desc = t.get("description", "")
                    inp = t.get("inputSchema", {})
                    ann = t.get("annotations", {})
                    tools.append(ProviderTool(
                        name=name,
                        description=desc if isinstance(desc, str) else "",
                        input_schema=inp if isinstance(inp, dict) else {},
                        annotations=ann if isinstance(ann, dict) else {},
                        task_support=task_support,
                    ))
            return tools
        except httpx.RequestError as exc:
            raise OutcomeUnavailable(f"tools/list network error: {exc}") from exc

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolCallResult:
        raise DispatchRefused("a paid capability requires a dispatch authorization")

    async def call_paid_tool(
        self, tool: str, arguments: Mapping[str, Any], authorization: DispatchAuthorization,
    ) -> ToolCallResult:
        url = f"{self.base_url.rstrip('/')}/mcp"
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if self.access_token:
            headers["cf-access-jwt-assertion"] = self.access_token

        # Note: per projects-mcp ingress contract, _mctl_dispatch_authorization is passed
        # in-band and stripped by the remote ingress before computing args_digest.
        args = dict(arguments)
        args["_mctl_dispatch_authorization"] = authorization.to_dict()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }

        try:
            client = self._get_client()
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code in (401, 403):
                detail = resp.text[:128].strip()
                raise DispatchRefused(f"HTTP {resp.status_code}: {detail}" if detail else f"HTTP {resp.status_code}")
            if resp.status_code != 200:
                raise OutcomeUnavailable(f"tools/call failed: HTTP {resp.status_code}")
            data = _parse_mcp_response(resp.text)
            # Paid capability execution binds an authorization and ledger liability:
            # validate response id first to ensure unattributable error frames (e.g. id: null)
            # cannot trigger an unverified pre-acceptance refusal release.
            raw_id = data.get("id")
            if raw_id is True or raw_id not in (1, "1"):
                raise OutcomeUnavailable(f"tools/call response id mismatch: expected 1, got {raw_id}")

            # 1. JSON-RPC protocol error branch
            if "error" in data:
                err = data["error"]
                err_data = err.get("data") if isinstance(err, dict) else None
                refusal_marker = (
                    (err_data.get("_mctl_refusal") or err_data.get("mctl_refusal"))
                    if isinstance(err_data, dict)
                    else None
                )
                err_msg = str(err.get("message", err) if isinstance(err, dict) else err)
                if refusal_marker == "pre_acceptance":
                    raise DispatchRefused(err_msg)
                if refusal_marker == "post_acceptance":
                    raise OutcomeUnavailable(f"provider operation state: {err_msg}")
                if any(hint in err_msg.lower() for hint in POST_ACCEPTANCE_HINTS):
                    raise OutcomeUnavailable(f"provider operation state: {err_msg}")
                if is_pre_dispatch_refusal(err_msg):
                    raise DispatchRefused(err_msg)
                raise OutcomeUnavailable(f"tools/call JSON-RPC error: {err_msg}")

            # 2. Result structure check
            if "result" not in data:
                raise OutcomeUnavailable(f"tools/call response missing 'result': {data}")
            result = data["result"]
            if not isinstance(result, dict):
                raise OutcomeUnavailable(f"tools/call result is not a mapping: {result}")

            # 3. Structured refusal marker from ingress (ADR 020 payment boundary)
            refusal_marker = result.get("_mctl_refusal") or result.get("mctl_refusal")
            content = result.get("content", [])
            if not isinstance(content, list):
                content = [content]
            text_parts = []
            for c in content:
                if isinstance(c, str):
                    text_parts.append(c)
                elif isinstance(c, dict) and "text" in c:
                    text_parts.append(str(c["text"]))
            text = "\n".join(text_parts)

            if refusal_marker == "pre_acceptance":
                raise DispatchRefused(text)
            if refusal_marker == "post_acceptance":
                raise OutcomeUnavailable(f"provider operation state: {text}")

            is_error = bool(result.get("isError", False))
            if is_error:
                # Precedence rule: post-acceptance markers take priority over refusal hints
                # so that any post-acceptance errors raise OutcomeUnavailable.
                if any(hint in text.lower() for hint in POST_ACCEPTANCE_HINTS):
                    raise OutcomeUnavailable(f"provider operation state: {text}")
                if is_pre_dispatch_refusal(text):
                    raise DispatchRefused(text)
                raise OutcomeUnavailable(f"provider operation state: {text}")

            # Note: An executed error (is_error=True) that did not match pre-acceptance
            # refusal hints raises OutcomeUnavailable above to be definitively resolved
            # through the provider's /commerce/outcome/{key} endpoint. Reaching here
            # guarantees a successful, executed tool result (is_error=False).
            return ToolCallResult(is_error=False, text=text)
        except httpx.RequestError as exc:
            raise OutcomeUnavailable(f"tools/call network error: {exc}") from exc

    async def outcome(self, provider_operation_key: str) -> ProviderOutcome:
        if (
            not PROVIDER_OPERATION_KEY_PATTERN.fullmatch(provider_operation_key)
            or ".." in provider_operation_key
        ):
            raise OutcomeUnavailable(f"invalid provider_operation_key: {provider_operation_key!r}")
        safe_key = quote(provider_operation_key, safe="/:")
        url = f"{self.base_url.rstrip('/')}/commerce/outcome/{safe_key}"
        headers = {"accept": "application/json"}
        if self.access_token:
            headers["cf-access-jwt-assertion"] = self.access_token

        try:
            client = self._get_client()
            resp = await client.get(url, headers=headers)
            if resp.status_code not in (200, 404):
                raise OutcomeUnavailable(f"outcome lookup failed: HTTP {resp.status_code}")
            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise OutcomeUnavailable(
                    f"malformed outcome JSON response (HTTP {resp.status_code}): {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise OutcomeUnavailable(f"outcome response is not a mapping: {data!r}")
            if resp.status_code == 404:
                status = data.get("status", "")
                if status == NOT_FOUND:
                    return ProviderOutcome(NOT_FOUND)
                raise OutcomeUnavailable(f"HTTP 404 returned unexpected status: {status!r}")
            status = data.get("status", "")
            if status not in (SUCCEEDED, FAILED, IN_PROGRESS, NOT_FOUND):
                raise OutcomeUnavailable(f"unknown outcome status {status!r}")
            return ProviderOutcome(
                status=status,
                is_error=bool(data.get("is_error", False)),
                text=str(data.get("text", "")),
            )
        except httpx.RequestError as exc:
            raise OutcomeUnavailable(f"outcome lookup network error: {exc}") from exc

