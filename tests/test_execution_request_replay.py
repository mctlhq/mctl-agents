"""Replay guard for the dispatched DevLoop path (mctlhq/mctl-agents#461).

The dispatched path adds two activities to `DevLoopWorkflow` — the bind
before anything else runs, the advance right after the first investigate —
under `workflow.patched("execution-request-dispatch")`, and only for a loop
whose start input carries an `execution_request_id`. Two things must hold:

1. **Old histories never take the new path.** Every history recorded before
   this change has no `execution_request_id` in its input, so the new
   commands are never scheduled against it; the existing
   `test_workflow_replay.py` fixtures replaying green is that guarantee, and
   `test_no_older_dev_loop_history_records_the_dispatch_marker` pins that
   they really predate it.
2. **A dispatched history replays against today's code.**
   `dev_loop_dispatched.json` is a real history of the path (claim, start,
   fulfil, bind, investigate, advance, park, abandon), recorded from
   `record()` below. Like the implement-sweep fixtures, it is standalone
   rather than a `SCENARIOS` entry: those entries are built around the
   #251 exec-queue migration and assert things (a pre-patch recording) that
   a path born after it cannot have.

Regenerating the fixture (only when the dispatched path's command shape is
MEANT to change, and never to turn a red run green): run
`uv run python -m tests.test_execution_request_replay`, which records a fresh
history with `record()` and overwrites the file.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from orchestrator.temporal.workflows.dev_loop import EXECUTION_REQUEST_PATCH, DevLoopWorkflow

HISTORY_DIR = Path(__file__).resolve().parent / "fixtures" / "histories"
DISPATCHED_HISTORY = HISTORY_DIR / "dev_loop_dispatched.json"

pytestmark = pytest.mark.anyio


def _events(history: dict[str, Any]) -> list[dict]:
    return history["events"]


def _patch_ids(events: list[dict]) -> set[str]:
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


def _scheduled(events: list[dict]) -> list[str]:
    return [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
    ]


async def record(env: WorkflowEnvironment) -> dict[str, Any]:
    """One dispatched loop, end to end against the fake mctl-api, as a
    history dict. The caller has already routed `WorkItemClient._request`
    to `api`."""
    from tests.test_execution_request_dispatch import (
        _dispatcher,
        _end,
        _investigate_log,
        _wait_for,
        _worker,
    )

    api = _CURRENT_API[0]
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    async with _worker(env, submit):
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == "fulfilled", outcome
        await _wait_for(lambda: len(seen) == 1 and api.executions[0]["phase"] == "Succeeded")
        await _end(env, outcome.workflow_id)
        history = await env.client.get_workflow_handle(outcome.workflow_id).fetch_history()
    assert rid in outcome.workflow_id
    return history.to_json_dict()


_CURRENT_API: list[Any] = []


@pytest.fixture
def routed_api(monkeypatch):
    from orchestrator.work_context.client import WorkItemClient
    from tests.test_execution_request_dispatch import DispatchFakeApi

    fake = DispatchFakeApi()
    monkeypatch.setattr(
        WorkItemClient, "_request", lambda self, method, path, payload=None: fake.request(method, path, payload)
    )
    _CURRENT_API[:] = [fake]
    yield fake
    _CURRENT_API.clear()


async def test_the_dispatched_history_replays_against_current_definitions():
    """A NondeterminismError here means today's edit would wedge an
    in-flight dispatched loop. Guard the change with `workflow.patched()`;
    do not re-record to make it pass."""
    history = WorkflowHistory.from_json("replay-dev-loop-dispatched", DISPATCHED_HISTORY.read_text(encoding="utf-8"))
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(history)


def test_the_dispatched_history_binds_before_running_and_ends_its_execution_after():
    """What the fixture must contain to be worth replaying: the marker, the
    bind as the very first command, and the advance right after the
    investigate submit. A fixture without them covers nothing new."""
    events = _events(json.loads(DISPATCHED_HISTORY.read_text(encoding="utf-8")))
    assert EXECUTION_REQUEST_PATCH in _patch_ids(events)
    scheduled = _scheduled(events)
    assert scheduled[0] == "bind_dispatched_execution"
    investigate = scheduled.index("submit_and_wait")
    assert scheduled.index("advance_dispatched_execution") > investigate
    assert scheduled.count("bind_dispatched_execution") == 1
    assert scheduled.count("advance_dispatched_execution") == 1


def test_no_older_dev_loop_history_records_the_dispatch_marker():
    """Every other recorded DevLoop history predates the dispatched path, so
    it must carry neither the marker nor either new activity — otherwise it
    was re-recorded and no longer proves that old histories replay."""
    for path in sorted(HISTORY_DIR.glob("dev_loop_*.json")):
        if path == DISPATCHED_HISTORY:
            continue
        events = _events(json.loads(path.read_text(encoding="utf-8")))
        assert EXECUTION_REQUEST_PATCH not in _patch_ids(events), path.name
        assert not {"bind_dispatched_execution", "advance_dispatched_execution"} & set(_scheduled(events)), path.name


async def test_todays_dispatched_loop_replays_its_own_history(routed_api):
    """Ties the guard to the CODE, not only to the committed recording: a
    fresh dispatched run must replay, and must record the marker."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        history = await record(env)
    assert EXECUTION_REQUEST_PATCH in _patch_ids(_events(history))
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(
        WorkflowHistory.from_json("replay-dev-loop-dispatched-fresh", history)
    )


def _regenerate() -> None:  # pragma: no cover — a maintenance entry point, not a test
    from orchestrator.work_context.client import WorkItemClient
    from tests.test_execution_request_dispatch import DispatchFakeApi

    fake = DispatchFakeApi()
    WorkItemClient._request = lambda self, method, path, payload=None: fake.request(  # type: ignore[method-assign]
        method, path, payload
    )
    _CURRENT_API[:] = [fake]

    async def main() -> None:
        async with await WorkflowEnvironment.start_time_skipping() as env:
            history = await record(env)
        DISPATCHED_HISTORY.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {DISPATCHED_HISTORY}")

    asyncio.run(main())


if __name__ == "__main__":  # pragma: no cover
    _regenerate()
