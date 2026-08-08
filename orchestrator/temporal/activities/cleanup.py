"""Activity: cancel DevLoopWorkflows whose GitHub issue is now closed.

ReconcileWorkflow calls this once per tick AFTER discovery and orphan
detection.  For every Running DevLoopWorkflow in the mctl-agents namespace,
the activity:

1. Parses the issue number and repo from the deterministic workflow ID
   ``dev-loop-{owner}-{repo}-{issue_number}``.
2. Queries GitHub for the issue state via ``gh issue view``.
3. If the issue is ``CLOSED`` it cancels the workflow via the Temporal
   client, so Temporal records the terminal status as ``Canceled`` rather
   than leaving it ``Running`` forever.

Design notes
------------
* Read-only GitHub call first, cancel only when confirmed closed.
* The Temporal client is built inside the activity (not passed in) — same
  pattern as the existing ``start.py`` helpers.
* A cancel request on an already-terminal workflow is a no-op from
  Temporal's perspective (it simply raises ``WorkflowNotFoundError`` or
  succeeds silently on a Canceled run).  Both are swallowed gracefully.
* We intentionally do NOT terminate (``terminate=True``): ``cancel``
  delivers a ``CancelledError`` so the workflow can clean up handlers if
  it ever adds them.
* Only workflows matching ``dev-loop-mctlhq-*`` are inspected — the
  ``manual-test-issue-poll`` poller and other non-dev-loop workflows are
  left alone.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass, field

from temporalio import activity

from orchestrator.temporal.start import connect

# Matches: dev-loop-mctlhq-<repo>-<issue_number>
_WORKFLOW_ID_RE = re.compile(
    r"^dev-loop-mctlhq-(?P<repo>[^-].+?)-(?P<number>\d+)$"
)
_ORG = "mctlhq"


@dataclass(frozen=True)
class WorkflowCancelResult:
    workflow_id: str
    repo: str
    issue_number: int
    cancelled: bool
    reason: str


@dataclass(frozen=True)
class CleanupResult:
    inspected: int
    cancelled: int
    skipped: int
    details: list[WorkflowCancelResult] = field(default_factory=list)


def _issue_state(repo: str, number: int) -> str:
    """Return 'OPEN' or 'CLOSED' by shelling out to gh.

    Returns 'OPEN' on any error (fail-safe: never cancel when uncertain).
    """
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "view", str(number),
                "--repo", f"{_ORG}/{repo}",
                "--json", "state",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        data = json.loads(proc.stdout or "{}")
        return data.get("state", "OPEN").upper()
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "Could not fetch issue state for %s/%s#%d: %s — treating as OPEN",
            _ORG, repo, number, exc,
        )
        return "OPEN"


async def _cancel_workflow(client, workflow_id: str) -> None:
    handle = client.get_workflow_handle(workflow_id)
    await handle.cancel()


async def _process_running_workflows() -> CleanupResult:
    client = await connect()

    # List all Running DevLoopWorkflows
    running_handles = []
    async for wf in client.list_workflows(
        query='WorkflowType = "DevLoopWorkflow" AND ExecutionStatus = "Running"'
    ):
        running_handles.append(wf.id)

    details: list[WorkflowCancelResult] = []
    cancelled_count = 0
    skipped_count = 0

    for wf_id in running_handles:
        m = _WORKFLOW_ID_RE.match(wf_id)
        if not m:
            # Not a user-issue dev-loop — skip (e.g. test workflows)
            skipped_count += 1
            continue

        repo = m.group("repo")
        number = int(m.group("number"))

        state = _issue_state(repo, number)

        if state != "CLOSED":
            activity.logger.info(
                "workflow=%s issue=%s/%s#%d state=%s — keeping",
                wf_id, _ORG, repo, number, state,
            )
            skipped_count += 1
            details.append(WorkflowCancelResult(
                workflow_id=wf_id,
                repo=repo,
                issue_number=number,
                cancelled=False,
                reason=f"issue state is {state}",
            ))
            continue

        # Issue is CLOSED — cancel the workflow
        try:
            await _cancel_workflow(client, wf_id)
            activity.logger.info(
                "CANCELLED workflow=%s — issue %s/%s#%d is CLOSED",
                wf_id, _ORG, repo, number,
            )
            cancelled_count += 1
            details.append(WorkflowCancelResult(
                workflow_id=wf_id,
                repo=repo,
                issue_number=number,
                cancelled=True,
                reason="issue is CLOSED on GitHub",
            ))
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "Could not cancel workflow=%s: %s", wf_id, exc
            )
            skipped_count += 1
            details.append(WorkflowCancelResult(
                workflow_id=wf_id,
                repo=repo,
                issue_number=number,
                cancelled=False,
                reason=f"cancel failed: {exc}",
            ))

    return CleanupResult(
        inspected=len(running_handles),
        cancelled=cancelled_count,
        skipped=skipped_count,
        details=details,
    )


@activity.defn
async def cleanup_closed_issue_workflows() -> CleanupResult:
    """Cancel DevLoopWorkflows whose GitHub issue has been closed."""
    return await asyncio.ensure_future(_process_running_workflows())
