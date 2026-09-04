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
import sys

from orchestrator.temporal.issue_ref import parse_issue_url
from orchestrator.temporal.start import connect, start_dev_loop_workflow
from orchestrator.temporal.workflows.dev_loop import DevLoopResult, DevLoopWorkflow


async def start(issue_url: str) -> None:
    try:
        parse_issue_url(issue_url)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    handle = await start_dev_loop_workflow(issue_url)
    print(f"started {handle.id} (run_id={handle.result_run_id})")


async def approve(workflow_id: str, approver: str) -> None:
    client = await connect()
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal(DevLoopWorkflow.approve, {"approver": approver})
    print(f"signalled approve on {workflow_id} as {approver}")


async def status(workflow_id: str) -> None:
    client = await connect()
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    print(f"{workflow_id}: {desc.status}")
    if desc.status is not None and desc.status.name == "COMPLETED":
        result: DevLoopResult = await handle.result()
        print(f"  investigate: {result.investigate.phase} ({result.investigate.workflow_name})")
        if result.approve:
            print(f"  approve:     {result.approve.phase} ({result.approve.workflow_name})")
        if result.implement:
            print(f"  implement:   {result.implement.phase} ({result.implement.workflow_name})")
        elif result.approve and not result.approve.succeeded:
            print("  implement:   not reached (the approve flip failed — see above)")
        else:
            print("  implement:   not reached (never approved, or investigate did not succeed)")
        # Merge-detection outcome (stage 6.1, #214). Absent on results from
        # pre-merge-detection histories and when implement never produced a
        # PR to watch.
        if result.pr is not None:
            if result.pr.found:
                print(f"  pr:          {result.pr.state} ({result.pr.pr_url})")
            else:
                print(
                    "  pr:          unresolvable"
                    + (f" ({result.pr.pr_url})" if result.pr.pr_url else " (no PR link recorded)")
                )
        elif result.implement and result.implement.succeeded:
            print("  pr:          not watched (pre-merge-detection history, or no PR link appeared)")
        # Deploy observation (stages 6.2/6.3, #215). Absent unless the PR
        # merged under a history new enough to carry the stage.
        if result.deploy is not None:
            d = result.deploy
            where = f"{d.team}/{d.app}" if d.team and d.app else "-"
            print(f"  deploy:      {d.outcome} ({where})")
            if d.release_tag:
                print(f"    release:   {d.release_tag}")
            if d.health or d.sync_status:
                print(f"    argocd:    {d.sync_status or '?'}/{d.health or '?'} tag={d.image_tag or '-'}")
            if d.detail:
                print(f"    detail:    {d.detail}")
        # Incident watch (stage 6.4, #216).
        if result.incidents is not None and result.incidents.watched:
            w = result.incidents
            more = " (truncated — more than the query cap)" if w.truncated else ""
            print(f"  incidents:   {len(w.incidents)} in {w.window_minutes}m after the rollout{more}")
            for incident in w.incidents:
                print(f"    - [{incident.severity or '?'}] {incident.id} {incident.title}")
            if w.detail:
                print(f"    detail:    {w.detail}")


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser.

    Split out of main() so tests can assert that the approve command the
    investigator posts to real GitHub issues still parses (gitops#986).
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="Start a DevLoopWorkflow for one issue")
    p_start.add_argument("issue_url")

    p_approve = sub.add_parser("approve", help="Signal approval on a running DevLoopWorkflow")
    p_approve.add_argument("workflow_id")
    # Required, and deliberately not defaulted. A bare signal carried no
    # payload, so the approver recorded in .status.yaml and in the gitops
    # commit message was the literal string "unknown" - an approval naming
    # nobody. The implementer now refuses those (gitops#986), so a default
    # here would only produce proposals it declines to act on.
    #
    # This is an operator escape hatch against the Temporal frontend, so the
    # value is asserted rather than verified. The endpoint that takes the
    # approver from the authenticated caller is the trustworthy path:
    # POST /api/v1/agents/dev-loop/{workflow_id}/approve.
    p_approve.add_argument(
        "--approver",
        required=True,
        help="Identity to record as the approver (not verified here; prefer "
             "the mctl-api approve endpoint, which takes it from the caller)",
    )

    p_status = sub.add_parser("status", help="Print a DevLoopWorkflow's status (and result, if complete)")
    p_status.add_argument("workflow_id")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "start":
        asyncio.run(start(args.issue_url))
    elif args.command == "approve":
        asyncio.run(approve(args.workflow_id, args.approver))
    elif args.command == "status":
        asyncio.run(status(args.workflow_id))


if __name__ == "__main__":
    main()
