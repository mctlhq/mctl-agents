## REMOVED Requirements

- `### Requirement: Approve operation names the approver`

## MODIFIED Requirements

### Requirement: Approval park is bounded
The system SHALL bound a DevLoop's wait for approval: it SHALL re-read the source issue on a bounded cadence, end without approving or implementing when the issue is observed closed, end with the expiry recorded at the wait deadline, leave the wait on an `abandon` signal, and keep waiting while the issue read fails. The wait deadline SHALL be 14 days on the legacy path and just under the 7-day life of the loop's grant on the canonical path.

Enforced by `orchestrator/temporal/workflows/dev_loop.py` (`APPROVAL_WAIT_DEADLINE`, `CANONICAL_APPROVAL_WAIT`, `APPROVAL_POLL_INTERVAL`).

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

#### Scenario: Canonical park matches the grant
- **WHEN** the loop parks on the canonical path
- **THEN** the park is no longer than the grant it waits for
- Pinned by `tests/test_dev_loop_workflow.py::test_the_canonical_park_is_as_long_as_the_grant_it_waits_for`

## ADDED Requirements

### Requirement: Canonical approval goes through a bound grant
On the canonical path the system SHALL create (or find by a deterministic key) an `aar_` approval request bound to the loop's execution `we_` before the human is asked, SHALL exercise `mctl-agents-approve` through mctl-api with an execution-scoped run token on the signalled grant, and SHALL never send an approver identity of its own. A signal without a valid `aar_` ref SHALL NOT approve, and any typed refusal SHALL end the loop without falling back to a direct CWFT submit.

Enforced by `orchestrator/temporal/activities/delegated_approval.py` (`request_devloop_approval`, `submit_delegated_approve`) and `orchestrator/temporal/workflows/dev_loop.py` (`CANONICAL_APPROVAL_PATCH`).

#### Scenario: Signalled grant is the one exercised
- **WHEN** a human approves the loop's `aar_` and the approve signal names it
- **THEN** that grant is exercised and no approver identity travels from the worker
- Pinned by `tests/test_dev_loop_workflow.py::test_the_signalled_approval_ref_is_the_one_exercised_and_no_approver_travels`

#### Scenario: Signal without a grant
- **WHEN** an approve signal carries no valid `aar_` ref
- **THEN** a canonical loop is not approved
- Pinned by `tests/test_dev_loop_workflow.py::test_a_signal_without_a_grant_never_approves_a_canonical_loop`

#### Scenario: Transport failure is not answered by the legacy path
- **WHEN** the operation call fails in transport
- **THEN** it is retried within a bound and never answered with a direct CWFT submit
- Pinned by `tests/test_delegated_approval.py::test_a_transport_failure_at_the_operation_is_not_answered_by_the_legacy_path`
