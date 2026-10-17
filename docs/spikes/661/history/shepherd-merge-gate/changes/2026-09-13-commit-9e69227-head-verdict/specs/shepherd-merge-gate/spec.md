## MODIFIED Requirements

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

## ADDED Requirements

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
