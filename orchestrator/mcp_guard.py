"""Shared mctl MCP connectivity guard for ClaudeSDKClient-based agent runs.

Originally built for the incident-responder's silent false-green bug (see
incident-responder-issue.md): a session can dispatch its first turn before
the mctl MCP HTTP handshake completes, silently running with zero
mcp__mctl__* tools. `alwaysLoad` on the server config (set in
orchestrator/options.py's mctl_mcp_config) closes that race by blocking
first-turn dispatch until the server connects — this module adds a positive
verification on top, for callers that want to know (and optionally fail on)
whether that connection actually succeeded, since a transient api.mctl.ai
outage or an invalid MCTL_TOKEN reproduces the same symptom even with
alwaysLoad in place.

Every mode that configures mctl MCP (service-agent, implementer, mentor,
issue-investigator, incident-responder) shares this guard. Only
incident-responder treats a failed connection as fatal (`fatal=True`) — its
own prompt explicitly treats "no mcp__mctl__* tools" as a silent, valid
success, so a missed connection there means zero real work happened. The
other modes still do useful work without mctl tools (WebSearch/WebFetch/
Read/Write/Bash), so they log a warning and continue — matching the
data-driven finding from the 2026-08-02 incident that a real MCTL_TOKEN
outage was previously invisible across all five modes, not just
incident-responder's.
"""
from __future__ import annotations

import sys

import anyio
from claude_agent_sdk import ClaudeSDKClient

# alwaysLoad's own blocking guarantee is tied to first-turn dispatch, not to
# ClaudeSDKClient construction — a single status read immediately after
# __aenter__ can observe a legitimately still-connecting ("pending") server
# and misreport it as failed. Poll instead, bounded so a genuinely dead
# server still surfaces within a reasonable window.
MCP_CONNECT_POLL_TIMEOUT_S = 5.0
MCP_CONNECT_POLL_INTERVAL_S = 0.5
_MCP_TERMINAL_FAILURE_STATUSES = frozenset({"failed", "needs-auth", "disabled"})


class McpNotConnectedError(RuntimeError):
    """mctl MCP was configured (MCTL_TOKEN present) but never reached
    'connected' status before the prompt was dispatched — the session would
    otherwise silently run with zero mcp__mctl__* tools and no error at all.
    """


async def wait_for_mctl_connected(client: ClaudeSDKClient) -> None:
    """Poll get_mcp_status() until the mctl server is connected or reaches a
    terminal non-connected state. Raises McpNotConnectedError on a terminal
    failure, on timeout, or if the status check itself fails (e.g. the SDK
    control request errors out) — a failure to even verify connectivity
    means the caller cannot trust the tool set either, same as an explicit
    non-connected status.

    The whole loop runs inside one anyio.fail_after scope rather than a
    manual anyio.current_time() deadline check (Codex finding on PR #84): a
    plain clock check only fires *after* an await returns, so a wedged SDK
    control request — get_mcp_status() itself never returning — blocked
    forever and the "bounded" guarantee never actually applied.
    anyio.fail_after instead cancels the enclosing scope (including an
    in-flight await) once the deadline passes, converting to TimeoutError.
    """
    current = "pending"
    try:
        with anyio.fail_after(MCP_CONNECT_POLL_TIMEOUT_S):
            while True:
                # Response *parsing* (dict indexing below), not just the
                # await itself, must stay inside this try: a malformed/
                # unexpected response (missing mcpServers/name/status keys)
                # would otherwise raise a raw KeyError instead of
                # McpNotConnectedError.
                try:
                    status = await client.get_mcp_status()
                    mctl_status = next(
                        (s for s in status["mcpServers"] if s["name"] == "mctl"), None
                    )
                    current = mctl_status["status"] if mctl_status else "missing"
                except Exception as exc:
                    raise McpNotConnectedError(
                        f"failed to verify mctl MCP connection status: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                if current == "connected":
                    return
                if current in _MCP_TERMINAL_FAILURE_STATUSES:
                    raise McpNotConnectedError(
                        f"mctl MCP server status={current} "
                        f"error={mctl_status.get('error') if mctl_status else None!r} "
                        f"— check api.mctl.ai/mcp health and MCTL_TOKEN validity"
                    )
                await anyio.sleep(MCP_CONNECT_POLL_INTERVAL_S)
    except TimeoutError as exc:
        raise McpNotConnectedError(
            f"mctl MCP server still status={current} after "
            f"{MCP_CONNECT_POLL_TIMEOUT_S}s (a wedged get_mcp_status() call "
            f"would also surface here now) — check api.mctl.ai/mcp health "
            f"and MCTL_TOKEN validity"
        ) from exc


async def ensure_mctl_connected(client: ClaudeSDKClient, *, fatal: bool) -> None:
    """Verify mctl MCP connectivity before dispatching the prompt.

    fatal=True re-raises McpNotConnectedError (incident-responder: no mctl
    tools means zero real work, must not be a silent success).
    fatal=False logs a warning and returns normally (every other mode: mctl
    tools are a bonus, not the whole job — but the outage must be visible in
    logs instead of invisible, per the 2026-08-02 finding that this exact
    condition was previously silent everywhere but incident-responder).
    """
    try:
        await wait_for_mctl_connected(client)
    except McpNotConnectedError as exc:
        if fatal:
            raise
        print(f"⚠️  {exc}", file=sys.stderr)
