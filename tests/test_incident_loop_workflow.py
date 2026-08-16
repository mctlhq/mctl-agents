"""IncidentLoopWorkflow orchestration tests.

Same shape as test_dev_loop_workflow.py: the real workflow definition runs
against temporalio's time-skipping test environment with a fake
`submit_and_wait` standing in for the HTTP-calling one (that activity is
covered directly, against mocked HTTP, in test_temporal_activities.py).

What matters here is that the workflow delegates to Argo AT ALL — the
regression these tests guard is the 2026-08-15 incident (mctl-agents#179),
where the responder ran the Claude SDK inside the worker pod, which has
neither the gitops checkout it writes proposals into nor the memory to hold
an SDK session.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.workflows.incidents import IncidentLoopWorkflow

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-incident-loop"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _capturing_submit(phase: str = "Succeeded"):
    seen: list[SubmitAndWaitInput] = []

    @activity.defn(name="submit_and_wait")
    async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
        seen.append(input)
        return WorkflowResult(workflow_name=f"{input.operation}-fake", phase=phase)

    return fake_submit_and_wait, seen


async def _run(env, activity_fn) -> object:
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IncidentLoopWorkflow],
        activities=[activity_fn],
    ):
        return await env.client.execute_workflow(
            IncidentLoopWorkflow.run,
            id=f"incident-loop-test-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


class TestIncidentLoopWorkflow:
    async def test_submits_the_incidents_operation_in_responder_mode(self, env):
        fake_submit, seen = _capturing_submit()

        result = await _run(env, fake_submit)

        assert [i.operation for i in seen] == ["mctl-agents-incidents"]
        # mode is locked by the operation's Enum in mctl-api's registry;
        # passing it explicitly makes a drift there fail at submit time
        # rather than silently running a different run mode.
        assert seen[0].params == {"mode": "incident-responder"}
        assert result.responder_result.workflow_name == "mctl-agents-incidents-fake"
        assert result.responder_result.succeeded is True

    async def test_failed_argo_run_is_returned_not_raised(self, env):
        """A failed responder run is an ordinary tick outcome — the next
        tick starts over — so it must come back as a phase on the result,
        not blow up the workflow execution."""
        fake_submit, _ = _capturing_submit(phase="Failed")

        result = await _run(env, fake_submit)

        assert result.responder_result.phase == "Failed"
        assert result.responder_result.succeeded is False
