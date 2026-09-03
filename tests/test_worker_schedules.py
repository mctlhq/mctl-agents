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
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
)

from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.worker import _ensure_schedule, setup_schedules
from orchestrator.temporal.workflows.incidents import IncidentLoopWorkflow

pytestmark = pytest.mark.anyio

SCHEDULE_ID = "incidents-mctl-agents-schedule"


def _schedule(
    every: timedelta,
    *,
    paused: bool = False,
    note: str | None = None,
    overlap: ScheduleOverlapPolicy = ScheduleOverlapPolicy.SKIP,
) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            IncidentLoopWorkflow.run,
            id="incidents-mctl-agents",
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
        state=ScheduleState(note=note, paused=paused),
        policy=SchedulePolicy(overlap=overlap),
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


class TestOverlapPolicy:
    """Duplicate-run behaviour for the scheduled loops (#149 criterion 3).

    Every schedule this worker registers runs a tick that can outlast its own
    interval — reconcile most obviously: it fires every 15 minutes and its
    apply step waits up to 35 for the shared gitops write mutex. What stops
    two of them racing that mutex is the overlap policy, and until now nothing
    declared one: the guarantee rested on temporalio's default being SKIP.

    A default is not a decision. It is not visible to a reader, it is not
    checked, and it moves when a dependency moves.
    """

    async def test_the_effective_policy_is_skip(self):
        """Every registered schedule resolves to SKIP.

        Guards the direction that changes behaviour today — someone setting
        ALLOW_ALL or BUFFER_ALL. It canNOT tell a declared SKIP from an
        inherited one: `Schedule()` fills in a `SchedulePolicy()` whose
        overlap is already SKIP, so the constructed object carries no trace
        of whether anyone chose it. That is the whole reason the declaration
        matters and the reason the next test reads the source instead.
        """
        client = _FakeClient(existing=None)
        await setup_schedules(client)

        assert client.created, "setup_schedules registered nothing"
        for schedule_id, schedule in client.created:
            assert schedule.policy is not None, f"{schedule_id} declares no policy"
            assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP, (
                f"{schedule_id} overlap is {schedule.policy.overlap}, not SKIP — two ticks "
                "of the same loop would run concurrently against the shared gitops mutex"
            )

    async def test_every_schedule_declares_the_policy_explicitly(self):
        """The declaration itself is the protection, so it is checked.

        The first version of this test asserted only the effective value and
        was FALSE CONFIDENCE: deleting `policy=` from a schedule left it
        green, because the SDK default happens to be SKIP too. An
        undeclared policy behaves identically right up until temporalio
        changes its default — at which point two reconcile ticks race the
        gitops write mutex and nothing in this repository ever said they
        should not.

        Source inspection rather than object inspection, for the same reason
        `_check_legacy_env_override` greps for a real `os.getenv` call: the
        claim is about what the code says, and the object cannot answer it.
        """
        client = _FakeClient(existing=None)
        await setup_schedules(client)

        source = inspect.getsource(setup_schedules)
        declared = source.count("policy=SchedulePolicy(")
        assert declared == len(client.created), (
            f"{len(client.created)} schedules registered but {declared} declare "
            "policy=SchedulePolicy(...) — an undeclared one inherits temporalio's "
            "default, which is SKIP today and is not a decision this repo made"
        )

    async def test_a_stale_overlap_policy_is_converged(self):
        """The reason this test exists at all.

        `_ensure_schedule` converged only `.spec`, and every schedule here is
        already registered on the cluster — so `create_schedule` never runs
        again and a newly declared policy would be decorative. That is not a
        hypothetical: the same function's docstring records the interval being
        decorative for a year for exactly this reason.
        """
        existing = _schedule(timedelta(hours=1), overlap=ScheduleOverlapPolicy.ALLOW_ALL)
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        assert len(client.handle.updates) == 1, "a stale overlap policy was not pushed"
        updated = client.handle.updates[0].schedule
        assert updated.policy.overlap == ScheduleOverlapPolicy.SKIP

    async def test_converging_the_policy_does_not_disturb_the_spec_or_pause(self):
        """A policy-only convergence must not rewrite the interval and must
        not un-pause: `incidents-mctl-agents-schedule` is paused on purpose
        (#179), and a deploy that quietly restarts the responder is the
        failure this repo already had once."""
        existing = _schedule(
            timedelta(hours=1),
            paused=True,
            note="Paused 2026-08-15: see #179",
            overlap=ScheduleOverlapPolicy.ALLOW_ALL,
        )
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        updated = client.handle.updates[0].schedule
        assert updated.policy.overlap == ScheduleOverlapPolicy.SKIP
        assert updated.spec.intervals == [ScheduleIntervalSpec(every=timedelta(hours=1))]
        assert updated.state.paused is True
        assert updated.state.note == "Paused 2026-08-15: see #179"


class TestConvergenceLogging:
    """These logs are the only signal from outside that a declared value
    reached the cluster, so they have to name what actually converged.

    The version before this said "spec converged" for every update, including
    one that pushed only an overlap policy — one field's log standing in for
    another's (claude P3 on #297). Nothing caught it because nothing asserted
    the messages at all.
    """

    async def test_a_policy_only_convergence_does_not_claim_the_spec_changed(self, caplog):
        existing = _schedule(timedelta(hours=1), overlap=ScheduleOverlapPolicy.ALLOW_ALL)
        client = _FakeClient(existing=existing)

        with caplog.at_level(logging.INFO):
            await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        messages = [r.getMessage() for r in caplog.records]
        assert any("converged to the declared overlap policy" in m for m in messages), messages
        assert not any("declared spec" in m for m in messages), messages

    async def test_a_spec_only_convergence_does_not_claim_the_policy_changed(self, caplog):
        client = _FakeClient(existing=_schedule(timedelta(minutes=30)))

        with caplog.at_level(logging.INFO):
            await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        messages = [r.getMessage() for r in caplog.records]
        assert any("converged to the declared spec" in m for m in messages), messages
        assert not any("overlap policy" in m and "converged" in m for m in messages), messages

    async def test_no_change_says_both_are_current(self, caplog):
        client = _FakeClient(existing=_schedule(timedelta(hours=1)))

        with caplog.at_level(logging.INFO):
            await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        assert client.handle.updates == []
        assert any(
            "spec and overlap policy are current" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]
