"""Task-based Claude model selection.

The policy is declarative so model aliases and task routing can change without
touching orchestrator code. Existing task-specific environment variables remain
the highest-priority overrides for backwards compatibility.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

DEFAULT_POLICY_PATH = Path(__file__).with_name("model-policy.yaml")


class ModelPolicyError(ValueError):
    """Raised when the model policy is missing or invalid."""


#: Every CLI model slot a provider session pins to the resolved model.
MODEL_SLOTS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

#: Reasoning efforts the SDK/CLI accept (pinned to the SDK by a test).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

_PROVIDER_KEYS = frozenset(
    {"name", "api", "base_url", "credential_env", "pin_model_slots", "fallback"}
)
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_anthropic_credential_name(name: str) -> bool:
    return name.startswith("ANTHROPIC_") or name.startswith("CLAUDE_CODE_OAUTH_TOKEN")


@dataclass(frozen=True)
class ProviderSpec:
    """A non-Anthropic Anthropic-Messages endpoint declared by a profile."""

    name: str
    api: str
    base_url: str
    credential_env: str


def active_policy_path() -> Path:
    """The policy file ``ModelPolicy.load()`` reads when given no path."""
    configured_path = os.getenv("MODEL_POLICY_PATH", "").strip()
    return Path(configured_path) if configured_path else DEFAULT_POLICY_PATH


@dataclass(frozen=True)
class ModelSelection:
    """Resolved model and the policy metadata used to select it."""

    task: str
    profile: str
    model: str
    source: str
    provider: ProviderSpec | None = None
    effort: str | None = None

    def log(self) -> None:
        suffix = f" provider={self.provider.name}" if self.provider else ""
        if self.effort is not None:
            suffix += f" effort={self.effort}"
        print(
            "[model-policy] "
            f"task={self.task} profile={self.profile} model={self.model} "
            f"source={self.source}{suffix}"
        )


def _validate_provider(profile_name: str, profile: Mapping[str, Any]) -> ProviderSpec:
    def fail(message: str) -> ModelPolicyError:
        return ModelPolicyError(f"profile {profile_name!r} provider {message}")

    block = profile["provider"]
    if not isinstance(block, Mapping):
        raise fail("must be a mapping")
    unknown = sorted(str(k) for k in block if k not in _PROVIDER_KEYS)
    if unknown:
        raise fail(f"has unknown keys {unknown}")
    if profile.get("model_env") is not None:
        raise fail("profile must not set model_env")
    for key in ("name", "api", "base_url", "credential_env"):
        value = block.get(key)
        if not isinstance(value, str) or not value.strip():
            raise fail(f"must define {key}")
    name, api = block["name"], block["api"]
    base_url, credential_env = block["base_url"], block["credential_env"]
    if not _PROVIDER_NAME_RE.match(name):
        raise fail("name must match [a-z0-9-]+")
    if api != "anthropic-messages":
        raise fail("api must be 'anthropic-messages'")
    try:
        parts = urlsplit(base_url)
        host, _port = parts.hostname, parts.port
    except ValueError as exc:
        raise fail(f"base_url is invalid: {exc}") from exc
    if (
        parts.scheme != "https"
        or not host
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise fail(
            "base_url must be an absolute https URL with a host and no "
            "credentials, query or fragment"
        )
    if not _ENV_NAME_RE.match(credential_env):
        raise fail("credential_env must be an upper-case env var name")
    if _is_anthropic_credential_name(credential_env):
        raise fail("credential_env must not name an Anthropic credential")
    if block.get("pin_model_slots") is not True:
        raise fail("pin_model_slots must be true")
    if block.get("fallback") != "none":
        raise fail("fallback must be 'none'")
    return ProviderSpec(
        name=name, api=api, base_url=base_url, credential_env=credential_env
    )


def provider_session_env(
    provider: ProviderSpec, model: str, env: Mapping[str, str]
) -> dict[str, str]:
    """Session env for a provider-routed session. Pure; no I/O.

    ``env`` is the effective env the CLI will receive (inherited process env
    overlaid with the builder's env), never a sparse builder env.
    """
    if not model.strip():
        raise ModelPolicyError("provider session requires a non-empty model")
    credential = env.get(provider.credential_env, "")
    if not credential.strip():
        raise ModelPolicyError(
            f"provider {provider.name!r} credential env "
            f"{provider.credential_env} is unset or empty"
        )
    anthropic_names = [k for k in env if _is_anthropic_credential_name(k)]
    # Only secret-bearing names count as credentials: ANTHROPIC_BASE_URL, model
    # slots and boolean flags are non-secret and may legitimately recur in out.
    originals = {
        env[k]
        for k in anthropic_names
        if env[k]
        and k != "ANTHROPIC_BASE_URL"
        and k not in MODEL_SLOTS
        and any(t in k for t in ("KEY", "TOKEN", "SECRET", "CREDENTIAL", "PASSWORD"))
    }
    out = dict(env)
    for key in anthropic_names:
        out[key] = ""
    out[provider.credential_env] = ""
    out["ANTHROPIC_BASE_URL"] = provider.base_url
    out["ANTHROPIC_AUTH_TOKEN"] = credential
    for slot in MODEL_SLOTS:
        out[slot] = model
    out["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if credential in originals:
        raise ModelPolicyError(
            f"provider {provider.name!r} credential equals an Anthropic credential"
        )
    for key, value in out.items():
        if value and value in originals:
            raise ModelPolicyError(
                f"Anthropic credential value survives in provider env {key}"
            )
    return out


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
        self._providers: dict[str, ProviderSpec] = {}
        self._validate()

    @classmethod
    def load(cls, path: Path | None = None) -> ModelPolicy:
        policy_path = path or active_policy_path()
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
            if "effort" in profile:
                effort = profile["effort"]
                if not isinstance(effort, str) or effort not in EFFORT_LEVELS:
                    raise ModelPolicyError(
                        f"profile {name!r} effort must be one of "
                        f"{list(EFFORT_LEVELS)}"
                    )
            if "provider" in profile:
                self._providers[name] = _validate_provider(name, profile)

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
        provider = self._providers.get(profile_name)
        model = ""
        source = "policy"

        if legacy_model_env:
            model = os.getenv(legacy_model_env, "").strip()
            if model and provider is not None:
                raise ModelPolicyError(
                    f"task {task!r} routes to provider profile {profile_name!r}; "
                    f"the legacy override {legacy_model_env} must be unset"
                )
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
            provider=provider,
            effort=profile.get("effort"),
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
