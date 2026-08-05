"""Start a DevLoopWorkflow for a GitHub issue.

Shared by orchestrator.temporal.cli (manual operator trigger) and
orchestrator.run_issue_poller (automatic `agents:intake`-label trigger) so
the workflow-ID scheme, connection settings, and dedup policy are defined in
exactly one place.
"""
from __future__ import annotations

import os

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from orchestrator.temporal.issue_ref import parse_issue_url
from orchestrator.temporal.worker import TASK_QUEUE
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef


def workflow_id_for(issue_url: str) -> str:
    # dev-loop-{owner}-{repo}-{issue} — Temporal's own workflow-ID dedup
    # replaces the hand-rolled "already past proposed" guard the cron
    # pipeline needed pre-Temporal.
    parts = parse_issue_url(issue_url)
    return f"dev-loop-{parts.owner}-{parts.repo}-{parts.number}"


async def connect() -> Client:
    address = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "mctl-agents")
    return await Client.connect(address, namespace=namespace)


async def start_dev_loop_workflow(issue_url: str) -> WorkflowHandle:
    """Start (or attach to) the DevLoopWorkflow for ``issue_url``.

    id_reuse_policy=REJECT_DUPLICATE + id_conflict_policy=USE_EXISTING makes
    a repeated start against an issue with a currently-RUNNING workflow a
    true no-op (returns the existing run). A prior CLOSED run for the same
    issue instead raises temporalio.exceptions.WorkflowAlreadyStartedError —
    callers that may see a re-added label on an already-completed issue
    (i.e. the poller) must catch that and treat it as "already handled",
    not a bug.
    """
    client = await connect()
    return await client.start_workflow(
        DevLoopWorkflow.run,
        IssueRef(issue_url=issue_url),
        id=workflow_id_for(issue_url),
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
