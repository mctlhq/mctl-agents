"""Сборка ClaudeAgentOptions для агентов и ментора."""
import os
from pathlib import Path
from claude_agent_sdk import ClaudeAgentOptions

from config.settings import MCTL_MCP_URL


def mctl_mcp_config() -> dict:
    """MCP-конфиг для подключения к https://api.mctl.ai/mcp.

    Возвращает пустой dict если MCTL_TOKEN не задан — агент тогда работает
    без mcp__mctl__* тулзов (только Read/Write/WebSearch/WebFetch/Bash).
    Удобно для smoke-тестов и dev без mctl-доступа.
    """
    token = os.environ.get("MCTL_TOKEN", "").strip()
    if not token:
        print("⚠️  MCTL_TOKEN не задан — агент запустится без mctl MCP tools.")
        return {}
    return {
        "mctl": {
            "type": "http",
            "url": MCTL_MCP_URL,
            "headers": {"Authorization": f"Bearer {token}"},
        }
    }


def _mctl_tool_globs() -> list[str]:
    """Если MCP сконфигурён — разрешаем mcp__mctl__*. Иначе — пусто."""
    return ["mcp__mctl__*"] if mctl_mcp_config() else []


SERVICE_AGENT_BUDGET_USD = float(os.getenv("SERVICE_AGENT_BUDGET_USD", "1.50"))
MENTOR_BUDGET_USD = float(os.getenv("MENTOR_BUDGET_USD", "2.00"))


def build_service_agent_options(service_dir: Path, model: str) -> ClaudeAgentOptions:
    """Опции для агента-владельца сервиса."""
    return ClaudeAgentOptions(
        cwd=str(service_dir),                  # CLAUDE.md, .claude/, inbox/, proposals/
        setting_sources=["project"],           # подхватить .claude/skills и .claude/agents
        model=model,
        allowed_tools=[
            "Read", "Write", "Edit", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "Bash",
        ] + _mctl_tool_globs(),
        mcp_servers=mctl_mcp_config(),
        permission_mode="acceptEdits",         # без интерактива — для cron
        max_budget_usd=SERVICE_AGENT_BUDGET_USD,
    )


def build_mentor_options(mentor_dir: Path, model: str) -> ClaudeAgentOptions:
    """Опции для ментора. Доступ только на чтение к репо агентов + запись в digest/."""
    return ClaudeAgentOptions(
        cwd=str(mentor_dir.parent),            # .../agents — чтобы видеть всех агентов
        setting_sources=["project"],
        model=model,
        allowed_tools=[
            "Read", "Glob", "Grep",
            "Write", "Edit",                   # пишет только в _mentor/digest/
        ] + _mctl_tool_globs(),
        mcp_servers=mctl_mcp_config(),
        permission_mode="acceptEdits",
        max_budget_usd=MENTOR_BUDGET_USD,
    )
