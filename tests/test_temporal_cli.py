"""Tests for orchestrator/temporal/start.py's URL-parsing logic, plus
cli.py's own output-formatting logic against a mocked client/handle.

The URL parsing is the only pure-function part of the Temporal-start path
that otherwise talks to a live Temporal frontend (start/approve/status);
`connect()` and the workflow handle are stubbed below the same way
test_run_issue_poller.py stubs `run_issue_poller.connect` — no live
frontend needed. Shared by cli.py (manual trigger) and run_issue_poller.py
(automatic trigger) — see start.py's module docstring.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from orchestrator.temporal import cli
from orchestrator.temporal.start import workflow_id_for
from orchestrator.temporal.workflows.dev_loop import AbandonState


def test_workflow_id_for_well_formed_issue_url():
    assert workflow_id_for("https://github.com/mctlhq/mctl-telegram/issues/296") == "dev-loop-mctlhq-mctl-telegram-296"


def test_workflow_id_for_matches_go_side_format():
    # Must stay byte-for-byte identical to
    # mctl-api/internal/temporalclient.WorkflowIDForIssueURL's output —
    # both sides start/signal the same Temporal workflow ID.
    assert workflow_id_for("https://github.com/mctlhq/mctl-openclaw/issues/1") == "dev-loop-mctlhq-mctl-openclaw-1"


def _run(coro):
    return asyncio.run(coro)


def _stub_client(*, desc_status_name: str, abandoned: bool, reason: str = ""):
    """A `connect()` stub whose handle answers `describe()` and `query()`
    the way `status()` calls them."""
    handle = SimpleNamespace(
        describe=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name=desc_status_name))),
        query=AsyncMock(return_value=AbandonState(abandoned=abandoned, reason=reason)),
        result=AsyncMock(),
    )
    client = SimpleNamespace(get_workflow_handle=lambda _wid: handle)

    async def _connect(*_a, **_kw):
        return client

    return _connect, handle


def test_status_queries_abandon_state_while_still_running(monkeypatch, capsys):
    """mctl-agents#420: `abandon_state` exists so an operator can tell "still
    parked" apart from "an operator ended this" WITHOUT waiting for the
    execution to complete — the query had no caller anywhere in the tree,
    despite `cli.py status` being named in its own docstring."""
    connect, handle = _stub_client(
        desc_status_name="RUNNING", abandoned=True, reason="operator cleanup"
    )
    monkeypatch.setattr(cli, "connect", connect)

    _run(cli.status("dev-loop-mctlhq-mctl-agents-420"))

    handle.query.assert_awaited_once()
    handle.result.assert_not_awaited()
    assert "abandoned:   operator cleanup" in capsys.readouterr().out


def test_status_says_nothing_when_still_running_and_not_abandoned(monkeypatch, capsys):
    connect, handle = _stub_client(desc_status_name="RUNNING", abandoned=False)
    monkeypatch.setattr(cli, "connect", connect)

    _run(cli.status("dev-loop-mctlhq-mctl-agents-420"))

    handle.query.assert_awaited_once()
    assert "abandoned:" not in capsys.readouterr().out


def test_status_skips_query_when_terminated_or_failed(monkeypatch, capsys):
    """mctl-agents#420: status must only query abandon_state on RUNNING workflows,
    avoiding WorkflowQueryFailedError on TERMINATED or FAILED executions."""
    connect, handle = _stub_client(desc_status_name="TERMINATED", abandoned=False)
    monkeypatch.setattr(cli, "connect", connect)

    _run(cli.status("dev-loop-mctlhq-mctl-agents-420"))

    handle.query.assert_not_awaited()
    handle.result.assert_not_awaited()
    assert "TERMINATED" in capsys.readouterr().out
