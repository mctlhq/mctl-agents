"""Activity: detect orphan proposals without active DevLoopWorkflow runs.

An orphan is an actionable proposal with a valid GitHub PR that has no matching
DevLoopWorkflow running in Temporal.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity

from orchestrator.run_shepherd import (
    DEFAULT_STATE_DIR,
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


def _sync_detect_orphans(state_dir: Path, active_workflow_ids: set[str] | None = None) -> OrphanDetectionResult:
    if not state_dir.is_dir():
        return OrphanDetectionResult(total_actionable=0, orphans=[])

    refs = _discover_refs(state_dir, reconcile=True)
    actionable_refs = [r for r in refs if r.status in ACTIONABLE_STATUSES]

    orphans: list[OrphanSignal] = []
    active_ids = active_workflow_ids or set()

    for ref in actionable_refs:
        pr = find_pr_for_proposal(ref.service, ref.slug, state_dir=state_dir)
        if pr is None or pr.closed_unmerged or pr.merged:
            continue

        # Expected workflow ID for this proposal. Real DevLoop IDs are
        # dev-loop-{owner}-{repo}-{issue-number} (see start.py:workflow_id_for),
        # NOT ...-{slug} — the old slug-based reconstruction matched nothing,
        # so every actionable proposal was reported as an orphan
        # (mctl-agents#151). The issue number is recoverable from the slug's
        # issue-<N>- prefix; slugs without it (incident-*, pre-Temporal) never
        # had a DevLoop, so no active-ID check applies to them.
        m = re.match(r"issue-(\d+)-", ref.slug)
        if m:
            # Owner comes from the PR snapshot rather than a hardcoded
            # "mctlhq": parse_issue_url currently only admits mctlhq issues,
            # but deriving it keeps this comparison correct if that ever
            # widens (agy P2 on PR #212).
            owner = pr.repo.split("/")[0] if pr.repo and "/" in pr.repo else "mctlhq"
            expected_workflow_id = f"dev-loop-{owner}-{ref.service}-{m.group(1)}"
            if expected_workflow_id in active_ids:
                continue

        reason = "No active DevLoopWorkflow found for open PR proposal"
        signal = OrphanSignal(
            service=ref.service,
            slug=ref.slug,
            status=ref.status,
            pr_url=ref.pr_url or (f"https://github.com/{pr.repo}/pull/{pr.number}" if pr else None),
            reason=reason,
        )
        orphans.append(signal)

    return OrphanDetectionResult(
        total_actionable=len(actionable_refs),
        orphans=orphans,
    )


@activity.defn
async def detect_orphans(
    state_dir_path: str = "",
    active_workflow_ids: list[str] | None = None,
) -> OrphanDetectionResult:
    state_dir = Path(state_dir_path) if state_dir_path else DEFAULT_STATE_DIR
    if not state_dir.is_dir():
        return OrphanDetectionResult(total_actionable=0, orphans=[])

    active_set = set(active_workflow_ids) if active_workflow_ids else set()
    result = await asyncio.to_thread(_sync_detect_orphans, state_dir, active_set)

    for signal in result.orphans:
        activity.logger.info(
            "ORPHAN service=%s slug=%s status=%s pr_url=%s reason=%s",
            signal.service,
            signal.slug,
            signal.status,
            signal.pr_url,
            signal.reason,
        )

    return result
