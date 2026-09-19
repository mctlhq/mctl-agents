"""Replay guard for ImplementSweepWorkflow / SweptImplementWorkflow (#412).

Deliberately NOT folded into `tests/replay_scenarios.py`'s `SCENARIOS`: that
shared machinery — and every generic test parametrized over it in
`test_workflow_replay.py` — is built around the #251/#395 exec-queue /
implement-queue MIGRATION. A `SCENARIOS` entry is expected to record a
"prepatch" history (no `exec-queue` marker, no activity routed off the
workflow's own queue) and a "patched" one (the marker present, and every
`submit_and_wait` on `EXECUTION_TASK_QUEUE`), and every scenario is required
to reach `submit_and_wait` directly in its OWN recorded history.

None of that describes this workflow, by design rather than by omission
(see design.md's Migrations note): it is a brand-new type with no unpatched
branch to guard, its own history never calls `submit_and_wait` at all (that
happens one level down, in the `SweptImplementWorkflow` child it starts and
abandons), and the child routes to the admission queue
(`IMPLEMENTATION_TASK_QUEUE`) unconditionally — there is no `exec-queue`
marker to find and no `EXECUTION_TASK_QUEUE` to land on. Forcing an entry
into `SCENARIOS` would fail two of its generic assertions for reasons that
are true of this workflow by construction, not defects in it.

So this file does the part of #412's task 10 that generalises: each
workflow type gets its own recorded history (`implement_sweep.json` for the
parent, `swept_implement.json` for the child), replayed against today's
definitions on every run — the same nondeterminism guarantee
`test_workflow_replay.py` gives dev_loop/reconcile/incidents, applied
directly rather than through machinery that assumes a migration this
workflow never had.

Regenerating the fixtures (only if this workflow's shape changes in a way
that is meant to, and never merely to make a red run green): run the
workflow via `WorkflowEnvironment.start_time_skipping()` against the fakes
in this file, `fetch_history()` on both the parent handle and the child
handle (id `implement-sweep-mctl-web-issue-10-replay`), and write
`history.to_json_dict()` with `json.dumps(..., indent=2, sort_keys=True)`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from orchestrator.temporal.workflows.implement_sweep import (
    ImplementSweepWorkflow,
    SweptImplementWorkflow,
)

HISTORY_DIR = Path(__file__).resolve().parent / "fixtures" / "histories"
PARENT_HISTORY = HISTORY_DIR / "implement_sweep.json"
CHILD_HISTORY = HISTORY_DIR / "swept_implement.json"

pytestmark = pytest.mark.anyio


def _events(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["events"]


def _scheduled_activity_names(events: list[dict]) -> set[str]:
    return {
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in events
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
    }


async def test_the_parent_history_replays_against_current_definitions():
    """A NondeterminismError here means today's edit would wedge an
    in-flight tick — the failure `workflow.patched()` exists to prevent
    elsewhere; this workflow is new enough to need no marker at all, so the
    only guard against breaking a live tick is this replay."""
    history = WorkflowHistory.from_json(
        "replay-implement-sweep", PARENT_HISTORY.read_text(encoding="utf-8")
    )
    await Replayer(workflows=[ImplementSweepWorkflow]).replay_workflow(history)


async def test_the_child_history_replays_against_current_definitions():
    """Same guarantee for the child a live tick may have already started and
    abandoned — its own execution keeps running under `ParentClosePolicy.
    ABANDON` long after the parent tick that started it has returned."""
    history = WorkflowHistory.from_json(
        "replay-swept-implement", CHILD_HISTORY.read_text(encoding="utf-8")
    )
    await Replayer(workflows=[SweptImplementWorkflow]).replay_workflow(history)


def test_the_parent_history_reaches_find_stranded_accepted():
    """Guards against a vacuous fixture: the recorded tick actually ran the
    visibility query and the stranding scan, not just the trivial no-op
    path."""
    names = _scheduled_activity_names(_events(PARENT_HISTORY))
    assert {"list_active_dev_loop_ids", "find_stranded_accepted"} <= names


def test_the_child_history_reaches_submit_and_wait():
    """Guards against a vacuous fixture: the recorded child actually
    submitted the implement operation, the one thing it exists to do."""
    names = _scheduled_activity_names(_events(CHILD_HISTORY))
    assert "submit_and_wait" in names
