"""Tests that keep docs/agent-inventory.yaml true.

The inventory is documentation that later phases treat as data: the agent
registry hashes each agent's `promptSources` to produce its immutable version.
That only works while the file is accurate, and a YAML file nothing executes
rots quietly — every claim in it was already wrong once during review
(three entrypoints, one brace-glob that matched nothing, one glob reaching
into other agents' prompts, one missing inline PROMPT, one wrong fan-out set).

These tests are the reason the next such drift fails loudly. They live in
tests/ rather than a standalone script so the existing `tests` job in
pr-validation.yml is the gate — no new CI wiring, same trigger as everything
else.
"""
from __future__ import annotations

import glob
import importlib
from pathlib import Path

import pytest
import yaml

from config.settings import ROTATING_SERVICES

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = REPO_ROOT / "docs" / "agent-inventory.yaml"
MODEL_POLICY = REPO_ROOT / "config" / "model-policy.yaml"

# The mctl-gitops checkout lives beside this repo in the developer workspace
# but is absent in CI, which checks out this repo alone. Assertions that need
# it are skipped rather than failed — a check that cannot run is not a check
# that failed, and marking it xfail would hide a real break locally.
GITOPS_CWFT_DIR = (
    REPO_ROOT.parent
    / "mctl-gitops"
    / "platform-gitops"
    / "argo-workflows"
    / "cluster-templates"
)


def _inventory() -> dict:
    return yaml.safe_load(INVENTORY.read_text())


def _agents() -> list[dict]:
    return _inventory()["agents"]


def _agent_ids() -> list[str]:
    return [a["name"] for a in _agents()]


def _resolve(ref: str) -> tuple[object, str]:
    """Turn a `path/to/module.py:symbol` or `pkg.module:symbol` ref into
    (module, symbol). Both spellings appear in the file: entrypoints use
    dotted module paths, promptSources inline refs use file paths."""
    path, _, symbol = ref.partition(":")
    module = path.replace("/", ".").removesuffix(".py")
    return importlib.import_module(module), symbol


@pytest.mark.parametrize("agent", _agents(), ids=_agent_ids())
def test_entrypoint_and_options_builder_resolve(agent):
    """A driver that has been renamed must not keep a stale inventory entry."""
    for key in ("entrypoint", "optionsBuilder"):
        module, symbol = _resolve(agent[key])
        assert hasattr(module, symbol), f"{agent['name']}.{key} -> {agent[key]}"


@pytest.mark.parametrize("agent", _agents(), ids=_agent_ids())
def test_model_policy_task_exists(agent):
    tasks = yaml.safe_load(MODEL_POLICY.read_text())["tasks"]
    assert agent["modelPolicyTask"] in tasks


@pytest.mark.parametrize("agent", _agents(), ids=_agent_ids())
def test_prompt_sources_resolve(agent):
    """Every hashable prompt input must actually exist.

    A glob that matches nothing is the dangerous case: it looks like coverage
    while contributing nothing to the version hash. That is how the original
    `{researcher,analyst,spec-writer}.md` brace pattern got in — Python's glob
    does not expand braces, so it silently matched zero files.
    """
    for source in agent["promptSources"]:
        if "file" in source:
            target = REPO_ROOT / source["file"].split("#")[0].strip()
            assert target.exists(), f"{agent['name']}: missing {source['file']}"
        elif "glob" in source:
            matches = glob.glob(source["glob"], root_dir=REPO_ROOT, recursive=True)
            assert matches, f"{agent['name']}: glob matched nothing: {source['glob']}"
        elif "inline" in source:
            module, symbol = _resolve(source["inline"])
            assert symbol.isidentifier(), (
                f"{agent['name']}: inline ref must be module:symbol, "
                f"got {source['inline']!r}"
            )
            assert hasattr(module, symbol), f"{agent['name']}: {source['inline']}"
        else:
            pytest.fail(f"{agent['name']}: promptSources entry has no known key: {source}")


@pytest.mark.parametrize("agent", _agents(), ids=_agent_ids())
def test_prompt_sources_do_not_reach_into_other_agents(agent):
    """The invariant the review caught us breaking.

    Underscore-prefixed directories under agents/ are other agents' homes
    (_mentor, _shepherd, _incident-responder) plus the _generic fallback. A
    glob that reaches into one makes editing that agent's prompt bump this
    agent's version hash — a false bump, which is precisely what the inventory
    exists to prevent. Files owned by an agent are still declared explicitly
    via `file:`, which this does not restrict.
    """
    for source in agent["promptSources"]:
        if "glob" not in source:
            continue
        for match in glob.glob(source["glob"], root_dir=REPO_ROOT, recursive=True):
            owner = Path(match).parts[1]
            assert not owner.startswith("_"), (
                f"{agent['name']}: glob {source['glob']} reaches into {match}, "
                f"which belongs to {owner}"
            )


def test_service_agent_scope_matches_rotating_services():
    """service-agent's globs must track ROTATING_SERVICES exactly.

    They are written as `agents/[!_]*/...` rather than a hand-listed set so
    they stay derived from disk. That only pays off if the derivation is
    actually equal to the list it stands in for — otherwise it is a guess that
    happens to be right today.
    """
    agent = next(a for a in _agents() if a["name"] == "service-agent")
    matched = {
        Path(m).parts[1]
        for source in agent["promptSources"]
        if "glob" in source
        for m in glob.glob(source["glob"], root_dir=REPO_ROOT, recursive=True)
    }
    assert matched == set(ROTATING_SERVICES)


def test_agents_match_the_options_builders():
    """One agent per build_*_options() — the definition of "agent" this file uses.

    Adding a builder without an inventory entry (or vice versa) means the
    registry would either miss an agent or version one that does not exist.
    """
    options = importlib.import_module("orchestrator.options")
    builders = {
        name for name in dir(options)
        if name.startswith("build_") and name.endswith("_options")
    }
    declared = {a["optionsBuilder"].partition(":")[2] for a in _agents()}
    assert declared == builders


@pytest.mark.skipif(
    not GITOPS_CWFT_DIR.is_dir(),
    reason="mctl-gitops checkout not present (expected in CI; verified locally)",
)
@pytest.mark.parametrize("agent", _agents(), ids=_agent_ids())
def test_cluster_workflow_template_exists(agent):
    names = {
        yaml.safe_load(path.read_text())["metadata"]["name"]
        for path in GITOPS_CWFT_DIR.glob("cwft-*.yaml")
    }
    assert agent["sandbox"]["clusterWorkflowTemplate"] in names


def test_orchestration_entries_are_not_agents():
    """The classification must stay a partition, not overlapping sets."""
    inventory = _inventory()
    agents = {a["name"] for a in inventory["agents"]}
    orchestration = {o["name"] for o in inventory["orchestration"]}
    assert not (agents & orchestration)
