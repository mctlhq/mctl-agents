"""Activity: discover proposals drifting from GitHub authoritative state (read-only).

Reconcile passes discover every non-terminal proposal across agents-state,
fetch their linked GitHub PR status, and project any status narrowings.
This activity is strictly read-only: gitops commits remain protected by the
Argo CWFT mctl-gitops-main-writes mutex.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity

from orchestrator.run_shepherd import (
    DEFAULT_STATE_DIR,
    RECONCILE_INPUT_STATUSES,
    ProposalRef,
    _discover_refs,
    _load_status,
    find_pr_for_proposal,
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


@activity.defn
async def discover_and_project(state_dir_path: str = "") -> ReconcileDiscoveryResult:
    state_dir = Path(state_dir_path) if state_dir_path else DEFAULT_STATE_DIR
    if not state_dir.is_dir():
        activity.logger.warning("state_dir %s not found", state_dir)
        return ReconcileDiscoveryResult(total_inspected=0, projections=[])

    refs = _discover_refs(state_dir, reconcile=True)
    projections: list[ProposalProjection] = []

    for ref in refs:
        if ref.status not in RECONCILE_INPUT_STATUSES:
            continue

        pr = find_pr_for_proposal(ref.service, ref.slug, state_dir=state_dir)
        if pr is None:
            continue

        target_status = ref.status
        notes = None

        if pr.merged:
            if ref.status in {"implemented", "review-fixing"}:
                target_status = "merged"
                notes = f"PR {pr.repo}#{pr.number} merged on GitHub"
        elif pr.closed_unmerged:
            if ref.status in RECONCILE_INPUT_STATUSES:
                target_status = "rejected"
                notes = f"PR {pr.repo}#{pr.number} closed unmerged on GitHub"

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
