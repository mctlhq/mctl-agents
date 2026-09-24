# Runbook: human approval checkpoints (mctl-agents#198, ADR 016)

For an operator enabling, deciding, or disabling a `REQUIRE_APPROVAL`
governed action. See `docs/adr/014-policy-checkpoint.md` and
`docs/adr/016-human-approval-checkpoints.md` for the design; this page is
only the how-to.

## Enable the first governed path (the shepherd's PR merge)

Two independent switches; both must be on for anything to change.

1. **Turn on the durable approval store**, per environment/service:

   ```
   MCTL_POLICY_APPROVALS=mctl-api
   ```

   With this unset, empty, or `none`, every `REQUIRE_APPROVAL` rule keeps
   blocking exactly as it always has — no HTTP call is ever made, and
   nothing in this runbook applies yet.

2. **Flip the `github-pr-merge` rule to `REQUIRE_APPROVAL`** for the one
   service you want to gate. The built-in policy
   (`orchestrator/policy_checkpoint.py::BUILTIN_POLICY`) ships that rule as
   `ALLOW`; gating it is a policy change, scoped to the service(s) you name,
   not a code change or a new default. Do this last, after step 1 is
   confirmed live, so the first parked merge is never a surprise.

Optional knob: `MCTL_POLICY_APPROVAL_TTL_S` (default 24h, mctl-api caps at
7 days) — how long a newly-created approval request stays decidable before
it expires.

## Find a parked merge

A parked merge never shows up as a normal PR-review blocker; it shows up as
one of:

- The proposal's `.status.yaml` carries an `approval` block:

  ```yaml
  approval:
    ticket:
      approval_ref: aar_...
      target: https://github.com/<org>/<repo>/pull/<n>
      reason: "rule github-pr-merge: needs an approval bound to ..."
      expires_at: "2026-...Z"
      artifact_ref: <head sha>
      ...
    denials: 0
    attempt: 0
  ```

- The shepherd's tick log carries a line: `APPROVAL_PARKED pr=<url>
  ref=<approval id> trace_id=<id>`.
- `GET /action-approvals?status=pending` on mctl-api lists the same
  receipt (`ApprovalTicket.approval_ref`).

There is no dedicated UI yet (tracked as a mctl-api follow-up in ADR 016);
until one exists, these three are the only surfaces.

## Decide it

```
POST /action-approvals/{id}/decision
{"decision": "approve"}   # or "deny"
```

The next shepherd tick (cron cadence, no manual trigger needed) re-reads
the receipt and:

- **approved** — merges the PR once (still bound to the head SHA the
  ticket recorded: a push since the request was opened makes this an
  `approval_intent_mismatch` instead, and a fresh request opens for the
  new head), logs `APPROVAL_RESUMED`, and clears the ticket.
- **denied** — stays parked, `approval.denials` increments. After 3 denials
  of the same receipt the proposal moves to `needs-triage` with
  `failure.code: approval-denied` — an operator must re-approve (a fresh
  request) or close the PR from there; the shepherd stops re-asking it
  automatically.
- **left pending** — nothing changes; the next tick asks again.

If `expires_at` passes with no decision, the ticket clears itself on the
next tick and a fresh request opens the next time the merge is attempted.

## Disable it

Coarsest first, both take effect in seconds with no deploy:

1. Revert the `github-pr-merge` rule to `ALLOW`. Any already-parked merge
   resolves as an ordinary allowed merge on its next tick.
2. Unset `MCTL_POLICY_APPROVALS`. Every `REQUIRE_APPROVAL` rule reverts to
   blocking with no HTTP call — the pre-#198 behaviour, for both the
   Temporal wait and this cron driver.

A stranded `approval` block left behind in `.status.yaml` is inert: every
reader tolerates its absence or presence equally, so it needs no manual
cleanup.
