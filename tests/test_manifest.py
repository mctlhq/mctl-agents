"""Gate agents/_manifests/*/agent.yaml the same way tests/test_agent_inventory.py
gates docs/agent-inventory.yaml: every claim a manifest makes must be checked
against the real code, or it will eventually be wrong and nothing will notice.

This is also where the inventory's stated purpose gets enforced — see
docs/agent-inventory.yaml's header: "Consumed by
orchestrator/validate_manifest.py (phase 1) to assert that every agent listed
here has a manifest and vice versa."
"""
from __future__ import annotations

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
