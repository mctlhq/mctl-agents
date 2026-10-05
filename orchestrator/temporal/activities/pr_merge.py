"""Activity: the shepherd's merge, gated behind a human approval
(mctlhq/mctl-agents#519, docs/adr/016-shepherd-merge-approval.md).

The merge side effect moves out of the per-tick shepherd pod and into this
gated Temporal activity, owned by `DevLoopWorkflow`'s merge watch — the one
durable execution that can span the whole human wait
(`orchestrator/temporal/workflows/action_approval.py::run_gated_action`).

Order of work, all of it recomputed from GitHub in THIS call (never trusted
from the caller beyond the head SHA it asked about):

1. The service is in `run_shepherd.NEVER_MERGE_SERVICES`: nothing is ever
   merged here, so it answers before any network call -- `merge_gate_disabled`
   with the gate off, `merge_forbidden` with it on (no human is ever asked
   to approve a merge the code forbids anyway).
2. Read the PR snapshot. With the gate off (no
   `MCTL_POLICY_MERGE_APPROVAL=require`, or the service is not in
   `SHEPHERD_MERGE_APPROVAL_SERVICES`) the call ends here as
   `merge_gate_disabled` UNLESS the PR changes agent definitions
   (`run_shepherd.merge_operation_for`, mctlhq/mctl-agents#470): that merge
   is a human decision whatever the gate says, and this activity is the
   only path that can carry one, so it goes on. An unreadable snapshot is
   `merge_precondition_unmet`, never "gate disabled": it could not be
   observed whether the PR is such a PR.
3. Read the codex review and the required-check status.
4. Any precondition the merge no longer meets (unreadable snapshot, merged,
   closed, draft, the head moved) -> `merge_precondition_unmet`, nothing
   created or consumed.
5. `run_shepherd.decide()` on that freshly read state must itself say
   `merge`; anything else is also `merge_precondition_unmet`. This keeps the
   settle window, the fresh-findings filter and the required-check gates in
   force at the moment of the merge, not at the moment of the request.
6. Only then, `run_gated()`: the checkpoint decides the operation
   `merge_operation_for` named (asking mctl-api for an approval when the
   rule requires one), and the side effect -- `run_shepherd.merge_pr_unchecked`
   under that same operation -- runs only on a permitted decision.
"""
from __future__ import annotations

import asyncio
import time
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

#: `run_gated`'s contract: the side effect retries its own transient errors
#: before raising, because an escape after the consume spends the human's
#: approval (and the watch then latches this head as `effect_failed`). A
#: non-zero `gh pr merge` that reaches the side effect is the transient set
#: -- a 5xx, a secondary rate limit, a token that lapsed, a mergeability
#: recompute in flight -- since the head-moved case is screened out above.
MERGE_EFFECT_ATTEMPTS = 3
#: Seconds to wait before attempts 2 and 3.
MERGE_EFFECT_BACKOFF_SECONDS = (5.0, 15.0)
#: Seam for tests.
_sleep = time.sleep


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

    gate_on = _gate_enabled(service)
    if service in run_shepherd.NEVER_MERGE_SERVICES:
        if not gate_on:
            return _blocked(CODE_MERGE_GATE_DISABLED, f"merge-approval gate is off for {service}")
        return _blocked(CODE_MERGE_FORBIDDEN, f"{service} merges are gated on a human CODEOWNER")

    pr = run_shepherd._fetch_pr_snapshot(repo, number)
    if pr is None:
        return _blocked(CODE_MERGE_PRECONDITION_UNMET, f"{repo}#{number}: PR snapshot unreadable")
    # The operation this merge is decided under: the plain `merge`, or the
    # agent-definition one, which requires an approval in every policy
    # variant and regardless of this service's gate (mctlhq/mctl-agents#470).
    operation = run_shepherd.merge_operation_for(pr)
    if not gate_on and operation == pc.MERGE_OPERATION:
        return _blocked(CODE_MERGE_GATE_DISABLED, f"merge-approval gate is off for {service}")
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
        for attempt in range(MERGE_EFFECT_ATTEMPTS):
            if attempt:
                _sleep(MERGE_EFFECT_BACKOFF_SECONDS[attempt - 1])
                # Re-read before retrying: a merge that actually landed
                # despite the non-zero exit is never attempted again, and a
                # head that moved is not merged under this approval (the
                # `match_head_commit` guard would refuse it anyway).
                snap = run_shepherd._fetch_pr_snapshot(repo, number)
                if snap is not None and snap.merged:
                    return {"merge_commit": snap.merge_commit or ""}
                if snap is not None and snap.head_sha != pr.head_sha:
                    raise RuntimeError(
                        f"{repo}#{number} head moved to {snap.head_sha} after the approved merge failed"
                    )
            # The operation rides along only when it is not the default,
            # so an ordinary merge calls the transport exactly as before.
            if operation == pc.MERGE_OPERATION:
                ok, merge_commit = run_shepherd.merge_pr_unchecked(pr)
            else:
                ok, merge_commit = run_shepherd.merge_pr_unchecked(pr, operation=operation)
            if ok:
                return {"merge_commit": merge_commit or ""}
        # Every escape here costs a human decision (run_gated's contract):
        # the receipt is already spent, so this is reported as
        # effect_error/effect_failed, never retried on this receipt.
        raise RuntimeError(
            f"gh pr merge failed {MERGE_EFFECT_ATTEMPTS} times for {repo}#{number} "
            "(see activity log for the gh output)"
        )

    return run_gated(
        inp,
        pc.GITHUB_PR_MERGE,
        operation,
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
