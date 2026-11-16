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
| #847 (proposal-time explainer activity behind a flag) | Where the artifact would be produced in DevLoop | Unknown state; no callsite in `orchestrator/` references the explainer or `decision-context/v0` (search found only the spike and tests) | Not implemented in source |
| mctl-api#840 (artifact digest beside the `aar_` grant) | Correlation of artifact and decision | Not observed | Other repository |
| #848 (time the owner on 2-3 high-risk approvals with/without the artifact; decide full GO) | The reviewer-time measurement, named in #670 as the missing GO criterion | Design of the measurement, truth oracle, defect-detection arm, n, safety criterion are not stated in #670 beyond "time the owner" | **Owner of measurement.** #974 should feed #848, not duplicate it. |
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
   Tier B (mctl-api) was not observed to exist. Search found no consumer in `run_shepherd`/DevLoop. Treat as
   not deployed.

Limitations of the current flow relevant to #974:

- No record of reviewer time, reviewer action (diff opened or not) or reviewer-found defects exists in
  source. Time-to-decision is not captured anywhere (#670 and #918 both say `unobserved`/`not-measured`).
- The explainer is not wired: no callsite, `run_explainer.sh` is a stub, #846/#847 state unknown.
- The only measured artifact result is one run, produced by a subagent standing in for the operator path.
- Freshness is `unknown` whenever current values are missing; a stale artifact must be regenerated.

## 5. Authority boundary (applies to every option)

- Authoritative: the approved scope (proposal content bound by the grant digest), the human grant, merge
  requirements, owner/CODEOWNERS review and unfiltered bot findings. Explanatory: any decision-context
  artifact. The artifact is never permission: no field may skip, satisfy or waive a gate. `decision-context/v0`
  already rejects `skip_review`, `auto_approve`, `satisfies_gate`, `waive_*` at any depth.
- It does not replace approval, merge requirements, owner review, or bot findings, and bot findings stay
  unfiltered (nothing here suppresses or ranks them away from the reviewer).
- Humans may always open the full diff. No option removes that access or lowers mandatory review. A
  "context first, diff optional" arm in the study is an experimental presentation order, not a policy change.
- Ownership, policy, approval and merge policy stay separate (ADR 010).
- Digest scope: the artifact must stay outside `PROPOSAL_DIGEST_FILES`, otherwise approving the explanation
  becomes part of approving the change (#670 Q5).

## 6. Options compared

| | REUSE_EXISTING | NARROW_MEASUREMENT_PROPOSAL | DEFER |
|---|---|---|---|
| What | Treat #670 (LIMITED_GO) + #848 as the full answer; point #974 at them | Hand #848 a minimal offline study design (section 7) and keep it NOT_RUN | Do nothing until #846/#847 land |
| Answers "can review use artifacts" | Partly: artifact quality (n = 1), no reviewer data | Defines how to answer it | No |
| Answers "what evidence shows time saved without lost detection" | No: #848 text only says "time the owner on 2-3 approvals", which cannot show detection non-inferiority | Yes (design only) | No |
| New surface | none | one doc; no runtime, store, epic | none |
| Risk | Claims a savings story that was never measured | Design may be too small to power; stated limits | Question stays open indefinitely; #848 could run with an unsound design |
| Cost | 0 | Doc only; a future study has human and model cost, not incurred here | 0 |

Recommendation (proposed, owner-gated): **NARROW_MEASUREMENT_PROPOSAL, delivered as input to #848**, with
REUSE_EXISTING for everything about the artifact itself. Rationale: #670 explicitly left reviewer effort
unmeasured, and the 2-3 approval timing in #848 as written has no defect oracle, so a time saving could not
be separated from missed defects. DEFER is acceptable if the owner prefers to wait for #846, since artifact
production cost cannot be measured before it. The owner decides; nothing proceeds without that decision.

## 7. Smallest future study design (NOT_RUN)

Status: NOT_RUN. No data, timing, defect count or saving exists. Any number below is a design parameter, not
a result. Offline, synthetic or authorized-review only; no live history collection, no model calls in this
spike; a future run needs separate owner authorization for model use and for any participant.

Hypotheses
- H1 (safety, primary): context-plus-optional-diff is non-inferior to the full-diff baseline on defects
  missed.
- H2 (effort, secondary, only if H1 holds): total reviewer decision time, including artifact production,
  verification and fallback, is lower.

Design
- Units: frozen change sets (diff + proposal triplet at pinned SHAs), each with a pre-registered truth
  oracle written before any reviewer sees it: a list of seeded or historically confirmed defects plus a list
  of known-clean hunks, using the #918 taxonomy (deterministic classes first; classes with `NO_SAFE_ORACLE`
  are excluded from scoring and reported as excluded, never as passes). Oracle authors are not reviewers.
- Pairing: every change set is reviewed in both arms (full-diff baseline; context + optional diff) by
  different reviewers, or by the same reviewer after a washout on a different change set; assignment is
  blinded where possible (arm cannot be hidden from the reviewer, so the scorer and oracle are blinded) and
  counterbalanced (Latin square over arm order and reviewer).
- Same inputs apart from presentation: same truth oracle, same time cap, same bot findings shown unfiltered
  in both arms, same approve/changes-requested decision form. Diff access is available in both arms; in the
  context arm, whether and when the reviewer opens the diff is recorded, not forbidden.
- Artifact arm provenance: artifacts generated once per change set from pinned inputs only (no
  implementation visible, as in #670), validated by the existing `decision-context/v0` validator and
  `freshness()`; a `stale` or `unknown` artifact triggers regeneration or fallback to baseline and counts
  its cost.
- Minimum size for a feasibility pilot (explicit, not powered): 16 change sets (8 high-risk, 8 low-risk),
  4 reviewers, each change set reviewed once per arm, so 32 reviews in total. This detects only gross
  differences. The non-inferiority margin and any claim threshold are owner-set before the run; if the margin
  cannot be met at this n, the result is `INCONCLUSIVE`, never "no difference".
- Outcomes per review: seeded/confirmed defects found and missed (per defect class), false positives raised
  against known-clean hunks, decision (approve / request changes) versus oracle, whether the diff was opened,
  and which hunks were read.
- Timing: wall-clock distributions (median, p90, max, individual values), not just means, split into:
  artifact production, artifact verification by the reviewer, reading, decision, and fallback to full diff.
  Record artifact production time and token/cost per change set (#846 prerequisite). Timestamps come only
  from the study harness; nothing is back-filled from history.
- Missing vs unknown: a field not captured is `unobserved`; a field that could not be determined is
  `unknown`; neither is imputed as zero or a pass. Reviews with missing outcome data are reported separately,
  not dropped silently.
- Safety non-inferiority: report defects missed per arm with an exact or bootstrap interval; any seeded
  high-severity defect missed only in the context arm is reported individually and blocks a favourable
  conclusion regardless of mean time.
- Stop rules: abort the study if a reviewer approves a change set the oracle marks defective under the context
  arm more often than baseline by the owner-set margin; no gate or permission is touched by any outcome.
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
- The artifact is generated text shown to an approver; the digest-scope rule (outside
  `PROPOSAL_DIGEST_FILES`) is the control that keeps persuasive text from being approved as content. This is
  enforced by the constant at `proposal_state.py:165`; no test asserting the explainer file is excluded was
  located.
- Not observed: deployed mctl-api behaviour, Tier B receipts, any live approval data.

## 10. Owner decisions needed

1. Choose REUSE_EXISTING, NARROW_MEASUREMENT_PROPOSAL (recommended) or DEFER.
2. If narrow: confirm #848 owns the study and accept or amend the section 7 design, including the
   non-inferiority margin and whether 16 change sets x 4 reviewers is an acceptable feasibility size.
3. State of #846/#847/#848 and mctl-api#840 (not observable here) and whether #846 must land first.
4. Authorization for any future model calls or human participants (not given for this spike).
5. Whether to cross-reference this report from #670/#848 (not done here; sole allowed file is this one).
