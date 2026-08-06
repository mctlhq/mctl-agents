"""IssuePollWorkflow: single-pass issue poll sweep on Temporal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.issue_poll import IssuePollActivityResult, poll_issues_activity

ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass(frozen=True)
class IssuePollWorkflowInput:
    label: str = "agents:intake"
    max_issues: int = 5


@workflow.defn
class IssuePollWorkflow:
    @workflow.run
    async def run(self, input_data: IssuePollWorkflowInput | None = None) -> IssuePollActivityResult:
        label = input_data.label if input_data else "agents:intake"
        max_issues = input_data.max_issues if input_data else 5

        return await workflow.execute_activity(
            poll_issues_activity,
            args=[label, max_issues],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )
