"""Activity: poll GitHub issues for `agents:intake` label and dispatch DevLoopWorkflow runs.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from temporalio import activity

from orchestrator.run_issue_poller import poll_once


@dataclass(frozen=True)
class IssuePollActivityResult:
    started: int
    failures: int


@activity.defn
async def poll_issues_activity(label: str = "agents:intake", max_issues: int = 5) -> IssuePollActivityResult:
    result = await asyncio.to_thread(
        poll_once,
        label=label,
        max_issues=max_issues,
        dry_run=False,
    )
    return IssuePollActivityResult(
        started=result.started,
        failures=result.failures,
    )
