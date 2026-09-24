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

A resume delivered onto a live loop (#461 gap 1) adds an Update, a second
bind and a second advance, from a task the Update starts, under
`workflow.patched("execution-request-resume")`. `dev_loop_resumed.json` is a
real history of it (claim, start, fulfil, bind, investigate, advance, park;
the resume's Update, fulfil, bind; the re-approval, the resumed execution's
advance, and the rest of the loop to its end), recorded from
`record_resumed()`. No other fixture may carry that marker.

A dispatched loop whose bind is refused although its execution was minted
(the item is about another issue, or the advance to Running is refused) ends
that execution `Failed` under `workflow.patched("execution-request-stranded")`.
The path ends the loop in the same workflow task as the refusal, so it is
guarded by fresh recordings rather than a fixture: today's recording records
the marker and replays, and one recorded with the gate answering False (the
released loop's shape) replays against today's code too.

Since #461 option A the dispatcher reaches the issue's one loop
(`dev-loop-<owner>-<repo>-<n>`) with Update-with-Start, and a dispatched
loop binds its execution under `<loop>#<request id>` behind
`workflow.patched("issue-keyed-dispatch")`. `dev_loop_dispatched.json` and
`dev_loop_resumed.json` were recorded before that (a `dev-loop-xr_*` loop
started with a plain start, bound under its bare id) and are kept exactly as
they are: they are the in-flight shape a worker upgrade must still replay.
`dev_loop_issue_keyed.json` is today's shape, recorded from
`record_issue_keyed()`: the start request's Update-with-Start, the intake
poller joining the same loop, a second `start` refused, a resume delivered
and its update id re-sent, the re-approval, and the rest of the loop.

Regenerating the fixtures (only when a path's command shape is MEANT to
change, and never to turn a red run green): run
`uv run python -m tests.test_execution_request_replay [issue_keyed]`, which
records a fresh history with `record_issue_keyed()` and overwrites that file.
The two pre-option-A fixtures have no recorder: today's code cannot record
them again, and they must never be replaced.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from orchestrator.temporal.workflows import dev_loop
from orchestrator.temporal.workflows.dev_loop import (
    EXECUTION_REQUEST_PATCH,
    EXECUTION_REQUEST_RESUME_PATCH,
    EXECUTION_REQUEST_STRANDED_PATCH,
    ISSUE_KEYED_DISPATCH_PATCH,
    DevLoopWorkflow,
    IssueRef,
)

HISTORY_DIR = Path(__file__).resolve().parent / "fixtures" / "histories"
DISPATCHED_HISTORY = HISTORY_DIR / "dev_loop_dispatched.json"
RESUMED_HISTORY = HISTORY_DIR / "dev_loop_resumed.json"
ISSUE_KEYED_HISTORY = HISTORY_DIR / "dev_loop_issue_keyed.json"
#: Recorded before #461 option A; today's code cannot record them again.
PRE_ISSUE_KEYED_HISTORIES = (DISPATCHED_HISTORY, RESUMED_HISTORY)

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
    from orchestrator.temporal.issue_ref import workflow_id_for
    from tests.test_execution_request_dispatch import (
        _dispatcher,
        _end,
        _investigate_log,
        _wait_for,
        _worker,
    )
    from tests.test_work_context_resume_acceptance import URL

    api = _CURRENT_API[0]
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    async with _worker(env, submit):
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == "fulfilled", outcome
        await _wait_for(lambda: len(seen) == 1 and api.executions[0]["phase"] == "Succeeded")
        await _end(env, outcome.workflow_id)
        history = await env.client.get_workflow_handle(outcome.workflow_id).fetch_history()
    assert outcome.workflow_id == workflow_id_for(URL) and outcome.engine_ref == f"{outcome.workflow_id}#{rid}"
    return history.to_json_dict()


async def record_resumed(env: WorkflowEnvironment) -> dict[str, Any]:
    """A dispatched loop parked at approval takes a resume delivered onto
    it, binds the resumed execution, is re-approved by the resuming actor
    and runs to its end, as a history dict."""
    from tests.test_execution_request_dispatch import _dispatcher, _investigate_log, _wait_for, _worker

    api = _CURRENT_API[0]
    submit, _ = _investigate_log()
    api.create_request("start")
    async with _worker(env, submit):
        started = await _dispatcher(env).dispatch_once()
        assert started.action == "fulfilled", started
        await _wait_for(lambda: bool(api.executions) and api.executions[0]["phase"] == "Succeeded")
        rid = api.create_request("resume")
        resumed = await _dispatcher(env).dispatch_once()
        assert resumed.action == "fulfilled" and resumed.engine_ref.endswith(f"#{rid}"), resumed
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        handle = env.client.get_workflow_handle(started.workflow_id)
        await handle.signal(DevLoopWorkflow.approve, {"approver": "alice"})
        await _wait_for(lambda: api.executions[1]["phase"] == "Succeeded")
        # The rest of the loop (merge watch, deploy observation, incident
        # watch) waits on timers and retry backoffs, which the test server
        # skips only while the test sleeps.
        await env.sleep(timedelta(days=30))
        await handle.result()
        history = await handle.fetch_history()
    return history.to_json_dict()


async def record_issue_keyed(env: WorkflowEnvironment) -> dict[str, Any]:
    """Option A end to end, as a history dict: a `start` request starts the
    issue's loop with Update-with-Start; the intake poller's own start of the
    same issue joins that run; a second `start` is refused `loop-active`
    (a refused Update writes nothing); a resume is delivered and its update id
    re-sent (Temporal's registry answers); the resuming actor re-approves and
    the loop runs to its end."""
    from orchestrator.temporal import dispatcher as dx
    from orchestrator.temporal.issue_ref import workflow_id_for
    from orchestrator.temporal.start import start_dev_loop_workflow
    from orchestrator.temporal.workflows.dev_loop import ResumeDelivery
    from tests.test_execution_request_dispatch import _dispatcher, _investigate_log, _wait_for, _worker
    from tests.test_work_context_resume_acceptance import URL, WID

    api = _CURRENT_API[0]
    submit, _ = _investigate_log()
    loop = workflow_id_for(URL)
    first = api.create_request("start")
    async with _worker(env, submit):
        started = await _dispatcher(env).dispatch_once()
        assert started.action == "fulfilled" and started.workflow_id == loop, started
        run_id = (await env.client.get_workflow_handle(loop).describe()).run_id
        joined = await start_dev_loop_workflow(URL, client=env.client)
        assert joined.id == loop and (await joined.describe()).run_id == run_id
        await _wait_for(lambda: bool(api.executions) and api.executions[0]["phase"] == "Succeeded")

        second = api.create_request("start")
        refused = await _dispatcher(env).dispatch_once()
        assert refused.action == "rejected" and api.request_state(second)["reason"] == "loop_active", refused

        rid = api.create_request("resume")
        resumed = await _dispatcher(env).dispatch_once()
        assert resumed.action == "fulfilled" and resumed.engine_ref == f"{loop}#{rid}", resumed
        await _wait_for(lambda: len(api.executions) == 2 and api.executions[1]["phase"] == "Running")
        again = await dx.TemporalClientPort(env.client).deliver(
            IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid),
            ResumeDelivery(
                execution_request_id=rid, work_item_id=WID, surface="api", actor_kind="human", actor_id="user:alice"
            ),
        )
        assert again.verdict == dx.DELIVERED
        handle = env.client.get_workflow_handle(loop)
        await handle.signal(DevLoopWorkflow.approve, {"approver": "alice"})
        await _wait_for(lambda: api.executions[1]["phase"] == "Succeeded")
        await env.sleep(timedelta(days=30))
        await handle.result()
        assert (await handle.describe()).run_id == run_id
        history = await handle.fetch_history()
    assert [e["engine_ref"] for e in api.executions] == [f"{loop}#{first}", f"{loop}#{rid}"]
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
        if path in (DISPATCHED_HISTORY, RESUMED_HISTORY, ISSUE_KEYED_HISTORY):
            continue
        events = _events(json.loads(path.read_text(encoding="utf-8")))
        assert EXECUTION_REQUEST_PATCH not in _patch_ids(events), path.name
        assert not {"bind_dispatched_execution", "advance_dispatched_execution"} & set(_scheduled(events)), path.name


async def test_todays_dispatched_loop_replays_its_own_history(routed_api):
    """Ties the guard to the CODE, not only to the committed recording: a
    fresh dispatched run must replay, and must record the markers."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        history = await record(env)
    assert {EXECUTION_REQUEST_PATCH, ISSUE_KEYED_DISPATCH_PATCH} <= _patch_ids(_events(history))
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(
        WorkflowHistory.from_json("replay-dev-loop-dispatched-fresh", history)
    )


def _accepted_update_ids(events: list[dict]) -> list[str]:
    return [
        e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["meta"]["updateId"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"
    ]


async def test_the_resumed_history_replays_against_current_definitions():
    """A NondeterminismError here means today's edit would wedge a loop that
    took a delivered resume. Guard the change with `workflow.patched()`; do
    not re-record to make it pass."""
    history = WorkflowHistory.from_json("replay-dev-loop-resumed", RESUMED_HISTORY.read_text(encoding="utf-8"))
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(history)


def test_the_resumed_history_accepts_the_update_and_binds_and_ends_the_resumed_execution():
    """What the resume fixture must contain to be worth replaying: the
    marker, one accepted Update, and a second bind and a second advance
    after it (the resumed execution), besides the dispatched path's own."""
    events = _events(json.loads(RESUMED_HISTORY.read_text(encoding="utf-8")))
    assert {EXECUTION_REQUEST_PATCH, EXECUTION_REQUEST_RESUME_PATCH} <= _patch_ids(events)
    assert len(_accepted_update_ids(events)) == 1 and _accepted_update_ids(events)[0].startswith("xr_")
    scheduled = _scheduled(events)
    assert scheduled.count("bind_dispatched_execution") == 2
    assert scheduled.count("advance_dispatched_execution") == 2
    accepted_at = next(
        i for i, e in enumerate(events) if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"
    )
    after = _scheduled(events[accepted_at:])
    assert after.index("bind_dispatched_execution") < after.index("advance_dispatched_execution")
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"


def test_no_history_without_a_delivery_records_the_resume_marker():
    """The resume marker is recorded where a delivery starts, so no other
    history — the dispatched one included — may carry it."""
    for path in sorted(HISTORY_DIR.glob("*.json")):
        if path in (RESUMED_HISTORY, ISSUE_KEYED_HISTORY):
            continue
        events = _events(json.loads(path.read_text(encoding="utf-8")))
        assert EXECUTION_REQUEST_RESUME_PATCH not in _patch_ids(events), path.name
        assert _accepted_update_ids(events) == [], path.name


# -- #461 option A: the issue's one loop, reached by Update-with-Start -------


async def test_the_issue_keyed_history_replays_against_current_definitions():
    """A NondeterminismError here means today's edit would wedge a loop the
    dispatcher reached by Update-with-Start. Guard the change with
    `workflow.patched()`; do not re-record to make it pass."""
    history = WorkflowHistory.from_json("replay-dev-loop-issue-keyed", ISSUE_KEYED_HISTORY.read_text(encoding="utf-8"))
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(history)


def test_the_issue_keyed_history_is_one_run_with_one_update_per_request():
    """What the fixture must contain to be worth replaying: every marker of
    the path, exactly two accepted Updates (the start request that came with
    the start, then the resume; neither the refused second start nor the
    re-sent resume wrote one), a bind and an advance for each execution, and
    one run from start to completion."""
    events = _events(json.loads(ISSUE_KEYED_HISTORY.read_text(encoding="utf-8")))
    assert {EXECUTION_REQUEST_PATCH, ISSUE_KEYED_DISPATCH_PATCH, EXECUTION_REQUEST_RESUME_PATCH} <= _patch_ids(events)
    accepted = _accepted_update_ids(events)
    assert len(accepted) == 2 and all(rid.startswith("xr_") for rid in accepted) and accepted[0] != accepted[1]
    started = events[0]["workflowExecutionStartedEventAttributes"]
    assert started["input"]["payloads"], "the start input carries the request"
    start_input = json.loads(base64.b64decode(started["input"]["payloads"][0]["data"]))
    assert start_input["execution_request_id"] == accepted[0]
    scheduled = _scheduled(events)
    assert scheduled.count("bind_dispatched_execution") == 2
    assert scheduled.count("advance_dispatched_execution") == 2
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"
    assert not [e for e in events if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW"]


def test_no_pre_option_a_history_records_the_issue_keyed_marker():
    """The pre-option-A fixtures prove that loops already in flight at the
    upgrade replay; carrying the marker would mean they were re-recorded."""
    for path in sorted(HISTORY_DIR.glob("*.json")):
        if path == ISSUE_KEYED_HISTORY:
            continue
        events = _events(json.loads(path.read_text(encoding="utf-8")))
        assert ISSUE_KEYED_DISPATCH_PATCH not in _patch_ids(events), path.name


async def test_todays_issue_keyed_loop_replays_its_own_history(routed_api):
    """Ties the option-A guard to the CODE: a fresh recording must replay."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        history = await record_issue_keyed(env)
    assert ISSUE_KEYED_DISPATCH_PATCH in _patch_ids(_events(history))
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(
        WorkflowHistory.from_json("replay-dev-loop-issue-keyed-fresh", history)
    )


async def test_todays_resumed_loop_replays_its_own_history(routed_api):
    """Ties the resume guard to the CODE: a fresh recording must replay and
    must record the marker where the delivery starts."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        history = await record_resumed(env)
    assert EXECUTION_REQUEST_RESUME_PATCH in _patch_ids(_events(history))
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(
        WorkflowHistory.from_json("replay-dev-loop-resumed-fresh", history)
    )


def _regenerate(names: list[str]) -> None:  # pragma: no cover — a maintenance entry point, not a test
    """Re-record the named fixtures (today only `issue_keyed`; every one when
    none is named), each against a fresh fake mctl-api. The pre-option-A
    fixtures have no recorder any more: today's code cannot produce them."""
    from orchestrator.work_context.client import WorkItemClient
    from tests.test_execution_request_dispatch import DispatchFakeApi

    recorders = {"issue_keyed": (ISSUE_KEYED_HISTORY, record_issue_keyed)}

    async def main() -> None:
        for name in names or list(recorders):
            target, recorder = recorders[name]
            fake = DispatchFakeApi()
            WorkItemClient._request = (  # type: ignore[method-assign]
                lambda self, method, route, payload=None, fake=fake: fake.request(method, route, payload)
            )
            _CURRENT_API[:] = [fake]
            async with await WorkflowEnvironment.start_time_skipping() as env:
                history = await recorder(env)
            target.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"wrote {target}")

    asyncio.run(main())


if __name__ == "__main__":  # pragma: no cover
    import sys

    _regenerate(sys.argv[1:])


# -- a dispatched loop whose bind is refused, with its execution minted --------


async def record_stranded(env: WorkflowEnvironment, workflow: type = DevLoopWorkflow) -> dict[str, Any]:
    """A dispatched loop whose item turns out to be about another issue, as
    a history dict: the fulfil minted its `we_` under this loop's engine ref,
    then the item was re-pointed, and the bind refuses it."""
    from temporalio.worker import Worker as TemporalWorker

    from orchestrator.temporal import dispatcher as dx
    from orchestrator.temporal.constants import TASK_QUEUE
    from orchestrator.temporal.issue_ref import workflow_id_for
    from orchestrator.work_context.client import WorkItemClient
    from tests.test_execution_request_dispatch import FakeTemporal, _investigate_log, _loop_activities
    from tests.test_work_context_resume_acceptance import URL, WID

    api = _CURRENT_API[0]
    submit, _ = _investigate_log()
    rid = api.create_request("start")
    assert (await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()).action == "fulfilled"
    api.external_key = "https://github.com/mctlhq/mctl-telegram/issues/9999"
    issue = IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid)
    async with TemporalWorker(
        env.client, task_queue=TASK_QUEUE, workflows=[workflow], activities=_loop_activities(submit)
    ):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run, issue, id=workflow_id_for(issue.issue_url), task_queue=TASK_QUEUE
        )
        result = await handle.result()
        history = await handle.fetch_history()
    assert "work-item-mismatch" in result.ended
    return history.to_json_dict()


async def test_a_stranded_dispatched_loop_records_the_marker_ends_its_execution_and_replays(routed_api):
    """A fresh recording of the stranded path records its own marker, ends
    the minted execution with one advance, and replays."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        history = await record_stranded(env)
    events = _events(history)
    assert EXECUTION_REQUEST_STRANDED_PATCH in _patch_ids(events)
    assert _scheduled(events) == ["bind_dispatched_execution", "advance_dispatched_execution"]
    assert [e["phase"] for e in routed_api.executions] == ["Failed"]
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(
        WorkflowHistory.from_json("replay-dev-loop-stranded-fresh", history)
    )


@workflow.defn(name="DevLoopWorkflow", sandboxed=False)
class _ReleasedStrandedLoop(DevLoopWorkflow):
    """Today's loop with the stranded gate answering False: the shape the
    released loop recorded (the refused bind ends the loop, no advance)."""

    async def _bind_dispatched_execution(self, issue: IssueRef) -> tuple[Any, str]:
        real = dev_loop.workflow.patched

        def patched(patch_id: str) -> bool:
            return False if patch_id == EXECUTION_REQUEST_STRANDED_PATCH else real(patch_id)

        dev_loop.workflow.patched = patched  # type: ignore[assignment]
        try:
            return await super()._bind_dispatched_execution(issue)
        finally:
            dev_loop.workflow.patched = real  # type: ignore[assignment]

    @workflow.run
    async def run(self, issue: IssueRef) -> Any:
        return await super().run(issue)


async def test_a_stranded_history_recorded_before_the_patch_replays_unchanged(routed_api):
    """The released loop (1.54.0) ended on the refused bind without ending
    the execution. Its history, even one whose bind answer already names the
    stranded execution (a worker rollout that ran the new activity under the
    old workflow code), must replay: the gate answers False without the
    marker, so no advance is expected."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        history = await record_stranded(env, _ReleasedStrandedLoop)
    events = _events(history)
    assert EXECUTION_REQUEST_STRANDED_PATCH not in _patch_ids(events)
    assert _scheduled(events) == ["bind_dispatched_execution"]
    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(
        WorkflowHistory.from_json("replay-dev-loop-stranded-released", history)
    )


def test_no_committed_history_records_the_stranded_marker():
    for path in sorted(HISTORY_DIR.glob("*.json")):
        events = _events(json.loads(path.read_text(encoding="utf-8")))
        assert EXECUTION_REQUEST_STRANDED_PATCH not in _patch_ids(events), path.name
