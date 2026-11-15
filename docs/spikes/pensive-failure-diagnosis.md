# Spike #1073: Pensive-style failure diagnosis for MCTL

STATUS: BOUNDED_PENSIVE_PATTERN_PILOT (proposed verdict; the issue owner records the final one)

Refs mctlhq/mctl-agents#1073. Research only. This file is the only change. No code, schema,
config, instruction, DevLoop, live-log, model, production, release or permission change was made or
is requested here. Allowed verdict tokens: `REUSE_EXISTING_DIAGNOSTICS`,
`BOUNDED_PENSIVE_PATTERN_PILOT`, `DEFER`, `REJECT`.

Base: `origin/main` at `4d7efd3555eafbb4af969687e029c5b5b9863b14`, fetched 2026-11-15. The old branch `fix/auditor-empty-namespace-selector-954` was left untouched.

## 1. Evidence labels used below

- **SHIPPED** - read in this checkout at the cited path. Line numbers are as read on 2026-11-15.
- **PROPOSED** - appears only in an issue, ADR "proposed" status, or this document. Not a contract.
- **UNVERIFIED** - could not be read offline (no network was allowed). Not assumed either way.
- **VENDOR** - Netflix's Pensive article (Oct 2026), read by the orchestrator, paraphrased only.
  It is an observation about Netflix, not an MCTL result and not causal proof for MCTL.

## 2. Vendor pattern, bounded paraphrase (VENDOR)

1. Existing deterministic classifiers stay. The model is an add-on, not a replacement.
2. The model selects log line *ranges*; the excerpt is rebuilt from the original source, so the
   model never writes the evidence text. It can still select the wrong span.
3. Extraction runs under a shared deadline with a fallback; deeper diagnosis is on demand only.
4. A cache claim coordinates one failed attempt; abandoned work can be reclaimed.
5. Frozen archived inputs support inclusion, precision, noise and latency evaluation and release
   checks.

Not adopted: Netflix's retry authority, any numeric limit, any retention period. None is an
approved MCTL contract and none is copied here as a default.

## 3. Current MCTL baseline (SHIPPED unless stated)

### 3.1 Diagnostics and outcome classification

| Capability | Where | What it does |
|---|---|---|
| Single attempt-end mapping | `orchestrator/run_implementer.py:5527` `_classify_result` | One mapping from an `ImplementResult` to `(outcome, stop_reason_code, exit_code)`; rate-limit, stale source, budget, policy gate, claim refused, fenced, harness failure. Deterministic, no model. |
| Completion record | `orchestrator/implementation_completion.py` (ADR 021) | Sealed completion keyed `(run_id, step, attempt_id)`; `stop_reason(code, detail)` redacts and bounds `detail` via `redact_text` (line 199, uses `contains_credential` from `orchestrator/redaction.py:63`; a credential-shaped value is replaced whole, never partly masked). |
| Refusal marker | `run_implementer.py:486` `_read_refusal_marker` | Reads a refusal reason the agent wrote. |
| Probe failure classes | `orchestrator/assurance_harness.py:77` `FAILURE_CLASSES`; ADR 023 | `failure_class` and `stop_reason` per probe. |
| Safety-classifier interruption | `docs/spikes/796-rea-investigation-evidence.md` (#849) | Notes `orchestrator/` had no detection for this notice at that time. Current status UNVERIFIED. |
| Evidence envelope | `orchestrator/execution_evidence.py`, ADR 018 (status "proposed" in the ADR header, module present) | Joins existing canonical records by reference. Declares no free-text, path or payload field; `from_dict` rejects unknown keys; `Gap` codes include `redacted_out`. |
| Context evaluation | `orchestrator/context_eval.py`, ADR 015 (accepted) | Precision/recall/noise/latency metrics, `null` for unlabelled, fixture `baseline.json` drift failure, `assess_evidence` with the closed status set `fresh/missing/stale/mismatched/insufficient-observations`, one observation per execution. |
| Tracing | `orchestrator/tracing.py`, `tracing_sdk.py`, `docs/observability/execution-traces.md` | One trace per DevLoop run (#195 as cited by spike 854), allowlisted attribute keys (`key_allowed`), redaction guard on export, log correlation by `trace_id=` (mctl-agent#97). |
| Observability gaps already inventoried | `docs/spikes/854-agent-observability-gaps.md` | Adopted subset F0-F3 (eval events, run outcome attributes, RED metrics); F4-F7 deferred. |

There is **no** shipped component that reads raw agent or pod log lines and selects spans from
them. Log correlation (mctl-agent#97) finds lines by trace id; it does not extract meaning.

### 3.2 Attempt identity today

- `orchestrator/execution_provenance.py` (`execution_attempt_v1`): `attempt_id = "at_" +
  sha256(canonical_json([version, "attempt", engine, engine_run(engine_ref)]))[:32]`.
  `engine_run` strips everything after `#`. For a DevLoop run ref of the form `<loop>#run_<id>`
  (`issue_ref.run_engine_ref`), that makes the id depend on the loop workflow id only. So, as
  read, two runs of one loop (for example across `continue_as_new`, which mints a new run id per
  `docs/observability/execution-traces.md:65`) derive the same `at_` id. Whether that is intended
  for the attempt concept (versus a lineage concept) is UNVERIFIED; the mctl-api document that
  defines it was not readable.
- `run_implementer._resolve_attempt_id` (line 1121): `WORKFLOW_UID` if set, else
  `sha256(service|slug|owner_epoch|attempt_ordinal|HOSTNAME)`. Callers at lines 3088 and 4865 pass
  `owner_epoch=0`. Its docstring records a residual: two processes on one host without
  `WORKFLOW_UID` collide.
- ADR 021 line 61 records primary and fallback steps can have coinciding `attempt_id`s, which is
  why the completion key also carries `step`.
- ADR 015 sec. 7 already states retries of one execution count once and a record with no
  `store_ref`/`execution_ref` cannot tell attempts apart.

Conclusion: three attempt-identity notions exist and none is designed to key *diagnosis of one
failed attempt's log bytes*. This spike proposes one such key (section 6) without changing any of
them.

## 4. Dedup map

Issue bodies and titles were not readable offline. Rows marked UNVERIFIED are matched by number
only, from references found in this checkout. Nothing here takes over any of them.

| Ref | What this checkout says | Relation to #1073 |
|---|---|---|
| #195 | Spike 854: "already ships one OpenTelemetry trace per DevLoop run". SHIPPED. | Reuse. Diagnosis output must ride existing trace/redaction rules. |
| #199 | ADR 018: "the #199 evidence store"; Tier B storage owned by mctl-api. | Reuse. Any stored diagnosis is an `ExecutionEvidence` ref, not a new store. |
| #196, #310, #698, #699, #700, #912, #1032 | Not referenced in any file of this checkout. UNVERIFIED. | Must be read by the owner or orchestrator before approval of any follow-up. |
| mctl-agent#97 | Log-to-trace correlation, SHIPPED (`tracing.install_log_correlation`, `pod_root_span`). | Reuse. Pensive-style spans would key off the same `trace_id`. |
| .github#342 graph-wide RCA | Not referenced here. UNVERIFIED. | Out of scope: graph-wide RCA across services is not per-attempt span extraction. |
| #854 / #871-#873 | Eval events, run outcome, RED metrics (adopted), F4-F7 deferred. | Complementary. This spike's proposal does not add telemetry names. |
| #266 / ADR 015 | Evaluator and promotion boundaries. | Reuse as the evaluation base (section 9). |
| #1014 | Metrics, researched separately by another agent in its own Markdown. | Not touched. |

## 5. Tier A: deterministic bounded extraction (PROPOSED)

Goal: from one failed attempt's log bytes, produce a few cited spans without a model. This is the
Netflix "existing classifier stays" half, and it is what the pilot would build and measure.

Contract (all PROPOSED):

- Input: an immutable byte blob plus its `sha256` and a source label. The extractor never
  re-reads a live source; if the bytes' hash differs from the recorded one it refuses.
- Output per span: `{source_sha256, line_start, line_end, rule_id, rule_version}`. 1-based
  inclusive line spans over the original bytes. The excerpt text is *derived on demand* from the
  original source at read time, never stored in the record and never produced by a model.
- Bounded by explicit parameters the pilot must choose and justify (max input bytes, max spans,
  max span lines, max total excerpt bytes, deadline). **Values are intentionally not proposed
  here**; they are pilot outputs, measured on fixtures, and no Netflix figure is imported.
- Deterministic: same bytes and same rule version give the same spans, byte for byte.
- Refusal, not repair, on any of: hash mismatch; span out of range or inverted; span not on line
  boundaries; a span whose derived excerpt makes `contains_credential` true; a span overlapping
  a region marked redacted; oversize input; invalid UTF-8 beyond a stated tolerance; deadline
  exceeded. A refusal is a closed reason code and yields "no spans", never a partial excerpt.
  This reuses the `redact_text` policy: a credential-shaped value is dropped whole.
- Unknown is not zero: an unrun or refused extraction records `unknown`/`null` metrics, never
  `0`, matching ADR 015 sec. 2.
- Authority: none. Output is observability and operator context. It grants nothing, blocks
  nothing and is never read by a policy, retry, budget or capability decision (ADR 009 sec. 5/6,
  ADR 014, ADR 015 sec. 6, spike 854's non-authorization rule).

## 6. Proposed immutable diagnosis-attempt identity (PROPOSED, no deployed schema invented)

Purpose: one stable key per *(failed attempt, exact input bytes, extractor version)*, so retries,
reset, replay, Continue-As-New and redelivery cannot alias or fork a diagnosis. Working name
`diag_key`. It is a derived value and needs no new storage to be defined; whether and where it is
persisted is a follow-up decision (section 10, Q2).

```
diag_key = "dk_" + sha256_hex( canonical_json([
    "diagnosis_attempt_v0-proposed",
    engine,                 # e.g. "temporal" or "argo"
    engine_ref_full,        # NOT engine_run(): keeps the "#run_<id>" suffix
    step, phase,            # as in ADR 021 AttemptBinding
    attempt_ordinal,        # integer; required, never defaulted
    owner_epoch,            # integer; required, never defaulted to 0
    input_sha256,           # sha256 of the exact log bytes diagnosed
    extractor_id, extractor_version
]) )
```

Rules: canonical JSON as `execution_provenance._cj` does (sorted keys, compact, ASCII). Full
64-hex digest, not a 32-hex truncation. Every component is required; a missing one is a refusal,
never an empty default (the `owner_epoch=0` hard-coding seen at `run_implementer.py:3088` and
`:4865` must not be inherited). No random or wall-clock component.

| Event | Expected effect on `diag_key` | Why |
|---|---|---|
| Retry of the same attempt after pod restart | Same key if same `attempt_ordinal` and same input bytes | Idempotent: a re-derive finds the existing work, like `_resolve_attempt_id`'s restart case. |
| New attempt (ordinal+1) | New key | Different failure, different evidence. |
| Reset (owner epoch bump) | New key | `owner_epoch` is a component. |
| Replay of the same recorded history | Same key | All components are recorded values; none is wall-clock or random. |
| Continue-As-New | New key per run | `engine_ref_full` keeps the run id. A separate non-key `lineage` field, if needed, joins runs (spike 854 already joins traces by `mctl.workflow.id`). |
| Redelivery of the same diagnosis request | Same key | Singleflight/cache hit (section 7). |
| Same attempt, log re-fetched and now different or truncated | New key | `input_sha256` differs; the old result is never served for new bytes. |
| Two pods on one host, no `WORKFLOW_UID` | Distinct only if `engine_ref_full` or ordinal differ | Known residual of `_resolve_attempt_id`; this spike does not hide it. Refuse if `engine_ref_full` is empty. |

Open point (UNVERIFIED): whether the existing `at_` id should instead absorb the run suffix. That
is mctl-api's contract; this spike proposes a separate key precisely so as not to change it.

## 7. Tier B: optional advisory on-demand diagnosis (PROPOSED, not recommended for the first follow-up)

Tier B is a model call that *selects line ranges* over Tier A's bounded candidate window. Shape
only; no values are approved.

- Advisory only. It has no authority over retries, escalation, merge, budget or any lifecycle
  state. Netflix's retry authority is explicitly not adopted. Any retry logic stays in the
  deterministic classifiers (`_classify_result` and the lifecycle contract).
- On demand: a human or a named read-only caller asks; nothing is triggered automatically by a
  failure.
- The model returns only `{line_start, line_end}` pairs. The excerpt is rebuilt from the original
  bytes and passes the same Tier A refusal checks. A model-selected span that fails any check is
  discarded, and the result is "no valid span", not a repaired one.
- Cache, keyed by `diag_key` plus `model_id` and `prompt_version`, so a model or prompt change
  never serves old results. Version fields are required.
- Singleflight: one in-flight diagnosis per key; concurrent requests wait on it or observe its
  result. A claim records its holder and lease deadline.
- Abandonment: a claim whose lease has expired may be reclaimed by a new request; the first
  holder's late result is rejected if the claim is no longer its own (the same
  `_status_is_still_ours` idea in `run_implementer.py:1501`).
- TTL and deadlines: explicit, owner-set parameters; **no value proposed** and none imported.
  Retention of any stored diagnosis is a data-protection decision for mctl-api (ADR 018 already
  rejects a 3650-day gitops retention); this spike makes no retention claim.
- Fallback: on deadline or error, return the Tier A result, or "unavailable". Failure of Tier B
  must never change a job's outcome.
- Provider: whichever the owner approves later; not chosen here. The log bytes may contain
  customer-adjacent text, so sending them to any provider is a policy question (Q3).

## 8. Fixture plan: at least 10 synthetic or sanitized cases (PROPOSED)

All fixtures are synthetic. No production, live or customer log is read, copied or sanitized
into the repository. Each case carries the frozen log bytes, their `sha256`, and an oracle of
meaningful spans (line ranges plus the reason code a human would expect). Authoring labels is
human work and the oracle is the ground truth, as in ADR 015's "labelled candidates".

| # | Case | Kind | Expected |
|---|---|---|---|
| F1 | Single clear error line in a short log | positive | The error line is included. |
| F2 | Error line buried in 10x repeated noise | positive | Included; noise lines not. |
| F3 | Stack trace with the root cause at the end | positive | Root-cause frame included. |
| F4 | Multiple unrelated failures; one fatal | positive | Fatal included; the benign warnings excluded. |
| F5 | Rate-limit/429 failure already named by `_classify_result` | positive | Classifier verdict unchanged; spans only add context. |
| F6 | Log truncated mid-line | edge | Span on a partial line refused or clipped to the last whole line per rule; stated. |
| F7 | Log with a fake credential-shaped token inside the error line | negative | Span refused (`contains_credential`); no partial masking. |
| F8 | Log with a pre-redacted marker region overlapping the error | negative | Overlapping span refused. |
| F9 | Healthy log, no error at all (negative control) | control | Abstain; zero spans; not a false "failure". |
| F10 | Empty and whitespace-only log | control | Abstain with `unknown`, not `0`. |
| F11 | Oversize log beyond the byte bound | edge | Refuse by size; bounded cost. |
| F12 | Non-UTF-8 and CRLF/mixed line endings | edge | Line numbering stable and documented. |
| F13 | Same bytes, two attempts (ordinals 0 and 1) | identity | Distinct `diag_key`. |
| F14 | Same attempt re-delivered | identity | Same `diag_key`, one result. |
| F15 | Same attempt, bytes changed by one byte | identity | Different `diag_key`; no stale hit. |
| F16 | Model-selected inverted or out-of-range span (Tier B mutation) | negative | Discarded. |

Metrics (all reported with the denominator, `null` when unmeasurable):

- **Meaningful-span precision**: spans overlapping an oracle span / spans emitted.
- **Inclusion (recall)**: oracle spans covered by some emitted span / oracle spans.
- **Abstention correctness**: of control cases, fraction with no spans emitted.
- **Noise rate** and **excerpt bytes** per case, as ADR 015 `noise_rate`/`context_bytes`.
- **Latency** per case from the pipeline's own timer, and **cost** (zero model tokens for Tier A;
  for Tier B, billed usage stays in `usage_ledger.py`, never an estimate presented as billed).
- A metric over an empty or unlabelled set is `null`, never `0.0` or `1.0`.

## 9. Reuse of existing evaluator and promotion boundaries

No new evaluation framework. The pilot would add *cases and a metric set* in the shape ADR 015
already fixes:

- Record names itself (`record_kind`, `evaluator_name`, `evaluator_version`) so an old number is
  never compared with a new rule.
- Frozen fixtures plus a committed baseline that fails on drift and is regenerated only by an
  explicit run (ADR 015 sec. 4).
- Evidence freshness and promotion use `assess_evidence`'s closed statuses and ADR 019's policy
  constants as given; this spike changes no constant. Whether a diagnosis evaluator should even
  *be* a promotable strategy is UNVERIFIED and not proposed; for the pilot, results are
  informational only.
- Netflix's archived-input evaluation is the same idea as ADR 015's fixture contract, which is
  why no new harness is justified.

## 10. Options and the single proposed verdict

| Option | Summary | Cost | Risk |
|---|---|---|---|
| REUSE_EXISTING_DIAGNOSTICS | Keep `_classify_result`, completion `stop_reason`, traces and log correlation as is. Do nothing new. | None. | Leaves "which lines in a 40k-line log matter" to humans; no measurement of whether that is a real pain. |
| **BOUNDED_PENSIVE_PATTERN_PILOT** | Offline, fixture-only Tier A extractor plus evaluator cases and the section 6 key as a pure function. No runtime wiring, no model. | Small, local, reviewable. | Could duplicate an UNVERIFIED listed issue; mitigated by the gate below. |
| DEFER | Wait for evidence of operator pain or for #854 F1-F3 to ship. | None now. | Loses cheap offline learning; no pain measurement either. |
| REJECT | Treat model-assisted span selection as out of scope. | None. | Forecloses Tier B without measuring Tier A. |

**Proposed verdict: `BOUNDED_PENSIVE_PATTERN_PILOT`**, limited to Tier A offline. Reasons: the
pattern's valuable part (frozen inputs, span inclusion/precision, refusal on invalid spans) needs
no model and no production access; MCTL has no shipped log-span extraction, so
`REUSE_EXISTING_DIAGNOSTICS` would not address the gap; Tier B has real unresolved policy
questions, so it should not be in the first step. This is conditional: if the owner's dedup read
of #196/#310/#698/#699/#700/#912/#1032 shows one already covers span extraction, the verdict
should become `REUSE_EXISTING_DIAGNOSTICS` pointing at that issue. There is no MCTL measurement
showing log diagnosis is a bottleneck; the Netflix experience is not evidence for MCTL.

### Smallest separately approvable follow-up

"Offline Tier A span-extraction fixtures and evaluator cases", one PR, no runtime path, no model,
no network, no production log. Provisional allowlist of paths it may touch (to be confirmed by
the owner): a new `tools/` or `tests/` module for the extractor and the `diag_key` pure function;
new synthetic fixtures under a new `tests/fixtures/` subdirectory; one new test file; one short
doc. Explicitly outside: `orchestrator/` runtime modules, any schema, `execution_evidence.py`,
`tracing*.py`, workflow code, config, instructions, CI, gitops.

### Negative and mutation test plan for that follow-up

- Every F7, F8, F9, F10, F11, F15, F16 case above must assert refusal or abstention.
- Mutations that must each fail at least one test: drop the hash check; allow inverted spans;
  allow a span to cross the end of input; skip the credential check on the derived excerpt;
  return `0` instead of `null` for an empty set; drop `engine_ref_full`'s run suffix from the key;
  default `owner_epoch` to 0; truncate the key; make the key depend on wall-clock; let the
  extractor read a live source; let any output reach a retry or policy decision.
- Mutations are applied temporarily, committed first and restored after, as the workspace rules
  require, and the run results are recorded.
- A determinism test runs the extractor twice and compares bytes.

## 11. Unknowns and questions

Not blocking this inventory; listed so none is silently assumed.

1. Contents of #196, #310, #698, #699, #700, #912, #1032 and .github#342 (UNVERIFIED). Needed
   before approval of the follow-up, to confirm no duplicate.
2. Whether a `diag_key` should ever be persisted and by whom (mctl-api owns durable evidence per
   ADR 018). The pilot stores nothing.
3. Whether log bytes may be sent to any model provider at all (data classification). Needed only
   before any Tier B step.
4. Current status of the safety-classifier-interruption detection (#849) (UNVERIFIED).
5. The mctl-api definition behind `execution_attempt_v1`, in particular whether stripping the
   `#run_` suffix is intentional (UNVERIFIED).
6. The Netflix article's internal numbers and its measured results were not reproduced here and
   are not MCTL facts.

The one question that is indispensable: **does any of the unread listed issues already own
log-span extraction?** If yes, this spike should be closed as a duplicate.

## 12. Checks performed

- Local reads of the paths cited above. No network beyond `git fetch origin`, no installs, no
  tests run (Markdown only).
- `git diff --check` and a sole-file check are reported in the handoff.
