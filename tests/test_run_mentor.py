"""Wiring-level tests for run_mentor()'s mctl MCP connectivity guard.

See tests/test_run_service_agent.py's module docstring for why these exist
(Claude-review finding on PR #84).
"""
from __future__ import annotations

import types

import anyio

from orchestrator import run_mentor as rm
from tests.conftest import fake_mcp_client_factory


def _stub_build_options(monkeypatch, *, mcp_servers):
    monkeypatch.setattr(
        rm, "build_mentor_options",
        lambda *args, **kwargs: types.SimpleNamespace(mcp_servers=mcp_servers),
    )
    # run_mentor() also rotates old digests on disk before building options —
    # not relevant to the MCP guard, no-op it so the test doesn't need a real
    # digest directory.
    monkeypatch.setattr(rm, "_rotate_old_digests", lambda *args, **kwargs: None)


def test_connected_mcp_dispatches_query_without_warning(monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rm, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}]
        ),
    )
    anyio.run(rm.run_mentor)
    assert "warn:" not in capsys.readouterr().err


def test_failed_mcp_warns_but_still_dispatches_query(monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    factory = fake_mcp_client_factory(
        statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}]
    )
    monkeypatch.setattr(rm, "ClaudeSDKClient", factory)
    anyio.run(rm.run_mentor)  # must not raise
    assert "boom" in capsys.readouterr().err


def test_mcp_not_configured_skips_status_check_entirely(monkeypatch):
    _stub_build_options(monkeypatch, mcp_servers={})
    monkeypatch.setattr(rm, "ClaudeSDKClient", fake_mcp_client_factory())
    anyio.run(rm.run_mentor)  # must not raise
