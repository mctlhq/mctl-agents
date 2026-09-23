"""Activity: detect orphan proposals without active DevLoopWorkflow runs.

An orphan is an actionable proposal with a valid GitHub PR that has no matching
DevLoopWorkflow running in Temporal: neither its issue-keyed loop nor a
dispatched `dev-loop-xr_*` loop whose memo names that loop's id
(mctlhq/mctl-agents#474, `active_loops`).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.run_shepherd import (
    _discover_refs,
    find_pr_for_proposal,
)
from orchestrator.temporal import active_loops
from orchestrator.temporal.activities.gitops_state import (
    fetch_pr_snapshots,
    list_proposal_refs,
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
    # Set when orphan detection did NOT run (e.g. the active-workflow
    # visibility query failed and the tick skipped the check) so result
    # consumers can tell a skipped tick from a genuinely clean one.
    skipped_reason: str | None = None


def _sync_detect_orphans(
    state_dir: Path, active: active_loops.ActiveLoops | None = None
) -> OrphanDetectionResult:
    if not state_dir.is_dir():
        return OrphanDetectionResult(total_actionable=0, orphans=[])

    refs = _discover_refs(state_dir, reconcile=True)
    actionable_refs = [r for r in refs if r.status in ACTIONABLE_STATUSES]

    orphans: list[OrphanSignal] = []
    active = active or active_loops.ActiveLoops()

    for ref in actionable_refs:
        pr = find_pr_for_proposal(ref.service, ref.slug, state_dir=state_dir)
        if pr is None or pr.closed_unmerged or pr.merged:
            continue

        if active.owner_of(_expected_workflow_id(ref.slug, pr.repo, ref.service)):
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


def expected_dev_loop_id(slug: str, repo: str | None, service: str) -> str | None:
    """The DevLoop workflow ID this proposal would have, if it has one.

    Real IDs are dev-loop-{owner}-{repo}-{issue-number} (start.py's
    workflow_id_for), NOT ...-{slug}: the old slug-based reconstruction
    matched nothing, so every actionable proposal was reported as an orphan
    (#151). Slugs without an issue-<N>- prefix (incident-*, pre-Temporal)
    never had a DevLoop, so no active-ID check applies to them — which is
    also why the implement-sweep (mctl-agents#412) can pick up the incident
    responder's auto-accepted proposals: they are never "owned" by this
    check.

    The owner comes from the PR rather than a hardcoded "mctlhq":
    parse_issue_url only admits mctlhq issues today, but deriving it keeps
    the comparison correct if that ever widens (agy P2 on PR #212).

    Public (promoted from `_expected_workflow_id` for #412) because
    `stranded.py` needs the exact same reconstruction the orphan sweep uses;
    `_expected_workflow_id` stays as an alias for `lifecycle_reconcile.py`'s
    existing import.
    """
    m = re.match(r"issue-(\d+)-", slug)
    if not m:
        return None
    owner = repo.split("/")[0] if repo and "/" in repo else "mctlhq"
    return f"dev-loop-{owner}-{service}-{m.group(1)}"


# Back-compat alias — see expected_dev_loop_id's docstring.
_expected_workflow_id = expected_dev_loop_id


async def _detect_from_github(active: active_loops.ActiveLoops) -> OrphanDetectionResult:
    refs = [r for r in await list_proposal_refs() if r.status in ACTIONABLE_STATUSES]
    snapshots = await fetch_pr_snapshots(refs)

    orphans: list[OrphanSignal] = []
    for ref in refs:
        pr = snapshots.get((ref.service, ref.slug))
        if pr is None or pr.closed_unmerged or pr.merged:
            continue

        if active.owner_of(_expected_workflow_id(ref.slug, pr.repo, ref.service)):
            continue

        orphans.append(
            OrphanSignal(
                service=ref.service,
                slug=ref.slug,
                status=ref.status,
                pr_url=ref.pr_url or f"https://github.com/{pr.repo}/pull/{pr.number}",
                reason="No active DevLoopWorkflow found for open PR proposal",
            )
        )

    return OrphanDetectionResult(total_actionable=len(refs), orphans=orphans)


@activity.defn
async def detect_orphans(
    state_dir_path: str = "",
    active_workflow_ids: list[Any] | None = None,
) -> OrphanDetectionResult:
    """Actionable proposals with an open PR and no DevLoopWorkflow running.

    Reads gitops over the API unless an explicit ``state_dir_path`` names a
    local checkout — see discover_and_project for why the old filesystem
    default made this sweep a no-op in the worker (#270). This one returned
    its empty result without even a warning, so nothing in the logs
    distinguished "no orphans" from "never ran".

    `active_workflow_ids` is `list_active_dev_loop_ids`' result as the
    workflow hands it through: `{workflow_id, issue_workflow_id}` entries, or
    bare ids from a result recorded before #474 (`active_loops.index`).
    """
    active_set = active_loops.index(active_workflow_ids)

    if state_dir_path:
        state_dir = Path(state_dir_path)
        if not await asyncio.to_thread(state_dir.is_dir):
            raise ApplicationError(
                f"state_dir {state_dir} does not exist", non_retryable=True
            )
        result = await asyncio.to_thread(_sync_detect_orphans, state_dir, active_set)
    else:
        result = await _detect_from_github(active_set)

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
