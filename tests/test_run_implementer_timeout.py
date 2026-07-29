"""Wall-clock timeout tests for the Tier 2 implementer."""
from __future__ import annotations

import anyio
import pytest

from orchestrator import run_implementer


async def _slow_query(*, prompt, options):
    await anyio.sleep(10)
    yield "unreachable"


async def _fast_query(*, prompt, options):
    yield "done"


def test_implementer_agent_times_out(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_implementer, "query", _slow_query)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(TimeoutError):
        anyio.run(
            run_implementer._run_implementer_agent,
            tmp_path,
            "prompt",
            tmp_path,
        )


def test_implementer_agent_completes_before_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_implementer, "query", _fast_query)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)

    anyio.run(
        run_implementer._run_implementer_agent,
        tmp_path,
        "prompt",
        tmp_path,
    )
