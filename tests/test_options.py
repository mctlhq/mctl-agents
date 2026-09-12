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


def test_implementer_drain_timeout_defaults_to_five_minutes():
    """Sub-deadline for awaiting an async-launched sub-agent (mctl-agents#366).

    Nested inside IMPLEMENTER_TIMEOUT_SECONDS on purpose, and strictly shorter:
    if a wedged child were allowed to eat the whole outer budget the run would
    surface as a plain operation timeout (exit 44), which the shepherd counts as
    deterministic and charges to the proposal — the very bug #366 fixes.
    """
    assert options.IMPLEMENTER_DRAIN_TIMEOUT_SECONDS == 300.0
    assert options.IMPLEMENTER_DRAIN_TIMEOUT_SECONDS < options.IMPLEMENTER_TIMEOUT_SECONDS


def test_implementer_drain_timeout_honours_its_env_override(monkeypatch):
    import importlib

    monkeypatch.setenv("IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", "42")
    reloaded = importlib.reload(options)
    try:
        assert reloaded.IMPLEMENTER_DRAIN_TIMEOUT_SECONDS == 42.0
    finally:
        monkeypatch.delenv("IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", raising=False)
        importlib.reload(options)


def test_hooks_are_what_hold_stdin_open_for_every_drainable_builder(tmp_path, monkeypatch):
    """Load-bearing precondition for the #366 drain, not incidental config.

    claude_agent_sdk only keeps stdin open past a result frame with tasks in
    flight when `sdk_mcp_servers or hooks` is truthy. Strip a mode's hooks and
    the CLI would exit at the first result, so awaiting the sub-agent would
    block on a dead child instead of recovering its work.

    And it must be the HOOKS carrying it, not the MCP config: the SDK lifts a
    server into `sdk_mcp_servers` only when its config says `type: "sdk"`
    (`_internal/client.py`), and mctl_mcp_config() emits `type: "http"`.
    Asserting both halves keeps the test from passing for the wrong reason, and
    makes it demand an update the day an sdk-type server does appear.

    MCTL_TOKEN is set so mcp_servers is actually populated — without it the
    config is `{}` and the "no sdk-type server" half is vacuously true, which
    is the specific way this test could rot into a no-op.

    One test over every builder rather than one per driver: the property is a
    single fact about the SDK, and stating it once is what keeps the drivers
    that drain (implementer #367, issue-investigator and service-agent #368)
    from drifting apart from the one that could (incident-responder, which has
    the precondition but nothing to delegate to today).
    """
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    repo_dir = tmp_path / "mctl-telegram"
    repo_dir.mkdir()
    incident_dir = tmp_path / "_incident-responder"
    incident_dir.mkdir()
    proposal_dir = tmp_path / "proposals" / "issue-123"

    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="e" * 40))
    builders = {
        "implementer": options.build_implementer_agent_options(repo_dir, "test-model"),
        "service-agent": options.build_service_agent_options(repo_dir, "test-model"),
        "incident-responder": options.build_incident_responder_options(
            agent_dir=incident_dir, model="test-model", state_dir=tmp_path,
        ),
        "issue-investigator": options.build_issue_investigator_options(
            repo_dir, model="test-model", proposal_dir=proposal_dir,
        ),
        "issue-investigator/from_plan": options.build_issue_investigator_options_from_plan(
            plan, repo_dir, proposal_dir,
        ),
    }
    for name, built in builders.items():
        assert built.hooks, f"{name} lost the hooks the #366 drain relies on"
        assert built.mcp_servers, f"{name}: expected the http mctl server to be configured"
        assert not [
            server for server, cfg in built.mcp_servers.items()
            if isinstance(cfg, dict) and cfg.get("type") == "sdk"
        ], f"{name}: an sdk-type server would also satisfy the precondition — update this test"


def test_the_mentor_has_neither_hooks_nor_an_sdk_mcp_server(tmp_path, monkeypatch):
    """The negative result of the #368 audit, made executable.

    `build_mentor_options` passes `mcp_servers=` but NO hooks, and the mctl
    server it passes is `type: "http"` — which the SDK does not lift into
    `sdk_mcp_servers`. So `sdk_mcp_servers or hooks` is falsy and the SDK
    closes stdin at the first result frame: the CLI exits, the stream ends, and
    there is no child left alive to drain toward.

    This test exists to stop the #366 pattern being copied here on the
    assumption that "it has an MCP server, so it qualifies". It does not.
    Draining run_mentor as it stands would convert a silent loss into a
    guaranteed OrphanedSubagentError on every delegating run. Giving the mentor
    the audit hooks to make it drainable is a real behaviour change to a mode
    that does not delegate today — a deliberate decision, not a refactor, and
    if it is ever taken this test is the thing that must change with it.
    """
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    mentor_dir = tmp_path / "_mentor"
    mentor_dir.mkdir()

    built = options.build_mentor_options(mentor_dir, "test-model")

    assert not built.hooks, (
        "the mentor grew hooks — it is now drainable, which is a deliberate "
        "behaviour change; see mctl-agents#366/#368 before updating this test"
    )
    assert built.mcp_servers, "expected the http mctl server to be configured"
    assert not [
        server for server, cfg in built.mcp_servers.items()
        if isinstance(cfg, dict) and cfg.get("type") == "sdk"
    ], "an sdk-type server WOULD satisfy the precondition — the mentor is now drainable"


def test_drain_timeout_clamps_a_non_positive_env_value(monkeypatch, capsys):
    """A bad value must be loud and harmless, not silent and unbounded.

    `move_on_after(0)` cancels before the first read, so a drain deadline of 0
    orphans every delegating run. Raising at the point of use would be worse:
    the error surfaces only once a sub-agent is launched, gets swallowed into a
    generic exit 1, and the shepherd classifies that transient — the one arm
    with no counter at all. A typo in one env var would then re-clone the repo
    and re-run a paid SDK call every tick forever, which is precisely what
    MAX_HARNESS_FAILURES exists to prevent.
    """
    import importlib

    # "nan" is the one that matters: `value <= 0` is False for nan, so a naive
    # guard passes it straight to move_on_after(), whose deadline is then nan,
    # whose every `deadline <= now` test is False — the scope never cancels and
    # the sub-deadline is silently gone. inf disables it the same way.
    for bad in ("0", "-5", "not-a-number", "nan", "inf", "-inf"):
        monkeypatch.setenv("IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", bad)
        reloaded = importlib.reload(options)
        try:
            assert reloaded.IMPLEMENTER_DRAIN_TIMEOUT_SECONDS == 300.0, bad
            assert "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS" in capsys.readouterr().err
        finally:
            monkeypatch.delenv("IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", raising=False)
            importlib.reload(options)
