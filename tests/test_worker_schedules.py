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
from datetime import UTC, datetime, timedelta
from math import lcm
from types import SimpleNamespace

import pytest
from temporalio.api.workflow.v1 import NewWorkflowExecutionInfo
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
from temporalio.converter import DataConverter

from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.scheduled_dispatch import (
    DISPATCH_SCHEDULE_PREFIX,
    DISPATCH_SCHEDULE_SUFFIX,
    RETIRED_DISPATCH_SCHEDULE_IDS,
    WEEKLY_DISPATCH_TARGETS,
    DispatchTarget,
)
from orchestrator.temporal.worker import (
    ISSUE_POLL_SCHEDULE_ID,
    _ensure_schedule,
    _gc_dispatch_schedules,
    setup_schedules,
)
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


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeClient:
    data_converter = DataConverter.default

    def __init__(self, existing: Schedule | None, *, handle_fails: bool = False, listed=()) -> None:
        self.created: list[tuple[str, Schedule]] = []
        self.listed = list(listed)
        self.list_fails = False
        self.deleted: list[str] = []
        self.delete_fails: set[str] = set()
        self.handle = _FakeHandle(existing, fail=handle_fails) if existing is not None else None

    async def create_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        if self.handle is not None:
            raise ScheduleAlreadyRunningError
        self.created.append((schedule_id, schedule))

    async def list_schedules(self):
        if self.list_fails:
            raise RuntimeError("visibility down")
        return _AsyncIter(SimpleNamespace(id=i) for i in self.listed)

    def get_schedule_handle(self, schedule_id: str):
        if schedule_id in self.listed:
            client = self

            class _H:
                async def delete(self_inner):
                    if schedule_id in client.delete_fails:
                        raise RuntimeError("nope")
                    client.deleted.append(schedule_id)

            return _H()
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


def _minutes_of_hour(interval: ScheduleIntervalSpec) -> set[int]:
    """Which minutes past the hour an interval schedule fires on.

    The window is `lcm(period, 60)`, not a flat 60. For a period that divides
    an hour — every schedule here today — the two are the same. For one that
    does not (say 25 minutes) the firing pattern SHIFTS from hour to hour, so
    scanning only the first 60 absolute minutes reports a subset and the check
    quietly stops being a check: 25/0 fires at absolute 75, i.e. :15, which a
    60-minute scan never reaches (agy on #309).
    """
    period = int(interval.every.total_seconds() // 60)
    offset = int((interval.offset or timedelta()).total_seconds() // 60)
    assert period > 0, f"schedule fires more often than once a minute: {interval}"
    return {m % 60 for m in range(lcm(period, 60)) if (m - offset) % period == 0}


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
        # The weekly dispatch targets share ONE declaration inside a loop.
        expected = len(client.created) - len(WEEKLY_DISPATCH_TARGETS) + (1 if WEEKLY_DISPATCH_TARGETS else 0)
        assert declared == expected, (
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


class TestConvergenceIsNotDestructive:
    """Converging one field must not silently drop the others.

    `schedule.spec = desired.spec` and `schedule.policy = desired.policy`
    replaced whole objects, so a live schedule's jitter, calendars,
    catchup_window or pause_on_failure disappeared — and only on the boots
    where an update happened to trigger, which makes the loss intermittent
    and easy to attribute to something else (agy P2 on #297).

    This code declares an interval and an overlap policy. Those are the only
    two things it gets to decide.
    """

    async def test_a_policy_convergence_keeps_the_rest_of_the_policy(self):
        existing = _schedule(timedelta(hours=1), overlap=ScheduleOverlapPolicy.ALLOW_ALL)
        existing.policy.catchup_window = timedelta(hours=6)
        existing.policy.pause_on_failure = True
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        updated = client.handle.updates[0].schedule
        assert updated.policy.overlap == ScheduleOverlapPolicy.SKIP
        assert updated.policy.catchup_window == timedelta(hours=6)
        assert updated.policy.pause_on_failure is True

    async def test_a_spec_convergence_keeps_the_rest_of_the_spec(self):
        existing = _schedule(timedelta(minutes=30))
        existing.spec.jitter = timedelta(seconds=90)
        existing.spec.time_zone_name = "Europe/Berlin"
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, SCHEDULE_ID, _schedule(timedelta(hours=1)), "IncidentLoopWorkflow")

        updated = client.handle.updates[0].schedule
        assert updated.spec.intervals == [ScheduleIntervalSpec(every=timedelta(hours=1))]
        assert updated.spec.jitter == timedelta(seconds=90)
        assert updated.spec.time_zone_name == "Europe/Berlin"


class TestConvergenceRetries:
    """`update()` uses optimistic concurrency and may call the callback again
    after a conflicting server-side write.

    `changed` lives in the enclosing scope, so without a reset an earlier
    attempt's entries survive into the next — and a retry that decides no
    update is needed would still log a convergence that never happened (agy
    P3 on #297). The log lying about the cluster is the one thing these
    messages exist not to do.
    """

    async def test_a_retry_does_not_carry_the_previous_attempts_verdict(self, caplog):
        """First call sees a stale interval, second sees a current one — as a
        server-side write landing between attempts would produce. The verdict
        must come from the LAST call."""
        stale = _schedule(timedelta(minutes=30))
        current = _schedule(timedelta(hours=1))
        client = _FakeClient(existing=stale)

        seen: list[int] = []

        async def _twice(updater):
            for existing in (stale, current):
                seen.append(1)
                # Mirrors _FakeHandle.update above, which has always handled
                # both shapes. `_converge_spec` is `async def`, so awaiting
                # unconditionally is correct TODAY and this changes no
                # behaviour — but two mocks in one file disagreeing about the
                # same SDK contract is the kind of difference a reader has to
                # resolve by going and checking, and a reviewer flagged it
                # three times running for exactly that reason.
                result = updater(
                    SimpleNamespace(description=SimpleNamespace(schedule=existing))
                )
                if inspect.isawaitable(result):
                    result = await result
                if result is not None and existing is current:
                    client.handle.updates.append(result)
            return None

        client.handle.update = _twice  # type: ignore[method-assign]

        with caplog.at_level(logging.INFO):
            await _ensure_schedule(client, SCHEDULE_ID, current, "IncidentLoopWorkflow")

        assert len(seen) == 2, "the callback was not driven twice"
        messages = [r.getMessage() for r in caplog.records]
        assert any("spec and overlap policy are current" in m for m in messages), messages
        assert not any("converged to the declared" in m for m in messages), messages


class TestIntakeCadence:
    """The intake poller's tick rate, which is a latency budget.

    This value drifted to 12 hours on 2026-08-09 with a commit message citing
    "excessive GitHub API polling", and nothing here noticed. It surfaced on
    2026-09-04 as five issues sitting in `agents:intake` for hours with no
    proposal — not a failure anyone could see, because the schedule was
    healthy and every tick returned `{"started": 0}` truthfully.

    An idle tick is one `gh search issues` call, so the cost side of that
    trade was never real. The latency side was.
    """

    async def test_intake_polls_at_least_four_times_an_hour(self):
        client = _FakeClient(existing=None)
        await setup_schedules(client)

        registered = dict(client.created)
        spec = registered[ISSUE_POLL_SCHEDULE_ID].spec
        assert spec.intervals, f"{ISSUE_POLL_SCHEDULE_ID} declares no interval"
        every = spec.intervals[0].every
        assert every <= timedelta(minutes=15), (
            f"intake polls every {every} — an issue labelled just after a tick waits "
            f"that long for a proposal, and an idle tick costs one `gh search` call"
        )

    async def test_no_two_schedules_fire_on_the_same_minute(self):
        """No two registered schedules share a firing minute.

        Interval schedules are aligned to the epoch, so `every` and `offset`
        together fix the phase. reconcile, issue-poll and the incident loop all
        end in a step that holds `mctl-gitops-main-writes`, so two of them
        landing on the same minute is contention on the shared gitops write
        mutex — the thing the Argo cron was moved off `*/5` to avoid.

        The first version of this test grouped schedules by `every` and only
        compared offsets WITHIN a group. That is blind by construction to the
        collision that was actually live: incidents at `every=1h, offset=0`
        fires at :00, exactly where reconcile's `every=15m` already fires, and
        the two groups were never compared. Confirmed on the cluster before
        fixing — `temporal schedule list` at 2026-09-04 20:59:56 showed both
        with `NextRunTime: 2 seconds from now` (agy P2 on #309).

        Simulating the phases over their least common multiple compares every
        pair regardless of cadence.
        """
        client = _FakeClient(existing=None)
        await setup_schedules(client)

        def minutes(interval: ScheduleIntervalSpec) -> tuple[int, int]:
            period = interval.every.total_seconds() / 60
            offset = (interval.offset or timedelta()).total_seconds() / 60
            assert period == int(period) and offset == int(offset), (
                f"schedule fires on a sub-minute boundary ({interval}) — this test "
                "reasons in whole minutes and would silently under-report collisions"
            )
            return int(period), int(offset)

        specs: list[tuple[str, int, int]] = []
        for schedule_id, schedule in client.created:
            for interval in schedule.spec.intervals or []:
                period, offset = minutes(interval)
                specs.append((schedule_id, period, offset))

        window = 1
        for _, period, _ in specs:
            window = lcm(window, period)

        fires: dict[int, list[str]] = {}
        for schedule_id, period, offset in specs:
            for minute in range(window):
                if (minute - offset) % period == 0:
                    fires.setdefault(minute, []).append(schedule_id)

        collisions = {m: ids for m, ids in fires.items() if len(ids) > 1}
        assert not collisions, (
            f"schedules fire together at minute(s) {sorted(collisions)}: {collisions} — "
            "they race the shared mctl-gitops-main-writes mutex on every such tick"
        )

    async def test_the_weekly_dispatch_fires_sunday_1001_utc(self):
        client = _FakeClient(existing=None)
        await setup_schedules(client)

        schedule = dict(client.created)["dispatch-mctlhq-portfolio-weekly-refresh-schedule"]
        (interval,) = schedule.spec.intervals
        assert interval.every == timedelta(days=7)
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        reference = datetime(2026, 10, 1, tzinfo=UTC)
        period = interval.every
        n = (reference - epoch - interval.offset) // period + 1
        fire = epoch + interval.offset + n * period
        assert (fire.weekday(), fire.hour, fire.minute) == (6, 10, 1)
        assert fire > reference

    async def test_no_schedule_lands_on_an_argo_cron_minute(self):
        """Temporal schedules also avoid the Argo crons holding the same mutex.

        `mctl-gitops-main-writes` is contended from both sides: the Temporal
        schedules here, and CronWorkflows in mctl-gitops whose commit-and-push
        steps take the same mutex. Staggering only the Temporal schedules
        against each other is half the invariant — reconcile at :00/:15/:30/:45
        sat directly on top of two Argo crons until this was checked (agy P3
        on #309).

        The minutes below MIRROR another repository and cannot be derived from
        here, so they are stated explicitly rather than implied by a comment
        on the schedule. If a CronWorkflow's cadence changes in mctl-gitops
        this list has to change with it; a stale mirror is a real limitation,
        but a visible and testable one, which the previous arrangement — a
        prose claim in worker.py that nothing checked — was not.
        """
        client = _FakeClient(existing=None)
        await setup_schedules(client)

        # platform-gitops/argo-workflows/cluster-templates/, schedules as of
        # 2026-09-04: rotate-github-app-tokens `*/30` -> :00/:30;
        # mctl-agents-shepherd `0 7-21/2` -> :00; mctl-agents-incidents
        # `15 * * * *` -> :15.
        argo_minutes = {0, 15, 30}

        offenders: dict[str, int] = {}
        for schedule_id, schedule in client.created:
            for interval in schedule.spec.intervals or []:
                hit = _minutes_of_hour(interval) & argo_minutes
                if hit:
                    offenders[schedule_id] = min(hit)

        assert "dispatch-mctlhq-portfolio-weekly-refresh-schedule" in {sid for sid, _ in client.created}
        assert not offenders, (
            f"{offenders} fire on a minute an Argo cron already uses — both sides "
            "take mctl-gitops-main-writes, so this is contention on the shared mutex"
        )

    def test_the_hour_window_covers_a_period_that_does_not_divide_60(self):
        """The scan window is lcm(period, 60), and that is load-bearing.

        Every schedule registered today has a period dividing an hour, so a
        flat 60-minute scan happens to be complete and this test would pass
        either way against the real ones. It uses a synthetic 25-minute
        schedule instead, where the two windows genuinely disagree: 25/0 fires
        at absolute minute 75, i.e. :15 past the hour — a minute an Argo cron
        uses — which a 60-minute scan never reaches.

        Written against the helper directly rather than through
        setup_schedules, because the bug is only reachable with a cadence this
        repo does not currently register; going through the caller would make
        the assertion true for the wrong reason.
        """
        every_25 = ScheduleIntervalSpec(every=timedelta(minutes=25))

        minutes = _minutes_of_hour(every_25)
        assert 15 in minutes, (
            "a 25-minute schedule fires at :15 past the hour (absolute minute 75); "
            "a window of 60 stops at :50 and never sees it"
        )

        naive = {m for m in range(60) if m % 25 == 0}
        assert 15 not in naive, "the naive 60-minute scan is supposed to miss this"

    def test_the_hour_window_is_exact_for_a_period_that_does_divide_60(self):
        """No over-reporting: the wider window must not invent firing minutes."""
        assert _minutes_of_hour(
            ScheduleIntervalSpec(every=timedelta(minutes=15), offset=timedelta(minutes=3))
        ) == {3, 18, 33, 48}
        assert _minutes_of_hour(
            ScheduleIntervalSpec(every=timedelta(hours=1), offset=timedelta(minutes=11))
        ) == {11}



async def _live_action(ref: str, *, id: str = "dispatch-x-y") -> ScheduleActionStartWorkflow:
    """What describe() returns: an action carrying only raw_info protos."""
    from temporalio.api.common.v1 import Payloads

    from orchestrator.temporal.workflows.scheduled_dispatch import ScheduledDispatchInput

    payloads = await DataConverter.default.encode([ScheduledDispatchInput(repo="a/b", workflow_file="c.yml", ref=ref)])
    raw = NewWorkflowExecutionInfo(workflow_id=id)
    raw.workflow_type.name = "ScheduledDispatchWorkflow"
    raw.task_queue.name = TASK_QUEUE
    raw.input.CopyFrom(Payloads(payloads=payloads))
    return ScheduleActionStartWorkflow("<unset>", raw_info=raw)


def _dispatch_schedule(ref: str, every: timedelta = timedelta(days=7), overlap=ScheduleOverlapPolicy.SKIP):
    from orchestrator.temporal.workflows.scheduled_dispatch import (
        ScheduledDispatchInput,
        ScheduledDispatchWorkflow,
    )

    return Schedule(
        action=ScheduleActionStartWorkflow(
            ScheduledDispatchWorkflow.run,
            ScheduledDispatchInput(repo="a/b", workflow_file="c.yml", ref=ref),
            id="dispatch-x-y",
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
        policy=SchedulePolicy(overlap=overlap),
    )


class TestActionConvergence:
    async def test_stale_ref_is_replaced_and_state_kept(self):
        existing = _dispatch_schedule("main")
        existing.action = await _live_action("main")
        existing.state = ScheduleState(note="held", paused=True)
        client = _FakeClient(existing=existing)
        desired = _dispatch_schedule("release")

        await _ensure_schedule(client, "dispatch-x-y-schedule", desired, "W", converge_action=True)

        assert len(client.handle.updates) == 1
        updated = client.handle.updates[0].schedule
        assert updated.action is desired.action
        assert updated.state.paused is True
        assert updated.state.note == "held"

    async def test_identical_action_no_update(self):
        existing = _dispatch_schedule("main")
        existing.action = await _live_action("main")
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, "s", _dispatch_schedule("main"), "W", converge_action=True)

        assert client.handle.updates == []

    async def test_uncomparable_live_action_is_left_untouched(self, caplog):
        # A live action the SDK does not expose as raw (fingerprint None) must
        # read as "do not touch", never as "differs" -> an update every boot.
        existing = _dispatch_schedule("main")  # constructed, not described: no raw_info
        client = _FakeClient(existing=existing)

        with caplog.at_level(logging.WARNING):
            await _ensure_schedule(client, "s", _dispatch_schedule("release"), "W", converge_action=True)

        assert client.handle.updates == []
        assert "cannot fingerprint the live action" in caplog.text

    async def test_action_difference_ignored_without_opt_in(self):
        existing = _dispatch_schedule("main")
        existing.action = await _live_action("main")
        client = _FakeClient(existing=existing)

        await _ensure_schedule(client, "s", _dispatch_schedule("release"), "W")

        assert client.handle.updates == []

    async def test_all_stale_one_update_and_retry_does_not_double_count(self, caplog):
        existing = _dispatch_schedule("main", every=timedelta(days=1), overlap=ScheduleOverlapPolicy.BUFFER_ONE)
        existing.action = await _live_action("main")
        client = _FakeClient(existing=existing)
        desired = _dispatch_schedule("release")
        handle = client.handle
        original = handle.update

        async def _twice(updater):
            await original(updater)
            handle.updates.clear()
            existing.spec.intervals = [ScheduleIntervalSpec(every=timedelta(days=1))]
            existing.policy.overlap = ScheduleOverlapPolicy.BUFFER_ONE
            existing.action = await _live_action("main")
            await original(updater)

        handle.update = _twice
        with caplog.at_level(logging.INFO):
            await _ensure_schedule(client, "s", desired, "W", converge_action=True)

        assert len(handle.updates) == 1
        assert "spec and overlap policy and action" in caplog.text


class TestGcDispatchSchedules:
    LISTED = (
        "dispatch-a-x-schedule",
        "dispatch-b-y-schedule",
        "reconcile-mctl-agents-schedule",
        "dispatch-foo",
    )

    async def test_only_retired_undeclared_dispatch_schedule_deleted(self):
        client = _FakeClient(existing=None, listed=self.LISTED)
        await _gc_dispatch_schedules(client, {"dispatch-a-x-schedule"}, {"dispatch-b-y-schedule"})
        assert client.deleted == ["dispatch-b-y-schedule"]

    async def test_undeclared_but_not_retired_is_left_in_place(self, caplog):
        # The older-image case: a rollback pod lacks a newer target, so that
        # schedule is undeclared FOR IT. Without a tombstone it must survive.
        client = _FakeClient(existing=None, listed=self.LISTED)
        with caplog.at_level(logging.WARNING):
            await _gc_dispatch_schedules(client, {"dispatch-a-x-schedule"}, set())
        assert client.deleted == []
        assert "dispatch-b-y-schedule is not in RETIRED_DISPATCH_SCHEDULE_IDS" in caplog.text

    async def test_retired_but_declared_is_never_deleted(self):
        client = _FakeClient(existing=None, listed=self.LISTED)
        await _gc_dispatch_schedules(client, {"dispatch-a-x-schedule"}, {"dispatch-a-x-schedule"})
        assert client.deleted == []

    async def test_list_failure_deletes_nothing_and_does_not_raise(self):
        client = _FakeClient(existing=None, listed=self.LISTED)
        client.list_fails = True
        await _gc_dispatch_schedules(client, set(), {"dispatch-a-x-schedule", "dispatch-b-y-schedule"})
        assert client.deleted == []

    async def test_delete_failure_does_not_stop_the_rest(self):
        client = _FakeClient(existing=None, listed=self.LISTED)
        client.delete_fails = {"dispatch-a-x-schedule"}
        await _gc_dispatch_schedules(client, set(), {"dispatch-a-x-schedule", "dispatch-b-y-schedule"})
        assert client.deleted == ["dispatch-b-y-schedule"]

    async def test_non_dispatch_ids_are_never_deleted_even_if_retired(self):
        client = _FakeClient(existing=None, listed=self.LISTED)
        await _gc_dispatch_schedules(client, set(), {"reconcile-mctl-agents-schedule", "dispatch-foo"})
        assert client.deleted == []

    async def test_setup_schedules_deletes_only_tombstoned(self, monkeypatch):
        import orchestrator.temporal.worker as w

        monkeypatch.setattr(w, "RETIRED_DISPATCH_SCHEDULE_IDS", ("dispatch-gone-wf-schedule",))
        client = _FakeClient(
            existing=None,
            listed=("dispatch-gone-wf-schedule", "dispatch-newer-wf-schedule", "incidents-mctl-agents-schedule"),
        )
        await setup_schedules(client)
        assert client.deleted == ["dispatch-gone-wf-schedule"]
        assert client.created

    def test_retired_and_declared_are_disjoint(self):
        declared = {t.schedule_id for t in WEEKLY_DISPATCH_TARGETS}
        assert not declared & set(RETIRED_DISPATCH_SCHEDULE_IDS)
        for sid in RETIRED_DISPATCH_SCHEDULE_IDS:
            assert sid.startswith(DISPATCH_SCHEDULE_PREFIX) and sid.endswith(DISPATCH_SCHEDULE_SUFFIX)


class TestDispatchTargetValidation:
    def _t(self, **kw):
        base = dict(
            repo="mctlhq/portfolio", workflow_file="weekly-refresh.yml", ref="main", weekday=6, hour=10, minute=1
        )
        base.update(kw)
        return DispatchTarget(**base)

    def test_valid(self):
        assert self._t().schedule_id == "dispatch-mctlhq-portfolio-weekly-refresh-schedule"

    @pytest.mark.parametrize(
        "kw",
        [
            {"repo": "mctlhq/../x"},
            {"repo": "a/b/c"},
            {"repo": "mctlhq/portfolio?x"},
            {"workflow_file": "wf.yml/../dispatches"},
            {"workflow_file": "wf.txt"},
            {"workflow_file": ".yml"},
            {"ref": ""},
            {"ref": "a b"},
            {"weekday": 7},
            {"minute": 60},
        ],
    )
    def test_invalid(self, kw):
        with pytest.raises(ValueError):
            self._t(**kw)

    def test_declared_ids_unique_and_shaped(self):
        ids = [t.schedule_id for t in WEEKLY_DISPATCH_TARGETS]
        assert len(ids) == len(set(ids))
        assert all(i.startswith(DISPATCH_SCHEDULE_PREFIX) and i.endswith(DISPATCH_SCHEDULE_SUFFIX) for i in ids)
