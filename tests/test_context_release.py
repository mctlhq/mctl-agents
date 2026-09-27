"""Tests for orchestrator/context_release.py — mctlhq/mctl-agents#472's
Slice A (the inert contract, catalog and validation). T3-T6/T13/T14 below
map onto that proposal's tasks.md "## Tests" section; T1/T2 and the T11
extensions live in tests/test_context_snapshot.py, per tasks.md.

Every isolation test builds its own fixture tree under `tmp_path` and passes
`versions_dir=`/`bindings_dir=` explicitly rather than monkeypatching module
constants — `context_release`'s loaders take those as parameters for exactly
this reason. Declared `implementation.files` still name real files under the
repository root (`orchestrator/context_assembly.py` et al.): `compute_
implementation_hash` always hashes the actual running image, by design
(the catalog pins bytes, not a test double), so an isolated fixture that
wants a hash to match must hash real files, and one that wants "absent
implementation file" gets it for free by naming one that does not exist.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from orchestrator import context_release as cr

REPO_ROOT = Path(__file__).resolve().parent.parent

_REAL_FILES = ["orchestrator/context_assembly.py", "orchestrator/context_snapshot.py"]


def _version_doc(
    *,
    name: str = "deterministic-fixed-order",
    version: str = "1.0.0",
    lifecycle: str = "published",
    files: list[str] | None = None,
    implementation_hash: str | None = None,
    content_hash: str | None = None,
    agents: list[str] | None = None,
    api_version: str = cr.API_VERSION,
    kind: str = cr.VERSION_KIND,
) -> dict:
    files = _REAL_FILES if files is None else files
    real_hash = implementation_hash or (cr.compute_implementation_hash(files) if files else "sha256:" + "0" * 64)
    spec = {
        "lifecycle": lifecycle,
        "implementation": {"files": files, "implementationHash": implementation_hash or real_hash},
        "agents": agents or ["issue-investigator"],
    }
    doc = {"apiVersion": api_version, "kind": kind, "metadata": {"name": name, "version": version}, "spec": spec}
    spec["contentHash"] = content_hash or cr.compute_content_hash(doc)
    return doc


def _write_version(
    tmp_path: Path, *, name: str = "deterministic-fixed-order", version: str = "1.0.0", **overrides
) -> Path:
    doc = _version_doc(name=name, version=version, **overrides)
    path = tmp_path / "versions" / name / f"{version}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _binding_doc(*, agent: str = "issue-investigator", environment: str = "shadow", history: list[dict]) -> dict:
    return {
        "apiVersion": cr.API_VERSION,
        "kind": cr.BINDING_KIND,
        "metadata": {"agent": agent, "environment": environment},
        "spec": {"history": history},
    }


def _revision(
    *,
    revision: int = 1,
    strategy: str = "deterministic-fixed-order",
    version: str = "1.0.0",
    lifecycle: str = "published",
    content_hash: str | None = None,
    implementation_hash: str | None = None,
    promoted_by: str = "octocat",
    promoted_at: str = "2026-09-27T00:00:00Z",
    reason: str = "baseline",
    evidence_kind: str = "none",
    rollback_of: int | None = None,
) -> dict:
    real = _version_doc(name=strategy, version=version, lifecycle=lifecycle)
    entry = {
        "revision": revision,
        "strategy": strategy,
        "version": version,
        "contentHash": content_hash or real["spec"]["contentHash"],
        "implementationHash": implementation_hash or real["spec"]["implementation"]["implementationHash"],
        "promotedBy": promoted_by,
        "promotedAt": promoted_at,
        "reason": reason,
        "evidence": {"kind": evidence_kind, "ref": None, "evaluatorVersion": None},
    }
    if rollback_of is not None:
        entry["rollbackOf"] = rollback_of
    return entry


def _write_binding(
    tmp_path: Path, *, agent: str = "issue-investigator", environment: str = "shadow", history: list[dict]
) -> Path:
    doc = _binding_doc(agent=agent, environment=environment, history=history)
    path = tmp_path / "bindings" / environment / f"{agent}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# T3 — version loading
# ---------------------------------------------------------------------------
def test_load_version_rejects_unknown_api_version(tmp_path):
    _write_version(tmp_path, api_version="context.mctl.ai/v1")
    with pytest.raises(cr.ContextReleaseError, match="unsupported apiVersion"):
        cr.load_version("deterministic-fixed-order", "1.0.0", versions_dir=tmp_path / "versions")


def test_load_version_rejects_unknown_kind(tmp_path):
    _write_version(tmp_path, kind="SomethingElse")
    with pytest.raises(cr.ContextReleaseError, match="kind must be"):
        cr.load_version("deterministic-fixed-order", "1.0.0", versions_dir=tmp_path / "versions")


def test_load_version_refuses_disabled_lifecycle():
    """No fixture tree needed: `disabled` must refuse resolution regardless
    of where the document lives, so this exercises it via a synthetic
    tmp_path tree, matching the others."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_version(tmp_path, lifecycle="disabled")
        with pytest.raises(cr.ContextReleaseError) as excinfo:
            cr.load_version("deterministic-fixed-order", "1.0.0", versions_dir=tmp_path / "versions")
        assert excinfo.value.code == cr.VERDICT_VERSION_DISABLED


def test_load_version_absent_implementation_file_raises(tmp_path):
    _write_version(
        tmp_path,
        files=["orchestrator/this-file-does-not-exist.py"],
        implementation_hash="sha256:" + "1" * 64,
    )
    with pytest.raises(cr.ContextReleaseError, match=r"orchestrator/this-file-does-not-exist\.py"):
        cr.load_version("deterministic-fixed-order", "1.0.0", versions_dir=tmp_path / "versions")


def test_load_version_tampered_implementation_hash_names_the_file_and_the_fix(tmp_path):
    path = _write_version(tmp_path, implementation_hash="sha256:" + "2" * 64)
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.load_version("deterministic-fixed-order", "1.0.0", versions_dir=tmp_path / "versions")
    message = str(excinfo.value)
    assert excinfo.value.code == cr.VERDICT_HASH_MISMATCH
    assert str(path) in message
    assert "python tools/context_release.py publish" in message


def test_load_version_deprecated_still_loads():
    """A `deprecated` version resolves for an existing binding — only
    `disabled` refuses resolution outright."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_version(tmp_path, lifecycle="deprecated")
        loaded = cr.load_version("deterministic-fixed-order", "1.0.0", versions_dir=tmp_path / "versions")
        assert loaded.lifecycle == "deprecated"


def test_load_version_rejects_name_not_in_strategies(tmp_path):
    path = tmp_path / "versions" / "not-a-real-strategy" / "1.0.0.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = _version_doc(name="not-a-real-strategy")
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(cr.ContextReleaseError, match="not a strategy"):
        cr.load_version("not-a-real-strategy", "1.0.0", versions_dir=tmp_path / "versions")


# ---------------------------------------------------------------------------
# T4 — binding loading
# ---------------------------------------------------------------------------
def test_load_binding_rejects_agent_path_mismatch(tmp_path):
    doc = _binding_doc(agent="issue-investigator", history=[_revision()])
    path = tmp_path / "bindings" / "shadow" / "someone-else.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(cr.ContextReleaseError, match=r"metadata\.agent"):
        cr.load_binding("someone-else", "shadow", bindings_dir=tmp_path / "bindings")


def test_load_binding_rejects_environment_path_mismatch(tmp_path):
    doc = _binding_doc(environment="production", history=[_revision()])
    path = tmp_path / "bindings" / "shadow" / "issue-investigator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(cr.ContextReleaseError, match=r"metadata\.environment"):
        cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")


def test_load_binding_rejects_non_positive_revision(tmp_path):
    _write_binding(tmp_path, history=[_revision(revision=0)])
    with pytest.raises(cr.ContextReleaseError, match="positive integer"):
        cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")


def test_load_binding_rejects_duplicated_revision(tmp_path):
    _write_binding(tmp_path, history=[_revision(revision=1), _revision(revision=1, promoted_by="other")])
    with pytest.raises(cr.ContextReleaseError, match="reused"):
        cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")


def test_load_binding_rejects_gap_in_revision_sequence(tmp_path):
    _write_binding(tmp_path, history=[_revision(revision=1), _revision(revision=3)])
    with pytest.raises(cr.ContextReleaseError, match="no gap"):
        cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")


def test_load_binding_rejects_empty_history(tmp_path):
    doc = _binding_doc(history=[])
    path = tmp_path / "bindings" / "shadow" / "issue-investigator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(cr.ContextReleaseError, match="non-empty"):
        cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")


def test_load_binding_or_none_answers_none_for_missing_file(tmp_path):
    assert cr.load_binding_or_none("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings") is None


def test_active_revision_is_the_highest(tmp_path):
    _write_binding(tmp_path, history=[_revision(revision=1), _revision(revision=2, promoted_by="second")])
    binding = cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")
    assert binding.active.revision == 2
    assert binding.active.promoted_by == "second"


# ---------------------------------------------------------------------------
# T5 — promotion rules
# ---------------------------------------------------------------------------
def test_production_promotion_is_always_refused_as_evidence_missing(tmp_path):
    """Whatever the evidence block says, including a well-formed-looking
    context-eval block — Slice A has no #526 evaluator on the image."""
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.promote(
            None,
            agent="issue-investigator",
            environment="production",
            strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0",
            promoted_by="octocat",
            reason="looks ready",
            promoted_at="2026-09-27T00:00:00Z",
            evidence_kind="context-eval",
            evidence_ref="context-eval-run-42",
            evidence_evaluator_version="1.0.0",
            versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISSING


def test_non_shadow_promotion_is_refused_even_when_not_named_production(tmp_path):
    """`promote()` allowlists 'shadow' as the only evidence-free environment
    (commit 8266e37) — any other environment name, not just the literal
    string 'production', must be refused."""
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.promote(
            None,
            agent="issue-investigator",
            environment="staging",
            strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0",
            promoted_by="octocat",
            reason="looks ready",
            promoted_at="2026-09-27T00:00:00Z",
            versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISSING


def test_promote_refuses_empty_promoted_by(tmp_path):
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    with pytest.raises(cr.ContextReleaseError, match="promoted_by"):
        cr.promote(
            None,
            agent="issue-investigator",
            environment="shadow",
            strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0",
            promoted_by="   ",
            reason="baseline",
            promoted_at="2026-09-27T00:00:00Z",
            versions_dir=tmp_path / "versions",
        )


def test_promote_refuses_empty_promoted_at(tmp_path):
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    with pytest.raises(cr.ContextReleaseError, match="promoted_at"):
        cr.promote(
            None,
            agent="issue-investigator",
            environment="shadow",
            strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0",
            promoted_by="octocat",
            reason="baseline",
            promoted_at="   ",
            versions_dir=tmp_path / "versions",
        )


def test_shadow_promotion_with_none_evidence_and_reason_is_accepted(tmp_path):
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    updated = cr.promote(
        None,
        agent="issue-investigator",
        environment="shadow",
        strategy_name="deterministic-fixed-order",
        strategy_version="1.0.0",
        promoted_by="octocat",
        reason="inert shadow baseline",
        promoted_at="2026-09-27T00:00:00Z",
        evidence_kind="none",
        versions_dir=versions_dir,
    )
    assert updated.active.revision == 1
    assert updated.active.strategy == "deterministic-fixed-order"
    assert updated.active.evidence_kind == "none"


def test_promoting_a_deprecated_version_is_refused_while_existing_binding_still_resolves(tmp_path):
    versions_dir = tmp_path / "versions"
    bindings_dir = tmp_path / "bindings"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0", lifecycle="deprecated")
    _write_binding(tmp_path, history=[_revision(lifecycle="deprecated", evidence_kind="none")])

    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.promote(
            None,
            agent="issue-investigator",
            environment="shadow",
            strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0",
            promoted_by="octocat",
            reason="try to promote a deprecated version",
            promoted_at="2026-09-27T00:00:00Z",
            versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_VERSION_NOT_PROMOTABLE

    # The EXISTING binding (already pointing at this now-deprecated version)
    # still resolves — deprecation blocks only new promotions.
    resolved = cr.resolve("issue-investigator", "shadow", versions_dir=versions_dir, bindings_dir=bindings_dir)
    assert resolved.verdict == cr.VERDICT_OK


def test_promote_refuses_empty_reason(tmp_path):
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    with pytest.raises(cr.ContextReleaseError, match="reason"):
        cr.promote(
            None,
            agent="issue-investigator",
            environment="shadow",
            strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0",
            promoted_by="octocat",
            reason="   ",
            promoted_at="2026-09-27T00:00:00Z",
            versions_dir=tmp_path / "versions",
        )


def test_promote_never_mutates_or_drops_a_prior_revision(tmp_path):
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    binding = cr.promote(
        None,
        agent="issue-investigator",
        environment="shadow",
        strategy_name="deterministic-fixed-order",
        strategy_version="1.0.0",
        promoted_by="octocat",
        reason="first",
        promoted_at="2026-09-27T00:00:00Z",
        versions_dir=versions_dir,
    )
    second = cr.promote(
        binding,
        agent="issue-investigator",
        environment="shadow",
        strategy_name="deterministic-fixed-order",
        strategy_version="1.0.0",
        promoted_by="octocat",
        reason="second",
        promoted_at="2026-09-27T01:00:00Z",
        versions_dir=versions_dir,
    )
    assert [r.revision for r in second.history] == [1, 2]
    assert second.history[0] == binding.history[0]
    assert second.active.revision == 2


# ---------------------------------------------------------------------------
# T6 — rollback rules
# ---------------------------------------------------------------------------
def test_rollback_appends_a_revision_restoring_the_exact_prior_triple(tmp_path):
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    _write_version(tmp_path, name="trust-freshness-ranked", version="1.0.0")
    _write_binding(
        tmp_path,
        history=[
            _revision(revision=1, strategy="deterministic-fixed-order", version="1.0.0"),
            _revision(revision=2, strategy="trust-freshness-ranked", version="1.0.0"),
        ],
    )
    binding = cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")
    rolled_back = cr.rollback(
        binding, to_revision=1, promoted_by="octocat", reason="bad promotion", promoted_at="2026-09-27T02:00:00Z",
        versions_dir=versions_dir,
    )
    assert [r.revision for r in rolled_back.history] == [1, 2, 3]
    new_entry = rolled_back.active
    assert new_entry.revision == 3
    assert new_entry.rollback_of == 1
    assert new_entry.strategy == binding.history[0].strategy
    assert new_entry.version == binding.history[0].version
    assert new_entry.content_hash == binding.history[0].content_hash
    assert new_entry.implementation_hash == binding.history[0].implementation_hash
    # revisions 1..2 are untouched
    assert rolled_back.history[0] == binding.history[0]
    assert rolled_back.history[1] == binding.history[1]


def test_rollback_never_infers_a_target(tmp_path):
    """`rollback` requires an explicit `to_revision`; there is no default."""
    import inspect

    params = inspect.signature(cr.rollback).parameters
    assert "to_revision" in params
    assert params["to_revision"].default is inspect.Parameter.empty


def test_rollback_to_disabled_version_is_refused_and_names_it(tmp_path):
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0", lifecycle="disabled")
    _write_binding(tmp_path, history=[_revision(revision=1, strategy="deterministic-fixed-order", version="1.0.0")])
    binding = cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.rollback(
            binding, to_revision=1, promoted_by="octocat", reason="try", promoted_at="2026-09-27T02:00:00Z",
            versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_VERSION_DISABLED
    assert "deterministic-fixed-order@1.0.0" in str(excinfo.value)


def test_rollback_to_unknown_revision_raises(tmp_path):
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    _write_binding(tmp_path, history=[_revision(revision=1)])
    binding = cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")
    with pytest.raises(cr.ContextReleaseError, match="no such revision"):
        cr.rollback(
            binding, to_revision=99, promoted_by="octocat", reason="x", promoted_at="2026-09-27T02:00:00Z",
            versions_dir=tmp_path / "versions",
        )


def test_rollback_refuses_empty_promoted_by(tmp_path):
    """A binding `rollback()` builds must always round-trip through
    `load_binding()`, which requires `promotedBy` to be a non-empty string —
    so `rollback()` validates it up front, matching `promote()`."""
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    _write_binding(tmp_path, history=[_revision(revision=1)])
    binding = cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")
    with pytest.raises(cr.ContextReleaseError, match="promoted_by"):
        cr.rollback(
            binding, to_revision=1, promoted_by="   ", reason="x", promoted_at="2026-09-27T02:00:00Z",
            versions_dir=tmp_path / "versions",
        )


def test_rollback_refuses_empty_promoted_at(tmp_path):
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0")
    _write_binding(tmp_path, history=[_revision(revision=1)])
    binding = cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")
    with pytest.raises(cr.ContextReleaseError, match="promoted_at"):
        cr.rollback(
            binding, to_revision=1, promoted_by="octocat", reason="x", promoted_at="   ",
            versions_dir=tmp_path / "versions",
        )


# ---------------------------------------------------------------------------
# build_version_document — lifecycle/agents preservation on refresh
# (commit 8266e37)
# ---------------------------------------------------------------------------
def test_build_version_document_preserves_existing_lifecycle_and_agents_on_refresh(tmp_path):
    """The documented hash-drift repair command — `publish` with no
    `--lifecycle` — must be a pure hash refresh: it must not silently
    resurrect a deprecated/disabled version back to 'published' or reset
    `agents` to the single-agent default."""
    versions_dir = tmp_path / "versions"
    first = cr.build_version_document(
        "deterministic-fixed-order",
        "1.0.0",
        lifecycle="deprecated",
        agents=["issue-investigator", "some-other-agent"],
        versions_dir=versions_dir,
    )
    path = versions_dir / "deterministic-fixed-order" / "1.0.0.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(first), encoding="utf-8")

    refreshed = cr.build_version_document("deterministic-fixed-order", "1.0.0", versions_dir=versions_dir)
    assert refreshed["spec"]["lifecycle"] == "deprecated"
    assert refreshed["spec"]["agents"] == ["issue-investigator", "some-other-agent"]


def test_build_version_document_defaults_when_no_existing_document(tmp_path):
    versions_dir = tmp_path / "versions"
    document = cr.build_version_document("deterministic-fixed-order", "1.0.0", versions_dir=versions_dir)
    assert document["spec"]["lifecycle"] == "published"
    assert document["spec"]["agents"] == ["issue-investigator"]


# ---------------------------------------------------------------------------
# T13 — drift guard: a synthetic edit to a declared implementation file makes
# the real-catalog drift-guard test below fail with the republish command.
# ---------------------------------------------------------------------------
def test_synthetic_edit_to_implementation_file_changes_the_hash(tmp_path):
    """Simulates "a change to context_assembly.py without republishing":
    editing a byte anywhere in a declared file moves
    `compute_implementation_hash`, which is exactly what makes the real
    catalog's drift-guard test (test_published_catalog_hashes_are_not_drifted,
    below) fail loudly instead of silently."""
    scratch = tmp_path / "orchestrator"
    scratch.mkdir()
    target = scratch / "context_assembly.py"
    target.write_text("STRATEGY_VERSION = '1.0.0'\n")
    before = cr.compute_implementation_hash(["orchestrator/context_assembly.py"], root=tmp_path)
    target.write_text("STRATEGY_VERSION = '1.0.1'  # a change nobody republished\n")
    after = cr.compute_implementation_hash(["orchestrator/context_assembly.py"], root=tmp_path)
    assert before != after


# ---------------------------------------------------------------------------
# Drift guard against the REAL, committed catalog (task 11's CI guard) and
# the shadow binding's CI preflight (task 11's `resolve` preflight).
# ---------------------------------------------------------------------------
def test_published_catalog_hashes_are_not_drifted():
    """Recomputes every published version's implementationHash against the
    working tree. If this fails: `orchestrator/context_assembly.py` or
    `orchestrator/context_snapshot.py` changed without republishing —
    the message names the exact command that fixes it."""
    for name in cr.IMPLEMENTATION_FILES_BY_STRATEGY:
        try:
            cr.load_version(name, "1.0.0")
        except cr.ContextReleaseError as exc:
            pytest.fail(
                f"{name}@1.0.0 failed to load against the working tree: {exc}. If this is a hash "
                f"mismatch, republish with: python tools/context_release.py publish --strategy {name} "
                "--version 1.0.0"
            )


def test_committed_shadow_binding_resolves():
    """The CI preflight: every committed binding must resolve on this
    commit. It must resolve to exactly what the investigator runs today.
    Slice A shipped revision 1 (the shadow baseline); Slice B
    (mctlhq/mctl-agents#527) appends revision 2 when it republishes the
    catalog for its own `context_assembly.py` changes — the exact revision
    number is whatever the append-only history's highest entry is, never
    hard-coded here (design.md's "the second to merge rebases and appends")."""
    resolved = cr.resolve("issue-investigator", "shadow")
    assert resolved.verdict == cr.VERDICT_OK
    assert resolved.strategy == "deterministic-fixed-order"
    assert resolved.version == "1.0.0"
    assert resolved.release_revision >= 2


def test_committed_catalog_has_no_production_binding():
    """Slice A explicitly does not ship a production binding — task 3's DoD."""
    assert not (cr.BINDINGS_DIR / "production" / "issue-investigator.yaml").exists()


# ---------------------------------------------------------------------------
# T14 — end-to-end: publish -> promote -> resolve on a temporary catalog
# root; --dry-run writes nothing.
# ---------------------------------------------------------------------------
def test_cli_publish_promote_resolve_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    versions_dir = tmp_path / "versions"
    bindings_dir = tmp_path / "bindings"

    published = cr.build_version_document("deterministic-fixed-order", "1.0.0")
    path = versions_dir / "deterministic-fixed-order" / "1.0.0.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(published), encoding="utf-8")

    binding = cr.promote(
        None,
        agent="issue-investigator",
        environment="shadow",
        strategy_name="deterministic-fixed-order",
        strategy_version="1.0.0",
        promoted_by="octocat",
        reason="e2e",
        promoted_at="2026-09-27T00:00:00Z",
        versions_dir=versions_dir,
    )
    binding_path = bindings_dir / "shadow" / "issue-investigator.yaml"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(yaml.safe_dump(binding.to_dict()), encoding="utf-8")

    resolved = cr.resolve("issue-investigator", "shadow", versions_dir=versions_dir, bindings_dir=bindings_dir)
    assert resolved.verdict == cr.VERDICT_OK
    assert resolved.strategy == "deterministic-fixed-order"
    assert resolved.release_revision == 1


def test_cli_publish_dry_run_writes_nothing(tmp_path):
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "tools" / "context_release.py"),
            "publish", "--strategy", "deterministic-fixed-order", "--version", "1.0.0", "--dry-run",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "would write" in result.stdout
    assert not (tmp_path / "config").exists()


def test_cli_resolve_prints_verdict():
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "tools" / "context_release.py"),
            "resolve", "--agent", "issue-investigator", "--environment", "shadow",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "verdict=ok" in result.stdout


def test_cli_promote_rejects_unsafe_agent_path_segment(tmp_path):
    """`--agent`/`--environment` are validated against a safe path-segment
    pattern before they reach `BINDINGS_DIR / environment / f"{agent}.yaml"`
    (commit 8266e37) — a `..` segment must be rejected by argparse, not
    silently escape the catalog directory."""
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "tools" / "context_release.py"),
            "promote", "--agent", "../escape", "--environment", "shadow",
            "--strategy", "deterministic-fixed-order", "--version", "1.0.0",
            "--promoted-by", "octocat", "--reason", "x",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "must be a single path segment" in result.stderr


def test_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "context_release.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "publish" in result.stdout
    assert "promote" in result.stdout
    assert "rollback" in result.stdout
    assert "resolve" in result.stdout
