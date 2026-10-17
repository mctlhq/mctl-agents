## ADDED Requirements

### Requirement: Required checks gate the merge
The system SHALL hand a failing required check on the head to remediation as a blocker, SHALL re-run rather than remediate a check that failed for infrastructure reasons, SHALL wait while a required check is pending, and SHALL never merge when the check state could not be read.

Enforced by `orchestrator/run_shepherd.py` `decide` (`ci-infra`, `ci-unknown`) and `orchestrator/ci_checks.py` `read_required_checks`.

#### Scenario: Failing required check
- **WHEN** a required check failed on the head for a code reason
- **THEN** the decision is `address-review` carrying the check
- Pinned by `tests/test_run_shepherd.py::test_decide_ci_actionable_failure_routes_to_address_review`

#### Scenario: Check state unreadable
- **WHEN** the required-check probe failed
- **THEN** the decision is `ci-unknown` and nothing merges
- Pinned by `tests/test_run_shepherd.py::test_decide_ci_unknown_fails_closed_never_merges`
