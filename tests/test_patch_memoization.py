"""What `workflow.patched()` does to an execution that is already running.

This exists because a plausible-sounding claim about it was wrong, was
written into ADR-008 and into a PR body, and was caught in review (agy P2
on #282). The claim was that an in-flight workflow "replays its earlier
decisions on the old path and routes its NEW ones to the new path", so a
14-day merge watch would migrate to the execution queue partway through.

It does not. `_workflow_instance.workflow_patch` memoizes per patch id:

    use_patch = self._patches_memoized.get(id)
    if use_patch is not None:
        return use_patch

An execution whose history has no marker replays that call, gets False and
memoizes False — so every later call in that execution returns False too,
including calls made long after replay has finished. The unpatched branch
is sticky for the life of the execution.

Reading the source is not the same as knowing, and reasoning from source is
how the wrong claim got written in the first place. So this drives the real
thing: run the workflow without the routing, leave the execution open,
restart the worker with the routing enabled, and look at where the next
activity was actually scheduled.

## Why this matters operationally rather than as trivia

For #251 it decides what the queue metrics mean during the soak. Dev loops
already running when the flip deployed keep sending their Argo polls to the
CONTROL queue until they finish — up to `MERGE_WATCH_DEADLINE`. Migration
is by attrition: new executions route to exec immediately, old ones never
do. A control queue still busy a week after the flip is that, not a broken
flip.

## Two harness details that are load-bearing

- `start_local`, not `start_time_skipping`. Restarting a worker leaves the
  pending workflow task on the dead worker's sticky queue, and the server
  only reassigns it once the sticky timeout elapses. Under time skipping the
  server clock does not advance while the test polls, so the task waits
  forever and the run reads as "the workflow never progressed at all" —
  which is how this probe first appeared to show something much worse than
  memoization.
- Phase one waits for the activity to COMPLETE, not merely to be scheduled.
  Shutting the worker down in between leaves the activity pending, so the
  next phase retries it instead of moving on.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.argo import WorkflowResult
from orchestrator.temporal.constants import EXECUTION_TASK_QUEUE
from orchestrator.temporal.constants import TASK_QUEUE as DEV_LOOP_QUEUE
from orchestrator.temporal.workflows.dev_loop import (
    DEFERRED_RESUME_PATCH,
    DevLoopWorkflow,
    IssueRef,
    MergeWatchResume,
)
from tests.temporal_harness import Worker
from tests.test_dev_loop_workflow import _fake_activities

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-patch-memoization"

# Stands in for a deploy. Read inside the workflow, which is only sound
# because the definition is unsandboxed and the flag never changes within a
# single workflow task. It models "the worker restarted with new code",
# which is the one thing this test is about and cannot be expressed by
# editing a file mid-run.
DEPLOYED = {"flip": False}


@activity.defn(name="submit_and_wait")
async def _fake_submit(step: str) -> str:
    """Named submit_and_wait so the harness registers it on the exec queue."""
    return step


@workflow.defn(name="PatchMemoProbe", sandboxed=False)
class PatchMemoProbe:
    """One activity per `step` signal, routed the way _run_cwft routes."""

    def __init__(self) -> None:
        self._steps = 0
        self._done = False

    @workflow.run
    async def run(self) -> int:
        while True:
            await workflow.wait_condition(lambda: self._steps > 0)
            self._steps -= 1
            if DEPLOYED["flip"] and workflow.patched("exec-queue"):
                await workflow.execute_activity(
                    "submit_and_wait",
                    "s",
                    task_queue=EXECUTION_TASK_QUEUE,
                    start_to_close_timeout=timedelta(seconds=30),
                )
            else:
                await workflow.execute_activity(
                    "submit_and_wait",
                    "s",
                    start_to_close_timeout=timedelta(seconds=30),
                )
            if self._done:
                return 0

    @workflow.signal
    async def step(self) -> None:
        self._steps += 1

    @workflow.signal
    async def finish(self) -> None:
        self._done = True


async def _scheduled_queues(handle) -> list[str]:
    history = await handle.fetch_history()
    return [
        event.activity_task_scheduled_event_attributes.task_queue.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


async def _completed_count(handle) -> int:
    history = await handle.fetch_history()
    return sum(
        1
        for event in history.events
        if event.HasField("activity_task_completed_event_attributes")
    )


async def _await_completions(handle, n: int) -> None:
    for _ in range(300):
        if await _completed_count(handle) >= n:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"only {await _completed_count(handle)} of {n} activities completed; "
        f"scheduled on {await _scheduled_queues(handle)}"
    )


async def _drain(handle) -> None:
    await handle.signal(PatchMemoProbe.finish)
    await handle.signal(PatchMemoProbe.step)
    await handle.result()


async def test_an_execution_that_predates_the_marker_never_adopts_it() -> None:
    """The behaviour #251's operators actually get.

    Not "in-flight executions migrate on their next tick" — they do not.
    The second half matters just as much: a brand new execution DOES route,
    which is what makes this migration-by-attrition rather than a flip that
    silently did nothing.
    """
    DEPLOYED["flip"] = False
    async with await WorkflowEnvironment.start_local() as env:
        try:
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[PatchMemoProbe],
                activities=[_fake_submit],
            ):
                handle = await env.client.start_workflow(
                    PatchMemoProbe.run,
                    id=f"patch-memo-{uuid.uuid4()}",
                    task_queue=TASK_QUEUE,
                )
                await handle.signal(PatchMemoProbe.step)
                await _await_completions(handle, 1)

            assert await _scheduled_queues(handle) == [TASK_QUEUE]

            # --- the flip deploys: same execution, restarted worker ---
            DEPLOYED["flip"] = True
            async with Worker(
                env.client,
                task_queue=TASK_QUEUE,
                workflows=[PatchMemoProbe],
                activities=[_fake_submit],
            ):
                await handle.signal(PatchMemoProbe.step)
                await _await_completions(handle, 2)
                in_flight = await _scheduled_queues(handle)
                await _drain(handle)

                fresh = await env.client.start_workflow(
                    PatchMemoProbe.run,
                    id=f"patch-memo-new-{uuid.uuid4()}",
                    task_queue=TASK_QUEUE,
                )
                await fresh.signal(PatchMemoProbe.step)
                await _await_completions(fresh, 1)
                started_after = await _scheduled_queues(fresh)
                await _drain(fresh)
        finally:
            DEPLOYED["flip"] = False

    assert in_flight == [TASK_QUEUE, TASK_QUEUE], (
        f"an execution predating the marker scheduled onto {in_flight}; if "
        "this starts failing, patched() memoization changed and ADR-008's "
        "rollout note must change with it"
    )
    assert EXECUTION_TASK_QUEUE not in in_flight
    assert started_after == [EXECUTION_TASK_QUEUE], (
        f"a new execution scheduled onto {started_after}; the flip is not "
        "reaching new work at all"
    )


# ---------------------------------------------------------------------------
# DEFERRED_RESUME_PATCH on the real workflow (mctlhq/mctl-agents#1006, T4)
# ---------------------------------------------------------------------------
def _deferred_resume_markers(history: Any) -> int:
    converter = DataConverter.default.payload_converter
    count = 0
    for event in history.events:
        if not event.HasField("marker_recorded_event_attributes"):
            continue
        attrs = event.marker_recorded_event_attributes
        if attrs.marker_name != "core_patch":
            continue
        for payload in attrs.details["patch-data"].payloads:
            count += converter.from_payload(payload)["id"] == DEFERRED_RESUME_PATCH
    return count


async def test_the_deferred_resume_marker_is_recorded_once_fresh_and_never_on_a_merge_watch_hop() -> None:
    """A hop carries the decision instead of asking `patched()` again: in a
    new run the question is unmemoized, so it would answer True for a loop
    that passed its gates without the patch and change that loop's contract."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        activities, _calls, investigate_ran, _ownership = _fake_activities(released=True)
        async with Worker(
            env.client, task_queue=DEV_LOOP_QUEUE, workflows=[DevLoopWorkflow], activities=activities
        ):
            fresh = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/1"),
                id=f"marker-fresh-{uuid.uuid4()}",
                task_queue=DEV_LOOP_QUEUE,
            )
            await investigate_ran.wait()
            await fresh.signal(DevLoopWorkflow.approve, {"approval_ref": "aar_test"})
            await fresh.result()
            fresh_markers = _deferred_resume_markers(await fresh.fetch_history())

            hopped = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(
                    issue_url="https://github.com/mctlhq/mctl-telegram/issues/2",
                    resume=MergeWatchResume(
                        service="mctl-telegram",
                        slug="issue-2-x",
                        deadline="2027-01-01T00:00:00Z",
                        deferred_resume_contract=True,
                        investigate=WorkflowResult(workflow_name="i", phase="Succeeded"),
                        implement=WorkflowResult(workflow_name="m", phase="Succeeded"),
                        approve=WorkflowResult(workflow_name="a", phase="Succeeded"),
                    ),
                ),
                id=f"marker-hop-{uuid.uuid4()}",
                task_queue=DEV_LOOP_QUEUE,
            )
            await hopped.result()
            hopped_markers = _deferred_resume_markers(await hopped.fetch_history())

    assert fresh_markers == 1
    assert hopped_markers == 0
