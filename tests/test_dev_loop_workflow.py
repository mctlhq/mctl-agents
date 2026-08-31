"""DevLoopWorkflow orchestration tests: run the real workflow definition
against temporalio's time-skipping test environment, with fake activity
implementations standing in for the real HTTP-calling ones (those are
covered directly, against mocked HTTP, in test_temporal_activities.py).

Needs network access once, to fetch Temporal's bundled time-skipping test
server binary (cached under ~/.cache after the first run).
"""
from __future__ import annotations

import asyncio
import logging
import uuid

import anyio
import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.deploy_state import DeployStatus, DeployTarget, ReleaseInfo
from orchestrator.temporal.activities.incidents import Incident, IncidentQueryResult
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.activities.registry import ResolvedRelease
from orchestrator.temporal.activities.state import ExecutionRecord
from orchestrator.temporal.workflows import dev_loop
from orchestrator.temporal.workflows.dev_loop import (
    INCIDENT_WATCH_WINDOW,
    SHEPHERD_TICK_EVERY_POLLS,
    SHEPHERD_TICKS_MAX,
    DevLoopWorkflow,
    IssueRef,
)

_SENTINEL_TARGET = DeployTarget(team="admins", app="mctl-telegram")
_DEFAULT_RELEASE = ReleaseInfo(tag="9.9.9", published_at="2026-08-30T00:00:00Z")

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
    pr_state_raises_after: int | None = None,
    pr_state_error_type: str | None = None,
    pr_state_raises_at: set[int] | None = None,
    shepherd_fails: bool = False,
    deploy_target: DeployTarget | None = _SENTINEL_TARGET,
    release: ReleaseInfo | None = _DEFAULT_RELEASE,
    deploy_statuses: list[DeployStatus] | None = None,
    release_lookup_bug: bool = False,
    deploy_status_bug: bool = False,
    incidents: list[Incident] | None = None,
    incident_reads_fail: bool = False,
    incident_query: dict[str, str] | None = None,
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
        "shepherd": ResolvedRelease(
            agent="shepherd", environment="production", version="3.0.0", image_ref="ghcr.io/x@sha256:ccc"
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
        if input.operation == "mctl-agents-shepherd":
            # In-loop tick (#213) must be scoped to exactly this proposal.
            assert input.params.get("service")
            assert input.params.get("slug", "").startswith("issue-")
            # ...and must run the RELEASED shepherd, not the CWFT's baked-in
            # default, so a promotion/rollback reaches in-loop ticks too
            # (Codex P2 on PR #230).
            if released:
                assert input.params.get("agent_image") == "ghcr.io/x@sha256:ccc"
                assert input.params.get("agent_version") == "shepherd@3.0.0"
            if shepherd_fails:
                from temporalio.exceptions import ApplicationError

                raise ApplicationError("tick exploded", non_retryable=True)
            return WorkflowResult(workflow_name="mctl-agents-shepherd-fake", phase="Succeeded")
        return WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded")

    @activity.defn(name="record_execution")
    async def fake_record_execution(record: ExecutionRecord) -> None:
        return None

    # Stages 6.2/6.3 (#215). Defaults describe the happy path: the repo
    # deploys an app, a release appears, and it goes Healthy at once.
    @activity.defn(name="resolve_deploy_target")
    async def fake_resolve_deploy_target(repo: str) -> DeployTarget | None:
        return deploy_target

    @activity.defn(name="get_release_after")
    async def fake_get_release_after(repo: str, after: str) -> ReleaseInfo | None:
        if release_lookup_bug:
            from temporalio.exceptions import ApplicationError

            # NOT a ProposalListingError: an unexpected defect, which must
            # not be retried for the whole lookup deadline.
            raise ApplicationError("boom", type="TypeError", non_retryable=True)
        return release

    deploy_sequence = deploy_statuses if deploy_statuses is not None else [
        DeployStatus(found=True, image_tag="9.9.9", health="Healthy", sync_status="Synced")
    ]
    deploy_index = {"i": 0}

    @activity.defn(name="list_service_incidents")
    async def fake_list_service_incidents(service: str, since: str) -> IncidentQueryResult:
        if incident_query is not None:
            incident_query.setdefault("since", since)
        if incident_reads_fail:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError("incident store down", non_retryable=True)
        return IncidentQueryResult(incidents=list(incidents or []))

    @activity.defn(name="get_deploy_status")
    async def fake_get_deploy_status(team: str, app: str) -> DeployStatus:
        if deploy_status_bug:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError("boom", type="KeyError", non_retryable=True)
        i = min(deploy_index["i"], len(deploy_sequence) - 1)
        deploy_index["i"] += 1
        return deploy_sequence[i]

    # Merge-detection polls this after implement; the sequence is consumed
    # one state per poll, then the last entry repeats (a real PR's state is
    # sticky once terminal). Default: merged on the first poll.
    sequence = pr_states if pr_states is not None else [MERGED_PR]
    poll_index = {"i": 0}

    @activity.defn(name="get_pr_state")
    async def fake_get_pr_state(service: str, slug: str) -> PRState:
        # `raises_after` is sticky (outage); `raises_at` fails those polls
        # only, then recovers — a transient hiccup, and it does NOT consume
        # an entry from `sequence`.
        if pr_state_raises_at is not None and poll_index["i"] in pr_state_raises_at:
            from temporalio.exceptions import ApplicationError

            pr_state_raises_at.discard(poll_index["i"])
            raise ApplicationError(
                "github unreachable", type=pr_state_error_type, non_retryable=True
            )
        if pr_state_raises_after is not None and poll_index["i"] >= pr_state_raises_after:
            from temporalio.exceptions import ApplicationError

            raise ApplicationError(
                "github unreachable", type=pr_state_error_type, non_retryable=True
            )
        i = min(poll_index["i"], len(sequence) - 1)
        poll_index["i"] += 1
        return sequence[i]

    activities = [
        fake_resolve_agent_release,
        fake_submit_and_wait,
        fake_record_execution,
        _fake_find_proposal_slug,
        fake_get_pr_state,
        fake_resolve_deploy_target,
        fake_get_release_after,
        fake_get_deploy_status,
        fake_list_service_incidents,
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

    async def test_merge_detection_survives_read_failures_until_deadline(self, env):
        """A get_pr_state that fails even its Temporal retries must not
        abort the watch (agy P2): the loop rides the outage out to the
        deadline and ends with the last observed state — and it must never
        fail a loop whose implement already succeeded."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            pr_states=[open_pr],
            pr_state_raises_after=1,
            # The type get_pr_state wraps all expected read failures in —
            # the loop treats exactly this (and activity timeouts) as
            # transient and rides it out.
            pr_state_error_type="ProposalListingError",
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/80"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.implement is not None and result.implement.phase == "Succeeded"
        assert result.pr is not None
        assert result.pr.state == "OPEN"

    async def test_merge_detection_preserves_unresolvable_recorded_pr(self, env):
        """A recorded PR link that 404s (deleted repo, lost token access)
        ends the watch after the grace polls with the reference preserved
        in the result — not the misleading 'no PR link' None."""
        vanished = PRState(
            found=False,
            pr_url="https://github.com/mctlhq/mctl-telegram/pull/81",
            repo="mctlhq/mctl-telegram",
            number=81,
        )
        activities, _calls, investigate_ran = _fake_activities(
            released=True, pr_states=[vanished]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/81"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.pr is not None
        assert result.pr.found is False
        assert (result.pr.repo, result.pr.number) == ("mctlhq/mctl-telegram", 81)

    async def test_merge_detection_ends_when_status_file_vanishes_after_found(self, env):
        """A status file deleted AFTER the PR was once resolved must end
        the watch after the grace polls with the last found state — not
        leave a zombie loop polling a missing file to the deadline
        (agy P3)."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, _calls, investigate_ran = _fake_activities(
            released=True, pr_states=[open_pr, PRState(found=False)]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/82"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.pr is not None
        assert result.pr.found is True
        assert result.pr.state == "OPEN"

    async def test_merge_detection_stops_on_unexpected_activity_bug(self, env):
        """An unexpected (non-ProposalListingError) failure is a bug in the
        activity, not weather — the watch ends immediately with the last
        observed state instead of masking the defect for 14 days, and the
        workflow itself still succeeds."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            pr_states=[open_pr],
            pr_state_raises_after=1,
            pr_state_error_type="AttributeError",
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/83"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.implement is not None and result.implement.phase == "Succeeded"
        assert result.pr is not None
        assert result.pr.state == "OPEN"

    async def test_merge_detection_keeps_resolved_state_over_later_404(self, env):
        """A recorded PR that 404s AFTER a successful resolve must not
        downgrade the result: the watch ends with the confirmed OPEN state,
        not the poorer found=False reference."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        vanished_with_ref = PRState(
            found=False,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
        )
        activities, _calls, investigate_ran = _fake_activities(
            released=True, pr_states=[open_pr, vanished_with_ref]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/84"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.pr is not None
        assert result.pr.found is True
        assert result.pr.state == "OPEN"

    async def test_shepherd_tick_runs_while_pr_stays_open(self, env):
        """Stage 6.1 review loop (#213): while the PR is open, every 8th
        poll submits a slug-scoped shepherd tick; the merged poll after it
        ends the watch."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, calls, investigate_ran = _fake_activities(
            released=True, pr_states=[open_pr] * 8 + [MERGED_PR]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/85"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.pr is not None and result.pr.state == "MERGED"
        assert calls == [
            "mctl-agents-investigate",
            "mctl-agents-approve",
            "mctl-agents-implement",
            "mctl-agents-shepherd",
        ]

    async def test_shepherd_in_loop_query_answers_true_during_the_watch(self, env):
        """The sweeper asks the workflow, not its status (Codex P1 on #230).

        While the merge watch runs under the shepherd-in-loop patch, the
        query must already answer True — the cron has to stand down from
        the moment the watch starts, not from the first tick 4 h later.
        """
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        seen: list[bool] = []
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
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/87"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            # Before the watch exists the workflow is not shepherding.
            seen.append(await handle.query(DevLoopWorkflow.shepherd_in_loop))
            await handle.signal(DevLoopWorkflow.approve)
            await handle.result()
            seen.append(await handle.query(DevLoopWorkflow.shepherd_in_loop))

        assert seen[0] is False
        assert seen[1] is True

    async def _run_to_completion(self, env, activities, investigate_ran, issue: int):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=f"https://github.com/mctlhq/mctl-telegram/issues/{issue}"),
            id=f"dev-loop-test-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        with anyio.fail_after(10):
            await investigate_ran.wait()
        await handle.signal(DevLoopWorkflow.approve)
        return await handle.result()

    async def test_deploy_observed_healthy_on_the_released_tag(self, env):
        """Stage 6.2/6.3 happy path (#215): merged → release → Healthy."""
        activities, _calls, investigate_ran = _fake_activities(released=True)
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 90)

        assert result.deploy is not None
        assert result.deploy.outcome == "healthy"
        assert result.deploy.release_tag == "9.9.9"
        assert result.deploy.image_tag == "9.9.9"
        assert (result.deploy.team, result.deploy.app) == ("admins", "mctl-telegram")

    async def test_deploy_waits_for_the_tag_to_catch_up(self, env):
        """Synced/Healthy on the PREVIOUS tag is not this release landing.

        The app is Healthy throughout — on the old image. Reporting that as
        verified would call every rollout successful the instant it was
        asked, before the new tag ever reached the cluster.
        """
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            deploy_statuses=[
                DeployStatus(found=True, image_tag="9.9.8", health="Healthy", sync_status="Synced"),
                DeployStatus(found=True, image_tag="9.9.9", health="Progressing", sync_status="Synced"),
                DeployStatus(found=True, image_tag="9.9.9", health="Healthy", sync_status="Synced"),
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 91)

        assert result.deploy is not None and result.deploy.outcome == "healthy"
        assert result.deploy.image_tag == "9.9.9"

    async def test_deploy_unverified_when_it_never_goes_healthy(self, env):
        """The deadline passing is an observation, not a workflow failure."""
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            deploy_statuses=[
                DeployStatus(found=True, image_tag="9.9.9", health="Degraded", sync_status="Synced")
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 92)

        assert result.deploy is not None
        assert result.deploy.outcome == "unverified"
        assert result.deploy.health == "Degraded"
        # The rest of the loop still reports success — implement landed and
        # the PR merged; an unverified rollout must not retro-fail either.
        assert result.pr is not None and result.pr.state == "MERGED"
        assert result.implement is not None and result.implement.succeeded

    async def test_no_release_for_a_docs_only_merge(self, env):
        """release-please cutting nothing is normal, not a fault."""
        activities, _calls, investigate_ran = _fake_activities(released=True, release=None)
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 93)

        assert result.deploy is not None and result.deploy.outcome == "no-release"

    async def test_a_bug_in_the_release_lookup_is_not_retried_for_the_deadline(self, env):
        """Same rule _watch_pr already follows: mask transport, not defects.

        An unexpected exception type is a bug in the activity; retrying it
        for the whole 20-minute lookup window would hide it behind a
        plausible-looking no-release.
        """
        activities, _calls, investigate_ran = _fake_activities(
            released=True, release_lookup_bug=True
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 101)

        assert result.deploy is not None
        # Not "no-release": that would read as a normal outcome and hide
        # the defect (agy P2).
        assert result.deploy.outcome == "unverified"
        assert "non-transient" in (result.deploy.detail or "")

    async def test_a_bug_in_the_status_read_ends_the_verify_immediately(self, env):
        activities, _calls, investigate_ran = _fake_activities(
            released=True, deploy_status_bug=True
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 102)

        assert result.deploy is not None and result.deploy.outcome == "unverified"
        assert "non-transient" in (result.deploy.detail or "")

    async def test_no_target_when_the_repo_deploys_no_app(self, env):
        """A repo whose release only bumps cluster templates has nothing to verify."""
        activities, _calls, investigate_ran = _fake_activities(released=True, deploy_target=None)
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 94)

        assert result.deploy is not None and result.deploy.outcome == "no-target"

    async def test_deploy_verifies_on_sync_health_when_no_image_tag_is_reported(self, env):
        """Platform apps (mctl-api itself) resolve no service record.

        mctl-api reports argocd health/sync but no imageTag for those, so
        waiting for a tag match would time out every such loop.
        """
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            deploy_statuses=[
                # Stale: ArgoCD last synced BEFORE this release existed.
                DeployStatus(
                    found=True,
                    image_tag=None,
                    health="Healthy",
                    sync_status="Synced",
                    updated_at="2026-08-29T00:00:00Z",
                ),
                DeployStatus(
                    found=True,
                    image_tag=None,
                    health="Healthy",
                    sync_status="Synced",
                    updated_at="2026-08-30T00:01:00Z",
                ),
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 95)

        assert result.deploy is not None and result.deploy.outcome == "healthy"
        assert result.deploy.image_tag is None

    async def test_tagless_app_already_healthy_on_the_old_revision_is_not_verified(self, env):
        """The freshness gate, stated as its own case (claude P3).

        A platform app reports no image tag, so Healthy/Synced alone is
        satisfied by the state it was in BEFORE this release synced. With
        ArgoCD's updatedAt permanently older than the release, the rollout
        must never be reported as verified.
        """
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            deploy_statuses=[
                DeployStatus(
                    found=True,
                    image_tag=None,
                    health="Healthy",
                    sync_status="Synced",
                    updated_at="2026-08-29T00:00:00Z",
                )
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 103)

        assert result.deploy is not None and result.deploy.outcome == "unverified"

    async def test_fractional_seconds_do_not_read_as_older(self, env):
        """agy P2: ArgoCD emits fractional seconds, GitHub does not.

        "…:00.500Z" sorts BEFORE "…:00Z" as a string, because "." < "Z",
        so a lexicographic compare would call a sync that happened half a
        second AFTER the release older than it — and never verify.
        """
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            release=ReleaseInfo(tag="9.9.9", published_at="2026-08-30T00:00:00Z"),
            deploy_statuses=[
                DeployStatus(
                    found=True,
                    image_tag=None,
                    health="Healthy",
                    sync_status="Synced",
                    updated_at="2026-08-30T00:00:00.500Z",
                )
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 104)

        assert result.deploy is not None and result.deploy.outcome == "healthy"

    async def test_a_timestamp_without_an_offset_does_not_wedge_the_workflow(self, env):
        """agy P1: naive vs aware datetimes raise TypeError.

        Inside the workflow loop that is not a wrong answer, it is a
        workflow task Temporal retries forever on identical input. An
        offset-less timestamp must simply be read as UTC.
        """
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            release=ReleaseInfo(tag="9.9.9", published_at="2026-08-30T00:00:00Z"),
            deploy_statuses=[
                DeployStatus(
                    found=True,
                    image_tag=None,
                    health="Healthy",
                    sync_status="Synced",
                    updated_at="2026-08-30T00:00:01",  # no offset at all
                )
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 106)

        assert result.deploy is not None and result.deploy.outcome == "healthy"

    async def test_a_new_argocd_application_is_waited_for(self, env):
        """A release can introduce the app; ArgoCD registers it a bit later."""
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            deploy_statuses=[
                DeployStatus(found=False),
                DeployStatus(found=False),
                DeployStatus(found=True, image_tag="9.9.9", health="Healthy", sync_status="Synced"),
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 105)

        assert result.deploy is not None and result.deploy.outcome == "healthy"

    async def test_unknown_argocd_application_gives_up_after_the_grace(self, env):
        """Past the grace polls, a name resolving to nothing is a wrong name."""
        activities, _calls, investigate_ran = _fake_activities(
            released=True, deploy_statuses=[DeployStatus(found=False)]
        )  # repeated for every poll — the grace runs out and the watch gives up
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 96)

        assert result.deploy is not None and result.deploy.outcome == "unverified"
        assert "no ArgoCD application" in (result.deploy.detail or "")

    async def test_incident_watch_reports_a_clean_window(self, env):
        """Stage 6.4 (#216): a healthy rollout with no incidents."""
        activities, _calls, investigate_ran = _fake_activities(released=True)
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 97)

        assert result.incidents is not None
        assert result.incidents.watched is True
        assert result.incidents.service == "mctl-telegram"
        assert result.incidents.incidents == []

    async def test_incident_window_opens_before_the_deploy_observation(self, env):
        """agy P2: a bad rollout breaks things WHILE the deploy is watched.

        _observe_deploy can block for over an hour. A window opened after
        it would start past the incidents that rollout caused — exactly
        the ones this stage exists to surface. The queried `since` must
        therefore predate the deploy stage, not follow it.
        """
        query: dict[str, str] = {}
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            incident_query=query,
            deploy_statuses=[
                DeployStatus(found=True, image_tag="9.9.8", health="Progressing", sync_status="Synced"),
                DeployStatus(found=True, image_tag="9.9.9", health="Healthy", sync_status="Synced"),
            ],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 107)

        assert result.incidents is not None
        assert result.incidents.since == query["since"]
        # The real assertion: the reported window spans MORE than
        # INCIDENT_WATCH_WINDOW, which is only possible if `since` was
        # taken before the deploy stage consumed its poll intervals.
        # Comparing since to itself would hold no matter when it was
        # captured — the earlier version of this test did exactly that
        # and would not have caught a revert (claude P2).
        flat_window = int(INCIDENT_WATCH_WINDOW.total_seconds() // 60)
        assert result.incidents.window_minutes > flat_window

    async def test_incident_watch_deduplicates_across_polls(self, env):
        """An incident firing for the whole window is one finding, not six.

        The fake returns the same incident on every poll — reporting it
        once per poll would make a single alert look like a storm.
        """
        activities, _calls, investigate_ran = _fake_activities(
            released=True,
            incidents=[Incident(id="alert-1", title="pods crashlooping", severity="critical")],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 98)

        assert result.incidents is not None
        assert [i.id for i in result.incidents.incidents] == ["alert-1"]
        # An incident is evidence for a human, never a workflow failure:
        # implement, merge and rollout all already succeeded.
        assert result.deploy is not None and result.deploy.outcome == "healthy"

    async def test_incident_read_failure_does_not_end_the_watch(self, env):
        """A failing incident store must not discard the stage's result."""
        activities, _calls, investigate_ran = _fake_activities(
            released=True, incident_reads_fail=True
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 99)

        assert result.incidents is not None
        assert result.incidents.watched is True
        assert "incident read failed" in (result.incidents.detail or "")

    async def test_no_incident_watch_when_nothing_was_released(self, env):
        """no-release means nothing shipped — the window would be someone else's news."""
        activities, _calls, investigate_ran = _fake_activities(released=True, release=None)
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            result = await self._run_to_completion(env, activities, investigate_ran, 100)

        assert result.deploy is not None and result.deploy.outcome == "no-release"
        assert result.incidents is None

    async def test_failed_shepherd_tick_does_not_end_the_watch(self, env):
        """A failing shepherd tick is logged, not fatal — the watch keeps
        polling and still reports the merge."""
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, calls, investigate_ran = _fake_activities(
            released=True,
            pr_states=[open_pr] * 8 + [MERGED_PR],
            shepherd_fails=True,
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/86"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.implement is not None and result.implement.phase == "Succeeded"
        assert result.pr is not None and result.pr.state == "MERGED"
        assert "mctl-agents-shepherd" in calls

    async def test_transient_poll_failure_delays_the_tick_instead_of_dropping_it(
        self, env
    ):
        """A failed read must not consume a tick boundary (#230 P3).

        `poll_index` counts successful reads only. Polls 1-7 see an open
        PR, poll 8 — what would have been the boundary — fails
        transiently, and the poll after it recovers and becomes the 8th
        successful read, so the tick still fires. Counting every attempt
        instead would spend the boundary on the failure and defer the
        tick by a full 8 polls (~4 h).
        """
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, calls, investigate_ran = _fake_activities(
            released=True,
            pr_states=[open_pr] * 8 + [MERGED_PR],
            pr_state_raises_at={7},
            pr_state_error_type="ProposalListingError",
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/87"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.pr is not None and result.pr.state == "MERGED"
        assert "mctl-agents-shepherd" in calls

    async def test_shepherd_ticks_stop_at_the_cap(self, env):
        """SHEPHERD_TICKS_MAX must actually stop ticking (#230 P3).

        Each tick provisions a Hetzner volume, and the shepherd itself
        flips review-stuck after 3 address-review attempts — so a wedged
        PR must not burn one every ~4 h for the full 14-day watch. Drive
        the watch past 13 tick boundaries and assert the 13th produces
        nothing.
        """
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        polls = SHEPHERD_TICK_EVERY_POLLS * (SHEPHERD_TICKS_MAX + 1)
        activities, calls, investigate_ran = _fake_activities(
            released=True, pr_states=[open_pr] * polls + [MERGED_PR]
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/88"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()

        assert result.pr is not None and result.pr.state == "MERGED"
        assert calls.count("mctl-agents-shepherd") == SHEPHERD_TICKS_MAX


SERVICE = "mctl-telegram"
SLUG = "issue-88-fake-title"


@pytest.fixture
def tick_logger(monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
    """A plain logger standing in for `workflow.logger` (#231).

    temporalio's workflow logger is an adapter that asks the workflow
    runtime whether it is replaying; called outside a workflow event loop
    it raises _NotInWorkflowEventLoopError, so the tick helpers below —
    which log on every branch they take — cannot be exercised directly
    without this swap. Replacing the module attribute also makes the log
    lines assertable via caplog, which is how these tests observe that a
    finished tick's exception was actually retrieved.
    """
    logger = logging.getLogger("dev-loop-tick-unit")
    monkeypatch.setattr(dev_loop.workflow, "logger", logger)
    return logger


class TestTickSettling:
    """Direct unit tests for the in-loop tick helpers (#231).

    Deliberately not run through the Temporal harness. The branch that
    matters here — a shepherd tick STILL IN FLIGHT when the watch ends —
    is unreachable under the time-skipping test server: an outstanding
    activity stops it advancing timers, so a workflow-level test can never
    get a poll to land past a running tick, and `_settle_tick` is always
    reached with `tick_task.done()` already true. That is precisely how a
    P1 (`except Exception` does not catch `asyncio.CancelledError`, which
    is a BaseException) shipped through a green suite: the cancel-and-await
    path never executed in CI. These call the methods on a bare
    DevLoopWorkflow instance so that path executes for real.
    """

    async def test_settle_tick_cancels_an_in_flight_tick_without_raising(
        self, tick_logger: logging.Logger
    ) -> None:
        """The P1 regression.

        `_settle_tick` cancels the task and awaits it; `await` on a task
        that has been cancelled re-raises CancelledError in the awaiter.
        Since it inherits from BaseException, an `except Exception` there
        does not catch it — it escapes `_settle_tick`, escapes `_watch_pr`'s
        `finally`, and (because an exception raised in a `finally` replaces
        the pending return) discards the MERGED state the watch was about
        to return and fails a workflow whose implement and merge both
        succeeded.
        """
        workflow_obj = DevLoopWorkflow()
        running = asyncio.Event()

        async def never_finishes() -> None:
            running.set()
            await asyncio.sleep(3600)

        tick_task = asyncio.create_task(never_finishes())
        # Await the task's own signal rather than sleeping: `_settle_tick`
        # must be entered with the tick genuinely in flight, which is the
        # whole point of the test. A task that had not started yet would
        # take the same code path but prove less.
        with anyio.fail_after(5):
            await running.wait()
        assert not tick_task.done()

        await workflow_obj._settle_tick(tick_task, SERVICE, SLUG)

        assert tick_task.cancelled()

    async def test_settle_tick_survives_an_already_cancelled_tick(
        self, tick_logger: logging.Logger
    ) -> None:
        """A task cancelled from elsewhere is `done()`, so `_settle_tick`
        takes the drain branch — and `Task.exception()` on a cancelled task
        raises CancelledError rather than returning it. `_drain_tick`'s
        `cancelled()` guard is the only thing standing between that and the
        same workflow failure the test above covers.
        """
        workflow_obj = DevLoopWorkflow()

        async def never_finishes() -> None:
            await asyncio.sleep(3600)

        tick_task = asyncio.create_task(never_finishes())
        await asyncio.sleep(0)
        tick_task.cancel()
        with anyio.fail_after(5):
            with pytest.raises(asyncio.CancelledError):
                await tick_task
        assert tick_task.cancelled()

        await workflow_obj._settle_tick(tick_task, SERVICE, SLUG)

    async def test_settle_tick_retrieves_a_finished_ticks_exception(
        self, tick_logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A tick that finished by raising must have its exception read.

        `_shepherd_tick` catches its own failures, so this should normally
        find nothing — but if one ever escapes, the task holds it, the
        watch loop drops the reference on the next boundary, and the only
        trace is asyncio's "exception was never retrieved" warning at GC
        time. The error log is the observable proof that `_drain_tick` ran.
        """
        workflow_obj = DevLoopWorkflow()

        async def raises_immediately() -> None:
            raise RuntimeError("tick blew up")

        tick_task = asyncio.create_task(raises_immediately())
        # asyncio.wait, specifically: it reports the task as done without
        # reading its exception, so the retrieval under test below is still
        # genuinely the first one. Awaiting the task itself would raise it
        # here and destroy the thing being measured.
        with anyio.fail_after(5):
            await asyncio.wait({tick_task})

        with caplog.at_level(logging.ERROR, logger=tick_logger.name):
            await workflow_obj._settle_tick(tick_task, SERVICE, SLUG)

        assert "unretrieved" in caplog.text
        assert "RuntimeError" in caplog.text
        assert SLUG in caplog.text
        # Reading it here is what makes the assertion above meaningful: had
        # _drain_tick not called .exception(), this would be the first read.
        assert isinstance(tick_task.exception(), RuntimeError)

    async def test_settle_tick_accepts_a_watch_that_never_ticked(
        self, tick_logger: logging.Logger
    ) -> None:
        """Most watches end without a tick ever starting (the first
        boundary is ~4 h in). `finally` still calls `_settle_tick`, with
        None."""
        await DevLoopWorkflow()._settle_tick(None, SERVICE, SLUG)

    async def test_shepherd_tick_swallows_an_unexpected_error(
        self,
        tick_logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`_shepherd_tick` runs as a task, so anything escaping it is
        stored on that task instead of raised — and the next poll overwrites
        the reference. It caught only ActivityError, which left every other
        failure (a KeyError in the params, a bug in _record) silent. It must
        log and return instead, at the one point that still has the
        service/slug context.
        """

        async def resolve_explodes(agent: str) -> None:
            raise RuntimeError("registry client is broken")

        monkeypatch.setattr(dev_loop, "_resolve", resolve_explodes)

        with caplog.at_level(logging.ERROR, logger=tick_logger.name):
            await DevLoopWorkflow()._shepherd_tick(SERVICE, SLUG)

        assert "unexpected RuntimeError" in caplog.text
        assert SERVICE in caplog.text and SLUG in caplog.text
