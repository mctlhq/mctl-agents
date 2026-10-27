# Decision context: mctl-agents#702 (HAND-AUTHORED example, not model output)

Illustrates `decision-context/v0` for a high-risk approval-semantics proposal.
`target_repo_sha` is the base-branch commit at spike time, NOT the #702 PR
merge-base (that value is `unobserved`; the operator run replaces it). Line
ranges were read at that SHA.

```yaml
schema: decision-context/v0
subject:
  service: mctl-agents
  slug: issue-702-fix-devloop-a-failed-approval-must-not-p
  issue: mctlhq/mctl-agents#702
binding:
  proposal_content_digest: sha256:0a0dd447ecc3512cb2cee720e460ec66d56e53c0c9ee817cd1ace86415a63d8c
  target_repo: mctlhq/mctl-agents
  target_repo_sha: 7afb04b561358bbf46e6a666d011d130a0b75aab
  generator: hand-authored
risk:
  level: high
  areas: [approval-semantics, idempotency, durable-audit]
sections:
  what_changes:
    - text: A failed or refused approve flip must end the loop's own WorkItem execution Failed, not Succeeded.
      kind: explanatory
      refs: ["proposal:requirements.md"]
  why:
    - text: A stale grant was refused, yet the WorkItem read Succeeded and the loop could not be restarted on its own id.
      kind: explanatory
      refs: ["proposal:requirements.md"]
  how_it_works_today:
    - text: The exit path ends the execution with the pending phase, else the end phase, else Failed; the end phase holds the investigator's phase.
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@7afb04b561358bbf46e6a666d011d130a0b75aab#L3464-L3464"]
  behaviour_after:
    - text: Every pre-flip exit of the canonical approve branch ends the execution Failed and carries a typed outcome.
      kind: explanatory
      refs: ["proposal:design.md"]
  invariants:
    - text: A grant authorizes execution only for the exact content digest it was issued for.
      kind: grounded
      refs: ["repo:orchestrator/approval_evidence.py@7afb04b561358bbf46e6a666d011d130a0b75aab#L117-L135"]
  interfaces:
    - text: The loop result gains a typed terminal outcome (approval_refused or approval_failed).
      kind: explanatory
      refs: ["proposal:design.md"]
  security_governance:
    - text: Uncertainty about a grant is never treated as authorization; the change must not relax any refusal.
      kind: grounded
      refs: ["repo:orchestrator/approval_evidence.py@7afb04b561358bbf46e6a666d011d130a0b75aab#L109-L115"]
  failure_modes:
    - mode: retry
      text: A refused loop must be restartable on the same workflow id without opening a new issue.
      kind: grounded
      refs: ["proposal:requirements.md"]
    - mode: timeout_partial
      text: An unavailable delegated approve (infrastructure) must be distinguishable from a refusal.
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@7afb04b561358bbf46e6a666d011d130a0b75aab#L2349-L2352"]
    - mode: wrong_identity
      text: Not applicable to this change; the decider identity check lives in approval_evidence and is untouched.
      kind: grounded
      not_applicable: true
      refs: ["repo:orchestrator/approval_evidence.py@7afb04b561358bbf46e6a666d011d130a0b75aab#L117-L135"]
  evidence:
    - text: The production incident is recorded in the proposal context.
      kind: grounded
      refs: ["proposal:requirements.md"]
  review_pointers:
    - text: Exit-phase selection for the canonical execution.
      why: The false Succeeded originates here.
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@7afb04b561358bbf46e6a666d011d130a0b75aab#L3454-L3464"]
  not_changing:
    - text: PROPOSAL_DIGEST_FILES and the approval grant verification.
      kind: explanatory
      refs: ["proposal:design.md"]
```
