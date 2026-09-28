"""Tests for orchestrator/context_rollout.py — mctlhq/mctl-agents#527 Slice
B's off/observe/enforce/only ladder (ADR 019 sec. 4). T7 below maps onto
that proposal's tasks.md "## Tests" section; T8-T13/T15/T17 live in
tests/test_context_assembly.py (the module that actually wires the ladder
into `assemble()`) and T14 lives in tests/test_tracing.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import context_assembly as ca
from orchestrator import context_rollout as rollout
from tests.test_context_assembly import _assembly_input, _execution

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rollout.ENV_VAR, raising=False)
    monkeypatch.delenv(rollout.REQUIRED_ENV_VAR, raising=False)
    monkeypatch.delenv(ca.STRATEGY_ENV_VAR, raising=False)
    monkeypatch.delenv("AGENT_ENVIRONMENT", raising=False)


# ---------------------------------------------------------------------------
# The ladder itself
# ---------------------------------------------------------------------------
def test_the_default_is_off():
    assert rollout.mode() == rollout.OFF
    assert rollout.binding_is_observed() is False
    assert rollout.binding_decides() is False
    assert rollout.binding_is_sole_selector() is False
    assert rollout.blocks_on_unknown() is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("off", rollout.OFF),
        ("observe", rollout.OBSERVE),
        ("enforce", rollout.ENFORCE),
        ("only", rollout.ONLY),
        ("  OBSERVE  ", rollout.OBSERVE),
    ],
)
def test_each_stage_is_recognised(monkeypatch: pytest.MonkeyPatch, value: str, expected: str) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, value)
    assert rollout.mode() == expected


def test_an_unrecognised_value_is_off_not_an_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, "observee")
    assert rollout.mode() == rollout.OFF
    assert "observee" in capsys.readouterr().out


def test_the_stages_are_ordered_not_independent_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ONLY)
    assert rollout.binding_is_observed()
    assert rollout.binding_decides()
    assert rollout.binding_is_sole_selector()

    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    assert rollout.binding_is_observed()
    assert not rollout.binding_decides()
    assert not rollout.binding_is_sole_selector()


def test_at_least_refuses_an_unknown_stage() -> None:
    with pytest.raises(ValueError):
        rollout.at_least("soak")


def test_context_release_required_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    for false_value in ("false", "FALSE", " False ", "no", "0", "off"):
        monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, false_value)
        assert rollout.context_release_required() is False
    for true_value in ("true", "yes", "1", "anything-else"):
        monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, true_value)
        assert rollout.context_release_required() is True
    monkeypatch.delenv(rollout.REQUIRED_ENV_VAR, raising=False)
    assert rollout.context_release_required() is True  # unset means required


def test_context_release_required_has_no_effect_below_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, "true")
    for stage in (rollout.OFF, rollout.OBSERVE):
        monkeypatch.setenv(rollout.ENV_VAR, stage)
        assert rollout.blocks_on_unknown() is False


def test_break_glass_opens_and_closes_at_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, "true")
    assert rollout.blocks_on_unknown() is True
    monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, "false")
    assert rollout.blocks_on_unknown() is False


# ---------------------------------------------------------------------------
# Import isolation at off.
# ---------------------------------------------------------------------------
def test_importing_the_ladder_loads_neither_yaml_nor_context_release():
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.context_rollout, sys; "
            "print(chr(10).join(sorted(sys.modules)))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.split("\n"))
    assert "yaml" not in loaded
    assert "orchestrator.context_release" not in loaded


def test_resolve_strategy_for_run_at_off_imports_neither_yaml_nor_context_release():
    """`off` opens no catalog file: proven by asserting neither module a
    catalog read would require ever lands in `sys.modules`, in a fresh
    subprocess (a bare in-process check would be lying: pytest has already
    imported half the codebase by the time this test runs)."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from orchestrator import context_assembly as ca\n"
            "config = ca.AssemblyConfig()\n"
            "assert ca.resolve_strategy_for_run('issue-investigator', config) == (config.strategy, None)\n"
            "import sys\n"
            "assert 'yaml' not in sys.modules\n"
            "assert 'orchestrator.context_release' not in sys.modules\n"
            "print('ok')\n",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# ---------------------------------------------------------------------------
# resolve_strategy_for_run wired through the real committed catalog
# (config/context-strategies/bindings/shadow/issue-investigator.yaml ->
# deterministic-fixed-order).
# ---------------------------------------------------------------------------
def test_off_returns_the_config_strategy_untouched():
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    assert ca.resolve_strategy_for_run("issue-investigator", config) == (ca.RANKED_STRATEGY_NAME, None)


def test_observe_leaves_the_env_var_strategy_authoritative_and_observes_the_shadow_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    effective, resolution = ca.resolve_strategy_for_run("issue-investigator", config, environment="production")
    assert effective == ca.RANKED_STRATEGY_NAME  # the env var still decides
    assert resolution.mode == rollout.OBSERVE
    assert resolution.reason == ca.RELEASE_REASON_OBSERVE
    assert resolution.bound_strategy == "deterministic-fixed-order"
    assert resolution.binding_revision is not None


def test_enforce_lets_the_committed_binding_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    effective, resolution = ca.resolve_strategy_for_run("issue-investigator", config, environment="shadow")
    assert effective == "deterministic-fixed-order"
    assert resolution.mode == rollout.ENFORCE
    assert resolution.reason == ca.RELEASE_REASON_BINDING


def test_only_rejects_a_set_strategy_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ONLY)
    monkeypatch.setenv(ca.STRATEGY_ENV_VAR, "trust-freshness-ranked")
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    with pytest.raises(ca.ContextStrategyNotResolved, match=ca.STRATEGY_ENV_VAR):
        ca.resolve_strategy_for_run("issue-investigator", config, environment="shadow")


def test_only_with_no_override_lets_the_binding_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ONLY)
    config = ca.AssemblyConfig()
    effective, resolution = ca.resolve_strategy_for_run("issue-investigator", config, environment="shadow")
    assert effective == "deterministic-fixed-order"
    assert resolution.mode == rollout.ONLY


def test_observe_runs_run_pipeline_twice_when_the_bound_strategy_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`observe`'s whole point: the bound strategy runs as a SECOND pass over
    the same candidate list, without changing which one is authoritative."""
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    assembly_input = _assembly_input(tmp_path, config=config)
    execution = _execution(environment="production")

    calls: list[str] = []
    real_run_pipeline = ca.run_pipeline

    def counting_run_pipeline(candidates, cfg, now):
        calls.append(cfg.strategy)
        return real_run_pipeline(candidates, cfg, now)

    monkeypatch.setattr(ca, "run_pipeline", counting_run_pipeline)
    result = ca.assemble(assembly_input, mode="shadow", execution=execution)

    assert calls == [ca.RANKED_STRATEGY_NAME, "deterministic-fixed-order"]
    assert result.metrics.strategy_name == ca.RANKED_STRATEGY_NAME  # the env var still decided
