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
from pathlib import Path

import pytest

from orchestrator import options, resolver

_IDENTITY_FIXTURE = str(Path(__file__).parent / "fixtures" / "identity" / "investigator-context.json")


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


def test_ci_log_guard_hook_denies_gh_run_view_with_a_global_repo_flag():
    """mctl-agents#423 fix-forward: verified Agy P2 on PR #425's merged head.

    `-R owner/repo` (and `--repo`/`--repo=owner/repo`) sit between `gh` and
    `run`, which used to break the `\\bgh\\s+run\\s+view\\b` anchor and let
    the fetch through undenied. Each form must still be denied.
    """
    import anyio

    for cmd in (
        "gh -R o/r run view 123 --log-failed",
        "gh --repo o/r run view 123 --log-failed",
        "gh --repo=o/r run view 123 --log-failed",
        "gh -R o/r api repos/o/r/actions/jobs/1/logs",
    ):
        result = anyio.run(
            options._ci_log_guard_hook,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            None, None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


def test_ci_log_guard_hook_denies_a_line_continued_gh_run_view():
    """mctl-agents#423 fix-forward: verified Agy P2 on PR #425's merged head.

    A trailing `\\` before the newline is a shell line continuation — the
    shell joins the lines before running the command — but the deny
    pattern's `[^&|;\\n]*` used to stop at the literal `\\n` in the tool-call
    string, before ever reaching `--log-failed`.
    """
    import anyio

    result = anyio.run(
        options._ci_log_guard_hook,
        {"tool_name": "Bash", "tool_input": {"command": "gh run view \\\n  123 --log-failed"}},
        None, None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_ci_log_guard_hook_denies_gh_run_view_with_odd_flag_spacing():
    """mctl-agents#423 fix-forward, round 2: verified Agy P2 on this PR.

    `[=\\s]\\S+` treats `=`/whitespace as a single interchangeable
    character, so two spaces (or an attached shorthand value with none)
    between a global flag and its argument left the argument unconsumed and
    broke the `run\\s+view` anchor downstream. Each form must still deny.
    """
    import anyio

    for cmd in (
        "gh -R  o/r run view 123 --log-failed",
        "gh -Ro/r run view 123 --log-failed",
        "gh --repo  o/r run view 123 --log-failed",
    ):
        result = anyio.run(
            options._ci_log_guard_hook,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            None, None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


def test_ci_log_guard_hook_denies_a_mid_token_line_continuation():
    """mctl-agents#423 fix-forward, round 2: verified Agy P2 on this PR.

    A continuation with no whitespace on either side (`vi\\\\\\new`) is
    POSIX line-continuation *inside* a token — the shell rejoins it with
    nothing inserted. Collapsing it to a space instead mutated `view` into
    `vi ew`, which no longer matches `run\\s+view` and slipped past.
    """
    import anyio

    for cmd in (
        "gh run vi\\\new 123 --log-failed",
        "gh run view 123 --log-\\\nfailed",
    ):
        result = anyio.run(
            options._ci_log_guard_hook,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            None, None,
        )
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", cmd


def test_ci_log_guard_hook_still_allows_unrelated_gh_commands_with_global_flags():
    """Skipping `gh` global flags to find the subcommand must not turn into
    over-matching unrelated commands that merely happen to carry a flag."""
    import anyio

    for cmd in (
        "gh -R o/r pr view 1",
        "gh --repo o/r issue list",
        "gh --hostname example.com run view 123",
    ):
        result = anyio.run(
            options._ci_log_guard_hook,
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            None, None,
        )
        assert result == {}, cmd


def test_normalize_shell_command_joins_continuations_but_keeps_real_boundaries():
    assert options._normalize_shell_command("gh run view \\\n  123") == "gh run view  123"
    assert options._normalize_shell_command("echo a\necho b") == "echo a\necho b"


def test_normalize_shell_command_rejoins_a_mid_token_continuation_with_no_space():
    """A continuation with no whitespace on either side must vanish entirely
    (POSIX semantics), not become a space that splits the token in two."""
    assert options._normalize_shell_command("gh run vi\\\new 123") == "gh run view 123"
    assert options._normalize_shell_command("--log-\\\nfailed") == "--log-failed"


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


def test_plan_grants_human_input_tracks_the_capability_in_plan_tools():
    """`plan_grants_human_input` has no caller yet (the producer lands with
    mctl-gitops#1277); pin its contract directly so it does not rot as dead
    code in the meantime."""
    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="a" * 40))

    granted = dataclasses.replace(
        plan, tools=tuple(plan.tools) + (options.HUMAN_INPUT_CAPABILITY,)
    )
    assert options.plan_grants_human_input(granted) is True

    ungranted = dataclasses.replace(
        plan, tools=tuple(t for t in plan.tools if t != options.HUMAN_INPUT_CAPABILITY)
    )
    assert options.plan_grants_human_input(ungranted) is False


# ---------------------------------------------------------------------------
# Per-command execution budget (mctl-agents#430)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", (
    "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS",
    "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS",
    "IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS",
))
def test_command_budget_knobs_clamp_hostile_env_values(monkeypatch, capsys, name):
    import importlib

    defaults = {
        "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS": 120.0,
        "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS": 20.0,
        "IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS": 5.0,
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


def test_command_budget_knobs_honour_their_env_override(monkeypatch):
    import importlib

    monkeypatch.setenv("IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", "99")
    monkeypatch.setenv("IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", "7")
    monkeypatch.setenv("IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS", "3")
    reloaded = importlib.reload(options)
    try:
        assert reloaded.IMPLEMENTER_TEARDOWN_RESERVE_SECONDS == 99.0
        assert reloaded.IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS == 7.0
        assert reloaded.IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS == 3.0
    finally:
        for name in (
            "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS",
            "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS",
            "IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        importlib.reload(options)


def test_bound_commands_break_glass_defaults_true_and_honours_0(monkeypatch):
    import importlib

    assert options.IMPLEMENTER_BOUND_COMMANDS is True
    monkeypatch.setenv("IMPLEMENTER_BOUND_COMMANDS", "0")
    reloaded = importlib.reload(options)
    try:
        assert reloaded.IMPLEMENTER_BOUND_COMMANDS is False
    finally:
        monkeypatch.delenv("IMPLEMENTER_BOUND_COMMANDS", raising=False)
        importlib.reload(options)


def test_validate_budget_contract_clamps_the_teardown_reserve_when_the_envelope_is_too_tight(
    monkeypatch, capsys,
):
    """The second assertion, kept separate from the drain-reserve one: on a
    tight envelope it clamps IMPLEMENTER_TEARDOWN_RESERVE_SECONDS, never the
    ceiling or the mutation reserve."""
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TIMEOUT_CEILING_SECONDS", 10.0)
    monkeypatch.setattr(options, "IMPLEMENTER_CI_ANALYSIS_SECONDS", 0.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MUTATION_RESERVE_SECONDS", 2.0)
    monkeypatch.setattr(options, "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 1.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)

    options.validate_budget_contract()

    # envelope (10) < 120+1+2 -> clamp: 10 - 1 - 2 = 7.0
    assert options.IMPLEMENTER_TEARDOWN_RESERVE_SECONDS == 7.0
    assert "clamping IMPLEMENTER_TEARDOWN_RESERVE_SECONDS" in capsys.readouterr().err


def test_validate_budget_contract_holds_the_second_assertion_for_every_work_class_at_defaults():
    required = (
        options.IMPLEMENTER_TEARDOWN_RESERVE_SECONDS
        + options.IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS
        + options.IMPLEMENTER_MUTATION_RESERVE_SECONDS
    )
    for work_class in ("review", "ci-remediation", "mixed"):
        envelope = options.implementer_envelope(work_class, n_checks=options.CI_LOG_MAX_CHECKS)
        assert envelope >= required, work_class


# ---------------------------------------------------------------------------
# The deadline guard (mctl-agents#430) — T4 / T5
# ---------------------------------------------------------------------------
def _guard_callback(built):
    matchers = (built.hooks or {}).get("PreToolUse") or []
    callbacks = [h for m in matchers for h in m.hooks]
    # The audit hook and (optionally) the CI-log guard are plain functions;
    # the deadline guard is the one callback that is a closure, not either
    # of those two module-level functions.
    for cb in callbacks:
        if cb not in (options._audit_pre_tool_use, options._ci_log_guard_hook):
            return cb
    raise AssertionError("no deadline-guard callback found among the composed hooks")


def test_build_implementer_agent_options_omits_the_guard_without_both_params(tmp_path):
    """Byte-identical to today's behaviour when either is missing."""
    only_deadline = options.build_implementer_agent_options(
        tmp_path, "test-model", deadline_monotonic=100.0,
    )
    only_ledger = options.build_implementer_agent_options(
        tmp_path, "test-model", budget_ledger=options.CommandBudgetLedger(),
    )
    neither = options.build_implementer_agent_options(tmp_path, "test-model")
    for built in (only_deadline, only_ledger, neither):
        matchers = (built.hooks or {}).get("PreToolUse") or []
        callbacks = [h for m in matchers for h in m.hooks]
        assert callbacks == [options._audit_pre_tool_use]


def test_build_implementer_agent_options_installs_the_guard_for_every_work_class(tmp_path):
    """The defect is generic over work class, not CI-remediation-only."""
    for work_class in ("review", "ci-remediation", "mixed"):
        built = options.build_implementer_agent_options(
            tmp_path, "test-model", work_class=work_class,
            deadline_monotonic=anyio_current_time_stub(),
            budget_ledger=options.CommandBudgetLedger(),
        )
        _guard_callback(built)  # raises if absent


def anyio_current_time_stub() -> float:
    # `command_budget()` is called with `anyio.current_time()` inside the
    # hook (an asyncio-backend event loop), which is `time.monotonic()`
    # under the hood -- calling `time.monotonic()` directly here, outside any
    # event loop, lands on the same clock without needing a running loop.
    import time

    return time.monotonic()


def _run_guard(cb, tool_input: dict, tool_name: str = "Bash"):
    import anyio

    return anyio.run(cb, {"tool_name": tool_name, "tool_input": tool_input}, None, None)


def test_deadline_guard_denies_run_in_background_true(tmp_path):
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model",
        deadline_monotonic=anyio_current_time_stub() + 1000.0,
        budget_ledger=ledger,
    )
    cb = _guard_callback(built)
    result = _run_guard(cb, {"command": "pytest -q", "run_in_background": True})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert ledger.denied_background == 1


def test_deadline_guard_denies_each_detachment_form_with_a_reason_naming_it(tmp_path):
    for command in (
        "go test -race ./... > /tmp/test-race.log 2>&1 &",
        "nohup ./run.sh",
        "setsid ./run.sh",
        "./run.sh & disown",
    ):
        ledger = options.CommandBudgetLedger()
        built = options.build_implementer_agent_options(
            tmp_path, "test-model",
            deadline_monotonic=anyio_current_time_stub() + 1000.0,
            budget_ledger=ledger,
        )
        cb = _guard_callback(built)
        result = _run_guard(cb, {"command": command})
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny", command
        assert ledger.denied_background == 1, command


def test_deadline_guard_allows_an_ordinary_command_with_a_derived_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model",
        deadline_monotonic=anyio_current_time_stub() + 1000.0,
        budget_ledger=ledger,
    )
    cb = _guard_callback(built)
    result = _run_guard(cb, {"command": "pytest -q"})
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["timeout"] <= 300 * 1000
    assert out["updatedInput"]["timeout"] > 0
    assert "timeout" in out["updatedInput"]["command"]  # rewritten under `timeout`
    assert ledger.clamped == 1


def test_deadline_guard_never_widens_a_caller_supplied_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model",
        deadline_monotonic=anyio_current_time_stub() + 1000.0,
        budget_ledger=ledger,
    )
    cb = _guard_callback(built)
    # Caller already asked for a much narrower 5s timeout (5000ms).
    result = _run_guard(cb, {"command": "pytest -q", "timeout": 5000})
    assert result["hookSpecificOutput"]["updatedInput"]["timeout"] == 5000


def test_deadline_guard_denies_once_the_budget_floor_is_crossed(tmp_path, monkeypatch):
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model",
        # Only 10s left in total -- less than the 120s reserve alone.
        deadline_monotonic=anyio_current_time_stub() + 10.0,
        budget_ledger=ledger,
    )
    cb = _guard_callback(built)
    result = _run_guard(cb, {"command": "pytest -q"})
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "verification_budget_exhausted" in out["permissionDecisionReason"]
    assert ledger.denied_exhausted == 1
    assert ledger.exhausted is True


def test_deadline_guard_leaves_non_bash_tools_untouched(tmp_path):
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model",
        deadline_monotonic=anyio_current_time_stub() + 1000.0,
        budget_ledger=ledger,
    )
    cb = _guard_callback(built)
    result = _run_guard(cb, {"file_path": "x"}, tool_name="Read")
    assert result == {}
    assert ledger.clamped == 0


def test_deadline_guard_falls_back_to_clamp_only_when_timeout_is_unavailable(tmp_path, monkeypatch):
    """IF the `timeout` binary is unavailable THEN the command is admitted
    with a narrowed tool-input timeout but NOT rewritten under `timeout`."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model",
        deadline_monotonic=anyio_current_time_stub() + 1000.0,
        budget_ledger=ledger,
        timeout_available=False,
    )
    cb = _guard_callback(built)
    result = _run_guard(cb, {"command": "pytest -q"})
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["command"] == "pytest -q"


def test_deadline_guard_composes_with_the_ci_log_guard_and_the_audit_hook(tmp_path):
    """`hooks` stays truthy and every guard is present -- subagent_wait's
    drain precondition, and the CI-log guard (#423) must not be dropped."""
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model", work_class="ci-remediation",
        deadline_monotonic=anyio_current_time_stub() + 1000.0,
        budget_ledger=ledger,
    )
    matchers = (built.hooks or {}).get("PreToolUse") or []
    callbacks = [h for m in matchers for h in m.hooks]
    assert options._audit_pre_tool_use in callbacks
    assert options._ci_log_guard_hook in callbacks
    assert len(callbacks) == 3  # audit + ci-log + deadline


def _os_bound_seconds(rendered: str) -> float:
    """The `<budget>s` GNU `timeout` was rendered with (may be fractional)."""
    import re as _re

    found = _re.search(r"timeout --kill-after=\d+s ([\d.]+)s bash -c ", rendered)
    assert found is not None, rendered
    return float(found.group(1))


def _guard_for(tmp_path, ledger, *, work_class="review", remaining=1000.0):
    built = options.build_implementer_agent_options(
        tmp_path, "test-model", work_class=work_class,
        deadline_monotonic=anyio_current_time_stub() + remaining,
        budget_ledger=ledger,
    )
    return _guard_callback(built)


def test_deadline_guard_bounds_the_wrapper_at_the_caller_timeout_not_the_envelope(
    tmp_path, monkeypatch
):
    """agy P2 on `630ac27`: deriving the OS bound from the ENVELOPE while the
    CLI's own tool timeout is much narrower reopens the orphaned-background
    defect -- the CLI backgrounds at 5s, `timeout` waits 300s, and the
    process escapes for the 295s in between."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    cb = _guard_for(tmp_path, ledger)
    result = _run_guard(cb, {"command": "sleep 20", "timeout": 5000})
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["timeout"] == 5000
    # The OS-level bound comes from the SAME 5s, not the 300s ceiling -- and
    # lands strictly BELOW it so GNU `timeout` fires before the CLI's own
    # timer (agy P2 on `624a433`).
    assert "timeout --kill-after=5s 4s bash -c" in out["updatedInput"]["command"]
    assert "300s" not in out["updatedInput"]["command"]
    assert ledger.last_bound_s == 5.0
    assert _os_bound_seconds(out["updatedInput"]["command"]) * 1000 < out["updatedInput"]["timeout"]


def test_deadline_guard_still_uses_the_envelope_bound_without_a_caller_timeout(
    tmp_path, monkeypatch
):
    """The narrowing is `min`, so an absent (or non-positive) caller timeout
    leaves the envelope-derived bound in force."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    for tool_input in ({"command": "pytest -q"}, {"command": "pytest -q", "timeout": 0}):
        ledger = options.CommandBudgetLedger()
        cb = _guard_for(tmp_path, ledger)
        out = _run_guard(cb, dict(tool_input))["hookSpecificOutput"]
        assert out["updatedInput"]["timeout"] == 300 * 1000, tool_input
        assert "timeout --kill-after=5s 299s bash -c" in out["updatedInput"]["command"]
        assert (
            _os_bound_seconds(out["updatedInput"]["command"]) * 1000
            < out["updatedInput"]["timeout"]
        ), tool_input


def test_the_os_bound_always_fires_before_the_cli_tool_timeout(tmp_path, monkeypatch):
    """The ordering invariant itself, across the whole useful budget range.

    If the CLI's timer wins, it BACKGROUNDS the still-live command (ADR-011
    "Containment") and the orphaned-process window reopens -- which is the
    single thing this guard exists to prevent (agy P2 on `624a433`)."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    for caller_timeout_ms in (None, 20_600, 25_000, 60_000, 299_000, 1_000_000):
        ledger = options.CommandBudgetLedger()
        cb = _guard_for(tmp_path, ledger)
        tool_input = {"command": "pytest -q"}
        if caller_timeout_ms is not None:
            tool_input["timeout"] = caller_timeout_ms
        out = _run_guard(cb, tool_input)["hookSpecificOutput"]
        rendered = out["updatedInput"]["command"]
        assert rendered.startswith("timeout --kill-after="), caller_timeout_ms
        assert (
            _os_bound_seconds(rendered) * 1000 < out["updatedInput"]["timeout"]
        ), caller_timeout_ms
        # And the clamp is still one-directional.
        if caller_timeout_ms is not None:
            assert out["updatedInput"]["timeout"] <= caller_timeout_ms


def test_the_bash_tool_ceiling_binds_the_effective_budget_not_just_the_clamp(
    tmp_path, monkeypatch
):
    """claude P3 on `624a433`: the Bash tool ignores a `timeout` above its own
    ceiling. Clamping only the tool input would leave the OS bound ABOVE the
    CLI's real timer, putting the CLI first again."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 900.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    cb = _guard_for(tmp_path, ledger, remaining=5000.0)
    out = _run_guard(cb, {"command": "pytest -q"})["hookSpecificOutput"]
    assert out["updatedInput"]["timeout"] == options.BASH_TOOL_MAX_TIMEOUT_MS
    assert (
        _os_bound_seconds(out["updatedInput"]["command"]) * 1000
        < out["updatedInput"]["timeout"]
    )


def test_the_ordering_holds_for_a_sub_second_caller_timeout(tmp_path, monkeypatch):
    """The one range where the whole-second grid could not express the
    ordering, so it inverted (claude P3 on `aa60779`)."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    for caller_timeout_ms in (200, 500, 900, 1000):
        ledger = options.CommandBudgetLedger()
        cb = _guard_for(tmp_path, ledger)
        out = _run_guard(
            cb, {"command": "true", "timeout": caller_timeout_ms}
        )["hookSpecificOutput"]
        assert out["updatedInput"]["timeout"] == caller_timeout_ms
        assert (
            _os_bound_seconds(out["updatedInput"]["command"]) * 1000
            < out["updatedInput"]["timeout"]
        ), caller_timeout_ms


def test_deadline_guard_allows_a_quoted_ampersand(tmp_path, monkeypatch):
    """claude P2 on `630ac27`: `&` inside quotes is data, not the async-list
    operator, and denying it blocked ordinary commits."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    cb = _guard_for(tmp_path, ledger)
    result = _run_guard(cb, {"command": 'git commit -m "A & B"'})
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert ledger.denied_background == 0


def test_deadline_guard_denial_reason_quotes_what_tripped_it(tmp_path):
    """A denial the agent cannot act on is a denial it will retry blindly."""
    ledger = options.CommandBudgetLedger()
    cb = _guard_for(tmp_path, ledger)
    result = _run_guard(
        cb, {"command": "go test -race ./... > /tmp/test-race.log 2>&1 &"}
    )
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "test-race.log" in out["permissionDecisionReason"]


def test_deadline_guard_leaves_a_cd_unwrapped_so_it_persists(tmp_path, monkeypatch):
    """claude P2 on `630ac27`: the Bash tool carries the working directory
    across calls; `bash -c` would discard it, so `cd /repo` in one call would
    stop applying to the next."""
    monkeypatch.setattr(options, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(options, "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", 120.0)
    monkeypatch.setattr(options, "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", 20.0)
    ledger = options.CommandBudgetLedger()
    cb = _guard_for(tmp_path, ledger)
    out = _run_guard(cb, {"command": "cd /repo"})["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"]["command"] == "cd /repo"
    # The tool-input clamp still applies -- only the rewrite is skipped.
    assert out["updatedInput"]["timeout"] == 300 * 1000
    # A compound that also runs real work is still bounded.
    out2 = _run_guard(cb, {"command": "cd /repo && go test ./..."})["hookSpecificOutput"]
    assert out2["updatedInput"]["command"].startswith("timeout --kill-after=")


def test_ci_log_deny_survives_the_deadline_guards_allow(tmp_path):
    """claude P2 on `630ac27`: the composition test pinned REGISTRATION only.
    On a ci-remediation run the deadline guard returns `allow` for a `gh run
    view --log-failed` (it is not detached and fits the budget), so nothing
    but #423's own deny stands between the agent and an unbounded CI-log
    fetch. Pin that at least one composed hook still DENIES it."""
    ledger = options.CommandBudgetLedger()
    built = options.build_implementer_agent_options(
        tmp_path, "test-model", work_class="ci-remediation",
        deadline_monotonic=anyio_current_time_stub() + 1000.0,
        budget_ledger=ledger,
    )
    matchers = (built.hooks or {}).get("PreToolUse") or []
    callbacks = [h for m in matchers for h in m.hooks]
    command = "gh run view 123 --log-failed"
    decisions = []
    for cb in callbacks:
        result = _run_guard(cb, {"command": command})
        specific = (result or {}).get("hookSpecificOutput") or {}
        if specific.get("permissionDecision"):
            decisions.append(specific["permissionDecision"])
    assert "deny" in decisions, decisions
    # And the deadline guard is indeed the one that would have allowed it --
    # i.e. this is a real composition guarantee, not a vacuous one.
    assert _run_guard(_guard_callback(built), {"command": command})[
        "hookSpecificOutput"
    ]["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# SDK contract pin (mctl-agents#430 T8): `updatedInput` on an `allow`
# `PreToolUseHookSpecificOutput` must stay honoured by the pinned SDK.
# ---------------------------------------------------------------------------
def test_pretooluse_hook_output_still_declares_updated_input():
    """A drift guard, in the spirit of tests/test_subagent_wait.py's
    AWAITED_TASK_TYPES pin: if a future claude-agent-sdk bump drops
    `updatedInput` from `PreToolUseHookSpecificOutput`, this fails loudly
    instead of the deadline guard silently stopping narrowing commands.

    The documented fallback if this ever regresses: DENY the unbounded form
    instead of allowing with a rewritten command, handing the agent the
    exact `timeout`-wrapped command to re-issue in the deny reason -- see
    `options._deadline_guard_hook`'s docstring. That needs no SDK support.
    """
    from claude_agent_sdk.types import PreToolUseHookSpecificOutput

    assert "updatedInput" in PreToolUseHookSpecificOutput.__annotations__


# ---------------------------------------------------------------------------
# Execution identity headers (mctl-agents#196, ADR 011): the degrade path
# is a single narrow catch, and require mode passes through it fail-closed.
# ---------------------------------------------------------------------------
def test_execution_context_headers_degrade_on_broken_file(monkeypatch, tmp_path, capsys):
    path = tmp_path / "context.json"
    path.write_bytes(b"\xff\xfe\x00garbage")  # UnicodeDecodeError territory
    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", str(path))
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    assert options._execution_context_headers() == {}
    assert "omitting identity headers" in capsys.readouterr().out


def test_execution_context_headers_from_a_sealed_file(monkeypatch):
    """Positive path: a valid context file produces exactly the two identity
    headers — the zero-agent-cooperation guarantee this PR exists for."""
    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", _IDENTITY_FIXTURE)
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    assert options._execution_context_headers() == {
        "X-Mctl-Execution-Context": "ex-c5618d6437519c29",
        "X-Mctl-Trace-Id": "a" * 32,
    }


def test_mctl_mcp_config_merges_identity_headers_next_to_authorization(monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", _IDENTITY_FIXTURE)
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    headers = options.mctl_mcp_config()["mctl"]["headers"]
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["X-Mctl-Execution-Context"] == "ex-c5618d6437519c29"
    assert headers["X-Mctl-Trace-Id"] == "a" * 32


def test_mctl_mcp_config_headers_are_exactly_authorization_when_env_unset(monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    headers = options.mctl_mcp_config()["mctl"]["headers"]
    assert set(headers) == {"Authorization"}


def test_mctl_mcp_config_fails_closed_at_build_time_even_without_a_token(monkeypatch):
    """The require-mode raise must fire at options CONSTRUCTION, before the
    MCTL_TOKEN early-return — not only from inside the PreToolUse audit
    hook, whose exception propagation is an SDK implementation detail."""
    from orchestrator.execution_identity import ExecutionContextRequiredError

    monkeypatch.delenv("MCTL_TOKEN", raising=False)
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setenv("MCTL_REQUIRE_EXECUTION_CONTEXT", "1")
    with pytest.raises(ExecutionContextRequiredError):
        options.mctl_mcp_config()


def test_execution_context_headers_fail_closed_when_required_and_env_unset(monkeypatch):
    """agy finding 1 on 86fb8e8: the early `return {}` for an unset file env
    ran BEFORE load_from_environment() could raise, so require mode silently
    proceeded headerless in exactly the missing-env case."""
    from orchestrator.execution_identity import ExecutionContextRequiredError

    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setenv("MCTL_REQUIRE_EXECUTION_CONTEXT", "1")
    with pytest.raises(ExecutionContextRequiredError):
        options._execution_context_headers()


def test_execution_context_headers_fail_closed_in_require_mode(monkeypatch, tmp_path):
    """Driver-level fail-closed proof: with MCTL_REQUIRE_EXECUTION_CONTEXT
    set and a broken context file, the consumer must NOT degrade — the
    require-mode error passes through the narrow ExecutionIdentityError
    catch and aborts the caller. All four degrade sites (options plus the
    three run_* drivers) share this exact catch shape."""
    from orchestrator.execution_identity import ExecutionContextRequiredError

    path = tmp_path / "context.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", str(path))
    monkeypatch.setenv("MCTL_REQUIRE_EXECUTION_CONTEXT", "1")
    with pytest.raises(ExecutionContextRequiredError):
        options._execution_context_headers()


def test_audit_hook_appends_execution_context_when_present(monkeypatch, capsys):
    import asyncio

    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", _IDENTITY_FIXTURE)
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    asyncio.run(
        options._audit_pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)
    )
    out = capsys.readouterr().out
    assert "AUDIT tool=Bash cmd='ls' execution_context=ex-c5618d6437519c29" in out


def test_audit_hook_omits_execution_context_when_absent(monkeypatch, capsys):
    import asyncio

    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    asyncio.run(
        options._audit_pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)
    )
    out = capsys.readouterr().out
    assert "AUDIT tool=Bash cmd='ls'" in out
    assert "execution_context" not in out
