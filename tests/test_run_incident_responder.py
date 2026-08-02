"""Unit tests for the incident responder's MCP-connectivity guard.

Regression coverage for the silent false-green bug (see
incident-responder-issue.md): the responder could run with zero
mcp__mctl__* tools, do nothing, and still have the workflow report
Succeeded. The fix has two parts, tested separately:

  1. `run_incident_responder()` switched from the one-shot `query()` to
     `ClaudeSDKClient` so it can call `get_mcp_status()` before dispatching
     the prompt, polling via `_wait_for_mctl_connected()` (not a single
     immediate read — alwaysLoad's blocking guarantee is tied to first-turn
     dispatch, not client construction, so a lone read can race a
     legitimately still-connecting server) and raising
     `McpNotConnectedError` if mctl MCP was configured but never settles on
     "connected".
  2. `run_all._safe_run_incident_responder()` must NOT swallow that
     specific exception the way it swallows transient SDK/budget errors —
     it has to `sys.exit(MCP_NOT_CONNECTED_EXIT_CODE)` so Argo's
     assert-attempt gate sees a real failure instead of a false green.

The responder's own agent logic (prompt building, incident processing) is
agent-driven (see agents/_incident-responder/CLAUDE.md) and not unit-tested
here.
"""
from __future__ import annotations

import types

import anyio
import pytest

from orchestrator import mcp_guard
from orchestrator import run_all
from orchestrator import run_incident_responder as rir
from orchestrator.mcp_guard import McpNotConnectedError


class _FakeClient:
    def __init__(self, *, statuses, messages, status_error=None, options=None):
        # `statuses` is a list of {"mcpServers": [...]} dicts consumed in
        # order, one per get_mcp_status() call; the last entry repeats once
        # exhausted (so a "never connects" test can just supply one status
        # and let the poll loop hit it every tick until timeout).
        self._statuses = list(statuses)
        self._messages = messages
        self._status_error = status_error
        self.queried_prompt = None
        self.status_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_mcp_status(self):
        self.status_calls += 1
        if self._status_error is not None:
            raise self._status_error
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    async def query(self, prompt):
        self.queried_prompt = prompt

    async def receive_response(self):
        for message in self._messages:
            yield message


def _fake_client_factory(*, statuses, messages=(), status_error=None):
    def _factory(*, options):
        return _FakeClient(
            statuses=statuses, messages=messages, status_error=status_error,
            options=options,
        )
    return _factory


def _stub_build_options(monkeypatch, *, mcp_servers):
    """Bypass the real build_incident_responder_options() (which reads the
    real MCTL_TOKEN env var) with a stand-in exposing just the .mcp_servers
    attribute run_incident_responder() actually reads."""
    monkeypatch.setattr(
        rir, "build_incident_responder_options",
        lambda **kwargs: types.SimpleNamespace(mcp_servers=mcp_servers),
    )


def _shrink_poll_window(monkeypatch):
    """Tests that exercise the timeout path would otherwise take
    MCP_CONNECT_POLL_TIMEOUT_S (5s) each — shrink it so the suite stays fast.
    Lives in orchestrator.mcp_guard now (shared across all 5 modes), not rir."""
    monkeypatch.setattr(mcp_guard, "MCP_CONNECT_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mcp_guard, "MCP_CONNECT_POLL_INTERVAL_S", 0.01)


# ---------------------------------------------------------------------------
# run_incident_responder — MCP status guard
# ---------------------------------------------------------------------------
def test_raises_when_mctl_never_connects(monkeypatch):
    _shrink_poll_window(monkeypatch)
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "pending"}]}]
        ),
    )
    with pytest.raises(McpNotConnectedError):
        anyio.run(rir.run_incident_responder)


def test_raises_immediately_on_terminal_failure_status(monkeypatch):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(
            statuses=[{"mcpServers": [
                {"name": "mctl", "status": "failed", "error": "handshake refused"}
            ]}]
        ),
    )
    with pytest.raises(McpNotConnectedError, match="handshake refused"):
        anyio.run(rir.run_incident_responder)


def test_raises_when_mctl_missing_from_status_list(monkeypatch):
    """The server never showing up in mcpServers at all is the same failure
    as an explicit non-connected status — must not be treated as fine."""
    _shrink_poll_window(monkeypatch)
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(statuses=[{"mcpServers": []}]),
    )
    with pytest.raises(McpNotConnectedError):
        anyio.run(rir.run_incident_responder)


def test_raises_when_status_check_itself_errors(monkeypatch):
    """get_mcp_status() failing outright (SDK control-request timeout,
    malformed response, ...) means zero real work happened too — it must
    surface as McpNotConnectedError, not fall through to run_all's generic
    swallow-everything branch and recreate the false-green bug."""
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(statuses=[], status_error=RuntimeError("control request timed out")),
    )
    with pytest.raises(McpNotConnectedError, match="control request timed out"):
        anyio.run(rir.run_incident_responder)


def test_raises_when_status_response_is_malformed(monkeypatch):
    """Codex P1 on the first version of this fix: response *parsing* (dict
    indexing), not just the get_mcp_status() await itself, has to be inside
    the try/except — a status payload missing 'mcpServers' would otherwise
    raise a raw KeyError that isn't McpNotConnectedError, falls through
    run_all's generic swallow, and recreates the false-green bug."""
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(statuses=[{"unexpected_shape": True}]),
    )
    with pytest.raises(McpNotConnectedError, match="KeyError"):
        anyio.run(rir.run_incident_responder)


def test_recovers_after_transient_pending_status(monkeypatch):
    """Regression for the race Codex and claude[bot] both flagged: a status
    read immediately after connect can legitimately observe 'pending' on a
    server that connects moments later. The poll loop must NOT treat that
    as a permanent failure — only a terminal status or a full timeout."""
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(statuses=[
            {"mcpServers": [{"name": "mctl", "status": "pending"}]},
            {"mcpServers": [{"name": "mctl", "status": "pending"}]},
            {"mcpServers": [{"name": "mctl", "status": "connected"}]},
        ]),
    )
    anyio.run(rir.run_incident_responder)  # must not raise


def test_does_not_raise_when_mctl_connected(monkeypatch):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}]
        ),
    )
    anyio.run(rir.run_incident_responder)  # must not raise


def test_skips_check_when_mcp_not_configured(monkeypatch):
    """MCTL_TOKEN unset is the documented degrade-gracefully / local-dev
    path (mctl_mcp_config's own docstring) — it must not be conflated with
    a real connectivity failure."""
    _stub_build_options(monkeypatch, mcp_servers={})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        # Would fail the check if it were evaluated — proves the guard is
        # actually skipped, not just coincidentally passing.
        _fake_client_factory(statuses=[{"mcpServers": []}]),
    )
    anyio.run(rir.run_incident_responder)  # must not raise


# ---------------------------------------------------------------------------
# run_all._safe_run_incident_responder — exit-code wiring
# ---------------------------------------------------------------------------
def test_safe_run_exits_nonzero_on_mcp_not_connected(monkeypatch):
    async def _raise():
        raise McpNotConnectedError("mctl MCP server status=pending")

    monkeypatch.setattr(run_all, "run_incident_responder", _raise)
    with pytest.raises(SystemExit) as exc_info:
        anyio.run(run_all._safe_run_incident_responder)
    assert exc_info.value.code == run_all.MCP_NOT_CONNECTED_EXIT_CODE


def test_safe_run_still_swallows_unrelated_exceptions(monkeypatch):
    """Existing behavior for transient SDK/budget/network failures must be
    unchanged — only McpNotConnectedError gets the non-zero exit."""
    async def _raise():
        raise RuntimeError("transient blip")

    monkeypatch.setattr(run_all, "run_incident_responder", _raise)
    anyio.run(run_all._safe_run_incident_responder)  # must not raise
