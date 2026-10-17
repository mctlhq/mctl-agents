## MODIFIED Requirements

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

## ADDED Requirements

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
