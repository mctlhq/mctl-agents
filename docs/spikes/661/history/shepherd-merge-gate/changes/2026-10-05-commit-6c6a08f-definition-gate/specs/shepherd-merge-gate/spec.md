## ADDED Requirements

### Requirement: Agent-definition changes need a human merge
The system SHALL never merge, without a human decision, a PR that changes any path under the agent-definition trees (`agents/`, `platform-gitops/agent-platform/`), and SHALL treat a changed-file list it could not read completely as touching those trees.

Enforced by `orchestrator/run_shepherd.py` (`AGENT_DEFINITION_PATH_PREFIXES`, `CHANGED_PATHS_*`).

#### Scenario: Mixed PR
- **WHEN** a PR changes both ordinary code and a file under `agents/`
- **THEN** it counts as touching agent definitions
- Pinned by `tests/test_definition_merge_gate.py::test_a_mixed_pr_touches_agent_definitions`

#### Scenario: Truncated file list
- **WHEN** the changed-file list is truncated and shows no protected path
- **THEN** it still counts as touching agent definitions
- Pinned by `tests/test_definition_merge_gate.py::test_a_truncated_list_counts_as_touching_even_with_no_protected_path_seen`
