# ADR 015 — Context evaluation contract: measuring retrieval quality

> **Status:** accepted
> **Date:** 2026-09-26
> **Issue:** mctlhq/mctl-agents#266 (parent: ADR 009 amendment 1's closing
> line, `docs/adr/009-context-snapshot-contract.md`: "Wiring
> promotion/rollback of strategies is #472; measuring them is #266")
> **Supersedes:** nothing. It defines the evaluator's contract; it does not
> reopen `ContextSnapshot`'s schema (ADR 009) or its ranking behaviour (ADR
> 009 amendment 1, #471).

## Context

`orchestrator/context_assembly.py` assembles, ranks, deduplicates, budgets
and seals a `ContextSnapshot` for every issue-investigation (#265, #471),
and every snapshot of a store execution (`we_...`) is persisted in mctl-api
at `WORK_CONTEXT_ROLLOUT_MODE >= observe` (#431, proven live in #490).
`AssemblyMetrics` (`context_assembly.py`) already counts what the pipeline
did — candidates, drops, bytes, latency. It does not state whether the
evidence the model needed was actually selected, and it does not compare
one strategy against the other on the same input.

This ADR fixes the contract for a retrieval/context evaluator that is
**independent of** final model-output scoring (#60), so the platform can
tell bad reasoning over good context apart from good reasoning over
missing, stale, noisy or duplicated context, and can detect a retrieval
regression caused by a ranking, filter or config change.

## Decision

### 1. Identity — two hash pairs, verified before any metric

Two provenance modes (mctlhq/mctl-agents#528 amends this section to name
the second one explicitly):

- **`store-backed`** — the evaluated snapshot was persisted; verification
  checks both the document identity and the store identity below, and the
  record carries `store_ref`.
- **`execution-observed`** — the evaluated snapshot was never persisted (a
  Slice B `observe` shadow pass's non-authoritative candidate,
  `evidence_kind: "observe-candidate"`); verification checks document
  identity only, and the record carries `execution_ref` — the store
  execution the candidate was assembled in — instead of a `StoreRef`, since
  there is no stored document for a store identity check to describe.

Every evaluation first recomputes both identities a snapshot can carry,
and computes no metric until both check out:

- **Document identity** — `cs-` + `content_hash`: `recompute_content_hash
  (snapshot) == snapshot.content_hash` and `snapshot.snapshot_id == "cs-" +
  content_hash[7:23]` (`context_snapshot.py`). This hash excludes
  `created_at`.
- **Store identity** — `cs_` + `store_content_hash`: `hash_bytes
  (canonical_bytes(snapshot)) == store_ref.store_content_hash`
  (`work_context/snapshots.py`), a hash over the *whole* canonical document,
  minted by mctl-api (or, on a cross-attempt replay, `==
  store_ref.local_content_hash`; see below). `store_snapshot_id`
  (`cs_`-prefixed) is opaque: it is
  carried and compared to what the store reported, and is never recomputed
  locally.

`StoreRef` is `{work_item_id, execution_id, store_snapshot_id,
store_content_hash}`, plus `local_content_hash` on a cross-attempt replay
only: when `persist` finds after a 409 that the store kept another attempt's
document, differing only in retry-volatile fields, `store_content_hash`
stays mctl-api's own digest and `local_content_hash` carries the hash this
attempt sealed. Verification then reports `store_match: retry-equivalent`
instead of `stored`, so a retried execution is measured without claiming the
store holds its bytes. When no store execution exists, or the persist
answer was not `stored`, evaluation still verifies the document identity
and records `store_ref: null` rather than failing. A hash mismatch, on
either pair, produces a record with `verdict: "hash-mismatch"`, the two
disagreeing field names, and **no metrics at all** — a document whose
identity does not hold is not measured.

### 2. Metrics — distinct from final model output

Computed from the sealed snapshot plus the pipeline's own counters, never
from re-derived text comparison:

| Metric | Definition |
|---|---|
| `selected_precision` | selected ∩ useful / selected, over labelled candidates; `null` when a case carries no labels |
| `useful_recall` | selected ∩ useful / declared useful; `null` when unlabelled |
| `f1` | harmonic mean of the two; `null` when either is `null` |
| `missing_expected` | declared useful ids that no candidate carried |
| `stale_rate` | sources whose `freshness.staleness == "stale"` OR `selection.reason_code == "stale-demoted"`, over selected sources — the ranked strategy demotes rather than drops, so a demoted source still counts as stale |
| `duplicate_rate` | `dropped_duplicate` / `candidates_total` |
| `noise_rate` | selected-but-labelled-noise / selected |
| `context_bytes` / `context_tokens_estimate` | `budget.used_bytes`; `ceil(used_bytes / 4)`, declared an estimate derived from bytes, never a billed token count (billed cost stays with `usage_ledger.py`, ADR 012) |
| `assembly_latency_ms` | from the pipeline's own counters |
| `capability_calls` | collector calls plus store round trips (0 in offline replay) |
| `coverage_by_kind` | per source kind: candidates, included, bytes |
| `conflicts_detected` / `conflicts_expected_detected` / `conflict_sources_capped` | read from `snapshot.conflicts` only, never re-derived from text |

Unlabelled candidates report `selected_precision`/`useful_recall`/`f1` as
`null`, never as `0.0` or `1.0` — a case with no ground truth makes no
claim about quality.

### 3. Telemetry safety

Every record carries only ids, kinds, closed-vocabulary codes, counts and
ratios — no `locator`, no `selector`, no payload byte, no rendered text —
the same rule `AssemblyMetrics.to_log_dict()` already keeps. A `source_id`
that is not a plain token (`context_assembly._SAFE_SOURCE_ID`) is replaced
by its kind rather than emitted.

Every record names itself: `record_kind: "context-eval"`,
`evaluator_name`, `evaluator_version` — so a metric-definition change is
attributable and an old number is never silently compared against a new
rule.

### 4. Fixture contract

Every fixture case is run through the real pipeline (`context_assembly
.run_pipeline`) under **both** `deterministic-fixed-order` and
`trust-freshness-ranked`, sealed with a fixed `now` and a fixed
`ExecutionCorrelation`, so results are byte-stable — never re-implemented
in test code, which would measure a copy of the pipeline instead of the
system. A committed `baseline.json` holds every metric for every (case,
strategy) pair; the suite fails when any metric drifts from it, and the
baseline is regenerated only by an explicit, deliberate run — never
silently in CI.

### 5. Outcome-link rule

A live evaluation record cannot know the outcome of the execution it
describes yet, so it carries `outcome: null` and its join keys
(`execution_id`, `context_snapshot_id`). Resolving the outcome afterward
is layered outside the evaluator:

1. On the store ledger — `WorkItem.state` and the matching `ExecutionRef
   .phase`, joined on `execution_id`, `prior_execution_ids` and
   `resumed_from_snapshot_id`, when a `we_`-prefixed execution exists.
2. Only when no store execution exists, the published proposal's
   `.status.yaml` `status` field, with `outcome_source: "status-yaml"`
   recorded so the weaker link is visible.

### 6. Context relevance is never authorization (restated from ADR 009 sec. 5)

A retrieval-quality score orders and measures what the model was shown. It
grants nothing, blocks nothing, and is never read by a policy or
capability-eligibility decision (ADR 009 sec. 5/6; ADR 014). This
evaluator adds a second read-only measurement of the same non-authoritative
data; it does not change what boundary enforces anything.

### 7. Evidence freshness and promotion readiness

Added by mctlhq/mctl-agents#526, alongside the delivery of the module this
ADR had deferred (sec. "Implementation map" below). `context_eval.py` records
each evaluation's evidence `kind` from the closed set `{"none",
"fixture-baseline", "stored-replay", "live"}`, plus the evaluated strategy's
name, version, ranker (name/version), the evaluator's own identity
(`evaluator_name`, `evaluator_version`, `metrics_contract_version`), and the
strategy version's catalog identity: mctlhq/mctl-agents#472's
`contentHash`/`implementationHash` for that (name, version), obtained by the
CALLER (the live emitter, the fixture harness, the replay CLI) through
`orchestrator.context_release.load_version`, imported inside the function
body — `context_eval` itself never reads the catalog. `pipeline_source_hash`
travels alongside as a diagnostic only; it is never an assessment input.

`assess_evidence(records, *, expected, now, policy)` returns a status from
the closed set `{"fresh", "missing", "stale", "mismatched",
"insufficient-observations"}` with a machine-readable reason code, applying
this fixed precedence (never order-dependent):

1. No usable record, or the newest one's `evidence_kind == "none"` ->
   `missing`.
2. Any declared-identity field (strategy name/version, ranker name/version)
   differs from the promotion candidate's -> `mismatched`.
3. The catalog identity (`strategy_content_hash`/
   `strategy_implementation_hash`) is empty (the caller could not load the
   version) or differs from the candidate's -> `mismatched`, with
   `catalog-identity-unavailable` when empty.
4. The newest observation (the newest store-backed record, step 5; none at
   all -> `insufficient-observations`, `no-store-backed-observation`) is
   older than the caller-supplied freshness window -> `stale`.
5. Fewer than the caller-supplied minimum number of consecutive
   newest-first observations agree on the full identity -> `insufficient-
   observations`. Observations, not records, are counted: one per execution
   — `store_ref.execution_id` for a `store-backed` record, or
   `execution_ref.execution_id` for an `execution-observed`
   (`observe-candidate`) record (mctlhq/mctl-agents#528) — so the retries of
   one execution count once, under either provenance mode. A record with
   neither `store_ref` nor `execution_ref` is not a promotion observation —
   a retry restamps every local identity field, so nothing tells its
   attempts apart — and is never counted. The run ends at the first
   `evidence_kind: none` record. `observations` in the assessment reports
   this deduplicated count.
6. Otherwise -> `fresh`.

The freshness window and minimum-observation count are ADR 019's **v1
promotion policy constants** (mctlhq/mctl-agents#472 Slice A): 7 days
(`ADR019_V1_FRESHNESS_WINDOW_SECONDS = 604800`) and 3 consecutive
observations (`ADR019_V1_MIN_CONSECUTIVE_OBSERVATIONS = 3`). `context_eval`
exports them as named constants and takes them as explicit
`FreshnessPolicy` parameters — no `from_env()`, no environment variable, no
inference from observation history. Changing either value is an ADR 019
amendment, not a runtime knob.

`assess_evidence` reports a status and a reason code only. It does not
decide whether a `fresh` assessment permits a production promotion — that
decision belongs to mctlhq/mctl-agents#528 (#472 Slice C), reading ADR 019.
Production promotion MUST refuse evidence whose `kind` is `"none"`; a
`fresh` assessment is `assess_evidence`'s only possible answer for evidence
that could license a promotion, and it is unreachable for `kind == "none"`
by construction.

## Alternatives

See design.md for the full comparison; summarized:

1. Extending `AssemblyMetrics` in place — rejected: cannot score one case
   under both strategies, nor replay against a snapshot sealed days ago,
   and would push label data into the production assembly path.
2. Deriving context quality from #60's final-output score — rejected: the
   exact conflation this ADR exists to avoid.
3. A new mctl-api table for evaluation records — rejected for v1: the
   durable artefact already exists (#431/#490); a stored evaluation record
   would be a derived duplicate that can silently disagree with its source.
4. Re-implementing ranking in the test suite — rejected: it would measure a
   copy, not the system, hence the `run_pipeline` extraction this ADR's
   fixture contract depends on.
5. A model judge for relevance labelling — rejected: out of scope (no
   learned model in the evaluation path), non-deterministic, and an
   injection surface in a measurement path.

## Non-goals

- Training or shipping a production reranker, or any learned model in the
  evaluation path.
- One universal retrieval metric for every agent. The fixture set and
  metrics are the issue-investigator's; other agents may reuse the module.
- Storing raw production context anywhere, or a new mctl-api route.
- Changing what the assembler selects, or promoting/rolling back a
  strategy on this evidence (that is #472,
  `docs/adr/019-context-strategy-release-contract.md`).
- Replacing or re-implementing #60's final-output evaluation.

## Platform impact

- **Migrations:** none. No schema change, no new mctl-api route.
- **Backward compatibility:** additive. `ContextSnapshot`'s `content_hash`
  inputs are untouched, so every already-persisted snapshot keeps its
  identity. The `run_pipeline` extraction inside `context_assembly.py` is a
  behaviour-preserving move, pinned by the existing golden fixture and by
  `tests/test_context_ranking.py`.
- **Security/telemetry:** records carry ids, kinds, codes and numbers only,
  per sec. 3 above.

## Implementation map

This proposal landed incrementally, matching ADR 009's own precedent of
shipping the contract ahead of a full producer. #266 delivered:

```
docs/adr/015-context-evaluation-contract.md   # this document
docs/adr/009-context-snapshot-contract.md     # follow-up table row (b) + a new row
orchestrator/context_assembly.py              # run_pipeline extraction (behaviour-preserving)
tests/test_context_assembly.py                # run_pipeline unit coverage
```

mctlhq/mctl-agents#526 delivered the rest, plus sec. 7 above:

```
orchestrator/work_context/snapshots.py   # StoreRef, store_ref_from
orchestrator/context_assembly.py         # returns the persist answer; AssemblyResult.store_ref
orchestrator/context_eval.py             # the evaluator itself: identity, metrics, telemetry
                                          # safety, outcome linking, freshness/promotion readiness
orchestrator/run_issue_investigator.py   # guarded live emission (ISSUE_INVESTIGATOR_CONTEXT_EVAL)
orchestrator/run_context_eval.py         # read-only stored-replay CLI
tests/fixtures/context_eval/             # 7 curated cases + the committed baseline
tests/test_context_eval.py               # the fixture harness, and every T1-T29 in tasks.md
tests/test_work_context_snapshots.py     # StoreRef/store_ref_from/persist-return-value coverage
docs/adr/009-context-snapshot-contract.md  # follow-up row (f) annotated with #526
README.md                                # "Context evaluation" runbook subsection
.env.example                             # ISSUE_INVESTIGATOR_CONTEXT_EVAL, commented out
```

Sections 1 through 6 above were normative for that work and were not
reopened by it.
