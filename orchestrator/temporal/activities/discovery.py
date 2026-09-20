"""Activity: discover proposals drifting from GitHub authoritative state (read-only).

Reconcile passes discover every non-terminal proposal across agents-state,
fetch their linked GitHub PR status, and project any status narrowings.
This activity is strictly read-only: gitops commits remain protected by the
Argo CWFT mctl-gitops-main-writes mutex.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.directives import acked_comment_ids, parse_comments
from orchestrator.run_issue_directive_poller import (
    TERMINAL_STATUSES as DIRECTIVE_TERMINAL_STATUSES,
)
from orchestrator.run_issue_directive_poller import (
    _scan_disabled,
    issue_url_for,
    read_issue_comments,
)
from orchestrator.run_shepherd import (
    RECONCILE_INPUT_STATUSES,
    _discover_refs,
    find_pr_for_proposal,
)
from orchestrator.temporal.activities.gitops_state import (
    ProposalStateRef,
    fetch_pr_snapshots,
    list_proposal_refs,
)


@dataclass(frozen=True)
class ProposalProjection:
    service: str
    slug: str
    current_status: str
    projected_status: str
    pr_url: str | None
    notes: str | None


@dataclass(frozen=True)
class StaleDirective:
    """A directive-shaped comment newer than its proposal's recorded
    `updated_at`, with no matching acknowledgement — belt-and-braces for the
    case where `run_issue_directive_poller`'s own scan is broken, down, or
    capped-out (mctl-agents#417). Report-only: reconcile never dispatches or
    writes anything for this condition — see ReconcileWorkflow.
    """

    service: str
    slug: str
    issue_url: str
    comment_id: str
    author: str


# The staleness sweep is one sequential `gh issue view` per non-terminal,
# non-directive-terminal proposal — report-only, and NOT the reason this
# activity exists (the projection sweep above is). Left unbounded it grows
# with the live proposal count and sits on the critical path of the whole
# reconcile tick (`discover_and_project`), delaying `projections` — which
# ReconcileWorkflow does act on — behind an arbitrarily long, purely
# advisory sweep (codex review on #417). Bounded the same way
# run_issue_directive_poller.DEFAULT_MAX_DIRECTIVES bounds its own scan:
# candidates beyond the cap are skipped this tick and picked up by a later
# one, same as a directive beyond that cap is.
MAX_STALE_DIRECTIVE_CANDIDATES = 25


@dataclass(frozen=True)
class ReconcileDiscoveryResult:
    total_inspected: int
    projections: list[ProposalProjection]
    #: None on a filesystem-backed sweep (`_sync_discover_and_project`,
    #: which has no `updated_at` to compare against) and on any result
    #: recorded before this field existed. Populated only on the
    #: GitHub-backed path (`_discover_from_github`, production).
    stale_directives: list[StaleDirective] | None = None


def _sync_discover_and_project(state_dir: Path) -> ReconcileDiscoveryResult:
    if not state_dir.is_dir():
        return ReconcileDiscoveryResult(total_inspected=0, projections=[])

    refs = _discover_refs(state_dir, reconcile=True)
    projections: list[ProposalProjection] = []

    for ref in refs:
        if ref.status not in RECONCILE_INPUT_STATUSES:
            continue

        pr = find_pr_for_proposal(ref.service, ref.slug, state_dir=state_dir)
        if pr is None:
            continue

        target_status, notes = _project(
            ref.status, pr.merged, pr.closed_unmerged, pr.repo, pr.number
        )

        if target_status != ref.status:
            projections.append(
                ProposalProjection(
                    service=ref.service,
                    slug=ref.slug,
                    current_status=ref.status,
                    projected_status=target_status,
                    pr_url=ref.pr_url,
                    notes=notes,
                )
            )

    return ReconcileDiscoveryResult(
        total_inspected=len(refs),
        projections=projections,
    )


def _project(status: str, merged: bool, closed_unmerged: bool, repo: str, number: int) -> tuple[str, str | None]:
    """The projection rule itself, shared by both backends.

    Returns the status this proposal should carry and the note explaining
    why, or the status unchanged and None.
    """
    if merged and status in {"implemented", "review-fixing"}:
        return "merged", f"PR {repo}#{number} merged on GitHub"
    if closed_unmerged and status in RECONCILE_INPUT_STATUSES:
        return "rejected", f"PR {repo}#{number} closed unmerged on GitHub"
    return status, None


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse an RFC 3339 / ISO 8601 timestamp into a timezone-aware
    `datetime`, or None when unparseable or absent.

    Never compare `ref.updated_at`/`directive.created_at` as raw strings:
    GitHub's `createdAt` uses a `Z` suffix (`...T...Z`), while an unquoted
    `.status.yaml` timestamp parses to a native `datetime.datetime` whose
    `str()` form uses a space separator and a `+00:00` offset
    (`gitops_state._parse_status_yaml`). Because `'T' > ' '` and `'Z' > '+'`
    in ASCII, `<=` on the two raw strings gives the wrong answer across that
    format boundary — a directive from earlier the same day can compare as
    "newer" than it is, so this must always go through actual `datetime`
    values instead.

    An unquoted `.status.yaml` timestamp with no offset at all (PyYAML
    parses e.g. `2026-09-19 10:00:00` to a naive `datetime`, and `str()` of
    that drops no information there is none of) yields a naive result here
    too; assumed UTC, since every timestamp this module ever writes or reads
    (GitHub's `createdAt`, this repo's own `updated_at` writers) is UTC.
    Comparing a naive and an aware `datetime` raises `TypeError`, which
    would turn one proposal's odd status file into a sweep-ending crash
    (the same class of thing `list_proposal_refs`' per-file tolerance
    exists to prevent) rather than the report-only best-effort this is.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # TypeError alongside ValueError: `value` is typed `str | None`, and
        # `gitops_state._parse_status_yaml` normalizes `updated_at` to
        # `str(...)` precisely so it always is — but this function is the
        # one place that assumption gets exercised, so a caller that (now
        # or later) hands it a raw PyYAML `datetime` must degrade to the
        # same report-only "unparseable" outcome as a malformed string,
        # not crash the whole reconcile tick on a bad `.replace()` call.
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


# Rotates the `_stale_directives` scan window across ticks so every
# candidate is eventually examined, instead of the same alphabetically-first
# `MAX_STALE_DIRECTIVE_CANDIDATES` forever. `list_proposal_refs` returns
# proposals in stable git-tree order every tick and this activity is
# read-only and stateless — no cursor, offset, or persisted progress marker
# — so a fixed prefix slice permanently starves everything past index
# `MAX_STALE_DIRECTIVE_CANDIDATES` (agy P2 on #421; the same class of bug
# PR #415 fixed in visibility.py's per-id traversal, and #417's own earlier
# review before that). This cursor is process-lifetime state, the same
# category `_blob_cache` already is — not durable across a worker restart,
# but a restart also empties `_blob_cache` and the sweep still converges,
# just from position zero again.
_stale_directive_cursor = 0


def _rotate_window(items: list[ProposalStateRef], cap: int) -> list[ProposalStateRef]:
    """Up to `cap` items starting at the module cursor, wrapping around, and
    advance the cursor so the next call continues where this one left off.

    The real, honest property this gives: no candidate can be stably
    starved (the window's start position always moves forward), and
    traversal converges fairly over time under a reasonably stable
    candidate set. It is NOT an exact ceil(len(items) / cap)-tick coverage
    bound — `items` is `list_proposal_refs()`'s current snapshot, which can
    gain or lose entries between ticks (a proposal created, merged, or
    rejected), and this cursor has no memory of which items it has already
    covered, only a position modulo the current `len(items)`; a churning
    candidate set can shift or reorder the window's contents from what a
    static-list tick-count bound would predict (claude P3 on #421 — an
    earlier round of this comment/tests overclaimed the precise bound).
    """
    global _stale_directive_cursor
    n = len(items)
    if n <= cap:
        _stale_directive_cursor = 0
        return items
    start = _stale_directive_cursor % n
    window = [items[(start + i) % n] for i in range(cap)]
    _stale_directive_cursor = (start + cap) % n
    return window


async def _stale_directives(refs: list[ProposalStateRef]) -> list[StaleDirective] | None:
    """Every unacked directive-shaped comment newer than its proposal's
    `updated_at`, across `refs`. One `gh issue view` per ref, same
    per-issue tolerance `run_issue_directive_poller.scan` has: a `gh`
    failure on one issue is logged and the sweep continues with the rest.

    `updated_at` missing (a status file older than #417, or one that
    genuinely never records it) makes ANY unacked directive on that issue
    stale — there is nothing to compare against, and reporting nothing
    would be exactly the silence this check exists to catch.

    Bounded at `MAX_STALE_DIRECTIVE_CANDIDATES` per tick via a rotating
    window (`_rotate_window`): this is a report-only, best-effort sweep, not
    the reason `discover_and_project` exists, and an unbounded sequential
    `gh` sweep here would sit on the critical path of the whole reconcile
    tick (codex review on #417) — but the window's start position advances
    every tick rather than staying pinned to the same prefix, so every
    candidate is eventually examined instead of the ones past the cap being
    permanently skipped.

    Consults `run_issue_directive_poller._scan_disabled()`
    (`MCTL_DIRECTIVE_SCAN_ENABLED=false`) first: that env var is documented
    as the feature's "fastest kill, no deploy" switch, and this sweep is
    part of the same feature (mctl-agents#417) — flipping the switch must
    stop it from making `gh` calls too, not just `run_issue_directive_poller
    .scan()` (claude P3, repeated across rounds on #421).

    Returns None, not `[]`, when the kill switch is on: `[]` already means
    "swept every candidate, found nothing stale", the same meaning
    `ReconcileDiscoveryResult.stale_directives`'s own `None` sentinel
    describes as "not checked" (its docstring: "None on a filesystem-backed
    sweep ... Populated only on the GitHub-backed path"). Returning `[]`
    here for "not checked because the switch is off" would read to any
    consumer of that field as a clean sweep instead of a skipped one
    (claude P3 on #421).
    """
    if _scan_disabled():
        return None
    if len(refs) > MAX_STALE_DIRECTIVE_CANDIDATES:
        activity.logger.warning(
            "reconcile: %d directive-staleness candidate(s) found — scanning a "
            "rotating window of %d this tick; the window advances every tick so no "
            "candidate is stably starved, though under a churning candidate set this "
            "is not an exact per-tick coverage guarantee",
            len(refs),
            MAX_STALE_DIRECTIVE_CANDIDATES,
        )
        refs = _rotate_window(refs, MAX_STALE_DIRECTIVE_CANDIDATES)

    stale: list[StaleDirective] = []
    for ref in refs:
        issue_url = issue_url_for(ref.service, ref.slug)
        if issue_url is None:
            continue
        try:
            comments = await asyncio.to_thread(read_issue_comments, issue_url)
        except Exception as exc:  # noqa: BLE001 — one issue's comments must not blind the sweep to the rest
            activity.logger.warning(
                "reconcile: could not read comments for %s (%s); skipping "
                "directive-staleness check for this proposal",
                issue_url,
                exc,
            )
            continue

        acked = acked_comment_ids(comments)
        for directive in parse_comments(comments):
            if not directive.comment_id or directive.comment_id in acked:
                continue
            ref_updated_at = _parse_timestamp(ref.updated_at)
            directive_created_at = _parse_timestamp(directive.created_at)
            if ref_updated_at and directive_created_at and directive_created_at <= ref_updated_at:
                continue
            stale.append(
                StaleDirective(
                    service=ref.service,
                    slug=ref.slug,
                    issue_url=issue_url,
                    comment_id=directive.comment_id,
                    author=directive.author,
                )
            )
    return stale


async def _discover_from_github() -> ReconcileDiscoveryResult:
    all_refs = await list_proposal_refs()
    refs = [r for r in all_refs if r.status in RECONCILE_INPUT_STATUSES]
    snapshots = await fetch_pr_snapshots(refs)

    projections: list[ProposalProjection] = []
    for ref in refs:
        pr = snapshots.get((ref.service, ref.slug))
        if pr is None:
            continue
        target_status, notes = _project(
            ref.status, pr.merged, pr.closed_unmerged, pr.repo, pr.number
        )
        if target_status != ref.status:
            projections.append(
                ProposalProjection(
                    service=ref.service,
                    slug=ref.slug,
                    current_status=ref.status,
                    projected_status=target_status,
                    pr_url=ref.pr_url,
                    notes=notes,
                )
            )

    # Deliberately a WIDER set than `refs` above: RECONCILE_INPUT_STATUSES
    # excludes "proposed", which is exactly the status a `reinvestigate`
    # directive acts on (#395's proposal never left it). Anything not
    # DIRECTIVE_TERMINAL_STATUSES is a candidate, same filter
    # run_issue_directive_poller.scan applies.
    directive_candidates = [r for r in all_refs if r.status not in DIRECTIVE_TERMINAL_STATUSES]
    stale_directives = await _stale_directives(directive_candidates)

    return ReconcileDiscoveryResult(
        total_inspected=len(refs), projections=projections, stale_directives=stale_directives
    )


@activity.defn
async def discover_and_project(state_dir_path: str = "") -> ReconcileDiscoveryResult:
    """Project GitHub PR state onto proposal statuses (read-only).

    With no ``state_dir_path`` — production — the state is read from
    mctl-gitops over the API. The worker has no gitops checkout, so the
    old filesystem default resolved to a path that never exists and this
    sweep silently returned nothing from 2026-08-06 until #270.

    An explicit ``state_dir_path`` still reads that directory: an Argo pod
    (and the tests) has the clone right there, and re-fetching what is
    already on local disk would be strictly worse.
    """
    if state_dir_path:
        state_dir = Path(state_dir_path)
        if not await asyncio.to_thread(state_dir.is_dir):
            # An explicitly named directory that does not exist is a
            # misconfiguration, not an empty sweep. Raising keeps it from
            # reading as "no drift found" the way the old default did.
            raise ApplicationError(
                f"state_dir {state_dir} does not exist", non_retryable=True
            )
        return await asyncio.to_thread(_sync_discover_and_project, state_dir)

    return await _discover_from_github()
