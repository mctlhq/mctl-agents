# #848 reviewer-time measurement protocol (proposal)

STATUS: PROPOSED / NOT_RUN

Refs mctlhq/mctl-agents#848 and #974. Does not close either.

This is a protocol and recording-form proposal only. Nothing was run. There
are no participants, no study, no model calls and no scorer implementation.
The operator approved the direction on 2026-11-17. Participants, model use and
running any test are not approved. A separate execution authorization is
required for each (section 11). No number in this document is a result.

## 1. Scope and relation to existing work

- **#848 (existing body, as stated in `docs/spikes/670-decision-context.md`):**
  time the owner on two or three real high-risk approvals with and without the
  artifact, recording time, risk noticed and pointers used. It depends on #847.
  The #848 issue body itself was not re-read here. This protocol does not
  replace that scope. It adds a synthetic-first method and a recording form
  around it.
- **#670 contract `decision-context/v0`:** reused as is. The artifact is
  proposal-time and advisory (`docs/spikes/670-decision-context.md`). Nothing
  here changes the contract.
- **#974 report (PR #1090, head `6552e1f051b3e24a1fcc802e4ece2e34042dde1f`,
  file `docs/spikes/reviewer-decision-context-974.md`, plus the completed
  read-only task t-4901e613-d095-4647-8625-bc90e82731c5):** its section 7
  separates two questions. This protocol takes only the **primary one:
  approval of a proposal** (the `aar_` grant over the proposal files). It does
  not cover review of an implementation PR. That is a different artifact
  contract and is out of scope.
- **Not a replacement of #848 by a larger study.** The #974 draft sketched 16
  units and 4 reviewers. That is one possible size, not a decision. Sample
  size, participants and limits remain owner choices (section 10). Nothing here
  silently adopts it.
- **Deployment state:** whether #846, #847 or mctl-api#840 are merged or
  deployed is unknown here. Absence of source in this checkout is not a claim
  about any runtime.

## 2. What is measured, and what is not claimed

| quantity | owner of the number | counted as |
|---|---|---|
| Human reviewer effort: reading, checking the artifact, fallback, decision, common work | the reviewer-time record | the primary quantity |
| Machine latency, tokens, cost of producing the artifact | the producer run (#846) | separate; never added to reviewer effort |
| End-to-end turnaround (proposal ready to decision) | optional, own timestamps | separate endpoint, optional |

Rules:

- **Fallback is a component of human time, not an extra term.** If a reviewer
  falls back to the full proposal, that time is inside reviewer effort and is
  also reported as its own line. It is never added a second time.
- **Common work** (reading the proposal in either arm) is recorded in both
  arms.
- **Missing is not zero.** A field never captured is `unobserved`. A field
  that could not be determined is `unknown`. Neither is treated as `0`, a pass
  or "no risk". Records with missing data are reported separately, not
  dropped.
- Report individual values and median, min, max. With a handful of records, do
  not report a percentage saving.

Not claimed by this protocol or its pilot:

- any time saving, any "equal defect detection", any non-inferiority
- any cost figure (docs-only work is `unmeasured`, not zero)
- any statement about production behaviour

Risk noticed is **not full ground truth.** A reviewer who noticed nothing may
have missed something. With two or three real cases, equal defect detection
cannot be shown. This protocol adds no new powered-study GO gate. If the owner
later wants one, that is a separate decision.

## 3. Phases

1. **Synthetic method feasibility (recommended first).** Test whether the
   form, the timing method and the roles work on synthetic, non-sending
   material. Output: whether the instrument can capture the fields, nothing
   about reviewer speed.
2. **Real approvals (2 or 3, separately authorized).** Record the real owner
   decisions as #848 describes. Needs its own authorization and the
   dependencies in section 10.
3. A larger study is not proposed here. It would need its own owner decision.

## 4. Roles, provenance and the frozen oracle (synthetic cases)

Roles are held by different people, or at least different sessions, where
synthetic cases exist:

| role | does | must not |
|---|---|---|
| Author | writes the synthetic proposal unit and any seeded content | review it, score it |
| Oracle author | writes the oracle list of true proposal-level issues and freezes it before any reviewer sees the unit | review it |
| Artifact producer | generates the artifact from pinned inputs only | see the oracle or any implementation |
| Reviewer | decides on the unit and fills the reviewer parts of the record | see the oracle before scoring |
| Scorer | compares the reviewer record with the frozen oracle | author or review the same unit; know the arm, where feasible |
| Study coordinator | assigns pseudonyms, arms and order, holds the access-separated link | review or score |

- **Frozen oracle:** a list written before the first reviewer sees the unit,
  then hashed (digest recorded). Any later edit is a new oracle version and is
  logged. The oracle is kept apart from reviewers and the artifact producer.
- **Provenance:** each unit records who authored it, which pinned SHA and
  proposal digest it uses, and how the artifact was produced. A record with
  missing provenance is `unknown`, not trusted.
- **Defect classes with no safe oracle** are excluded and reported as
  excluded, not scored as pass. The class taxonomy from
  `docs/spikes/918-finding-oracle-classes.md` may be reused.
- Realistic limit: a small team may not have enough separate people. State the
  collapsed roles in the record rather than hiding it. Different Claude
  sessions alone are not proof of independence.

**Blind scoring method (synthetic cases).** The scorer never receives the full
review form, because it contains arm, artifact, timing and pointer fields.

- The coordinator holds the full record and the pseudonymous join.
- The coordinator builds a separate scoring packet per review. It contains only
  the anonymized proposal-level responses (decision and risks noticed) and the
  oracle criterion references the scorer needs. It contains no arm, no machine
  time, no artifact metadata and no pointer list.
- The oracle author and the scorer are different role holders, with separated
  access to the oracle and the join.
- Residual inference: if the responses themselves reveal the arm (for example
  by naming artifact pointers), blindness is partial. Record that disclosure.
- No new storage or permission contract is adopted here. The owner chooses
  authorized locations later (section 9).

## 5. Synthetic controls and containment

Synthetic units may include these control types:

- known clean
- known harmful (the artifact states something wrong about a defective unit)
- stale (artifact bound to an older digest or SHA; `freshness()` is `stale`)
- unknown (current value missing; `freshness()` is `unknown`)

Containment:

- They exist only as synthetic, non-sending material: not posted to GitHub,
  not sent to mctl-api, not placed in any live approval, grant, queue or
  gitops tree. Harmful controls use only synthetic, non-sending context. No
  live grant is changed.

Pre-review artifact validity and freshness behavior (artifact arm):

- If, before review, the artifact is invalid, stale or of unknown freshness,
  the reviewer must not use it to approve. The coordinator records either a
  regeneration from the pinned inputs or a fallback to the full-proposal
  baseline.
- Human fallback time is counted once, inside reviewer effort. Machine latency
  of any regeneration is recorded separately and never added to reviewer time.
- Record the arm actually used, any deviation from the assigned arm, and any
  failures. Unknown freshness is recorded as `unknown`, never reinterpreted as
  fresh or present.
- This is a recording rule for a proposed protocol. No study was executed.
- **No seeded defects and no deliberately harmful artifact in a live
  approval.** Real approvals (phase 2) use real, unmodified material only.
- A synthetic unit must be visibly marked synthetic to the study staff so it
  cannot be mistaken for a real proposal.

## 6. Authority boundary

- The full proposal is available in every arm. The mandatory approval, merge
  requirements and review rules are unchanged.
- **The artifact is never permission.** It does not skip, satisfy or waive any
  gate. A reviewer who relies on the artifact and skips reading is a measured
  outcome, not an allowed policy. This adds no new reading mandate and no new
  authority: the full proposal stays available and the existing mandatory
  reviews are unchanged. Observed behavior is not authority.
- Ownership, policy, approval and merge stay separate (ADR 010).
- "Context first, full proposal optional" is only a presentation order in the
  experiment.
- **Provenance caveat.** `PROPOSAL_DIGEST_FILES` in
  `orchestrator/proposal_state.py` lists four filenames
  (`requirements.md`, `design.md`, `tasks.md`, `proposed-content.md`).
  Keeping the artifact out of that list stops it entering the approved digest.
  It is **not proof of who or what wrote text** inside those four files. Text
  generated into an approved file would change the digest and be approved with
  it. This protocol does not address that and makes no provenance claim from
  the filename list.
- **Validator claims (verified against `docs/spikes/670/decision_context.py`
  by reading source only; tests were not read or run):**
  - `_scan_waivers` scans mapping keys recursively through dicts and lists and
    flags keys in `GATE_WAIVER_KEYS` or starting with `waive_`. It checks
    keys, not text values.
  - `freshness()` returns `fresh`, `stale` or `unknown`; a missing current
    value is `unknown`.
  - The validator is a spike fixture. No claim is made that it is wired into
    any runtime.
  Anything stronger must be labeled as documented only, not verified.
- Name collision: `action_approvals.DecisionContext` is policy metadata for
  mctl-api approvals. It is not the #670 artifact. Do not conflate them in any
  record.

## 7. Time capture

- The reviewer-effort clock starts when the reviewer is given the unit and
  stops at the recorded decision. Pauses and interruptions are logged, not
  hidden.
- **The instrument does not capture section reads or times automatically.**
  This protocol does not assume any tooling does. Start and end times,
  whether the full proposal was opened and which pointers were used are
  recorded by the coordinator or self-reported by the reviewer, and the record
  states which. Capture method is a required field.
- Self-reported values are labeled as such. A reviewer's recall is weaker than
  an observed log.
- Timestamps come from the session itself. Nothing is back-filled from
  history, and no live approval log is mined.
- Optional machine timings (latency, tokens, cost) come from the artifact
  producer run and stay in separate fields.

## 8. Limits to disclose with any result

- Real reviews use different units, so a with/without comparison across them
  is confounded by unit difficulty.
- Learning effects: a reviewer who has seen one arm is not naive for the next.
  Record the review index. With 2 or 3 real cases, order control is
  insufficient or incomplete and confounding is strong. Results are
  descriptive only, with no causal savings inference.
- The reviewer may be the owner who knows the system. This does not
  generalize.
- Counts are tiny. Results are descriptive only (individual records). No
  inferential statistics, no non-inferiority, no savings claim.
- Defect classes excluded from the oracle are reported, never counted as
  pass.
- No tests or scorer exist yet. Scoring tooling is future work and is not
  built here.

## 9. Secure, authorized data handling (proposal)

No new storage contract is created. If a study is later authorized:

- Records use pseudonymous reviewer IDs. The ID-to-person mapping is held by
  the study coordinator in an access-controlled place the owner chooses, apart
  from the records and apart from the oracle.
- Records hold no credentials, tokens, direct identities, real proposal text
  or private content. Pseudonyms and reference codes are allowed as designed.
  Brief category codes are preferable to free-text risk descriptions.
  Real-case records reference a proposal by digest and pinned SHA only.
- The master template stays blank. Only a later, authorized record copy is
  filled. Time and read capture stays honest: no hidden automatic
  instrumentation, and who recorded each value is stated.
- The frozen oracle and the access-separated link are readable by the scorer
  and coordinator only, until scoring ends.
- Where the records are stored, who can read them and how long they are kept
  are owner decisions made at execution authorization. Until then, no
  completed record is stored anywhere. The blank template is the only artifact.
- Any later correlation with an approval goes through the route the owner
  chooses (mctl-api#840) and the existing evidence vocabulary, not a new
  store.

## 10. Owner choices (unanswered)

1. Confirm or amend this protocol.
2. Who authors, who reviews, who scores, and whether roles may collapse.
3. Size, time cap, quality criteria and stop conditions, chosen before any
   run.
4. Dependencies: #846, #847 and mctl-api#840 correlation. Their state is
   unknown here.
5. Separate execution authorization for participants and any model use.

No decision is recorded in this document. No owner field is filled.

## 11. Next steps and status

- Directions approved: write this protocol and form.
- Not approved: participants, model calls, running tests, scorer
  implementation, real-case timing.
- Suggested order, if the owner agrees: confirm the protocol, run synthetic
  method feasibility under separate authorization, then decide on 2 or 3 real
  cases.
- No fabricated observations exist or should be added. Status stays
  PROPOSED / NOT_RUN until the owner records otherwise.
