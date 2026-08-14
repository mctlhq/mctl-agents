"""Unit tests for orchestrator.options's mctl MCP config, in particular the
`always_load` wiring introduced to fix the incident-responder silent
false-green bug (see incident-responder-issue.md) and then extended to every
other mctl-consuming mode after the 2026-08-02 finding that a genuinely dead
MCTL_TOKEN was silently degrading all of them, not just incident-responder.

Regression coverage for a real Claude-review finding on the original fix's
PR: no test asserted `alwaysLoad: True` was actually present in the config
used by the incident responder — only the defense-in-depth guard in
run_incident_responder.py was exercised, via a mocked mctl_mcp_config. These
tests exercise the real function output instead, for every builder.
"""
from __future__ import annotations

from orchestrator import options


def test_mctl_mcp_config_default_omits_always_load(monkeypatch):
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
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    agent_dir = tmp_path / "_incident-responder"
    agent_dir.mkdir()
    built = options.build_incident_responder_options(
        agent_dir=agent_dir, model="test-model", state_dir=tmp_path,
    )
    assert built.mcp_servers["mctl"]["alwaysLoad"] is True


def test_build_service_agent_options_requests_always_load(tmp_path, monkeypatch):
    """Every mctl-consuming mode gets alwaysLoad now — the 2026-08-02
    MCTL_TOKEN outage showed service-agent's own prompt ("no mcp__mctl__*
    tools, skip silently") has the same silent-degradation shape as
    incident-responder did, just with a smaller blast radius per run."""
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    service_dir = tmp_path / "mctl-api"
    service_dir.mkdir()
    built = options.build_service_agent_options(service_dir, model="test-model")
    assert built.mcp_servers["mctl"]["alwaysLoad"] is True


def test_build_implementer_agent_options_requests_always_load(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    repo_dir = tmp_path / "mctl-api"
    repo_dir.mkdir()
    built = options.build_implementer_agent_options(repo_dir, model="test-model")
    assert built.mcp_servers["mctl"]["alwaysLoad"] is True


def test_build_mentor_options_requests_always_load(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    mentor_dir = tmp_path / "agents" / "_mentor"
    mentor_dir.mkdir(parents=True)
    built = options.build_mentor_options(mentor_dir, model="test-model")
    assert built.mcp_servers["mctl"]["alwaysLoad"] is True


def test_build_issue_investigator_options_requests_always_load(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    repo_dir = tmp_path / "mctl-api"
    repo_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"
    built = options.build_issue_investigator_options(
        repo_dir, model="test-model", proposal_dir=proposal_dir,
    )
    assert built.mcp_servers["mctl"]["alwaysLoad"] is True


def test_build_shepherd_options_has_no_mcp_at_all(tmp_path, monkeypatch):
    """shepherd never calls mcp__mctl__* — it classifies a pre-filtered
    bundle of review findings that's already fully in the prompt. Nothing to
    always-load; mcp_servers stays the empty dict it always was."""
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    shepherd_dir = tmp_path / "_shepherd"
    shepherd_dir.mkdir()
    built = options.build_shepherd_options(shepherd_dir, model="test-model")
    assert built.mcp_servers == {}


def test_bash_modes_install_command_audit_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    service_dir = tmp_path / "mctl-api"
    service_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"
    incident_dir = tmp_path / "_incident-responder"
    incident_dir.mkdir()

    builders = [
        options.build_service_agent_options(service_dir, model="test-model"),
        options.build_implementer_agent_options(service_dir, model="test-model"),
        options.build_incident_responder_options(
            agent_dir=incident_dir, model="test-model", state_dir=tmp_path,
        ),
        options.build_issue_investigator_options(
            service_dir, model="test-model", proposal_dir=proposal_dir,
        ),
    ]
    for built in builders:
        matchers = (built.hooks or {}).get("PreToolUse") or []
        assert any(m.matcher == "Bash" for m in matchers)
