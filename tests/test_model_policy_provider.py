from __future__ import annotations

import copy
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from config import model_policy
from config.model_policy import ModelPolicy, ModelPolicyError, provider_session_env
from orchestrator import options, resolver

FIXTURE = Path(__file__).parent / "fixtures" / "model-policy-provider.yaml"
TASK = "provider_test_task"
HZ_KEY = "HETZNER_INFERENCE_API_KEY"
SENTINELS = {
    "ANTHROPIC_API_KEY": "sk-ant-api-sentinel",
    "ANTHROPIC_API_KEY_SECONDARY": "sk-ant-api2-sentinel",
    "ANTHROPIC_AUTH_TOKEN": "sk-ant-auth-sentinel",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-sentinel",
    "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY": "oauth2-sentinel",
    "ANTHROPIC_FOO": "foo-sentinel",
}


def _doc() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(FIXTURE.read_text()))


def _provider_spec() -> model_policy.ProviderSpec:
    selection = ModelPolicy(_doc()).resolve(TASK, log=False)
    assert selection.provider is not None
    return selection.provider


def test_fixture_policy_loads_and_exposes_provider() -> None:
    selection = ModelPolicy(_doc()).resolve(TASK, log=False)
    assert selection.provider is not None
    assert selection.provider.base_url == "https://inference.hetzner.com/api"
    assert selection.provider.credential_env == HZ_KEY


def test_committed_policy_has_no_provider() -> None:
    policy = ModelPolicy.load(model_policy.DEFAULT_POLICY_PATH)
    assert policy.resolve("service_agent", log=False).provider is None


def _mutate(fn: Any) -> dict[str, Any]:
    doc = copy.deepcopy(_doc())
    fn(doc["profiles"]["experimental_hetzner"])
    return doc


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p["provider"].pop("base_url"),
        lambda p: p["provider"].update(base_url="http://inference.hetzner.com/api"),
        lambda p: p["provider"].update(base_url="http://127.0.0.1:8080"),
        lambda p: p["provider"].update(base_url="https://user:pw@inference.hetzner.com/api"),
        lambda p: p["provider"].update(base_url="https://inference.hetzner.com/api?x=1"),
        lambda p: p["provider"].update(base_url="https://inference.hetzner.com/api#f"),
        lambda p: p["provider"].update(credential_env="ANTHROPIC_API_KEY"),
        lambda p: p["provider"].update(credential_env="ANTHROPIC_FOO"),
        lambda p: p["provider"].update(credential_env="CLAUDE_CODE_OAUTH_TOKEN"),
        lambda p: p["provider"].update(credential_env="hetzner_key"),
        lambda p: p["provider"].update(fallback="claude-sonnet-5-5"),
        lambda p: p["provider"].update(pin_model_slots=False),
        lambda p: p.update(model_env="CLAUDE_BALANCED_MODEL"),
        lambda p: p["provider"].update(extra="x"),
        lambda p: p["provider"].update(api="openai"),
    ],
)
def test_invalid_provider_blocks_are_rejected(mutation: Any) -> None:
    with pytest.raises(ModelPolicyError, match="experimental_hetzner"):
        ModelPolicy(_mutate(mutation))


def test_provider_profile_rejects_legacy_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGACY_MODEL", "other")
    policy = ModelPolicy(_doc())
    with pytest.raises(ModelPolicyError):
        policy.resolve(TASK, legacy_model_env="LEGACY_MODEL", log=False)
    monkeypatch.setenv("LEGACY_MODEL", "")
    assert policy.resolve(TASK, legacy_model_env="LEGACY_MODEL", log=False).provider
    monkeypatch.setenv("LEGACY_MODEL", "other")
    plain = policy.resolve("service_agent", legacy_model_env="LEGACY_MODEL", log=False)
    assert plain.model == "other" and plain.provider is None


def _assert_no_leak(child: dict[str, str], model: str, config_dir_required: bool = True) -> None:
    values = set(child.values())
    for value in SENTINELS.values():
        assert value not in values
    assert child["ANTHROPIC_AUTH_TOKEN"] == "hz-sentinel"
    assert child["ANTHROPIC_BASE_URL"] == "https://inference.hetzner.com/api"
    for slot in model_policy.MODEL_SLOTS:
        assert child[slot] == model
    assert child[HZ_KEY] == ""
    assert child["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    if config_dir_required:
        config_dir = Path(child["CLAUDE_CONFIG_DIR"])
        assert config_dir.is_dir() and not list(config_dir.iterdir())


def _seed(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in SENTINELS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(HZ_KEY, "hz-sentinel")
    monkeypatch.setenv("MCTL_TOKEN", "")


def test_provider_session_has_no_anthropic_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(monkeypatch)
    provider = _provider_spec()
    plan = SimpleNamespace(
        model="Qwen/Qwen3.6-35B-A3B-FP8",
        tools=("Read", "Grep"),
        budget_usd=1.0,
        provider=provider,
        effort=None,
    )
    built = options.build_issue_investigator_options_from_plan(
        cast(Any, plan), tmp_path, tmp_path / "proposal"
    )
    assert built.fallback_model is None
    child = {**__import__("os").environ, **(built.env or {})}
    _assert_no_leak(child, plan.model)


def test_sparse_builder_env_cannot_let_inherited_credentials_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(monkeypatch)
    from claude_agent_sdk import ClaudeAgentOptions

    built = options._scrubbed(
        ClaudeAgentOptions(model="m1", env={"PROPOSAL_DIR": str(tmp_path)}),
        provider=_provider_spec(),
    )
    child = {**__import__("os").environ, **(built.env or {})}
    _assert_no_leak(child, "m1")


def test_scrubbed_refuses_fallback_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(monkeypatch)
    from claude_agent_sdk import ClaudeAgentOptions

    with pytest.raises(ValueError):
        options._scrubbed(
            ClaudeAgentOptions(model="m1", fallback_model="x"), provider=_provider_spec()
        )


@pytest.mark.parametrize("value", [None, ""])
def test_missing_credential_raises(value: str | None) -> None:
    env = {} if value is None else {HZ_KEY: value}
    with pytest.raises(ModelPolicyError):
        provider_session_env(_provider_spec(), "m", env)


def test_credential_equal_to_anthropic_value_raises() -> None:
    env = {HZ_KEY: "same", "CLAUDE_CODE_OAUTH_TOKEN": "same"}
    with pytest.raises(ModelPolicyError):
        provider_session_env(_provider_spec(), "m", env)


def test_empty_anthropic_original_is_ignored() -> None:
    out = provider_session_env(_provider_spec(), "m", {HZ_KEY: "k", "ANTHROPIC_API_KEY": ""})
    assert out["ANTHROPIC_AUTH_TOKEN"] == "k"
    assert out["ANTHROPIC_API_KEY"] == ""


def test_settings_import_fails_closed_for_provider_routed_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.settings

    doc = _doc()
    doc["tasks"]["service_agent"] = "experimental_hetzner"
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("MODEL_POLICY_PATH", str(path))
    monkeypatch.delenv("SERVICE_AGENT_MODEL", raising=False)
    try:
        with pytest.raises(ModelPolicyError):
            importlib.reload(config.settings)
    finally:
        monkeypatch.delenv("MODEL_POLICY_PATH")
        importlib.reload(config.settings)


def test_policy_version_tracks_loaded_file_and_log_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODEL_POLICY_PATH", raising=False)
    default = resolver._model_policy_version()
    _, default_hash = resolver._read_yaml_and_hash(model_policy.DEFAULT_POLICY_PATH)
    assert default.endswith(default_hash)
    monkeypatch.setenv("MODEL_POLICY_PATH", str(FIXTURE))
    _, fixture_hash = resolver._read_yaml_and_hash(FIXTURE)
    assert resolver._model_policy_version().endswith(fixture_hash)
    assert fixture_hash != default_hash


def test_execution_plan_log_dict_includes_provider() -> None:
    fields = {f: None for f in resolver.ExecutionPlan.__dataclass_fields__}
    plan = SimpleNamespace(**fields)
    plan.provider = _provider_spec()
    plan.permissions = {}
    plan.approval = {}
    plan.service_skills = ()
    plan.capability_providers = ()
    for name in ("prompt_hashes", "skill_hashes", "tools", "evidence"):
        setattr(plan, name, ())
    logged = resolver.ExecutionPlan.to_log_dict(cast(Any, plan))
    assert logged["provider"] == {
        "name": "hetzner",
        "base_url": "https://inference.hetzner.com/api",
    }


def test_provider_profile_with_effort_resolves_it_and_committed_policy_has_none() -> None:
    doc = _doc()
    doc["profiles"]["experimental_hetzner"]["effort"] = "medium"
    assert ModelPolicy(doc).resolve(TASK, log=False).effort == "medium"
    committed = ModelPolicy.load(model_policy.DEFAULT_POLICY_PATH)
    raw = yaml.safe_load(model_policy.DEFAULT_POLICY_PATH.read_text())
    for task in raw["tasks"]:
        assert committed.resolve(task, log=False).effort is None


def test_settings_import_fails_closed_for_effort_routed_string_only_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import config.settings

    doc = _doc()
    doc["profiles"]["balanced"]["effort"] = "medium"
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("MODEL_POLICY_PATH", str(path))
    monkeypatch.delenv("SERVICE_AGENT_MODEL", raising=False)
    try:
        with pytest.raises(ModelPolicyError, match="effort"):
            importlib.reload(config.settings)
    finally:
        monkeypatch.delenv("MODEL_POLICY_PATH")
        importlib.reload(config.settings)


def test_execution_plan_carries_and_logs_effort() -> None:
    fields = {f: None for f in resolver.ExecutionPlan.__dataclass_fields__}
    plan = SimpleNamespace(**fields)
    plan.effort = "medium"
    plan.permissions = {}
    plan.approval = {}
    plan.service_skills = ()
    plan.capability_providers = ()
    for name in ("prompt_hashes", "skill_hashes", "tools", "evidence"):
        setattr(plan, name, ())
    assert resolver.ExecutionPlan.to_log_dict(cast(Any, plan))["effort"] == "medium"
    real = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="e" * 40))
    assert real.effort is None and real.to_log_dict()["effort"] is None
