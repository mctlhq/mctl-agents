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
import importlib
import os
import tempfile
from pathlib import Path

import pytest
import yaml

from orchestrator.manifest import AgentManifest, ManifestError, load, load_all
from orchestrator.validate_manifest import (
    _check_legacy_env_override,
    _check_prompt_sources,
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
        "metadata": {"name": "badtest"},
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
