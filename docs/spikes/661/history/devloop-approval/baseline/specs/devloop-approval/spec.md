# devloop-approval Specification

## Purpose
When an agents-state proposal counts as human-approved, what that approval binds, how a DevLoop waits for it, and how the approve operation is exercised. Each requirement names the code that enforces it and the test that pins it (paths relative to mctl-agents).

## Requirements

### Requirement: Approval requirement read from the proposal
The system SHALL require a human approval whenever a proposal's `control` block is present and `control.requires_human_approval` is not an explicit falsey spelling (absent, `false`, a falsey string, or `0`), and SHALL treat a `control` block that is not a mapping as requiring approval. A proposal with no `control` block SHALL NOT require approval.

Enforced by `orchestrator/proposal_state.py` `requires_human_approval`.

#### Scenario: Quoted true still requires approval
- **WHEN** `control.requires_human_approval` is the string `"true"`
- **THEN** approval is required
- Pinned by `tests/test_run_implementer_approval.py::test_a_string_true_still_requires_approval`

#### Scenario: Unrecognised value fails closed
- **WHEN** `control.requires_human_approval` is a list, a mapping or a typo
- **THEN** approval is required
- Pinned by `tests/test_run_implementer_approval.py::test_an_unrecognised_requirement_value_fails_closed`

#### Scenario: Malformed control block
- **WHEN** `control` is present but is not a mapping
- **THEN** the approval requirement is not satisfied
- Pinned by `tests/test_run_implementer_approval.py::test_a_malformed_control_block_fails_closed`

#### Scenario: No control block
- **WHEN** a proposal has no `control` block (the incident-responder shape)
- **THEN** approval is not required
- Pinned by `tests/test_run_implementer_approval.py::test_a_proposal_with_no_control_block_does_not_require_approval`

### Requirement: Anonymous approver refused
The system SHALL treat an approval whose `approval.approved_by` is missing, not a string, or one of `""`, `unknown`, `none`, `null` (case-insensitive, trimmed) as not approved.

Enforced by `orchestrator/proposal_state.py` `human_approval_satisfied`.

#### Scenario: Approval naming nobody
- **WHEN** `approval.approved_by` is `unknown`
- **THEN** the proposal is not human-approved
- Pinned by `tests/test_run_implementer_approval.py::test_an_approval_naming_nobody_is_not_satisfied`

#### Scenario: Named approver
- **WHEN** `approval.approved_by` names a real login
- **THEN** the approval requirement is satisfied
- Pinned by `tests/test_run_implementer_approval.py::test_a_named_approver_satisfies_the_requirement`

### Requirement: Implementer refuses an unapproved proposal
The system SHALL refuse to implement an `accepted` proposal whose approval requirement is not satisfied, before any dry-run shortcut, and SHALL report it as a skip with a `skipped_reason`.

Enforced by `orchestrator/proposal_state.py` `human_approval_satisfied` and `orchestrator/run_implementer.py` `_implement_one`.

#### Scenario: Accepted without approval
- **WHEN** the implementer picks up an `accepted` proposal that requires approval and has none
- **THEN** it does not run and the result is a skip
- Pinned by `tests/test_run_implementer_approval.py::test_implement_one_refuses_an_unapproved_proposal`

#### Scenario: Refusal precedes the dry run
- **WHEN** the same proposal is run with `--dry-run`
- **THEN** it is still refused
- Pinned by `tests/test_run_implementer_approval.py::test_the_refusal_precedes_the_dry_run_shortcut`

### Requirement: Approval park
The system SHALL park a DevLoop after its investigation until an `approve` signal arrives.

Enforced by `orchestrator/temporal/workflows/dev_loop.py` (`wait_condition` on the approve signal).

#### Scenario: Approve releases the park
- **WHEN** an approve signal arrives for the parked loop
- **THEN** the loop proceeds to the approve flip and the implementer
- Pinned by `tests/test_dev_loop_workflow.py::test_investigate_then_wait_then_implement_after_approval`

### Requirement: Approve operation names the approver
The system SHALL perform the `proposed` to `accepted` flip by submitting the `mctl-agents-approve` CWFT with the approver login carried by the approve signal, recorded as `approval.approved_by`.

Enforced by `orchestrator/temporal/workflows/dev_loop.py` (approve step, `atomic_approve`).

#### Scenario: Approver carried by the signal
- **WHEN** an approve signal names a login
- **THEN** the approve CWFT records that login as `approval.approved_by`
- Pinned by `tests/test_dev_loop_workflow.py::test_investigate_then_wait_then_implement_after_approval`

### Requirement: Approve signal bound to one workflow
The system SHALL bind the approve signal to the DevLoop workflow id of exactly one issue (`dev-loop-<org>-<repo>-<issue>`).

Enforced by `orchestrator/temporal/issue_ref.py` `workflow_id_for`.

#### Scenario: Signal for another issue
- **WHEN** an approve signal targets a different workflow id
- **THEN** the parked workflow for this issue is not released
- Pinned by `tests/test_dev_loop_workflow.py` (workflow id derivation)
