# Spike #989: would tiered memory, expiry and token budgeting reduce stale-context failures and token cost in DevLoop?

STATUS: DRAFT (research only)

Refs mctlhq/mctl-agents#989. Docs only. No code, test, setting, permission, dependency, model call, production read
or network experiment was made. Every recommendation below is **proposed**; the owner records the decision before
anything is implemented.

Proposed verdict (for the owner to accept, change or reject): **REUSE the shipped primitives now; DEFER any new
policy; do not build a tiered-memory subsystem.** One narrow deterministic extension (section 7, option B) is
described so the owner can choose it, and a synthetic test (section 8) is designed that could falsify it. Nothing in
this document has been measured.

## 0. Evidence labels

| label | meaning |
|---|---|
| `[S]` | source-read: the text of the cited lines was read at the base commit. It was **not executed**. Applies only to the files/lines that section 1 lists as read; `[G]` and `[A]` below are weaker. |
| `[G]` | grep or outline observation only: a pattern search or symbol list, surrounding code not read. Existence/absence within the searched paths only. |
| `[A]` | ADR-only: taken from ADR text, not re-derived from code. |
| `[P]` | proposal: a suggestion, not shipped, not decided. |
| `[I]` | inferred from `[S]` facts; reasoning shown, not observed. |
| `[U]` | unknown. Also written `NOT_OBSERVED` when a source was out of reach. |
| `NOT_RUN` | a measurement that is designed but was not performed. No number is given for it. |

Base: `origin/main` `4d7efd3` (Merge PR #1079). Branch `docs/context-memory-budget-989`.

## 1. Sources and limits

| source | status |
|---|---|
| The InfoQ article named in the issue | `NOT_OBSERVED`. Not fetched (no network). Nothing here asserts what the article says. The issue's three terms, "tiered memory", "expiry", "token budgeting", are used as the question's vocabulary only. |
| `docs/adr/009-context-snapshot-contract.md` (incl. amendments 1 and 2) | read `[S]` |
| `docs/adr/010-lifecycle-ownership-contract.md` | read `[S]` |
| `docs/adr/011-execution-identity-contract.md`, `011-execution-budget-contract.md`, `011-work-item-resume-contract.md` (first 200 lines) | read `[S]` |
| `docs/adr/012-model-usage-cost-attribution-contract.md` (first 170 lines) | read `[S]` partly |
| `docs/adr/015-context-evaluation-contract.md`, `019-context-strategy-release-contract.md` | read `[S]` |
| `orchestrator/context_assembly.py` | read `[S]`: constants, `apply_budget`, `run_pipeline`, collectors, `assemble`, `assemble_investigator_context`, `_to_context_source` |
| `orchestrator/context_snapshot.py` | read `[S]` for `_content_payload` only |
| `orchestrator/run_issue_investigator.py` | read `[S]` for `_context_mode`, `_assemble_context`, `_render_assembled_context_section`, the issue block of `_build_prompt` |
| `orchestrator/options.py:532-543`, `orchestrator/usage_ledger.py` | outline and grep only `[G]` |
| `orchestrator/context_eval.py` | grep/outline in the first pass `[G]`; lines 385-429 (used_bytes, `context_tokens_estimate`, `coverage_by_kind`, `EvalMetrics`) and `context_assembly.py` `apply_budget` (`:481-525`) read in the correction pass `[S]` |
| `context_release.py`, `context_rollout.py`, `exec_budget.py`, `work_context/*` | grep or outline only `[G]`. Behaviour is taken from the ADRs `[A]`, not re-derived. |
| `orchestrator/redaction.py` | symbol list only `[G]` (`contains_credential`, `value_allowed`, `safe_scalar`, ...). Whether it covers snapshot/completeness redaction is `[U]`. |
| `tests/fixtures/context_eval/cases/02-…`, `04-…` | read `[S]`. The other five cases, `baseline.json` and every test file were **not** read. No test was executed; nothing here says what any test does or does not guarantee. |
| `.github` workflow code, mctl-gitops, mctl-api, production logs, Temporal histories | `NOT_OBSERVED` (cross-repo and no production reads) |
| Issues #597, #747, #885, #848, #1014, #1059, #1073, `.github#309` | `NOT_OBSERVED` as text. Statuses below are the orchestrator's snapshot, quoted as data, not verified. |

Preflight on this checkout: no file under `docs/spikes/` mentions 989, and no local or remote-tracking branch name
matches `989` (other than the one created for this task). That is negative evidence for a parallel owner on this
clone only. It is not exhaustive.

Related-issue boundaries (orchestrator snapshot, unverified here):

- #747 is PARKED / DO NOT START (parked Git coordination / immutable execution state per the orchestrator; it is not
  confirmed here to own persistent agent memory). This document does not reopen, activate or depend on it, and does
  not unpark it. Whether any later memory proposal overlaps #747 is `[U]`; if it does, the owner decides.
- `.github#309` is ACTIVE RESEARCH RUNNING. Not duplicated.
- #848 needs actual owner timing. `[U]`.
- #1059 (closed, typed read contract) is related and grants no permission to change context handling.
- #1014 (report/evaluator), #1073 (synthetic log-span selector pilot) and #885 (affinity) are not duplicated.
  Section 8 states how this document's test differs from each.

## 2. Three distinctions the question blurs

The issue says "tiered memory, expiry and token budgeting" as one idea. In this repo they are three layers plus two
pairs that are easy to confuse.

| layer or pair | what it is here | owner | `[S]` anchor |
|---|---|---|---|
| **Memory storage** | Durable records that outlive one run. The WorkItem and its execution ledger, and the sealed `ContextSnapshot` document, live in mctl-api Postgres. Gitops holds proposals and bindings. | mctl-api, gitops | ADR 011-resume sec. 1 and 6; ADR 009 sec. 7 (`retention` classes `telemetry`, `execution-record`, `gitops`) |
| **Candidate selection** | A deterministic pipeline that decides which collected sources are *included*: freshness classification, dedupe, per-source truncation, count/byte budget, optional trust-first ranking. | `context_assembly.run_pipeline` | `context_assembly.py:1297` |
| **Execution snapshot** | The sealed record of what was selected: ids, hashes, selectors, byte counts, no payload. | `context_snapshot.seal` | ADR 009 sec. 1-2 |
| **Expiry** vs **authorization** | Expiry is a data-age or lease concept: `Freshness.max_age_seconds`, `RetentionPolicy.expires_after_days`, claim `lease_until`, liveness bounds. Authorization is policy/approval/provider enforcement. Neither implies the other. | freshness: assembler; retention: store; authz: ADR 014/#197 | ADR 009 sec. 5 ("context relevance is never an authorization mechanism"); ADR 010 ("ownership never implies authorization") |
| **Per-call/assembly limit** vs **total execution budget** | The assembly budget is `max_sources`/`max_bytes`/`max_bytes_per_source`: bytes, no tokens, no model. The execution budgets are wall-clock envelopes (ADR 011-budget), a USD cap per agent (`options.py`), and per-(run, model) token usage in the usage ledger (ADR 012). | assembler; implementer envelope; SDK `max_budget_usd`; usage ledger | ADR 009 sec. 6; `context_assembly.py:225-227`; `options.py:543` |

Consequence for the question: "expiry" in the article's sense maps onto the freshness vocabulary and retention
classes the repo already has. "Token budgeting" is a different layer from the assembly budget, and today those
layers are not connected (gap G5).

## 3. Inventory of shipped primitives (labelled per item; nothing executed)

1. **Snapshot contract.** `ContextSnapshot` with per-source `freshness{observed_at, staleness, max_age_seconds}`,
   `trust{tier}`, `selection{rank, score, reason_code, included}`, `redaction`, and a `ContextBudget` in
   sources/bytes only. `unknown` is the fail-safe freshness default (ADR 009 sec. 6).
2. **Assembly.** `assemble_investigator_context` is the feature-gated entry. In a grep over `orchestrator/` and
   `tools/` `[G]` the only caller found is `run_issue_investigator.py`, and `context_snapshot.seal` is called only at
   `context_assembly.py:1666` and `:1720` (same grep; other `seal()` hits belong to other modules' own `seal`
   functions). That bounds the claim to the inspected paths; it does not prove no other producer exists elsewhere.
   `ISSUE_INVESTIGATOR_CONTEXT_MODE` defaults to `off` (`run_issue_investigator.py:163`). `shadow` seals and logs
   but leaves the prompt untouched; `on` appends rendered sources.
3. **Two strategies.** `deterministic-fixed-order` 1.0.0 (default: rank = collector order, drops `stale`) and
   `trust-freshness-ranked` 1.0.0 (opt-in: pinned kinds first, then trust, then freshness, then recency; demotes
   stale instead of dropping; records conflicts).
4. **Budget knobs.** `ISSUE_INVESTIGATOR_CONTEXT_MAX_SOURCES` (12), `_MAX_BYTES` (120000), `_MAX_BYTES_PER_SOURCE`
   (50000); fixed `max_comments` 20 and `max_candidates` 100 (`context_assembly.py:225-229`).
5. **Release.** ADR 019: immutable content-pinned strategy versions, append-only per-(agent, environment)
   bindings, `observe` shadow pass that seals a non-persisted candidate, production gate requiring >=3 consecutive
   observe-candidate observations within 7 days and no `hash-mismatch`. A human-reviewed commit is still required.
6. **Evaluation.** ADR 015 / `context_eval.py`: identity verification first, then `selected_precision`,
   `useful_recall`, `missing_expected`, `stale_rate`, `duplicate_rate`, `noise_rate`, `context_bytes`,
   `context_tokens_estimate = ceil(used_bytes / 4)` (an estimate, "never a billed token count"), latency; seven
   curated fixture cases under `tests/fixtures/context_eval/cases/`; read-only replay CLI `run_context_eval.py`.
7. **Persistence and resume.** Sealed snapshots persist to mctl-api when `WORK_CONTEXT_ROLLOUT_MODE >= observe` and
   a store execution (`we_...`) exists (ADR 011-resume sec. 6). Resume links a prior snapshot and WorkItem intents
   are budgeted sources, the resume intent pinned (`apply_budget`, `context_assembly.py:489-505`).
8. **Execution identity.** `ExecutionContext` (`ex-...`) is control-plane minted and trust-tiered, but ADR
   011-identity states that as of 2026-10-21 the mctl-api endpoint and CWFT parameters had not landed; the
   investigator therefore binds to the store execution `we_...`. Current status `[U]` (cross-repo).
9. **Cost.** `usage_ledger.py` records per-(run, model) token counts with `execution_id` correlation (grep:
   `execution_id`, `input_tokens`, `cache_read_tokens`). `calculated_cost` is a list-price estimate, not billing
   (ADR 012). Whether production runs emit it, and with what coverage, is `[U]`.
10. **Execution budgets.** The implementer has a wall-clock envelope with per-command bounds (ADR 011-budget). The
    investigator has a USD cap `ISSUE_INVESTIGATOR_BUDGET_USD` = 8.00 (`options.py:543`). How that value reaches
    the investigator's SDK options was not read `[U]`.

## 4. How the article's three terms map onto the repo

| term from the issue | already in repo? | where | note |
|---|---|---|---|
| tiered memory | partly, as *retention classes* and *trust tiers*, not as hot/warm/cold storage | ADR 009 `RetentionPolicy.class`, `Trust.tier`; WorkItem + snapshot store | ADR 009 "no payload, ever, anywhere" limits the *snapshot schema, its store and telemetry*; it is not a ban on every platform payload store. ADR 009 also lists a generic search/context service as a non-goal `[A]`. A memory tier that put content into the snapshot or its telemetry would reopen that; a separate store would need its own owner and contract `[U]`. |
| expiry | yes, for selection | `Freshness.max_age_seconds`, `classify_freshness` (fresh <= max/2, aging <= max, stale beyond), `stale` drop / `stale-demoted`; snapshot `expires_after_days` = 180 | ADR 009 Amendment 1 defines GitHub freshness as time since retrieval (see G1): an intended contract, not a defect |
| token budgeting | no (deliberately) | ADR 009 sec. 6 defers `max_tokens`: tokenizer dependency absent and model policy can change under a sealed snapshot | an optional `max_tokens`/`used_tokens` pair is allowed later without an `apiVersion` bump |

## 5. Source-backed gaps and counterexamples

Each gap is `[S]` at the cited line unless marked. None was run. "Reachable" claims are inferences `[I]`.

**G1. Freshness is defined as retrieval age (intended contract); content age is a separate, proposed axis.**
ADR 009 Amendment 1 states "GitHub-fetched sources keep time since retrieval" `[A]`. The collectors follow that:
`collect_github_issue`, `collect_issue_comments` and `collect_target_repo` set `observed_at=_iso(assembly_input.now)`
(`context_assembly.py:757`, `:792`, `:816`), and `classify_freshness` is called with the same `now`
(`run_pipeline`, `:1347`). For freshly collected input the retrieval age is 0, so these classify `fresh`. This is the
documented guard working as designed, not a broken freshness check. `collect_prior_proposal` uses retrieval time
under the default strategy (`:893`); the ranked strategy uses the proposal's `.status.yaml` `updated_at`
(`:896-902`), as Amendment 1 also states.
`[I]` Consequence: retrieval-time freshness cannot say that a *content* is old. Whether content age should matter
is a **proposed research question**, not a proven defect. The comment's own age (`content_time`) is used only for
recency ordering (`_recency`, `:542`). Fixture `02-stale-source.json` hand-sets `observed_at: 2026-01-01T00:00:00Z`
for a comment; that validly exercises the generic pipeline's `stale` path, but it does not model freshly collected
input (a real collector would emit `observed_at = now`). It is not evidence that the baseline is invalid. The
fixture also labels that stale comment **useful**: an old actionable comment must not be discarded merely for age, so
any content-age idea would have to flag or demote, never drop on age alone `[P]`.

**G2. Default-strategy budget order puts untrusted comments ahead of the prior proposal.**
Default rank is collector order (`assign_ranks`, `:1343`). `_COLLECTOR_ORDER` is template, issue, comments,
target-repo, prior-proposal (`:1136-1142`). `apply_budget` walks ascending rank and, from the first source that does
not fit, excludes it and everything after (`:481-525`, "stopping at the first miss"). With `max_sources=12` and up
to 20 retained comments, the three prior-proposal documents (`corroborated`, `proposal-dir`) are the first cut, while
`untrusted` comments are kept. Counterexample, constructible from source: 1 template + 1 issue + 1 target-repo
+ 9 comments fill 12 sources; prior proposal gets `budget-exhausted` although a re-investigation exists to read it.
`[I]` The ranked strategy (shipped, opt-in) orders by trust first and so does not have this shape. Whether the
production environment runs it is `[U]`. This is evidence for *using the existing strategy*, not for a new one.

**G3. First-miss stop can discard small, important sources behind one large one.** Same function. Once any source
fails to fit, every later included source is excluded too, even a 2 KB one that would still fit (`budget_hit`,
`:506-515`). Counterexample, constructible from source: with 100 KB already used, a 30 KB comment misses
`max_bytes=120000`, and a later 2 KB prior-proposal document is excluded with it. `[I]` Simplicity was a stated
choice ("keeps the rule one sentence long"); the cost is order sensitivity. Frequency `[U]`.

**G4. Truncation is a byte prefix; the model-visible text and the hashed bytes differ.**
`truncate_to_per_source_limit` keeps `raw[:N]` (`:467-478`). For comments, `raw` is the canonical-JSON envelope
while `render_text` is a different string ("Comment by ... :\n body"), cut separately to N bytes. So
`content_hash`/`byte_count` describe the JSON prefix, not the rendered bytes the model reads. The `CandidateSource`
docstring says hashing and rendering "can differ". ADR 009 sec. 7 says `content_hash` "describes the bytes the model
actually saw". These two statements do not agree for comments and proposal docs. For the **issue body** the
mismatch is larger: `_build_prompt` substitutes the full `issue.body` (`run_issue_investigator.py:2209-2211`) and
`github-issue` has no `render_text`, but the snapshot's issue source is truncated at 50000 bytes. `[I]` A body over
50000 bytes (GitHub's body limit is higher than that, from general knowledge, not from this repo) is sealed as
truncated or even `budget-exhausted` while the model saw all of it. A tail-loss risk also exists for sources that
*are* cut: the part lost is always the end, e.g. a long comment whose actionable ask is last. There is no marker
saying a source is *required*, other than the `pinned` flag used only for the resume intent.

**G5. The assembly budget covers a small share of what costs tokens, and is not connected to cost.**
In `on` mode only sources with `render_text` reach the prompt: comments, prior-proposal documents, WorkItem
intents (`_render_assembled_context_section`, `run_issue_investigator.py:1493-1532`). The issue body is outside the
budget (G4), the target repo is explored by the agent with Glob/Grep/Read (ADR 009 sec. 8, "agent-directed"),
tool results, human answers and capability blocks are separate prompt parts. What the metrics measure
(`context_eval.py:394-399`, `apply_budget` `:481-525`, both `[S]`): `context_bytes` is `assembly.used_bytes` (or
`snapshot.budget.used_bytes`), the sum of `byte_count` of the *selected raw candidates*. For comments and proposal
documents `byte_count` is the canonical-JSON envelope length (G4), and the selected set includes the issue and
template envelopes. So it is neither exactly the rendered appended section nor the complete prompt, and it
excludes tool results, human answers, capability blocks and agent-directed repo reads. `context_tokens_estimate` is
`ceil(used_bytes / 4)` of that same quantity, an estimate and never billed tokens `[S]`. The usage ledger carries
tokens and `execution_id`, but in the grep `[G]` its field list showed no snapshot id. A join through the store
execution id looks possible `[I]`; no code performing it was found in the paths inspected, and unassessed joins
elsewhere (mctl-api, gitops, other modules) are `[U]`. Consequence: in the inspected paths nothing attributes token
cost to context selection choices, so a "token saving" claim has no instrument there.

**G6. Everything above is off by default and unobserved.** `ISSUE_INVESTIGATOR_CONTEXT_MODE` defaults to `off`,
context eval and release rollout default to `off`. Which environments set them is in mctl-gitops: `[U]`. So there
is no production-shaped distribution of stale or truncated sources to size any of G1-G4 against `[U]` (none was searched for beyond this repo).

**G7. Snapshot identity distinguishes timestamps; three cases must not be conflated.** ADR 009 sec. 2 says only
`created_at` is excluded from the hash `[A]`. `_to_context_source` puts `retrieved_at` and `freshness.observed_at`
into each source (`:387-392`) and `_content_payload` hashes `sources` (`context_snapshot.py:1231-1263`) `[S]`. With
live collectors both equal `now`. Therefore:
(a) **identical full inputs, including identical observation timestamps**: same identity (the ADR's intent);
(b) **identical text with changed observation timestamps**: different `snapshot_id`, by construction;
(c) **changed timestamps landing at a later second** (a retry): ADR 011-resume sec. 6 already says a retry at a later
second restamps them, and a field-by-field "retry-equivalent" compare handles it `[A]`. This is not the same
statement as "never the same id": identical *full* inputs do share one.
Reuse of one identity for different content is refused (store answers 409, divergence blocks from `enforce`) `[A]`.
A content-only digest (hash over source `content_hash` + ids, excluding timestamps) would be a different quantity.
It is **not** freshness, authorization, cache validity or proof of context equivalence: locators, selection, trust,
strategy and budget can differ while content hashes match. Any content-only grouping is a **non-authoritative
proposal, pending the owner's decision**. Ambiguity in ADR wording is not permission to change a contract.

**G8. Pins can drift from the thing later acted on.** `target-repo` is `fresh` by being content-addressed at the
clone's SHA (`_CONTENT_ADDRESSED_KINDS`, `:122`). In the assembler code read `[S]`, nothing re-checks that SHA against the branch head at a later phase. A grep of
`orchestrator/` and `tools/` `[G]` found `context_snapshot.seal` called only inside `context_assembly.py`
(`:1666`, `:1720`) and `assemble_investigator_context` called only from `run_issue_investigator.py`, so in those
inspected entrypoints the implement and review-fix phases do not build a context snapshot. Other producers outside
the inspected paths, or other drift evidence for implement-phase work (approval binding to a proposal digest is a
separate mechanism; spike 670 describes `freshness(artifact, current_digest, current_sha)`, not read here),
are `[U]`. This does not show implement-phase drift is unmeasurable in general.

Not found as gaps in the lines read `[S]`: missing provenance for selected sources (each carries locator, selector,
hash, trust, selection reason); silent drops (each exclusion has a `reason_code` and a counter); authority creep
(no allow/deny field in the shape). ADR 009 sec. 5 and ADR 019 describe import-direction tests `[A]`; no test was
read or run here, so nothing is asserted about them and the owner should not read this paragraph as a guarantee.

## 6. Duplicate map (who owns what)

| concern | existing owner | this spike's relation |
|---|---|---|
| which sources are selected, in what order, under what limits | `context_assembly.run_pipeline` (#265, #471) | reuse |
| strategy versioning, promotion, rollback, evidence gate | ADR 019, `context_release.py` (#472, #527, #528) | any new policy ships as a new strategy version through this, never an edit |
| context quality metrics and replay | ADR 015, `context_eval.py`, `run_context_eval.py` (#266, #526) | reuse the metrics; do not add a second evaluator |
| cost attribution | ADR 012, `usage_ledger.py` | reuse; the join to a snapshot is the open piece (G5) |
| per-run wall-clock/command budget | ADR 011-budget, `exec_budget.py` | out of scope, different layer |
| identity and claims | ADR 011-identity, ADR 010 | out of scope; only consumed |
| report/evaluator for DevLoop outcomes | #1014 | not duplicated |
| synthetic log-span selector pilot | #1073 | not duplicated; same *style* of offline fixture only |
| affinity | #885 | not duplicated |
| persistent agent memory | #747 (PARKED) | not touched |
| earlier related: #597 | `[U]` | not read |

## 7. Options

| option | what it is | fits shipped contracts? | evidence it is needed | cost and risk |
|---|---|---|---|---|
| **A. Reuse current primitives** | Use `trust-freshness-ranked`, the existing size knobs, `observe` + ADR 015 evaluation, and the usage ledger as they are. Add no code. | yes, entirely | G2 is already answered by the ranked strategy `[S]` | needs someone to turn `shadow` and `observe` on and read the logs: a gitops decision, outside this repo and outside this task |
| **B. Narrow deterministic policy extension** | A *new strategy version* (ADR 019) that (1) *researches* a content-age signal from `content_time` where present, flagging or demoting, never dropping on age alone; (2) lets a source be marked *required* so budget cuts and truncation cannot silently remove it, with a recorded reason; (3) optionally, a **non-authoritative** content-only grouping of sources (G7), only if the owner wants it. Item (3) is not freshness, authorization or cache validity. | **not established.** Optional fields are not shown compatible with the closed schemas and their consumers; ADR 009 sec. 6 growth rule is cited as precedent `[A]` only. Any B needs revised consumer, schema and owner contracts, decided by the owner, before it is implementable. | G2, G3, G4 are source-visible. G1 and G7 are intended contract versus a research question, not defects. Frequency is `[U]`. | new code under review, new fixture baseline, ADR amendment(s), an implementation-hash republish. Benefit is unmeasured. |
| **C. Tiered-memory subsystem** | A store of summarised or cached context with hot/warm/cold tiers and expiry. | not as stated: storing content in the snapshot or its telemetry is rejected by ADR 009 sec. 7 / Alternative 2 `[A]`, and a generic retrieval service is a non-goal. A separate store would need its own owner and contract; overlap with #747 is `[U]` and #747 stays parked | none found | highest cost, largest authority and privacy surface. `orchestrator/redaction.py` exists `[G]`; whether it is span-safe or complete enough for snapshots or stored summaries is unassessed `[U]` |
| **D. Defer** | Record G1-G8, take no action until A produces production-shaped numbers. | yes | G6: no production distribution exists | the question stays unanswered; zero implementation risk |

Proposed (not decided): **A now, D for B and C.** Rationale: the cheapest honest step is to obtain real counters from
primitives that already exist. B is plausible, but its value rests on frequencies this research could not observe.
C contradicts two accepted ADR decisions and has no supporting evidence. If the owner prefers to learn more before
asking gitops to enable anything, the synthetic test below is the smallest offline step, and it only tests whether
the *mechanism* exists (G2-G4, plus the G1 content-age question), not how often it bites.

An A-only outcome does not satisfy the issue's "reduce token cost" half. No evidence in the repo says assembly
choices move token cost materially (G5), and none is claimed.

## 8. Smallest synthetic future test (NOT_RUN, not implemented here)

Purpose: decide whether option B's three checks fix a failure that option A (the shipped ranked strategy) does not.
Falsifiable, offline, no model, no network, no production read.

Design `[P]`:

- **Harness.** Reuse `run_pipeline` and the ADR 015 metric functions with a fixed `now`, exactly as the existing
  fixture contract does. A new directory of frozen JSON cases, separate from `tests/fixtures/context_eval/`, so the
  existing baseline is untouched. Candidate lists use **live-shaped** timestamps: `observed_at = now` and
  `content_time` = the real age. This is the property the existing fixtures lack (G1).
- **Cases** (one failure mechanism each, five in total): C1 an old actionable comment under a one-hour window
  (content-age research question, G1; expected: kept, possibly flagged, never dropped on age); C2 template + issue + target-repo + 9 comments + prior proposal at `max_sources=12` (G2); C3 a large
  middle source ahead of small required ones (G3); C4 a required fact located at the tail of a source over the
  per-source limit (G4); C5 the same content assembled at two `now` values, recording which of G7's cases (a)/(b)/(c) apply; no equivalence
  is asserted.
- **Frozen oracle.** Per case, a committed list of `required_source_ids`, `expected_excluded` with expected
  `reason_code`s, and for C5 whether text-identical sources differ only in timestamps (descriptive, non-authoritative). Labels are written by hand before any
  candidate output is seen, and stored with their rationale.
- **Baseline.** `deterministic-fixed-order` 1.0.0 and `trust-freshness-ranked` 1.0.0 as shipped, outputs committed as
  a frozen file. Candidate: the option B strategy, only when it exists; until then the test is a design.
- **Measured per case and arm:** `missing_expected`, `useful_recall`, `stale_rate` (computed from content age),
  count of required sources excluded or truncated, `context_bytes`, `capability_calls`. All values `NOT_RUN`.
- **Pre-registered decision rule.** B is worth proposing for implementation only if, on at least one case, the
  ranked baseline loses a required source or mislabels a stale one **and** B does not, with no case worse and no
  added `capability_calls`. If the ranked baseline already passes every case, the verdict is A (REUSE), and
  B stays deferred. If B ties everywhere, B is dropped.
- **What it cannot show.** Frequency in production, latency, model quality, and any token or dollar saving. Token
  cost needs ledger data joined to snapshots (G5) and stays `NOT_RUN`. No saving is claimed anywhere in this
  document.
- **Not duplicating:** it scores selection of text sources by context-eval metrics, not log spans (#1073), not
  finding or outcome reports (#1014), and not repo/agent affinity (#885).

## 9. Measurements table (all NOT_RUN)

| measurement | baseline | candidate | status |
|---|---|---|---|
| success on current MCTL workload | `[U]` | `[U]` | `NOT_RUN`, needs production-shaped runs, forbidden here |
| cost (tokens, USD) | `[U]` | `[U]` | `NOT_RUN`, no snapshot-to-ledger join found (G5). `unknown` is not zero. |
| latency | `[U]` | `[U]` | `NOT_RUN`. ADR 019 sizes the `observe` pass as one extra in-memory pass `[S]`; no timing measured. |
| reliability | `[U]` | `[U]` | `NOT_RUN` |
| stale-context failure rate | `[U]` | `[U]` | `NOT_RUN`. Not measured by anything inspected. ADR 015's `stale_rate` is the share of *selected sources classified `stale`* (selection staleness by retrieval-time freshness); it is not a stale-context failure rate, and no zero or non-zero value is claimed for either. |

## 10. Invariants this document keeps

- No rollout, no authority expansion, no new contract adopted. Identity, policy and approval boundaries stay as in
  ADR 009 sec. 5, ADR 010 and ADR 019's safety invariant.
- Any future change goes through ADR 019 as a new strategy version, and through an ADR 009 amendment for fields.
- A freshness or ranking result is ordering and measurement only. It must not feed a policy, eligibility or
  approval decision.

## 11. Follow-ups for the owner (all proposed, none opened)

1. Decide A / B / C / D (section 7). Proposed: A now, D for B and C.
2. If A: ask the gitops owner whether `ISSUE_INVESTIGATOR_CONTEXT_MODE`, `ISSUE_INVESTIGATOR_CONTEXT_EVAL` and
   `CONTEXT_RELEASE_ROLLOUT_MODE` are set anywhere, and read their existing log lines. `[U]` from this repo.
3. Owner to decide whether ADR 009 sec. 2 and sec. 7 wording needs clarification against G4 (hash covers the raw
   envelope) and G7 (identity includes source timestamps). Wording ambiguity alone does not authorise any contract
   change; this is a question, not a fix.
4. If B: write the synthetic cases of section 8 first, then decide. Open one issue per proven gap, not one epic.
5. Whether a snapshot-to-usage join exists or is wanted (G5) is a question for the ADR 012 owner.

Decision record (owner fills in): `decision.verdict:` _pending_.
