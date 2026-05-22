"""mctl platform configuration."""
import os
from pathlib import Path

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
    # "upwork-mcp",
]

# Services that are valid implementer/investigator targets but are NOT
# analyzed by the proactive researcher/analyst/spec-writer rotation.
NON_ROTATING_SERVICES = {"mctl-agents", "mctl-telegram", "mctl-design"}

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

# Models
SERVICE_AGENT_MODEL = os.getenv("SERVICE_AGENT_MODEL", "claude-sonnet-4-6")
MENTOR_MODEL = os.getenv("MENTOR_MODEL", "claude-opus-4-7")
# Tier 3 shepherd: a single-shot JSON formatter — sonnet is plenty.
SHEPHERD_MODEL = os.getenv("SHEPHERD_MODEL", "claude-sonnet-4-6")
