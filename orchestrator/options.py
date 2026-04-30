"""Build ClaudeAgentOptions for service agents and the mentor."""
import os
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions

from config.settings import MCTL_MCP_URL


def mctl_mcp_config() -> dict:
    """MCP config for https://api.mctl.ai/mcp.

    Returns an empty dict when MCTL_TOKEN is unset — the agent then runs
    without mcp__mctl__* tools (Read/Write/WebSearch/WebFetch/Bash only).
    Convenient for smoke tests and local dev without mctl access.
    """
    token = os.environ.get("MCTL_TOKEN", "").strip()
    if not token:
        print("⚠️  MCTL_TOKEN is not set — agent will run without mctl MCP tools.")
        return {}
    return {
        "mctl": {
            "type": "http",
            "url": MCTL_MCP_URL,
            "headers": {"Authorization": f"Bearer {token}"},
        }
    }


def _mctl_tool_globs() -> list[str]:
    """Allow mcp__mctl__* when MCP is configured; empty otherwise."""
    return ["mcp__mctl__*"] if mctl_mcp_config() else []


SERVICE_AGENT_BUDGET_USD = float(os.getenv("SERVICE_AGENT_BUDGET_USD", "5.00"))
MENTOR_BUDGET_USD = float(os.getenv("MENTOR_BUDGET_USD", "2.00"))
# Tier 2 implementer budget — soft cap per single proposal implementation.
# A proposal touching one or two files usually finishes well under this.
# No hard kill: the SDK stops sampling once the cap is exceeded but the
# already-applied edits remain. Caller can raise via env if a proposal needs more.
IMPLEMENTER_BUDGET_USD = float(os.getenv("IMPLEMENTER_BUDGET_USD", "3.00"))
# Tier 3 shepherd budget — soft cap per shepherd tick.
# A tick spends Claude tokens only when there are unresolved codex findings
# to summarise; most ticks find nothing to do and exit free. Default
# matches MENTOR_BUDGET_USD's order of magnitude but is intentionally lower
# (1.00) because the shepherd's SDK call is a single-shot JSON formatter,
# not an open-ended analysis.
SHEPHERD_BUDGET_USD = float(os.getenv("SHEPHERD_BUDGET_USD", "1.00"))


# Some service agents need read access to sibling mctl-* repos (e.g. mctl-docs
# scans their git log). Configurable via env so the same orchestrator works
# locally (paths cloned by user) and in cluster (paths cloned by workflow init).
SIBLING_REPOS_PATH = os.getenv(
    "SIBLING_REPOS_PATH",
    "/Users/dmitriimashkov/PycharmProjects/mctlhq",
)
SERVICES_NEEDING_SIBLING_ACCESS = {"mctl-docs"}
_SIBLING_REPOS = (
    "mctl-api", "mctl-web", "mctl-portal", "mctl-agent",
    "mctl-agents", "mctl-gitops", "mctl-openclaw",
)


def _sibling_add_dirs(service_name: str) -> list[str]:
    """For services that scan sibling repos, expand the workspace to include them."""
    if service_name not in SERVICES_NEEDING_SIBLING_ACCESS:
        return []
    base = Path(SIBLING_REPOS_PATH)
    return [str(base / r) for r in _SIBLING_REPOS if (base / r).exists()]


def build_service_agent_options(service_dir: Path, model: str) -> ClaudeAgentOptions:
    """Options for a service-owner agent."""
    return ClaudeAgentOptions(
        cwd=str(service_dir),                  # CLAUDE.md, .claude/, inbox/, proposals/
        setting_sources=["project"],           # pick up .claude/skills and .claude/agents
        model=model,
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "Bash",
        ] + _mctl_tool_globs(),
        mcp_servers=mctl_mcp_config(),
        permission_mode="acceptEdits",         # non-interactive — meant for cron
        max_budget_usd=SERVICE_AGENT_BUDGET_USD,
        add_dirs=_sibling_add_dirs(service_dir.name),
        # Extend (NOT replace) parent env — child needs PATH/HOME/etc. for
        # npm-installed `claude` CLI and the Claude credentials lookup.
        env={**os.environ, "SIBLING_REPOS_PATH": SIBLING_REPOS_PATH},
    )


def build_implementer_agent_options(repo_dir: Path, model: str, proposal_dir: Optional[Path] = None) -> ClaudeAgentOptions:
    """Options for the Tier 2 implementer agent.

    Runs with cwd inside the cloned sibling repo (not agents/<svc>/).
    setting_sources=["project"] picks up .claude/agents/implementer.md from
    cwd, so the orchestrator copies implementer.md into
    cloned-repo/.claude/agents/ before launching the agent
    (see run_implementer.py).

    add_dirs=[] — no need to see other sibling repos: the agent only
    works in cwd (the cloned target repo) and reads the spec from
    PROPOSAL_DIR (gitops worktree mounted in the pod, path passed via env).

    GITHUB_TOKEN is forwarded from the parent env — the gh CLI in the
    Python wrapper needs it for clone + pr create, but the SDK agent
    itself does not (the agent commits, never pushes).
    """
    env = {**os.environ}
    if proposal_dir is not None:
        env["PROPOSAL_DIR"] = str(proposal_dir)
    return ClaudeAgentOptions(
        cwd=str(repo_dir),
        setting_sources=["project"],
        model=model,
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "Bash",
        ] + _mctl_tool_globs(),
        mcp_servers=mctl_mcp_config(),
        permission_mode="acceptEdits",
        max_budget_usd=IMPLEMENTER_BUDGET_USD,
        add_dirs=[],
        env=env,
    )


def build_mentor_options(mentor_dir: Path, model: str) -> ClaudeAgentOptions:
    """Options for the mentor. Read-only across agent repos, writes only to digest/."""
    return ClaudeAgentOptions(
        cwd=str(mentor_dir.parent),            # .../agents — so the mentor sees every agent
        setting_sources=["project"],
        model=model,
        allowed_tools=[
            "Read", "Glob", "Grep",
            "Write", "Edit",                   # writes only into _mentor/digest/
        ] + _mctl_tool_globs(),
        mcp_servers=mctl_mcp_config(),
        permission_mode="acceptEdits",
        max_budget_usd=MENTOR_BUDGET_USD,
    )


def build_shepherd_options(shepherd_dir: Path, model: str) -> ClaudeAgentOptions:
    """Options for the Tier 3 shepherd sub-agent.

    The sub-agent's only job is to translate a pre-filtered bundle of
    P1/P2 codex findings into a JSON `{p1, p2, summaries}` object the
    Python wrapper hands to the Tier 2 implementer via
    `--review-feedback`. It is read-only — no Write/Edit/Bash. The cwd
    is `agents/_shepherd/` so the SDK picks up `shepherd.md` via
    `setting_sources=["project"]` (same pattern as the mentor and the
    implementer agent staging).

    No mctl MCP, no sibling repos — the bundle is self-contained text.
    """
    return ClaudeAgentOptions(
        cwd=str(shepherd_dir),
        setting_sources=["project"],
        model=model,
        allowed_tools=["Read"],
        mcp_servers={},
        permission_mode="acceptEdits",
        max_budget_usd=SHEPHERD_BUDGET_USD,
        env={**os.environ},
    )
