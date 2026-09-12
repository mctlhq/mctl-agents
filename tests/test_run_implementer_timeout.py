"""Wall-clock timeout and sub-agent-await tests for the Tier 2 implementer."""
from __future__ import annotations

import types

import anyio
import pytest

from orchestrator import run_implementer
from tests.conftest import fake_mcp_client_factory


class _FakeClient:
    """Stands in for ClaudeSDKClient — no MCTL_TOKEN in the test env means
    build_implementer_agent_options() returns mcp_servers={}, so
    ensure_mctl_connected() is never called; only query() and the two receive
    methods need faking here. The driver reads receive_messages()
    (mctl-agents#366); receive_response() is kept for the other fakes' shape."""

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

    async def receive_messages(self):
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
    assert "warn:" not in capsys.readouterr().err


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


# ---------------------------------------------------------------------------
# Awaiting an asynchronously-launched sub-agent (mctl-agents#366)
#
# The incident: the implementer read the finding, derived the correct fix,
# launched the staged `implementer` sub-agent, and ended its turn 25.8s later
# with the child still running. `receive_response()` returned at that
# ResultMessage, the driver returned, `git log old_head..HEAD` was empty, and
# the shepherd charged the proposal a review attempt for work the platform
# itself had discarded.
# ---------------------------------------------------------------------------
from claude_agent_sdk import (  # noqa: E402 — grouped with the tests that use them
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from orchestrator.subagent_wait import OrphanedSubagentError  # noqa: E402


def _started(task_id="t1", task_type="local_agent"):
    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=task_id,
        description="apply findings", uuid="u", session_id="s",
        task_type=task_type,
    )


def _updated(task_id="t1", status="completed"):
    return TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id=task_id,
        patch={"status": status}, status=status,
    )


def _result():
    return ResultMessage(
        subtype="success", duration_ms=25801, duration_api_ms=1,
        is_error=False, num_turns=6, session_id="s", total_cost_usd=0.1,
    )


def test_implementer_waits_for_async_launched_subagent(tmp_path, monkeypatch) -> None:
    """The headline regression. Fails on main, which stops at the ResultMessage.

    `consumed` is the real assertion: returning normally is not enough, the
    driver must have read *past* the ResultMessage to see the child settle.
    """
    consumed: list[object] = []

    async def messages():
        for message in (_started(), _result(), _updated("t1", "completed")):
            consumed.append(message)
            yield message

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)

    anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)

    assert any(isinstance(m, TaskUpdatedMessage) for m in consumed), (
        "driver stopped at the ResultMessage and abandoned the live sub-agent"
    )


def test_implementer_returns_immediately_when_no_tasks_are_live(tmp_path, monkeypatch) -> None:
    """Runs without a delegated child must not pay for the drain."""
    consumed: list[object] = []

    async def messages():
        for message in ("chatter", _result(), _updated("t1", "completed")):
            consumed.append(message)
            yield message

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)

    anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)

    assert not any(isinstance(m, TaskUpdatedMessage) for m in consumed)


def test_implementer_raises_orphaned_when_task_never_settles(tmp_path, monkeypatch) -> None:
    """And specifically NOT ImplementerOperationTimeout: code 44 is in the
    shepherd's deterministic set, so regressing to it would charge the attempt
    again by another route."""
    async def messages():
        yield _started()
        yield _result()
        await anyio.sleep(10)

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(run_implementer.ImplementerOrphanedSubagent, match=r"orphaned sub-agent:"):
        anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)


def test_implementer_raises_orphaned_when_stream_ends_with_live_task(tmp_path, monkeypatch) -> None:
    """The CLI exited while the child was still live."""
    async def messages():
        yield _started()
        yield _result()

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", 5)

    with pytest.raises(run_implementer.ImplementerOrphanedSubagent):
        anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)


def test_implementer_does_not_orphan_on_failed_terminal_status(tmp_path, monkeypatch, capsys) -> None:
    """A failed child is quiescent, not orphaned.

    Nothing is still mutating the worktree, so `_has_new_commits` is the right
    adjudicator: no commit -> 42, which IS deterministic and should charge an
    attempt. Only "we cannot prove the child is quiescent" earns 46.
    """
    async def messages():
        yield _started()
        yield _result()
        yield TaskNotificationMessage(
            subtype="task_notification", data={}, task_id="t1", status="failed",
            output_file="/dev/null", summary="boom", uuid="u", session_id="s",
        )

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", 5)

    anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)

    out = capsys.readouterr().out
    assert "warn:" in out and "failed" in out


def test_outer_timeout_still_wins_over_drain(tmp_path, monkeypatch) -> None:
    """Accepted ordering: if the outer bound fires first we get 44, not 46 —
    it means the turn itself was already at the wall."""
    async def messages():
        yield _started()
        yield _result()
        await anyio.sleep(10)

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", 30)

    with pytest.raises(run_implementer.ImplementerOperationTimeout):
        anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)


def test_orphaned_subagent_has_its_own_exit_code() -> None:
    assert run_implementer.EXIT_ORPHANED_SUBAGENT == 46
    assert run_implementer.EXIT_ORPHANED_SUBAGENT not in {
        run_implementer.EXIT_NO_FOLLOWUP_COMMITS,
        run_implementer.EXIT_BRANCH_MISSING_ON_ORIGIN,
        run_implementer.EXIT_OPERATION_TIMEOUT,
        run_implementer.EXIT_BLOCKED_ONLY,
    }
    assert run_implementer._review_feedback_exit_code(
        "orphaned sub-agent: 1 task(s) still live: t1"
    ) == run_implementer.EXIT_ORPHANED_SUBAGENT
    # The neighbouring mappings must not have moved.
    assert run_implementer._review_feedback_exit_code(
        "implementer produced no follow-up commits"
    ) == run_implementer.EXIT_NO_FOLLOWUP_COMMITS
    assert run_implementer._review_feedback_exit_code(
        "operation timed out: 900s"
    ) == run_implementer.EXIT_OPERATION_TIMEOUT


def test_implementer_orphan_subclasses_the_shared_error() -> None:
    assert issubclass(run_implementer.ImplementerOrphanedSubagent, OrphanedSubagentError)
