"""Wiring-level tests for run_mentor()'s mctl MCP connectivity guard.

See tests/test_run_service_agent.py's module docstring for why these exist
(Claude-review finding on PR #84).
"""
from __future__ import annotations

import types

import anyio

from orchestrator import run_mentor as rm
from tests.conftest import fake_mcp_client_factory


def _stub_build_options(monkeypatch, *, mcp_servers):
    monkeypatch.setattr(
        rm, "build_mentor_options",
        lambda *args, **kwargs: types.SimpleNamespace(mcp_servers=mcp_servers),
    )
    # run_mentor() also rotates old digests on disk before building options —
    # not relevant to the MCP guard, no-op it so the test doesn't need a real
    # digest directory.
    monkeypatch.setattr(rm, "_rotate_old_digests", lambda *args, **kwargs: None)


def test_connected_mcp_dispatches_query_without_warning(monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rm, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}]
        ),
    )
    anyio.run(rm.run_mentor)
    assert "warn:" not in capsys.readouterr().err


def test_failed_mcp_warns_but_still_dispatches_query(monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    factory = fake_mcp_client_factory(
        statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}]
    )
    monkeypatch.setattr(rm, "ClaudeSDKClient", factory)
    anyio.run(rm.run_mentor)  # must not raise
    assert "boom" in capsys.readouterr().err


def test_mcp_not_configured_skips_status_check_entirely(monkeypatch):
    _stub_build_options(monkeypatch, mcp_servers={})
    monkeypatch.setattr(rm, "ClaudeSDKClient", fake_mcp_client_factory())
    anyio.run(rm.run_mentor)  # must not raise


def _digests(tmp_path, weeks):
    for year, week in weeks:
        (tmp_path / f"{year}-W{week:02d}.md").write_text("x")


def test_rotate_keeps_exactly_keep_weeks_digests(tmp_path):
    _digests(tmp_path, [(2025, w) for w in range(1, 21)])
    rm._rotate_old_digests(tmp_path, keep_weeks=8)
    kept = sorted(p.name for p in tmp_path.glob("*.md"))
    assert kept == [f"2025-W{w:02d}.md" for w in range(13, 21)]
    assert len(list((tmp_path / "archive").glob("*.md"))) == 12


def test_rotate_window_crosses_a_53_week_year(tmp_path):
    # 2020 has a W53: keeping 3 weeks from 2021-W02 is 2021-W02, W01, 2020-W53.
    _digests(tmp_path, [(2020, 52), (2020, 53), (2021, 1), (2021, 2)])
    rm._rotate_old_digests(tmp_path, keep_weeks=3)
    assert sorted(p.name for p in tmp_path.glob("*.md")) == ["2020-W53.md", "2021-W01.md", "2021-W02.md"]


def test_rotate_keep_one_keeps_only_the_latest(tmp_path):
    _digests(tmp_path, [(2025, 9), (2025, 10)])
    rm._rotate_old_digests(tmp_path, keep_weeks=1)
    assert [p.name for p in tmp_path.glob("*.md")] == ["2025-W10.md"]


def test_rotate_refuses_a_window_that_would_archive_the_latest(tmp_path):
    import pytest

    _digests(tmp_path, [(2025, 10)])
    with pytest.raises(ValueError, match="keep_weeks"):
        rm._rotate_old_digests(tmp_path, keep_weeks=0)
    assert (tmp_path / "2025-W10.md").exists()
