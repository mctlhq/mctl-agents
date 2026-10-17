## MODIFIED Requirements

### Requirement: Merge needs a mergeable, green and settled head
The system SHALL merge only when GitHub reports the PR mergeable, its checks are green, and the head was pushed longer ago than the settle window.

Enforced by `orchestrator/run_shepherd.py` `decide` (`MERGEABLE_STATES`, `SHEPHERD_MERGE_SETTLE_MIN`).

#### Scenario: Merge state blocked
- **WHEN** GitHub reports the merge state `BLOCKED`
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_decide_wait_merge_state_blocked`

#### Scenario: Inside the settle window
- **WHEN** a clean, green head was pushed within the settle window
- **THEN** the decision is `wait`
- Pinned by `tests/test_run_shepherd.py::test_decide_wait_within_settle_window`

#### Scenario: Ready to merge
- **WHEN** the head is approved, findings-free, green, mergeable and settled
- **THEN** the decision is `merge`
- Pinned by `tests/test_run_shepherd.py::test_decide_merge`
