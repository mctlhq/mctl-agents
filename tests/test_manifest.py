"""Gate agents/_manifests/*/agent.yaml the same way tests/test_agent_inventory.py
gates docs/agent-inventory.yaml: every claim a manifest makes must be checked
against the real code, or it will eventually be wrong and nothing will notice.

This is also where the inventory's stated purpose gets enforced — see
docs/agent-inventory.yaml's header: "Consumed by
orchestrator/validate_manifest.py (phase 1) to assert that every agent listed
here has a manifest and vice versa."
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from _pytest.outcomes import Failed, Skipped

from config.model_policy import resolve_model
from orchestrator import validate_manifest as validate_manifest_module
from orchestrator.manifest import AgentManifest, ManifestError, load, load_all
from orchestrator.validate_manifest import (
    GITOPS_CATALOG_PROFILES_DIR,
    _check_legacy_env_override,
    _check_prompt_sources,
    check_binding_pins_match_definitions,
    check_catalog_profiles_match_builders,
    check_manifests_match_inventory,
    validate,
)

MANIFESTS = load_all()


def _manifest_ids() -> list[str]:
    return list(MANIFESTS)


@pytest.mark.parametrize("manifest", MANIFESTS.values(), ids=_manifest_ids())
def test_manifest_is_valid(manifest: AgentManifest) -> None:
    """Runs every check validate_manifest.py runs: entrypoints resolve, prompt
    sources exist, modelPolicy.task is real, and toolPolicy/execution match
    what orchestrator.options actually builds."""
    errors = validate(manifest)
    assert not errors, f"{manifest.name}: {errors}"


def test_manifests_match_inventory() -> None:
    errors = check_manifests_match_inventory(MANIFESTS)
    assert not errors, errors


def test_catalog_profiles_match_builders() -> None:
    """The mctl-gitops ExecutionProfile catalog must state real tool grants.

    **The skip is conditional on NOT being under CI**, and getting that
    wrong once already is why it is spelled out here. An unconditional
    `pytest.skip` when the checkout is absent intercepts execution before
    `_gitops_missing` can turn absence into an error — so a failed checkout
    in CI would produce a green build from a test that never ran, which is
    the precise failure this PR exists to remove, reproduced inside its own
    test (agy P2 on #284).

    Locally the skip is right: a developer without the sibling checkout
    should see a visible skip rather than a failure about someone else's
    repository.
    """
    if not GITOPS_CATALOG_PROFILES_DIR.is_dir() and not os.environ.get("CI"):
        pytest.skip(f"mctl-gitops catalog not checked out at {GITOPS_CATALOG_PROFILES_DIR}")
    errors = check_catalog_profiles_match_builders(MANIFESTS)
    assert not errors, errors


def test_catalog_check_does_not_leave_options_reloaded(monkeypatch) -> None:
    """The catalog check must not pin orchestrator.options to cleared env.

    Its sibling `_check_tool_policy_and_budget_match_options_py` reloads
    orchestrator.options against a cleared environment and restores it in a
    `finally`; a caller that borrows that helper and forgets the restore
    leaves the module pinned to coded defaults for the rest of the process,
    silently changing what every later reader sees (claude P2 on #284).

    This check sidesteps it entirely — allowed_tools depends on none of the
    cleared vars — and that is asserted here rather than argued in a comment,
    since "we don't need the helper" is exactly the kind of claim that stops
    being true when someone adds a budget comparison later.
    """
    if not GITOPS_CATALOG_PROFILES_DIR.is_dir() and not os.environ.get("CI"):
        pytest.skip("mctl-gitops catalog not checked out")
    monkeypatch.setenv("IMPLEMENTER_BUDGET_USD", "42.00")
    import orchestrator.options as options_module

    try:
        importlib.reload(options_module)
        assert options_module.IMPLEMENTER_BUDGET_USD == 42.00

        assert check_catalog_profiles_match_builders(MANIFESTS) == []

        assert options_module.IMPLEMENTER_BUDGET_USD == 42.00, (
            "orchestrator.options was left reloaded against a cleared environment"
        )
    finally:
        # monkeypatch restores os.environ but NOT sys.modules, so without
        # this the module stays at 42.00 for every later test in the session
        # — this test would leak exactly the state it exists to detect (agy
        # P3 on #284). The env var is already gone by the time the reload
        # runs at teardown order, so pop it explicitly first.
        os.environ.pop("IMPLEMENTER_BUDGET_USD", None)
        importlib.reload(options_module)


def test_an_unmapped_catalog_profile_is_an_error_not_a_skip(tmp_path, monkeypatch) -> None:
    """A profile nobody mapped must fail, not pass unchecked.

    This is the failure mode the check is most likely to die of: someone adds
    a fourth profile, `_AGENT_BY_CATALOG_PROFILE.get()` returns None, and a
    lenient lookup waves it through forever. Asserting on the behaviour
    rather than on the table's contents, so the test survives the table
    growing legitimately.
    """
    profiles = tmp_path / "execution-profiles" / "brand-new-default"
    profiles.mkdir(parents=True)
    (profiles / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "spec": {
                    "tools": ["Read"],
                    "runtime": {"optionsBuilder": "orchestrator.options:build_shepherd_options"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_PROFILES_DIR", profiles.parent
    )
    errors = check_catalog_profiles_match_builders(MANIFESTS)
    assert any("brand-new-default" in e and "_AGENT_BY_CATALOG_PROFILE" in e for e in errors), errors


@pytest.mark.parametrize(
    "spec",
    [None, "a string", [], {"tools": "Read"}],
    ids=["empty-spec", "string-spec", "list-spec", "tools-not-a-list"],
)
def test_a_malformed_profile_is_reported_not_crashed(tmp_path, monkeypatch, spec) -> None:
    """One bad file in ANOTHER repo must not abort the whole validation run.

    `spec:` with nothing under it parses as None, and `tools:` can be any
    YAML node — so reading them outside the guard raises AttributeError or
    TypeError, kills the run and hides every other profile's result behind a
    file this repo does not own (agy P2 on #284).

    Same shape claude flagged on the mctl-gitops side of this work, which is
    why it is parametrized over four malformations rather than fixed for the
    one that was reported: the defect is "risky reads outside the guard", not
    "empty spec".
    """
    profiles = tmp_path / "execution-profiles" / "issue-investigator-default"
    profiles.mkdir(parents=True)
    (profiles / "profile.yaml").write_text(yaml.safe_dump({"spec": spec}), encoding="utf-8")
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_PROFILES_DIR", profiles.parent
    )

    errors = check_catalog_profiles_match_builders(MANIFESTS)

    assert errors, "a malformed profile produced no error at all"
    assert all("profile.yaml" in e for e in errors), errors


def test_an_empty_catalog_directory_is_an_error(tmp_path, monkeypatch) -> None:
    """A directory that exists but holds no profiles checked nothing.

    Same silent-no-op shape as the missing checkout, reached differently: a
    rename or restructure in mctl-gitops leaves the path valid and the glob
    empty, and the loop would return [] having validated zero profiles (agy
    P3 on #284).
    """
    empty = tmp_path / "execution-profiles"
    empty.mkdir()
    monkeypatch.setattr(validate_manifest_module, "GITOPS_CATALOG_PROFILES_DIR", empty)

    errors = check_catalog_profiles_match_builders(MANIFESTS)

    assert errors and "no */profile.yaml" in errors[0], errors


def test_a_missing_gitops_checkout_fails_under_ci(tmp_path, monkeypatch) -> None:
    """Absence must be an error where the check is the only thing looking.

    Locally it degrades to a warning so a developer without the sibling
    checkout still gets a usable run; under CI it must fail, because a check
    that silently returns [] reports success for precisely the environment
    that gates the merge.
    """
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_PROFILES_DIR", tmp_path / "absent"
    )

    monkeypatch.delenv("CI", raising=False)
    assert check_catalog_profiles_match_builders(MANIFESTS) == []

    monkeypatch.setenv("CI", "true")
    errors = check_catalog_profiles_match_builders(MANIFESTS)
    assert errors and "absent" in errors[0]


def test_no_duplicate_manifest_names() -> None:
    """load_all() itself raises ManifestError on a duplicate — this test just
    documents that MANIFESTS having loaded at module level is already proof,
    and gives a named place for that invariant to fail from if the loader's
    behaviour ever changes."""
    assert len(MANIFESTS) == len(set(MANIFESTS))


def test_validate_does_not_leak_clean_env_reload_into_module() -> None:
    """validate() reloads orchestrator.options with budget/timeout env vars
    cleared, to compare the manifest against the coded default regardless of
    the caller's shell. That reload mutates the shared module object in
    place — if it isn't undone, a later test in this same pytest session that
    relies on a real IMPLEMENTER_BUDGET_USD override would silently see the
    coded default instead. Runs validate() (which pins the module to its
    clean-env state internally) then asserts the module has already been
    restored to reflect the override still sitting in os.environ."""
    os.environ["IMPLEMENTER_BUDGET_USD"] = "42.00"
    try:
        errors = validate(MANIFESTS["implementer"])
        assert not errors, errors  # comparison still passes: it ignored the override, as intended

        # Deliberately NOT reloading here — reload() would restore the module
        # itself and mask the bug this test guards against. Read whatever
        # validate() already left behind in sys.modules.
        import orchestrator.options as options_module

        assert options_module.IMPLEMENTER_BUDGET_USD == 42.00, (
            "orchestrator.options was left reloaded with the clean-env default "
            "instead of being restored to reflect the real os.environ override"
        )
    finally:
        os.environ.pop("IMPLEMENTER_BUDGET_USD", None)
        import orchestrator.options as options_module

        importlib.reload(options_module)


def test_legacy_env_override_typo_is_rejected() -> None:
    """modelPolicy.legacyEnvOverride claims the agent's own driver module
    reads this exact env var name via os.getenv — a typo'd or stale name
    must fail, not silently pass."""
    real = MANIFESTS["issue-investigator"]
    assert _check_legacy_env_override(real) == []

    typo = dataclasses.replace(real, model_policy_legacy_env_override="TYPO_VAR")
    errors = _check_legacy_env_override(typo)
    assert len(errors) == 1
    assert "TYPO_VAR" in errors[0]


def test_inventory_binding_mismatch_is_rejected() -> None:
    """check_manifests_match_inventory must compare more than the agent name
    set — a manifest whose runtime.entrypoint was rebound to a different
    agent's real callable (e.g. a copy-paste mistake) has to fail here, since
    a registry consuming it would then dispatch the wrong agent."""
    mismatched = dict(MANIFESTS)
    mismatched["service-agent"] = dataclasses.replace(
        MANIFESTS["service-agent"], entrypoint="orchestrator.run_mentor:run_mentor"
    )
    errors = check_manifests_match_inventory(mismatched)
    assert any("service-agent" in e and "entrypoint" in e for e in errors), errors


def test_prompt_source_glob_rejects_emptied_directory() -> None:
    """A recursive glob ending in `/**` matches the directory itself even
    when every file under it has been deleted — that must not count as a
    match, or the version hash would silently stop covering that source."""
    with tempfile.TemporaryDirectory() as tmp:
        empty_dir = Path(tmp) / "agents" / "foo" / "context" / "empty"
        empty_dir.mkdir(parents=True)
        source = type(MANIFESTS["service-agent"].prompt_sources[0])(kind="glob", value="agents/*/context/empty/**")
        manifest = dataclasses.replace(MANIFESTS["service-agent"], prompt_sources=(source,))

        import orchestrator.validate_manifest as validate_manifest_module

        original_root = validate_manifest_module.REPO_ROOT
        validate_manifest_module.REPO_ROOT = Path(tmp)
        try:
            errors = _check_prompt_sources(manifest)
        finally:
            validate_manifest_module.REPO_ROOT = original_root
        assert errors == [f"prompt source glob matches no files: {source.value}"]


def test_malformed_manifest_field_raises_manifest_error() -> None:
    """A structurally-valid-YAML manifest with a wrong field type/value (e.g.
    execution.budgetUsd that isn't a number) must raise ManifestError, not a
    raw AttributeError/TypeError/ValueError — main()'s per-manifest loop only
    catches ManifestError, so an unwrapped exception would abort validation
    of every later manifest in the same run."""
    document = {
        "apiVersion": "agents.mctl.ai/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": "badtest", "owner": "test-owner"},
        "spec": {
            "runtime": {"type": "claude-agent-sdk", "entrypoint": "a:b", "optionsBuilder": "a:b"},
            "prompt": {"sources": [{"file": "x"}]},
            "modelPolicy": {"task": "x"},
            "execution": {
                "budgetUsd": "not-a-number",
                "sandbox": {"backend": "argo", "clusterWorkflowTemplate": "x"},
            },
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "badtest" / "agent.yaml"
        path.parent.mkdir()
        path.write_text(yaml.dump(document))
        with pytest.raises(ManifestError):
            load(path)


def test_legacy_env_override_omitted_for_agent_that_has_one_is_rejected() -> None:
    """The inverse of the typo case: an agent whose driver DOES read a
    legacy override (issue-investigator, incident-responder) must declare
    it — omitting the field would understate what actually controls the
    agent's model, and the inventory comparison can't catch this since it
    doesn't track this field at all."""
    omitted = dataclasses.replace(MANIFESTS["issue-investigator"], model_policy_legacy_env_override=None)
    errors = _check_legacy_env_override(omitted)
    assert len(errors) == 1
    assert "ISSUE_INVESTIGATOR_MODEL" in errors[0]


def test_legacy_env_override_declared_for_unmapped_agent_is_rejected() -> None:
    """An agent with no entry in _LEGACY_MODEL_ENV_VAR_BY_AGENT (e.g. mentor)
    declaring modelPolicy.legacyEnvOverride anyway would be an unchecked
    claim — there's nothing in the mapping to verify it against."""
    spurious = dataclasses.replace(MANIFESTS["mentor"], model_policy_legacy_env_override="SOME_VAR")
    errors = _check_legacy_env_override(spurious)
    assert len(errors) == 1
    assert "SOME_VAR" in errors[0]


def test_prompt_source_file_rejects_directory() -> None:
    """A `file:` source accidentally repointed at a directory must fail —
    same drift-protection gap the glob branch closes with is_file()."""
    source = type(MANIFESTS["mentor"].prompt_sources[0])(kind="file", value="orchestrator")
    manifest = dataclasses.replace(MANIFESTS["mentor"], prompt_sources=(source,))
    errors = _check_prompt_sources(manifest)
    assert len(errors) == 1
    assert "orchestrator" in errors[0]


def test_inventory_duplicate_agent_names_are_rejected() -> None:
    """A dict keyed by name would otherwise silently keep only the last of
    two same-named inventory entries, reporting success for an inventory
    that no longer has a one-to-one relationship with manifests."""
    import orchestrator.validate_manifest as validate_manifest_module

    real_inventory_text = validate_manifest_module.INVENTORY.read_text(encoding="utf-8")
    real_inventory = yaml.safe_load(real_inventory_text)
    duplicated = dict(real_inventory)
    duplicated["agents"] = [*real_inventory["agents"], real_inventory["agents"][0]]

    with tempfile.TemporaryDirectory() as tmp:
        fake_inventory_path = Path(tmp) / "agent-inventory.yaml"
        fake_inventory_path.write_text(yaml.dump(duplicated))

        original_inventory = validate_manifest_module.INVENTORY
        validate_manifest_module.INVENTORY = fake_inventory_path
        try:
            errors = check_manifests_match_inventory(MANIFESTS)
        finally:
            validate_manifest_module.INVENTORY = original_inventory

    assert any("duplicate agent names" in e and real_inventory["agents"][0]["name"] in e for e in errors), errors


# ---------------------------------------------------------------------------
# v1alpha2 (mctlhq/mctl-agents#227 declarative resolver pilot)
# ---------------------------------------------------------------------------
def test_issue_investigator_manifest_is_v1alpha2() -> None:
    """The one agent this pilot migrates — every other manifest stays
    v1alpha1 (see test_only_issue_investigator_is_v1alpha2 below)."""
    manifest = MANIFESTS["issue-investigator"]
    assert manifest.api_version == "agents.mctl.ai/v1alpha2"
    # The mctl-gitops catalog profile, and a SEMVER RANGE rather than the
    # sha256 content pin this carried while the profile lived under
    # tests/fixtures/ (#277 step 4). The range is asserted for shape, not
    # value, so a legitimate widening does not have to be edited here twice.
    assert manifest.execution_profile_ref["name"] == "issue-investigator-default"
    compatibility = manifest.execution_profile_ref["compatibility"]
    assert not compatibility.startswith("sha256:")
    assert re.fullmatch(r"(?:[<>]=?|==)\s*\d+(?:\.\d+)*(?:\s+(?:[<>]=?|==)\s*\d+(?:\.\d+)*)*", compatibility), (
        f"executionProfileRef.compatibility {compatibility!r} is not a comparator range"
    )


def test_only_issue_investigator_is_v1alpha2() -> None:
    """T1: v1alpha1 remains valid for every other agent — the pilot migrates
    exactly one manifest, not the whole directory."""
    for name, manifest in MANIFESTS.items():
        if name == "issue-investigator":
            continue
        assert manifest.api_version == "agents.mctl.ai/v1alpha1", name
        assert manifest.execution_profile_ref is None, name


def test_unknown_api_version_fails_loudly() -> None:
    """T1: an apiVersion that is neither v1alpha1 nor v1alpha2 must fail
    loudly, not silently fall back to either schema."""
    document = {
        "apiVersion": "agents.mctl.ai/v1alpha99",
        "kind": "Agent",
        "metadata": {"name": "badtest", "owner": "test-owner"},
        "spec": {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "badtest" / "agent.yaml"
        path.parent.mkdir()
        path.write_text(yaml.dump(document))
        with pytest.raises(ManifestError, match="unsupported apiVersion"):
            load(path)


def test_v1alpha2_wrong_kind_is_rejected() -> None:
    document = {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "Agent",  # v1alpha1's kind, not v1alpha2's "AgentDefinition"
        "metadata": {"name": "badtest", "owner": "test-owner"},
        "spec": {
            "prompt": {"sources": [{"file": "x"}]},
            "executionProfileRef": {"name": "x", "compatibility": "sha256:00"},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "badtest" / "agent.yaml"
        path.parent.mkdir()
        path.write_text(yaml.dump(document))
        with pytest.raises(ManifestError, match="kind must be 'AgentDefinition'"):
            load(path)


def test_v1alpha2_missing_execution_profile_ref_is_rejected() -> None:
    document = {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "AgentDefinition",
        "metadata": {"name": "badtest", "owner": "test-owner"},
        "spec": {"prompt": {"sources": [{"file": "x"}]}},
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "badtest" / "agent.yaml"
        path.parent.mkdir()
        path.write_text(yaml.dump(document))
        with pytest.raises(ManifestError, match="executionProfileRef"):
            load(path)


def test_manifest_without_owner_is_rejected() -> None:
    """metadata.owner used to default to "" silently — nothing else checks
    it, so an omitted/misspelled owner would lose phase-1's ownership
    metadata with no gate noticing."""
    document = {
        "apiVersion": "agents.mctl.ai/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": "badtest"},
        "spec": {
            "runtime": {"type": "claude-agent-sdk", "entrypoint": "a:b", "optionsBuilder": "a:b"},
            "prompt": {"sources": [{"file": "x"}]},
            "modelPolicy": {"task": "x"},
            "execution": {"budgetUsd": 1.0, "sandbox": {"backend": "argo", "clusterWorkflowTemplate": "x"}},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "badtest" / "agent.yaml"
        path.parent.mkdir()
        path.write_text(yaml.dump(document))
        with pytest.raises(ManifestError, match="owner"):
            load(path)


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("issue_investigator", "not a task in config/model-policy.yaml"),
        ("shepherd", "not a task in config/model-policy.yaml"),
        (None, "missing or not a string"),
        (["service_agent"], "missing or not a string"),
    ],
    ids=["phantom-investigator-task", "phantom-shepherd-task", "absent", "not-a-string"],
)
def test_a_catalog_profile_naming_an_unknown_model_task_is_an_error(
    tmp_path, monkeypatch, task, expected
) -> None:
    """A profile must name a model-policy task that exists in THIS repo.

    The two phantom ids are the real values the catalog carried until
    2026-09-03. mctl-gitops' own `knownModelPolicyTasks` allowlist listed
    both, so its validator compared each profile against a list nobody had
    compared to `config/model-policy.yaml` — the profile was checked, the
    allowlist was not. `resolve_model()` raises `ModelPolicyError` on either,
    so #277 step 4 would have failed closed on its first resolution.

    Parametrized over absence and a non-string too: this check reads a nested
    key, and "task is missing entirely" must fail as loudly as "task is
    wrong" rather than falling through as None == not-in-tasks by accident.
    """
    profiles = tmp_path / "execution-profiles" / "shepherd-default"
    profiles.mkdir(parents=True)
    spec = {
        "tools": ["Read"],
        "runtime": {"optionsBuilder": "orchestrator.options:build_shepherd_options"},
    }
    if task is not None:
        spec["modelPolicyRef"] = {"task": task}
    (profiles / "profile.yaml").write_text(yaml.safe_dump({"spec": spec}), encoding="utf-8")
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_PROFILES_DIR", profiles.parent
    )

    errors = check_catalog_profiles_match_builders(MANIFESTS)

    assert any("shepherd-default" in e and expected in e for e in errors), errors


def test_every_real_catalog_profile_names_a_resolvable_model_task() -> None:
    """The end the check exists for: every task the catalog names must
    actually resolve through config/model_policy.py, not merely appear in a
    dict. Asserted against `resolve_model()` itself rather than against the
    same `tasks` mapping the check reads, so a profile naming a key that
    parses but cannot resolve is still caught."""
    profiles = sorted(GITOPS_CATALOG_PROFILES_DIR.glob("*/profile.yaml"))
    if not profiles:
        # The skip idiom the sibling tests use is NOT enough here, and the
        # first version of this test got it wrong (agy P2 on #288). Those
        # tests call check_catalog_profiles_match_builders(), which turns an
        # absent catalog into an error under CI itself. This one iterates the
        # glob directly — and a glob over a missing or emptied directory
        # yields nothing without raising, so under CI the skip was bypassed,
        # the loop body never ran, and the test passed green having checked
        # zero profiles.
        #
        # Emptiness covers both causes deliberately: no checkout at all, and
        # a checkout whose layout moved.
        if os.environ.get("CI"):
            pytest.fail(
                f"no */profile.yaml under {GITOPS_CATALOG_PROFILES_DIR} under CI — the "
                "catalog is not checked out or has moved, and this test checked nothing"
            )
        pytest.skip(f"mctl-gitops catalog not checked out at {GITOPS_CATALOG_PROFILES_DIR}")
    for profile_path in profiles:
        spec = yaml.safe_load(profile_path.read_text(encoding="utf-8"))["spec"]
        task = spec["modelPolicyRef"]["task"]
        resolve_model(task, log=False)


def test_the_real_catalog_task_test_fails_rather_than_no_ops_under_ci(tmp_path, monkeypatch) -> None:
    """The guard above must fail under CI, not pass having iterated nothing.

    Asserting on the behaviour rather than on the presence of the guard: a
    glob over a missing directory raises nothing and yields nothing, so the
    first version of that test bypassed its own skip under CI and then passed
    green with zero profiles checked (agy P2 on #288) — the exact silent-no-op
    shape this PR exists to remove, reproduced inside its own test.

    Locally the same absence must stay a visible skip: a developer without the
    sibling checkout should not get a failure about someone else's repository.
    """
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_PROFILES_DIR", tmp_path / "absent"
    )
    monkeypatch.setattr(
        sys.modules[__name__], "GITOPS_CATALOG_PROFILES_DIR", tmp_path / "absent"
    )

    monkeypatch.setenv("CI", "true")
    with pytest.raises(Failed, match="checked nothing"):
        test_every_real_catalog_profile_names_a_resolvable_model_task()

    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(Skipped):
        test_every_real_catalog_profile_names_a_resolvable_model_task()


def _write_binding(root: Path, agent: str, *, path: str, content_hash: str) -> Path:
    """One ReleaseBindingIntent under <root>/<env>/<agent>.yaml, catalog shape."""
    target = root / "shadow" / f"{agent}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "agents.mctl.ai/v1alpha2",
                "kind": "ReleaseBindingIntent",
                "metadata": {"agent": agent, "environment": "shadow"},
                "spec": {
                    "sourceManifest": {
                        "repo": "mctlhq/mctl-agents",
                        "path": path,
                        "gitSha": "0" * 40,
                        "contentHash": content_hash,
                    },
                    "bindingSource": "compatibility-fixture",
                    "promotable": False,
                    "definition": {"name": agent, "version": "1"},
                },
            }
        ),
        encoding="utf-8",
    )
    return target


def test_every_real_binding_pin_matches_its_definition() -> None:
    """The end this check exists for: all three catalog bindings pin the real
    sha256 of the manifest they name, not just the one the resolver reads.

    Emptiness is the guard, not `is_dir()`: a glob over a missing or
    restructured directory yields nothing without raising, so under CI the
    skip would be bypassed and the loop would verify zero bindings — the
    silent-no-op shape that reached review on #288.
    """
    bindings = sorted(validate_manifest_module.GITOPS_CATALOG_RELEASES_DIR.glob("*/*.yaml"))
    if not bindings:
        if os.environ.get("CI"):
            pytest.fail(
                f"no <env>/<agent>.yaml under "
                f"{validate_manifest_module.GITOPS_CATALOG_RELEASES_DIR} under CI — the "
                "release catalog is not checked out or has moved, and this test checked nothing"
            )
        pytest.skip("mctl-gitops release catalog not checked out")
    assert check_binding_pins_match_definitions(MANIFESTS) == []


def test_a_stale_pin_is_reported(tmp_path, monkeypatch) -> None:
    """A definition edited without re-pinning must fail.

    This is the whole point: two of the three pins are read by nothing else
    (only `issue-investigator` resolves), so without this check they would rot
    silently (#293).
    """
    releases = tmp_path / "releases"
    _write_binding(
        releases, "shepherd",
        path="agents/_manifests/shepherd/agent.yaml",
        content_hash="sha256:" + "0" * 64,
    )
    monkeypatch.setattr(validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", releases)

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert any("does not match" in e and "shepherd" in e for e in errors), errors


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/etc/passwd", "escapes"),
        ("../../../../etc/passwd", "escapes"),
        # Both of these are "not shepherd's manifest" — one names a file that
        # does not exist, the other a real manifest belonging to a different
        # agent. The path cross-check subsumes existence, and says the more
        # useful thing: the problem is not that a file is missing, it is that
        # this binding points somewhere it must not.
        ("agents/_manifests/nope/agent.yaml", "is not shepherd's manifest"),
        ("agents/_manifests/implementer/agent.yaml", "is not shepherd's manifest"),
    ],
    ids=["absolute", "traversal", "nonexistent", "wrong-agent"],
)
def test_a_binding_cannot_pin_the_wrong_file(tmp_path, monkeypatch, path, expected) -> None:
    """`sourceManifest.path` comes from another repository, so it is checked
    rather than trusted.

    The `wrong-agent` case is the one that is not about safety: a binding
    pinning the CORRECT hash of the WRONG manifest would otherwise validate
    green forever, which is a pin that proves nothing about the agent it
    claims to bind.
    """
    releases = tmp_path / "releases"
    _write_binding(releases, "shepherd", path=path, content_hash="sha256:" + "0" * 64)
    monkeypatch.setattr(validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", releases)

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize(
    "content_hash", [None, "", "not-a-hash", "1234", 42],
    ids=["absent", "empty", "unprefixed", "digits", "not-a-string"],
)
def test_a_missing_or_malformed_pin_is_reported(tmp_path, monkeypatch, content_hash) -> None:
    """A binding without a usable pin must fail, not read as "nothing to
    check" — that is exactly how the pin came to be unverified for two of the
    three agents in the first place."""
    releases = tmp_path / "releases"
    binding = _write_binding(
        releases, "shepherd",
        path="agents/_manifests/shepherd/agent.yaml",
        content_hash="sha256:" + "0" * 64,
    )
    document = yaml.safe_load(binding.read_text(encoding="utf-8"))
    if content_hash is None:
        del document["spec"]["sourceManifest"]["contentHash"]
    else:
        document["spec"]["sourceManifest"]["contentHash"] = content_hash
    binding.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", releases)

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert any("unpinned" in e for e in errors), errors


def test_an_empty_release_directory_is_an_error(tmp_path, monkeypatch) -> None:
    """A directory that exists but holds no bindings verified nothing.

    Same shape as the missing checkout, reached by a rename in mctl-gitops
    instead of an absent clone — and the loop would return [] having checked
    zero pins.
    """
    empty = tmp_path / "releases"
    empty.mkdir()
    monkeypatch.setattr(validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", empty)

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert errors and "no <environment>/<agent>.yaml" in errors[0], errors


@pytest.mark.parametrize(
    "spec", [None, "a string", [], {"sourceManifest": "oops"}],
    ids=["empty-spec", "string-spec", "list-spec", "source-not-a-mapping"],
)
def test_a_malformed_binding_is_reported_not_crashed(tmp_path, monkeypatch, spec) -> None:
    """One bad file in ANOTHER repository must not abort the run and hide
    every other binding's result behind it."""
    releases = tmp_path / "releases" / "shadow"
    releases.mkdir(parents=True)
    (releases / "shepherd.yaml").write_text(yaml.safe_dump({"spec": spec}), encoding="utf-8")
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", releases.parent
    )

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert errors, "a malformed binding produced no error at all"
    assert all("shepherd.yaml" in e for e in errors), errors


def test_a_missing_release_checkout_fails_under_ci(tmp_path, monkeypatch) -> None:
    """Absence must be an error where this check is the only thing looking."""
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", tmp_path / "absent"
    )
    monkeypatch.delenv("CI", raising=False)
    assert check_binding_pins_match_definitions(MANIFESTS) == []

    monkeypatch.setenv("CI", "true")
    errors = check_binding_pins_match_definitions(MANIFESTS)
    assert errors and "absent" in errors[0]


@pytest.mark.parametrize(
    ("agent_field", "expected"),
    [
        ({"version": "1"}, "spec.definition.name is required"),
        ({"name": "typoed-agent", "version": "1"}, "unknown agent"),
        (None, "spec.definition.name is required"),
    ],
    ids=["name-absent", "name-unknown", "definition-absent"],
)
def test_an_unresolvable_agent_is_an_error_not_a_skipped_cross_check(
    tmp_path, monkeypatch, agent_field, expected
) -> None:
    """A binding whose agent cannot be resolved must fail.

    The first version fell through -- `if agent_name and agent_name in
    manifests:` -- so a binding with an absent, typo'd or renamed
    `definition.name` skipped the path cross-check entirely and was accepted
    as long as its hash matched SOME real file. That is precisely the "right
    hash of the wrong manifest" case the cross-check exists to refuse, and it
    was reachable by a typo. Both reviewers on #294 found it independently and
    both noted no test covered the path.

    The binding here pins shepherd's REAL path and hash, so nothing else can
    fail: the only reason this must error is the unresolvable agent.
    """
    releases = tmp_path / "releases"
    real = MANIFESTS["shepherd"].path
    binding = _write_binding(
        releases, "shepherd",
        path=real.relative_to(validate_manifest_module.REPO_ROOT).as_posix(),
        content_hash="sha256:" + hashlib.sha256(real.read_bytes()).hexdigest(),
    )
    document = yaml.safe_load(binding.read_text(encoding="utf-8"))
    if agent_field is None:
        del document["spec"]["definition"]
    else:
        document["spec"]["definition"] = agent_field
    binding.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", releases)

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert any(expected in e for e in errors), errors


def test_a_control_character_in_the_path_is_reported_not_crashed(tmp_path, monkeypatch) -> None:
    """YAML permits control characters in a double-quoted scalar, and a NUL
    raises `ValueError: embedded null byte` inside pathlib -- before any check
    of ours runs.

    Path construction used to sit OUTSIDE the try/except, so that ValueError
    escaped the function, aborted main(), and hid every other binding's result
    and every later check (claude P2 on #294) -- the exact failure the
    malformed-binding test above claims this function guards against.
    """
    releases = tmp_path / "releases" / "shadow"
    releases.mkdir(parents=True)
    (releases / "shepherd.yaml").write_text(
        'apiVersion: agents.mctl.ai/v1alpha2\n'
        'kind: ReleaseBindingIntent\n'
        'metadata: {agent: shepherd, environment: shadow}\n'
        'spec:\n'
        '  sourceManifest:\n'
        '    repo: mctlhq/mctl-agents\n'
        '    path: "\\0/etc/passwd"\n'
        f'    contentHash: "sha256:{"0" * 64}"\n'
        '  definition: {name: shepherd, version: "1"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", releases.parent
    )

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert errors, "a NUL in the path produced no error at all"
    assert all("shepherd.yaml" in e for e in errors), errors


def test_bindings_for_another_repository_are_skipped_but_never_silently(
    tmp_path, monkeypatch
) -> None:
    """A binding for a different repository cannot be verified here — its file
    is not in this checkout — so it is skipped. But if EVERY binding is
    skipped that way, the loop would return [] having verified nothing and
    read as success (agy P3 on #294). Emptiness of the verified set is
    therefore its own error, the same shape as the empty-directory guard.
    """
    releases = tmp_path / "releases"
    binding = _write_binding(
        releases, "someone-else",
        path="agents/_manifests/shepherd/agent.yaml",
        content_hash="sha256:" + "0" * 64,
    )
    document = yaml.safe_load(binding.read_text(encoding="utf-8"))
    document["spec"]["sourceManifest"]["repo"] = "mctlhq/some-other-repo"
    binding.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(validate_manifest_module, "GITOPS_CATALOG_RELEASES_DIR", releases)

    errors = check_binding_pins_match_definitions(MANIFESTS)

    assert errors and "none targets mctlhq/mctl-agents" in errors[0], errors
