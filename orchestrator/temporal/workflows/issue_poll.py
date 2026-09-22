"""IssuePollWorkflow: single-pass issue poll sweep on Temporal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.issue_poll import (
        DirectiveScanResult,
        IssuePollActivityResult,
        directive_scan_activity,
        poll_issues_activity,
    )

ACTIVITY_TIMEOUT = timedelta(minutes=5)
ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)

# Mirrors orchestrator.run_issue_directive_poller.DEFAULT_MAX_DIRECTIVES.
# Not imported: that module is a plain script with no temporalio import of
# its own, and this workflow file should not need to reach past the
# activities layer to get one integer default.
_DEFAULT_MAX_DIRECTIVES = 3


@dataclass(frozen=True)
class IssuePollWorkflowInput:
    label: str = "agents:intake"
    max_issues: int = 5
    max_directives: int = _DEFAULT_MAX_DIRECTIVES


@dataclass(frozen=True)
class IssuePollWorkflowResult:
    poll: IssuePollActivityResult
    #: None on a pre-#417 replay execution that took the unpatched branch
    #: below — see `workflow.patched("directive-scan")`.
    directives: DirectiveScanResult | None = None


@workflow.defn
class IssuePollWorkflow:
    @workflow.run
    async def run(self, input_data: IssuePollWorkflowInput | None = None) -> IssuePollWorkflowResult:
        label = input_data.label if input_data else "agents:intake"
        max_issues = input_data.max_issues if input_data else 5
        max_directives = input_data.max_directives if input_data else _DEFAULT_MAX_DIRECTIVES

        poll_result: IssuePollActivityResult = await workflow.execute_activity(
            poll_issues_activity,
            args=[label, max_issues],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )

        # Second pass of the same tick (mctl-agents#417): dispatch any
        # unacked `@MCTL reinvestigate` directive comments. Same schedule,
        # same offset, no new Temporal schedule — see design.md's "Wiring"
        # section. Gated so an execution already mid-flight when this
        # deploys keeps taking the branch its history recorded.
        directive_result: DirectiveScanResult | None = None
        if workflow.patched("directive-scan"):
            directive_result = await workflow.execute_activity(
                directive_scan_activity,
                args=[max_directives],
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )

        return IssuePollWorkflowResult(poll=poll_result, directives=directive_result)
