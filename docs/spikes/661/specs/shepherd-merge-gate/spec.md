# shepherd-merge-gate Specification

## Purpose
When the PR shepherd may merge an implementer-opened pull request, when it must wait, remediate or hand the merge to someone else, and what binds a merge to the reviewed code. The review-fix loop itself (attempt caps, follow-up commits) is a separate capability. Each requirement names the code that enforces it and the test that pins it (paths relative to mctl-agents).

## Requirements

### Requirement: Terminal PR state settles the proposal
The system SHALL flip a proposal to `merged`, recording the merge commit, when its PR is observed merged, and to `rejected`, recording the closing note, when its PR is observed closed without merging, whoever merged or closed it.

Enforced by `orchestrator/run_shepherd.py` `decide` (`flip-to-merged`, `flip-to-rejected`).

#### Scenario: Merged out of band
- **WHEN** the PR was merged by a human between ticks
- **THEN** the decision is `flip-to-merged` with the merge commit
- Pinned by `tests/test_run_shepherd.py::test_decide_flip_to_merged`

#### Scenario: Closed without merging
- **WHEN** the PR was closed unmerged
- **THEN** the decision is `flip-to-rejected`
- Pinned by `tests/test_run_shepherd.py::test_decide_flip_to_rejected`

### Requirement: Merge waits for a reviewed head
The system SHALL wait, and merge nothing, while the PR is a draft or the primary reviewer has not responded on the current head.

Enforced by `orchestrator/run_shepherd.py` `decide`.

#### Scenario: Draft PR
- **WHEN** the PR is a draft
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_decide_wait_draft`

#### Scenario: Reviewer still pending
- **WHEN** the reviewer has not responded on the head
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_decide_wait_codex_pending`

### Requirement: Findings on the current head block the merge
The system SHALL route a PR to `address-review` whenever a P1 or P2 finding written against the current head exists, whoever raised it and whatever the primary reviewer concluded, and SHALL decide what belongs to the current head by when the finding was written, not by the commit GitHub currently anchors it to.

Enforced by `orchestrator/run_shepherd.py` `decide` (findings filter by `created_at`).

#### Scenario: Finding on the head
- **WHEN** a P2 finding was written against the current head
- **THEN** the decision is `address-review`
- Pinned by `tests/test_run_shepherd.py::test_decide_address_review`

#### Scenario: Re-anchored old finding
- **WHEN** an inline finding written before the current head is re-anchored onto it and the head is approved
- **THEN** it does not block the merge
- Pinned by `tests/test_run_shepherd.py::test_a_re_anchored_finding_does_not_block_an_approved_head`

### Requirement: Merge follows the head's own verdict
The system SHALL merge only when the primary reviewer approved the current head, and SHALL wait when that reviewer requested changes on the head or never ruled on it, even with no open finding.

Enforced by `orchestrator/run_shepherd.py` `decide` (`head_verdict`).

#### Scenario: No verdict on this head
- **WHEN** the reviewer responded on the head but issued no verdict
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_no_verdict_on_this_head_is_a_wait_not_a_merge`

#### Scenario: Changes requested, nothing left to fix
- **WHEN** the head verdict is `CHANGES_REQUESTED` and every finding was answered
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_changes_requested_with_nothing_fresh_left_is_a_wait`

### Requirement: Required checks gate the merge
The system SHALL hand a failing required check on the head to remediation as a blocker, SHALL re-run rather than remediate a check that failed for infrastructure reasons, SHALL wait while a required check is pending, and SHALL never merge when the check state could not be read.

Enforced by `orchestrator/run_shepherd.py` `decide` (`ci-infra`, `ci-unknown`) and `orchestrator/ci_checks.py` `read_required_checks`.

#### Scenario: Failing required check
- **WHEN** a required check failed on the head for a code reason
- **THEN** the decision is `address-review` carrying the check
- Pinned by `tests/test_run_shepherd.py::test_decide_ci_actionable_failure_routes_to_address_review`

#### Scenario: Check state unreadable
- **WHEN** the required-check probe failed
- **THEN** the decision is `ci-unknown` and nothing merges
- Pinned by `tests/test_run_shepherd.py::test_decide_ci_unknown_fails_closed_never_merges`

### Requirement: Merge needs a mergeable, green and settled head
The system SHALL merge only when GitHub reports the PR mergeable, its checks are green, and the head was pushed longer ago than the settle window. An otherwise ready PR whose merge state stays `UNKNOWN` past the settle window SHALL get its own bounded decision and SHALL never merge on it.

Enforced by `orchestrator/run_shepherd.py` `decide` (`MERGEABLE_STATES`, `SHEPHERD_MERGE_SETTLE_MIN`, `UNKNOWN_MERGE_STATES`).

#### Scenario: Merge state blocked
- **WHEN** GitHub reports the merge state `BLOCKED`
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_decide_wait_merge_state_blocked`

#### Scenario: Inside the settle window
- **WHEN** a clean, green head was pushed within the settle window
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_decide_wait_within_settle_window`

#### Scenario: Ready but merge state unknown
- **WHEN** a clean, green, settled PR reports merge state `UNKNOWN`
- **THEN** the decision is `merge-state-unknown`, never `merge`
- Pinned by `tests/test_run_shepherd.py::test_decide_ready_but_merge_state_unknown`

#### Scenario: Ready to merge
- **WHEN** the head is approved, findings-free, green, mergeable and settled
- **THEN** the decision is `merge`
- Pinned by `tests/test_run_shepherd.py::test_decide_merge`

### Requirement: Merge bound to the reviewed head
The system SHALL merge with `gh pr merge --merge --delete-branch --match-head-commit <SHA>` using the head SHA the decision was made on, and SHALL treat a head mismatch as a `wait`.

Enforced by `orchestrator/run_shepherd.py` `merge_pr_unchecked`.

#### Scenario: Head moved after the decision
- **WHEN** a commit lands between the decision and the merge
- **THEN** the merge is refused and the next tick re-evaluates the new head
- Pinned by `tests/test_pr_merge_activity.py::test_head_moved_is_a_precondition_miss`

### Requirement: Some services are never merged by automation
The system SHALL never merge a PR for a service in `NEVER_MERGE_SERVICES` (`mctl-academy`, `mctl-gitops`, `.github`), whatever any environment setting or flag says, and SHALL refuse it again inside the merge call itself.

Enforced by `orchestrator/run_shepherd.py` `NEVER_MERGE_SERVICES`, `_service_mode`, `merge_pr`.

#### Scenario: Academy PR ready to merge
- **WHEN** an `mctl-academy` PR is otherwise ready
- **THEN** the decision is never `merge`
- Pinned by `tests/test_run_shepherd.py::test_decide_never_returns_merge_for_academy`

#### Scenario: Merge reached for a never-merge service
- **WHEN** `merge_pr` is called for such a service
- **THEN** it refuses without merging
- Pinned by `tests/test_run_shepherd_attempt_fresh.py::test_merge_pr_ignores_claims_and_still_refuses_never_merge_services`

### Requirement: Fix-only services defer the merge
The system SHALL, for a service in fix-only mode, review and remediate exactly as in full mode but return `defer-merge` where it would merge, recording `merge_owner` and leaving the proposal's `status` unchanged.

Enforced by `orchestrator/run_shepherd.py` `decide` (`fix_only`), `_service_mode`.

#### Scenario: Ready PR in fix-only mode
- **WHEN** a fix-only service's PR is ready to merge
- **THEN** the decision is `defer-merge`
- Pinned by `tests/test_run_shepherd.py::test_decide_defer_merge_in_fix_only`

#### Scenario: Other decisions unchanged
- **WHEN** a fix-only service's PR would wait or be remediated
- **THEN** the decision is the same as in full mode
- Pinned by `tests/test_run_shepherd.py::test_decide_fix_only_does_not_change_non_merge_decisions`

### Requirement: Agent-definition changes need a human merge
The system SHALL never merge, without a human decision, a PR that changes any path under the agent-definition trees (`agents/`, `platform-gitops/agent-platform/`), and SHALL treat a changed-file list it could not read completely as touching those trees.

Enforced by `orchestrator/run_shepherd.py` (`AGENT_DEFINITION_PATH_PREFIXES`, `CHANGED_PATHS_*`).

#### Scenario: Mixed PR
- **WHEN** a PR changes both ordinary code and a file under `agents/`
- **THEN** it counts as touching agent definitions
- Pinned by `tests/test_definition_merge_gate.py::test_a_mixed_pr_touches_agent_definitions`

#### Scenario: Truncated file list
- **WHEN** the changed-file list is truncated and shows no protected path
- **THEN** it still counts as touching agent definitions
- Pinned by `tests/test_definition_merge_gate.py::test_a_truncated_list_counts_as_touching_even_with_no_protected_path_seen`

### Requirement: Gated merges go through a single-use approval
The system SHALL, when the merge policy requires approval and the service is listed in `SHEPHERD_MERGE_APPROVAL_SERVICES`, never merge from a shepherd tick but return `defer-merge` with `merge_owner: devloop-workflow`, and SHALL perform the merge only from the DevLoop's gated activity after re-reading the PR, re-running `decide`, and consuming a human approval bound to the exact repository, PR number and head SHA, at most once per approval.

Enforced by `orchestrator/run_shepherd.py` `_merge_gate_delegated`, `orchestrator/policy_checkpoint.py` (`MCTL_POLICY_MERGE_APPROVAL`) and the gated merge activity.

#### Scenario: Gated service from a shepherd tick
- **WHEN** a gated service's PR is ready to merge
- **THEN** the shepherd defers it to the DevLoop workflow and does not merge
- Pinned by `tests/test_run_shepherd.py::test_process_one_defers_a_gated_services_merge_to_the_devloop`

#### Scenario: Approval granted
- **WHEN** a human approves the gated merge
- **THEN** the PR is merged exactly once
- Pinned by `tests/test_pr_merge_activity.py::test_a_granted_approval_merges_exactly_once`

#### Scenario: Gate off
- **WHEN** the policy is unconfigured
- **THEN** no service is gated
- Pinned by `tests/test_run_shepherd.py::test_merge_gate_delegated_false_when_policy_unconfigured`
