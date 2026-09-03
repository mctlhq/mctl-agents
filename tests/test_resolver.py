"""Tests for orchestrator/resolver.py — mctlhq/mctl-agents#227's declarative
`execute(agent, task)` pilot. T1-T9 below map onto that proposal's
tasks.md "## Tests" section; see orchestrator/resolver.py's module docstring
for the fail-closed contract every negative test here asserts.

Every test builds its own isolated fixture tree under `tmp_path` and
monkeypatches resolver's directory constants rather than touching the real
`agents/_manifests/issue-investigator/` files or the real mctl-gitops
catalog —
those are exercised end-to-end by `test_real_issue_investigator_fixtures_resolve`
below plus `tests/test_manifest.py`'s existing parametrized checks.
"""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import re
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
_PROFILE_VERSION = "1.0.0"
_COMPATIBILITY = ">=1.0.0 <2.0.0"


def _content_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _base_profile_doc() -> dict:
    return {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ExecutionProfile",
        "metadata": {"name": _PROFILE, "owner": "platform"},
        "spec": {
            "version": _PROFILE_VERSION,
            "modelPolicyRef": {"task": "service_agent", "compatibility": ">=1.0.0 <2.0.0"},
            "skills": [],
            "tools": ["Read", "Write"],
            "policyRef": "test-policy",
            "permissions": {"targetRepository": "read"},
            "budgetUsd": 3.0,
            "timeoutSeconds": 900.0,
            "runtime": {
                "entrypoint": "orchestrator.run_issue_investigator:investigate",
                "optionsBuilder": "orchestrator.options:build_issue_investigator_options",
                "sandbox": {
                    "backend": "argo",
                    "clusterWorkflowTemplate": "mctl-agents-investigate",
                    "approved": True,
                },
            },
            "approval": {"requiredBefore": []},
            "evidence": {"required": ["proposal"]},
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


def _base_binding_doc(
    *,
    definition_version: str,
    profile_version: str,
    definition_content_hash: str,
    revision: int = 1,
    compatibility: str = _COMPATIBILITY,
) -> dict:
    return {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ReleaseBindingIntent",
        "metadata": {"agent": _AGENT, "environment": resolver.DEFAULT_ENVIRONMENT},
        "spec": {
            "sourceManifest": {
                "repo": "mctlhq/mctl-agents",
                "path": f"agents/_manifests/{_AGENT}/agent.yaml",
                "gitSha": "deadbeef" * 5,
                # The REAL hash of the definition written alongside, not a
                # placeholder: this is the pin execute() checks, and a
                # builder that fed it an arbitrary string would make every
                # test pass whether the gate worked or not — which is how
                # the gate went missing unnoticed in the first place.
                "contentHash": definition_content_hash,
            },
            "bindingSource": "compatibility-fixture",
            "promotable": False,
            "registryLifecycle": {"definition": "published", "profile": "published"},
            "definition": {
                "name": _AGENT,
                "version": definition_version,
                "profileCompatibility": compatibility,
            },
            "profile": {"name": _PROFILE, "version": profile_version},
            "bindingRevision": revision,
        },
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
    catalog_dir = tmp_path / "agent-platform"
    profiles_dir = catalog_dir / "execution-profiles"
    releases_dir = catalog_dir / "releases"
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", definitions_dir)
    monkeypatch.setattr(resolver, "CATALOG_DIR", catalog_dir)
    monkeypatch.setattr(resolver, "CATALOG_PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(resolver, "CATALOG_RELEASES_DIR", releases_dir)

    profile_doc = _base_profile_doc()
    if profile_overrides:
        profile_doc["spec"].update(profile_overrides)
    # Catalog layout: <profiles>/<name>/profile.yaml, not <profiles>/<name>.yaml.
    profile_path = _write(profiles_dir / _PROFILE / "profile.yaml", profile_doc)
    profile_hash = _content_hash(profile_path)
    profile_version = profile_doc["spec"]["version"]

    effective_compatibility = compatibility if compatibility is not None else _COMPATIBILITY
    definition_doc = _base_definition_doc(effective_compatibility)
    if definition_overrides:
        definition_doc["spec"].update(definition_overrides)
    definition_path = _write(definitions_dir / _AGENT / "agent.yaml", definition_doc)
    definition_hash = _content_hash(definition_path)

    if write_binding:
        binding_doc = _base_binding_doc(
            definition_version="1",
            profile_version=profile_version,
            definition_content_hash=definition_hash,
            # The binding mirrors whatever the definition actually declares,
            # so the default set stays self-consistent and the mirror-drift
            # check only fires for tests that deliberately break it.
            compatibility=definition_doc["spec"]["executionProfileRef"]["compatibility"],
        )
        if binding_overrides:
            binding_doc["spec"].update(binding_overrides)
        _write(releases_dir / resolver.DEFAULT_ENVIRONMENT / f"{_AGENT}.yaml", binding_doc)

    return {
        "definitions_dir": definitions_dir,
        "catalog_dir": catalog_dir,
        "profiles_dir": profiles_dir,
        "releases_dir": releases_dir,
        "profile_path": profile_path,
        "definition_path": definition_path,
        "profile_hash": profile_hash,
        "definition_hash": definition_hash,
        "profile_version": profile_version,
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


@pytest.mark.parametrize(
    "constraint", [">=2.0.0 <3.0.0", "<1.0.0", "==1.1.0"],
    ids=["above-range", "below-range", "pinned-elsewhere"],
)
def test_execute_fails_on_compatibility_mismatch(tmp_path, monkeypatch, constraint):
    """T4: compatibility is checked against the CONCRETE selected profile
    version (its declared `spec.version`), never read from a profile-owned
    range."""
    _build_fixture_set(tmp_path, monkeypatch, compatibility=constraint)
    with pytest.raises(resolver.ResolverError, match="compatibility mismatch"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="h" * 40))


def test_execute_fails_when_the_binding_and_the_profile_disagree_on_version(tmp_path, monkeypatch):
    """Bumping a profile's `spec.version` without re-binding must fail
    closed — an 'ambiguous version', not a silent re-resolution of whatever
    the profile file now says."""
    built = _build_fixture_set(tmp_path, monkeypatch)
    profile_doc = _base_profile_doc()
    profile_doc["spec"]["version"] = "1.1.0"
    _write(built["profile_path"], profile_doc)
    with pytest.raises(resolver.ResolverError, match="ambiguous version"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="i" * 40))


def test_a_profile_edited_without_a_version_bump_still_resolves(tmp_path, monkeypatch):
    """The guarantee this pilot GAVE UP when the profile moved to the
    catalog, asserted rather than described.

    While the profile lived under `tests/fixtures/`, its version was the
    sha256 of the file: any content edit moved it, so editing without
    re-pinning the binding could not resolve. The catalog versions profiles
    with a declared `spec.version` — a claim about the content, not the
    content — so an edit that leaves that string alone resolves exactly as
    before, with the NEW values.

    Here the budget changes from 3.0 to 9.0 and the plan carries 9.0 against
    an unchanged `1.0.0`. That is the real behaviour and it must be visible
    in the test suite rather than only in a docstring. Closing the gap needs
    a mctl-gitops CI check comparing a profile's diff against its version
    bump; when that lands, this test is the one that documents what changed.

    The plan's `profile_content_hash` does move, which is the point of
    recording it: provenance survives even where the gate did not.
    """
    built = _build_fixture_set(tmp_path, monkeypatch)
    before = resolver.execute(_AGENT, resolver.Task(target_repository_sha="k" * 40))

    profile_doc = _base_profile_doc()
    profile_doc["spec"]["budgetUsd"] = 9.0
    _write(built["profile_path"], profile_doc)
    after = resolver.execute(_AGENT, resolver.Task(target_repository_sha="k" * 40))

    assert before.budget_usd == 3.0
    assert after.budget_usd == 9.0
    assert before.profile_version == after.profile_version == "1.0.0"
    assert before.profile_content_hash != after.profile_content_hash


def test_execute_fails_when_the_binding_mirrors_a_stale_compatibility(tmp_path, monkeypatch):
    """The binding duplicates the definition's `executionProfileRef.
    compatibility` as `spec.definition.profileCompatibility`, because
    mctl-gitops CI cannot read the mctl-agents source file — its schema says
    so. This process reads both, so it is the only place the mirror is
    checkable at all.

    Left unchecked, gitops CI would evaluate compatibility against its copy
    while the resolver used the real one: two sources of truth wearing one
    name, agreeing right up until someone edited one of them.
    """
    _build_fixture_set(
        tmp_path, monkeypatch,
        binding_overrides={
            "definition": {
                "name": _AGENT,
                "version": "1",
                "profileCompatibility": ">=9.0.0 <10.0.0",
            }
        },
    )
    with pytest.raises(resolver.ResolverError, match="mirror drift"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="m" * 40))


def test_execute_fails_on_unknown_profile_reference(tmp_path, monkeypatch):
    _build_fixture_set(
        tmp_path, monkeypatch,
        binding_overrides={"profile": {"name": "some-other-profile", "version": "1.0.0"}},
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
def test_load_profile_requires_a_declared_version(tmp_path, monkeypatch):
    """`spec.version` is what the binding references and what the
    definition's compatibility range is evaluated against, so a profile
    without one cannot resolve.

    This replaced a `metadata.promotable: false` assertion. The catalog
    schema is `additionalProperties: false` and declares no such field, so
    demanding it here would reject every real profile. Non-promotability is
    still asserted — on the RELEASE BINDING, which is where the catalog
    actually carries it (see the two binding tests below).
    """
    monkeypatch.setattr(resolver, "CATALOG_PROFILES_DIR", tmp_path / "profiles")
    doc = _base_profile_doc()
    del doc["spec"]["version"]
    _write(tmp_path / "profiles" / _PROFILE / "profile.yaml", doc)
    with pytest.raises(resolver.ResolverError, match=r"spec\.version is required"):
        resolver.load_profile(_PROFILE)


def test_load_profile_fails_closed_on_non_mapping_model_policy_ref(tmp_path, monkeypatch):
    """A scalar where `spec.modelPolicyRef` must be a mapping raises
    `ResolverError`, not a raw `AttributeError` from a bare `.get()` call."""
    monkeypatch.setattr(resolver, "CATALOG_PROFILES_DIR", tmp_path / "profiles")
    doc = _base_profile_doc()
    doc["spec"]["modelPolicyRef"] = "oops"
    _write(tmp_path / "profiles" / _PROFILE / "profile.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="modelPolicyRef must be a mapping"):
        resolver.load_profile(_PROFILE)


def test_load_release_binding_requires_compatibility_fixture_source(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "CATALOG_RELEASES_DIR", tmp_path / "releases")
    doc = _base_binding_doc(
        definition_version="1",
        profile_version="1.0.0",
        definition_content_hash="sha256:" + "0" * 64,
    )
    doc["spec"]["bindingSource"] = "registry"
    _write(tmp_path / "releases" / resolver.DEFAULT_ENVIRONMENT / f"{_AGENT}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="bindingSource"):
        resolver.load_release_binding(_AGENT)


def test_load_release_binding_requires_promotable_false(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver, "CATALOG_RELEASES_DIR", tmp_path / "releases")
    doc = _base_binding_doc(
        definition_version="1",
        profile_version="1.0.0",
        definition_content_hash="sha256:" + "0" * 64,
    )
    doc["spec"]["promotable"] = True
    _write(tmp_path / "releases" / resolver.DEFAULT_ENVIRONMENT / f"{_AGENT}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="promotable"):
        resolver.load_release_binding(_AGENT)


def test_load_release_binding_fails_closed_on_non_mapping_registry_lifecycle(tmp_path, monkeypatch):
    """A scalar where `registryLifecycle` must be a mapping raises
    `ResolverError`, not a raw `AttributeError` from a bare `.get()` call."""
    monkeypatch.setattr(resolver, "CATALOG_RELEASES_DIR", tmp_path / "releases")
    doc = _base_binding_doc(
        definition_version="1",
        profile_version="1.0.0",
        definition_content_hash="sha256:" + "0" * 64,
    )
    doc["spec"]["registryLifecycle"] = "oops"
    _write(tmp_path / "releases" / resolver.DEFAULT_ENVIRONMENT / f"{_AGENT}.yaml", doc)
    with pytest.raises(resolver.ResolverError, match="registryLifecycle must be a mapping"):
        resolver.load_release_binding(_AGENT)


# ---------------------------------------------------------------------------
# The real issue-investigator definition against the real mctl-gitops
# catalog — end-to-end, no monkeypatch.
# ---------------------------------------------------------------------------
def _require_real_catalog() -> None:
    """Guard for the tests below, which read the actual mctl-gitops catalog.

    Absence must FAIL under CI and only skip locally. A plain `pytest.skip`
    on `not is_dir()` would let these pass having resolved nothing in the one
    environment that gates the merge — the silent-no-op shape that got
    through review once already on #288, so it is not repeated here.
    """
    if resolver.CATALOG_DIR.is_dir():
        return
    message = (
        f"mctl-gitops catalog not present at {resolver.CATALOG_DIR}; set MCTL_GITOPS_ROOT "
        "or check the repository out as a sibling"
    )
    if os.environ.get("CI"):
        pytest.fail(f"{message} — under CI this must not be skipped")
    pytest.skip(message)


def test_real_issue_investigator_definition_resolves_against_the_catalog():
    _require_real_catalog()
    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="f" * 40))
    assert plan.agent == "issue-investigator"
    assert plan.budget_usd == 3.0
    assert plan.timeout_seconds == 7200.0
    assert plan.cluster_workflow_template == "mctl-agents-investigate"
    assert plan.entrypoint == "orchestrator.run_issue_investigator:investigate"
    assert plan.options_builder == "orchestrator.options:build_issue_investigator_options"
    # Catalog shape, not the fixture's boolean `required`.
    assert plan.approval == {"requiredBefore": []}
    assert plan.evidence == ("proposal",)
    # The values that only exist because this is the real catalog: the
    # corrected model-policy task (#277 / gitops#1002) and the renamed
    # policy (gitops#981).
    assert plan.model == "claude-sonnet-5"
    assert plan.policy_ref == "scoped-proposal-authoring"
    assert plan.binding_source == "compatibility-fixture"
    # The legacy env override the catalog could not express until
    # gitops#1007 -- it OUTRANKS modelPolicyRef.task, so a plan that
    # resolved without it would name a model the environment can silently
    # replace.
    assert plan.model_policy_version.startswith("v1+sha256:")
    # Shape, not a pinned value: asserting "1.1.0" here would make every
    # legitimate profile bump a two-repository edit, and the version's
    # correctness is already gated by the binding/profile agreement inside
    # execute() plus mctl-gitops' own validator.
    assert re.fullmatch(r"\d+\.\d+\.\d+", plan.profile_version), plan.profile_version
    assert plan.profile_content_hash.startswith("sha256:")


def test_prompt_hash_changes_when_prompt_source_changes():
    """Sanity check on `_hash_prompt_source`'s inline branch: it hashes the
    real source text, not a static placeholder."""
    _require_real_catalog()
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


def test_an_absent_catalog_is_named_not_reported_as_a_missing_profile(monkeypatch):
    """CI checks mctl-gitops out, the Argo CWFT clones it and points
    MCTL_GITOPS_ROOT at it, and a developer has it as a sibling — so its
    absence is not a normal condition anywhere.

    That is exactly why the message matters: the failure would otherwise
    surface as "missing execution profile issue-investigator-default",
    which reads like a catalog typo and sends the operator looking for one
    file to add, when in fact no catalog is mounted at all (claude P2 on
    #234, carried across the move to the real catalog).
    """
    monkeypatch.setattr(resolver, "CATALOG_DIR", resolver.REPO_ROOT / "no-such-dir")

    with pytest.raises(resolver.ResolverError) as excinfo:
        resolver.execute("issue-investigator", resolver.Task(target_repository_sha="a" * 40))

    message = str(excinfo.value)
    assert "no catalog to resolve against" in message
    assert "MCTL_GITOPS_ROOT" in message
    # The old, misleading message must not be what surfaces.
    assert "missing execution profile" not in message


def test_the_recorded_hash_describes_the_bytes_that_were_parsed(tmp_path, monkeypatch):
    """Parse and hash must come from ONE read of the file.

    Reading twice — once for `yaml.safe_load`, once for the hash — lets the
    two disagree if the file changes in between, and the plan then pins a
    `content_hash` describing bytes it was not built from. That pin is the
    whole provenance claim, so a pin that can describe different content is
    not a weaker guarantee but the absence of one (claude P3 on #234).

    Simulated by making the file's content change between reads: `read_bytes`
    is stubbed to return different content on each call. With a single read
    the definition simply reflects whichever bytes it got; with two, the
    parsed name and the recorded hash come from different documents.
    """
    built = _build_fixture_set(tmp_path, monkeypatch)
    definition_path = built["definition_path"]
    original = definition_path.read_bytes()
    mutated = original.replace(b"name: " + _AGENT.encode(), b"name: " + _AGENT.encode() + b"-changed")
    assert mutated != original  # the premise: the swap really is a different document

    real_read_bytes = Path.read_bytes
    calls = {"n": 0}

    def flip_flopping_read(self, *args, **kwargs):
        if self == definition_path:
            calls["n"] += 1
            return original if calls["n"] == 1 else mutated
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", flip_flopping_read)

    definition = resolver.load_definition(_AGENT)

    # Exactly one read of this file, so the hash cannot describe the other
    # document — and it matches a hash computed over the bytes that produced
    # the parsed name.
    assert calls["n"] == 1
    assert definition.content_hash == "sha256:" + hashlib.sha256(original).hexdigest()


def test_semver_matches_the_mctl_gitops_implementation():
    """`_version_satisfies` duplicates mctl-gitops' `version_satisfies`.

    There is no package shared between the two repositories, so the
    duplication is unavoidable — but it must not stay an assumption. Both
    evaluate the SAME constraint against the SAME catalog: gitops CI decides
    whether a binding is valid, this resolver decides whether it resolves,
    and a disagreement means one of them accepts a pair the other rejects
    while both report success.

    So the real script is loaded and the two are compared over a table that
    includes the boundary cases a hand-rolled comparator gets wrong:
    exclusive vs inclusive bounds, unequal component counts (zero-padding),
    and a multi-comparator range.
    """
    _require_real_catalog()
    script = resolver.GITOPS_ROOT.parent / "scripts" / "validate-agent-platform.py"
    if not script.is_file():
        pytest.fail(f"cannot cross-check semver: {script} is missing")
    spec = importlib.util.spec_from_file_location("_gitops_validator", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    cases = [
        ("1.0.0", ">=1.0.0 <2.0.0"),
        ("1.9.9", ">=1.0.0 <2.0.0"),
        ("2.0.0", ">=1.0.0 <2.0.0"),
        ("0.9.9", ">=1.0.0 <2.0.0"),
        ("1.0", ">=1.0.0"),
        ("1", ">=1.0.0 <2.0.0"),
        ("1.0.0", "<=1.0.0"),
        ("1.0.1", "<=1.0.0"),
        ("1.0.0", "==1.0.0"),
        ("1.0.0.1", ">1.0.0"),
        ("10.0.0", ">=9.0.0 <11.0.0"),
        ("1.2.3", ">1.2.2 <1.2.4"),
    ]
    for version, constraint in cases:
        mine = resolver._version_satisfies(
            version, constraint, path=Path("x"), field_path="compatibility"
        )
        theirs = module.version_satisfies(version, constraint)
        assert mine == theirs, (
            f"{version!r} vs {constraint!r}: resolver says {mine}, mctl-gitops says {theirs}"
        )


def test_the_catalogs_legacy_env_override_actually_overrides(monkeypatch):
    """`modelPolicyRef.legacyEnvOverride` must reach the resolved model, not
    merely be present in the profile.

    The catalog schema could not express this field until gitops#1007, so
    the profile described a model selection that `ISSUE_INVESTIGATOR_MODEL`
    could silently replace. Asserting the OVERRIDE rather than the string
    means a future profile that drops the field, or a resolver that reads it
    and ignores it, both go red — a field that is declared but not wired is
    the same lie as one that is missing.
    """
    _require_real_catalog()
    task = resolver.Task(target_repository_sha="n" * 40)

    monkeypatch.delenv("ISSUE_INVESTIGATOR_MODEL", raising=False)
    from_policy = resolver.execute("issue-investigator", task).model

    monkeypatch.setenv("ISSUE_INVESTIGATOR_MODEL", "claude-opus-4-8")
    overridden = resolver.execute("issue-investigator", task).model

    assert from_policy != "claude-opus-4-8", "pick a sentinel the policy would not return"
    assert overridden == "claude-opus-4-8"


def test_execute_fails_when_the_definition_drifts_from_its_pin(tmp_path, monkeypatch):
    """Editing the AgentDefinition without re-pinning the binding must fail
    closed.

    This gate existed under the in-repo fixture, was dropped when the
    profile moved to the catalog, and was found by BOTH reviewers on #291 --
    independently, both P1. The catalog's `definition.version` is a registry
    number ("1") that names no bytes, so `spec.sourceManifest.contentHash`
    (gitops#1011) is the only thing pinning this half.

    Asserted on a real edit rather than a hand-written hash: the failure has
    to come from the definition's actual bytes moving, which is what happens
    in practice.
    """
    built = _build_fixture_set(tmp_path, monkeypatch)
    definition_doc = _base_definition_doc(_COMPATIBILITY)
    definition_doc["metadata"]["owner"] = "someone-else"
    _write(built["definition_path"], definition_doc)

    with pytest.raises(resolver.ResolverError, match="ambiguous version"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="p" * 40))


def test_a_binding_without_a_definition_pin_does_not_resolve(tmp_path, monkeypatch):
    """A binding that simply omits the pin must be refused, not treated as
    "nothing to check" -- that is how the gate disappeared the first time."""
    _build_fixture_set(
        tmp_path, monkeypatch,
        binding_overrides={
            "sourceManifest": {
                "repo": "mctlhq/mctl-agents",
                "path": f"agents/_manifests/{_AGENT}/agent.yaml",
                "gitSha": "deadbeef" * 5,
            }
        },
    )
    with pytest.raises(resolver.ResolverError, match=r"sourceManifest\.contentHash"):
        resolver.execute(_AGENT, resolver.Task(target_repository_sha="q" * 40))


@pytest.mark.parametrize(
    "constraint",
    [">=1.0.0 GARBAGE <2.0.0", "~1.0.0", ">=1.0.0 or something", "whatever >=1.0.0"],
    ids=["text-between", "unsupported-operator", "trailing-prose", "leading-prose"],
)
def test_a_partly_understood_constraint_does_not_resolve(constraint):
    """`findall` skips whatever sits BETWEEN matches, so
    ">=1.0.0 GARBAGE <2.0.0" evaluated as ">=1.0.0 <2.0.0" -- a constraint
    nobody wrote, silently honoured, while `_version_satisfies`'s own
    docstring promised an unparseable constraint would raise (agy P3 on
    #291).

    "Only partly understood" is the point: `~1.0.0` has no comparator at all
    and already raised, but the three that contain a VALID comparator plus
    text are the ones that resolved happily.
    """
    with pytest.raises(resolver.ResolverError, match=r"not a parseable constraint|outside its comparators"):
        resolver._version_satisfies(
            "1.0.0", constraint, path=Path("x"), field_path="compatibility"
        )
