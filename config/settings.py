"""Конфигурация платформы mctl."""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / "agents"

# Сервисы платформы. Добавляй сюда по мере роста.
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

# mctl MCP — общий для всех агентов
MCTL_MCP_URL = "https://api.mctl.ai/mcp"

# Модели
SERVICE_AGENT_MODEL = os.getenv("SERVICE_AGENT_MODEL", "claude-sonnet-4-6")
MENTOR_MODEL = os.getenv("MENTOR_MODEL", "claude-opus-4-7")
