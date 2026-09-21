"""Regression for mctl-agents#404, task T1.

`MergeWatchResume` is the only thing that crosses a merge-watch
continue-as-new boundary: `DevLoopWorkflow.run` reads it back from
`IssueRef.resume` at the top of a continued run and rehydrates every
query-visible and lifecycle-claim field from it before the first await. If
any field failed to round-trip through Temporal's actual JSON payload
converter -- as opposed to an in-process dataclass copy, which every other
test in this module implicitly exercises -- a continued run would silently
diverge from the run it replaces.

Mirrors `tests/test_stranded_skipped_roundtrip.py`: a tiny probe workflow
that receives a fully populated `MergeWatchResume` (including a non-None
`last_pr` and three `WorkflowResult`s) as its own run argument, so the
SDK's real encode/decode path is exercised end to end, not faked.
"""
from __future__ import annotations

import uuid

import pytest
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import WorkflowResult
    from orchestrator.temporal.activities.pr_state import PRState
    from orchestrator.temporal.workflows.dev_loop import ImplementExecutionState, MergeWatchResume

from tests.temporal_harness import Worker

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-merge-watch-resume-roundtrip"

_RESUME = MergeWatchResume(
    service="mctl-telegram",
    slug="issue-404-fake-title",
    deadline="2026-10-01T00:00:00Z",
    last_pr=PRState(
        found=True,
        pr_url="https://github.com/mctlhq/mctl-telegram/pull/1",
        repo="mctlhq/mctl-telegram",
        number=1,
        state="OPEN",
        head_sha="a" * 40,
    ),
    polls_without_pr=2,
    poll_index=17,
    shepherd_ticks=3,
    fast_cadence=True,
    shepherd_in_loop=True,
    concurrent_ticks=True,
    track_ownership=True,
    owned_entity_id="mctlhq/mctl-telegram#1",
    owner_epoch=4,
    owned_head_sha="a" * 40,
    poll_index_for_heartbeat=9,
    claim_refused=True,
    claim_refused_until_poll=49,
    refused_by_type="pr-steward",
    refused_by_id="steward",
    refusals_observed=1,
    unknown_acquires=2,
    unknown_progress=1,
    unknown_heartbeats=0,
    proposal_ref="mctl-telegram/issue-404-fake-title",
    policy_ref="devloop:mctl-telegram",
    last_lifecycle_op="",
    last_lifecycle_op_landed=False,
    claim_abandoned=False,
    investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
    implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
    approve=WorkflowResult(workflow_name="mctl-agents-approve-fake", phase="Succeeded"),
    implement_state=ImplementExecutionState(
        stage="implementer",
        queued_at="2026-09-20T00:00:00Z",
        prestart_requeues=1,
        outcome="success",
    ),
    hops=2,
)


@workflow.defn
class _MergeWatchResumeRoundTripProbe:
    """Not part of the production graph: exists only to observe what a real
    Temporal payload conversion does to a fully populated `MergeWatchResume`."""

    @workflow.run
    async def run(self, resume: MergeWatchResume) -> MergeWatchResume:
        return resume


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_a_fully_populated_merge_watch_resume_survives_the_payload_converter(env):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[_MergeWatchResumeRoundTripProbe],
        activities=[],
    ):
        outcome = await env.client.execute_workflow(
            _MergeWatchResumeRoundTripProbe.run,
            _RESUME,
            id=f"merge-watch-resume-roundtrip-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )

    assert outcome == _RESUME
    # Equality alone would also pass for two dicts that happen to compare
    # equal; pin the actual reconstructed types too, since those are what a
    # silently degraded conversion (dataclass -> plain dict) would lose.
    assert isinstance(outcome.last_pr, PRState)
    assert isinstance(outcome.investigate, WorkflowResult)
    assert isinstance(outcome.implement, WorkflowResult)
    assert isinstance(outcome.approve, WorkflowResult)
    assert isinstance(outcome.implement_state, ImplementExecutionState)
