"""Load AgentManifest files (agents/_manifests/<agent>/agent.yaml).

An AgentManifest is a contract, not a second options builder. `spec.toolPolicy`
and `spec.execution` are claims about what a `build_*_options()` function in
orchestrator/options.py already does — this module resolves the manifest's
`runtime.optionsBuilder` reference back to that real function so
orchestrator/validate_manifest.py can call it and check the claims, instead of
this repo maintaining two descriptions of the same `ClaudeAgentOptions` that
can silently drift apart. options.py stays the single place that constructs
`ClaudeAgentOptions`; nothing here re-implements that.

See docs/agent-inventory.yaml for the classification this formalises.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = REPO_ROOT / "agents" / "_manifests"

SUPPORTED_API_VERSION = "agents.mctl.ai/v1alpha1"


class ManifestError(ValueError):
    """Raised for a structurally invalid or unresolvable AgentManifest."""


@dataclass(frozen=True)
class PromptSource:
    """One entry of `spec.prompt.sources` — mirrors docs/agent-inventory.yaml's
    promptSources shape (`file:` / `glob:` / `inline:`), the input to a future
    version hash. See that file's module docstring for why it is a list."""

    kind: str  # "file" | "glob" | "inline"
    value: str

    @classmethod
    def from_dict(cls, source: dict[str, Any]) -> PromptSource:
        for kind in ("file", "glob", "inline"):
            if kind in source:
                return cls(kind=kind, value=source[kind])
        raise ManifestError(f"prompt source has no file/glob/inline key: {source}")


def _resolve_ref(ref: str) -> tuple[Any, str]:
    """Turn a `pkg.module:symbol` reference into (module, symbol)."""
    module_name, _, symbol = ref.partition(":")
    if not module_name or not symbol:
        raise ManifestError(f"expected 'module:symbol', got {ref!r}")
    return importlib.import_module(module_name), symbol


@dataclass(frozen=True)
class AgentManifest:
    """One agent's contract, parsed from agent.yaml. Field names track
    spec.* in the YAML; see agents/_manifests/issue-investigator/agent.yaml
    for an annotated example."""

    name: str
    owner: str
    entrypoint: str
    options_builder: str
    prompt_sources: tuple[PromptSource, ...]
    model_policy_task: str
    tool_allow: tuple[str, ...]
    budget_usd: float
    timeout_seconds: float | None
    sandbox_backend: str
    cluster_workflow_template: str
    path: Path

    def resolve_entrypoint(self) -> Callable[..., Any]:
        module, symbol = _resolve_ref(self.entrypoint)
        if not hasattr(module, symbol):
            raise ManifestError(f"{self.name}: entrypoint {self.entrypoint!r} does not resolve")
        return getattr(module, symbol)

    def resolve_options_builder(self) -> Callable[..., Any]:
        module, symbol = _resolve_ref(self.options_builder)
        if not hasattr(module, symbol):
            raise ManifestError(f"{self.name}: optionsBuilder {self.options_builder!r} does not resolve")
        return getattr(module, symbol)


def load(path: Path) -> AgentManifest:
    """Parse one agent.yaml. Raises ManifestError on anything structurally
    wrong; does not check the claims against the real code — that is
    orchestrator/validate_manifest.py's job."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ManifestError(f"{path}: root must be a mapping")

    if document.get("apiVersion") != SUPPORTED_API_VERSION:
        raise ManifestError(
            f"{path}: unsupported apiVersion {document.get('apiVersion')!r}, "
            f"expected {SUPPORTED_API_VERSION!r}"
        )
    if document.get("kind") != "Agent":
        raise ManifestError(f"{path}: kind must be 'Agent'")

    metadata = document.get("metadata") or {}
    spec = document.get("spec") or {}
    runtime = spec.get("runtime") or {}
    prompt = spec.get("prompt") or {}
    model_policy = spec.get("modelPolicy") or {}
    tool_policy = spec.get("toolPolicy") or {}
    execution = spec.get("execution") or {}
    sandbox = execution.get("sandbox") or {}

    name = metadata.get("name")
    if not name:
        raise ManifestError(f"{path}: metadata.name is required")
    # The directory is the primary key everywhere else this system refers to
    # an agent (docs/agent-inventory.yaml, gitops CWFT names); metadata.name
    # drifting from it would let two manifests silently claim the same name
    # or one manifest live under a misleading path.
    if path.parent.name != name:
        raise ManifestError(
            f"{path}: metadata.name {name!r} must match its directory name "
            f"(agents/_manifests/{path.parent.name}/agent.yaml)"
        )

    timeout_raw = execution.get("timeoutSeconds")
    return AgentManifest(
        name=name,
        owner=metadata.get("owner", ""),
        entrypoint=runtime.get("entrypoint", ""),
        options_builder=runtime.get("optionsBuilder", ""),
        prompt_sources=tuple(PromptSource.from_dict(s) for s in prompt.get("sources", [])),
        model_policy_task=model_policy.get("task", ""),
        tool_allow=tuple(tool_policy.get("allow", [])),
        budget_usd=float(execution.get("budgetUsd", 0)),
        timeout_seconds=float(timeout_raw) if timeout_raw is not None else None,
        sandbox_backend=sandbox.get("backend", ""),
        cluster_workflow_template=sandbox.get("clusterWorkflowTemplate", ""),
        path=path,
    )


def load_all(directory: Path | None = None) -> dict[str, AgentManifest]:
    """Load every agent.yaml under agents/_manifests/, keyed by agent name.
    Raises ManifestError on a duplicate name (two directories claiming the
    same metadata.name is a bug the directory-match check above cannot
    catch, since it only checks a manifest against its own directory)."""
    base = directory or MANIFESTS_DIR
    manifests: dict[str, AgentManifest] = {}
    for manifest_path in sorted(base.glob("*/agent.yaml")):
        manifest = load(manifest_path)
        if manifest.name in manifests:
            raise ManifestError(
                f"duplicate agent name {manifest.name!r}: "
                f"{manifest_path} and {manifests[manifest.name].path}"
            )
        manifests[manifest.name] = manifest
    return manifests
