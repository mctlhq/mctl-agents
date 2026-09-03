"""Entry point: `python -m orchestrator.temporal.worker`.

Connects to the shared Temporal deployment (TEMPORAL_ADDRESS,
TEMPORAL_NAMESPACE=mctl-agents — see mctl-gitops's
infra-components/data/temporal/tenant-namespace-job.yaml for the namespace +
search-attribute registration) and runs DevLoopWorkflow, ReconcileWorkflow,
IssuePollWorkflow, IncidentLoopWorkflow plus their activities on task queue TASK_QUEUE.
Deployed as its own service (mctl-agents-worker, ingress disabled).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from temporalio.worker import Worker

from orchestrator.temporal.activities.argo import submit_and_wait
from orchestrator.temporal.activities.deploy_state import (
    get_deploy_status,
    get_release_after,
    resolve_deploy_target,
)
from orchestrator.temporal.activities.discovery import discover_and_project
from orchestrator.temporal.activities.docs_delta import process_docs_delta_activity
from orchestrator.temporal.activities.incidents import list_service_incidents
from orchestrator.temporal.activities.issue_poll import poll_issues_activity
from orchestrator.temporal.activities.orphans import detect_orphans
from orchestrator.temporal.activities.pr_state import get_pr_state
from orchestrator.temporal.activities.proposals import find_proposal_slug
from orchestrator.temporal.activities.registry import resolve_agent_release
from orchestrator.temporal.activities.state import record_execution
from orchestrator.temporal.activities.visibility import VisibilityActivities
from orchestrator.temporal.constants import (
    CONTROL_MAX_CONCURRENT_ACTIVITIES,
    CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS,
    EXECUTION_MAX_CONCURRENT_ACTIVITIES,
    EXECUTION_TASK_QUEUE,
    METRICS_PORT,
    TASK_QUEUE,
)
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow
from orchestrator.temporal.workflows.docs_delta import DocsDeltaWorkflow
from orchestrator.temporal.workflows.incidents import IncidentLoopWorkflow
from orchestrator.temporal.workflows.issue_poll import IssuePollWorkflow, IssuePollWorkflowInput
from orchestrator.temporal.workflows.reconcile import ReconcileWorkflow, ReconcileWorkflowInput

RECONCILE_SCHEDULE_ID = "reconcile-mctl-agents-schedule"
RECONCILE_WORKFLOW_ID = "reconcile-mctl-agents"

ISSUE_POLL_SCHEDULE_ID = "issue-poll-mctl-agents-schedule"
ISSUE_POLL_WORKFLOW_ID = "issue-poll-mctl-agents"

INCIDENTS_SCHEDULE_ID = "incidents-mctl-agents-schedule"
INCIDENTS_WORKFLOW_ID = "incidents-mctl-agents"

# The three *_WORKFLOW_ID constants above are the id of the schedule's
# ACTION, not the id the started workflow ends up with: Temporal appends the
# action's nominal time when a schedule fires ("The Action's timestamp is
# appended to the Workflow Id" — docs.temporal.io/schedule), so each tick
# runs as e.g. `incidents-mctl-agents-2026-09-01T18:00:00Z`. Reading the
# constant as the per-run id makes every scheduled tick look like the same
# workflow, which is what led two reviewers to file the same finding against
# the incident loop's execution record (#254).

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

# How long to let the surviving workers drain when a worker died on its own
# rather than on a signal. Short on purpose: on that path the drain is a
# courtesy and the restart is the fix, so the cap only has to be long enough
# for cancelled activities to unwind. A signalled shutdown is not capped —
# there Kubernetes already holds the stopwatch.
CRASH_DRAIN_TIMEOUT_SECONDS = 20.0


async def _ensure_schedule(client: Client, schedule_id: str, desired: Schedule, label: str) -> None:
    """Create the schedule, or converge an existing one's spec to `desired`.

    `create_schedule` is a no-op once the schedule exists, so for the first
    year of this worker's life the interval declared here was decorative:
    editing it changed nothing on a cluster that had already registered the
    schedule (found 2026-08-16, changing the incidents cadence). Everything
    but the spec is left alone on purpose — in particular `state`, which
    carries the paused flag: `incidents-mctl-agents-schedule` is paused
    pending a manual verification run (mctl-agents#179), and a deploy must
    not quietly un-pause it.
    """
    try:
        await client.create_schedule(schedule_id, desired)
        logger.info("Created Temporal schedule %s for %s", schedule_id, label)
        return
    except ScheduleAlreadyRunningError:
        pass
    except Exception as exc:  # noqa: BLE001
        # Deliberately not fatal — see the test in test_worker_schedules.py.
        # ERROR, not WARNING: the concern this swallow trades away is a
        # worker that comes up healthy and never registers reconcile or
        # intake for its whole lifetime (agy P2 on #249). Riding out a blip
        # is worth it; doing so quietly is not.
        logger.error(
            "Temporal schedule %s NOT registered (%s) — this worker is "
            "serving its task queue but its schedules are not current",
            schedule_id,
            exc,
        )
        return

    changed: list[str] = []

    async def _converge_spec(input: ScheduleUpdateInput) -> ScheduleUpdate | None:
        schedule = input.description.schedule
        spec_stale = schedule.spec.intervals != desired.spec.intervals
        # The overlap policy is converged for the SAME reason the spec is, and
        # the reason is already written above: a value declared here but never
        # pushed to an existing schedule is decorative. That happened to the
        # interval for a year. Declaring `overlap` without converging it would
        # repeat it exactly — every schedule this code registers already
        # exists on the cluster, so a fresh `create_schedule` never runs again.
        live_overlap = getattr(schedule.policy, "overlap", None)
        want_overlap = getattr(desired.policy, "overlap", None)
        policy_stale = live_overlap != want_overlap
        if not spec_stale and not policy_stale:
            return None
        if spec_stale:
            logger.info(
                "Updating Temporal schedule %s spec: %s -> %s",
                schedule_id,
                schedule.spec.intervals,
                desired.spec.intervals,
            )
            schedule.spec = desired.spec
            changed.append("spec")
        if policy_stale:
            logger.info(
                "Updating Temporal schedule %s overlap policy: %s -> %s",
                schedule_id,
                live_overlap,
                want_overlap,
            )
            schedule.policy = desired.policy
            changed.append("overlap policy")
        return ScheduleUpdate(schedule=schedule)

    try:
        await client.get_schedule_handle(schedule_id).update(_converge_spec)
        # These logs are the only way to tell from outside whether a change
        # actually landed, so they name WHAT converged rather than asserting
        # "spec" for everything. Saying "spec converged" on a boot that only
        # pushed an overlap policy is the same defect as claiming "spec is
        # current" on a boot that just rewrote it — one field's log standing
        # in for another's (claude P3 on #297).
        if changed:
            logger.info(
                "Temporal schedule %s converged to the declared %s",
                schedule_id,
                " and ".join(changed),
            )
        else:
            logger.info(
                "Temporal schedule %s already exists; spec and overlap policy are current",
                schedule_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Temporal schedule %s NOT converged (%s) — its live spec or overlap "
            "policy may differ from the ones declared here",
            schedule_id,
            exc,
        )


# Duplicate-run behaviour for every scheduled loop (mctlhq/mctl-agents#149).
#
# All three schedules below declare `overlap=SKIP`: while a tick is still
# running, the next scheduled fire is DROPPED rather than started alongside
# it. That is the behaviour these loops need — reconcile and the incident
# responder both take the shared `mctl-gitops-main-writes` mutex, so two
# concurrent ticks would serialize behind each other while holding worker
# slots, and the second would do nothing the first has not already done.
#
# It was the behaviour before this declaration too, because temporalio's
# default is SKIP. The declaration is not a change; it is the difference
# between a property and an accident. Nothing here said it, nothing checked
# it, and it would have moved silently with a dependency bump.
#
# What SKIP costs, stated so it is not later mistaken for a fault:
# reconcile fires every 15 minutes and its apply step waits up to 35 for the
# mutex (`reconcile.APPLY_STEP_TIMEOUT`), so a tick that reaches apply can
# swallow the next two fires. Skipped ticks under load are expected. The
# alternative — BUFFER or ALLOW_ALL — trades that for a queue of ticks all
# contending for one mutex, which is worse.
async def setup_schedules(client: Client) -> None:
    reconcile_schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            ReconcileWorkflow.run,
            ReconcileWorkflowInput(),
            id=RECONCILE_WORKFLOW_ID,
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(minutes=15))],
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )

    await _ensure_schedule(client, RECONCILE_SCHEDULE_ID, reconcile_schedule, "ReconcileWorkflow")

    issue_poll_schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            IssuePollWorkflow.run,
            IssuePollWorkflowInput(),
            id=ISSUE_POLL_WORKFLOW_ID,
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(hours=12))],
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )

    await _ensure_schedule(client, ISSUE_POLL_SCHEDULE_ID, issue_poll_schedule, "IssuePollWorkflow")

    incidents_schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            IncidentLoopWorkflow.run,
            id=INCIDENTS_WORKFLOW_ID,
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            # Hourly, not the original 30 min: each tick now submits an Argo
            # workflow whose `workdir` is a fresh RWO Hetzner volume, so the
            # cadence is also a volume-churn budget (mctl-gitops#856). The
            # responder ignores incidents younger than MIN_AGE_MINUTES=30
            # anyway, so halving the tick rate costs no real responsiveness.
            # Matches what the (suspended) Argo cron did: `15 * * * *`.
            intervals=[ScheduleIntervalSpec(every=timedelta(hours=1))],
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )

    await _ensure_schedule(client, INCIDENTS_SCHEDULE_ID, incidents_schedule, "IncidentLoopWorkflow")


ROLES = ("all", "control", "execution")


@dataclass(frozen=True)
class WorkerPlan:
    """What one role registers, and where. See ADR-008.

    A plain description rather than a Worker, so the routing decision — the
    part with the actual consequences — is a pure function that can be
    asserted on without a live Temporal connection (Worker() insists on a
    real bridge client and dials on construction).
    """

    task_queue: str
    workflows: list[type]
    activities: list[Callable[..., Any]]
    max_concurrent_activities: int | None = None
    max_concurrent_workflow_tasks: int | None = None

    @property
    def activity_names(self) -> set[str]:
        return {getattr(a, "__temporal_activity_definition").name for a in self.activities}


def owns_schedules(role: str) -> bool:
    """Does this role create and converge the Temporal schedules?

    Only a role that runs the workflows may assert their specs. An
    execution worker declaring a cadence for workflows it does not run is
    a lie waiting to drift — and since _ensure_schedule converges an
    existing spec in place, that lie would actively overwrite the truth.

    A named function rather than an inline check in main(), for the same
    reason worker_plans exists: main() needs a live Temporal client, so
    anything decided inside it is untestable (claude P3 on #249).
    """
    return role in ("all", "control")


def telemetry_config() -> TelemetryConfig:
    """The SDK telemetry the process publishes — ADR-008 D5, #252.

    Split out of main() and returning the config rather than the Runtime for
    the same reason worker_plans exists: constructing a Runtime spins up a
    thread pool AND binds the port, so a test that asserted on one would be
    a test that races the real listener. The config is a value; the decision
    it encodes can be asserted without a bridge.

    Role-independent on purpose. Both halves of the split need the same
    numbers or the comparison the split exists to make — is the control pool
    saturated while the execution pool idles? — has only one side. The
    queue is a metric LABEL (task_queue), not a deployment difference.

    durations_as_seconds: the SDK otherwise reports latencies as integer
    milliseconds, which quantises the sub-second range the control queue
    should normally sit in down to a handful of buckets and reads as a
    different unit than every other histogram in this cluster. Verified
    against a real worker: buckets come out as `le="0.1"`, not `le="100"`.

    counters_total_suffix is deliberately NOT set. Its docstring promises
    the OpenMetrics `_total` suffix, but on temporalio 1.31.0 a live worker
    emits `temporal_activity_task_received` and `temporal_workflow_completed`
    with and without the flag alike — it changes nothing here. Setting it
    would put an unverified claim in the config and, worse, invite the
    VMRules to be written against `_total` names that do not exist.
    """
    return TelemetryConfig(
        metrics=PrometheusConfig(
            bind_address=f"0.0.0.0:{METRICS_PORT}",
            durations_as_seconds=True,
        )
    )


def worker_plans(role: str, visibility: VisibilityActivities) -> list[WorkerPlan]:
    """The queue/registration layout for one role — one plan per queue.

    A list, because `all` must poll BOTH queues. It is the documented
    rollback target, and after the routing flip patched histories schedule
    submit_and_wait onto the execution queue: an `all` process listening
    only on the control queue would leave those activities with no poller
    until they time out, so collapsing the split deployments back would
    not be a rollback at all (codex P1 on #249).

    `all` is the default and is byte-for-byte the single-queue worker this
    repo has always run — same queue, same activities, no slot limits — so
    this change is releasable on its own and a rollback is a values edit
    rather than a code revert.

    Workflows go on the control queue only. The execution worker never
    needs them: it services activities scheduled BY those workflows, and a
    workflow task is short by construction, so putting them on the
    long-holding queue would reintroduce the starvation the split removes.
    """
    if role not in ROLES:
        raise SystemExit(f"--role must be one of {', '.join(ROLES)}, got {role!r}")

    short_activities: list[Callable[..., Any]] = [
        resolve_agent_release,
        record_execution,
        find_proposal_slug,
        get_pr_state,
        resolve_deploy_target,
        get_release_after,
        get_deploy_status,
        list_service_incidents,
        discover_and_project,
        detect_orphans,
        visibility.list_active_dev_loop_ids,
        poll_issues_activity,
        process_docs_delta_activity,
    ]
    workflows: list[type] = [
        DevLoopWorkflow, ReconcileWorkflow, IssuePollWorkflow, IncidentLoopWorkflow, DocsDeltaWorkflow
    ]

    execution_plan = WorkerPlan(
        task_queue=EXECUTION_TASK_QUEUE,
        workflows=[],
        activities=[submit_and_wait],
        max_concurrent_activities=EXECUTION_MAX_CONCURRENT_ACTIVITIES,
    )

    if role == "execution":
        return [execution_plan]
    if role == "control":
        return [WorkerPlan(
            task_queue=TASK_QUEUE,
            workflows=workflows,
            # submit_and_wait stays registered here too: nothing routes to
            # the execution queue until the patched flip lands, so removing
            # it now would strand every Argo submit in the gap between the
            # two PRs.
            activities=[*short_activities, submit_and_wait],
            max_concurrent_activities=CONTROL_MAX_CONCURRENT_ACTIVITIES,
            max_concurrent_workflow_tasks=CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS,
        )]
    # `all`: one process, both queues, no limits — the pre-split shape plus
    # a poller for the queue the routing flip starts using.
    return [
        WorkerPlan(
            task_queue=TASK_QUEUE,
            workflows=workflows,
            activities=[*short_activities, submit_and_wait],
        ),
        execution_plan,
    ]


def build_worker(client: Client, plan: WorkerPlan) -> Worker:
    """Turn a plan into a Worker. Kept trivial on purpose — everything
    worth testing lives in worker_plans()."""
    kwargs: dict[str, Any] = {}
    if plan.max_concurrent_activities is not None:
        kwargs["max_concurrent_activities"] = plan.max_concurrent_activities
    if plan.max_concurrent_workflow_tasks is not None:
        kwargs["max_concurrent_workflow_tasks"] = plan.max_concurrent_workflow_tasks
    return Worker(
        client,
        task_queue=plan.task_queue,
        workflows=plan.workflows,
        activities=plan.activities,
        **kwargs,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role",
        default=os.environ.get("WORKER_ROLE", "all"),
        choices=ROLES,
        help=(
            "which half of the split this process serves (ADR-008). "
            "'all' — one process, one queue, as before (default)."
        ),
    )
    args = parser.parse_args()

    address = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "mctl-agents")

    # One Runtime for the process, built BEFORE the client: a client keeps
    # the runtime it was created with, so a later one would leave this
    # process's metrics on the default runtime that exports nothing. For
    # `--role all` both workers share this client and therefore this
    # exporter, and separate by the task_queue label rather than by port.
    runtime = Runtime(telemetry=telemetry_config())
    logger.info("serving metrics on :%d/metrics", METRICS_PORT)

    logger.info("connecting to Temporal at %s (namespace=%s)", address, namespace)
    client = await Client.connect(address, namespace=namespace, runtime=runtime)

    if owns_schedules(args.role):
        await setup_schedules(client)

    visibility = VisibilityActivities(client)
    plans = worker_plans(args.role, visibility)
    workers = [build_worker(client, plan) for plan in plans]

    logger.info(
        "worker starting: role=%s task_queues=%s",
        args.role,
        ", ".join(plan.task_queue for plan in plans),
    )
    await run_until_signalled(workers)


async def run_until_signalled(workers: list[Worker]) -> None:
    """Run every worker until SIGTERM/SIGINT or a worker dies, then drain.

    The Python SDK installs no signal handlers (unlike some of the other
    Temporal SDKs — `add_signal_handler` appears nowhere in temporalio),
    so SIGTERM used to hit Python's default and end the process outright:
    no drain, in-flight activities cut mid-flight. That was already true
    of the single worker; a queue split whose whole purpose is rollouts
    should not leave rollouts ungraceful.

    A worker that dies on its own must still take the process down. The
    pre-split code got that for free from a bare `await worker.run()`. Two
    workers make it something that has to be arranged: if only the control
    worker's poll loop dies — a dropped connection, an auth failure — the
    execution worker keeps the process alive and looking healthy while
    reconcile, intake and every DevLoop go dark, with nothing to restart
    because nothing crashed. So the shutdown signal is RACED against the
    run tasks and a worker's failure is re-raised (claude P1 / codex P1 on
    #249).

    Explicit tasks rather than `async with`: the SDK's context manager
    does propagate a fatal error, but by cancelling whichever task entered
    it, which is subtle enough under AsyncExitStack that a reader cannot
    check it locally. `run()` plus `shutdown()` is the SDK's own
    documented pair and says what it does.
    """
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:  # pragma: no cover — non-POSIX only
            pass

    run_tasks = [asyncio.create_task(worker.run()) for worker in workers]
    signal_task = asyncio.create_task(shutdown.wait())
    try:
        done, _ = await asyncio.wait(
            [*run_tasks, signal_task], return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        signal_task.cancel()

    crashed = signal_task not in done
    if crashed:
        # Nobody asked for this shutdown, so nothing is holding a stopwatch
        # over it: there was no SIGTERM, so there is no SIGKILL coming 30 s
        # later either. An unbounded drain here can only end one way — the
        # pod stays Running with a queue nobody polls, which is the exact
        # state the race above exists to escape. Every activity is async
        # today and dies on the first await after cancellation, so the
        # drain is quick; that is a property of the current activity set,
        # not of this function, and one sync activity would silently make
        # it untrue (agy P1 on #249, whose reasoning assumed the drain
        # already blocks for hours — it does not, but nothing stops it
        # from starting to).
        logger.error(
            "a worker stopped on its own; draining with a %ss cap, then crashing",
            CRASH_DRAIN_TIMEOUT_SECONDS,
        )
    else:
        logger.info("draining %d worker(s)", len(workers))

    # shutdown() is safe to call repeatedly and on a worker that already
    # stopped, so this needs no bookkeeping about which one finished first.
    #
    # ONE deadline over the shutdowns AND the run tasks, rather than a
    # bounded wait on each. Two sequential waits let a slow drain spend
    # the cap twice — 40 s under a 20 s policy — and, worse, the second
    # one was `asyncio.wait`, which does not raise on timeout: it returns
    # (done, pending) quietly, so the `except TimeoutError` that was
    # supposed to log why the pod is about to die could never fire (agy P3
    # on #249). The pending set is the signal; ask it directly.
    timeout = CRASH_DRAIN_TIMEOUT_SECONDS if crashed else None
    shutdowns = [asyncio.ensure_future(worker.shutdown()) for worker in workers]
    _, pending = await asyncio.wait([*shutdowns, *run_tasks], timeout=timeout)
    if pending:
        logger.error(
            "%d task(s) still running %ss after shutdown was requested; "
            "crashing without them",
            len(pending),
            timeout,
        )
        for task in pending:
            task.cancel()

    # A shutdown() that raised is not itself fatal — the worker failure
    # below is the real story — but its exception has to be retrieved, or
    # asyncio logs it as never-retrieved noise right as the pod dies.
    for task in shutdowns:
        if task.done() and not task.cancelled():
            task.exception()

    for task in run_tasks:
        if not task.done() or task.cancelled():
            continue
        exc = task.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            raise exc

    if crashed:
        # run() returning cleanly without a signal is still fatal: that
        # queue has stopped being polled and only a restart resumes it.
        raise RuntimeError(
            "a worker stopped polling without raising and without a shutdown "
            "signal — crashing so the pod restarts"
        )



if __name__ == "__main__":
    asyncio.run(main())
