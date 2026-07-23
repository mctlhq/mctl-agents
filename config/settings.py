"""mctl platform configuration."""
from pathlib import Path

from config.model_policy import resolve_model

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Platform services. Append as the platform grows.
#
# This list gates two things:
#   1) Service-agent rotation (researcher / analyst / spec-writer cron).
#   2) Tier 2 implementer's `--service` filter — only proposals under
#      agents-state/<svc>/ where svc is in SERVICES are eligible.
#
# `mctl-agents` (the agent platform itself) is included so the implementer
# can accept self-improvement proposals targeting this repo (new
# orchestrator modules, sub-agent prompts, etc.). The service-agent
# rotation skips it — analysis of the agent platform is the mentor's job
# via _mentor/ aggregations, not a per-service researcher run.
#
# `mctl-telegram` is included as a valid implementer/investigator target
# but is NOT a rotation service: it has no agents/<svc>/ scaffold and is
# driven only by GitHub-issue proposals via the issue-investigator.
#
# `mctl-design` is the autonomous-pipeline pilot (design system + Storybook
# at ui.mctl.ai). It is an issue-driven implementer target only — no
# proactive rotation, and its review/fix/merge is owned by pr-steward, not
# the shepherd (see SHEPHERD_SKIP_SERVICES in run_shepherd.py). The generic
# implementer sub-agent (agents/_generic/.claude/agents/implementer.md)
# covers it; no per-service scaffold.
SERVICES = [
    "mctl-web",
    "mctl-openclaw",
    "mctl-docs",
    "mctl-api",
    "mctl-portal",
    "mctl-agent",
    "mctl-gitops",
    "mctl-agents",
    "mctl-telegram",
    "mctl-design",
    "mctl-pairdesk",
    # "upwork-mcp",
]

# Services that are valid implementer/investigator targets but are NOT
# analyzed by the proactive researcher/analyst/spec-writer rotation.
NON_ROTATING_SERVICES = {"mctl-agents", "mctl-telegram", "mctl-design", "mctl-pairdesk"}

# Subset of SERVICES that the proactive R&D rotation analyzes via
# researcher/analyst/spec-writer. Anything in SERVICES but NOT here is
# accepted by the implementer (--service filter) but is not subject to
# automatic proposal generation; proposals for those services are
# authored by the mentor, by humans, or by the issue-investigator.
ROTATING_SERVICES = [s for s in SERVICES if s not in NON_ROTATING_SERVICES]

MENTOR_DIR = AGENTS_DIR / "_mentor"
SHEPHERD_DIR = AGENTS_DIR / "_shepherd"

# mctl MCP — shared by every agent
MCTL_MCP_URL = "https://api.mctl.ai/mcp"

# Models. Task-specific env vars remain supported as highest-priority
# overrides so existing workflow configuration remains backwards compatible.
SERVICE_AGENT_SELECTION = resolve_model(
    "service_agent",
    legacy_model_env="SERVICE_AGENT_MODEL",
)
MENTOR_SELECTION = resolve_model(
    "mentor_digest",
    legacy_model_env="MENTOR_MODEL",
)
SHEPHERD_SELECTION = resolve_model(
    "review_findings_normalize",
    legacy_model_env="SHEPHERD_MODEL",
)

SERVICE_AGENT_MODEL = SERVICE_AGENT_SELECTION.model
MENTOR_MODEL = MENTOR_SELECTION.model
SHEPHERD_MODEL = SHEPHERD_SELECTION.model
