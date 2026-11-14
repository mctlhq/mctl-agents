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

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.registry import ResolvedRelease
from orchestrator.temporal.activities.state import ExecutionRecord
from orchestrator.temporal.workflows.incidents import IncidentLoopWorkflow
from tests.temporal_harness import Worker  # polls the execution queue too — see #251

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


def _capturing_resolve(release: ResolvedRelease | None):
    """Stand in for the registry lookup. None models an unpublished agent."""
    seen: list[tuple[str, str]] = []

    @activity.defn(name="resolve_agent_release")
    async def fake_resolve(agent: str, environment: str) -> ResolvedRelease | None:
        seen.append((agent, environment))
        return release

    return fake_resolve, seen


def _failing_resolve():
    """Stand in for the registry being DOWN, as opposed to empty."""
    attempts: list[tuple[str, str]] = []

    @activity.defn(name="resolve_agent_release")
    async def fake_resolve(agent: str, environment: str) -> ResolvedRelease | None:
        attempts.append((agent, environment))
        raise RuntimeError("mctl-api is down")

    return fake_resolve, attempts


def _capturing_record(fail: bool = False):
    seen: list[ExecutionRecord] = []

    @activity.defn(name="record_execution")
    async def fake_record(record: ExecutionRecord) -> None:
        seen.append(record)
        if fail:
            raise RuntimeError("mctl-api is down")

    return fake_record, seen


async def _run(env, *activity_fns) -> object:
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[IncidentLoopWorkflow],
        activities=list(activity_fns),
    ):
        return await env.client.execute_workflow(
            IncidentLoopWorkflow.run,
            id=f"incident-loop-test-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )


PINNED = ResolvedRelease(
    agent="incident-responder",
    environment="production",
    version="1.35.0",
    image_ref="ghcr.io/mctlhq/mctl-agents:1.35.0",
)


class TestIncidentLoopWorkflow:
    async def test_submits_the_incidents_operation_in_responder_mode(self, env):
        fake_submit, seen = _capturing_submit()

        fake_resolve, _ = _capturing_resolve(PINNED)
        fake_record, _ = _capturing_record()

        result = await _run(env, fake_submit, fake_resolve, fake_record)

        assert [i.operation for i in seen] == ["mctl-agents-incidents"]
        # mode is locked by the operation's Enum in mctl-api's registry;
        # passing it explicitly makes a drift there fail at submit time
        # rather than silently running a different run mode.
        assert seen[0].params["mode"] == "incident-responder"
        assert result.responder_result.workflow_name == "mctl-agents-incidents-fake"
        assert result.responder_result.succeeded is True

    async def test_failed_argo_run_is_returned_not_raised(self, env):
        """A failed responder run is an ordinary tick outcome — the next
        tick starts over — so it must come back as a phase on the result,
        not blow up the workflow execution."""
        fake_submit, _ = _capturing_submit(phase="Failed")
        fake_resolve, _ = _capturing_resolve(PINNED)
        fake_record, _ = _capturing_record()

        result = await _run(env, fake_submit, fake_resolve, fake_record)

        assert result.responder_result.phase == "Failed"
        assert result.responder_result.succeeded is False

    async def test_the_responder_image_is_not_pinned(self, env):
        """The image comes only from gitops (mctlhq/mctl-gitops#1980).

        The responder is the template holding MCTL_TOKEN, and every release
        before its session was built without Bash still has a shell. A pin
        from the registry, a promotion or a rollback could bring one back
        next to the token, so the loop neither asks the registry nor sends
        agent_image or agent_version. This replaces the registry pin of #149
        for this agent only.
        """
        fake_submit, seen = _capturing_submit()
        fake_resolve, resolved = _capturing_resolve(PINNED)
        fake_record, recorded = _capturing_record()

        result = await _run(env, fake_submit, fake_resolve, fake_record)

        assert result.responder_result.succeeded is True
        assert resolved == []
        assert set(seen[0].params) == {"mode"}
        # The execution record says nothing about a version it did not choose.
        assert recorded[0].version == ""
        assert recorded[0].image_ref == ""

    async def test_a_registry_outage_cannot_stop_the_tick(self, env):
        """The registry is not asked at all, so it being down changes nothing."""
        fake_submit, seen = _capturing_submit()
        fake_resolve, attempts = _failing_resolve()
        fake_record, _ = _capturing_record()

        result = await _run(env, fake_submit, fake_resolve, fake_record)

        assert attempts == []
        assert result.responder_result.succeeded is True
        assert set(seen[0].params) == {"mode"}

    async def test_the_run_is_traceable_to_its_argo_workflow(self, env):
        """#149's acceptance criterion: Temporal workflow ID -> Argo name.

        The dev loop writes an execution record for every CWFT it submits;
        this loop wrote none, so the only trace of what an unattended
        incident run actually did was a log line in a since-recycled pod.
        """
        fake_submit, _ = _capturing_submit()
        fake_resolve, _ = _capturing_resolve(PINNED)
        fake_record, recorded = _capturing_record()

        await _run(env, fake_submit, fake_resolve, fake_record)

        assert len(recorded) == 1
        assert recorded[0].agent == "incident-responder"
        assert recorded[0].argo_workflow_name == "mctl-agents-incidents-fake"
        assert recorded[0].phase == "Succeeded"
        assert recorded[0].temporal_workflow_id.startswith("incident-loop-test-")

    async def test_a_failed_run_is_recorded_too(self, env):
        """A tick that failed is exactly the one worth being able to trace."""
        fake_submit, _ = _capturing_submit(phase="Failed")
        fake_resolve, _ = _capturing_resolve(PINNED)
        fake_record, recorded = _capturing_record()

        await _run(env, fake_submit, fake_resolve, fake_record)

        assert [r.phase for r in recorded] == ["Failed"]

    async def test_an_audit_write_failure_does_not_lose_the_tick(self, env):
        """The CWFT has already run by the time this is called.

        Letting an mctl-api blip fail the tick would turn a missing audit
        row into a missed incident response — strictly the worse trade.
        """
        fake_submit, _ = _capturing_submit()
        fake_resolve, _ = _capturing_resolve(PINNED)
        fake_record, attempts = _capturing_record(fail=True)

        result = await _run(env, fake_submit, fake_resolve, fake_record)

        assert result.responder_result.succeeded is True
        assert len(attempts) > 1, "the audit write should be retried before giving up"
