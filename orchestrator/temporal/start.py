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

from orchestrator.temporal.constants import TASK_QUEUE

# workflow_id_for moved to issue_ref (temporalio-free) so agent-container
# callers can import it without the SDK; re-exported here because this
# module is where every Temporal-side caller historically found it.
from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef


async def connect() -> Client:
    address = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "mctl-agents")
    return await Client.connect(address, namespace=namespace)


async def start_dev_loop_workflow(issue_url: str, client: Client | None = None) -> WorkflowHandle:
    """Start (or attach to) the DevLoopWorkflow for ``issue_url``.

    Connects fresh when ``client`` is None — the single-shot cli.py case.
    Callers dispatching many issues per cycle (the poller) should connect
    once and pass that ``Client`` in, instead of reconnecting to the
    Temporal frontend once per issue.

    id_conflict_policy=USE_EXISTING makes a repeated start against an issue
    with a currently-RUNNING workflow a true no-op (returns the existing
    run). A prior SUCCEEDED run for the same issue instead raises
    temporalio.exceptions.WorkflowAlreadyStartedError — callers that may see
    a re-added label on an already-completed issue (i.e. the poller) must
    catch that and treat it as "already handled", not a bug.

    ALLOW_DUPLICATE_FAILED_ONLY, not REJECT_DUPLICATE: a FAILED run must
    stay restartable. The A4 registry gate (#241) fails the loop
    non-retryably when an agent has no pinned image, and its own message
    tells the operator to publish, promote and retry — under
    REJECT_DUPLICATE that instruction was impossible to follow, because
    the same issue could never be started again without an out-of-band
    Temporal reset (codex P1 on #241). The same trap applied to every
    other terminal failure; only a run that actually completed is "already
    handled".
    """
    if client is None:
        client = await connect()
    return await client.start_workflow(
        DevLoopWorkflow.run,
        IssueRef(issue_url=issue_url),
        id=workflow_id_for(issue_url),
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
