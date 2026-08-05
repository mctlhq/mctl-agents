"""Tests for orchestrator/temporal/start.py's URL-parsing logic — the only
pure-function part of the Temporal-start path that otherwise talks to a live
Temporal frontend (start/approve/status), which is out of scope for unit
tests here. Shared by cli.py (manual trigger) and run_issue_poller.py
(automatic trigger) — see start.py's module docstring.
"""
from __future__ import annotations

from orchestrator.temporal.start import workflow_id_for


def test_workflow_id_for_well_formed_issue_url():
    assert workflow_id_for("https://github.com/mctlhq/mctl-telegram/issues/296") == "dev-loop-mctlhq-mctl-telegram-296"


def test_workflow_id_for_matches_go_side_format():
    # Must stay byte-for-byte identical to
    # mctl-api/internal/temporalclient.WorkflowIDForIssueURL's output —
    # both sides start/signal the same Temporal workflow ID.
    assert workflow_id_for("https://github.com/mctlhq/mctl-openclaw/issues/1") == "dev-loop-mctlhq-mctl-openclaw-1"
