# ADR 012 — Canonical model-usage and cost attribution contract

> **Status:** proposed
> **Date:** 2026-09-22
> **Issue:** mctlhq/.github#49 (agent-finops, contract phase)
> **Supersedes:** nothing. This is the first finops contract; no prior draft
> exists in mctl-agents, mctl-docs or mctlhq/.github.

## Context

Roadmap epic `mctlhq/.github#48` (agent-finops) needs one model invocation to
be attributable to the exact execution, work item, repository, issue/PR and
agent stage that caused it — before any dashboard or budget enforcement is
built on top. `mctlhq/.github#49` asks for a backend-neutral contract that
does not require prompt/completion storage and does not depend on a specific
observability backend.

Two facts fix the shape of this contract:

1. **Every model call in mctl-agents goes through the claude-agent-sdk
   `ClaudeSDKClient` streaming session.** There is no direct `anthropic` SDK
   use and no raw `messages.create` anywhere in the orchestrator. Usage
   arrives once per session on the final `ResultMessage`, not per API call.
2. **Billing runs in subscription-quota mode** (decision recorded on #49 on
   2026-09-11): the runtime authenticates with the Claude subscription OAuth
   token, so no provider-metered dollar cost exists per invocation.
   `provider_reported_cost` is structurally null on this path, and quota-window
   consumption is the billable dimension the operator actually experiences.

Today **none** of the usage fields the SDK already returns are recorded
anywhere: the only `ResultMessage` fields consumed are `is_error` and the
429/quota detection in the investigator. Everything needed for attribution is
already present in-process at the call sites and is currently dropped.

## Invocation-path inventory

All paths below are Anthropic-only via claude-agent-sdk; model choice comes
from `config/model_policy.py` + `config/model-policy.yaml` v1 (profiles
`cheap`/`balanced`, env overrides, task-specific legacy envs highest
priority). `ModelSelection` already logs `task/profile/model/source`.

| Path | Call site | Model source | Retry / replay context | Correlation available at call site |
| --- | --- | --- | --- | --- |
| investigator | `run_issue_investigator.py:1517` | `INVESTIGATOR_MODEL` → model_policy | Argo pod; subagent relaunch ledger; 429 via `ResultMessage.is_error` + `api_error_status` | repo/issue, proposal slug, devloop stage, Argo workflow name |
| implementer | `run_implementer.py:2101` | model_policy + env override | Argo pod, 7200 s deadline + mutex; re-entered via shepherd subprocess (`apply_followup`) | repo/issue/PR, proposal slug, stage, Argo workflow name |
| shepherd | no model call of its own | — | subprocess into run_implementer (review-feedback mode) | inherits implementer's frame |
| incident-responder | `run_incident_responder.py:180` | model_policy | per-incident; Temporal activity retries upstream | incident id, repo, Temporal workflow/run id |
| mentor | `run_mentor.py:149` | task `mentor_digest` → profile `cheap` | scheduled | schedule id |
| service-agent | `run_service_agent.py:156` | task `service_agent` → profile `balanced` | per-service runs | service/tenant |
| subagent drain | `subagent_wait.py:311` | same SDK session | child usage arrives in the parent's stream; orphan/grace semantics deliberately avoid double-charging | parent's frame |

Reviewer bots (claude-review.yml) run in GitHub Actions outside this repo's
observation and are **out of scope**: mctl neither observes nor pays their
usage through this path.

## Decision

### 1. Record granularity: one record per SDK session

The SDK exposes usage only on the session-final `ResultMessage`
(`usage`, `total_cost_usd`, `num_turns`, `duration_ms`, `session_id`,
`subtype`, `is_error`). A `ModelUsageRecord` therefore describes **one agent
session**, carrying `num_turns` as its internal-multiplicity signal.
Per-API-call granularity is unavailable on this provider path; the schema
does not preclude it (a future provider path may emit finer records with the
same fields), but nothing in this contract may assume it.

Delegated sub-agents stream into the parent session and are covered by the
parent's final usage. The orphan rule from `subagent_wait` carries into the
contract verbatim: **never charge a frame the parent never received** — an
orphaned child whose result frame never reached the parent contributes
nothing, and no synthetic record is invented for it.

### 2. Canonical `ModelUsageRecord` (schema_version 1)

```yaml
schema_version: 1
id:                     # ULID, minted at emission
timestamp:              # session end, RFC 3339 UTC
execution_id:           # lifecycle ownership execution (ADR 010); null only for paths outside DevLoop ownership
workflow_id:            # Temporal workflow id, when in scope
run_id:                 # Temporal run id, when in scope
argo_workflow:          # Argo workflow name, when in scope
work_item_id:           # optional, canonical WorkItem id
repository:             # e.g. mctlhq/mctl-telegram
issue:                  # optional issue number
pr:                     # optional PR number
agent:                  # investigator | implementer | shepherd-followup | incident-responder | mentor | service-agent
devloop_stage:          # optional, e.g. investigate | implement | review-feedback
provider: anthropic
model:                  # resolved model id actually used
model_selection:        # {task, profile, source} from ModelSelection
usage:
  input_tokens:
  output_tokens:
  cache_read_tokens:    # from cache_read_input_tokens
  cache_write_tokens:   # from cache_creation_input_tokens
  reasoning_tokens: null # not surfaced by this provider path; nullable by contract
num_turns:
duration_ms:
session_id:             # SDK session id
retry_attempt:          # 0-based; see idempotency
provider_request_id: null # not surfaced by this provider path; nullable by contract
provider_reported_cost: null  # structurally null in subscription-quota mode
calculated_cost:        # SDK total_cost_usd, see cost semantics
pricing_version:        # pinned identifier of the price table in force
outcome:                # ok | error | quota_exhausted | orphaned | cancelled
```

Prompt and completion payloads are **never** part of the record. Attribution
requires none of them.

### 3. Cost semantics — three values that must not be confused

1. `provider_reported_cost` — only ever set when the provider/API meters and
   reports a real charge. On the subscription OAuth path this is null, and a
   consumer treating null as zero is wrong by contract: null means
   *not metered*, not *free*.
2. `calculated_cost` — usage priced by a versioned mctl price catalog. In
   subscription-quota mode the SDK's `total_cost_usd` (computed from the
   Claude Code price table) is recorded here, with `pricing_version` pinned
   to the identifier of that table at emission time. Historical records stay
   reproducible after price changes because the version, not the current
   table, is authoritative for them.
3. `invoice_reconciled_cost` — defined for future reconciliation against
   provider billing exports; unused today, never backfilled silently.

The billable dimension operators actually experience today is quota-window
consumption; `calculated_cost` is the comparable-across-time proxy, not an
invoice claim.

### 4. Idempotency and replay

Dedupe key: **`(execution_id, session_id, retry_attempt)`**.

Enumerated re-entry paths and their behavior:

- **Argo pod retry** — a retried pod runs a new SDK session (new
  `session_id`), `retry_attempt` increments; both records stand, each
  attributable, no double count within one attempt.
- **Temporal activity retry** (incident-responder) — same rule; the activity
  attempt number is the `retry_attempt`.
- **Shepherd → implementer subprocess** (`apply_followup`) — a distinct
  session in the same execution: separate record, `agent:
  shepherd-followup`, same `execution_id`.
- **Subagent orphaning** — no frame received, no record; the parent's
  eventual record reflects only what its stream actually carried.

An emitter that crashes after the model call but before emission loses the
record rather than inventing one at replay; the contract prefers undercount
with a visible gap (session known from logs, record absent) over fabricated
usage.

### 5. OTel compatibility

The record is backend-neutral and remains valid if Traceway, Langfuse, Tempo
or Phoenix are swapped. Compatibility is a **translation table owned by
mctl**, pinned to a named OTel GenAI semantic-convention version (initially
`gen_ai` conventions as of semconv 1.29): `model` → `gen_ai.request.model`,
`usage.input_tokens` → `gen_ai.usage.input_tokens`, `usage.output_tokens` →
`gen_ai.usage.output_tokens`, provider → `gen_ai.system`. `trace_id`/
`span_id` are optional fields populated only once devloop-traces
(mctl-agents#195) lands; their absence never invalidates a record.

### 6. Storage boundary (recommendation, not implementation)

Durable records and their query API belong in **mctl-api**, which owns
durable platform state; storage in shared-pg per platform guidance (no new
PVC/volume for this). Emission from mctl-agents call sites is a later
implementation child of the epic — explicitly out of #49's scope, as is any
dashboard or budget enforcement.

## Consequences

- The implementation child can wrap every `ClaudeSDKClient` session exit in
  one emitter reading fields the SDK already returns; no call-site redesign
  is needed.
- `reasoning_tokens` and `provider_request_id` are nullable from day one, so
  adding a metered API-key path later is additive (fill
  `provider_reported_cost`, per-call granularity) rather than breaking.
- Consumers can never confuse "not metered" with "free", and price-table
  changes never rewrite history.
