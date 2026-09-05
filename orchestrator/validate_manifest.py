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
import hashlib
import importlib
import os
import re
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

# mctl-gitops lives beside this repo in the developer workspace, and is now
# checked out in CI too (pr-validation.yml) so the two cross-repo checks
# below actually run there.
#
# It used to be absent in CI and both checks returned [] when the directory
# was missing. That is a check which reports success for the one environment
# where it is the only thing looking — `_check_cluster_workflow_template` was
# a silent no-op in CI for its entire life. Absence is now an ERROR under CI
# and a printed warning locally: a developer without the sibling checkout
# still gets a usable run, and nothing green is ever produced by a check that
# did not execute (mctl-agents#277).
# MCTL_GITOPS_ROOT exists because actions/checkout refuses to write outside
# $GITHUB_WORKSPACE, so CI cannot reproduce the sibling-directory layout a
# developer workspace has. The default is that layout; CI checks the repo out
# into the workspace and points this at it.
GITOPS_ROOT = Path(
    os.environ.get("MCTL_GITOPS_ROOT")
    or REPO_ROOT.parent / "mctl-gitops" / "platform-gitops"
)
GITOPS_CWFT_DIR = GITOPS_ROOT / "argo-workflows" / "cluster-templates"
GITOPS_CATALOG_PROFILES_DIR = GITOPS_ROOT / "agent-platform" / "execution-profiles"
GITOPS_CATALOG_RELEASES_DIR = GITOPS_ROOT / "agent-platform" / "releases"


def _gitops_missing(directory: Path, what: str) -> list[str]:
    """Error under CI, warn locally, for an absent sibling checkout."""
    message = (
        f"{directory} not found; cannot check {what}. In CI this is a "
        "failure — pr-validation.yml checks mctl-gitops out for exactly this."
    )
    if os.environ.get("CI"):
        return [message]
    print(f"warn: {message} (skipping: not running under CI)")
    return []


# Which agent's builder each catalog ExecutionProfile has to agree with.
#
# An explicit table, and a MISSING entry is an error rather than a skip: a
# fourth profile added to the catalog must either be mapped here or fail. A
# lookup that silently passes unknown profiles is how this check would stop
# working without ever going red — the same shape as the CI skip above.
_AGENT_BY_CATALOG_PROFILE = {
    "issue-investigator-default": "issue-investigator",
    "implementer-default": "implementer",
    "shepherd-default": "shepherd",
}

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

# Agents whose driver module reads an env var as a highest-priority model
# override, bypassing config/model_policy.py — e.g. run_issue_investigator.py's
# `INVESTIGATOR_MODEL = os.getenv("ISSUE_INVESTIGATOR_MODEL", SERVICE_AGENT_MODEL)`.
# Same "checked against the real value, not trusted YAML" contract as
# _TIMEOUT_CONSTANT_BY_AGENT above, in both directions: an agent listed here
# must declare modelPolicy.legacyEnvOverride matching this exact name (a
# manifest that omits it would understate what actually controls the agent's
# model), and an agent NOT listed here must not declare one at all.
_LEGACY_MODEL_ENV_VAR_BY_AGENT = {
    "issue-investigator": "ISSUE_INVESTIGATOR_MODEL",
    "incident-responder": "INCIDENT_RESPONDER_MODEL",
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
            # is_file(), not exists(): the same drift-protection gap the glob
            # branch closes below — a `file:` source accidentally repointed at
            # a directory would otherwise pass despite hashing nothing.
            if not target.is_file():
                errors.append(f"prompt source file does not exist: {source.value}")
        elif source.kind == "glob":
            # Filter to regular files: a recursive glob ending in `/**` also
            # matches the directory itself, so an emptied-out directory (every
            # file under it deleted) would otherwise still count as "matched"
            # even though it now contributes nothing to the version hash.
            matches = [
                m for m in globmod.glob(source.value, root_dir=REPO_ROOT, recursive=True)
                if (REPO_ROOT / m).is_file()
            ]
            if not matches:
                errors.append(f"prompt source glob matches no files: {source.value}")
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


def _check_catalog_model_policy_task(profile_name: str, declared_task: object) -> list[str]:
    """A catalog profile's modelPolicyRef.task must be a key this repo's
    config/model-policy.yaml actually defines.

    The mctl-gitops half of this (`knownModelPolicyTasks` in
    `agent-platform/policy.yaml`) is an allowlist maintained by hand in a
    repository that cannot read model-policy.yaml — which is exactly how it
    came to list two task names that do not exist. Only this side can make
    the claim checkable.
    """
    tasks = yaml.safe_load(MODEL_POLICY.read_text(encoding="utf-8"))["tasks"]
    if not isinstance(declared_task, str) or not declared_task:
        return [
            f"{profile_name}: spec.modelPolicyRef.task is missing or not a string "
            f"({declared_task!r}), so the profile names no model tier at all"
        ]
    if declared_task not in tasks:
        return [
            f"{profile_name}: spec.modelPolicyRef.task {declared_task!r} is not a task in "
            f"config/model-policy.yaml ({sorted(tasks)}); resolve_model() would raise "
            "ModelPolicyError on it"
        ]
    return []


def _check_legacy_env_override(manifest: AgentManifest) -> list[str]:
    """spec.modelPolicy.legacyEnvOverride documents an env var the agent's own
    driver module reads directly (bypassing config/model_policy.py) as the
    highest-priority model override — e.g. run_issue_investigator.py's
    `INVESTIGATOR_MODEL = os.getenv("ISSUE_INVESTIGATOR_MODEL", ...)`. Checked
    in both directions against _LEGACY_MODEL_ENV_VAR_BY_AGENT, the same
    "real value, not trusted YAML" contract _TIMEOUT_CONSTANT_BY_AGENT uses:
    a listed agent must declare the exact matching name (an omitted or
    typo'd override would understate what actually controls the agent's
    model), and an unlisted agent must not declare one at all (there'd be
    nothing real to check it against)."""
    expected_var = _LEGACY_MODEL_ENV_VAR_BY_AGENT.get(manifest.name)
    declared_var = manifest.model_policy_legacy_env_override

    if expected_var is None:
        if declared_var is not None:
            return [
                f"modelPolicy.legacyEnvOverride {declared_var!r} is declared but {manifest.name!r} has no "
                "entry in _LEGACY_MODEL_ENV_VAR_BY_AGENT to check it against — either the claim is "
                "unchecked, or the mapping is missing"
            ]
        return []
    if declared_var != expected_var:
        return [
            f"modelPolicy.legacyEnvOverride is {declared_var!r} but {manifest.name!r}'s driver reads "
            f"{expected_var!r} as its highest-priority model override — declare it exactly"
        ]

    try:
        entrypoint = manifest.resolve_entrypoint()
    except ManifestError as exc:
        return [str(exc)]
    module = sys.modules[entrypoint.__module__]
    source_file = getattr(module, "__file__", None)
    if source_file is None:
        return [f"cannot find source file for {manifest.entrypoint} to verify modelPolicy.legacyEnvOverride"]
    source = Path(source_file).read_text(encoding="utf-8")
    if not re.search(rf'os\.getenv\(\s*["\']{re.escape(expected_var)}["\']', source):
        return [
            f"modelPolicy.legacyEnvOverride {expected_var!r} not found as an os.getenv(...) call in "
            f"{source_file} — update or remove the claim"
        ]
    return []


def _check_cluster_workflow_template(manifest: AgentManifest) -> list[str]:
    if not GITOPS_CWFT_DIR.is_dir():
        return _gitops_missing(GITOPS_CWFT_DIR, "clusterWorkflowTemplate references")
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

    try:
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
    finally:
        # _resolve_builder_module_with_clean_env reloaded `module` with the
        # budget/timeout env vars cleared, and only restored os.environ — the
        # module itself was left pinned to those clean-env defaults in
        # sys.modules. reload() mutates the module's __dict__ in place, so
        # without this second reload every later consumer of
        # orchestrator.options in this process (e.g. a subsequent test in the
        # same `uv run pytest -q` session) would silently see the coded
        # defaults instead of a real env var override, even after it's back
        # in os.environ.
        importlib.reload(module)


def check_manifests_match_inventory(manifests: dict[str, AgentManifest]) -> list[str]:
    """docs/agent-inventory.yaml's own header states its purpose: "Consumed by
    orchestrator/validate_manifest.py (phase 1) to assert that every agent
    listed here has a manifest and vice versa." This is that assertion —
    a name-set mismatch means either a manifest was added for something that
    isn't an agent, or an agent was classified in the inventory but never got
    a manifest.

    Beyond the name set, this also compares each shared agent's actual
    binding — entrypoint, optionsBuilder, promptSources, modelPolicyTask,
    sandbox — field by field. Comparing names alone would let a manifest
    rebind e.g. service-agent's runtime.entrypoint to another agent's real
    callable (a copy-paste mistake) and still pass, even though a registry
    consuming that manifest would then dispatch the wrong agent for that
    name."""
    inventory = yaml.safe_load(INVENTORY.read_text(encoding="utf-8"))
    inventory_agent_names = [agent["name"] for agent in inventory["agents"]]

    errors: list[str] = []
    duplicate_names = sorted({name for name in inventory_agent_names if inventory_agent_names.count(name) > 1})
    if duplicate_names:
        # A dict keyed by name would otherwise silently keep only the last of
        # each duplicate, reporting success for an inventory that no longer
        # has the one-to-one relationship with manifests this function exists
        # to assert.
        errors.append(f"duplicate agent names in docs/agent-inventory.yaml: {duplicate_names}")

    inventory_by_name = {agent["name"]: agent for agent in inventory["agents"]}
    inventory_names = set(inventory_by_name)
    manifest_names = set(manifests)

    missing_manifests = inventory_names - manifest_names
    if missing_manifests:
        errors.append(f"agents in docs/agent-inventory.yaml with no manifest: {sorted(missing_manifests)}")
    extra_manifests = manifest_names - inventory_names
    if extra_manifests:
        errors.append(f"manifests for agents not in docs/agent-inventory.yaml: {sorted(extra_manifests)}")

    for name in sorted(inventory_names & manifest_names):
        entry = inventory_by_name[name]
        manifest = manifests[name]
        if entry.get("entrypoint") != manifest.entrypoint:
            errors.append(
                f"{name}: inventory entrypoint {entry.get('entrypoint')!r} does not match "
                f"manifest runtime.entrypoint {manifest.entrypoint!r}"
            )
        if entry.get("optionsBuilder") != manifest.options_builder:
            errors.append(
                f"{name}: inventory optionsBuilder {entry.get('optionsBuilder')!r} does not match "
                f"manifest runtime.optionsBuilder {manifest.options_builder!r}"
            )
        if entry.get("modelPolicyTask") != manifest.model_policy_task:
            errors.append(
                f"{name}: inventory modelPolicyTask {entry.get('modelPolicyTask')!r} does not match "
                f"manifest modelPolicy.task {manifest.model_policy_task!r}"
            )
        inventory_sandbox = entry.get("sandbox") or {}
        if inventory_sandbox.get("backend") != manifest.sandbox_backend:
            errors.append(
                f"{name}: inventory sandbox.backend {inventory_sandbox.get('backend')!r} does not match "
                f"manifest execution.sandbox.backend {manifest.sandbox_backend!r}"
            )
        if inventory_sandbox.get("clusterWorkflowTemplate") != manifest.cluster_workflow_template:
            errors.append(
                f"{name}: inventory sandbox.clusterWorkflowTemplate "
                f"{inventory_sandbox.get('clusterWorkflowTemplate')!r} does not match manifest "
                f"execution.sandbox.clusterWorkflowTemplate {manifest.cluster_workflow_template!r}"
            )
        inventory_prompt_sources = {
            (kind, value) for source in (entry.get("promptSources") or []) for kind, value in source.items()
        }
        manifest_prompt_sources = {(s.kind, s.value) for s in manifest.prompt_sources}
        if inventory_prompt_sources != manifest_prompt_sources:
            errors.append(
                f"{name}: inventory promptSources {sorted(inventory_prompt_sources)} does not match "
                f"manifest prompt.sources {sorted(manifest_prompt_sources)}"
            )
    return errors


def check_catalog_profiles_match_builders(manifests: dict[str, AgentManifest]) -> list[str]:
    """The mctl-gitops ExecutionProfile catalog must state the tools that
    `orchestrator/options.py` actually grants.

    Why this lives here and not in mctl-gitops: the comparison has to call
    the real builders, which only exist in this repo. The other half of the
    contract — budgetUsd/timeoutSeconds against the CWFT that enforces them —
    is entirely inside mctl-gitops and lives in its
    `scripts/validate-agent-platform.py`.

    Why it is needed at all: until 2026-09-02 nothing compared the catalog to
    the code, and it had drifted in BOTH directions at once (#277). The
    investigator profile omitted Write/Edit/Bash, understating what the agent
    can do — a security claim the platform does not enforce. The shepherd
    profile listed Bash/Grep/Glob/WebFetch while `build_shepherd_options`
    grants exactly ["Read"], overstating it — in a catalog meant to become
    authoritative, a claim that would have WIDENED the agent's permissions
    the day something started reading it. The implementer profile named
    `build_implementer_options`, which does not exist.

    `spec.tools` and `spec.modelPolicyRef.task` are compared. Budgets are
    deliberately NOT: the catalog records EFFECTIVE values, which come from
    the CWFT's env, not from this module's defaults — implementer-default
    correctly says $20.00 where options.py defaults to $3.00. Comparing them
    here would fire immediately and be wrong.

    modelPolicyRef.task was added on 2026-09-03 after two of the three
    profiles turned out to name tasks that do not exist —
    `issue_investigator` and `shepherd`, against a config/model-policy.yaml
    that defines only service_agent/mentor_digest/review_findings_normalize.
    `resolve_model()` raises on either, so the resolver would have failed
    closed the moment #277 step 4 pointed it here. mctl-gitops' own
    `policy.yaml` did not catch it because its `knownModelPolicyTasks`
    allowlist listed both phantom names: a profile was checked against a
    list that had never been compared to the file it mirrors. This check is
    that comparison, and it belongs here for the same reason the tools one
    does — model-policy.yaml only exists in this repo.
    """
    if not GITOPS_CATALOG_PROFILES_DIR.is_dir():
        return _gitops_missing(GITOPS_CATALOG_PROFILES_DIR, "the agent-platform catalog")

    errors: list[str] = []
    profile_paths = sorted(GITOPS_CATALOG_PROFILES_DIR.glob("*/profile.yaml"))
    # A directory that exists but holds no profiles validates nothing, and
    # would report success for doing so — the same silent-no-op shape as the
    # missing-checkout case above, reached by a rename or a restructure in
    # mctl-agents rather than by an absent clone (agy P3 on #284).
    if not profile_paths:
        return [
            f"{GITOPS_CATALOG_PROFILES_DIR} contains no */profile.yaml; the "
            "catalog moved or was emptied, and nothing was checked."
        ]
    for profile_path in profile_paths:
        try:
            document = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or not isinstance(document.get("spec"), dict):
                raise ManifestError("missing or non-mapping 'spec'")
            spec = document["spec"]
            # Read and shape-check inside the guard, not after it. `spec:` with
            # nothing under it parses as None, and `tools:` can be any YAML
            # node — so `spec.get(...)` or `set(spec.get("tools"))` outside
            # this block raises AttributeError/TypeError and aborts the whole
            # run, hiding every other profile's result behind one malformed
            # file in a DIFFERENT repository (agy P2 on #284).
            declared_builder = (spec.get("runtime") or {}).get("optionsBuilder")
            # Same guard, same reason: `modelPolicyRef:` with nothing under
            # it parses as None, and `.get("task")` on that raises outside
            # this block.
            declared_task = (spec.get("modelPolicyRef") or {}).get("task")
            tools = spec.get("tools") or []
            # Explicitly a list. `set("Read")` on a scalar `tools: Read` is
            # not an error — it silently yields {'R','e','a','d'}, and the
            # mismatch that follows names four letters instead of the real
            # problem.
            if not isinstance(tools, list):
                raise ManifestError(f"spec.tools must be a list, got {type(tools).__name__}")
            declared_tools = set(tools)
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append(f"{profile_path}: {exc}")
            continue

        profile_name = profile_path.parent.name
        agent_name = _AGENT_BY_CATALOG_PROFILE.get(profile_name)
        if agent_name is None:
            errors.append(
                f"{profile_name}: no entry in _AGENT_BY_CATALOG_PROFILE, so its "
                "tools are checked against nothing. Map it to the agent whose "
                "builder it describes, or remove the profile."
            )
            continue
        manifest = manifests.get(agent_name)
        if manifest is None:
            errors.append(f"{profile_name}: maps to unknown agent {agent_name!r}")
            continue

        # Checked before the builder is resolved and called: this needs
        # nothing but the YAML and model-policy.yaml, and reporting it is
        # useful even when the builder half cannot run at all.
        errors += _check_catalog_model_policy_task(profile_name, declared_task)

        if declared_builder != manifest.options_builder:
            errors.append(
                f"{profile_name}: runtime.optionsBuilder {declared_builder!r} does "
                f"not match {agent_name}'s manifest {manifest.options_builder!r}"
            )
            continue

        # Deliberately NOT _resolve_builder_module_with_clean_env: every var
        # it clears is a budget or a timeout, and this check compares only
        # allowed_tools, which none of them affect. Using it would mean
        # inheriting its contract — it reloads orchestrator.options against a
        # cleared env and leaves restoring the module to the CALLER — for no
        # benefit, and three profiles' worth of chances to forget (claude P2
        # on #284). The one env var that does reach allowed_tools is
        # MCTL_TOKEN, forced below.
        try:
            builder = manifest.resolve_options_builder()
        except ManifestError as exc:
            errors.append(f"{profile_name}: {exc}")
            continue

        args, kwargs = _builder_call_args(manifest.options_builder.rpartition(":")[2])
        previous_token = os.environ.get("MCTL_TOKEN")
        os.environ["MCTL_TOKEN"] = _DUMMY_MCTL_TOKEN
        try:
            options = builder(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - report as a validation failure
            errors.append(f"{profile_name}: {manifest.options_builder} raised: {exc}")
            continue
        finally:
            if previous_token is None:
                os.environ.pop("MCTL_TOKEN", None)
            else:
                os.environ["MCTL_TOKEN"] = previous_token

        actual = set(options.allowed_tools or [])
        if actual != declared_tools:
            errors.append(
                f"{profile_name}: spec.tools {sorted(declared_tools)} does not match "
                f"{manifest.options_builder}'s actual allowed_tools {sorted(actual)}"
            )
    return errors


def check_binding_pins_match_definitions(manifests: dict[str, AgentManifest]) -> list[str]:
    """Every ReleaseBindingIntent's `spec.sourceManifest.contentHash` must be
    the sha256 of the AgentDefinition it names.

    mctl-gitops#1011 made that pin required on every binding, so all three
    shadow bindings carry one. Only ONE of them was verified by anything:
    `orchestrator/resolver.py` recomputes it, and the resolver runs for
    `issue-investigator` alone — the only v1alpha2 manifest. mctl-gitops CI
    cannot verify any of them, because it cannot read this repository's files;
    its own binding schema says so.

    So two of the three pins were values shaped like a gate that nothing
    checked (#293), introduced while closing exactly that class of defect in
    #277. Harmless today, because nothing reads them — and precisely the kind
    of harmless that ends with a hash which has been wrong for months
    surfacing as a resolver bug the day `implementer` migrates to v1alpha2.

    This is the only process that can see both repositories, so it is the only
    place the claim is checkable at all. Same reason the tools and
    model-policy comparisons live here.
    """
    if not GITOPS_CATALOG_RELEASES_DIR.is_dir():
        return _gitops_missing(GITOPS_CATALOG_RELEASES_DIR, "the release binding pins")

    binding_paths = sorted(GITOPS_CATALOG_RELEASES_DIR.glob("*/*.yaml"))
    # A directory that exists but holds no bindings validates nothing and
    # would report success for doing so — the same silent-no-op shape as the
    # missing checkout above, reached by a restructure rather than an absent
    # clone. Identical guard to the profiles check, for the identical reason.
    if not binding_paths:
        return [
            f"{GITOPS_CATALOG_RELEASES_DIR} contains no <environment>/<agent>.yaml; the "
            "release catalog moved or was emptied, and nothing was checked."
        ]

    errors: list[str] = []
    checked = 0
    for binding_path in binding_paths:
        # Everything that touches values from the other repository lives
        # inside this block, path construction and the final read included.
        # `declared_path` is a YAML scalar from mctl-gitops, and a
        # double-quoted scalar may carry control characters — `path: "\0/x"`
        # raises ValueError on `Path.__truediv__` before any check of ours
        # runs. Outside the guard that ValueError aborts main() and hides
        # every OTHER binding's result plus every later check, which is the
        # exact failure this function's malformed-binding test claims to
        # prevent (claude P2 on #294).
        try:
            document = yaml.safe_load(binding_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or not isinstance(document.get("spec"), dict):
                raise ManifestError("missing or non-mapping 'spec'")
            spec = document["spec"]
            source_manifest = spec.get("sourceManifest")
            if not isinstance(source_manifest, dict):
                raise ManifestError("spec.sourceManifest must be a mapping")
            declared_repo = source_manifest.get("repo")
            declared_path = source_manifest.get("path")
            declared_hash = source_manifest.get("contentHash")
            definition = spec.get("definition")
            agent_name = definition.get("name") if isinstance(definition, dict) else None

            # A binding for a DIFFERENT repository cannot be verified from
            # here — its file is not in this checkout. Skipping is a fact,
            # not a policy choice. It is still counted, though: if every
            # binding were skipped this way the loop would return [] having
            # verified nothing, and the guard below turns that into an error
            # rather than a green run (agy P3 on #294).
            if declared_repo != "mctlhq/mctl-agents":
                continue

            if not isinstance(declared_hash, str) or not declared_hash.startswith("sha256:"):
                errors.append(
                    f"{binding_path}: spec.sourceManifest.contentHash is missing or not a "
                    f"'sha256:...' pin (got {declared_hash!r}); the definition half of this "
                    "binding is unpinned"
                )
                continue
            if not isinstance(declared_path, str) or not declared_path:
                errors.append(f"{binding_path}: spec.sourceManifest.path is required")
                continue

            # The agent must resolve, and an unresolvable one is an ERROR, not
            # a skipped cross-check. Falling through left the "right hash of
            # the wrong manifest" case wide open for any binding whose
            # definition.name was absent, typo'd or renamed — defeating the
            # one comparison this block exists for. Both reviewers on #294
            # found it independently; the sibling check above already treats
            # an unknown agent this way.
            if not agent_name:
                errors.append(
                    f"{binding_path}: spec.definition.name is required — without it the "
                    "path below is checked against nothing"
                )
                continue
            if agent_name not in manifests:
                errors.append(
                    f"{binding_path}: maps to unknown agent {agent_name!r}; it has no "
                    "manifest in this repository, so its pin cannot be verified"
                )
                continue

            # Refuse an absolute path or one that climbs out of the repository
            # before it is joined: `REPO_ROOT / "/etc/passwd"` is "/etc/passwd",
            # and this value comes from a different repository.
            target = (REPO_ROOT / declared_path).resolve()
            if REPO_ROOT not in target.parents:
                errors.append(
                    f"{binding_path}: spec.sourceManifest.path {declared_path!r} escapes "
                    "this repository"
                )
                continue

            expected_path = manifests[agent_name].path.relative_to(REPO_ROOT).as_posix()
            if declared_path != expected_path:
                errors.append(
                    f"{binding_path}: spec.sourceManifest.path {declared_path!r} is not "
                    f"{agent_name}'s manifest ({expected_path!r})"
                )
                continue
            # No `is_file()` branch: `expected_path` is derived from a manifest
            # this process already loaded, so a path equal to it exists by
            # construction and the check above rejects every path that is not.
            # A dead branch reads as a guard and is not one — if the read
            # somehow fails anyway, the try/except reports it as this
            # binding's error rather than aborting the run.
            actual = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
            checked += 1
            if actual != declared_hash:
                errors.append(
                    f"{binding_path}: spec.sourceManifest.contentHash {declared_hash} does not "
                    f"match {declared_path} ({actual}). The definition was edited without "
                    "re-pinning the binding — re-pin it in mctl-gitops."
                )
        except Exception as exc:  # noqa: BLE001 - report and continue
            errors.append(f"{binding_path}: {exc}")
            continue

    # Bindings existed but not one of them was verifiable here. Without this
    # the function returns [] and reads as success — the same silent-no-op
    # shape as the empty directory above, reached by every binding naming
    # another repository.
    if not checked and not errors:
        errors.append(
            f"{GITOPS_CATALOG_RELEASES_DIR} holds {len(binding_paths)} binding(s) but none "
            "targets mctlhq/mctl-agents, so no pin was verified."
        )
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
    # Same guard for the entrypoint side: _check_legacy_env_override resolves
    # the entrypoint itself.
    if not any("entrypoint" in error for error in errors):
        errors += _check_legacy_env_override(manifest)
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    paths = [Path(p) for p in args] if args else sorted(MANIFESTS_DIR.glob("*/agent.yaml"))

    if not paths:
        print(f"no manifests found under {MANIFESTS_DIR}", file=sys.stderr)
        return 1

    if not GITOPS_ROOT.is_dir():
        # Otherwise every "OK" line below is indistinguishable from one where
        # the cross-repo checks actually ran.
        note = (
            "reported as FAILURES below (CI is set)"
            if os.environ.get("CI")
            else "skipped below"
        )
        print(f"NOTE mctl-gitops not found at {GITOPS_ROOT} — cross-repo checks are {note}")

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

        catalog_errors = check_catalog_profiles_match_builders(manifests)
        if catalog_errors:
            exit_code = 1
            print("FAIL mctl-gitops agent-platform catalog <-> orchestrator/options.py:")
            for error in catalog_errors:
                print(f"  - {error}")

        pin_errors = check_binding_pins_match_definitions(manifests)
        if pin_errors:
            exit_code = 1
            print("FAIL mctl-gitops release bindings <-> agents/_manifests/:")
            for error in pin_errors:
                print(f"  - {error}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
