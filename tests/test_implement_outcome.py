"""Unit tests for the implementer outcome taxonomy (#395).

`classify` and `finalization_evidence` are pure, and the strings they
return are what a human reads on a failed loop and what the recovery plane
(#353, mctl-api#294) keys on. They are tested directly here so a change to
the wording cannot silently drop a distinction that only shows up through
an activity test.
"""
from __future__ import annotations

import pytest

from orchestrator.temporal.implement_outcome import (
    classify,
    finalization_evidence,
    observe_implementer,
    pre_start_reason,
)


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


def _implementer_node(phase: str, **extra: object) -> dict:
    return {"type": "Pod", "templateName": "run-implementer", "phase": phase, **extra}


class TestPreStartReason:
    """The 2026-09-19 burst, restated as the six node shapes #418 needs to
    tell apart (T1): a lock wait, by structure or by message, is not the
    same as "Argo never scheduled a pod", and neither is the same as "the
    node graph was not readable at all"."""

    def test_a_structured_lock_wait_mark_is_lock_wait(self) -> None:
        node = _implementer_node(
            "Failed", synchronizationStatus={"waiting": "argo-workflows/Mutex/mctl-agents-proposal-claims"}
        )
        observation = observe_implementer({"nodes": {"a": node}})
        assert observation.ran is False
        assert observation.pre_start_reason == "lock_wait"

    def test_a_lock_wait_message_is_also_lock_wait(self) -> None:
        node = _implementer_node(
            "Failed",
            message="Waiting for argo-workflows/Mutex/mctl-agents-proposal-claims. Lock status: 0/1",
        )
        observation = observe_implementer({"nodes": {"a": node}})
        assert observation.ran is False
        assert observation.pre_start_reason == "lock_wait"

    @pytest.mark.parametrize(
        "extra",
        [
            {"message": "Waiting for argo-workflows/ConfigMap/limits/workflow. Lock status: 0/2"},
            {"message": "Waiting for argo-workflows/Mutex/some-other-lock. Lock status: 0/1"},
            {"synchronizationStatus": {"waiting": "argo-workflows/ConfigMap/limits/workflow"}},
            {"synchronizationStatus": {"waiting": "argo-workflows/Mutex/some-other-lock"}},
        ],
    )
    def test_a_wait_on_any_other_lock_is_not_lock_wait(self, extra: dict) -> None:
        """`lock_wait` means the claims mutex (#459 review P3): a semaphore
        wait (whose message also says `Lock status:`) or another mutex must
        not be reported as the #418 deadline-vs-claims-lock shape."""
        observation = observe_implementer({"nodes": {"a": _implementer_node("Failed", **extra)}})
        assert observation.ran is False
        assert observation.pre_start_reason == "unscheduled"

    def test_a_failed_node_with_no_pod_and_no_lock_mark_is_unscheduled(self) -> None:
        node = _implementer_node("Failed")
        observation = observe_implementer({"nodes": {"a": node}})
        assert observation.ran is False
        assert observation.pre_start_reason == "unscheduled"

    def test_a_missing_node_map_is_unknown(self) -> None:
        observation = observe_implementer({})
        assert observation.ran is None
        assert observation.pre_start_reason is None
        assert pre_start_reason(observation.pre_start_reason) == "unknown"

    def test_an_empty_node_map_is_unknown(self) -> None:
        observation = observe_implementer({"nodes": {}})
        assert observation.ran is None
        assert observation.pre_start_reason is None
        assert pre_start_reason(observation.pre_start_reason) == "unknown"

    def test_a_pod_that_ran_reports_no_reason_at_all(self) -> None:
        node = _implementer_node("Succeeded", hostNodeName="k3s-worker-1")
        observation = observe_implementer({"nodes": {"a": node}})
        assert observation.ran is True
        assert observation.pre_start_reason is None

    def test_render_never_reports_unscheduled_for_an_unreadable_graph(self) -> None:
        """The rule spelled out on the type: absence of evidence about the
        cluster is not evidence about the cluster."""
        assert pre_start_reason(None) == "unknown"
        assert pre_start_reason("lock_wait") == "lock_wait"
        assert pre_start_reason("unscheduled") == "unscheduled"
