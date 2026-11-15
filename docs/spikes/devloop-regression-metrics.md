# Spike #1014: do current DevLoop metrics catch regressions that pass-rate misses

STATUS: DRAFT (research only; partial evidence, see "Sources and limits")

Refs #1014 (not Closes). Docs only. No runtime, test, config, schema, gate or instruction change, no DevLoop run,
no live logs, no LLM calls, no paid experiment. The owner records the decision.

Base: `origin/main` at `4d7efd3555eafbb4af969687e029c5b5b9863b14`. All `path:line` references are at that SHA.

## Sources and limits

| source | status |
|---|---|
| `orchestrator/context_eval.py` (`EvalMetrics` L267, `_compute_metrics` L339, `link_outcome` L572, `assess_evidence` L962, `_observation_key` L941) | read |
| `orchestrator/usage_ledger.py` (stages L153-166, `_COUNTERS` L169, `num_turns`/`duration_api_ms` L628-651, `_FORBIDDEN_COST_FIELDS` L200) | read |
| `orchestrator/review_receipts.py` `evaluate_review_gate` L821 | read |
| `orchestrator/temporal/workflows/dev_loop.py` (`DevLoopResult` L1207, retry policies L225-409, `MAX_REVIEW_ATTEMPTS` comment L720) | read; retry-policy list from a keyword search, not a full read |
| `orchestrator/run_shepherd.py` `MAX_REVIEW_ATTEMPTS = 5` L455 | line only |
| `orchestrator/policy_checkpoint.py` (`Decision`, `CODE_*`, `decision_record`) | symbol list only; bodies not read |
| `orchestrator/ci_checks.py` (`CheckBlocker` L175, `CIStatus` L212) | symbol only |
| ADR 015 sec. 7 and ADR 019 sec. "promotion evidence" (L160-203, L355) | read |
| `docs/spikes/854-agent-observability-gaps.md` (STATUS ADOPT_SUBSET) | read; gap matrix reused, not repeated |
| `docs/spikes/918-finding-oracle-classes.md` (STATUS DRAFT) | read; its outcome vocabulary is reused |
| #848, #974 (dedup targets named by the task) | **not-observed**: no local file or commit message names them, and no network. Overlap with them is unchecked. |
| `.github#313` | **not-observed** (no network). `git log` shows `mctlhq/mctl-agents` PR #313 (`ci/bump-claude-review-pin`), a different repo, so it is not evidence about `.github#313`. |
| #1073 diagnosis (separate report path) | not touched, no parallel diagnostics runtime proposed |
| ADR agents990/992 | held/stuck per the task; not edited, not relied on |
| https://thenewstack.io/monitoring-production-ai-agents/ | **UNAVAILABLE** (403 to the orchestrator on 2026-11-15). Its purported seven failure modes are UNVERIFIED and are not used. |
| live Temporal histories, mctl-api usage rows, Tempo, gitops `.status.yaml` | not-observed |
| mctl-api consumer side of the usage ledger (pricing, aggregation) | not-observed; it is another repository |

Duplicate-plan check: `grep` of `docs` and `tools` for "regress" finds only ADR 005, 008, 015, 017, 024 and the
security-finding runbook, none of which proposes a baseline-vs-candidate metric comparison. No local standalone
plan for #1014 was found. This is absence in this checkout, not proof that none exists elsewhere.

## Article taxonomy vs this report's cases

The article's seven modes could not be read, so **nothing here claims to accept, reject or map them**. The five
categories in "Synthetic cases" (cost, latency, reliability, authority, evidence) are this spike's own hypotheses,
chosen because the sources above carry a field for each. They are not a verified or complete list.

## Inventory: what exists today

State: **emitted** (a producer writes it), **consumed** (code in this repo reads it to decide something),
**proposed** (only in a spike or issue). Nothing in this table is a measurement of a real run.

| area | field / symbol | emitted | consumed in this repo | unit / denominator / missingness |
|---|---|---|---|---|
| success | `DevLoopResult.outcome`, `.ended`, `.pr`, `.deploy`, `.incidents` (`dev_loop.py:1207-1250`) | yes, per workflow run | `RESTARTABLE_ENDED_PREFIXES` (L1194) for restart only | one record per run; `None` means stage did not run, not failure |
| success | `OutcomeLink.outcome` in `OUTCOMES` (`context_eval.py:87`, `link_outcome` L572) | yes, on eval records | stored on the record; no code in the files read computes a pass rate from it | `unknown` + `reason_code` when ledger fields missing; `in-progress` is not a failure |
| quality | `EvalMetrics.selected_precision/useful_recall/f1` (L273-275) | yes, only with `CaseLabels` | no gate consumes them (ADR 019 L355: no automatic promotion on a metric threshold) | `null`, never `0.0`, when unlabelled (L271) |
| quality | `stale_rate`, `duplicate_rate`, `noise_rate` (L282-284) | yes | not consumed for gating | ratios over selected sources / candidates; `0.0` on an empty denominator (L382, L392), which is **indistinguishable from a true zero** |
| cost | `context_bytes`, `context_tokens_estimate = ceil(used_bytes/4)` (L399) | yes | no | bytes; an estimate, not provider tokens |
| cost | usage record `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `web_search_requests` (`usage_ledger.py:169-175`) | yes, one per model per turn, as deltas | by mctl-api (not observed) | tokens; `None` fields are omitted, not zero (`_int`, L414) |
| cost | any USD field | **never emitted**: `_FORBIDDEN_COST_FIELDS` (L200); mctl-api prices at ingest | not observed | no local USD figure exists to compare |
| latency | `assembly_latency_ms` (`AssemblyCounters` L257) | only if the caller supplies counters | no | `null` when absent (L287) |
| latency | `duration_api_ms` (`usage_ledger.py:649`) | yes, when the SDK reports it | not observed | ms of API time, **not** wall clock |
| latency | wall-clock per stage, approval waits | **missing** (854 G7, deferred) | no | n/a |
| reliability | `num_turns` (`usage_ledger.py:628`) | yes | not observed | per session; the dedupe key falls back to `turns:{n}` (L631) |
| reliability | Temporal `RetryPolicy` bounds (`dev_loop.py:225, 267, 272, 319, 361, 633, 871, 894`) | policy only | by Temporal | the number of retries actually taken is not a field of `DevLoopResult`; not observed elsewhere |
| reliability | usage delivery `ATTEMPTS = 2`, undelivered count warning (`usage_ledger.py:129, 740`) | log line | no | process-local count |
| retries | `MAX_REVIEW_ATTEMPTS = 5` (`run_shepherd.py:455`) | constant | by the shepherd | review-round count as a metric is 854 G1 `pr.review_rounds`, proposed only |
| authority | `Decision.verdict` in {ALLOW, DENY, REQUIRE_APPROVAL}, `CODE_*` (`policy_checkpoint.py:60-124`) | yes, as `mctl.policy.decision` events (854 Baseline) | by the checkpoint itself | one per action request; `UNDECIDED_CODES` (L124) are unknowns, not denials |
| evidence | `assess_evidence` status in {fresh, missing, stale, mismatched, insufficient-observations} (`context_eval.py:95`) | derived | by `context_release` soak gate, ADR 019 table L187-198 | needs `now`, a `FreshnessPolicy`, and `_observation_key` identity |
| evidence | `evaluate_review_gate` result (`review_receipts.py:821`): unknown, stale, missing, not_independent, advisory_only, then the receipt outcome | derived | by the caller of the gate | keyed on subject binding and request id |
| aggregate | RED metrics `mctl.agent.invocations` etc. | **proposed** (854 G3, filed as #873) | no | not emitted |
| aggregate | pass-rate | **no producer found** in the files read | none | there is no pass-rate field to regress against; this is the first thing to confirm with the owner |

Finding 1: in the files read, no code computes a pass rate, so "metrics that pass-rate misses" has no concrete
baseline in the repo. The comparison below defines pass-rate explicitly as the fraction of cases whose
`OutcomeLink.outcome == "succeeded"`, an assumption stated here and not observed in a consumer.

Finding 2: the emitted numeric signals (`EvalMetrics`, usage tokens, `num_turns`, `duration_api_ms`) are not
consumed by any gate in the files read. The consumed signals are identity and freshness (`assess_evidence`) and
review binding (`evaluate_review_gate`). So today a cost, latency or retry regression is visible only if a human
reads the records.

## Identity: stage and attempt

| axis | key | source | hazard |
|---|---|---|---|
| stage | `devloop_stage` in `DEVLOOP_STAGES` (`usage_ledger.py:157`) | ledger records | `reviewer` is produced by another collector (`.github#126`, not observed); agents absent from `_AGENT_STAGES` record no stage (L159-161), so their cost is unattributed, not zero |
| execution | `(work_item_id, execution_id)` (`context_eval.py:951`) | eval records | a record with no `store_ref` or `execution_ref` is never an observation (L941-959) |
| retry | store retry identity; a retry restamps snapshot id, `content_hash`, `observed_at` (L946-948) | eval records | counting records instead of executions double-counts retries |
| approval attempt | `_approval_attempt` (`dev_loop.py:2327`) | workflow state | a stale decision from an ended attempt is not adopted (L2364-2388) |
| review | `request_id`, `attempt_id`, `lineage_root`, `authoring_execution_id` (`review_receipts.py:856-873`) | receipts | an unobserved worker identity is `not_independent`, not independent |

Rule for the experiment: a metric is computed per `(stage, execution_id)` and aggregated over executions, never
over raw records or turns.

## Denominators, units and missingness

- Success: denominator is executions with a terminal `OutcomeLink.outcome` in {succeeded, failed, abandoned}.
  `in-progress` and `unknown` are excluded from the denominator and reported as their own counts. `unknown` is
  never counted as failure or success.
- Cost: sum of token fields per `(stage, execution_id)` over records that carry them. An execution with no usage
  record has cost `unknown`. A baseline mean uses only executions with a known cost, and the report states the
  known/total count. Token counts are not converted to USD (no local price).
- Latency: `duration_api_ms` per execution, summed over turns. Absent is `unknown`. Never mixed with wall clock.
- Reliability: retries taken, turns, and review rounds per execution. None are emitted as retry counts today, so
  the experiment uses only values present in the synthetic fixture, and reports real-data coverage as
  `not-measured`.
- Ratios with an empty denominator are `unknown`. This deliberately differs from `EvalMetrics`, which returns
  `0.0` for `stale_rate`, `duplicate_rate` and `noise_rate` when the denominator is empty (L382, L392); the
  harness must read the denominator, not the ratio, to avoid that false zero.

## False positives and stale or mismatched evidence

| risk | cause | handling in the experiment |
|---|---|---|
| tiny-n noise | one outlier flips a mean | require a minimum known-count per arm (default 3, mirroring ADR 019's 3-observation minimum, L165); below it the verdict is `INSUFFICIENT` |
| retry double-count | records keyed on snapshot id | key on `_observation_key` semantics |
| unit mixing | `duration_api_ms` vs wall clock; byte estimate vs provider tokens | one named field per metric, no cross-field comparison |
| mixed model or price | `price_source` / provider differ (`usage_ledger.py:192`) | candidate and baseline must carry equal model and `price_source`, else `MISMATCHED` |
| stale baseline | baseline older than the window | reuse `assess_evidence` semantics with explicit `now` and a window supplied by the fixture, not a new constant |
| mismatched baseline | evaluator or strategy identity differs (`EVALUATOR_VERSION` L74, `METRICS_CONTRACT_VERSION` L78) | `MISMATCHED`, no comparison |
| expected change | a deliberate context-size increase | the fixture declares `expected_delta` per metric; only an undeclared change is a regression |
| false zero | empty-denominator ratio | see above |

## Proposed experiment (smallest frozen offline baseline vs candidate)

Everything below is **proposed**. Nothing exists yet. It reuses `EvalMetrics`, `OutcomeLink`, `assess_evidence`
and `evaluate_review_gate` as the metric and evidence vocabulary, and the #918 spike's outcome style. It adds no
framework, no gate and no emission.

Design: a frozen JSON fixture of N executions per arm (baseline, candidate), each with `stage`, `execution_id`,
`outcome`, and optional `tokens`, `duration_api_ms`, `turns`, `retries`, `review_rounds`, `policy_code`,
`review_gate`, `evidence_status`, `observed_at`. Fields are hand-authored and marked `origin: synthetic`. No real
run, log or price is used. A pure function reads the fixture and emits, per metric, one of `REGRESSION`,
`NO_CHANGE`, `IMPROVEMENT`, `INSUFFICIENT`, `MISMATCHED`, `UNKNOWN`.

### Metric computation

Let B and C be the executions of baseline and candidate with a known value for the metric (known-count n_B, n_C).

- pass_rate = succeeded / (succeeded + failed + abandoned).
- tokens_per_exec = mean over known of (input + output + cache_write tokens). Cache reads are reported separately.
- api_ms_per_exec = mean over known of summed `duration_api_ms`; also p-max, because a mean hides one slow tail.
- reliability: mean `retries`, mean `turns`, mean `review_rounds`, each over known.
- authority: count of executions whose `policy_code` is in the allow set while the fixture's expected verdict is
  `REQUIRE_APPROVAL` or `DENY` (`expected_verdict`, `policy_checkpoint.py:534`), reported as
  `unexpected_allow`; plus `undecided` = codes in `UNDECIDED_CODES`, kept apart.
- evidence: share of executions whose `review_gate` is not in {independent-current} and share with
  `evidence_status != "fresh"`, each over executions where the field is present.

Regression rule: `REGRESSION` when n_B >= 3 and n_C >= 3 and (C - B) exceeds the fixture-declared tolerance and is
not covered by `expected_delta`. Tolerances are fixed in the fixture before the run (frozen) and are synthetic
hypotheses, not recommended production thresholds.

### Synthetic cases (pass-rate stays equal, e.g. 1.0 vs 1.0)

| id | hypothesis category | construction | expected oracle |
|---|---|---|---|
| S1 | cost | candidate tokens_per_exec 2.0x baseline, all succeed | tokens `REGRESSION`, pass_rate `NO_CHANGE` |
| S2 | latency | candidate api_ms p-max 5x, mean +20% (inside tolerance) | p-max `REGRESSION`, mean `NO_CHANGE` |
| S3 | reliability | candidate mean retries 0.2 -> 2.0, still succeeds | retries `REGRESSION` |
| S4 | reliability | candidate review_rounds 1 -> 4 (limit 5) | review_rounds `REGRESSION` |
| S5 | authority | candidate has 1 `ALLOW` where expected is `REQUIRE_APPROVAL` | `unexpected_allow` `REGRESSION` (any single case, since a count is not a mean) |
| S6 | evidence | candidate review_gate `not_independent` on 2 of 4 | evidence `REGRESSION` |
| S7 | evidence | candidate `evidence_status` `stale` | evidence `REGRESSION`, reason from `assess_evidence` |

### Negative controls (must NOT flag)

| id | construction | expected |
|---|---|---|
| N1 | identical arms | all `NO_CHANGE` |
| N2 | candidate cost 2.0x but declared in `expected_delta` | `NO_CHANGE` |
| N3 | candidate cost field missing for all | `UNKNOWN`, not `IMPROVEMENT` (unknown is not zero) |
| N4 | n_C = 2 with large delta | `INSUFFICIENT` |
| N5 | candidate model differs from baseline | `MISMATCHED` |
| N6 | candidate `unknown` outcomes only | pass_rate `UNKNOWN`, excluded from denominator |
| N7 | retry records duplicated per snapshot id | counted once per execution |
| N8 | candidate faster and cheaper | `IMPROVEMENT`, no regression |
| N9 | `UNDECIDED_CODES` present, no allow | `undecided` count reported, `unexpected_allow` 0 |

### Mutation plan

Mutate the harness (not the repo runtime) and require the suite of S and N cases to fail on each: (a) treat
missing as 0, (b) count records instead of executions, (c) drop the n >= 3 guard, (d) ignore `expected_delta`,
(e) compare across model or `price_source`, (f) merge `unexpected_allow` into a mean, (g) swap stage keys, (h) use
wall clock in place of `duration_api_ms`. A mutant that still passes means the corpus is too weak. Expected: 8 of 8
killed. Results are **not-measured** until implemented.

### Expected outcome of the experiment

If S1-S7 are flagged and N1-N9 are not, the offline comparison separates the five hypotheses from pass-rate on
the fixture. That shows only the harness separates synthetic cases. It does not show that production DevLoop
regresses in these ways, and it says nothing about real rates or thresholds.

## Minimal separately approvable implementation allowlist

A separate owner approval is needed. Only these paths, no runtime import changes:

1. `tools/spike_1014_regression_metrics.py` (pure offline harness, reads the fixture, no network)
2. `docs/spikes/1014/fixture.json` and `docs/spikes/1014/results.json`
3. `tests/test_spike_1014_regression_metrics.py` (S, N and mutation cases)
4. an update of `docs/spikes/devloop-regression-metrics.md` with measured results

Out of scope: any change to `orchestrator/`, `context_eval.py`, `context_release.py`, `review_receipts.py`,
`policy_checkpoint.py`, gates, ADRs, Argo or gitops config, telemetry emission (854 F1-F3 own that), or promotion
policy.

## Recommendation

Exactly one, **pending owner**: `RUN_OFFLINE_BASELINE_CANDIDATE_REGRESSION_AUDIT`: approve the allowlist above to
run the frozen synthetic experiment and record measured results here. It changes no promotion gate and makes no
global policy claim.

## Unknowns and deferrals

- Pass-rate has no producer in the files read; its definition here is an assumption (Finding 1).
- #848, #974 and `.github#313` overlap is unchecked (no network); DEFER until the owner supplies excerpts.
- The seven-mode article taxonomy is unverified and unused.
- Real distributions, tolerances, retry counts actually taken, mctl-api consumption of ledger rows: not-measured.
- `policy_checkpoint.py`, `ci_checks.py` and `run_shepherd.py` were only symbol-checked.
