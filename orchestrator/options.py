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
        add_dirs=_sibling_add_dirs(service_dir.name),
        # Extend (NOT replace) parent env — child needs PATH/HOME/etc. for
        # npm-installed `claude` CLI and the Claude credentials lookup.
        env={**os.environ, "SIBLING_REPOS_PATH": SIBLING_REPOS_PATH},
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
