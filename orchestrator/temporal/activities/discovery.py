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

from orchestrator.run_shepherd import (
    RECONCILE_INPUT_STATUSES,
    _discover_refs,
    find_pr_for_proposal,
)
from orchestrator.temporal.activities.gitops_state import (
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
class ReconcileDiscoveryResult:
    total_inspected: int
    projections: list[ProposalProjection]


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


async def _discover_from_github() -> ReconcileDiscoveryResult:
    refs = [r for r in await list_proposal_refs() if r.status in RECONCILE_INPUT_STATUSES]
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

    return ReconcileDiscoveryResult(total_inspected=len(refs), projections=projections)


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
