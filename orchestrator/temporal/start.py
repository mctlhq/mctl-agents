"""Start a DevLoopWorkflow for a GitHub issue.

Shared by orchestrator.temporal.cli (manual operator trigger) and
orchestrator.run_issue_poller (automatic `agents:intake`-label trigger) so
the workflow-ID scheme, connection settings, and dedup policy are defined in
exactly one place.
"""
from __future__ import annotations

import os

from temporalio.client import Client, WithStartWorkflowOperation, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from orchestrator.temporal.constants import TASK_QUEUE

# workflow_id_for moved to issue_ref (temporalio-free) so agent-container
# callers can import it without the SDK; re-exported here because this
# module is where every Temporal-side caller historically found it.
from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef

#: The one DevLoop per issue (mctlhq/mctl-agents#461 option A): every start,
#: the intake poller's and the execution-request dispatcher's alike, names
#: `workflow_id_for(issue_url)` with `USE_EXISTING` on conflict, so a start
#: against an issue whose loop is RUNNING attaches to it instead of starting
#: a second. That shared conflict policy is what makes the two starters
#: converge on one workflow, whichever reaches Temporal first.
DEV_LOOP_ID_CONFLICT_POLICY = WorkflowIDConflictPolicy.USE_EXISTING
#: The intake poller's reuse policy: a FAILED run stays restartable (see
#: `start_dev_loop_workflow`); a run that COMPLETED is "already handled" and
#: a re-added label starts nothing (`WorkflowAlreadyStartedError`).
DEV_LOOP_ID_REUSE_POLICY = WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY
#: The dispatcher's reuse policy: any closed run may be followed by a new one.
#: A request is an explicit ask to run: a `resume` for an issue whose loop has
#: ended starts a continuation (ADR 011, #267), and mctl-api's fulfil
#: re-decides whether the item may run at all. It cannot run one request
#: twice: a request can only be claimed again while it is unfulfilled, and a
#: run executes nothing for a request before its fulfilment binds the `we_`
#: (`_bind_dispatched_execution`, `_deliver`), so a closed run that took it
#: never ran it; a fulfilled request is closed for good.
DISPATCH_ID_REUSE_POLICY = WorkflowIDReusePolicy.ALLOW_DUPLICATE


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
        id_reuse_policy=DEV_LOOP_ID_REUSE_POLICY,
        id_conflict_policy=DEV_LOOP_ID_CONFLICT_POLICY,
    )


def dispatch_start_operation(issue: IssueRef) -> WithStartWorkflowOperation:
    """The start half of the execution-request dispatcher's Update-with-Start
    (mctlhq/mctl-agents#461 option A): the issue's own DevLoop, under
    `start_dev_loop_workflow`'s id and conflict policy, and the dispatcher's
    reuse policy (`DISPATCH_ID_REUSE_POLICY`).

    Temporal applies the start and the `accept_execution_request` Update as
    one operation. When the issue's loop is RUNNING the start input is
    ignored and the Update is delivered to that loop, whose validator
    decides (a `start` it was not started for is `loop_active`; a `resume`
    follows the `resume` signal's rules). When no loop runs, a new run starts
    with `issue.execution_request_id` as its own request and the Update
    accepts it before `run` executes: a `start` starts the issue's loop, a
    `resume` a continuation of it.

    Per-request exactly-once no longer rides a per-request workflow id: the
    Update id is the request id (Temporal answers a repeat from the run's
    registry) and the loop's `accepted_request_ids` covers a repeat after a
    continue-as-new."""
    if not issue.execution_request_id:
        raise ValueError("dispatch_start_operation needs an IssueRef with an execution_request_id")
    return WithStartWorkflowOperation(
        DevLoopWorkflow.run,
        issue,
        id=workflow_id_for(issue.issue_url),
        task_queue=TASK_QUEUE,
        id_reuse_policy=DISPATCH_ID_REUSE_POLICY,
        id_conflict_policy=DEV_LOOP_ID_CONFLICT_POLICY,
    )
