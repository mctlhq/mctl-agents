## ADDED Requirements

### Requirement: Some services are never merged by automation
The system SHALL never merge a PR for a service in `NEVER_MERGE_SERVICES` (`mctl-academy`, `mctl-gitops`, `.github`), whatever any environment setting or flag says, and SHALL refuse it again inside the merge call itself.

Enforced by `orchestrator/run_shepherd.py` `NEVER_MERGE_SERVICES`, `_service_mode`, `merge_pr`.

#### Scenario: Academy PR ready to merge
- **WHEN** an `mctl-academy` PR is otherwise ready
- **THEN** the decision is never `merge`
- Pinned by `tests/test_run_shepherd.py::test_decide_never_returns_merge_for_academy`

#### Scenario: Merge reached for a never-merge service
- **WHEN** `merge_pr` is called for such a service
- **THEN** it refuses without merging
- Pinned by `tests/test_run_shepherd_attempt_fresh.py::test_merge_pr_ignores_claims_and_still_refuses_never_merge_services`

### Requirement: Fix-only services defer the merge
The system SHALL, for a service in fix-only mode, review and remediate exactly as in full mode but return `defer-merge` where it would merge, recording `merge_owner` and leaving the proposal's `status` unchanged.

Enforced by `orchestrator/run_shepherd.py` `decide` (`fix_only`), `_service_mode`.

#### Scenario: Ready PR in fix-only mode
- **WHEN** a fix-only service's PR is ready to merge
- **THEN** the decision is `defer-merge`
- Pinned by `tests/test_run_shepherd.py::test_decide_defer_merge_in_fix_only`

#### Scenario: Other decisions unchanged
- **WHEN** a fix-only service's PR would wait or be remediated
- **THEN** the decision is the same as in full mode
- Pinned by `tests/test_run_shepherd.py::test_decide_fix_only_does_not_change_non_merge_decisions`
