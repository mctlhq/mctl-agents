"""#1021 M2b-1d-2b-i (#1055): the merge watch's deadline decision is wired.

A fresh (patched) run that rides to the 14-day deadline reads the PR once more
before it lets go of the claim. These tests run on the time-skipping server
with a `get_pr_state` fake that answers OPEN until the watch deadline and a
chosen answer after it. The committed-fixture half of the slice (recorder,
D15b, D16) is 1d-2b-ii and is not here.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.workflows.dev_loop import (
    FINAL_READ_ATTEMPTS,
    MERGE_WATCH_RECONCILE_UNRECORDED,
    DevLoopWorkflow,
    IssueRef,
)
from tests.temporal_harness import Worker
from tests.test_dev_loop_workflow import MERGED_PR, _fake_activities

pytestmark = pytest.mark.anyio

HEAD = "abc1234def5678"
OPEN_PR = dataclasses.replace(MERGED_PR, state="OPEN", merged=False, head_sha=HEAD)


# The workflow sandbox re-imports the module, so the 14-day deadline cannot be
# patched; the time-skipping server fast-forwards the ~1344 polls instead.
DEADLINE = timedelta(days=14)


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@dataclasses.dataclass
class _Run:
    handle: Any
    events: list[Any]
    ops: list[Any]
    reads: list[datetime]
    cutoff: datetime
    result: Any = None
    failure: BaseException | None = None


async def _ride_to_deadline(env: WorkflowEnvironment, after_deadline: Any) -> _Run:
    """Run a fresh watch to its deadline. `after_deadline` is a PRState, or an
    exception type name string (raised as a non-retryable ApplicationError)
    for every read at or after the deadline."""
    base, _calls, investigate_ran, ops = _fake_activities(released=True)
    reads: list[datetime] = []
    cutoff_holder: dict[str, datetime] = {}

    @activity.defn(name="get_pr_state")
    async def get_pr_state(service: str, slug: str) -> PRState:
        now = await env.get_current_time()
        cutoff_holder.setdefault("start", now)
        # Polls land every 15 min from `start` (the first poll is ~43 s after
        # the watch began); the last in-loop poll is at about start+13d23h45m
        # and the final read at about start+14d. A cutoff between them keeps
        # every in-loop poll OPEN.
        if now < cutoff_holder["start"] + DEADLINE - timedelta(minutes=5):
            return OPEN_PR
        reads.append(now)
        if isinstance(after_deadline, str):
            raise ApplicationError("github unreachable", type=after_deadline, non_retryable=True)
        return after_deadline

    activities = [a for a in base if a.__temporal_activity_definition.name != "get_pr_state"] + [get_pr_state]
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/1055"),
            id=f"final-read-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        with anyio.fail_after(10):
            await investigate_ran.wait()
        await handle.signal(DevLoopWorkflow.approve, {"approval_ref": "aar_test"})
        run = _Run(handle=handle, events=[], ops=ops, reads=reads, cutoff=datetime.now(UTC))
        try:
            run.result = await handle.result()
        except WorkflowFailureError as exc:
            run.failure = exc
    run.events = list((await handle.fetch_history()).events)
    return run


def _scheduled(run: _Run, name: str) -> int:
    return sum(
        1
        for e in run.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name == name
    )


async def test_final_read_merged_writes_terminal(env):
    """D3: a PR that merged near the deadline is reported MERGED, `terminal` only."""
    run = await _ride_to_deadline(env, dataclasses.replace(MERGED_PR, head_sha=HEAD))
    assert run.failure is None
    assert len(run.reads) == 1
    assert run.result.pr is not None and run.result.pr.state == "MERGED"
    assert run.result.watch_end == "final-read"
    ops = [o.op for o in run.ops]
    assert ops.count("terminal") == 1
    assert "release" not in ops


async def test_final_read_open_releases_with_fresh_state(env):
    """D4: still OPEN at the deadline: `final-read`, release on the fresh head."""
    fresh = dataclasses.replace(OPEN_PR, head_sha="fresh999")
    run = await _ride_to_deadline(env, fresh)
    assert run.failure is None
    assert run.result.pr is not None and run.result.pr.head_sha == "fresh999"
    assert run.result.watch_end == "final-read"
    releases = [o for o in run.ops if o.op == "release"]
    assert len(releases) == 1 and releases[0].version == "fresh999"
    assert "terminal" not in [o.op for o in run.ops]


async def test_final_read_closed_writes_terminal(env):
    """D5: CLOSED at the deadline: `terminal`, `final-read`."""
    run = await _ride_to_deadline(env, dataclasses.replace(OPEN_PR, state="CLOSED"))
    assert run.failure is None
    assert run.result.pr is not None and run.result.pr.state == "CLOSED"
    assert run.result.watch_end == "final-read"
    ops = [o.op for o in run.ops]
    assert ops.count("terminal") == 1 and "release" not in ops


@pytest.mark.parametrize(
    "after_deadline",
    ["ProposalListingError", "SomethingElse", PRState(found=False)],
    ids=["transient", "non-transient", "not-found"],
)
async def test_unreadable_final_read_fails_closed(env, after_deadline, caplog):
    """D6: an unreadable final read ends Failed with the claim retained."""
    run = await _ride_to_deadline(env, after_deadline)
    assert run.failure is not None
    cause = run.failure.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == MERGE_WATCH_RECONCILE_UNRECORDED
    assert len(run.reads) == FINAL_READ_ATTEMPTS
    ops = [o.op for o in run.ops]
    assert "release" not in ops and "terminal" not in ops
    assert _scheduled(run, "record_merge_watch_reconcile") == 0
    assert _scheduled(run, "confirm_no_pr") == 0
