"""Artifact discovery (orchestrator.run_usage_collector.list_artifacts)."""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from orchestrator import run_usage_collector as collector

CUTOFF = datetime(2026, 9, 20, tzinfo=UTC)


def _proc(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _row(artifact_id: int, created_at: str, *, expired: bool = False) -> dict:
    return {"id": artifact_id, "created_at": created_at, "expired": expired}


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch):
    """Every test in this module supplies its own `_run_gh` stub; a stray
    real `gh` invocation should fail loudly rather than hit the network."""
    def _boom(args):
        raise AssertionError(f"unexpected real gh call: {args}")
    monkeypatch.setattr(collector, "_run_gh", _boom)


def test_single_page_in_window(monkeypatch):
    rows = [_row(1, "2026-09-25T10:00:00Z")]
    monkeypatch.setattr(collector, "_run_gh", lambda args: _proc(json.dumps({"artifacts": rows})))
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
    assert [a.id for a in found] == [1]


def test_listing_call_carries_no_field_flag_so_gh_defaults_to_get(monkeypatch):
    """`gh api` defaults to GET only when it has no `-f`/`-F` fields to send
    — any field flag on an unmethoded call makes `gh` issue a POST instead,
    which silently fails artifact discovery. The listing call must pass its
    parameters via the endpoint's own query string, never via `-f`/`-F`."""
    captured: list[list[str]] = []

    def _fake(args):
        captured.append(args)
        return _proc(json.dumps({"artifacts": []}))

    monkeypatch.setattr(collector, "_run_gh", _fake)
    collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=1)
    assert len(captured) == 1
    assert not any(token in ("-f", "-F") for token in captured[0])
    assert any("name=" in token and "per_page=" in token and "page=" in token for token in captured[0])


def test_pagination_across_full_pages(monkeypatch):
    """A full first page (100 rows) is followed by a second, short page —
    both must be read."""
    page1 = [_row(i, "2026-09-25T10:00:00Z") for i in range(100)]
    page2 = [_row(200, "2026-09-25T10:00:00Z")]
    calls: list[int] = []

    def _fake(args):
        page = int(next(part.split("=", 1)[1] for a in args for part in a.split("&") if part.startswith("page=")))
        calls.append(page)
        rows = page1 if page == 1 else page2
        return _proc(json.dumps({"artifacts": rows}))

    monkeypatch.setattr(collector, "_run_gh", _fake)
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
    assert calls == [1, 2]
    assert len(found) == 101


def test_max_pages_caps_reading_even_with_full_pages(monkeypatch):
    calls: list[int] = []

    def _fake(args):
        page = int(next(part.split("=", 1)[1] for a in args for part in a.split("&") if part.startswith("page=")))
        calls.append(page)
        rows = [_row(page * 1000 + i, "2026-09-25T10:00:00Z") for i in range(100)]
        return _proc(json.dumps({"artifacts": rows}))

    monkeypatch.setattr(collector, "_run_gh", _fake)
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=2)
    assert calls == [1, 2]
    assert len(found) == 200


def test_expired_artifact_is_skipped(monkeypatch):
    rows = [_row(1, "2026-09-25T10:00:00Z", expired=True), _row(2, "2026-09-25T11:00:00Z")]
    monkeypatch.setattr(collector, "_run_gh", lambda args: _proc(json.dumps({"artifacts": rows})))
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
    assert [a.id for a in found] == [2]


def test_out_of_window_entry_before_in_window_entry_on_same_page(monkeypatch):
    """The listing is ordered by artifact id (upload order), not by
    created_at — an out-of-window row can be listed before an in-window one.
    Must not break early on the first out-of-window row."""
    rows = [
        _row(1, "2026-09-10T23:51:00Z"),  # out of window, listed first
        _row(2, "2026-09-25T23:43:00Z"),  # in window, listed second
    ]
    monkeypatch.setattr(collector, "_run_gh", lambda args: _proc(json.dumps({"artifacts": rows})))
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
    assert [a.id for a in found] == [2]


def test_empty_listing(monkeypatch):
    monkeypatch.setattr(collector, "_run_gh", lambda args: _proc(json.dumps({"artifacts": []})))
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
    assert found == []


def test_malformed_listing_json_is_handled_without_raising(monkeypatch):
    monkeypatch.setattr(collector, "_run_gh", lambda args: _proc("not json"))
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
    assert found == []


def test_listing_missing_artifacts_key_is_handled_without_raising(monkeypatch):
    monkeypatch.setattr(collector, "_run_gh", lambda args: _proc(json.dumps({"totally": "unexpected"})))
    found = collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
    assert found == []


def test_gh_command_failure_propagates(monkeypatch):
    """A genuine gh failure (auth, network) is NOT swallowed here — the
    caller (collect()) is what turns this into a counted per-repository
    failure; list_artifacts itself must let it through."""
    def _fail(args):
        raise collector.CommandFailed(1, ["gh", *args], output="", stderr="401 Bad credentials")
    monkeypatch.setattr(collector, "_run_gh", _fail)
    with pytest.raises(collector.CommandFailed):
        collector.list_artifacts("mctlhq/.github", CUTOFF, max_pages=5)
