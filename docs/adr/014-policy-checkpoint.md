# ADR 014 — Runtime policy checkpoint for agent actions

> **Status:** proposed
> **Date:** 2026-09-23
> **Issue:** mctlhq/mctl-agents#197 (related: #195 tracing, #196 execution identity, #198 approvals; mctl-api#366)

## Context

Agents act on GitHub and on mctl through static credentials, and the only
thing that shapes what they do with them is prompt text. #197 asks for one
runtime decision point with three outcomes, evaluated outside any prompt,
fail-closed, and recorded.

## Decision

### 1. Where it sits

The owner's decision: the canonical enforcement boundary is **mctl-agents'
own action-execution boundary, immediately before the external side
effect**. It is not the MCP gateway, and it is not each business function.

```text
ActionRequest -> policy_checkpoint.decide() -> ALLOW | DENY | REQUIRE_APPROVAL
              -> transport (gh / httpx / MCP) runs only on a permitted decision
```

`orchestrator/policy_checkpoint.py` is stdlib-only, so the Temporal worker,
the pollers and the SDK hooks all use the same code.

### 2. Input

An `ActionRequest` carries:

- the execution identity: `execution_id` and `trace_id` from the sealed
  #196 context, plus the actor as `type:id`;
- `action_kind`, e.g. `github.issue.comment`, `mctl.operation.execute`,
  `mcp.tool.call`;
- `operation`, the normalized operation: a tool name, or an operation id;
- `target`;
- `args_digest`, the sha256 of the canonical arguments. The arguments
  themselves never enter the request;
- safe `metadata`;
- `grants`, the executing agent's allow-list;
- the policy version, which comes from the policy.

Only a context read from `MCTL_EXECUTION_CONTEXT_FILE` counts as an
identity. A locally minted fallback context gets a new id on every call,
which would make the digest unstable. When `MCTL_REQUIRE_EXECUTION_CONTEXT`
is set and there is no context, the request is refused.

### 3. Outcomes and failure behaviour

- The first matching rule wins. When no rule matches, the outcome is DENY
  (`no_matching_rule`).
- A rule with `requires_grant` refuses an operation that none of the grants
  match (`grant_missing`).
- The following are all DENY with a typed code, and `checkpoint()` never
  raises: an evaluator error, an undescribable request, arguments that
  cannot be digested, a failed approval lookup, and a missing identity in
  require mode.
- A REQUIRE_APPROVAL is permitted only when the approval store has just
  spent, for this call, an approved receipt bound to the exact action (§6).
  The binding covers kind, operation, target, args digest, execution id and
  actor. Changing any argument, or the execution, or the actor, produces a
  different action, and an approval for the old one does not cover it.

### 4. Record

Every decision prints one `POLICY_DECISION <json>` line containing:

- the execution id, trace id and actor;
- the action kind, operation and target;
- the action digest and args digest;
- the policy version and rule id;
- the decision, code and reason;
- the approval ref.

The record contains no raw arguments. #195's trace does not exist yet, so the
line follows the existing structured-log convention
(`lifecycle/claim.py`'s `_emit`), and its fields are the decision's trace
attributes when #195 lands.

A permitted REQUIRE_APPROVAL is recorded as `decision: REQUIRE_APPROVAL`,
`code: approved`, with the approval ref. The verdict is the rule's, and the
code says what happened to it. Whether the action ran is therefore
`code in {allowed, approved}`, never the `decision` field alone.

### 5. Governed paths in this slice

- **MCP.** Every `mcp__.*` tool call made by the service agent, the
  implementer, the incident responder and the issue investigator (both
  builders) passes through a `PreToolUse` hook, `options._PolicyCheckpointHook`.
  The grants it evaluates against are the builder's own `allowed_tools`. Any
  exception inside the hook is a deny.
- **GitHub mutation and an mctl-api operation.** In the directive poller,
  `_post_reply` (`gh issue comment`) and `submit_investigate` (`POST
  /operations/mctl-agents-investigate/execute`) decide before the transport
  runs. A refused dispatch is answered once, with its ack marker, and is not
  retried.

Built-in policy `mctl-agents/policy/v1`:

| Action | Decision |
|---|---|
| issue comments | ALLOW |
| the investigate operation | ALLOW |
| sealing this execution's context snapshot (`mctl.work_item.write`, `seal:context-snapshot`; insert-only in mctl-api, #431) | ALLOW |
| attaching this run's own engine run to its work item, or advancing that execution's phase (`mctl.work_item.write`, `attach:work-item-execution`; keyed by `(engine, engine_ref)` in mctl-api, #455) | ALLOW |
| mctl MCP reads (`get_`, `list_`, `read_`, `search_`, `describe_`, `whoami`, …) and the agent mutations `resolve_incident`, `acknowledge_incident` | ALLOW |
| every other granted mctl MCP tool, including any added to mctl-api later | REQUIRE_APPROVAL |
| anything else | DENY |

### 6. Durable single-use approvals (mctl-api#366)

The owner's decision: **mctl-api is the durable approval authority**;
Temporal is only the wait/resume mechanism, and its state is never the
approval record.

```text
checkpoint -> REQUIRE_APPROVAL -> ActionApprovalRequest in mctl-api (pending)
           -> a human decides in mctl-api
           -> checkpoint again: recompute the intent, revalidate the receipt,
              consume it (approved -> consumed, atomic)
           -> the side effect runs
```

`orchestrator/action_approvals.py` holds the client and the
`ApprovalLookup` backed by it (`MctlApiApprovals`).

- **Intent.** `intent_hash()` reproduces mctl-api's `IntentHash` byte for
  byte (pinned by the Go test's vector). The intent of a checkpoint request
  is `execution_id`, `action_kind` as `<kind>:<operation>`, `target`,
  `args_digest`, the matching rule id, the policy version, and, as
  `artifact_hash`, the checkpoint's own action digest, which adds the actor.
  An approval therefore binds at least what the digest binding did before.
  The create call sends the locally computed hash, so mctl-api refuses it
  (`intent_hash_mismatch`) if the two encodings ever disagree.
- **Find or create.** The idempotency key is derived from the intent hash:
  the same intent finds its request, and a different intent is a different
  request. mctl-api answers a replayed key with the stored request whatever
  its state, so a denied, expired or consumed request stays that way for
  that intent in that execution. `idempotency_key(intent, attempt)` leaves
  room for a deliberate re-request with a new human decision; only the
  #198 wait's `next_attempt()` (§7) ever asks above attempt 0.
- **Consume at decision time.** For a REQUIRE_APPROVAL action, `decide()`
  asks the lookup to redeem: recompute the intent hash, refuse unless the
  receipt's stored hash equals it (`approval_intent_mismatch`), refuse
  unless it is approved and unexpired, then consume it with the fresh hash.
  Only a consume that mctl-api confirms, for exactly that receipt and hash,
  permits (`code: approved`, `approval_ref` = the receipt id). A refused,
  malformed or uncertain consume refuses, so the side effect does not run.

  Why at decision time and not a grant object whose `consume()` the caller
  calls later: every governed path already treats `Decision.permitted` as
  "run now" and calls the checkpoint immediately before the side effect.
  Consuming inside the decision means there is no value meaning "approved
  but not yet consumed" that a caller could run on by mistake, and no new
  step that a future call site could forget. The failure it leaves is the
  safe one: a crash after the consume and before the effect burns the
  approval without acting, and it never acts twice.
- **Outcomes.** Each keeps the REQUIRE_APPROVAL verdict, and none permits:
  `approval_pending` (with the request id in `approval_ref`;
  `Decision.awaiting_approval`), `approval_denied`, `approval_expired`,
  `approval_consumed`, `approval_intent_mismatch` and `approval_refused`
  (the store refused the request itself, or there is no execution identity
  to bind it to). A store that cannot answer (transport error, 5xx, 408,
  425, 429, a malformed answer) is `approval_lookup_error`, a DENY that
  counts as undecided.
- **Revalidating a named receipt.** `checkpoint(..., approval_ref=...)`
  revalidates exactly the receipt a caller waited on, instead of finding it
  by key. A changed action is then `approval_intent_mismatch`, and nothing
  is consumed.
- **Off by default.** The lookup is `configured_approvals()`:
  `MCTL_POLICY_APPROVALS` unset, empty or `none` is `NO_APPROVALS` (no HTTP,
  REQUIRE_APPROVAL always blocks, as before); `mctl-api` enables this store;
  any other value is a misconfiguration, and every REQUIRE_APPROVAL then
  fails closed. `MCTL_POLICY_APPROVAL_TTL_S` sets a new request's expiry
  (default 24h, capped under mctl-api's 7 days). The client uses the
  existing `MCTL_TOKEN` and `MCTL_API_BASE_URL`; mctl-api requires a
  service principal acting directly to create and consume.

### 7. Waiting for an approval (#198)

A workflow never holds a pod while it waits.

1. The activity that performs the side effect calls the checkpoint. On
   `approval_pending` it returns the decision (with `approval_ref`) as its
   result and does not raise. The activity ends, and so does any pod it ran
   in.
2. The workflow enters `WAITING_FOR_APPROVAL` with the receipt id in its
   state. It waits on `workflow.wait_condition` for a signal carrying only
   the receipt id, bounded by a durable timer set from the receipt's
   `expires_at`. The signal is sent by mctl-api after it records a
   decision. It is best effort, and it is a wake-up, never an approval: its
   payload is not trusted.
3. Because the signal can be lost, the timer also fires on a poll cadence
   (for example every 15 minutes until expiry), and each firing runs a
   short read-only activity (`GET /action-approvals/{id}`) that returns the
   state. Nothing is held between polls.
4. On any wake, the workflow re-runs the side-effect activity with
   `approval_ref` set. That activity recomputes the intent and redeems the
   receipt through the checkpoint (§6). The workflow's own belief about the
   state never authorizes anything.
5. The outcome is deterministic. `approved` means the effect ran once;
   `approval_consumed` on a retry of that activity means it already ran, or
   crashed after the consume, and it is never re-run. `approval_denied`,
   `approval_expired` or `approval_intent_mismatch` ends the step according
   to the workflow's policy, and asking again needs a new request (a new
   `attempt`) and a new human decision. `approval_lookup_error` is retried
   with backoff while the timer still runs.

**Built** (`orchestrator/temporal/workflows/action_approval.py`,
`orchestrator/temporal/activities/action_approval.py`):

- **The gated-action contract.** A Temporal activity whose side effect a
  REQUIRE_APPROVAL rule governs takes a `GatedActionInput` (its own
  `payload`, plus the `execution_id`/`actor` the approval binds to, the
  `attempt` and the `approval_ref`) and does its work through `run_gated()`,
  which decides with that explicit identity (the worker has no
  `MCTL_EXECUTION_CONTEXT_FILE`) and calls the side effect only on a
  permitted decision. It must recompute its arguments from the world on
  every call, so a changed action is `approval_intent_mismatch`.
- **Where the wait lives: a child workflow keyed by the receipt.**
  `run_gated_action()` runs the activity once from the caller's workflow; on
  `approval_pending` it starts `ActionApprovalWaitWorkflow` as a child with
  id `action-approval-<receipt id>`, which holds no pod and no activity while
  it waits. No DevLoop step reaches REQUIRE_APPROVAL at the Temporal level
  today (the only gated calls are an agent's own `mcp__mctl__*` tools inside
  its pod), so the wait is a reusable helper rather than an edit to a
  particular step, and it adds no command to any existing workflow. A step
  that adopts it guards the call with its own `workflow.patched()` marker.
  Keying the child by the receipt means mctl-api can address the wake-up
  with nothing but the id it already stores.
- **Wakes.** Signal `action_approval_decided` with `{"approval_id": ...}`
  (a signal naming another receipt is ignored; several collapse into one
  re-check); otherwise a durable timer every `poll_seconds` (15 min) runs
  `read_action_approval`, a read-only GET, and re-checks only when the store
  reports a decision. The timer is bounded by the receipt's `expires_at`
  (and a 7-day ceiling), with one final read at the deadline.
- **Outcomes** (`ApprovalWaitResult.outcome`): `ran`, `denied`, `expired`,
  `timed_out`, `consumed`, `mismatch`, `refused`, `blocked`, and, from the
  caller's first call only, `undecided`.
- **Re-request.** `next_attempt()` accepts only `denied`, `expired` and
  `timed_out`, and returns the input with `attempt + 1` and no receipt.
  `MctlApiApprovals(attempt=N)` puts the attempt in the idempotency key, so
  the new attempt is a new request that needs a new human decision; the
  intent hash is unchanged.
- **Not built.** The mctl-api side of the signal (it signals nothing yet:
  the poll alone makes the wait work, at up to `poll_seconds` of latency),
  and the approval surfaces for humans.

## Open decisions (not settled here)

1. **The approval wait (#198).** The Temporal wait is built (§7). The
   signal from mctl-api, the approval surfaces (UI, Telegram, GitHub) and a
   first step that adopts `run_gated_action` are not. Until gitops sets `MCTL_POLICY_APPROVALS=mctl-api`,
   REQUIRE_APPROVAL keeps blocking. Once it is set, an agent's gated MCP call
   is refused with `approval_pending` and the request id, and a later
   identical call in the same execution succeeds once a human has approved.
2. **The mentor.** It has MCP tools but no hooks, and adding any hook makes it
   drainable (#366/#368). Until that is decided separately, its MCP calls are
   not governed.
3. **Other GitHub mutations.** The implementer's pushes and PR creation,
   the investigator's comment, the issue poller's label removal, and the
   shepherd's merge, `@claude review` comment and CI rerun do not go
   through the checkpoint yet. `run_implementer.py` and
   `run_issue_investigator.py` are owned by open PRs (#409 and #422). Each
   one becomes a one-line `require(checkpoint(...))` before its `_run`.

4. **Bash as a second transport.** The builders that carry the MCP hook also
   grant `Bash`, and `gh` / `git` are on PATH, so a side effect this slice
   gates through MCP (or a GitHub mutation) is still reachable through a
   shell command. This slice governs the MCP transport and the
   orchestrator's own calls, not the shell. Governing it is a separate
   decision: either put Bash commands through the checkpoint (the existing
   Bash `PreToolUse` hooks are the place) or remove credentials from the
   agent's shell.

## Consequences

When this ships, an agent that calls a gated mctl tool gets a deny that
carries the action digest, instead of performing the side effect. That is
the behaviour change #197 asks for, and it takes effect on the release that
includes it.

The durable approval store (§6) changes nothing until
`MCTL_POLICY_APPROVALS=mctl-api` is set. With it set, every checkpoint on a
REQUIRE_APPROVAL action makes up to two calls to mctl-api (find-or-create,
then consume) on its write budget. The client timeout is 10s per call, so a
single gated checkpoint can wait up to ~20s in the worst case. The checkpoint
itself is synchronous; the MCP `PreToolUse` hook runs it in a worker thread,
so that wait holds only the tool call being checked, not the agent SDK's
event loop. A 401 or an untyped 403 from mctl-api (a rotated `MCTL_TOKEN`, a
principal without the approval scope) is `approval_lookup_error` — undecided,
like an outage — not `approval_refused`.
