"""Tests for orchestrator/resolver.py — mctlhq/mctl-agents#227's declarative
`execute(agent, task)` pilot. T1-T9 below map onto that proposal's
tasks.md "## Tests" section; see orchestrator/resolver.py's module docstring
for the fail-closed contract every negative test here asserts.

Every test builds its own isolated fixture tree under `tmp_path` and
monkeypatches resolver's directory constants rather than touching the real
`agents/_manifests/issue-investigator/` / `tests/fixtures/resolver/` files —
those are exercised end-to-end by `test_real_issue_investigator_fixtures_resolve`
below plus `tests/test_manifest.py`'s existing parametrized checks.
"""
from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest
import yaml

from orchestrator import resolver
from orchestrator.manifest import PromptSource

# ---------------------------------------------------------------------------
# Fixture-tree builder
# ---------------------------------------------------------------------------

_AGENT = "test-agent"
_PROFILE = "test-profile"


def _content_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _base_profile_doc() -> dict:
    return {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ExecutionProfile",
        "metadata": {"name": _PROFILE, "promotable": False},
        "spec": {
            "modelPolicyRef": {"task": "service_agent"},
            "skills": [],
            "tools": ["Read", "Write"],
            "policyRef": "test-policy",
            "permissions": {"targetRepository": "read"},
            "budgetUsd": 3.0,
            "timeoutSeconds": 900.0,
            "runtime": {
                "type": "claude-agent-sdk",
                "entrypoint": "orchestrator.run_issue_investigator:investigate",
                "optionsBuilder": "orchestrator.options:build_issue_investigator_options",
                "sandbox": {
                    "backend": "argo",
                    "clusterWorkflowTemplate": "mctl-agents-investigate",
                    "approved": True,
                },
            },
            "approval": {"required": False},
            "evidence": ["proposal_triplet"],
        },
    }


def _base_definition_doc(compatibility: str) -> dict:
    return {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "AgentDefinition",
        "metadata": {"name": _AGENT, "owner": "mctl-agents"},
        "spec": {
            "prompt": {"sources": [{"inline": "tests.test_resolver:_dummy_prompt"}]},
            "executionProfileRef": {"name": _PROFILE, "compatibility": compatibility},
        },
    }


def _dummy_prompt() -> str:
    """Stand-in `inline:` prompt source target — hashed by
    `orchestrator.resolver._hash_prompt_source`, never actually rendered."""
    return "dummy"


def _base_binding_doc(*, definition_version: str, profile_version: str, revision: int = 1) -> dict:
    return {
        "bindingSource": "compatibility-fixture",
        "promotable": False,
        "environment": "production",
        "definition": {"name": _AGENT, "version": definition_version},
        "profile": {"name": _PROFILE, "version": profile_version},
        "releaseRevision": revision,
        "registryLifecycle": {"definition": "published", "profile": "published"},
    }


def _write(path: Path, document: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(document), encoding="utf-8")
    return path


def _build_fixture_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile_overrides: dict | None = None,
    definition_overrides: dict | None = None,
    binding_overrides: dict | None = None,
    compatibility: str | None = None,
    write_binding: bool = True,
) -> dict:
    """Write a self-consistent definition/profile/binding fixture set under
    tmp_path and point resolver's module-level directory constants at it.
    Returns the three parsed documents (pre-write) for callers that want to
    mutate one field and rewrite just that file."""
    definitions_dir = tmp_path / "manifests"
    profiles_dir = tmp_path / "profiles"
    releases_dir = tmp_path / "releases"
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", definitions_dir)
    monkeypatch.setattr(resolver, "PROFILES_FIXTURE_DIR", profiles_dir)
    monkeypatch.setattr(resolver, "RELEASES_FIXTURE_DIR", releases_dir)

    profile_doc = _base_profile_doc()
    if profile_overrides:
        profile_doc["spec"].update(profile_overrides)
    profile_path = _write(profiles_dir / f"{_PROFILE}.yaml", profile_doc)
    profile_hash = _content_hash(profile_path)

    definition_doc = _base_definition_doc(compatibility if compatibility is not None else profile_hash)
    if definition_overrides:
        definition_doc["spec"].update(definition_overrides)
    definition_path = _write(definitions_dir / _AGENT / "agent.yaml", definition_doc)
    definition_hash = _content_hash(definition_path)

    if write_binding:
        binding_doc = _base_binding_doc(definition_version=definition_hash, profile_version=profile_hash)
        if binding_overrides:
            binding_doc.update(binding_overrides)
        _write(releases_dir / f"{_AGENT}.yaml", binding_doc)

    return {
        "definitions_dir": definitions_dir,
        "profiles_dir": profiles_dir,
        "releases_dir": releases_dir,
        "profile_path": profile_path,
        "definition_path": definition_path,
        "profile_hash": profile_hash,
        "definition_hash": definition_hash,
    }


# ---------------------------------------------------------------------------
# T2 — valid v1alpha2 definition/profile resolves
# ---------------------------------------------------------------------------
def test_execute_resolves_a_valid_fixture_set(tmp_path, monkeypatch):
    _build_fixture_set(tmp_path, monkeypatch)
    plan = resolver.execute(_AGENT, resolver.Task(target_repository_sha="a" * 40))

    assert plan.agent == _AGENT
    assert plan.tools == ("Read", "Write")
    assert plan.budget_usd == 3.0
    assert plan.timeout_seconds == 900.0
    assert plan.cluster_workflow_template == "mctl-agents-investigate"
    assert plan.sandbox_backend == "argo"
    assert plan.target_repository_sha == "a" * 40
    assert plan.release_revision == 1
    assert plan.binding_source == "compatibility-fixture"


# ---------------------------------------------------------------------------
# T5 — determinism: same fixture + same task/target SHA -> identical plan
# ---------------------------------------------------------------------------
def test_execute_is_deterministic_for_the_same_input(tmp_path, monkeypatch):
    _build_fixture_set(tmp_path, monkeypatch)
    task = resolver.Task(target_repository_sha="b" * 40)
    plan_one = resolver.execute(_AGENT, task)
    plan_two = resolver.execute(_AGENT, task)
    assert plan_one == plan_two


def test_execute_differs_only_by_target_sha(tmp_path, monkeypatch):
    """A later promotion never mutates an already created plan — here,
    changing only the per-run input (target SHA) changes only that field."""
    _build_fixture_set(tmp_path, monkeypatch)
    plan_one = resolver.execute(_AGENT, resolver.Task(target_repository_sha="c" * 40))
    plan_two = resolver.execute(_AGENT, resolver.Task(target_repository_sha="d" * 40))
    assert plan_one.target_repository_sha != plan_two.target_repository_sha
    fields_that_must_still_match = {
        k: v for k, v in plan_one.to_log_dict().items() if k != "target_repository_sha"
    }
    assert {k: v for k, v in plan_two.to_log_dict().items() if k != "target_repository_sha"} == (
        fields_that_must_still_match
    )


# ---------------------------------------------------------------------------
# T3 — missing/ambiguous/disabled/incompatible/unknown/unbounded/unapproved
# fail before Argo submission (never fall back).
# ---------------------------------------------------------------------------
def test_execute_fails_on_missing_release_binding(tmp_path, monkeypatch):
    _build_fixture_set(tmp_path, monkeypatch, write_binding=False)
    with pytest.raises(resolver.ResolverError, match="missing release"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="e" * 40))


def test_execute_fails_on_missing_profile(tmp_path, monkeypatch):
    built = _build_fixture_set(tmp_path, monkeypatch)
    built["profile_path"].unlink()
    with pytest.raises(resolver.ResolverError, match="missing execution profile"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="f" * 40))


def test_execute_fails_on_disabled_registry_lifecycle(tmp_path, monkeypatch):
    _build_fixture_set(
        tmp_path, monkeypatch,
        binding_overrides={"registryLifecycle": {"definition": "disabled", "profile": "published"}},
    )
    with pytest.raises(resolver.ResolverError, match="registryLifecycle"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="g" * 40))


def test_execute_fails_on_compatibility_mismatch(tmp_path, monkeypatch):
    """T4: compatibility is checked against the CONCRETE selected profile
    version — a definition pinned to the wrong hash must fail, and the
    profile's own file never declares an accepted range for this to read."""
    _build_fixture_set(tmp_path, monkeypatch, compatibility="sha256:" + "0" * 64)
    with pytest.raises(resolver.ResolverError, match="compatibility mismatch"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="h" * 40))


def test_execute_fails_when_profile_drifts_from_pinned_hash(tmp_path, monkeypatch):
    """Editing the profile fixture without re-pinning the release binding's
    profile.version must fail closed — an 'ambiguous version', not a silent
    re-resolution of whatever the profile file now says."""
    built = _build_fixture_set(tmp_path, monkeypatch)
    profile_doc = _base_profile_doc()
    profile_doc["spec"]["budgetUsd"] = 99.0  # any content change moves the hash
    _write(built["profile_path"], profile_doc)
    with pytest.raises(resolver.ResolverError, match="ambiguous version"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="i" * 40))


def test_execute_fails_on_unknown_profile_reference(tmp_path, monkeypatch):
    _build_fixture_set(
        tmp_path, monkeypatch,
        binding_overrides={"profile": {"name": "some-other-profile", "version": "sha256:" + "0" * 64}},
    )
    with pytest.raises(resolver.ResolverError, match="unknown reference"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="j" * 40))


@pytest.mark.parametrize("bad_budget", [0, -1, None, "not-a-number", 10_000])
def test_execute_fails_on_unbounded_budget(tmp_path, monkeypatch, bad_budget):
    _build_fixture_set(tmp_path, monkeypatch, profile_overrides={"budgetUsd": bad_budget})
    with pytest.raises(resolver.ResolverError, match="budgetUsd"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="k" * 40))


@pytest.mark.parametrize("bad_timeout", [0, -1, None])
def test_execute_fails_on_unbounded_timeout(tmp_path, monkeypatch, bad_timeout):
    _build_fixture_set(tmp_path, monkeypatch, profile_overrides={"timeoutSeconds": bad_timeout})
    with pytest.raises(resolver.ResolverError, match="timeoutSeconds"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="l" * 40))


def test_execute_fails_on_unapproved_sandbox(tmp_path, monkeypatch):
    profile = _base_profile_doc()
    profile["spec"]["runtime"]["sandbox"]["approved"] = False
    _build_fixture_set(tmp_path, monkeypatch, profile_overrides=profile["spec"])
    with pytest.raises(resolver.ResolverError, match="sandbox"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="m" * 40))


def test_execute_fails_on_missing_policy_ref(tmp_path, monkeypatch):
    _build_fixture_set(tmp_path, monkeypatch, profile_overrides={"policyRef": ""})
    with pytest.raises(resolver.ResolverError, match="policyRef"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="n" * 40))


def test_execute_fails_on_empty_permissions(tmp_path, monkeypatch):
    _build_fixture_set(tmp_path, monkeypatch, profile_overrides={"permissions": {}})
    with pytest.raises(resolver.ResolverError, match="permissions"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="o" * 40))


def test_execute_fails_on_missing_target_sha(tmp_path, monkeypatch):
    _build_fixture_set(tmp_path, monkeypatch)
    with pytest.raises(resolver.ResolverError, match="target_repository_sha"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha=""))


def test_load_definition_fails_on_unknown_api_version(tmp_path, monkeypatch):
    definitions_dir = tmp_path / "manifests"
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", definitions_dir)
    doc = _base_definition_doc("sha256:" + "0" * 64)
    doc["apiVersion"] = "agents.mctl.ai/v1alpha3"
    _write(definitions_dir / _AGENT / "agent.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="apiVersion"):
        resolver.load_definition(_AGENT)


def test_load_definition_fails_on_unknown_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", tmp_path / "empty")
    with pytest.raises(resolver.ResolverError, match="unknown agent"):
        resolver.load_definition("nonexistent-agent")


def test_load_definition_fails_closed_on_non_mapping_metadata(tmp_path, monkeypatch):
    """A scalar where `metadata` must be a mapping raises `ResolverError`,
    not a raw `AttributeError` from a bare `.get()` call."""
    definitions_dir = tmp_path / "manifests"
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", definitions_dir)
    doc = _base_definition_doc("sha256:" + "0" * 64)
    doc["metadata"] = "oops"
    _write(definitions_dir / _AGENT / "agent.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="metadata must be a mapping"):
        resolver.load_definition(_AGENT)


# ---------------------------------------------------------------------------
# T8 — fixtures cannot be interpreted as promotable registry state.
# ---------------------------------------------------------------------------
def test_load_profile_requires_explicit_promotable_false(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "PROFILES_FIXTURE_DIR", tmp_path / "profiles")
    doc = _base_profile_doc()
    del doc["metadata"]["promotable"]
    _write(tmp_path / "profiles" / f"{_PROFILE}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="promotable"):
        resolver.load_profile(_PROFILE)


def test_load_profile_fails_closed_on_non_mapping_model_policy_ref(tmp_path, monkeypatch):
    """A scalar where `spec.modelPolicyRef` must be a mapping raises
    `ResolverError`, not a raw `AttributeError` from a bare `.get()` call."""
    monkeypatch.setattr(resolver, "PROFILES_FIXTURE_DIR", tmp_path / "profiles")
    doc = _base_profile_doc()
    doc["spec"]["modelPolicyRef"] = "oops"
    _write(tmp_path / "profiles" / f"{_PROFILE}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="modelPolicyRef must be a mapping"):
        resolver.load_profile(_PROFILE)


def test_load_release_binding_requires_compatibility_fixture_source(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "RELEASES_FIXTURE_DIR", tmp_path / "releases")
    doc = _base_binding_doc(definition_version="sha256:" + "0" * 64, profile_version="sha256:" + "1" * 64)
    doc["bindingSource"] = "registry"
    _write(tmp_path / "releases" / f"{_AGENT}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="bindingSource"):
        resolver.load_release_binding(_AGENT)


def test_load_release_binding_requires_promotable_false(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "RELEASES_FIXTURE_DIR", tmp_path / "releases")
    doc = _base_binding_doc(definition_version="sha256:" + "0" * 64, profile_version="sha256:" + "1" * 64)
    doc["promotable"] = True
    _write(tmp_path / "releases" / f"{_AGENT}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="promotable"):
        resolver.load_release_binding(_AGENT)


def test_load_release_binding_fails_closed_on_non_mapping_registry_lifecycle(tmp_path, monkeypatch):
    """A scalar where `registryLifecycle` must be a mapping raises
    `ResolverError`, not a raw `AttributeError` from a bare `.get()` call."""
    monkeypatch.setattr(resolver, "RELEASES_FIXTURE_DIR", tmp_path / "releases")
    doc = _base_binding_doc(definition_version="sha256:" + "0" * 64, profile_version="sha256:" + "1" * 64)
    doc["registryLifecycle"] = "oops"
    _write(tmp_path / "releases" / f"{_AGENT}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="registryLifecycle must be a mapping"):
        resolver.load_release_binding(_AGENT)


# ---------------------------------------------------------------------------
# Real, checked-in issue-investigator fixtures — end-to-end, no monkeypatch.
# ---------------------------------------------------------------------------
def test_real_issue_investigator_fixtures_resolve():
    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="f" * 40))
    assert plan.agent == "issue-investigator"
    assert plan.budget_usd == 3.0
    assert plan.timeout_seconds == 7200.0
    assert plan.cluster_workflow_template == "mctl-agents-investigate"
    assert plan.entrypoint == "orchestrator.run_issue_investigator:investigate"
    assert plan.options_builder == "orchestrator.options:build_issue_investigator_options"
    assert plan.approval == {
        "required": False,
        "reason": "investigation only writes a status:proposed proposal; no mutation.",
    }


def test_prompt_hash_changes_when_prompt_source_changes():
    """Sanity check on `_hash_prompt_source`'s inline branch: it hashes the
    real source text, not a static placeholder."""
    from orchestrator import run_issue_investigator

    expected = "sha256:" + hashlib.sha256(inspect.getsource(run_issue_investigator._build_prompt).encode()).hexdigest()
    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="g" * 40))
    assert plan.prompt_hashes == (expected,)


def test_hash_prompt_source_fails_closed_on_empty_module_path():
    """`importlib.import_module("")` raises `ValueError`, not `ImportError` —
    a malformed `inline:` ref must still raise `ResolverError`, never a raw
    traceback (module docstring's fail-closed contract)."""
    with pytest.raises(resolver.ResolverError):
        resolver._hash_prompt_source(PromptSource(kind="inline", value=":symbol"))


def test_hash_prompt_source_fails_closed_on_unretrievable_source():
    """`inspect.getsource()` on a builtin with no source raises `TypeError`,
    outside the import/getattr error set — must still raise `ResolverError`."""
    with pytest.raises(resolver.ResolverError):
        resolver._hash_prompt_source(PromptSource(kind="inline", value="builtins:len"))


@pytest.mark.parametrize(
    "value",
    [
        "/etc/hosts",            # absolute: pathlib discards REPO_ROOT entirely
        "../../../../etc/hosts",  # relative walk-out
    ],
)
def test_a_file_prompt_source_cannot_reach_outside_the_repository(value):
    """`REPO_ROOT / "/etc/hosts"` is `/etc/hosts` — an absolute right-hand
    operand overrides the left in pathlib, and `../` walks out just as well.

    Both must be refused as escapes rather than silently hashed. Uses a path
    that really exists on the host, so a passing test means the guard fired
    and not merely that the file was missing (claude P3 on #234).
    """
    with pytest.raises(resolver.ResolverError, match="escapes the repository root"):
        resolver._hash_prompt_source(PromptSource(kind="file", value=value))


def test_an_absent_fixture_tree_is_named_not_reported_as_a_missing_profile(monkeypatch):
    """The Dockerfile ships `tests/fixtures/resolver/` explicitly, so the
    tree is present in the image as well as in a checkout — its absence is
    not a normal condition in either.

    That is exactly why the message matters: the failure would otherwise
    surface as "missing execution profile issue-investigator-default",
    which reads like a catalog typo and sends the operator looking for one
    file to add, when in fact nothing is there at all (claude P2 on #234).
    """
    monkeypatch.setattr(resolver, "FIXTURES_DIR", resolver.REPO_ROOT / "no-such-dir")

    with pytest.raises(resolver.ResolverError) as excinfo:
        resolver.execute("issue-investigator", resolver.Task(target_repository_sha="a" * 40))

    message = str(excinfo.value)
    assert "no fixtures to resolve against" in message
    # The old, misleading message must not be what surfaces.
    assert "missing execution profile" not in message
