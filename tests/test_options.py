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

import dataclasses

from orchestrator import options, resolver


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


# ---------------------------------------------------------------------------
# build_issue_investigator_options_from_plan — legacy/declarative equivalence
# (mctlhq/mctl-agents#227's "T6. Legacy and declarative options are
# equivalent" acceptance test). MCTL_TOKEN is set for both sides, same as
# orchestrator/validate_manifest.py's own comparison, so the mcp_servers
# and allowed_tools "mcp__mctl__*" entry aren't a false-diff artifact of
# whichever environment the test happens to run in.
# ---------------------------------------------------------------------------
def test_build_issue_investigator_options_from_plan_matches_legacy_builder(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.delenv("ISSUE_INVESTIGATOR_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_BALANCED_MODEL", raising=False)

    repo_dir = tmp_path / "mctl-telegram"
    repo_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"

    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="e" * 40))
    legacy = options.build_issue_investigator_options(
        repo_dir, model=plan.model, proposal_dir=proposal_dir,
    )
    declarative = options.build_issue_investigator_options_from_plan(plan, repo_dir, proposal_dir)

    assert declarative.cwd == legacy.cwd
    assert declarative.model == legacy.model
    assert sorted(declarative.allowed_tools) == sorted(legacy.allowed_tools)
    assert declarative.mcp_servers == legacy.mcp_servers
    assert declarative.permission_mode == legacy.permission_mode
    assert declarative.max_budget_usd == legacy.max_budget_usd
    assert declarative.add_dirs == legacy.add_dirs
    assert declarative.env == legacy.env


def test_build_issue_investigator_options_from_plan_omits_mctl_tools_without_token(tmp_path, monkeypatch):
    """The profile fixture hardcodes `mcp__mctl__*` in spec.tools unconditionally,
    but the legacy builder only allows it when MCTL_TOKEN is set (via
    `_mctl_tool_globs()`). Without MCTL_TOKEN, the declarative path must match
    that and omit `mcp__mctl__*` too — otherwise it would silently diverge
    from the "matches the legacy builder exactly, by construction" claim."""
    monkeypatch.delenv("MCTL_TOKEN", raising=False)

    repo_dir = tmp_path / "mctl-telegram"
    repo_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"

    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="f" * 40))
    legacy = options.build_issue_investigator_options(
        repo_dir, model=plan.model, proposal_dir=proposal_dir,
    )
    declarative = options.build_issue_investigator_options_from_plan(plan, repo_dir, proposal_dir)

    assert "mcp__mctl__*" not in declarative.allowed_tools
    assert "mcp__mctl__*" not in legacy.allowed_tools
    assert sorted(declarative.allowed_tools) == sorted(legacy.allowed_tools)
    assert declarative.mcp_servers == {}
    assert declarative.mcp_servers == legacy.mcp_servers


def test_build_issue_investigator_options_from_plan_uses_the_plans_model_and_budget(tmp_path, monkeypatch):
    """The plan-based builder is built FROM the plan, not from
    orchestrator.options's own ISSUE_INVESTIGATOR_MODEL/_BUDGET_USD
    constants — a plan carrying a different model/budget must be reflected
    verbatim, independent of those constants."""
    repo_dir = tmp_path / "mctl-telegram"
    repo_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"

    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="d" * 40))
    fake_plan = dataclasses.replace(plan, model="a-completely-different-model", budget_usd=42.0)

    built = options.build_issue_investigator_options_from_plan(fake_plan, repo_dir, proposal_dir)
    assert built.model == "a-completely-different-model"
    assert built.max_budget_usd == 42.0


def test_a_profile_that_withholds_the_mctl_tools_does_not_get_them_back(tmp_path, monkeypatch):
    """`plan.tools` is the authoritative allow-list under ADR 007, and a
    profile may narrow it.

    Gating the mctl glob on `MCTL_TOKEN` alone re-grants the tools to a
    profile that deliberately omitted them, whenever the token happens to
    be set — a privilege escalation, not a divergence. Dormant today only
    because the single checked-in fixture always lists `mcp__mctl__*`,
    which is an accident of the fixture rather than a property of the
    design (claude P2 on #234, raised three rounds running).
    """
    monkeypatch.setenv("MCTL_TOKEN", "set-and-therefore-tempting")
    repo_dir = tmp_path / "mctl-telegram"
    repo_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"

    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="e" * 40))
    restricted = dataclasses.replace(
        plan, tools=tuple(t for t in plan.tools if t != "mcp__mctl__*")
    )

    built = options.build_issue_investigator_options_from_plan(restricted, repo_dir, proposal_dir)

    assert not [t for t in built.allowed_tools if t.startswith("mcp__mctl__")]


def test_a_profile_that_grants_the_mctl_tools_still_gets_them(tmp_path, monkeypatch):
    """The other half of the conjunction — narrowing must not become a
    blanket drop. A profile that lists `mcp__mctl__*` with MCP configured
    keeps it, exactly as the legacy builder does."""
    monkeypatch.setenv("MCTL_TOKEN", "set")
    repo_dir = tmp_path / "mctl-telegram"
    repo_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"

    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="f" * 40))
    assert "mcp__mctl__*" in plan.tools  # guards the premise of the test above

    built = options.build_issue_investigator_options_from_plan(plan, repo_dir, proposal_dir)

    assert "mcp__mctl__*" in built.allowed_tools
