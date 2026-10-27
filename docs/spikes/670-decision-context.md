# Spike #670: decision-context explainer before human approval

STATUS: DRAFT -- no operator measurement run backs this report yet, so it
carries no decision token. The contract, validator, fixtures and guards are
delivered; measurements come from an operator run. Refs mctlhq/mctl-agents#670.

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

| subject | latency | in tokens | out tokens | cost | bytes | files inspected | reviewer time current | reviewer time experimental | risks surfaced | pointers matched PR | post-merge accuracy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| #702 (high) | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |
| low-risk merged | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |

Polished-but-wrong instances: unobserved. The shipped #702 example is
hand-authored and bound to the base SHA at spike time, not the
pre-implementation commit of #702 (`a42c3e7c6160671cb8b4a052ab57092d8901ab7e`,
the first parent of the #709 merge), so it is a contract demonstration, not a
measurement. The operator run generates the #702 artifact at that commit.

## GO criteria checklist

- [ ] Pointers matched the merged change with acceptable recall/precision
- [ ] No contradicted high-scrutiny claim
- [ ] Reviewer time to decision not worse than the current path
- [ ] Cost within the investigator envelope

On GO or LIMITED_GO, follow-ups: proposal-time explainer activity, grant
correlation in mctl-api, post-implementation drift check, on-demand slice
explainer.
