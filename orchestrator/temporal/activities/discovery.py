"""Activity: discover proposals drifting from GitHub authoritative state (read-only).

Reconcile passes discover every non-terminal proposal across agents-state,
fetch their linked GitHub PR status, and project any status narrowings.
This activity is strictly read-only: gitops commits remain protected by the
Argo CWFT mctl-gitops-main-writes mutex.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.directives import acked_comment_ids, parse_comments
from orchestrator.run_issue_directive_poller import (
    TERMINAL_STATUSES as DIRECTIVE_TERMINAL_STATUSES,
)
from orchestrator.run_issue_directive_poller import (
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


async def _stale_directives(refs: list[ProposalStateRef]) -> list[StaleDirective]:
    """Every unacked directive-shaped comment newer than its proposal's
    `updated_at`, across `refs`. One `gh issue view` per ref, same
    per-issue tolerance `run_issue_directive_poller.scan` has: a `gh`
    failure on one issue is logged and the sweep continues with the rest.

    `updated_at` missing (a status file older than #417, or one that
    genuinely never records it) makes ANY unacked directive on that issue
    stale — there is nothing to compare against, and reporting nothing
    would be exactly the silence this check exists to catch.

    Bounded at `MAX_STALE_DIRECTIVE_CANDIDATES`: this is a report-only,
    best-effort sweep, not the reason `discover_and_project` exists, and an
    unbounded sequential `gh` sweep here would sit on the critical path of
    the whole reconcile tick (codex review on #417). Candidates beyond the
    cap are skipped this tick and picked up by a later one.
    """
    if len(refs) > MAX_STALE_DIRECTIVE_CANDIDATES:
        activity.logger.warning(
            "reconcile: %d directive-staleness candidate(s) found — capping this "
            "tick's sweep at %d; the rest are picked up by a later tick",
            len(refs),
            MAX_STALE_DIRECTIVE_CANDIDATES,
        )
        refs = refs[:MAX_STALE_DIRECTIVE_CANDIDATES]

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
            if ref.updated_at and directive.created_at and directive.created_at <= ref.updated_at:
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
