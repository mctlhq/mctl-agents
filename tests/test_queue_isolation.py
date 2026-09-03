"""A saturated execution queue must not starve the control queue (#152).

This is the criterion the queue split exists for — "a slow or failing agent
workload cannot starve approval, scheduling or reconciliation" — and until
now nothing demonstrated it. Production could not: over the 44 h baseline to
2026-09-03 the control pool never fell below 96 free slots of 100, so the
isolation was never exercised in either direction. A criterion that has never
been put under the load it describes is not met, it is untested.

So the load is staged here instead, deterministically and without spending a
cent of model quota: N slow activities on the execution queue, holding their
slots the way a two-hour `submit_and_wait` poll does, and short control
activities racing beside them.

The assertion is on the short activities COMPLETING while the execution queue
is saturated, not on wall-clock latency. Latency in a time-skipping
environment is meaningless, and "the test passed" would be satisfied by a run
that never saturated anything — so the saturation itself is waited for and
asserted before the probe is allowed to start.

Mutation that must break this file: schedule the slow activities on
`TASK_QUEUE` instead of `EXECUTION_TASK_QUEUE` (i.e. revert the #282 routing
flip). `test_a_saturated_execution_queue_does_not_starve_control` then times
out, because the slow activities take the control worker's slots — which is
precisely the starvation the split removed.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from orchestrator.temporal.constants import (
    CONTROL_MAX_CONCURRENT_ACTIVITIES,
    EXECUTION_MAX_CONCURRENT_ACTIVITIES,
    EXECUTION_TASK_QUEUE,
)

pytestmark = pytest.mark.anyio

CONTROL_QUEUE = "test-queue-isolation-control"

# More slow activities than the execution worker has slots, so the queue is
# genuinely backed up rather than merely busy. Deliberately also more than
# the control worker's slot limit: that is what makes the mutation above
# starve control rather than merely slow it, and a smaller number would let a
# reverted routing flip keep this test green.
SLOW_ACTIVITY_COUNT = CONTROL_MAX_CONCURRENT_ACTIVITIES + 20

PROBE_ACTIVITY_COUNT = 10


class _Gate:
    """Shared state between the test and the activities it runs.

    A plain object rather than module globals: pytest keeps modules alive
    across tests in a session, and a leaked `asyncio.Event` bound to a closed
    loop fails in a way that reads as a Temporal bug.
    """

    def __init__(self) -> None:
        self.released = asyncio.Event()
        self.started = 0
        self.saturated = asyncio.Event()
        self.probes_done = 0


@activity.defn(name="submit_and_wait")
async def _slow_activity(gate_key: str) -> str:
    gate = _GATES[gate_key]
    gate.started += 1
    if gate.started >= EXECUTION_MAX_CONCURRENT_ACTIVITIES:
        gate.saturated.set()
    await gate.released.wait()
    return "slow-done"


@activity.defn(name="record_execution")
async def _short_activity(gate_key: str) -> str:
    gate = _GATES[gate_key]
    gate.probes_done += 1
    return "short-done"


_GATES: dict[str, _Gate] = {}


@workflow.defn(name="SaturateExecutionQueue")
class _SaturateWorkflow:
    @workflow.run
    async def run(self, gate_key: str, count: int, queue: str) -> int:
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "submit_and_wait",
                    gate_key,
                    task_queue=queue,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                for _ in range(count)
            ]
        )
        return len(results)


@workflow.defn(name="ProbeControlQueue")
class _ProbeWorkflow:
    @workflow.run
    async def run(self, gate_key: str, count: int) -> int:
        results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "record_execution",
                    gate_key,
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                for _ in range(count)
            ]
        )
        return len(results)


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def _run_isolation_probe(client: Client, slow_queue: str) -> int:
    """Saturate `slow_queue`, then see how many control probes get through.

    Returns the number of completed probe activities. The caller decides what
    that number has to be — which is what lets the same body express both the
    property and its mutation.
    """
    gate_key = str(uuid.uuid4())
    gate = _Gate()
    _GATES[gate_key] = gate
    try:
        control = Worker(
            client,
            task_queue=CONTROL_QUEUE,
            workflows=[_SaturateWorkflow, _ProbeWorkflow],
            activities=[_slow_activity, _short_activity],
            max_concurrent_activities=CONTROL_MAX_CONCURRENT_ACTIVITIES,
        )
        execution = Worker(
            client,
            task_queue=EXECUTION_TASK_QUEUE,
            activities=[_slow_activity],
            max_concurrent_activities=EXECUTION_MAX_CONCURRENT_ACTIVITIES,
        )
        async with control, execution:
            saturator = await client.start_workflow(
                _SaturateWorkflow.run,
                args=[gate_key, SLOW_ACTIVITY_COUNT, slow_queue],
                id=f"saturate-{gate_key}",
                task_queue=CONTROL_QUEUE,
            )

            # Wait for the queue to be genuinely backed up before probing.
            # Without this the probe could run before a single slow activity
            # had taken a slot, and the test would pass having staged nothing.
            await asyncio.wait_for(gate.saturated.wait(), timeout=60)

            try:
                return await asyncio.wait_for(
                    client.execute_workflow(
                        _ProbeWorkflow.run,
                        args=[gate_key, PROBE_ACTIVITY_COUNT],
                        id=f"probe-{gate_key}",
                        task_queue=CONTROL_QUEUE,
                    ),
                    timeout=30,
                )
            except TimeoutError:
                return gate.probes_done
            finally:
                gate.released.set()
                try:
                    await asyncio.wait_for(saturator.result(), timeout=60)
                except TimeoutError:  # pragma: no cover
                    await saturator.cancel()
    finally:
        _GATES.pop(gate_key, None)


async def test_a_saturated_execution_queue_does_not_starve_control(env) -> None:
    """The criterion, staged: exec is full, control still serves everything."""
    completed = await _run_isolation_probe(env.client, EXECUTION_TASK_QUEUE)
    assert completed == PROBE_ACTIVITY_COUNT


async def test_the_same_load_on_one_queue_does_starve_control(env) -> None:
    """The control: without the split, this load is exactly the outage.

    Runs the identical staging with the slow activities routed to the control
    queue — the pre-#282 world — and asserts the probe does NOT get through.
    Without this the passing test above proves only that ten short activities
    can run, which they can on any topology whatsoever.
    """
    completed = await _run_isolation_probe(env.client, CONTROL_QUEUE)
    assert completed < PROBE_ACTIVITY_COUNT
