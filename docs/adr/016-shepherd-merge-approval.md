# ADR 016 — Gate the shepherd's merge behind action-approval

> **Status:** accepted
> **Date:** 2026-09-27
> **Issue:** mctlhq/mctl-agents#519

## Context

`orchestrator/run_shepherd.py` merges its own PRs: `merge_pr()` checkpoints
`GITHUB_PR_MERGE` under `BUILTIN_POLICY` (always `ALLOW`) and then runs
`gh pr merge --merge --delete-branch --match-head-commit <SHA>`. #519 asks
for a way to require a human decision before that merge runs, for services
that opt in, without weakening the checkpoint's existing single-decision-
point design (ADR-014).

**Why #484's `ApprovalTicket` is not repairable.** #484 tried to gate the
merge from inside the shepherd pod itself, persisting an `ApprovalTicket`
into `.status.yaml` across ticks so a later tick could redeem it. Each
shepherd tick, though, runs in a fresh Argo pod with a fresh sealed
execution context: `execution_id` is minted per pod and is itself a field
of the approval intent hash (`orchestrator/action_approvals.py`'s
`ActionIntent`, reproducing mctl-api's `IntentHash`). A ticket created by
tick N's `execution_id` can never be redeemed by tick N+1, because tick
N+1 recomputes a different intent hash and `MctlApiApprovals.redeem`
answers `mismatch`, not `approved`. Making the ticket idea work would
require either dropping `execution_id` from the intent — weakening the
binding mctl-api enforces on every other approval too — or minting a
synthetic long-lived identity shared across unrelated pods, which names no
real execution. It would also put approval state in a gitops file that the
reconciler, the implementer and the steward all write, which is exactly
the kind of field mctlhq/mctl-agents#344 warns gets misread as a merge
authorization. #484's ticket design is dropped for this reason; only its
evidence plumbing (`Decision.approver`, `Decision.decided_at`,
`ApprovalOutcome.decided_by`) carries forward, rewired onto the new flow.

## Decision

### 1. A merge-approval policy variant, selected by an env var

`orchestrator/policy_checkpoint.py` adds, next to `configured_approvals()`:

- `MERGE_APPROVAL_ENV = "MCTL_POLICY_MERGE_APPROVAL"`, recognized value
  `MERGE_APPROVAL_REQUIRE = "require"`.
- `MERGE_APPROVAL_POLICY`: `BUILTIN_POLICY` with its `github-pr-merge` rule
  replaced by `Rule("github-pr-merge-approval", GITHUB_PR_MERGE, "merge",
  REQUIRE_APPROVAL)`, under its own `policy_version`
  (`mctl-agents/policy/v1-merge-approval`). The version is deliberately
  distinct: `policy_version` is part of the approval intent hash, so a
  receipt approved under one rule set can never be spent under the other.
- `configured_policy() -> Policy`: `BUILTIN_POLICY` when the env is unset,
  empty or `none` (today's behaviour, unchanged byte for byte);
  `MERGE_APPROVAL_POLICY` for `require`; any other value fails closed to a
  policy whose merge rule is `DENY`, mirroring the existing
  `_MisconfiguredApprovals` rule for `MCTL_POLICY_APPROVALS`.

`configured_policy()` is passed as `policy=` only by the two call sites
built for this feature (the gated activity and, before this decision was
tightened, `run_shepherd.merge_pr`'s own checkpoint). Every other governed
path keeps `BUILTIN_POLICY`, so nothing else changes.

### 2. The merge side effect moves into a gated Temporal activity

`orchestrator/temporal/activities/pr_merge.py` adds
`merge_pull_request_gated`, owned by `DevLoopWorkflow`, not the shepherd
pod. All reads run in a thread (`asyncio.to_thread`) because the
underlying calls are synchronous `gh` subprocesses. In order:

1. **Gate off** — `configured_policy() is BUILTIN_POLICY`, or the payload's
   `service` is not in `SHEPHERD_MERGE_APPROVAL_SERVICES` — returns
   `CODE_MERGE_GATE_DISABLED` before any network call.
2. **Never-merge repos** — `service in run_shepherd.NEVER_MERGE_SERVICES`
   returns `CODE_MERGE_FORBIDDEN` before the checkpoint, so no human is
   ever asked to approve a merge the code already refuses.
3. **Recompute the world**: `run_shepherd._fetch_pr_snapshot`,
   `run_shepherd.read_codex_review`, `ci_checks.read_required_checks` —
   never trusted from the caller beyond the head SHA it asked about.
4. **Preconditions** unmet (unreadable snapshot, merged, closed unmerged,
   draft, or the head moved since the caller's snapshot) return
   `CODE_MERGE_PRECONDITION_UNMET`, with nothing created or consumed.
5. **The shepherd's own decision**: `run_shepherd.decide(pr, codex, ci=ci)`
   must itself say `merge`; anything else is also
   `CODE_MERGE_PRECONDITION_UNMET`. This keeps the settle window, the
   fresh-findings filter and the required-check gates in force at the
   moment of the merge, not at the moment of the original request.
6. Only then does `run_gated()` run: the checkpoint decides (asking
   mctl-api for an approval under the merge-approval policy), and the side
   effect — `run_shepherd.merge_pr_unchecked`, one `gh pr merge` plus the
   re-read for the merge commit oid — runs only on a permitted decision.

`merge_pull_request_gated` is registered in `orchestrator/temporal/worker.py`
beside `read_action_approval`, on the control queue: it is a handful of
bounded GitHub reads plus, on a permitted decision, one `gh pr merge` — the
same shape as the shepherd's own per-tick merge.

### 3. The shepherd stops merging for gated services

`orchestrator/run_shepherd.py` adds
`SHEPHERD_MERGE_APPROVAL_SERVICES = _service_set_from_env(...)` (same
comma/whitespace parsing and typo warning as `SHEPHERD_SKIP_SERVICES`;
unset = empty = today's behaviour) and:

```python
def _merge_gate_delegated(service: str) -> bool:
    return (
        policy_checkpoint.configured_policy() is not policy_checkpoint.BUILTIN_POLICY
        and service in SHEPHERD_MERGE_APPROVAL_SERVICES
    )
```

Both conditions independently, mirroring `pr_merge._gate_enabled`: the
policy env alone would gate every service sharing the pod at once, and the
service list alone would do nothing while `configured_policy()` is still
`BUILTIN_POLICY`. `merge_pr` is split into `merge_pr_unchecked` (the `gh`
invocation and snapshot re-read, shared with the gated activity's side
effect) and `merge_pr` itself, which keeps the checkpoint wrapper and, for
a gated service, prints `MERGE_GATED` and returns `(False, None)` before
the checkpoint even runs — defence in depth so a shepherd pod can never
create an approval request itself. The tick's merge arm treats a gated
service like fix-only for the purpose of `decide()`, so it yields
`defer-merge` and `merge_pr` is never reached; review and follow-up fixes
continue exactly as for any other service. Nothing about approvals is read
or written by the shepherd — no ticket, no attempt counter, no denial
counter.

### 4. `DevLoopWorkflow` becomes the first production caller of `run_gated_action`

In `_watch_pr`'s poll loop, guarded by `workflow.patched(MERGE_GATE_PATCH)`
so no in-flight execution's history replays a command it never recorded:

- **Identity.** `_merge_gate_identity()` mints one execution context via
  `mint_execution_context` on the first gate attempt of this watch and
  caches it (`self._merge_gate_execution_id` / `_trace_id`), carried across
  a `continue_as_new` hop through `MergeWatchResume`'s
  `merge_gate_execution_id` / `merge_gate_trace_id` fields — the property
  #484's per-tick pods could never have.
- **The call is concurrent, never inline.** While the polled `PRState` is
  open and no gate task is in flight, the loop starts
  `gate_task = asyncio.create_task(self._merge_gate(...))`, which calls
  `run_gated_action(MERGE_GATE_ACTIVITY, GatedActionInput(...))` — the same
  background-task pattern the loop already uses for the in-loop shepherd
  tick (`tick_task`). The poll loop never awaits it: it keeps reading
  `get_pr_state`, submitting shepherd ticks, heartbeating the
  lifecycle-ownership claim and honouring `abandon` while a human decides,
  for as long as days.
- **Outcomes**, applied by `_apply_gate_outcome` once `gate_task.done()`:

  | outcome | action |
  | --- | --- |
  | `ran` | log `MERGE_APPROVED` with `approval_ref`, approver, `decided_at`; the next poll observes `MERGED` via the ordinary `get_pr_state` path |
  | `blocked` (gate off, forbidden, precondition unmet, `approval_required`) | nothing; keep watching |
  | `mismatch` | the head moved; the next poll asks about the new head, a new intent and a new human decision |
  | `denied`, `expired`, `timed_out` | log and keep watching; never call `next_attempt()` automatically, so a denial is never worn down by asking again |
  | `consumed`, `effect_failed` | never merge again on that receipt; keep watching so a human sees it |
  | `already_waiting`, `undecided` | nothing this poll |

  The `ran` outcome is logged from two places: the ordinary poll-boundary
  check further down the loop, and — because a successful gated merge's
  own `gh pr merge` call means the very next poll observes `MERGED` before
  that check ever runs — the loop's MERGED/CLOSED branch also drains and
  applies a just-finished `gate_task` before returning, so `MERGE_APPROVED`
  is not lost on the one path it exists to record.
- **Settling.** The watch's existing `finally` (`_settle_gate`, mirroring
  `_settle_tick`) cancels and awaits any still-in-flight `gate_task` on
  every exit — `MERGED`/`CLOSED`, the mctl-agents#516 terminal exit, the
  deadline, `abandon`. Cancellation propagates into `run_gated_action`'s
  child `ActionApprovalWaitWorkflow`; an unconsumed receipt simply expires.

## Consequences

Zero behaviour change until gitops sets both
`MCTL_POLICY_MERGE_APPROVAL=require` (worker and shepherd CWFT) and
`SHEPHERD_MERGE_APPROVAL_SERVICES=<service>`: the gated activity's first
check short-circuits, so an unset flag costs one cheap activity per
15-minute poll. `MCTL_POLICY_APPROVALS=mctl-api` must also be set on the
worker for an approval request to be created at all; without it the gate
is `approval_required` -> `blocked` -> no merge, which fails closed rather
than open. `workflow.patched(MERGE_GATE_PATCH)` means in-flight executions
never enter the gate; only executions started after the deploy do.

Per gated merge-watch poll, the activity performs roughly the same GitHub
reads the shepherd tick already does (PR snapshot, reviews, review
comments, checks) — about a doubling of that PR's read traffic while the
gate is on. The wait itself holds no pod and no activity: its cost is one
durable timer plus one read-only GET every 15 minutes, bounded by the
receipt's own expiry and the remaining merge-watch budget
(`MERGE_WATCH_DEADLINE`, 14 days).

A gated service with no live `DevLoopWorkflow` watching its PR simply does
not merge; the `MERGE_GATED` line printed by `run_shepherd.merge_pr` is the
greppable signal for an operator to fall back to a manual merge. A policy
flip mid-wait invalidates any pending receipt as `mismatch`, which is
logged and recoverable by re-approving under the new policy, not silently
lost.
