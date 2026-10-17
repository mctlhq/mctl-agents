## ADDED Requirements

### Requirement: Gated merges go through a single-use approval
The system SHALL, when the merge policy requires approval and the service is listed in `SHEPHERD_MERGE_APPROVAL_SERVICES`, never merge from a shepherd tick but return `defer-merge` with `merge_owner: devloop-workflow`, and SHALL perform the merge only from the DevLoop's gated activity after re-reading the PR, re-running `decide`, and consuming a human approval bound to the exact repository, PR number and head SHA, at most once per approval.

Enforced by `orchestrator/run_shepherd.py` `_merge_gate_delegated`, `orchestrator/policy_checkpoint.py` (`MCTL_POLICY_MERGE_APPROVAL`) and the gated merge activity.

#### Scenario: Gated service from a shepherd tick
- **WHEN** a gated service's PR is ready to merge
- **THEN** the shepherd defers it to the DevLoop workflow and does not merge
- Pinned by `tests/test_run_shepherd.py::test_process_one_defers_a_gated_services_merge_to_the_devloop`

#### Scenario: Approval granted
- **WHEN** a human approves the gated merge
- **THEN** the PR is merged exactly once
- Pinned by `tests/test_pr_merge_activity.py::test_a_granted_approval_merges_exactly_once`

#### Scenario: Gate off
- **WHEN** the policy is unconfigured
- **THEN** no service is gated
- Pinned by `tests/test_run_shepherd.py::test_merge_gate_delegated_false_when_policy_unconfigured`
