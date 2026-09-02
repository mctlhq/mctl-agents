"""`execute(agent, task)` — the declarative resolver pilot (mctlhq/mctl-agents#227).

ADR 007 (docs/adr/007-agent-definition-execution-profile-contract.md) defines
`AgentDefinition`, an independently published `ExecutionProfile`, an atomic
per-environment `ReleaseBinding`, and one immutable `ExecutionPlan` per run.
This module implements that resolver for exactly one agent —
`issue-investigator` — against checked-in, explicitly non-promotable
compatibility fixtures (`tests/fixtures/resolver/`). It is a pilot, not
production activation:

- Real `mctl-api` registry/release resolution is mctlhq/mctl-gitops#950's
  job. This module never calls it and never invents a substitute — every
  input it reads is a file already reviewed and committed to this repo.
- `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`
  (orchestrator/run_issue_investigator.py) is opt-in; the default stays
  `legacy`, which does not import or call this module at all.
- Every failure mode below is fail-closed and non-retryable: `execute()`
  either returns one complete, immutable `ExecutionPlan` or raises
  `ResolverError` before Argo submission. It never falls back to a
  baked-in CWFT default, and a `ResolverError` never triggers legacy mode —
  that switch is only ever explicit operator/env configuration.
- Fixture files under `tests/fixtures/resolver/` are marked
  `promotable: false` (profiles) / `bindingSource: compatibility-fixture`
  + `promotable: false` (releases) and this module refuses to load a
  fixture missing either marker. They must never be read as registry or
  production-activation state.

`ExecutionPlan` identifiers (definition/profile version, release revision,
hashes, target SHA) are a pure function of the fixture files' committed
content plus the caller's `Task` — the same binding fixture and the same
task/target SHA always resolve to the identical plan, and a later edit to
the profile/definition without also updating the release binding's pinned
hashes fails closed instead of silently promoting a different pair.
"""
from __future__ import annotations

import glob as globmod
import hashlib
import importlib
import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

from config.model_policy import DEFAULT_POLICY_PATH, resolve_model
from orchestrator.manifest import SUPPORTED_RUNTIME_TYPE, PromptSource
from orchestrator.manifest import ManifestError as _ManifestError

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFINITIONS_DIR = REPO_ROOT / "agents" / "_manifests"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "resolver"
PROFILES_FIXTURE_DIR = FIXTURES_DIR / "profiles"
RELEASES_FIXTURE_DIR = FIXTURES_DIR / "releases"

SUPPORTED_DEFINITION_API_VERSION = "agents.mctl.ai/v1alpha2"
SUPPORTED_PROFILE_API_VERSION = "agents.mctl.ai/v1alpha2"
DEFAULT_ENVIRONMENT = "production"

# "unbounded limit" (mctlhq/mctl-agents#227 acceptance criteria) means missing
# OR unreasonably large, not just missing — these are sanity ceilings, not
# real per-agent tuning; a genuinely bigger agent gets its own profile value
# under these, checked by _bounded() below.
MAX_BUDGET_USD = 100.0
MAX_TIMEOUT_SECONDS = 86400.0  # 24h


class ResolverError(ValueError):
    """Fail-closed resolution failure. Every raise site below corresponds to
    one of the acceptance-criteria failure modes: missing release/profile/
    policy, disabled or ambiguous version, compatibility mismatch, unknown
    reference, unbounded limit, or unapproved sandbox. Non-retryable and
    non-actionable by this module — callers must fix the fixture/definition,
    never catch this to fall back to legacy mode."""


@dataclass(frozen=True)
class Task:
    """Per-run input `execute()` cannot derive from committed fixtures alone.

    `target_repository_sha` pins the target repo's git SHA the caller's
    runtime context (e.g. the investigator's read-only clone) is actually
    running against — see docs/agent-inventory.yaml's runtimeContextInputs
    note: an agent version is reproducible only against a fixed target SHA.
    """

    target_repository_sha: str


@dataclass(frozen=True)
class AgentDefinition:
    """Parsed `agents.mctl.ai/v1alpha2` `AgentDefinition` (identity/prompt/
    triggers plus exactly one `executionProfileRef`)."""

    name: str
    owner: str
    prompt_sources: tuple[PromptSource, ...]
    execution_profile_name: str
    execution_profile_compatibility: str
    path: Path
    # Hashed from the same bytes that were parsed, not re-read later — see
    # _read_yaml_and_hash. This is the value the release binding pins.
    content_hash: str


@dataclass(frozen=True)
class ExecutionProfile:
    """Parsed `ExecutionProfile` compatibility fixture. See ADR 007 sec. 2
    for the field list this mirrors."""

    name: str
    model_policy_task: str
    model_policy_legacy_env_override: str | None
    skills: tuple[str, ...]
    tools: tuple[str, ...]
    policy_ref: str
    permissions: Mapping[str, Any]
    budget_usd: float
    timeout_seconds: float
    entrypoint: str
    options_builder: str
    sandbox_backend: str
    cluster_workflow_template: str
    approval: Mapping[str, Any]
    evidence: tuple[str, ...]
    path: Path
    content_hash: str = field(compare=False)


@dataclass(frozen=True)
class ReleaseBinding:
    """Parsed atomic environment `ReleaseBinding` compatibility fixture."""

    agent: str
    environment: str
    binding_source: str
    definition_name: str
    definition_version: str
    profile_name: str
    profile_version: str
    release_revision: int
    registry_lifecycle: Mapping[str, str]
    path: Path


@dataclass(frozen=True)
class ExecutionPlan:
    """One immutable, pinned execution contract — materialized once per run,
    before Argo submission. Field set matches the design's resolver.py sketch
    (mctlhq/mctl-agents#227 design.md) and ADR 007 sec. 5 verbatim."""

    agent: str
    definition_version: str
    profile_version: str
    release_revision: int
    binding_source: str
    model: str
    model_policy_version: str
    prompt_hashes: tuple[str, ...]
    skill_hashes: tuple[str, ...]
    tools: tuple[str, ...]
    policy_ref: str
    permissions: Mapping[str, Any]
    budget_usd: float
    timeout_seconds: float
    entrypoint: str
    options_builder: str
    sandbox_backend: str
    cluster_workflow_template: str
    target_repository_sha: str
    approval: Mapping[str, Any]
    evidence: tuple[str, ...]

    def to_log_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot for structured logging (mctlhq/mctl-agents#227
        acceptance criteria: "ExecutionPlan identifiers are logged in
        structured form")."""
        return {
            "agent": self.agent,
            "definition_version": self.definition_version,
            "profile_version": self.profile_version,
            "release_revision": self.release_revision,
            "binding_source": self.binding_source,
            "model": self.model,
            "model_policy_version": self.model_policy_version,
            "prompt_hashes": list(self.prompt_hashes),
            "skill_hashes": list(self.skill_hashes),
            "tools": list(self.tools),
            "policy_ref": self.policy_ref,
            "permissions": dict(self.permissions),
            "budget_usd": self.budget_usd,
            "timeout_seconds": self.timeout_seconds,
            "entrypoint": self.entrypoint,
            "options_builder": self.options_builder,
            "sandbox_backend": self.sandbox_backend,
            "cluster_workflow_template": self.cluster_workflow_template,
            "target_repository_sha": self.target_repository_sha,
            "approval": dict(self.approval),
            "evidence": list(self.evidence),
        }

    def log(self) -> None:
        print(f"[resolver] execution_plan={json.dumps(self.to_log_dict(), sort_keys=True)}")


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ResolverError(f"could not read {path}: {exc}") from exc


def _hash_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _content_hash(path: Path) -> str:
    return _hash_bytes(_read_bytes(path))


def _read_yaml_and_hash(path: Path) -> tuple[dict[str, Any], str]:
    """Parse `path` and hash it from the SAME bytes.

    Reading twice — once for `yaml.safe_load`, once for the hash — lets the
    two disagree if the file changes in between, and what the resolver would
    then record is a `content_hash` describing bytes it did not parse. That
    hash is the entire provenance claim: a plan pins it, and `execute()`
    fails closed when a file drifts out from under its release binding. A
    pin that can describe different content than the plan was built from is
    not a weaker guarantee, it is the absence of one (claude P3 on #234).

    Narrow window and checked-in files, so this is cheap insurance rather
    than a live bug — but the fix is one read instead of two.
    """
    raw = _read_bytes(path)
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ResolverError(f"{path}: not valid UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ResolverError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ResolverError(f"{path}: root must be a mapping")
    return document, _hash_bytes(raw)


def _read_yaml(path: Path) -> dict[str, Any]:
    return _read_yaml_and_hash(path)[0]


def _require_mapping(value: Any, *, path: Path, field_path: str) -> dict[str, Any]:
    """Validate an already `or {}`-defaulted field is actually a mapping
    before any caller does `.get()` on it — a fixture supplying a scalar or
    list here must raise `ResolverError`, not a raw `AttributeError`."""
    if not isinstance(value, dict):
        raise ResolverError(f"{path}: {field_path} must be a mapping, got {type(value).__name__}")
    return value


def _bounded(value: Any, *, maximum: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= maximum


def _parse_prompt_source(raw: Any, path: Path) -> PromptSource:
    try:
        return PromptSource.from_dict(raw)
    except _ManifestError as exc:
        raise ResolverError(f"{path}: {exc}") from exc


def load_definition(agent: str) -> AgentDefinition:
    """Load and structurally validate one `agents.mctl.ai/v1alpha2`
    `AgentDefinition`. Unknown apiVersion/kind fails loudly — this pilot
    resolves v1alpha2 only; v1alpha1 agents stay on the legacy path
    entirely and never reach this function."""
    path = DEFINITIONS_DIR / agent / "agent.yaml"
    if not path.is_file():
        raise ResolverError(f"unknown agent {agent!r}: no manifest at {path}")
    document, content_hash = _read_yaml_and_hash(path)
    api_version = document.get("apiVersion")
    if api_version != SUPPORTED_DEFINITION_API_VERSION:
        raise ResolverError(
            f"{path}: resolver requires apiVersion {SUPPORTED_DEFINITION_API_VERSION!r}, got "
            f"{api_version!r} — unknown/legacy API versions fail loudly here; use "
            "ISSUE_INVESTIGATOR_RESOLVER_MODE=legacy for a v1alpha1 agent instead"
        )
    if document.get("kind") != "AgentDefinition":
        raise ResolverError(f"{path}: kind must be 'AgentDefinition' for apiVersion {api_version!r}")

    metadata = _require_mapping(document.get("metadata") or {}, path=path, field_path="metadata")
    spec = _require_mapping(document.get("spec") or {}, path=path, field_path="spec")
    name = metadata.get("name")
    owner = metadata.get("owner")
    if not name or not owner:
        raise ResolverError(f"{path}: metadata.name and metadata.owner are required")
    if path.parent.name != name:
        raise ResolverError(
            f"{path}: metadata.name {name!r} must match its directory name "
            f"(agents/_manifests/{path.parent.name}/agent.yaml)"
        )

    prompt = _require_mapping(spec.get("prompt") or {}, path=path, field_path="spec.prompt")
    prompt_sources = tuple(_parse_prompt_source(s, path) for s in prompt.get("sources", []))
    if not prompt_sources:
        raise ResolverError(f"{path}: spec.prompt.sources must not be empty")

    profile_ref = _require_mapping(
        spec.get("executionProfileRef") or {}, path=path, field_path="spec.executionProfileRef"
    )
    profile_name = profile_ref.get("name")
    compatibility = profile_ref.get("compatibility")
    if not profile_name or not compatibility:
        raise ResolverError(f"{path}: spec.executionProfileRef.name and .compatibility are required")

    return AgentDefinition(
        name=name,
        owner=owner,
        prompt_sources=prompt_sources,
        execution_profile_name=profile_name,
        execution_profile_compatibility=compatibility,
        path=path,
        content_hash=content_hash,
    )


def load_profile(name: str) -> ExecutionProfile:
    """Load and validate one checked-in `ExecutionProfile` compatibility
    fixture. Fails closed on every unbounded/missing/unapproved field this
    pilot's acceptance criteria name — a profile fixture missing any of
    these is treated the same as a missing profile, not silently accepted."""
    path = PROFILES_FIXTURE_DIR / f"{name}.yaml"
    if not path.is_file():
        raise ResolverError(f"missing execution profile {name!r}: no fixture at {path}")
    document, content_hash = _read_yaml_and_hash(path)
    if document.get("apiVersion") != SUPPORTED_PROFILE_API_VERSION:
        raise ResolverError(f"{path}: unsupported profile apiVersion {document.get('apiVersion')!r}")
    if document.get("kind") != "ExecutionProfile":
        raise ResolverError(f"{path}: kind must be 'ExecutionProfile'")

    metadata = _require_mapping(document.get("metadata") or {}, path=path, field_path="metadata")
    spec = _require_mapping(document.get("spec") or {}, path=path, field_path="spec")
    profile_name = metadata.get("name")
    if profile_name != name:
        raise ResolverError(f"{path}: metadata.name {profile_name!r} must match fixture file name {name!r}")
    if metadata.get("promotable") is not False:
        raise ResolverError(
            f"{path}: metadata.promotable must be explicit false — a compatibility fixture must "
            "never be interpretable as promotable registry state"
        )

    model_policy_ref = _require_mapping(
        spec.get("modelPolicyRef") or {}, path=path, field_path="spec.modelPolicyRef"
    )
    model_policy_task = model_policy_ref.get("task")
    if not model_policy_task:
        raise ResolverError(f"{path}: spec.modelPolicyRef.task is required")

    policy_ref = spec.get("policyRef")
    permissions = spec.get("permissions")
    if not policy_ref or not isinstance(permissions, dict) or not permissions:
        raise ResolverError(f"{path}: spec.policyRef and a non-empty spec.permissions are required")

    budget = spec.get("budgetUsd")
    timeout = spec.get("timeoutSeconds")
    if not _bounded(budget, maximum=MAX_BUDGET_USD):
        raise ResolverError(f"{path}: spec.budgetUsd must be a bounded positive number, got {budget!r}")
    if not _bounded(timeout, maximum=MAX_TIMEOUT_SECONDS):
        raise ResolverError(f"{path}: spec.timeoutSeconds must be a bounded positive number, got {timeout!r}")
    # _bounded() already proved both are real numbers; cast (not assert —
    # ruff S101 flags bare asserts outside tests/) so mypy narrows them from
    # `Any | None` to a float() input.
    budget = cast("int | float", budget)
    timeout = cast("int | float", timeout)

    runtime = _require_mapping(spec.get("runtime") or {}, path=path, field_path="spec.runtime")
    if runtime.get("type") != SUPPORTED_RUNTIME_TYPE:
        raise ResolverError(f"{path}: spec.runtime.type must be {SUPPORTED_RUNTIME_TYPE!r}")
    entrypoint = runtime.get("entrypoint")
    options_builder = runtime.get("optionsBuilder")
    if not entrypoint or not options_builder:
        raise ResolverError(f"{path}: spec.runtime.entrypoint and .optionsBuilder are required")

    sandbox = _require_mapping(runtime.get("sandbox") or {}, path=path, field_path="spec.runtime.sandbox")
    sandbox_backend = sandbox.get("backend")
    cluster_workflow_template = sandbox.get("clusterWorkflowTemplate")
    if sandbox_backend != "argo" or not cluster_workflow_template or sandbox.get("approved") is not True:
        raise ResolverError(
            f"{path}: spec.runtime.sandbox must declare backend='argo', a clusterWorkflowTemplate, "
            "and approved=true — an unapproved sandbox must not resolve"
        )

    approval = spec.get("approval")
    if not isinstance(approval, dict) or "required" not in approval:
        raise ResolverError(f"{path}: spec.approval.required is required")
    evidence = spec.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ResolverError(f"{path}: spec.evidence must be a non-empty list")

    return ExecutionProfile(
        name=profile_name,
        model_policy_task=model_policy_task,
        model_policy_legacy_env_override=model_policy_ref.get("legacyEnvOverride"),
        skills=tuple(spec.get("skills") or []),
        tools=tuple(spec.get("tools") or []),
        policy_ref=policy_ref,
        permissions=permissions,
        budget_usd=float(budget),
        timeout_seconds=float(timeout),
        entrypoint=entrypoint,
        options_builder=options_builder,
        sandbox_backend=sandbox_backend,
        cluster_workflow_template=cluster_workflow_template,
        approval=approval,
        evidence=tuple(evidence),
        path=path,
        content_hash=content_hash,
    )


def load_release_binding(agent: str) -> ReleaseBinding:
    """Load one atomic environment `ReleaseBinding` compatibility fixture.
    `execute()` is the only caller that also cross-checks its pinned hashes
    against the live definition/profile files — this function only checks
    the fixture's own shape."""
    path = RELEASES_FIXTURE_DIR / f"{agent}.yaml"
    if not path.is_file():
        raise ResolverError(f"missing release: no compatibility-fixture binding for {agent!r} at {path}")
    document = _read_yaml(path)
    if document.get("bindingSource") != "compatibility-fixture":
        raise ResolverError(f"{path}: bindingSource must be 'compatibility-fixture'")
    if document.get("promotable") is not False:
        raise ResolverError(f"{path}: promotable must be explicit false")

    environment = document.get("environment")
    if not environment:
        raise ResolverError(f"{path}: environment is required")

    lifecycle = _require_mapping(
        document.get("registryLifecycle") or {}, path=path, field_path="registryLifecycle"
    )
    if lifecycle.get("definition") != "published" or lifecycle.get("profile") != "published":
        raise ResolverError(
            f"{path}: registryLifecycle.definition and .profile must both be 'published' — a "
            "disabled/deprecated/draft version must not resolve"
        )

    revision = document.get("releaseRevision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ResolverError(f"{path}: releaseRevision must be a positive integer")

    definition = _require_mapping(document.get("definition") or {}, path=path, field_path="definition")
    profile = _require_mapping(document.get("profile") or {}, path=path, field_path="profile")
    def_name, def_version = definition.get("name"), definition.get("version")
    prof_name, prof_version = profile.get("name"), profile.get("version")
    if not def_name or not def_version or not prof_name or not prof_version:
        raise ResolverError(f"{path}: definition.{{name,version}} and profile.{{name,version}} are required")

    return ReleaseBinding(
        agent=agent,
        environment=environment,
        binding_source=document["bindingSource"],
        definition_name=def_name,
        definition_version=def_version,
        profile_name=prof_name,
        profile_version=prof_version,
        release_revision=revision,
        registry_lifecycle=dict(lifecycle),
        path=path,
    )


def _model_policy_version() -> str:
    """Version identifier for config/model-policy.yaml at resolution time —
    the declared schema `version:` plus a content hash, so a policy edit
    that changes model routing without bumping `version:` still changes the
    identifier a plan pins."""
    document, content_hash = _read_yaml_and_hash(DEFAULT_POLICY_PATH)
    schema_version = document.get("version")
    return f"v{schema_version}+{content_hash}"


def _under_repo_root(value: str) -> Path:
    """Resolve `value` beneath REPO_ROOT, refusing anything that escapes it.

    `Path(REPO_ROOT) / "/etc/shadow"` is `/etc/shadow` — pathlib lets an
    absolute right-hand operand discard the left entirely — and a relative
    `../..` walks out just as well. In practice `value` comes from a
    reviewed, committed agent.yaml/profile, the same trust boundary as the
    rest of this module, and the result is only ever a sha256. That makes
    this cheap rather than unnecessary: the check costs two lines, and
    "it can only leak a hash of the file" is a sentence worth not having
    to write again (claude P3 on #234).
    """
    candidate = (REPO_ROOT / value).resolve()
    if candidate != REPO_ROOT and REPO_ROOT not in candidate.parents:
        raise ResolverError(f"prompt source escapes the repository root: {value}")
    return candidate


def _hash_prompt_source(source: PromptSource) -> str:
    """Deterministic content hash for one PromptSource, mirroring
    docs/agent-inventory.yaml's promptSources contract: `file`/`glob` hash
    on-disk bytes, `inline` hashes the referenced callable's real source
    text (so an edit to the prompt template changes the hash even though no
    file path changed)."""
    if source.kind == "file":
        target = _under_repo_root(source.value.split("#")[0].strip())
        if not target.is_file():
            raise ResolverError(f"prompt source file does not exist: {source.value}")
        return _content_hash(target)
    if source.kind == "glob":
        matches = sorted(
            m for m in globmod.glob(source.value, root_dir=REPO_ROOT, recursive=True)
            if _under_repo_root(m).is_file()
        )
        if not matches:
            raise ResolverError(f"prompt source glob matches no files: {source.value}")
        digest = hashlib.sha256()
        for match in matches:
            digest.update(_under_repo_root(match).read_bytes())
        return "sha256:" + digest.hexdigest()
    if source.kind == "inline":
        module_name, _, symbol = source.value.partition(":")
        try:
            module = importlib.import_module(module_name.replace("/", ".").removesuffix(".py"))
            obj = getattr(module, symbol)
            source_text = inspect.getsource(obj)
        except (ImportError, AttributeError, ValueError, TypeError, OSError) as exc:
            raise ResolverError(f"prompt source inline ref does not resolve: {source.value} ({exc})") from exc
        return "sha256:" + hashlib.sha256(source_text.encode()).hexdigest()
    raise ResolverError(f"unknown prompt source kind {source.kind!r}")


def _require_fixtures_present() -> None:
    """Say the real thing when the fixture tree is not where it should be.

    `FIXTURES_DIR` lives under `tests/`, which is otherwise not shipped —
    the Dockerfile copies exactly `tests/fixtures/resolver/` and nothing
    else from that tree, deliberately. So its absence is not a normal
    condition anywhere: not in a source checkout, and not in the image.
    It means the build lost that COPY, or the resolver is running somewhere
    nobody designed for.

    Defence in depth, then, rather than an expected path — but it earns its
    keep by what the alternative reports. Without it the failure surfaces as
    "missing execution profile issue-investigator-default", which reads like
    a catalog typo and sends the operator looking for a file to add, when
    the actual problem is that no fixtures exist at all.
    """
    # Deliberately the fixture ROOT, not the two directories execute()
    # actually reads. Their absence is a real and different failure — a
    # missing profile or an unwritten release binding — which must keep
    # reporting itself as such; tests redirect those two paths and this
    # guard must not fire for them. What the root's absence uniquely means
    # is "tests/ was never shipped here", i.e. the image.
    if FIXTURES_DIR.is_dir():
        return
    raise ResolverError(
        f"declarative resolver mode has no fixtures to resolve against: {FIXTURES_DIR} "
        "does not exist. This is not a missing profile — the whole compatibility-fixture "
        "tree is absent. In the image that means the Dockerfile's "
        "`COPY tests/fixtures/resolver/` was lost; in a checkout it means the tree was "
        "deleted. Set ISSUE_INVESTIGATOR_RESOLVER_MODE=legacy to fall back explicitly."
    )


def execute(agent: str, task: Task) -> ExecutionPlan:
    """Resolve one immutable `ExecutionPlan` for `agent`, entirely from
    checked-in v1alpha2 fixtures. Raises `ResolverError` — never falls back —
    for every missing/ambiguous/disabled/incompatible/unbounded/unapproved
    condition ADR 007 and mctlhq/mctl-agents#227's acceptance criteria name.
    """
    _require_fixtures_present()

    if not task.target_repository_sha or not task.target_repository_sha.strip():
        raise ResolverError("task.target_repository_sha is required and must be non-empty")

    definition = load_definition(agent)
    binding = load_release_binding(agent)

    if binding.environment != DEFAULT_ENVIRONMENT:
        raise ResolverError(
            f"release binding environment {binding.environment!r} is not the resolver's "
            f"supported environment {DEFAULT_ENVIRONMENT!r}"
        )
    if binding.definition_name != definition.name:
        raise ResolverError(
            f"unknown reference: release binding definition.name {binding.definition_name!r} does "
            f"not match resolved definition {definition.name!r}"
        )
    definition_content_hash = definition.content_hash
    if binding.definition_version != definition_content_hash:
        raise ResolverError(
            f"ambiguous version: release binding definition.version {binding.definition_version!r} "
            f"does not match the current definition file content ({definition_content_hash!r}) — "
            "the fixture is stale or the definition changed without updating the binding"
        )

    if binding.profile_name != definition.execution_profile_name:
        raise ResolverError(
            f"unknown reference: definition executionProfileRef.name "
            f"{definition.execution_profile_name!r} does not match release binding profile.name "
            f"{binding.profile_name!r}"
        )
    profile = load_profile(binding.profile_name)
    if binding.profile_version != profile.content_hash:
        raise ResolverError(
            f"ambiguous version: release binding profile.version {binding.profile_version!r} does "
            f"not match the current profile file content ({profile.content_hash!r}) — the fixture "
            "is stale or the profile changed without updating the binding"
        )

    if definition.execution_profile_compatibility != profile.content_hash:
        raise ResolverError(
            f"compatibility mismatch: definition executionProfileRef.compatibility "
            f"{definition.execution_profile_compatibility!r} does not accept the concrete "
            f"selected profile version {profile.content_hash!r} — compatibility is evaluated "
            "against the resolved profile version, not a profile-owned range"
        )

    model_selection = resolve_model(
        profile.model_policy_task,
        legacy_model_env=profile.model_policy_legacy_env_override,
        log=False,
    )

    return ExecutionPlan(
        agent=definition.name,
        definition_version=definition_content_hash,
        profile_version=profile.content_hash,
        release_revision=binding.release_revision,
        binding_source=binding.binding_source,
        model=model_selection.model,
        model_policy_version=_model_policy_version(),
        prompt_hashes=tuple(_hash_prompt_source(s) for s in definition.prompt_sources),
        skill_hashes=tuple(hashlib.sha256(skill.encode()).hexdigest() for skill in profile.skills),
        tools=profile.tools,
        policy_ref=profile.policy_ref,
        permissions=dict(profile.permissions),
        budget_usd=profile.budget_usd,
        timeout_seconds=profile.timeout_seconds,
        entrypoint=profile.entrypoint,
        options_builder=profile.options_builder,
        sandbox_backend=profile.sandbox_backend,
        cluster_workflow_template=profile.cluster_workflow_template,
        target_repository_sha=task.target_repository_sha.strip(),
        approval=dict(profile.approval),
        evidence=profile.evidence,
    )
