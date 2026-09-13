"""The rollout stage, and the boundary between it and the other two switches.

The assertions worth having here are about what each switch may NOT do. Three
overlapping toggles in one area is how a deployment ends up in a state nobody
configured, so each test below pins one of them to its own job.
"""
from __future__ import annotations

import pytest

from orchestrator.lifecycle import rollout


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rollout.ENV_VAR, raising=False)
    monkeypatch.delenv("LIFECYCLE_OWNERSHIP_REQUIRED", raising=False)


def test_the_default_is_off() -> None:
    """An unconfigured deployment must not silently start deciding by the new
    answer. Every other default in this area fails closed for the same reason."""
    assert rollout.mode() == rollout.OFF
    assert rollout.records_writes() is False
    assert rollout.computes_new_answer() is False
    assert rollout.new_answer_may_veto() is False
    assert rollout.new_answer_decides() is False


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
def test_each_stage_is_recognised(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, value)
    assert rollout.mode() == expected


def test_an_unrecognised_value_is_off_not_an_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo in a gitops env map must not crash the shepherd.

    And of the two ways to be wrong about an unreadable stage, the one that
    changes nothing is the right one — `observe` would start writing to a
    store this deployment was never meant to touch.
    """
    monkeypatch.setenv(rollout.ENV_VAR, "obsrve")
    assert rollout.mode() == rollout.OFF
    assert "obsrve" in capsys.readouterr().out


def test_the_stages_are_ordered_not_independent_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`only` implies everything `enforce` implies, and so on down.

    Written as an ordering test because the alternative — four booleans — is
    how a deployment reaches "decides by the new answer, but does not write
    it", which is not a stage anybody intends.
    """
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ONLY)
    assert rollout.records_writes()
    assert rollout.computes_new_answer()
    assert rollout.new_answer_may_veto()
    assert rollout.new_answer_decides()

    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    assert rollout.records_writes()
    assert rollout.computes_new_answer()
    assert not rollout.new_answer_may_veto()
    assert not rollout.new_answer_decides()


def test_observe_still_compares_at_enforce_and_above(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison does not switch off at the stage where it starts to
    matter most: a divergence at enforce is the one that changed an outcome,
    and it has to stay legible afterwards."""
    for stage in (rollout.ENFORCE, rollout.ONLY):
        monkeypatch.setenv(rollout.ENV_VAR, stage)
        assert rollout.computes_new_answer() is True


def test_break_glass_has_no_effect_below_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIFECYCLE_OWNERSHIP_REQUIRED is the break-glass INSIDE enforce, not a
    second rollout switch.

    Below enforce the new answer decides nothing, so an unreachable store
    cannot block anything regardless of how this is set. Without the mode as
    the outer condition, a deployment in `observe` with the default `true`
    would start blocking mutations on an mctl-api outage — during the stage
    whose entire promise is that it changes no outcomes.
    """
    monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", "true")
    for stage in (rollout.OFF, rollout.OBSERVE):
        monkeypatch.setenv(rollout.ENV_VAR, stage)
        assert rollout.blocks_on_unknown() is False


def test_break_glass_opens_and_closes_at_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", "true")
    assert rollout.blocks_on_unknown() is True
    # The documented escape hatch during an mctl-api outage.
    monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", "false")
    assert rollout.blocks_on_unknown() is False


def test_at_least_refuses_an_unknown_stage() -> None:
    """A caller asking about a stage that does not exist is a bug in the
    caller, and answering False would hide it as "not yet at that stage"."""
    with pytest.raises(ValueError):
        rollout.at_least("soak")
