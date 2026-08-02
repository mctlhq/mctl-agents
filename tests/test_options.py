"""Unit tests for orchestrator.options's mctl MCP config, in particular the
`always_load` scoping introduced to fix the incident-responder silent
false-green bug (see incident-responder-issue.md).

Regression coverage for a real Claude-review finding on that fix's PR: no
test asserted `alwaysLoad: True` was actually present in the config used by
the incident responder — only the defense-in-depth guard in
run_incident_responder.py was exercised, via a mocked mctl_mcp_config.
These tests exercise the real function output instead.
"""
from __future__ import annotations

from orchestrator import options


def test_mctl_mcp_config_default_omits_always_load(monkeypatch):
    """The other three modes (service-agent, implementer, mentor) must keep
    their current non-blocking behavior — alwaysLoad is opt-in, not global,
    per the Codex finding that a shared default would give a single mctl
    outage more blast radius than necessary."""
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    config = options.mctl_mcp_config()
    assert "alwaysLoad" not in config["mctl"]


def test_mctl_mcp_config_always_load_true_sets_flag(monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    config = options.mctl_mcp_config(always_load=True)
    assert config["mctl"]["alwaysLoad"] is True


def test_mctl_mcp_config_no_token_ignores_always_load(monkeypatch):
    monkeypatch.delenv("MCTL_TOKEN", raising=False)
    assert options.mctl_mcp_config(always_load=True) == {}


def test_build_incident_responder_options_requests_always_load(tmp_path, monkeypatch):
    """The wiring-level check the PR review flagged as missing: the
    responder's own options builder must actually pass always_load=True
    through to mctl_mcp_config(), not just the helper support it."""
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    agent_dir = tmp_path / "_incident-responder"
    agent_dir.mkdir()
    built = options.build_incident_responder_options(
        agent_dir=agent_dir, model="test-model", state_dir=tmp_path,
    )
    assert built.mcp_servers["mctl"]["alwaysLoad"] is True


def test_build_service_agent_options_does_not_request_always_load(tmp_path, monkeypatch):
    """Sibling modes must NOT get the blocking behavior — confirms the
    scoping decision actually holds at the call site, not just in theory."""
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    service_dir = tmp_path / "mctl-api"
    service_dir.mkdir()
    built = options.build_service_agent_options(service_dir, model="test-model")
    assert "alwaysLoad" not in built.mcp_servers["mctl"]
