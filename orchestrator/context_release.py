"""Loader, resolver and promote/rollback document builders for the
context-strategy release catalog (mctlhq/mctl-agents#472, ADR 019).

This proposal's `tasks.md` is **Slice A only**: the inert contract, catalog
and validation. Nothing in `orchestrator/context_assembly.py` imports this
module yet, and nothing here changes what the investigator reads — the
`off/observe/enforce/only` rollout ladder and the `assemble()` wiring are
Slice B (mctlhq/mctl-agents#527), imported the same deferred way
`context_assembly._work_context_active`/`_client`/`_persist_to_work_item_store`
already import `orchestrator.work_context` from inside a function body, and
only once the rollout mode is past `off`.

Unlike `context_snapshot.py`/`context_assembly.py`, this module is **not**
stdlib-only: it parses the YAML catalog the same way `orchestrator/
resolver.py` parses the mctl-gitops agent-platform catalog. Reading a
document without hashing the exact same bytes it was parsed from "is not a
weaker guarantee, it is the absence of one" (`resolver._read_yaml_and_hash`'s
docstring), so this module repeats that discipline locally rather than
importing `orchestrator.resolver`, whose own import graph (capability,
service_skills, config.model_policy) has no business riding along with a
context-strategy lookup.

Every check below is a `ContextReleaseError` naming the file and, where one
exists, the exact command that fixes it — `resolver.load_release_binding`'s
style. `ContextReleaseError.code` is drawn from the closed vocabulary in
`VERDICTS`; anything this module cannot classify is `unknown`, never a
silent `ok` (`orchestrator/lifecycle/contract.py`'s style).

**Safety invariant (ADR 009 sec. 5, restated in ADR 019):** a strategy
version, a binding revision and a comparison metric are ordering and
measurement only. No promotion, binding or hash produced here may ever be
read by a policy, capability-eligibility or authorization decision, and this
module must never be imported by a policy path.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from orchestrator.context_assembly import (
    RANKED_STRATEGY_NAME,
    RANKER_NAME,
    RANKER_VERSION,
    STRATEGIES,
)
from orchestrator.context_snapshot import canonical_json, hash_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO_ROOT / "config" / "context-strategies"
VERSIONS_DIR = CATALOG_DIR / "versions"
BINDINGS_DIR = CATALOG_DIR / "bindings"

API_VERSION = "context.mctl.ai/v1alpha1"
VERSION_KIND = "ContextStrategyVersion"
BINDING_KIND = "ContextStrategyBinding"

LIFECYCLE_VALUES = frozenset({"published", "deprecated", "disabled"})
EVIDENCE_KINDS = frozenset({"none", "context-eval"})

#: `promote()`'s environment allow-list (mctlhq/mctl-agents#528, Slice C):
#: anything outside this set is refused as `unknown` — this module cannot
#: classify it, never a silent `evidence-missing`.
PROMOTION_ENVIRONMENTS = frozenset({"shadow", "production"})
#: Environments that accept `evidence.kind: none` — every other environment
#: in `PROMOTION_ENVIRONMENTS` requires `assess_production_evidence` to pass.
EVIDENCE_FREE_ENVIRONMENTS = frozenset({"shadow"})
#: The only `context_eval.EvalRecord.evidence_kind` this module's production
#: gate ever counts (mctlhq/mctl-agents#528): `live`, `stored-replay`,
#: `fixture-baseline` and `none` are dropped before assessment, including a
#: `live` record of a run where the candidate was itself authoritative — so
#: "we already rolled it out" can never satisfy the soak gate.
PRODUCTION_EVIDENCE_KINDS = frozenset({"observe-candidate"})

# The declared implementation surface for every strategy this repository
# ships today (mirrors `STRATEGIES`, `context_assembly.py:84`). Both
# strategies share the same two files: `context_assembly.py` defines the
# selection/ranking stages both branches run, and `context_snapshot.py`
# is what `seal()` uses to hash and validate either one's output. A future
# strategy declares its own tuple here rather than defaulting to this one.
IMPLEMENTATION_FILES_BY_STRATEGY: Mapping[str, tuple[str, ...]] = {
    name: ("orchestrator/context_assembly.py", "orchestrator/context_snapshot.py") for name in STRATEGIES
}

# The ranker each strategy declares, or None for a strategy with no ranker
# of its own (mirrors `context_assembly.py`'s RANKER_NAME/RANKER_VERSION,
# which only the ranked strategy uses).
_RANKER_BY_STRATEGY: Mapping[str, tuple[str, str] | None] = {
    RANKED_STRATEGY_NAME: (RANKER_NAME, RANKER_VERSION),
}

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Closed vocabulary (`orchestrator/lifecycle/contract.py`'s style): every
# `ContextReleaseError` this module raises carries one of these as its
# `.code`. A code this module cannot classify from context is `unknown`,
# never silently `ok`.
VERDICT_OK = "ok"
VERDICT_EVIDENCE_MISSING = "evidence-missing"
VERDICT_EVIDENCE_MISMATCH = "evidence-mismatch"
VERDICT_EVIDENCE_STALE = "evidence-stale"
VERDICT_EVIDENCE_INSUFFICIENT = "evidence-insufficient"
VERDICT_VERSION_NOT_PROMOTABLE = "version-not-promotable"
VERDICT_VERSION_DISABLED = "version-disabled"
VERDICT_HASH_MISMATCH = "hash-mismatch"
VERDICT_UNKNOWN = "unknown"

VERDICTS = frozenset(
    {
        VERDICT_OK,
        VERDICT_EVIDENCE_MISSING,
        VERDICT_EVIDENCE_MISMATCH,
        VERDICT_EVIDENCE_STALE,
        VERDICT_EVIDENCE_INSUFFICIENT,
        VERDICT_VERSION_NOT_PROMOTABLE,
        VERDICT_VERSION_DISABLED,
        VERDICT_HASH_MISMATCH,
        VERDICT_UNKNOWN,
    }
)


class ContextReleaseError(ValueError):
    """A catalog document, promotion or rollback failed a check. `code` is
    always a member of `VERDICTS`; the message is prefixed `"<code>: "` so a
    caller can extract the closed-vocabulary code without parsing English."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code if code in VERDICTS else VERDICT_UNKNOWN
        super().__init__(f"{self.code}: {message}")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextStrategyVersion:
    name: str
    version: str
    lifecycle: str
    ranker_name: str | None
    ranker_version: str | None
    implementation_files: tuple[str, ...]
    implementation_hash: str
    content_hash: str
    agents: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class ContextStrategyBindingRevision:
    """One append-only entry in a binding's history. `evidence_kind` is
    always a member of `EVIDENCE_KINDS`; `rollback_of` is set only on a
    revision produced by `rollback()`."""

    revision: int
    strategy: str
    version: str
    content_hash: str
    implementation_hash: str
    promoted_by: str
    promoted_at: str
    reason: str
    evidence_kind: str
    evidence_ref: str | None = None
    evidence_evaluator_version: str | None = None
    rollback_of: int | None = None
    #: The newest counted observation's `observed_at` and the deduplicated
    #: observation count a passing production gate recorded (mctlhq/
    #: mctl-agents#528). `None` for every `evidence.kind: none` revision and
    #: for every revision that predates this field. Omitted from `to_dict()`
    #: when `None` — the same omitted-when-unset discipline `rollback_of`
    #: already uses, so revisions 1-5 of the committed shadow binding stay
    #: byte-identical.
    evidence_observed_at: str | None = None
    evidence_observations: int | None = None

    def to_dict(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "kind": self.evidence_kind,
            "ref": self.evidence_ref,
            "evaluatorVersion": self.evidence_evaluator_version,
        }
        if self.evidence_observed_at is not None:
            evidence["observedAt"] = self.evidence_observed_at
        if self.evidence_observations is not None:
            evidence["observations"] = self.evidence_observations
        doc: dict[str, Any] = {
            "revision": self.revision,
            "strategy": self.strategy,
            "version": self.version,
            "contentHash": self.content_hash,
            "implementationHash": self.implementation_hash,
            "promotedBy": self.promoted_by,
            "promotedAt": self.promoted_at,
            "reason": self.reason,
            "evidence": evidence,
        }
        if self.rollback_of is not None:
            doc["rollbackOf"] = self.rollback_of
        return doc


@dataclass(frozen=True)
class ContextStrategyBinding:
    """Atomic, append-only, per-(agent, environment). `history` is never
    rewritten; the entry with the highest `revision` is active."""

    agent: str
    environment: str
    history: tuple[ContextStrategyBindingRevision, ...]
    path: Path | None = None

    @property
    def active(self) -> ContextStrategyBindingRevision:
        if not self.history:
            raise ContextReleaseError(VERDICT_UNKNOWN, f"{self.agent}/{self.environment}: binding has no history")
        return max(self.history, key=lambda r: r.revision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": API_VERSION,
            "kind": BINDING_KIND,
            "metadata": {"agent": self.agent, "environment": self.environment},
            "spec": {"history": [r.to_dict() for r in self.history]},
        }


@dataclass(frozen=True)
class ResolvedContextStrategy:
    """The frozen result of `resolve()`: everything a caller needs to know
    which strategy is bound, and the identity it was bound under."""

    agent: str
    environment: str
    strategy: str
    version: str
    ranker_name: str | None
    ranker_version: str | None
    content_hash: str
    implementation_hash: str
    release_revision: int
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "environment": self.environment,
            "strategy": self.strategy,
            "version": self.version,
            "ranker_name": self.ranker_name,
            "ranker_version": self.ranker_version,
            "content_hash": self.content_hash,
            "implementation_hash": self.implementation_hash,
            "release_revision": self.release_revision,
            "verdict": self.verdict,
        }


# ---------------------------------------------------------------------------
# Reading and hashing — resolver._read_yaml_and_hash's discipline: parse and
# hash the same bytes.
# ---------------------------------------------------------------------------


def _read_yaml_and_hash(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContextReleaseError(VERDICT_UNKNOWN, f"missing catalog document: {path} ({exc})") from exc
    try:
        document = yaml.safe_load(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: not valid UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: root must be a mapping")
    return document, hash_bytes(raw)


def compute_implementation_hash(files: Sequence[str], *, root: Path = REPO_ROOT) -> str:
    """sha256 over the declared implementation files, in sorted order by
    path, as length-prefixed `"<len>\\n<path><len>\\n<bytes>"` pairs — the
    exact encoding, and the exact ambiguity rationale,
    `tools/publish_agent_release.py`'s `prompt_hash` already documents:
    plain concatenation lets text move between one file's path and the next
    file's content without changing the digest."""
    if not files:
        raise ContextReleaseError(VERDICT_UNKNOWN, "implementation.files must declare at least one file")
    digest = hashlib.sha256()
    for relpath in sorted(files):
        candidate = (root / relpath).resolve()
        if candidate != root and root not in candidate.parents:
            raise ContextReleaseError(VERDICT_UNKNOWN, f"implementation file escapes the repository root: {relpath}")
        try:
            content = candidate.read_bytes()
        except OSError as exc:
            raise ContextReleaseError(
                VERDICT_HASH_MISMATCH,
                f"declared implementation file is missing: {relpath} ({exc}); regenerate with: "
                "python tools/context_release.py publish --strategy <name> --version <version>",
            ) from exc
        path_bytes = relpath.encode("utf-8")
        digest.update(f"{len(path_bytes)}\n".encode())
        digest.update(path_bytes)
        digest.update(f"{len(content)}\n".encode())
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def compute_content_hash(document: Mapping[str, Any]) -> str:
    """sha256 over the document's canonical JSON, minus `spec.contentHash`
    (`context_snapshot.canonical_json`/`hash_bytes` — the one canonical-hash
    rule this repository uses everywhere, never a second convention)."""
    spec = dict(document.get("spec") or {})
    spec.pop("contentHash", None)
    payload = {
        "apiVersion": document.get("apiVersion"),
        "kind": document.get("kind"),
        "metadata": document.get("metadata"),
        "spec": spec,
    }
    return hash_bytes(canonical_json(payload))


# ---------------------------------------------------------------------------
# load_version / load_binding
# ---------------------------------------------------------------------------


def load_version(name: str, version: str, *, versions_dir: Path = VERSIONS_DIR) -> ContextStrategyVersion:
    """Load and validate one `ContextStrategyVersion`. Fails closed: a
    missing file, an unsupported apiVersion/kind, a path/metadata
    disagreement, an unknown lifecycle, a `disabled` lifecycle, or a
    recomputed hash that disagrees with the committed one all raise
    `ContextReleaseError` rather than returning a partial result."""
    path = versions_dir / name / f"{version}.yaml"
    document, _raw_hash = _read_yaml_and_hash(path)

    if document.get("apiVersion") != API_VERSION:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: unsupported apiVersion {document.get('apiVersion')!r}, expected {API_VERSION!r}"
        )
    if document.get("kind") != VERSION_KIND:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: kind must be {VERSION_KIND!r}, got {document.get('kind')!r}"
        )

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: metadata must be a mapping")
    declared_name = metadata.get("name")
    declared_version = metadata.get("version")
    if declared_name != name:
        raise ContextReleaseError(
            VERDICT_UNKNOWN,
            f"{path}: metadata.name {declared_name!r} does not match the directory it was read from ({name!r})",
        )
    if str(declared_version) != version:
        raise ContextReleaseError(
            VERDICT_UNKNOWN,
            f"{path}: metadata.version {declared_version!r} does not match the file name ({version!r})",
        )
    if declared_name not in STRATEGIES:
        raise ContextReleaseError(
            VERDICT_UNKNOWN,
            f"{path}: metadata.name {declared_name!r} is not a strategy orchestrator/context_assembly.py "
            f"implements (one of {sorted(STRATEGIES)!r})",
        )
    if not isinstance(declared_version, str) or not _SEMVER_RE.match(declared_version):
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: metadata.version {declared_version!r} must be semver X.Y.Z"
        )

    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec must be a mapping")

    lifecycle = spec.get("lifecycle")
    if lifecycle not in LIFECYCLE_VALUES:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: spec.lifecycle must be one of {sorted(LIFECYCLE_VALUES)!r}, got {lifecycle!r}"
        )
    if lifecycle == "disabled":
        raise ContextReleaseError(
            VERDICT_VERSION_DISABLED, f"{path}: {name}@{version} is disabled and may not be resolved at all"
        )

    ranker = spec.get("ranker")
    ranker_name = ranker.get("name") if isinstance(ranker, dict) else None
    ranker_version = ranker.get("version") if isinstance(ranker, dict) else None

    implementation = spec.get("implementation")
    if not isinstance(implementation, dict):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec.implementation must be a mapping")
    files = implementation.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: spec.implementation.files must be a non-empty list of paths"
        )
    declared_implementation_hash = implementation.get("implementationHash")
    if not isinstance(declared_implementation_hash, str) or not declared_implementation_hash.startswith("sha256:"):
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: spec.implementation.implementationHash must be a 'sha256:...' pin"
        )

    recomputed_implementation_hash = compute_implementation_hash(files)
    if recomputed_implementation_hash != declared_implementation_hash:
        raise ContextReleaseError(
            VERDICT_HASH_MISMATCH,
            f"{path}: implementationHash {declared_implementation_hash!r} does not match the code in this commit "
            f"(recomputed {recomputed_implementation_hash!r}); regenerate with: "
            f"python tools/context_release.py publish --strategy {name} --version {version}",
        )

    declared_content_hash = spec.get("contentHash")
    if not isinstance(declared_content_hash, str) or not declared_content_hash.startswith("sha256:"):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec.contentHash must be a 'sha256:...' pin")
    recomputed_content_hash = compute_content_hash(document)
    if recomputed_content_hash != declared_content_hash:
        raise ContextReleaseError(
            VERDICT_HASH_MISMATCH,
            f"{path}: contentHash {declared_content_hash!r} does not match this document's own bytes "
            f"(recomputed {recomputed_content_hash!r}); regenerate with: "
            f"python tools/context_release.py publish --strategy {name} --version {version}",
        )

    agents = spec.get("agents")
    if not isinstance(agents, list) or not agents or not all(isinstance(a, str) for a in agents):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec.agents must be a non-empty list of agent names")

    return ContextStrategyVersion(
        name=name,
        version=version,
        lifecycle=lifecycle,
        ranker_name=ranker_name,
        ranker_version=ranker_version,
        implementation_files=tuple(files),
        implementation_hash=declared_implementation_hash,
        content_hash=declared_content_hash,
        agents=tuple(agents),
        path=path,
    )


def _require_str_field(entry: Mapping[str, Any], key: str, path: Path) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec.history[].{key} must be a non-empty string")
    return value


def _require_sha256_field(entry: Mapping[str, Any], key: str, path: Path) -> str:
    value = _require_str_field(entry, key, path)
    if not value.startswith("sha256:"):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec.history[].{key} must carry the 'sha256:' prefix")
    return value


def load_binding(agent: str, environment: str, *, bindings_dir: Path = BINDINGS_DIR) -> ContextStrategyBinding:
    """Load and validate one `ContextStrategyBinding`. `metadata.agent`/
    `metadata.environment` must agree with the path it was read from — the
    same cross-check `resolver.load_release_binding` performs. `history`
    must be non-empty, every revision a positive integer, strictly
    increasing, with no gap and no reuse."""
    path = bindings_dir / environment / f"{agent}.yaml"
    document, _raw_hash = _read_yaml_and_hash(path)

    if document.get("apiVersion") != API_VERSION:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: unsupported apiVersion {document.get('apiVersion')!r}, expected {API_VERSION!r}"
        )
    if document.get("kind") != BINDING_KIND:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: kind must be {BINDING_KIND!r}, got {document.get('kind')!r}"
        )

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: metadata must be a mapping")
    if metadata.get("agent") != agent:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: metadata.agent {metadata.get('agent')!r} does not match file name {agent!r}"
        )
    if metadata.get("environment") != environment:
        raise ContextReleaseError(
            VERDICT_UNKNOWN,
            f"{path}: metadata.environment {metadata.get('environment')!r} does not match the directory it was "
            f"read from ({environment!r})",
        )

    spec = document.get("spec")
    if not isinstance(spec, dict):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec must be a mapping")
    history_raw = spec.get("history")
    if not isinstance(history_raw, list) or not history_raw:
        raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: spec.history must be a non-empty list")

    revisions: list[ContextStrategyBindingRevision] = []
    seen: set[int] = set()
    for entry in history_raw:
        if not isinstance(entry, dict):
            raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: each spec.history entry must be a mapping")
        revision = entry.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ContextReleaseError(
                VERDICT_UNKNOWN, f"{path}: spec.history[].revision must be a positive integer, got {revision!r}"
            )
        if revision in seen:
            raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: revision {revision} is reused in spec.history")
        seen.add(revision)

        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: revision {revision}: evidence must be a mapping")
        evidence_kind = evidence.get("kind")
        if evidence_kind not in EVIDENCE_KINDS:
            raise ContextReleaseError(
                VERDICT_UNKNOWN,
                f"{path}: revision {revision}: evidence.kind must be one of {sorted(EVIDENCE_KINDS)!r}, "
                f"got {evidence_kind!r}",
            )
        rollback_of = entry.get("rollbackOf")
        if rollback_of is not None and (not isinstance(rollback_of, int) or isinstance(rollback_of, bool)):
            raise ContextReleaseError(VERDICT_UNKNOWN, f"{path}: revision {revision}: rollbackOf must be an integer")

        # mctlhq/mctl-agents#528: a `context-eval` revision must carry the
        # production gate's four evidence fields; a `none` revision must
        # carry neither of the two new ones. Both fail closed with `unknown`,
        # naming the offending field, in `_require_str_field`'s style.
        evidence_observed_at: str | None = None
        evidence_observations: int | None = None
        if evidence_kind == "context-eval":
            ref_value = evidence.get("ref")
            if not isinstance(ref_value, str) or not ref_value:
                raise ContextReleaseError(
                    VERDICT_UNKNOWN,
                    f"{path}: revision {revision}: evidence.kind='context-eval' requires a non-empty "
                    "evidence.ref",
                )
            evaluator_version_value = evidence.get("evaluatorVersion")
            if not isinstance(evaluator_version_value, str) or not evaluator_version_value:
                raise ContextReleaseError(
                    VERDICT_UNKNOWN,
                    f"{path}: revision {revision}: evidence.kind='context-eval' requires a non-empty "
                    "evidence.evaluatorVersion",
                )
            observed_at_value = evidence.get("observedAt")
            if not isinstance(observed_at_value, str) or not observed_at_value:
                raise ContextReleaseError(
                    VERDICT_UNKNOWN,
                    f"{path}: revision {revision}: evidence.kind='context-eval' requires a non-empty "
                    "evidence.observedAt",
                )
            observations_value = evidence.get("observations")
            if (
                not isinstance(observations_value, int)
                or isinstance(observations_value, bool)
                or observations_value < 1
            ):
                raise ContextReleaseError(
                    VERDICT_UNKNOWN,
                    f"{path}: revision {revision}: evidence.kind='context-eval' requires a positive integer "
                    "evidence.observations",
                )
            evidence_observed_at = observed_at_value
            evidence_observations = observations_value
        elif evidence.get("observedAt") is not None or evidence.get("observations") is not None:
            raise ContextReleaseError(
                VERDICT_UNKNOWN,
                f"{path}: revision {revision}: evidence.kind={evidence_kind!r} must not carry "
                "evidence.observedAt or evidence.observations",
            )

        revisions.append(
            ContextStrategyBindingRevision(
                revision=revision,
                strategy=_require_str_field(entry, "strategy", path),
                version=_require_str_field(entry, "version", path),
                content_hash=_require_sha256_field(entry, "contentHash", path),
                implementation_hash=_require_sha256_field(entry, "implementationHash", path),
                promoted_by=_require_str_field(entry, "promotedBy", path),
                promoted_at=_require_str_field(entry, "promotedAt", path),
                reason=_require_str_field(entry, "reason", path),
                evidence_kind=evidence_kind,
                evidence_ref=evidence.get("ref"),
                evidence_evaluator_version=evidence.get("evaluatorVersion"),
                rollback_of=rollback_of,
                evidence_observed_at=evidence_observed_at,
                evidence_observations=evidence_observations,
            )
        )

    ordered = tuple(sorted(revisions, key=lambda r: r.revision))
    expected = list(range(1, len(ordered) + 1))
    actual = [r.revision for r in ordered]
    if actual != expected:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{path}: spec.history revisions must be 1..N with no gap, got {actual}"
        )

    return ContextStrategyBinding(agent=agent, environment=environment, history=ordered, path=path)


def load_binding_or_none(
    agent: str, environment: str, *, bindings_dir: Path = BINDINGS_DIR
) -> ContextStrategyBinding | None:
    """Like `load_binding`, but a missing file answers `None` instead of
    raising — the shape a promotion that creates the very first revision
    needs (there is nothing yet to disagree with)."""
    path = bindings_dir / environment / f"{agent}.yaml"
    if not path.is_file():
        return None
    return load_binding(agent, environment, bindings_dir=bindings_dir)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def resolve(
    agent: str, environment: str, *, versions_dir: Path = VERSIONS_DIR, bindings_dir: Path = BINDINGS_DIR
) -> ResolvedContextStrategy:
    """What a run would resolve for `(agent, environment)` today: load the
    binding, load its active revision's version, and cross-check the two
    still agree. Fails closed on a disabled version or a hash mismatch —
    never returns a partial or best-effort result."""
    binding = load_binding(agent, environment, bindings_dir=bindings_dir)
    active = binding.active
    version_doc = load_version(active.strategy, active.version, versions_dir=versions_dir)

    if version_doc.content_hash != active.content_hash:
        raise ContextReleaseError(
            VERDICT_HASH_MISMATCH,
            f"{binding.path}: revision {active.revision} pins contentHash {active.content_hash!r} but "
            f"{version_doc.path} now declares {version_doc.content_hash!r}",
        )
    if version_doc.implementation_hash != active.implementation_hash:
        raise ContextReleaseError(
            VERDICT_HASH_MISMATCH,
            f"{binding.path}: revision {active.revision} pins implementationHash {active.implementation_hash!r} "
            f"but {version_doc.path} now declares {version_doc.implementation_hash!r}",
        )

    return ResolvedContextStrategy(
        agent=agent,
        environment=environment,
        strategy=version_doc.name,
        version=version_doc.version,
        ranker_name=version_doc.ranker_name,
        ranker_version=version_doc.ranker_version,
        content_hash=version_doc.content_hash,
        implementation_hash=version_doc.implementation_hash,
        release_revision=active.revision,
        verdict=VERDICT_OK,
    )


# ---------------------------------------------------------------------------
# The production evidence gate (mctlhq/mctl-agents#528, ADR 019 sec. 2).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionEvidenceVerdict:
    """`assess_production_evidence`'s answer, returned only on a pass
    (every refusal raises `ContextReleaseError` instead): `reason_code` is
    mctlhq/mctl-agents#526's own `EvidenceAssessment.reason_code` (`"ok"` on
    a pass); `observations`, `newest_observed_at` and `window_seconds` are
    exactly what a passing revision's `evidence` block records."""

    code: str
    reason_code: str
    observations: int
    newest_age_seconds: int | None
    newest_observed_at: str
    window_seconds: int


def assess_production_evidence(
    *,
    version: ContextStrategyVersion,
    records: Sequence[Any],
    evidence_evaluator_version: str | None,
    now: datetime,
) -> ProductionEvidenceVerdict:
    """The production soak gate. Turns mctlhq/mctl-agents#526's
    `assess_evidence` status into this module's closed-vocabulary promotion
    verdict (`ContextReleaseError.code`). Imports `orchestrator.context_eval`
    INSIDE this function body only — never at module scope
    (`test_context_release_never_imports_context_eval_at_module_scope` pins
    this with a source scan) — the same deferred direction
    `run_issue_investigator._load_catalog_identity` already uses for this
    module in the opposite direction.

    Counts only `evidence_kind == "observe-candidate"` records
    (`PRODUCTION_EVIDENCE_KINDS`): a `live`, `stored-replay`,
    `fixture-baseline` or `none` record — including a `live` record of a run
    where the candidate was itself authoritative — is dropped before
    assessment, so "we already rolled it out" can never satisfy this gate on
    its own.

    Fixed precedence (never order-dependent), each a distinct
    `ContextReleaseError.code`:

    1. No `observe-candidate` records supplied -> `evidence-missing`.
    2. Any record whose declared strategy+version match `version` carries
       `verdict: "hash-mismatch"` -> `hash-mismatch` — checked BEFORE
       `assess_evidence`, which silently drops a non-`evaluated`-verdict
       record from `usable` and would otherwise surface this as
       `evidence-insufficient`, hiding the real fault.
    3. `evidence_evaluator_version` is empty, or disagrees with the
       records' own `evaluator_version` -> `evidence-mismatch`.
    4. `assess_evidence -> missing` -> `evidence-missing`.
    5. `assess_evidence -> mismatched` -> `evidence-mismatch`.
    6. `assess_evidence -> stale` -> `evidence-stale`.
    7. `assess_evidence -> insufficient-observations` -> `evidence-insufficient`.
    8. `assess_evidence -> fresh` -> accepted.

    (The caller, `promote()`, handles the one precedence step this function
    never sees: `evidence_kind != "context-eval"`, including `"none"`, is
    refused as `evidence-missing` before this function is ever called.)

    Every raised message repeats #526's own `reason_code`, the counted
    observations and the freshness window in seconds, so an operator reading
    one refusal sees both layers' answers."""
    from orchestrator import context_eval

    window_seconds = context_eval.ADR019_V1_FRESHNESS_WINDOW_SECONDS
    min_observations = context_eval.ADR019_V1_MIN_CONSECUTIVE_OBSERVATIONS

    production_records = [r for r in records if getattr(r, "evidence_kind", None) in PRODUCTION_EVIDENCE_KINDS]
    if not production_records:
        raise ContextReleaseError(
            VERDICT_EVIDENCE_MISSING,
            f"no evidence.kind='observe-candidate' records were supplied for {version.name}@{version.version}; "
            f"reason_code=no-evidence, observations=0, window={window_seconds}s",
        )

    matching_hash_mismatch = [
        r
        for r in production_records
        if r.identity.strategy_name == version.name
        and r.identity.strategy_version == version.version
        and r.verdict == context_eval.VERDICT_HASH_MISMATCH
    ]
    if matching_hash_mismatch:
        raise ContextReleaseError(
            VERDICT_HASH_MISMATCH,
            f"{len(matching_hash_mismatch)} supplied observe-candidate record(s) for {version.name}@"
            f"{version.version} carry verdict='hash-mismatch'; reason_code=hash-mismatch, "
            f"observations={len(production_records)}, window={window_seconds}s",
        )

    evaluator_versions = {r.evaluator_version for r in production_records}
    if (
        not evidence_evaluator_version
        or len(evaluator_versions) != 1
        or evidence_evaluator_version not in evaluator_versions
    ):
        raise ContextReleaseError(
            VERDICT_EVIDENCE_MISMATCH,
            f"evidence.evaluatorVersion {evidence_evaluator_version!r} disagrees with the evaluator_version the "
            f"supplied records carry ({sorted(evaluator_versions)!r}); reason_code=evaluator-version-mismatch, "
            f"observations={len(production_records)}, window={window_seconds}s",
        )

    expected = context_eval.EvidenceIdentity(
        strategy_name=version.name,
        strategy_version=version.version,
        ranker_name=version.ranker_name,
        ranker_version=version.ranker_version,
        strategy_content_hash=version.content_hash,
        strategy_implementation_hash=version.implementation_hash,
        evaluator_name=context_eval.EVALUATOR_NAME,
        evaluator_version=context_eval.EVALUATOR_VERSION,
        metrics_contract_version=context_eval.METRICS_CONTRACT_VERSION,
    )
    policy = context_eval.FreshnessPolicy(
        window_seconds=window_seconds, min_consecutive_observations=min_observations
    )
    assessment = context_eval.assess_evidence(production_records, expected=expected, now=now, policy=policy)

    status_to_code = {
        "missing": VERDICT_EVIDENCE_MISSING,
        "mismatched": VERDICT_EVIDENCE_MISMATCH,
        "stale": VERDICT_EVIDENCE_STALE,
        "insufficient-observations": VERDICT_EVIDENCE_INSUFFICIENT,
    }
    if assessment.status in status_to_code:
        raise ContextReleaseError(
            status_to_code[assessment.status],
            f"production evidence for {version.name}@{version.version} is {assessment.status!r}: "
            f"reason_code={assessment.reason_code}, observations={assessment.observations}, "
            f"window={window_seconds}s, minimum={min_observations}",
        )
    if assessment.status != "fresh":  # pragma: no cover — assess_evidence's own status set is closed
        raise ContextReleaseError(
            VERDICT_UNKNOWN,
            f"assess_evidence returned an unrecognised status {assessment.status!r}",
        )

    # `assess_evidence`'s own anchor selection: the newest `verdict:
    # "evaluated"` record that carries an observation key. Every
    # `production_records` entry is `evidence_kind == "observe-candidate"`,
    # so its observation key is always its own `execution_ref` (never a
    # `store_ref`, which `evaluate()` refuses for this evidence kind).
    usable = [r for r in production_records if r.verdict == context_eval.VERDICT_EVALUATED]
    ordered = sorted(usable, key=lambda r: r.observed_at, reverse=True)
    anchor = next((r for r in ordered if r.execution_ref is not None and r.execution_ref.execution_id), None)
    newest_observed_at = anchor.observed_at if anchor is not None else ""

    return ProductionEvidenceVerdict(
        code=VERDICT_OK,
        reason_code=assessment.reason_code,
        observations=assessment.observations,
        newest_age_seconds=assessment.newest_age_seconds,
        newest_observed_at=newest_observed_at,
        window_seconds=window_seconds,
    )


# ---------------------------------------------------------------------------
# promote / rollback — pure document builders. Never write; the CLI writes.
# ---------------------------------------------------------------------------


def promote(
    binding: ContextStrategyBinding | None,
    *,
    agent: str,
    environment: str,
    strategy_name: str,
    strategy_version: str,
    promoted_by: str,
    reason: str,
    promoted_at: str,
    evidence_kind: str = "none",
    evidence_ref: str | None = None,
    evidence_evaluator_version: str | None = None,
    evidence_records: Sequence[Any] | None = None,
    now: datetime | None = None,
    versions_dir: Path = VERSIONS_DIR,
) -> ContextStrategyBinding:
    """Append one new revision, or raise. Never mutates or drops a prior
    revision; `binding=None` starts revision 1 for an agent/environment
    with no committed binding yet. Still never writes — the CLI writes —
    and still never reads a clock: `now` is caller-supplied, exactly as
    `context_eval.assess_evidence` already requires.

    mctlhq/mctl-agents#528: a `production` promotion is possible for the
    first time, gated on real `context-eval` evidence
    (`assess_production_evidence`); `shadow` keeps Slice A's behaviour
    byte-for-byte for the default case — `evidence_kind="none"` is accepted
    with no records and no clock read (evidence is never *forbidden*, only
    *required* for `production`). A `shadow` promotion that explicitly
    declares `context-eval` evidence still runs `assess_production_evidence`
    to fill the `observedAt`/`observations` fields `load_binding()` requires
    of every `context-eval` revision, in any environment — it is accepted
    only when that evidence is itself well-formed and fresh.

    Order: argument validation -> environment allow-list (`unknown` outside
    `PROMOTION_ENVIRONMENTS`) -> `load_version()` (already raises
    `version-disabled`/`hash-mismatch`) -> lifecycle `published` check
    (`version-not-promotable`; an EXISTING binding on a now-`deprecated`
    version still resolves — `resolve()` is untouched) -> for an environment
    outside `EVIDENCE_FREE_ENVIRONMENTS`, the production evidence gate ->
    append exactly one revision."""
    if binding is not None and (binding.agent != agent or binding.environment != environment):
        raise ContextReleaseError(
            VERDICT_UNKNOWN, "binding does not match the requested (agent, environment)"
        )
    if not reason.strip():
        raise ContextReleaseError(VERDICT_UNKNOWN, "reason must be a non-empty string")
    if not isinstance(promoted_by, str) or not promoted_by.strip():
        raise ContextReleaseError(VERDICT_UNKNOWN, "promoted_by must be a non-empty string")
    if not isinstance(promoted_at, str) or not promoted_at.strip():
        raise ContextReleaseError(VERDICT_UNKNOWN, "promoted_at must be a non-empty string")
    if evidence_kind not in EVIDENCE_KINDS:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"evidence.kind must be one of {sorted(EVIDENCE_KINDS)!r}, got {evidence_kind!r}"
        )

    if environment not in PROMOTION_ENVIRONMENTS:
        raise ContextReleaseError(
            VERDICT_UNKNOWN,
            f"environment must be one of {sorted(PROMOTION_ENVIRONMENTS)!r}, got {environment!r} — this module "
            "cannot classify anything else",
        )

    version_doc = load_version(strategy_name, strategy_version, versions_dir=versions_dir)
    if version_doc.lifecycle != "published":
        raise ContextReleaseError(
            VERDICT_VERSION_NOT_PROMOTABLE,
            f"{strategy_name}@{strategy_version} is {version_doc.lifecycle!r}; only a 'published' version may "
            "be newly promoted (an existing binding on it may still resolve)",
        )

    evidence_observed_at: str | None = None
    evidence_observations: int | None = None
    if environment not in EVIDENCE_FREE_ENVIRONMENTS:
        # Precedence step 1 (mctlhq/mctl-agents#528, `assess_production_
        # evidence`'s docstring): the caller must have declared real
        # evidence at all before this module even looks at the records.
        if evidence_kind != "context-eval":
            raise ContextReleaseError(
                VERDICT_EVIDENCE_MISSING,
                f"{environment!r} promotion requires evidence.kind='context-eval', got {evidence_kind!r}; "
                "reason_code=no-evidence-declared, observations=0",
            )
    if evidence_kind == "context-eval":
        # mctlhq/mctl-agents#528: `load_binding()` requires every
        # `context-eval` revision — in ANY environment, not only
        # `production` — to carry a non-empty `observedAt` and a positive
        # `observations` (`load_binding`, above). Running the assessment
        # whenever `context-eval` evidence is declared, not only for an
        # environment outside `EVIDENCE_FREE_ENVIRONMENTS`, is what fills
        # those two fields; without it a `shadow` promotion that declares
        # `context-eval` evidence would write a binding `load_binding()`
        # can never read back.
        if now is None:
            raise ContextReleaseError(
                VERDICT_UNKNOWN, "evidence.kind='context-eval' requires `now` to assess the supplied evidence"
            )
        verdict = assess_production_evidence(
            version=version_doc,
            records=evidence_records or (),
            evidence_evaluator_version=evidence_evaluator_version,
            now=now,
        )
        evidence_observed_at = verdict.newest_observed_at
        evidence_observations = verdict.observations

    next_revision = (binding.active.revision + 1) if binding is not None else 1
    new_entry = ContextStrategyBindingRevision(
        revision=next_revision,
        strategy=version_doc.name,
        version=version_doc.version,
        content_hash=version_doc.content_hash,
        implementation_hash=version_doc.implementation_hash,
        promoted_by=promoted_by,
        promoted_at=promoted_at,
        reason=reason,
        evidence_kind=evidence_kind,
        evidence_ref=evidence_ref,
        evidence_evaluator_version=evidence_evaluator_version,
        rollback_of=None,
        evidence_observed_at=evidence_observed_at,
        evidence_observations=evidence_observations,
    )
    history = (binding.history if binding is not None else ()) + (new_entry,)
    path = binding.path if binding is not None else None
    return ContextStrategyBinding(agent=agent, environment=environment, history=history, path=path)


def rollback(
    binding: ContextStrategyBinding,
    *,
    to_revision: int,
    promoted_by: str,
    reason: str,
    promoted_at: str,
    versions_dir: Path = VERSIONS_DIR,
) -> ContextStrategyBinding:
    """Append a revision restoring `to_revision`'s exact
    (strategy, version, contentHash, implementationHash) tuple, with
    `rollbackOf=to_revision` recorded. Never infers "one step back"; refuses
    a target whose version has since become `disabled` and names it."""
    if not reason.strip():
        raise ContextReleaseError(VERDICT_UNKNOWN, "reason must be a non-empty string")
    if not isinstance(promoted_by, str) or not promoted_by.strip():
        raise ContextReleaseError(VERDICT_UNKNOWN, "promoted_by must be a non-empty string")
    if not isinstance(promoted_at, str) or not promoted_at.strip():
        raise ContextReleaseError(VERDICT_UNKNOWN, "promoted_at must be a non-empty string")
    target = next((r for r in binding.history if r.revision == to_revision), None)
    if target is None:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"no such revision {to_revision} in {binding.agent}/{binding.environment}'s history"
        )

    version_doc_lifecycle = load_version(target.strategy, target.version, versions_dir=versions_dir).lifecycle
    if version_doc_lifecycle == "disabled":
        raise ContextReleaseError(
            VERDICT_VERSION_DISABLED,
            f"cannot roll back to revision {to_revision}: {target.strategy}@{target.version} is now disabled",
        )

    next_revision = binding.active.revision + 1
    new_entry = ContextStrategyBindingRevision(
        revision=next_revision,
        strategy=target.strategy,
        version=target.version,
        content_hash=target.content_hash,
        implementation_hash=target.implementation_hash,
        promoted_by=promoted_by,
        promoted_at=promoted_at,
        reason=reason,
        evidence_kind=target.evidence_kind,
        evidence_ref=target.evidence_ref,
        evidence_evaluator_version=target.evidence_evaluator_version,
        rollback_of=to_revision,
        # A rollback restores the EXACT target revision's evidence block too
        # (mctlhq/mctl-agents#528): `load_binding()` requires a
        # `context-eval` revision to carry `observedAt`/`observations`, so
        # dropping them here would make a rollback of a production
        # promotion fail its own binding's next load.
        evidence_observed_at=target.evidence_observed_at,
        evidence_observations=target.evidence_observations,
    )
    return ContextStrategyBinding(
        agent=binding.agent,
        environment=binding.environment,
        history=(*binding.history, new_entry),
        path=binding.path,
    )


# ---------------------------------------------------------------------------
# publish — the version-document builder `tools/context_release.py publish`
# writes to disk.
# ---------------------------------------------------------------------------


def build_version_document(
    name: str,
    version: str,
    *,
    lifecycle: str | None = None,
    agents: Sequence[str] | None = None,
    root: Path = REPO_ROOT,
    versions_dir: Path = VERSIONS_DIR,
) -> dict[str, Any]:
    """Build a fresh `ContextStrategyVersion` document for `name`@`version`,
    with `implementationHash`/`contentHash` computed from the code in
    `root` at call time. Raises if `name` is not a strategy
    `orchestrator/context_assembly.py` implements.

    `lifecycle`/`agents` default to the CURRENT on-disk document's values
    when `versions_dir/name/version.yaml` already exists, and only fall back
    to `'published'`/`['issue-investigator']` for a version with no prior
    document. This is what makes the documented hash-drift repair command —
    `python tools/context_release.py publish --strategy <name> --version
    <version>`, run with no `--lifecycle` — a pure hash refresh: it must not
    silently resurrect a `deprecated`/`disabled` version back to `published`
    or reset a version's `agents` list to the single-agent default. Pass
    `lifecycle`/`agents` explicitly to change either on purpose.
    """
    if name not in STRATEGIES:
        raise ContextReleaseError(
            VERDICT_UNKNOWN, f"{name!r} is not a strategy orchestrator/context_assembly.py implements "
            f"(one of {sorted(STRATEGIES)!r})"
        )
    if not _SEMVER_RE.match(version):
        raise ContextReleaseError(VERDICT_UNKNOWN, f"version must be semver X.Y.Z, got {version!r}")

    existing_path = versions_dir / name / f"{version}.yaml"
    existing_lifecycle: str | None = None
    existing_agents: list[str] | None = None
    if existing_path.is_file():
        existing_document, _ = _read_yaml_and_hash(existing_path)
        existing_spec = existing_document.get("spec")
        if isinstance(existing_spec, dict):
            if existing_spec.get("lifecycle") in LIFECYCLE_VALUES:
                existing_lifecycle = existing_spec["lifecycle"]
            candidate_agents = existing_spec.get("agents")
            if (
                isinstance(candidate_agents, list)
                and candidate_agents
                and all(isinstance(a, str) for a in candidate_agents)
            ):
                existing_agents = list(candidate_agents)

    resolved_lifecycle = lifecycle if lifecycle is not None else (existing_lifecycle or "published")
    resolved_agents = list(agents) if agents is not None else (existing_agents or ["issue-investigator"])

    if resolved_lifecycle not in LIFECYCLE_VALUES:
        raise ContextReleaseError(VERDICT_UNKNOWN, f"lifecycle must be one of {sorted(LIFECYCLE_VALUES)!r}")
    if not resolved_agents or not all(isinstance(a, str) for a in resolved_agents):
        raise ContextReleaseError(VERDICT_UNKNOWN, "agents must be a non-empty list of agent names")

    files = sorted(IMPLEMENTATION_FILES_BY_STRATEGY[name])
    implementation_hash = compute_implementation_hash(files, root=root)
    ranker = _RANKER_BY_STRATEGY.get(name)

    # Key order mirrors design.md's worked example (lifecycle, ranker,
    # implementation, contentHash, agents) — `contentHash` is inserted here
    # as a placeholder and overwritten below, so its position in the written
    # YAML does not move once the real hash is known. `compute_content_hash`
    # pops whatever is there before hashing, so the placeholder never
    # participates in its own hash.
    spec: dict[str, Any] = {"lifecycle": resolved_lifecycle}
    if ranker is not None:
        spec["ranker"] = {"name": ranker[0], "version": ranker[1]}
    spec["implementation"] = {"files": files, "implementationHash": implementation_hash}
    spec["contentHash"] = ""
    spec["agents"] = resolved_agents

    document = {
        "apiVersion": API_VERSION,
        "kind": VERSION_KIND,
        "metadata": {"name": name, "version": version},
        "spec": spec,
    }
    spec["contentHash"] = compute_content_hash(document)
    return document
