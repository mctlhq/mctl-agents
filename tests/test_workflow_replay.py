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

Two consequences, both load-bearing:

- This file is real coverage for the nine existing markers, which all guard
  added or reordered commands.
- It CANNOT verify the #251 queue flip. An unguarded `task_queue=` change
  replays clean. Routing is only visible in recorded history content, which
  is what `test_prepatch_histories_predate_the_exec_queue_flip` reads — a
  green replay run is not evidence that the flip was guarded.

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
from temporalio.worker import Replayer

from tests.replay_scenarios import SCENARIOS, Scenario

pytestmark = pytest.mark.anyio


def _events(scenario: Scenario) -> list[dict]:
    payload = json.loads(scenario.path.read_text(encoding="utf-8"))
    return payload["events"]


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
    expected = {s.path.name for s in SCENARIOS}
    assert on_disk == expected
