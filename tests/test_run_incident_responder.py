"""Unit tests for the incident responder's MCP-connectivity guard.

Regression coverage for the silent false-green bug (see
incident-responder-issue.md): the responder could run with zero
mcp__mctl__* tools, do nothing, and still have the workflow report
Succeeded. The fix has two parts, tested separately:

  1. `run_incident_responder()` switched from the one-shot `query()` to
     `ClaudeSDKClient` so it can call `get_mcp_status()` before dispatching
     the prompt, and raises `McpNotConnectedError` if mctl MCP was
     configured but never reached "connected".
  2. `run_all._safe_run_incident_responder()` must NOT swallow that
     specific exception the way it swallows transient SDK/budget errors —
     it has to `sys.exit(MCP_NOT_CONNECTED_EXIT_CODE)` so Argo's
     assert-attempt gate sees a real failure instead of a false green.

The responder's own agent logic (prompt building, incident processing) is
agent-driven (see agents/_incident-responder/CLAUDE.md) and not unit-tested
here.
"""
from __future__ import annotations

import anyio
import pytest

from orchestrator import run_all
from orchestrator import run_incident_responder as rir
from orchestrator.run_incident_responder import McpNotConnectedError


class _FakeClient:
    def __init__(self, *, mcp_status, messages, options=None):
        self._mcp_status = mcp_status
        self._messages = messages
        self.queried_prompt = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_mcp_status(self):
        return self._mcp_status

    async def query(self, prompt):
        self.queried_prompt = prompt

    async def receive_response(self):
        for message in self._messages:
            yield message


def _fake_client_factory(*, mcp_status, messages=()):
    def _factory(*, options):
        return _FakeClient(mcp_status=mcp_status, messages=messages, options=options)
    return _factory


# ---------------------------------------------------------------------------
# run_incident_responder — MCP status guard
# ---------------------------------------------------------------------------
def test_raises_when_mctl_status_not_connected(monkeypatch):
    monkeypatch.setattr(rir, "mctl_mcp_config", lambda: {"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(
            mcp_status={"mcpServers": [{"name": "mctl", "status": "pending"}]}
        ),
    )
    with pytest.raises(McpNotConnectedError):
        anyio.run(rir.run_incident_responder)


def test_raises_when_mctl_missing_from_status_list(monkeypatch):
    """The server never showing up in mcpServers at all is the same failure
    as an explicit non-connected status — must not be treated as fine."""
    monkeypatch.setattr(rir, "mctl_mcp_config", lambda: {"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(mcp_status={"mcpServers": []}),
    )
    with pytest.raises(McpNotConnectedError):
        anyio.run(rir.run_incident_responder)


def test_does_not_raise_when_mctl_connected(monkeypatch):
    monkeypatch.setattr(rir, "mctl_mcp_config", lambda: {"mctl": {}})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        _fake_client_factory(
            mcp_status={"mcpServers": [{"name": "mctl", "status": "connected"}]}
        ),
    )
    anyio.run(rir.run_incident_responder)  # must not raise


def test_skips_check_when_mcp_not_configured(monkeypatch):
    """MCTL_TOKEN unset is the documented degrade-gracefully / local-dev
    path (mctl_mcp_config's own docstring) — it must not be conflated with
    a real connectivity failure."""
    monkeypatch.setattr(rir, "mctl_mcp_config", lambda: {})
    monkeypatch.setattr(
        rir, "ClaudeSDKClient",
        # Would fail the check if it were evaluated — proves the guard is
        # actually skipped, not just coincidentally passing.
        _fake_client_factory(mcp_status={"mcpServers": []}),
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
