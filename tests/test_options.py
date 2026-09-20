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

import pytest

from orchestrator import options, resolver

# Every drain sub-deadline (mctl-agents#366/#368). They share the
# `_positive_seconds` clamp, so the clamp and env-name tests below are one
# property of that helper's callers rather than three separate facts — a fourth
# knob belongs here the moment it is added.
_DRAIN_TIMEOUT_ENV_VARS = (
    "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS",
    "SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS",
    "ISSUE_INVESTIGATOR_DRAIN_TIMEOUT_SECONDS",
)


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
    # hooks too: build_issue_investigator_options_from_plan's docstring lists it
    # among the structural fields "the equivalence tests in tests/test_options.py
    # assert this directly", and it was the one field that claim did not cover.
    # It is also load-bearing, not cosmetic — hooks are what make the SDK hold
    # the CLI open past a result frame (mctl-agents#366), so dropping them from
    # the declarative builder alone would turn every delegating investigation in
    # that mode into a stream that ends with a live task.
    assert declarative.hooks == legacy.hooks


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


@pytest.mark.parametrize("name", _DRAIN_TIMEOUT_ENV_VARS)
def test_drain_timeout_clamps_a_non_positive_env_value(monkeypatch, capsys, name):
    """A bad value must be loud and harmless, not silent and unbounded.

    `move_on_after(0)` cancels before the first read, so a drain deadline of 0
    orphans every delegating run. Raising at the point of use would be worse:
    the error surfaces only once a sub-agent is launched, gets swallowed into a
    generic exit 1, and the shepherd classifies that transient — the one arm
    with no counter at all. A typo in one env var would then re-clone the repo
    and re-run a paid SDK call every tick forever, which is precisely what
    MAX_HARNESS_FAILURES exists to prevent.

    Parametrised over all three knobs rather than the implementer's alone: they
    read through the same `_positive_seconds` clamp, so this is one property of
    that helper's callers, and a fourth knob added with a bare
    `float(os.getenv(...))` should fail here rather than reintroduce the
    pathology under a new name. The blast radius does differ — the
    service-agent and issue-investigator do not go through the shepherd's
    classification — but the clamp is the right shape for all of them.
    """
    import importlib

    # "nan" is the one that matters: `value <= 0` is False for nan, so a naive
    # guard passes it straight to move_on_after(), whose deadline is then nan,
    # whose every `deadline <= now` test is False — the scope never cancels and
    # the sub-deadline is silently gone. inf disables it the same way.
    for bad in ("0", "-5", "not-a-number", "nan", "inf", "-inf"):
        monkeypatch.setenv(name, bad)
        reloaded = importlib.reload(options)
        try:
            assert getattr(reloaded, name) == 300.0, bad
            assert name in capsys.readouterr().err
        finally:
            monkeypatch.delenv(name, raising=False)
            importlib.reload(options)


def test_the_other_two_drain_timeouts_default_to_five_minutes():
    """Same sub-deadline, same default, for the two drivers #368 converted."""
    assert options.SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS == 300.0
    assert options.ISSUE_INVESTIGATOR_DRAIN_TIMEOUT_SECONDS == 300.0


# ---------------------------------------------------------------------------
# Work-class-derived execution envelope (mctl-agents#423) — T3
# ---------------------------------------------------------------------------
def test_implementer_envelope_review_class_is_the_base_unconditionally():
    assert options.implementer_envelope("review") == options.IMPLEMENTER_TIMEOUT_SECONDS
    assert options.implementer_envelope("review", n_checks=99) == options.IMPLEMENTER_TIMEOUT_SECONDS
    assert options.implementer_envelope("something-else") == options.IMPLEMENTER_TIMEOUT_SECONDS


def test_implementer_envelope_ci_class_widens_per_check_and_caps_at_the_ceiling(monkeypatch):
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_SECONDS", 900.0)
    monkeypatch.setattr(options, "IMPLEMENTER_CI_ANALYSIS_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_CEILING_SECONDS", 1800.0)

    assert options.implementer_envelope("ci-remediation", n_checks=0) == 900.0
    assert options.implementer_envelope("ci-remediation", n_checks=1) == 1020.0
    assert options.implementer_envelope("ci-remediation", n_checks=3) == 1260.0
    assert options.implementer_envelope("mixed", n_checks=3) == 1260.0

    # A per-check analysis budget large enough to exceed the ceiling even at
    # the (CI_LOG_MAX_CHECKS-capped) check count: capped, not extrapolated.
    monkeypatch.setattr(options, "IMPLEMENTER_CI_ANALYSIS_SECONDS", 1000.0)
    assert options.implementer_envelope("ci-remediation", n_checks=3) == 1800.0


def test_implementer_envelope_caps_n_checks_at_ci_log_max_checks(monkeypatch):
    """A bundle claiming more checks than fetch_failure_logs() would ever
    retrieve must not widen the envelope past what CI_LOG_MAX_CHECKS funds."""
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_SECONDS", 900.0)
    monkeypatch.setattr(options, "IMPLEMENTER_CI_ANALYSIS_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_CEILING_SECONDS", 100000.0)

    at_cap = options.implementer_envelope("ci-remediation", n_checks=options.CI_LOG_MAX_CHECKS)
    beyond_cap = options.implementer_envelope("ci-remediation", n_checks=options.CI_LOG_MAX_CHECKS + 50)
    assert at_cap == beyond_cap


@pytest.mark.parametrize("name", (
    "IMPLEMENTER_CI_ANALYSIS_SECONDS",
    "IMPLEMENTER_TIMEOUT_CEILING_SECONDS",
    "IMPLEMENTER_MUTATION_RESERVE_SECONDS",
    "IMPLEMENTER_TEARDOWN_GRACE_SECONDS",
))
def test_new_budget_knobs_clamp_hostile_env_values(monkeypatch, capsys, name):
    """Same `_positive_seconds` contract as the pre-existing drain knobs."""
    import importlib

    defaults = {
        "IMPLEMENTER_CI_ANALYSIS_SECONDS": 120.0,
        "IMPLEMENTER_TIMEOUT_CEILING_SECONDS": 1800.0,
        "IMPLEMENTER_MUTATION_RESERVE_SECONDS": 180.0,
        "IMPLEMENTER_TEARDOWN_GRACE_SECONDS": 15.0,
    }
    for bad in ("0", "-5", "not-a-number", "nan", "inf", "-inf"):
        monkeypatch.setenv(name, bad)
        reloaded = importlib.reload(options)
        try:
            assert getattr(reloaded, name) == defaults[name], bad
            assert name in capsys.readouterr().err
        finally:
            monkeypatch.delenv(name, raising=False)
            importlib.reload(options)


def test_new_budget_knobs_honour_their_env_override(monkeypatch):
    import importlib

    monkeypatch.setenv("IMPLEMENTER_CI_ANALYSIS_SECONDS", "42")
    monkeypatch.setenv("IMPLEMENTER_TIMEOUT_CEILING_SECONDS", "4200")
    monkeypatch.setenv("IMPLEMENTER_MUTATION_RESERVE_SECONDS", "99")
    monkeypatch.setenv("IMPLEMENTER_TEARDOWN_GRACE_SECONDS", "7")
    reloaded = importlib.reload(options)
    try:
        assert reloaded.IMPLEMENTER_CI_ANALYSIS_SECONDS == 42.0
        assert reloaded.IMPLEMENTER_TIMEOUT_CEILING_SECONDS == 4200.0
        assert reloaded.IMPLEMENTER_MUTATION_RESERVE_SECONDS == 99.0
        assert reloaded.IMPLEMENTER_TEARDOWN_GRACE_SECONDS == 7.0
    finally:
        for name in (
            "IMPLEMENTER_CI_ANALYSIS_SECONDS", "IMPLEMENTER_TIMEOUT_CEILING_SECONDS",
            "IMPLEMENTER_MUTATION_RESERVE_SECONDS", "IMPLEMENTER_TEARDOWN_GRACE_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(options)


# ---------------------------------------------------------------------------
# Budget-contract invariant (mctl-agents#423) — T8
# ---------------------------------------------------------------------------
def test_validate_budget_contract_holds_for_every_work_class_at_defaults():
    """With the shipped defaults, no work class needs clamping at all."""
    required = 2 * options.IMPLEMENTER_DRAIN_TIMEOUT_SECONDS + options.IMPLEMENTER_MUTATION_RESERVE_SECONDS
    for work_class in ("review", "ci-remediation", "mixed"):
        envelope = options.implementer_envelope(work_class, n_checks=options.CI_LOG_MAX_CHECKS)
        assert envelope >= required, work_class


def test_validate_budget_contract_clamps_the_drain_when_the_envelope_is_too_tight(monkeypatch, capsys):
    """A pathologically small ceiling must clamp the drain sub-budget rather
    than silently leaving no mutation reserve."""
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_CEILING_SECONDS", 10.0)
    monkeypatch.setattr(options, "IMPLEMENTER_CI_ANALYSIS_SECONDS", 0.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MUTATION_RESERVE_SECONDS", 2.0)
    monkeypatch.setattr(options, "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", 300.0)

    options.validate_budget_contract()

    # envelope (10) < 2*300+2 -> clamp: (10 - 2) / 2 = 4.0
    assert options.IMPLEMENTER_DRAIN_TIMEOUT_SECONDS == 4.0
    assert "clamping IMPLEMENTER_DRAIN_TIMEOUT_SECONDS" in capsys.readouterr().err


def test_review_claim_lease_covers_the_widest_possible_envelope(monkeypatch):
    """T8's second half: the lease must never be outlived by the widest
    envelope any work class could select, plus clone/push time."""
    from orchestrator import run_implementer

    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_CEILING_SECONDS", 1800.0)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)

    lease = run_implementer._review_claim_lease_default()
    widest = options.implementer_envelope("ci-remediation", n_checks=options.CI_LOG_MAX_CHECKS)
    assert lease.total_seconds() >= widest + 2 * run_implementer.IMPLEMENTER_COMMAND_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# CI-log guard hook (mctl-agents#423) — T6 / T7 (containment)
# ---------------------------------------------------------------------------
def test_ci_log_guard_hook_denies_gh_run_view_log_failed():
    import anyio

    result = anyio.run(
        options._ci_log_guard_hook,
        {"tool_name": "Bash", "tool_input": {"command": "gh run view 123 --log-failed"}},
        None,
        None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "bounded" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_ci_log_guard_hook_denies_gh_api_logs_route():
    import anyio

    result = anyio.run(
        options._ci_log_guard_hook,
        {"tool_name": "Bash", "tool_input": {"command": "gh api repos/o/r/actions/jobs/1/logs"}},
        None, None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ci_log_guard_hook_denies_curl_and_wget_of_a_logs_url():
    import anyio

    for cmd in (
        "curl -sL https://example.com/actions/runs/1/logs -o out.txt",
        "wget https://example.com/actions/runs/1/logs",
    ):
        result = anyio.run(
            options._ci_log_guard_hook,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            None, None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


def test_ci_log_guard_hook_allows_ordinary_commands():
    import anyio

    for cmd in ("pytest -q", "npm test", "git status", "gh pr view 1", "gh run view 123"):
        result = anyio.run(
            options._ci_log_guard_hook,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            None, None,
        )
        assert result == {}, cmd


def test_ci_log_guard_hook_ignores_non_bash_tools():
    import anyio

    result = anyio.run(
        options._ci_log_guard_hook,
        {"tool_name": "Read", "tool_input": {"file_path": "x"}},
        None, None,
    )
    assert result == {}


def test_build_implementer_agent_options_installs_the_guard_hook_for_ci_classes(tmp_path):
    for work_class in ("ci-remediation", "mixed"):
        built = options.build_implementer_agent_options(tmp_path, "test-model", work_class=work_class)
        matchers = (built.hooks or {}).get("PreToolUse") or []
        callbacks = [h for m in matchers for h in m.hooks]
        assert options._ci_log_guard_hook in callbacks, work_class
        # Composed WITH, not instead of, the audit hook.
        assert options._audit_pre_tool_use in callbacks, work_class


def test_build_implementer_agent_options_omits_the_guard_hook_for_review(tmp_path):
    built = options.build_implementer_agent_options(tmp_path, "test-model", work_class="review")
    matchers = (built.hooks or {}).get("PreToolUse") or []
    callbacks = [h for m in matchers for h in m.hooks]
    assert options._ci_log_guard_hook not in callbacks
    assert options._audit_pre_tool_use in callbacks
    # Default work_class (no kwarg at all) matches "review" — every pre-#423 caller.
    default_built = options.build_implementer_agent_options(tmp_path, "test-model")
    default_callbacks = [h for m in ((default_built.hooks or {}).get("PreToolUse") or []) for h in m.hooks]
    assert options._ci_log_guard_hook not in default_callbacks


def test_ci_builder_still_keeps_hooks_truthy_for_the_366_drain(tmp_path):
    """The guard hook must be composed WITH _command_audit_hooks(), never
    replace it — subagent_wait.drain_until_settled's precondition is that
    `hooks` stays truthy on every drainable driver."""
    built = options.build_implementer_agent_options(tmp_path, "test-model", work_class="ci-remediation")
    assert built.hooks


@pytest.mark.parametrize("name", _DRAIN_TIMEOUT_ENV_VARS)
def test_every_drain_timeout_honours_its_env_override(monkeypatch, name):
    """Pins the env-var NAME, which the driver tests cannot.

    They monkeypatch the module attribute, so the value is exercised but the
    `os.getenv` string never is. A typo there would be silent in exactly the
    worst way: the knob documents itself as tunable while doing nothing, and
    you find out when you raise it during an incident and nothing changes.
    """
    import importlib

    monkeypatch.setenv(name, "42")
    reloaded = importlib.reload(options)
    try:
        assert getattr(reloaded, name) == 42.0
    finally:
        monkeypatch.delenv(name, raising=False)
        importlib.reload(options)
