"""Параллельный прогон всех агентов сервисов + ментор.

Usage:
    python -m orchestrator.run_all
"""
import anyio

from config.settings import SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.run_service_agent import run_service_agent
from orchestrator.run_mentor import run_mentor


async def main() -> None:
    # Сначала параллельно — каждый агент работает в своём cwd, друг другу не мешают
    async with anyio.create_task_group() as tg:
        for service in SERVICES:
            tg.start_soon(run_service_agent, service)

    # Затем ментор — он читает proposals/, которые наполнили агенты
    await run_mentor()


if __name__ == "__main__":
    ensure_auth_for_sdk()
    anyio.run(main)
