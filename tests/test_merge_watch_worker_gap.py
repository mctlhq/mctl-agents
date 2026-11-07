"""A worker gap that crosses the merge-watch deadline (mctlhq/mctl-agents#1029,
M2b-1a'; the code path behind #1021).

#1026 could not cross a 14-day deadline on the time-skipping server: the test
server keeps virtual time locked while a workflow task is scheduled and no
worker has started it, so with no worker polling, time stops at the first
timer that fires in the gap. This harness takes the other route: it starts a
real `DevLoopWorkflow.run` from a `MergeWatchResume` whose absolute `deadline`
lies between "now" and the next poll timer (`MERGE_POLL_INTERVAL`, 15 min).
Worker 1 is stopped after it recorded the first `get_pr_state` result and the
poll timer, virtual time is advanced past the deadline but before the timer
(no timer fires, no task is pending, so time moves freely), and worker 2 then
picks the workflow up. Its first task starts past the deadline.

Scenario A (deadline start+10 min): the watch ends on the deadline with the
pre-gap `OPEN` state and no further `get_pr_state` read; the fake already
answers `MERGED` by then. Scenario B (deadline start+20 min, the control): the
same gap, but the deadline is still ahead, so worker 2 reads `MERGED`.

Mapping of the `_watch_pr` statements this run executes against an ordinary
production merge watch:

- `deadline = _as_utc(resume.deadline)` runs instead of `now +
  MERGE_WATCH_DEADLINE`. DIFFERENCE: production computes it once, 14 days out,
  on its first run; a hopped production run takes it from `resume` as here.
- The flags (`fast_cadence`, `shepherd_in_loop`, `concurrent_ticks`,
  `track_ownership`, `proposal_terminal_end`) come from `resume`, not from
  `workflow.patched`. DIFFERENCE: an ordinary first run evaluates the patch
  markers; a hopped production run reads them from `resume` exactly as here.
  No flag was forced False: the factory supplies the lifecycle-ownership
  fakes.
- `hop_enabled` and `gate_enabled` are evaluated as in production.
- The loop condition, `get_pr_state`, the OPEN branch, ownership tracking and
  `workflow.sleep(cadence.poll_interval)` are the same as in production.
- The shepherd tick is not reached (`poll_index` 0, so this run's first poll is
  1). DIFFERENCE: production reaches it from poll 2 on.
- The merge gate is suppressed by `merge_gate_settled_head` equal to the fake
  head SHA. DIFFERENCE: production would ask a human about an un-settled head.
  A real hop record can carry this state.
- The deadline exit without a read is the same code path as production. Only
  the gap differs: 12 virtual minutes, not 14 days. This is NOT equivalent to
  a 14-day outage.
- One run, no continue-as-new: the same as a production watch that never hops.
- Both workers run with `max_cached_workflows=0` so every task goes to the
  normal queue and worker 2 replays from history. DIFFERENCE: production
  workers use the SDK default cache (sticky queues).

Honest limit: in scenario A the deadline exit without a read happens with or
without the gap, because the timer fires after the deadline either way. The
gap is proven by the interruption assertions (identities, silence, same run),
not by the pin.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.argo import WorkflowResult
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.workflows.dev_loop import (
    MERGE_POLL_INTERVAL,
    DevLoopWorkflow,
    IssueRef,
    MergeWatchResume,
)
from tests.temporal_harness import Worker
from tests.test_dev_loop_workflow import MERGED_PR, _fake_activities

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-merge-watch-worker-gap"
ISSUE_URL = "https://github.com/mctlhq/mctl-telegram/issues/88"
SERVICE = "mctl-telegram"
SLUG = "issue-88-fake-title"
HEAD_SHA = "abc1234def5678"
WORKER_1 = "gap-worker-1"
WORKER_2 = "gap-worker-2"
GAP = timedelta(minutes=12)

OPEN_PR = PRState(
    found=True,
    pr_url=MERGED_PR.pr_url,
    repo=MERGED_PR.repo,
    number=MERGED_PR.number,
    state="OPEN",
    head_sha=HEAD_SHA,
)
MERGED_AFTER_GAP = dataclasses.replace(MERGED_PR, head_sha=HEAD_SHA)

OWNED_ENTITY_ID = "ent-gap-1"
OWNER_EPOCH = 3


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@dataclass
class _Holder:
    state: PRState = OPEN_PR
    calls: list[PRState] = field(default_factory=list)

    @property
    def merged_returns(self) -> int:
        return sum(1 for c in self.calls if c.state == "MERGED")


def _activities(holder: _Holder) -> list[Any]:
    activities, *_ = _fake_activities(released=True, pr_states=[OPEN_PR])

    @activity.defn(name="get_pr_state")
    async def get_pr_state(service: str, slug: str) -> PRState:
        holder.calls.append(holder.state)
        return holder.state

    return [a for a in activities if a.__temporal_activity_definition.name != "get_pr_state"] + [get_pr_state]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _resume_record(t0: datetime, offset: timedelta) -> MergeWatchResume:
    """A record shaped like one `_watch_pr` itself would hand over at a hop."""
    return MergeWatchResume(
        service=SERVICE,
        slug=SLUG,
        deadline=_iso(t0 + offset),
        last_pr=OPEN_PR,
        saw_open_pr=True,
        poll_index=0,
        fast_cadence=True,
        shepherd_in_loop=True,
        concurrent_ticks=True,
        track_ownership=True,
        proposal_terminal_end=True,
        deferred_resume_contract=True,
        merge_gate_settled_head=HEAD_SHA,
        owned_entity_id=OWNED_ENTITY_ID,
        owner_epoch=OWNER_EPOCH,
        owned_head_sha=HEAD_SHA,
        investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
        implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
        approve=WorkflowResult(workflow_name="mctl-agents-approve-fake", phase="Succeeded"),
        hops=0,
    )


def _worker(env: WorkflowEnvironment, holder: _Holder, identity: str) -> Worker:
    return Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[DevLoopWorkflow],
        activities=_activities(holder),
        identity=identity,
        max_cached_workflows=0,
    )


def _ts(event: Any) -> datetime:
    return event.event_time.ToDatetime(tzinfo=UTC)


def _of(events: list[Any], event_type: int) -> list[Any]:
    return [e for e in events if e.event_type == event_type]


def _pr_state_schedules(events: list[Any]) -> list[Any]:
    return [
        e
        for e in _of(events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
        if e.activity_task_scheduled_event_attributes.activity_type.name == "get_pr_state"
    ]


def _first_pass_recorded(events: list[Any]) -> bool:
    """The get_pr_state result and the poll timer that follows it are in the history."""
    done = [
        e.event_id
        for e in _of(events, EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED)
        if any(
            s.event_id == e.activity_task_completed_event_attributes.scheduled_event_id
            for s in _pr_state_schedules(events)
        )
    ]
    # TimerStarted is committed by the same workflow task that completed
    # after the result, so its presence means that task is recorded.
    return any(e.event_id > done[0] for e in _of(events, EventType.EVENT_TYPE_TIMER_STARTED)) if done else False


@dataclass
class _Run:
    result: Any
    events: list[Any]
    holder: _Holder
    record: MergeWatchResume
    handle: WorkflowHandle
    run_id_before: str
    gap_start: datetime
    gap_end: datetime
    deadline: datetime


async def _run_with_gap(env: WorkflowEnvironment, offset: timedelta) -> _Run:
    holder = _Holder()
    t0 = await env.get_current_time()
    record = _resume_record(t0, offset)
    workflow_id = f"merge-watch-gap-{offset.total_seconds():.0f}"

    async with _worker(env, holder, WORKER_1):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=ISSUE_URL, resume=record),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        run_id_before = handle.result_run_id or ""
        for _ in range(400):
            history = await handle.fetch_history()
            if _first_pass_recorded(list(history.events)):
                break
            await asyncio.sleep(0.05)
        else:
            names = [EventType.Name(e.event_type) for e in history.events]
            raise AssertionError(f"worker 1 never recorded the first poll and the poll timer: {names}")

    # The gap: no worker, no pending task, no timer in range.
    holder.state = MERGED_AFTER_GAP
    gap_start = await env.get_current_time()
    await env.sleep(GAP)
    gap_end = await env.get_current_time()

    async with _worker(env, holder, WORKER_2):
        result = await handle.result()
    history = await handle.fetch_history()
    return _Run(
        result=result,
        events=list(history.events),
        holder=holder,
        record=record,
        handle=handle,
        run_id_before=run_id_before,
        gap_start=gap_start,
        gap_end=gap_end,
        deadline=t0 + offset,
    )


async def _started_input(run: _Run, env: WorkflowEnvironment) -> IssueRef:
    started = _of(run.events, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED)[0]
    payloads = list(started.workflow_execution_started_event_attributes.input.payloads)
    decoded = await env.client.data_converter.decode(payloads, [IssueRef])
    return decoded[0]


async def _assert_interrupted(run: _Run, env: WorkflowEnvironment) -> None:
    events = run.events
    task_started = _of(events, EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED)
    before = {
        e.workflow_task_started_event_attributes.identity for e in task_started if _ts(e) <= run.gap_start
    }
    after = {e.workflow_task_started_event_attributes.identity for e in task_started if _ts(e) >= run.gap_end}
    assert before == {WORKER_1}
    assert after == {WORKER_2}
    assert before != after

    silent = [
        e
        for e in (
            *_of(events, EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED),
            *task_started,
            *_pr_state_schedules(events),
        )
        if run.gap_start <= _ts(e) <= run.gap_end
    ]
    # The first post-gap task may be scheduled by the timer firing, after the
    # gap ends; nothing may land inside the gap itself.
    assert [e.event_id for e in silent] == []

    description = await run.handle.describe()
    assert description.run_id == run.run_id_before
    assert description.id == run.handle.id
    assert not _of(events, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW)
    first = _of(events, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED)[0]
    assert first.workflow_execution_started_event_attributes.first_execution_run_id == run.run_id_before

    issue = await _started_input(run, env)
    assert issue.resume is not None
    assert issue.resume.deadline == run.record.deadline


async def test_deadline_in_gap_ends_watch_without_reading_merged(env):
    run = await _run_with_gap(env, timedelta(minutes=10))
    await _assert_interrupted(run, env)

    assert len(_pr_state_schedules(run.events)) == 1
    assert run.holder.merged_returns == 0
    assert run.result.pr is not None and run.result.pr.state == "OPEN"

    post_gap = [
        e for e in _of(run.events, EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED) if _ts(e) >= run.gap_end
    ]
    assert post_gap
    assert _ts(post_gap[0]) >= run.deadline


async def test_deadline_after_poll_timer_reads_merged_after_gap(env):
    run = await _run_with_gap(env, timedelta(minutes=20))
    await _assert_interrupted(run, env)

    schedules = _pr_state_schedules(run.events)
    assert len(schedules) == 2
    assert _ts(schedules[1]) >= run.gap_end
    assert run.holder.merged_returns == 1
    assert run.result.pr is not None and run.result.pr.state == "MERGED"


async def test_resume_record_fields_reach_the_merge_watch_path(env):
    run = await _run_with_gap(env, timedelta(minutes=10))
    issue = await _started_input(run, env)
    got, want = issue.resume, run.record
    assert got is not None

    assert got.deadline == want.deadline
    assert got.fast_cadence is True
    assert got.shepherd_in_loop is True
    assert got.concurrent_ticks is True
    assert got.track_ownership is True
    assert got.proposal_terminal_end is True
    assert got.deferred_resume_contract is True
    assert got.saw_open_pr is True
    assert got.poll_index == 0
    assert got.merge_gate_settled_head == HEAD_SHA
    assert got.owned_entity_id == OWNED_ENTITY_ID
    assert got.owner_epoch == OWNER_EPOCH
    assert got.owned_head_sha == HEAD_SHA
    assert got.last_pr == OPEN_PR
    assert got.investigate == want.investigate
    assert got.implement == want.implement
    assert got.approve == want.approve
    assert got.hops == 0
    assert got == want

    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in _of(run.events, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    ]
    assert "submit_and_wait" not in scheduled
    assert "get_pr_state" in scheduled

    timers = _of(run.events, EventType.EVENT_TYPE_TIMER_STARTED)
    assert timers
    assert timers[0].timer_started_event_attributes.start_to_fire_timeout.ToTimedelta() == MERGE_POLL_INTERVAL
