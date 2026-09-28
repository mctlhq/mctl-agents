"""Tests for `orchestrator.context_eval` (mctlhq/mctl-agents#526, ADR 015).

Two halves:

- A fixture harness (`_all_records`/`_seal_case`) that drives every curated
  case under `tests/fixtures/context_eval/cases/` through the REAL
  `context_assembly.run_pipeline`, under both strategies, with a fixed `now`
  and a fixed `ExecutionCorrelation` (the same literals
  `tests/test_context_ranking.py:32-58` already uses) — never re-implementing
  a pipeline stage — and compares the result against the committed
  `tests/fixtures/context_eval/baseline.json`.
- Focused unit tests against hand-built `ContextSnapshot`s for identity,
  telemetry safety, outcome linking, freshness/promotion-readiness, purity
  and non-authorization (ADR 015 sec. 1, 3, 5, "Evidence freshness" and
  sec. 6), and the replay CLI.

Regenerating the baseline (only when a curated case or the pipeline's
behaviour is DELIBERATELY changing, never to turn a red run green):

    uv run python -m tests.test_context_eval --regenerate [case_id ...]

a `# pragma: no cover` function behind `if __name__ == "__main__":`, exactly
the `tests/test_execution_request_replay.py:397` pattern — never a pytest
fixture, never a CI step.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orchestrator import context_assembly as ca
from orchestrator import context_eval as ce
from orchestrator import context_snapshot as cs
from orchestrator import run_context_eval as rce
from orchestrator.work_context import contract as wc_contract
from orchestrator.work_context import snapshots as wc_snapshots

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = REPO_ROOT / "tests" / "fixtures" / "context_eval" / "cases"
BASELINE_PATH = REPO_ROOT / "tests" / "fixtures" / "context_eval" / "baseline.json"

# The same fixed `now`/`ExecutionCorrelation` literals
# tests/test_context_ranking.py:31-57 already uses.
NOW = datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC)
CREATED_AT = "2026-09-19T00:00:40Z"


def _execution() -> cs.ExecutionCorrelation:
    return cs.ExecutionCorrelation(
        agent="issue-investigator", environment="production",
        temporal_workflow_id="dev-loop-mctlhq-mctl-agents-471", target_repository_sha="a" * 40,
        definition_version="legacy", definition_content_hash="sha256:" + "1a" * 32,
        profile_version="legacy", profile_content_hash="sha256:" + "2b" * 32, release_revision=0,
    )


def _load_cases() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES_DIR.glob("*.json"))]


def _candidate_from(entry: dict[str, Any]) -> ca.CandidateSource:
    fields = dict(entry)
    fields["raw"] = fields.pop("raw_text").encode("utf-8")
    return ca.CandidateSource(**fields)


def _config_from(entry: dict[str, Any], *, strategy: str) -> ca.AssemblyConfig:
    return ca.AssemblyConfig(strategy=strategy, **entry)


def _labels_from(raw: dict[str, Any] | None) -> ce.CaseLabels | None:
    return None if raw is None else ce.CaseLabels.from_dict(raw)


def _catalog_identity(strategy_name: str, strategy_version: str) -> tuple[str, str]:
    from orchestrator import context_release

    version = context_release.load_version(strategy_name, strategy_version)
    return version.content_hash, version.implementation_hash


def _seal_case(case: dict[str, Any], strategy: str) -> tuple[cs.ContextSnapshot, ca.PipelineOutcome, int]:
    """Build `case`'s candidates, run them through the REAL
    `context_assembly.run_pipeline` under `strategy`, and seal the result.
    Never re-implements a pipeline stage."""
    candidates = [_candidate_from(c) for c in case["candidates"]]
    config = _config_from(case["config"], strategy=strategy)
    outcome = ca.run_pipeline(candidates, config, NOW)
    sources = tuple(ca._to_context_source(c) for c in outcome.candidates)
    retention = cs.RetentionPolicy(class_="execution-record", expires_after_days=180)
    snapshot = cs.seal(
        execution=_execution(), strategy=outcome.strategy, budget=outcome.budget, retention=retention,
        created_at=CREATED_AT, sources=sources, conflicts=outcome.conflicts,
    )
    return snapshot, outcome, len(candidates)


def _all_records() -> dict[str, dict[str, ce.EvalRecord]]:
    records: dict[str, dict[str, ce.EvalRecord]] = {}
    for case in _load_cases():
        case_id = case["case_id"]
        records[case_id] = {}
        labels = _labels_from(case.get("labels"))
        for strategy in (ca.STRATEGY_NAME, ca.RANKED_STRATEGY_NAME):
            snapshot, outcome, candidates_total = _seal_case(case, strategy)
            content_hash, implementation_hash = _catalog_identity(strategy, "1.0.0")
            assembly = ce.AssemblyCounters(
                candidates_total=candidates_total, assembly_latency_ms=0.0, capability_calls=0,
                conflict_sources_capped=outcome.counters.conflict_sources_capped,
            )
            records[case_id][strategy] = ce.evaluate(
                snapshot, observed_at=snapshot.created_at, labels=labels, assembly=assembly,
                evidence_kind="fixture-baseline", strategy_content_hash=content_hash,
                strategy_implementation_hash=implementation_hash,
            )
    return records


def _regenerate(names: list[str] | None) -> None:  # pragma: no cover — a maintenance entry point, not a test
    """Regenerate `baseline.json` for the named cases (every case when none
    is named). Requires an explicit, deliberate invocation — never a side
    effect of a normal test run or of CI."""
    records = _all_records()
    existing: dict[str, Any] = {}
    if BASELINE_PATH.is_file():
        existing = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    target_ids = set(names) if names else set(records)
    baseline: dict[str, Any] = {k: v for k, v in existing.items() if k not in target_ids}
    baseline["_header"] = {
        "evaluator_name": ce.EVALUATOR_NAME,
        "evaluator_version": ce.EVALUATOR_VERSION,
        "metrics_contract_version": ce.METRICS_CONTRACT_VERSION,
    }
    for case_id, by_strategy in records.items():
        if case_id not in target_ids:
            continue
        baseline[case_id] = {
            strategy: (record.metrics.to_dict() if record.metrics is not None else {"verdict": record.verdict})
            for strategy, record in by_strategy.items()
        }
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {BASELINE_PATH}")


if __name__ == "__main__":  # pragma: no cover
    import sys

    _argv = sys.argv[1:]
    if "--regenerate" not in _argv:
        raise SystemExit("usage: python -m tests.test_context_eval --regenerate [case_id ...]")
    _argv.remove("--regenerate")
    _regenerate(_argv or None)


# ---------------------------------------------------------------------------
# Fixture harness / baseline (T5-T7, T19-T22)
# ---------------------------------------------------------------------------


def test_baseline_matches():
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    records = _all_records()
    failures: list[str] = []
    for case_id, by_strategy in records.items():
        for strategy, record in by_strategy.items():
            actual = record.metrics.to_dict()
            expected = baseline[case_id][strategy]
            for metric, expected_value in expected.items():
                if actual.get(metric) != expected_value:
                    failures.append(
                        f"{case_id}/{strategy}/{metric}: actual={actual.get(metric)!r} expected={expected_value!r}"
                    )
    assert not failures, "\n".join(failures)


def test_baseline_drift_is_detected_and_named():
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    case_id, strategy, metric = "02-stale-source", ca.STRATEGY_NAME, "stale_rate"
    mutated = copy.deepcopy(baseline)
    mutated[case_id][strategy][metric] = 999.0

    records = _all_records()
    actual = records[case_id][strategy].metrics.to_dict()
    failures = [
        f"{case_id}/{strategy}/{key}"
        for key, expected_value in mutated[case_id][strategy].items()
        if actual.get(key) != expected_value
    ]
    assert failures == [f"{case_id}/{strategy}/{metric}"]


def test_fixture_harness_ignores_context_env_vars(monkeypatch):
    for name, value in (
        ("ISSUE_INVESTIGATOR_CONTEXT_MODE", "on"),
        ("ISSUE_INVESTIGATOR_CONTEXT_STRATEGY", "trust-freshness-ranked"),
        ("ISSUE_INVESTIGATOR_CONTEXT_MAX_SOURCES", "1"),
        ("ISSUE_INVESTIGATOR_CONTEXT_MAX_BYTES", "1"),
        ("ISSUE_INVESTIGATOR_CONTEXT_MAX_BYTES_PER_SOURCE", "1"),
        ("ISSUE_INVESTIGATOR_CONTEXT_EVAL", "on"),
    ):
        monkeypatch.setenv(name, value)
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    records = _all_records()
    for case_id, by_strategy in records.items():
        for strategy, record in by_strategy.items():
            assert record.metrics.to_dict() == baseline[case_id][strategy]


def test_every_case_runs_under_both_strategies():
    cases = _load_cases()
    records = _all_records()
    pairs = sum(len(by_strategy) for by_strategy in records.values())
    assert pairs == len(cases) * 2
    for case in cases:
        assert set(records[case["case_id"]]) == {ca.STRATEGY_NAME, ca.RANKED_STRATEGY_NAME}


def test_harness_uses_the_real_run_pipeline(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("run_pipeline must not be bypassed")

    monkeypatch.setattr(ca, "run_pipeline", _boom)
    for case in _load_cases():
        with pytest.raises(RuntimeError):
            _seal_case(case, ca.STRATEGY_NAME)


def test_stale_rate_counts_both_dropped_and_demoted_shapes():
    """T6: `stale_rate` is computed "over selected sources" (ADR 015 sec. 2).
    The default strategy DROPS its stale source (excluded from `selected`),
    so it never counts and `stale_rate` is `0.0` — dropped content was never
    delivered to the model. The ranked strategy DEMOTES rather than drops, so
    the still-selected `reason_code == "stale-demoted"` source does count and
    `stale_rate > 0`. Counting the dropped source too (over all sources
    rather than selected ones) would hide exactly this distinction."""
    case = next(c for c in _load_cases() if c["case_id"] == "02-stale-source")
    default_snapshot, _, _ = _seal_case(case, ca.STRATEGY_NAME)
    ranked_snapshot, _, _ = _seal_case(case, ca.RANKED_STRATEGY_NAME)

    stale_source = next(s for s in default_snapshot.sources if s.source_id == "issue-comment-old")
    assert stale_source.selection.included is False
    assert stale_source.selection.reason_code == "stale"
    assert stale_source.freshness.staleness == "stale"

    demoted_source = next(s for s in ranked_snapshot.sources if s.source_id == "issue-comment-old")
    assert demoted_source.selection.included is True
    assert demoted_source.selection.reason_code == "stale-demoted"

    default_metrics = ce.evaluate(default_snapshot, observed_at=default_snapshot.created_at).metrics
    ranked_metrics = ce.evaluate(ranked_snapshot, observed_at=ranked_snapshot.created_at).metrics
    assert default_metrics.stale_rate == 0.0
    assert ranked_metrics.stale_rate > 0


def test_conflict_metrics_come_from_snapshot_conflicts_only():
    """T7: a case whose ranked run populates conflicts and whose default run
    does not."""
    case = next(c for c in _load_cases() if c["case_id"] == "05-conflicting-prior-proposal")
    default_snapshot, _, _ = _seal_case(case, ca.STRATEGY_NAME)
    ranked_snapshot, _, _ = _seal_case(case, ca.RANKED_STRATEGY_NAME)
    assert default_snapshot.conflicts == ()
    assert len(ranked_snapshot.conflicts) > 0

    default_metrics = ce.evaluate(default_snapshot, observed_at=default_snapshot.created_at).metrics
    ranked_metrics = ce.evaluate(ranked_snapshot, observed_at=ranked_snapshot.created_at).metrics
    assert default_metrics.conflicts_detected == 0
    assert ranked_metrics.conflicts_detected > 0

    labels = _labels_from(case["labels"])
    ranked_with_labels = ce.evaluate(ranked_snapshot, observed_at=ranked_snapshot.created_at, labels=labels).metrics
    assert ranked_with_labels.conflicts_expected_detected == len(labels.expected_conflict_source_ids)


def test_labelled_case_yields_expected_ratios_and_unlabelled_case_yields_null():
    """T5."""
    labelled_case = next(c for c in _load_cases() if c["case_id"] == "01-all-fresh-labelled")
    snapshot, _, _ = _seal_case(labelled_case, ca.STRATEGY_NAME)
    labels = _labels_from(labelled_case["labels"])
    metrics = ce.evaluate(snapshot, observed_at=snapshot.created_at, labels=labels).metrics
    # 4 sources selected (inline-template, issue, target-repo, issue-comment-1);
    # only "issue" and "issue-comment-1" are declared useful, so precision is
    # 2/4 while recall is 2/2 — both declared-useful ids were selected.
    assert metrics.selected_precision == pytest.approx(0.5)
    assert metrics.useful_recall == 1.0
    assert metrics.f1 == pytest.approx(2 / 3)
    assert metrics.missing_expected == ()
    assert metrics.noise_rate == 0.0

    unlabelled_case = next(c for c in _load_cases() if c["case_id"] == "07-unlabelled")
    assert unlabelled_case["labels"] is None
    unlabelled_snapshot, _, _ = _seal_case(unlabelled_case, ca.STRATEGY_NAME)
    unlabelled_metrics = ce.evaluate(unlabelled_snapshot, observed_at=unlabelled_snapshot.created_at).metrics
    assert unlabelled_metrics.selected_precision is None
    assert unlabelled_metrics.useful_recall is None
    assert unlabelled_metrics.f1 is None


# ---------------------------------------------------------------------------
# Identity (T1, T2, T3 for the evaluator side; T10, T11 pinning)
# ---------------------------------------------------------------------------


def _make_source(
    source_id: str = "s1", kind: str = "github-issue", included: bool = True,
    reason_code: str = "primary-problem-statement", staleness: str = "fresh", byte_count: int = 10, rank: int = 1,
) -> cs.ContextSource:
    return cs.ContextSource(
        source_id=source_id, kind=kind, locator="https://example.invalid/x", selector={},
        content_hash=cs.hash_bytes(b"x" * byte_count), byte_count=byte_count, retrieved_at=CREATED_AT,
        freshness=cs.Freshness(observed_at=CREATED_AT, staleness=staleness, max_age_seconds=3600),
        trust=cs.Trust(tier="untrusted", rationale_code="test"),
        selection=cs.Selection(rank=rank, reason_code=reason_code, included=included),
    )


def _seal(sources: tuple[cs.ContextSource, ...], conflicts: tuple[cs.ContextConflict, ...] = ()) -> cs.ContextSnapshot:
    included = [s for s in sources if s.selection.included]
    budget = cs.ContextBudget(
        max_sources=12, max_bytes=120000, max_bytes_per_source=50000,
        used_sources=len(included), used_bytes=sum(s.byte_count for s in included),
    )
    return cs.seal(
        execution=_execution(), strategy=cs.ContextStrategy(name=ca.STRATEGY_NAME, version=ca.STRATEGY_VERSION),
        budget=budget, retention=cs.RetentionPolicy(class_="execution-record", expires_after_days=180),
        created_at=CREATED_AT, sources=sources, conflicts=conflicts,
    )


def test_document_identity_verifies_for_a_sealed_snapshot():
    snapshot = _seal((_make_source(),))
    check = ce.verify_identity(snapshot)
    assert check.ok
    assert check.mismatch_fields == ()


def test_tampered_content_hash_and_snapshot_id_are_both_named():
    snapshot = _seal((_make_source(),))
    tampered_hash = replace(snapshot, content_hash="sha256:" + "0" * 64)
    check = ce.verify_identity(tampered_hash)
    assert not check.ok
    assert set(check.mismatch_fields) == {"content_hash", "snapshot_id"}

    tampered_id = replace(snapshot, snapshot_id="cs-" + "0" * 16)
    check_id = ce.verify_identity(tampered_id)
    assert not check_id.ok
    assert check_id.mismatch_fields == ("snapshot_id",)


def test_hash_mismatch_record_has_no_metrics():
    snapshot = _seal((_make_source(),))
    tampered = replace(snapshot, content_hash="sha256:" + "0" * 64)
    record = ce.evaluate(tampered, observed_at=CREATED_AT)
    assert record.verdict == ce.VERDICT_HASH_MISMATCH
    assert record.metrics is None
    assert set(record.mismatch_fields) == {"content_hash", "snapshot_id"}


def test_store_identity_verifies_and_mismatches():
    snapshot = _seal((_make_source(),))
    store_hash = ce.hash_bytes(ce.canonical_json(snapshot.to_dict()))
    good_ref = wc_snapshots.StoreRef(
        work_item_id="wi_1", execution_id="we_1", store_snapshot_id="cs_arbitrary", store_content_hash=store_hash
    )
    assert ce.verify_identity(snapshot, good_ref).ok

    bad_ref = replace(good_ref, store_content_hash="sha256:" + "9" * 64)
    check = ce.verify_identity(snapshot, bad_ref)
    assert not check.ok
    assert "store_content_hash" in check.mismatch_fields


def test_no_store_ref_still_verifies_document_identity_and_proceeds_to_metrics():
    snapshot = _seal((_make_source(),))
    record = ce.evaluate(snapshot, observed_at=CREATED_AT, store_ref=None)
    assert record.verdict == ce.VERDICT_EVALUATED
    assert record.store_ref is None
    assert record.metrics is not None


def test_safe_source_id_pattern_is_pinned_to_context_assembly():
    assert ce.SAFE_SOURCE_ID.pattern == ca._SAFE_SOURCE_ID.pattern


def test_store_hash_is_pinned_to_work_context_snapshots_canonical_bytes():
    snapshot = _seal((_make_source(),))
    assert ce.hash_bytes(ce.canonical_json(snapshot.to_dict())) == ce.hash_bytes(wc_snapshots.canonical_bytes(snapshot))


# ---------------------------------------------------------------------------
# from_dict round-trip (T1, mctlhq/mctl-agents#528)
# ---------------------------------------------------------------------------


def test_eval_record_from_dict_round_trips_a_full_record():
    snapshot = _seal((_make_source(),))
    store_ref = wc_snapshots.StoreRef(
        work_item_id="wi_1", execution_id="we_1", store_snapshot_id="cs_x",
        store_content_hash=ce.hash_bytes(ce.canonical_json(snapshot.to_dict())),
    )
    outcome = ce.OutcomeLink(
        outcome="succeeded", outcome_source=ce.OUTCOME_SOURCE_LEDGER,
        work_item_state="completed", execution_phase="Succeeded", reason_code="",
    )
    record = ce.evaluate(
        snapshot, observed_at=CREATED_AT, store_ref=store_ref, outcome=outcome,
        labels=ce.CaseLabels(useful_source_ids=("s1",)),
        assembly=ce.AssemblyCounters(candidates_total=1),
    )
    assert ce.EvalRecord.from_dict(record.to_log_dict()) == record


def test_eval_record_from_dict_round_trips_a_minimal_record():
    snapshot = _seal((_make_source(),))
    record = ce.evaluate(snapshot, observed_at=CREATED_AT)
    assert record.store_ref is None
    assert record.metrics is not None
    assert record.outcome is None
    assert ce.EvalRecord.from_dict(record.to_log_dict()) == record


def test_eval_record_from_dict_round_trips_a_hash_mismatch_record_with_no_metrics():
    snapshot = _seal((_make_source(),))
    tampered = replace(snapshot, content_hash="sha256:" + "0" * 64)
    record = ce.evaluate(tampered, observed_at=CREATED_AT)
    assert record.metrics is None
    assert ce.EvalRecord.from_dict(record.to_log_dict()) == record


def test_eval_record_from_dict_rejects_a_foreign_record_kind():
    snapshot = _seal((_make_source(),))
    payload = ce.evaluate(snapshot, observed_at=CREATED_AT).to_log_dict()
    payload["record_kind"] = "something-else"
    with pytest.raises(ValueError):
        ce.EvalRecord.from_dict(payload)


def test_eval_record_from_dict_rejects_an_unknown_verdict():
    snapshot = _seal((_make_source(),))
    payload = ce.evaluate(snapshot, observed_at=CREATED_AT).to_log_dict()
    payload["verdict"] = "not-a-real-verdict"
    with pytest.raises(ValueError):
        ce.EvalRecord.from_dict(payload)


# ---------------------------------------------------------------------------
# execution-observed provenance (T1b/T17, mctlhq/mctl-agents#528)
# ---------------------------------------------------------------------------


def test_observe_candidate_record_never_carries_a_store_ref():
    snapshot = _seal((_make_source(),))
    with pytest.raises(ValueError):
        ce.evaluate(
            snapshot, observed_at=CREATED_AT, evidence_kind="observe-candidate",
            store_ref=_store_ref_for_eval("we_1"),
        )


def test_execution_ref_refused_outside_observe_candidate():
    snapshot = _seal((_make_source(),))
    execution_ref = ce.ExecutionRef(work_item_id="wi_9", execution_id="we_9")
    with pytest.raises(ValueError):
        ce.evaluate(snapshot, observed_at=CREATED_AT, evidence_kind="live", execution_ref=execution_ref)


def test_observe_candidate_record_carries_its_own_execution_ref_and_verifies_on_document_identity_only():
    snapshot = _seal((_make_source(),))
    execution_ref = ce.ExecutionRef(work_item_id="wi_9", execution_id="we_9")
    record = ce.evaluate(
        snapshot, observed_at=CREATED_AT, evidence_kind="observe-candidate", execution_ref=execution_ref,
    )
    assert record.execution_ref == execution_ref
    assert record.store_ref is None
    check = ce.verify_identity(snapshot, record.store_ref)
    assert check.store_ok is None
    assert ce.EvalRecord.from_dict(record.to_log_dict()) == record


def _store_ref_for_eval(execution_id: str) -> wc_snapshots.StoreRef:
    return wc_snapshots.StoreRef(
        work_item_id="wi_1", execution_id=execution_id, store_snapshot_id="cs_" + execution_id,
        store_content_hash="sha256:" + "a" * 64,
    )


# ---------------------------------------------------------------------------
# Telemetry safety (T8, T9)
# ---------------------------------------------------------------------------


def test_no_locator_selector_or_payload_ever_reaches_the_log_dict():
    locator_marker = "LOCATOR-MARKER-zzz999"
    payload_marker = "PAYLOAD-MARKER-yyy888"
    source = replace(_make_source(), locator=f"https://example.invalid/{locator_marker}")
    snapshot = _seal((source,))
    record = ce.evaluate(snapshot, observed_at=CREATED_AT)
    dumped = json.dumps(record.to_log_dict())
    assert locator_marker not in dumped
    assert payload_marker not in dumped
    assert "selector" not in dumped


def test_unsafe_source_id_is_replaced_by_kind_in_missing_expected():
    unsafe_id = "bad id/with a slash"
    source = _make_source(
        source_id=unsafe_id, kind="github-issue-comment", included=False, reason_code="budget-exhausted"
    )
    other = _make_source(source_id="s2", included=True, rank=2)
    snapshot = _seal((source, other))
    labels = ce.CaseLabels(useful_source_ids=(unsafe_id,))
    metrics = ce.evaluate(snapshot, observed_at=CREATED_AT, labels=labels).metrics
    assert metrics.missing_expected == ("github-issue-comment",)
    assert unsafe_id not in json.dumps(metrics.to_dict())


def test_unsafe_source_id_with_no_matching_source_falls_back_to_unknown():
    snapshot = _seal((_make_source(),))
    labels = ce.CaseLabels(useful_source_ids=("bad id/never seen",))
    metrics = ce.evaluate(snapshot, observed_at=CREATED_AT, labels=labels).metrics
    assert metrics.missing_expected == ("unknown",)


def test_record_always_names_itself():
    snapshot = _seal((_make_source(),))
    record = ce.evaluate(snapshot, observed_at=CREATED_AT)
    log = record.to_log_dict()
    assert log["record_kind"] == "context-eval"
    assert log["evaluator_name"] == ce.EVALUATOR_NAME
    assert log["evaluator_version"] == ce.EVALUATOR_VERSION


# ---------------------------------------------------------------------------
# Outcome linking (T12, T13, T14)
# ---------------------------------------------------------------------------


def test_outcome_mapping_is_total_over_work_item_states():
    for state in wc_contract.WORK_ITEM_STATES:
        link = ce.link_outcome(work_item_state=state, execution_phase="Succeeded")
        assert link.outcome in ce.OUTCOMES
        if link.outcome == "unknown":
            assert link.reason_code


def test_outcome_vocabularies_are_pinned_to_work_context_contract():
    assert ce.WORK_ITEM_STATES == wc_contract.WORK_ITEM_STATES


def test_unrecognised_work_item_state_is_unknown_with_a_reason():
    """ADR 015 sec. 3: `reason_code` is a closed-vocabulary code, never
    interpolated external text — the unrecognised value itself must not
    appear in it (`work_item_state` still carries the raw value in its own
    dedicated field)."""
    link = ce.link_outcome(work_item_state="quantum-superposed", execution_phase="Succeeded")
    assert link.outcome == "unknown"
    assert link.reason_code == "unrecognised-work-item-state"
    assert "quantum-superposed" not in link.reason_code
    assert link.work_item_state == "quantum-superposed"


def test_status_yaml_fallback_used_only_without_a_store_execution():
    link = ce.link_outcome(status_yaml_status="merged", store_execution=False)
    assert (link.outcome, link.outcome_source) == ("succeeded", ce.OUTCOME_SOURCE_STATUS_YAML)
    ledger_link = ce.link_outcome(work_item_state="completed", execution_phase="Succeeded", store_execution=True)
    assert ledger_link.outcome_source == ce.OUTCOME_SOURCE_LEDGER


def test_live_record_carries_null_outcome_and_its_join_keys():
    snapshot = _seal((_make_source(),), )
    work_context = cs.WorkContextRef(
        work_item_id="wi_1", work_item_revision="1", execution_id="we_1", execution_sequence=1
    )
    live_snapshot = replace(snapshot, work_context=work_context)
    record = ce.evaluate(live_snapshot, observed_at=CREATED_AT, outcome=None, evidence_kind="live")
    assert record.outcome is None
    assert record.context_snapshot_id == live_snapshot.snapshot_id


# ---------------------------------------------------------------------------
# Freshness / promotion readiness (T15, T16)
# ---------------------------------------------------------------------------


def _identity(**overrides: Any) -> ce.EvidenceIdentity:
    base = dict(
        strategy_name="deterministic-fixed-order", strategy_version="1.0.0", ranker_name=None, ranker_version=None,
        strategy_content_hash="sha256:" + "a" * 64, strategy_implementation_hash="sha256:" + "b" * 64,
        evaluator_name=ce.EVALUATOR_NAME, evaluator_version=ce.EVALUATOR_VERSION,
        metrics_contract_version=ce.METRICS_CONTRACT_VERSION,
    )
    base.update(overrides)
    return ce.EvidenceIdentity(**base)


def _record(identity: ce.EvidenceIdentity, *, observed_at: str, verdict: str = ce.VERDICT_EVALUATED) -> ce.EvalRecord:
    return ce.EvalRecord(
        record_kind=ce.RECORD_KIND, evaluator_name=ce.EVALUATOR_NAME, evaluator_version=ce.EVALUATOR_VERSION,
        verdict=verdict, identity=identity, evidence_kind="stored-replay", context_snapshot_id="cs-x",
        content_hash="sha256:" + "c" * 64, store_ref=None, metrics=None, outcome=None, observed_at=observed_at,
    )


def _store_ref(execution_id: str) -> wc_snapshots.StoreRef:
    return wc_snapshots.StoreRef(
        work_item_id="wi_1", execution_id=execution_id, store_snapshot_id="cs_" + execution_id,
        store_content_hash="sha256:" + "a" * 64,
    )


def test_assess_evidence_precedence():
    expected = _identity()
    policy = ce.FreshnessPolicy(window_seconds=ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS, min_consecutive_observations=3)
    now = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)

    missing = ce.assess_evidence([], expected=expected, now=now, policy=policy)
    assert missing.status == "missing"

    none_kind = _record(expected, observed_at="2026-09-26T00:00:00Z")
    none_kind = replace(none_kind, evidence_kind="none")
    assert ce.assess_evidence([none_kind], expected=expected, now=now, policy=policy).status == "missing"

    differing_version = _record(_identity(strategy_version="9.9.9"), observed_at="2026-09-26T00:00:00Z")
    result = ce.assess_evidence([differing_version], expected=expected, now=now, policy=policy)
    assert result.status == "mismatched" and result.reason_code == "declared-identity-mismatch"

    differing_impl = _record(
        _identity(strategy_implementation_hash="sha256:" + "9" * 64), observed_at="2026-09-26T00:00:00Z"
    )
    result = ce.assess_evidence([differing_impl], expected=expected, now=now, policy=policy)
    assert result.status == "mismatched" and result.reason_code == "catalog-identity-mismatch"

    empty_catalog = _record(
        _identity(strategy_content_hash="", strategy_implementation_hash=""), observed_at="2026-09-26T00:00:00Z"
    )
    empty_expected = _identity(strategy_content_hash="", strategy_implementation_hash="")
    result = ce.assess_evidence([empty_catalog], expected=empty_expected, now=now, policy=policy)
    assert result.status == "mismatched" and result.reason_code == "catalog-identity-unavailable"

    # A differing pipeline_source_hash alone never gates: still fresh.
    three_agreeing = [
        replace(
            _record(_identity(pipeline_source_hash="sha256:" + str(i) * 64), observed_at=iso),
            store_ref=_store_ref(f"we_{i}"),
        )
        for i, iso in enumerate(("2026-09-26T00:00:00Z", "2026-09-25T00:00:00Z", "2026-09-24T00:00:00Z"))
    ]
    fresh = ce.assess_evidence(three_agreeing, expected=expected, now=now, policy=policy)
    assert fresh.status == "fresh" and fresh.reason_code == "ok"

    stale_record = replace(_record(expected, observed_at="2026-01-01T00:00:00Z"), store_ref=_store_ref("we_old"))
    stale = ce.assess_evidence([stale_record], expected=expected, now=now, policy=policy)
    assert stale.status == "stale"

    two_only = three_agreeing[:2]
    insufficient = ce.assess_evidence(two_only, expected=expected, now=now, policy=policy)
    assert insufficient.status == "insufficient-observations"

    assert ce.assess_evidence(three_agreeing, expected=expected, now=now, policy=policy).status != "missing"
    # `fresh` is never reachable for evidence_kind == "none".
    none_only = [replace(r, evidence_kind="none") for r in three_agreeing]
    assert ce.assess_evidence(none_only, expected=expected, now=now, policy=policy).status == "missing"


def test_assess_evidence_never_counts_a_record_without_store_backing():
    """ADR 015 sec. 7 step 5: a record with `store_ref: null` (the live
    emitter below the work-context `observe` stage, or a non-`we_`
    execution) is not a promotion observation. The retries of one such
    execution restamp every local identity field, so three of them must not
    reach `fresh`; neither may one record duplicated three times."""
    expected = _identity()
    policy = ce.FreshnessPolicy(window_seconds=ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS, min_consecutive_observations=3)
    now = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)

    base = _record(expected, observed_at="2026-09-26T00:00:03Z")
    restamped_retries = [
        replace(base, observed_at=f"2026-09-26T00:00:0{i}Z", context_snapshot_id=f"cs-{i}",
                content_hash="sha256:" + str(i) * 64)
        for i in (3, 2, 1)
    ]
    result = ce.assess_evidence(restamped_retries, expected=expected, now=now, policy=policy)
    assert result.status == "insufficient-observations" and result.observations == 0

    result = ce.assess_evidence([base, base, base], expected=expected, now=now, policy=policy)
    assert result.status == "insufficient-observations" and result.observations == 0

    # Unbacked records between store-backed ones neither count nor break the run.
    mixed = [replace(base, store_ref=_store_ref("we_a")), restamped_retries[1],
             replace(base, observed_at="2026-09-25T00:00:00Z", store_ref=_store_ref("we_b")),
             replace(base, observed_at="2026-09-24T00:00:00Z", store_ref=_store_ref("we_c"))]
    result = ce.assess_evidence(mixed, expected=expected, now=now, policy=policy)
    assert result.status == "fresh" and result.observations == 3


def test_assess_evidence_measures_freshness_from_the_newest_observation_not_an_unbacked_record():
    """ADR 015 sec. 7 step 4: the window is measured from the newest
    store-backed observation. Three observations older than the window plus
    one unbacked record from today is `stale`, not `fresh`; a pool with no
    store-backed record at all is `insufficient-observations`."""
    expected = _identity()
    policy = ce.FreshnessPolicy(window_seconds=ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS, min_consecutive_observations=3)
    now = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)

    today_unbacked = _record(expected, observed_at="2026-09-26T23:00:00Z")
    old_backed = [
        replace(_record(expected, observed_at=f"2026-09-0{i}T00:00:00Z"), store_ref=_store_ref(f"we_{i}"))
        for i in (3, 2, 1)
    ]
    result = ce.assess_evidence([today_unbacked, *old_backed], expected=expected, now=now, policy=policy)
    assert result.status == "stale" and result.reason_code == "observation-older-than-window"
    assert result.newest_age_seconds == int((now - datetime(2026, 9, 3, tzinfo=UTC)).total_seconds())

    result = ce.assess_evidence([today_unbacked], expected=expected, now=now, policy=policy)
    assert result.status == "insufficient-observations" and result.reason_code == "no-store-backed-observation"


def test_assess_evidence_counts_store_executions_not_retry_attempts():
    """mctlhq/mctl-agents#526, ADR 015 sec. 7 step 5: the retries of one store
    execution restamp `created_at` (so `observed_at`), `content_hash` and the
    local snapshot id, yet the store calls them one execution. Three such
    records are ONE observation; three executions are three. An
    `evidence_kind: none` record ends the run, and `observations` reports the
    deduplicated count."""
    expected = _identity()
    policy = ce.FreshnessPolicy(window_seconds=ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS, min_consecutive_observations=3)
    now = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)
    isos = ("2026-09-26T00:00:03Z", "2026-09-26T00:00:02Z", "2026-09-26T00:00:01Z")

    ref = _store_ref

    base = _record(expected, observed_at=isos[0])
    retries = [
        replace(base, observed_at=iso, context_snapshot_id=f"cs-{i}", content_hash="sha256:" + str(i) * 64,
                store_ref=ref("we_1"))
        for i, iso in enumerate(isos)
    ]
    result = ce.assess_evidence(retries, expected=expected, now=now, policy=policy)
    assert result.status == "insufficient-observations" and result.observations == 1

    executions = [replace(r, store_ref=ref(f"we_{i}")) for i, r in enumerate(retries)]
    result = ce.assess_evidence(executions, expected=expected, now=now, policy=policy)
    assert result.status == "fresh" and result.observations == 3

    none_in_run = [executions[0], executions[1], replace(executions[2], evidence_kind="none")]
    result = ce.assess_evidence(none_in_run, expected=expected, now=now, policy=policy)
    assert result.status == "insufficient-observations" and result.observations == 2


def test_assess_evidence_counts_observe_candidate_observations_by_execution_ref():
    """mctlhq/mctl-agents#528: an `observe-candidate` record has no
    `store_ref` (it is never persisted) but still counts as one observation
    per `execution_ref.(work_item_id, execution_id)`, exactly like a stored
    observation."""
    expected = _identity()
    policy = ce.FreshnessPolicy(window_seconds=ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS, min_consecutive_observations=3)
    now = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)
    isos = ("2026-09-26T00:00:00Z", "2026-09-25T00:00:00Z", "2026-09-24T00:00:00Z")

    records = [
        replace(
            _record(expected, observed_at=iso), evidence_kind="observe-candidate",
            execution_ref=ce.ExecutionRef(work_item_id="wi_1", execution_id=f"we_{i}"),
        )
        for i, iso in enumerate(isos)
    ]
    result = ce.assess_evidence(records, expected=expected, now=now, policy=policy)
    assert result.status == "fresh" and result.observations == 3

    # Retries of one execution still count once.
    retries = [
        replace(records[0], observed_at=iso, context_snapshot_id=f"cs-{i}", content_hash="sha256:" + str(i) * 64)
        for i, iso in enumerate(isos)
    ]
    result = ce.assess_evidence(retries, expected=expected, now=now, policy=policy)
    assert result.status == "insufficient-observations" and result.observations == 1


def test_assess_evidence_malformed_observed_at_is_stale_not_a_crash():
    """A malformed `observed_at` string (not ISO-8601 at all) cannot be
    parsed for a freshness comparison; `assess_evidence` must fail closed
    (`status: "stale"`) rather than raise `ValueError`."""
    expected = _identity()
    policy = ce.FreshnessPolicy(window_seconds=ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS, min_consecutive_observations=1)
    now = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)

    malformed = replace(_record(expected, observed_at="not-a-timestamp"), store_ref=_store_ref("we_1"))
    result = ce.assess_evidence([malformed], expected=expected, now=now, policy=policy)
    assert result.status == "stale"
    assert result.reason_code == "observed-at-unparseable"
    assert result.newest_age_seconds is None


def test_assess_evidence_timezone_naive_observed_at_is_stale_not_a_crash():
    """A timezone-naive `observed_at` (no `Z` and no UTC offset) cannot be
    subtracted from the tz-aware `now` `assess_evidence` receives;
    `datetime.fromisoformat` alone would raise `TypeError`, so this must fail
    closed (`status: "stale"`) instead."""
    expected = _identity()
    policy = ce.FreshnessPolicy(window_seconds=ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS, min_consecutive_observations=1)
    now = datetime(2026, 9, 27, 0, 0, 0, tzinfo=UTC)

    naive = replace(_record(expected, observed_at="2026-09-26T00:00:00"), store_ref=_store_ref("we_1"))
    result = ce.assess_evidence([naive], expected=expected, now=now, policy=policy)
    assert result.status == "stale"
    assert result.reason_code == "observed-at-unparseable"
    assert result.newest_age_seconds is None


def test_freshness_constants_pinned_and_policy_rejects_non_positive():
    assert ce.ADR019_V1_FRESHNESS_WINDOW_SECONDS == 604800
    assert ce.ADR019_V1_MIN_CONSECUTIVE_OBSERVATIONS == 3
    with pytest.raises(ValueError):
        ce.FreshnessPolicy(window_seconds=0, min_consecutive_observations=3)
    with pytest.raises(ValueError):
        ce.FreshnessPolicy(window_seconds=604800, min_consecutive_observations=0)
    source = Path(ce.__file__).read_text(encoding="utf-8")
    assert "CONTEXT_EVAL_" not in source


def test_adr_019_states_the_same_constants():
    adr = (REPO_ROOT / "docs" / "adr" / "019-context-strategy-release-contract.md").read_text(encoding="utf-8")
    assert "7 days" in adr
    assert "3 consecutive" in adr


# ---------------------------------------------------------------------------
# Purity and non-authorization (T27, T28)
# ---------------------------------------------------------------------------


def test_context_eval_module_is_pure():
    source = Path(ce.__file__).read_text(encoding="utf-8")
    assert "os.getenv(" not in source
    assert "os.environ[" not in source and "os.environ.get(" not in source
    assert not re.search(r"^\s*import os\b", source, re.MULTILINE)
    for token in ("import urllib", "import httpx", "import subprocess", "import yaml", "yaml.safe_load"):
        assert token not in source, token
    assert not re.search(r"^\s*(import|from)\s+orchestrator\.context_release\b", source, re.MULTILINE)
    assert not re.search(r"^\s*(import|from)\s+orchestrator\.context_assembly\b", source, re.MULTILINE)
    assert "datetime.now(" not in source


def test_only_investigator_and_replay_cli_import_context_eval():
    """mctlhq/mctl-agents#528 adds one legitimate fourth caller:
    `orchestrator/context_release.py` imports `context_eval` inside
    `assess_production_evidence`'s function body only (never at module
    scope — `test_context_release_never_imports_context_eval_at_module_scope`
    pins that), the same direction `context_release` already imports
    `context_snapshot`."""
    orchestrator_dir = REPO_ROOT / "orchestrator"
    allowed = {"run_issue_investigator.py", "run_context_eval.py", "context_eval.py", "context_release.py"}
    offenders = []
    for path in sorted(orchestrator_dir.rglob("*.py")):
        if path.name in allowed:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "import" in stripped and re.search(r"\bcontext_eval\b", stripped):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {stripped}")
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# Replay CLI (T23-T26, T29)
# ---------------------------------------------------------------------------


class _FakeWorkItemClient:
    """A GET-only fake: any other verb fails the test outright (T24)."""

    def __init__(self, item: wc_contract.WorkItem, snapshots: dict[str, dict[str, Any]]) -> None:
        self._item = item
        self._snapshots = snapshots
        self.calls: list[str] = []

    def get(self, work_item_id: str) -> wc_contract.WorkItemAnswer:
        self.calls.append("GET get_work_item")
        if work_item_id != self._item.work_item_id:
            return wc_contract.WorkItemAnswer(verdict=wc_contract.WORK_ITEM_ABSENT, reason="no record")
        return wc_contract.WorkItemAnswer(verdict=wc_contract.WORK_ITEM_FOUND, item=self._item, accepted=True)

    def execution_snapshot(self, work_item_id: str, execution_id: str) -> wc_snapshots.SnapshotAnswer:
        self.calls.append("GET execution_snapshot")
        doc = self._snapshots.get(execution_id)
        if doc is None:
            return wc_snapshots.SnapshotAnswer(wc_snapshots.SNAPSHOT_ABSENT, reason="no snapshot sealed")
        # The real store hash: sha256 of the canonical JSON of the WHOLE
        # document (`work_context.snapshots.canonical_bytes`'s convention),
        # so `verify_identity`'s store check genuinely holds for this fake.
        content_hash = cs.hash_bytes(cs.canonical_json(doc)) if isinstance(doc, dict) else "sha256:" + "0" * 64
        return wc_snapshots.SnapshotAnswer(
            wc_snapshots.SNAPSHOT_REPLAYED, snapshot_id="cs_stored", content_hash=content_hash, stored_document=doc,
        )

    def __getattr__(self, name: str) -> Any:  # any write-shaped call fails the test
        raise AssertionError(f"unexpected non-GET call: {name}")


def _replay_snapshot() -> cs.ContextSnapshot:
    return _seal((_make_source(),))


def _replay_work_item(
    *, execution_id: str = "we_1", phase: str = "Succeeded", state: str = "completed"
) -> wc_contract.WorkItem:
    execution = wc_contract.ExecutionRef(execution_id=execution_id, sequence=1, phase=phase)
    return wc_contract.WorkItem(work_item_id="wi_1", revision="1", state=state, executions=(execution,))


def test_replay_cli_happy_path_selects_newest_snapshot_carrying_execution():
    snapshot = _replay_snapshot()
    item = _replay_work_item()
    client = _FakeWorkItemClient(item, {"we_1": snapshot.to_dict()})
    payload = rce.replay(client, "wi_1")
    assert payload["evidence_kind"] == "stored-replay"
    assert payload["metrics"]["capability_calls"] == 0
    assert payload["replay_store_reads"] == 2


def test_replay_cli_is_read_only():
    snapshot = _replay_snapshot()
    item = _replay_work_item()
    client = _FakeWorkItemClient(item, {"we_1": snapshot.to_dict()})
    rce.replay(client, "wi_1")
    with pytest.raises(AssertionError):
        client.seal_snapshot("wi_1", "we_1", {})


def test_replay_cli_failure_paths():
    empty_item = wc_contract.WorkItem(work_item_id="wi_1", revision="1", state="active", executions=())
    client = _FakeWorkItemClient(empty_item, {})
    with pytest.raises(rce.ReplayError):
        rce.replay(client, "wi_999")  # absent work item
    with pytest.raises(rce.ReplayError):
        rce.replay(client, "wi_1")  # no execution carries a snapshot

    item = _replay_work_item()
    client_with_bad_doc = _FakeWorkItemClient(item, {"we_1": {"not": "a snapshot"}})
    with pytest.raises(rce.ReplayError) as excinfo:
        rce.replay(client_with_bad_doc, "wi_1")
    assert "not a snapshot" not in str(excinfo.value)


def test_replay_cli_rejects_a_non_we_prefixed_execution_argument():
    item = _replay_work_item()
    client = _FakeWorkItemClient(item, {"we_1": _replay_snapshot().to_dict()})
    with pytest.raises(rce.ReplayError):
        rce.replay(client, "wi_1", execution_id="not-we-prefixed")


def test_replay_cli_catalog_identity_is_populated_and_empty_on_a_bad_catalog(tmp_path):
    from orchestrator import context_release

    snapshot = _replay_snapshot()  # sealed under STRATEGY_NAME/1.0.0
    item = _replay_work_item()
    client = _FakeWorkItemClient(item, {"we_1": snapshot.to_dict()})
    payload = rce.replay(client, "wi_1")
    assert payload["identity"]["strategy_content_hash"]
    assert payload["identity"]["strategy_implementation_hash"]

    def _boom(*args, **kwargs):
        raise context_release.ContextReleaseError(context_release.VERDICT_HASH_MISMATCH, "tampered")

    import orchestrator.context_release as real_module

    original = real_module.load_version
    real_module.load_version = _boom  # type: ignore[assignment]
    try:
        payload2 = rce.replay(client, "wi_1")
    finally:
        real_module.load_version = original  # type: ignore[assignment]
    assert payload2["identity"]["strategy_content_hash"] == ""
    assert payload2["identity"]["strategy_implementation_hash"] == ""
