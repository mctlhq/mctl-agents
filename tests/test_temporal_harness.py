"""The multi-queue test Worker in tests/temporal_harness.py (#251, #395).

The harness is what every workflow test runs on, so a defect in it is a
defect in the evidence the whole suite produces. Two properties matter:
it mirrors `worker_plans("all")` — one SDK worker per queue, the
implementation one carrying the admission limit — and no start or stop
path can leave an SDK worker polling after the `async with` block ends
(agy P2/P3 on #397).

`temporalio.worker.Worker` dials on construction, so the SDK class is
replaced with a recording fake; what is asserted is what the harness asks
of it, which is exactly the routing decision worth pinning.
"""
from __future__ import annotations

from typing import Any, ClassVar

import pytest
from temporalio import activity

from orchestrator.temporal.constants import (
    EXECUTION_TASK_QUEUE,
    IMPLEMENTATION_TASK_QUEUE,
    TASK_QUEUE,
    implementation_max_concurrent_activities,
)
from tests import temporal_harness


@activity.defn(name="submit_and_wait")
async def _fake_submit_and_wait() -> None:
    return None


@activity.defn(name="find_proposal_slug")
async def _fake_short() -> None:
    return None


class _RecordingWorker:
    """Stands in for temporalio.worker.Worker: records kwargs, scripted lifecycle."""

    instances: ClassVar[list[_RecordingWorker]] = []
    fail_on_enter: ClassVar[set[str]] = set()
    fail_on_exit: ClassVar[set[str]] = set()

    def __init__(self, client: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.task_queue = kwargs["task_queue"]
        self.entered = False
        self.exited = False
        _RecordingWorker.instances.append(self)

    async def __aenter__(self) -> _RecordingWorker:
        if self.task_queue in _RecordingWorker.fail_on_enter:
            raise RuntimeError(f"cannot start {self.task_queue}")
        self.entered = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.exited = True
        if self.task_queue in _RecordingWorker.fail_on_exit:
            raise RuntimeError(f"cannot stop {self.task_queue}")


@pytest.fixture
def recording(monkeypatch):
    _RecordingWorker.instances = []
    _RecordingWorker.fail_on_enter = set()
    _RecordingWorker.fail_on_exit = set()
    monkeypatch.setattr(temporal_harness, "_TemporalWorker", _RecordingWorker)
    return _RecordingWorker


def _by_queue(recording) -> dict[str, _RecordingWorker]:
    return {w.task_queue: w for w in recording.instances}


def test_one_sdk_worker_per_queue_when_submit_and_wait_is_registered(recording):
    temporal_harness.Worker(
        object(), task_queue=TASK_QUEUE, workflows=[], activities=[_fake_short, _fake_submit_and_wait]
    )

    workers = _by_queue(recording)
    assert set(workers) == {TASK_QUEUE, EXECUTION_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE}
    # Only the routed activity on the long queues, as production registers it.
    assert workers[EXECUTION_TASK_QUEUE].kwargs["activities"] == [_fake_submit_and_wait]
    assert workers[IMPLEMENTATION_TASK_QUEUE].kwargs["activities"] == [_fake_submit_and_wait]
    assert "activities" in workers[TASK_QUEUE].kwargs
    assert len(workers[TASK_QUEUE].kwargs["activities"]) == 2


def test_no_long_queue_workers_without_submit_and_wait(recording):
    """A test that never routes must not pay for pollers it cannot use."""
    temporal_harness.Worker(object(), task_queue=TASK_QUEUE, workflows=[], activities=[_fake_short])

    assert set(_by_queue(recording)) == {TASK_QUEUE}


def test_the_implementation_worker_carries_the_admission_limit(recording):
    temporal_harness.Worker(object(), task_queue=TASK_QUEUE, activities=[_fake_submit_and_wait])

    workers = _by_queue(recording)
    assert (
        workers[IMPLEMENTATION_TASK_QUEUE].kwargs["max_concurrent_activities"]
        == implementation_max_concurrent_activities()
    )
    # And ONLY that one: exec and control are unbounded in tests, as under
    # `--role all`.
    assert "max_concurrent_activities" not in workers[EXECUTION_TASK_QUEUE].kwargs
    assert "max_concurrent_activities" not in workers[TASK_QUEUE].kwargs


def test_a_caller_may_override_the_limit_without_a_duplicate_keyword(recording):
    """The limit is a default, not a second value for the same keyword.

    `**kwargs` already forwards max_concurrent_activities to the control
    and execution workers; passing it explicitly beside the default raised
    `TypeError: got multiple values for keyword argument` for the
    implementation one (agy P2 on #397).
    """
    temporal_harness.Worker(
        object(), task_queue=TASK_QUEUE, activities=[_fake_submit_and_wait], max_concurrent_activities=1
    )

    for worker in recording.instances:
        assert worker.kwargs["max_concurrent_activities"] == 1


@pytest.mark.anyio
async def test_every_worker_starts_and_stops_in_order(recording):
    harness = temporal_harness.Worker(object(), task_queue=TASK_QUEUE, activities=[_fake_submit_and_wait])

    async with harness:
        assert all(w.entered for w in recording.instances)
        assert not any(w.exited for w in recording.instances)

    assert all(w.exited for w in recording.instances)


@pytest.mark.anyio
async def test_a_failed_start_tears_down_what_already_started(recording):
    """`async with` never binds on a raising __aenter__, so the harness has
    to unwind itself — otherwise the started workers keep polling and some
    later test hangs on a task a ghost worker took."""
    recording.fail_on_enter = {IMPLEMENTATION_TASK_QUEUE}
    harness = temporal_harness.Worker(object(), task_queue=TASK_QUEUE, activities=[_fake_submit_and_wait])

    with pytest.raises(RuntimeError, match="cannot start"):
        async with harness:
            pytest.fail("the block must not run")

    workers = _by_queue(recording)
    assert workers[TASK_QUEUE].exited
    assert workers[EXECUTION_TASK_QUEUE].exited
    assert not workers[IMPLEMENTATION_TASK_QUEUE].entered


@pytest.mark.anyio
async def test_the_unwind_stops_every_started_worker_even_if_one_refuses(recording):
    """A teardown error in the unwind must not abandon the rest (agy P3 on
    #397): exec refusing to stop is no reason to leave control polling."""
    recording.fail_on_enter = {IMPLEMENTATION_TASK_QUEUE}
    recording.fail_on_exit = {EXECUTION_TASK_QUEUE}
    harness = temporal_harness.Worker(object(), task_queue=TASK_QUEUE, activities=[_fake_submit_and_wait])

    with pytest.raises(RuntimeError, match="cannot start") as excinfo:
        async with harness:
            pytest.fail("the block must not run")

    workers = _by_queue(recording)
    assert workers[TASK_QUEUE].exited, "control was abandoned after exec refused to stop"
    assert workers[EXECUTION_TASK_QUEUE].exited
    # The startup error is the one reported; the teardown error rides along.
    assert "cannot stop" in str(excinfo.value.__cause__)


@pytest.mark.anyio
async def test_exit_stops_every_worker_even_if_one_raises(recording):
    recording.fail_on_exit = {EXECUTION_TASK_QUEUE}
    harness = temporal_harness.Worker(object(), task_queue=TASK_QUEUE, activities=[_fake_submit_and_wait])

    with pytest.raises(RuntimeError, match="cannot stop"):
        async with harness:
            pass

    assert all(w.exited for w in recording.instances)
