"""Task-based Claude model selection.

The policy is declarative so model aliases and task routing can change without
touching orchestrator code. Existing task-specific environment variables remain
the highest-priority overrides for backwards compatibility.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = Path(__file__).with_name("model-policy.yaml")


class ModelPolicyError(ValueError):
    """Raised when the model policy is missing or invalid."""


@dataclass(frozen=True)
class ModelSelection:
    """Resolved model and the policy metadata used to select it."""

    task: str
    profile: str
    model: str
    source: str

    def log(self) -> None:
        print(
            "[model-policy] "
            f"task={self.task} profile={self.profile} model={self.model} "
            f"source={self.source}"
        )


class ModelPolicy:
    """Validated model policy loaded from YAML."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        if document.get("version") != 1:
            raise ModelPolicyError("model policy version must be 1")

        profiles = document.get("profiles")
        tasks = document.get("tasks")
        if not isinstance(profiles, Mapping) or not profiles:
            raise ModelPolicyError("model policy must define profiles")
        if not isinstance(tasks, Mapping) or not tasks:
            raise ModelPolicyError("model policy must define tasks")

        self._profiles = dict(profiles)
        self._tasks = dict(tasks)
        self._validate()

    @classmethod
    def load(cls, path: Path | None = None) -> ModelPolicy:
        configured_path = os.getenv("MODEL_POLICY_PATH", "").strip()
        policy_path = path or (
            Path(configured_path) if configured_path else DEFAULT_POLICY_PATH
        )
        try:
            document = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ModelPolicyError(
                f"could not read model policy at {policy_path}: {exc}"
            ) from exc
        except yaml.YAMLError as exc:
            raise ModelPolicyError(
                f"invalid YAML in model policy at {policy_path}: {exc}"
            ) from exc
        if not isinstance(document, Mapping):
            raise ModelPolicyError("model policy root must be a mapping")
        return cls(document)

    def _validate(self) -> None:
        for name, profile in self._profiles.items():
            if not isinstance(name, str) or not isinstance(profile, Mapping):
                raise ModelPolicyError("each profile must be a named mapping")
            model = profile.get("model")
            if not isinstance(model, str) or not model.strip():
                raise ModelPolicyError(f"profile {name!r} must define a model")
            model_env = profile.get("model_env")
            if model_env is not None and (
                not isinstance(model_env, str) or not model_env.strip()
            ):
                raise ModelPolicyError(
                    f"profile {name!r} model_env must be a non-empty string"
                )

        for task, profile_name in self._tasks.items():
            if profile_name not in self._profiles:
                raise ModelPolicyError(
                    f"task {task!r} references unknown profile {profile_name!r}"
                )

    def resolve(
        self,
        task: str,
        *,
        legacy_model_env: str | None = None,
        log: bool = True,
    ) -> ModelSelection:
        """Resolve a task to a concrete model.

        Precedence is task-specific legacy override, profile environment
        override, then the YAML default.
        """
        try:
            profile_name = self._tasks[task]
        except KeyError as exc:
            raise ModelPolicyError(f"unknown model-policy task {task!r}") from exc

        profile = self._profiles[profile_name]
        model = ""
        source = "policy"

        if legacy_model_env:
            model = os.getenv(legacy_model_env, "").strip()
            if model:
                source = legacy_model_env

        model_env = profile.get("model_env")
        if not model and model_env:
            model = os.getenv(model_env, "").strip()
            if model:
                source = model_env

        if not model:
            model = profile["model"].strip()

        selection = ModelSelection(
            task=task,
            profile=profile_name,
            model=model,
            source=source,
        )
        if log:
            selection.log()
        return selection


def resolve_model(
    task: str,
    *,
    legacy_model_env: str | None = None,
    log: bool = True,
) -> ModelSelection:
    """Resolve one task using the configured policy file."""
    return ModelPolicy.load().resolve(
        task,
        legacy_model_env=legacy_model_env,
        log=log,
    )
