"""Validate agents/_manifests/*/agent.yaml against the real code they describe.

Usage:
    uv run python -m orchestrator.validate_manifest
    uv run python -m orchestrator.validate_manifest agents/_manifests/issue-investigator/agent.yaml

With no arguments, validates every manifest under agents/_manifests/. Same
trust-but-verify spirit as tests/test_agent_inventory.py: every claim a
manifest makes (an entrypoint resolves, a tool is actually allowed, a budget
matches) is checked against the real orchestrator.options builder, not taken
on faith. That file's docstring lists the concrete ways a hand-maintained
description of this codebase has gone stale before — this validator exists so
the next one fails loudly instead of merging quietly.
"""
from __future__ import annotations

import glob as globmod
import importlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from orchestrator.manifest import (
    MANIFESTS_DIR,
    REPO_ROOT,
    AgentManifest,
    ManifestError,
    load,
)

MODEL_POLICY = REPO_ROOT / "config" / "model-policy.yaml"
INVENTORY = REPO_ROOT / "docs" / "agent-inventory.yaml"

# mctl-gitops lives beside this repo in the developer workspace but is absent
# in CI, which checks out this repo alone — same skip pattern as
# tests/test_agent_inventory.py's test_cluster_workflow_template_exists.
GITOPS_CWFT_DIR = (
    REPO_ROOT.parent
    / "mctl-gitops"
    / "platform-gitops"
    / "argo-workflows"
    / "cluster-templates"
)

# Every build_*_options() builder consults mctl_mcp_config()/_mctl_tool_globs(),
# which only includes "mcp__mctl__*" in allowed_tools when MCTL_TOKEN is set.
# Force it on for the duration of the tool-policy check so the comparison is
# deterministic regardless of whether the validator runs on a laptop with a
# real token or in CI with none.
_DUMMY_MCTL_TOKEN = "validate-manifest-dummy-token"  # noqa: S105 - not a real credential

# None of the build_*_options() functions touch the filesystem at
# construction time (verified by reading orchestrator/options.py: cwd/add_dirs
# are stored, not stat'd, except a sibling-repo existence check that safely
# no-ops when the path is absent), so a nonexistent path is fine here — this
# call exists only to inspect the ClaudeAgentOptions it returns.
_DUMMY_PATH = Path("/nonexistent/agent-manifest-validation")

# Every one of these is read once, at options.py's module import time, into a
# module-level constant (e.g. `IMPLEMENTER_BUDGET_USD = float(os.getenv(...))`).
# If any is exported in the shell running this validator, the "actual" value
# below reflects that override instead of the coded default the manifest
# claims to mirror — cleared for the duration of the comparison so the same
# checkout validates the same way in a clean CI shell and a configured
# developer shell.
_ENV_VARS_AFFECTING_OPTIONS_DEFAULTS = (
    "SERVICE_AGENT_BUDGET_USD",
    "MENTOR_BUDGET_USD",
    "IMPLEMENTER_BUDGET_USD",
    "IMPLEMENTER_TIMEOUT_SECONDS",
    "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS",
    "SHEPHERD_BUDGET_USD",
    "ISSUE_INVESTIGATOR_BUDGET_USD",
    "INCIDENT_RESPONDER_BUDGET_USD",
)

# Agents whose execution.timeoutSeconds claims to mirror a real options.py
# wall-clock constant, checked the same way toolPolicy/budgetUsd are: by
# reading the real value, not trusting the YAML. Only implementer has one
# today (IMPLEMENTER_TIMEOUT_SECONDS) — the other five don't bound wall-clock
# time in options.py, so they must not declare timeoutSeconds at all (see the
# "declared but unchecked" branch below).
_TIMEOUT_CONSTANT_BY_AGENT = {
    "implementer": "IMPLEMENTER_TIMEOUT_SECONDS",
}


def _builder_call_args(builder_name: str) -> tuple[tuple[object, ...], dict[str, object]]:
    """Positional/keyword args for each build_*_options() signature."""
    if builder_name == "build_issue_investigator_options":
        return (_DUMMY_PATH, "dummy-model", _DUMMY_PATH / "proposal"), {}
    # service_agent / implementer / mentor / incident_responder / shepherd
    # all take (dir, model) with everything else optional.
    return (_DUMMY_PATH, "dummy-model"), {}


def _check_entrypoints_resolve(manifest: AgentManifest) -> list[str]:
    errors: list[str] = []
    for resolver in (manifest.resolve_entrypoint, manifest.resolve_options_builder):
        try:
            resolver()
        except ManifestError as exc:
            errors.append(str(exc))
    return errors


def _check_prompt_sources(manifest: AgentManifest) -> list[str]:
    """Every hashable prompt input must actually exist — mirrors
    tests/test_agent_inventory.py's test_prompt_sources_resolve, the check
    that caught the original `{researcher,analyst,spec-writer}.md` brace glob
    silently matching nothing."""
    errors: list[str] = []
    for source in manifest.prompt_sources:
        if source.kind == "file":
            target = REPO_ROOT / source.value.split("#")[0].strip()
            if not target.exists():
                errors.append(f"prompt source file does not exist: {source.value}")
        elif source.kind == "glob":
            matches = globmod.glob(source.value, root_dir=REPO_ROOT, recursive=True)
            if not matches:
                errors.append(f"prompt source glob matches nothing: {source.value}")
        elif source.kind == "inline":
            module_name, _, symbol = source.value.partition(":")
            try:
                module, resolved_symbol = __import__(
                    module_name.replace("/", ".").removesuffix(".py"), fromlist=[symbol]
                ), symbol
            except ImportError as exc:
                errors.append(f"prompt source inline module does not import: {source.value} ({exc})")
                continue
            if not hasattr(module, resolved_symbol):
                errors.append(f"prompt source inline symbol not found: {source.value}")
    return errors


def _check_model_policy_task(manifest: AgentManifest) -> list[str]:
    tasks = yaml.safe_load(MODEL_POLICY.read_text(encoding="utf-8"))["tasks"]
    if manifest.model_policy_task not in tasks:
        return [f"modelPolicy.task {manifest.model_policy_task!r} is not in config/model-policy.yaml"]
    return []


def _check_cluster_workflow_template(manifest: AgentManifest) -> list[str]:
    if not GITOPS_CWFT_DIR.is_dir():
        return []  # mctl-gitops checkout not present; expected in CI
    names = {
        yaml.safe_load(p.read_text(encoding="utf-8"))["metadata"]["name"]
        for p in GITOPS_CWFT_DIR.glob("cwft-*.yaml")
    }
    if manifest.cluster_workflow_template not in names:
        return [
            f"execution.sandbox.clusterWorkflowTemplate {manifest.cluster_workflow_template!r} "
            f"not found among {sorted(names)}"
        ]
    return []


def _resolve_builder_module_with_clean_env(manifest: AgentManifest) -> tuple[Callable[..., Any], ModuleType]:
    """Resolve the manifest's optionsBuilder with
    _ENV_VARS_AFFECTING_OPTIONS_DEFAULTS cleared, reloading the module if it
    was already imported (by this process's earlier manifests, or by another
    test module) so its constants reflect the clean environment rather than
    whatever was cached from the first import. Raises ManifestError if the
    builder itself doesn't resolve."""
    previous_env = {name: os.environ.pop(name, None) for name in _ENV_VARS_AFFECTING_OPTIONS_DEFAULTS}
    try:
        builder = manifest.resolve_options_builder()
        module = sys.modules[builder.__module__]
        importlib.reload(module)
        # reload() re-executes the module body in place, so the pre-reload
        # `builder` reference above may be stale — re-fetch by name.
        builder = getattr(module, builder.__name__)
        return builder, module
    finally:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _check_tool_policy_and_budget_match_options_py(manifest: AgentManifest) -> list[str]:
    """The manifest's toolPolicy/execution must match what options.py
    actually builds. This is the "derived from options.py, not a second
    copy" contract: rather than trusting the YAML's allow list, budget
    number, and timeout, call the real builder (and read the real module
    constants) and compare. An options.py edit that isn't mirrored here
    fails this check instead of silently drifting."""
    try:
        builder, module = _resolve_builder_module_with_clean_env(manifest)
    except ManifestError as exc:
        return [str(exc)]

    builder_name = manifest.options_builder.rpartition(":")[2]
    args, kwargs = _builder_call_args(builder_name)

    previous_token = os.environ.get("MCTL_TOKEN")
    os.environ["MCTL_TOKEN"] = _DUMMY_MCTL_TOKEN
    try:
        options = builder(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - report as a validation failure, not a crash
        return [f"{manifest.options_builder} raised while building options: {exc}"]
    finally:
        if previous_token is None:
            os.environ.pop("MCTL_TOKEN", None)
        else:
            os.environ["MCTL_TOKEN"] = previous_token

    errors: list[str] = []
    actual_tools = set(options.allowed_tools or [])
    declared_tools = set(manifest.tool_allow)
    if actual_tools != declared_tools:
        errors.append(
            f"toolPolicy.allow {sorted(declared_tools)} does not match "
            f"options.py's actual allowed_tools {sorted(actual_tools)}"
        )
    if manifest.budget_usd != options.max_budget_usd:
        errors.append(
            f"execution.budgetUsd {manifest.budget_usd} does not match "
            f"options.py's actual max_budget_usd {options.max_budget_usd}"
        )

    timeout_constant = _TIMEOUT_CONSTANT_BY_AGENT.get(manifest.name)
    if timeout_constant is None:
        if manifest.timeout_seconds is not None:
            errors.append(
                f"execution.timeoutSeconds is declared ({manifest.timeout_seconds}) but "
                f"{manifest.name!r} has no entry in _TIMEOUT_CONSTANT_BY_AGENT to check it "
                "against — either the claim is unchecked, or the mapping is missing"
            )
    else:
        actual_timeout = getattr(module, timeout_constant, None)
        if actual_timeout is None:
            errors.append(f"expected {module.__name__}.{timeout_constant} to exist for the timeout comparison")
        elif manifest.timeout_seconds != actual_timeout:
            errors.append(
                f"execution.timeoutSeconds {manifest.timeout_seconds} does not match "
                f"{module.__name__}.{timeout_constant} {actual_timeout}"
            )
    return errors


def check_manifests_match_inventory(manifests: dict[str, AgentManifest]) -> list[str]:
    """docs/agent-inventory.yaml's own header states its purpose: "Consumed by
    orchestrator/validate_manifest.py (phase 1) to assert that every agent
    listed here has a manifest and vice versa." This is that assertion —
    a set mismatch means either a manifest was added for something that
    isn't an agent, or an agent was classified in the inventory but never
    got a manifest."""
    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    inventory_names = {agent["name"] for agent in inventory["agents"]}
    manifest_names = set(manifests)

    errors: list[str] = []
    missing_manifests = inventory_names - manifest_names
    if missing_manifests:
        errors.append(f"agents in docs/agent-inventory.yaml with no manifest: {sorted(missing_manifests)}")
    extra_manifests = manifest_names - inventory_names
    if extra_manifests:
        errors.append(f"manifests for agents not in docs/agent-inventory.yaml: {sorted(extra_manifests)}")
    return errors


def validate(manifest: AgentManifest) -> list[str]:
    """Return human-readable errors for one manifest; empty means valid."""
    errors: list[str] = []
    errors += _check_entrypoints_resolve(manifest)
    errors += _check_prompt_sources(manifest)
    errors += _check_model_policy_task(manifest)
    errors += _check_cluster_workflow_template(manifest)
    if manifest.sandbox_backend != "argo":
        errors.append(
            f"execution.sandbox.backend {manifest.sandbox_backend!r} is not 'argo' "
            "(the only backend v1 supports)"
        )
    # Skip the options.py comparison if the optionsBuilder already failed to
    # resolve above — otherwise this reports the same failure twice.
    if not any("optionsBuilder" in error for error in errors):
        errors += _check_tool_policy_and_budget_match_options_py(manifest)
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    paths = [Path(p) for p in args] if args else sorted(MANIFESTS_DIR.glob("*/agent.yaml"))

    if not paths:
        print(f"no manifests found under {MANIFESTS_DIR}", file=sys.stderr)
        return 1

    if not GITOPS_CWFT_DIR.is_dir():
        # Otherwise every "OK" line below is indistinguishable from one where
        # clusterWorkflowTemplate was actually cross-checked against
        # mctl-gitops, which in CI it never is (that checkout isn't present).
        print(f"NOTE mctl-gitops not found at {GITOPS_CWFT_DIR} — clusterWorkflowTemplate checks are skipped below")

    manifests: dict[str, AgentManifest] = {}
    exit_code = 0
    for manifest_path in paths:
        try:
            manifest = load(manifest_path)
        except ManifestError as exc:
            print(f"FAIL {manifest_path}: {exc}")
            exit_code = 1
            continue

        if manifest.name in manifests:
            print(
                f"FAIL {manifest_path}: duplicate agent name {manifest.name!r} "
                f"(also in {manifests[manifest.name].path})"
            )
            exit_code = 1
            continue
        manifests[manifest.name] = manifest

        # validate() resolves entrypoints and calls the real options.py
        # builder for every manifest — a bad reference in ANY one of them
        # must not abort the batch and leave the rest unchecked, which is
        # the entire point of running this over every manifest at once.
        try:
            errors = validate(manifest)
        except Exception as exc:  # noqa: BLE001 - report as this manifest's failure, not a crash
            print(f"FAIL {manifest_path} ({manifest.name}): unexpected error: {exc}")
            exit_code = 1
            continue

        if errors:
            exit_code = 1
            print(f"FAIL {manifest_path} ({manifest.name}):")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK   {manifest_path} ({manifest.name})")

    # Only meaningful when validating the whole directory — a single
    # explicit manifest path on the CLI is a partial view by design.
    if not args:
        inventory_errors = check_manifests_match_inventory(manifests)
        if inventory_errors:
            exit_code = 1
            print("FAIL docs/agent-inventory.yaml <-> agents/_manifests/ consistency:")
            for error in inventory_errors:
                print(f"  - {error}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
