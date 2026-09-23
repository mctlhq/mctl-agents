# ADR 014 — Runtime policy checkpoint for agent actions

> **Status:** proposed
> **Date:** 2026-09-23
> **Issue:** mctlhq/mctl-agents#197 (related: #195 tracing, #196 execution identity)

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
- A REQUIRE_APPROVAL is permitted only when the approval store returns an
  approval bound to the exact `action_digest`. That digest covers kind,
  operation, target, args digest, execution id and actor. Changing any
  argument, or the execution, or the actor, produces a different action,
  and an approval for the old one does not cover it.

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

## Open decisions (not settled here)

1. **The durable approval store.** `ApprovalLookup` is the seam. Its only
   implementation today, `NO_APPROVALS`, never finds an approval, so
   REQUIRE_APPROVAL always blocks. That is safe, but it means a gated MCP tool
   is unusable by an agent until a store exists. The owner requires the store
   to be durable, bound to the exact request, and invalidated by changed
   arguments, with a retry that does not duplicate the side effect. The
   candidate is the approval projection the mctl-api work-item contract
   already names (`approval_requested` / `approval_decided` events), which is
   not built yet. Single-use consumption belongs to the same store.
2. **The mentor.** It has MCP tools but no hooks, and adding any hook makes it
   drainable (#366/#368). Until that is decided separately, its MCP calls are
   not governed.
3. **Other GitHub mutations.** The implementer's push and PR creation, the
   investigator's comment, and the shepherd's merge and rerun do not go
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
