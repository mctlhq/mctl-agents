"""TEMPORARY probe: proves the CI gate actually fails a PR.

This file exists only to verify that pr-validation.yml turns the PR red on a
failing test. It is deleted before the branch is closed and must never reach
main.
"""


def test_ci_gate_must_turn_red() -> None:
    assert False, "deliberate failure: proving the CI gate blocks a red PR"
