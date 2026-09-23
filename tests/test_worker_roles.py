"""Worker role selection — the first slice of the queue split (ADR-008, #152).

Nothing routes to the execution queue yet: the flip is a later step behind
`workflow.patched("exec-queue")`, because a worker has to be POLLING a
queue before a workflow may schedule onto it. What these tests pin is the
half that ships now — that `--role all` is byte-for-byte the old
behaviour, and that the two new roles register the right things on the
right queues.

These assert on `worker_plans`, the pure function that decides the layout,
rather than on a constructed `Worker`. That is not a convenience: a real
Worker insists on a live bridge client and dials on construction, so the
routing decision is only unit-testable once it is separated from the
object that acts on it.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import signal
from unittest import mock
from unittest.mock import MagicMock

import pytest

from orchestrator.temporal import worker as worker_module
from orchestrator.temporal.constants import (
    CONTROL_MAX_CONCURRENT_ACTIVITIES,
    CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS,
    EXECUTION_MAX_CONCURRENT_ACTIVITIES,
    EXECUTION_TASK_QUEUE,
    IMPLEMENTATION_TASK_QUEUE,
    METRICS_PORT,
    TASK_QUEUE,
    implementation_max_concurrent_activities,
)
from orchestrator.temporal.worker import (
    owns_schedules,
    run_until_signalled,
    telemetry_config,
    worker_plans,
)


@pytest.fixture
def visibility():
    stub = MagicMock()
    stub.list_active_dev_loop_ids = _named_activity("list_active_dev_loop_ids")
    # Named too, not left as a bare MagicMock attribute: `activity_names`
    # reads `__temporal_activity_definition.name`, and on a plain MagicMock
    # that is another MagicMock — so an activity this fixture does not name
    # can never be asserted on, and a dropped registration for it is
    # invisible to this whole suite (review P3 on #412).
    stub.count_swept_prestart_failures = _named_activity("count_swept_prestart_failures")
    return stub


def _named_activity(name):
    from temporalio import activity

    @activity.defn(name=name)
    async def _fn() -> None:
        return None

    return _fn


def test_all_keeps_the_original_control_queue_shape(visibility):
    """`all` must still be today's worker — step 1 is a no-op in production."""
    control = next(p for p in worker_plans("all", visibility) if p.task_queue == TASK_QUEUE)

    assert "submit_and_wait" in control.activity_names
    assert "find_proposal_slug" in control.activity_names
    assert control.max_concurrent_activities is None


def test_the_implement_sweep_registers_on_the_control_queue_only(visibility):
    """The sweep (#412) is a control-queue workflow like the other three —
    it services no long Argo poll itself and must not land on either split
    queue, which register no workflows at all."""
    from orchestrator.temporal.workflows.implement_sweep import (
        ImplementSweepWorkflow,
        SweptImplementWorkflow,
    )

    control = next(p for p in worker_plans("all", visibility) if p.task_queue == TASK_QUEUE)
    assert ImplementSweepWorkflow in control.workflows
    assert SweptImplementWorkflow in control.workflows
    assert "find_stranded_accepted" in control.activity_names
    # Both visibility activities are scheduled by STRING name from
    # ImplementSweepWorkflow, so a dropped registration is not a type error
    # anywhere — the tick just fails its budget query every 15 minutes.
    assert "count_swept_prestart_failures" in control.activity_names
    assert "list_active_dev_loop_ids" in control.activity_names

    for role in ("execution", "implementation"):
        for plan in worker_plans(role, visibility):
            assert ImplementSweepWorkflow not in plan.workflows
            assert SweptImplementWorkflow not in plan.workflows


def test_implement_sweep_tunables_are_read_from_the_environment(monkeypatch):
    """Same `_int_env` rule IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES follows
    (mctl-agents#412): env override, default, refusal on a bad value."""
    from orchestrator.temporal.constants import (
        implement_sweep_grace_minutes,
        implement_sweep_max_submits,
    )

    monkeypatch.delenv("IMPLEMENT_SWEEP_GRACE_MINUTES", raising=False)
    monkeypatch.delenv("IMPLEMENT_SWEEP_MAX_SUBMITS", raising=False)
    assert implement_sweep_grace_minutes() == 20
    assert implement_sweep_max_submits() == 5

    monkeypatch.setenv("IMPLEMENT_SWEEP_GRACE_MINUTES", "30")
    monkeypatch.setenv("IMPLEMENT_SWEEP_MAX_SUBMITS", "1")
    assert implement_sweep_grace_minutes() == 30
    assert implement_sweep_max_submits() == 1


@pytest.mark.parametrize("bad", ["0", "-1", "three", "2.5"])
@pytest.mark.parametrize(
    "env_var,fn_name",
    [
        ("IMPLEMENT_SWEEP_GRACE_MINUTES", "implement_sweep_grace_minutes"),
        ("IMPLEMENT_SWEEP_MAX_SUBMITS", "implement_sweep_max_submits"),
    ],
)
def test_a_malformed_implement_sweep_tunable_is_refused_at_startup(monkeypatch, bad, env_var, fn_name):
    """A non-positive or unparseable value must not become a silent default
    — it is a startup refusal, read where setup_schedules builds the
    schedule's input, never inside workflow code."""
    import orchestrator.temporal.constants as constants_module

    monkeypatch.setenv(env_var, bad)
    with pytest.raises(SystemExit):
        getattr(constants_module, fn_name)()


def test_all_also_polls_every_routed_queue(visibility):
    """`all` is the documented rollback target, so it has to work as one.

    After a routing flip, patched histories schedule submit_and_wait onto
    the execution or implementation queue. Collapsing the split deployments
    back to a process that listens only on the control queue would leave
    those activities with no poller until they time out — a rollback that
    strands work is not a rollback (codex P1 on #249).
    """
    queues = {p.task_queue for p in worker_plans("all", visibility)}

    assert queues == {TASK_QUEUE, EXECUTION_TASK_QUEUE, IMPLEMENTATION_TASK_QUEUE}


def test_all_keeps_the_admission_limit_on_the_implementation_queue(visibility):
    """The one limit `all` must NOT drop.

    Control and execution run unbounded under `all` because their limits
    are about starvation, and a single dev process has none. The
    implementation limit is different in kind: it is the admission
    capacity (#395). An `all` process admitting everything would be a
    rollback that silently removes the property the queue exists for.
    """
    plans = worker_plans("all", visibility)
    implementation = next(p for p in plans if p.task_queue == IMPLEMENTATION_TASK_QUEUE)

    assert implementation.max_concurrent_activities == implementation_max_concurrent_activities()
    assert implementation.activity_names == {"submit_and_wait"}


def test_the_implementation_worker_polls_only_the_admission_queue(visibility):
    """Same activity as execution, different queue, its own capacity.

    It runs no workflows and serves nothing else: a short activity landing
    here would take an implementation slot from an implementer, and a
    second long operation would make N mean two things at once.
    """
    plans = worker_plans("implementation", visibility)

    assert [p.task_queue for p in plans] == [IMPLEMENTATION_TASK_QUEUE]
    assert plans[0].activity_names == {"submit_and_wait"}
    assert plans[0].workflows == []
    assert plans[0].max_concurrent_activities == implementation_max_concurrent_activities()
    assert plans[0].max_concurrent_workflow_tasks is None


def test_the_execution_worker_is_untouched_by_the_admission_queue(visibility):
    """#395 adds a queue; it does not re-shape the one ADR-008 built.

    Lowering exec from 40, or making it poll the admission queue too,
    would re-couple investigate/reconcile/incidents to implementer
    capacity — exactly the coupling the split removed.
    """
    plans = worker_plans("execution", visibility)

    assert [p.task_queue for p in plans] == [EXECUTION_TASK_QUEUE]
    assert plans[0].max_concurrent_activities == EXECUTION_MAX_CONCURRENT_ACTIVITIES
    assert EXECUTION_MAX_CONCURRENT_ACTIVITIES == 40


def test_implementation_capacity_is_read_from_the_environment(monkeypatch, visibility):
    """N is the number an operator moves, so it comes from values.yaml via
    env — not from a constant that needs a code release to change. Bounded
    at 1 while the mirror still names run-implementer (#418) — see
    TestImplementationCapacityIsBoundToTheMutex for the ceiling itself."""
    monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "1")
    assert worker_plans("implementation", visibility)[0].max_concurrent_activities == 1

    monkeypatch.delenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES")
    assert worker_plans("implementation", visibility)[0].max_concurrent_activities == 1


@pytest.mark.parametrize("bad", ["0", "-1", "three", "2.5"])
def test_a_capacity_that_admits_nothing_is_refused_at_startup(monkeypatch, visibility, bad):
    """Zero or garbage must not become a worker that polls and admits
    nothing forever — that is a queue nobody reads with extra steps."""
    monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", bad)
    with pytest.raises(SystemExit):
        worker_plans("implementation", visibility)


class TestImplementationCapacityIsBoundToTheMutex:
    """T5 (#418): N cannot exceed the Argo mutex width while that mutex
    still guards the timed `run-implementer` step — the 2026-09-19 shape,
    reproduced at a smaller N. A larger configured N is CLAMPED to the
    width with one warning, never refused: mctl-gitops pins N="3" against
    width 1 today, and a refusal would crash-loop the implementation worker.
    `worker_plans` reads the ceiling through
    `implementation_max_concurrent_activities()`, so patching the constants
    module's mirror is enough to drive both states.
    """

    def test_n_three_against_width_one_is_clamped_to_one_and_does_not_raise(
        self, monkeypatch, visibility, caplog
    ):
        monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "3")
        with caplog.at_level(logging.WARNING, logger="orchestrator.temporal.constants"):
            plans = worker_plans("implementation", visibility)
        assert plans[0].max_concurrent_activities == 1

        warnings = [r for r in caplog.records if r.name == "orchestrator.temporal.constants"]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES=3" in message
        assert "mctl-agents-proposal-claims=1" in message

    def test_the_clamp_is_the_function_contract_not_only_the_plan(self, monkeypatch):
        monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "3")
        assert implementation_max_concurrent_activities() == 1

    def test_n_at_or_under_the_width_is_not_warned_about(self, monkeypatch, visibility, caplog):
        monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "1")
        with caplog.at_level(logging.WARNING, logger="orchestrator.temporal.constants"):
            assert worker_plans("implementation", visibility)[0].max_concurrent_activities == 1
        assert not [r for r in caplog.records if r.name == "orchestrator.temporal.constants"]

    def test_all_is_clamped_the_same_way(self, monkeypatch, visibility):
        monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "3")
        plans = {p.task_queue: p for p in worker_plans("all", visibility)}
        assert plans[IMPLEMENTATION_TASK_QUEUE].max_concurrent_activities == 1

    def test_control_and_execution_are_unaffected(self, monkeypatch, visibility):
        monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "3")
        assert worker_plans("control", visibility)
        assert worker_plans("execution", visibility)

    def test_n_above_the_width_is_accepted_once_the_mutex_moves_off_run_implementer(
        self, monkeypatch, visibility
    ):
        # Patch the globals of the function worker.py actually calls, not
        # `from orchestrator.temporal import constants`: in the full suite
        # the Temporal workflow sandbox re-imports that module, leaving the
        # package attribute pointing at a different module object than the
        # one `implementation_max_concurrent_activities` closes over — so a
        # setattr on the package attribute passed alone and silently patched
        # nothing in the full run.
        monkeypatch.setitem(
            worker_module.implementation_max_concurrent_activities.__globals__,
            "ARGO_IMPLEMENT_MUTEX_TEMPLATE",
            "commit-and-push",
        )
        monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "3")
        assert worker_plans("implementation", visibility)[0].max_concurrent_activities == 3


@pytest.mark.parametrize("role", ["control", "execution"])
def test_a_bad_capacity_cannot_take_down_a_role_that_never_admits(monkeypatch, visibility, role):
    """A typo in a shared env must fail only the admission worker.

    N is read when the implementation plan is built, not on import: a
    module-level constant would SystemExit every role at import time, so a
    bad value in a configmap reused across the three deployments would
    crash-loop control and execution workers that never touch the queue
    (claude P3 on #397).
    """
    monkeypatch.setenv("IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES", "nope")
    plans = worker_plans(role, visibility)

    assert IMPLEMENTATION_TASK_QUEUE not in {p.task_queue for p in plans}


def test_the_execution_worker_polls_only_the_new_queue(visibility):
    """It services activities scheduled by workflows it does not run."""
    plans = worker_plans("execution", visibility)

    assert [p.task_queue for p in plans] == [EXECUTION_TASK_QUEUE]
    assert plans[0].activity_names == {"submit_and_wait"}


def test_the_control_worker_still_serves_submit_and_wait(visibility):
    """The ordering constraint that makes step 1 releasable on its own.

    Nothing routes to the execution queue until the patched flip lands, so
    a control worker that dropped submit_and_wait would strand every Argo
    submit the moment it rolled out — a self-inflicted outage in the gap
    between two PRs.
    """
    plans = worker_plans("control", visibility)

    assert [p.task_queue for p in plans] == [TASK_QUEUE]
    assert "submit_and_wait" in plans[0].activity_names


def test_slot_limits_are_set_for_the_split_roles(visibility):
    """Explicit limits are the point: an unbounded pool is what turns
    exhaustion into an invisible stall instead of a visible backlog."""
    control = worker_plans("control", visibility)[0]
    execution = worker_plans("execution", visibility)[0]

    assert control.max_concurrent_activities == CONTROL_MAX_CONCURRENT_ACTIVITIES
    assert control.max_concurrent_workflow_tasks == CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS
    assert execution.max_concurrent_activities == EXECUTION_MAX_CONCURRENT_ACTIVITIES
    # The execution worker runs no workflows, so a workflow-task limit there
    # would be a number with nothing to bound.
    assert execution.max_concurrent_workflow_tasks is None


def test_metrics_are_published_where_the_deployment_can_be_scraped():
    """ADR-008 D5 / #252 — the exporter has to land on the port the pod
    already declares, on an address a vmagent in another pod can reach.

    mctl-gitops's base-service chart renders containerPort `http` and
    Service port `http` from .Values.service.port unconditionally; the
    separate `metrics` port only exists under `metrics.enabled`, which also
    renders a monitoring.coreos.com ServiceMonitor that this cluster's
    VictoriaMetrics operator auto-converts and orphans. So the VMServiceScrape
    on the gitops side targets `port: http` — a bind on any other port, or on
    loopback, is a scrape pool that stays empty while looking configured.
    """
    metrics = telemetry_config().metrics

    assert metrics is not None
    assert metrics.bind_address == f"0.0.0.0:{METRICS_PORT}"
    # Spelled out rather than derived from the constant: 8080 is not a free
    # choice, it is base-service's `service.port` default over in
    # mctl-gitops. Asserting it against itself would pass for any value and
    # catch exactly the change that breaks the scrape.
    assert METRICS_PORT == 8080


def test_latencies_are_reported_in_seconds():
    """The one number the split exists to expose is schedule-to-start, and
    the SDK's default is integer milliseconds — which quantises the
    sub-second range a healthy control queue sits in into a handful of
    buckets, and reads as a different unit than every other histogram the
    VMRules are written against."""
    metrics = telemetry_config().metrics

    assert metrics is not None
    assert metrics.durations_as_seconds is True


@pytest.mark.anyio
async def test_the_client_is_built_on_the_runtime_that_exports():
    """A client keeps the runtime it was created with.

    Constructing the Runtime but connecting without it is the silent
    failure this guards: the port binds, /metrics answers, and every
    `temporal_*` series stays on the default runtime that publishes
    nothing — an exporter that looks wired from the outside and reports an
    empty queue forever.
    """
    connected = {}

    async def fake_connect(address, **kwargs):
        connected.update(kwargs)
        return MagicMock()

    with (
        mock.patch.object(worker_module.Client, "connect", side_effect=fake_connect),
        mock.patch.object(worker_module, "setup_schedules", new=mock.AsyncMock()),
        mock.patch.object(worker_module, "build_worker", return_value=MagicMock()),
        mock.patch.object(worker_module, "run_until_signalled", new=mock.AsyncMock()),
        mock.patch.object(worker_module, "Runtime") as runtime_cls,
        mock.patch("sys.argv", ["worker", "--role", "control"]),
    ):
        await worker_module.main()

    runtime_cls.assert_called_once_with(telemetry=mock.ANY)
    assert connected.get("runtime") is runtime_cls.return_value


def test_an_unknown_role_is_refused(visibility):
    """argparse guards the CLI, but worker_plans is also called directly."""
    with pytest.raises(SystemExit):
        worker_plans("orchestration", visibility)


def test_only_workflow_running_roles_own_the_schedules():
    """An execution worker must not assert a spec for workflows it does not run.

    Not merely redundant: `_ensure_schedule` converges an existing spec in
    place, so a role declaring a cadence it does not serve would actively
    overwrite the real one.
    """
    assert owns_schedules("all") is True
    assert owns_schedules("control") is True
    assert owns_schedules("execution") is False
    assert owns_schedules("implementation") is False


def test_no_control_ceiling_is_lowered_before_the_routing_flip():
    """Until the routing flip the control worker still carries every long
    Argo poll AND all five workflow types. Any ceiling below current
    behaviour in that window reintroduces the starvation the split removes
    (claude P2 on #249, twice — once per limit). They are tightened only
    after the flip has taken the workload away and the soak has produced
    numbers to pick from (codex P2 on #249).

    Both limits, because fixing one and leaving the sibling is exactly how
    this was got wrong the first time. Note the workflow-task default is
    not 100: unset, the SDK builds a 500-thread pool, so any number there
    is a much bigger step down than it looks.
    """
    assert CONTROL_MAX_CONCURRENT_ACTIVITIES >= 100
    assert CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS is None


# ---------------------------------------------------------------------------
# Graceful shutdown across every worker in the process (agy P1 on #249)
# ---------------------------------------------------------------------------
class _FakeWorker:
    """Mimics the SDK pair this design uses: run() blocks, shutdown() ends it."""

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self._stop = asyncio.Event()
        self._fail_with = fail_with
        self.ran = False
        self.shut_down = False

    async def run(self) -> None:
        self.ran = True
        if self._fail_with is not None:
            raise self._fail_with
        await self._stop.wait()

    async def shutdown(self) -> None:
        self.shut_down = True
        self._stop.set()


@pytest.mark.anyio
async def test_every_worker_is_drained_on_shutdown():
    """Both workers must drain, not just one.

    `--role all` runs two workers in one process. The SDK installs no
    signal handlers of its own, so without this the pod's SIGTERM ended
    the process outright and in-flight activities were cut mid-flight.
    """
    workers = [_FakeWorker(), _FakeWorker()]

    task = asyncio.create_task(run_until_signalled(workers))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(task, timeout=5)

    assert all(w.ran for w in workers)
    assert all(w.shut_down for w in workers)


@pytest.mark.anyio
async def test_a_worker_dying_alone_takes_the_process_down():
    """A dead poller must not leave the process looking healthy.

    The pre-split code got this from a bare `await worker.run()`. With two
    workers it has to be arranged: if only the control worker's loop dies,
    the execution worker keeps the process alive while reconcile, intake
    and every DevLoop go dark — and nothing restarts, because nothing
    crashed (claude P1 / codex P1 on #249).
    """
    healthy = _FakeWorker()
    doomed = _FakeWorker(fail_with=RuntimeError("connection lost"))

    with pytest.raises(RuntimeError, match="connection lost"):
        await asyncio.wait_for(
            run_until_signalled([healthy, doomed]),  # type: ignore[arg-type]
            timeout=5,
        )

    # ...and the survivor is drained rather than abandoned mid-flight.
    assert healthy.shut_down


@pytest.mark.anyio
async def test_a_survivor_that_will_not_drain_cannot_hold_the_crash_open():
    """The crash path must not wait on a drain nobody is timing.

    A worker died with no signal, so there was no SIGTERM and there is no
    SIGKILL coming either. A survivor whose shutdown does not return
    therefore parks the process in exactly the state the crash exists to
    escape: Running, healthy-looking, one queue unpolled. Today every
    activity is async and unwinds on the first await, so the drain is
    quick — but that is a property of the activity set, not of this
    function (agy P1 on #249).
    """
    class _StuckWorker(_FakeWorker):
        async def shutdown(self) -> None:
            self.shut_down = True
            await asyncio.Event().wait()  # never returns

    stuck = _StuckWorker()
    doomed = _FakeWorker(fail_with=RuntimeError("connection lost"))

    with mock.patch.object(worker_module, "CRASH_DRAIN_TIMEOUT_SECONDS", 0.05):
        with pytest.raises(RuntimeError, match="connection lost"):
            await asyncio.wait_for(
                run_until_signalled([stuck, doomed]),  # type: ignore[arg-type]
                timeout=5,
            )

    assert stuck.shut_down, "the drain should still be attempted, just not waited on"


@pytest.mark.anyio
async def test_the_undrained_tasks_are_reported_before_the_crash(caplog):
    """The one diagnostic explaining why the pod died must actually print.

    `asyncio.wait` does NOT raise on timeout — it returns (done, pending)
    quietly — so an `except TimeoutError` around it could never fire, and
    the log line meant to name the stuck tasks never reached the pod's
    output (agy P3 on #249). The pending set is the signal, so assert on
    what the pending set produced.
    """
    class _StuckWorker(_FakeWorker):
        async def shutdown(self) -> None:
            self.shut_down = True
            await asyncio.Event().wait()

    workers = [_StuckWorker(), _FakeWorker(fail_with=RuntimeError("connection lost"))]

    with caplog.at_level(logging.ERROR, logger=worker_module.__name__):
        with mock.patch.object(worker_module, "CRASH_DRAIN_TIMEOUT_SECONDS", 0.05):
            with pytest.raises(RuntimeError, match="connection lost"):
                await asyncio.wait_for(
                    run_until_signalled(workers),  # type: ignore[arg-type]
                    timeout=5,
                )

    assert any(
        "still running" in record.getMessage() for record in caplog.records
    ), f"no diagnostic named the undrained tasks: {[r.getMessage() for r in caplog.records]}"


@pytest.mark.anyio
async def test_a_worker_that_stops_polling_without_an_error_is_still_fatal():
    """`run()` returning cleanly is not a healthy state without a signal.

    Nothing raises, so the exception check above sees nothing to re-raise
    — and the queue has still stopped being polled, with only a restart to
    resume it.
    """
    class _QuietlyStoppingWorker(_FakeWorker):
        async def run(self) -> None:
            self.ran = True

    with pytest.raises(RuntimeError, match="stopped polling without raising"):
        await asyncio.wait_for(
            run_until_signalled([_FakeWorker(), _QuietlyStoppingWorker()]),  # type: ignore[arg-type]
            timeout=5,
        )


def test_the_sdk_still_offers_the_run_shutdown_pair_this_module_drives():
    """Pin the SDK surface run_until_signalled actually calls.

    `_FakeWorker` cannot tell the difference if a future SDK upgrade moves
    this: the fake would keep passing while production stopped polling.
    So the contract worth pinning is the one production depends on —
    `run()` to poll and `shutdown()` to drain, both awaitable.

    This deliberately does NOT pin `__aenter__`/`__aexit__` any more. It
    did while the code used `async with`; the code now drives run() and
    shutdown() directly, and a test guarding an API this module never
    calls would fail the build over an SDK change that cannot affect us
    (agy P2 on #249).
    """
    from temporalio.worker import Worker

    for name in ("run", "shutdown"):
        member = getattr(Worker, name, None)
        assert member is not None, f"Worker no longer has {name}()"
        assert inspect.iscoroutinefunction(member), f"Worker.{name}() is no longer awaitable"


def test_the_visibility_activity_names_the_workflow_schedules_by_string_exist():
    """The other half of the registration assertion above.

    `worker_plans` is tested against a MagicMock stub, so it can only pin that
    whatever the stub exposes gets registered. This pins the real class
    actually exposes those two activity names — the strings
    `ImplementSweepWorkflow` schedules by. A rename on either side is silent
    otherwise: the workflow compiles, the worker starts, and every tick fails
    its budget query.
    """
    from orchestrator.temporal.activities.visibility import VisibilityActivities

    names = {
        getattr(getattr(VisibilityActivities, attr), "__temporal_activity_definition").name
        for attr in ("list_active_dev_loop_ids", "count_swept_prestart_failures")
    }
    assert names == {"list_active_dev_loop_ids", "count_swept_prestart_failures"}
