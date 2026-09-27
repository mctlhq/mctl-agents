"""Activity: the shepherd's merge, gated behind a human approval
(mctlhq/mctl-agents#519, docs/adr/017-shepherd-merge-approval.md).

The merge side effect moves out of the per-tick shepherd pod and into this
gated Temporal activity, owned by `DevLoopWorkflow`'s merge watch — the one
durable execution that can span the whole human wait
(`orchestrator/temporal/workflows/action_approval.py::run_gated_action`).

Order of work, all of it recomputed from GitHub in THIS call (never trusted
from the caller beyond the head SHA it asked about):

1. Gate off (no `MCTL_POLICY_MERGE_APPROVAL=require`, or the service is not
   in `SHEPHERD_MERGE_APPROVAL_SERVICES`) -> `merge_gate_disabled`, before
   any network call.
2. The service is in `run_shepherd.NEVER_MERGE_SERVICES` -> `merge_forbidden`,
   before the checkpoint: no human is ever asked to approve a merge the code
   forbids anyway.
3. Read the PR snapshot, the codex review and the required-check status.
4. Any precondition the merge no longer meets (unreadable snapshot, merged,
   closed, draft, the head moved) -> `merge_precondition_unmet`, nothing
   created or consumed.
5. `run_shepherd.decide()` on that freshly read state must itself say
   `merge`; anything else is also `merge_precondition_unmet`. This keeps the
   settle window, the fresh-findings filter and the required-check gates in
   force at the moment of the merge, not at the moment of the request.
6. Only then, `run_gated()`: the checkpoint decides (asking mctl-api for an
   approval when the merge-approval policy applies), and the side effect —
   `run_shepherd.merge_pr_unchecked` — runs only on a permitted decision.
"""
from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity

from orchestrator import policy_checkpoint as pc
from orchestrator import run_shepherd
from orchestrator.ci_checks import read_required_checks
from orchestrator.temporal.activities.action_approval import GatedActionInput, GatedActionResult, run_gated

#: Answered before any network call when the gate is off for this call's
#: service. `outcome_of()` (action_approval.py) maps an unrecognised code
#: to `blocked`, which every caller already treats as "nothing to do".
CODE_MERGE_GATE_DISABLED = "merge_gate_disabled"
#: The service is in `run_shepherd.NEVER_MERGE_SERVICES`: refused before the
#: checkpoint, independent of policy.
CODE_MERGE_FORBIDDEN = "merge_forbidden"
#: The PR, recomputed just now, is not in a mergeable state, or the
#: shepherd's own `decide()` does not currently say `merge`.
CODE_MERGE_PRECONDITION_UNMET = "merge_precondition_unmet"


def _blocked(code: str, reason: str) -> GatedActionResult:
    return GatedActionResult(code=code, reason=reason)


def _gate_enabled(service: str) -> bool:
    """True only when the merge-approval policy is actually in force AND
    this service opted in. Both conditions, independently: the policy env
    alone would gate every service at once, the service list alone would do
    nothing under the unchanged ALLOW rule."""
    return pc.configured_policy() is not pc.BUILTIN_POLICY and service in run_shepherd.SHEPHERD_MERGE_APPROVAL_SERVICES


def _sync_merge_pull_request_gated(inp: GatedActionInput) -> GatedActionResult:
    payload = inp.payload
    repo = str(payload.get("repo") or "")
    try:
        number = int(payload.get("pr_number") or 0)
    except (TypeError, ValueError):
        number = 0
    head_sha = str(payload.get("head_sha") or "")
    service = str(payload.get("service") or "")

    if not repo or not number or not head_sha or not service:
        return _blocked(CODE_MERGE_PRECONDITION_UNMET, "payload missing repo, pr_number, head_sha or service")

    if not _gate_enabled(service):
        return _blocked(CODE_MERGE_GATE_DISABLED, f"merge-approval gate is off for {service}")

    if service in run_shepherd.NEVER_MERGE_SERVICES:
        return _blocked(CODE_MERGE_FORBIDDEN, f"{service} merges are gated on a human CODEOWNER")

    pr = run_shepherd._fetch_pr_snapshot(repo, number)
    if pr is None:
        return _blocked(CODE_MERGE_PRECONDITION_UNMET, f"{repo}#{number}: PR snapshot unreadable")
    if pr.merged:
        return _blocked(CODE_MERGE_PRECONDITION_UNMET, f"{repo}#{number} is already merged")
    if pr.closed_unmerged:
        return _blocked(CODE_MERGE_PRECONDITION_UNMET, f"{repo}#{number} is closed without merging")
    if pr.is_draft:
        return _blocked(CODE_MERGE_PRECONDITION_UNMET, f"{repo}#{number} is a draft")
    if pr.head_sha != head_sha:
        return _blocked(
            CODE_MERGE_PRECONDITION_UNMET,
            f"{repo}#{number} head moved: asked about {head_sha}, now {pr.head_sha}",
        )

    codex = run_shepherd.read_codex_review(pr)
    ci = read_required_checks(pr)
    decision, _payload = run_shepherd.decide(pr, codex, ci=ci)
    if decision != "merge":
        return _blocked(
            CODE_MERGE_PRECONDITION_UNMET, f"{repo}#{number}: decide() returned {decision!r}, not merge",
        )

    pr_ref = f"https://github.com/{repo}/pull/{number}"

    def _side_effect() -> dict[str, Any] | None:
        ok, merge_commit = run_shepherd.merge_pr_unchecked(pr)
        if not ok:
            # Every escape here costs a human decision (run_gated's
            # contract): the receipt is already spent, so this is reported
            # as effect_error/effect_failed, never retried on this receipt.
            raise RuntimeError(f"gh pr merge failed for {repo}#{number} (see activity log for the gh output)")
        return {"merge_commit": merge_commit or ""}

    return run_gated(
        inp,
        pc.GITHUB_PR_MERGE,
        "merge",
        pr_ref,
        {"method": "merge", "delete_branch": True, "match_head_commit": pr.head_sha},
        _side_effect,
        metadata={"repo": repo, "pr": str(number), "head_sha": pr.head_sha},
        policy=pc.configured_policy(),
    )


@activity.defn
async def merge_pull_request_gated(inp: GatedActionInput) -> GatedActionResult:
    """The gated activity `DevLoopWorkflow`'s merge watch runs through
    `run_gated_action`. All I/O is synchronous `gh`/GraphQL calls under the
    hood (same readers the shepherd tick uses), so it runs in a thread."""
    return await asyncio.to_thread(_sync_merge_pull_request_gated, inp)
