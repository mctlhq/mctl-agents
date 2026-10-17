from __future__ import annotations

import pytest

from config.model_policy import DEFAULT_POLICY_PATH, ModelPolicy, ModelPolicyError


def _policy() -> ModelPolicy:
    return ModelPolicy(
        {
            "version": 1,
            "profiles": {
                "cheap": {
                    "model": "cheap-default",
                    "model_env": "CLAUDE_CHEAP_MODEL",
                },
                "balanced": {
                    "model": "balanced-default",
                    "model_env": "CLAUDE_BALANCED_MODEL",
                },
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


def test_unknown_task_is_rejected() -> None:
    with pytest.raises(ModelPolicyError, match="unknown model-policy task"):
        _policy().resolve("missing", log=False)


# --- committed policy (config/model-policy.yaml) ---------------------------

_ENV_VARS = (
    "MODEL_POLICY_PATH",
    "CLAUDE_CHEAP_MODEL",
    "CLAUDE_BALANCED_MODEL",
    "SERVICE_AGENT_MODEL",
    "MENTOR_MODEL",
    "SHEPHERD_MODEL",
)


def _committed(monkeypatch: pytest.MonkeyPatch) -> ModelPolicy:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return ModelPolicy.load(DEFAULT_POLICY_PATH)


def _cheap_probe(policy: ModelPolicy) -> ModelPolicy:
    return ModelPolicy(
        {
            "version": 1,
            "profiles": policy._profiles,
            "tasks": {"probe": "cheap"},
        }
    )


def test_committed_cheap_profile_defaults_to_haiku_5_5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _cheap_probe(_committed(monkeypatch))

    selection = probe.resolve("probe", log=False)

    assert selection.model == "claude-haiku-5-5"
    assert selection.source == "policy"


def test_committed_routing_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _committed(monkeypatch)

    for task in ("service_agent", "mentor_digest", "review_findings_normalize"):
        selection = policy.resolve(task, log=False)
        assert (selection.profile, selection.model) == (
            "balanced",
            "claude-sonnet-5-5",
        )
    assert "cheap" not in policy._tasks.values()
    assert policy._profiles["balanced"]["model_env"] == "CLAUDE_BALANCED_MODEL"


def test_committed_cheap_rollback_env(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _cheap_probe(_committed(monkeypatch))
    monkeypatch.setenv("CLAUDE_CHEAP_MODEL", "claude-haiku-4-5")

    selection = probe.resolve("probe", log=False)

    assert selection.model == "claude-haiku-4-5"
    assert selection.source == "CLAUDE_CHEAP_MODEL"
