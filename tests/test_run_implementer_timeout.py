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

    with pytest.raises(
        run_implementer.ImplementerOperationTimeout,
        match=r"model stream exceeded 0\.05s",
    ):
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


def test_shell_command_times_out(monkeypatch) -> None:
    def _timed_out(*args, **kwargs):
        raise run_implementer.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(run_implementer.subprocess, "run", _timed_out)
    monkeypatch.setattr(
        run_implementer,
        "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS",
        0.25,
    )

    with pytest.raises(
        run_implementer.ImplementerOperationTimeout,
        match=r"command exceeded 0\.25s: git fetch origin",
    ):
        run_implementer._run(["git", "fetch", "origin"])


def test_review_feedback_timeout_has_deterministic_exit_code() -> None:
    error = "operation timed out: command exceeded 300s: git push origin branch"

    assert (
        run_implementer._review_feedback_exit_code(error)
        == run_implementer.EXIT_OPERATION_TIMEOUT
    )
