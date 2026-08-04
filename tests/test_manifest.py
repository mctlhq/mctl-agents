"""Gate agents/_manifests/*/agent.yaml the same way tests/test_agent_inventory.py
gates docs/agent-inventory.yaml: every claim a manifest makes must be checked
against the real code, or it will eventually be wrong and nothing will notice.

This is also where the inventory's stated purpose gets enforced — see
docs/agent-inventory.yaml's header: "Consumed by
orchestrator/validate_manifest.py (phase 1) to assert that every agent listed
here has a manifest and vice versa."
"""
from __future__ import annotations

import importlib
import os

import pytest

from orchestrator.manifest import AgentManifest, load_all
from orchestrator.validate_manifest import check_manifests_match_inventory, validate

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
