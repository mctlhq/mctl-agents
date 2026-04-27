"""Запуск ментора — агрегирует proposals/ всех агентов в weekly digest.

Usage:
    python -m orchestrator.run_mentor
"""
import anyio
from datetime import date
from claude_agent_sdk import query

from config.settings import MENTOR_DIR, MENTOR_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_mentor_options


def build_prompt() -> str:
    iso_year, iso_week, _ = date.today().isocalendar()
    digest_path = f"_mentor/digest/{iso_year}-W{iso_week:02d}.md"
    services_list = ", ".join(SERVICES)

    return f"""\
Ты ментор платформы mctl. Сегодня собираешь еженедельный дайджест.

Активные сервисы: {services_list}.

1. Прочитай proposals/ во всех агентских репо ({services_list}).
   Читай только свежие предложения, которых ещё нет в предыдущих дайджестах.
2. Для каждого предложения оцени:
   - impact (1-5): эффект для платформы
   - effort (1-5): сложность реализации
   - конфликты с другими предложениями (несовместимые изменения)
   - соответствие текущим приоритетам платформы
3. Сгруппируй связанные предложения (одна тема — один блок).
4. Используй mcp__mctl__* тулзы чтобы свериться с реальным состоянием:
   текущие версии сервисов, открытые инциденты, лимиты тенантов.
5. Запиши результат в {digest_path}:
   - топ-5 предложений готовых к ревью
   - короткое summary по каждому: что, почему, impact/effort, конфликты
   - отдельный блок "Платформенные риски" — общие наблюдения
   - блок "Отложено" — что отбросил и почему

В конце выдай короткое сообщение со ссылкой на созданный файл."""


async def run_mentor() -> None:
    options = build_mentor_options(MENTOR_DIR, MENTOR_MODEL)
    print(f"\n=== Запускаю ментора ({MENTOR_MODEL}) ===\n")

    async for message in query(prompt=build_prompt(), options=options):
        print(message)


def main() -> None:
    ensure_auth_for_sdk()
    anyio.run(run_mentor)


if __name__ == "__main__":
    main()
