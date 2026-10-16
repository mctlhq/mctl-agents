# devloop-approval Specification

## Requirements

### Requirement: Anonymous approver refused
The system SHALL treat an approval whose `approved_by` is empty, `unknown`, `none` or `null` (case-insensitive) as not approved.

#### Scenario: Anonymous identity
- **WHEN** `approval.approved_by` is `unknown`
- **THEN** the proposal is not human-approved

### Requirement: Approval required by default
The system SHALL require human approval whenever `control.requires_human_approval` is present and is not an explicit falsey spelling, and SHALL fail closed on a malformed `control` block.

#### Scenario: Malformed control block
- **WHEN** `control` is present but is not a mapping
- **THEN** the approval requirement is not satisfied

### Requirement: Approve signal bound to one workflow
The system SHALL bind the approve signal to the DevLoop workflow id of exactly one issue (`dev-loop-<org>-<repo>-<issue>`).

#### Scenario: Signal for another issue
- **WHEN** an approve signal targets a different workflow id
- **THEN** the parked workflow for this issue is not released

### Requirement: Legacy manual approval flip
The system SHALL accept a manual `.status.yaml` edit to `accepted` as an approval path.

#### Scenario: Manual flip
- **WHEN** `.status.yaml` is edited to `accepted`
- **THEN** the implementer may pick the proposal up
