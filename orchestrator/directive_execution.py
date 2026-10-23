"""Resolve the execution identity an `@MCTL reinvestigate` run must carry
(mctl-agents#758).

mctl-api's run-token binder refuses a Workflow whose `execution_id` argument
is empty, so the directive poller may only submit `mctl-agents-investigate`
with the identity of the DevLoopWorkflow that owns the issue — the same
`we_` the loop hands its own investigate step. This module does a read-only
describe + `work_context` query; it never starts, signals or updates a loop,
and never mints or ends a WorkItem execution.

`NoExecution` is a definite answer (reply and ack); `ExecutionLookupUnavailable`
means Temporal could not answer (transient, retried within the poller's
dispatch-attempt budget).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.client import Client, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.work_context.snapshots import is_store_execution

QUERY_NAME = "work_context"
_RPC_TIMEOUT = timedelta(seconds=10)


@dataclass(frozen=True)
class LoopExecution:
    work_item_id: str
    execution_id: str
    workflow_id: str
    run_id: str


class NoExecution(Exception):
    """A definite answer: no usable execution identity exists for the issue."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class ExecutionLookupUnavailable(Exception):
    """Temporal could not answer; transient."""


def _field(state: Any, name: str) -> str:
    value = state.get(name) if isinstance(state, dict) else getattr(state, name, "")
    return value if isinstance(value, str) else ""


async def _client() -> Client:
    if activity.in_activity():
        return activity.client()
    from orchestrator.temporal.start import connect

    return await connect()


async def resolve_loop_execution(issue_url: str, client: Client | None = None) -> LoopExecution:
    workflow_id = workflow_id_for(issue_url)
    try:
        if client is None:
            client = await _client()
        handle = client.get_workflow_handle(workflow_id)
        desc = await handle.describe(rpc_timeout=_RPC_TIMEOUT)
    except RPCError as e:
        if e.status == RPCStatusCode.NOT_FOUND:
            raise NoExecution("no-live-loop", f"no DevLoopWorkflow {workflow_id}") from e
        raise ExecutionLookupUnavailable(f"describe {workflow_id} failed: {e}") from e
    except Exception as e:  # noqa: BLE001 — connect/timeout/anything else is "cannot answer"
        raise ExecutionLookupUnavailable(f"describe {workflow_id} failed: {e!r}") from e

    status = getattr(desc, "status", None)
    if getattr(status, "name", str(status)) != "RUNNING":
        raise NoExecution("no-live-loop", f"{workflow_id} is not running ({getattr(status, 'name', status)})")
    run_id = getattr(desc, "run_id", "") or ""

    try:
        state = await handle.query(QUERY_NAME, rpc_timeout=_RPC_TIMEOUT)
    except WorkflowQueryFailedError as e:
        if "unknown" in str(e).lower():
            raise NoExecution("loop-not-canonical", f"{workflow_id} has no {QUERY_NAME} query") from e
        raise ExecutionLookupUnavailable(f"query {QUERY_NAME} on {workflow_id} failed: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise ExecutionLookupUnavailable(f"query {QUERY_NAME} on {workflow_id} failed: {e!r}") from e

    work_item_id = _field(state, "work_item_id")
    execution_id = _field(state, "execution_id")
    if not work_item_id:
        raise NoExecution("no-work-item", f"{workflow_id} reports no work item")
    if not is_store_execution(execution_id):
        raise NoExecution("loop-not-canonical", f"{workflow_id} execution is not a store execution")
    return LoopExecution(
        work_item_id=work_item_id, execution_id=execution_id, workflow_id=workflow_id, run_id=run_id,
    )
