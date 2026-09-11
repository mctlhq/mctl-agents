"""Tests for orchestrator/context_snapshot.py — mctlhq/mctl-agents#264's
`ContextSnapshot` contract (ADR 009:
docs/adr/009-context-snapshot-contract.md). T1-T9 below map onto that
proposal's tasks.md "## Tests" section; see context_snapshot.py's module
docstring for the fail-closed contract every negative test here asserts.

Naming convention (`# T<n> —` section banners) matches
tests/test_resolver.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

from orchestrator import context_snapshot as cs

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "context" / "investigator-snapshot.json"

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _execution(**overrides) -> cs.ExecutionCorrelation:
    fields = {
        "agent": "issue-investigator",
        "environment": "production",
        "temporal_workflow_id": "dev-loop-mctlhq-mctl-agents-264",
        "target_repository_sha": "a" * 40,
        "definition_version": "3",
        "definition_content_hash": "sha256:" + "1a" * 32,
        "profile_version": "5",
        "profile_content_hash": "sha256:" + "2b" * 32,
        "release_revision": 12,
    }
    fields.update(overrides)
    return cs.ExecutionCorrelation(**fields)


def _strategy(**overrides) -> cs.ContextStrategy:
    fields = {"name": "deterministic-fixed-order", "version": "1.0.0"}
    fields.update(overrides)
    return cs.ContextStrategy(**fields)


def _budget(**overrides) -> cs.ContextBudget:
    # used_sources/used_bytes default to 0/0 (matching zero sources) since
    # validate() now reconciles them against the actual `sources` list
    # whenever truncated=False; callers that attach sources override both.
    fields = {
        "max_sources": 5,
        "max_bytes": 60000,
        "max_bytes_per_source": 50000,
        "used_sources": 0,
        "used_bytes": 0,
        "truncated": False,
    }
    fields.update(overrides)
    return cs.ContextBudget(**fields)


def _retention(**overrides) -> cs.RetentionPolicy:
    fields = {"class_": "execution-record", "expires_after_days": 90}
    fields.update(overrides)
    return cs.RetentionPolicy(**fields)


def _source(**overrides) -> cs.ContextSource:
    fields = dict(
        source_id="s1",
        kind="target-repo",
        locator="git+https://github.com/mctlhq/mctl-agents@" + "a" * 40,
        selector={"mode": "agent-directed"},
        content_hash="sha256:" + "3c" * 32,
        byte_count=100,
        retrieved_at="2026-09-11T00:00:00Z",
        freshness=cs.Freshness(observed_at="2026-09-11T00:00:00Z", staleness="fresh"),
        trust=cs.Trust(tier="authoritative", rationale_code="pinned-target-repository-sha"),
        selection=cs.Selection(rank=1, reason_code="target-repository-tree", included=True),
    )
    fields.update(overrides)
    return cs.ContextSource(**fields)


def _minimal_snapshot(**overrides) -> cs.ContextSnapshot:
    """A minimal, valid, sourceless snapshot — the second T1 fixture."""
    fields = dict(
        execution=_execution(),
        strategy=_strategy(),
        budget=_budget(used_sources=0, used_bytes=0),
        retention=_retention(),
        created_at="2026-09-11T00:00:00Z",
    )
    fields.update(overrides)
    return cs.seal(**fields)


def _load_fixture_snapshot() -> cs.ContextSnapshot:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return cs.ContextSnapshot.from_dict(data)


# ---------------------------------------------------------------------------
# T1 — round-trip: from_dict(to_dict(snapshot)) == snapshot
# ---------------------------------------------------------------------------
def test_round_trip_investigator_fixture():
    snapshot = _load_fixture_snapshot()
    assert cs.ContextSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_round_trip_minimal_snapshot_with_no_sources():
    snapshot = _minimal_snapshot()
    assert snapshot.sources == ()
    assert cs.ContextSnapshot.from_dict(snapshot.to_dict()) == snapshot


# ---------------------------------------------------------------------------
# Selection.score normalization: coerced to float on every construction
# path (not just from_dict), so seal -> serialize -> reload -> verify keeps
# the golden-hash guarantee for a non-null integer score.
# ---------------------------------------------------------------------------
def test_selection_score_int_normalizes_to_float_and_hash_is_stable_after_reload():
    source = _source(
        selection=cs.Selection(rank=1, reason_code="target-repository-tree", included=True, score=1)
    )
    assert source.selection.score == 1.0
    assert isinstance(source.selection.score, float)

    snapshot = _minimal_snapshot(sources=[source], budget=_budget(used_sources=1, used_bytes=100))
    reloaded = cs.ContextSnapshot.from_dict(snapshot.to_dict())
    assert reloaded.content_hash == snapshot.content_hash
    assert cs.recompute_content_hash(reloaded) == snapshot.content_hash


def test_selection_score_rejects_bool():
    with pytest.raises(cs.ContextSnapshotError, match="score"):
        cs.Selection(rank=1, reason_code="x", included=True, score=True)


# ---------------------------------------------------------------------------
# T2 — hash determinism: created_at excluded; any other field change moves
# both content_hash and snapshot_id.
# ---------------------------------------------------------------------------
def test_seal_is_deterministic_across_created_at():
    common = dict(execution=_execution(), strategy=_strategy(), budget=_budget(), retention=_retention())
    snap_one = cs.seal(created_at="2026-01-01T00:00:00Z", **common)
    snap_two = cs.seal(created_at="2099-01-01T00:00:00Z", **common)
    assert snap_one.content_hash == snap_two.content_hash
    assert snap_one.snapshot_id == snap_two.snapshot_id
    assert snap_one.created_at != snap_two.created_at


def test_seal_hash_changes_when_a_non_timestamp_field_changes():
    base = cs.seal(
        execution=_execution(), strategy=_strategy(), budget=_budget(), retention=_retention(),
        created_at="2026-01-01T00:00:00Z",
    )
    changed = cs.seal(
        execution=_execution(agent="implementer"), strategy=_strategy(), budget=_budget(), retention=_retention(),
        created_at="2026-01-01T00:00:00Z",
    )
    assert base.content_hash != changed.content_hash
    assert base.snapshot_id != changed.snapshot_id


def test_snapshot_id_is_derived_from_content_hash():
    snapshot = _minimal_snapshot()
    assert snapshot.snapshot_id == "cs-" + snapshot.content_hash[7:23]
    assert snapshot.content_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# T3 — golden stability: the checked-in fixture's content_hash equals a
# freshly computed one.
# ---------------------------------------------------------------------------
def test_golden_fixture_hash_is_stable():
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    recorded_hash = raw["content_hash"]
    snapshot = cs.ContextSnapshot.from_dict(raw)
    assert cs.recompute_content_hash(snapshot) == recorded_hash
    assert snapshot.snapshot_id == "cs-" + recorded_hash[7:23]


def test_golden_fixture_has_no_free_text_payload_field():
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    for source in raw["sources"]:
        assert set(source) == {
            "source_id", "kind", "locator", "selector", "content_hash", "byte_count",
            "retrieved_at", "freshness", "trust", "selection", "redaction",
        }


# ---------------------------------------------------------------------------
# T4 — fail-loud versioning: unknown api_version/kind, and unknown keys in
# any nested object, are rejected rather than silently ignored.
# ---------------------------------------------------------------------------
def test_from_dict_rejects_unknown_api_version():
    doc = _minimal_snapshot().to_dict()
    doc["api_version"] = "context.mctl.ai/v1alpha2"
    with pytest.raises(cs.ContextSnapshotError, match="api_version"):
        cs.ContextSnapshot.from_dict(doc)


def test_from_dict_rejects_unknown_kind():
    doc = _minimal_snapshot().to_dict()
    doc["kind"] = "ContextBundle"
    with pytest.raises(cs.ContextSnapshotError, match="kind"):
        cs.ContextSnapshot.from_dict(doc)


def test_from_dict_rejects_unknown_top_level_key():
    doc = _minimal_snapshot().to_dict()
    doc["spec"] = {}
    with pytest.raises(cs.ContextSnapshotError, match="unknown key"):
        cs.ContextSnapshot.from_dict(doc)


def test_from_dict_rejects_unknown_nested_key():
    doc = _load_fixture_snapshot().to_dict()
    doc["sources"][0]["payload"] = "not allowed"
    with pytest.raises(cs.ContextSnapshotError, match="unknown key"):
        cs.ContextSnapshot.from_dict(doc)


def test_from_dict_rejects_unknown_key_in_execution_block():
    doc = _minimal_snapshot().to_dict()
    doc["execution"]["extra_field"] = "nope"
    with pytest.raises(cs.ContextSnapshotError, match="unknown key"):
        cs.ContextSnapshot.from_dict(doc)


# ---------------------------------------------------------------------------
# T5 — no-authorization invariant: no allow/deny/permit/grant/authorized
# token anywhere in the schema, and the module is stdlib-only importable.
# ---------------------------------------------------------------------------
_FORBIDDEN_TOKENS = ("allow", "deny", "permit", "grant", "authorized")


def _walk_keys(value):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _walk_keys(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_serialized_schema_has_no_authorization_field_name():
    snapshot = _load_fixture_snapshot()
    keys = set(_walk_keys(snapshot.to_dict()))
    for key in keys:
        lowered = key.lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in lowered, f"field name {key!r} contains forbidden token {token!r}"


def test_module_import_is_stdlib_only():
    """Mirrors tests/test_worker_isolation.py: a subprocess import of
    orchestrator.context_snapshot must not pull in claude_agent_sdk or any
    third-party package, so the long-lived Temporal worker and the agent
    sandbox can both import it safely."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.context_snapshot, sys; "
            "print(chr(10).join(sorted(sys.modules)))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr[-2000:]}"
    loaded = set(result.stdout.split("\n"))
    third_party_prefixes = ("claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio", "mcp")
    leaked = sorted(
        name for name in loaded
        if any(name == prefix or name.startswith(prefix + ".") for prefix in third_party_prefixes)
    )
    assert not leaked, f"orchestrator.context_snapshot pulled in third-party modules: {leaked}"


# ---------------------------------------------------------------------------
# T6 — boundary invariants: evidence_refs accept only {evidence_id, kind};
# the trace-export helper never emits a locator/selector/payload string.
# ---------------------------------------------------------------------------
def test_evidence_ref_rejects_extra_fields():
    with pytest.raises(cs.ContextSnapshotError, match="unknown key"):
        cs.EvidenceRef.from_dict({"evidence_id": "ev-1", "kind": "proposal", "url": "https://example.com"})


def test_evidence_ref_round_trips_id_and_kind_only():
    ref = cs.EvidenceRef.from_dict({"evidence_id": "ev-1", "kind": "proposal"})
    assert ref.to_dict() == {"evidence_id": "ev-1", "kind": "proposal"}


def test_to_log_dict_never_emits_locator_selector_or_payload():
    snapshot = _load_fixture_snapshot()
    log_dict = snapshot.to_log_dict()
    forbidden_keys = {"locator", "selector", "sources", "execution", "evidence_refs", "retention"}
    assert not (set(log_dict) & forbidden_keys)
    serialized = json.dumps(log_dict)
    for source in snapshot.sources:
        assert source.locator not in serialized
        assert json.dumps(dict(source.selector)) not in serialized
    expected_keys = {
        "snapshot_id", "content_hash", "strategy_name", "strategy_version", "ranker_name",
        "ranker_version", "source_count", "included_source_count", "evidence_ref_count",
        "max_sources", "max_bytes", "used_sources", "used_bytes", "truncated",
    }
    assert set(log_dict) == expected_keys


# ---------------------------------------------------------------------------
# T7 — vocabulary closure: freshness.staleness, trust.tier, source.kind and
# retention.class reject values outside their documented sets; an omitted
# max_age_seconds/staleness yields "unknown", never "fresh".
# ---------------------------------------------------------------------------
def test_freshness_defaults_to_unknown_not_fresh():
    freshness = cs.Freshness.from_dict({"observed_at": "2026-09-11T00:00:00Z"})
    assert freshness.staleness == "unknown"


def test_freshness_rejects_a_malformed_staleness_with_context_snapshot_error():
    # Regression: staleness used to skip _require_str, so an unhashable
    # value (e.g. a list) blew up with a raw TypeError inside the later
    # `staleness not in FRESHNESS_VALUES` check instead of failing closed
    # with ContextSnapshotError.
    with pytest.raises(cs.ContextSnapshotError, match=r"freshness\.staleness"):
        cs.Freshness.from_dict({"observed_at": "2026-09-11T00:00:00Z", "staleness": ["fresh"]})


def test_validate_rejects_unknown_freshness_staleness():
    # Built via dataclasses.replace, not seal(), so the invalid value is
    # injected AFTER construction and validate() is the thing under test —
    # seal() itself would already refuse to produce this snapshot.
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad_source = dc_replace(
        snapshot.sources[0],
        freshness=cs.Freshness(observed_at="2026-09-11T00:00:00Z", staleness="brand-new"),
    )
    bad_snapshot = dc_replace(snapshot, sources=(bad_source,))
    with pytest.raises(cs.ContextSnapshotError, match="staleness"):
        bad_snapshot.validate()


def test_validate_rejects_unknown_trust_tier():
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad_source = dc_replace(snapshot.sources[0], trust=cs.Trust(tier="omniscient", rationale_code="x"))
    bad_snapshot = dc_replace(snapshot, sources=(bad_source,))
    with pytest.raises(cs.ContextSnapshotError, match=r"trust\.tier"):
        bad_snapshot.validate()


def test_validate_rejects_unknown_source_kind():
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad_source = dc_replace(snapshot.sources[0], kind="carrier-pigeon")
    bad_snapshot = dc_replace(snapshot, sources=(bad_source,))
    with pytest.raises(cs.ContextSnapshotError, match="kind"):
        bad_snapshot.validate()


def test_validate_rejects_unknown_retention_class():
    snapshot = _minimal_snapshot()
    bad_snapshot = dc_replace(snapshot, retention=cs.RetentionPolicy(class_="forever", expires_after_days=1))
    with pytest.raises(cs.ContextSnapshotError, match=r"retention\.class"):
        bad_snapshot.validate()


@pytest.mark.parametrize("value", sorted(cs.FRESHNESS_VALUES))
def test_every_documented_freshness_value_is_accepted(value):
    freshness = cs.Freshness.from_dict({"observed_at": "2026-09-11T00:00:00Z", "staleness": value})
    assert freshness.staleness == value


@pytest.mark.parametrize("value", sorted(cs.TRUST_TIERS))
def test_every_documented_trust_tier_is_accepted(value):
    trust = cs.Trust.from_dict({"tier": value, "rationale_code": "x"})
    assert trust.tier == value


def test_validate_wraps_unserializable_selector_as_context_snapshot_error():
    # Regression: _canonical_json could raise a raw TypeError out of
    # validate() (via the selector-length check in _check_source), violating
    # the documented contract that validate()/seal() only ever raise
    # ContextSnapshotError.
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad_source = dc_replace(snapshot.sources[0], selector={"bad": {1, 2, 3}})
    bad_snapshot = dc_replace(snapshot, sources=(bad_source,))
    with pytest.raises(cs.ContextSnapshotError, match="JSON-serializable"):
        bad_snapshot.validate()


# ---------------------------------------------------------------------------
# T8 — step chaining: a child whose execution block differs from its
# parent's is rejected; sequence must be strictly increasing; a root
# snapshot with a parent_snapshot_id is rejected (structurally impossible
# without a step block, see StepRef).
# ---------------------------------------------------------------------------
def test_root_snapshot_has_no_step_block():
    snapshot = _minimal_snapshot()
    assert snapshot.step is None


def test_child_snapshot_with_matching_execution_validates_against_parent():
    parent = _minimal_snapshot()
    child = _minimal_snapshot(
        step=cs.StepRef(parent_snapshot_id=parent.snapshot_id, step="implement", sequence=1),
    )
    child.validate(parent=parent)  # must not raise


def test_child_snapshot_execution_mismatch_is_rejected():
    parent = _minimal_snapshot()
    child = _minimal_snapshot(
        execution=_execution(agent="implementer"),
        step=cs.StepRef(parent_snapshot_id=parent.snapshot_id, step="implement", sequence=1),
    )
    with pytest.raises(cs.ContextSnapshotError, match="execution block must equal its parent"):
        child.validate(parent=parent)


def test_child_snapshot_parent_id_mismatch_is_rejected():
    parent = _minimal_snapshot()
    child = _minimal_snapshot(
        step=cs.StepRef(parent_snapshot_id="cs-doesnotexist0000", step="implement", sequence=1),
    )
    with pytest.raises(cs.ContextSnapshotError, match="parent_snapshot_id"):
        child.validate(parent=parent)


def test_stepless_snapshot_cannot_validate_against_a_parent():
    parent = _minimal_snapshot()
    child = _minimal_snapshot()  # no step block
    with pytest.raises(cs.ContextSnapshotError, match="step block"):
        child.validate(parent=parent)


def test_validate_step_sequence_accepts_strictly_increasing_sequence():
    parent = _minimal_snapshot()
    children = [
        _minimal_snapshot(step=cs.StepRef(parent_snapshot_id=parent.snapshot_id, step="investigate", sequence=1)),
        _minimal_snapshot(
            execution=_execution(agent="implementer"),
            step=cs.StepRef(parent_snapshot_id=parent.snapshot_id, step="implement", sequence=2),
        ),
        _minimal_snapshot(
            execution=_execution(agent="shepherd"),
            step=cs.StepRef(parent_snapshot_id=parent.snapshot_id, step="review-fix", sequence=3),
        ),
    ]
    cs.validate_step_sequence(children)  # must not raise


def test_validate_step_sequence_rejects_non_increasing_sequence():
    parent = _minimal_snapshot()
    children = [
        _minimal_snapshot(step=cs.StepRef(parent_snapshot_id=parent.snapshot_id, step="investigate", sequence=2)),
        _minimal_snapshot(
            execution=_execution(agent="implementer"),
            step=cs.StepRef(parent_snapshot_id=parent.snapshot_id, step="implement", sequence=2),
        ),
    ]
    with pytest.raises(cs.ContextSnapshotError, match="strictly greater"):
        cs.validate_step_sequence(children)


def test_validate_step_sequence_rejects_a_stepless_entry():
    with pytest.raises(cs.ContextSnapshotError, match="no step block"):
        cs.validate_step_sequence([_minimal_snapshot()])


def test_validate_step_sequence_rejects_siblings_with_different_parents():
    # Regression: validate_step_sequence only checked that `sequence` was
    # strictly increasing, never that every child shared one
    # parent_snapshot_id, despite StepRef's docstring promising both halves
    # of the "siblings sharing one parent" rule.
    parent_one = _minimal_snapshot()
    parent_two = _minimal_snapshot(execution=_execution(agent="implementer"))
    children = [
        _minimal_snapshot(step=cs.StepRef(parent_snapshot_id=parent_one.snapshot_id, step="investigate", sequence=1)),
        _minimal_snapshot(
            execution=_execution(agent="shepherd"),
            step=cs.StepRef(parent_snapshot_id=parent_two.snapshot_id, step="implement", sequence=2),
        ),
    ]
    with pytest.raises(cs.ContextSnapshotError, match="parent_snapshot_id"):
        cs.validate_step_sequence(children)


# ---------------------------------------------------------------------------
# T9 — budget semantics: used_sources/used_bytes exceeding maxima without
# truncated=true is rejected; no token/context-window field exists anywhere
# in the schema.
# ---------------------------------------------------------------------------
def test_budget_overrun_without_truncated_is_rejected():
    snapshot = _minimal_snapshot()
    bad_snapshot = dc_replace(snapshot, budget=_budget(max_sources=1, used_sources=2, truncated=False))
    with pytest.raises(cs.ContextSnapshotError, match="used_sources"):
        bad_snapshot.validate()


def test_budget_byte_overrun_without_truncated_is_rejected():
    snapshot = _minimal_snapshot()
    bad_snapshot = dc_replace(snapshot, budget=_budget(max_bytes=10, used_bytes=20, truncated=False))
    with pytest.raises(cs.ContextSnapshotError, match="used_bytes"):
        bad_snapshot.validate()


def test_budget_overrun_with_truncated_true_is_accepted():
    snapshot = _minimal_snapshot()
    ok_snapshot = dc_replace(snapshot, budget=_budget(max_sources=1, used_sources=2, truncated=True))
    ok_snapshot.validate()  # must not raise


def test_budget_rejects_source_over_max_bytes_per_source():
    # Regression: max_bytes_per_source was declared, parsed, hashed and
    # documented (ADR 009 sec. 1/6) but never enforced by validate().
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad_snapshot = dc_replace(
        snapshot, budget=_budget(used_sources=1, used_bytes=100, max_bytes_per_source=50)
    )
    with pytest.raises(cs.ContextSnapshotError, match="max_bytes_per_source"):
        bad_snapshot.validate()


def test_budget_per_source_cap_skipped_when_truncated():
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    ok_snapshot = dc_replace(
        snapshot,
        budget=_budget(used_sources=1, used_bytes=100, max_bytes_per_source=50, truncated=True),
    )
    ok_snapshot.validate()  # must not raise


def test_budget_used_sources_must_reconcile_with_included_sources():
    # Regression: used_sources/used_bytes were never reconciled against the
    # actual `sources` list, so a snapshot could declare totals that don't
    # match reality.
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad_snapshot = dc_replace(snapshot, budget=_budget(used_sources=2, used_bytes=100))
    with pytest.raises(cs.ContextSnapshotError, match="used_sources"):
        bad_snapshot.validate()


def test_budget_used_bytes_must_reconcile_with_included_sources_byte_sum():
    snapshot = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad_snapshot = dc_replace(snapshot, budget=_budget(used_sources=1, used_bytes=999))
    with pytest.raises(cs.ContextSnapshotError, match="used_bytes"):
        bad_snapshot.validate()


def test_budget_reconciliation_only_counts_included_sources():
    # A dropped/not-included source still appears in `sources` (the ADR's
    # "every source considered, included or not"), but must not count
    # toward used_sources/used_bytes — matching the golden fixture's shape.
    included = _source()
    excluded = _source(
        source_id="s2",
        selection=cs.Selection(rank=2, reason_code="budget-exhausted", included=False),
    )
    snapshot = _minimal_snapshot(
        sources=[included, excluded], budget=_budget(used_sources=1, used_bytes=100)
    )
    snapshot.validate()  # must not raise: the excluded source doesn't count


def test_no_token_or_context_window_field_in_budget():
    field_names = {f.name for f in __import__("dataclasses").fields(cs.ContextBudget)}
    for name in field_names:
        assert "token" not in name.lower()
        assert "context_window" not in name.lower()


def test_context_budget_from_dict_rejects_a_smuggled_token_field():
    doc = _budget().to_dict()
    doc["max_tokens"] = 8000
    with pytest.raises(cs.ContextSnapshotError, match="unknown key"):
        cs.ContextBudget.from_dict(doc)


# ---------------------------------------------------------------------------
# ContextSource.selector defensive copy: a caller mutating the dict it
# passed to a direct-construction/seal() call must never change the sealed
# document's effective content.
# ---------------------------------------------------------------------------
def test_context_source_defensively_copies_selector_on_direct_construction():
    selector = {"mode": "agent-directed"}
    source = _source(selector=selector)
    selector["mode"] = "mutated-after-construction"
    assert source.selector == {"mode": "agent-directed"}


# ---------------------------------------------------------------------------
# Cross-cutting: fixture matches the worked-example shape tasks.md describes.
# ---------------------------------------------------------------------------
def test_fixture_matches_worked_investigator_example():
    snapshot = _load_fixture_snapshot()
    assert snapshot.execution.temporal_workflow_id == "dev-loop-mctlhq-mctl-agents-264"
    kinds = {s.kind for s in snapshot.sources}
    assert kinds == {"github-issue", "target-repo", "loki-logs", "incident"}

    issue_source = next(s for s in snapshot.sources if s.kind == "github-issue")
    assert issue_source.trust.tier == "untrusted"
    assert issue_source.freshness.staleness == "fresh"

    repo_source = next(s for s in snapshot.sources if s.kind == "target-repo")
    assert repo_source.selector["mode"] == "agent-directed"
    assert repo_source.trust.tier == "authoritative"
    assert snapshot.execution.target_repository_sha in repo_source.locator

    loki_sources = [s for s in snapshot.sources if s.kind == "loki-logs"]
    included_loki = next(s for s in loki_sources if s.selection.included)
    assert included_loki.selector == {"lines": 50, "since": "1h"}
    assert included_loki.trust.tier == "corroborated"

    dropped = [s for s in snapshot.sources if not s.selection.included]
    assert len(dropped) == 1
    assert dropped[0].selection.reason_code == "budget-exhausted"

    incident_source = next(s for s in snapshot.sources if s.kind == "incident")
    assert incident_source.trust.tier == "corroborated"
    assert incident_source.freshness.staleness == "aging"

    assert len(snapshot.evidence_refs) == 1
    assert snapshot.budget.max_sources > 0
    assert snapshot.budget.max_bytes > 0
