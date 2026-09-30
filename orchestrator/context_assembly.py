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

import copy
import json
import os
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.context_snapshot import (
    FRESHNESS_VALUES,
    MAX_CONFLICT_SOURCE_IDS,
    TRUST_TIERS,
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
    from orchestrator.work_context.intents import Intent
    from orchestrator.work_context.snapshots import SnapshotAnswer, StoreRef

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
# Both tables are indexed with `[]`; a vocabulary added in context_snapshot
# without a place here must fail at import, not as a KeyError mid-assembly.
if set(_TRUST_ORDER) != TRUST_TIERS or set(_FRESHNESS_ORDER) != FRESHNESS_VALUES:
    raise RuntimeError("context_assembly ranking tables are out of step with context_snapshot's vocabularies")
_PINNED_SCORE = 100.0

# The one fixed conflict rule (mctlhq/mctl-agents#471) and what the ranked
# strategy does about it: keep every source, ordered — never drop one.
CONFLICT_PRIOR_PROPOSAL_SUPERSEDED = "prior-proposal-superseded-by-later-comment"
CONFLICT_RESOLUTION_KEPT_RANKED = "kept-all-ranked-by-trust-freshness-recency"

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
# A WorkItem intent is an immutable record (mctlhq/mctl-agents#542): its
# bytes never change once appended, so it is content-addressed too.
_CONTENT_ADDRESSED_KINDS = frozenset({"target-repo", "inline-template", "work-item-intent"})

# Per-kind freshness table (requirements.md "Freshness, deduplication,
# truncation, budget"): None means content-addressed, handled above.
_DEFAULT_FRESHNESS_TABLE: dict[str, int | None] = {
    "github-issue": 3600,
    "github-issue-comment": 3600,
    "proposal-dir": 86400,
    "target-repo": None,
    "inline-template": None,
    "work-item-intent": None,
}

# How many WorkItem intents newer than the prior snapshot's enter one
# assembly (mctlhq/mctl-agents#542). The pinned resume intent is added on
# top of these, never counted against them.
MAX_WORK_ITEM_INTENTS = 20

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
    #: Kept first like a `_PINNED_KINDS` source, for this one candidate
    #: only. Set for the resume intent (mctlhq/mctl-agents#542), whose kind
    #: is not pinned as a whole: older intents stay ranked and droppable.
    pinned: bool = False


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
    #: Canonical WorkItem intents already read from mctl-api
    #: (mctlhq/mctl-agents#542), ascending by id. Empty whenever the
    #: `WORK_ITEM_INTENT_SOURCE` switch is off or the run is not
    #: WorkItem-backed, in which case no intent collector runs at all.
    work_item_intents: tuple[Intent, ...] = ()
    #: The intent the dispatched resume's execution request names, pinned.
    resume_intent: Intent | None = None
    #: The highest `work-item-intent` id among the prior snapshot's (C1's)
    #: sources, or None when there is no C1 or it carries none.
    prior_intent_high_water: int | None = None
    #: The base URL of the client that read the intents, so a sealed
    #: locator names the URL actually read. Empty = `api_base()`.
    work_item_api_base: str = ""
    #: True when the intent source was on and read for this assembly, even
    #: if it selected nothing: the snapshot then records its intent
    #: high-water mark (`WorkContextRef.intent_high_water`).
    record_intent_high_water: bool = False


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
    conflict_sources_capped: int = 0
    # mctlhq/mctl-agents#527 Slice B: the context-release resolution this run
    # used. `off`-safe defaults, so a run at `off` is byte-for-byte what this
    # line looked like before Slice B.
    release_mode: str = "off"
    binding_revision: int | None = None
    strategy_content_hash: str | None = None
    override_active: bool = False

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
            "conflict_sources_capped": self.conflict_sources_capped,
            "release_mode": self.release_mode,
            "binding_revision": self.binding_revision,
            "strategy_content_hash": self.strategy_content_hash,
            "override_active": self.override_active,
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
    #: The store's identity for `snapshot`, when a store execution persisted
    #: it successfully (mctlhq/mctl-agents#526, ADR 015 sec. 1) — `None`
    #: otherwise (no store execution, an unfavourable persist answer, or a
    #: reported id that is not `cs_`-prefixed). A defaulted final field, so
    #: every existing `AssemblyResult(...)` construction compiles unchanged.
    store_ref: StoreRef | None = None
    #: The `observe` shadow pass's sealed candidate snapshot (mctlhq/
    #: mctl-agents#528), set only when that pass ran and sealed one — `None`
    #: at `off`/`enforce`/`only`, or when the shadow pass failed or produced
    #: no candidate. Read ONLY by `_emit_context_eval`'s best-effort second
    #: `observe-candidate` record: it is never rendered, never returned as
    #: `snapshot`, and never reaches the work-item store client (`assemble_
    #: investigator_context` persists `result.snapshot` only).
    observe_candidate: ContextSnapshot | None = None


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
    sentence long and reproducible.

    The one exemption is a candidate marked `pinned` — only ever the resume
    intent (mctlhq/mctl-agents#542): it is budgeted first, so it is never
    dropped for sources ranked ahead of it. If it alone exceeds `max_bytes`
    it is cut to fit, the same way `truncate_to_per_source_limit` cuts."""
    used_sources = 0
    used_bytes = 0
    budget_hit = False
    for candidate in sorted((c for c in candidates if c.included and c.pinned), key=lambda c: c.rank):
        if used_sources + 1 > config.max_sources or used_bytes >= config.max_bytes:
            candidate.included = False
            candidate.reason_code = "budget-exhausted"
            budget_hit = True
            continue
        if used_bytes + candidate.byte_count > config.max_bytes:
            truncate_to_per_source_limit(candidate, config.max_bytes - used_bytes)
        used_sources += 1
        used_bytes += candidate.byte_count
    for candidate in sorted((c for c in candidates if c.included and not c.pinned), key=lambda c: c.rank):
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
    """The source's own content time, or `None` (sorted last). Never the
    retrieval/observation time: that is the same moment for every source
    fetched in one assembly, so falling back to it would rank an undatable
    source as if it were the newest."""
    return _epoch(candidate.content_time)


def _is_pinned(candidate: CandidateSource) -> bool:
    return candidate.kind in _PINNED_KINDS or candidate.pinned


def ranking_score(candidate: CandidateSource) -> float:
    """The score recorded in `Selection.score`: pinned kinds outrank
    everything; otherwise ten points per trust step and one per freshness
    step, so trust always dominates freshness — the same precedence the
    sort key in `rank_candidates` applies (recency only breaks ties, and so
    is not part of the score)."""
    if _is_pinned(candidate):
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
    pinned = [c for _, c in indexed if _is_pinned(c)]

    def key(item: tuple[int, CandidateSource]) -> tuple[int, int, float, int]:
        index, candidate = item
        recency = _recency(candidate)
        return (
            _TRUST_ORDER[candidate.trust_tier],
            _FRESHNESS_ORDER[candidate.freshness_staleness],
            -recency if recency is not None else float("inf"),
            index,
        )

    rest = [c for _, c in sorted((i for i in indexed if not _is_pinned(i[1])), key=key)]
    ordered = pinned + rest
    for rank, candidate in enumerate(ordered, start=1):
        candidate.rank = rank
        candidate.score = ranking_score(candidate)
    return ordered


def flag_stale(candidates: Sequence[CandidateSource]) -> None:
    """Ranked strategy: a stale source stays included — `rank_candidates`
    has already demoted it — and is flagged `reason_code="stale-demoted"`,
    so its staleness is visible in the snapshot instead of silently
    removing it. The metric is counted later, after dedupe and the budget
    (see `assemble`), so this returns nothing."""
    for candidate in candidates:
        if candidate.included and candidate.freshness_staleness == "stale":
            candidate.reason_code = "stale-demoted"


def detect_conflicts(candidates: Sequence[CandidateSource]) -> tuple[list[ContextConflict], int]:
    """The one fixed conflict rule — no model, no text comparison: a prior
    proposal whose `.status.yaml` `updated_at` (its `content_time`) predates
    a later issue comment was written without that comment, so the comment
    supersedes it. Every source involved is kept and named, in rank order;
    nothing is dropped. A proposal with no readable `updated_at` cannot be
    dated and so never fires the rule.

    Returns the conflicts and how many later comments the
    `MAX_CONFLICT_SOURCE_IDS` cap left out of them (a metric, never hidden)."""
    proposals = [c for c in candidates if c.kind == "proposal-dir" and _epoch(c.content_time) is not None]
    if not proposals:
        return [], 0
    written = max(_epoch(c.content_time) or 0.0 for c in proposals)
    later = [
        c for c in candidates
        if c.kind == "github-issue-comment" and (_epoch(c.content_time) or float("-inf")) > written
    ]
    # Bounded by the schema's own ceiling (`MAX_CONFLICT_SOURCE_IDS`), so a
    # busy issue can never make `seal()` reject the snapshot: every proposal
    # document is kept, and of the later comments the newest (the ones that
    # supersede it most) fill the remaining slots.
    # `len(proposals) <= len(TRIPLET_FILENAMES)` (3): `collect_prior_proposal`
    # yields at most one candidate per triplet file, so `room` is always >= 61.
    room = MAX_CONFLICT_SOURCE_IDS - len(proposals)
    ordered_later = sorted(later, key=lambda c: (-(_epoch(c.content_time) or 0.0), c.rank))
    later = ordered_later[: max(0, room)]
    capped = len(ordered_later) - len(later)
    if not later:
        return [], capped
    involved = sorted(proposals + later, key=lambda c: c.rank)
    return [
        ContextConflict(
            subject=CONFLICT_PRIOR_PROPOSAL_SUPERSEDED,
            source_ids=tuple(c.source_id for c in involved),
            resolution_code=CONFLICT_RESOLUTION_KEPT_RANKED,
        )
    ], capped


_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _reason(source: ContextSource) -> str:
    code = source.selection.reason_code
    return code if _SAFE_SOURCE_ID.match(code) else "excluded"


def _conflict_line(conflict: ContextConflict, by_id: Mapping[str, ContextSource]) -> str | None:
    """One notice entry, or `None` when fewer than two of the conflict's
    members are in the prompt (or, for the superseded-proposal rule, when
    either side is missing) — a conflict the model cannot see both sides of
    is left recorded in the snapshot but not asserted in the prompt.

    Only included members are named as present; a member the budget or
    deduplication left out (ADR 009 amendment 1 allows that) is named
    separately, with its `reason_code`, as not in the prompt."""
    members = [by_id[i] for i in conflict.source_ids if i in by_id and _SAFE_SOURCE_ID.match(i)]
    shown = [m for m in members if m.selection.included]
    left_out = [m for m in members if not m.selection.included]
    if len(shown) < 2:
        return None
    if conflict.subject == CONFLICT_PRIOR_PROPOSAL_SUPERSEDED:
        documents = [m.source_id for m in shown if m.kind == "proposal-dir"]
        comments = [m.source_id for m in shown if m.kind == "github-issue-comment"]
        if not documents or not comments:
            return None
        line = (
            f"- Prior proposal documents {', '.join(documents)} were written before "
            f"later issue comments {', '.join(comments)}. Where they disagree, the prior "
            "proposal may be out of date: check it against the later comments instead of "
            "carrying it forward unchanged."
        )
    else:
        line = f"- {conflict.subject}: {', '.join(m.source_id for m in shown)}."
    if left_out:
        excluded = ", ".join(f"{m.source_id} ({_reason(m)})" for m in left_out)
        line += f" Also part of this conflict but not in this prompt: {excluded}."
    return line


def render_conflict_notice(snapshot: ContextSnapshot) -> str:
    """The `on`-mode prompt notice for the snapshot's recorded conflicts —
    `""` when there are none (or none with both sides in the prompt), so a
    conflict-free prompt is unchanged. Built from source ids and codes only
    (never payload); an id that is not a plain token is left out rather than
    rendered."""
    by_id = {s.source_id: s for s in snapshot.sources}
    lines = [line for c in snapshot.conflicts if (line := _conflict_line(c, by_id)) is not None]
    if not lines:
        return ""
    return (
        "\n\n### Conflicting evidence\n\n"
        "A fixed rule found these sources in conflict, ordered by trust tier, then "
        "freshness, then recency:\n\n"
        + "\n".join(lines) + "\n"
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
    # The reader returns up to ceiling + 1 bytes: more than the ceiling means
    # the file was cut, and a cut line could still parse as a shorter, wrong
    # timestamp (`...T00:00:00+02:00` read as `...T00:00:00`). Our writer's
    # file is a few hundred bytes, so an oversized one is not trusted at all.
    if raw is None or len(raw) > _STATUS_READ_CEILING:
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


def select_work_item_intents(
    intents: Sequence[Intent],
    *,
    prior_high_water: int | None,
    resume_intent: Intent | None,
    cap: int = MAX_WORK_ITEM_INTENTS,
) -> tuple[Intent | None, tuple[Intent, ...], int]:
    """`(pinned, ranked, over_cap)`: the pinned resume intent; the newest
    `cap` intents above the prior snapshot's highest carried one, emitted
    ascending by id (mctlhq/mctl-agents#542); and how many qualifying
    intents the cap cut, so a truncated selection is counted, never silent.
    With no prior high-water mark every intent qualifies. The resume intent
    is never repeated in `ranked`. A pure function of its arguments, so the
    same inputs select the same intents in the same order."""
    pinned_id = resume_intent.intent_id if resume_intent is not None else None
    newer = sorted(
        (
            i for i in intents
            if (prior_high_water is None or i.intent_id > prior_high_water) and i.intent_id != pinned_id
        ),
        key=lambda i: i.intent_id,
    )
    kept = newer[-cap:] if cap > 0 else []
    return resume_intent, tuple(kept), len(newer) - len(kept)


def prior_intent_high_water(prior_document: Mapping[str, Any] | None) -> int | None:
    """The intent high-water mark a stored snapshot document leaves: the mark
    it recorded (`work_context.intent_high_water`) when it has one, else the
    mark its own intent sources imply (`intent_mark_from_sources`). None when
    it has neither, as for every snapshot sealed before the intent source
    existed or with the switch off, so every intent qualifies up to the cap.

    The recorded mark is authoritative. It was computed from the intents
    offered to the pipeline (`_next_intent_high_water`), which sees what
    `sources` cannot: intents the candidate ceiling cut before sealing. So it
    is never raised by the sources, which would jump past those intents. It
    also survives a quiet execution: one that selected no intent still
    records the mark it inherited."""
    if not isinstance(prior_document, Mapping):
        return None
    work_context = prior_document.get("work_context")
    recorded = work_context.get("intent_high_water") if isinstance(work_context, Mapping) else None
    if not isinstance(recorded, int) or isinstance(recorded, bool) or recorded < 0:
        recorded = None
    if recorded is not None:
        return recorded
    sources = prior_document.get("sources")
    return intent_mark_from_sources(sources) if isinstance(sources, list) else None


def intent_mark_from_sources(sources: Sequence[Any]) -> int | None:
    """The intent id below which a snapshot's sources show every ranked
    intent as seen, or None when they hold no intent.

    Seen means `selection.included`, or excluded as `duplicate-content`
    (identical bytes reached the prompt under another source id). A ranked
    intent excluded for any other reason (budget, candidate ceiling) was not
    seen, so the mark stops just below the lowest such id and it is offered
    again, even when higher ids were carried: re-offering a seen intent is a
    superset, dropping an unseen one is not. The pinned resume intent
    (`strategy_step: primary`) may be older than the mark and never lowers
    it."""
    seen: list[int] = []
    unseen: list[int] = []
    for source in sources:
        if not isinstance(source, Mapping) or source.get("kind") != "work-item-intent":
            continue
        selector = source.get("selector")
        value = selector.get("intent_id") if isinstance(selector, Mapping) else None
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        selection = source.get("selection")
        selection = selection if isinstance(selection, Mapping) else {}
        if selection.get("included") is True or selection.get("reason_code") == "duplicate-content":
            seen.append(value)
        elif selection.get("strategy_step") != "primary":
            unseen.append(value)
    if unseen:
        return min(unseen) - 1
    return max(seen) if seen else None


def _next_intent_high_water(assembly_input: AssemblyInput, sources: Sequence[ContextSource]) -> int:
    """The mark this snapshot records for the next execution, never below
    the one it inherited (the listing started there).

    It is computed from the ranked intents OFFERED to the pipeline, not from
    `sources`: the candidate ceiling cuts candidates before anything is
    sealed, and the ranked intents are collected last, so a cut intent
    leaves no trace in `sources`. A ranked intent counts as seen only when
    its source is included or excluded as `duplicate-content` (its bytes
    reached the prompt under another id). If any offered intent was not
    seen, the mark stops just below the lowest such id, whatever else was
    carried, so the pinned resume intent never lifts it past an unseen one.
    Otherwise it is the highest seen id, the pinned intent included.

    Intents the cap cut are older than every kept one and stay behind the
    mark: they are counted in `candidates_dropped_pre_budget`, not offered
    again."""
    _, ranked, _ = select_work_item_intents(
        assembly_input.work_item_intents,
        prior_high_water=assembly_input.prior_intent_high_water,
        resume_intent=assembly_input.resume_intent,
    )
    seen: set[int] = set()
    for source in sources:
        if source.kind != "work-item-intent":
            continue
        value = source.selector.get("intent_id")
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        if source.selection.included or source.selection.reason_code == "duplicate-content":
            seen.add(value)
    unseen = [i.intent_id for i in ranked if i.intent_id not in seen]
    derived = min(unseen) - 1 if unseen else max(seen, default=0)
    return max(assembly_input.prior_intent_high_water or 0, derived)


def _with_intent_high_water(
    work_context: WorkContextRef | None, assembly_input: AssemblyInput, sources: Sequence[ContextSource]
) -> WorkContextRef | None:
    if work_context is None or not assembly_input.record_intent_high_water:
        return work_context
    return replace(work_context, intent_high_water=_next_intent_high_water(assembly_input, sources))


def _intent_candidate(assembly_input: AssemblyInput, intent: Intent, *, pinned: bool) -> CandidateSource:
    """One intent as a source. The hashed content is the canonical JSON of
    `{text, params}`; the provenance (who, from where, when, and whether
    retention removed the text) lives in the selector. `text_redacted` is
    recorded explicitly, so an empty text there reads as "removed", never as
    an empty intent."""
    from orchestrator.work_context.client import ROUTES, api_base, quote_path_segment

    route = ROUTES["work_item_intent"].format(
        id=quote_path_segment(intent.work_item_id), intent_id=quote_path_segment(str(intent.intent_id))
    )
    base = assembly_input.work_item_api_base or api_base()
    payload = {"text": intent.text, "params": intent.params}
    header = (
        f"WorkItem intent {intent.intent_id} by {intent.actor_principal}"
        f"{' via ' + intent.surface if intent.surface else ''} at {intent.created_at}"
    )
    body = "[text removed by retention]" if intent.text_redacted else intent.text
    return CandidateSource(
        source_id=f"work-item-intent:{intent.work_item_id}:{intent.intent_id}",
        kind="work-item-intent",
        locator=f"{base}{route}",
        selector={
            "work_item_id": intent.work_item_id,
            "intent_id": intent.intent_id,
            "actor_principal": intent.actor_principal,
            "surface": intent.surface,
            "created_at": intent.created_at,
            "text_redacted": intent.text_redacted,
            "fields": "text,params",
        },
        raw=canonical_json(payload),
        observed_at=intent.created_at,
        max_age_seconds=assembly_input.config.freshness_table.get("work-item-intent"),
        trust_tier="reported",
        trust_rationale="work-item-intent-recorded-by-mctl-api",
        reason_code="resume-intent" if pinned else "work-item-intent",
        strategy_step="primary" if pinned else "secondary",
        render_text=f"{header}:\n{body}",
        content_time=intent.created_at,
        retrieved_at=_iso(assembly_input.now),
        pinned=pinned,
    )


def collect_work_item_intents(assembly_input: AssemblyInput) -> list[CandidateSource]:
    """The canonical WorkItem intents (mctlhq/mctl-agents#542): the pinned
    resume intent first, then the selected newer intents in ascending id
    order. Reads nothing itself; the intents were read from mctl-api before
    assembly, so this stays a pure collector like the others."""
    pinned, ranked, _ = select_work_item_intents(
        assembly_input.work_item_intents,
        prior_high_water=assembly_input.prior_intent_high_water,
        resume_intent=assembly_input.resume_intent,
    )
    out = [_intent_candidate(assembly_input, pinned, pinned=True)] if pinned is not None else []
    out.extend(_intent_candidate(assembly_input, i, pinned=False) for i in ranked)
    return out


def _work_item_intents_over_cap(assembly_input: AssemblyInput) -> int:
    if not assembly_input.work_item_intents:
        return 0
    return select_work_item_intents(
        assembly_input.work_item_intents,
        prior_high_water=assembly_input.prior_intent_high_water,
        resume_intent=assembly_input.resume_intent,
    )[2]


def _collect_pinned_work_item_intent(assembly_input: AssemblyInput) -> list[CandidateSource]:
    return [c for c in collect_work_item_intents(assembly_input) if c.pinned]


def _collect_ranked_work_item_intents(assembly_input: AssemblyInput) -> list[CandidateSource]:
    return [c for c in collect_work_item_intents(assembly_input) if not c.pinned]


_COLLECTOR_ORDER: tuple[Collector, ...] = (
    collect_inline_template,
    collect_github_issue,
    collect_issue_comments,
    collect_target_repo,
    collect_prior_proposal,
)

# The same order with the WorkItem intents in it (mctlhq/mctl-agents#542):
# the pinned resume intent right after the issue, so the default strategy's
# rank-order budget reaches it before any comment, and the other intents
# after everything else. Used only when intents were read; otherwise
# `_COLLECTOR_ORDER` runs unchanged and the snapshot keeps today's bytes.
_COLLECTOR_ORDER_WITH_INTENTS: tuple[Collector, ...] = (
    collect_inline_template,
    collect_github_issue,
    _collect_pinned_work_item_intent,
    collect_issue_comments,
    collect_target_repo,
    collect_prior_proposal,
    _collect_ranked_work_item_intents,
)


def _collectors_for(assembly_input: AssemblyInput) -> tuple[Collector, ...]:
    if assembly_input.work_item_intents or assembly_input.resume_intent is not None:
        return _COLLECTOR_ORDER_WITH_INTENTS
    return _COLLECTOR_ORDER


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
    passed workflow id the issue-keyed one is derived, which is the same
    loop: every DevLoop is issue-keyed (#461 option A, `loop_workflow_id`).
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


@dataclass(frozen=True)
class PipelineCounters:
    """The drop/dup/budget/stale counts `run_pipeline` produces, read into
    `AssemblyMetrics` by its caller. `conflict_count` is deliberately not
    one of these fields: it is `len(PipelineOutcome.conflicts)`, so it can
    never disagree with the conflicts a caller actually receives."""

    dropped_stale: int
    dropped_duplicate: int
    excluded_budget: int
    truncated_sources: int
    stale_demoted: int
    conflict_sources_capped: int
    excluded_candidate_ceiling: int


@dataclass(frozen=True)
class PipelineOutcome:
    """The result of running the deterministic selection pipeline — ranking
    or dropping, deduplication, per-source truncation and the budget — over
    one strategy's candidate list, before `seal()`. `candidates` holds every
    candidate considered, included and excluded alike, ordered by final
    rank: the same list `assemble()` seals into `ContextSource`s."""

    candidates: list[CandidateSource]
    strategy: ContextStrategy
    budget: ContextBudget
    conflicts: list[ContextConflict]
    counters: PipelineCounters


def run_pipeline(candidates: list[CandidateSource], config: AssemblyConfig, now: datetime) -> PipelineOutcome:
    """The deterministic selection pipeline (mctlhq/mctl-agents#266): the
    pre-budget candidate-count ceiling first, then, for `config.strategy`,
    either the default drop-stale branch or the ranked
    rank/`detect_conflicts`/`flag_stale` branch, then the shared
    `deduplicate` / `truncate_to_per_source_limit` / `apply_budget` tail
    every strategy applies. This is the complete candidate-list pipeline
    `assemble()` runs between its collectors and `seal()` — the sibling
    comment-count ceiling stays in `assemble()`, since it counts
    `assembly_input.issue.comments`, not `CandidateSource`s, so it is out of
    this function's inputs.

    A pure function of its arguments — no collector, no I/O, no clock beyond
    `now` — and it never mutates the `CandidateSource` objects it is given:
    every candidate is copied before any stage rewrites it (rank, inclusion,
    hash, byte count), so the same input list can be run through this
    function twice — once per strategy, as ADR 015 sec. 4's fixture contract
    requires — without the second call seeing the first call's rewrites.
    This is what lets an evaluator run a fixture's candidate list through the
    real pipeline under either strategy with no network, no collector and no
    clone, rather than re-implementing ranking in test code."""
    candidates = [copy.copy(c) for c in candidates]
    excluded_candidate_ceiling = 0
    if len(candidates) > config.max_candidates:
        excluded_candidate_ceiling = len(candidates) - config.max_candidates
        candidates = candidates[: config.max_candidates]

    dropped_stale = 0
    conflicts: list[ContextConflict] = []
    conflict_sources_capped = 0
    if config.ranked:
        # mctlhq/mctl-agents#471: freshness first (the ranker orders by it),
        # then rank, then record conflicts and flag — never drop — stale.
        for candidate in candidates:
            normalize(candidate)
            classify_freshness(candidate, now)
        candidates = rank_candidates(candidates)
        conflicts, conflict_sources_capped = detect_conflicts(candidates)
        flag_stale(candidates)
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
            classify_freshness(candidate, now)

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

    truncated_before_budget = {id(c) for c in candidates if c.truncated}
    budget = apply_budget(candidates, config)
    # A pinned resume intent the budget cut to fit is a truncated source too.
    truncated_sources += sum(1 for c in candidates if c.truncated and id(c) not in truncated_before_budget)
    excluded_budget = sum(1 for c in candidates if c.reason_code == "budget-exhausted")
    # Counted after deduplication and the budget, which may overwrite a
    # `stale-demoted` reason: the metric reports what the snapshot shows.
    stale_demoted = sum(1 for c in candidates if c.reason_code == "stale-demoted")

    ordered = sorted(candidates, key=lambda c: c.rank)
    counters = PipelineCounters(
        dropped_stale=dropped_stale,
        dropped_duplicate=dropped_duplicate,
        excluded_budget=excluded_budget,
        truncated_sources=truncated_sources,
        stale_demoted=stale_demoted,
        conflict_sources_capped=conflict_sources_capped,
        excluded_candidate_ceiling=excluded_candidate_ceiling,
    )
    return PipelineOutcome(
        candidates=ordered, strategy=strategy, budget=budget, conflicts=conflicts, counters=counters
    )


# ---------------------------------------------------------------------------
# Context-release selection (mctlhq/mctl-agents#527 Slice B, ADR 019 sec. 4).
# `orchestrator/context_rollout.py` is this module's stdlib-only sibling
# ladder; `orchestrator/context_release.py` is the (non-stdlib) catalog
# loader. Both are imported from inside `resolve_strategy_for_run`'s body
# only, never at module scope, so this module stays stdlib-only at `off`.
# ---------------------------------------------------------------------------

# Closed reason vocabulary a `StrategyResolution` can carry.
RELEASE_REASON_OFF = "off-strategy-var-decides"
RELEASE_REASON_OBSERVE = "observe-strategy-var-decides"
RELEASE_REASON_BINDING = "binding-resolved"
RELEASE_REASON_OBSERVE_SKIPPED = "binding-unresolved-observe-skipped"
RELEASE_REASON_FALLBACK = "binding-unresolved-fallback-default"
RELEASE_REASON_OBSERVE_FAILED = "observe-pass-failed"

RELEASE_REASONS = frozenset(
    {
        RELEASE_REASON_OFF,
        RELEASE_REASON_OBSERVE,
        RELEASE_REASON_BINDING,
        RELEASE_REASON_OBSERVE_SKIPPED,
        RELEASE_REASON_FALLBACK,
        RELEASE_REASON_OBSERVE_FAILED,
    }
)


@dataclass(frozen=True)
class StrategyResolution:
    """What `resolve_strategy_for_run` decided, everything the two release
    telemetry lines need without a second catalog lookup. `mode` is never
    `"off"` — at `off`, `resolve_strategy_for_run` returns `None` instead."""

    mode: str
    reason: str
    bound_strategy: str | None
    bound_version: str | None
    binding_revision: int | None
    strategy_content_hash: str | None
    override_active: bool
    verdict: str | None


class ContextStrategyNotResolved(RuntimeError):
    """The bound strategy could not be resolved at a stage where that blocks
    the run (`enforce`/`only` with `blocks_on_unknown()`), or `only` found
    `ISSUE_INVESTIGATOR_CONTEXT_STRATEGY` set. Defined here, beside
    `SnapshotNotPersisted`, rather than in `orchestrator.context_release`, so
    this module never needs that module at module scope for a type."""


def resolve_strategy_for_run(
    agent: str, config: AssemblyConfig, *, environment: str | None = None
) -> tuple[str, StrategyResolution | None]:
    """Which strategy actually decides this run, and why. Returns
    `(config.strategy, None)` at rollout `off` with no import of
    `orchestrator.context_release` — the byte-for-byte-unchanged path. Past
    `off`, defers both `orchestrator.context_rollout` and
    `orchestrator.context_release` into this function body, the same pattern
    `_work_context_active`/`_client`/`_persist_to_work_item_store` already
    use for `orchestrator.work_context`."""
    from orchestrator import context_rollout

    stage = context_rollout.mode()
    if stage == context_rollout.OFF:
        return config.strategy, None

    from orchestrator import context_release

    override_active = bool(os.environ.get(STRATEGY_ENV_VAR, "").strip())

    if stage == context_rollout.OBSERVE:
        binding_environment = context_rollout.OBSERVE_ENVIRONMENT
    else:
        binding_environment = environment if environment is not None else os.getenv("AGENT_ENVIRONMENT", "production")

    if stage == context_rollout.ONLY and override_active:
        raise ContextStrategyNotResolved(
            f"{STRATEGY_ENV_VAR} is set while rollout stage is 'only', where the "
            f"{agent}/{binding_environment} binding must be the sole selector; unset "
            f"{STRATEGY_ENV_VAR} or step the rollout down to 'enforce'"
        )

    try:
        resolved = context_release.resolve(agent, binding_environment)
    except context_release.ContextReleaseError as exc:
        if stage == context_rollout.OBSERVE:
            return config.strategy, StrategyResolution(
                mode=stage,
                reason=RELEASE_REASON_OBSERVE_SKIPPED,
                bound_strategy=None,
                bound_version=None,
                binding_revision=None,
                strategy_content_hash=None,
                override_active=override_active,
                verdict=exc.code,
            )
        if context_rollout.blocks_on_unknown():
            raise ContextStrategyNotResolved(
                f"{agent}/{binding_environment}: binding unresolved at rollout stage {stage!r} "
                f"with CONTEXT_RELEASE_REQUIRED in effect ({exc})"
            ) from exc
        return STRATEGY_NAME, StrategyResolution(
            mode=stage,
            reason=RELEASE_REASON_FALLBACK,
            bound_strategy=None,
            bound_version=None,
            binding_revision=None,
            strategy_content_hash=None,
            override_active=override_active,
            verdict=exc.code,
        )

    if stage == context_rollout.OBSERVE:
        return config.strategy, StrategyResolution(
            mode=stage,
            reason=RELEASE_REASON_OBSERVE,
            bound_strategy=resolved.strategy,
            bound_version=resolved.version,
            binding_revision=resolved.release_revision,
            strategy_content_hash=resolved.content_hash,
            override_active=override_active,
            verdict=resolved.verdict,
        )

    return resolved.strategy, StrategyResolution(
        mode=stage,
        reason=RELEASE_REASON_BINDING,
        bound_strategy=resolved.strategy,
        bound_version=resolved.version,
        binding_revision=resolved.release_revision,
        strategy_content_hash=resolved.content_hash,
        override_active=override_active,
        verdict=resolved.verdict,
    )


def _emit_release_verdict(
    execution: ExecutionCorrelation, effective_strategy: str, resolution: StrategyResolution | None
) -> None:
    """One `CONTEXT_STRATEGY_RELEASE` line per run, unconditionally
    (mctlhq/mctl-agents#527 Slice B, ADR 019 sec. 5) — at `off`, `mode` is
    `"off"` and everything binding-shaped is `null`.

    `strategy` always names the strategy this run actually used
    (`effective_strategy`); `bound_strategy` always names the strategy the
    context-release binding decided on (or `None` at `off`). The two agree at
    every stage except `observe`, where the binding never drives the
    authoritative run — there, `strategy` is what ran and `bound_strategy` is
    only the shadow/compare candidate."""
    line: dict[str, str | int | bool | None]
    if resolution is None:
        line = {
            "mode": "off",
            "agent": execution.agent,
            "environment": execution.environment,
            "strategy": effective_strategy,
            "bound_strategy": None,
            "version": None,
            "content_hash": None,
            "binding_revision": None,
            "override_active": False,
            "verdict": None,
            "reason": RELEASE_REASON_OFF,
        }
    else:
        line = {
            "mode": resolution.mode,
            "agent": execution.agent,
            "environment": execution.environment,
            "strategy": effective_strategy,
            "bound_strategy": resolution.bound_strategy,
            "version": resolution.bound_version,
            "content_hash": resolution.strategy_content_hash,
            "binding_revision": resolution.binding_revision,
            "override_active": resolution.override_active,
            "verdict": resolution.verdict,
            "reason": resolution.reason,
        }
    print("CONTEXT_STRATEGY_RELEASE " + json.dumps(line, sort_keys=True), flush=True)


def _emit_strategy_compare(
    resolution: StrategyResolution,
    *,
    authoritative_strategy: str,
    authoritative_version: str,
    authoritative_snapshot_id: str,
    bound_snapshot_id: str,
) -> None:
    """One `CONTEXT_STRATEGY_COMPARE` line, emitted only when a candidate
    `snapshot_id` was actually produced (`observe`, bound strategy resolved
    and different from the authoritative one). Carries only the two strategy
    identities, the binding revision, the two `snapshot_id`s and — added in
    Slice C, mctlhq/mctl-agents#528 — the evaluator reference an operator
    needs to correlate this line with the `[context] context_eval=` records
    it can join on `snapshot_id`. This line still carries no counter
    arithmetic and no verdict about the two strategies' outcome; that
    remains mctlhq/mctl-agents#526's evaluator's own job, never re-derived
    here against a shadow snapshot that is deliberately never persisted."""
    record_kind: str | None
    evaluator_name: str | None
    evaluator_version: str | None
    metrics_contract_version: str | None
    try:
        from orchestrator import context_eval

        record_kind = context_eval.RECORD_KIND
        evaluator_name = context_eval.EVALUATOR_NAME
        evaluator_version = context_eval.EVALUATOR_VERSION
        metrics_contract_version = context_eval.METRICS_CONTRACT_VERSION
    except ImportError:
        record_kind = evaluator_name = evaluator_version = metrics_contract_version = None

    line = {
        "mode": resolution.mode,
        "authoritative_strategy": authoritative_strategy,
        "authoritative_version": authoritative_version,
        "authoritative_snapshot_id": authoritative_snapshot_id,
        "bound_strategy": resolution.bound_strategy,
        "bound_version": resolution.bound_version,
        "bound_snapshot_id": bound_snapshot_id,
        "binding_revision": resolution.binding_revision,
        "record_kind": record_kind,
        "evaluator_name": evaluator_name,
        "evaluator_version": evaluator_version,
        "metrics_contract_version": metrics_contract_version,
    }
    print("CONTEXT_STRATEGY_COMPARE " + json.dumps(line, sort_keys=True), flush=True)


def assemble(
    assembly_input: AssemblyInput,
    *,
    mode: str,
    execution: ExecutionCorrelation,
    work_context: WorkContextRef | None = None,
) -> AssemblyResult:
    """Runs every collector, the deterministic pipeline (`run_pipeline`), and
    `seal()`. Sealing the same inputs twice at two different `created_at`
    values yields one `snapshot_id` — `created_at` is excluded from the hash
    by `context_snapshot.seal` (ADR 009 sec. 2).

    mctlhq/mctl-agents#527 Slice B: before the collectors run,
    `resolve_strategy_for_run` may substitute `config.strategy` with the
    context-release binding's — done here, not just before `run_pipeline`,
    because `collect_prior_proposal` reads `assembly_input.config.ranked`
    (`:821`). At `off` this is a no-op: `resolve_strategy_for_run` returns
    `(config.strategy, None)` without importing `orchestrator.context_release`,
    so every snapshot keeps its exact bytes and `snapshot_id`."""
    start = time.monotonic()
    config = assembly_input.config

    effective_strategy, resolution = resolve_strategy_for_run(
        execution.agent, config, environment=execution.environment
    )
    if effective_strategy != config.strategy:
        config = replace(config, strategy=effective_strategy)
        assembly_input = replace(assembly_input, config=config)

    candidates: list[CandidateSource] = []
    collector_calls = 0
    for collector in _collectors_for(assembly_input):
        candidates.extend(collector(assembly_input))
        collector_calls += 1

    candidates_before_ceiling = _count_by_kind(candidates)
    candidates_total = len(candidates)

    outcome = run_pipeline(candidates, config, assembly_input.now)
    candidates_dropped_pre_budget = (
        max(0, len(assembly_input.issue.comments) - config.max_comments)
        + outcome.counters.excluded_candidate_ceiling
        + _work_item_intents_over_cap(assembly_input)
    )

    sources = tuple(_to_context_source(c) for c in outcome.candidates)
    retention = RetentionPolicy(class_="execution-record", expires_after_days=180)
    snapshot = seal(
        execution=execution,
        strategy=outcome.strategy,
        budget=outcome.budget,
        retention=retention,
        created_at=_iso(assembly_input.now),
        work_context=_with_intent_high_water(work_context, assembly_input, sources),
        sources=sources,
        evidence_refs=(),
        conflicts=outcome.conflicts,
    )

    # The `observe` shadow pass (mctlhq/mctl-agents#527 Slice B): a second,
    # non-authoritative `run_pipeline` pass over the same pre-pipeline
    # candidate list, sealed into a LOCAL solely to read its `snapshot_id`.
    # `run_pipeline` copies its input before rewriting it (`:1017`) and is
    # documented pure, so this cannot see or affect the authoritative pass
    # above. The whole thing is wrapped in `try/except`: `observe` is
    # behaviour-neutral by definition, so a failure here must not fail a run
    # the authoritative pass already completed.
    observe_snapshot_id: str | None = None
    #: mctlhq/mctl-agents#528: the same shadow snapshot this block already
    #: seals, now also kept (never re-sealed) so `AssemblyResult.observe_
    #: candidate` can carry it to `_emit_context_eval`'s best-effort second
    #: record. Still never rendered, never `AssemblyResult.snapshot`, never
    #: persisted — only this local's value changes; nothing about what the
    #: shadow pass does or how it is discarded from the authoritative path
    #: changes.
    observe_candidate: ContextSnapshot | None = None
    if (
        resolution is not None
        and resolution.reason == RELEASE_REASON_OBSERVE
        and resolution.bound_strategy not in (None, config.strategy)
    ):
        try:
            # Re-run the collector loop under the substituted (shadow) config,
            # not just `run_pipeline`, for the same reason the docstring above
            # gives for the authoritative pass: `collect_prior_proposal` (:815)
            # branches on `assembly_input.config.ranked`, so reusing the
            # module-level `candidates` (collected under the authoritative
            # `config.ranked`) would silently misclassify a prior proposal's
            # freshness whenever the bound strategy differs from the running
            # one in `ranked`-ness. T13
            # (`test_enforce_substitution_reaches_the_collectors_not_only_the_pipeline`)
            # pins the analogous enforce-path substitution but never reaches
            # this observe-only branch; the pin for this branch is
            # `test_observe_shadow_pass_recollects_under_the_bound_strategy`.
            shadow_config = replace(config, strategy=resolution.bound_strategy)
            shadow_input = replace(assembly_input, config=shadow_config)
            shadow_candidates: list[CandidateSource] = []
            for collector in _collectors_for(shadow_input):
                shadow_candidates.extend(collector(shadow_input))
            shadow_outcome = run_pipeline(shadow_candidates, shadow_config, assembly_input.now)
            shadow_sources = tuple(_to_context_source(c) for c in shadow_outcome.candidates)
            shadow_snapshot = seal(
                execution=execution,
                strategy=shadow_outcome.strategy,
                budget=shadow_outcome.budget,
                retention=retention,
                created_at=_iso(assembly_input.now),
                work_context=_with_intent_high_water(work_context, shadow_input, shadow_sources),
                sources=shadow_sources,
                evidence_refs=(),
                conflicts=shadow_outcome.conflicts,
            )
            observe_snapshot_id = shadow_snapshot.snapshot_id
            observe_candidate = shadow_snapshot
        except Exception:  # noqa: BLE001 — observe must never fail an already-completed run
            observe_snapshot_id = None
            observe_candidate = None
            resolution = replace(resolution, reason=RELEASE_REASON_OBSERVE_FAILED)

    # Emitted here, not before the collectors: the reason code must reflect
    # RELEASE_REASON_OBSERVE_FAILED when the shadow pass above just failed,
    # and this is the one place in the function where that is known.
    _emit_release_verdict(execution, effective_strategy, resolution)

    if observe_snapshot_id is not None and resolution is not None:
        _emit_strategy_compare(
            resolution,
            authoritative_strategy=outcome.strategy.name,
            authoritative_version=outcome.strategy.version,
            authoritative_snapshot_id=snapshot.snapshot_id,
            bound_snapshot_id=observe_snapshot_id,
        )

    latency_ms = (time.monotonic() - start) * 1000
    included_by_kind = _count_by_kind([c for c in outcome.candidates if c.included])
    rendered = {
        c.source_id: c.render_text for c in outcome.candidates if c.included and c.render_text is not None
    }

    metrics = AssemblyMetrics(
        mode=mode,
        candidates_by_kind=candidates_before_ceiling,
        included_by_kind=included_by_kind,
        candidates_total=candidates_total,
        candidates_dropped_pre_budget=candidates_dropped_pre_budget,
        dropped_stale=outcome.counters.dropped_stale,
        dropped_duplicate=outcome.counters.dropped_duplicate,
        excluded_budget=outcome.counters.excluded_budget,
        truncated_sources=outcome.counters.truncated_sources,
        used_sources=outcome.budget.used_sources,
        used_bytes=outcome.budget.used_bytes,
        assembly_latency_ms=latency_ms,
        collector_calls=collector_calls,
        strategy_name=outcome.strategy.name,
        strategy_version=outcome.strategy.version,
        snapshot=snapshot,
        stale_demoted=outcome.counters.stale_demoted,
        conflict_count=len(outcome.conflicts),
        conflict_sources_capped=outcome.counters.conflict_sources_capped,
        release_mode=resolution.mode if resolution is not None else "off",
        binding_revision=resolution.binding_revision if resolution is not None else None,
        strategy_content_hash=resolution.strategy_content_hash if resolution is not None else None,
        override_active=resolution.override_active if resolution is not None else False,
    )
    return AssemblyResult(
        mode=mode, snapshot=snapshot, rendered=rendered, metrics=metrics, observe_candidate=observe_candidate
    )


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
    execution_request_id: str | None = None,
) -> AssemblyResult | None:
    """The feature-gated entry point `run_issue_investigator.investigate()`
    calls. Returns `None` when `mode == "off"` — no collector runs, no
    snapshot is sealed, matching today's behaviour byte-for-byte.

    With the work-context rollout at `observe` or above and a store
    execution (`we_...`), the sealed snapshot is also persisted to mctl-api
    (mctlhq/mctl-agents#431, `_persist_to_work_item_store`).

    With `WORK_ITEM_INTENT_SOURCE=on` and that same store execution, the
    item's canonical intents are read from mctl-api first and become sources
    (mctlhq/mctl-agents#542); the intent `execution_request_id`'s request
    names is pinned. An intent that must be carried but cannot be read
    raises `IntentUnresolved`, and nothing is sealed or persisted."""
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
    work_context, prior_answer = _link_prior_snapshot_with_answer(work_context, client)
    reading = _resolve_work_item_intents(
        work_context, client, execution_request_id=execution_request_id, prior_answer=prior_answer
    )
    if reading is not None:
        intents, resume_intent, high_water = reading
        assembly_input = replace(
            assembly_input,
            work_item_intents=intents,
            resume_intent=resume_intent,
            prior_intent_high_water=high_water,
            work_item_api_base=str(getattr(client, "base_url", "") or ""),
            record_intent_high_water=True,
        )
    result = assemble(assembly_input, mode=mode, execution=execution, work_context=work_context)
    answer = _persist_to_work_item_store(result.snapshot, client)
    if answer is not None:
        from orchestrator.work_context.snapshots import store_ref_from

        return replace(result, store_ref=store_ref_from(result.snapshot, answer))
    return result


class IntentUnresolved(RuntimeError):
    """A WorkItem intent this assembly must carry could not be read
    (mctlhq/mctl-agents#542): the resume intent the execution request names,
    the request itself, or the item's intent listing. The run fails as
    `failed` / `intent-unresolved` and seals nothing: a C2 that silently
    lacks the intent would claim a completeness it does not have."""


def _resolve_work_item_intents(
    work_context: WorkContextRef | None,
    client: Any | None,
    *,
    execution_request_id: str | None,
    prior_answer: Any | None,
) -> tuple[tuple[Intent, ...], Intent | None, int | None] | None:
    """`(intents, resume_intent, prior_high_water)` for a WorkItem-backed
    assembly, or None when there is none to read: no store execution (no
    client), or the `WORK_ITEM_INTENT_SOURCE` switch is off. A tuple, even
    an empty one, means the source was on and read.

    Every read that fails is `IntentUnresolved`, never "no intent": the
    execution request (whose `intent_id` decides what is pinned), the pinned
    intent itself, and the listing. Only a request that names no intent pins
    nothing."""
    if work_context is None or client is None:
        return None
    from orchestrator.work_context import execution_requests as xr
    from orchestrator.work_context import intents as wi
    from orchestrator.work_context.snapshots import SNAPSHOT_REPLAYED

    if wi.switch() != wi.SWITCH_ON:
        print("[context] work-item-intent source=off", flush=True)
        return None
    wid = work_context.work_item_id
    resume_intent: Intent | None = None
    if execution_request_id:
        request_answer = client.execution_request(wid, execution_request_id)
        request = request_answer.request
        if request_answer.verdict != xr.FOUND or request is None:
            raise IntentUnresolved(
                f"execution request {execution_request_id} of {wid} could not be read: {request_answer.reason}"
            )
        if request.work_item_id != wid:
            raise IntentUnresolved(
                f"execution request {execution_request_id} belongs to {request.work_item_id}, not {wid}"
            )
        if request.intent_id_malformed:
            raise IntentUnresolved(
                f"execution request {execution_request_id} of {wid} carries a malformed intent_id"
            )
        if request.intent_id is not None:
            read = client.work_item_intent(wid, request.intent_id)
            if read.verdict != wi.INTENT_FOUND or read.intent is None:
                raise IntentUnresolved(
                    f"resume intent {request.intent_id} of {wid} is unresolved ({read.verdict}): {read.reason}"
                )
            resume_intent = read.intent
    prior_document = (
        prior_answer.stored_document
        if prior_answer is not None and prior_answer.verdict == SNAPSHOT_REPLAYED
        else None
    )
    high_water = prior_intent_high_water(prior_document)
    # Only intents above the mark can be selected, so the listing starts
    # there; the pinned intent was read by id above.
    listing = client.list_intents(wid, after_id=high_water or 0)
    if listing.verdict != wi.INTENTS_LISTED:
        raise IntentUnresolved(f"intents of {wid} could not be listed ({listing.verdict}): {listing.reason}")
    print(
        "[context] work-item-intent source=on "
        f"listed={len(listing.intents)} "
        f"resume_intent_id={resume_intent.intent_id if resume_intent is not None else '-'} "
        f"prior_high_water={high_water if high_water is not None else '-'}",
        flush=True,
    )
    return listing.intents, resume_intent, high_water


class SnapshotNotPersisted(RuntimeError):
    """The sealed snapshot could not be stored as this execution's, at a
    rollout stage where that blocks the run."""


def _emit_snapshot_answer(step: str, answer: Any) -> None:
    line = {"step": step, "verdict": answer.verdict, "snapshot_id": answer.snapshot_id,
            "content_hash": answer.content_hash, "reason": answer.reason}
    # A replay decided by comparing documents names both sides, so one line
    # correlates this attempt's snapshot with the stored one (#455 item 9).
    if answer.local_snapshot_id or answer.local_content_hash:
        line["local_snapshot_id"] = answer.local_snapshot_id
        line["local_content_hash"] = answer.local_content_hash
    print("WORK_CONTEXT_SNAPSHOT " + json.dumps(line, sort_keys=True), flush=True)


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


def _link_prior_snapshot_with_answer(
    work_context: WorkContextRef | None, client: Any | None
) -> tuple[WorkContextRef | None, Any | None]:
    """Point `resumed_from_snapshot_id` at the prior execution's sealed
    snapshot before this execution's is sealed. A convenience pointer
    (ADR 011): a failed lookup is logged and never blocks, and a retry that
    gets a different answer is not a divergence (`snapshots.differing_fields`
    ignores the pointer)."""
    if work_context is None or client is None:
        return work_context, None
    from orchestrator.work_context.snapshots import SNAPSHOT_SKIPPED, resumed_from

    linked, answer = resumed_from(work_context, client)
    if answer.verdict != SNAPSHOT_SKIPPED:
        _emit_snapshot_answer("resumed_from", answer)
    return linked, answer


def _persist_to_work_item_store(snapshot: ContextSnapshot, client: Any | None) -> SnapshotAnswer | None:
    """Store the sealed snapshot as its execution's (mctl-api, insert-only).
    Returns the store's `SnapshotAnswer`, or `None` when there is no client
    (no store execution, or the rollout gate is below `observe`) — the answer
    is the caller's (`assemble_investigator_context`'s) to turn into a
    `StoreRef` (mctlhq/mctl-agents#526); nothing here changes because of that.

    A divergence — this execution already sealed a different context — is
    refused by the store and never overwritten. It blocks the run from
    `enforce` up, like any answer that leaves the store without this
    execution's snapshot when `blocks_on_unknown()` holds; at `observe` it is
    logged and the issue path still decides."""
    if client is None:
        return None
    from orchestrator.work_context import rollout
    from orchestrator.work_context.snapshots import SNAPSHOT_DIVERGED, persist

    answer = persist(snapshot, client)
    _emit_snapshot_answer("persist", answer)
    if answer.stored:
        return answer
    if (answer.verdict == SNAPSHOT_DIVERGED and rollout.new_answer_may_veto()) or rollout.blocks_on_unknown():
        raise SnapshotNotPersisted(f"{answer.verdict}: {answer.reason}")
    return answer
