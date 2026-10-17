## RENAMED Requirements

- FROM: `### Requirement: Approval park`
- TO: `### Requirement: Approval park is bounded`

## MODIFIED Requirements

### Requirement: Approval park is bounded
The system SHALL bound a DevLoop's wait for approval: it SHALL re-read the source issue on a bounded cadence, end without approving or implementing when the issue is observed closed, end with the expiry recorded after 14 days, leave the wait on an `abandon` signal, and keep waiting while the issue read fails.

Enforced by `orchestrator/temporal/workflows/dev_loop.py` (`APPROVAL_WAIT_DEADLINE`, `APPROVAL_POLL_INTERVAL`).

#### Scenario: Approve releases the park
- **WHEN** an approve signal arrives for the parked loop
- **THEN** the loop proceeds to the approve flip and the implementer
- Pinned by `tests/test_dev_loop_workflow.py::test_investigate_then_wait_then_implement_after_approval`

#### Scenario: Issue closes while parked
- **WHEN** the source issue is observed closed during the park
- **THEN** the loop ends without submitting approve or implement
- Pinned by `tests/test_dev_loop_workflow.py::test_parked_loop_ends_when_the_source_issue_closes`

#### Scenario: Deadline reached
- **WHEN** the park reaches its deadline with no approval
- **THEN** the loop ends with the expiry recorded
- Pinned by `tests/test_dev_loop_workflow.py::test_parked_loop_expires_at_the_approval_deadline`

#### Scenario: Abandon while parked
- **WHEN** an `abandon` signal arrives during the park
- **THEN** the loop leaves the wait and ends with the abandonment recorded
- Pinned by `tests/test_dev_loop_workflow.py::test_abandon_signal_ends_a_parked_loop`
