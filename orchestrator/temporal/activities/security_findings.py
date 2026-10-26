"""Activity: bind a merged DevLoop PR's landed revision to its security attempt.

Called from `DevLoopWorkflow._finish_after_watch` (behind
`workflow.patched("security-record-landed")`) only when the PR merged and has
a merge commit. It never records a verification and never raises: an
unavailable store answers `unknown` and the loop continues, because the
verifier sweep re-binds from the occurrence side.
"""
from __future__ import annotations

import asyncio

from temporalio import activity

from orchestrator.security_findings import landed
from orchestrator.security_findings.client import UNKNOWN, SecurityFindingsClient


def _bind(workflow_id: str, target_repo: str, pr_url: str, merge_commit: str) -> list[dict[str, str]]:
    try:
        outcomes = landed.bind_landed(
            SecurityFindingsClient(),
            workflow_id=workflow_id,
            target_repo=target_repo,
            pr_url=pr_url,
            merge_commit=merge_commit,
        )
    except Exception as exc:  # noqa: BLE001 — "unknown, never an exception" is this activity's contract
        print(f"warn: security_record_landed failed for {workflow_id}: {type(exc).__name__}: {exc}")
        return [landed.LandedOutcome("", UNKNOWN).to_dict()]
    return [o.to_dict() for o in outcomes]


@activity.defn
async def security_record_landed(
    workflow_id: str, target_repo: str, pr_url: str, merge_commit: str
) -> list[dict[str, str]]:
    return await asyncio.to_thread(_bind, workflow_id, target_repo, pr_url, merge_commit)
