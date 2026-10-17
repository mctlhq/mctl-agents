## ADDED Requirements

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
