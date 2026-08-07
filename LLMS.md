# LLMS.md — mctl-agents Multi-Agent Architecture

> `mctl-agents` is the autonomous multi-agent system for the mctl platform. It contains orchestrator scripts, Temporal activities/workflows, and specialized agent prompts for automated issue intake, proposal implementation, and pull request shepherd merging.

## Agent Roles & Tiers

- **Tier 1 (Issue Poller & Investigator)**: `orchestrator/run_issue_poller.py` sweeps issues with `agents:intake`, clones target repos read-only, generates architectural proposals, and posts links back to GitHub.
- **Tier 2 (Implementer)**: `orchestrator/run_implementer.py` picks up accepted proposals, runs the Claude Agent SDK inside Python sub-agents, writes code, creates branches (`feat/agents-<slug>`), and opens pull requests.
- **Tier 3 (Shepherd)**: `orchestrator/run_shepherd.py` reviews open agent PRs, validates test suites, handles merge conflicts via rebase, and performs safe merges into target `main` branches.
- **Mentor Agent**: `orchestrator/run_mentor.py` periodically analyzes agent performance, common errors, and updates knowledge bases.

## Temporal Workflows & Activities

- `orchestrator/temporal/workflows/`: Workflows managing long-running agent loops (e.g. `IncidentLoopWorkflow`, `DevLoopWorkflow`).
- `orchestrator/temporal/activities/`: Temporal activities encapsulating discrete tasks (`incidents.py`, `poller.py`, `shepherd.py`).
- `orchestrator/temporal/worker.py`: Main Temporal worker entrypoint.

## Agent Prompt Conventions

- Prompt Markdown files live in `agents/<service>/.claude/agents/` or `agents/_generic/.claude/agents/`.
- Must follow strict non-destructive constraints (no direct `main` commits, mandatory testing, clear step-by-step reasoning).

## Development & Test Commands

```bash
uv run pytest              # Run full pytest test suite
uv run ruff check .        # Run ruff linter
uv run mypy .              # Run mypy static type checker
```
