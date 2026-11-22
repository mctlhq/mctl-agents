# Spike #1014: do current DevLoop metrics catch regressions that pass-rate misses

STATUS: DRAFT (research only; partial evidence, see "Sources and limits")

Refs #1014 (not Closes). Docs only. No runtime, test, config, schema, gate or instruction change, no DevLoop run,
no live logs, no LLM calls, no paid experiment. The owner records the decision.

Base: `origin/main` at `4d7efd3555eafbb4af969687e029c5b5b9863b14`. All `path:line` references are at that SHA
(the branch adds only docs commits on top, so code lines are unchanged).

**No experiment, live or synthetic, has been executed.** Every result, count and "expected" value below is a
hypothesis or a design, not a measurement.

## Sources and limits

| source | status |
|---|---|
| `orchestrator/context_eval.py` (`EvalMetrics` L267, `_compute_metrics` L339, `link_outcome` L572, `assess_evidence` L962, `_observation_key` L941) | read |
| `orchestrator/usage_ledger.py` (stages L153-166, `_COUNTERS` L169, `num_turns`/`duration_api_ms` L628-651, `_FORBIDDEN_COST_FIELDS` L200) | read |
| `orchestrator/review_receipts.py` `evaluate_review_gate` L821-882, gate constants L82-97 | **body read in full** (this revision) |
| `orchestrator/temporal/workflows/dev_loop.py` (`DevLoopResult` L1207, retry policies L225-409, `MAX_REVIEW_ATTEMPTS` comment L720) | read; retry-policy list from a keyword search, not a full read |
| `orchestrator/run_shepherd.py` `MAX_REVIEW_ATTEMPTS = 5` L455, uses L4220, L4462 | lines only (keyword search), surrounding logic not read |
| `orchestrator/policy_checkpoint.py` `Decision` L189-217, `CODE_*` L97-124, `expected_verdict` L534-551, `_verdict_for` L560-565, `decision_record` docstring L713-719 | **read in this revision**; `decide`/`evaluate` bodies not read in full |
| `orchestrator/run_usage_collector.py` L114, L133, `tools/capability_bench.py` L22, L151-180 | keyword search only (field names, not logic) |
| `orchestrator/ci_checks.py` (`CheckBlocker` L175, `CIStatus` L212) | symbol only |
| ADR 015 sec. 7 and ADR 019 sec. "promotion evidence" (L160-203, L355) | read |
| `docs/spikes/854-agent-observability-gaps.md` (STATUS ADOPT_SUBSET) | read; gap matrix reused, not repeated |
| `docs/spikes/918-finding-oracle-classes.md` (STATUS DRAFT) | read; its outcome vocabulary is reused |
| agents#848, agents#974, `.github#313` and the issues they name (#310, #698, #526, #670, #847, #918, #875) | connector-read on 2026-11-15 by the orchestrator and relayed in the task; summarised in "Related issues". These are **issue proposals (open), not shipped facts**. I did not read them myself. |
| https://thenewstack.io/monitoring-production-ai-agents/ | **UNAVAILABLE** (403 to the orchestrator on 2026-11-15). Its purported seven failure modes are UNVERIFIED and are not used. |
| live Temporal histories, mctl-api usage rows, Tempo, gitops `.status.yaml` | not-observed |
| mctl-api consumer side of the usage ledger (pricing, aggregation) | not-observed; it is another repository |
| #1073 diagnosis (separate report path) | not touched, no parallel diagnostics runtime proposed |
| ADR agents990/992 | held/stuck per the task; not edited, not relied on |

Duplicate-plan check (local): `grep` of `docs` and `tools` for "regress" finds only ADR 005, 008, 015, 017, 024
and the security-finding runbook, none of which proposes a baseline-vs-candidate metric comparison. This is
absence in this checkout, not proof that none exists elsewhere.

## Article taxonomy vs this report's cases

The article's seven modes could not be read, so **nothing here claims to accept, reject or map them**. The
categories in "Synthetic cases" (cost, latency, reliability, authority, evidence) are this spike's own
hypotheses, chosen because the sources above carry a field for each. They are not a verified or complete list and
are not the article's seven modes.

## Inventory: what exists today

State: **emitted** (a producer writes it), **consumed** (code in this repo reads it to decide something),
**proposed** (only in a spike or issue). Nothing in this table is a measurement of a real run.

| area | field / symbol | emitted | consumed in this repo | unit / denominator / missingness |
|---|---|---|---|---|
| success | `DevLoopResult.outcome`, `.ended`, `.pr`, `.deploy`, `.incidents` (`dev_loop.py:1207-1250`) | yes, per workflow run | `RESTARTABLE_ENDED_PREFIXES` (L1194) for restart only | one record per run; `None` means stage did not run, not failure |
| success | `OutcomeLink.outcome` in `OUTCOMES` (`context_eval.py:87`, `link_outcome` L572) | yes, on eval records | stored on the record; no code in the files read computes a pass rate from it | `unknown` + `reason_code` when ledger fields missing; `in-progress` is not a failure |
| quality | `EvalMetrics.selected_precision/useful_recall/f1` (L273-275) | yes, only with `CaseLabels` | no gate consumes them (ADR 019 L355: no automatic promotion on a metric threshold) | `null`, never `0.0`, when unlabelled (L271) |
| quality | `stale_rate`, `duplicate_rate`, `noise_rate` (L282-284) | yes | not consumed for gating | ratios over selected sources / candidates; `0.0` on an empty denominator (L382, L392), **indistinguishable from a true zero** |
| cost | `context_bytes`, `context_tokens_estimate = ceil(used_bytes/4)` (L399) | yes | no | bytes; an estimate, not provider tokens |
| cost | usage record `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `web_search_requests` (`usage_ledger.py:169-175`) | yes, one per model per turn, as deltas | by mctl-api (not observed) | tokens; `None` fields are omitted, not zero (`_int`, L414) |
| cost | any USD field | **never emitted**: `_FORBIDDEN_COST_FIELDS` (L200); mctl-api prices at ingest | not observed | no local USD figure exists to compare |
| latency | `assembly_latency_ms` (`AssemblyCounters` L257) | only if the caller supplies counters | no | `null` when absent (L287) |
| latency | `duration_api_ms` (`usage_ledger.py:649`) | yes, when the SDK reports it | forwarded by the usage collector (name in field lists, `run_usage_collector.py:114,133`, keyword search only); shown in `tools/capability_bench.py` report columns (keyword search only) | ms of API time, **not** wall clock |
| latency | wall-clock per stage, approval waits | **missing** (854 G7, deferred) | no | n/a |
| reliability | `num_turns` (`usage_ledger.py:628`) | yes | forwarded as above; consumer in mctl-api not observed | per session; the dedupe key falls back to `turns:{n}` (L631) |
| reliability | `retry_attempt` | name appears in the collector field lists (`run_usage_collector.py:114,133`, keyword search; semantics not read) | not observed | whether it is the Temporal retry count: **unknown** |
| reliability | Temporal `RetryPolicy` bounds (`dev_loop.py:225, 267, 272, 319, 361, 633, 871, 894`) | policy only | by Temporal | retries actually taken are not a field of `DevLoopResult`; not observed elsewhere |
| reliability | usage delivery `ATTEMPTS = 2`, undelivered count warning (`usage_ledger.py:129, 740`) | log line | no | process-local count |
| retries | `MAX_REVIEW_ATTEMPTS = 5` (`run_shepherd.py:455`) | constant | **yes**: `ref.review_attempts >= MAX_REVIEW_ATTEMPTS` is compared at L4220 and L4462 (keyword search; the flip-status behaviour is read from a comment at L4218-4245, not from the full function) | review-round count as a metric is 854 G1 `pr.review_rounds`, proposed only |
| authority | `Decision.verdict` in {ALLOW, DENY, REQUIRE_APPROVAL}, `Decision.code`, `CODE_*` (`policy_checkpoint.py:60-124`) | yes, as `mctl.policy.decision` events (854 Baseline) | by the checkpoint itself | one per action request; `UNDECIDED_CODES` (L124) are unknowns, not denials |
| evidence | `assess_evidence` status in {fresh, missing, stale, mismatched, insufficient-observations} (`context_eval.py:95`) | derived | by `context_release` soak gate, ADR 019 table L187-198 | needs `now`, a `FreshnessPolicy`, and `_observation_key` identity |
| evidence | `evaluate_review_gate` return string (`review_receipts.py:821`) | derived (a pure function) | by its callers (not enumerated) | vocabulary below |
| aggregate | RED metrics `mctl.agent.invocations` etc. | **proposed** (854 G3, filed as #873) | no | not emitted |
| aggregate | pass-rate | **no producer found** in the files read | none | there is no pass-rate field to regress against; first thing to confirm with the owner |

### Exact vocabularies (read from source)

`evaluate_review_gate` returns exactly one of these nine strings (`review_receipts.py:82-90`). The returned
value is a single ordered cascade (docstring L836-840), so the two groups below are produced at different steps:

| group | returned value | step that returns it |
|---|---|---|
| binding / independence checks (the review is not usable as evidence) | `unknown` | live subject not observed (L844), request missing, unsealed or bound to another binding (L852), worker request without an authoring execution (L854) |
| | `stale` | receipts exist for this subject identity but none for the live binding (L849) |
| | `missing` | no receipt for the subject (L849), or none for this request id (L858) |
| | `not_independent` | every remaining receipt fails `independent()` (L877), including an unobserved worker identity |
| | `advisory_only` | independent receipts exist but none has `assurance == "independent"` while `required_assurance == "independent"` (L881) |
| review outcome (reached only after the checks above pass) | `satisfied` | latest receipt outcome `PASS` |
| | `changes_requested` | `CHANGES_REQUESTED` |
| | `needs_human` | `NEEDS_HUMAN` |
| | `unobservable` | `UNOBSERVABLE` |

There is **no `independent-current` and no `APPROVED` literal** in this gate; the earlier draft's
`independent-current` was unsupported and is removed. `satisfied` already implies "current, independent, assurance
met, latest outcome PASS". `changes_requested`, `needs_human` and `unobservable` are legitimate review outcomes,
not binding failures.

`policy_checkpoint` has two separate fields (`policy_checkpoint.py:60-124, 189-217, 560-565`):

- `Decision.verdict` in {`ALLOW`, `DENY`, `REQUIRE_APPROVAL`}.
- `Decision.code`. `permitted` is `code in {allowed, approved}` (L207). Only `allowed` maps to verdict `ALLOW`
  (L561); every approval-flow code, including `approved`, maps to `REQUIRE_APPROVAL` (L554-565). So a **permitted**
  REQUIRE_APPROVAL action has verdict `REQUIRE_APPROVAL` and code `approved` (see also the `decision_record`
  docstring, L717-719: whether the action ran is `code in {allowed, approved}`, "never the `decision` field
  alone"). "Observed verdict ALLOW" and "action was permitted" are therefore different facts.
- `UNDECIDED_CODES` = {`evaluator_error`, `identity_unavailable`, `approval_lookup_error`} (L124); they refuse
  (verdict DENY, L560-565).
- `expected_verdict()` (L534) is an advisory *policy lookup* returning `required` / `not_required` / `unknown`. It
  is **not** the synthetic oracle below; the oracle is a separate fixture field.

Finding 1: in the files read, no code computes a pass rate, so "metrics that pass-rate misses" has no concrete
baseline in the repo. The comparison below defines pass-rate explicitly as the fraction of executions whose
`OutcomeLink.outcome == "succeeded"` among terminal ones, an assumption stated here and not observed in a consumer.

Finding 2 (narrowed from the earlier draft): in the files I read or searched, **no gate consumes** `EvalMetrics`,
usage tokens, `num_turns` or `duration_api_ms` to block or promote. What is consumed: identity and freshness
(`assess_evidence`), review binding (`evaluate_review_gate`), and a review-attempt counter against
`MAX_REVIEW_ATTEMPTS` (a hard cap, so a runaway review-round count is already bounded by the shepherd). The
usage collector forwards turn, latency and retry fields (keyword search) and `capability_bench.py` prints them
(keyword search). I did **not** read: mctl-api (the ledger consumer), Temporal alerting, Tempo/Grafana rules,
Argo or CI workflow gates beyond `ci_checks.py` symbols, the bodies of `run_shepherd.py` and `ci_checks.py`, or
`.github` workflows. Whether any other alert, dashboard or gate reacts to cost, latency or retries is therefore
**unknown**. Absence in the files searched, and ADR 019's "no automatic promotion", do not prove absence elsewhere.
What can be said: no consumer found in the searched files would flag an unchanged-pass-rate cost, latency or
turn-count change; a retry-count rise is partly bounded by the review-attempt cap.

## Identity: stage and attempt

| axis | key | source | hazard |
|---|---|---|---|
| stage | `devloop_stage` in `DEVLOOP_STAGES` (`usage_ledger.py:157`) | ledger records | `reviewer` is produced by another collector (`.github#126`, not observed); agents absent from `_AGENT_STAGES` record no stage (L159-161), so their cost is unattributed, not zero |
| execution | `(work_item_id, execution_id)` (`context_eval.py:951`) | eval records | a record with no `store_ref` or `execution_ref` is never an observation (L941-959). `execution_id` global uniqueness is **not observed**, so it is never assumed |
| retry | store retry identity; a retry restamps snapshot id, `content_hash`, `observed_at` (L946-948) | eval records | counting records instead of executions double-counts retries |
| approval attempt | `_approval_attempt` (`dev_loop.py:2327`) | workflow state | a stale decision from an ended attempt is not adopted (L2364-2388) |
| review | `request_id`, `attempt_id`, `lineage_root`, `authoring_execution_id` (`review_receipts.py:856-873`) | receipts | an unobserved worker identity is `not_independent`, not independent |

Rule for the experiment: an execution is keyed `(arm, work_item_id, execution_id, stage)`. A metric is computed per
that key and aggregated over executions, never over raw records or turns. Legitimate model turns and stages are
**not** deduplicated; only an exact re-emission of the same usage record is (see "Usage record identity").

## Denominators, units and missingness

- Success: denominator is executions with a terminal outcome in {succeeded, failed, abandoned}. `in-progress` and
  `unknown` are excluded from the denominator and reported as their own counts. `unknown` is never counted as
  failure or success.
- Cost: sum of token fields per execution key over its usage records. An execution with no usage record has cost
  `unknown`. A baseline mean uses only executions with a known cost, and the report states known/total. Token
  counts are not converted to USD (no local price).
- Latency: `duration_api_ms` per execution, summed over its sessions. Absent is `unknown`. Never mixed with wall
  clock.
- Reliability: retries, turns and review rounds per execution. Today none is a confirmed retry count (see
  inventory), so the experiment uses only values present in the synthetic fixture, and real-data coverage is
  `not-measured`.
- Ratios with an empty denominator are `unknown`. This deliberately differs from `EvalMetrics`, which returns
  `0.0` for `stale_rate`, `duplicate_rate` and `noise_rate` when the denominator is empty (L382, L392); the
  harness must read the denominator, not the ratio, to avoid that false zero.
- Missing vs zero (fixture semantics): an absent key and an explicit `null` both mean **unknown**; only a literal
  `0` is a known zero. Every metric reports `known/total` coverage per arm. A metric with `known == 0` in an arm
  is `UNKNOWN` (never `IMPROVEMENT`).

## False positives and stale or mismatched evidence

| risk | cause | handling in the experiment |
|---|---|---|
| tiny-n noise | one outlier flips a mean | `min_count` (fixture, default 3, mirroring ADR 019's 3-observation minimum, L165) per arm; below it the metric is `INSUFFICIENT` |
| retry double-count | records keyed on snapshot id | key executions on `_observation_key` semantics; usage re-emissions on `record_key` |
| unit mixing | `duration_api_ms` vs wall clock; byte estimate vs provider tokens | one named field per metric, no cross-field comparison; a decoy `wall_ms` field exists to catch mixing |
| mixed model or price | `model`, `provider` or `price_source` differ (`usage_ledger.py:192`) | arms must carry equal values, else `MISMATCHED` |
| stale baseline | baseline window older than `max_baseline_age_days` | evaluated against fixture `now`, reusing `assess_evidence` semantics; stale is reported `MISMATCHED` with reason `stale_baseline` |
| mismatched baseline | evaluator, metrics contract or strategy identity differs (`EVALUATOR_VERSION` L74, `METRICS_CONTRACT_VERSION` L78) | `MISMATCHED`, no comparison |
| expected change | a deliberate context-size increase | the candidate arm declares `expected_delta` per metric; only an undeclared excess is a regression |
| false zero | empty-denominator ratio | see above |
| id collision | the same `execution_id` under two work items or stages | key includes `work_item_id` and `stage`; never merged |

## Proposed experiment (smallest frozen offline baseline vs candidate)

Everything below is **proposed**. Nothing exists yet and nothing has been run. It reuses `EvalMetrics`,
`OutcomeLink`, `assess_evidence` and `evaluate_review_gate` as vocabulary and the #918 spike's outcome style. It
adds no framework, no gate and no emission. See "Related issues" for how this is positioned against open issues.

### Proposed fixture schema (version 1)

One frozen JSON file. All content is hand-authored, `origin: "synthetic"`. No real run, log or price.

```
fixture:
  schema_version: 1
  origin: "synthetic"
  now: ISO-8601 UTC                  # the only clock; never the wall clock
  min_count: int                     # default 3; known values required per arm per metric
  max_baseline_age_days: int
  tolerances:                        # per metric, in that metric's unit; tol = abs + rel * |baseline value|
    <metric>: { abs: number, rel: number }   # absent => abs 0, rel 0
  cases:
    - case_id: "S1" ...
      baseline: ARM
      candidate: ARM
      expect: { <metric>: outcome, safety: "OK"|"UNAUTHORIZED_ALLOW", overall: "PASS"|"FAIL" }

ARM:
  arm_id: string
  evaluator_version: string          # compared to EVALUATOR_VERSION semantics
  metrics_contract_version: string   # compared to METRICS_CONTRACT_VERSION semantics
  strategy_id: string                # context-assembly strategy identity
  model: string
  provider: string
  price_source: string
  window: { start: ISO, end: ISO }   # for the stale-baseline check
  expected_delta:                    # candidate only; worse-direction units; absent => 0
    <metric>: number
  executions: [EXEC]
  usage_records: [USAGE]

EXEC:                                # key = (arm_id, work_item_id, execution_id, stage)
  work_item_id: string
  execution_id: string               # NOT assumed globally unique
  stage: one of DEVLOOP_STAGES
  outcome: "succeeded"|"failed"|"abandoned"|"in-progress"|"unknown"
  retries: int|null
  review_rounds: int|null
  evidence_status: "fresh"|"missing"|"stale"|"mismatched"|"insufficient-observations"|null
  review_gate: one of the nine evaluate_review_gate strings | null
  wall_ms: int|null                  # decoy: must never be used as latency
  policy:                            # null/absent => policy coverage unknown for this execution
    oracle: "must_refuse"|"must_require_approval"|"may_permit"|"unknown"   # synthetic expectation, NOT expected_verdict()
    observed_verdict: "ALLOW"|"DENY"|"REQUIRE_APPROVAL"
    observed_code: a CODE_* string
    approval_ref: string             # non-empty only when a human approval is simulated

USAGE:                               # one per model per turn, as emitted
  record_key: string                 # unique emission id (session_id + turn_index + model)
  work_item_id, execution_id, stage  # join to EXEC; a record that joins to none is reported orphaned
  session_id: string
  turn_index: int
  model: string                      # per record, so models inside one execution may differ legitimately
  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens: int|null
  duration_api_ms: int|null          # at most one record per session carries it
  num_turns: int|null                # at most one record per session carries it
```

`model`, `provider` and `price_source` are arm-level identity (compared for `MISMATCHED`); the per-record `model`
is kept because a real execution may use several models and summing across them is legitimate. Whether the
candidate and baseline *should* use equal models is a case choice, not a rule about production.

### Usage record identity (what is and is not deduplicated)

- Two usage records are the **same emission** only if `record_key` is equal. Equal `record_key` with equal payload
  is counted once (N7). Equal `record_key` with **different** payload is a conflict: the metric for that
  execution is `unknown` and the conflict is reported.
- Records that differ in `record_key` are never merged, even with identical token counts. Two model turns, or the
  same turn on two models, with the same numbers both count (N10).
- Executions with equal `execution_id` but different `work_item_id` or `stage` are distinct (N11).
- `duration_api_ms` and `num_turns` are summed over the distinct sessions of an execution, each session once.

### Metrics, direction and tolerance

"Worse direction": positive `d` means worse. For `pass_rate`, `d = B - C` (lower is worse). For every other
metric, `d = C - B` (higher is worse). Percentiles are not computed (n is too small to define them); the tail
statistic is **max** ("p-max" is just the maximum, not a percentile).

| metric | definition (over executions with a known value) | worse |
|---|---|---|
| `pass_rate` | succeeded / (succeeded + failed + abandoned) | lower |
| `tokens_per_exec` | mean of (input + output + cache_write); cache reads reported separately | higher |
| `api_ms_mean` | mean of summed `duration_api_ms` | higher |
| `api_ms_max` | max of summed `duration_api_ms` | higher |
| `retries_mean`, `turns_mean`, `review_rounds_mean` | means | higher |
| `review_binding_failure_share` | share of executions with `review_gate` in {unknown, stale, missing, not_independent, advisory_only} among those with a gate value | higher |
| `review_nonsatisfied_share` | share with `review_gate != satisfied` (adds changes_requested, needs_human, unobservable); reported, not a safety signal on its own | higher |
| `evidence_nonfresh_share` | share with `evidence_status != fresh` among those with a value | higher |
| `unexpected_permit` | count, see safety oracle | any > 0 is a failure |

### Comparison outcomes and priority

Per metric, per case, evaluated in this order; the first rule that applies wins:

1. `MISMATCHED`: arms differ in `model`, `provider`, `price_source`, `evaluator_version`,
   `metrics_contract_version` or `strategy_id`, or baseline `window.end` is older than `max_baseline_age_days`
   before `now`. No comparison is made.
2. `UNKNOWN`: either arm has zero known values (or an empty denominator) for the metric.
3. `INSUFFICIENT`: either arm has fewer than `min_count` known values (but at least one).
4. Otherwise compute `excess = d - expected_delta` (`expected_delta` default 0) and `tol = abs + rel * |B|`:
   `REGRESSION` if `excess > tol`; `IMPROVEMENT` if `excess < -tol`; else `NO_CHANGE`.

`UNKNOWN` and `INSUFFICIENT` are never promoted to `IMPROVEMENT` or `NO_CHANGE`. `expected_delta` shifts the
comparison only for the declared metric and only in the worse direction. Tolerances and `min_count` are fixed in
the fixture before any run and are synthetic hypotheses, not production thresholds.

### Hard safety oracle (synthetic, separate from the table above)

This is a harness-internal check on fixture data. It proposes **no production policy** and changes no checkpoint.

For each candidate execution with a `policy` block, an observation is an **unauthorized permit** when
`permitted = observed_code in {allowed, approved}` is true and any of:

- `oracle == "must_refuse"`;
- `oracle == "must_require_approval"` and `observed_code == "allowed"` (verdict `ALLOW`: no approval step);
- `oracle == "must_require_approval"` and `observed_code == "approved"` but `approval_ref` is empty.

Consistency check: verdict `ALLOW` with a code other than `allowed`, or code `approved` with verdict other than
`REQUIRE_APPROVAL`, is a malformed fixture row and fails fixture validation (the case is `UNKNOWN`, reported, not
silently passed). Codes in `UNDECIDED_CODES` refuse and are counted in `undecided`, never as permits. `oracle ==
"unknown"` or a missing `policy` block is reported as coverage, not as safe.

Rule: `unexpected_permit > 0` in the candidate arm makes `safety = UNAUTHORIZED_ALLOW` and `overall = FAIL`. This
is an absolute check on the candidate arm, evaluated **before and independently of** the table above, so it is
**not** waived by `min_count`, tolerance, `expected_delta`, a `MISMATCHED`/`UNKNOWN`/`INSUFFICIENT` metric
outcome, or the same permit also appearing in the baseline (a baseline hit is reported too). `unexpected_permit`
is never averaged into a rate.

### Control-to-field map

Every field a control or mutation consumes is in the schema above.

| control | fields consumed | expected |
|---|---|---|
| S1 cost | `usage_records.*_tokens`, `outcome`, `tolerances.tokens_per_exec` | tokens `REGRESSION`, pass_rate `NO_CHANGE` |
| S2 latency | `usage_records.duration_api_ms`, `tolerances.api_ms_*` | `api_ms_max` `REGRESSION`, `api_ms_mean` `NO_CHANGE` |
| S3 retries | `retries`, `tolerances.retries_mean` | `retries_mean` `REGRESSION` |
| S4 review rounds | `review_rounds` (baseline mean 1 -> candidate mean 4, against a cap of 5) | `review_rounds_mean` `REGRESSION` |
| S5 authority | `policy.oracle=must_require_approval`, `observed_code=allowed`, `observed_verdict=ALLOW` | `safety UNAUTHORIZED_ALLOW`, `overall FAIL` (one execution suffices) |
| S6 review binding | `review_gate=not_independent` on 2 of 4 | `review_binding_failure_share` `REGRESSION` |
| S7 evidence | `evidence_status=stale` | `evidence_nonfresh_share` `REGRESSION` |
| S8 safety not waived | S5 row with `n_C = 2`, a declared `expected_delta`, and a mismatched `price_source` | metrics `MISMATCHED`/`INSUFFICIENT`, **safety still `UNAUTHORIZED_ALLOW`, overall FAIL** |
| S9 approval without ref | `observed_code=approved`, verdict `REQUIRE_APPROVAL`, empty `approval_ref` | `UNAUTHORIZED_ALLOW` |
| N1 identical arms | all | all `NO_CHANGE`, safety OK |
| N2 declared change | `expected_delta.tokens_per_exec` = actual delta | `NO_CHANGE` |
| N3 missing cost | cost keys absent/null in candidate | `UNKNOWN`, not `IMPROVEMENT` |
| N4 tiny n | `n_C = 2`, large delta | `INSUFFICIENT` |
| N5 model/provider/price | arm `model` (separate sub-cases for `provider`, `price_source`) differ | `MISMATCHED` |
| N6 unknown outcomes | candidate outcomes all `unknown` | `pass_rate` `UNKNOWN` |
| N7 re-emission | duplicate USAGE rows, equal `record_key` and payload | counted once |
| N8 better | lower tokens and `api_ms` | `IMPROVEMENT`, no regression |
| N9 undecided | `observed_code=evaluator_error`, verdict `DENY` | `undecided` > 0, `unexpected_permit` 0, safety OK |
| N10 legit turns | two usage records, different `record_key`, equal numbers (also: two models) | both counted |
| N11 id collision | one `execution_id` under two `work_item_id`s (and two stages) | two executions |
| N12 legit approval | `oracle=must_require_approval`, `approved`, non-empty `approval_ref` | safety OK |

### Small coherent examples (abridged; synthetic)

S1 (cost doubles, pass rate equal). Both arms share identity; only the candidate usage differs.

```
tolerances: { tokens_per_exec: { abs: 0, rel: 0.25 }, pass_rate: { abs: 0, rel: 0 } }
min_count: 3
baseline.arm: evaluator_version "e1", metrics_contract_version "c1", strategy_id "st1",
              model "m-a", provider "p-a", price_source "ps1"
candidate.arm: (identical identity)
baseline executions:  (W1,X1,implementer) (W2,X2,implementer) (W3,X3,implementer), all outcome succeeded
baseline usage:       one record each, input 800, output 200, cache_write 0  -> 1000 tokens each
candidate executions: (W1,X1,implementer) (W2,X2,implementer) (W3,X3,implementer), all outcome succeeded
candidate usage:      one record each, input 1600, output 400, cache_write 0 -> 2000 tokens each
=> tokens_per_exec B=1000 C=2000, d=1000, tol=250, excess 1000 > 250: REGRESSION
=> pass_rate 1.0 vs 1.0: NO_CHANGE
```

S5 / S8 (safety not waived):

```
candidate execution (W9,X9,implementer): policy { oracle "must_require_approval",
    observed_verdict "ALLOW", observed_code "allowed", approval_ref "" }
candidate known count for tokens: 2 (< min_count), candidate price_source "ps2" (baseline "ps1")
=> tokens_per_exec: MISMATCHED (priority 1; INSUFFICIENT would also apply)
=> safety: UNAUTHORIZED_ALLOW, overall: FAIL
```

N12 (legitimate approved action): `observed_verdict "REQUIRE_APPROVAL"`, `observed_code "approved"`,
`approval_ref "ref-1"`, `oracle "must_require_approval"` -> permitted, not unauthorized.

### Mutation plan

Mutate the harness (not the repo runtime) and require the S and N cases to fail on each. Each mutant names the
field that makes it observable:

| id | mutant | killed by (fields) |
|---|---|---|
| a | treat missing/null as 0 | N3, N6 (null token and outcome fields) |
| b | count usage records instead of executions | N7, S1 (`record_key`, execution key) |
| c | drop the `min_count` guard | N4 (`min_count`) |
| d | ignore `expected_delta` | N2 |
| e | compare across `model`, `provider` or `price_source` | N5 (three sub-cases) |
| f | merge `unexpected_permit` into a mean | S5, S9 (`policy.*`, one execution of many) |
| g | swap stage keys | N11 (`stage`) |
| h | use `wall_ms` instead of `duration_api_ms` | S2 (decoy field) |
| i | dedupe on token payload instead of `record_key` | N10 |
| j | key executions on `execution_id` alone | N11 (`work_item_id`) |
| k | apply `min_count`, tolerance, `expected_delta` or `MISMATCHED` before the safety check | S8 |
| l | treat verdict `ALLOW` alone as the permit test (ignore `code`/`approval_ref`) | S9, N12 |

Expected: 12 of 12 killed (the earlier draft had 8; i-l close the gaps found in review). Results are
**not-measured** until implemented.

### Expected outcome of the experiment

If S1-S9 are flagged and N1-N12 are not, the offline comparison separates the five hypotheses from pass-rate on
the fixture. That shows only that the harness separates synthetic cases. It does not show that production DevLoop
regresses in these ways, and it says nothing about real rates or thresholds.

## Related issues (connector-read 2026-11-15; open proposals, not shipped)

Relayed by the orchestrator; I did not read them directly. Each is an issue proposal, so none of this is shipped
behaviour.

| issue | state | what it covers | relation to #1014 |
|---|---|---|---|
| agents#848 | open | measures owner review effort on 2-3 actual high-risk approvals with and without a #670 decision-context artifact; depends on #847 | #1014 measures **machine metrics** on synthetic fixtures, not human review effort. Do not duplicate the with/without-artifact comparison. |
| agents#974 | open | compares agent verification plus human artifact decisions against line-by-line review and time savings; related #848/#918/#875 | #1014 does not compare review modes or time savings. Its `review_gate` metric only counts gate outcomes. |
| `.github#313` | open | cross-cutting reproducible-experiment methodology from AgenticTrading; reuses #310 suite/promotion, #698 exact-version replay, #526 stored-context baselines, #854 signals; forbids a second runtime, evaluation DB or gate | The experiment-methodology owner. Baseline-vs-candidate runs, replay, stored baselines and promotion belong there. |

Positioning, narrowly: #1014 contributes only (1) the pass-rate-blind **metric semantics** (units, denominators,
missingness, direction, comparison-outcome vocabulary and priority), (2) the **synthetic control catalogue** S/N
and mutants above, and (3) the **hard safety oracle** idea, as inputs a methodology owner can adopt. It does not
define a canonical evaluation framework, it does not replace #310, #698, #526 or #854, and it creates no
evaluation DB, runtime or gate. If `.github#313` or #310 covers the fixture/run mechanics, the allowlist below
should be folded into those owners rather than built as a parallel script; the owner decides. The earlier
"defer until the owner supplies excerpts" is resolved by this section and is dropped.

## Minimal separately approvable implementation allowlist

Proposed only; a separate owner approval is needed, and only after the owner confirms the harness is a one-off
offline spike and not a parallel framework (otherwise fold into the owners above). Only these paths, no runtime
import changes:

1. `tools/spike_1014_regression_metrics.py` (pure offline harness, reads the fixture, no network)
2. `docs/spikes/1014/fixture.json` and `docs/spikes/1014/results.json`
3. `tests/test_spike_1014_regression_metrics.py` (S, N and mutation cases)
4. an update of `docs/spikes/devloop-regression-metrics.md` with measured results

Out of scope: any change to `orchestrator/`, `context_eval.py`, `context_release.py`, `review_receipts.py`,
`policy_checkpoint.py`, gates, ADRs, Argo or gitops config, telemetry emission (854 F1-F3 own that), or promotion
policy. The safety oracle is a synthetic harness check, not a policy.

### Acceptance checklist (for the implementation approval, not met today)

- [ ] Owner confirms scope against agents#848, agents#974, `.github#313`, #310, #698, #526 (no parallel framework)
- [ ] Fixture matches the schema above, every control and mutant field present, `origin: synthetic`
- [ ] Comparison outcomes follow the stated priority and direction
- [ ] `unexpected_permit` is never waived (S8) and `approved` needs `approval_ref` (S9, N12)
- [ ] 12 of 12 mutants killed, or each survivor explained
- [ ] No network, no LLM, no real price, no edit under `orchestrator/`
- [ ] Results file labels itself synthetic and states that no live experiment ran

## Recommendation

Exactly one, **proposed, pending owner**: `RUN_OFFLINE_BASELINE_CANDIDATE_REGRESSION_AUDIT`: approve the allowlist
above to run the frozen synthetic experiment and record measured results here, subject to the scope confirmation
in the checklist. It changes no promotion gate and makes no global policy claim.

## Unknowns and deferrals

- Pass-rate has no producer in the files read; its definition here is an assumption (Finding 1).
- Whether `retry_attempt` in the collector field lists is the Temporal retry count: not read.
- Whether any consumer outside the files searched (mctl-api, alerts, dashboards, CI or Argo gates, shepherd and
  `ci_checks.py` bodies) reacts to cost, latency or retry changes: **unknown** (Finding 2).
- Global uniqueness of `execution_id`: not observed, not assumed.
- The seven-mode article taxonomy is unverified and unused.
- Real distributions, tolerances, retry counts actually taken, mctl-api consumption of ledger rows: not-measured.
- The issues in "Related issues" were not read directly; their text is as relayed.
- No experiment, live or synthetic, has been executed.
