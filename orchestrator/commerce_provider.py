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
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from orchestrator.capability_gateway import ProviderTool, ToolCallResult
from orchestrator.commerce import (
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
                         annotations=t.annotations)
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
            self._db.execute(
                "INSERT INTO paid_operations (provider_operation_key, invocation_id, capability_id, args_digest, "
                "tenant_id, state, accepted_at) VALUES (?, ?, ?, ?, ?, 'in_progress', ?)",
                (key, authorization.invocation_id, authorization.capability_id, authorization.args_digest,
                 authorization.tenant_id, self._now().isoformat()))
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
