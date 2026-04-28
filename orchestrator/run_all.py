"""Run every service agent in parallel, then the mentor.

Usage:
    python -m orchestrator.run_all                # mode=full (default)
    RUN_MODE=mentor-only python -m orchestrator.run_all
    RUN_MODE=single-service RUN_SERVICE=mctl-api python -m orchestrator.run_all

Modes:
    full           — all service agents in parallel, then the mentor (default)
    mentor-only    — mentor only, reads existing proposals/ from state
    single-service — one agent (name in RUN_SERVICE), no mentor
"""
import os
import sys
import anyio

from config.settings import SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.run_service_agent import run_service_agent
from orchestrator.run_mentor import run_mentor


async def _full() -> None:
    async with anyio.create_task_group() as tg:
        for service in SERVICES:
            tg.start_soon(run_service_agent, service)
    await run_mentor()


async def _mentor_only() -> None:
    print("=== mode=mentor-only — skipping service agents ===")
    await run_mentor()


async def _single_service(service: str) -> None:
    if service not in SERVICES:
        print(
            f"ERROR: unknown service '{service}'. "
            f"Valid: {', '.join(SERVICES)}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"=== mode=single-service — only {service}, no mentor ===")
    await run_service_agent(service)


async def main() -> None:
    mode = os.getenv("RUN_MODE", "full").strip()
    service = os.getenv("RUN_SERVICE", "").strip()

    if mode == "full":
        await _full()
    elif mode == "mentor-only":
        await _mentor_only()
    elif mode == "single-service":
        await _single_service(service)
    else:
        print(
            f"ERROR: unknown RUN_MODE '{mode}'. "
            f"Valid: full, mentor-only, single-service",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    ensure_auth_for_sdk()
    anyio.run(main)
