"""Activity: detect orphan proposals without active DevLoopWorkflow runs.

An orphan is an actionable proposal with a valid GitHub PR that has no matching
DevLoopWorkflow running in Temporal.

A second, separately reported signal covers "approved, never started"
(mctl-agents#413): an authorized `accepted`/`in-progress` proposal with no PR,
no live attempt lease and no live DevLoopWorkflow, older than
NEVER_STARTED_THRESHOLD_MINUTES. Report-only.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.run_shepherd import (
    _discover_refs,
    find_pr_for_proposal,
)
from orchestrator.temporal.activities.gitops_state import (
    ProposalStateRef,
    _parse_status_yaml,
    fetch_pr_snapshots,
    list_proposal_refs,
    parse_iso,
)
from orchestrator.temporal.constants import never_started_threshold_minutes

ACTIONABLE_STATUSES = {"accepted", "in-progress", "implemented", "review-fixing"}
NEVER_STARTED_STATUSES = {"accepted", "in-progress"}
NEVER_STARTED_REASON = "approved, no PR, no live DevLoopWorkflow: execution never started"


@dataclass(frozen=True)
class NeverStartedSignal:
    service: str
    slug: str
    status: str
    approved_at: str | None
    age_minutes: int | None
    reason: str


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
    # Approved proposals that never started (mctl-agents#413). Defaulted so
    # results recorded before this field existed still deserialize.
    never_started: list[NeverStartedSignal] = field(default_factory=list)
    # Accepted/in-progress candidates excluded only for lack of execution
    # authorization (the legacy shape); counted, not listed.
    never_started_unauthorized: int = 0
    # Local-checkout proposals whose .status.yaml could not be read; they are
    # absent from never_started, so this count keeps them visible.
    never_started_unreadable: int = 0


def _classify_never_started(
    refs: list[ProposalStateRef],
    active_ids: set[str],
    threshold_minutes: int,
    now: datetime,
) -> tuple[list[NeverStartedSignal], int]:
    """Pure classifier for the never-started signal. Returns (signals, unauthorized_count)."""
    signals: list[NeverStartedSignal] = []
    unauthorized = 0
    for ref in refs:
        if ref.status not in NEVER_STARTED_STATUSES or ref.pr_url:
            continue
        expires = parse_iso(ref.attempt_expires_at)
        if expires is not None and expires > now:
            continue
        if ref.unrunnable or ref.blocked:
            continue
        # Authorization is checked before the active-set and age checks
        # (design section 3): an unauthorized proposal cannot be executing.
        if ref.execution_authorization is None:
            unauthorized += 1
            continue
        expected = expected_dev_loop_id(ref.slug, None, ref.service)
        if expected and expected in active_ids:
            continue
        started = parse_iso(ref.approved_at) or parse_iso(ref.updated_at)
        age_minutes: int | None = None
        if started is not None:
            age = now - started
            if age.total_seconds() < threshold_minutes * 60:
                continue
            age_minutes = int(age.total_seconds() // 60)
        # else: neither timestamp parses -> report with age_minutes=None
        # (design section 3: fail toward visibility).
        signals.append(
            NeverStartedSignal(
                service=ref.service,
                slug=ref.slug,
                status=ref.status,
                approved_at=ref.approved_at,
                age_minutes=age_minutes,
                reason=NEVER_STARTED_REASON,
            )
        )
    return signals, unauthorized


def _local_state_refs(state_dir: Path, refs) -> tuple[list[ProposalStateRef], int]:
    """ProposalStateRefs for local-checkout proposals, parsed like the GitHub path.

    Returns (refs, unreadable_count): unreadable files are counted, never
    treated as "nothing to report".
    """
    out: list[ProposalStateRef] = []
    unreadable = 0
    for ref in refs:
        try:
            # status_path is already rooted at state_dir by _discover_refs;
            # joining it again would double a relative state_dir.
            parsed = _parse_status_yaml(Path(ref.status_path).read_text())
        except (OSError, ValueError, yaml.YAMLError):
            unreadable += 1
            continue
        (status, pr_url, updated_at, attempt_expires_at, unrunnable, blocked,
         authorization, unauthorized_reason, approved_at) = parsed
        out.append(
            ProposalStateRef(
                service=ref.service,
                slug=ref.slug,
                status=status,
                pr_url=pr_url,
                updated_at=updated_at,
                approved_at=approved_at,
                attempt_expires_at=attempt_expires_at,
                unrunnable=unrunnable,
                blocked=blocked,
                execution_authorization=authorization,
                unauthorized_reason=unauthorized_reason,
            )
        )
    return out, unreadable


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

        expected_workflow_id = _expected_workflow_id(ref.slug, pr.repo, ref.service)
        if expected_workflow_id and expected_workflow_id in active_ids:
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

    never_started: list[NeverStartedSignal] = []
    unauthorized = 0
    unreadable = 0
    if active_workflow_ids is not None:
        state_refs, unreadable = _local_state_refs(state_dir, actionable_refs)
        never_started, unauthorized = _classify_never_started(
            state_refs,
            active_ids,
            never_started_threshold_minutes(),
            datetime.now(UTC),
        )

    return OrphanDetectionResult(
        total_actionable=len(actionable_refs),
        orphans=orphans,
        never_started=never_started,
        never_started_unauthorized=unauthorized,
        never_started_unreadable=unreadable,
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


async def _detect_from_github(
    active_ids: set[str], classify_never_started: bool = False
) -> OrphanDetectionResult:
    refs = [r for r in await list_proposal_refs() if r.status in ACTIONABLE_STATUSES]
    snapshots = await fetch_pr_snapshots(refs)

    orphans: list[OrphanSignal] = []
    for ref in refs:
        pr = snapshots.get((ref.service, ref.slug))
        if pr is None or pr.closed_unmerged or pr.merged:
            continue

        expected = _expected_workflow_id(ref.slug, pr.repo, ref.service)
        if expected and expected in active_ids:
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

    never_started: list[NeverStartedSignal] = []
    unauthorized = 0
    if classify_never_started:
        never_started, unauthorized = _classify_never_started(
            refs, active_ids, never_started_threshold_minutes(), datetime.now(UTC)
        )
    return OrphanDetectionResult(
        total_actionable=len(refs),
        orphans=orphans,
        never_started=never_started,
        never_started_unauthorized=unauthorized,
    )


@activity.defn
async def detect_orphans(
    state_dir_path: str = "",
    active_workflow_ids: list[str] | None = None,
) -> OrphanDetectionResult:
    """Actionable proposals with an open PR and no DevLoopWorkflow running,
    plus (separately) approved proposals that never started.

    The never-started list stays empty when ``active_workflow_ids`` is None:
    an unknown active set must not flag every approved proposal (#151).

    Reads gitops over the API unless an explicit ``state_dir_path`` names a
    local checkout — see discover_and_project for why the old filesystem
    default made this sweep a no-op in the worker (#270). This one returned
    its empty result without even a warning, so nothing in the logs
    distinguished "no orphans" from "never ran".
    """
    active_set = set(active_workflow_ids) if active_workflow_ids else set()
    # None (unknown active set) must stay distinguishable from empty.
    known_active = set(active_workflow_ids) if active_workflow_ids is not None else None

    if state_dir_path:
        state_dir = Path(state_dir_path)
        if not await asyncio.to_thread(state_dir.is_dir):
            raise ApplicationError(
                f"state_dir {state_dir} does not exist", non_retryable=True
            )
        result = await asyncio.to_thread(_sync_detect_orphans, state_dir, known_active)
    else:
        result = await _detect_from_github(
            active_set, classify_never_started=active_workflow_ids is not None
        )

    for signal in result.orphans:
        activity.logger.info(
            "ORPHAN service=%s slug=%s status=%s pr_url=%s reason=%s",
            signal.service,
            signal.slug,
            signal.status,
            signal.pr_url,
            signal.reason,
        )

    for ns in result.never_started:
        activity.logger.warning(
            "NEVER_STARTED service=%s slug=%s status=%s approved_at=%s age_minutes=%s reason=%s",
            ns.service,
            ns.slug,
            ns.status,
            ns.approved_at,
            ns.age_minutes,
            ns.reason,
        )
    activity.logger.info(
        "detect_orphans: actionable=%d orphans=%d never_started=%d "
        "never_started_unauthorized=%d never_started_unreadable=%d",
        result.total_actionable,
        len(result.orphans),
        len(result.never_started),
        result.never_started_unauthorized,
        result.never_started_unreadable,
    )

    return result
