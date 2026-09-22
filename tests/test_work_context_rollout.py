"""The work-context rollout stage, mirroring tests/test_lifecycle_rollout.py.

T5 in the proposal's tasks.md: default off; each of the four modes; an
unrecognised value warns and answers off without raising; blocks_on_unknown()
is false below enforce regardless of WORK_CONTEXT_REQUIRED.
"""
from __future__ import annotations

import pytest

from orchestrator.work_context import rollout


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rollout.ENV_VAR, raising=False)
    monkeypatch.delenv(rollout.REQUIRED_ENV_VAR, raising=False)


def test_the_default_is_off() -> None:
    assert rollout.mode() == rollout.OFF
    assert rollout.computes_new_answer() is False
    assert rollout.new_answer_may_veto() is False
    assert rollout.new_answer_decides() is False
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
    monkeypatch.setenv(rollout.ENV_VAR, "obsrve")
    assert rollout.mode() == rollout.OFF
    assert "obsrve" in capsys.readouterr().out


def test_the_stages_are_ordered_not_independent_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ONLY)
    assert rollout.computes_new_answer()
    assert rollout.new_answer_may_veto()
    assert rollout.new_answer_decides()

    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    assert rollout.computes_new_answer()
    assert not rollout.new_answer_may_veto()
    assert not rollout.new_answer_decides()


def test_break_glass_has_no_effect_below_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_at_least_refuses_an_unknown_stage() -> None:
    with pytest.raises(ValueError):
        rollout.at_least("soak")
