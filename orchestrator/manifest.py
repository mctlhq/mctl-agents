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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFESTS_DIR = REPO_ROOT / "agents" / "_manifests"

SUPPORTED_API_VERSION = "agents.mctl.ai/v1alpha1"
# v1alpha2 (mctlhq/mctl-agents#227's declarative resolver pilot, ADR 007):
# identity/prompt/triggers plus exactly one `executionProfileRef`; the
# execution shape v1alpha1 carried inline (toolPolicy/execution/runtime)
# moves to an independently published `ExecutionProfile`. Maps apiVersion to
# the `kind` a document under it must declare. Unknown apiVersion continues
# to fail loudly in `load()` below — this dict is the complete allow-list,
# not a prefix/pattern match.
SUPPORTED_API_VERSIONS = {
    SUPPORTED_API_VERSION: "Agent",
    "agents.mctl.ai/v1alpha2": "AgentDefinition",
}
# The only runtime.type v1 knows how to execute. See docs/agent-inventory.yaml's
# L3 roadmap note (plan phase L3) for why a second runtime is a bigger step
# than adding a string here.
SUPPORTED_RUNTIME_TYPE = "claude-agent-sdk"


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
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ManifestError(f"cannot import {module_name!r}: {exc}") from exc
    return module, symbol


@dataclass(frozen=True)
class AgentManifest:
    """One agent's contract, parsed from agent.yaml. Field names track
    spec.* in the YAML; see agents/_manifests/issue-investigator/agent.yaml
    for an annotated example."""

    name: str
    owner: str
    runtime_type: str
    entrypoint: str
    options_builder: str
    prompt_sources: tuple[PromptSource, ...]
    model_policy_task: str
    model_policy_legacy_env_override: str | None
    tool_allow: tuple[str, ...]
    budget_usd: float
    timeout_seconds: float | None
    sandbox_backend: str
    cluster_workflow_template: str
    path: Path
    # v1alpha2 only (mctlhq/mctl-agents#227 pilot). None for every v1alpha1
    # manifest. `execution_profile_ref` is the raw {name, compatibility}
    # claim from spec.executionProfileRef; every field above it is still
    # populated for a v1alpha2 manifest too, resolved from the referenced
    # ExecutionProfile fixture — see _parse_fields_v1alpha2 below for why
    # that keeps every existing check in this module and
    # orchestrator/validate_manifest.py working unchanged.
    api_version: str = SUPPORTED_API_VERSION
    execution_profile_ref: Mapping[str, str] | None = None

    def _resolve_callable(self, ref: str, field: str) -> Callable[..., Any]:
        module, symbol = _resolve_ref(ref)
        if not hasattr(module, symbol):
            raise ManifestError(f"{self.name}: {field} {ref!r} does not resolve")
        value = getattr(module, symbol)
        if not callable(value):
            raise ManifestError(f"{self.name}: {field} {ref!r} resolves to {value!r}, which is not callable")
        return value

    def resolve_entrypoint(self) -> Callable[..., Any]:
        return self._resolve_callable(self.entrypoint, "entrypoint")

    def resolve_options_builder(self) -> Callable[..., Any]:
        return self._resolve_callable(self.options_builder, "optionsBuilder")


def load(path: Path) -> AgentManifest:
    """Parse one agent.yaml. Raises ManifestError on anything structurally
    wrong; does not check the claims against the real code — that is
    orchestrator/validate_manifest.py's job."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError(f"{path}: root must be a mapping")

    api_version = document.get("apiVersion")
    expected_kind = SUPPORTED_API_VERSIONS.get(api_version) if isinstance(api_version, str) else None
    if expected_kind is None:
        raise ManifestError(
            f"{path}: unsupported apiVersion {api_version!r}, expected one of "
            f"{sorted(SUPPORTED_API_VERSIONS)!r}"
        )
    if document.get("kind") != expected_kind:
        raise ManifestError(f"{path}: kind must be {expected_kind!r} for apiVersion {api_version!r}")

    try:
        if api_version == SUPPORTED_API_VERSION:
            return _parse_fields_v1alpha1(document, path)
        return _parse_fields_v1alpha2(document, path)
    except ManifestError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        # A structurally-valid-YAML-but-wrong-shape document (e.g. `spec: [1]`
        # instead of a mapping, or `execution.budgetUsd: not-a-number`) would
        # otherwise raise a raw AttributeError/TypeError/ValueError here.
        # main()'s per-manifest loop only guards against ManifestError, so an
        # unwrapped exception from one bad manifest would abort the whole
        # batch instead of being reported and skipped like any other invalid
        # manifest.
        raise ManifestError(f"{path}: malformed manifest field: {exc}") from exc


def _parse_fields_v1alpha1(document: dict[str, Any], path: Path) -> AgentManifest:
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
    owner = metadata.get("owner")
    if not owner:
        # A silently-defaulted "" owner would let phase-1's contract lose its
        # ownership metadata with nothing noticing — no check anywhere else
        # (validate_manifest.py, the inventory comparison) looks at owner.
        raise ManifestError(f"{path}: metadata.owner is required")
    # The directory is the primary key everywhere else this system refers to
    # an agent (docs/agent-inventory.yaml, gitops CWFT names); metadata.name
    # drifting from it would let two manifests silently claim the same name
    # or one manifest live under a misleading path.
    if path.parent.name != name:
        raise ManifestError(
            f"{path}: metadata.name {name!r} must match its directory name "
            f"(agents/_manifests/{path.parent.name}/agent.yaml)"
        )

    prompt_sources = tuple(PromptSource.from_dict(s) for s in prompt.get("sources", []))
    if not prompt_sources:
        # An empty list is not "no prompt sources declared yet" — it silently
        # defeats the entire manifest contract, since a future version hash
        # over zero inputs would be constant regardless of what the agent
        # actually does. Every real agent has at least one (see
        # docs/agent-inventory.yaml).
        raise ManifestError(f"{path}: spec.prompt.sources must not be empty")

    runtime_type = runtime.get("type", "")
    if runtime_type != SUPPORTED_RUNTIME_TYPE:
        raise ManifestError(
            f"{path}: spec.runtime.type {runtime_type!r} is not {SUPPORTED_RUNTIME_TYPE!r} "
            "(the only runtime v1 supports)"
        )

    timeout_raw = execution.get("timeoutSeconds")
    return AgentManifest(
        name=name,
        owner=owner,
        runtime_type=runtime_type,
        entrypoint=runtime.get("entrypoint", ""),
        options_builder=runtime.get("optionsBuilder", ""),
        prompt_sources=prompt_sources,
        model_policy_task=model_policy.get("task", ""),
        model_policy_legacy_env_override=model_policy.get("legacyEnvOverride"),
        tool_allow=tuple(tool_policy.get("allow", [])),
        budget_usd=float(execution.get("budgetUsd", 0)),
        timeout_seconds=float(timeout_raw) if timeout_raw is not None else None,
        sandbox_backend=sandbox.get("backend", ""),
        cluster_workflow_template=sandbox.get("clusterWorkflowTemplate", ""),
        path=path,
    )


def _parse_fields_v1alpha2(document: dict[str, Any], path: Path) -> AgentManifest:
    """Parse an `agents.mctl.ai/v1alpha2` `AgentDefinition` — the
    mctlhq/mctl-agents#227 declarative resolver pilot (ADR 007 sec. 2).

    v1alpha2 carries only identity/prompt/triggers plus a single
    `executionProfileRef {name, compatibility}`; the execution shape
    v1alpha1 declared inline (toolPolicy/execution/runtime) now lives in the
    referenced `ExecutionProfile`. This function resolves that reference
    (via orchestrator/resolver.py's `load_profile`, imported lazily below to
    avoid a circular import — resolver.py itself imports PromptSource/
    ManifestError from this module) and populates the SAME toolPolicy/
    execution/runtime-shaped fields v1alpha1 always has, so every existing
    consumer (orchestrator/validate_manifest.py, tests/test_manifest.py)
    keeps comparing the identical claim shape against orchestrator/options.py
    it always has — just sourced from the profile instead of this file. See
    orchestrator/resolver.py's module docstring for why that coupling to a
    tests/fixtures/ path is pilot-only, not a production dependency.
    """
    # Deferred import: orchestrator.resolver imports PromptSource/ManifestError
    # from this module at ITS top level. Importing it back here at
    # manifest.py's own module level would be circular; deferring to call
    # time (only reached once this module has fully finished loading) is not.
    from orchestrator import resolver as _resolver

    metadata = document.get("metadata") or {}
    spec = document.get("spec") or {}
    prompt = spec.get("prompt") or {}
    profile_ref = spec.get("executionProfileRef") or {}

    name = metadata.get("name")
    if not name:
        raise ManifestError(f"{path}: metadata.name is required")
    owner = metadata.get("owner")
    if not owner:
        raise ManifestError(f"{path}: metadata.owner is required")
    if path.parent.name != name:
        raise ManifestError(
            f"{path}: metadata.name {name!r} must match its directory name "
            f"(agents/_manifests/{path.parent.name}/agent.yaml)"
        )

    prompt_sources = tuple(PromptSource.from_dict(s) for s in prompt.get("sources", []))
    if not prompt_sources:
        raise ManifestError(f"{path}: spec.prompt.sources must not be empty")

    profile_name = profile_ref.get("name")
    compatibility = profile_ref.get("compatibility")
    if not profile_name or not compatibility:
        raise ManifestError(f"{path}: spec.executionProfileRef.name and .compatibility are required")

    try:
        profile = _resolver.load_profile(profile_name)
    except _resolver.ResolverError as exc:
        raise ManifestError(f"{path}: cannot resolve executionProfileRef {profile_name!r}: {exc}") from exc

    return AgentManifest(
        name=name,
        owner=owner,
        runtime_type=SUPPORTED_RUNTIME_TYPE,
        entrypoint=profile.entrypoint,
        options_builder=profile.options_builder,
        prompt_sources=prompt_sources,
        model_policy_task=profile.model_policy_task,
        model_policy_legacy_env_override=profile.model_policy_legacy_env_override,
        tool_allow=profile.tools,
        budget_usd=profile.budget_usd,
        # Only 'implementer' declares execution.timeoutSeconds today (a
        # Python-enforced wall-clock bound) — see
        # orchestrator/validate_manifest.py's _TIMEOUT_CONSTANT_BY_AGENT.
        # issue-investigator's profile.timeoutSeconds mirrors the Argo CWFT
        # deadline instead (not Python-enforced), matching its v1alpha1
        # manifest, which never declared timeoutSeconds either.
        timeout_seconds=None,
        sandbox_backend=profile.sandbox_backend,
        cluster_workflow_template=profile.cluster_workflow_template,
        path=path,
        api_version="agents.mctl.ai/v1alpha2",
        execution_profile_ref={"name": profile_name, "compatibility": compatibility},
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
