"""Entry point: `python -m orchestrator.temporal.worker`.

Connects to the shared Temporal deployment (TEMPORAL_ADDRESS,
TEMPORAL_NAMESPACE=mctl-agents — see mctl-gitops's
infra-components/data/temporal/tenant-namespace-job.yaml for the namespace +
search-attribute registration) and runs DevLoopWorkflow, ReconcileWorkflow,
IssuePollWorkflow, IncidentLoopWorkflow plus their activities on task queue TASK_QUEUE.
Deployed as its own service (mctl-agents-worker, ingress disabled).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
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
from orchestrator.temporal.constants import TASK_QUEUE
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

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)


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
        logger.warning("Could not register Temporal schedule %s: %s", schedule_id, exc)
        return

    converged = False

    async def _converge_spec(input: ScheduleUpdateInput) -> ScheduleUpdate | None:
        nonlocal converged
        schedule = input.description.schedule
        if schedule.spec.intervals == desired.spec.intervals:
            return None
        logger.info(
            "Updating Temporal schedule %s spec: %s -> %s",
            schedule_id,
            schedule.spec.intervals,
            desired.spec.intervals,
        )
        schedule.spec = desired.spec
        converged = True
        return ScheduleUpdate(schedule=schedule)

    try:
        await client.get_schedule_handle(schedule_id).update(_converge_spec)
        # Distinct messages, because these logs are the only way to tell from
        # outside whether a cadence change actually landed — claiming "spec is
        # current" on the same boot that just rewrote it reads as a no-op.
        if converged:
            logger.info("Temporal schedule %s spec converged to the declared one", schedule_id)
        else:
            logger.info("Temporal schedule %s already exists; spec is current", schedule_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not converge Temporal schedule %s: %s", schedule_id, exc)


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
    )

    await _ensure_schedule(client, INCIDENTS_SCHEDULE_ID, incidents_schedule, "IncidentLoopWorkflow")


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "mctl-agents")

    logger.info("connecting to Temporal at %s (namespace=%s)", address, namespace)
    client = await Client.connect(address, namespace=namespace)

    await setup_schedules(client)

    visibility = VisibilityActivities(client)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DevLoopWorkflow, ReconcileWorkflow, IssuePollWorkflow, IncidentLoopWorkflow, DocsDeltaWorkflow],
        activities=[
            resolve_agent_release,
            submit_and_wait,
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
        ],
    )
    logger.info("worker starting on task queue %s", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
