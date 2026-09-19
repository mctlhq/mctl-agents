"""Replay recorded histories against today's workflow definitions (#251).

The gap this closes: every other workflow test starts a FRESH workflow in
`WorkflowEnvironment`. None of them can catch the failure `workflow.patched()`
exists to prevent — an edit that changes the commands a workflow schedules,
breaking executions that are already mid-flight. Both branches of a patch can
pass under the test environment while a real 14-day merge watch wedges on
deploy. `Replayer` is the only thing here that answers "does today's code
still agree with a history recorded by an older version?".

Fixtures are recorded by `tools/record_workflow_history.py`; the scenarios
they come from live in `tests/replay_scenarios.py`.

## What replay actually detects — measured, not assumed

`Replayer` compares the SHAPE of the command stream: how many commands, of
what kind, in what order. It does not compare a command's attributes against
the event it is matched to. `handle_command_event` in sdk-core pops the next
command off the queue and feeds it the event; the activity state machine has
no `matches_event` at all.

Measured against these fixtures, on temporalio 1.31.0:

| change to a workflow | replay |
| --- | --- |
| an activity added, removed or reordered | NondeterminismError |
| `task_queue=` added to an existing activity | invisible |
| `start_to_close_timeout` changed | invisible |
| an extra argument added to an existing activity | invisible |
| a divergence in the LAST recorded workflow task | invisible |

That last row was measured for #395 and is the reason there is no fixture
here for "a loop that predates `implement-outcome` still completes on a
failed implement". A history recorded before the marker ends the moment the
implement fails — completing right there is exactly the old behaviour — so
the only divergence today's code could produce sits in the final workflow
task, and replay does not compare it. Verified both ways on temporalio
1.31.0: the same extra `submit_and_wait` command turns the dev_loop_full
fixtures red, where it lands mid-history, and replays clean on a history
that ends at the implement.

Two consequences, both load-bearing:

- Replay is real coverage for the nine existing markers, which all guard
  added or reordered commands.
- Replay CANNOT verify the #251 queue flip. An unguarded `task_queue=` change
  replays clean — measured, not inferred.

So routing is verified from recorded history CONTENT instead, which is the
only place a task queue is visible at all. Two fixtures per scenario:
`*.prepatch.json`, recorded before the flip, must contain no `exec-queue`
marker and no routed activity; `*.patched.json`, recorded after it, must
contain both the marker and every `submit_and_wait` on the execution queue,
and nothing else routed. Neither half alone is enough — the marker without
the queue would pass if `patched()` were called and its result ignored, and
the queue without the marker would pass on an unguarded change.

A `*.patched.json` describes a workflow that started AFTER the flip, and
only that. It is not a picture of what a running dev loop does: `patched()`
memoizes, so an execution that predates the marker keeps taking the
unpatched branch for the rest of its life and never appears in a fixture of
this shape at all. That half is pinned by
`tests/test_patch_memoization.py`, and it is the half that decides how to
read the queue metrics during the soak.

## Why this file asserts on fixture CONTENT, not just on replay

A `*.prepatch.json` is evidence about what an older version scheduled, and it
is the only thing exercising the *unpatched* branch of a marker. Re-recording
one after the change it guards is not a merge conflict and not a test
failure: it silently swaps that evidence for a history of the new behaviour,
and replay keeps passing while the coverage is gone.

So `test_prepatch_histories_predate_the_exec_queue_flip` reads the recorded
patch markers and the recorded task queues directly. A fixture regenerated
after the flip fails there, loudly, instead of passing vacuously.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from orchestrator.temporal.constants import EXECUTION_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE
from tests.replay_scenarios import SCENARIOS, Scenario, record, scenario_by_name

pytestmark = pytest.mark.anyio


def _events_at(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["events"]


def _events(scenario: Scenario) -> list[dict]:
    return _events_at(scenario.path)


def _patch_ids(events: list[dict]) -> set[str]:
    """Patch ids recorded in the history.

    `workflow.patched("x")` does not write a marker named "x": it writes a
    `core_patch` marker whose `patch-data` payload is base64'd
    `{"id": "x", "deprecated": false}`. Matching on the marker name alone
    would match every patch in the file and prove nothing.
    """
    ids: set[str] = set()
    for event in events:
        if event["eventType"] != "EVENT_TYPE_MARKER_RECORDED":
            continue
        attrs = event["markerRecordedEventAttributes"]
        if attrs.get("markerName") != "core_patch":
            continue
        for payload in attrs.get("details", {}).get("patch-data", {}).get("payloads", []):
            ids.add(json.loads(base64.b64decode(payload["data"]))["id"])
    return ids


def _scheduled_activities(events: list[dict]) -> list[tuple[str, str]]:
    """(activity name, task queue) for every ActivityTaskScheduled event."""
    out = []
    for event in events:
        if event["eventType"] != "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED":
            continue
        attrs = event["activityTaskScheduledEventAttributes"]
        out.append((attrs["activityType"]["name"], attrs["taskQueue"]["name"]))
    return out


def _workflow_task_queue(events: list[dict]) -> str:
    started = events[0]["workflowExecutionStartedEventAttributes"]
    return started["taskQueue"]["name"]


_IDS = [s.name for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_IDS)
async def test_recorded_history_replays_against_current_definitions(
    scenario: Scenario,
) -> None:
    """The point of the whole file: today's code must agree with old history.

    A NondeterminismError here means the change on this branch would wedge
    every workflow execution that is already running in production — not that
    the fixture is stale. Guard the change with `workflow.patched()` (see
    ADR-008 D4 and the existing markers in reconcile.py); do not re-record.

    The converse does NOT hold: a green run here does not mean a change is
    replay-safe in every sense. See the capability table in the module
    docstring — attribute-only changes are invisible to replay.
    """
    history = WorkflowHistory.from_json(
        f"replay-{scenario.name}", scenario.path.read_text(encoding="utf-8")
    )
    await Replayer(workflows=scenario.workflows).replay_workflow(history)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_IDS)
def test_prepatch_histories_predate_the_exec_queue_flip(scenario: Scenario) -> None:
    """A fixture re-recorded after the flip must fail here, not pass quietly.

    Two independent signatures of a post-flip recording, because either one
    alone can be absent: the `exec-queue` patch marker, and a submit_and_wait
    scheduled onto a task queue other than the workflow's own. Note that
    EVERY scheduled activity carries a taskQueue in history — routing shows
    up as a DIFFERENT queue, never as a missing field.
    """
    events = _events(scenario)
    assert "exec-queue" not in _patch_ids(events), (
        f"{scenario.path.name} records the exec-queue patch, so it was "
        "recorded AFTER the flip and no longer exercises the unpatched "
        "branch. Restore it from git rather than re-recording."
    )
    workflow_queue = _workflow_task_queue(events)
    routed = [
        (name, queue)
        for name, queue in _scheduled_activities(events)
        if queue != workflow_queue
    ]
    assert not routed, (
        f"{scenario.path.name} schedules {routed} away from the workflow's "
        f"own queue {workflow_queue!r}; a pre-patch history cannot contain "
        "routed activities."
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_IDS)
async def test_patched_history_replays_against_current_definitions(
    scenario: Scenario,
) -> None:
    """The post-flip history must replay too, not just the pre-flip one."""
    history = WorkflowHistory.from_json(
        f"replay-{scenario.name}-patched",
        scenario.patched_path.read_text(encoding="utf-8"),
    )
    await Replayer(workflows=scenario.workflows).replay_workflow(history)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_IDS)
def test_patched_histories_show_submit_and_wait_on_the_execution_queue(
    scenario: Scenario,
) -> None:
    """The actual acceptance criterion for the #251 flip.

    Replay cannot verify routing — it compares command shape and is blind to
    `task_queue`, measured above. So the flip is verified the only way it
    can be: by reading a history recorded after it and checking where the
    activity was scheduled.

    Both halves are asserted. The marker alone would pass if `patched()`
    were called and its result ignored; the queue alone would pass if the
    routing were unguarded. Neither is the change #251 asks for.
    """
    events = _events_at(scenario.patched_path)
    assert "exec-queue" in _patch_ids(events), (
        f"{scenario.patched_path.name} has no exec-queue marker: the routing "
        "change is unguarded, so a rollback could not be replayed."
    )
    workflow_queue = _workflow_task_queue(events)
    scheduled = _scheduled_activities(events)
    submits = [(name, queue) for name, queue in scheduled if name == "submit_and_wait"]
    assert submits, f"{scenario.patched_path.name} records no submit_and_wait"
    assert all(queue == EXECUTION_TASK_QUEUE for _, queue in submits), (
        f"{scenario.patched_path.name} still schedules {submits} on the "
        "control queue"
    )
    # Nothing ELSE may move. The short activities are what the control
    # worker exists to keep responsive; routing one of them to exec would
    # put it behind the long Argo polls, which is the starvation ADR-008
    # removes, reintroduced from the other side.
    strays = [
        (name, queue)
        for name, queue in scheduled
        if name != "submit_and_wait" and queue != workflow_queue
    ]
    assert not strays, f"{scenario.patched_path.name} also routed {strays}"


async def test_todays_code_still_routes_and_still_guards() -> None:
    """Ties the routing assertions to the CODE, not to a committed snapshot.

    `*.patched.json` is a recording. It keeps saying the guard was there and
    the activity was routed no matter what the workflows do afterwards, so on
    its own it cannot catch someone later dropping `workflow.patched(...)` or
    the `task_queue=` argument. This runs the workflow for real against
    today's definitions and reads the history it actually produces.

    One scenario, deliberately: incident_loop is the cheapest (27 events, no
    timers) and the routing decision it exercises is the same
    `patched("exec-queue")` the other two make.
    """
    scenario = scenario_by_name("incident_loop")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle = await record(env.client, scenario)
        history = await handle.fetch_history()

    events = history.to_json_dict()["events"]
    assert "exec-queue" in _patch_ids(events), (
        "the routing change is no longer guarded by workflow.patched(): "
        "in-flight executions would have no marker recording which queue "
        "they used, and a rollback could not be replayed."
    )
    submits = [
        (name, queue)
        for name, queue in _scheduled_activities(events)
        if name == "submit_and_wait"
    ]
    assert submits and all(queue == EXECUTION_TASK_QUEUE for _, queue in submits), (
        f"today's code schedules {submits}; submit_and_wait must go to "
        f"{EXECUTION_TASK_QUEUE}"
    )


def _activity_scheduled_attrs(events: list[dict], name: str) -> list[dict]:
    return [
        e["activityTaskScheduledEventAttributes"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
        and e["activityTaskScheduledEventAttributes"]["activityType"]["name"] == name
    ]


async def test_todays_dev_loop_admits_the_implement_submit_on_its_own_queue() -> None:
    """The #395 flip, verified the only way routing can be: from history.

    Three things, each load-bearing on its own:

    - the `implement-queue` marker is recorded, so a rollback replays and
      the history says which queue this execution used;
    - the implement submit is scheduled on the admission queue and EVERY
      other submit_and_wait stays on exec — routing investigate or approve
      to the admission queue would spend implementer capacity on them;
    - the implement submit carries NO schedule-to-start and NO
      schedule-to-close timeout. On the admission queue the schedule-to-
      start wait is the queue itself; a bound there turns "waiting for
      capacity" back into a failure, one layer earlier than the bug this
      fixes. The server records an unset timeout as its own ten-year cap
      rather than as zero, so the assertion below tolerates both and
      rejects only a bound a run could actually hit.
    """
    scenario = scenario_by_name("dev_loop_full")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle = await record(env.client, scenario)
        history = await handle.fetch_history()

    events = history.to_json_dict()["events"]
    ids = _patch_ids(events)
    assert "implement-queue" in ids
    assert "exec-queue" in ids

    submits = [(a, a["taskQueue"]["name"]) for a in _activity_scheduled_attrs(events, "submit_and_wait")]
    queues = [queue for _, queue in submits]
    assert IMPLEMENTATION_TASK_QUEUE in queues, f"no submit reached the admission queue: {queues}"
    assert queues.count(IMPLEMENTATION_TASK_QUEUE) == 1, f"more than the implement submit was admitted: {queues}"
    assert all(q in (IMPLEMENTATION_TASK_QUEUE, EXECUTION_TASK_QUEUE) for q in queues), queues

    # The server records an UNSET schedule-to-start/close as its own cap
    # (ten years, 315360000s), not as zero — so "no timeout" is asserted
    # as "not a bound anyone could hit", one year being far beyond every
    # deadline in this workflow. A real value set "for safety" would be
    # hours and fails here.
    one_year = 365 * 24 * 3600
    for attrs, queue in submits:
        for key in ("scheduleToStartTimeout", "scheduleToCloseTimeout"):
            value = attrs.get(key)
            if value is None:
                continue
            # No silent fallback: a shape this parser does not recognise
            # would otherwise read as 0, i.e. as "no timeout", and the
            # assertion would pass on exactly the change it exists to
            # catch. Protobuf JSON writes durations as "<seconds>s", and a
            # day it stops doing that is a day this test must say so.
            assert isinstance(value, str) and value.endswith("s"), (
                f"submit_and_wait on {queue} has {key}={value!r}, which this assertion cannot "
                "read as a duration; teach it the new shape rather than trusting the default"
            )
            seconds = float(value.rstrip("s"))
            assert seconds == 0 or seconds >= one_year, (
                f"submit_and_wait on {queue} has {key}={value!r}; a capacity wait must not be a timeout"
            )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_IDS)
def test_every_history_actually_reaches_submit_and_wait(scenario: Scenario) -> None:
    """Guards against a vacuous fixture.

    Every scenario exists to keep a `submit_and_wait` command replayable. A
    scenario whose fakes drift so it stops reaching that activity would still
    replay green and still pass the content guard above, while testing
    nothing about the thing #251 changes.
    """
    names = [name for name, _ in _scheduled_activities(_events(scenario))]
    assert "submit_and_wait" in names, (
        f"{scenario.path.name} records no submit_and_wait; the scenario no "
        "longer covers what it was recorded for."
    )


def test_every_recorded_fixture_belongs_to_a_scenario() -> None:
    """An orphan fixture is a fixture nothing replays.

    Deleting a scenario without deleting its file leaves a history that looks
    like coverage in the directory listing and is never loaded.
    """
    from tests.replay_scenarios import HISTORY_DIR

    on_disk = {p.name for p in Path(HISTORY_DIR).glob("*.json")}
    expected = {s.path.name for s in SCENARIOS} | {s.patched_path.name for s in SCENARIOS}
    assert on_disk == expected

