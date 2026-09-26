# ADR 012 — Canonical model-usage and cost-attribution contract

> **Status:** proposed
> **Date:** 2026-09-22
> **Issue:** mctlhq/.github#49 (parent roadmap mctlhq/.github#48, `agent-finops`)
> **Supersedes:** nothing. It is the contract that `usage-ledger` is built
> against, and it deliberately decides nothing about dashboards, budget
> enforcement or the observability backend.

## Context

`agent-finops` wants one model invocation attributable to the exact
execution, work item, repository, issue/PR and agent stage that caused it —
without storing prompts or completions, and without the answer changing when
the tracing backend does.

The starting position is not "we have usage data with the wrong shape". It is
that **mctl-agents records no model usage or cost at all today**. Verified on
`abf5ea2`:

```
$ grep -rn "total_cost_usd\|model_usage" --include="*.py" orchestrator/
(no matches)
```

Six code paths run a model, and not one of them reads the usage the SDK
already hands it. So the cost of this contract is not a migration; it is
deciding what to write down before anything writes the wrong thing.

## Invocation-path inventory

Every path that spends model quota, on `abf5ea2`. "Reads `ResultMessage`"
means the module already has the object in hand — for those, capture is a
field read, not new plumbing.

| Path | Entry point | Reads `ResultMessage` | Model selection |
|---|---|---|---|
| `run_implementer.py` | `ClaudeSDKClient` (streaming, multi-turn) | yes (4 sites) | not in `model-policy.yaml` |
| `run_issue_investigator.py` | `ClaudeSDKClient` | yes (7 sites) | not in `model-policy.yaml` |
| `run_service_agent.py` | `ClaudeSDKClient` | yes (4 sites) | `service_agent` → `balanced` |
| `run_shepherd.py` | `query()` one-shot (`orchestrator/run_shepherd.py:2108`) | **no** (yes since the 2026-09-24 amendment) | `review_findings_normalize` → `cheap` |
| `run_incident_responder.py` | `ClaudeSDKClient` | **no** | not in `model-policy.yaml` |
| `run_mentor.py` | `ClaudeSDKClient` | **no** | `mentor_digest` → `cheap` |

Helpers that hold a client but do not constitute an independent billable
invocation: `mcp_guard.py` (one status read on an existing client) and
`subagent_wait.py` (drains messages for a run another path owns). They must
not emit their own records, or a delegated sub-agent would be counted twice.

Three of the six do not currently touch `ResultMessage`. That is the real
implementation cost of `usage-ledger`, and it is why this ADR states the
capture point rather than leaving it to each path.

## What the SDK actually exposes

Not from memory — read from the pinned wheel. `pyproject.toml:29` pins
`claude-agent-sdk==0.2.136`; the sdist in `uv.lock` was downloaded and its
SHA-256 checked against the lock (`a45bff05…86bc3`) before reading
`src/claude_agent_sdk/types.py`.

`ResultMessage` carries `total_cost_usd: float | None`, `usage: dict | None`,
`num_turns`, `duration_api_ms`, `session_id`, `uuid`, `api_error_status`,
`stop_reason`, `terminal_reason`, and:

```python
model_usage: dict[str, ModelUsage] | None
```

```python
class ModelUsage(TypedDict):
    inputTokens: int
    outputTokens: int
    cacheReadInputTokens: int
    cacheCreationInputTokens: int
    webSearchRequests: int
    costUSD: float
    contextWindow: int
    maxOutputTokens: int
    canonicalModel: NotRequired[str]   # id used for the pricing lookup
    provider: NotRequired[str]         # firstParty | bedrock | vertex | …
```

Four consequences the contract is shaped by:

1. **`model_usage` is keyed by model.** One agent run legitimately spans more
   than one model — a main loop plus a `cheap`-profile sub-agent. A single
   record per run would silently merge them and make per-model cost
   unrecoverable. **The grain is (run, model), not run.**
2. **`canonicalModel` is the pricing key** and may differ from the string the
   entry is keyed by. Record both: the key is what was asked for, the
   canonical id is what was priced.
3. **There is no reasoning-token field.** `#49` lists `reasoning_tokens?` as
   optional; this SDK surface does not expose it. The field stays in the
   schema as nullable and is **absent**, not zero — zero would assert an
   observation nothing backs.
4. **`webSearchRequests` is a billable non-token unit.** A token-only record
   cannot reproduce cost when it is non-zero, so it is part of the contract.

## Cost semantics

`#49` requires three concepts that must never be confused. The decision
recorded on that issue on 2026-09-11 — DevLoop runs on the Claude subscription
OAuth token — determines which one is populated today:

| Concept | Source | Populated today |
|---|---|---|
| `provider_reported_cost` | an invoice-grade figure from a billing API | **never — always null** |
| `calculated_cost` | usage × a versioned mctl price table | yes |
| `invoice_reconciled_cost` | reconciliation against a provider billing export | defined, unused |

**`costUSD` and `total_cost_usd` from the SDK are `calculated`, not
`provider_reported`.** Under a subscription there is no per-call charge to
report: the figure is the Claude Code price table applied to token counts. It
is an estimate of list price, and writing it into `provider_reported_cost`
would launder an estimate into a billing claim. The billable dimension under a
subscription is quota-window consumption, so token counts — not dollars — are
the load-bearing numbers.

`pricing_version` is mandatory whenever `calculated_cost` is non-null, and
identifies the table that produced it. Historical reproducibility follows from
never recomputing a stored `calculated_cost`: a price change produces new
records under a new `pricing_version`, and old rows keep their original value.
Recomputation would rewrite history to match today's prices, which is exactly
the property `#49` asks to preserve.

## The record

Version the envelope, not each field: `schema_version` is the compatibility
gate, so a consumer can refuse a shape it does not understand instead of
silently misreading it.

```yaml
schema_version: 1            # required

# identity / dedupe
id                           # deterministic, see Idempotency
session_id                   # ResultMessage.session_id
result_uuid                  # ResultMessage.uuid, nullable on older CLIs
model_key                    # the model_usage dict key, as served
canonical_model              # ModelUsage.canonicalModel, pricing lookup id
provider                     # ModelUsage.provider (firstParty | bedrock | …)

# correlation — see Correlation contract
temporal_workflow_id
argo_workflow_name
agent                        # investigator | implementer | shepherd | …
devloop_stage
target_repo
issue_number                 # nullable
pr_number                    # nullable
work_item_id                 # nullable
execution_id                 # nullable; store we_ when the run has one, else ExecutionContext ex- (#499)
trace_id / span_id           # nullable

# usage — absent, never zero, when the provider does not report it
input_tokens
output_tokens
cache_read_tokens
cache_write_tokens
reasoning_tokens             # nullable; not exposed by 0.2.136
web_search_requests

# cost
provider_reported_cost       # null under the subscription decision
calculated_cost
pricing_version              # required when calculated_cost is non-null
invoice_reconciled_cost      # defined, unused

# outcome
outcome                      # success | error | interrupted
api_error_status             # ResultMessage.api_error_status, nullable
stop_reason / terminal_reason
num_turns
duration_api_ms
retry_attempt
recorded_at
```

**Absent versus zero** is a contract rule, not a style preference. A provider
that does not report cache tokens and a run that read no cache are different
facts, and a consumer summing a column must be able to tell "nothing was
spent" from "nothing was measured".

## Correlation contract

There is already a correlation spine and this record joins it rather than
inventing a second one. `orchestrator/temporal/activities/state.py:48`
`record_execution` POSTs to `mctl-api` `/api/v1/agents/executions` with
`temporal_workflow_id`, `agent`, `environment`, `version`, `image_ref`,
`target_repo`, `argo_workflow_name`, `phase`.

`temporal_workflow_id` is therefore the join key, and `argo_workflow_name`
disambiguates the Argo run within it. Everything `#49` asks to correlate —
execution, workflow/run, repository, agent, stage — already exists on that
row, so the usage record carries the key and does not duplicate the
attributes. Issue/PR/work-item and `trace_id`/`span_id` are carried directly
because they are per-invocation rather than per-execution.

A record has one repository, and `issue_number` and `pr_number` are both read
against it (#499). `target_repo` is the PR's repository when there is a PR,
else the issue's, else the repository the run works in. A source issue in a
different repository than the PR is omitted rather than recorded, so a missing
`issue_number` beside a `pr_number` means "no issue in this repository", not
"no source issue". `execution_id` is the work-context store's `we_…` when the
run has one, else the runner's ExecutionContext `ex-…`, recorded only where
that id is logged or control-plane-minted.

The model is recorded **as served** (`model_key`/`canonical_model`), never as
configured. `model-policy.yaml` v1 names only three tasks
(`service_agent` → `balanced`, `mentor_digest` → `cheap`,
`review_findings_normalize` → `cheap`); implementer and investigator resolve
theirs elsewhere, env overrides exist, and an alias can resolve to something
else entirely. Attribution must reflect what was paid for, not what was asked
for.

## Idempotency and dedupe

The same paid invocation must be counted once across four distinct replay
mechanisms.

`id` is **deterministic**, not random:

```
id = sha256(session_id | result_uuid | model_key)
```

A random id would make every replay a new row, which is precisely the failure
mode. With a deterministic id, an idempotent upsert (insert-or-ignore on `id`)
makes re-recording a no-op, and the four cases collapse to one rule:

| Replay mechanism | Why the id is stable |
|---|---|
| Temporal activity retry (`SDK_STEP_RETRY_POLICY`, `maximum_attempts=3`) | a retry that re-records the *same* result carries the same `session_id`/`uuid` |
| Temporal workflow replay | replay re-executes deterministic code; the activity result is taken from history, not re-run |
| Process restart mid-record | the write is an upsert, so repeating it is safe |
| Telemetry/export reprocessing | export is a read of the stored row; it does not mint ids |

The distinction that matters: a Temporal retry which **re-runs the model**
produces a genuinely different invocation with a new `session_id`, a real new
charge, and `retry_attempt` incremented. It must NOT be deduped. Retry means
"counted once per invocation", not "counted once per activity".

`result_uuid` is nullable on older CLIs. When it is absent the id falls back to
`sha256(session_id | model_key | num_turns)`; `session_id` plus the model is
already unique per run, and `num_turns` keeps two records from colliding if a
single session ever emits two results for one model.

## OpenTelemetry mapping

The mapping is **mctl-owned and versioned**, so a change in an external
semantic convention cannot silently reinterpret historical analytics. This is
the same rule the platform telemetry attribute catalog already applies
(`mctl-docs`, `docs/reference/telemetry-attributes.md`), and this ADR does not
restate that catalog — it defers to it for resource-level and correlation
attributes.

| Record field | OTel attribute | Note |
|---|---|---|
| `canonical_model` | `gen_ai.request.model` | as served |
| `provider` | `gen_ai.system` | mctl value set, not the upstream enum |
| `input_tokens` | `gen_ai.usage.input_tokens` | |
| `output_tokens` | `gen_ai.usage.output_tokens` | |
| `cache_read_tokens` / `cache_write_tokens` | — | **no stable upstream attribute; mctl-owned `mctl.gen_ai.usage.cache_*`** |
| `calculated_cost` | — | deliberately not exported as a `gen_ai.*` attribute |
| `temporal_workflow_id`, `agent`, stage | per the telemetry attribute catalog | |

Two deliberate divergences:

- **Cache tokens have no stable upstream home.** The GenAI conventions are
  still moving (the registry now lives in
  `open-telemetry/semantic-conventions-genai`), so cache accounting uses an
  `mctl.`-prefixed name. An `mctl.`-prefixed attribute cannot be silently
  redefined by an upstream release.
- **Cost is not exported as a span attribute.** A span is sampled; a cost
  ledger must not be. Sampling would under-count spend by exactly the sampling
  rate, and the resulting number would look plausible. Cost lives in the
  durable record; spans carry the correlation ids needed to join to it.

`otel_mapping_version` accompanies exported data, so a future remapping is a
version bump rather than a reinterpretation of old rows.

## Storage boundary

**`mctl-api`, as a sibling table to `agent_executions`.** Not a new service,
and not mctl-agents' own storage.

- The correlation spine (`agent_executions`) is already there, already written
  by a Temporal activity, and already has the HTTP write path and
  authentication this record needs (`/api/v1/agents/executions`).
- ADR-010 already settled that mctl-api holding a fact about mctl-agents'
  execution does not make it a second orchestrator. The same reasoning applies
  here: a usage row is a fact about an execution, not a decision about one.
- A separate store would have to re-derive the join key, re-authenticate and
  be reconciled against `agent_executions` — three ways to drift, for no gain.
- mctl-agents must not own it: agents are ephemeral Argo pods, and a ledger
  whose lifetime is a pod's is not a ledger.

The query API is a read model over that table. This ADR records the boundary;
the endpoint shape belongs to `usage-ledger`.

## Testable invariants

1. Every path in the inventory can emit a record with all required fields
   populated from a single `ResultMessage`.
2. A run spanning two models produces two records with the same
   `temporal_workflow_id` and different `model_key`.
3. Recording the same `ResultMessage` twice leaves exactly one row.
4. A Temporal retry that re-runs the model produces a second row with an
   incremented `retry_attempt`.
5. `provider_reported_cost` is null for every record produced under the
   subscription decision.
6. `calculated_cost` non-null implies `pricing_version` non-null.
7. A price-table change does not alter any stored `calculated_cost`.
8. No field of the record can contain prompt or completion text — the record
   has no field of that shape.
9. `cache_read_tokens` absent and `cache_read_tokens = 0` are distinguishable
   after a round-trip.
10. Every producer record carries a `devloop_stage` vocabulary value or none.
11. A review-feedback run's records carry `agent=implementer` and
    `devloop_stage=shepherd`.

## Amendment 2026-09-24 — the producer (mctlhq/.github#50)

**Where it records.** `orchestrator/usage_ledger.UsageRecorder` is fed by
`tracing.agent_run`'s observer, the one place the investigator, the
implementer and (now) the shepherd already hand their whole SDK stream to. It
records whether tracing is on or off. `subagent_wait` and `mcp_guard` feed the
same observer and never record on their own, as required above.

**Who writes.** Records are appended with `MCTL_USAGE_WRITER_TOKEN`, the bearer
of `service:mctl-agents-usage` (mctl-api#385): a principal whose only
permission is `usage:write` and which mctl-api confines to
`POST /api/v1/usage/records`. The admin `MCTL_TOKEN` is never used for
ingestion (variant B of #50). The token is blanked in every SDK session's
environment (every `ClaudeAgentOptions` in `options.py` goes through
`_scrubbed`), so the model cannot read it through Bash. Every row records the
writer (`ingested_by`, `ingested_by_principal_id`), server-side.

**`model_usage` is cumulative per session.** This corrects an assumption the
record section above leaves implicit. Measured on claude-agent-sdk 0.2.136,
two turns of one `ClaudeSDKClient` session with Haiku reported:

| Turn | `model_usage` outputTokens | cacheCreationInputTokens | `total_cost_usd` |
|---|---|---|---|
| 1 | 53 | 26199 | 0.0532 |
| 2 | 100 (= 53 + 47) | 34487 (= 26199 + 8288) | 0.0726 |

So a ResultMessage is not an independent invocation within its session. The
implementer and the investigator drain past the first result (#366), and
recording each ResultMessage as-is would count every earlier turn again. The
producer therefore sends, per (session, model), the difference between this
result's counters and the ones last recorded. The identity is unchanged,
`(session_id, result_uuid, model_key)`, and the rows of a session sum to its
reported total. Invariant 3 holds per result. Invariant 2 still holds, since
model is part of both the key and the delta.

Two consequences for delivery:
- The delta baseline advances only when a batch may have been stored. A
  batch that certainly was not stored (no connection, or an HTTP error: the
  ingest is one transaction) is carried by the next turn's delta rather than
  lost. "May have been stored" is sticky across the retry attempts.
- A batch whose answer was lost advances the baseline, because a double count
  is the worse error.

**Off the event loop.** `observe` only queues. One daemon thread per process plans, delivers and commits, in order, so a slow mctl-api never stalls the drivers' anyio deadlines. An `atexit` hook flushes what is still queued, so the last turn of a run is delivered too.

**Transport.** The token is sent only to an https `MCTL_API_BASE_URL`. Any other scheme disables recording.

**Cost.** The producer sends no cost. mctl-api prices the token counts from
its versioned catalog at ingest (`calculated_cost` + `pricing_version`), and
`provider_reported_cost` stays null as decided above.

**`devloop_stage` is a closed v1 vocabulary (owner decision 2).** Four values,
and no others: `investigator`, `implementer`, `reviewer`, `shepherd`. Every
ledger agent name defaults to a stage:

| Ledger `agent` | Default `devloop_stage` |
|---|---|
| `investigator` | `investigator` |
| `implementer` | `implementer` |
| `shepherd` | `shepherd` |

The one exception is the shepherd's review-feedback follow-up: it forks
`python -m orchestrator.run_implementer --review-feedback`, whose SDK session
still opens as `tracing.agent_run("implementer", ...)`, so `agent` stays
`implementer`. But the remediation cost belongs to the stage that ordered it,
not the stage that spent it, so that run's records carry
`agent=implementer` and `devloop_stage=shepherd`. `agent` is never renamed;
`devloop_stage` is an additional field on the same record.

A `devloop_stage` outside the vocabulary — free text, wrong case, a
non-string, empty — is omitted from the record with one warning, exactly as
`target_repo` and `execution_id` are today: the ingest is one transaction, so
a malformed field would otherwise cost the whole batch. A ledger agent absent
from the default table (a future `service-agent`, `mentor` or
`incident-responder` path) records no `devloop_stage` at all rather than
guessing one.

`reviewer` is part of the vocabulary constant so the producer and the
collector cannot drift, but no path in this repo emits it: it is written by
the review collector (mctlhq/.github#126), not by `UsageRecorder`.

## Non-goals

Dashboards. Hard budget enforcement. Replacing provider invoices. Selecting
the observability backend. Persisting prompt or completion content. Changing
which model any path uses.
