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
The system SHALL refuse to implement an `accepted` proposal whose approval requirement is not satisfied, before any dry-run shortcut, and SHALL classify the outcome as `blocked` with the stable code `approval-missing`, distinct from `failed` and `skipped`.

Enforced by `orchestrator/proposal_state.py` `unrunnable_reason` and `orchestrator/run_implementer.py` `_implement_one`.

#### Scenario: Accepted without approval
- **WHEN** the implementer picks up an `accepted` proposal that requires approval and has none
- **THEN** it does not run and the result is `blocked` with code `approval-missing`
- Pinned by `tests/test_run_implementer_approval.py::test_implement_one_classifies_the_refusal_as_blocked`

#### Scenario: Refusal precedes the dry run
- **WHEN** the same proposal is run with `--dry-run`
- **THEN** it is still refused
- Pinned by `tests/test_run_implementer_approval.py::test_the_refusal_precedes_the_dry_run_shortcut`

### Requirement: Blocked marker is durable and written once
The system SHALL record a refused proposal as a top-level `blocked` block (`code`, `since`, `message`, `remedy`) in its `.status.yaml`, leave `status: accepted` unchanged, leave the file byte-identical while the condition is unchanged, and write nothing under `--dry-run`. The remedy SHALL name the derived DevLoop workflow id when the proposal has a `github_issue` source, and SHALL say no DevLoop exists otherwise.

Enforced by `orchestrator/run_implementer.py` (blocked marker writer).

#### Scenario: Written exactly once
- **WHEN** a blocked proposal is seen on two consecutive runs
- **THEN** the second run leaves its `.status.yaml` byte-identical
- Pinned by `tests/test_run_implementer_approval.py::test_the_blocked_marker_is_written_exactly_once`

#### Scenario: Remedy without a source issue
- **WHEN** the blocked proposal has no `github_issue` source
- **THEN** the remedy states that no DevLoop workflow exists for it
- Pinned by `tests/test_run_implementer_approval.py::test_blocked_remedy_names_no_devloop_workflow_without_a_source_block`

### Requirement: No writer creates an unrunnable proposal
The system SHALL refuse, leaving the file untouched, any `.status.yaml` write whose result is `accepted` with an unsatisfied approval requirement when the record on disk was not already in that state, and SHALL still permit writes that only annotate a record already in that state.

Enforced by `orchestrator/proposal_state.py` `update_status_file` (`UnrunnableProposalError`).

#### Scenario: Clean write into the unrunnable state
- **WHEN** a writer sets `status: accepted` on a proposal that requires approval and has none
- **THEN** `UnrunnableProposalError` is raised and the file is unchanged
- Pinned by `tests/test_run_implementer_approval.py::test_update_status_file_refuses_a_clean_write_into_the_unrunnable_state`

#### Scenario: Annotating an already-unrunnable record
- **WHEN** the implementer writes the `blocked` marker on such a record
- **THEN** the write is permitted
- Pinned by `tests/test_run_implementer_approval.py::test_update_status_file_permits_annotating_an_already_unrunnable_proposal`

### Requirement: Approval binds the approved content
The system SHALL record `approval.content_digest` when the approve operation flips a proposal from `proposed` to `accepted`, computed over `requirements.md`, `design.md`, `tasks.md` and `proposed-content.md` in that order, and the implementer SHALL refuse a proposal that requires approval when the digest is missing (`approval-digest-missing`) or differs from the content on disk (`approval-digest-mismatch`). There is no grandfathering.

Enforced by `orchestrator/proposal_state.py` `proposal_content_digest` / `approval_digest_problem`, `orchestrator/run_implementer.py` `_implement_one`, and mctl-gitops `cwft-mctl-agents-approve.yaml` `flip-status`.

#### Scenario: Content changed after approval
- **WHEN** any of the four files changes after the flip
- **THEN** the implementer refuses with `approval-digest-mismatch`
- Pinned by `tests/test_proposal_digest.py` (drift cases, parametrised over all four files)

#### Scenario: Approval without a digest
- **WHEN** a proposal requires approval and `approval.content_digest` is absent
- **THEN** the implementer refuses with `approval-digest-missing`
- Pinned by `tests/test_proposal_digest.py`

### Requirement: Execution needs a human authorization
The system SHALL start execution on a record nobody is watching (the implement sweep) only when the record carries an approval with a named approver, and SHALL report a record with no approval metadata as `legacy auto-accepted / unreviewed` and one approved without an identity as `approved with no recorded approver identity`. A `requires_human_approval: false` waiver SHALL authorize nothing by itself.

Enforced by `orchestrator/proposal_state.py` `execution_authorization`.

#### Scenario: Legacy auto-accepted record
- **WHEN** the sweep finds an `accepted` record with no `approval` block
- **THEN** it is not executed and is reported as `legacy auto-accepted / unreviewed`
- Pinned by `tests/test_stranded_activity.py`

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

### Requirement: Approve signal bound to one workflow
The system SHALL bind the approve signal to the DevLoop workflow id of exactly one issue (`dev-loop-<org>-<repo>-<issue>`).

Enforced by `orchestrator/temporal/issue_ref.py` `workflow_id_for`.

#### Scenario: Signal for another issue
- **WHEN** an approve signal targets a different workflow id
- **THEN** the parked workflow for this issue is not released
- Pinned by `tests/test_dev_loop_workflow.py` (workflow id derivation)
