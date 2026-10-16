## ADDED Requirements

### Requirement: Approval records proposal digest
The system SHALL record a digest of `requirements.md`, `design.md` and `tasks.md` in the `approval` block at approval time.

#### Scenario: Digest recorded
- **WHEN** a human approves a proposal
- **THEN** `approval.content_digest` equals the digest of the triplet

## MODIFIED Requirements

### Requirement: Anonymous approver refused
The system SHALL treat an approval whose `approved_by` is empty, `unknown`, `none` or `null` (case-insensitive) as not approved.

#### Scenario: Anonymous identity
- **WHEN** `approval.approved_by` is `unknown`
- **THEN** the proposal is not human-approved

#### Scenario: Missing digest
- **WHEN** `approval.content_digest` is absent on a new approval
- **THEN** the proposal is not human-approved

## REMOVED Requirements

### Requirement: Legacy manual approval flip
