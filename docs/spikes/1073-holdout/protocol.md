# Independent log-span holdout protocol (Refs #1073)

**Status: PROPOSED / NOT_RUN.**
This is a documentation-only protocol. No holdout has been authored, locked or
run. Nothing here is merged, runtime-observed, or a measured benefit.

- The operator approved the *direction* (an independent log-span holdout that
  keeps coverage on head/tail positive controls). The operator did **not**
  choose an author, accept any acceptance threshold, or authorize execution.
- There is **no owner sign-off** recorded in this document. Every "owner
  decision" below is open until the owner records it, in a place outside this
  file, before the primary run.
- This document authorizes no test code, fixture, scorer, data, dependency,
  setting or runtime change. Section 11 lists *proposals only*.

## 1. Basis and its limits

Basis: the author's own reviewed pilot, `tests/test_log_span_pilot.py` on the
existing branch `test/synthetic-log-span-pilot-1073` at head
`c97a1e6328c7aefc2a1cd69282823e2266df6105`. That branch is **not merged** into
`main`; this protocol was written from reading it, not from running it.

What the pilot is: a test-local, pure line-span selector compared with the
shipped `orchestrator.ci_checks._bound_log` head/tail excerpt on 17 invented
cases, using `ci_checks.CI_LOG_MAX_CHARS` (8000 characters on current `main`) as
the cap. Its recorded verdict is **MIXED** (some cases ahead, at least one
recorded loss: `16-regression-unrecognized-tail-failure`).

Why it is not evidence of generalization: the same author wrote the selector,
the fixtures and the oracle labels, and the selector regexes were shaped while
the cases were visible. Hence **no independent holdout exists**. The pilot's own
header says NO_GAIN is legitimate and no quality floor is asserted; this
protocol does not change that statement for the pilot.

## 2. Purpose and non-claims

Purpose: define, before anything is run, how one independent holdout would be
authored, frozen, executed once, and reported, so the result cannot be bent
after the fact.

Non-claims:

- No universal or production-quality claim, from any outcome. The holdout is
  invented, small and synthetic; it bounds one candidate against one baseline
  on one case set.
- No claim about real CI logs, real secrets, real models or networks.
- A favorable outcome would mean only "on this holdout, under these frozen
  rules"; it would not authorize shipping anything.

## 3. Roles

| Role | Does | Must not |
| --- | --- | --- |
| Owner | Selects the independent author; fixes the remaining criteria (section 9) before the test; separately authorizes execution | Be implied by this document |
| Independent author | Writes holdout cases from the frozen input spec (section 4) | See the selector, pilot cases, pilot outcomes, baseline output, or `baseline.json` |
| Runner | Pins versions (section 5), runs once, reports (section 8) | Edit candidate, baseline, harness or cases after disclosure |

The pilot author is not eligible as holdout author. The runner may be the pilot
author only for execution, and only after the cases are frozen.

## 4. Frozen author input spec

The independent author receives **only**:

1. The task statement: "invent log text with known evidence lines; mark which
   lines a reader needs."
2. The category list in section 7, with a minimum case count per category set
   by the owner (suggested floor: at least 2 per category; owner decides).
3. The character cap value, stated as characters not bytes, and the line
   convention: LF-separated, a CR from CRLF stays inside its line, a final LF
   ends the last line instead of starting an empty one.
4. The oracle format (section 6).
5. The invented-content rules: no real logs, no secrets or credential-shaped
   strings, no real hostnames/people, no network or model use.

The author is **not** shown: the selector or its regexes, the pilot cases or
labels, pilot outcomes or the MIXED verdict, the baseline's marker format,
`CONTEXT_BEFORE/AFTER`, the trace-walk limit, or any candidate behavior.

The spec is frozen as bytes (hash recorded, section 5) before the author starts.

### Known-access limitations (to be recorded, not assumed away)

- A fresh session or a new agent context **alone is not proof of
  independence**. Model/author memory, shared repository access, shared
  prompts or the same underlying model can leak the pilot. Independence is a
  claim to be argued from a recorded access log, not a property of "a new
  session".
- The author must declare, in writing before authoring: what they had access
  to (repository, branches, issue #1073 text, prior chat), whether they read
  the pilot or `ci_checks`, and which tools they used. Any access to the
  pilot/selector/outcomes is recorded and graded: **not-exposed**,
  **possibly-exposed** or **exposed**.
- If exposure is `exposed`, the run is not an independent holdout and is
  reported as such (it may still run as a non-independent check, labelled so).
- Because #1073 is public to the repository, "not shown" is a process
  commitment, not a technical guarantee. State that limitation in the report.

## 5. Pins and hashes (fixed before disclosure)

Before the author's cases are disclosed to anyone who can see the candidate,
record, in one pinned manifest:

- candidate: commit SHA of the selector and its source-file blob hashes;
- baseline: commit SHA of `main` and the blob hash of `orchestrator/ci_checks.py`
  (so `_bound_log` and `CI_LOG_MAX_CHARS` are fixed);
- harness: commit SHA and blob hashes of the scorer/runner;
- interpreter and dependency versions used by the run;
- the frozen author input spec hash;
- the cap value in characters.

**Hashes fix bytes, not provenance.** A SHA-256 of a fixture proves the bytes
did not change; it does not prove who wrote them, that the author was
independent, or that a run happened. The report must not present a matching hash
as authentication of authorship or execution.

If any pinned item changes after disclosure, the holdout is spent for that
candidate (section 10).

## 6. Oracle requirements

Each case provides, per source, required lines (the reader needs them) and
acceptable lines (not noise, not required).

The oracle must identify lines **unambiguously, including repeated lines**. The
pilot's resolver matches oracle entries by line text and rejects anything that
does not match exactly one source line; that is unusable when the same evidence
text legitimately appears several times (for example repeated retry lines or a
repeated error message where only one occurrence is the root cause). The
holdout oracle therefore must identify a line by something that distinguishes
occurrences. Acceptable designs, owner picks one before authoring:

- 1-based line number against the frozen byte content (with the line convention
  of section 4); or
- text plus an explicit occurrence index (the Nth identical line).

Rules: an oracle that is ambiguous for a given line is a **case authoring
defect** and the case is excluded and listed, never silently fixed by the
runner. Required and acceptable sets must be disjoint. An unlabelled case is
allowed only if reported with undefined (null) quality, never 0. This section
specifies a requirement; it designs no schema and implements nothing.

## 7. Required case categories (all invented)

1. **Head positive controls** - the sole failure evidence is within the first
   part that the baseline's head excerpt keeps.
2. **Tail positive controls** - the sole failure evidence is within the tail
   that the baseline keeps.
3. **Middle evidence over cap** - the log exceeds the cap and the required line
   lies in the part the baseline elides.
4. **Early root cause plus late anchors** - the cause is early; later lines
   look like failure but are consequences.
5. **Fatal / recovered / benign mixed** - a fatal failure alongside recovered
   errors and benign error-like text (zero-error summaries, flag names, handler
   names).
6. **Healthy / empty / unknown** - success logs, empty input, and logs with no
   recognizable verdict.
7. **Encoding and line-shape** - Unicode (multibyte, so characters differ from
   bytes), CRLF, no final LF, and **repeated lines**.

Head/tail positive controls exist to detect the one regression this protocol
treats as blocking (section 9). Cases must be invented; none may be derived from
a real log, contain a real secret or credential shape, or need network/model.

## 8. Metrics and reporting

Measure against the **exact baseline output**: the real result of
`_bound_log(text, CAP)` on the same source, not a re-derived head/tail formula.
The runner reads which source characters/lines that output actually carries from
the output itself (as the pilot does with the elision marker), and does not copy
the baseline function into the harness; the harness imports the shipped one.

Budget: the cap is the current `CI_LOG_MAX_CHARS`, counted in **characters**.
Emitted text of both sides must be at most the cap.

Report two views **symmetrically**, both sides always treated identically:

- **raw**: emitted content as produced;
- **screened**: after the existing credential-shape screen with whole-content
  refusal (recognized shapes only; not comprehensive secret detection). The
  screen is applied to baseline and candidate alike.

Per case and view, report coverage (required lines fully carried / required) and
noise (emitted lines that are neither required nor acceptable / emitted lines)
with explicit numerator and denominator. Undefined is null, never 0.

Keep these outcomes **distinct**, never merged or counted as wins/losses:

- **refusal** (screen refused the whole emission);
- **unknown / abstain** (selector declined to choose spans);
- **not-comparable** (a metric is defined for one side only);
- **tie**;
- **loss** (baseline ahead) and **gain** (candidate ahead), with what decided it
  (coverage or noise).

An abstention is not a refusal and neither is a tie. A case where the candidate
abstains while the baseline carries the required line is a coverage loss, not
"not comparable"; the report must state which rule applied.

## 9. Acceptance

**Fixed, blocking criterion:** no coverage loss on the head/tail positive
controls (categories 1 and 2) in either view. Any such loss means the
candidate fails this protocol regardless of other results.

Reporting rules for everything else:

- All other regressions (any loss in categories 3-7, noise regressions,
  refusals or abstentions that cost coverage) are **always listed separately**,
  per case.
- **No compensation**: gains from noise reduction do not offset coverage
  losses; per-case outcomes are not netted into one score.
- Any threshold for a **favorable overall conclusion** (for example how many
  middle-evidence cases must be gained, how many other losses are tolerable) is
  an **owner decision made and recorded before the test**. It is not set here and
  not set after seeing results.
- Because of section 2, even a favorable conclusion carries no universal or
  production-quality claim.

### Status vocabulary

- **PREREQUISITE_UNMET**: any of these is missing: an owner-selected
  independent author, recorded author access declaration, frozen input spec,
  locked pins/manifest, owner-recorded criteria, or execution authorization.
  The run does not happen. This is **never reported as NO_GAIN**: NO_GAIN is a
  measured outcome and PREREQUISITE_UNMET means nothing was measured.
- **NOT_RUN**: authorized prerequisites exist but the run has not occurred (the
  current state of this document is PROPOSED / NOT_RUN).
- **NO_GAIN / MIXED / GAIN / REGRESSION**: only after a real run, per the
  pilot's definitions applied to the pre-recorded owner criteria.

## 10. Run discipline

- **One primary holdout run.** Its result is the result.
- **No tuning afterward.** After the cases are disclosed to the runner, the
  candidate, baseline, harness, cases and criteria are frozen. Fixing a defect
  found by the run does not rescue that run.
- A **changed candidate requires a new, fresh holdout** (new cases, preferably
  a new independent author or at least cases the changed candidate's author has
  not seen). A holdout already used to inform a change is spent.
- Re-running unchanged inputs for determinism/diagnostics is permitted and is
  labelled a replay, not a second result.
- Do not pick the best variant on the holdout (section 12).

## 11. Proposed future artifacts (proposals only; not authorized)

Illustrative placements, each subject to separate owner authorization and
review; none is created by this change:

- holdout cases and oracle under a path such as `tests/fixtures/log_span_holdout/`;
- a holdout runner/scorer test such as `tests/test_log_span_holdout.py`;
- a recorded pin manifest and the one result record.

**Dependency on the unmerged pilot.** The pilot's selector, scorer,
`resolve_oracle`, `compare` and baseline interval recovery live only on the
unmerged branch at the head above. A holdout runner would depend on that code.
Rules for such a dependency:

- import the pilot symbols from a pinned, reviewed revision; do **not** copy
  the baseline function or the pilot's baseline arithmetic into new files;
- import `orchestrator.ci_checks._bound_log` and `CI_LOG_MAX_CHARS` from the
  shipped module, so the baseline is the real one;
- **safe-import check**: before running, verify the module being imported has
  no import-time side effects (no network, filesystem writes, subprocess,
  clock/env reads at import) and that the imported object is the pinned one
  (identity check, as the pilot does for `_bound_log`);
- if the pilot is never merged, the dependency is unmet and the status is
  PREREQUISITE_UNMET, not a reason to reimplement a copy;
- the pilot's oracle resolver would need a repeated-line-safe replacement
  (section 6); that is a proposal and is not designed here.

## 12. Integrity mutation proof vs research variants

Two different things; never conflate them.

**Integrity mutation proof** checks that the *harness guards work*: remove or
break one guard (hash check, range validation, character cap, version/pin
guard) and confirm the **intended test fails**. It proves the guard is
load-bearing. Use the repository's usual temporary-mutation discipline
(commit first, mutate, run, restore, confirm clean diff). It says nothing about
which selector is better.

**Research algorithm variants** (a fallback to head/tail, a head/tail
combination, regex or context changes) are *candidates*, not mutations. They
must **not** be called mutation proof, and **must not be selected by best result
on the holdout**. Any such variant is a different candidate and, if tried after
the primary run, needs a new fresh holdout (section 10).

## 13. Remaining owner decisions

1. Select the independent author and the exposure grading accepted.
2. Choose the oracle identification design (line number vs occurrence index).
3. Set minimum cases per category.
4. Set the threshold for a favorable overall conclusion, before the test.
5. Confirm the cap and baseline revision to pin.
6. Decide how the unmerged pilot dependency is satisfied (merge, pin, or other).
7. **Separately authorize** future execution. This document does not.

## 14. Evidence statement

Reviewed by reading only: the pilot at `c97a1e6328c7aefc2a1cd69282823e2266df6105`
and `orchestrator/ci_checks.py::_bound_log` on `main`. Nothing was executed, no
tests were run, no experiment was performed, no author was recruited, and no
result exists.
