"""Wall-clock timeout tests for the Tier 2 implementer."""
from __future__ import annotations

import types

import anyio
import pytest

from orchestrator import run_implementer
from tests.conftest import fake_mcp_client_factory


class _FakeClient:
    """Stands in for ClaudeSDKClient — no MCTL_TOKEN in the test env means
    build_implementer_agent_options() returns mcp_servers={}, so
    ensure_mctl_connected() is never called; only query()/receive_response()
    need faking here."""

    def __init__(self, *, options, message_gen):
        self._message_gen = message_gen

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def query(self, prompt):
        pass

    async def receive_response(self):
        async for message in self._message_gen():
            yield message


def _fake_client_factory(message_gen):
    def _factory(*, options):
        return _FakeClient(options=options, message_gen=message_gen)
    return _factory


async def _slow_messages():
    await anyio.sleep(10)
    yield "unreachable"


async def _fast_messages():
    yield "done"


def test_implementer_agent_times_out(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(_slow_messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(
        run_implementer.ImplementerOperationTimeout,
        match=r"operation exceeded 0\.05s",
    ):
        anyio.run(
            run_implementer._run_implementer_agent,
            tmp_path,
            "prompt",
            tmp_path,
        )


def test_implementer_agent_completes_before_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(_fast_messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)

    anyio.run(
        run_implementer._run_implementer_agent,
        tmp_path,
        "prompt",
        tmp_path,
    )


# ---------------------------------------------------------------------------
# mctl MCP connectivity guard, wiring level
#
# Regression coverage for a Claude-review finding on PR #84: the tests above
# all run with MCTL_TOKEN unset (mcp_servers={}), so ensure_mctl_connected()
# is never actually invoked here — only in isolation via test_mcp_guard.py.
# These stub build_implementer_agent_options() to force mcp_servers
# non-empty, matching tests/test_run_incident_responder.py's pattern.
# ---------------------------------------------------------------------------
def _stub_build_options(monkeypatch, *, mcp_servers):
    monkeypatch.setattr(
        run_implementer, "build_implementer_agent_options",
        lambda *args, **kwargs: types.SimpleNamespace(mcp_servers=mcp_servers),
    )


def test_implementer_agent_connected_mcp_dispatches_without_warning(tmp_path, monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        run_implementer, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}],
            messages=["done"],
        ),
    )
    anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)
    assert "⚠️" not in capsys.readouterr().err


def test_implementer_agent_failed_mcp_warns_but_still_dispatches(tmp_path, monkeypatch, capsys):
    """fatal=False: a broken mctl connection must not stop the implementer
    from applying an already-written proposal via Read/Write/Edit/Bash."""
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        run_implementer, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}],
            messages=["done"],
        ),
    )
    anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)  # must not raise
    assert "boom" in capsys.readouterr().err


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
