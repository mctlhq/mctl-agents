## Purpose
When the PR shepherd may merge an implementer-opened pull request, when it must wait, remediate or hand the merge to someone else, and what binds a merge to the reviewed code. The review-fix loop itself (attempt caps, follow-up commits) is a separate capability. Each requirement names the code that enforces it and the test that pins it (paths relative to mctl-agents).

## ADDED Requirements

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
The system SHALL route a PR to `address-review` whenever a P1 or P2 finding anchored to the current head SHA exists.

Enforced by `orchestrator/run_shepherd.py` `decide`.

#### Scenario: Finding on the head
- **WHEN** a P2 finding was written against the current head
- **THEN** the decision is `address-review`
- Pinned by `tests/test_run_shepherd.py::test_decide_address_review`

### Requirement: Merge needs a mergeable, green and settled head
The system SHALL merge only when GitHub reports the PR mergeable and its checks are green.

Enforced by `orchestrator/run_shepherd.py` `decide` (`MERGEABLE_STATES`).

#### Scenario: Merge state blocked
- **WHEN** GitHub reports the merge state `BLOCKED`
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_decide_wait_merge_state_blocked`

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
