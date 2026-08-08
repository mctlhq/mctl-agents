"""Unit tests for the cleanup_closed_issue_workflows activity.

Tests are fully offline: no Temporal server, no GitHub API calls.
The `_issue_state` helper and Temporal client calls are patched.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.temporal.activities.cleanup import (
    CleanupResult,
    _issue_state,
    _process_running_workflows,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# _issue_state: unit tests (subprocess-patched)
# ---------------------------------------------------------------------------


def test_issue_state_open(tmp_path):
    """Returns OPEN for an open issue."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout='{"state": "OPEN"}', returncode=0)
        assert _issue_state("mctl-academy", 83) == "OPEN"


def test_issue_state_closed():
    """Returns CLOSED for a closed issue."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout='{"state": "CLOSED"}', returncode=0)
        assert _issue_state("mctl-academy", 83) == "CLOSED"


def test_issue_state_subprocess_error():
    """On subprocess failure returns OPEN (fail-safe: never cancel when uncertain)."""
    with patch("subprocess.run", side_effect=Exception("network error")):
        assert _issue_state("mctl-academy", 83) == "OPEN"


def test_issue_state_missing_key():
    """Missing 'state' key in JSON treated as OPEN."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout='{}', returncode=0)
        assert _issue_state("mctl-academy", 99) == "OPEN"


# ---------------------------------------------------------------------------
# _process_running_workflows: integration-style unit tests
# ---------------------------------------------------------------------------


def _mock_workflow(wf_id: str):
    wf = MagicMock()
    wf.id = wf_id
    return wf


async def _async_iter(items):
    for item in items:
        yield item


async def test_cancels_closed_issue_workflow():
    """A DevLoopWorkflow for a CLOSED issue is cancelled."""
    running = [_mock_workflow("dev-loop-mctlhq-mctl-academy-83")]

    mock_client = MagicMock()
    mock_client.list_workflows = MagicMock(return_value=_async_iter(running))
    handle = AsyncMock()
    mock_client.get_workflow_handle = MagicMock(return_value=handle)

    with (
        patch("orchestrator.temporal.activities.cleanup.connect", return_value=mock_client),
        patch("orchestrator.temporal.activities.cleanup._issue_state", return_value="CLOSED"),
    ):
        result: CleanupResult = await _process_running_workflows()

    assert result.cancelled == 1
    assert result.skipped == 0
    assert result.inspected == 1
    handle.cancel.assert_awaited_once()

    detail = result.details[0]
    assert detail.cancelled is True
    assert detail.repo == "mctl-academy"
    assert detail.issue_number == 83


async def test_skips_open_issue_workflow():
    """A DevLoopWorkflow for an OPEN issue is left running."""
    running = [_mock_workflow("dev-loop-mctlhq-mctl-academy-89")]

    mock_client = MagicMock()
    mock_client.list_workflows = MagicMock(return_value=_async_iter(running))
    handle = AsyncMock()
    mock_client.get_workflow_handle = MagicMock(return_value=handle)

    with (
        patch("orchestrator.temporal.activities.cleanup.connect", return_value=mock_client),
        patch("orchestrator.temporal.activities.cleanup._issue_state", return_value="OPEN"),
    ):
        result: CleanupResult = await _process_running_workflows()

    assert result.cancelled == 0
    assert result.skipped == 1
    handle.cancel.assert_not_awaited()


async def test_non_dev_loop_workflow_skipped():
    """Non-dev-loop workflows (e.g. manual-test-issue-poll) are never touched."""
    running = [_mock_workflow("manual-test-issue-poll")]

    mock_client = MagicMock()
    mock_client.list_workflows = MagicMock(return_value=_async_iter(running))
    handle = AsyncMock()
    mock_client.get_workflow_handle = MagicMock(return_value=handle)

    with (
        patch("orchestrator.temporal.activities.cleanup.connect", return_value=mock_client),
        patch("orchestrator.temporal.activities.cleanup._issue_state") as mock_state,
    ):
        result: CleanupResult = await _process_running_workflows()

    # _issue_state must not be called for non-dev-loop IDs
    mock_state.assert_not_called()
    assert result.cancelled == 0
    assert result.skipped == 1
    handle.cancel.assert_not_awaited()


async def test_cancel_failure_logged_not_raised():
    """A cancel failure is counted as skipped, not propagated as an exception."""
    running = [_mock_workflow("dev-loop-mctlhq-mctl-academy-83")]

    mock_client = MagicMock()
    mock_client.list_workflows = MagicMock(return_value=_async_iter(running))
    handle = AsyncMock()
    handle.cancel.side_effect = Exception("temporal unavailable")
    mock_client.get_workflow_handle = MagicMock(return_value=handle)

    with (
        patch("orchestrator.temporal.activities.cleanup.connect", return_value=mock_client),
        patch("orchestrator.temporal.activities.cleanup._issue_state", return_value="CLOSED"),
    ):
        result: CleanupResult = await _process_running_workflows()

    assert result.cancelled == 0
    assert result.skipped == 1
    detail = result.details[0]
    assert detail.cancelled is False
    assert "temporal unavailable" in detail.reason


async def test_empty_running_workflows():
    """No running workflows produces a zero-count result."""
    mock_client = MagicMock()
    mock_client.list_workflows = MagicMock(return_value=_async_iter([]))
    mock_client.get_workflow_handle = MagicMock()

    with patch("orchestrator.temporal.activities.cleanup.connect", return_value=mock_client):
        result: CleanupResult = await _process_running_workflows()

    assert result.inspected == 0
    assert result.cancelled == 0
    assert result.details == []


async def test_mixed_open_and_closed():
    """Only closed-issue workflows are cancelled; open ones are left alone."""
    running = [
        _mock_workflow("dev-loop-mctlhq-mctl-academy-81"),  # OPEN
        _mock_workflow("dev-loop-mctlhq-mctl-academy-83"),  # CLOSED
        _mock_workflow("dev-loop-mctlhq-mctl-gitops-761"),  # CLOSED
    ]

    mock_client = MagicMock()
    mock_client.list_workflows = MagicMock(return_value=_async_iter(running))
    handle = AsyncMock()
    mock_client.get_workflow_handle = MagicMock(return_value=handle)

    def state_by_id(repo, number):
        return "OPEN" if number == 81 else "CLOSED"

    with (
        patch("orchestrator.temporal.activities.cleanup.connect", return_value=mock_client),
        patch("orchestrator.temporal.activities.cleanup._issue_state", side_effect=state_by_id),
    ):
        result: CleanupResult = await _process_running_workflows()

    assert result.cancelled == 2
    assert result.inspected == 3
    assert handle.cancel.await_count == 2
