"""Shared pytest fixtures.

The repo has no pyproject.toml; we resolve the project root by walking
up from this file so `pytest` can be run from anywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Put the repo root on sys.path so `import orchestrator.run_shepherd`
# resolves without an editable install.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FakeMcpClient:
    """Stands in for ClaudeSDKClient across the mctl-MCP-guard tests.

    ``statuses`` is a list of {"mcpServers": [...]} dicts consumed in order
    by get_mcp_status() — the last entry repeats once exhausted. ``messages``
    is what receive_response() yields. Pass ``status_error`` to make
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


def fake_mcp_client_factory(*, statuses=(), messages=(), status_error=None):
    """Returns a ClaudeSDKClient-shaped factory: `Factory(options=...)`."""
    def _factory(*, options):
        return FakeMcpClient(
            options=options, statuses=statuses, messages=messages,
            status_error=status_error,
        )
    return _factory
