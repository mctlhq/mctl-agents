"""Unit tests for orchestrator.mcp_guard — the shared mctl MCP connectivity
guard used by every mode that configures mctl MCP (service-agent,
implementer, mentor, issue-investigator, incident-responder).

wait_for_mctl_connected()'s poll/timeout/malformed-response behavior is
already exercised end-to-end via test_run_incident_responder.py (through
ensure_mctl_connected(fatal=True)); these tests cover the module directly,
plus the fatal=False warn-and-continue path that only the four non-incident
-responder modes use (see options.py's build_* functions).
"""
from __future__ import annotations

from functools import partial

import anyio
import pytest

from orchestrator import mcp_guard
from orchestrator.mcp_guard import McpNotConnectedError, ensure_mctl_connected


class _FakeClient:
    def __init__(self, *, statuses):
        self._statuses = list(statuses)

    async def get_mcp_status(self):
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]


def _shrink_poll_window(monkeypatch):
    monkeypatch.setattr(mcp_guard, "MCP_CONNECT_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mcp_guard, "MCP_CONNECT_POLL_INTERVAL_S", 0.01)


def test_ensure_mctl_connected_fatal_true_reraises(monkeypatch):
    client = _FakeClient(
        statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}]
    )
    with pytest.raises(McpNotConnectedError, match="boom"):
        anyio.run(partial(ensure_mctl_connected, client, fatal=True))


def test_ensure_mctl_connected_fatal_false_warns_not_raises(monkeypatch, capsys):
    client = _FakeClient(
        statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}]
    )
    anyio.run(partial(ensure_mctl_connected, client, fatal=False))  # must not raise
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert "mctl MCP server status=failed" in captured.err


def test_ensure_mctl_connected_fatal_false_connected_is_silent(capsys):
    client = _FakeClient(statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}])
    anyio.run(partial(ensure_mctl_connected, client, fatal=False))
    captured = capsys.readouterr()
    assert captured.err == ""


def test_ensure_mctl_connected_fatal_false_timeout_warns(monkeypatch, capsys):
    _shrink_poll_window(monkeypatch)
    client = _FakeClient(statuses=[{"mcpServers": [{"name": "mctl", "status": "pending"}]}])
    anyio.run(partial(ensure_mctl_connected, client, fatal=False))  # must not raise
    captured = capsys.readouterr()
    assert "still status=pending" in captured.err
