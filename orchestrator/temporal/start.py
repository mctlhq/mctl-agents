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

    A restart re-runs investigate, which used to derive the proposal slug
    from the issue title — so renaming the issue between the failure and
    the retry wrote a SECOND proposal beside the first, after which
    find_proposal_slug refused the now-ambiguous issue-<N>-* lookup (codex
    P2 on #241). run_issue_investigator.resolve_slug closed that by keying
    the directory on the issue number and reusing whatever dir already
    exists (#246), so a restart is safe across renames.
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


def dispatched_workflow_id(execution_request_id: str) -> str:
    """The DevLoop workflow id for one mctl-api execution request
    (mctlhq/mctl-agents#461): a pure function of the request id, so every
    claim of the same request — the first, or a re-claim after a crash and
    a lapsed lease, under a new claim token — names the same run. It is
    also the `engine_ref` the dispatcher fulfils the request with, so the
    same run is the same `we_` execution by mctl-api's
    `(engine, engine_ref)` idempotency."""
    return f"dev-loop-{execution_request_id}"


async def start_dispatched_dev_loop(client: Client, issue: IssueRef) -> WorkflowHandle:
    """Start (or attach to) the DevLoop for `issue.execution_request_id`.

    The policies are the "never a second run" half of the dispatcher's
    idempotency, and are deliberately stricter than
    `start_dev_loop_workflow`'s:

    - `USE_EXISTING` on conflict: a RUNNING loop for the same request is
      returned as-is — the re-claim after a crash between start and fulfil
      converges on it instead of starting another.
    - `REJECT_DUPLICATE` on reuse: a CLOSED loop for the same request raises
      `WorkflowAlreadyStartedError`, whatever its outcome. A request is run
      once. Unlike an issue-keyed loop, a failed dispatched run is not
      restartable under the same id: the surface asks again (a new request,
      a new id), which is what keeps one request from ever owning two runs.
    """
    if not issue.execution_request_id:
        raise ValueError("start_dispatched_dev_loop needs an IssueRef with an execution_request_id")
    return await client.start_workflow(
        DevLoopWorkflow.run,
        issue,
        id=dispatched_workflow_id(issue.execution_request_id),
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
