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
)
from temporalio.worker import Worker

from orchestrator.temporal.activities.argo import submit_and_wait
from orchestrator.temporal.activities.cleanup import cleanup_closed_issue_workflows
from orchestrator.temporal.activities.discovery import discover_and_project
from orchestrator.temporal.activities.incidents import respond_incidents_activity
from orchestrator.temporal.activities.issue_poll import poll_issues_activity
from orchestrator.temporal.activities.orphans import detect_orphans
from orchestrator.temporal.activities.registry import resolve_agent_release
from orchestrator.temporal.activities.state import record_execution
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow
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

    try:
        await client.create_schedule(RECONCILE_SCHEDULE_ID, reconcile_schedule)
        logger.info("Created Temporal schedule %s for ReconcileWorkflow", RECONCILE_SCHEDULE_ID)
    except ScheduleAlreadyRunningError:
        logger.info("Temporal schedule %s already exists", RECONCILE_SCHEDULE_ID)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register Temporal schedule %s: %s", RECONCILE_SCHEDULE_ID, exc)

    issue_poll_schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            IssuePollWorkflow.run,
            IssuePollWorkflowInput(),
            id=ISSUE_POLL_WORKFLOW_ID,
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(minutes=30))],
        ),
    )

    try:
        await client.create_schedule(ISSUE_POLL_SCHEDULE_ID, issue_poll_schedule)
        logger.info("Created Temporal schedule %s for IssuePollWorkflow", ISSUE_POLL_SCHEDULE_ID)
    except ScheduleAlreadyRunningError:
        logger.info("Temporal schedule %s already exists", ISSUE_POLL_SCHEDULE_ID)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register Temporal schedule %s: %s", ISSUE_POLL_SCHEDULE_ID, exc)

    incidents_schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            IncidentLoopWorkflow.run,
            id=INCIDENTS_WORKFLOW_ID,
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(minutes=30))],
        ),
    )

    try:
        await client.create_schedule(INCIDENTS_SCHEDULE_ID, incidents_schedule)
        logger.info("Created Temporal schedule %s for IncidentLoopWorkflow", INCIDENTS_SCHEDULE_ID)
    except ScheduleAlreadyRunningError:
        logger.info("Temporal schedule %s already exists", INCIDENTS_SCHEDULE_ID)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register Temporal schedule %s: %s", INCIDENTS_SCHEDULE_ID, exc)


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "mctl-agents")

    logger.info("connecting to Temporal at %s (namespace=%s)", address, namespace)
    client = await Client.connect(address, namespace=namespace)

    await setup_schedules(client)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DevLoopWorkflow, ReconcileWorkflow, IssuePollWorkflow, IncidentLoopWorkflow],
        activities=[
            resolve_agent_release,
            submit_and_wait,
            record_execution,
            discover_and_project,
            detect_orphans,
            poll_issues_activity,
            respond_incidents_activity,
            cleanup_closed_issue_workflows,
        ],
    )
    logger.info("worker starting on task queue %s", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
