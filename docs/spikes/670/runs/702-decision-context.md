# Decision context: mctl-agents#702, a failed approval must not end as success

```yaml
schema: decision-context/v0
subject: {service: mctl-agents, slug: issue-702-fix-devloop-a-failed-approval-must-not-p, issue: "mctlhq/mctl-agents#702"}
binding:
  proposal_content_digest: sha256:0a0dd447ecc3512cb2cee720e460ec66d56e53c0c9ee817cd1ace86415a63d8c
  target_repo: mctlhq/mctl-agents
  target_repo_sha: a42c3e7c6160671cb8b4a052ab57092d8901ab7e
  generator: {model: claude, mode: subagent-read-only}
risk:
  level: high
  areas: [approval-semantics, workflow-determinism, restart-policy, workitem-execution-phase]
sections:
  what_changes:
    - text: "DevLoopResult gains a defaulted `outcome` field (approval_refused | approval_failed | empty). Two new ended prefixes, 'approval refused: ' and 'approval failed: ', join ABANDONED_ENDED_PREFIX as restart markers."
      kind: explanatory
      refs: ["proposal:design.md", "proposal:tasks.md"]
    - text: "The two failure returns of the canonical approve branch in _run_loop are replaced. They set the we_ end phase to Failed and classify the outcome. A non-Succeeded CWFT gets one post-failure freshness check, which can lead to a re-grant on the same we_. All of this sits behind a new patch, approval-failure-outcome."
      kind: explanatory
      refs: ["proposal:design.md", "proposal:tasks.md"]
    - text: "start.restart_if_abandoned is renamed restart_if_restartable, with the old name kept as an alias. It restarts a COMPLETED run whose implement is None and whose ended starts with any of the three prefixes."
      kind: explanatory
      refs: ["proposal:tasks.md"]
  why:
    - text: "Today a refused or failed flip returns a normal DevLoopResult. run() then ends the we_ with the investigator's phase, which is Succeeded. Restart refuses the loop because its ended text has no abandon prefix. The proposal cites the production incident mctlhq/.github#182."
      kind: explanatory
      refs: ["proposal:requirements.md", "repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L3377-L3383"]
  how_it_works_today:
    - text: "After investigate, the we_ end phase is set to the investigator's result."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L3536-L3538"]
    - text: "In the canonical branch, _ensure_fresh_grant runs first, then _delegated_approve. A None result returns ended=refused. A non-Succeeded result returns ended='approve flip ended <phase>'. Neither return touches the end phase. Only the success path ends the we_ explicitly."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L3922-L3969"]
    - text: "_delegated_approve returns one of three kinds of reason. 'approve flip refused: ...' covers a changed slug, a missing grant, or a typed 4xx refusal with an optional code. 'approve flip unavailable: ...' covers an ActivityError."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2271-L2298"]
    - text: "restart_if_abandoned restarts only a COMPLETED run with implement None and ended starting with 'abandoned: '. The CLI is its only caller."
      kind: grounded
      refs: ["repo:orchestrator/temporal/start.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L101-L143", "repo:orchestrator/temporal/cli.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L30-L44"]
  behaviour_after:
    - text: "A failed or refused canonical flip ends the own we_ Failed and returns a typed outcome. A provably stale grant (positive digest mismatch, or a stale refusal code) is superseded, and a fresh aar_ is asked on the same we_. That loop is bounded by MAX_GRANT_SUPERSEDES and CANONICAL_APPROVAL_WAIT. A successful flip is unchanged."
      kind: explanatory
      refs: ["proposal:requirements.md", "proposal:design.md"]
    - text: "The proposal does not show that the earlier pre-flip exits of _ensure_fresh_grant ('approval superseded too many times' and 'approval freshness unreadable') get an outcome or a Failed we_. Task 4 only names the two returns after the flip. Under the current run() exit, those exits would still end the we_ with the investigator's Succeeded phase."
      kind: explanatory
      refs: ["proposal:tasks.md", "repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2234-L2260"]
  invariants:
    - text: "The we_ end phase is chosen at run() exit as pending, then end phase, then Failed. Setting _canonical_end_phase='Failed' before returning is therefore enough to end the we_ Failed on a normal return. The exception path already forces Failed."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L3363-L3385"]
    - text: "The canonical execution store accepts only Succeeded, Failed and Error as terminal phases."
      kind: grounded
      refs: ["repo:orchestrator/temporal/activities/canonical_execution.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L46-L46"]
    - text: "_supersede_grant_if_stale returns 'fresh' without any activity when no ref or digest is bound. An unreadable digest is never treated as a match. A supersede clears the grant, bumps the attempt and supersede count, and returns 'capped' past MAX_GRANT_SUPERSEDES (8)."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2172-L2218", "repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L335-L335"]
    - text: "The new restart markers must match mctl-api byte for byte, as ABANDONED_ENDED_PREFIX already does."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L1091-L1095"]
    - text: "DevLoopResult fields are defaulted so that old results still deserialize. The new outcome field must follow the same rule."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L1098-L1131"]
  interfaces:
    - text: "DevLoopResult.outcome and the two ended prefixes are a contract with mctl-api, through restartIfAbandoned and the dev-loop GET. That side is out of scope. Until it ships, mctl-api still answers already_exists for these runs, and only cli.py can restart them."
      kind: explanatory
      refs: ["proposal:requirements.md", "proposal:design.md"]
    - text: "DelegatedApproveResult.code is free-form: it holds mctl-api's typed code when one was sent. STALE_GRANT_REFUSAL_CODES (codes containing 'digest' or 'drift') is an assumption that the proposal itself leaves open."
      kind: grounded
      refs: ["repo:orchestrator/temporal/activities/delegated_approval.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L226-L233", "proposal:requirements.md"]
  security_governance:
    - text: "A stale or superseded grant is never presented again. Its ref is moved into _superseded_refs and the approval state is cleared, so a re-grant always needs a new human decision on a new aar_."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2204-L2215"]
    - text: "Before the flip, the re-grant park asks on the loop's own we_ (execution_id empty unless after_flip). The proposal keeps after_flip false, so the grant stays bound to the we_ the run token names."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2389-L2431", "repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2279-L2286"]
    - text: "Widening restart to approval outcomes keeps it off the intake poller: the poller's reuse policy treats COMPLETED as handled, and the restart is called only from the CLI. A restart re-investigates and asks a new grant, so it adds no approval."
      kind: grounded
      refs: ["repo:orchestrator/temporal/start.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L30-L47", "repo:orchestrator/temporal/start.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L122-L126"]
  failure_modes:
    - mode: retry
      text: "The delegated approve runs at most 3 attempts. After submit, the CWFT name is heartbeated, so a retry resumes polling and never spends the grant twice. Exhausted retries become 'approve flip unavailable', which the proposal maps to approval_failed with no re-grant."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L307-L307", "repo:orchestrator/temporal/activities/delegated_approval.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L263-L281"]
    - mode: timeout_partial
      text: "An ActivityError raised after submit means the grant is already spent and the CWFT's outcome is unknown. The proposal classifies it as approval_failed and ends the we_ Failed, although the CWFT may have succeeded. Separately, a 5xx or retryable code counts as uncertainty, not as a refusal."
      kind: grounded
      refs: ["repo:orchestrator/temporal/activities/delegated_approval.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L249-L259", "repo:orchestrator/temporal/activities/delegated_approval.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L276-L278", "repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2292-L2293"]
    - mode: wrong_identity
      text: "The flip names _canonical_execution_id() and _approval_ref. A transition that lands during the post-failure freshness read is caught by the attempt counter, which reports 'superseded' rather than reusing the old grant."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2181-L2194", "repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2300-L2304"]
    - mode: state_after_approval
      text: "The run can now end Failed (we_) and restartable, while gitops may already hold the accepted proposal, for example when only the poll failed. A CLI restart then re-investigates and flips again on the reused proposal directory. Whether a second approve CWFT on an already-accepted proposal is idempotent is not visible in this repo."
      kind: grounded
      refs: ["repo:orchestrator/temporal/start.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L81-L87", "proposal:design.md"]
  evidence:
    - text: "There is a precedent test for ending the own we_ Failed instead of the investigator's Succeeded. T1-T3 follow its advance_log pattern."
      kind: grounded
      refs: ["evidence:test_a_failed_continuation_ends_the_canonical_execution_failed"]
    - text: "Existing tests cover a failed flip never implementing, the supersede cap, and restart refusing every non-abandon ending (including 'approval wait expired')."
      kind: grounded
      refs: ["evidence:test_failed_approve_flip_never_implements", "evidence:test_t6_too_many_supersedes_end_the_loop", "evidence:test_restart_if_abandoned_refuses_everything_else", "evidence:test_restart_if_abandoned_restarts_a_run_abandoned_before_implement"]
  review_pointers:
    - text: "The canonical approve branch and its two failure returns are where the patch gate and the re-grant loop land."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L3922-L3969"]
      why: "Replay determinism. The new patch must be evaluated once and placed so that unpatched histories issue identical commands. A gate that also covers the non-canonical _run_cwft branch (task 6) must not reorder commands."
    - text: "The exits of _ensure_fresh_grant."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2234-L2260"]
      why: "These pre-flip approval endings are not named in the tasks. Check whether they also get outcome and a Failed we_, or still record Succeeded."
    - text: "The restart predicate."
      kind: grounded
      refs: ["repo:orchestrator/temporal/start.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L127-L143"]
      why: "The implement-is-None guard and the pinned run_id read must survive the rename. The legacy 'approve flip ended' text must stay non-restartable (T7)."
    - text: "_delegated_approve classification."
      kind: grounded
      refs: ["repo:orchestrator/temporal/workflows/dev_loop.py@a42c3e7c6160671cb8b4a052ab57092d8901ab7e#L2271-L2298"]
      why: "The 'proposal changed since requested' refusal is a stale condition, yet the design maps it to terminal approval_refused. The guessed stale codes decide whether a refusal re-grants or ends the loop."
  not_changing:
    - text: "Not changed: the approve CWFT, the #681 digest check, mctl-api, the intake poller's reuse policy, abandon handling, and the already-closed run for .github#182."
      kind: explanatory
      refs: ["proposal:requirements.md"]
    - text: "The proposal could not confirm whether deployed worker image 1.84.0 already contains #701 (grant freshness), which the design reuses. I did not check this either."
      kind: explanatory
      refs: ["proposal:requirements.md"]
```
