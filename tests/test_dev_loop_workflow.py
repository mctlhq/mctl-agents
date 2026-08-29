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
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.activities.registry import ResolvedRelease
from orchestrator.temporal.activities.state import ExecutionRecord
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef

MERGED_PR = PRState(
    found=True,
    pr_url="https://github.com/mctlhq/mctl-telegram/pull/77",
    repo="mctlhq/mctl-telegram",
    number=77,
    state="MERGED",
    merged=True,
    merge_commit="cafe1234",
)


@activity.defn(name="find_proposal_slug")
async def _fake_find_proposal_slug(service: str, issue_number: str) -> str | None:
    """Deterministic fake mirroring the real activity's contract: the slug
    for issue N always starts with issue-<N>-."""
    return f"issue-{issue_number}-fake-title"


pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-dev-loop"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _fake_activities(
    *,
    released: bool,
    investigate_phase: str = "Succeeded",
    pr_states: list[PRState] | None = None,
):
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

    # Merge-detection polls this after implement; the sequence is consumed
    # one state per poll, then the last entry repeats (a real PR's state is
    # sticky once terminal). Default: merged on the first poll.
    sequence = pr_states if pr_states is not None else [MERGED_PR]
    poll_index = {"i": 0}

    @activity.defn(name="get_pr_state")
    async def fake_get_pr_state(service: str, slug: str) -> PRState:
        i = min(poll_index["i"], len(sequence) - 1)
        poll_index["i"] += 1
        return sequence[i]

    activities = [
        fake_resolve_agent_release,
        fake_submit_and_wait,
        fake_record_execution,
        _fake_find_proposal_slug,
        fake_get_pr_state,
    ]
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
        assert result.approve is not None
        assert result.approve.phase == "Succeeded"
        assert calls == ["mctl-agents-investigate", "mctl-agents-approve", "mctl-agents-implement"]

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

        activities = [
            fake_resolve_agent_release,
            capturing_submit_and_wait,
            fake_record_execution,
            _fake_find_proposal_slug,
        ]

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
        assert seen_params["mctl-agents-implement"]["slug"] == "issue-42-fake-title"
        # The atomic approve flip is scoped to exactly the same proposal.
        assert seen_params["mctl-agents-approve"]["service"] == "mctl-telegram"
        assert seen_params["mctl-agents-approve"]["slug"] == "issue-42-fake-title"

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

        activities = [
            fake_resolve_agent_release,
            fake_submit_and_wait,
            capturing_record_execution,
            _fake_find_proposal_slug,
        ]

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

        # Investigate and implement record with no pinned release in this
        # fake -- neither should ever be recorded as if a specific version
        # had actually run. The approve flip deliberately does NOT write an
        # executions row (it is not an SDK agent run; its audit trail is the
        # .status.yaml approval block + gitops commit + workflow history).
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

        activities = [
            fake_resolve_agent_release,
            capturing_submit_and_wait,
            fake_record_execution,
            _fake_find_proposal_slug,
        ]

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

        activities = [
            fake_resolve_agent_release,
            fake_submit_and_wait,
            always_failing_record_execution,
            _fake_find_proposal_slug,
        ]

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

    async def test_missing_proposal_slug_fails_instead_of_unscoped_implement(self, env):
        """When no issue-<N>-* proposal dir exists after approval, the
        workflow must fail loudly rather than fall back to an unscoped
        implement run — unscoped, it could implement a different accepted
        proposal in the repo (the same-repo race of mctl-agents#203)."""
        implement_calls: list[str] = []
        investigate_ran = anyio.Event()

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            return None

        @activity.defn(name="submit_and_wait")
        async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            if input.operation == "mctl-agents-investigate":
                investigate_ran.set()
            else:
                implement_calls.append(input.operation)
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

        @activity.defn(name="record_execution")
        async def fake_record_execution(record: ExecutionRecord) -> None:
            return None

        @activity.defn(name="find_proposal_slug")
        async def missing_find_proposal_slug(service: str, issue_number: str) -> str | None:
            return None

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=[
                fake_resolve_agent_release,
                fake_submit_and_wait,
                fake_record_execution,
                missing_find_proposal_slug,
            ],
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/7"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            with pytest.raises(WorkflowFailureError) as excinfo:
                await handle.result()

        assert "refusing an unscoped implement run" in str(excinfo.value.cause)

    async def test_approve_signal_payload_carries_approver_identity(self, env):
        """A structured approve payload ({"approver": ...}) must reach the
        approve CWFT's params for the audit trail; a legacy bare signal (see
        the other tests) falls back to "unknown" — both are exercised across
        this file."""
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

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=[
                fake_resolve_agent_release,
                capturing_submit_and_wait,
                fake_record_execution,
                _fake_find_proposal_slug,
            ],
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/8"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve, {"approver": "mashkovd"})
            await handle.result()

        assert seen_params["mctl-agents-approve"]["approver"] == "mashkovd"

    async def test_bare_approve_signal_records_unknown_approver(self, env):
        """Legacy senders signal approve() with no payload — that must stay
        valid, with the approve CWFT told approver=unknown."""
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

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=[
                fake_resolve_agent_release,
                capturing_submit_and_wait,
                fake_record_execution,
                _fake_find_proposal_slug,
            ],
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/9"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            await handle.result()

        assert seen_params["mctl-agents-approve"]["approver"] == "unknown"

    async def test_failed_approve_flip_never_implements(self, env):
        """If the approve CWFT fails (missing proposal dir, unexpected
        status, push failure), the loop must stop there: running implement
        anyway would be a silent no-op against a proposal that never became
        accepted."""
        calls: list[str] = []
        resolved_agents: list[str] = []
        investigate_ran = anyio.Event()

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            resolved_agents.append(agent)
            return None

        @activity.defn(name="submit_and_wait")
        async def failing_approve_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            calls.append(input.operation)
            if input.operation == "mctl-agents-investigate":
                investigate_ran.set()
                return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")
            if input.operation == "mctl-agents-approve":
                return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Failed")
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

        @activity.defn(name="record_execution")
        async def fake_record_execution(record: ExecutionRecord) -> None:
            return None

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=[
                fake_resolve_agent_release,
                failing_approve_submit_and_wait,
                fake_record_execution,
                _fake_find_proposal_slug,
            ],
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/10"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.approve is not None
        assert result.approve.phase == "Failed"
        assert result.implement is None
        assert calls == ["mctl-agents-investigate", "mctl-agents-approve"]
        # The implementer registry lookup must be skipped entirely on a
        # failed flip — the resolve happens only after approval is durable
        # (codex P1 on PR #212).
        assert resolved_agents == ["issue-investigator"]

    async def test_merge_detection_reports_merged_pr(self, env):
        """Stage 6.1 (#214): after implement succeeds, the loop polls
        get_pr_state and returns the terminal PR state in the result."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, _calls, investigate_ran = _fake_activities(
            released=True, pr_states=[open_pr, open_pr, MERGED_PR]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/77"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.implement is not None and result.implement.phase == "Succeeded"
        assert result.pr is not None
        assert result.pr.state == "MERGED"
        assert result.pr.merged is True
        assert result.pr.merge_commit == "cafe1234"

    async def test_merge_detection_gives_up_when_pr_link_never_appears(self, env):
        """A proposal whose .status.yaml never gains a pr: link stops the
        watch after the grace polls (result.pr is None), instead of polling
        for the full 14-day deadline."""
        activities, _calls, investigate_ran = _fake_activities(
            released=True, pr_states=[PRState(found=False)]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/78"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.implement is not None and result.implement.phase == "Succeeded"
        assert result.pr is None

    async def test_merge_detection_deadline_returns_last_open_state(self, env):
        """A PR still open when MERGE_WATCH_DEADLINE expires is returned
        as-is (state OPEN) — the workflow is bounded, not eternal. The
        time-skipping environment fast-forwards the 14 days of sleeps."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, _calls, investigate_ran = _fake_activities(
            released=True, pr_states=[open_pr]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/79"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(30):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.pr is not None
        assert result.pr.state == "OPEN"
        assert result.pr.merged is False
