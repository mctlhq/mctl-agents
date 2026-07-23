from __future__ import annotations

import pytest

from config.model_policy import ModelPolicy, ModelPolicyError


def _policy() -> ModelPolicy:
    return ModelPolicy(
        {
            "version": 1,
            "profiles": {
                "cheap": {
                    "model": "cheap-default",
                    "model_env": "CLAUDE_CHEAP_MODEL",
                    "escalates_to": "balanced",
                },
                "balanced": {
                    "model": "balanced-default",
                    "model_env": "CLAUDE_BALANCED_MODEL",
                    "escalates_to": "strong",
                },
                "strong": {"model": "strong-default"},
            },
            "tasks": {
                "mentor_digest": "cheap",
                "review_findings_normalize": "cheap",
                "service_agent": "balanced",
            },
        }
    )


def test_low_cost_tasks_use_cheap_profile() -> None:
    policy = _policy()

    mentor = policy.resolve("mentor_digest", log=False)
    shepherd = policy.resolve("review_findings_normalize", log=False)

    assert (mentor.profile, mentor.model) == ("cheap", "cheap-default")
    assert (shepherd.profile, shepherd.model) == ("cheap", "cheap-default")
    assert mentor.escalation_profile == "balanced"


def test_legacy_task_override_has_highest_precedence(monkeypatch) -> None:
    monkeypatch.setenv("MENTOR_MODEL", "task-override")
    monkeypatch.setenv("CLAUDE_CHEAP_MODEL", "profile-override")

    selection = _policy().resolve(
        "mentor_digest",
        legacy_model_env="MENTOR_MODEL",
        log=False,
    )

    assert selection.model == "task-override"
    assert selection.source == "MENTOR_MODEL"


def test_profile_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CHEAP_MODEL", "profile-override")

    selection = _policy().resolve("mentor_digest", log=False)

    assert selection.model == "profile-override"
    assert selection.source == "CLAUDE_CHEAP_MODEL"


def test_escalation_advances_one_profile() -> None:
    selection = _policy().resolve("mentor_digest", escalate=True, log=False)

    assert selection.profile == "balanced"
    assert selection.model == "balanced-default"
    assert selection.escalation_profile == "strong"


def test_unknown_task_is_rejected() -> None:
    with pytest.raises(ModelPolicyError, match="unknown model-policy task"):
        _policy().resolve("missing", log=False)
