"""Универсальный auth для Claude Agent SDK.

Сценарии:
- Локальная разработка / прототип: CLAUDE_CODE_OAUTH_TOKEN (Claude Pro/Max).
- Production / деплой в кластер: ANTHROPIC_API_KEY (Console-биллинг).

Один и тот же код работает в обоих режимах — выбор по наличию env-переменной.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AuthMode:
    name: str          # "oauth" или "api_key"
    env_var: str       # имя env-переменной с токеном/ключом
    description: str   # человекочитаемое описание


def detect_auth() -> AuthMode:
    """Определить режим auth по env. OAuth имеет приоритет."""
    oauth = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if oauth:
        if not oauth.startswith("sk-ant-oat01-"):
            print(
                "⚠️  CLAUDE_CODE_OAUTH_TOKEN не похож на OAuth-токен "
                "(должен начинаться с sk-ant-oat01-). Возможно, ты подсунул API-ключ."
            )
        return AuthMode(
            name="oauth",
            env_var="CLAUDE_CODE_OAUTH_TOKEN",
            description="OAuth-токен Claude Pro/Max (расход по подписке)",
        )

    if api_key:
        if not api_key.startswith("sk-ant-api03-"):
            print(
                "⚠️  ANTHROPIC_API_KEY не похож на API-ключ "
                "(должен начинаться с sk-ant-api03-)."
            )
        return AuthMode(
            name="api_key",
            env_var="ANTHROPIC_API_KEY",
            description="API-ключ Anthropic Console (pay-per-token)",
        )

    # Третий путь: ни OAuth-токена, ни API-ключа в env, но локальный
    # `claude` CLI авторизован (~/.claude/.credentials.json или интерактивная сессия).
    # Claude Agent SDK подцепит CLI auth через subprocess. Это удобно в dev, но в
    # production / cron / Docker полагаться нельзя — там CLI не авторизован.
    import shutil
    if shutil.which("claude"):
        return AuthMode(
            name="cli_session",
            env_var="(claude CLI session)",
            description="существующая авторизация локального `claude` CLI",
        )

    raise RuntimeError(
        "Auth не найден: нет ни CLAUDE_CODE_OAUTH_TOKEN, ни ANTHROPIC_API_KEY в env, "
        "и `claude` CLI не установлен. Положи токен в .env или установи Claude Code."
    )


def ensure_auth_for_sdk() -> AuthMode:
    """Подготовить env так, чтобы Claude Agent SDK подхватил нужный credential.

    SDK сам читает обе переменные. Мы только логируем выбор и подчищаем
    лишнее, чтобы не было конфликтов приоритетов.
    """
    mode = detect_auth()

    if mode.name == "oauth":
        # API-ключ имеет приоритет над OAuth у CLI — убираем, если он пустой/мусорный
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            os.environ.pop("ANTHROPIC_API_KEY", None)

    print(f"🔑 Auth mode: {mode.name} — {mode.description}")
    return mode


if __name__ == "__main__":
    ensure_auth_for_sdk()
