"""Tests for orchestrator/context_assembly.py — mctlhq/mctl-agents#265's
producer wired into the issue-investigator (ADR 009 follow-up row (a),
docs/adr/009-context-snapshot-contract.md). T<n> below map onto this
proposal's tasks.md "## Tests" section.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestrator import context_assembly as ca
from orchestrator import context_release as cr
from orchestrator import context_rollout as rollout
from orchestrator import context_snapshot as cs
from orchestrator.run_issue_investigator import IssueData, IssueRef

REPO_ROOT = Path(__file__).resolve().parent.parent


def _issue(*, number=265, title="Assemble investigator context", comments=()) -> IssueData:
    ref = IssueRef(
        owner="mctlhq",
        repo="mctl-agents",
        number=number,
        url=f"https://github.com/mctlhq/mctl-agents/issues/{number}",
    )
    return IssueData(ref=ref, title=title, body="Please add context assembly.", state="OPEN", comments=comments)


def _assembly_input(
    tmp_path: Path,
    *,
    issue: IssueData | None = None,
    now: datetime | None = None,
    config: ca.AssemblyConfig | None = None,
    proposal_dir: Path | None = None,
) -> ca.AssemblyInput:
    return ca.AssemblyInput(
        issue=issue or _issue(),
        repo_dir=tmp_path / "repo",
        target_repo_sha="a" * 40,
        full_repo="mctlhq/mctl-agents",
        proposal_dir=proposal_dir if proposal_dir is not None else tmp_path / "proposal",
        service="mctl-agents",
        slug="issue-265-assemble-investigator-context",
        prompt_template="PROMPT SCAFFOLD",
        now=now or datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC),
        config=config or ca.AssemblyConfig(),
    )


def _execution(**overrides) -> cs.ExecutionCorrelation:
    fields = dict(
        agent="issue-investigator",
        environment="production",
        temporal_workflow_id="dev-loop-mctlhq-mctl-agents-265",
        target_repository_sha="a" * 40,
        definition_version="legacy",
        definition_content_hash="sha256:" + "1a" * 32,
        profile_version="legacy",
        profile_content_hash="sha256:" + "2b" * 32,
        release_revision=0,
    )
    fields.update(overrides)
    return cs.ExecutionCorrelation(**fields)


def _assemble(assembly_input: ca.AssemblyInput, *, mode: str = "shadow") -> ca.AssemblyResult:
    return ca.assemble(assembly_input, mode=mode, execution=_execution())


# ---------------------------------------------------------------------------
# T1 — determinism
# ---------------------------------------------------------------------------
def test_assembling_the_same_input_twice_is_deterministic(tmp_path):
    assembly_input = _assembly_input(tmp_path)
    first = _assemble(assembly_input)
    second = _assemble(assembly_input)
    assert first.snapshot.content_hash == second.snapshot.content_hash
    assert first.snapshot.snapshot_id == second.snapshot.snapshot_id


def test_sealing_the_same_sources_at_two_created_at_values_is_one_snapshot_id(tmp_path):
    # Exercises context_assembly's OWN source-conversion path
    # (_to_context_source), not a bare context_snapshot.seal() call — the
    # created_at-excluded-from-hash rule must hold for what this module
    # actually produces.
    assembly_input = _assembly_input(tmp_path)
    candidates = [c for collector in ca._COLLECTOR_ORDER for c in collector(assembly_input)]
    ca.assign_ranks(candidates)
    for c in candidates:
        ca.normalize(c)
        ca.classify_freshness(c, assembly_input.now)
    ca.deduplicate(candidates)
    budget = ca.apply_budget(candidates, assembly_input.config)
    sources = tuple(ca._to_context_source(c) for c in sorted(candidates, key=lambda c: c.rank))
    strategy = cs.ContextStrategy(name=ca.STRATEGY_NAME, version=ca.STRATEGY_VERSION)
    retention = cs.RetentionPolicy(class_="execution-record", expires_after_days=180)
    execution = _execution()
    snap_one = cs.seal(
        execution=execution, strategy=strategy, budget=budget, retention=retention,
        created_at="2026-01-01T00:00:00Z", sources=sources,
    )
    snap_two = cs.seal(
        execution=execution, strategy=strategy, budget=budget, retention=retention,
        created_at="2099-01-01T00:00:00Z", sources=sources,
    )
    assert snap_one.content_hash == snap_two.content_hash
    assert snap_one.snapshot_id == snap_two.snapshot_id


# ---------------------------------------------------------------------------
# T2 — freshness
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC)


def _candidate(**overrides) -> ca.CandidateSource:
    fields = dict(
        source_id="s1",
        kind="github-issue",
        locator="https://github.com/mctlhq/mctl-agents/issues/265",
        selector={},
        raw=b"hello",
        observed_at=ca._iso(NOW),
        max_age_seconds=3600,
        trust_tier="untrusted",
        trust_rationale="github-issue-body-third-party-text",
        reason_code="primary-problem-statement",
    )
    fields.update(overrides)
    return ca.CandidateSource(**fields)


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [(1800, "fresh"), (3600, "aging"), (3601, "stale")],
)
def test_freshness_boundaries(age_seconds, expected):
    observed = NOW.timestamp() - age_seconds
    candidate = _candidate(observed_at=ca._iso(datetime.fromtimestamp(observed, tz=UTC)))
    ca.classify_freshness(candidate, NOW)
    assert candidate.freshness_staleness == expected


def test_stale_candidate_stays_in_sources_but_excluded():
    candidates = [_candidate(observed_at=ca._iso(datetime(2020, 1, 1, tzinfo=UTC)))]
    ca.assign_ranks(candidates)
    for c in candidates:
        ca.normalize(c)
        ca.classify_freshness(c, NOW)
        if c.freshness_staleness == "stale":
            c.included = False
            c.reason_code = "stale"
    assert candidates[0].included is False
    assert candidates[0].reason_code == "stale"


def test_content_addressed_kind_is_fresh_with_null_max_age():
    candidate = _candidate(kind="target-repo", max_age_seconds=None)
    ca.classify_freshness(candidate, NOW)
    assert candidate.freshness_staleness == "fresh"


def test_unclassified_kind_with_no_max_age_falls_back_to_unknown():
    candidate = _candidate(kind="github-pr", max_age_seconds=None)
    ca.classify_freshness(candidate, NOW)
    assert candidate.freshness_staleness == "unknown"


# ---------------------------------------------------------------------------
# T3 — deduplication
# ---------------------------------------------------------------------------
def test_duplicate_content_keeps_the_lower_rank():
    winner = _candidate(source_id="a", raw=b"same bytes")
    loser = _candidate(source_id="b", raw=b"same bytes")
    candidates = [winner, loser]
    ca.assign_ranks(candidates)
    for c in candidates:
        ca.normalize(c)
    dropped = ca.deduplicate(candidates)
    assert dropped == 1
    assert winner.included is True
    assert loser.included is False
    assert loser.reason_code == "duplicate-content"


# ---------------------------------------------------------------------------
# T4 — per-source truncation
# ---------------------------------------------------------------------------
def test_truncation_rehashes_the_truncated_bytes():
    candidate = _candidate(raw=b"x" * 100)
    ca.normalize(candidate)
    full_hash = candidate.content_hash
    truncated = ca.truncate_to_per_source_limit(candidate, 10)
    assert truncated is True
    assert candidate.byte_count == 10
    assert candidate.content_hash == cs.hash_bytes(b"x" * 10)
    assert candidate.content_hash != full_hash
    assert candidate.selector["byte_range"] == [0, 10]
    assert candidate.truncated is True


def test_no_truncation_when_under_the_limit():
    candidate = _candidate(raw=b"short")
    ca.normalize(candidate)
    assert ca.truncate_to_per_source_limit(candidate, 50) is False
    assert candidate.truncated is False


# ---------------------------------------------------------------------------
# T5 — budget
# ---------------------------------------------------------------------------
def test_budget_excludes_from_the_first_miss_onward():
    candidates = [
        _candidate(source_id="s1", raw=b"a" * 10),
        _candidate(source_id="s2", raw=b"b" * 10),
        _candidate(source_id="s3", raw=b"c" * 10),
    ]
    ca.assign_ranks(candidates)
    for c in candidates:
        ca.normalize(c)
    config = ca.AssemblyConfig(max_sources=1, max_bytes=1000, max_bytes_per_source=1000)
    budget = ca.apply_budget(candidates, config)
    assert candidates[0].included is True
    assert candidates[1].included is False
    assert candidates[1].reason_code == "budget-exhausted"
    assert candidates[2].included is False
    assert candidates[2].reason_code == "budget-exhausted"
    assert budget.truncated is True
    assert budget.used_sources == 1


def test_budget_reconciliation_passes_snapshot_validate(tmp_path):
    config = ca.AssemblyConfig(max_sources=1, max_bytes=100000, max_bytes_per_source=50000)
    assembly_input = _assembly_input(tmp_path, config=config)
    result = _assemble(assembly_input)
    result.snapshot.validate()  # must not raise


# ---------------------------------------------------------------------------
# T6 — source coverage
# ---------------------------------------------------------------------------
def test_source_coverage_spans_heterogeneous_kinds_and_known_vocabularies(tmp_path):
    proposal_dir = tmp_path / "proposal"
    proposal_dir.mkdir()
    (proposal_dir / "requirements.md").write_text("prior requirements")
    (proposal_dir / "design.md").write_text("prior design")
    (proposal_dir / "tasks.md").write_text("prior tasks")
    assembly_input = _assembly_input(tmp_path, proposal_dir=proposal_dir)
    result = _assemble(assembly_input)

    kinds = {s.kind for s in result.snapshot.sources}
    assert kinds >= {"github-issue", "target-repo", "inline-template", "proposal-dir"}
    assert kinds <= cs.SOURCE_KINDS

    for source in result.snapshot.sources:
        assert source.trust.tier in cs.TRUST_TIERS

    issue_source = next(s for s in result.snapshot.sources if s.kind == "github-issue")
    assert issue_source.trust.tier == "untrusted"

    repo_source = next(s for s in result.snapshot.sources if s.kind == "target-repo")
    assert repo_source.trust.tier == "authoritative"
    assert repo_source.byte_count == 0
    assert repo_source.selector == {"mode": "agent-directed"}


def test_github_issue_comment_sources_are_untrusted(tmp_path):
    comments = (("c1", "alice", "2026-09-18T00:00:00Z", "a comment"),)
    assembly_input = _assembly_input(tmp_path, issue=_issue(comments=comments))
    result = _assemble(assembly_input)
    comment_sources = [s for s in result.snapshot.sources if s.kind == "github-issue-comment"]
    assert comment_sources
    for source in comment_sources:
        assert source.trust.tier == "untrusted"


# ---------------------------------------------------------------------------
# T7 — telemetry safety
# ---------------------------------------------------------------------------
def test_no_locator_selector_or_payload_leaks_into_metrics(tmp_path, capsys):
    marker = "CONTEXT-LEAK-CANARY"
    comments = (("c1", "alice", "2026-09-18T00:00:00Z", marker),)
    assembly_input = _assembly_input(tmp_path, issue=_issue(comments=comments))
    result = _assemble(assembly_input)
    print(f"[context] context_assembly={json.dumps(result.metrics.to_log_dict(), sort_keys=True)}")
    output = capsys.readouterr().out
    assert marker not in output
    for source in result.snapshot.sources:
        assert source.locator not in output
        assert json.dumps(dict(source.selector)) not in output


def test_rendered_carries_the_marker_but_metrics_log_dict_never_does(tmp_path):
    marker = "CONTEXT-LEAK-CANARY"
    comments = (("c1", "alice", "2026-09-18T00:00:00Z", marker),)
    assembly_input = _assembly_input(tmp_path, issue=_issue(comments=comments))
    result = _assemble(assembly_input, mode="on")
    assert any(marker in text for text in result.rendered.values())
    serialized_metrics = json.dumps(result.metrics.to_log_dict())
    assert marker not in serialized_metrics


# ---------------------------------------------------------------------------
# T8 — hash-rule unity
# ---------------------------------------------------------------------------
def test_candidate_content_hash_matches_context_snapshot_hash_bytes():
    candidate = _candidate(raw=b"some bytes to hash")
    ca.normalize(candidate)
    assert candidate.content_hash == cs.hash_bytes(b"some bytes to hash")


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------
def test_collect_target_repo_shape(tmp_path):
    assembly_input = _assembly_input(tmp_path)
    [source] = ca.collect_target_repo(assembly_input)
    assert source.kind == "target-repo"
    assert source.locator == f"git+https://github.com/mctlhq/mctl-agents@{'a' * 40}"
    assert source.selector == {"mode": "agent-directed"}
    assert source.raw == b""
    assert source.trust_tier == "authoritative"


def test_collect_issue_comments_orders_ascending_and_caps_at_max_comments(tmp_path):
    comments = tuple(
        (f"c{i}", "alice", f"2026-09-{i + 1:02d}T00:00:00Z", f"comment {i}")
        for i in range(25)
    )
    config = ca.AssemblyConfig(max_comments=20)
    assembly_input = _assembly_input(tmp_path, issue=_issue(comments=comments), config=config)
    produced = ca.collect_issue_comments(assembly_input)
    assert len(produced) == 20
    # The newest 20 (by created_at) survive — comment 5 through comment 24.
    assert produced[0].source_id == "issue-comment-c5"
    assert produced[-1].source_id == "issue-comment-c24"


def test_collect_prior_proposal_yields_nothing_for_a_missing_directory(tmp_path):
    assembly_input = _assembly_input(tmp_path, proposal_dir=tmp_path / "does-not-exist")
    assert ca.collect_prior_proposal(assembly_input) == []


def test_collect_prior_proposal_reads_the_existing_triplet(tmp_path):
    proposal_dir = tmp_path / "proposal"
    proposal_dir.mkdir()
    (proposal_dir / "requirements.md").write_text("req")
    assembly_input = _assembly_input(tmp_path, proposal_dir=proposal_dir)
    produced = ca.collect_prior_proposal(assembly_input)
    assert len(produced) == 1
    assert produced[0].kind == "proposal-dir"
    assert produced[0].trust_tier == "corroborated"
    assert produced[0].render_text == "req"


# ---------------------------------------------------------------------------
# _read_prior_proposal_file hardening — three hazards a gitops-published
# proposal directory (agent-authored, hence untrusted) can pose to the
# worker reading it (orchestrator/context_assembly.py:508).
# ---------------------------------------------------------------------------
def test_read_prior_proposal_file_refuses_a_symlink(tmp_path):
    """Hazard 1: a symlink planted where a plain triplet file is expected
    must not be followed — O_NOFOLLOW makes the open() fail closed instead
    of silently reading whatever the link points at (e.g. a path outside
    the proposal directory, or elsewhere on the worker's filesystem)."""
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the proposal directory")
    proposal_dir = tmp_path / "proposal"
    proposal_dir.mkdir()
    (proposal_dir / "requirements.md").symlink_to(secret)

    assert ca._read_prior_proposal_file(proposal_dir / "requirements.md", max_bytes=1000) is None

    # And the collector treats it exactly like a missing file, not an error.
    assembly_input = _assembly_input(tmp_path, proposal_dir=proposal_dir)
    assert ca.collect_prior_proposal(assembly_input) == []


def test_read_prior_proposal_file_refuses_a_non_regular_file(tmp_path):
    """Hazard 2: anything that is not a regular file — a FIFO here, stood
    in for the class of special files (FIFOs, devices) the fstat check
    rejects — must not be read, since a FIFO with no writer would block
    the worker's `open()`/`read()` indefinitely."""
    proposal_dir = tmp_path / "proposal"
    proposal_dir.mkdir()
    fifo_path = proposal_dir / "requirements.md"
    os.mkfifo(fifo_path)

    # O_NONBLOCK on the open() keeps this from hanging even before fstat
    # gets a say; the read must still come back None, not block or raise.
    assert ca._read_prior_proposal_file(fifo_path, max_bytes=1000) is None


def test_read_prior_proposal_file_bounds_the_read_below_file_size(tmp_path):
    """Hazard 3: the read is capped at `max_bytes + 1` regardless of how
    large the file actually is, so an oversized (or still-growing) triplet
    file cannot exhaust the worker's memory just by being opened."""
    proposal_dir = tmp_path / "proposal"
    proposal_dir.mkdir()
    big = proposal_dir / "requirements.md"
    big.write_bytes(b"x" * 10_000)

    result = ca._read_prior_proposal_file(big, max_bytes=100)

    assert result is not None
    assert len(result) == 101  # max_bytes + 1, not the file's full 10_000 bytes


# ---------------------------------------------------------------------------
# build_execution_correlation
# ---------------------------------------------------------------------------
def test_build_execution_correlation_legacy_branch(tmp_path):
    execution = ca.build_execution_correlation(
        resolver_mode="legacy",
        issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
        target_repository_sha="a" * 40,
        legacy_model="claude-sonnet-5",
        legacy_allowed_tools=("Read", "Write"),
        legacy_budget_usd=8.0,
    )
    assert execution.definition_version == "legacy"
    assert execution.definition_content_hash.startswith("sha256:")
    assert execution.profile_version == "legacy"
    assert execution.profile_content_hash.startswith("sha256:")
    assert execution.release_revision == 0
    assert execution.agent == "issue-investigator"
    assert execution.temporal_workflow_id == "dev-loop-mctlhq-mctl-agents-265"


def test_build_execution_correlation_legacy_hash_changes_with_inputs():
    base = ca.build_execution_correlation(
        resolver_mode="legacy",
        issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
        target_repository_sha="a" * 40,
        legacy_model="claude-sonnet-5",
        legacy_allowed_tools=("Read",),
        legacy_budget_usd=8.0,
    )
    changed = ca.build_execution_correlation(
        resolver_mode="legacy",
        issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
        target_repository_sha="a" * 40,
        legacy_model="claude-opus-5",
        legacy_allowed_tools=("Read",),
        legacy_budget_usd=8.0,
    )
    assert base.profile_content_hash != changed.profile_content_hash


def test_build_execution_correlation_declarative_branch_copies_the_plan():
    plan = types.SimpleNamespace(
        definition_version="3",
        definition_content_hash="sha256:" + "1a" * 32,
        profile_version="5",
        profile_content_hash="sha256:" + "2b" * 32,
        release_revision=12,
    )
    execution = ca.build_execution_correlation(
        resolver_mode="declarative",
        issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
        target_repository_sha="a" * 40,
        plan=plan,
    )
    assert execution.definition_version == "3"
    assert execution.profile_version == "5"
    assert execution.release_revision == 12


def test_build_execution_correlation_declarative_requires_a_plan():
    with pytest.raises(ValueError, match="ExecutionPlan"):
        ca.build_execution_correlation(
            resolver_mode="declarative",
            issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
            target_repository_sha="a" * 40,
            plan=None,
        )


# ---------------------------------------------------------------------------
# assemble_investigator_context — the "off" gate
# ---------------------------------------------------------------------------
def test_assemble_investigator_context_returns_none_when_off(tmp_path):
    result = ca.assemble_investigator_context(
        mode="off",
        issue=_issue(),
        issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
        full_repo="mctlhq/mctl-agents",
        repo_dir=tmp_path / "repo",
        target_repo_sha="a" * 40,
        proposal_dir=tmp_path / "proposal",
        service="mctl-agents",
        slug="issue-265-x",
        prompt_template="PROMPT",
        resolver_mode="legacy",
    )
    assert result is None


def test_assemble_investigator_context_shadow_mode_seals_a_snapshot(tmp_path):
    result = ca.assemble_investigator_context(
        mode="shadow",
        issue=_issue(),
        issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
        full_repo="mctlhq/mctl-agents",
        repo_dir=tmp_path / "repo",
        target_repo_sha="a" * 40,
        proposal_dir=tmp_path / "proposal",
        service="mctl-agents",
        slug="issue-265-x",
        prompt_template="PROMPT",
        resolver_mode="legacy",
        legacy_model="claude-sonnet-5",
        legacy_allowed_tools=("Read", "Write"),
        legacy_budget_usd=8.0,
    )
    assert result is not None
    result.snapshot.validate()  # must not raise
    assert result.mode == "shadow"


# ---------------------------------------------------------------------------
# T13 — import isolation
# ---------------------------------------------------------------------------
def test_module_import_is_stdlib_only():
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.context_assembly, sys; "
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
    assert not leaked, f"orchestrator.context_assembly pulled in third-party modules: {leaked}"


# ---------------------------------------------------------------------------
# T14 — authorization boundary
# ---------------------------------------------------------------------------
_FORBIDDEN_TOKENS = ("allow", "deny", "permit", "grant", "authorized")

# mctlhq/mctl-agents#527 Slice B task 13: the new stdlib-only ladder module
# gets the same coverage as context_assembly.py itself.
_GUARDED_MODULES = ("orchestrator/context_assembly.py", "orchestrator/context_rollout.py")


@pytest.mark.parametrize("relpath", _GUARDED_MODULES)
def test_module_source_has_no_authorization_vocabulary(relpath):
    # Whole-word match, matching test_context_snapshot.py's field-name check
    # in spirit: a bare substring match would false-positive on
    # `allowed_tools` (a legitimate parameter name copied from
    # options.py's tool list, not an authorization decision).
    source = (REPO_ROOT / relpath).read_text(encoding="utf-8").lower()
    for token in _FORBIDDEN_TOKENS:
        assert not re.search(rf"\b{token}\b", source), (
            f"forbidden authorization token {token!r} found in {relpath}"
        )


@pytest.mark.parametrize("relpath", _GUARDED_MODULES)
def test_module_does_not_import_a_policy_or_permission_symbol(relpath):
    source = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "policy" not in stripped.lower()
            assert "permission" not in stripped.lower()


# ---------------------------------------------------------------------------
# #408 round 2 — work_context threads through assemble() into the sealed
# snapshot (the producer-side join the round-1 finding was about).
# ---------------------------------------------------------------------------
def test_assemble_threads_work_context_into_the_sealed_snapshot(tmp_path):
    wc = cs.WorkContextRef(
        work_item_id="wi-1",
        work_item_revision="r1",
        execution_id="e2",
        execution_sequence=2,
        prior_execution_ids=("e1",),
        current_surface="telegram",
        actor_kind="human",
        actor_id="carol",
    )
    assembly_input = _assembly_input(tmp_path)
    with_wc = ca.assemble(assembly_input, mode="shadow", execution=_execution(), work_context=wc)
    without = ca.assemble(assembly_input, mode="shadow", execution=_execution())
    assert with_wc.snapshot.work_context == wc
    assert without.snapshot.work_context is None
    assert with_wc.snapshot.snapshot_id != without.snapshot.snapshot_id


# ---------------------------------------------------------------------------
# mctlhq/mctl-agents#266 T1 — `run_pipeline` extraction. tasks.md task 3:
# `assemble()` becomes a caller of `run_pipeline`, a pure function covering
# both strategy branches plus the shared dedupe/truncate/budget tail. The
# golden-fixture and pinned-id tests above already prove `assemble()`'s
# output is unchanged end to end; these exercise the extracted function
# directly, which is what lets an evaluator run a candidate list through the
# real pipeline with no collector and no clone.
# ---------------------------------------------------------------------------


def _pipeline_candidate(
    source_id: str,
    *,
    kind: str = "github-issue-comment",
    raw: bytes = b"x",
    trust_tier: str = "untrusted",
    max_age_seconds: int | None = 3600,
    observed_at: str = "2026-09-19T00:00:00Z",
    content_time: str | None = None,
) -> ca.CandidateSource:
    return ca.CandidateSource(
        source_id=source_id,
        kind=kind,
        locator=f"locator/{source_id}",
        selector={},
        raw=raw,
        observed_at=observed_at,
        max_age_seconds=max_age_seconds,
        trust_tier=trust_tier,
        trust_rationale="test",
        reason_code="test-candidate",
        content_time=content_time,
    )


_NOW = datetime(2026, 9, 19, 0, 0, 0, tzinfo=UTC)


def test_run_pipeline_default_strategy_drops_stale_and_dedupes():
    candidates = [
        _pipeline_candidate("fresh", raw=b"a"),
        _pipeline_candidate("stale", raw=b"b", observed_at="2026-01-01T00:00:00Z"),
        _pipeline_candidate("dup-1", raw=b"same"),
        _pipeline_candidate("dup-2", raw=b"same"),
    ]
    outcome = ca.run_pipeline(candidates, ca.AssemblyConfig(), _NOW)
    by_id = {c.source_id: c for c in outcome.candidates}
    assert outcome.strategy.name == ca.STRATEGY_NAME
    assert by_id["fresh"].included
    assert not by_id["stale"].included and by_id["stale"].reason_code == "stale"
    assert by_id["dup-1"].included and not by_id["dup-2"].included
    assert by_id["dup-2"].reason_code == "duplicate-content"
    assert outcome.counters.dropped_stale == 1
    assert outcome.counters.dropped_duplicate == 1
    assert outcome.conflicts == []
    assert [c.source_id for c in outcome.candidates] == sorted(by_id, key=lambda i: by_id[i].rank)


def test_run_pipeline_ranked_strategy_detects_conflicts_and_demotes_stale():
    candidates = [
        _pipeline_candidate("issue", kind="github-issue", raw=b"issue"),
        _pipeline_candidate(
            "proposal-dir-requirements.md",
            kind="proposal-dir",
            raw=b"req",
            trust_tier="corroborated",
            max_age_seconds=86400,
            observed_at="2026-09-19T00:00:00Z",
            content_time="2026-09-10T00:00:00Z",
        ),
        _pipeline_candidate(
            "issue-comment-1",
            raw=b"a later comment",
            observed_at="2026-09-19T00:00:00Z",
            content_time="2026-09-15T00:00:00Z",
        ),
        _pipeline_candidate(
            "old", raw=b"old-source", observed_at="2026-01-01T00:00:00Z", content_time="2026-01-01T00:00:00Z"
        ),
    ]
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    outcome = ca.run_pipeline(candidates, config, _NOW)
    assert outcome.strategy.name == ca.RANKED_STRATEGY_NAME
    assert len(outcome.conflicts) == 1
    assert outcome.conflicts[0].subject == ca.CONFLICT_PRIOR_PROPOSAL_SUPERSEDED
    by_id = {c.source_id: c for c in outcome.candidates}
    assert by_id["old"].included and by_id["old"].reason_code == "stale-demoted"
    assert outcome.counters.stale_demoted == 1
    assert outcome.counters.dropped_stale == 0


def test_run_pipeline_applies_the_max_candidates_ceiling_before_the_rest_of_the_pipeline():
    candidates = [_pipeline_candidate(f"c{i}", raw=f"payload-{i}".encode()) for i in range(5)]
    config = ca.AssemblyConfig(max_candidates=3)
    outcome = ca.run_pipeline(candidates, config, _NOW)
    assert [c.source_id for c in outcome.candidates] == ["c0", "c1", "c2"]
    assert outcome.counters.excluded_candidate_ceiling == 2


def test_run_pipeline_leaves_the_ceiling_counter_at_zero_when_under_the_ceiling():
    candidates = [_pipeline_candidate("only", raw=b"payload")]
    outcome = ca.run_pipeline(candidates, ca.AssemblyConfig(max_candidates=3), _NOW)
    assert outcome.counters.excluded_candidate_ceiling == 0


def test_run_pipeline_does_not_mutate_its_input_candidates():
    candidates = [_pipeline_candidate("only", raw=b"payload")]
    outcome = ca.run_pipeline(candidates, ca.AssemblyConfig(), _NOW)
    assert outcome.candidates[0] is not candidates[0]
    assert outcome.candidates[0].content_hash == cs.hash_bytes(b"payload")
    assert candidates[0].content_hash == ""
    assert candidates[0].rank == 0


def test_run_pipeline_can_run_twice_over_the_same_candidates_one_call_per_ordering():
    # ADR 015 sec. 4: every fixture case is run through `run_pipeline` under
    # BOTH orderings, from the same candidate list — this must not let the
    # first call's rewrites (rank, hash, inclusion) leak into the second.
    candidates = [
        _pipeline_candidate("issue", kind="github-issue", raw=b"issue"),
        _pipeline_candidate("dup-1", raw=b"same"),
        _pipeline_candidate("dup-2", raw=b"same"),
        _pipeline_candidate("stale", raw=b"stale-source", observed_at="2026-01-01T00:00:00Z"),
    ]
    default_outcome = ca.run_pipeline(candidates, ca.AssemblyConfig(), _NOW)
    ranked_config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    ranked_outcome = ca.run_pipeline(candidates, ranked_config, _NOW)
    second_default_outcome = ca.run_pipeline(candidates, ca.AssemblyConfig(), _NOW)

    def snapshot(outcome: ca.PipelineOutcome) -> list[tuple[str, bool, str, int]]:
        return [(c.source_id, c.included, c.reason_code, c.rank) for c in outcome.candidates]

    assert snapshot(default_outcome) == snapshot(second_default_outcome)
    assert default_outcome.strategy.name == ca.STRATEGY_NAME
    assert ranked_outcome.strategy.name == ca.RANKED_STRATEGY_NAME
    # The caller's own objects are never touched by either call.
    assert all(c.rank == 0 and c.content_hash == "" for c in candidates)


# ---------------------------------------------------------------------------
# mctlhq/mctl-agents#527 Slice B — context-release selection, the `observe`
# shadow pass, and the two release telemetry lines. T8-T11/T13/T15/T17 map
# onto that proposal's tasks.md "## Tests" section; T7 lives in
# tests/test_context_rollout.py, T12 is the existing isolation tests above
# (unchanged), T14 lives in tests/test_tracing.py.
# ---------------------------------------------------------------------------


def _walk_keys(value):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _walk_keys(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


@pytest.fixture(autouse=True)
def _clean_release_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rollout.ENV_VAR, raising=False)
    monkeypatch.delenv(rollout.REQUIRED_ENV_VAR, raising=False)
    monkeypatch.delenv(ca.STRATEGY_ENV_VAR, raising=False)


# ---------------------------------------------------------------------------
# T8 — break-glass
# ---------------------------------------------------------------------------
def test_enforce_blocks_on_unresolvable_binding_by_default(monkeypatch):
    """`production` (the real default `execution.environment`) has no
    committed binding — the fail-closed default until mctlhq/mctl-agents#528
    creates one."""
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    config = ca.AssemblyConfig()
    with pytest.raises(ca.ContextStrategyNotResolved):
        ca.resolve_strategy_for_run("issue-investigator", config, environment="production")


def test_enforce_with_required_explicitly_true_also_blocks(monkeypatch):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, "true")
    config = ca.AssemblyConfig()
    with pytest.raises(ca.ContextStrategyNotResolved):
        ca.resolve_strategy_for_run("issue-investigator", config, environment="production")


def test_enforce_with_required_false_falls_back_to_the_default_strategy(monkeypatch):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, "false")
    config = ca.AssemblyConfig()
    effective, resolution = ca.resolve_strategy_for_run("issue-investigator", config, environment="production")
    assert effective == ca.STRATEGY_NAME
    assert resolution.mode == rollout.ENFORCE
    assert resolution.reason == ca.RELEASE_REASON_FALLBACK
    assert resolution.verdict in cr.VERDICTS
    assert resolution.bound_strategy is None
    assert resolution.binding_revision is None


# ---------------------------------------------------------------------------
# T9 — observe isolation, the stage's whole safety claim.
# ---------------------------------------------------------------------------
def test_observe_isolation_preserves_authoritative_bytes_environment_and_rendered(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_ENVIRONMENT", raising=False)
    monkeypatch.setenv(ca.STRATEGY_ENV_VAR, "trust-freshness-ranked")
    proposal_dir = tmp_path / "proposal"

    def _run():
        return ca.assemble_investigator_context(
            mode="shadow",
            issue=_issue(),
            issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
            full_repo="mctlhq/mctl-agents",
            repo_dir=tmp_path / "repo",
            target_repo_sha="a" * 40,
            proposal_dir=proposal_dir,
            service="mctl-agents",
            slug="issue-265-x",
            prompt_template="PROMPT",
            resolver_mode="legacy",
            now=NOW,
        )

    monkeypatch.delenv(rollout.ENV_VAR, raising=False)
    baseline = _run()

    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    observed = _run()

    assert observed.snapshot.content_hash == baseline.snapshot.content_hash
    assert observed.snapshot.snapshot_id == baseline.snapshot.snapshot_id
    assert observed.rendered == baseline.rendered
    assert observed.snapshot.execution.environment == "production"
    assert observed.metrics.release_mode == rollout.OBSERVE
    assert observed.metrics.binding_revision is not None
    assert observed.metrics.strategy_name == "trust-freshness-ranked"


def test_observe_persists_the_snapshot_exactly_once_per_run(tmp_path, monkeypatch):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    monkeypatch.setenv(ca.STRATEGY_ENV_VAR, "trust-freshness-ranked")
    calls: list[cs.ContextSnapshot] = []
    monkeypatch.setattr(ca, "_persist_to_work_item_store", lambda snapshot, client: calls.append(snapshot))

    ca.assemble_investigator_context(
        mode="shadow",
        issue=_issue(),
        issue_url="https://github.com/mctlhq/mctl-agents/issues/265",
        full_repo="mctlhq/mctl-agents",
        repo_dir=tmp_path / "repo",
        target_repo_sha="a" * 40,
        proposal_dir=tmp_path / "proposal",
        service="mctl-agents",
        slug="issue-265-x",
        prompt_template="PROMPT",
        resolver_mode="legacy",
    )
    assert len(calls) == 1


def test_observe_shadow_pass_failure_logs_observe_pass_failed_and_the_run_still_succeeds(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("AGENT_ENVIRONMENT", raising=False)
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    assembly_input = _assembly_input(tmp_path, config=config)
    execution = _execution()

    real_run_pipeline = ca.run_pipeline
    calls = {"n": 0}

    def flaky_run_pipeline(candidates, cfg, now):
        calls["n"] += 1
        if calls["n"] == 2:  # the shadow pass
            raise RuntimeError("boom")
        return real_run_pipeline(candidates, cfg, now)

    monkeypatch.setattr(ca, "run_pipeline", flaky_run_pipeline)
    result = ca.assemble(assembly_input, mode="shadow", execution=execution)

    result.snapshot.validate()  # the authoritative pass completed successfully
    assert result.metrics.strategy_name == ca.RANKED_STRATEGY_NAME

    out = capsys.readouterr().out
    release_lines = [line for line in out.splitlines() if line.startswith("CONTEXT_STRATEGY_RELEASE ")]
    assert len(release_lines) == 1
    parsed = json.loads(release_lines[0].split(" ", 1)[1])
    assert parsed["reason"] == ca.RELEASE_REASON_OBSERVE_FAILED

    compare_lines = [line for line in out.splitlines() if line.startswith("CONTEXT_STRATEGY_COMPARE ")]
    assert compare_lines == []  # no candidate snapshot_id was produced


# ---------------------------------------------------------------------------
# T10 — telemetry safety
# ---------------------------------------------------------------------------
def test_release_and_compare_lines_never_carry_a_locator_selector_or_payload(tmp_path, monkeypatch, capsys):
    marker = "CONTEXT-LEAK-CANARY"
    comments = (("c1", "alice", "2026-09-18T00:00:00Z", marker),)
    monkeypatch.delenv("AGENT_ENVIRONMENT", raising=False)
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    assembly_input = _assembly_input(tmp_path, issue=_issue(comments=comments), config=config)
    result = ca.assemble(assembly_input, mode="shadow", execution=_execution())

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    release_lines = [line for line in lines if line.startswith("CONTEXT_STRATEGY_RELEASE ")]
    compare_lines = [line for line in lines if line.startswith("CONTEXT_STRATEGY_COMPARE ")]
    assert len(release_lines) == 1
    assert len(compare_lines) == 1

    for prefix, group in (("CONTEXT_STRATEGY_RELEASE ", release_lines), ("CONTEXT_STRATEGY_COMPARE ", compare_lines)):
        for line in group:
            assert "\n" not in line
            assert marker not in line
            payload = line[len(prefix):]
            parsed = json.loads(payload)
            assert json.dumps(parsed, sort_keys=True) == payload
            for key in _walk_keys(parsed):
                assert "locator" not in key.lower()
                assert "selector" not in key.lower()
            for source in result.snapshot.sources:
                assert source.locator not in payload
                assert json.dumps(dict(source.selector)) not in payload


# ---------------------------------------------------------------------------
# T11 — delegation to mctlhq/mctl-agents#526
# ---------------------------------------------------------------------------
_FORBIDDEN_EVALUATION_TOKENS = ("delta", "ratio", "score_diff", "better", "winner")


def test_release_telemetry_does_not_reimplement_evaluation_semantics():
    source = (REPO_ROOT / "orchestrator" / "context_assembly.py").read_text(encoding="utf-8")
    start = source.index("def _emit_release_verdict")
    end = source.index("def assemble(")
    emitters_source = source[start:end].lower()
    for token in _FORBIDDEN_EVALUATION_TOKENS:
        assert token not in emitters_source, f"forbidden evaluation token {token!r} found in release telemetry"
    assert "assemblymetrics" not in emitters_source.replace("_", "")
    assert "pipelinecounters" not in emitters_source.replace("_", "")


def test_compare_line_key_set_is_exactly_the_eight_names_task_9_lists(capsys):
    resolution = ca.StrategyResolution(
        mode=rollout.OBSERVE,
        reason=ca.RELEASE_REASON_OBSERVE,
        bound_strategy="deterministic-fixed-order",
        bound_version="1.0.0",
        binding_revision=2,
        strategy_content_hash="sha256:" + "0" * 64,
        override_active=True,
        verdict=cr.VERDICT_OK,
    )
    ca._emit_strategy_compare(
        resolution,
        authoritative_strategy="trust-freshness-ranked",
        authoritative_version="1.0.0",
        authoritative_snapshot_id="cs-authoritative",
        bound_snapshot_id="cs-bound",
    )
    out = capsys.readouterr().out.strip()
    prefix, _, payload = out.partition(" ")
    assert prefix == "CONTEXT_STRATEGY_COMPARE"
    parsed = json.loads(payload)
    assert set(parsed) == {
        "mode",
        "authoritative_strategy",
        "authoritative_version",
        "authoritative_snapshot_id",
        "bound_strategy",
        "bound_version",
        "bound_snapshot_id",
        "binding_revision",
    }


# ---------------------------------------------------------------------------
# T13 — enforce substitution reaches the collectors, not only the pipeline
# ---------------------------------------------------------------------------
def test_enforce_substitution_reaches_the_collectors_not_only_the_pipeline(tmp_path, monkeypatch):
    """`collect_prior_proposal` (`:821`) reads `assembly_input.config.ranked`;
    an `enforce` run bound to `trust-freshness-ranked` (via a stubbed
    `context_release.resolve` — no `trust-freshness-ranked` binding is
    committed to the catalog) must seal byte-identical to a run configured
    with that strategy directly, proving the substitution happened before
    the collector loop and not only before `run_pipeline`."""
    proposal_dir = tmp_path / "proposal"
    proposal_dir.mkdir()
    (proposal_dir / "requirements.md").write_text("req")
    (proposal_dir / ".status.yaml").write_text("updated_at: '2026-09-10T00:00:00Z'\n")

    fake_resolved = cr.ResolvedContextStrategy(
        agent="issue-investigator",
        environment="production",
        strategy=ca.RANKED_STRATEGY_NAME,
        version=ca.RANKED_STRATEGY_VERSION,
        ranker_name=ca.RANKER_NAME,
        ranker_version=ca.RANKER_VERSION,
        content_hash="sha256:" + "0" * 64,
        implementation_hash="sha256:" + "1" * 64,
        release_revision=99,
        verdict=cr.VERDICT_OK,
    )
    monkeypatch.setattr(cr, "resolve", lambda agent, environment: fake_resolved)
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)

    default_config = ca.AssemblyConfig()  # deterministic — the binding must override it
    assembly_input = _assembly_input(tmp_path, config=default_config, proposal_dir=proposal_dir)
    execution = _execution(environment="production")
    enforced = ca.assemble(assembly_input, mode="shadow", execution=execution)

    monkeypatch.delenv(rollout.ENV_VAR, raising=False)
    ranked_config = ca.AssemblyConfig(strategy=ca.RANKED_STRATEGY_NAME)
    assembly_input_ranked = _assembly_input(tmp_path, config=ranked_config, proposal_dir=proposal_dir)
    baseline = ca.assemble(assembly_input_ranked, mode="shadow", execution=execution)

    assert enforced.snapshot.content_hash == baseline.snapshot.content_hash
    assert enforced.snapshot.snapshot_id == baseline.snapshot.snapshot_id


# ---------------------------------------------------------------------------
# T15 — metrics shape at off
# ---------------------------------------------------------------------------
def test_metrics_shape_at_off_carries_off_safe_defaults(tmp_path):
    assembly_input = _assembly_input(tmp_path)
    result = _assemble(assembly_input)

    assert result.metrics.release_mode == "off"
    assert result.metrics.binding_revision is None
    assert result.metrics.strategy_content_hash is None
    assert result.metrics.override_active is False

    log_dict = result.metrics.to_log_dict()
    assert log_dict["release_mode"] == "off"
    assert log_dict["binding_revision"] is None
    assert log_dict["strategy_content_hash"] is None
    assert log_dict["override_active"] is False

    pre_existing_keys = {
        "mode", "candidates_by_kind", "included_by_kind", "candidates_total",
        "candidates_dropped_pre_budget", "dropped_stale", "dropped_duplicate",
        "excluded_budget", "truncated_sources", "used_sources", "used_bytes",
        "assembly_latency_ms", "collector_calls", "strategy_name", "strategy_version",
        "stale_demoted", "conflict_count", "conflict_sources_capped", "snapshot",
    }
    assert pre_existing_keys <= set(log_dict)


# ---------------------------------------------------------------------------
# T17 — observe always resolves `shadow`; enforce passes the environment
# through unchanged.
# ---------------------------------------------------------------------------
def test_observe_always_resolves_shadow_regardless_of_execution_environment(monkeypatch):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    seen: list[str] = []

    def fake_resolve(agent, environment):
        seen.append(environment)
        raise cr.ContextReleaseError(cr.VERDICT_UNKNOWN, "stub")

    monkeypatch.setattr(cr, "resolve", fake_resolve)
    config = ca.AssemblyConfig()
    for env in ("production", "staging", None):
        ca.resolve_strategy_for_run("issue-investigator", config, environment=env)
    assert seen == ["shadow", "shadow", "shadow"]


def test_enforce_passes_the_given_environment_through_unchanged(monkeypatch):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, "false")  # avoid raising so the loop completes
    seen: list[str] = []

    def fake_resolve(agent, environment):
        seen.append(environment)
        raise cr.ContextReleaseError(cr.VERDICT_UNKNOWN, "stub")

    monkeypatch.setattr(cr, "resolve", fake_resolve)
    config = ca.AssemblyConfig()
    for env in ("production", "staging"):
        ca.resolve_strategy_for_run("issue-investigator", config, environment=env)
    assert seen == ["production", "staging"]

    monkeypatch.delenv("AGENT_ENVIRONMENT", raising=False)
    ca.resolve_strategy_for_run("issue-investigator", config, environment=None)
    assert seen[-1] == "production"
