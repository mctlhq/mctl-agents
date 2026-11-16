# Spike #1000: bounded-autonomy canvas, drift detection and decision records for DevLoop

STATUS: DRAFT (research only, recommendation pending owner decision)

Refs mctlhq/mctl-agents#1000. Research design in Markdown only. No prototype,
no policy, runtime, schema, tool, test or config change, no live read, no model
or network experiment. The issue lists a "drift-check prototype" outcome; this
bounded assignment authorizes none.

Recommendation token, one of which the owner replaces this marker with:
`REUSE_EXISTING_DECISION_CONTEXT`, `NARROW_ADVISORY_DRIFT_DESIGN`, `DEFER`.
Proposed (not decided): `REUSE_EXISTING_DECISION_CONTEXT`, with the single
bounded follow-up in "Recommendation" gated on owner approval.

## Evidence legend

Every factual claim below carries one label.

- DOC: stated in an ADR or spike report. A design statement, not behaviour.
- SRC: read in source, with the surrounding function, at `origin/main`.
- GREP: seen by search only; the surrounding code was not read.
- EXEC: executed proof. None in this task: nothing was run.
- UNK: unknown.

An ADR, a dataclass field or a test name is never counted as runtime
enforcement here. Nothing in testing or production was observed.

## Question and source status

Question (issue text): can a bounded-autonomy canvas, drift detection and
decision records limit scope and detect drift from intended behaviour in
DevLoop agents?

The InfoQ presentation: NOT_OBSERVED. Its text was not supplied; nothing in
this report claims what it says. The work is from the issue's question and
MCTL source only.

## Preflight and ownership check

- Branch `docs/bounded-autonomy-drift-1000` created from freshly fetched
  `origin/main`. No existing branch or commit was reset or rebased.
- `git branch -a --list "*1000*" "*drift*" "*canvas*"`: no local or remote
  branch (SRC, negative evidence only).
- A report for the same question already on any branch: none found in
  `docs/spikes/` on `origin/main` or on `origin/docs/context-memory-budget-989`
  (SRC, tree listing). The #989 report
  (`docs/spikes/context-memory-budget-989.md`, branch
  `docs/context-memory-budget-989`) exists and is not repeated here; it owns
  context memory, expiry and budget. This report does not restate it.
- The orchestrator's preflight (issue open and unassigned, `scout` label only,
  18 open PRs read, no #1000 reference) is taken as given: UNK to this task,
  not re-verified, since it needs live reads.
- `AGENTS.md`: none in the repository. `CONTRIBUTING.md` read. The bridge rules
  (Agent trailer, no Co-Authored-By) override the attribution reminder.
- Tooling note: the Grep and Glob tools were not available in this session.
  Searches used `git grep` and `git ls-tree` through Bash. They are read-only.

## What "canvas", "drift" and "decision record" mean here

Defined from the question, not from the article.

- Canvas: one bounded statement of intent per run: purpose, allowed scope,
  budget, expected results, stop conditions.
- Drift: a measured difference between that statement and what the run
  observably did.
- Decision record: a durable, bound, non-authoritative record of why a human or
  gate decided something.

## Duplicate and ownership map

Each row: the canvas concept, where it already exists, who owns it, and how
strong the evidence is. The "gap" column says what is not covered.

| Concept | Existing owner and mechanism | Evidence | Gap |
| --- | --- | --- | --- |
| Intention | The proposal triplet plus `proposed-content.md`, hashed by `proposal_state.content_digest_of` over `PROPOSAL_DIGEST_FILES` (`proposal_state.py:165-202`) | SRC | Prose. No machine-readable scope inside it |
| Human authorization of that intention | `aar_` grant: `request_devloop_approval` binds `artifact_hash = content_digest` (`delegated_approval.py:116-148`); the implementer reads it back and requires equal digest, human decider, matching target, state `consumed`/`executed` (`approval_evidence.verify_execution_grant`, `approval_evidence.py:117-167`) | SRC | None for freshness of approval against content |
| Stale approval or stale content | Mismatch refuses (`GRANT_CONTENT_MISMATCH`); a digest that cannot be computed refuses; an unreadable store is `transient`, never authorization (`approval_evidence.py:132-137,155-160`). Local blocker codes in `proposal_state.py:173-174` | SRC | None found for the file digest. Stale target repo head vs approval is UNK (not read) |
| Allowed files / scope | Prompt text only: "Stay strictly within the proposal's scope", "do not refactor outside the touched files" (`run_implementer.py:2154,2201,2014`) | SRC (prompt strings) | No machine-readable allowed-file list found. Textual instruction to the agent, not a check (GREP-level absence: no `allowed_paths`/`allowed_files` symbol anywhere in `orchestrator/`) |
| Path-level guard after the fact | (a) chart MAJOR bump detector run before push, blocks unless a `crd-migration*` file exists (`run_implementer.py:5129-5158`, `_detect_chart_major_bumps` at `:3468`); (b) shepherd merge gate on changed paths under `agents/`, `platform-gitops/agent-platform/`, fail-closed when the list is truncated or unreadable (`run_shepherd.py:710-765`, `merge_operation_for` at `:815`) | SRC | Both are single-purpose rules for named risks. Neither compares the diff to the proposal |
| Budget | Wall-clock envelope and nested per-command bounds (ADR 011; `exec_budget.py`); `budget_exhausted` outcomes in the completion record | DOC + SRC (completion mapping read at `run_implementer.py:5540-5568`; `exec_budget.py` not read in full) | Time only. No file-count, line-count or paths budget found |
| Expected results | `tasks.md` in the digest set; ADR 021 names follow-up F1 `task_coverage` that "compares the pushed head with the approved `tasks.md`" and says it does not exist | DOC; absence GREP (`task_coverage` appears only in docs and a module docstring, `implementation_completion.py:17`) | The expected-vs-observed comparison is the named, unbuilt F1 |
| Observed outcome | `ImplementationCompletion`: sealed record from the exit and observations, four required gates `commits_present`, `chart_major_guard`, `push_succeeded`, `claim_valid_at_push` (`run_implementer.py:5581-5635`) | SRC | `complete` means "gates passed", never "scope done" (ADR 021 DOC). No changed-path list in the record |
| Consumer of the observed outcome | ADR 021 lists shepherd shadow (#679) and Temporal observe-only (#680) as follow-ups | DOC; grep of `run_shepherd.py` and `orchestrator/temporal/` for `completion` consumers found none (GREP) | Whether anything reads the record today: GREP-negative only |
| Lifecycle ownership vs authorization | ADR 010: ownership and claims fence who may act, "Ownership never implies authorization"; epoch and head fences | DOC; fencing call sites cited in the ADR (not re-read here) | Out of scope for drift. Reuse, do not extend |
| Runtime action gate | ADR 014 checkpoint: ALLOW/DENY/REQUIRE_APPROVAL before the side effect, args digest, approval receipt bound to action, one record per decision | DOC; call sites enumerated in ADR §5. Bash is explicitly ungoverned (ADR 014 open decision 4) | Governs actions, not the agent's file edits. The checkpoint binds `push` to branch and lease, not the pushed commit (ADR 014 §5) |
| Decision record | `decision_records.py`: durable records of policy decisions, producer half of mctl-api#701 | SRC (module list read; internals not read) | Records gate decisions, not intent-vs-outcome |
| Explainer / decision context | `docs/spikes/670-decision-context.md`, `docs/spikes/670/decision_context.py`: grounded claims, `binding` (digest, repo, 40-hex SHA), `freshness()` returning `fresh`/`stale`/`unknown`, gate-waiver keys rejected at any depth. LIMITED_GO accepted 2026-10-27 on n=1 | DOC + SRC (validator names and test names read; `validate` body not read) | Follow-up "post-implementation drift check" is already named in the #670 report, not built |
| Review evidence | ADR 025 review receipts: bound to digest and SHAs, `stale` on change, unobserved never `PASS`; Tier A pure module, nothing consumes the gate yet (DOC) | DOC | Reviews proposals and spike reports, not diffs against intent |
| Reviewer-effort measurement | #974 and #848 (per orchestrator; #848 is also named in the #670 report as the timing follow-up) | DOC for #848; #974 UNK (not read) | Not measured here. No human timing in this task |
| Git coordination, immutable execution state | #747, PARKED, DO NOT START (per orchestrator) | UNK | Not activated, not treated as generic memory ownership |

### What is already covered, in one paragraph

Intention is bound to approval by digest and verified at execution (SRC).
Approval, ownership and action gating are separate authorities by design (DOC).
An observed-outcome record exists and says "gates passed" only (SRC). The
single missing link is a comparison between the approved intent and the
observed change, and the repo already names it twice: ADR 021 F1
`task_coverage`, and the #670 follow-up "post-implementation drift check". So
the drift idea is a duplicate of two existing, owned, unbuilt follow-ups. It is
not a new capability.

### Where the owners are

- Approval and content binding: DevLoop approval, ADR 014 §6 and
  `approval_evidence.py`.
- Observed implementer outcome and F1: ADR 021, issue #428, consumers #679 and
  #680.
- Advisory explanation and its follow-ups: #670 with #846, #847, #848 and
  mctl-api#840.
- Review binding: ADR 025, issue #905.
- Lifecycle: ADR 010, #350 and phases.

Owner status of the follow-up issues (open, closed, merged): UNK, not read.

## Source-read path: scope, claims, approval, digest, freshness

The path as read in source, in order. Each step is SRC unless stated.

1. Proposal files are hashed; the digest covers four files only
   (`proposal_state.py:165-202`). A missing file hashes distinctly from an
   empty one.
2. `request_devloop_approval` reads the content from gitops main at one
   commit, then creates the `aar_` with `artifact_hash = digest`. Unreadable
   content is raised for retry, never requested without a digest
   (`delegated_approval.py:137-170`).
3. At execution the implementer recomputes the digest and compares with the
   recorded one before starting (`approval_digest_verdict`,
   `proposal_state.py:208-226`), and separately verifies the human grant
   (`approval_evidence.py:117-167`). A mismatch is a durable refusal; an
   unreadable store is transient. Uncertainty is never authorization.
4. The ExecutionClaim and checkpoint are checked immediately before the push
   (ADR 010, ADR 014 DOC; implementer call sites cited in the ADRs, not
   re-read here).
5. Between steps 3 and 4 the agent runs under the prompt, a wall-clock
   envelope and the Bash budget. Nothing in the read path inspects which files
   the agent changed against the proposal, other than the chart guard
   (`run_implementer.py:5137`).
6. After the attempt, the completion record is sealed from observations.
7. The shepherd reads changed paths at merge time for the protected-prefix
   rule (`run_shepherd.py:1874`, GREP at the definition; summary SRC at
   `:715-765`).

Freshness: `decision_context.freshness()` returns `fresh`, `stale` or
`unknown`, with a missing current value `unknown` (DOC in the #670 report,
`tests/test_spike_670_decision_context.py::test_freshness` exists, test body
not read). The approval path's freshness is the digest equality in step 3
(SRC). The two are separate and agree on "unknown is not fresh".

### Caveats on the read

- `run_implementer.py` is large; only the cited regions and function
  boundaries were read. The claim check and push code were taken from ADR 010
  and ADR 014 text, not re-read. Treat those as DOC.
- `exec_budget.py`, `decision_records.py` internals and the
  `decision_context.py` `validate` body were not read in full.
- No test was run. "Test exists" means the name was seen, not that it passes.

## Options

All three keep one rule: any drift output is advisory evidence only, never
permission, and never a second authorization gate, identity, decision store or
generic canvas runtime. No option authorizes any run or merge.

### REUSE_EXISTING_DECISION_CONTEXT

Treat "canvas" as the proposal triplet plus a `decision-context/v0` artifact,
treat "decision record" as `decision_records` plus the `aar_` grant, and treat
"drift check" as the already-named follow-ups: ADR 021 F1 and the #670
post-implementation drift check. Add nothing. If the owner wants the
comparison, it is scheduled under those existing owners and under the
existing #848 measurement.

- Pro: no new identity, store or gate; no duplicate surface.
- Pro: the two owning follow-ups already carry the "advisory, not a gate"
  constraints (ADR 021 non-authorities; #670 `not_changing` and rejected
  waiver keys).
- Con: leaves the question "does a drift comparison add detection value over
  the chart guard and merge path gate?" unanswered until the owners schedule
  F1.
- Con: the #670 evidence is n=1, one high-risk subject, no human timing (DOC).

### NARROW_ADVISORY_DRIFT_DESIGN

A pure offline function comparing an approved scope statement to the observed
changed-path list, emitting `ok` / `drift` / `unknown`. It would live as the
ADR 021 F1 check, not as a new module family, and read `tasks.md`.

- Pro: closes the one named gap with a deterministic, replayable check.
- Con: needs a machine-readable scope inside prose `tasks.md`. That is either
  a schema change to the proposal contract (out of bounds for this task and a
  digest-set change) or a model-extracted scope, which makes the oracle
  probabilistic.
- Con: textual path scope cannot see behaviour (see false negatives).
- Not authorized now. Only after the experiment below shows a measurable gap.

### DEFER

Record the question as covered by #428 F1 and #670 follow-ups and take no
step until a measured miss exists in the existing pipeline.

- Pro: nothing is built on n=1 evidence.
- Con: no new information about whether the gap is real.

## Comparison

| Aspect | REUSE | NARROW | DEFER |
| --- | --- | --- | --- |
| New authority or store | none | none (pure function, advisory) | none |
| Touches approval, policy, digest set | no | only if a scope field enters `PROPOSAL_DIGEST_FILES` (to be avoided, see #670 Q5 rationale) | no |
| Gap closed | none directly | path-level intent vs change | none |
| Gap measured | no | by the experiment below | no |
| Duplicates an existing owner | no | overlaps F1 unless it is F1 | no |
| Cost, latency, success gain | unmeasured | unmeasured | unmeasured |

No gain in success, cost, latency or reliability is claimed for any option.
All are UNK until measured.

## Semantics versus textual scope checks

Two different checks, kept apart so one is never mistaken for the other.

- Textual scope check: changed paths against an allowed pattern list.
  Deterministic, cheap, covers only "where". The shepherd's protected-prefix
  rule is this kind (SRC) and fails closed on an unreadable list.
- Semantic check: does the change do what the approved intent says, and nothing
  else. Needs reading the diff and `tasks.md`. Not deterministic; the only
  semantic evidence in the repo is the explainer's claims, spot-checked by
  hand on one subject (DOC, #670).

A textual pass says nothing about semantic correctness. Reporting a combined
"no drift" from a path check alone would be a false assurance; the output
space must keep `unknown` for what was not checked.

## False-positive examples (drift flagged, nothing wrong)

1. Operator-authorized amendment. A human edits `tasks.md` after approval, then
   re-approves. The new digest is a different intent (SRC: mismatch is
   refused until re-approved). A drift check anchored on the old scope flags
   every change as drift. It must read the digest the grant is bound to, and
   treat "approval superseded" as its own state, not as drift.
2. Allowed test changes. The proposal says "fix X" and the agent adds tests.
   Test files are outside any path list that came from `tasks.md` prose. A
   path-list check flags them; a semantic reviewer would not.
3. Generated or lock files. A dependency or lockfile change follows from an
   in-scope edit (for example, regenerated files). Flagged by path, in scope by
   cause.
4. Chart bump with a `crd-migration*` file. The chart guard allows it by design
   (SRC `run_implementer.py:5138`). A drift check that re-flags it duplicates a
   decision the proposal author already made.
5. Review-remediation phase. Review-feedback runs change code for findings
   that were not in `tasks.md`. Their scope is the findings, not the proposal
   (SRC prompt `run_implementer.py:2154` "fixing the {work_items} only").
   Comparing them with the original proposal scope is the wrong baseline.

## False-negative scenarios (drift present, not flagged)

1. Correct paths, unrelated behaviour. The agent edits only allowed files but
   changes logic outside the task. A path check passes. Only a semantic
   comparison could see it.
2. Stale approval or head. The approved digest still matches `tasks.md`, but
   the target repo head moved after the explainer or review was produced. The
   #670 `freshness()` binds target SHA (DOC); the approval digest does not
   cover the target head (SRC: only the four proposal files). A check that
   reads only the digest would say fresh. Whether anything compares the target
   repo head at implementation time with the head the approval was decided
   against: UNK.
3. Forged or unverified evidence. A drift verdict is only as good as the
   observation it reads. The completion record is sealed from orchestrator
   observations, not authored by the agent (ADR 021 DOC; non-authorities
   rejected as unknown keys). A `.status.yaml` block is "only a claim"
   (`approval_evidence.py` docstring, SRC). Any drift input read from a
   writable tree is forgeable by whoever can write that tree. The experiment
   must include a forged-input mutation and the check must answer `unknown`,
   not `ok`.
4. Truncated or unreadable observation. A 100-path page limit exists on the
   changed-path read (`CHANGED_PATHS_PAGE = 100`, SRC). A check that treats a
   truncated list as complete answers `ok` for the paths it never saw. The
   shepherd handles this with three states (SRC); a drift check must reuse
   the same three-state shape, not collapse it.
5. Agent Bash outside the checkpoint. A side effect made from the agent's
   shell is not governed (ADR 014 open decision 4, DOC). No drift check of
   the file diff sees an external side effect.
6. Replayed completion. The completion replay key rules are documented
   (ADR 021 DOC); not read in code here.

## Missing, unknown and denial are different outputs

Three distinct outcomes, never merged.

- `ok`: every checked dimension was observed and matched.
- `drift`: a checked dimension was observed and differed. Advisory only.
- `unknown`: an input was missing, truncated, stale, unreadable or unverified.
  This is not `ok` and not `drift`. It is not a denial.

A denial is a gate outcome (ADR 014 DENY, ADR 021 refusals). The drift output
is never a gate input: no merge, approval, claim, budget or ownership decision
may read it. This mirrors `freshness()` returning `unknown` for a missing value
(DOC) and ADR 025's rule that unobserved evidence is never `PASS` (DOC).

## Smallest future offline synthetic experiment (NOT_RUN)

Status: NOT_RUN. Nothing in this section was executed. It is a design only,
for a later, separately authorized task.

Purpose: falsify "a drift check adds detection over the existing guards",
using no live data, no model call and no network.

### Frozen oracle

- A small set of synthetic proposals (hand-authored, at least 12) each with a
  recorded intended scope and a recorded true label for each synthetic change:
  `in_scope`, `path_drift`, `behaviour_drift`, `stale_binding`,
  `forged_input`, `unobservable`.
- Labels are written first and hashed (the existing digest framing) before any
  checker is run, then frozen. A checker may not read its own labels.
- Expected result per case is one of `ok`, `drift`, `unknown`.

### Same inputs, existing baseline

Both arms read the same frozen cases.

- Baseline arm: what exists today, replayed as pure functions over the case
  inputs: digest verdict (`approval_digest_verdict` logic), the chart major
  guard logic, and `touches_agent_definitions`-style path classification.
  Each baseline output is mapped to `ok`/`drift`/`unknown` by a fixed table
  written before the run.
- Candidate arm: the narrow comparison (changed paths versus scope statement)
  as a pure function.

A baseline reused as-is must not be modified to match the candidate.

### Measurements

All counts, never rates presented as gains, with `n` shown:

- false positives: `in_scope` cases flagged `drift`, per arm;
- false negatives: `path_drift`, `behaviour_drift`, `stale_binding` cases
  answered `ok`, per arm;
- `unknown` handling: share of `unobservable` and `forged_input` cases answered
  `unknown` rather than `ok`;
- agreement matrix between arms;
- every disagreement listed as a counterexample with its case id.

Cost, latency and reviewer time: UNK, not measured. The candidate arm is pure
so cost and latency would be near zero by construction, but that is not a
measurement and is not claimed. Reviewer effort belongs to #974 and #848; no
human timing here.

### Counterexamples and mutations to include

- Operator amendment with re-approval (false-positive example 1).
- Allowed test additions (example 2).
- Path list truncated at the 100-path page (false negative 4).
- Approval digest matches, target head moved (false negative 2).
- Mutations: flip one byte of `tasks.md` after approval; drop one changed path
  from the observation; replace the observation with an unsealed copy; swap
  `unknown` for `ok` in the candidate and confirm a case fails; relax the
  candidate to treat truncated as complete and confirm a case fails. Each
  guard must fail in both directions, as ADR 010 requires of its guards.

### Falsification and stop rules

- If the baseline already answers every frozen `path_drift` and
  `stale_binding` case correctly, there is no measured gap: outcome DEFER or
  REUSE.
- If the candidate matches the baseline on every case, same.
- If the candidate flags a false positive on an operator amendment or allowed
  test, the narrow design is rejected as specified.
- Behaviour drift cases are expected to defeat both arms. That is the point:
  it shows what a path check cannot claim.
- A synthetic corpus proves nothing about the production workload. A positive
  result earns only a follow-up on real, authorized data, not a rollout.

## Invariants

- Drift output is advisory evidence only, never permission.
- No second authorization gate, identity, decision store or generic canvas
  runtime. Reuse ADR 010 ownership, ADR 014 approvals and ADR 018 evidence.
- Any scope statement stays outside `PROPOSAL_DIGEST_FILES` unless the owner
  chooses a contract change deliberately (the #670 Q5 reasoning: approving an
  explanation must not become approving the change).
- #747 is not activated and is not treated as memory ownership. #989 is not
  duplicated.

## Possible vulnerabilities to report to the orchestrator

No security audit was assigned and none was done. No exploitation, no fix.
Observations made incidentally while reading, for the orchestrator to route:

- ADR 014 open decision 4 (DOC): the agent's own `Bash` is outside the policy
  checkpoint, so a side effect reachable by `gh`/`git` during the agent turn is
  not governed. Already documented by the ADR; listed only because it bounds
  what any drift check can see.
- `.status.yaml` content is described as "only a claim" (SRC,
  `approval_evidence.py`). Any drift input sourced from it would be forgeable by
  a writer of that tree. Design note, not a finding.

Neither was verified in running code.

## Recommendation (owner-gated)

Proposed: `REUSE_EXISTING_DECISION_CONTEXT`.

Reasoning from source: intent, approval, content freshness, budget, outcome and
review binding each already have an owner and a mechanism. The one unfilled
link, intent versus observed change, is already named twice as an unbuilt
follow-up (ADR 021 F1; #670 post-implementation drift check). The question as
posed does not justify a new design or runtime.

Owner decisions remaining:

1. Confirm REUSE, or choose NARROW_ADVISORY_DRIFT_DESIGN or DEFER.
2. Whether the single bounded follow-up below is wanted. It is a proposal, not
   a new issue, epic or implementation grant. If wanted, it belongs under ADR
   021 F1 / #428, not under #1000: a separately authorized offline run of the
   NOT_RUN experiment above, to learn whether the existing guards already answer
   the frozen `path_drift` and `stale_binding` cases. Its outcome decides
   whether F1 is worth building.
3. Whether the target-repo-head versus approval question (false negative 2) is
   worth a read-only look by the lifecycle owner; currently UNK.
4. Treatment of the article: provide the text if it should inform the work.

Not claimed anywhere: success, cost, latency or reliability gain; that the
completion record has a consumer; that any guard exists beyond what is cited.

## Source index

DOC: `docs/spikes/670-decision-context.md`,
`docs/adr/010-lifecycle-ownership-contract.md`,
`docs/adr/011-execution-budget-contract.md` (first 120 lines),
`docs/adr/014-policy-checkpoint.md`,
`docs/adr/021-implementation-completion-contract.md`,
`docs/adr/025-independent-review-receipts.md`.

SRC (regions read): `orchestrator/proposal_state.py:150-230`,
`orchestrator/approval_evidence.py` (whole file),
`orchestrator/temporal/activities/delegated_approval.py:100-170`,
`orchestrator/action_approvals.py:240-315`,
`orchestrator/run_implementer.py:3630-3690,5110-5170,5540-5650`,
`orchestrator/run_shepherd.py:700-870`.

GREP only: `task_coverage`, `allowed_paths`, `allowed_files`, `scope_done`,
completion consumers in `run_shepherd.py` and `orchestrator/temporal/`, test
names in `tests/test_spike_670_decision_context.py`, `decision_context.py`
symbol list.

Not read: `execution_evidence.py`, `exec_budget.py` internals,
`decision_records.py` internals, `decision_context.py` `validate` body,
`dev_loop.py`, the fencing call sites in `run_implementer.py`.
