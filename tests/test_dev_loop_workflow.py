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
from datetime import timedelta

import anyio
import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.lifecycle.contract import Owner, answer_from
from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.deploy_state import DeployStatus, DeployTarget, ReleaseInfo
from orchestrator.temporal.activities.incidents import Incident, IncidentQueryResult
from orchestrator.temporal.activities.lifecycle import _PATHS, OwnershipRequest, OwnershipResult
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.activities.registry import ResolvedRelease
from orchestrator.temporal.activities.state import ExecutionRecord
from orchestrator.temporal.workflows import dev_loop
from orchestrator.temporal.workflows.dev_loop import (
    INCIDENT_WATCH_WINDOW,
    LIFECYCLE_HEARTBEAT_EVERY_POLLS,
    LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT,
    LIFECYCLE_UNKNOWN_WRITE_LIMIT,
    SHEPHERD_TICK_EVERY_POLLS,
    SHEPHERD_TICKS_MAX,
    DevLoopWorkflow,
    IssueRef,
)
from tests.temporal_harness import Worker  # polls the execution queue too — see #251

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
    unpinned: set[str] | None = None,
    resolve_unavailable: set[str] | None = None,
    incidents: list[Incident] | None = None,
    incident_reads_fail: bool = False,
    incident_query: dict[str, str] | None = None,
    ownership_unavailable: bool = False,
    ownership_unavailable_op: str | None = None,
    ownership_owner: tuple[str, str] | None = None,
    ownership_owner_after: int | None = None,
    ownership_lost_after: int | None = None,
    ownership_self_unhealthy_after: int | None = None,
    ownership_progress_fails: bool = False,
    ownership_progress_unowned: bool = False,
    ownership_unowned_op: str | None = None,
    ownership_unknown_state: str | None = None,
    ownership_unknown_state_op: str = "acquire",
    ownership_body_less: str | None = None,
    ownership_terminal_fails: bool = False,
    ownership_raises: bool = False,
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
        if agent in (resolve_unavailable or set()):
            # A registry read that fails outright, as opposed to one that
            # answers "no release" — the two must not share a code path.
            raise ApplicationError(
                "registry unreachable", type="RegistryUnavailable", non_retryable=True
            )
        if agent in (unpinned or set()):
            return None
        return resolved.get(agent) if released else None

    calls: list[str] = []
    ownership_ops: list[OwnershipRequest] = []
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

    @activity.defn(name="lifecycle_ownership")
    async def fake_lifecycle_ownership(req: OwnershipRequest) -> OwnershipResult:
        ownership_ops.append(req)
        if ownership_raises:
            # The activity itself failing — an old worker with no
            # lifecycle_ownership registered, or retries exhausted. _ownership
            # returns None here, and nothing may dereference it.
            raise ApplicationError("activity not registered", non_retryable=True)
        if ownership_lost_after is not None and len(ownership_ops) > ownership_lost_after:
            return OwnershipResult(
                verdict="owned-by-other",
                owner_type="pr-steward",
                owner_id="steward",
                epoch=77,
                state="active",
                healthy=True,
                accepted=True,
            )
        if (
            ownership_self_unhealthy_after is not None
            and len(ownership_ops) > ownership_self_unhealthy_after
        ):
            # OUR OWN record, read back unhealthy. `verdict_for` answers
            # OWNED_BY_OTHER for an active record whose owner IS the caller
            # whenever `healthy` is False, so the verdict names this very
            # workflow. Reachable after a ~10h gap — a pod restart, or an
            # /acquire outage — and after a 48h quiet PR if `healthy` also
            # excludes `stuck`. No fake produced it, and the arms reading
            # OWNED_BY_OTHER could not tell it from a competitor.
            return OwnershipResult(
                verdict="owned-by-other",
                owner_type=req.owner_type,
                owner_id=req.owner_id,
                epoch=1,
                state="active",
                healthy=False,
                accepted=True,
            )
        if ownership_terminal_fails and req.op == "terminal":
            return OwnershipResult(verdict="unknown", reason="store down")
        if ownership_unavailable_op is not None and req.op == ownership_unavailable_op:
            # Scoped to ONE op, and only AFTER that op has succeeded once, so
            # the claim lands and then the heartbeat stops working. That is the
            # shape the heartbeat block had no cover for: `ownership_unavailable`
            # fails the claiming acquire too, so the loop never reaches the
            # heartbeat at all.
            done = [o for o in ownership_ops[:-1] if o.op == ownership_unavailable_op]
            if done:
                return OwnershipResult(verdict="unknown", reason="store down")
        if ownership_body_less is not None and req.op == ownership_body_less:
            # A body-less 2xx: mctl-api took the write and returned no record.
            #
            # Built by running the REAL classification over the REAL route for
            # this op, not hand-written. The hand-written version returned
            # `wrote-no-record` for whatever op it was handed, and that is a
            # verdict `answer_from` reserves for /release and /terminal —
            # RELINQUISHING_PATH_SUFFIXES is closed on those two precisely
            # because /progress and /acquire leave the entity HELD. So the fake
            # produced a shape the transport cannot, the arms reading it were
            # unreachable, and the tests pinning them were false guards. Going
            # through answer_from means the fake cannot drift from the contract
            # again without the contract's own tests catching it.
            answer = answer_from(
                204, {}, Owner(type=req.owner_type, id=req.owner_id),
                path=_PATHS[req.op], body_empty=True,
            )
            return OwnershipResult(
                verdict=answer.verdict, reason=answer.reason, accepted=answer.accepted
            )
        if ownership_unowned_op is not None and req.op == ownership_unowned_op:
            # A released record answered to any op, not just progress. The
            # heartbeat's UNOWNED arm has to sit ABOVE the accepted arm,
            # because a released record arrives in a 2xx with accepted True —
            # and the only `unowned` fixture was gated on progress, so no test
            # had ever produced an unowned ACQUIRE.
            #
            # Only AFTER the op has succeeded once, like ownership_unavailable_op
            # and for the same reason: answering it to the CLAIMING acquire
            # means no claim ever lands, the heartbeat block is never reached,
            # and every later op carries epoch 0 — which makes an assertion
            # about the claim being dropped trivially true.
            if [o for o in ownership_ops[:-1] if o.op == ownership_unowned_op]:
                return OwnershipResult(
                    verdict="unowned", state="released", reason="no record", accepted=True
                )
        if ownership_unknown_state is not None and req.op == ownership_unknown_state_op:
            # A 2xx carrying a holding state this image does not know. This
            # container lags mctl-api by a release, so `verdict_for` answers
            # UNKNOWN against its two CLOSED sets rather than guessing — and
            # the row may be held by somebody else in that state.
            #
            # An EMPTY state is the body-less 2xx: accepted, verdict UNKNOWN,
            # no record at all. It is the other side of the `result.state`
            # conjunct, and the shape the claim must SURVIVE.
            #
            # After the CLAIM — not after this op has succeeded once, because
            # for a progress op the claiming acquire is a different op and has
            # already landed. Keyed on the acquire either way.
            if [o for o in ownership_ops[:-1] if o.op == "acquire"]:
                return OwnershipResult(
                    verdict="unknown", state=ownership_unknown_state, accepted=True
                )
        if ownership_progress_unowned and req.op == "progress":
            # The reconciler already released the row underneath this loop, so
            # the progress write lands on nothing. Neither owned-by-me nor
            # owned-by-other: the verdict the progress branch used to match
            # against no branch at all.
            return OwnershipResult(verdict="unowned", reason="no record", accepted=True)
        if ownership_progress_fails and req.op == "progress":
            # The claim holds; only the progress write fails. This is the shape
            # the retry guard exists for, and the shape an earlier version of
            # its test never produced.
            return OwnershipResult(verdict="unknown", reason="store down")
        if ownership_unavailable:
            # The store is down. The activity's own contract is to report
            # `unknown` rather than raise, and the loop must survive it.
            return OwnershipResult(verdict="unknown", reason="store down")
        if ownership_owner is not None and (
            ownership_owner_after is None or len(ownership_ops) > ownership_owner_after
        ):
            return OwnershipResult(
                verdict="owned-by-other",
                owner_type=ownership_owner[0],
                owner_id=ownership_owner[1],
                epoch=9,
                state="active",
                healthy=True,
                accepted=True,
            )
        return OwnershipResult(
            verdict="owned-by-me",
            owner_type=req.owner_type,
            owner_id=req.owner_id,
            epoch=1,
            state="active",
            healthy=True,
            accepted=True,
        )

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
        fake_lifecycle_ownership,
    ]
    return activities, calls, investigate_ran, ownership_ops


class TestDevLoopWorkflow:
    async def test_investigate_then_wait_then_implement_after_approval(self, env):
        activities, calls, investigate_ran, _ownership_ops = _fake_activities(released=True)
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
            # Every agent resolves: since A4 an unpinned agent is fatal,
            # so a test about something else must not trip that gate.
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
            )

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

    async def test_failed_investigate_never_implements(self, env):
        """A Failed investigate short-circuits before implement.

        Unrelated to the A4 gate (every agent resolves here) — it guards
        the `if not investigate_result.succeeded: return` branch. Restored
        after review caught it being dropped as collateral of the A4
        rewrite rather than deliberately (claude P2 on #241).
        """
        activities, calls, _investigate_ran, _ownership_ops = _fake_activities(
            released=True, investigate_phase="Failed"
        )
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

    async def test_a_release_without_an_image_is_fatal(self, env):
        """A version row exists but yields no pullable image.

        Before A4 this fell through to the CWFT default and was merely
        recorded as "no pinned version". Now it fails: a half-resolved
        release is exactly as unpinned as a missing one, and the audit
        trail claiming a version that never ran the work was the reason
        this case was singled out in the first place.
        """

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            # version resolved, but no image_ref — e.g. the /versions
            # response had no matching entry to build one from.
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.2.3", image_ref=""
            )

        @activity.defn(name="submit_and_wait")
        async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            raise AssertionError("no CWFT may run without a pinned image")

        @activity.defn(name="record_execution")
        async def fake_record_execution(record: ExecutionRecord) -> None:
            return None

        activities = [
            fake_resolve_agent_release,
            fake_submit_and_wait,
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
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/4"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with pytest.raises(WorkflowFailureError) as caught:
                await handle.result()

        assert "issue-investigator" in str(caught.value.cause)

    async def test_unregistered_agent_fails_the_loop(self, env):
        """A4 (#220 follow-up): the CWFT-default fallback is retired.

        Until the registry was authoritative, an agent nothing had ever
        promoted fell through to whatever image the ClusterWorkflowTemplate
        had baked in. The release pipeline now publishes and promotes every
        manifest, so a missing row is a real misconfiguration — and
        silently running an unknown image is the one outcome that makes
        pinning pointless.
        """

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            return None

        @activity.defn(name="submit_and_wait")
        async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            raise AssertionError("no CWFT may run without a pinned image")

        @activity.defn(name="record_execution")
        async def fake_record_execution(record: ExecutionRecord) -> None:
            return None

        activities = [
            fake_resolve_agent_release,
            fake_submit_and_wait,
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
            with pytest.raises(WorkflowFailureError) as caught:
                await handle.result()

        assert "issue-investigator" in str(caught.value.cause)

    async def _run_with_gate(self, env, *, unpinned: str, issue: int):
        """Drive a loop where exactly ``unpinned`` has no released image.

        Every other agent resolves, so the gate is the only thing that can
        fail the workflow — and the call site under test is the one that
        reaches ``unpinned`` first.
        """

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            if agent == unpinned:
                return None
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
            )

        calls: list[str] = []
        investigate_ran = anyio.Event()

        @activity.defn(name="submit_and_wait")
        async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
            calls.append(input.operation)
            if input.operation == "mctl-agents-investigate":
                investigate_ran.set()
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

        @activity.defn(name="record_execution")
        async def fake_record_execution(record: ExecutionRecord) -> None:
            return None

        activities = [
            fake_resolve_agent_release,
            fake_submit_and_wait,
            fake_record_execution,
            _fake_find_proposal_slug,
        ]
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url=f"https://github.com/mctlhq/mctl-telegram/issues/{issue}"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            with pytest.raises(WorkflowFailureError) as caught:
                await handle.result()
        return calls, caught

    async def test_the_implementer_gate_fires_after_the_approval_is_durable(self, env):
        """The operationally sharpest call site (claude P2 on #241).

        By the time the implementer is resolved, mctl-agents-approve has
        already flipped this proposal to `accepted` as a gitops commit —
        so this gate fails AFTER a durable side effect, leaving an
        accepted proposal that never implements. That is the intended
        trade-off (fail loud beats running an unknown image), and the
        recovery path is real: the start policy is
        ALLOW_DUPLICATE_FAILED_ONLY, so re-adding the intake label after
        publishing the release restarts the issue, and the approve CWFT is
        a no-op on an already-accepted proposal. Asserted here so the
        ordering is not silently reversed later.
        """
        calls, caught = await self._run_with_gate(env, unpinned="implementer", issue=5)

        assert "implementer" in str(caught.value.cause)
        # The flip ran and the implement did not: the exact window this
        # trade-off accepts.
        assert calls == ["mctl-agents-investigate", "mctl-agents-approve"]

    async def test_an_unpinned_shepherd_declines_the_claim_instead_of_failing(self, env):
        """The shepherd gate is fail-open-to-sweeper, not fatal (#241).

        Two failure modes had to be excluded at once. Raising here would
        fail a workflow whose investigate, approve and implement all
        succeeded, throwing away merge detection and deploy observation
        over a tick that is an optimisation (claude P2). Leaving it to
        _shepherd_tick would swallow it — that task logs its exceptions
        rather than raising — so the loop would answer
        shepherd_in_loop=True while the sweeper stood down and nothing
        shepherded the PR for 14 days (codex P1). Declining the claim
        does neither: the watch runs to completion, no unpinned image
        runs, and the cron sweeper owns the proposal.
        """
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, calls, investigate_ran, _ownership_ops = _fake_activities(
            released=True,
            unpinned={"shepherd"},
            pr_states=[open_pr, open_pr, MERGED_PR],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/6"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            result = await handle.result()
            owned = await handle.query(DevLoopWorkflow.shepherd_in_loop)

        # The loop finished its real work rather than dying on the tick.
        assert result.implement is not None and result.pr is not None
        # Nothing unpinned ran...
        assert "mctl-agents-shepherd" not in calls
        # ...and the sweeper is told the proposal is unowned, which is the
        # only thing standing between it and a PR nobody shepherds.
        assert owned is False

    async def test_a_registry_outage_at_the_gate_declines_rather_than_raises(self, env):
        """The gate reads the registry — the read itself must stay fail-open.

        Companion to the test above, and the case that fix missed: there
        the registry answers "no release", here it does not answer at
        all. An unguarded await would let the ActivityError out of
        _shepherd_is_pinned and fail a loop whose implement had already
        succeeded — reintroducing, through the read, exactly the breach
        the decline was written to close (agy P2 on #241).
        """
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        activities, calls, investigate_ran, _ownership_ops = _fake_activities(
            released=True,
            resolve_unavailable={"shepherd"},
            pr_states=[open_pr, open_pr, MERGED_PR],
        )
        async with Worker(
            env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities
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
            result = await handle.result()
            owned = await handle.query(DevLoopWorkflow.shepherd_in_loop)

        assert result.implement is not None and result.pr is not None
        assert "mctl-agents-shepherd" not in calls
        assert owned is False

    async def test_persistent_record_execution_failure_does_not_fail_workflow(self, env):
        """record_execution is an audit-trail write, not the real work — a
        persistent failure there (e.g. mctl-api's executions endpoint down)
        must not fail the whole workflow or wedge wait_condition, since the
        underlying CWFT run it's trying to record already succeeded."""

        @activity.defn(name="resolve_agent_release")
        async def fake_resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
            # Every agent resolves: since A4 an unpinned agent is fatal,
            # so a test about something else must not trip that gate.
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
            )

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
            # Every agent resolves: since A4 an unpinned agent is fatal,
            # so a test about something else must not trip that gate.
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
            )

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
            # Every agent resolves: since A4 an unpinned agent is fatal,
            # so a test about something else must not trip that gate.
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
            )

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
            # Every agent resolves: since A4 an unpinned agent is fatal,
            # so a test about something else must not trip that gate.
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
            )

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
            return ResolvedRelease(
                agent=agent, environment=environment, version="1.0.0", image_ref="ghcr.io/x@sha256:aaa"
            )

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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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

    async def _run_ownership_loop(self, env, *, pr_states, issue: int, **kwargs):
        activities, _calls, investigate_ran, ownership_ops = _fake_activities(
            released=True, pr_states=pr_states, **kwargs
        )
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url=f"https://github.com/mctlhq/mctl-telegram/issues/{issue}"),
                id=f"dev-loop-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(10):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve)
            # Bounded on purpose. A workflow-code AttributeError is not a
            # FailureError: the workflow TASK fails and, because replay is
            # deterministic, keeps failing the same way forever. Without a
            # deadline the test for that would hang rather than fail, which is
            # the least useful way to report the most severe failure mode here.
            with anyio.fail_after(30):
                result = await handle.result()
        return result, ownership_ops

    async def test_ownership_claimed_on_first_resolved_pr_and_terminal_on_merge(self, env):
        """The loop records itself as the owner of the PR it is watching, and
        records a terminal state when the PR reaches one.

        It cannot claim any earlier than the first resolved poll: until then
        this execution knows a service and a slug, and the entity ownership
        attaches to is the pull request.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        result, ops = await self._run_ownership_loop(
            env, pr_states=[open_pr, open_pr, MERGED_PR], issue=901
        )
        assert result.pr is not None and result.pr.state == "MERGED"

        assert ops, "the loop recorded no ownership at all"
        first = ops[0]
        assert first.op == "acquire"
        assert first.kind == "pull-request"
        assert first.entity_id == f"{MERGED_PR.repo}#{MERGED_PR.number}"
        assert first.phase == "review-remediation"
        assert first.owner_type == "devloop-workflow"
        assert first.owner_id.startswith("dev-loop-test-")

        assert ops[-1].op == "terminal", [o.op for o in ops]
        # A merged PR is finished, not handed back: nothing should pick it up.
        assert "release" not in [o.op for o in ops]

    async def test_ownership_released_when_the_watch_ends_without_a_terminal_pr(self, env):
        """The watch gave up without the PR reaching MERGED or CLOSED, so the
        work REMAINS and somebody must be able to take it.

        Release, not terminal: terminal would mean finished, and a reconciler
        would leave it alone forever — which is the zero-owner gap #239
        describes, arrived at from the other direction.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        # Resolves once, then the link stops resolving until the grace polls
        # run out.
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[open_pr] + [PRState(found=False)] * 6,
            issue=902,
        )
        assert ops[0].op == "acquire"
        assert ops[-1].op == "release", [o.op for o in ops]
        assert "terminal" not in [o.op for o in ops]
        # And it carries the head this watch last saw. `_payload` sends
        # `version` unconditionally, so omitting it made the LAST write of the
        # watch the only one carrying an empty one — and the version is what
        # the row records about the entity it is letting go of.
        assert ops[-1].version == "a" * 40, (
            f"the final release carried no version: {ops[-1].version!r}"
        )

    async def test_ownership_store_outage_does_not_fail_the_loop(self, env):
        """Ownership is a coordination signal, not the work.

        A loop that died because it could not reach the ownership store would
        trade a bookkeeping outage for a delivery outage — after investigate,
        approve and implement had all succeeded. It holds no claim instead, and
        the cron sweeper keeps the PR exactly as it did before.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        result, ops = await self._run_ownership_loop(
            env, pr_states=[open_pr, open_pr, MERGED_PR], issue=903,
            ownership_unavailable=True,
        )
        assert result.pr is not None and result.pr.state == "MERGED"
        # It kept trying to acquire and never recorded a claim, so it never
        # reached terminal either — there was nothing to terminalise.
        assert all(o.op == "acquire" for o in ops), [o.op for o in ops]

    async def test_loop_holds_no_claim_when_somebody_else_owns_the_pr(self, env):
        """Another actor owns it. Nothing to escalate here — the reconciler
        resolves conflicts — so this loop simply does not record itself."""
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        result, ops = await self._run_ownership_loop(
            env, pr_states=[open_pr, open_pr, MERGED_PR], issue=904,
            ownership_owner=("pr-steward", "steward"),
        )
        assert result.pr is not None and result.pr.state == "MERGED"
        assert all(o.op == "acquire" for o in ops), [o.op for o in ops]

    async def test_a_refused_claim_is_asked_about_once_not_every_poll(self, env):
        """`_claim_refused` is permanent, and nothing pinned that it exists.

        The sibling test above asserts only `all(op == "acquire")`, which is as
        true of one acquire as of six hundred — so deleting the early return
        left it green through three review rounds. "Somebody else owns this" is
        an ANSWER, not a failure to answer: re-asking for the remaining
        fortnight is ~670 activities to re-learn one fact, and the reconciler
        is what resolves a conflict, not this loop.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[open_pr] * 12 + [MERGED_PR],
            issue=915,
            ownership_owner=("pr-steward", "steward"),
        )
        acquires = [o for o in ops if o.op == "acquire"]
        assert len(acquires) == 1, (
            f"a refused claim was re-asked {len(acquires)} times over 12 polls"
        )

    async def test_an_unanswering_store_is_retried_on_the_heartbeat_not_every_poll(
        self, env
    ):
        """The unclaimed back-off is a DECAY, and both halves of that need cover.

        Every ownership test drives at most four tracked polls against a limit
        of six, so neither half was reachable: reverting the decay to the
        previous permanent give-up stayed green, and deleting the gate stayed
        green too. Twelve polls separates all three behaviours.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[open_pr] * 12 + [MERGED_PR],
            issue=916,
            ownership_unavailable=True,
        )
        acquires = [o for o in ops if o.op == "acquire"]
        # Throttled: fewer than one per poll.
        assert len(acquires) < 12, f"the gate never engaged: {len(acquires)} acquires"
        # But NOT a permanent give-up: asking resumes on heartbeat boundaries
        # after the limit, which is the whole difference from the behaviour
        # this replaced.
        assert len(acquires) > LIFECYCLE_UNKNOWN_WRITE_LIMIT, (
            f"the loop gave up permanently at the limit: {len(acquires)} acquires"
        )

    async def test_a_failing_progress_write_is_throttled_too(self, env):
        """The commit that made the progress branch fall through claimed it
        bounded the retries. It did not.

        `LIFECYCLE_UNKNOWN_WRITE_LIMIT` was read and written only inside the
        unclaimed branch, so a claimed loop whose /progress 500s from poll two
        onward paid one activity per poll for the rest of the watch — the exact
        history cost the constant exists to refuse, arriving by the path that
        falling through created.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40)] + [_pr("b" * 40)] * 12 + [MERGED_PR],
            issue=917,
            ownership_progress_fails=True,
        )
        progress = [o for o in ops if o.op == "progress"]
        assert progress, f"the progress path was never reached: {[o.op for o in ops]}"
        assert len(progress) < 12, f"the progress write was never throttled: {len(progress)}"
        # And the heartbeat still runs while progress is throttled — the
        # liveness fix this back-off sits on top of must survive it.
        first = [o.op for o in ops].index("progress")
        assert "acquire" in [o.op for o in ops][first:], [o.op for o in ops]

    async def test_a_body_less_2xx_on_progress_is_a_landed_write(self, env):
        """A body-less 2xx on /progress is a write that LANDED, and the branch
        had no arm for it, so a 204 landed on `_unknown_progress += 1`.

        This test was a false guard for two rounds. The arm it covers read
        `verdict == WROTE_NO_RECORD`, and `answer_from` never answers that for
        /progress: RELINQUISHING_PATH_SUFFIXES is closed on ("/release",
        "/terminal") because /progress leaves the record active and owned by
        the caller. The arm was unreachable and the defect below was still
        live — the test passed only because the fake hand-built that verdict
        for whatever op it was handed. The fake now runs the real
        classification over the real route, which is what makes the shape here
        the one the transport can actually produce: `accepted` True with
        verdict UNKNOWN. Both halves of the predicate are load-bearing and each
        is killed by its own mutation.

        The write succeeded server-side while `_owned_head_sha` never advanced,
        so the identical evidence was re-sent for the rest of the watch. That
        refreshes `last_progress_at` forever for a head that stopped moving, so
        the stuck bound can NEVER fire — the one property the liveness/progress
        split exists to provide. Throttling to the heartbeat cadence does not
        save it: a refresh every two hours clears a stuck bound as well as one
        every thirty seconds. It also counted a successful write as an
        unanswered one, the opposite of what the gate is for.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40)] + [_pr("b" * 40)] * 12 + [MERGED_PR],
            issue=918,
            ownership_body_less="progress",
        )
        progress = [o for o in ops if o.op == "progress"]
        assert len(progress) == 1, (
            "a landed progress write was re-sent for a head that never moved "
            f"again: {[(o.op, o.version[:8]) for o in ops]}"
        )
        # And the shape the fake produced is the one the transport produces —
        # not the verdict the arm used to read. If RELINQUISHING_PATH_SUFFIXES
        # ever grows /progress this assertion fails and the arm above has to be
        # re-derived rather than silently going dead again.
        landed = answer_from(
            204, {}, Owner(type="devloop-workflow", id="w"),
            path=_PATHS["progress"], body_empty=True,
        )
        assert landed.accepted is True and landed.verdict == "unknown", landed

    async def test_a_body_less_2xx_on_acquire_is_not_a_claim(self, env):
        """A body-less acquire is not a claim.

        Nothing is known about who owns the entity, and it must count toward
        the back-off rather than silently taking no branch at all — which is
        what this pins, and which no fake produced before.

        It still does not pin the choice of predicate — the else arm increments
        the same counter, so the two remain behaviourally identical and no
        assertion here can separate them. What changed is that the predicate is
        now a REACHABLE one: the arm read `verdict == WROTE_NO_RECORD`, which
        `answer_from` reserves for /release and /terminal, so it was dead code
        that this test could not have detected. The reachability is asserted
        directly below, against the real route.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[open_pr] * 12 + [MERGED_PR],
            issue=919,
            ownership_body_less="acquire",
        )
        # No claim was recorded, so nothing but acquires was ever attempted...
        assert all(o.op == "acquire" for o in ops), [o.op for o in ops]
        # ...and it counted toward the back-off rather than being ignored.
        assert len(ops) < 12, f"a body-less acquire was never counted: {len(ops)}"
        assert len(ops) > LIFECYCLE_UNKNOWN_WRITE_LIMIT, len(ops)
        # The arm's predicate is reachable for this route, which is the thing
        # the previous version of this test could not see.
        landed = answer_from(
            204, {}, Owner(type="devloop-workflow", id="w"),
            path=_PATHS["acquire"], body_empty=True,
        )
        assert landed.accepted is True and landed.verdict == "unknown", landed

    async def test_a_failing_heartbeat_is_reported_and_eventually_drops_the_claim(self, env):
        """The heartbeat was the one ownership write whose failure was neither
        logged, nor counted, nor tested.

        The claim path has four arms and a counter and the progress path now has
        four arms and a counter; this block had two, and UNKNOWN, UNOWNED,
        WROTE_NO_RECORD and a `None` result all fell off the end in silence.
        It is where silence costs most: once the head stops moving the heartbeat
        is the ONLY liveness write a claimed loop makes, so an /acquire that
        503s stops refreshing `last_seen_at` with no signal at all, and the
        reconciler force-releases the row at the 10h bound while the workflow is
        alive and polling.

        No existing fixture could reach it — `ownership_unavailable` fails the
        claim so the loop never gets here, `ownership_progress_fails` leaves
        acquire healthy, and `ownership_lost_after` covers only the
        OWNED_BY_OTHER arm. Deleting the whole block left the suite green.

        What the loop does after the limit is give up the CLAIM, not the
        heartbeat: skipping the write whose absence is the problem cannot help,
        and after this many failures what is wrong is the belief. The row will
        be taken at the bound whatever this loop thinks.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        # The claim lands on poll 1 (head "a"), the head moves once so progress
        # succeeds, and from then on only the heartbeat is attempted — and only
        # the heartbeat fails.
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40), _pr("b" * 40)] + [_pr("b" * 40)] * 30 + [MERGED_PR],
            issue=920,
            ownership_unavailable_op="acquire",
        )
        kinds = [o.op for o in ops]
        # The claim was dropped, so the loop went back to the unclaimed path and
        # tried to acquire again — which is only reachable through the give-up.
        first_progress = kinds.index("progress")
        later_acquires = [k for k in kinds[first_progress:] if k == "acquire"]
        assert len(later_acquires) > LIFECYCLE_UNKNOWN_WRITE_LIMIT, (
            f"the heartbeat never gave up a claim it could not refresh: {kinds}"
        )
        # And it never terminalised a PR whose ownership it had given up.
        assert "terminal" not in kinds[first_progress:], kinds

    async def test_an_unhealthy_record_naming_this_loop_is_not_a_lost_claim(self, env):
        """`OWNED_BY_OTHER` can name US, and treating that as a competitor set
        the one permanent flag on the path.

        `verdict_for` returns OWNED_BY_OTHER for an `active` record whose owner
        IS the caller whenever `healthy` is False:

            if asking is not None and own.owner == asking and own.healthy:
                return OWNED_BY_ME
            return OWNED_BY_OTHER

        and both `_lose_claim` callers sit on the CLAIMED path, which is exactly
        where the owner named back is us. The log then read "lost … to
        devloop-workflow/<this workflow_id>", and `_claim_refused = True` ended
        every ownership call for the remaining watch INCLUDING the heartbeat —
        the write whose absence made the row unhealthy. One unhealthy read of
        our own row permanently stopped the write that would have restored it.

        The case does not depend on how mctl-api defines `healthy`: after a
        ~10h gap the row is dead and the first call that reaches the store
        returns our own record unhealthy.

        What must happen instead is the give-up the heartbeat already
        implements — drop the claim, do NOT refuse it — so the loop returns to
        the unclaimed path and keeps trying under its own gate.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40)] + [_pr("b" * 40)] * 40 + [MERGED_PR],
            issue=921,
            ownership_self_unhealthy_after=1,
        )
        kinds = [o.op for o in ops]
        # Nobody else ever claimed this PR, so the loop must not have stood
        # down permanently. With _claim_refused set, everything after the first
        # unhealthy read is a single terminal at the end.
        assert len(kinds) > LIFECYCLE_UNKNOWN_WRITE_LIMIT + 2, (
            f"an unhealthy record naming this loop was read as a lost claim: {kinds}"
        )
        # And it never adopted a competitor's epoch, because there is none: the
        # record is ours, so the writes keep going out under the claim's epoch
        # or under none at all — never under somebody else's.
        assert all(o.epoch in (0, 1) for o in ops), [(o.op, o.epoch) for o in ops]

    async def test_the_heartbeat_gives_up_inside_the_liveness_bound(self, env):
        """The give-up threshold counts HEARTBEATS, not polls.

        `_unknown_heartbeats` advances inside the
        `% LIFECYCLE_HEARTBEAT_EVERY_POLLS` block, so reusing
        LIFECYCLE_UNKNOWN_WRITE_LIMIT (six, sized for once-per-poll counters)
        made it six heartbeats — 24 polls, about twelve hours, past the 10h
        liveness bound the correction exists to arrive before. The reconciler
        force-releases at the bound and the loop went on believing it owned the
        row for another two hours.

        Pinned as a count of heartbeats rather than as a constant comparison,
        so raising the constant back to six fails here rather than silently
        restoring the twelve-hour window.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40), _pr("b" * 40)] + [_pr("b" * 40)] * 40 + [MERGED_PR],
            issue=922,
            ownership_unavailable_op="acquire",
        )
        kinds = [o.op for o in ops]
        first_progress = kinds.index("progress")
        # Heartbeats and re-claims are both `acquire`, so the op name cannot
        # separate them — the EPOCH can. A heartbeat goes out under the claim's
        # epoch; the give-up clears it, so every acquire after it carries 0.
        under_claim = 0
        for op in ops[first_progress + 1:]:
            if op.epoch == 0:
                break
            under_claim += 1
        assert under_claim == LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT, (
            "the heartbeat gave up after the wrong number of failures: "
            f"{[(o.op, o.epoch) for o in ops]}"
        )
        # And the threshold has to be inside the bound it exists to beat:
        # 10h, derived in ADR-010 as 2 x the reconciler cadence.
        held_without_liveness = (
            LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT
            * LIFECYCLE_HEARTBEAT_EVERY_POLLS
            * dev_loop.MERGE_POLL_INTERVAL
        )
        assert held_without_liveness < timedelta(hours=10), held_without_liveness

    async def test_a_heartbeat_the_store_accepted_is_not_a_missed_heartbeat(self, env):
        """The heartbeat was the only one of the three write paths with no
        `accepted` arm, so a liveness write mctl-api TOOK was counted as one
        that did not land.

        Three shapes reach it, all with `accepted` True: a body-less 2xx on
        /ownership/acquire (the exact pair the claim and progress arms were
        rewritten to read — the heartbeat IS an acquire and got neither), our
        own record read back unhealthy, and a 2xx carrying a state this image
        does not recognise. In all three `last_seen_at` was refreshed and the
        warning said the opposite.

        Three of them then dropped a claim the loop still held, after which it
        writes no progress, writes no terminal when the PR merges, and the
        finally releases nothing — the zero-owner state this epic exists to
        remove.

        Pinned on the EPOCH, which is what the give-up clears: the sibling test
        uses the same instrument in the other direction.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        # ownership_body_less is op-scoped, so the claiming acquire answers
        # body-less too and no claim lands. Drive it through the record-naming-
        # us shape instead, which is the one that reaches a CLAIMED loop.
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40)] + [_pr("b" * 40)] * 40 + [MERGED_PR],
            issue=923,
            ownership_self_unhealthy_after=1,
        )
        after_claim = [o for o in ops[1:] if o.op != "terminal"]
        assert after_claim, [o.op for o in ops]
        assert all(o.epoch != 0 for o in after_claim), (
            "a heartbeat the store accepted dropped a claim the loop held: "
            f"{[(o.op, o.epoch) for o in ops]}"
        )

    async def test_a_landed_progress_write_is_also_this_polls_heartbeat(self, env):
        """`_unknown_heartbeats` is read as a count of CONSECUTIVE missed
        heartbeats, and a landed progress write did not reset it.

        Both landed progress arms `return`, which skips the heartbeat block on
        that poll, so the counter neither advanced nor decayed across a write
        that refreshed `last_seen_at` server-side — which the line above it
        says it does, and ADR-010 §3 agrees (`last_seen_at` is refreshed by ANY
        tick).

        The failure is the actively-working loop this PR is built for:
        /ownership/acquire answering 503 while /ownership/progress stays
        healthy — the mirror of the case the give-up was added for. Every
        progress write lands, the heartbeat fails on every fourth poll, and the
        claim is dropped on the premise that liveness stopped, which its own
        landed writes disprove. The give-up then clears `_owned_head_sha`, so
        the progress writes stop too: the one path that was working is silenced
        and only then does the row genuinely go stale.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        # The head must REPEAT on the heartbeat polls and move on the others.
        # A head that moves on every poll makes the progress branch return
        # before the heartbeat block is ever reached, so the counter never
        # advances and the test passes with or without the fix — which is
        # exactly how the first version of this test was a false guard.
        heads = []
        for i in range(1, 25):
            if i % LIFECYCLE_HEARTBEAT_EVERY_POLLS == 0 and heads:
                heads.append(heads[-1])
            else:
                heads.append(_pr(chr(ord("a") + i) * 40))
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[*heads, MERGED_PR],
            issue=924,
            ownership_unavailable_op="acquire",
        )
        progress = [o for o in ops if o.op == "progress"]
        assert len(progress) > LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT + 2, (
            f"the progress path stopped: {[(o.op, o.epoch) for o in ops]}"
        )
        # Every op AFTER the claim, not just the progress writes: the give-up
        # clears the epoch, so an op carrying 0 is the claim having been
        # dropped. Asserting only on the progress ops cannot see it, because
        # the twelve that landed BEFORE the give-up already satisfy the count
        # above — which is how the first version of this assertion passed under
        # its own mutation.
        assert all(o.epoch != 0 for o in ops[1:]), (
            "a claim was dropped although its own progress writes were landing: "
            f"{[(o.op, o.epoch) for o in ops]}"
        )

    async def test_the_unclaimed_path_also_refuses_to_lose_to_itself(self, env):
        """The second call site of the guard, and the one the give-up lands on.

        After the heartbeat give-up the loop returns to the UNCLAIMED path —
        deliberately, since the give-up does not set `_claim_refused` — while
        the store is still returning our own active row with `healthy` False,
        which is why it gave up. Without the guard there, that answer sets
        `_claim_refused = True` and the loop stops making ownership calls of
        any kind for the remaining thirteen days, having refused a claim nobody
        else ever held.

        Every other test passes `ownership_self_unhealthy_after=1`, so `ops[0]`
        — the claiming acquire — is always a normal answer and this site is
        never exercised. `0` makes the very first call self-naming.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[open_pr] * 20 + [MERGED_PR],
            issue=925,
            ownership_self_unhealthy_after=0,
        )
        # _claim_refused would stop every ownership call after the first.
        assert len([o for o in ops if o.op == "acquire"]) > 1, (
            "a record naming this loop refused its own claim permanently: "
            f"{[o.op for o in ops]}"
        )

    async def test_a_competitor_without_an_id_does_not_read_as_our_own_record(self, env):
        """`_lost_to_someone_else` cannot carry the owner question alone.

        `Ownership.from_payload` requires `phase`, `owner.type`, `state` and
        `healthy` — but NOT `owner.id`. So a 2xx carrying
        `owner: {"type": "pr-steward"}` with `state: "active"` parses, answers
        OWNED_BY_OTHER, and is declined as a loss by the `bool(result.owner_id)`
        guard, which exists for the `lost … to /` case and is right to be
        there. Without an owner-TYPE conjunct such a record landed on the arm
        whose comment says "our OWN record read back unhealthy", which keeps
        the claim and advances the head — against a row a real competitor
        holds.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        heads = [_pr(f"{i:040d}") for i in range(1, 25)]
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[*heads, MERGED_PR],
            issue=935,
            ownership_owner=("pr-steward", ""),
            ownership_owner_after=1,
        )
        progress = [o for o in ops if o.op == "progress"]
        # The head must NOT have been advanced on every poll as though the
        # record were ours: an unowned-by-us answer falls through, so the
        # back-off gate closes and the writes are throttled.
        ceiling = (
            LIFECYCLE_UNKNOWN_WRITE_LIMIT
            + len(heads) // LIFECYCLE_HEARTBEAT_EVERY_POLLS
            + 2
        )
        assert len(progress) <= ceiling, (
            "a competitor with no id was treated as our own record: "
            f"{len(progress)} progress writes of {len(heads)} polls"
        )

    async def test_a_refusal_naming_nobody_does_not_refuse_the_claim(self, env):
        """`_lose_claim` compared ids, and a 409 whose body is not a record
        answers OWNED_BY_OTHER with `owner_id=""`.

        `"" != workflow_id`, so that reached `_lose_claim`, logged `lost … to
        /`, and set the one PERMANENT flag on the path naming nobody — from a
        refused write that may well have been refused because of this loop's
        own stale belief. `answer_from` produces exactly this shape from its
        `record_of(payload) is None` branch on a 409.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[open_pr] * 20 + [MERGED_PR],
            issue=926,
            ownership_owner=("", ""),
        )
        assert len([o for o in ops if o.op == "acquire"]) > 1, (
            "a refusal naming nobody refused the claim permanently: "
            f"{[o.op for o in ops]}"
        )

    async def test_a_state_this_image_cannot_read_still_drops_the_claim(self, env):
        """The one `accepted` shape that must COUNT rather than reset.

        mctl-api adds a holding state and hands the row to another actor in it.
        `verdict_for` classifies against two CLOSED sets and answers UNKNOWN
        rather than guessing, because this container lags mctl-api by a
        release; `accepted` is True because a record came back. An arm that
        zeroed the counter on every such heartbeat made the give-up
        UNFIREABLE — the loop would hold its claim indefinitely, and invisibly,
        against a row somebody else owns.

        Liveness is not the question here: the write landed. What is wrong
        after this many of them is the BELIEF, and uncertainty resolves toward
        dropping the claim — the same direction `verdict_for` itself takes.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40)] + [_pr("b" * 40)] * 40 + [MERGED_PR],
            issue=927,
            ownership_unknown_state="quiescing",
        )
        # The claim was dropped, which is only reachable through the give-up:
        # every op after it carries epoch 0.
        assert any(o.epoch == 0 for o in ops[1:]), (
            "a heartbeat in an unreadable state never dropped the claim: "
            f"{[(o.op, o.epoch, o.version[:2]) for o in ops]}"
        )

    async def test_our_own_unhealthy_record_is_still_a_landed_progress_write(self, env):
        """The progress branch matched none of its guards on the shape the
        heartbeat has enumerated since the accepted arm was added.

        `verdict_for` answers OWNED_BY_OTHER for an active row whose owner IS
        the caller whenever `healthy` is False; `_lost_to_someone_else`
        declines to call that a loss; and `accepted` is True because a 2xx
        carried a record. The heartbeat logs it, resets and keeps the claim.
        The progress branch fell to `_unknown_progress += 1`, so it counted a
        write mctl-api TOOK, never advanced the head — re-sending the identical
        evidence for the rest of the watch, every send landing, so
        last_progress_at was refreshed forever for a head that stopped moving
        and the stuck bound could never fire — and said nothing.

        The fixture already produced this: `ownership_self_unhealthy_after` is
        not op-scoped, so the sibling tests ran the defect on every poll while
        asserting only on `epoch`, which the heartbeat keeps alive. This
        asserts on the throttle instead, which is what the counter drives.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        # A head that moves on every poll, so every poll SHOULD produce a
        # progress write. Under the defect `_unknown_progress` reaches
        # LIFECYCLE_UNKNOWN_WRITE_LIMIT and `_backed_off` throttles the path to
        # the heartbeat cadence, so most of them never happen.
        heads = [_pr(chr(ord("a") + i) * 40) for i in range(1, 25)]
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[*heads, MERGED_PR],
            issue=933,
            ownership_self_unhealthy_after=1,
        )
        progress = [o for o in ops if o.op == "progress"]
        # Nearly every poll, not merely more than the limit: `_backed_off`
        # still lets one through on each heartbeat boundary, so the throttled
        # count is ~LIMIT + polls/HEARTBEAT_EVERY — which a threshold just
        # above the limit does not separate from the healthy one. That is how
        # the first version of this assertion survived its own mutation.
        assert len(progress) >= len(heads) - 2, (
            "landed progress writes were counted as failures and throttled: "
            f"{len(progress)} of {len(heads)} polls — {[o.op for o in ops]}"
        )
        # And the head really advanced each time, rather than the same
        # evidence being re-sent.
        versions = [o.version for o in progress]
        assert len(set(versions)) == len(versions), (
            f"the same head was re-sent: {[v[:2] for v in versions]}"
        )

    async def test_an_unreadable_state_on_progress_backs_off_without_dropping_the_claim(self, env):
        """A progress write that comes back in a state this image cannot read
        must not advance the head, must back off, and must NOT decide on its
        own that the claim is gone.

        `verdict_for` classifies against two CLOSED sets and answers UNKNOWN
        rather than guessing, because this container lags mctl-api by a
        release. The head therefore must not advance — the row may be held by
        somebody else in that state, so the evidence has to be re-sent rather
        than treated as recorded — and `_unknown_progress` has to count it, or
        the re-send happens once per poll forever.

        What it must NOT do is drive the give-up. An earlier version
        incremented `_unknown_heartbeats` here and returned, which inverted the
        constant: that counter is documented as three HEARTBEATS, about six
        hours and inside the 10h liveness bound, and this arm runs once per
        POLL — so the limit was reached in three polls, about ninety minutes,
        and the claim was dropped four times sooner than the bound it precedes.
        (Found by agy, not by this suite.)

        This test therefore pins the fall-through, not the arm: the arm is now
        a log line, and deleting it leaves this green. What it cannot be
        deleted from is the body-less arm below, which must keep requiring an
        empty state or an unreadable record advances the head after all.

        Falling through keeps the arithmetic honest, and it also gives the
        right answer here: /acquire still says the row is ours, so the claim
        stands. The case where the row really has moved is covered by
        test_a_state_this_image_cannot_read_still_drops_the_claim, where the
        heartbeat's own acquire answers the same way and counts at the
        heartbeat cadence.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        # A head that moves on every poll, so every poll would issue a progress
        # write if nothing throttled it.
        heads = [_pr(f"{i:040d}") for i in range(1, 41)]
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[*heads, MERGED_PR],
            issue=931,
            ownership_unknown_state="quiescing",
            ownership_unknown_state_op="progress",
        )
        progress = [o for o in ops if o.op == "progress"]
        assert progress, [o.op for o in ops]
        # Throttled by the SHARED fall-through at the end of the block, not by
        # anything this arm does: `_backed_off` closes the gate once
        # `_unknown_progress` passes the limit.
        ceiling = (
            LIFECYCLE_UNKNOWN_WRITE_LIMIT
            + len(heads) // LIFECYCLE_HEARTBEAT_EVERY_POLLS
            + 2
        )
        assert len(progress) <= ceiling, (
            "an unreadable state on progress was re-sent every poll: "
            f"{len(progress)} of {len(heads)} polls, ceiling {ceiling}"
        )
        # The head never advanced, so the evidence really is being re-sent
        # rather than treated as recorded: every progress write carries the
        # head of its own poll, and none of them is repeated.
        assert len({o.version for o in progress}) == len(progress), (
            f"the same head was re-sent: {[o.version[-3:] for o in progress]}"
        )
        # And the claim stands, because /acquire still says the row is ours.
        assert all(o.epoch != 0 for o in ops[1:]), (
            "the progress arm dropped a claim /acquire was still confirming: "
            f"{[(o.op, o.epoch) for o in ops]}"
        )

    async def test_a_body_less_progress_write_still_keeps_the_claim(self, env):
        """The other side of the same conjunct, on the progress path.

        An EMPTY state is the body-less 2xx: accepted, no record, nothing
        suggesting the row moved. The head must advance and the claim must
        stand — otherwise a store answering 204 to every progress write, which
        is legal, drops a healthy claim.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        heads = []
        for i in range(1, 30):
            if i % LIFECYCLE_HEARTBEAT_EVERY_POLLS == 0 and heads:
                heads.append(heads[-1])
            else:
                heads.append(_pr(chr(ord("a") + i) * 40))
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[*heads, MERGED_PR],
            issue=932,
            ownership_unknown_state="",
            ownership_unknown_state_op="progress",
        )
        assert all(o.epoch != 0 for o in ops[1:]), (
            "a body-less progress write dropped a claim nothing said had moved: "
            f"{[(o.op, o.epoch) for o in ops]}"
        )

    async def test_a_body_less_heartbeat_keeps_the_claim(self, env):
        """The other side of the `result.state` conjunct.

        A body-less 2xx on the heartbeat is accepted with verdict UNKNOWN and
        NO record, so nothing suggests the row moved: `last_seen_at` was
        refreshed and the claim must stand. Only a record whose STATE this
        image cannot classify is a reason to stop believing, because only that
        one says somebody may hold the row in a state we cannot read.

        Without the conjunct both collapse into "count it", and a store
        answering 204 to every heartbeat — legal, and what a body-less 2xx IS —
        drops a healthy claim every six hours.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40)] + [_pr("b" * 40)] * 40 + [MERGED_PR],
            issue=930,
            ownership_unknown_state="",
        )
        assert all(o.epoch != 0 for o in ops[1:]), (
            "a body-less heartbeat dropped a claim nothing said had moved: "
            f"{[(o.op, o.epoch) for o in ops]}"
        )

    async def test_an_unowned_heartbeat_drops_the_claim_rather_than_keeping_it(self, env):
        """The UNOWNED arm's POSITION is load-bearing and nothing tested it.

        It must sit above the `accepted` arm, because a released record arrives
        in a 2xx with `accepted=True`. Below it, an UNOWNED heartbeat resets the
        counter and returns, so the loop keeps a claim on a row the store says
        is free — forever, with no re-acquire, and a `finally` writing against a
        claim it does not hold.

        The only `unowned` fixture was gated on `req.op == "progress"`, so no
        test had ever produced an unowned ACQUIRE, which is what a heartbeat is.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40)] + [_pr("b" * 40)] * 20 + [MERGED_PR],
            issue=928,
            ownership_unowned_op="acquire",
        )
        # The claiming acquire is normal; from the first heartbeat on the row
        # reads as released, and the claim must not survive it.
        assert any(o.epoch == 0 for o in ops[1:]), (
            "an unowned heartbeat kept a claim on a row the store says is free: "
            f"{[(o.op, o.epoch) for o in ops]}"
        )

    async def test_a_body_less_progress_write_is_also_this_polls_heartbeat(self, env):
        """The second landed-progress arm, which had the same fix and no test.

        `ownership_unavailable_op` is checked before `ownership_body_less` in
        the fake, so the two compose: every acquire after the claim fails while
        every progress write lands with a body-less 2xx. Without the reset the
        heartbeat counter climbs across writes that refreshed `last_seen_at`
        and the claim is dropped — the behaviour this commit exists to remove,
        reached through the arm that had no cover.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        heads = []
        for i in range(1, 25):
            if i % LIFECYCLE_HEARTBEAT_EVERY_POLLS == 0 and heads:
                heads.append(heads[-1])
            else:
                heads.append(_pr(chr(ord("a") + i) * 40))
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[*heads, MERGED_PR],
            issue=929,
            ownership_unavailable_op="acquire",
            ownership_body_less="progress",
        )
        progress = [o for o in ops if o.op == "progress"]
        assert len(progress) > LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT + 2, (
            f"the progress path stopped: {[(o.op, o.epoch) for o in ops]}"
        )
        assert all(o.epoch != 0 for o in ops[1:]), (
            "a claim was dropped although its own body-less progress writes "
            f"were landing: {[(o.op, o.epoch) for o in ops]}"
        )

    async def test_progress_is_recorded_only_when_the_head_moves(self, env):
        """A poll that observed nothing new must not write progress.

        The whole reason liveness and progress are separate fields is that an
        owner must not be able to prove usefulness by continuing to breathe.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40), _pr("a" * 40), _pr("b" * 40), MERGED_PR],
            issue=905,
        )
        progress = [o for o in ops if o.op == "progress"]
        assert len(progress) == 1, [o.op for o in ops]
        assert progress[0].version == "b" * 40
        assert "bbbbbbbb" in progress[0].evidence

    async def test_a_failed_progress_write_is_retried_not_dropped(self, env):
        """`is not None` is not "the write succeeded".

        The activity never raises — that is its contract — so a 503 comes back
        as a real result carrying an `unknown` verdict. Advancing the recorded
        head on that drops the progress signal permanently: the next poll sees
        head == recorded and never retries.

        An earlier version of this test never reached the progress path at all
        (its own comment said so), so relaxing the guard to
        `result.verdict != OWNED_BY_OTHER` kept it green — the fix it is named
        for had no regression guard. The claim now succeeds and only `progress`
        fails, which is the shape that actually exercises it.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40), _pr("b" * 40), _pr("b" * 40), _pr("b" * 40), MERGED_PR],
            issue=910,
            ownership_progress_fails=True,
        )
        progress = [o for o in ops if o.op == "progress"]
        assert progress, f"the progress path was never reached: {[o.op for o in ops]}"
        # The head moved once and every later poll sees the same head. If the
        # failed write had been treated as success, _owned_head_sha would have
        # advanced and there would be exactly one attempt; the retry is what
        # makes more than one.
        assert len(progress) > 1, (
            f"a failed progress write was not retried: {[(o.op, o.version[:8]) for o in ops]}"
        )
        assert all(o.version == "b" * 40 for o in progress)

    async def test_a_failing_progress_write_does_not_silence_the_heartbeat(self, env):
        """The `return` at the end of the progress branch used to be
        unconditional, and that made the heartbeat unreachable.

        A failed progress write correctly does NOT advance the recorded head,
        so `head != _owned_head_sha` stays true on every later poll and control
        returns from the progress branch every time. The heartbeat `acquire`
        below it therefore never ran again for the rest of the watch: a
        `/progress` answering 412 — the record moved, which is exactly what the
        fencing epoch is for — while `acquire` would still have succeeded
        stopped refreshing `last_seen_at` entirely. The 10h liveness bound then
        expires and the reconciler declares this owner dead and force-releases
        the row, while the workflow is alive, polling and shepherding the PR.

        The retry the sibling test pins is still correct and still happens; the
        claim is that it must not be the ONLY thing that happens.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40), _pr("b" * 40), _pr("b" * 40), _pr("b" * 40), MERGED_PR],
            issue=913,
            ownership_progress_fails=True,
        )
        kinds = [o.op for o in ops]
        first_progress = kinds.index("progress")
        later = kinds[first_progress:]
        assert "acquire" in later, (
            f"the heartbeat never ran again after a failing progress write: {kinds}"
        )
        # And the retry the sibling test pins is unaffected.
        assert kinds.count("progress") > 1, kinds

    async def test_progress_against_a_released_row_re_acquires(self, env):
        """UNOWNED is the mirror of the case above.

        A progress write against a row the reconciler already released matched
        neither branch, so the claim was never dropped — and, because of the
        same unconditional return, never re-acquired either. The loop went on
        believing it owned an entity the store says is free, for the rest of
        the watch. Falling through to the heartbeat re-establishes it.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40), _pr("b" * 40), _pr("b" * 40), _pr("b" * 40), MERGED_PR],
            issue=914,
            ownership_progress_unowned=True,
        )
        kinds = [o.op for o in ops]
        first_progress = kinds.index("progress")
        assert "acquire" in kinds[first_progress:], (
            f"a released row was never re-acquired: {kinds}"
        )

    async def test_a_released_row_re_acquires_even_while_the_head_keeps_moving(self, env):
        """The dangerous half of the case above, which had no test — and whose
        absence let an arm intercept UNOWNED silently.

        `test_progress_against_a_released_row_re_acquires` lets the head stop
        moving after poll 2, so the `acquire` its assertion finds is the
        routine heartbeat rather than a recovery: any behaviour that keeps
        heart-beating satisfies it. With the head moving on EVERY poll the
        progress branch is entered every poll, so an arm that answers UNOWNED
        and returns starves the heartbeat entirely.

        That is the §5 recovery this epic is built on: the pod stalls past the
        liveness bound, the reconciler force-releases the row, the pod resumes
        pushing fixes. The loop must REACH the heartbeat at all: its acquire
        is what re-establishes the claim, and under the defect no acquire
        happens after the first progress for the rest of the watch.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[*[_pr(chr(ord("a") + i) * 40) for i in range(1, 13)], MERGED_PR],
            issue=934,
            ownership_unowned_op="progress",
        )
        kinds = [o.op for o in ops]
        first_progress = kinds.index("progress")
        after = ops[first_progress:]
        assert any(o.op == "acquire" for o in after), (
            "the heartbeat was starved by the progress branch, so a released "
            f"row was never re-acquired: {[(o.op, o.epoch) for o in ops]}"
        )

    async def test_an_activity_failure_does_not_wedge_the_loop(self, env):
        """`_ownership` returns None when the activity fails outright.

        Dereferencing it would raise AttributeError inside workflow code, which
        is not a FailureError: the workflow task fails, and because replay is
        deterministic it fails the same way forever — wedged until somebody
        terminates it. The likeliest trigger is this change's own rollout, when
        a control worker on the previous image has no `lifecycle_ownership`
        registered.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        result, ops = await self._run_ownership_loop(
            env, pr_states=[open_pr, open_pr, MERGED_PR], issue=912,
            ownership_raises=True,
        )
        # The loop completed normally and still reported the merge.
        assert result.pr is not None and result.pr.state == "MERGED"
        # It tried, held no claim, and never terminalised anything it did not own.
        assert ops and all(o.op == "acquire" for o in ops), [o.op for o in ops]

    async def test_a_failed_terminal_leaves_the_claim_for_the_reconciler(self, env):
        """Clearing the claim on a terminal that did not land would leave the
        row active with nobody believing they own it — the zero-owner state,
        produced by the cleanup written to prevent it.

        The loop keeps the claim instead, and the reconciler's liveness bound
        is what resolves it once this execution stops being seen.
        """
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env, pr_states=[open_pr, MERGED_PR], issue=911,
            ownership_terminal_fails=True,
        )
        terminals = [o for o in ops if o.op == "terminal"]
        assert terminals, [o.op for o in ops]
        # The claim was NOT dropped, so the finally tries again on the way out
        # rather than silently forgetting the entity.
        assert len(terminals) > 1, (
            f"a failed terminal dropped the claim: {[o.op for o in ops]}"
        )
        # And it retries TERMINAL, not release. The PR merged; marking a
        # finished entity as "somebody must take this" is the one state this
        # contract reserves for work that remains.
        assert not any(o.op == "release" for o in ops), (
            f"a merged PR was released: {[o.op for o in ops]}"
        )

    async def test_losing_the_claim_is_noticed_and_the_epoch_is_not_adopted(self, env):
        """A worker pod that stalls past its liveness bound loses the PR.

        The loop must notice, and must NOT adopt the winner's fencing
        generation: carrying it would make every later call assert an epoch it
        never held, which the server rejects and which reads in the events as
        this workflow acting on somebody else's claim.
        """
        def _pr(sha: str) -> PRState:
            return PRState(
                found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
                number=MERGED_PR.number, state="OPEN", head_sha=sha,
            )

        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[_pr("a" * 40), _pr("b" * 40), _pr("c" * 40), MERGED_PR],
            issue=907,
            ownership_lost_after=1,
        )
        # The first call claims; everything after is refused. No call may ever
        # carry the winner's epoch of 77.
        assert ops[0].op == "acquire"
        assert all(o.epoch != 77 for o in ops), [(o.op, o.epoch) for o in ops]
        # And once refused, the loop stops re-asking every poll.
        assert len(ops) <= 3, [o.op for o in ops]

    async def test_the_heartbeat_notices_a_lost_claim_too(self, env):
        """The heartbeat path, not the progress path.

        With an unchanged head the loop reaches the every-4th-poll re-acquire
        instead of `progress`, and an earlier version discarded that verdict and
        kept only the epoch — so this loop could never notice it had lost the PR
        through the one call it makes most often, and adopted the winner's
        fencing generation while failing to notice.
        """
        same = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env,
            pr_states=[same, same, same, same, same, MERGED_PR],
            issue=909,
            ownership_lost_after=1,
        )
        heartbeats = [o for o in ops if o.op == "acquire"][1:]
        assert heartbeats, f"the heartbeat path was never reached: {[o.op for o in ops]}"
        assert all(o.epoch != 77 for o in ops), [(o.op, o.epoch) for o in ops]

    async def test_ownership_rows_record_why_this_actor_owns_them(self, env):
        """ADR-010 §12: the row must answer "why does this actor own it"
        without reconstructing a CWFT env var in another repository."""
        open_pr = PRState(
            found=True, pr_url=MERGED_PR.pr_url, repo=MERGED_PR.repo,
            number=MERGED_PR.number, state="OPEN", head_sha="a" * 40,
        )
        _result, ops = await self._run_ownership_loop(
            env, pr_states=[open_pr, MERGED_PR], issue=908
        )
        assert ops[0].proposal_ref, "no proposal correlation recorded"
        assert ops[0].policy_ref, "no policy reason recorded"
        assert "mctl-telegram" in ops[0].proposal_ref

    async def test_merge_detection_gives_up_when_pr_link_never_appears(self, env):
        """A proposal whose .status.yaml never gains a pr: link stops the
        watch after the grace polls (result.pr is None), instead of polling
        for the full 14-day deadline."""
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(released=True)
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(released=True, release=None)
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(released=True, deploy_target=None)
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(released=True)
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, _calls, investigate_ran, _ownership_ops = _fake_activities(released=True, release=None)
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
        activities, calls, investigate_ran, _ownership_ops = _fake_activities(
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
        activities, calls, investigate_ran, _ownership_ops = _fake_activities(
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
        flips review-stuck after MAX_REVIEW_ATTEMPTS address-review
        attempts — so a wedged PR must not burn one every ~4 h for the
        full 14-day watch. Drive the watch past 13 tick boundaries and
        assert the 13th produces nothing.
        """
        open_pr = PRState(
            found=True,
            pr_url=MERGED_PR.pr_url,
            repo=MERGED_PR.repo,
            number=MERGED_PR.number,
            state="OPEN",
        )
        polls = SHEPHERD_TICK_EVERY_POLLS * (SHEPHERD_TICKS_MAX + 1)
        activities, calls, investigate_ran, _ownership_ops = _fake_activities(
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
