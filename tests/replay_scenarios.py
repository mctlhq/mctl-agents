"""Scenarios shared by the history recorder and the replay tests (#251).

Every scenario here does two jobs from one definition:

1. `tools/record_workflow_history.py` runs it once against the
   time-skipping test environment and writes the resulting event history to
   `tests/fixtures/histories/<name>.prepatch.json`.
2. `tests/test_workflow_replay.py` feeds that JSON back through temporalio's
   `Replayer` against the CURRENT workflow definitions.

Keeping both on one definition is the point. A recorder with its own copy of
the fakes would drift from the tests, and a history recorded from drifted
fakes proves nothing about the code that actually runs.

## Why replay at all, when there are already workflow tests

`WorkflowEnvironment` starts a FRESH workflow every time. It can therefore
never catch the failure `workflow.patched()` exists to prevent: an edit that
changes the commands a workflow schedules, breaking executions that are
already mid-flight. Both branches of a patch can pass under the test
environment while a real 14-day merge watch wedges on deploy.

`Replayer` is the only thing in this repo that answers "does today's code
still agree with a history recorded by yesterday's?".

## The rule about regenerating these fixtures

The `*.prepatch.json` files are recorded BEFORE a routing/ordering change and
must not be re-recorded after it. Re-recording is not a merge conflict and
not a test failure — it silently replaces the one history that exercises the
unpatched branch with one that exercises the patched branch, and every test
keeps passing while the coverage is gone.

`test_workflow_replay.py` therefore asserts on the CONTENT of the pre-patch
histories — no `exec-queue` patch marker, and no activity scheduled onto a
queue other than the workflow's own — so a regenerated fixture fails loudly
instead of passing vacuously.

## Honest limits

Two, and both matter when reading a green run:

- A generated history covers exactly the branches the scenario drove
  through. A marker on a path no scenario reaches replays green and proves
  nothing about that path.
- Replay compares the SHAPE of the command stream, not the attributes of the
  commands in it. Measured on temporalio 1.31.0: an added, removed or
  reordered activity fails; a changed `task_queue`, a changed timeout and an
  extra argument are all invisible. The capability table in
  `test_workflow_replay.py` has the details, and the consequence for #251 is
  that replay cannot verify the queue flip at all.
"""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Worker

from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef
from orchestrator.temporal.workflows.incidents import IncidentLoopWorkflow
from orchestrator.temporal.workflows.reconcile import ReconcileWorkflow, ReconcileWorkflowInput

HISTORY_DIR = Path(__file__).resolve().parent / "fixtures" / "histories"

TASK_QUEUE = "replay-record"


@dataclass(frozen=True)
class Scenario:
    """One recordable workflow execution.

    `drive` receives a started handle and is responsible for getting the
    workflow to completion — signalling it, waiting on it, whatever the
    workflow needs. It runs inside the Worker's context.
    """

    name: str
    workflows: list[type]
    build: Callable[[], tuple[list[Any], dict[str, Any]]]
    start: Callable[[Client, str], Awaitable[WorkflowHandle]]
    drive: Callable[[WorkflowHandle, dict[str, Any]], Awaitable[None]]
    # What this scenario is here to keep replayable. Printed by the recorder
    # and used as the test id, so a green run says what it covered.
    covers: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path(self) -> Path:
        return HISTORY_DIR / f"{self.name}.prepatch.json"


async def record(client: Client, scenario: Scenario) -> WorkflowHandle:
    """Run one scenario to completion and return its handle."""
    activities, state = scenario.build()
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=scenario.workflows,
        activities=activities,
    ):
        handle = await scenario.start(client, TASK_QUEUE)
        await scenario.drive(handle, state)
    return handle


# ---------------------------------------------------------------------------
# Scenario: the full dev loop
#
# The one that matters most. It reaches _run_cwft (the funnel feeding
# investigate / approve / implement / shepherd tick) and carries the
# atomic-approve, slug-scoped-implement and merge-detection markers, so it is
# the history that would wedge if a submit_and_wait command moved.
# ---------------------------------------------------------------------------
def _dev_loop_build() -> tuple[list[Any], dict[str, Any]]:
    # Imported here, not at module import time: pulling a test module in at
    # import would make this module unusable from a plain script run without
    # pytest's plugins loaded.
    from tests.test_dev_loop_workflow import _fake_activities

    activities, calls, investigate_ran = _fake_activities(released=True)
    return activities, {"calls": calls, "investigate_ran": investigate_ran}


async def _dev_loop_start(client: Client, task_queue: str) -> WorkflowHandle:
    return await client.start_workflow(
        DevLoopWorkflow.run,
        IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/1"),
        id=f"replay-dev-loop-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _dev_loop_drive(handle: WorkflowHandle, state: dict[str, Any]) -> None:
    import anyio

    # Approve only after investigate has actually run, so the recorded
    # history has the real command order rather than a race's.
    with anyio.fail_after(30):
        await state["investigate_ran"].wait()
    await handle.signal(DevLoopWorkflow.approve)
    await handle.result()


# ---------------------------------------------------------------------------
# Scenario: the incident loop
# ---------------------------------------------------------------------------
def _incidents_build() -> tuple[list[Any], dict[str, Any]]:
    from tests.test_incident_loop_workflow import (
        PINNED,
        _capturing_record,
        _capturing_resolve,
        _capturing_submit,
    )

    submit, seen = _capturing_submit()
    resolve, _ = _capturing_resolve(PINNED)
    record_activity, _ = _capturing_record()
    return [submit, resolve, record_activity], {"submits": seen}


async def _incidents_start(client: Client, task_queue: str) -> WorkflowHandle:
    return await client.start_workflow(
        IncidentLoopWorkflow.run,
        id=f"replay-incidents-{uuid.uuid4()}",
        task_queue=task_queue,
    )


# ---------------------------------------------------------------------------
# Scenario: one reconcile tick that reaches the apply submit
#
# `_apply` only submits when discovery produced projections, so the fake must
# return one — an empty sweep records no submit_and_wait at all and would be
# a history that cannot detect the thing being guarded.
# ---------------------------------------------------------------------------
def _reconcile_build() -> tuple[list[Any], dict[str, Any]]:
    from orchestrator.temporal.activities.discovery import ProposalProjection
    from tests.test_reconcile_workflow import _fake_activities

    projection = ProposalProjection(
        service="mctl-web",
        slug="issue-10-replay",
        current_status="implemented",
        projected_status="merged",
        pr_url="https://github.com/mctlhq/mctl-web/pull/10",
        notes="PR merged",
    )
    activities, received = _fake_activities(
        active_ids=["dev-loop-mctlhq-mctl-web-10"],
        projections=[projection],
    )
    return activities, received


async def _reconcile_start(client: Client, task_queue: str) -> WorkflowHandle:
    return await client.start_workflow(
        ReconcileWorkflow.run,
        ReconcileWorkflowInput(state_dir_path="/tmp/state"),
        id=f"replay-reconcile-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _await_result(handle: WorkflowHandle, state: dict[str, Any]) -> None:
    await handle.result()


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="dev_loop_full",
        workflows=[DevLoopWorkflow],
        build=_dev_loop_build,
        start=_dev_loop_start,
        drive=_dev_loop_drive,
        covers=(
            "investigate -> approve -> implement -> merge detection; reaches "
            "_run_cwft three times and records the atomic-approve, "
            "slug-scoped-implement and merge-detection markers"
        ),
    ),
    Scenario(
        name="incident_loop",
        workflows=[IncidentLoopWorkflow],
        build=_incidents_build,
        start=_incidents_start,
        drive=_await_result,
        covers="one responder submit under incident-registry-and-record",
    ),
    Scenario(
        name="reconcile_apply",
        workflows=[ReconcileWorkflow],
        build=_reconcile_build,
        start=_reconcile_start,
        drive=_await_result,
        covers="a sweep with drift, so the reconcile-apply submit is recorded",
    ),
)


def scenario_by_name(name: str) -> Scenario:
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise KeyError(f"unknown replay scenario: {name!r}")
