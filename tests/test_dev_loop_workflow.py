"""DevLoopWorkflow orchestration tests: run the real workflow definition
against temporalio's time-skipping test environment, with fake activity
implementations standing in for the real HTTP-calling ones (those are
covered directly, against mocked HTTP, in test_temporal_activities.py).

Needs network access once, to fetch Temporal's bundled time-skipping test
server binary (cached under ~/.cache after the first run).
"""
from __future__ import annotations

import uuid

import anyio
import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.registry import ResolvedRelease
from orchestrator.temporal.activities.state import ExecutionRecord
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-dev-loop"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _fake_activities(*, released: bool, investigate_phase: str = "Succeeded"):
    """Fakes with the same names/signatures as the real activities, so
    Worker(..., activities=[...]) can register them under the exact
    activity names DevLoopWorkflow's workflow.execute_activity references
    (temporalio matches by function reference at test-registration time,
    not by name, when passed as callables like this)."""

    resolved = {
        "issue-investigator": ResolvedRelease(
            agent="issue-investigator", environment="production", version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
        ),
        "implementer": ResolvedRelease(
            agent="implementer", environment="production", version="2.0.0", image_ref="ghcr.io/x@sha256:bbb"
        ),
    }

    @activity.defn(name="resolve_agent_release")
    async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
        return resolved.get(agent) if released else None

    calls: list[str] = []
    investigate_ran = anyio.Event()

    @activity.defn(name="submit_and_wait")
    async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
        calls.append(input.operation)
        if input.operation == "mctl-agents-investigate":
            assert input.params.get("issue_url")
            investigate_ran.set()
            return WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase=investigate_phase)
        return WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded")

    @activity.defn(name="record_execution")
    async def fake_record_execution(record: ExecutionRecord) -> None:
        return None

    activities = [fake_resolve_agent_release, fake_submit_and_wait, fake_record_execution]
    return activities, calls, investigate_ran


class TestDevLoopWorkflow:
    async def test_investigate_then_wait_then_implement_after_approval(self, env):
        activities, calls, investigate_ran = _fake_activities(released=True)
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/1"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )

            # Investigate must run before any approval is signalled.
            with anyio.fail_after(10):
                await investigate_ran.wait()

            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.investigate.phase == "Succeeded"
        assert result.implement is not None
        assert result.implement.phase == "Succeeded"
        assert calls == ["mctl-agents-investigate", "mctl-agents-implement"]

    async def test_approve_signal_tolerates_a_null_payload(self, env):
        """mctl-api's Go client signals via SignalWorkflow(..., arg=nil),
        which the Go SDK's data converter encodes as one JSON-null payload —
        not zero payloads. A zero-arg handler crashes the workflow task on
        every such call (observed live 2026-08-06 on
        dev-loop-mctlhq-mctl-academy-14, permanently wedging the run).
        Reproduce that exact call shape by signalling with an explicit None
        argument, which the Python SDK's own client would never do on its
        own (see the zero-arg signal in the test above)."""
        activities, calls, investigate_ran = _fake_activities(released=True)
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/1"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )

            with anyio.fail_after(10):
                await investigate_ran.wait()

            await handle.signal(DevLoopWorkflow.approve, None)
            result = await handle.result()

        assert result.implement is not None
        assert result.implement.phase == "Succeeded"

    async def test_implement_step_is_scoped_to_issues_own_repo(self, env):
        """The implement CWFT must only be allowed to touch proposals under
        this issue's own repo (the `service` param) — otherwise approve()
        could implement an unrelated already-accepted proposal in a
        different repo (see the module docstring's known-simplification
        note)."""
        seen_params: dict[str, dict[str, str]] = {}
        investigate_ran = anyio.Event()

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            return None

        @activity.defn(name="submit_and_wait")
        async def capturing_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            seen_params[input.operation] = input.params
            if input.operation == "mctl-agents-investigate":
                investigate_ran.set()
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

        @activity.defn(name="record_execution")
        async def fake_record_execution(record: ExecutionRecord) -> None:
            return None

        activities = [fake_resolve_agent_release, capturing_submit_and_wait, fake_record_execution]

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/42"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            await handle.result()

        assert seen_params["mctl-agents-implement"]["service"] == "mctl-telegram"

    async def test_unpinned_release_is_not_recorded_as_used(self, env):
        """resolve_agent_release can return a release with no image_ref (a
        version exists but couldn't be turned into a pullable image) — the
        CWFT then runs its own baked-in default, NOT that version. The
        execution record must reflect that (empty version/image_ref), or the
        audit trail would falsely claim a specific registry version produced
        the result."""
        recorded: list[ExecutionRecord] = []

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            # version resolved, but no image_ref -- e.g. the /versions
            # response had no matching entry to build one from.
            return ResolvedRelease(agent=agent, environment=environment, version="9.9.9", image_ref="")

        @activity.defn(name="submit_and_wait")
        async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

        @activity.defn(name="record_execution")
        async def capturing_record_execution(record: ExecutionRecord) -> None:
            recorded.append(record)

        activities = [fake_resolve_agent_release, fake_submit_and_wait, capturing_record_execution]

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/5"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            await handle.signal(DevLoopWorkflow.approve)
            await handle.result()

        # Both steps resolve an unpinned release in this fake (investigate,
        # then implement after approve) -- neither should ever be recorded
        # as if a specific version had actually run.
        assert len(recorded) == 2
        for record in recorded:
            assert record.version == ""
            assert record.image_ref == ""
            assert record.target_repo == "mctl-telegram"

    async def test_failed_investigate_never_implements(self, env):
        activities, calls, _investigate_ran = _fake_activities(released=True, investigate_phase="Failed")
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/2"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            result = await handle.result()

        assert result.investigate.phase == "Failed"
        assert result.implement is None
        assert calls == ["mctl-agents-investigate"]

    async def test_unregistered_agent_falls_back_to_cwft_default(self, env):
        """resolve_agent_release returning None (nothing ever promoted) must
        not fail the workflow — DevLoopWorkflow omits agent_image/agent_version
        entirely and lets the CWFT's own baked-in default apply."""

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            return None

        seen_params: dict[str, dict[str, str]] = {}
        investigate_ran = anyio.Event()

        @activity.defn(name="submit_and_wait")
        async def capturing_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            seen_params[input.operation] = input.params
            if input.operation == "mctl-agents-investigate":
                investigate_ran.set()
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

        @activity.defn(name="record_execution")
        async def fake_record_execution(record: ExecutionRecord) -> None:
            return None

        activities = [fake_resolve_agent_release, capturing_submit_and_wait, fake_record_execution]

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/3"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            await handle.result()

        assert "agent_image" not in seen_params["mctl-agents-investigate"]
        assert "agent_version" not in seen_params["mctl-agents-investigate"]

    async def test_persistent_record_execution_failure_does_not_fail_workflow(self, env):
        """record_execution is an audit-trail write, not the real work — a
        persistent failure there (e.g. mctl-api's executions endpoint down)
        must not fail the whole workflow or wedge wait_condition, since the
        underlying CWFT run it's trying to record already succeeded."""

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            return None

        @activity.defn(name="submit_and_wait")
        async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

        @activity.defn(name="record_execution")
        async def always_failing_record_execution(record: ExecutionRecord) -> None:
            raise ValueError("mctl-api executions endpoint unreachable")

        activities = [fake_resolve_agent_release, fake_submit_and_wait, always_failing_record_execution]

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/4"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.investigate.phase == "Succeeded"
        assert result.implement is not None
        assert result.implement.phase == "Succeeded"
