"""IssuePollWorkflow orchestration tests (mctl-agents#417): the second pass
that dispatches unacked `@MCTL reinvestigate` directive comments, wired in
behind `workflow.patched("directive-scan")`.

Runs the real workflow definition against temporalio's time-skipping test
environment with fake activities, same harness as test_reconcile_workflow.py.
A fresh execution always takes the patched branch — only a replay of a
pre-existing history can take the other one, which is what
tests/test_workflow_replay.py covers.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.issue_poll import DirectiveScanResult, IssuePollActivityResult
from orchestrator.temporal.workflows.issue_poll import IssuePollWorkflow, IssuePollWorkflowInput
from tests.temporal_harness import Worker

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-issue-poll"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _fake_activities(
    *,
    poll_result: IssuePollActivityResult | None = None,
    directive_result: DirectiveScanResult | None = None,
):
    received: dict = {}

    @activity.defn(name="poll_issues_activity")
    async def fake_poll_issues_activity(label: str, max_issues: int) -> IssuePollActivityResult:
        received["poll_args"] = (label, max_issues)
        return poll_result or IssuePollActivityResult(started=0, failures=0)

    @activity.defn(name="directive_scan_activity")
    async def fake_directive_scan_activity(max_directives: int) -> DirectiveScanResult:
        received["directive_max_directives"] = max_directives
        return directive_result or DirectiveScanResult()

    return [fake_poll_issues_activity, fake_directive_scan_activity], received


async def _run(env, activities, input_data: IssuePollWorkflowInput | None = None):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IssuePollWorkflow],
        activities=activities,
    ):
        return await env.client.execute_workflow(
            IssuePollWorkflow.run,
            input_data,
            id=f"issue-poll-test-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


class TestIssuePollWorkflowDirectiveScan:
    async def test_directive_scan_runs_as_the_second_pass(self, env):
        """A fresh execution takes the `workflow.patched("directive-scan")`
        branch: directive_scan_activity runs after poll_issues_activity and
        its result is carried on IssuePollWorkflowResult.directives."""
        directive_result = DirectiveScanResult(dispatched=2, replied=2, deferred=1, failed=0)
        activities, received = _fake_activities(
            poll_result=IssuePollActivityResult(started=1, failures=0),
            directive_result=directive_result,
        )

        result = await _run(
            env, activities, IssuePollWorkflowInput(label="agents:intake", max_issues=5, max_directives=7)
        )

        assert received["poll_args"] == ("agents:intake", 5)
        assert received["directive_max_directives"] == 7
        assert result.poll == IssuePollActivityResult(started=1, failures=0)
        assert result.directives == directive_result

    async def test_default_input_still_runs_the_directive_scan(self, env):
        """`IssuePollWorkflow.run(None)` (the schedule's own invocation
        shape) must still take the patched branch, not just an explicit
        IssuePollWorkflowInput."""
        activities, received = _fake_activities(
            directive_result=DirectiveScanResult(dispatched=0, replied=0, deferred=0, failed=0)
        )

        result = await _run(env, activities, None)

        assert received["directive_max_directives"] == 3
        assert result.directives is not None
