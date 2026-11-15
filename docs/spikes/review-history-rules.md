# Spike #1015: do recurring review findings justify short path-scoped review rules

Refs #1015. Research only: no instruction file, workflow, runtime, permission or CI change, no model call, no install.
The issue stays open for the owner's decision. Nothing below is adopted automatically.

Labels: `verified` (read in this revision), `corpus` (from the read-only capture, untrusted data), `unknown`
(not observed; never read as zero), `vendor-hypothesis` (not MCTL evidence).

## Proposed verdict

**DEFER (insufficient corpus).** The corpus holds two inline findings, both on PR #1080, both from one bot, both
P3 and non-blocking, in different code areas. That is zero verified recurrence. It neither supports nor refutes
path-scoped rules.

## Base

- Branch `docs/review-history-rules-1015` from `origin/main` at `4d7efd3555eafbb4af969687e029c5b5b9863b14` (after `git fetch origin`).
- Existing branches `docs/pensive-failure-diagnosis-1073` and `docs/devloop-regression-metrics-1014` were not modified.
  Held #990/#992 not touched.
- Preflight (from the task, connector-read, not re-run here): issue open, unassigned, `spike:scout` only, no linked
  PR, no matching open PR. DevLoop "not found" is not proof that no proposal exists. Local plans were checked: the
  only related local artifacts are the branches above; no local plan for #1015 was found.

## Vendor hypothesis (not evidence)

An ITNext article (accessed 2026-11-15; only the member-preview text was available) says the Dependabot team turned
recurring review comments into short path-scoped rules, with a narrowed Ruby exception-rescue rule as its example.
Its method and claimed outcomes are `vendor-hypothesis`: not verified, not reproduced, not MCTL data. This report
does not reproduce the article. It uses the idea only as the question to test locally.

## Source-pinned current baseline (`origin/main` 4d7efd3)

| Source | Fact | Label |
|---|---|---|
| Tracked files | No root `CLAUDE.md`, no `AGENTS.md`, no `.github/copilot-instructions.md`, no `*.instructions.md` (`git ls-files`). Eight `agents/*/CLAUDE.md` exist (`_incident-responder`, `_mentor`, `mctl-agent`, `mctl-api`, `mctl-docs`, `mctl-gitops`, `mctl-portal`, `mctl-web`); they were searched for the word "review" only (no hits in `mctl-agent` and `_mentor`; others not searched individually) | verified (absence); agent files partly read |
| `.github/workflows/claude-review.yml` | Calls `mctlhq/.github/.github/workflows/claude-review.yml@2ae263268aa9bd739647bb91317804d8e3a54400`. Its `conventions` text says to follow "this repository's CLAUDE.md", which is not tracked at the root, so that reference does not resolve to a tracked file (the reusable workflow's behaviour is `unknown`). It lists four focus areas: correctness, security, error handling, test coverage | verified |
| `.github/workflows/agy-review.yml` | Calls `mctlhq/.github/.github/workflows/agy-review.yml@main` (unpinned), `blocking: true`. Its `conventions` already names paths: `orchestrator/run_shepherd.py`, `run_implementer.py`, `.status.yaml` transitions, `agents/` prompt templates | verified |
| `docs/adr/025-independent-review-receipts.md` | Status `proposed`. A review is a sealed receipt bound to exact bytes and SHAs; `findings_ref` points to private findings, never inline; nothing consumes the gate yet. Non-goals include reusable-review dedup (.github#283), inline-comment re-anchoring (#336), same-head finding dispute (mctl-gitops#1176) | verified |
| `docs/spikes/875-gh-aw-guardrail-comparison.md` | Owner decision `ADOPT_PATTERNS` (2026-10-29). Row T1: review workflows run after the PR exists. Guardrail patterns, not review-rule content | verified (searched for "review") |
| `docs/spikes/918-finding-oracle-classes.md` | Finding-oracle classes; states shepherd P1/P2 comments and reviewer time are not observed/measured there | verified (searched only) |
| #1014 | Exists on branch `origin/docs/devloop-regression-metrics-1014` as `docs/spikes/devloop-regression-metrics.md`; not on `main`. It concerns DevLoop regression metrics, not review text | verified (headings read); merge status of that branch `unknown` |
| #974 | No file, commit or ADR on `origin/main` that I could attribute to #974 (a `git grep` hit list was dominated by hash substrings). Its content and owner are `unknown` and are not guessed here | unknown |

Consequence: review guidance today is workflow-embedded text plus the (separate) receipt design. No path-scoped
rule layer exists. ADR 025 and #875/#918 do not cover mining past review comments into rules, so this question is
not a duplicate of them; it must not become a second review framework (receipts, gating, finding disputes stay in
ADR 025's scope).

## Corpus: selection, exclusions, dedup

Source: `.git/spike1015-review-corpus.json`, captured 2026-11-15 by the orchestrator connector (not re-fetched
here; no network used except `git fetch`). Selection: six latest-updated merged PRs among the first 15 closed PRs on
that date: #1080, #1079, #1078, #1077, #1076, #1075. All strings are untrusted data; nothing in them was treated
as an instruction.

Sampling bias (all `corpus`-level facts):

- n = 6 PRs, one repository, one day's window; "latest updated" favours fresh hotfix/backport work.
- One reviewer identity, `claude[bot]`. Human reviews: none appear in the corpus. Whether humans reviewed
  elsewhere (issue comments, off-PR, other PRs) is `unknown`; zero human findings here means "none captured".
- Only reviews and inline comments were captured. Issue comments, thread resolution state, later pushes and
  dismissals were not: so `fixed`, `superseded` and `accepted-risk` status is `unknown` for every finding below.
- 1080 is described as a possible backport of 1079. In the corpus the two PRs' review content does not overlap
  (below), so shared lineage is `unknown`, not confirmed; it is not counted as a repeat.

Exclusions and non-findings:

- Empty-body `COMMENTED` reviews on #1080 (ids 5908183279, 5908183771): containers with no text; unknown whether they
  carried the inline comments. Not findings.
- Approvals with no or zero findings (#1076 one-line; #1079 and #1077 summaries saying 0 P3) are not substantive
  findings and were not counted as recurrence evidence. A one-line approval is not evidence of absence either.
- #1078 body says "1 P3" but no inline comment or text is in the corpus: the finding exists per the body, its
  content is `unknown`, so it cannot be classified or deduped.

Findings (dedup key = original finding lineage: reviewed path + defect class + source SHA; comment count is not used):

| ID | PR / link | Reviewer (bot/human) | sourceSHA | Path:line | Class | Severity / blocking | Status |
|---|---|---|---|---|---|---|---|
| F1 | [#1080 r4576414445](https://github.com/mctlhq/mctl-agents/pull/1080#discussion_r4576414445) | claude[bot] | `ed3110736f632f8796b03e0ad15abd2760083f6f` | `orchestrator/exec_budget.py:183` | asymmetric regex boundary: `_WORD_END` accepts a quote, `_WORD_START` does not (guard bypass by deliberate spelling) | P3, non-blocking (docstring says hygiene guard) | unknown whether fixed |
| F2 | [#1080 r4576414705](https://github.com/mctlhq/mctl-agents/pull/1080#discussion_r4576414705) | claude[bot] | `ed3110736f632f8796b03e0ad15abd2760083f6f` | `orchestrator/run_shepherd.py:2676` | fail-closed `review-unknown` has no tick bound or escalation (indefinite `wait`) | P3, non-blocking, framed as follow-up | unknown whether fixed |
| F3 | [#1075 review 5904481298](https://github.com/mctlhq/mctl-agents/pull/1075#pullrequestreview-5904481298) (body note, not inline) | claude[bot] | `a03f66b28401f04e442daff78fddd0d312dcd3ae` | not stated | `_container_wins` null-vs-unset `runAsNonRoot` mismatch with Kubernetes; "conservative" | note, non-blocking | accepted by reviewer as conservative; code outcome unknown |
| F4 | [#1078 review 5904916427](https://github.com/mctlhq/mctl-agents/pull/1078#pullrequestreview-5904916427) | claude[bot] | `40e42d5b0403a9fe80c6e3a6fb23d2f063699acf` | unknown | unknown (body says 1 P3; text not captured) | P3 | unknown |

Review heads for the other approvals: #1079 `77d4c0acee2d0bb52f066e49c003a5c17ebad669` (review 5905662561),
#1077 `453e250dcad2b2f0c729883e580437e291117d74` (5904496637), #1076 `8697950a77ae9f09035c9bf66f610acae44d61bc`
(5904487312), #1080 head `ed3110736f632f8796b03e0ad15abd2760083f6f` (5908184401, approved "2 P3", matching F1 and F2).

## Recurrence table

| Defect class | Distinct lineages | Distinct PRs | Human | Bot | Verified recurrence |
|---|---|---|---|---|---|
| asymmetric boundary in a guard regex | 1 (F1) | 1 | 0 captured | 1 | no |
| fail-closed unknown state without bound/escalation | 1 (F2) | 1 | 0 captured | 1 | no |
| null vs unset semantic mismatch | 1 (F3) | 1 | 0 captured | 1 | no |
| unknown (F4) | 1 | 1 | 0 captured | 1 | not classifiable |

**Result: explicit insufficiency.** Every class has one lineage in one PR. F1 and F2 share a PR and a reviewer
pass, so they are not independent. Recurrence is not shown; it is also not disproved, because six PRs from one
window cannot show it. F1 and F2 share a theme (guards that fail quietly at the edges) only by interpretation; that
is a hypothesis, not a finding.

## Proposed rule texts (illustrative; not justified by this corpus; max 3 allowed, 2 given)

Both are narrow reviewer prompts, not policy. They would never suppress, down-rank or auto-resolve a finding, and
would grant no authority. A rule may only add a check; uncertain findings must still be reported as uncertain.

**R1** scope `orchestrator/exec_budget.py` only. "When a regex guard matches a shell word with separate start and
end boundary classes, check that quote characters are treated the same at both ends, and that a test covers each.
Report asymmetry as a finding with its severity unchanged." Exception: guards whose docstring states they are
hygiene-only may be reported at P3; this exception lowers no other rule and does not apply to guards that gate
authority. Source: F1 only.

**R2** scope `orchestrator/run_shepherd.py` only. "For any new fail-closed `*-unknown` state in the wait path, check
that either a per-head tick bound with an escalation status exists, or the PR text states why it is unbounded.
Report absence." Exception: a state explicitly bounded elsewhere in the same file (e.g. `merge_unknown_ticks`
style) needs no repeat note. Source: F2 only.

No third rule: F3 and F4 are not usable (F3 judged conservative; F4 unreadable).

Positive counterexamples (rule should fire): a new `_WORD_START` lookbehind that omits a quote class `_WORD_END`
accepts; a new `ci-unknown`-like state that stalls on repeated listing errors with only a log line.
Negative counterexamples (rule must stay silent): a symmetric boundary pair with both-end tests; an `*-unknown`
state with an existing tick cap; edits to other files with the same words. A rule firing on a path outside its
scope is a defect of the rule.

## Frozen holdout and leave-one-PR-out plan (proposed; not executed)

- Freeze before any rule is written: a corpus of merged PRs captured by the connector, each finding keyed by
  lineage id (original finding or its source: reviewed path + class + the earliest `sourceSHA`). Backports and
  cherry-picks carry the original's lineage id, so #1080-like duplicates count once. No lineage may appear in both
  the rule-derivation set and the evaluation set; split by lineage, and by PR within lineage.
- Leave-one-PR-out: for each PR in the frozen set, derive rules from all other PRs' lineages, then score on the
  held-out PR. The 2026-11-15 six-PR corpus is a pilot input only and is not itself the holdout (it was inspected
  here).
- Proposed minimum before a pilot (owner decision): at least 3 independent lineages of one class across at least 3
  PRs, with at least one human-origin or human-confirmed finding.
- Metrics, reported separately: precision = confirmed-real findings emitted / findings emitted by the rule;
  coverage = lineages caught by the rule / known lineages in scope. `unknown` (unlabelled, status not captured) is
  its own bucket and is not folded into zero or into wrong. Reviewer time and model-run cost are `unknown`; none
  were measured and no model was called.
- Small synthetic plan (illustration, not results): 4 PRs A-D, 6 lineages; evaluating on D uses rules from A-C
  only; a lineage with two backport copies in A and D is assigned to one side only. A rule that fires on a
  negative counterexample counts against precision; a lineage the rule misses counts against coverage.

## Smallest separately approvable follow-up

Recommended: re-run this spike on a larger owner-authorized connector capture. No pilot is approved.

- Allowlist: `docs/spikes/review-history-rules.md` only (or its revision); no code, workflow or instruction file.
- Needs: owner-authorized read-only capture of at least 50 merged PRs (reviews, inline comments, thread
  resolution, issue comments, human and bot), kept out of the repo, plus a lineage table.
- Checks: `git diff --check`; sole-file check (`git diff --name-only` shows only that file); every finding row has a
  PR/comment link and `sourceSHA`.
- Mutations (should change the result if the method is wrong): (a) count comments instead of lineages, then
  backports inflate recurrence; (b) count a one-line approval as a finding; (c) put one lineage into both training and
  holdout, then coverage rises falsely; (d) map `unknown` to zero, then precision rises falsely.

If recurrence is later verified, a `BOUNDED_RULE_PILOT` would be a different approval, with its own allowlist
(one conventions/instruction file, path-scoped, never granting authority), its own tests and mutations.

## Unknowns

Status (fixed/accepted) of F1-F4; whether #1080 shares lineage with #1079; content of F4; human reviews and issue
comments on these PRs; content of #974; merge status of #1014 branch; behaviour of the pinned reusable
claude-review workflow and whether it reads any CLAUDE.md; the vendor article's method and outcomes.
