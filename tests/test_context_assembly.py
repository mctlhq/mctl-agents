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


def test_module_source_has_no_authorization_vocabulary():
    # Whole-word match, matching test_context_snapshot.py's field-name check
    # in spirit: a bare substring match would false-positive on
    # `allowed_tools` (a legitimate parameter name copied from
    # options.py's tool list, not an authorization decision).
    source = (REPO_ROOT / "orchestrator" / "context_assembly.py").read_text(encoding="utf-8").lower()
    for token in _FORBIDDEN_TOKENS:
        assert not re.search(rf"\b{token}\b", source), (
            f"forbidden authorization token {token!r} found in context_assembly.py"
        )


def test_module_does_not_import_a_policy_or_permission_symbol():
    source = (REPO_ROOT / "orchestrator" / "context_assembly.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            assert "policy" not in stripped.lower()
            assert "permission" not in stripped.lower()
