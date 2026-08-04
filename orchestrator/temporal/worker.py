"""Entry point: `python -m orchestrator.temporal.worker`.

Connects to the shared Temporal deployment (TEMPORAL_ADDRESS,
TEMPORAL_NAMESPACE=mctl-agents — see mctl-gitops's
infra-components/data/temporal/tenant-namespace-job.yaml for the namespace +
search-attribute registration) and runs DevLoopWorkflow plus its activities
on task queue TASK_QUEUE. Deployed as its own service (mctl-agents-worker,
ingress disabled) rather than as a sidecar on an HTTP service — unlike
kuptsi-app, nothing here serves inbound traffic.
"""
from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from orchestrator.temporal.activities.argo import submit_and_wait
from orchestrator.temporal.activities.registry import resolve_agent_release
from orchestrator.temporal.activities.state import record_execution
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow

TASK_QUEUE = "mctl-dev-loop"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "mctl-agents")

    logger.info("connecting to Temporal at %s (namespace=%s)", address, namespace)
    client = await Client.connect(address, namespace=namespace)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DevLoopWorkflow],
        activities=[resolve_agent_release, submit_and_wait, record_execution],
    )
    logger.info("worker starting on task queue %s", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
