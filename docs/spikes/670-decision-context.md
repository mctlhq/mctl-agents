# Spike #670: decision-context explainer before human approval

STATUS: DRAFT -- one measured run on #702 backs this report (see
Measurements). The recommended verdict is LIMITED_GO; the issue owner decides,
and this line becomes the token when they do. Refs mctlhq/mctl-agents#670.

Allowed decision tokens, one of which replaces this marker when finalized:
`GO`, `LIMITED_GO`, `NO_GO`. The issue owner records the verdict.

## Current-state map

- `orchestrator/run_issue_investigator.py` `post_proposal_comment`: the issue
  comment the approver reads today.
- `orchestrator/proposal_state.py`: `PROPOSAL_DIGEST_FILES`, `content_digest_of`.
- `orchestrator/temporal/activities/delegated_approval.py`
  `request_devloop_approval`: the `aar_` grant bound to the content digest.
- `orchestrator/approval_evidence.py`: requires grant `artifact_hash` equal to
  the current digest.
- `orchestrator/temporal/workflows/dev_loop.py`: investigate, approval park,
  implement, shepherd.
- References an artifact may cite: ADR 018 ExecutionEvidence, ADR 009 context
  snapshots, ADR 013 human input.

## Contract `decision-context/v0`

Markdown with a first fenced yaml block; implemented in
`docs/spikes/670/decision_context.py`. Blocks: `subject`, `binding`
(proposal digest from `content_digest_of`, target repo and 40-hex SHA,
optional context snapshot hash, generator), `risk` (advisory), `sections`
(`what_changes`, `why`, `how_it_works_today`, `behaviour_after`, `invariants`,
`interfaces`, `security_governance`, `failure_modes`, `evidence`,
`review_pointers`, `not_changing`). A claim is `explanatory` or `grounded`.
Refs: `repo:<path>@<sha>#L<a>-L<b>`, `proposal:<file>`, `evidence:<id>`.

- `invariants`, `security_governance`, `failure_modes`, `evidence`: every claim
  must be grounded with a well-formed ref.
- `repo:` ref SHA must equal `binding.target_repo_sha`.
- `review_pointers` need a `repo:` ref and a `why`.
- `risk.level: high` requires failure modes `retry`, `timeout_partial`,
  `wrong_identity`, `state_after_approval`.
- A `subject`, `binding`, `risk` or `sections` block that is present but not
  a mapping is rejected (`BAD_BLOCK`), never skipped.
- Unknown keys are rejected; `skip_review`, `auto_approve`, `satisfies_gate`
  and `waive_*` are rejected at any depth (the explainer is not authority).
- `freshness(artifact, current_digest, current_sha)` returns `fresh`, `stale`
  or `unknown`; a missing current value is `unknown`, never `fresh`.

## Hypotheses (to be confirmed or refuted by the run)

- Q1 when: after the proposal, before the approval park.
- Q2 who: a dedicated read-only pass, not the investigator.
- Q3 inputs: repo at pinned SHA, proposal digest, evidence ids.
- Q4 binding: digest and SHA with `freshness()`.
- Q5 correlation: record the artifact digest beside the proposal digest in the
  grant (follow-up). It must not enter `PROPOSAL_DIGEST_FILES`, or approving
  the explanation would become part of approving the change.
- Q9 accuracy: grounded-claim enforcement plus a retrospective contradiction
  count.
- Q6, Q7, Q8, Q10: unobserved until the run.

## Risk-based review depth (advisory only)

High risk: read the pointers at line level and the high-scrutiny sections.
Low risk: read the summary sections. The level never skips or satisfies a gate.

## Failure modes

polished-but-wrong (fluent, contradicted by the merged change), stale context
(proposal or repo moved), rubber-stamping (approver trusts the explainer),
digest-scope confusion (artifact mistaken for approved content).

## Comparison with the current flow

Current: proposal triplet plus issue comment, bound to a content digest.
Experimental: the same plus a grounded artifact. Reviewer time: unobserved.

## Measurements

One run, 2026-10-27, on the high-risk subject. The artifact is
`docs/spikes/670/runs/702-decision-context.md`; raw values are in
`docs/spikes/670/runs/702-measurements.json`. The explainer was a read-only
Claude Code subagent given only the #702 proposal triplet and the repository
at `a42c3e7` (the first parent of the #709 merge). It had no git or network
access, so it could not see the implementation.

| subject | latency | tokens (in+out) | cost | bytes | files inspected | reviewer time current | reviewer time experimental | risks surfaced | pointers matched PR | post-merge accuracy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| #702 (high) | 108 s | 122,934 (split unobserved) | unobserved (subscription) | 13,347 | 7 | unobserved | unobserved | 2 gaps in the proposal, 4 required failure modes | 3 of 4 inside changed code; 4 of 4 relevant | 0 contradictions found in the pointer and behaviour claims checked |
| low-risk merged | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |

Validator: 0 violations with a resolver that checks every `repo:` path and
line range at `a42c3e7`. `freshness()` reports `fresh` at `a42c3e7` and
`stale` at the #709 merge `56b9224`.

Ground truth is the #709 diff. It changed `DevLoopResult` and the ended
prefixes, `_delegated_approve` (old L2271-L2299), the `run()` exit (old
L3380), the canonical approve branch in `_run_loop` (old L3921-L3969),
`start.restart_if_abandoned` (L101-L145) and `cli.start`.

Pointers against that diff:

- Canonical approve branch, L3922-L3969: changed. Hit.
- `_ensure_fresh_grant` exits, L2234-L2260: not edited, but the explainer
  said the tasks did not cover these exits. The implementer's first commit
  (`776050f`) routes them through `_approval_ended` at the call site, so the
  gap was real.
- Restart predicate, `start.py` L127-L143: changed. Hit.
- `_delegated_approve` classification, L2271-L2298: changed. The explainer
  flagged the "proposal changed since requested" refusal as a stale
  condition that the design ends terminally. Claude review raised the same
  defect as a P2 on #709's first head (`776050f`), and the fix-up
  (`e1863ce`) added the `stale-slug` code. The explainer found it from the
  proposal alone, before any code existed.

Polished-but-wrong instances: none found in the four pointers and the
behaviour claims checked against the merged code. This was a spot check, not
an exhaustive audit of all 32 claims.

Limits: one subject (n = 1), no low-risk arm, no human reviewer timing, and
no token split or dollar cost. `run_explainer.sh` is still a stub, so the
operator path was not exercised; a subagent stood in for it.

## GO criteria checklist

- [x] Pointers matched the merged change with acceptable recall/precision (3 of 4 inside changed code, 4 of 4 relevant, n = 1)
- [x] No contradicted high-scrutiny claim (spot check)
- [ ] Reviewer time to decision not worse than the current path (unobserved)
- [x] Cost within the investigator envelope (108 s, about 123k tokens; an investigate is budgeted at $3.00)

On GO or LIMITED_GO, follow-ups: proposal-time explainer activity, grant
correlation in mctl-api, post-implementation drift check, on-demand slice
explainer.

## Recommendation: LIMITED_GO

The artifact did more than summarise. From the proposal alone it found a
defect that review only caught after the code was written, it named a gap in
the tasks, and it pointed at the code that later changed. It stayed bound to
the state it describes, and it touches no gate. Two things are missing for a
full GO: the reviewer-effort measurement, which needs a human, and more than
one subject.

LIMITED_GO means:

- An opt-in, proposal-time explainer for `risk: high` proposals only.
- It is advisory. It sits next to the proposal and stays outside
  `PROPOSAL_DIGEST_FILES`.
- Its digest is recorded beside the proposal digest in the approval grant.
- It is regenerated, never reused, when `freshness()` is not `fresh`.

Follow-ups:

1. Wire `run_explainer.sh` to a real read-only Agent SDK call that records the
   token split and cost.
2. Add a proposal-time explainer activity in DevLoop, behind a flag, for
   high-risk proposals.
3. Correlate the artifact digest with the `aar_` grant in mctl-api.
4. Time the owner on two or three high-risk approvals with and without the
   artifact, and decide on a full GO from that data.
