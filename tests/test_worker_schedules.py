"""`_ensure_schedule` — create-or-converge semantics for Temporal schedules.

The helper exists because `create_schedule` is a no-op once a schedule is
registered, which made the interval declared in worker.py decorative on any
cluster that already had it. These tests pin the three behaviours that
matter, all of which would otherwise fail silently — the exact failure mode
the helper was written to end:

  1. a differing spec is actually pushed;
  2. an already-current spec is left alone (no pointless update per boot);
  3. `state` survives — the incidents schedule is paused pending a manual
     verification run (mctl-agents#179), and a deploy must not un-pause it.
"""
from __future__ import annotations

import inspect
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleState,
)

from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.worker import _ensure_schedule
from orchestrator.temporal.workflows.incidents import IncidentLoopWorkflow

pytestmark = pytest.mark.anyio

SCHEDULE_ID = "incidents-mctl-agents-schedule"


def _schedule(every: timedelta, *, paused: bool = False, note: str | None = None) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            IncidentLoopWorkflow.run,
            id="incidents-mctl-agents",
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
        state=ScheduleState(note=note, paused=paused),
    )


class _FakeHandle:
    def __init__(self, existing: Schedule, *, fail: bool = False) -> None:
        self.existing = existing
        self.fail = fail
        self.updates: list[object] = []

    async def update(self, updater) -> None:
        if self.fail:
            raise RuntimeError("frontend unreachable")
        result = updater(SimpleNamespace(description=SimpleNamespace(schedule=self.existing)))
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            self.updates.append(result)


class _FakeClient:
    def __init__(self, existing: Schedule | None, *, handle_fails: bool = False) -> None:
        self.created: list[tuple[str, Schedule]] = []
        self.handle = _FakeHandle(existing, fail=handle_fails) if existing is not None else None

    async def create_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        if self.handle is not None:
            raise ScheduleAlreadyRunningError
        self.created.append((schedule_id, schedule))

    def get_schedule_handle(self, schedule_id: str):
        assert self.handle is not None
        return self.handle


class TestEnsureSchedule:
    async def test_creates_when_absent(self):
        client = _FakeClient(existing=None)
        desired = _schedule(timedelta(hours=1))

        await _ensure_schedule(client, SCHEDULE_ID, desired, "IncidentLoopWorkflow")

        assert [sid for sid, _ in client.created] == [SCHEDULE_ID]

    async def test_converges_a_stale_interval(self):
        """The 30min -> 1h change this PR makes: without the update call it
        would never reach a cluster that already had the schedule."""
        existing = _schedule(timedelta(minutes=30))
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        assert len(client.handle.updates) == 1
        updated = client.handle.updates[0].schedule
        assert updated.spec.intervals == [ScheduleIntervalSpec(every=timedelta(hours=1))]

    async def test_preserves_paused_state_and_note(self):
        """`incidents-mctl-agents-schedule` is paused on purpose. Only
        `.spec` may be reassigned; touching `state` would silently restart
        the responder on the next worker rollout."""
        existing = _schedule(timedelta(minutes=30), paused=True, note="Paused 2026-08-15: see #179")
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        updated = client.handle.updates[0].schedule
        assert updated.state.paused is True
        assert updated.state.note == "Paused 2026-08-15: see #179"

    async def test_no_update_when_spec_already_current(self):
        client = _FakeClient(existing=_schedule(timedelta(hours=1), paused=True))

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        assert client.handle.updates == []

    async def test_update_failure_does_not_take_the_worker_down(self):
        """Schedule registration is startup housekeeping — a Temporal blip
        here must not stop the worker from serving its task queue."""
        client = _FakeClient(existing=_schedule(timedelta(minutes=30)), handle_fails=True)

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        assert client.handle.updates == []
