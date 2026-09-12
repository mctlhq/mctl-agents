"""Shared pytest fixtures.

The repo has no pyproject.toml; we resolve the project root by walking
up from this file so `pytest` can be run from anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Put the repo root on sys.path so `import orchestrator.run_shepherd`
# resolves without an editable install.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def anyio_backend() -> str:
    # Pins anyio's pytest plugin (auto-registered since anyio is already a
    # pinned dependency — no separate pytest-asyncio needed) to asyncio only.
    # temporalio's own async machinery (ActivityEnvironment,
    # WorkflowEnvironment) is asyncio-native; anyio's other backend (trio)
    # would not interoperate with it.
    return "asyncio"


class FakeMcpClient:
    """Stands in for ClaudeSDKClient across the mctl-MCP-guard tests.

    ``statuses`` is a list of {"mcpServers": [...]} dicts consumed in order
    by get_mcp_status() — the last entry repeats once exhausted. ``messages``
    is what receive_response()/receive_messages() yield. Pass ``status_error`` to make
    get_mcp_status() raise instead (SDK control-request failure). If
    get_mcp_status is never expected to be called (mcp_configured=False),
    leave ``statuses`` empty — it will raise AssertionError on any call.
    """

    def __init__(self, *, options=None, statuses=(), messages=(), status_error=None):
        self._statuses = list(statuses)
        self._messages = messages
        self._status_error = status_error
        self.status_calls = 0
        self.queried_prompt = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_mcp_status(self):
        self.status_calls += 1
        if self._status_error is not None:
            raise self._status_error
        if not self._statuses:
            raise AssertionError("get_mcp_status() called but no statuses were configured")
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    async def query(self, prompt):
        self.queried_prompt = prompt

    async def receive_response(self):
        for message in self._messages:
            yield message

    async def receive_messages(self):
        # The implementer driver reads this one (mctl-agents#366): it must not
        # stop at the first ResultMessage, or an async-launched sub-agent is
        # abandoned mid-flight.
        for message in self._messages:
            yield message


def fake_mcp_client_factory(*, statuses=(), messages=(), status_error=None):
    """Returns a ClaudeSDKClient-shaped factory: `Factory(options=...)`."""
    def _factory(*, options):
        return FakeMcpClient(
            options=options, statuses=statuses, messages=messages,
            status_error=status_error,
        )
    return _factory


# ---------------------------------------------------------------------------
# SDK lifecycle-message builders (mctl-agents#366).
#
# The drain tests in test_run_implementer_timeout.py, test_run_service_agent.py
# and test_run_issue_investigator.py all need the same three task messages.
# Built here so a future SDK field addition is one edit rather than one per
# driver — the same reason FakeMcpClient lives here rather than in each file.
# ---------------------------------------------------------------------------
def task_started_message(task_id="t1", *, task_type="local_agent", description="delegated work"):
    """A delegated sub-agent launching. `task_type` is what decides whether the
    ledger adopts it — `local_agent`/`local_workflow` are awaited, anything
    else (including None) is deliberately not."""
    from claude_agent_sdk import TaskStartedMessage

    return TaskStartedMessage(
        subtype="task_started", data={}, task_id=task_id,
        description=description, uuid="u", session_id="s", task_type=task_type,
    )


def task_updated_message(task_id="t1", status="completed"):
    """A status patch. The SDK documents that a terminal state can arrive this
    way with no accompanying notification, so drains must honour both."""
    from claude_agent_sdk import TaskUpdatedMessage

    return TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id=task_id,
        patch={"status": status}, status=status,
    )


def task_notification_message(task_id="t1", status="completed", summary="done"):
    """The other terminal vocabulary: reports `stopped` where task_updated
    reports the raw `killed`."""
    from claude_agent_sdk import TaskNotificationMessage

    return TaskNotificationMessage(
        subtype="task_notification", data={}, task_id=task_id, status=status,
        output_file="/dev/null", summary=summary, uuid="u", session_id="s",
    )


def result_message(*, is_error=False, api_error_status=None, subtype="success"):
    """A result frame — which ends one TURN, not the RUN."""
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=0, is_error=is_error,
        num_turns=1, session_id="s", api_error_status=api_error_status,
    )
