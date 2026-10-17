## ADDED Requirements

### Requirement: Execution needs a human authorization
The system SHALL start execution on a record nobody is watching (the implement sweep) only when the record carries an approval with a named approver, and SHALL report a record with no approval metadata as `legacy auto-accepted / unreviewed` and one approved without an identity as `approved with no recorded approver identity`. A `requires_human_approval: false` waiver SHALL authorize nothing by itself.

Enforced by `orchestrator/proposal_state.py` `execution_authorization`.

#### Scenario: Legacy auto-accepted record
- **WHEN** the sweep finds an `accepted` record with no `approval` block
- **THEN** it is not executed and is reported as `legacy auto-accepted / unreviewed`
- Pinned by `tests/test_stranded_activity.py`
