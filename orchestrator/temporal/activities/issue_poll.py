"""Activity: poll GitHub issues for `agents:intake` label and dispatch DevLoopWorkflow runs.
"""
from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity

from orchestrator.run_issue_directive_poller import DEFAULT_MAX_DIRECTIVES, DirectiveScanResult
from orchestrator.run_issue_directive_poller import scan as directive_scan
from orchestrator.run_issue_poller import poll


@dataclass(frozen=True)
class IssuePollActivityResult:
    started: int
    failures: int


@activity.defn
async def poll_issues_activity(label: str = "agents:intake", max_issues: int = 5) -> IssuePollActivityResult:
    result = await poll(
        label=label,
        max_issues=max_issues,
        dry_run=False,
    )
    return IssuePollActivityResult(
        started=result.started,
        failures=result.failures,
    )


@activity.defn
async def directive_scan_activity(max_directives: int = DEFAULT_MAX_DIRECTIVES) -> DirectiveScanResult:
    """Second pass of the same tick (mctl-agents#417): dispatch any unacked
    `@MCTL reinvestigate` directive comments. Independent of
    `poll_issues_activity` above — see `orchestrator.run_issue_directive_poller`'s
    module docstring for why the two paths must not interact.
    """
    return await directive_scan(dry_run=False, max_directives=max_directives)
