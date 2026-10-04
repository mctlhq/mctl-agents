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

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from orchestrator import context_eval as ce
from orchestrator import context_release as cr
from orchestrator.work_context.snapshots import StoreRef

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
def test_production_promotion_with_no_evidence_is_refused_as_evidence_missing(tmp_path):
    """mctlhq/mctl-agents#528: production still refuses `evidence.kind:
    none` (or no evidence at all) as `evidence-missing` — this is still true
    once the real gate exists, now for a stated reason rather than
    unconditionally for every environment but `shadow`."""
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
            evidence_kind="none",
            versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISSING


def test_non_shadow_promotion_to_an_unrecognised_environment_is_unknown(tmp_path):
    """mctlhq/mctl-agents#528: `promote()` allowlists exactly `{shadow,
    production}` (`PROMOTION_ENVIRONMENTS`) — any other environment name is
    `unknown`, naming both supported ones; this module cannot classify
    anything else, so it no longer guesses `evidence-missing`."""
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
    assert excinfo.value.code == cr.VERDICT_UNKNOWN
    assert "shadow" in str(excinfo.value) and "production" in str(excinfo.value)


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
# mctlhq/mctl-agents#528 — the production evidence gate
# (assess_production_evidence, promote()'s production path)
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)


def _loaded_version(tmp_path: Path, *, name: str = "deterministic-fixed-order", version: str = "1.0.0", **overrides):
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name=name, version=version, **overrides)
    return cr.load_version(name, version, versions_dir=versions_dir)


def _identity_for(version: cr.ContextStrategyVersion, **overrides) -> ce.EvidenceIdentity:
    base = dict(
        strategy_name=version.name, strategy_version=version.version,
        ranker_name=version.ranker_name, ranker_version=version.ranker_version,
        strategy_content_hash=version.content_hash, strategy_implementation_hash=version.implementation_hash,
        evaluator_name=ce.EVALUATOR_NAME, evaluator_version=ce.EVALUATOR_VERSION,
        metrics_contract_version=ce.METRICS_CONTRACT_VERSION,
    )
    base.update(overrides)
    return ce.EvidenceIdentity(**base)


def _observe_candidate_record(
    version: cr.ContextStrategyVersion,
    *,
    observed_at: str,
    execution_id: str,
    work_item_id: str = "wi_1",
    verdict: str = ce.VERDICT_EVALUATED,
    evaluator_version: str = ce.EVALUATOR_VERSION,
    identity: ce.EvidenceIdentity | None = None,
) -> ce.EvalRecord:
    resolved_identity = (
        identity if identity is not None else _identity_for(version, evaluator_version=evaluator_version)
    )
    return ce.EvalRecord(
        record_kind=ce.RECORD_KIND, evaluator_name=ce.EVALUATOR_NAME, evaluator_version=evaluator_version,
        verdict=verdict, identity=resolved_identity, evidence_kind="observe-candidate",
        context_snapshot_id="cs-x", content_hash="sha256:" + "c" * 64, store_ref=None, metrics=None, outcome=None,
        observed_at=observed_at, execution_ref=ce.ExecutionRef(work_item_id=work_item_id, execution_id=execution_id),
    )


def _three_fresh_records(version: cr.ContextStrategyVersion) -> list[ce.EvalRecord]:
    isos = ("2026-09-26T00:00:00Z", "2026-09-25T00:00:00Z", "2026-09-24T00:00:00Z")
    return [
        _observe_candidate_record(version, observed_at=iso, execution_id=f"we_{i}") for i, iso in enumerate(isos)
    ]


def test_assess_production_evidence_passes_with_three_fresh_observations(tmp_path):
    version = _loaded_version(tmp_path)
    verdict = cr.assess_production_evidence(
        version=version, records=_three_fresh_records(version), evidence_evaluator_version=ce.EVALUATOR_VERSION,
        now=NOW,
    )
    assert verdict.code == cr.VERDICT_OK
    assert verdict.observations == 3
    assert verdict.newest_observed_at == "2026-09-26T00:00:00Z"
    assert verdict.window_seconds == ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS


def test_assess_production_evidence_no_records_is_evidence_missing(tmp_path):
    version = _loaded_version(tmp_path)
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=[], evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISSING


def test_assess_production_evidence_drops_non_observe_candidate_records(tmp_path):
    """`live`/`stored-replay`/`fixture-baseline`/`none` records — even a
    `live` record of a run where the candidate was itself authoritative —
    never satisfy the soak gate (T16)."""
    version = _loaded_version(tmp_path)
    live_records = [
        ce.EvalRecord(
            record_kind=ce.RECORD_KIND, evaluator_name=ce.EVALUATOR_NAME, evaluator_version=ce.EVALUATOR_VERSION,
            verdict=ce.VERDICT_EVALUATED, identity=_identity_for(version), evidence_kind="live",
            context_snapshot_id="cs-x", content_hash="sha256:" + "c" * 64,
            store_ref=None, metrics=None, outcome=None, observed_at=iso,
        )
        for iso in ("2026-09-26T00:00:00Z", "2026-09-25T00:00:00Z", "2026-09-24T00:00:00Z")
    ]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=live_records, evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISSING

    # Mixed with `observe-candidate` records of a DIFFERENT identity: still
    # never satisfies the soak gate for THIS version.
    other_identity = _identity_for(version, strategy_version="9.9.9")
    mixed = live_records + [
        _observe_candidate_record(version, observed_at=iso, execution_id=f"we_{i}", identity=other_identity)
        for i, iso in enumerate(("2026-09-26T00:00:01Z", "2026-09-25T00:00:01Z", "2026-09-24T00:00:01Z"))
    ]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=mixed, evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISMATCH  # declared-identity mismatch, not missing


def test_assess_production_evidence_hash_mismatch_precedes_insufficient(tmp_path):
    """A `verdict: hash-mismatch` record for the promoted identity is refused
    as `hash-mismatch`, not `evidence-insufficient` — pinning the precedence
    (T5): `assess_evidence` would otherwise silently drop it from `usable`
    and report `insufficient-observations` instead, hiding the real fault."""
    version = _loaded_version(tmp_path)
    records = [_observe_candidate_record(version, observed_at="2026-09-26T00:00:00Z", execution_id="we_0",
                                          verdict=ce.VERDICT_HASH_MISMATCH)]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=records, evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_HASH_MISMATCH


def test_assess_production_evidence_evaluator_version_mismatch(tmp_path):
    version = _loaded_version(tmp_path)
    records = _three_fresh_records(version)
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=records, evidence_evaluator_version="9.9.9", now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISMATCH


def test_assess_production_evidence_evaluator_version_required(tmp_path):
    version = _loaded_version(tmp_path)
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=_three_fresh_records(version), evidence_evaluator_version=None, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISMATCH


@pytest.mark.parametrize(
    "override",
    [
        {"strategy_name": "trust-freshness-ranked"},
        {"strategy_version": "9.9.9"},
        {"strategy_content_hash": "sha256:" + "9" * 64},
        {"strategy_implementation_hash": "sha256:" + "9" * 64},
    ],
)
def test_assess_production_evidence_four_way_identity_mismatch(tmp_path, override):
    version = _loaded_version(tmp_path)
    identity = _identity_for(version, **override)
    records = [
        _observe_candidate_record(version, observed_at=iso, execution_id=f"we_{i}", identity=identity)
        for i, iso in enumerate(("2026-09-26T00:00:00Z", "2026-09-25T00:00:00Z", "2026-09-24T00:00:00Z"))
    ]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=records, evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISMATCH


def test_assess_production_evidence_stale(tmp_path):
    version = _loaded_version(tmp_path)
    old_records = [
        _observe_candidate_record(version, observed_at=iso, execution_id=f"we_{i}")
        for i, iso in enumerate(("2026-01-03T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"))
    ]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=old_records, evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_STALE


def test_assess_production_evidence_insufficient_observations_and_retries_count_once(tmp_path):
    """T15: two distinct executions is insufficient; three retries of ONE
    execution still count once, not three."""
    version = _loaded_version(tmp_path)
    two_only = _three_fresh_records(version)[:2]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=two_only, evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_INSUFFICIENT

    retries = [
        _observe_candidate_record(version, observed_at=iso, execution_id="we_same")
        for iso in ("2026-09-26T00:00:03Z", "2026-09-26T00:00:02Z", "2026-09-26T00:00:01Z")
    ]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=retries, evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_INSUFFICIENT


def test_assess_production_evidence_rejects_observe_candidate_with_store_ref(tmp_path):
    """P1 (issue-528 slice C review, second remaining route): an
    `observe-candidate` record must never carry a `store_ref` — `evaluate()`
    refuses this at construction time, but a record parsed from a
    hand-authored `--evidence-file` is not `evaluate()`-built. Left
    unchecked, such a record's `store_ref`-keyed observation could win
    `assess_evidence`'s own anchor selection while `assess_production_
    evidence`'s anchor selection (which only ever looks at `execution_ref`)
    finds none, writing a `newest_observed_at=''` into an otherwise `fresh`
    verdict that `load_binding()` then refuses to read back."""
    version = _loaded_version(tmp_path)
    identity = _identity_for(version)
    malformed = ce.EvalRecord(
        record_kind=ce.RECORD_KIND, evaluator_name=ce.EVALUATOR_NAME, evaluator_version=ce.EVALUATOR_VERSION,
        verdict=ce.VERDICT_EVALUATED, identity=identity, evidence_kind="observe-candidate",
        context_snapshot_id="cs-x", content_hash="sha256:" + "c" * 64,
        store_ref=StoreRef(
            work_item_id="wi_1", execution_id="we_0", store_snapshot_id="ss-1",
            store_content_hash="sha256:" + "d" * 64,
        ),
        metrics=None, outcome=None, observed_at="2026-09-26T00:00:00Z", execution_ref=None,
    )
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=[malformed], evidence_evaluator_version=ce.EVALUATOR_VERSION, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_UNKNOWN
    assert "store_ref" in str(excinfo.value)


def test_assess_production_evidence_rejects_evaluator_version_not_matching_the_image(tmp_path):
    """P2 (issue-528 slice C review, still-open hole): sourcing `expected.
    evaluator_version` from `context_eval.EVALUATOR_VERSION` has no effect —
    `assess_evidence` never compares that field (`_declared_identity_matches`/
    `_catalog_identity_matches` do not read it). The gate must reject
    self-consistent-but-stale evidence directly, by comparing the declared
    `evaluator_version` against the running image's own constant."""
    version = _loaded_version(tmp_path)
    stale_version = ce.EVALUATOR_VERSION + "-stale"
    records = [
        _observe_candidate_record(version, observed_at=iso, execution_id=f"we_{i}", evaluator_version=stale_version)
        for i, iso in enumerate(("2026-09-26T00:00:00Z", "2026-09-25T00:00:00Z", "2026-09-24T00:00:00Z"))
    ]
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.assess_production_evidence(
            version=version, records=records, evidence_evaluator_version=stale_version, now=NOW
        )
    assert excinfo.value.code == cr.VERDICT_EVIDENCE_MISMATCH


def test_promote_context_eval_requires_non_empty_evidence_ref(tmp_path):
    """P1 (issue-528 slice C review, first remaining route): `promote()` is
    a public library function on its own, not only reachable through the
    CLI's own `--evidence-ref` check (tools/context_release.py). It must not
    rely on that caller to keep `load_binding()`'s invariant that every
    `context-eval` revision carries a non-empty `evidence.ref`."""
    versions_dir = tmp_path / "versions"
    version = _loaded_version(tmp_path)
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.promote(
            None, agent="issue-investigator", environment="shadow", strategy_name=version.name,
            strategy_version=version.version, promoted_by="octocat", reason="missing ref",
            promoted_at="2026-09-27T00:00:00Z", evidence_kind="context-eval", evidence_ref=None,
            evidence_evaluator_version=ce.EVALUATOR_VERSION, evidence_records=_three_fresh_records(version),
            now=NOW, versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_UNKNOWN
    assert "evidence_ref" in str(excinfo.value)


def test_context_release_never_imports_context_eval_at_module_scope():
    """mctlhq/mctl-agents#528's hard invariant: `assess_production_evidence`
    imports `orchestrator.context_eval` inside its own function body only —
    never at column 0 (module scope)."""
    source = (REPO_ROOT / "orchestrator" / "context_release.py").read_text(encoding="utf-8")
    assert not re.search(r"^(import|from)\s+orchestrator\.context_eval\b", source, re.MULTILINE)
    assert not re.search(r"^from\s+orchestrator\s+import\s+.*\bcontext_eval\b", source, re.MULTILINE)
    # ... but the deferred import genuinely exists, indented, inside a function body.
    assert re.search(r"^\s+from orchestrator import context_eval\b", source, re.MULTILINE)


def test_context_release_never_reads_an_environment_variable():
    source = (REPO_ROOT / "orchestrator" / "context_release.py").read_text(encoding="utf-8")
    assert "os.getenv" not in source
    assert "os.environ" not in source


def test_promote_production_with_fresh_evidence_appends_one_revision(tmp_path):
    """T8: a passing production promotion records `kind: context-eval`, a
    non-empty `ref`, the `evaluatorVersion`, the newest `observedAt` and the
    counted `observations`; every prior revision is byte-identical."""
    versions_dir = tmp_path / "versions"
    version = _loaded_version(tmp_path)
    shadow_binding = cr.promote(
        None, agent="issue-investigator", environment="shadow", strategy_name=version.name,
        strategy_version=version.version, promoted_by="octocat", reason="shadow baseline",
        promoted_at="2026-09-20T00:00:00Z", versions_dir=versions_dir,
    )
    updated = cr.promote(
        None, agent="issue-investigator", environment="production", strategy_name=version.name,
        strategy_version=version.version, promoted_by="octocat", reason="ready for production",
        promoted_at="2026-09-27T00:00:00Z", evidence_kind="context-eval",
        evidence_ref="argo-workflow-logs://issue-investigator/run-42",
        evidence_evaluator_version=ce.EVALUATOR_VERSION, evidence_records=_three_fresh_records(version), now=NOW,
        versions_dir=versions_dir,
    )
    assert updated.active.revision == 1
    assert updated.active.evidence_kind == "context-eval"
    assert updated.active.evidence_ref == "argo-workflow-logs://issue-investigator/run-42"
    assert updated.active.evidence_evaluator_version == ce.EVALUATOR_VERSION
    assert updated.active.evidence_observed_at == "2026-09-26T00:00:00Z"
    assert updated.active.evidence_observations == 3
    # The shadow binding this test also builds is untouched by the
    # production promotion above — they are different (agent, environment)
    # documents.
    assert shadow_binding.active.revision == 1
    assert shadow_binding.active.evidence_kind == "none"


def test_promote_shadow_with_context_eval_evidence_round_trips_through_load_binding(tmp_path):
    """A `shadow` promotion that declares `evidence.kind: context-eval` must
    still fill `observedAt`/`observations`: `load_binding()` requires both of
    those fields on ANY `context-eval` revision, not only a `production` one
    (`test_load_binding_context_eval_revision_requires_the_four_evidence_fields`).
    Without running the evidence assessment for `shadow` too, `promote()`
    would write a revision `load_binding()` can never read back."""
    versions_dir = tmp_path / "versions"
    bindings_dir = tmp_path / "bindings"
    version = _loaded_version(tmp_path)
    updated = cr.promote(
        None, agent="issue-investigator", environment="shadow", strategy_name=version.name,
        strategy_version=version.version, promoted_by="octocat", reason="shadow, with real evidence attached",
        promoted_at="2026-09-27T00:00:00Z", evidence_kind="context-eval",
        evidence_ref="argo-workflow-logs://issue-investigator/run-42",
        evidence_evaluator_version=ce.EVALUATOR_VERSION, evidence_records=_three_fresh_records(version), now=NOW,
        versions_dir=versions_dir,
    )
    assert updated.active.evidence_observed_at == "2026-09-26T00:00:00Z"
    assert updated.active.evidence_observations == 3

    path = bindings_dir / "shadow" / "issue-investigator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(updated.to_dict()), encoding="utf-8")

    reloaded = cr.load_binding("issue-investigator", "shadow", bindings_dir=bindings_dir)
    assert reloaded.active.evidence_kind == "context-eval"
    assert reloaded.active.evidence_observed_at == "2026-09-26T00:00:00Z"
    assert reloaded.active.evidence_observations == 3


def test_promote_production_never_mutates_a_prior_revision(tmp_path):
    versions_dir = tmp_path / "versions"
    version = _loaded_version(tmp_path)
    first = cr.promote(
        None, agent="issue-investigator", environment="production", strategy_name=version.name,
        strategy_version=version.version, promoted_by="octocat", reason="first",
        promoted_at="2026-09-27T00:00:00Z", evidence_kind="context-eval", evidence_ref="run-1",
        evidence_evaluator_version=ce.EVALUATOR_VERSION, evidence_records=_three_fresh_records(version), now=NOW,
        versions_dir=versions_dir,
    )
    later_records = [
        _observe_candidate_record(version, observed_at=iso, execution_id=f"we_later_{i}")
        for i, iso in enumerate(("2026-09-28T00:00:00Z", "2026-09-27T00:00:00Z", "2026-09-26T00:00:00Z"))
    ]
    second = cr.promote(
        first, agent="issue-investigator", environment="production", strategy_name=version.name,
        strategy_version=version.version, promoted_by="octocat", reason="second",
        promoted_at="2026-09-28T00:00:00Z", evidence_kind="context-eval", evidence_ref="run-2",
        evidence_evaluator_version=ce.EVALUATOR_VERSION, evidence_records=later_records,
        now=datetime(2026, 9, 28, tzinfo=UTC), versions_dir=versions_dir,
    )
    assert [r.revision for r in second.history] == [1, 2]
    assert second.history[0] == first.history[0]
    assert second.active.revision == 2


def test_promote_production_requires_now(tmp_path):
    versions_dir = tmp_path / "versions"
    version = _loaded_version(tmp_path)
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.promote(
            None, agent="issue-investigator", environment="production", strategy_name=version.name,
            strategy_version=version.version, promoted_by="octocat", reason="x",
            promoted_at="2026-09-27T00:00:00Z", evidence_kind="context-eval",
            evidence_ref="run-1", evidence_evaluator_version=ce.EVALUATOR_VERSION,
            evidence_records=_three_fresh_records(version), now=None, versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_UNKNOWN


def test_promote_production_deprecated_version_refused_while_existing_binding_resolves(tmp_path):
    versions_dir = tmp_path / "versions"
    bindings_dir = tmp_path / "bindings"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0", lifecycle="deprecated")
    _write_binding(
        tmp_path, environment="production",
        history=[_revision(lifecycle="deprecated", evidence_kind="none")],
    )
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.promote(
            None, agent="issue-investigator", environment="production", strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0", promoted_by="octocat", reason="try again", promoted_at="2026-09-27T00:00:00Z",
            evidence_kind="context-eval", evidence_ref="run-1", evidence_evaluator_version=ce.EVALUATOR_VERSION,
            now=NOW, versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_VERSION_NOT_PROMOTABLE
    resolved = cr.resolve(
        "issue-investigator", "production", versions_dir=versions_dir, bindings_dir=bindings_dir
    )
    assert resolved.verdict == cr.VERDICT_OK


def test_promote_production_disabled_version_refused(tmp_path):
    versions_dir = tmp_path / "versions"
    _write_version(tmp_path, name="deterministic-fixed-order", version="1.0.0", lifecycle="disabled")
    with pytest.raises(cr.ContextReleaseError) as excinfo:
        cr.promote(
            None, agent="issue-investigator", environment="production", strategy_name="deterministic-fixed-order",
            strategy_version="1.0.0", promoted_by="octocat", reason="x", promoted_at="2026-09-27T00:00:00Z",
            evidence_kind="context-eval", evidence_ref="run-1", evidence_evaluator_version=ce.EVALUATOR_VERSION,
            now=NOW, versions_dir=versions_dir,
        )
    assert excinfo.value.code == cr.VERDICT_VERSION_DISABLED


# ---------------------------------------------------------------------------
# load_binding()'s evidence-shape checks (Task 2 DoD)
# ---------------------------------------------------------------------------
def test_load_binding_context_eval_revision_requires_the_four_evidence_fields(tmp_path):
    doc = _binding_doc(
        environment="production",
        history=[
            {
                **_revision(evidence_kind="context-eval"),
                "evidence": {"kind": "context-eval", "ref": None, "evaluatorVersion": "1.0.0"},
            }
        ],
    )
    path = tmp_path / "bindings" / "production" / "issue-investigator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(cr.ContextReleaseError, match=r"evidence\.ref"):
        cr.load_binding("issue-investigator", "production", bindings_dir=tmp_path / "bindings")


def test_load_binding_context_eval_revision_requires_observations_to_be_a_positive_int(tmp_path):
    doc = _binding_doc(
        environment="production",
        history=[
            {
                **_revision(evidence_kind="context-eval"),
                "evidence": {
                    "kind": "context-eval", "ref": "run-1", "evaluatorVersion": "1.0.0",
                    "observedAt": "2026-09-26T00:00:00Z", "observations": 0,
                },
            }
        ],
    )
    path = tmp_path / "bindings" / "production" / "issue-investigator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(cr.ContextReleaseError, match=r"evidence\.observations"):
        cr.load_binding("issue-investigator", "production", bindings_dir=tmp_path / "bindings")


def test_load_binding_none_revision_must_not_carry_observed_at_or_observations(tmp_path):
    doc = _binding_doc(
        environment="shadow",
        history=[
            {
                **_revision(evidence_kind="none"),
                "evidence": {"kind": "none", "ref": None, "evaluatorVersion": None, "observations": 3},
            }
        ],
    )
    path = tmp_path / "bindings" / "shadow" / "issue-investigator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(cr.ContextReleaseError, match="must not carry"):
        cr.load_binding("issue-investigator", "shadow", bindings_dir=tmp_path / "bindings")


def test_load_binding_context_eval_revision_round_trips_through_to_dict(tmp_path):
    doc = _binding_doc(
        environment="production",
        history=[
            {
                **_revision(evidence_kind="context-eval"),
                "evidence": {
                    "kind": "context-eval", "ref": "run-1", "evaluatorVersion": "1.0.0",
                    "observedAt": "2026-09-26T00:00:00Z", "observations": 3,
                },
            }
        ],
    )
    path = tmp_path / "bindings" / "production" / "issue-investigator.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    binding = cr.load_binding("issue-investigator", "production", bindings_dir=tmp_path / "bindings")
    assert binding.active.evidence_observed_at == "2026-09-26T00:00:00Z"
    assert binding.active.evidence_observations == 3
    assert binding.to_dict()["spec"]["history"][0]["evidence"]["observations"] == 3


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


def test_cli_promote_requires_evidence_ref_for_context_eval_kind(tmp_path):
    """T13/T6: `--evidence-kind context-eval` without `--evidence-ref` is
    rejected before any catalog access — this never touches the real
    committed catalog, so it is safe regardless of its current hash state."""
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "tools" / "context_release.py"),
            "promote", "--agent", "issue-investigator", "--environment", "production",
            "--strategy", "deterministic-fixed-order", "--version", "1.0.0",
            "--promoted-by", "octocat", "--reason", "x", "--evidence-kind", "context-eval",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unknown:" in result.stderr
    assert "--evidence-ref" in result.stderr


def test_cli_promote_malformed_evidence_line_exits_nonzero_naming_the_line(tmp_path):
    """T13: an unparsable evidence file exits non-zero with `unknown:` and
    the offending line number, before any catalog access."""
    evidence_path = tmp_path / "evidence.jsonl"
    valid_line = json.dumps({"record_kind": ce.RECORD_KIND, "verdict": ce.VERDICT_EVALUATED})
    evidence_path.write_text(f"{valid_line}\nnot-json-at-all\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "tools" / "context_release.py"),
            "promote", "--agent", "issue-investigator", "--environment", "production",
            "--strategy", "deterministic-fixed-order", "--version", "1.0.0",
            "--promoted-by", "octocat", "--reason", "x", "--evidence-kind", "context-eval",
            "--evidence-ref", "run-1", "--evidence-file", str(evidence_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "unknown:" in result.stderr
    assert "line 2" in result.stderr


def _real_catalog_evidence_records() -> list[str]:
    """JSONL lines of three fresh, distinct-execution `observe-candidate`
    records matching the REAL committed `deterministic-fixed-order`@1.0.0
    identity — read via `cr.load_version` so this never hardcodes a hash
    that a republish would change."""
    version = cr.load_version("deterministic-fixed-order", "1.0.0")
    identity = ce.EvidenceIdentity(
        strategy_name=version.name, strategy_version=version.version, ranker_name=version.ranker_name,
        ranker_version=version.ranker_version, strategy_content_hash=version.content_hash,
        strategy_implementation_hash=version.implementation_hash, evaluator_name=ce.EVALUATOR_NAME,
        evaluator_version=ce.EVALUATOR_VERSION, metrics_contract_version=ce.METRICS_CONTRACT_VERSION,
    )
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0)
    isos = tuple((now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ") for d in (1, 2, 3))
    records = [
        ce.EvalRecord(
            record_kind=ce.RECORD_KIND, evaluator_name=ce.EVALUATOR_NAME, evaluator_version=ce.EVALUATOR_VERSION,
            verdict=ce.VERDICT_EVALUATED, identity=identity, evidence_kind="observe-candidate",
            context_snapshot_id="cs-x", content_hash="sha256:" + "c" * 64, store_ref=None, metrics=None,
            outcome=None, observed_at=iso,
            execution_ref=ce.ExecutionRef(work_item_id="wi_1", execution_id=f"we_{i}"),
        )
        for i, iso in enumerate(isos)
    ]
    return [json.dumps(r.to_log_dict()) for r in records]


def test_cli_promote_production_dry_run_with_fresh_evidence_prints_the_gate_and_writes_nothing(tmp_path):
    """T13: `promote --environment production --evidence-file <fresh>.jsonl
    --dry-run` prints the passing gate and writes no file. Runs against the
    REAL committed catalog (`cr.load_version` inside the CLI has no
    `--versions-dir` override), so this DoD is only exercised once task 10's
    republish has run — see `tests/test_context_release.py::test_published_
    catalog_hashes_are_not_drifted` for that guard."""
    evidence_path = tmp_path / "evidence.jsonl"
    evidence_path.write_text("\n".join(_real_catalog_evidence_records()) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "tools" / "context_release.py"),
            "promote", "--agent", "issue-investigator", "--environment", "production",
            "--strategy", "deterministic-fixed-order", "--version", "1.0.0",
            "--promoted-by", "octocat", "--reason", "3 clean observe-mode soak runs",
            "--evidence-kind", "context-eval", "--evidence-ref", "argo-workflow-logs://soak-2026-09",
            "--evidence-evaluator-version", ce.EVALUATOR_VERSION, "--evidence-file", str(evidence_path),
            "--dry-run",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "gate: status=ok" in result.stdout
    assert "observations=3" in result.stdout
    assert "would write" in result.stdout
    assert not (REPO_ROOT / "config" / "context-strategies" / "bindings" / "production").exists()


def test_cli_promote_production_stale_evidence_exits_nonzero_naming_evidence_stale(tmp_path):
    version = cr.load_version("deterministic-fixed-order", "1.0.0")
    identity = ce.EvidenceIdentity(
        strategy_name=version.name, strategy_version=version.version, ranker_name=version.ranker_name,
        ranker_version=version.ranker_version, strategy_content_hash=version.content_hash,
        strategy_implementation_hash=version.implementation_hash, evaluator_name=ce.EVALUATOR_NAME,
        evaluator_version=ce.EVALUATOR_VERSION, metrics_contract_version=ce.METRICS_CONTRACT_VERSION,
    )
    stale_isos = ("2026-01-03T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")
    records = [
        ce.EvalRecord(
            record_kind=ce.RECORD_KIND, evaluator_name=ce.EVALUATOR_NAME, evaluator_version=ce.EVALUATOR_VERSION,
            verdict=ce.VERDICT_EVALUATED, identity=identity, evidence_kind="observe-candidate",
            context_snapshot_id="cs-x", content_hash="sha256:" + "c" * 64, store_ref=None, metrics=None,
            outcome=None, observed_at=iso,
            execution_ref=ce.ExecutionRef(work_item_id="wi_1", execution_id=f"we_{i}"),
        )
        for i, iso in enumerate(stale_isos)
    ]
    evidence_path = tmp_path / "evidence.jsonl"
    evidence_path.write_text("\n".join(json.dumps(r.to_log_dict()) for r in records) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "tools" / "context_release.py"),
            "promote", "--agent", "issue-investigator", "--environment", "production",
            "--strategy", "deterministic-fixed-order", "--version", "1.0.0",
            "--promoted-by", "octocat", "--reason", "stale evidence", "--evidence-kind", "context-eval",
            "--evidence-ref", "argo-workflow-logs://soak-2026-01", "--evidence-evaluator-version",
            ce.EVALUATOR_VERSION, "--evidence-file", str(evidence_path), "--dry-run",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "evidence-stale" in result.stderr


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
