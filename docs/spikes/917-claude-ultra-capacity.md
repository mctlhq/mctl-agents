# Spike #917: Does verified Claude Ultra remove the session bottleneck for DevLoop?

STATUS: PENDING_EVIDENCE

Allowed outcomes (exactly one, recorded by the account owner):
`NO_MATERIAL_CAPACITY_CHANGE`, `UPDATE_SUBSCRIPTION_CAPACITY_MODEL`,
`UPDATE_CAPACITY_AND_USAGE_CREDIT_MODEL`. Until then the status stays
`PENDING_EVIDENCE`. Refs mctlhq/mctl-agents#917. No runtime behaviour changes.
The result concerns one subscription-backed account and grants no
shared-account, tenant-pooling or authority conclusion.

## Current state

- Credential path: `orchestrator/auth.py:detect_auth` prefers
  `CLAUDE_CODE_OAUTH_TOKEN` over `ANTHROPIC_API_KEY`, then a local CLI session.
  Deployed worker: OAuth token from Vault `platform/mctl-agents`, property
  `claude-code-oauth-token`
  (`services/admins/mctl-agents-worker/values.yaml`). Argo CronWorkflow pods
  read the same key via `mctl-agents-secrets`.
- Quota evidence: `orchestrator/rate_limit.py` (`RateLimitObservation` with
  `rate_limit_type`, `resets_at`, `overage_disabled_reason`; label via
  `account_label`). `allowed` and `allowed_warning` events are discarded today
  and there is no utilization field. The investigator still has its own inline
  429 check (mctl-agents#455).
- Cost: `docs/adr/012-model-usage-cost-attribution-contract.md`.

## Account facts

| Fact | Value |
|---|---|
| Plan tier | not observed |
| Verified | not observed |
| Usage credits enabled | not observed (usage-credit path: unmeasured) |
| Credential path | `CLAUDE_CODE_OAUTH_TOKEN` (per gitops values) |
| Account label (`account_label`) | not observed |
| Other consumers of the same token | control worker, Argo CronWorkflows; labs `agent-worker-preview` and `claude-remote` unknown |
| Fable stage in use | not observed (usage ledger returned 403 to the investigating run) |
| SDK reports distinct Fable weekly `rate_limit_type` (claude-agent-sdk 0.2.136) | not observed |

No token values appear in this report or its artefacts.

## Measurement protocol

- Before window: reconstructed from existing evidence only: `.status.yaml`
  `rate_limited` entries, investigator `rate-limit-exhausted` outcomes,
  archived Argo logs, usage-ledger buckets by `devloop_stage`,
  `canonical_model`, `outcome`. Record what each source covered and what
  retention cut off; unavailable source = "not observed" with reason.
- After window: owner-run on the verified Ultra account, representative stages
  back-to-back, long enough to cross a would-have-been five-hour boundary and,
  if feasible, reach a weekly warning. Record stage, model, effort, turns,
  tokens, duration, outcome, retries and every `RateLimitEvent`. Stop at the
  first weekly `allowed_warning` or a pre-set fraction of the week.
- Usage credits: never enabled for the spike. If already enabled, one bounded
  test under an owner-agreed spend cap; spend taken from the provider billing
  surface only.
- Normalization: per stage and per model (stages per hour/day, output tokens
  per hour). Raw run counts are never compared across windows with different
  stage mixes. Vendor headline ratios are not evidence.
- Analysis: `python -I tools/spike_917_capacity.py RECORDS.jsonl MANIFEST.json`;
  schema and synthetic fixture in `docs/spikes/917/`.

### Before-window evidence

| Source | Coverage | Result |
|---|---|---|
| `.status.yaml` `rate_limited` | not observed | pending |
| Investigator `rate-limit-exhausted` | not observed | pending |
| Archived Argo logs | not observed | pending |
| Usage ledger | not observed (403 to investigating run) | pending |

## FinOps mapping

| Bucket | Meaning | ADR-012 field | Source |
|---|---|---|---|
| Included subscription quota | entitlement (all-models weekly, Fable weekly) consumed as tokens | token counts; no cost field | SDK `usage` / `model_usage`; provider usage page for remaining % |
| List-price estimate | tokens at API list price | `calculated_cost` + `pricing_version` | SDK `costUSD` / `total_cost_usd` |
| Usage-credit spend | actual consumption-priced charge after included limits | not representable today | provider billing surface only |
| Console/API credits | separate account type, unused by DevLoop | `provider_reported_cost` if ever used | Console billing |

`provider_reported_cost` stays null for included subscription usage.

## Decision rule

- `NO_MATERIAL_CAPACITY_CHANGE`: before-window rejections predominantly
  `seven_day` (or none), or the weekly limit binds before the removed
  five-hour window would have.
- `UPDATE_SUBSCRIPTION_CAPACITY_MODEL`: `five_hour` rejections measurably
  bounded throughput before and are gone after, with marginal throughput
  gained before a weekly limit binds; credits unused or unmeasured.
- `UPDATE_CAPACITY_AND_USAGE_CREDIT_MODEL`: as above and credits were
  exercised in the bounded test, proving ADR-012 needs a usage-credit bucket.

Follow-ups are filed only for gaps the evidence proves (candidates: record
`allowed_warning` and utilization, adopt the shared observation in the
investigator per #455, ADR-012 usage-credit bucket).

## Findings

Pending evidence. Binding weekly limit (all-models vs Fable): not observed.
