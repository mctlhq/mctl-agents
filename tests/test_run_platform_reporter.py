"""Wiring-level tests for run_platform_reporter()'s mctl MCP guard.

The platform reporter has no job without live MCP state, so a missed
connection is fatal — unlike mentor/service-agent (fatal=False) and like
incident-responder (fatal=True). A missing MCTL_TOKEN is also fatal here
(incident-responder degrades; this mode must not).
"""
from __future__ import annotations

import types

import anyio
import pytest

from orchestrator import run_all
from orchestrator import run_platform_reporter as rpr
from orchestrator.mcp_guard import McpNotConnectedError
from tests.conftest import fake_mcp_client_factory


def _stub_build_options(monkeypatch, *, mcp_servers):
    monkeypatch.setattr(
        rpr, "build_platform_reporter_options",
        lambda *args, **kwargs: types.SimpleNamespace(mcp_servers=mcp_servers),
    )
    monkeypatch.setattr(rpr, "_rotate_old_digests", lambda *args, **kwargs: None)


def test_connected_mcp_dispatches_query_without_warning(monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rpr, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}]
        ),
    )
    anyio.run(rpr.run_platform_reporter)
    assert "warn:" not in capsys.readouterr().err


def test_failed_mcp_raises(monkeypatch):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rpr, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}]
        ),
    )
    with pytest.raises(McpNotConnectedError, match="boom"):
        anyio.run(rpr.run_platform_reporter)


def test_mcp_not_configured_raises_without_opening_client(monkeypatch):
    """No MCTL_TOKEN means zero live state — must not dispatch a prompt."""
    _stub_build_options(monkeypatch, mcp_servers={})
    monkeypatch.setattr(
        rpr, "ClaudeSDKClient",
        fake_mcp_client_factory(),
    )
    with pytest.raises(McpNotConnectedError, match="MCTL_TOKEN"):
        anyio.run(rpr.run_platform_reporter)


def test_prompt_names_required_mcp_reads_and_report_path():
    prompt = rpr.build_prompt()
    for needle in (
        "mctl_whoami",
        "mctl_list_tenants",
        "mctl_list_services",
        "mctl_get_resource_usage",
        "mctl_get_service_status",
        "mctl_incident_summary",
        "mctl_list_incidents",
        "mctl_list_recent_operations",
        "mctl_list_recent_agent_runs",
        "health/",
        "Do not call write",
    ):
        assert needle in prompt
    assert "mctl_deploy_service" not in prompt
    assert "mctl_resolve_incident" not in prompt


def test_run_all_full_mode_runs_reporter_after_mentor(monkeypatch):
    order: list[str] = []

    async def _mentor() -> None:
        order.append("mentor")

    async def _reporter() -> None:
        order.append("reporter")

    monkeypatch.setattr(run_all, "ROTATING_SERVICES", [])
    monkeypatch.setattr(run_all, "run_mentor", _mentor)
    monkeypatch.setattr(run_all, "run_platform_reporter", _reporter)
    anyio.run(run_all._full)
    assert order == ["mentor", "reporter"]


def test_run_all_platform_report_mode_dispatches(monkeypatch):
    called: list[bool] = []

    async def _fake() -> None:
        called.append(True)

    monkeypatch.setenv("RUN_MODE", "platform-report")
    monkeypatch.setattr(run_all, "run_platform_reporter", _fake)
    anyio.run(run_all.main)
    assert called == [True]


def test_run_all_unknown_mode_lists_platform_report(monkeypatch, capsys):
    monkeypatch.setenv("RUN_MODE", "not-a-mode")
    with pytest.raises(SystemExit) as exc_info:
        anyio.run(run_all.main)
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "platform-report" in err
