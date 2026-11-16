# Spike #974: can human review use decision-context artifacts instead of line-by-line review?

STATUS: DRAFT (research only; recommendation is proposed and owner-gated)

Refs mctlhq/mctl-agents#974. Nothing in this report was implemented, run, timed, collected or
modelled. No model calls, no human study, no live history collection, no permission or gate change.
The issue owner records any verdict.

## 1. Sources, limits and NOT_OBSERVED

| item | status |
|---|---|
| https://thenewstack.io/ai-code-review-invariants/ | NOT_OBSERVED: not supplied, not fetched. No claim here depends on it. |
| root `AGENTS.md` / `CLAUDE.md` in this checkout | NOT_OBSERVED at repo root (find, depth 3). Only `agents/*/CLAUDE.md` agent prompts exist. The workspace-level AGENTS rules were supplied by the task text, not read from a file. |
| `CONTRIBUTING.md`, `LLMS.md` | read; no rule about spikes or review beyond the tier description |
| `docs/spikes/670-decision-context.md`, `docs/spikes/670/` (`run_explainer.sh`, runs) | read |
| `tests/test_spike_670_decision_context.py` | test names read, bodies not |
| `docs/spikes/918-finding-oracle-classes.md` | read |
| `docs/spikes/875-gh-aw-guardrail-comparison.md` | read |
| `docs/spikes/854-agent-observability-gaps.md` | header only (`STATUS: ADOPT_SUBSET`); content not read |
| ADR 010, ADR 025 | read in full |
| `orchestrator/approval_evidence.py`, `action_approvals.py`, `run_issue_investigator.py::post_proposal_comment`, `run_shepherd.py` gating constants | read at the cited ranges |
| `orchestrator/review_receipts.py` and its tests | consumers located by search; module body not read beyond ADR 025 |
| GitHub state of #670, #846, #847, #848, #670-related PRs, #974 comments | NOT_OBSERVED. `gh` was refused in this run, so open/closed/assigned state of the follow-ups is unknown. Statements from the task text (issue open, unassigned, `scoutonly`, no comments, 20 open PRs inspected without actual #974 work) are the orchestrator's and are not re-verified. |
| #848 issue body and status | NOT_OBSERVED: never read. Everything here about #848 is only what `docs/spikes/670-decision-context.md` says about it (one follow-up line). Whether #848 already has a safety or defect-detection design is unknown. |
| `docs/spikes/670/decision_context.py` | read in full in the follow-up pass (validator source only; tests still not read, nothing run) |
| #875 and #918 | note: #918 and #875 were read as source-local reports. #875 is about gh-aw guardrails and #918 about finding oracles; neither studies reviewer time (see section 3). |
| the deployed runtime | NOT_OBSERVED. Current source is not proof of what runs. |
| a "Notfound" workflow result | negative evidence only |

## 2. Question and bottom line

Question: can a human approver use decision-context artifacts (an explanatory summary bound to the change)
instead of reading every line, and what evidence would show reviewer time saved without worse defect
detection?

Bottom line: the artifact side was already researched in #670 (contract, one measured run, LIMITED_GO).
The reviewer-effort side was explicitly deferred there to #848 and is unmeasured. So the part of #974 that
asks "what evidence" is a measurement-design question that belongs to #848, not a new research track. This
report maps the overlap and drafts a minimal, NOT_RUN study design for #848 to adopt or reject. It does not
claim any time saving, and nothing here changes who must review or what gates merge.

## 3. Duplicate and ownership map

| prior work | what it answers for #974 | what it does not answer | relation |
|---|---|---|---|
| #670 `docs/spikes/670-decision-context.md` (LIMITED_GO, owner-accepted 2026-10-27 on one run, #702) | Contract `decision-context/v0` (digest/SHA binding, grounded claims, `freshness()` = fresh/stale/unknown, gate-waiver keys rejected). Quality of one artifact against the #709 merge: 3 of 4 pointers inside changed code, 0 contradictions in a spot check (not all 32 claims). | Reviewer time: stated `unobserved`. Detection compared with a full-diff reviewer: not run. n = 1. Low-risk arm: not run. | REUSE. Owns the artifact contract. |
| #846 (wire explainer to a real read-only SDK call, record tokens/cost) | Producer cost accounting for the artifact | Unknown state; `run_explainer.sh` still prints a placeholder and writes `unobserved` measurements in this checkout | Dependency for any artifact-production cost number |
| #847 (proposal-time explainer activity behind a flag) | Where the artifact would be produced in DevLoop | Unknown state; in the earlier bounded text search of `orchestrator/` no file referenced the explainer or `decision-context/v0` (hits were only the spike and its tests) | Source observation only: no callsite found by that search. This is not proof about #847's status or about any deployment |
| mctl-api#840 (artifact digest beside the `aar_` grant) | Correlation of artifact and decision | Not observed | Other repository |
| #848 (time the owner on 2-3 high-risk approvals with/without the artifact; decide full GO) | The reviewer-time measurement, named in #670 as the missing GO criterion | #670 states the #848 scope only as "time the owner on two or three high-risk approvals with and without the artifact, and decide on a full GO". #848's own body was not read, so whether it has an oracle, detection arm, n or safety criterion is unknown | Named in #670 as the owner of the measurement. #974 should feed #848, not duplicate it. |
| #918 oracle classes (STATUS DRAFT) | Which finding classes have a deterministic oracle; metric table lists "reviewer effort / time-to-decision: not-measured" | No human study | REUSE its oracle taxonomy for the truth oracle (section 7) |
| #875 gh-aw comparison (ADOPT_PATTERNS, S2/P3 only) | Guardrail patterns; relevant only as precedent that advisory/analysis output stays separated from write authority (S1) | Nothing about reviewer time or context artifacts | Incidental. No reuse beyond the boundary statement. |
| ADR 010 lifecycle ownership | "Ownership never implies authorization"; owner, policy, approval and merge policy are separate | Not a review-effort topic | Boundary source (section 5) |
| ADR 025 independent review receipts | Review as a sealed receipt bound to bytes/SHAs; `PASS` needs every criterion `met`; names the explainer (#670/#846/#847) a non-goal that "stays advisory"; nothing consumes the gate yet | A receipt is a worker/manual assertion about a spike or proposal, not a human reviewer-time record | Closest existing "review evidence" record; do not add a competing store |

Ownership summary: artifact contract = #670; production cost = #846; DevLoop wiring = #847; reviewer-time
measurement = #848; review receipts = ADR 025 / #905 lineage. No source in this checkout shows any work
that is specifically #974 (consistent with the task text; the open-PR inspection itself was not repeated
here). No new epic, store or runtime is needed.

## 4. Actual review, approval and context flow today (source-read)

1. Investigator writes a proposal triplet (`requirements.md`, `design.md`, `tasks.md`) and
   `post_proposal_comment` (`orchestrator/run_issue_investigator.py:1385`) posts a comment linking the gitops
   tree. The comment tells the approver to "Review `requirements.md`, `design.md` and `tasks.md`". It links no
   decision-context artifact. The comment is policy-checkpointed (`GITHUB_ISSUE_COMMENT`).
2. The approval grant is an mctl-api `aar_` action approval, `artifact_hash` = proposal content digest over
   `PROPOSAL_DIGEST_FILES = ("requirements.md", "design.md", "tasks.md", "proposed-content.md")`
   (`orchestrator/proposal_state.py:165`). `request_devloop_approval` binds it
   (`orchestrator/temporal/activities/delegated_approval.py:116-138`).
3. Before execution `verify_execution_grant` (`orchestrator/approval_evidence.py:117-167`) requires: grant
   exists, kind `mctl-agents-approve`, human decider (`github:`/`oidc:`), target equals service/slug,
   `artifact_hash` equals the current digest, state `consumed` (or `executed` with no new run). Uncertainty is
   `transient`, never authorization. So an explainer placed outside `PROPOSAL_DIGEST_FILES` cannot change what
   is approved.
4. The implementer opens a PR. Merge gating in the shepherd uses `GATING_BOTS = (claude[bot],
   chatgpt-codex-connector[bot])` and `VERDICT_STATES = {APPROVED, CHANGES_REQUESTED}`
   (`orchestrator/run_shepherd.py:157,163`); unaddressed P1/P2 from the bots that reviewed gate the merge.
   Merge authority is re-evaluated at the merge boundary (ADR 010 section 6) and does not read ownership.
5. A different existing "decision context": `action_approvals.DecisionContext`
   (`orchestrator/action_approvals.py:167-218`) carries `risk_level`, `side_effect`, `policy_decision`,
   `policy_reasons` for mctl-api approvals, and `ApprovalRecord.context_digest` is read, never computed
   (line 251). This is policy metadata shown to the approver, not the #670 explainer. The two share a name and
   must not be conflated in any study or doc.
6. Review receipts (ADR 025, `orchestrator/review_receipts.py`) are implemented as pure Tier A code and
   tested (`tests/test_review_receipts.py`, parity vectors). The ADR states nothing consumes the gate and that
   Tier B (mctl-api) was not observed to exist. A bounded search of `run_shepherd`/DevLoop found no consumer.
   Not finding a consumer is not proof that receipts are undeployed or unused elsewhere; deployment status is
   NOT_OBSERVED.

Limitations of the current flow relevant to #974:

- In the paths inspected (the files listed in section 1, plus the bounded search), no record of reviewer time,
  reviewer action (diff opened or not) or reviewer-found defects was found. #670 and #918 both say
  `unobserved`/`not-measured` for time-to-decision. Metrics elsewhere (other modules, mctl-api, deployed
  systems) were not searched and are unknown.
- In this checkout the explainer has no callsite found by the bounded search, and `run_explainer.sh` is a
  stub; #846/#847 state is unknown.
- The only measured artifact result is one run, produced by a subagent standing in for the operator path.
- Freshness is `unknown` whenever current values are missing; a stale artifact must be regenerated.

## 5. Authority boundary (applies to every option)

- Authoritative: the approved scope (proposal content bound by the grant digest), the human grant, merge
  requirements, owner/CODEOWNERS review and unfiltered bot findings. Explanatory: any decision-context
  artifact. The artifact is never permission: no field may skip, satisfy or waive a gate. The documented #670
  contract says `skip_review`, `auto_approve`, `satisfies_gate`, `waive_*` are rejected at any depth. The
  validator source (`docs/spikes/670/decision_context.py`, `_scan_waivers`) was read and recursively scans
  mapping keys through nested dicts and lists. It checks keys only, not text values, and tests were not read or
  run. The validator is a spike fixture; the source says nothing in `orchestrator` imports it.
- It does not replace approval, merge requirements, owner review, or bot findings, and bot findings stay
  unfiltered (nothing here suppresses or ranks them away from the reviewer).
- Humans may always open the full diff. No option removes that access or lowers mandatory review. A
  "context first, diff optional" arm in the study is an experimental presentation order, not a policy change.
- Ownership, policy, approval and merge policy stay separate (ADR 010).
- Digest scope: the artifact must stay outside `PROPOSAL_DIGEST_FILES`, otherwise approving the explanation
  becomes part of approving the change (#670 Q5). This is a design boundary, not enforcement that generated
  content is excluded or has provenance: the constant only names four filenames. Text placed inside any of
  those approved files would change the digest and be approved with it; nothing inspected checks who or what
  wrote that text. No further control is proposed here.

## 6. Options compared

| | REUSE_EXISTING | NARROW_MEASUREMENT_PROPOSAL | DEFER |
|---|---|---|---|
| What | Treat #670 (LIMITED_GO) + #848 as the full answer; point #974 at them | Hand #848 a minimal offline study design (section 7) and keep it NOT_RUN | Do nothing until #846/#847 land |
| Answers "can review use artifacts" | Partly: artifact quality (n = 1), no reviewer data | Defines how to answer it | No |
| Answers "what evidence shows time saved without lost detection" | No: #848 text only says "time the owner on 2-3 approvals", which cannot show detection non-inferiority | Yes (design only) | No |
| New surface | none | one doc; no runtime, store, epic | none |
| Risk | Claims a savings story that was never measured | Design may be too small to power; stated limits | Question stays open indefinitely |
| Cost | unmeasured; no new runtime | Doc only; no new runtime; a future study has human and model cost, unmeasured and not incurred here | unmeasured; no new runtime |

Recommendation (proposed, owner-gated): **NARROW_MEASUREMENT_PROPOSAL, delivered as input to #848**, with
REUSE_EXISTING for everything about the artifact itself. Rationale: #670 explicitly left reviewer effort
unmeasured, and the #848 scope as #670 describes it (time the owner on 2-3 approvals) names no defect oracle,
so a time saving could not be separated from missed defects. #848's own text was not read, so it may already
cover this; the owner should reconcile. DEFER is acceptable if the owner prefers to wait for #846, since artifact
production cost cannot be measured before it. The owner decides; nothing proceeds without that decision.

## 7. Smallest future study design (NOT_RUN)

Status: NOT_RUN. No data, timing, defect count or saving exists. Any number below is a design parameter, not
a result. Offline, synthetic or authorized-review only; no live history collection, no model calls in this
spike; a future run needs separate owner authorization for model use and for any participant.

### 7.0 Two different questions: proposal approval vs implementation PR review

These must not be merged. The #670 artifact is produced at proposal time, from the proposal triplet and the
repository at a pinned SHA, with no access to any implementation (#670 Measurements: the explainer subagent
had no git or network access and could not see the implementation). The approver it serves approves the
proposal (the `aar_` grant over `PROPOSAL_DIGEST_FILES`). A proposal-only artifact says nothing verified
about whether a later implementation is correct, so it must not be assumed to represent implementation
correctness, and a study of seeded implementation defects does not test it.

- **Primary study (consistent with #670/#848): proposal-approval review.** Units are frozen proposal
  triplets at pinned repo SHAs. Artifacts are generated with no implementation access, as in #670. The oracle
  is proposal-level: a pre-registered list of missing requirements, design errors and task gaps (for
  example, the #702 case where the proposal's stale-condition handling was a design error), plus known-clean
  proposals. Reviewers decide approve / request changes on the proposal. This is the question #848 is
  described as answering in #670 (timing the owner on high-risk approvals).
- **Role of reference and bot evidence in the primary study.** Any later implementation, merged diff, or bot
  or human review of it (e.g. the #709 diff or the Claude review on #709) may be used by oracle authors only
  as a reference source to establish what the true proposal-level defects were. It is never given to the
  artifact producer or to reviewers (non-leakage), and it is not itself the oracle: a defect counts only if an
  oracle author states it at proposal level before any reviewer sees the units. Bot findings are not part of
  proposal approval and are not shown in this study.
- **Secondary, separate experiment (labelled): implementation-PR review.** Units would be completed diffs
  with seeded or confirmed implementation defects. This needs an artifact that describes an implementation,
  which differs from the #670 contract (no implementation access). It therefore needs a later owner
  decision on the artifact contract (who generates it, from what inputs, bound to which digest) before it can
  be designed. It is not designed here, is NOT_RUN, and must not be pooled with or presented as the primary
  study. Existing bot findings stay unfiltered if it is ever run.

Gates and oracle non-leakage are unchanged in both: no gate, permission, mandatory review or merge rule is
touched, and the oracle is withheld from reviewers and artifact producers until scoring.

Hypotheses (primary study)
- H1 (safety, primary): context-plus-optional-full-proposal is non-inferior to the full-proposal baseline on
  proposal-level defects missed.
- H2 (effort, secondary, only if H1 holds): see Measurement of effort below. No H2 is claimed without H1.

Design (primary study)
- Units: frozen proposals (triplet at pinned SHAs), each with a pre-registered truth oracle written before
  any reviewer sees it: a list of seeded or historically confirmed proposal-level defects (missing
  requirements, design errors, task gaps) plus known-clean content, using the #918 taxonomy (deterministic
  classes first; classes with `NO_SAFE_ORACLE` are excluded from scoring and reported as excluded, never as
  passes). Oracle authors are not reviewers, and scorers are separate people from oracle authors and are
  blinded to arm.
- Allocation: 16 change sets (here, 16 proposals), each reviewed exactly once in each arm (baseline;
  context + optional full proposal). That is 16 x 2 arms = 32 reviews, distributed across 4 reviewers at 8
  reviews each. It is not 16 x 4 = 32 and not every reviewer sees every item. Counterbalance within each
  reviewer: each reviewer gets 4 items in the baseline arm and 4 different items in the context arm, with
  arm order and item order counterbalanced (Latin-square style). No reviewer sees the same item twice, in
  either arm, so the two reviews of an item are always by different reviewers. Learning and order effects are
  controlled by this counterbalancing and by a recorded review index per reviewer; they are not assumed away.
- Same inputs apart from presentation: same truth oracle, same time cap, same approve/changes-requested
  decision form. Full-proposal access is available in both arms; in the context arm, whether and when the
  reviewer opens it is recorded, not forbidden. The arm cannot be hidden from the reviewer, so scorers and
  oracle authors are the blinded parties.
- Controls (included in the 16, counted in the scoring): known-clean proposals (to measure false positives);
  a deliberately harmful-artifact control (artifact states something wrong or reassuring about a defective
  proposal); a stale-artifact control (artifact bound to an older digest/SHA, `freshness()` = `stale`); and
  an unknown-artifact control (`freshness()` = `unknown`). These test whether reviewers are misled, not
  whether a policy is allowed.
- Artifact arm provenance: artifacts generated once per unit from pinned inputs only (no implementation
  visible, as in #670), validated by the existing `decision-context/v0` validator (source read; its tests
  were not read) and `freshness()`; a `stale` or `unknown` artifact triggers regeneration or fallback to
  baseline, and the regeneration/fallback effort is recorded.
- Pilot size (explicit, not powered): 16 units (8 high-risk, 8 low-risk), 4 reviewers, 32 reviews, as above.
  This detects only gross differences. A pilot cannot establish safety equivalence or non-inferiority. The
  non-inferiority margin and the power design (what n would be required) are set by the owner before the
  run; if the margin cannot be met at this n, the result is `INCONCLUSIVE`, never "no difference". Pilot
  output is at most feasibility information and a basis for the owner's power design.
- Outcomes per review: seeded/confirmed defects found and missed (per defect class), false positives raised
  against known-clean content, decision versus oracle, whether the full proposal was opened, and which parts
  were read.
- Measurement of effort. Three quantities are kept separate and not added together:
  1. Total human reviewer effort: reviewer time on the unit, including reading the artifact, verifying it,
     reading the proposal, deciding, and any fallback to the full proposal. For the baseline arm the same
     reviewer time is recorded, including any common work (e.g. both arms read the proposal), so common work
     is counted in both arms rather than only in one.
  2. Artifact machine latency (generation wall-clock) and token/cost per unit (#846 prerequisite). These are
     not reviewer time and are not summed into it. An end-to-end turnaround (proposal ready to decision) is
     a third, separate endpoint with its own timestamps, and is reported separately if the owner wants it.
  3. Fallback and regeneration work, reported as its own line.
  Wall-clock distributions (median, p90, max, individual values), not just means. Timestamps come only from
  the study harness; nothing is back-filled from history. Cost for docs-only, reuse or no-runtime options is
  `unmeasured`, not zero.
- Missing vs unknown: a field not captured is `unobserved`; a field that could not be determined is
  `unknown`; neither is imputed as zero or a pass. Reviews with missing outcome data are reported separately,
  not dropped silently.
- Safety non-inferiority: report defects missed per arm with an exact or bootstrap interval; any seeded
  high-severity defect missed only in the context arm is reported individually, acts as a veto on claiming
  benefit, and blocks a favourable conclusion regardless of mean time or effort.
- Stop rules: abort the study if a reviewer approves a unit the oracle marks defective under the context
  arm more often than baseline by the owner-set margin; no gate or permission is touched by any outcome.
- No production claims: nothing from this study, pilot or not, supports a claim about production behaviour,
  and mandatory review is not lowered by any outcome.
- Outputs allowed: one results file under the same spike path in a later task. No new store; correlation to
  an approval, if ever wanted, goes through the mctl-api#840 route and the ADR 018 evidence/ADR 025 receipt
  vocabulary rather than a new record.

Claims prohibited until measured: any saving percentage, any "equal detection" claim, any cost figure, any
inference of model or vendor identity from artifact text.

## 8. Risks and failure modes carried over from #670

polished-but-wrong artifact; stale context; rubber-stamping (reviewer trusts the explainer and skips the
diff); digest-scope confusion; and, for the study itself, learning effects across arms, oracle leakage to
reviewers, and artifacts generated with knowledge of the implementation. A reviewer who skips the diff in the
context arm is a measured outcome, not a policy permission.

## 9. Security observations (no exploit, no fix)

- Naming collision: `DecisionContext`/`context_digest` in `action_approvals.py` (policy metadata) vs the
  #670 `decision-context/v0` explainer. A future change that treats one as the other could present
  explanatory text as policy evidence. Observation only.
- The artifact is generated text shown to an approver. The digest-scope rule (artifact outside
  `PROPOSAL_DIGEST_FILES`) is a design boundary. What was inspected: the constant at `proposal_state.py:165`
  lists four filenames, and `verify_execution_grant` compares the grant `artifact_hash` to the current digest.
  Neither checks provenance or whether text in an approved file was generated. Content written into an
  approved file changes the digest and is approved with it. No test asserting the explainer file is excluded
  was located (bounded search). This is an observation, not a fix or a policy proposal.
- Not observed: deployed mctl-api behaviour, Tier B receipts, any live approval data.

## 10. Owner decisions needed

1. Choose REUSE_EXISTING, NARROW_MEASUREMENT_PROPOSAL (recommended) or DEFER.
2. If narrow: confirm #848 owns the study and accept or amend the section 7 design, including the
   non-inferiority margin, the power design, and whether 16 change sets (32 reviews, 8 per reviewer across 4
   reviewers) is an acceptable feasibility size. Confirm the primary study is proposal-approval review, and
   decide separately whether an implementation-PR review experiment (needing its own artifact contract) is
   wanted at all.
3. State of #846/#847/#848 and mctl-api#840 (not observable here) and whether #846 must land first.
4. Authorization for any future model calls or human participants (not given for this spike).
5. Whether to cross-reference this report from #670/#848 (not done here; sole allowed file is this one).
