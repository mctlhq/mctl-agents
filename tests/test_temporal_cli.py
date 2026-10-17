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

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from orchestrator.temporal import cli
from orchestrator.temporal.start import restart_if_abandoned, workflow_id_for
from orchestrator.temporal.workflows.dev_loop import ABANDONED_ENDED_PREFIX, AbandonState


def test_workflow_id_for_well_formed_issue_url():
    assert workflow_id_for("https://github.com/mctlhq/mctl-telegram/issues/296") == "dev-loop-mctlhq-mctl-telegram-296"


def test_workflow_id_for_matches_go_side_format():
    # Must stay byte-for-byte identical to
    # mctl-api/internal/temporalclient.WorkflowIDForIssueURL's output —
    # both sides start/signal the same Temporal workflow ID.
    assert workflow_id_for("https://github.com/mctlhq/mctl-docs/issues/1") == "dev-loop-mctlhq-mctl-docs-1"


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


# --- mctl-api#640: restart a DevLoop abandoned before implement -------------

_ISSUE = "https://github.com/mctlhq/mctl-telegram/issues/1"
_WF = "dev-loop-mctlhq-mctl-telegram-1"


def _restart_client(*, status, ended="", implement=None, result_error=None):
    """A Temporal client stub for `restart_if_abandoned`: describe answers
    `status` with run id run-old, the pinned result read answers
    `ended`/`implement` (or raises `result_error`), and start_workflow
    records what it was asked to start."""
    desc = SimpleNamespace(status=getattr(WorkflowExecutionStatus, status), run_id="run-old")
    result = AsyncMock(side_effect=result_error) if result_error else AsyncMock(
        return_value=SimpleNamespace(ended=ended, implement=implement)
    )
    pinned_reads = []

    def get_handle_for(_wf, workflow_id, *, run_id=None):
        pinned_reads.append((workflow_id, run_id))
        return SimpleNamespace(result=result)

    client = SimpleNamespace(
        get_workflow_handle=lambda _wid: SimpleNamespace(describe=AsyncMock(return_value=desc)),
        get_workflow_handle_for=get_handle_for,
        start_workflow=AsyncMock(return_value=SimpleNamespace(id=_WF, result_run_id="run-new")),
    )
    return client, pinned_reads


def test_abandoned_prefix_matches_mctl_api():
    # Must stay byte-for-byte identical to mctl-api's
    # temporalclient.AbandonedEndedPrefix: both sides key the restart on it.
    assert ABANDONED_ENDED_PREFIX == "abandoned: "


def test_restart_if_abandoned_restarts_a_run_abandoned_before_implement():
    client, reads = _restart_client(status="COMPLETED", ended="abandoned: superseded")
    handle = _run(restart_if_abandoned(_ISSUE, client))
    assert handle is not None and handle.result_run_id == "run-new"
    assert reads == [(_WF, "run-old")], "the result must be read pinned to the described run"
    kwargs = client.start_workflow.await_args.kwargs
    assert kwargs["id"] == _WF
    assert kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert kwargs["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING


def test_restart_if_abandoned_refuses_everything_else():
    cases = [
        dict(status="COMPLETED", ended="abandoned: cleanup", implement=SimpleNamespace(phase="Succeeded")),
        dict(status="COMPLETED", ended="approval wait expired"),
        dict(status="COMPLETED", ended=""),
    ]
    for case in cases:
        client, _ = _restart_client(**case)
        assert _run(restart_if_abandoned(_ISSUE, client)) is None, case
        client.start_workflow.assert_not_awaited()
    for status in ("FAILED", "TERMINATED", "TIMED_OUT", "CANCELED", "CONTINUED_AS_NEW"):
        client, reads = _restart_client(status=status, ended="abandoned: x")
        assert _run(restart_if_abandoned(_ISSUE, client)) is None, status
        assert reads == [], f"read the result of a {status} run"
        client.start_workflow.assert_not_awaited()


def test_restart_if_abandoned_never_restarts_an_unreadable_run():
    client, _ = _restart_client(status="COMPLETED", result_error=RuntimeError("frontend unreachable"))
    with pytest.raises(RuntimeError):
        _run(restart_if_abandoned(_ISSUE, client))
    client.start_workflow.assert_not_awaited()


def _cli_start(monkeypatch, *, restarted):
    async def _connect(*_a, **_kw):
        return SimpleNamespace()

    async def _start(*_a, **_kw):
        raise WorkflowAlreadyStartedError(_WF, "DevLoopWorkflow")

    async def _restart(*_a, **_kw):
        return restarted

    monkeypatch.setattr(cli, "connect", _connect)
    monkeypatch.setattr(cli, "start_dev_loop_workflow", _start)
    monkeypatch.setattr(cli, "restart_if_abandoned", _restart)


def test_cli_start_restarts_after_abandon(monkeypatch, capsys):
    _cli_start(monkeypatch, restarted=SimpleNamespace(id=_WF, result_run_id="run-new"))
    _run(cli.start(_ISSUE))
    assert f"restarted {_WF} after an abandoned run (run_id=run-new)" in capsys.readouterr().out


def test_cli_start_reports_a_completed_run_it_will_not_restart(monkeypatch, capsys):
    _cli_start(monkeypatch, restarted=None)
    with pytest.raises(SystemExit) as exc:
        _run(cli.start(_ISSUE))
    assert exc.value.code == 1
    assert "not started" in capsys.readouterr().err
