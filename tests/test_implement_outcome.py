"""Unit tests for the implementer outcome taxonomy (#395).

`classify` and `finalization_evidence` are pure, and the strings they
return are what a human reads on a failed loop and what the recovery plane
(#353, mctl-api#294) keys on. They are tested directly here so a change to
the wording cannot silently drop a distinction that only shows up through
an activity test.
"""
from __future__ import annotations

import pytest

from orchestrator.temporal.implement_outcome import classify, finalization_evidence


class TestFinalizationEvidence:
    @pytest.mark.parametrize("phase", ["Failed", "Error"])
    def test_a_failed_node_is_named(self, phase: str) -> None:
        assert f"reported {phase}" in finalization_evidence(phase)

    @pytest.mark.parametrize("phase", ["Pending", "Running"])
    def test_an_unfinished_step_says_the_commit_may_not_exist(self, phase: str) -> None:
        """The distinction this exists for: a wrap-up cut short is not a
        wrap-up that completed and was followed by something else."""
        message = finalization_evidence(phase)
        assert phase in message
        assert "may not exist" in message
        assert "past them" not in message

    def test_a_succeeded_step_points_past_the_finalization(self) -> None:
        message = finalization_evidence("Succeeded")
        assert "past them" in message
        assert "may well exist" in message

    def test_an_unreadable_graph_says_so(self) -> None:
        assert "no finalization node was readable" in finalization_evidence(None)


class TestClassify:
    def test_a_succeeded_workflow_is_success(self) -> None:
        assert classify("Succeeded", implementer_ran=True, implementer_phase="Succeeded") == "success"

    def test_a_pod_that_never_ran_is_pre_start(self) -> None:
        assert classify("Failed", implementer_ran=False, implementer_phase="Failed") == "pre_start"

    def test_an_unknown_graph_is_execution_never_pre_start(self) -> None:
        assert classify("Failed", implementer_ran=None, implementer_phase=None) == "execution"

    def test_a_succeeded_implementer_under_a_failed_workflow_is_finalization(self) -> None:
        assert classify("Failed", implementer_ran=True, implementer_phase="Succeeded") == "finalization"

    @pytest.mark.parametrize("finalization", [None, "Succeeded", "Running", "Failed"])
    def test_the_finalization_phase_does_not_move_the_verdict(self, finalization: str | None) -> None:
        """Clean or missing evidence must not send recovery back to the
        implementer: that branch would be taken every time Argo offloads
        the node map, and re-running an implementer that succeeded is the
        duplicate attempt the design exists to prevent."""
        assert (
            classify(
                "Failed",
                implementer_ran=True,
                implementer_phase="Succeeded",
                finalization_phase=finalization,
            )
            == "finalization"
        )
