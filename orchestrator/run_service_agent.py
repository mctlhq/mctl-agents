"""Запуск агента-владельца одного сервиса.

Usage:
    python -m orchestrator.run_service_agent mctl-web
"""
import sys
import anyio
from claude_agent_sdk import query

from config.settings import AGENTS_DIR, SERVICE_AGENT_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_service_agent_options


PROMPT = """\
Автономный дневной прогон. **Не задавай вопросов человеку — он отсутствует.** Работай с тем, что есть.

Используй context/ (read-only база знаний — архитектура, ADR, текущая версия) и .claude/skills/ как руководство к действию.

Жёсткая последовательность (выполнить каждый шаг и создать соответствующие файлы):

**Шаг 1 — researcher:**
- Создай файл `inbox/{сегодняшняя ISO-дата YYYY-MM-DD}.md`
- Источники:
  - GitHub releases ключевых deps (список в context/architecture.md, раздел "Dependencies для researcher") — через WebFetch
  - CVE / security advisories по тем же deps — через WebSearch
  - Если доступны mcp__mctl__* тулзы — используй их для статуса сервиса. **Если их нет в твоём наборе тулзов — пропусти молча, не спрашивай юзера, не пытайся авторизоваться.**
- Формат записи строго как в .claude/agents/researcher.md

**Шаг 2 — analyst:**
- Прочитай inbox-файл, который только что создал
- Отфильтруй нерелевантное, выбери Top-3 с обоснованием (impact 1-5, effort 1-5)
- Дополни тот же inbox-файл секцией `## Top-3 (для spec-writer)` (формат — в .claude/agents/analyst.md)
- Сверься с context/decisions/ — не предлагай уже отвергнутое

**Шаг 3 — spec-writer:**
- Для каждого из Top-3 создай `proposals/<slug>/` с тремя файлами: `requirements.md`, `design.md`, `tasks.md` (формат — в .claude/agents/spec-writer.md)
- EARS-нотация для acceptance criteria
- Если slug уже существует — добавь `-v2`

**Шаг 4 — короткий итоговый отчёт** одним сообщением: что нашёл (числом), что отбросил (числом), что оформил (список slug'ов).

**Важно:**
- Никогда не запрашивай интерактивный ввод от человека.
- Не редактируй context/ — оно read-only.
- Если какой-то шаг технически невозможен (например, нет интернета для WebFetch) — задокументируй это в inbox-файле и продолжай со следующим шагом."""


async def run_service_agent(service: str) -> None:
    service_dir = AGENTS_DIR / service
    if not service_dir.exists():
        raise SystemExit(f"Папка агента не найдена: {service_dir}")

    options = build_service_agent_options(service_dir, SERVICE_AGENT_MODEL)
    print(f"\n=== Запускаю агента {service} ({SERVICE_AGENT_MODEL}) ===\n")

    async for message in query(prompt=PROMPT, options=options):
        # Стримим сообщения. Можно красивее форматировать — здесь просто print.
        print(message)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python -m orchestrator.run_service_agent <service>")
        print(f"Available: {', '.join(SERVICES)}")
        sys.exit(1)

    service = sys.argv[1]
    if service not in SERVICES:
        print(f"Неизвестный сервис {service}. Доступные: {', '.join(SERVICES)}")
        sys.exit(1)

    ensure_auth_for_sdk()
    anyio.run(run_service_agent, service)


if __name__ == "__main__":
    main()
