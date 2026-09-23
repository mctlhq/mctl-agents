"""Assembles the issue-investigator's `ContextSnapshot` from real, fetched
sources (mctlhq/mctl-agents#265, ADR 009's follow-up row (a):
docs/adr/009-context-snapshot-contract.md).

`orchestrator/context_snapshot.py` is the frozen, inert contract — "no
retrieval, no ranking, no I/O". This module is the other half: collect
candidates from a fixed, declared set of collectors (the
`deterministic-fixed-order` strategy), normalize + hash them with
`context_snapshot`'s own rule, classify freshness, deduplicate, truncate
oversized sources, apply a source/byte budget, and call `seal()`.

A second, opt-in strategy, `trust-freshness-ranked` (mctlhq/mctl-agents#471,
ADR 009 amendment 1), selected by `ISSUE_INVESTIGATOR_CONTEXT_STRATEGY`:
the pinned primaries (inline template, issue, target repo) first, then every
other source ordered by trust tier, then freshness, then recency, with the
ranker's identity and each source's `Selection.score` recorded. Stale sources
are demoted and flagged rather than dropped, a prior proposal is aged by its
own `.status.yaml` `updated_at`, and a fixed rule records conflicting
evidence in the snapshot's `conflicts` block. The default strategy is
untouched: its snapshots keep their bytes and their `snapshot_id`s.

Stdlib only, deliberately, mirroring `context_snapshot.py`: imports from
`orchestrator.context_snapshot` and `orchestrator.temporal.issue_ref` only
(both stdlib-only themselves), so this module stays importable by whatever
imports the issue-investigator's pure helpers without pulling in
`claude_agent_sdk` (`tests/test_worker_isolation.py`,
`tests/test_context_snapshot.py`'s own isolation test).

This module never decides whether an action is permitted. It records
provenance and freshness only — see ADR 009 sec. 5, "context relevance is
never authorization".
"""
from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.context_snapshot import (
    ContextBudget,
    ContextConflict,
    ContextSnapshot,
    ContextSource,
    ContextStrategy,
    ExecutionCorrelation,
    Freshness,
    RetentionPolicy,
    Selection,
    Trust,
    WorkContextRef,
    canonical_json,
    hash_bytes,
    seal,
)
from orchestrator.temporal.issue_ref import loop_workflow_id

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from orchestrator.run_issue_investigator import IssueData

STRATEGY_NAME = "deterministic-fixed-order"
STRATEGY_VERSION = "1.0.0"

# mctlhq/mctl-agents#471: the opt-in ranked strategy and its ranker. The
# strategy names the whole assembly procedure; the ranker names the ordering
# function inside it, so either can move version independently (ADR 009
# sec. 1, `ContextStrategy`).
RANKED_STRATEGY_NAME = "trust-freshness-ranked"
RANKED_STRATEGY_VERSION = "1.0.0"
RANKER_NAME = "trust-freshness-recency"
RANKER_VERSION = "1.0.0"

STRATEGY_ENV_VAR = "ISSUE_INVESTIGATOR_CONTEXT_STRATEGY"
STRATEGIES = (STRATEGY_NAME, RANKED_STRATEGY_NAME)

# The ranked strategy keeps these kinds first, in collector order: the
# scaffold, the problem statement and the tree the model explores. Ranking
# only ever reorders what comes after them.
_PINNED_KINDS = frozenset({"inline-template", "github-issue", "target-repo"})

# Lower sorts first. Trust is origin-only and grants nothing (ADR 009 sec.
# 5/6): it orders what the model reads, never what anyone may do.
_TRUST_ORDER = {"authoritative": 0, "corroborated": 1, "reported": 2, "untrusted": 3}
_FRESHNESS_ORDER = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}
_PINNED_SCORE = 100.0

# The one fixed conflict rule (mctlhq/mctl-agents#471) and what the ranked
# strategy does about it: keep every source, ordered — never drop one.
CONFLICT_PRIOR_PROPOSAL_SUPERSEDED = "prior-proposal-superseded-by-later-comment"
CONFLICT_RESOLUTION_KEPT_RANKED = "kept-all-ranked-by-trust-then-recency"

# A `.status.yaml` is a few hundred bytes; read at most this much of it.
_STATUS_READ_CEILING = 8192

# The one legacy AgentDefinition file `build_execution_correlation`'s legacy
# branch hashes for `definition_content_hash` (see its docstring).
_LEGACY_DEFINITION_PATH = (
    Path(__file__).resolve().parent.parent / "agents" / "_manifests" / "issue-investigator" / "agent.yaml"
)

# Content-addressed kinds are pinned by something other than a wall-clock
# staleness window (a git SHA, an in-process hash) — they classify `fresh`
# explicitly rather than falling into the `unknown` fail-safe (ADR 009 sec. 6).
_CONTENT_ADDRESSED_KINDS = frozenset({"target-repo", "inline-template"})

# Per-kind freshness table (requirements.md "Freshness, deduplication,
# truncation, budget"): None means content-addressed, handled above.
_DEFAULT_FRESHNESS_TABLE: dict[str, int | None] = {
    "github-issue": 3600,
    "github-issue-comment": 3600,
    "proposal-dir": 86400,
    "target-repo": None,
    "inline-template": None,
}

# requirements.md's "Comment ceiling" open question: a deterministic ceiling
# of the 20 most recent comments, counted in metrics as a pre-budget drop.
_DEFAULT_MAX_COMMENTS = 20

# A ceiling on the candidate list itself, independent of the source/byte
# budget below — a 500-comment issue must not produce a 500-entry snapshot.
_DEFAULT_MAX_CANDIDATES = 100

TRIPLET_FILENAMES = ("requirements.md", "design.md", "tasks.md")


def _iso(dt: datetime) -> str:
    """RFC 3339 UTC timestamp without microseconds — matches
    `run_issue_investigator._now_iso`."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Data model (tasks.md #2)
# ---------------------------------------------------------------------------


@dataclass
class CandidateSource:
    """A source under consideration, in-process only. Holds the raw bytes
    that will be hashed into `ContextSource.content_hash` — unlike
    `ContextSource` itself, which never carries a payload field, this is a
    mutable scratch object the pipeline stages below rewrite in place
    (rank, inclusion, hash, byte count) as it moves through the pipeline.

    `render_text`, when set, is the human-readable text a collector wants
    rendered into the `on`-mode prompt (`AssemblyResult.rendered`) — kept
    separate from `raw` (which may be a JSON envelope of `raw`'s fields)
    so hashing and rendering can differ without a second hash convention.
    """

    source_id: str
    kind: str
    locator: str
    selector: dict[str, Any]
    raw: bytes
    observed_at: str
    max_age_seconds: int | None
    trust_tier: str
    trust_rationale: str
    reason_code: str
    strategy_step: str | None = None
    render_text: str | None = None
    # mctlhq/mctl-agents#471 (ranked strategy only reads these). When the
    # source's content carries its own time — a comment's `created_at`, a
    # prior proposal's `updated_at` — `content_time` is it, and recency
    # orders by it. `retrieved_at`, when set, is the retrieval moment for a
    # source whose `observed_at` is its content time instead; unset means
    # the two are the same moment, as for every default-strategy source.
    content_time: str | None = None
    retrieved_at: str | None = None
    score: float | None = None
    rank: int = 0
    included: bool = True
    content_hash: str = ""
    byte_count: int = 0
    freshness_staleness: str = "unknown"
    truncated: bool = False


Collector = Callable[["AssemblyInput"], list[CandidateSource]]


@dataclass(frozen=True)
class AssemblyConfig:
    """The assembly budget and candidate ceilings. `max_sources`/
    `max_bytes`/`max_bytes_per_source` are overridable by env (requirements.md
    "Default budget numbers" open question); `max_candidates`/`max_comments`
    are deterministic constants (requirements.md "Comment ceiling" open
    question) — nothing today asks for those to move independently of a code
    change."""

    max_sources: int = 12
    max_bytes: int = 120_000
    max_bytes_per_source: int = 50_000
    max_candidates: int = _DEFAULT_MAX_CANDIDATES
    max_comments: int = _DEFAULT_MAX_COMMENTS
    freshness_table: Mapping[str, int | None] = field(
        default_factory=lambda: dict(_DEFAULT_FRESHNESS_TABLE)
    )
    strategy: str = STRATEGY_NAME

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"{STRATEGY_ENV_VAR} must be one of {STRATEGIES}, got {self.strategy!r}")

    @property
    def ranked(self) -> bool:
        return self.strategy == RANKED_STRATEGY_NAME

    @classmethod
    def from_env(cls) -> AssemblyConfig:
        return cls(
            strategy=os.getenv(STRATEGY_ENV_VAR, STRATEGY_NAME).strip().lower() or STRATEGY_NAME,
            max_sources=int(os.getenv("ISSUE_INVESTIGATOR_CONTEXT_MAX_SOURCES", "12")),
            max_bytes=int(os.getenv("ISSUE_INVESTIGATOR_CONTEXT_MAX_BYTES", "120000")),
            max_bytes_per_source=int(
                os.getenv("ISSUE_INVESTIGATOR_CONTEXT_MAX_BYTES_PER_SOURCE", "50000")
            ),
        )


@dataclass(frozen=True)
class AssemblyInput:
    """Everything a collector needs, already fetched or cheaply derivable —
    no collector performs a network call other than through the existing
    `gh` path (the issue + its comments are already on `issue`)."""

    issue: IssueData
    repo_dir: Path
    target_repo_sha: str
    full_repo: str
    proposal_dir: Path
    service: str
    slug: str
    prompt_template: str
    now: datetime
    config: AssemblyConfig


@dataclass
class AssemblyMetrics:
    """One structured line's worth of counters (requirements.md's
    "Correlation, metrics, telemetry safety" criterion). Never carries a
    `locator`, `selector`, or payload byte — `to_log_dict()` is the only
    thing this module ever prints."""

    mode: str
    candidates_by_kind: dict[str, int]
    included_by_kind: dict[str, int]
    candidates_total: int
    candidates_dropped_pre_budget: int
    dropped_stale: int
    dropped_duplicate: int
    excluded_budget: int
    truncated_sources: int
    used_sources: int
    used_bytes: int
    assembly_latency_ms: float
    collector_calls: int
    strategy_name: str
    strategy_version: str
    snapshot: ContextSnapshot
    stale_demoted: int = 0
    conflict_count: int = 0

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "candidates_by_kind": dict(self.candidates_by_kind),
            "included_by_kind": dict(self.included_by_kind),
            "candidates_total": self.candidates_total,
            "candidates_dropped_pre_budget": self.candidates_dropped_pre_budget,
            "dropped_stale": self.dropped_stale,
            "dropped_duplicate": self.dropped_duplicate,
            "excluded_budget": self.excluded_budget,
            "truncated_sources": self.truncated_sources,
            "used_sources": self.used_sources,
            "used_bytes": self.used_bytes,
            "assembly_latency_ms": self.assembly_latency_ms,
            "collector_calls": self.collector_calls,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "stale_demoted": self.stale_demoted,
            "conflict_count": self.conflict_count,
            "snapshot": self.snapshot.to_log_dict(),
        }


@dataclass(frozen=True)
class AssemblyResult:
    """The output of one assembly run. `snapshot` is payload-free by
    construction; `rendered` is the ONLY place any payload text lives, and
    it is consumed exclusively by `_build_prompt` in `on` mode — no
    function in this module ever writes `rendered` to a log."""

    mode: str
    snapshot: ContextSnapshot
    rendered: Mapping[str, str]
    metrics: AssemblyMetrics


def _to_context_source(candidate: CandidateSource) -> ContextSource:
    return ContextSource(
        source_id=candidate.source_id,
        kind=candidate.kind,
        locator=candidate.locator,
        selector=candidate.selector,
        content_hash=candidate.content_hash,
        byte_count=candidate.byte_count,
        retrieved_at=candidate.retrieved_at or candidate.observed_at,
        freshness=Freshness(
            observed_at=candidate.observed_at,
            staleness=candidate.freshness_staleness,
            max_age_seconds=candidate.max_age_seconds,
        ),
        trust=Trust(tier=candidate.trust_tier, rationale_code=candidate.trust_rationale),
        selection=Selection(
            rank=candidate.rank,
            reason_code=candidate.reason_code,
            included=candidate.included,
            score=candidate.score,
            strategy_step=candidate.strategy_step,
        ),
    )


# ---------------------------------------------------------------------------
# Deterministic pipeline stages (tasks.md #3)
# ---------------------------------------------------------------------------


def assign_ranks(candidates: Sequence[CandidateSource]) -> None:
    """1-based rank, in candidate-list order, assigned once before any
    filtering and never renumbered — a rank is a stable statement of
    position, and every later stage only flips `included`/`reason_code`."""
    for index, candidate in enumerate(candidates, start=1):
        candidate.rank = index


def normalize(candidate: CandidateSource) -> None:
    """UTF-8 bytes are already on `candidate.raw`; hash them with the one
    rule `context_snapshot` defines (ADR 009's "a second, disagreeing hash
    convention" risk)."""
    candidate.content_hash = hash_bytes(candidate.raw)
    candidate.byte_count = len(candidate.raw)


def classify_freshness(candidate: CandidateSource, now: datetime) -> None:
    """`fresh` while age <= max/2, `aging` while age <= max, `stale` beyond.
    Content-addressed kinds (no `max_age_seconds`) are explicitly `fresh`;
    anything else with no declared `max_age_seconds` fails safe to
    `unknown` — never `fresh` by default (ADR 009 sec. 6)."""
    if candidate.max_age_seconds is None:
        candidate.freshness_staleness = "fresh" if candidate.kind in _CONTENT_ADDRESSED_KINDS else "unknown"
        return
    age_seconds = (now - _parse_iso(candidate.observed_at)).total_seconds()
    if age_seconds <= candidate.max_age_seconds / 2:
        candidate.freshness_staleness = "fresh"
    elif age_seconds <= candidate.max_age_seconds:
        candidate.freshness_staleness = "aging"
    else:
        candidate.freshness_staleness = "stale"


def deduplicate(candidates: Sequence[CandidateSource]) -> int:
    """Group by `content_hash` among still-included candidates; the lowest
    rank wins, the rest are marked `included=False,
    reason_code="duplicate-content"`. Returns the number dropped."""
    seen: dict[str, CandidateSource] = {}
    dropped = 0
    for candidate in sorted((c for c in candidates if c.included), key=lambda c: c.rank):
        if candidate.content_hash not in seen:
            seen[candidate.content_hash] = candidate
            continue
        candidate.included = False
        candidate.reason_code = "duplicate-content"
        dropped += 1
    return dropped


def truncate_to_per_source_limit(candidate: CandidateSource, max_bytes_per_source: int) -> bool:
    """Cut bytes to the limit BEFORE re-hashing, so `content_hash` describes
    what the model actually saw. Returns True when truncation happened.

    `render_text`, when set, is what actually reaches the `on`-mode prompt
    (`AssemblyResult.rendered`) — cut it to the same byte ceiling too, or a
    sealed snapshot recording `byte_count=max_bytes_per_source` would still
    let the untruncated text past `truncate_to_per_source_limit` reach the
    model, misdescribing what it actually saw."""
    if len(candidate.raw) <= max_bytes_per_source:
        return False
    candidate.raw = candidate.raw[:max_bytes_per_source]
    candidate.content_hash = hash_bytes(candidate.raw)
    candidate.byte_count = len(candidate.raw)
    candidate.selector = {**candidate.selector, "byte_range": [0, candidate.byte_count]}
    candidate.truncated = True
    if candidate.render_text is not None:
        candidate.render_text = candidate.render_text.encode("utf-8")[:max_bytes_per_source].decode(
            "utf-8", errors="ignore"
        )
    return True


def apply_budget(candidates: Sequence[CandidateSource], config: AssemblyConfig) -> ContextBudget:
    """Walk still-included candidates in ascending rank; from the first one
    that does not fit within `max_sources`/`max_bytes`, mark it and every
    higher-ranked (later) included candidate `included=False,
    reason_code="budget-exhausted"`. Stopping at the first miss, rather than
    opportunistically fitting a smaller later source, keeps the rule one
    sentence long and reproducible."""
    used_sources = 0
    used_bytes = 0
    budget_hit = False
    for candidate in sorted((c for c in candidates if c.included), key=lambda c: c.rank):
        if budget_hit:
            candidate.included = False
            candidate.reason_code = "budget-exhausted"
            continue
        if used_sources + 1 > config.max_sources or used_bytes + candidate.byte_count > config.max_bytes:
            candidate.included = False
            candidate.reason_code = "budget-exhausted"
            budget_hit = True
            continue
        used_sources += 1
        used_bytes += candidate.byte_count
    return ContextBudget(
        max_sources=config.max_sources,
        max_bytes=config.max_bytes,
        max_bytes_per_source=config.max_bytes_per_source,
        used_sources=used_sources,
        used_bytes=used_bytes,
        truncated=budget_hit or any(c.truncated for c in candidates),
    )


# ---------------------------------------------------------------------------
# `trust-freshness-ranked` stages (mctlhq/mctl-agents#471)
# ---------------------------------------------------------------------------


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return _parse_iso(value).timestamp()
    except ValueError:
        return None


def _recency(candidate: CandidateSource) -> float | None:
    return _epoch(candidate.content_time or candidate.observed_at)


def ranking_score(candidate: CandidateSource) -> float:
    """The score recorded in `Selection.score`: pinned kinds outrank
    everything; otherwise ten points per trust step and one per freshness
    step, so trust always dominates freshness — the same precedence the
    sort key in `rank_candidates` applies (recency only breaks ties, and so
    is not part of the score)."""
    if candidate.kind in _PINNED_KINDS:
        return _PINNED_SCORE
    trust = len(_TRUST_ORDER) - 1 - _TRUST_ORDER[candidate.trust_tier]
    freshness = len(_FRESHNESS_ORDER) - 1 - _FRESHNESS_ORDER[candidate.freshness_staleness]
    return float(10 * trust + freshness)


def rank_candidates(candidates: Sequence[CandidateSource]) -> list[CandidateSource]:
    """Order for the ranked strategy and assign 1-based ranks and scores.

    Pinned kinds keep their collector order at the top. Every other
    candidate follows, ordered by trust tier, then freshness (so a stale
    source is demoted below a fresher one of the same tier), then recency
    (newest first; a source with no readable time sorts last), then
    collector order — a total order, so the result is deterministic. Must
    run after `classify_freshness`."""
    indexed = list(enumerate(candidates))
    pinned = [c for _, c in indexed if c.kind in _PINNED_KINDS]

    def key(item: tuple[int, CandidateSource]) -> tuple[int, int, float, int]:
        index, candidate = item
        recency = _recency(candidate)
        return (
            _TRUST_ORDER[candidate.trust_tier],
            _FRESHNESS_ORDER[candidate.freshness_staleness],
            -recency if recency is not None else float("inf"),
            index,
        )

    rest = [c for _, c in sorted((i for i in indexed if i[1].kind not in _PINNED_KINDS), key=key)]
    ordered = pinned + rest
    for rank, candidate in enumerate(ordered, start=1):
        candidate.rank = rank
        candidate.score = ranking_score(candidate)
    return ordered


def flag_stale(candidates: Sequence[CandidateSource]) -> int:
    """Ranked strategy: a stale source stays included — `rank_candidates`
    has already demoted it — and is flagged `reason_code="stale-demoted"`,
    so its staleness is visible in the snapshot instead of silently
    removing it. Returns the number flagged."""
    flagged = 0
    for candidate in candidates:
        if candidate.included and candidate.freshness_staleness == "stale":
            candidate.reason_code = "stale-demoted"
            flagged += 1
    return flagged


def detect_conflicts(candidates: Sequence[CandidateSource]) -> list[ContextConflict]:
    """The one fixed conflict rule — no model, no text comparison: a prior
    proposal whose `.status.yaml` `updated_at` (its `content_time`) predates
    a later issue comment was written without that comment, so the comment
    supersedes it. Every source involved is kept and named, in rank order;
    nothing is dropped. A proposal with no readable `updated_at` cannot be
    dated and so never fires the rule."""
    proposals = [c for c in candidates if c.kind == "proposal-dir" and _epoch(c.content_time) is not None]
    if not proposals:
        return []
    written = max(_epoch(c.content_time) or 0.0 for c in proposals)
    later = [
        c for c in candidates
        if c.kind == "github-issue-comment" and (_epoch(c.content_time) or float("-inf")) > written
    ]
    if not later:
        return []
    involved = sorted(proposals + later, key=lambda c: c.rank)
    return [
        ContextConflict(
            subject=CONFLICT_PRIOR_PROPOSAL_SUPERSEDED,
            source_ids=tuple(c.source_id for c in involved),
            resolution_code=CONFLICT_RESOLUTION_KEPT_RANKED,
        )
    ]


_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def render_conflict_notice(snapshot: ContextSnapshot) -> str:
    """The `on`-mode prompt notice for the snapshot's recorded conflicts —
    `""` when there are none, so a conflict-free prompt is unchanged. Built
    from source ids and codes only (never payload); an id that is not a
    plain token is left out rather than rendered."""
    if not snapshot.conflicts:
        return ""
    lines = []
    for conflict in snapshot.conflicts:
        ids = ", ".join(i for i in conflict.source_ids if _SAFE_SOURCE_ID.match(i))
        if conflict.subject == CONFLICT_PRIOR_PROPOSAL_SUPERSEDED:
            lines.append(
                f"- The prior proposal was written before later issue comments ({ids}). "
                "Where they disagree, the prior proposal may be out of date: check it "
                "against the later comments instead of carrying it forward unchanged."
            )
        else:
            lines.append(f"- {conflict.subject}: {ids}")
    return (
        "\n\n### Conflicting evidence\n\n"
        "A fixed rule found these sources in conflict. All of them are kept above, "
        "ordered by trust tier and then recency:\n\n" + "\n".join(lines) + "\n"
    )


# ---------------------------------------------------------------------------
# Collectors (tasks.md #4) — five heterogeneous kinds, fixed order.
# loki-logs/incident are out of scope (requirements.md): reachable today
# only through mcp__mctl__* tools the MODEL calls, not this Python wrapper.
# ---------------------------------------------------------------------------


def collect_inline_template(assembly_input: AssemblyInput) -> list[CandidateSource]:
    """The un-interpolated `_build_prompt` scaffold — content-addressed, so
    it needs no freshness window at all."""
    raw = assembly_input.prompt_template.encode("utf-8")
    return [
        CandidateSource(
            source_id="inline-template",
            kind="inline-template",
            locator="orchestrator/run_issue_investigator.py:_build_prompt",
            selector={"mode": "content-addressed"},
            raw=raw,
            observed_at=_iso(assembly_input.now),
            max_age_seconds=assembly_input.config.freshness_table.get("inline-template"),
            trust_tier="authoritative",
            trust_rationale="in-process-prompt-scaffold",
            reason_code="prompt-scaffold",
            strategy_step="primary",
        )
    ]


_ISSUE_JSON_FIELDS = "number,title,body,state,url,comments"


def collect_github_issue(assembly_input: AssemblyInput) -> list[CandidateSource]:
    """The fields `gh_issue_view` fetches in its one `gh issue view --json`
    call — untrusted, third-party text (ADR 009 sec. 8)."""
    issue = assembly_input.issue
    payload = {
        "number": issue.ref.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "url": issue.ref.url,
    }
    raw = canonical_json(payload)
    return [
        CandidateSource(
            source_id="issue",
            kind="github-issue",
            locator=issue.ref.url,
            selector={"fields": _ISSUE_JSON_FIELDS},
            raw=raw,
            observed_at=_iso(assembly_input.now),
            max_age_seconds=assembly_input.config.freshness_table.get("github-issue"),
            trust_tier="untrusted",
            trust_rationale="github-issue-body-third-party-text",
            reason_code="primary-problem-statement",
            strategy_step="primary",
        )
    ]


def collect_issue_comments(assembly_input: AssemblyInput) -> list[CandidateSource]:
    """One source per comment, ordered by comment id ascending, newest
    `max_comments` retained — riding the same `gh issue view --json` call
    as `collect_github_issue`, zero extra API calls. Comments beyond the
    ceiling never become candidates at all; the caller counts that drop in
    metrics rather than hiding it.

    Sorted by `created_at`, not by GitHub's own comment `id`: `gh issue
    view --json comments` reports `id` as an opaque GraphQL node id, not a
    numeric, lexicographically-ordered value, while `created_at` is an
    ISO-8601 string that sorts correctly as text."""
    comments = sorted(assembly_input.issue.comments, key=lambda c: c[2])
    ceiling = assembly_input.config.max_comments
    kept = comments[-ceiling:] if ceiling > 0 else comments
    max_age = assembly_input.config.freshness_table.get("github-issue-comment")
    candidates = []
    for comment_id, author, created_at, body in kept:
        payload = {"id": comment_id, "author": author, "created_at": created_at, "body": body}
        candidates.append(
            CandidateSource(
                source_id=f"issue-comment-{comment_id}",
                kind="github-issue-comment",
                locator=f"{assembly_input.issue.ref.url}#issuecomment-{comment_id}",
                selector={"fields": "id,author,created_at,body"},
                raw=canonical_json(payload),
                observed_at=_iso(assembly_input.now),
                max_age_seconds=max_age,
                trust_tier="untrusted",
                trust_rationale="github-issue-body-third-party-text",
                reason_code="issue-comment",
                strategy_step="secondary",
                render_text=f"Comment by {author} at {created_at}:\n{body}",
                content_time=created_at,
            )
        )
    return candidates


def collect_target_repo(assembly_input: AssemblyInput) -> list[CandidateSource]:
    """No bytes — the model explores the clone itself with Glob/Grep/Read
    (ADR 009 sec. 8's single `agent-directed` source, not a per-file list)."""
    locator = f"git+https://github.com/{assembly_input.full_repo}@{assembly_input.target_repo_sha}"
    return [
        CandidateSource(
            source_id="target-repo",
            kind="target-repo",
            locator=locator,
            selector={"mode": "agent-directed"},
            raw=b"",
            observed_at=_iso(assembly_input.now),
            max_age_seconds=assembly_input.config.freshness_table.get("target-repo"),
            trust_tier="authoritative",
            trust_rationale="pinned-target-repository-sha",
            reason_code="target-repository-tree",
            strategy_step="primary",
        )
    ]


def _read_prior_proposal_file(path: Path, max_bytes: int) -> bytes | None:
    """Read one triplet file from a published (agent-authored, hence
    untrusted) proposal directory the way
    `run_issue_investigator._read_published_status` reads `.status.yaml`
    (`orchestrator/run_issue_investigator.py:543`): `O_NOFOLLOW` refuses a
    symlink planted where a plain file is expected, the `fstat` re-check
    refuses anything but a regular file, and the read is bounded at
    `max_bytes + 1` so a file (or a writer still appending to one) cannot
    exhaust the worker's memory. Returns `None` on any of those failures —
    the caller already treats a missing/unreadable triplet file as "this
    source does not exist yet" (`OSError` from `read_bytes()` previously)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        with open(fd, "rb", closefd=False) as f:
            return f.read(max_bytes + 1)
    finally:
        os.close(fd)


_UPDATED_AT_LINE = re.compile(r"""^updated_at:\s*['"]?([0-9T:.+\-Z]+)['"]?\s*$""", re.MULTILINE)


def read_proposal_updated_at(proposal_dir: Path) -> str | None:
    """The top-level `updated_at` of a prior proposal's `.status.yaml`, or
    `None` when it is missing, unreadable or not an ISO-8601 timestamp.

    Read with the same symlink-refusing bounded reader as the triplet, and
    matched with one anchored line pattern rather than a YAML parser, so this
    module stays stdlib-only; the investigator's own writer
    (`_write_status_yaml`) emits exactly one such top-level line."""
    raw = _read_prior_proposal_file(proposal_dir / ".status.yaml", _STATUS_READ_CEILING)
    if raw is None:
        return None
    match = _UPDATED_AT_LINE.search(raw.decode("utf-8", errors="replace"))
    if match is None:
        return None
    value = match.group(1)
    try:
        return _iso(_parse_iso(value))
    except ValueError:
        return None


def collect_prior_proposal(assembly_input: AssemblyInput) -> list[CandidateSource]:
    """The existing requirements/design/tasks triplet, when re-investigating
    a `proposed` proposal directory. A first investigation's proposal
    directory does not exist yet (or is empty), so this yields nothing.

    Under the ranked strategy (mctlhq/mctl-agents#471) a prior proposal is
    aged by its content, not by this retrieval: `observed_at` is its
    `.status.yaml` `updated_at`. Without a readable `updated_at` its age is
    unknown, so it carries no `max_age_seconds` and classifies `unknown` —
    never `fresh` by default (ADR 009 sec. 6). The default strategy keeps
    the retrieval time, so its snapshots are unchanged."""
    max_age = assembly_input.config.freshness_table.get("proposal-dir")
    read_ceiling = assembly_input.config.max_bytes_per_source
    now_iso = _iso(assembly_input.now)
    observed_at = now_iso
    content_time: str | None = None
    retrieved_at: str | None = None
    if assembly_input.config.ranked:
        content_time = read_proposal_updated_at(assembly_input.proposal_dir)
        retrieved_at = now_iso
        if content_time is None:
            max_age = None
        else:
            observed_at = content_time
    candidates = []
    for name in TRIPLET_FILENAMES:
        path = assembly_input.proposal_dir / name
        raw = _read_prior_proposal_file(path, read_ceiling)
        if raw is None:
            continue
        locator = f"gitops://agents-state/{assembly_input.service}/proposals/{assembly_input.slug}/{name}"
        candidates.append(
            CandidateSource(
                source_id=f"proposal-dir-{name}",
                kind="proposal-dir",
                locator=locator,
                selector={"path": name},
                raw=raw,
                observed_at=observed_at,
                max_age_seconds=max_age,
                trust_tier="corroborated",
                trust_rationale="prior-agent-authored-proposal",
                reason_code="prior-proposal-document",
                strategy_step="secondary",
                render_text=raw.decode("utf-8", errors="replace"),
                content_time=content_time,
                retrieved_at=retrieved_at,
            )
        )
    return candidates


_COLLECTOR_ORDER: tuple[Collector, ...] = (
    collect_inline_template,
    collect_github_issue,
    collect_issue_comments,
    collect_target_repo,
    collect_prior_proposal,
)


# ---------------------------------------------------------------------------
# Execution correlation (tasks.md #6)
# ---------------------------------------------------------------------------


def build_execution_correlation(
    *,
    resolver_mode: str,
    issue_url: str,
    target_repository_sha: str,
    plan: Any | None = None,
    legacy_model: str = "",
    legacy_allowed_tools: Sequence[str] = (),
    legacy_budget_usd: float = 0.0,
    environment: str | None = None,
    temporal_workflow_id: str | None = None,
    temporal_run_id: str | None = None,
    argo_workflow_name: str | None = None,
) -> ExecutionCorrelation:
    """Two branches (requirements.md's "Legacy-mode execution correlation"
    open question):

    - `declarative`: copy `definition_version`/`definition_content_hash`/
      `profile_version`/`profile_content_hash`/`release_revision` straight
      off `plan` (an `orchestrator.resolver.ExecutionPlan`), per ADR 009
      sec. 4. `resolver` itself is never imported here — only the plan's
      attributes are read — so this module stays stdlib-only at module
      scope even though `orchestrator/resolver.py` is not.
    - `legacy` (today's production default): `definition_version="legacy"`
      with `definition_content_hash` over the bytes of
      `agents/_manifests/issue-investigator/agent.yaml`;
      `profile_version="legacy"` with `profile_content_hash` over the
      canonical JSON of the constants that actually determine the legacy
      execution shape (model, allowed_tools, budget); `release_revision=0`
      meaning "no registry release resolved".

    Both hashes use `context_snapshot`'s one canonical-JSON hash rule —
    never a second convention.

    `temporal_workflow_id` / `temporal_run_id` are the loop that submitted
    this run, when it passed them (mctlhq/mctl-agents#461, #451). Without a
    passed workflow id the issue-keyed one is derived, which names the wrong
    loop for a dispatched `dev-loop-xr_*` run (see `loop_workflow_id`).
    """
    resolved_environment: str = (
        environment if environment is not None else os.getenv("AGENT_ENVIRONMENT", "production")
    )
    workflow_id = loop_workflow_id(issue_url, temporal_workflow_id)

    if resolver_mode == "declarative":
        if plan is None:
            raise ValueError("resolver_mode='declarative' requires an ExecutionPlan")
        return ExecutionCorrelation(
            agent="issue-investigator",
            environment=resolved_environment,
            temporal_workflow_id=workflow_id,
            temporal_run_id=temporal_run_id,
            argo_workflow_name=argo_workflow_name,
            target_repository_sha=target_repository_sha,
            definition_version=plan.definition_version,
            definition_content_hash=plan.definition_content_hash,
            profile_version=plan.profile_version,
            profile_content_hash=plan.profile_content_hash,
            release_revision=plan.release_revision,
        )

    definition_content_hash = hash_bytes(_LEGACY_DEFINITION_PATH.read_bytes())
    profile_payload = {
        "model": legacy_model,
        "allowed_tools": list(legacy_allowed_tools),
        "budget_usd": legacy_budget_usd,
    }
    profile_content_hash = hash_bytes(canonical_json(profile_payload))
    return ExecutionCorrelation(
        agent="issue-investigator",
        environment=resolved_environment,
        temporal_workflow_id=workflow_id,
        temporal_run_id=temporal_run_id,
        argo_workflow_name=argo_workflow_name,
        target_repository_sha=target_repository_sha,
        definition_version="legacy",
        definition_content_hash=definition_content_hash,
        profile_version="legacy",
        profile_content_hash=profile_content_hash,
        release_revision=0,
    )


# ---------------------------------------------------------------------------
# Assembly (tasks.md #7)
# ---------------------------------------------------------------------------


def _count_by_kind(candidates: Sequence[CandidateSource]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.kind] = counts.get(candidate.kind, 0) + 1
    return counts


def assemble(
    assembly_input: AssemblyInput,
    *,
    mode: str,
    execution: ExecutionCorrelation,
    work_context: WorkContextRef | None = None,
) -> AssemblyResult:
    """Runs every collector, the deterministic pipeline, and `seal()`.
    Sealing the same inputs twice at two different `created_at` values
    yields one `snapshot_id` — `created_at` is excluded from the hash by
    `context_snapshot.seal` (ADR 009 sec. 2)."""
    start = time.monotonic()
    config = assembly_input.config

    candidates: list[CandidateSource] = []
    collector_calls = 0
    for collector in _COLLECTOR_ORDER:
        candidates.extend(collector(assembly_input))
        collector_calls += 1

    candidates_before_ceiling = _count_by_kind(candidates)
    candidates_total = len(candidates)

    candidates_dropped_pre_budget = max(0, len(assembly_input.issue.comments) - config.max_comments)
    if len(candidates) > config.max_candidates:
        candidates_dropped_pre_budget += len(candidates) - config.max_candidates
        candidates = candidates[: config.max_candidates]

    dropped_stale = 0
    stale_demoted = 0
    conflicts: list[ContextConflict] = []
    if config.ranked:
        # mctlhq/mctl-agents#471: freshness first (the ranker orders by it),
        # then rank, then record conflicts and flag — never drop — stale.
        for candidate in candidates:
            normalize(candidate)
            classify_freshness(candidate, assembly_input.now)
        candidates = rank_candidates(candidates)
        conflicts = detect_conflicts(candidates)
        stale_demoted = flag_stale(candidates)
        strategy = ContextStrategy(
            name=RANKED_STRATEGY_NAME,
            version=RANKED_STRATEGY_VERSION,
            ranker_name=RANKER_NAME,
            ranker_version=RANKER_VERSION,
        )
    else:
        assign_ranks(candidates)
        for candidate in candidates:
            normalize(candidate)
        for candidate in candidates:
            classify_freshness(candidate, assembly_input.now)

        for candidate in candidates:
            if candidate.included and candidate.freshness_staleness == "stale":
                candidate.included = False
                candidate.reason_code = "stale"
                dropped_stale += 1
        strategy = ContextStrategy(name=STRATEGY_NAME, version=STRATEGY_VERSION)

    dropped_duplicate = deduplicate(candidates)

    truncated_sources = 0
    for candidate in candidates:
        if candidate.included and truncate_to_per_source_limit(candidate, config.max_bytes_per_source):
            truncated_sources += 1

    budget = apply_budget(candidates, config)
    excluded_budget = sum(1 for c in candidates if c.reason_code == "budget-exhausted")

    sources = tuple(_to_context_source(c) for c in sorted(candidates, key=lambda c: c.rank))
    retention = RetentionPolicy(class_="execution-record", expires_after_days=180)
    snapshot = seal(
        execution=execution,
        strategy=strategy,
        budget=budget,
        retention=retention,
        created_at=_iso(assembly_input.now),
        work_context=work_context,
        sources=sources,
        evidence_refs=(),
        conflicts=conflicts,
    )

    latency_ms = (time.monotonic() - start) * 1000
    included_by_kind = _count_by_kind([c for c in candidates if c.included])
    rendered = {c.source_id: c.render_text for c in candidates if c.included and c.render_text is not None}

    metrics = AssemblyMetrics(
        mode=mode,
        candidates_by_kind=candidates_before_ceiling,
        included_by_kind=included_by_kind,
        candidates_total=candidates_total,
        candidates_dropped_pre_budget=candidates_dropped_pre_budget,
        dropped_stale=dropped_stale,
        dropped_duplicate=dropped_duplicate,
        excluded_budget=excluded_budget,
        truncated_sources=truncated_sources,
        used_sources=budget.used_sources,
        used_bytes=budget.used_bytes,
        assembly_latency_ms=latency_ms,
        collector_calls=collector_calls,
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        snapshot=snapshot,
        stale_demoted=stale_demoted,
        conflict_count=len(conflicts),
    )
    return AssemblyResult(mode=mode, snapshot=snapshot, rendered=rendered, metrics=metrics)


def assemble_investigator_context(
    *,
    mode: str,
    issue: IssueData,
    issue_url: str,
    full_repo: str,
    repo_dir: Path,
    target_repo_sha: str,
    proposal_dir: Path,
    service: str,
    slug: str,
    prompt_template: str,
    resolver_mode: str,
    plan: Any | None = None,
    legacy_model: str = "",
    legacy_allowed_tools: Sequence[str] = (),
    legacy_budget_usd: float = 0.0,
    temporal_workflow_id: str | None = None,
    temporal_run_id: str | None = None,
    argo_workflow_name: str | None = None,
    config: AssemblyConfig | None = None,
    now: datetime | None = None,
    work_context: WorkContextRef | None = None,
    work_item_client: Any | None = None,
) -> AssemblyResult | None:
    """The feature-gated entry point `run_issue_investigator.investigate()`
    calls. Returns `None` when `mode == "off"` — no collector runs, no
    snapshot is sealed, matching today's behaviour byte-for-byte.

    With the work-context rollout at `observe` or above and a store
    execution (`we_...`), the sealed snapshot is also persisted to mctl-api
    (mctlhq/mctl-agents#431, `_persist_to_work_item_store`)."""
    if mode == "off":
        return None

    resolved_now = now or datetime.now(UTC)
    resolved_config = config or AssemblyConfig.from_env()
    execution = build_execution_correlation(
        resolver_mode=resolver_mode,
        issue_url=issue_url,
        target_repository_sha=target_repo_sha,
        plan=plan,
        legacy_model=legacy_model,
        legacy_allowed_tools=legacy_allowed_tools,
        legacy_budget_usd=legacy_budget_usd,
        temporal_workflow_id=temporal_workflow_id,
        temporal_run_id=temporal_run_id,
        argo_workflow_name=argo_workflow_name,
    )
    assembly_input = AssemblyInput(
        issue=issue,
        repo_dir=repo_dir,
        target_repo_sha=target_repo_sha,
        full_repo=full_repo,
        proposal_dir=proposal_dir,
        service=service,
        slug=slug,
        prompt_template=prompt_template,
        now=resolved_now,
        config=resolved_config,
    )
    # One client for the one conversation with the store, and only when
    # the rollout and the execution id say there is one.
    client = _client(work_item_client) if _work_context_active(work_context) else None
    work_context = _link_prior_snapshot(work_context, client)
    result = assemble(assembly_input, mode=mode, execution=execution, work_context=work_context)
    _persist_to_work_item_store(result.snapshot, client)
    return result


class SnapshotNotPersisted(RuntimeError):
    """The sealed snapshot could not be stored as this execution's, at a
    rollout stage where that blocks the run."""


def _emit_snapshot_answer(step: str, answer: Any) -> None:
    import json

    print(
        "WORK_CONTEXT_SNAPSHOT "
        + json.dumps(
            {"step": step, "verdict": answer.verdict, "snapshot_id": answer.snapshot_id,
             "content_hash": answer.content_hash, "reason": answer.reason},
            sort_keys=True,
        ),
        flush=True,
    )


def _work_context_active(work_context: WorkContextRef | None) -> bool:
    from orchestrator.work_context import rollout
    from orchestrator.work_context.snapshots import is_store_execution

    return (
        work_context is not None
        and is_store_execution(work_context.execution_id)
        and rollout.at_least(rollout.OBSERVE)
    )


def _client(work_item_client: Any | None) -> Any:
    if work_item_client is not None:
        return work_item_client
    from orchestrator.work_context.client import WorkItemClient

    return WorkItemClient()


def _link_prior_snapshot(work_context: WorkContextRef | None, client: Any | None) -> WorkContextRef | None:
    """Point `resumed_from_snapshot_id` at the prior execution's sealed
    snapshot before this execution's is sealed. A convenience pointer
    (ADR 011): a failed lookup is logged and never blocks, and a retry that
    gets a different answer is not a divergence (`snapshots.differing_fields`
    ignores the pointer)."""
    if work_context is None or client is None:
        return work_context
    from orchestrator.work_context.snapshots import SNAPSHOT_SKIPPED, resumed_from

    linked, answer = resumed_from(work_context, client)
    if answer.verdict != SNAPSHOT_SKIPPED:
        _emit_snapshot_answer("resumed_from", answer)
    return linked


def _persist_to_work_item_store(snapshot: ContextSnapshot, client: Any | None) -> None:
    """Store the sealed snapshot as its execution's (mctl-api, insert-only).

    A divergence — this execution already sealed a different context — is
    refused by the store and never overwritten. It blocks the run from
    `enforce` up, like any answer that leaves the store without this
    execution's snapshot when `blocks_on_unknown()` holds; at `observe` it is
    logged and the issue path still decides."""
    if client is None:
        return
    from orchestrator.work_context import rollout
    from orchestrator.work_context.snapshots import SNAPSHOT_DIVERGED, persist

    answer = persist(snapshot, client)
    _emit_snapshot_answer("persist", answer)
    if answer.stored:
        return
    if (answer.verdict == SNAPSHOT_DIVERGED and rollout.new_answer_may_veto()) or rollout.blocks_on_unknown():
        raise SnapshotNotPersisted(f"{answer.verdict}: {answer.reason}")
