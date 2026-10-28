"""Guard for the #852 sandbox-isolation delta report (docs-only spike)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "852-sandbox-isolation-delta.md"
TOKENS = ("NO_NEW_GAPS", "NEW_GAPS_FOLLOW_UP", "FOLD_INTO_807")
STATUSES = {"new", "covered-by-807", "covered-by-830", "not-observed", "not-a-gap"}


def _text() -> str:
    return REPORT.read_text(encoding="utf-8")


def _gap_rows() -> list[list[str]]:
    section = _text().split("## 3. Delta gap list", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] in ("id",) or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def test_status_line() -> None:
    lines = [ln for ln in _text().splitlines() if ln.startswith("STATUS:")]
    assert len(lines) == 1
    value = lines[0].split(":", 1)[1].strip()
    assert value == "DRAFT" or value in TOKENS


def test_gap_rows() -> None:
    rows = _gap_rows()
    assert rows
    for cells in rows:
        assert len(cells) == 7, cells
        assert cells[3], f"empty evidence: {cells[0]}"
        assert cells[4] in STATUSES, f"bad status {cells[4]!r} in {cells[0]}"


def test_no_token_shaped_strings() -> None:
    assert not re.search(r"ghp_|gho_|github_pat_|sk-ant-|-----BEGIN", _text())


def test_810_sha_and_web_tool_rows() -> None:
    text = _text()
    assert re.search(r"#810.*\b[0-9a-f]{40}\b", text, re.S)
    ids = {r[0] for r in _gap_rows()}
    assert {"D1-WebSearch", "D1-WebFetch"} <= ids


def test_no_runtime_paths_changed() -> None:
    """T3: the spike PR must not touch runtime code, config or manifests."""
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)

    base = git("merge-base", "HEAD", "origin/main")
    if base.returncode != 0:
        pytest.skip("origin/main not available")
    diff = git("diff", "--name-only", base.stdout.strip(), "HEAD", "--",
               "orchestrator", "config", "agents/_manifests")
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == "", f"runtime paths changed: {diff.stdout.split()}"
