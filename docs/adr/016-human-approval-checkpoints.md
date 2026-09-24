# ADR 016 — Human approval checkpoints: one contract, two wait drivers

> **Status:** accepted
> **Date:** 2026-09-24
> **Issue:** mctlhq/mctl-agents#198 (builds on #197/ADR 014, mctl-api#366)

## Context

ADR 014 placed the runtime policy checkpoint and defined `REQUIRE_APPROVAL`
as a verdict a durable approval store can turn into a permit
(`orchestrator/action_approvals.py`, mctl-api#366). ADR 014 §7
(mctl-agents#479) then built the Temporal half of the wait:
`run_gated_action()` and `ActionApprovalWaitWorkflow`
(`orchestrator/temporal/workflows/action_approval.py`) park a Temporal
activity on `approval_pending` without holding a pod, wake on a signal or a
bounded poll, and let only a re-run of the checkpoint
(`checkpoint(..., approval_ref=...)`) authorize the side effect.

That covers every Temporal-hosted step. It does not cover the issue's own
first candidate: the shepherd's PR merge
(`orchestrator/run_shepherd.py::merge_pr`). The shepherd is cron-driven, not
a Temporal workflow (`docs/temporal-flow.md`; ADR-006 phase 6, tracker
#217), so it cannot start a child workflow to wait on. This ADR is that
second driver, plus the parts of the contract that are shared regardless of
which driver parks an action: the durable ticket's shape, the outcome
table, and the approver record on the trace.

## Decision

**One contract, two wait drivers, and an authorization boundary that does
not move.**

### The contract

Every driver produces the same thing when a decision is `awaiting_approval`
(`Decision.code == approval_pending`): a receipt (`approval_ref`) a human
can decide in mctl-api, and enough context — action kind, target, policy
rule, reason, trace id — for a human or a surface to decide it without
reading a transcript. Only mctl-api's `consume` is authorization; a
driver's own belief about the store's state (a workflow query, a
`.status.yaml` block) is advisory and is always revalidated by re-running
`checkpoint(..., approval_ref=...)` before the side effect. This is
unchanged from ADR 014 §6 and is not renegotiated here.

### Driver 1: the Temporal wait (built, #479)

`run_gated_action()` / `ActionApprovalWaitWorkflow`. See ADR 014 §7 for the
built shape; nothing about it changes in this ADR.

### Driver 2: the cron ticket (this proposal)

The shepherd's tick already re-reads GitHub and `.status.yaml` every cycle
— it IS a poll loop that holds nothing between firings. So its wait needs
no timer, only durable ticket storage and re-entry:

- `orchestrator/approval_ticket.py` — a stdlib-only, frozen `ApprovalTicket`
  built by `ticket_from(decision, request, record)` from an
  `awaiting_approval` `Decision`, the `ActionRequest` that produced it, and
  the `ApprovalRecord` of one read-only `ActionApprovalClient.get()`. It
  carries identifiers, a policy reason and one caller-supplied
  `artifact_ref` (the PR's head SHA) — never a raw action argument.
- `orchestrator/proposal_state.py` gains an additive `approval` block in
  `.status.yaml`: `{"ticket": <ApprovalTicket.to_json() | null>, "denials":
  int, "attempt": int}`. Every reader tolerates its absence; there is no
  schema version bump and no backfill.
- `run_shepherd.merge_pr(pr, ref=None)` learns one optional parameter. With
  no `ref` (every existing caller and every existing test), an
  `awaiting_approval` decision is refused exactly as it was before this
  proposal — `merge_pr`'s `(False, None)` contract is unchanged. With
  `ref` (the one production call site in `process_one`), an
  `awaiting_approval` decision instead builds and persists the ticket,
  logs `APPROVAL_PARKED`, and returns the same `(False, None)`. On a later
  tick with a stored ticket, `merge_pr` calls
  `checkpoint(GITHUB_PR_MERGE, ..., approval_ref=ticket.approval_ref)`.
  Because the merge's arguments already bind the head SHA (ADR 014 §5), a
  push between request and decision recomputes a different intent hash,
  so the receipt is refused as `approval_intent_mismatch` and the stale
  ticket is cleared — "an approval cannot be reused for a materially
  different action" falls out of the existing binding, not new logic.

### The outcome table

`policy_checkpoint`'s `Decision.code` is the only thing either driver
branches on; the approval store's own state vocabulary
(`action_approvals.PENDING` / `APPROVED` / ...) never leaks into a driver's
control flow — it only fills in ticket fields.

| Outcome | Cron driver (`merge_pr`) | Temporal driver (#479, for reference) |
|---|---|---|
| `approved` | merge runs once; ticket cleared | step resumes |
| `pending` | stays parked, ticket kept | stays parked |
| `denied` | ticket kept, `denials += 1`; at `MERGE_APPROVAL_DENIAL_LIMIT` (3), `needs-triage` with `failure.code: approval-denied` | step ends per workflow policy; `next_attempt()` for a deliberate re-request |
| `expired` | ticket cleared; `attempt += 1` so the next create-or-find call gets a genuinely fresh, human-decidable request rather than the same expired receipt (mctl-api replays an idempotency key verbatim) | step ends; `next_attempt()` |
| `intent_mismatch` | ticket cleared (no attempt bump: a new head is already a new idempotency key); a later tick opens a fresh request for the current head | re-request |
| `consumed` | never re-merges; re-reads canonical PR state (`_fetch_pr_snapshot`, the same primitive `orchestrator/pr_adoption.py` uses) and reconciles: merged -> reports success without a second `gh pr merge`; not merged -> the accepted safe failure (ADR 014 §6), ticket cleared so a fresh approval can be requested | treated as already run; never re-run |
| `lookup_error` | ticket untouched; stays `undecided`, retried on the receipt's own deadline | retried with backoff while the timer runs |

### The approver record on the trace

`ApprovalOutcome` (the `ApprovalLookup` protocol's return value) gains
`decided_by`, populated from `ApprovalRecord.decided_by` for every outcome
where the store names one — approved, denied, and a locally-detected
expiry — but deliberately NOT for a mismatch, whose receipt belongs to a
different intent and must never lend its approver to the action being
decided now. `policy_checkpoint.Decision` gains `approver` and
`decided_at` (a wall-clock stamp of when this process observed the
decision, not a server-side timestamp mctl-api does not yet carry).
`decision_record()` (the stdout audit line) and
`tracing.record_policy_decision()` (the `mctl.policy.decision` span event,
as `mctl.policy.approval_ref` / `mctl.approval.approver` /
`mctl.approval.decided_at`) both carry them. All three keys already match
`tracing_sdk`'s `_ALLOWED_KEY` allowlist's generic `mctl.*` namespace
pattern — no allowlist change was needed, and a test
(`tests/test_tracing_agents.py::
test_a_granted_approval_decision_carries_approver_and_timestamp_through_export`)
pins that a future narrowing of the pattern cannot silently drop them
again. Since both drivers redeem through the same `checkpoint(...,
approval_ref=...)` call, the same fields populate identically regardless
of which driver resumed the action.

Two new structured log lines, `APPROVAL_PARKED` and `APPROVAL_RESUMED`
(plus `APPROVAL_DENIED` / `APPROVAL_CLEARED` for the cron driver's other
transitions), follow `orchestrator/lifecycle/claim.py`'s `_emit`
convention, so time-to-decision is derivable from the log without a trace
backend.

### Two approvals, deliberately not one

The proposal-level `approve` signal (`dev_loop.approve`, `proposed ->
accepted`) and this action-level approval are different things at
different layers with different identity guarantees:

- The proposal-level signal accepts an unverified free-text `approver` from
  any signaller — `dev_loop.approve` defaults it to `"unknown"` when the
  CLI sends none.
- The action-level approval's `approver` is `ApprovalRecord.decided_by`,
  as mctl-api itself recorded against an authenticated decision on a named
  receipt.

Naming is kept disjoint throughout (`action_approval_decided`,
`ApprovalTicket`, `APPROVAL_PARKED`) precisely so an operator reading a log
or a trace never has to guess which layer a given "approved" refers to.

## What is still not built

- **The mctl-api side of the signal** (mctl-api#381): after a decision,
  `POST /action-approvals/{id}/decision` should signal
  `action_approval_decided` to `action-approval-<id>`, best effort. Until
  it lands, the Temporal driver resolves on its poll
  (`DEFAULT_POLL_SECONDS`, 15 min) and the cron driver is unaffected (it
  never depended on the signal — its own tick is its poll).
- **Human approval surfaces.** mctl-api already exposes `GET
  /action-approvals`, `GET /action-approvals/{id}` and `POST
  /action-approvals/{id}/decision`; nothing renders a pending ticket to a
  human yet. Until a surface exists, a parked merge is visible in
  `.status.yaml`'s `approval` block, in the `APPROVAL_PARKED` log line, and
  in `GET /action-approvals?status=pending`. Tracked as a follow-up issue
  in mctl-api (an MCP tool pair to list pending approvals and submit a
  decision, reading `ApprovalTicket` fields), cross-linked to mctl-api#381
  and mctlhq/mctl-agents#198.
- **A first Temporal adopter of `run_gated_action`.** No DevLoop step
  reaches `REQUIRE_APPROVAL` at the Temporal level today; ADR 014's open
  decision 1 tracks this separately and is unchanged by this ADR.

## Rollout and rollback

Default-off twice over, unchanged from ADR 014 §5/§6: the store must be
enabled (`MCTL_POLICY_APPROVALS=mctl-api`) AND a rule must be flipped to
`REQUIRE_APPROVAL` (the built-in policy keeps `github-pr-merge: ALLOW`).
Three independent, additive levers to roll back, coarsest first: revert the
rule to `ALLOW`; unset `MCTL_POLICY_APPROVALS`; revert the code (a new
module, an additive `.status.yaml` block, a branch in `merge_pr`, new
optional record fields — no Temporal workflow definition changes, so no
replay exposure).

## Consequences

Enabling `REQUIRE_APPROVAL` for `github-pr-merge` on one service parks
that service's merges until a human decides the receipt named in
`.status.yaml`'s `approval` block or in `GET
/action-approvals?status=pending`; every other service, and every other
governed action, is unaffected. A parked merge holds no pod, no Argo
workflow and no model session, and its worst-case cost is one `GET
/action-approvals/{id}` per shepherd tick on mctl-api's read budget.
