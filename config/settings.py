"""mctl platform configuration."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Platform services. Append as the platform grows.
SERVICES = [
    "mctl-web",
    "mctl-openclaw",
    "mctl-docs",
    "mctl-api",
    "mctl-portal",
    "mctl-agent",
    "mctl-gitops",
    # "upwork-mcp",
]

MENTOR_DIR = AGENTS_DIR / "_mentor"

# mctl MCP — shared by every agent
MCTL_MCP_URL = "https://api.mctl.ai/mcp"

# Models
SERVICE_AGENT_MODEL = os.getenv("SERVICE_AGENT_MODEL", "claude-sonnet-4-6")
MENTOR_MODEL = os.getenv("MENTOR_MODEL", "claude-opus-4-7")
