"""Activity: detect orphan proposals without active DevLoopWorkflow runs.

An orphan is an actionable proposal with a valid GitHub PR that has no matching
DevLoopWorkflow running in Temporal.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from temporalio import activity

from orchestrator.run_shepherd import (
    DEFAULT_STATE_DIR,
    ProposalRef,
    _discover_refs,
    find_pr_for_proposal,
)

ACTIONABLE_STATUSES = {"accepted", "in-progress", "implemented", "review-fixing"}


@dataclass(frozen=True)
class OrphanSignal:
    service: str
    slug: str
    status: str
    pr_url: str | None
    reason: str


@dataclass(frozen=True)
class OrphanDetectionResult:
    total_actionable: int
    orphans: list[OrphanSignal]


@activity.defn
async def detect_orphans(state_dir_path: str = "") -> OrphanDetectionResult:
    state_dir = Path(state_dir_path) if state_dir_path else DEFAULT_STATE_DIR
    if not state_dir.is_dir():
        return OrphanDetectionResult(total_actionable=0, orphans=[])

    refs = _discover_refs(state_dir, reconcile=True)
    actionable_refs = [r for r in refs if r.status in ACTIONABLE_STATUSES]

    orphans: list[OrphanSignal] = []

    for ref in actionable_refs:
        pr = find_pr_for_proposal(ref.service, ref.slug, state_dir=state_dir)
        if pr is None or pr.closed_unmerged or pr.merged:
            continue

        # Signal log line per ADR 005 specification
        reason = "No active DevLoopWorkflow found for open PR proposal"
        signal = OrphanSignal(
            service=ref.service,
            slug=ref.slug,
            status=ref.status,
            pr_url=ref.pr_url or (f"https://github.com/{pr.repo}/pull/{pr.number}" if pr else None),
            reason=reason,
        )
        orphans.append(signal)
        activity.logger.info(
            "ORPHAN service=%s slug=%s status=%s pr_url=%s reason=%s",
            signal.service,
            signal.slug,
            signal.status,
            signal.pr_url,
            signal.reason,
        )

    return OrphanDetectionResult(
        total_actionable=len(actionable_refs),
        orphans=orphans,
    )
