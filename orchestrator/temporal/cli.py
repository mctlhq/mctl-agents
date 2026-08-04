"""Manual DevLoopWorkflow control: `python -m orchestrator.temporal.cli ...`.

Stand-in for the mctl-api MCP-tool trigger path (mctl_trigger_issue's planned
use_temporal flag — plan phase 4's "Trigger change", not yet wired as of this
module's introduction). Lets an operator start and approve a real workflow
run directly against the Temporal frontend for verification, without waiting
on the Go-side client.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

from temporalio.client import Client

from orchestrator.temporal.worker import TASK_QUEUE
from orchestrator.temporal.workflows.dev_loop import DevLoopResult, DevLoopWorkflow, IssueRef

_ISSUE_URL_RE = re.compile(r"^https://github\.com/mctlhq/[A-Za-z0-9_.-]+/issues/[0-9]+$")


def _workflow_id_for(issue_url: str) -> str:
    # dev-loop-{owner}-{repo}-{issue}, per the plan — Temporal's own
    # workflow-ID dedup then replaces the hand-rolled "already past proposed"
    # guard the cron pipeline needs today.
    _, _, rest = issue_url.partition("github.com/")
    owner, repo, _, issue_number = rest.split("/")[:4]
    return f"dev-loop-{owner}-{repo}-{issue_number}"


async def _connect() -> Client:
    address = os.environ.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "mctl-agents")
    return await Client.connect(address, namespace=namespace)


async def start(issue_url: str) -> None:
    if not _ISSUE_URL_RE.match(issue_url):
        print(f"error: {issue_url!r} does not look like a mctlhq GitHub issue URL", file=sys.stderr)
        raise SystemExit(2)
    client = await _connect()
    workflow_id = _workflow_id_for(issue_url)
    handle = await client.start_workflow(
        DevLoopWorkflow.run,
        IssueRef(issue_url=issue_url),
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    print(f"started {handle.id} (run_id={handle.result_run_id})")


async def approve(workflow_id: str) -> None:
    client = await _connect()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(DevLoopWorkflow.approve)
    print(f"signalled approve on {workflow_id}")


async def status(workflow_id: str) -> None:
    client = await _connect()
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    print(f"{workflow_id}: {desc.status}")
    if desc.status is not None and desc.status.name == "COMPLETED":
        result: DevLoopResult = await handle.result()
        print(f"  investigate: {result.investigate.phase} ({result.investigate.workflow_name})")
        if result.implement:
            print(f"  implement:   {result.implement.phase} ({result.implement.workflow_name})")
        else:
            print("  implement:   not reached (never approved, or investigate did not succeed)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Start a DevLoopWorkflow for one issue")
    p_start.add_argument("issue_url")

    p_approve = sub.add_parser("approve", help="Signal approval on a running DevLoopWorkflow")
    p_approve.add_argument("workflow_id")

    p_status = sub.add_parser("status", help="Print a DevLoopWorkflow's status (and result, if complete)")
    p_status.add_argument("workflow_id")

    args = parser.parse_args()
    if args.command == "start":
        asyncio.run(start(args.issue_url))
    elif args.command == "approve":
        asyncio.run(approve(args.workflow_id))
    elif args.command == "status":
        asyncio.run(status(args.workflow_id))


if __name__ == "__main__":
    main()
