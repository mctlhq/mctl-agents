"""Wiring-level tests for run_service_agent()'s mctl MCP connectivity guard.

Regression coverage for a Claude-review finding on PR #84: the modes newly
wired onto ensure_mctl_connected(fatal=False) (this one, run_mentor.py,
run_issue_investigator.py, run_implementer.py) had no test exercising the
mcp_configured=True branch — every existing test ran with MCTL_TOKEN unset,
so ensure_mctl_connected was never actually invoked at the integration
level, only in isolation via test_mcp_guard.py. These use the real
run_service_agent() with build_service_agent_options() stubbed, matching
the pattern already established in tests/test_run_incident_responder.py.
"""
from __future__ import annotations

import types

import anyio

from orchestrator import run_service_agent as rsa
from tests.conftest import fake_mcp_client_factory


def _stub_build_options(monkeypatch, *, mcp_servers):
    monkeypatch.setattr(
        rsa, "build_service_agent_options",
        lambda *args, **kwargs: types.SimpleNamespace(mcp_servers=mcp_servers),
    )


def test_connected_mcp_dispatches_query_without_warning(monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rsa, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}]
        ),
    )
    anyio.run(rsa.run_service_agent, "mctl-agent")
    assert "warn:" not in capsys.readouterr().err


def test_failed_mcp_warns_but_still_dispatches_query(monkeypatch, capsys):
    """fatal=False: a broken mctl connection must not stop the agent from
    running — service-agent still does useful work without mctl tools."""
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    factory = fake_mcp_client_factory(
        statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}]
    )
    monkeypatch.setattr(rsa, "ClaudeSDKClient", factory)
    anyio.run(rsa.run_service_agent, "mctl-agent")  # must not raise
    assert "boom" in capsys.readouterr().err


def test_mcp_not_configured_skips_status_check_entirely(monkeypatch):
    """MCTL_TOKEN unset -> mcp_servers={} -> get_mcp_status() must never be
    called (FakeMcpClient raises AssertionError if it is)."""
    _stub_build_options(monkeypatch, mcp_servers={})
    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory())
    anyio.run(rsa.run_service_agent, "mctl-agent")  # must not raise
