"""Pure logic of tools/diagram_facts.py: normalisation, diffing, release watermarks.

facts_from_code() reads real files and is exercised by the diagrams-refresh
workflow itself; what needs pinning here is the part that decides whether a
run reports drift, because a false "no drift" is the failure mode that lets
the diagrams rot silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import diagram_facts as df


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("minutes=30", "30 min"),
        ("days=14", "14 d"),
        ("hours=2", "2 h"),
        ("seconds=30", "30 s"),
        ("hours=1, minutes=30", "1 h 30 min"),
    ],
)
def test_timedelta_text(args: str, expected: str) -> None:
    assert df._timedelta_text(args) == expected


def test_grep_normalises_timedelta_and_reports_missing() -> None:
    text = "MERGE_POLL_INTERVAL = timedelta(minutes=30)\nSHEPHERD_TICKS_MAX = 12\n"
    out = df._grep(
        text,
        {
            "poll": r"^MERGE_POLL_INTERVAL = timedelta\((.+)\)",
            "ticks": r"^SHEPHERD_TICKS_MAX = (\d+)",
            "gone": r"^NOT_THERE = (\d+)",
        },
    )
    assert out == {"poll": "30 min", "ticks": "12", "gone": "<not found>"}


def test_diff_facts_reports_changed_added_and_removed_leaves() -> None:
    recorded = {"dev_loop": {"merge_poll_interval": "30 min", "removed": "x"}, "mcp_tool_count": 71}
    current = {"dev_loop": {"merge_poll_interval": "15 min", "added": "y"}, "mcp_tool_count": 71}
    drift = df.diff_facts(recorded, current)
    assert drift == [
        ("dev_loop.added", None, "y"),
        ("dev_loop.merge_poll_interval", "30 min", "15 min"),
        ("dev_loop.removed", "x", None),
    ]


def test_diff_facts_is_empty_when_equal_regardless_of_key_order() -> None:
    a = {"agents": ["a", "b"], "shepherd": {"max": "3", "min": "1"}}
    b = {"shepherd": {"min": "1", "max": "3"}, "agents": ["a", "b"]}
    assert df.diff_facts(a, b) == []


def test_new_releases_uses_latest_per_repo_and_watermark(tmp_path: Path) -> None:
    releases = tmp_path / "releases.json"
    releases.write_text(
        json.dumps(
            [
                {"repo": "mctlhq/mctl-api", "tag": "1.40.0", "published_at": "2026-09-01T00:00:00Z", "body": ""},
                {"repo": "mctlhq/mctl-api", "tag": "1.41.0", "published_at": "2026-09-02T00:00:00Z", "body": "x"},
                {"repo": "mctlhq/mctl-agents", "tag": "1.38.0", "published_at": "2026-09-02T00:00:00Z", "body": ""},
            ]
        ),
        encoding="utf-8",
    )
    seen = {"mctlhq/mctl-agents": "1.38.0", "mctlhq/mctl-api": "1.40.0"}
    new = df.new_releases(releases, seen)
    assert [(r["repo"], r["tag"]) for r in new] == [("mctlhq/mctl-api", "1.41.0")]


def test_new_releases_without_file_is_empty(tmp_path: Path) -> None:
    assert df.new_releases(tmp_path / "missing.json", {}) == []


def test_render_report_states_no_drift_explicitly() -> None:
    assert "No drift" in df.render_report([], [])


def test_render_report_lists_drift_and_release_notes() -> None:
    report = df.render_report(
        [("dev_loop.merge_poll_interval", "30 min", "15 min")],
        [{"repo": "mctlhq/mctl-api", "tag": "1.41.0", "published_at": "2026-09-02T00:00:00Z", "body": "notes"}],
    )
    assert "`dev_loop.merge_poll_interval` | `30 min` | `15 min`" in report
    assert "mctlhq/mctl-api 1.41.0" in report
    assert "notes" in report
