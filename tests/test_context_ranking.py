"""Retrieval ranking, freshness, trust and conflicting evidence
(mctlhq/mctl-agents#471, ADR 009 amendment 1).

Two halves:

- the DEFAULT strategy (`deterministic-fixed-order`) is unchanged: its
  snapshot ids and stored bytes are pinned to what `main` produced before
  this change, and an empty `conflicts` block changes no hash;
- the opt-in `trust-freshness-ranked` strategy: pinned primaries first, then
  trust tier, freshness and recency; stale sources demoted and flagged rather
  than dropped; a prior proposal aged by its `.status.yaml` `updated_at`; and
  the one fixed conflict rule recorded in the snapshot and rendered in `on`
  mode.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator import context_assembly as ca
from orchestrator import context_snapshot as cs
from orchestrator.run_issue_investigator import IssueData, IssueRef, _render_assembled_context_section

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "context" / "investigator-snapshot.json"

NOW = datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC)
NOW_ISO = "2026-09-19T00:00:00Z"

COMMENTS = (
    ("IC_a", "alice", "2026-09-10T00:00:00Z", "first comment"),
    ("IC_b", "bob", "2026-09-17T12:00:00Z", "a later comment"),
    ("IC_c", "carol", "2026-09-18T23:30:00Z", "the latest comment"),
)
PROPOSAL_UPDATED_AT = "2026-09-15T00:00:00Z"
_STATUS = f"status: proposed\nupdated_at: '{PROPOSAL_UPDATED_AT}'\n"


def _issue(comments=()) -> IssueData:
    ref = IssueRef(
        owner="mctlhq", repo="mctl-agents", number=471,
        url="https://github.com/mctlhq/mctl-agents/issues/471",
    )
    return IssueData(ref=ref, title="Rank context", body="Please rank context.", state="OPEN", comments=comments)


def _execution() -> cs.ExecutionCorrelation:
    return cs.ExecutionCorrelation(
        agent="issue-investigator", environment="production",
        temporal_workflow_id="dev-loop-mctlhq-mctl-agents-471", target_repository_sha="a" * 40,
        definition_version="legacy", definition_content_hash="sha256:" + "1a" * 32,
        profile_version="legacy", profile_content_hash="sha256:" + "2b" * 32, release_revision=0,
    )


def _proposal(tmp_path: Path, *, status: str | None = _STATUS) -> Path:
    d = tmp_path / "proposal"
    d.mkdir()
    (d / "requirements.md").write_text("# req\n")
    (d / "design.md").write_text("# design\n")
    if status is not None:
        (d / ".status.yaml").write_text(status)
    return d


def _input(tmp_path: Path, *, comments=(), proposal_dir: Path | None = None,
           config: ca.AssemblyConfig | None = None, now: datetime = NOW) -> ca.AssemblyInput:
    return ca.AssemblyInput(
        issue=_issue(comments), repo_dir=tmp_path / "repo", target_repo_sha="a" * 40,
        full_repo="mctlhq/mctl-agents",
        proposal_dir=proposal_dir if proposal_dir is not None else tmp_path / "none",
        service="mctl-agents", slug="issue-471-rank", prompt_template="PROMPT SCAFFOLD",
        now=now, config=config or ca.AssemblyConfig(),
    )


def _ranked(**overrides) -> ca.AssemblyConfig:
    # A function, not a module constant, so this file still collects against
    # a build without the ranked strategy (the fail-before-fix run).
    return ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME, **overrides)


def _assemble(assembly_input: ca.AssemblyInput, mode: str = "shadow") -> ca.AssemblyResult:
    return ca.assemble(assembly_input, mode=mode, execution=_execution())


def _by_id(result: ca.AssemblyResult) -> dict[str, cs.ContextSource]:
    return {s.source_id: s for s in result.snapshot.sources}


# ---------------------------------------------------------------------------
# Default strategy: golden — ids and stored bytes pinned to pre-#471 `main`
# ---------------------------------------------------------------------------
# (snapshot_id, content_hash, sha256 of canonical_json(to_dict())) produced
# by origin/main 7015207 for the same inputs, before this change existed.
_GOLDEN_DEFAULT = {
    "bare": (
        "cs-500643c3ec2f9bee",
        "sha256:500643c3ec2f9bee4bbda13c142c936d0cdd12b5bb62da5dc53532444917f035",
        "sha256:1d23eed05e9a68f18844acd8942e3700d9e235f12a6f961676f921f16fefd933",
    ),
    "comments-and-proposal": (
        "cs-b48075cea0f6496c",
        "sha256:b48075cea0f6496c3f7c5f85acbf94f0c20c8faf3147d2f5c5753b0d1b901e20",
        "sha256:3ed81ab0642795445780069620708baeef160c88984df64aeb086eefa59a6b26",
    ),
    "tight-budget": (
        "cs-c096a1d8952eadd2",
        "sha256:c096a1d8952eadd21a142b491d4a5aecbbea029c8921b5d91bae0ee7a6dfa409",
        "sha256:cd224e012b76efa7b9a7d426c607dc160b06f0028a21ea2424ed9aa3f4756bc0",
    ),
}


@pytest.mark.parametrize("case", sorted(_GOLDEN_DEFAULT))
def test_default_strategy_snapshot_ids_and_stored_bytes_are_unchanged(tmp_path, case):
    comments, with_proposal, config = {
        "bare": ((), False, ca.AssemblyConfig()),
        "comments-and-proposal": (COMMENTS, True, ca.AssemblyConfig()),
        "tight-budget": (COMMENTS, True, ca.AssemblyConfig(max_sources=4)),
    }[case]
    proposal_dir = _proposal(tmp_path) if with_proposal else None
    result = _assemble(_input(tmp_path, comments=comments, proposal_dir=proposal_dir, config=config))
    snapshot = result.snapshot
    stored = cs.hash_bytes(cs.canonical_json(snapshot.to_dict()))
    # Deliberately touches nothing #471 added, so it passes on pre-#471
    # `main` too: it is the invariant, not a new behaviour.
    assert (snapshot.snapshot_id, snapshot.content_hash, stored) == _GOLDEN_DEFAULT[case]


def test_default_strategy_snapshots_carry_no_conflicts_block(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path)))
    assert result.snapshot.conflicts == ()
    assert "conflicts" not in result.snapshot.to_dict()


def test_default_strategy_is_the_default_and_the_env_default(monkeypatch):
    monkeypatch.delenv(ca.STRATEGY_ENV_VAR, raising=False)
    assert ca.AssemblyConfig().strategy == ca.STRATEGY_NAME
    assert ca.AssemblyConfig.from_env().strategy == ca.STRATEGY_NAME
    assert not ca.AssemblyConfig.from_env().ranked


def test_golden_fixture_hash_and_bytes_unchanged_with_empty_conflicts():
    doc = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    snapshot = cs.ContextSnapshot.from_dict(doc)
    assert snapshot.conflicts == ()
    assert cs.recompute_content_hash(snapshot) == doc["content_hash"]
    assert "conflicts" not in snapshot.to_dict()
    assert snapshot.to_dict() == doc
    resealed = cs.seal(
        execution=snapshot.execution, strategy=snapshot.strategy, budget=snapshot.budget,
        retention=snapshot.retention, created_at=snapshot.created_at, step=snapshot.step,
        sources=snapshot.sources, evidence_refs=snapshot.evidence_refs, conflicts=(),
    )
    assert resealed.content_hash == doc["content_hash"]
    assert resealed.snapshot_id == doc["snapshot_id"]


def test_a_non_empty_conflicts_block_enters_the_hash():
    doc = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    snapshot = cs.ContextSnapshot.from_dict(doc)
    ids = tuple(s.source_id for s in snapshot.sources[:2])
    conflict = cs.ContextConflict(
        subject="prior-proposal-superseded-by-later-comment", source_ids=ids,
        resolution_code="kept-all-ranked-by-trust-freshness-recency",
    )
    with_conflict = cs.seal(
        execution=snapshot.execution, strategy=snapshot.strategy, budget=snapshot.budget,
        retention=snapshot.retention, created_at=snapshot.created_at,
        sources=snapshot.sources, evidence_refs=snapshot.evidence_refs, conflicts=(conflict,),
    )
    assert with_conflict.content_hash != doc["content_hash"]
    assert cs.recompute_content_hash(with_conflict) == with_conflict.content_hash
    assert cs.ContextSnapshot.from_dict(with_conflict.to_dict()) == with_conflict
    assert with_conflict.to_log_dict()["conflict_count"] == 1
    assert "conflict_count" not in snapshot.to_log_dict()


# ---------------------------------------------------------------------------
# Schema: the `conflicts` block validates like every other block
# ---------------------------------------------------------------------------
def _fixture_snapshot() -> cs.ContextSnapshot:
    return cs.ContextSnapshot.from_dict(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


def _seal_with(conflict: cs.ContextConflict) -> cs.ContextSnapshot:
    s = _fixture_snapshot()
    return cs.seal(
        execution=s.execution, strategy=s.strategy, budget=s.budget, retention=s.retention,
        created_at=s.created_at, sources=s.sources, conflicts=(conflict,),
    )


def _conflict(**overrides) -> cs.ContextConflict:
    ids = tuple(s.source_id for s in _fixture_snapshot().sources[:2])
    fields = dict(subject="prior-proposal-superseded-by-later-comment", source_ids=ids,
                  resolution_code="kept-all-ranked-by-trust-freshness-recency")
    fields.update(overrides)
    return cs.ContextConflict(**fields)


def test_conflict_must_name_sources_of_this_snapshot():
    with pytest.raises(cs.ContextSnapshotError, match="not sources of this snapshot"):
        _seal_with(_conflict(source_ids=(_fixture_snapshot().sources[0].source_id, "ghost")))


def test_conflict_needs_two_distinct_sources():
    first = _fixture_snapshot().sources[0].source_id
    with pytest.raises(cs.ContextSnapshotError, match="distinct sources"):
        _seal_with(_conflict(source_ids=(first,)))
    with pytest.raises(cs.ContextSnapshotError, match="distinct sources"):
        _seal_with(_conflict(source_ids=(first, first)))


def test_conflict_resolution_code_is_a_closed_vocabulary():
    with pytest.raises(cs.ContextSnapshotError, match="resolution_code"):
        _seal_with(_conflict(resolution_code="dropped-the-loser"))


def test_conflict_subject_is_bounded():
    with pytest.raises(cs.ContextSnapshotError, match="subject"):
        _seal_with(_conflict(subject="x" * (cs.MAX_CONFLICT_SUBJECT_LENGTH + 1)))
    with pytest.raises(cs.ContextSnapshotError, match="subject"):
        _seal_with(_conflict(subject=""))


def test_conflict_from_dict_rejects_an_unknown_key():
    doc = _seal_with(_conflict()).to_dict()
    doc["conflicts"][0]["note"] = "a smuggled payload"
    with pytest.raises(cs.ContextSnapshotError, match="unknown key"):
        cs.ContextSnapshot.from_dict(doc)


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------
def test_env_selects_the_ranked_strategy(monkeypatch):
    monkeypatch.setenv(ca.STRATEGY_ENV_VAR, "trust-freshness-ranked")
    assert ca.AssemblyConfig.from_env().ranked


def test_an_unknown_strategy_fails_loudly(monkeypatch):
    monkeypatch.setenv(ca.STRATEGY_ENV_VAR, "vector-magic")
    with pytest.raises(ValueError, match=ca.STRATEGY_ENV_VAR):
        ca.AssemblyConfig.from_env()


def test_ranked_snapshot_records_strategy_ranker_and_scores(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked()))
    strategy = result.snapshot.strategy
    assert (strategy.name, strategy.version) == ("trust-freshness-ranked", "1.0.0")
    assert (strategy.ranker_name, strategy.ranker_version) == (ca.RANKER_NAME, ca.RANKER_VERSION)
    assert all(s.selection.score is not None for s in result.snapshot.sources)
    assert result.metrics.strategy_name == "trust-freshness-ranked"
    log = result.metrics.to_log_dict()
    assert log["snapshot"]["ranker_name"] == ca.RANKER_NAME


def test_default_snapshot_records_no_ranker_and_no_score(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS))
    assert result.snapshot.strategy.ranker_name is None
    assert all(s.selection.score is None for s in result.snapshot.sources)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------
def test_pinned_primaries_come_first_in_collector_order(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked()))
    ordered = [s.source_id for s in sorted(result.snapshot.sources, key=lambda s: s.selection.rank)]
    assert ordered[:3] == ["inline-template", "issue", "target-repo"]


def test_trust_tier_orders_the_rest_then_recency(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked()))
    ordered = [s.source_id for s in sorted(result.snapshot.sources, key=lambda s: s.selection.rank)]
    # corroborated proposal documents before untrusted comments; the
    # comments newest first (all three are `fresh` by retrieval age).
    assert ordered[3:] == [
        "proposal-dir-requirements.md", "proposal-dir-design.md",
        "issue-comment-IC_c", "issue-comment-IC_b", "issue-comment-IC_a",
    ]


def _cand(source_id, *, tier="untrusted", staleness="fresh", content_time=None, kind="github-issue-comment"):
    c = ca.CandidateSource(
        source_id=source_id, kind=kind, locator="x", selector={}, raw=b"x",
        observed_at=NOW_ISO, max_age_seconds=3600, trust_tier=tier, trust_rationale="r", reason_code="r",
        content_time=content_time,
    )
    c.freshness_staleness = staleness
    return c


def test_freshness_orders_within_a_trust_tier():
    stale = _cand("stale", staleness="stale", content_time="2026-09-18T00:00:00Z")
    aging = _cand("aging", staleness="aging", content_time="2026-09-01T00:00:00Z")
    unknown = _cand("unknown", staleness="unknown", content_time="2026-09-18T00:00:00Z")
    fresh = _cand("fresh", staleness="fresh", content_time="2026-09-01T00:00:00Z")
    ordered = ca.rank_candidates([stale, unknown, aging, fresh])
    assert [c.source_id for c in ordered] == ["fresh", "aging", "unknown", "stale"]


def test_trust_dominates_freshness():
    untrusted_fresh = _cand("u", tier="untrusted", staleness="fresh")
    corroborated_stale = _cand("c", tier="corroborated", staleness="stale", kind="proposal-dir")
    ordered = ca.rank_candidates([untrusted_fresh, corroborated_stale])
    assert [c.source_id for c in ordered] == ["c", "u"]
    assert corroborated_stale.score is not None and untrusted_fresh.score is not None
    assert corroborated_stale.score > untrusted_fresh.score


def test_recency_breaks_ties_newest_first_and_undated_last():
    old = _cand("old", content_time="2026-09-01T00:00:00Z")
    new = _cand("new", content_time="2026-09-18T00:00:00Z")
    undated = _cand("undated", content_time="not-a-time")
    undated.observed_at = "also-not-a-time"
    ordered = ca.rank_candidates([undated, old, new])
    assert [c.source_id for c in ordered] == ["new", "old", "undated"]


def test_ranking_scores_are_recorded_per_rule():
    pinned = _cand("issue", kind="github-issue", tier="untrusted", staleness="stale")
    best = _cand("best", tier="authoritative", staleness="fresh", kind="gitops-file")
    worst = _cand("worst", tier="untrusted", staleness="stale")
    ca.rank_candidates([worst, best, pinned])
    assert pinned.score == 100.0
    assert best.score == 33.0
    assert worst.score == 0.0
    assert [pinned.rank, best.rank, worst.rank] == [1, 2, 3]


def test_budget_cuts_from_the_bottom_of_the_ranked_order(tmp_path):
    config = _ranked(max_sources=6)
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=config))
    by_id = _by_id(result)
    # Ranks 1..6 fit: 3 pinned, 2 proposal docs, the NEWEST comment.
    assert by_id["issue-comment-IC_c"].selection.included
    assert not by_id["issue-comment-IC_b"].selection.included
    assert not by_id["issue-comment-IC_a"].selection.included
    assert by_id["issue-comment-IC_a"].selection.reason_code == "budget-exhausted"


# ---------------------------------------------------------------------------
# Freshness (D3)
# ---------------------------------------------------------------------------
def test_ranked_prior_proposal_is_aged_by_its_status_updated_at(tmp_path):
    result = _assemble(_input(tmp_path, proposal_dir=_proposal(tmp_path), config=_ranked()))
    doc = _by_id(result)["proposal-dir-design.md"]
    assert doc.freshness.observed_at == PROPOSAL_UPDATED_AT
    assert doc.retrieved_at == NOW_ISO
    # four days old against the 86400 s proposal-dir window.
    assert doc.freshness.staleness == "stale"


def test_ranked_prior_proposal_without_a_readable_updated_at_is_unknown(tmp_path):
    for status in (None, "status: proposed\n", "status: proposed\nupdated_at: yesterday\n"):
        sub = tmp_path / str(abs(hash(status)))
        sub.mkdir()
        result = _assemble(_input(sub, proposal_dir=_proposal(sub, status=status), config=_ranked()))
        doc = _by_id(result)["proposal-dir-design.md"]
        assert doc.freshness.staleness == "unknown", status
        assert doc.freshness.max_age_seconds is None


def test_status_updated_at_must_be_a_real_timestamp_and_is_normalized(tmp_path):
    proposal_dir = _proposal(tmp_path, status="updated_at: '2026-13-45T99:00:00Z'\n")
    assert ca.read_proposal_updated_at(proposal_dir) is None
    (proposal_dir / ".status.yaml").write_text("updated_at: 2026-09-15T02:00:00+02:00\n")
    assert ca.read_proposal_updated_at(proposal_dir) == PROPOSAL_UPDATED_AT


def test_status_updated_at_is_read_without_following_a_symlink(tmp_path):
    proposal_dir = _proposal(tmp_path, status=None)
    target = tmp_path / "elsewhere.yaml"
    target.write_text(f"updated_at: '{PROPOSAL_UPDATED_AT}'\n")
    os.symlink(target, proposal_dir / ".status.yaml")
    assert ca.read_proposal_updated_at(proposal_dir) is None


def test_default_prior_proposal_keeps_the_retrieval_time(tmp_path):
    result = _assemble(_input(tmp_path, proposal_dir=_proposal(tmp_path)))
    doc = _by_id(result)["proposal-dir-design.md"]
    assert doc.freshness.observed_at == NOW_ISO
    assert doc.freshness.staleness == "fresh"


def test_ranked_github_sources_keep_the_retrieval_time(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, config=_ranked()))
    for source_id in ("issue", "issue-comment-IC_a"):
        source = _by_id(result)[source_id]
        assert source.freshness.observed_at == NOW_ISO
        assert source.freshness.staleness == "fresh"


def test_ranked_stale_source_is_demoted_and_flagged_not_dropped(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked()))
    doc = _by_id(result)["proposal-dir-design.md"]
    assert doc.freshness.staleness == "stale"
    assert doc.selection.included
    assert doc.selection.reason_code == "stale-demoted"
    assert result.metrics.stale_demoted == 2
    assert result.metrics.dropped_stale == 0


# ---------------------------------------------------------------------------
# Conflicting evidence
# ---------------------------------------------------------------------------
def test_prior_proposal_predating_a_later_comment_is_a_recorded_conflict(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked()))
    assert len(result.snapshot.conflicts) == 1
    conflict = result.snapshot.conflicts[0]
    assert conflict.subject == ca.CONFLICT_PRIOR_PROPOSAL_SUPERSEDED
    assert conflict.resolution_code == ca.CONFLICT_RESOLUTION_KEPT_RANKED
    # proposal docs + the two comments after 2026-09-15, in rank order; the
    # 2026-09-10 comment predates the proposal and is not part of it.
    assert conflict.source_ids == (
        "proposal-dir-requirements.md", "proposal-dir-design.md",
        "issue-comment-IC_c", "issue-comment-IC_b",
    )
    by_id = _by_id(result)
    assert all(by_id[i].selection.included for i in conflict.source_ids)
    assert result.metrics.conflict_count == 1
    assert result.snapshot.to_dict()["conflicts"][0]["subject"] == ca.CONFLICT_PRIOR_PROPOSAL_SUPERSEDED


def test_no_conflict_when_every_comment_predates_the_proposal(tmp_path):
    early = (COMMENTS[0],)
    result = _assemble(_input(tmp_path, comments=early, proposal_dir=_proposal(tmp_path), config=_ranked()))
    assert result.snapshot.conflicts == ()
    assert "conflicts" not in result.snapshot.to_dict()


def test_a_comment_at_the_same_instant_does_not_supersede(tmp_path):
    same = (("IC_s", "sam", PROPOSAL_UPDATED_AT, "same instant"),)
    result = _assemble(_input(tmp_path, comments=same, proposal_dir=_proposal(tmp_path), config=_ranked()))
    assert result.snapshot.conflicts == ()


def test_an_undatable_proposal_never_fires_the_rule(tmp_path):
    result = _assemble(
        _input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path, status=None), config=_ranked())
    )
    assert result.snapshot.conflicts == ()


def test_default_strategy_never_records_a_conflict(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path)))
    assert result.snapshot.conflicts == ()


# ---------------------------------------------------------------------------
# `on`-mode conflict notice
# ---------------------------------------------------------------------------
_NOTICE_TAIL = (
    "Where they disagree, the prior proposal may be out of date: check it against the "
    "later comments instead of carrying it forward unchanged."
)


def test_on_mode_renders_the_conflict_notice(tmp_path):
    result = _assemble(
        _input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked()), mode="on"
    )
    section = _render_assembled_context_section(result)
    assert "### Conflicting evidence" in section
    # the notice follows the sources it talks about
    assert section.index("### Conflicting evidence") > section.index("<context_source")


def test_notice_keeps_the_superseded_documents_and_the_later_comments_apart(tmp_path):
    result = _assemble(
        _input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked()), mode="on"
    )
    notice = ca.render_conflict_notice(result.snapshot)
    expected = (
        "- Prior proposal documents proposal-dir-requirements.md, proposal-dir-design.md "
        "were written before later issue comments issue-comment-IC_c, issue-comment-IC_b. "
        + _NOTICE_TAIL
    )
    assert expected + "\n" in notice
    assert "Also part of this conflict" not in notice


def test_notice_names_only_included_members_and_the_excluded_ones_separately(tmp_path):
    # max_sources=6 keeps IC_c and budget-excludes IC_b, a member of the conflict.
    result = _assemble(
        _input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked(max_sources=6)),
        mode="on",
    )
    assert _by_id(result)["issue-comment-IC_b"].selection.reason_code == "budget-exhausted"
    # the recorded conflict is unchanged: it still names IC_b.
    assert "issue-comment-IC_b" in result.snapshot.conflicts[0].source_ids
    notice = ca.render_conflict_notice(result.snapshot)
    expected = (
        "- Prior proposal documents proposal-dir-requirements.md, proposal-dir-design.md "
        "were written before later issue comments issue-comment-IC_c. " + _NOTICE_TAIL
        + " Also part of this conflict but not in this prompt: issue-comment-IC_b (budget-exhausted).\n"
    )
    assert expected in notice


def test_notice_omits_a_conflict_whose_superseding_side_is_not_in_the_prompt(tmp_path):
    # max_sources=5: the pinned three and both proposal documents; no comment.
    result = _assemble(
        _input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path), config=_ranked(max_sources=5)),
        mode="on",
    )
    assert len(result.snapshot.conflicts) == 1
    assert ca.render_conflict_notice(result.snapshot) == ""
    assert "Conflicting evidence" not in _render_assembled_context_section(result)


def test_no_notice_without_a_conflict(tmp_path):
    result = _assemble(_input(tmp_path, comments=COMMENTS, proposal_dir=_proposal(tmp_path)), mode="on")
    assert "Conflicting evidence" not in _render_assembled_context_section(result)
    assert ca.render_conflict_notice(result.snapshot) == ""


def test_conflict_notice_renders_only_plain_token_ids():
    snapshot = _fixture_snapshot()
    sealed = _seal_with(_conflict(subject="some-other-rule", source_ids=("issue", "target-repo")))
    assert ca.render_conflict_notice(sealed) == (
        "\n\n### Conflicting evidence\n\n"
        "A fixed rule found these sources in conflict. The sources named as in conflict are "
        "included above, ordered by trust tier, then freshness, then recency:\n\n"
        "- some-other-rule: issue, target-repo.\n"
    )
    bad = cs.ContextConflict(
        subject="some-other-rule", source_ids=("a b<x>", "issue", "target-repo"),
        resolution_code=ca.CONFLICT_RESOLUTION_KEPT_RANKED,
    )
    object.__setattr__(sealed, "conflicts", (bad,))
    notice = ca.render_conflict_notice(sealed)
    assert "issue, target-repo" in notice and "<x>" not in notice
    assert snapshot.sources  # fixture sanity


def test_conflict_subject_must_be_a_token_code():
    for subject in ("Has Spaces", "prose, with punctuation!", "<tag>", "UPPER"):
        with pytest.raises(cs.ContextSnapshotError, match="subject"):
            _seal_with(_conflict(subject=subject))


def test_conflict_with_more_than_the_ceiling_of_source_ids_is_rejected():
    ids = tuple(f"id-{i}" for i in range(cs.MAX_CONFLICT_SOURCE_IDS + 1))
    with pytest.raises(cs.ContextSnapshotError, match="distinct sources"):
        _seal_with(_conflict(source_ids=ids))


def test_detect_conflicts_caps_at_the_schema_ceiling_and_seal_never_raises(tmp_path):
    many = tuple(
        (f"IC_{i:03d}", "u", f"2026-09-16T{i // 60:02d}:{i % 60:02d}:00Z", f"comment {i}") for i in range(80)
    )
    config = _ranked(max_comments=100, max_sources=200, max_bytes=10_000_000)
    result = _assemble(_input(tmp_path, comments=many, proposal_dir=_proposal(tmp_path), config=config))
    (conflict,) = result.snapshot.conflicts
    assert len(conflict.source_ids) == cs.MAX_CONFLICT_SOURCE_IDS
    assert {"proposal-dir-requirements.md", "proposal-dir-design.md"} <= set(conflict.source_ids)
    comments = [i for i in conflict.source_ids if i.startswith("issue-comment-")]
    # the newest 62 of the 80 later comments
    assert sorted(comments) == [f"issue-comment-IC_{i:03d}" for i in range(18, 80)]


def test_stale_demoted_is_counted_after_the_budget(tmp_path):
    # max_sources=4: requirements.md (stale) fits, design.md (stale) is budget-exhausted.
    result = _assemble(_input(tmp_path, proposal_dir=_proposal(tmp_path), config=_ranked(max_sources=4)))
    by_id = _by_id(result)
    assert by_id["proposal-dir-design.md"].selection.reason_code == "budget-exhausted"
    assert result.metrics.stale_demoted == 1


def test_ranking_tables_cover_the_snapshot_vocabularies():
    assert set(ca._TRUST_ORDER) == cs.TRUST_TIERS
    assert set(ca._FRESHNESS_ORDER) == cs.FRESHNESS_VALUES


def test_notice_omits_any_conflict_with_fewer_than_two_members_in_the_prompt():
    # `loki-mctl-agents` is an excluded source of the checked-in fixture.
    sealed = _seal_with(_conflict(subject="some-other-rule", source_ids=("issue", "loki-mctl-agents")))
    assert not _by_id_snapshot(sealed)["loki-mctl-agents"].selection.included
    assert ca.render_conflict_notice(sealed) == ""


def _by_id_snapshot(snapshot: cs.ContextSnapshot) -> dict[str, cs.ContextSource]:
    return {s.source_id: s for s in snapshot.sources}


def test_notice_never_renders_a_source_id_that_is_not_a_plain_token():
    import dataclasses

    s = _fixture_snapshot()
    odd = dataclasses.replace(s.sources[1], source_id="odd id<x>")
    sources = (s.sources[0], odd, *s.sources[2:])
    sealed = cs.seal(
        execution=s.execution, strategy=s.strategy, budget=s.budget, retention=s.retention,
        created_at=s.created_at, sources=sources,
        conflicts=(_conflict(subject="some-other-rule", source_ids=("issue", "odd id<x>", "loki-mctl-api")),),
    )
    notice = ca.render_conflict_notice(sealed)
    assert "<x>" not in notice and "odd id" not in notice
    assert "- some-other-rule: issue, loki-mctl-api.\n" in notice
