"""Guard for the #875 gh-aw guardrail comparison report (docs-only spike)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "875-gh-aw-guardrail-comparison.md"
TOKENS = ("ADOPT_PATTERNS", "REFERENCE_ONLY", "NO_ACTION")
STATUSES = {
    "adopt",
    "already-covered",
    "covered-by-807",
    "covered-by-852",
    "not-applicable",
    "not-observed",
}
DIMENSIONS = {
    "safe outputs",
    "permissions",
    "network",
    "sandbox",
    "threat detection",
    "reusable enforcement",
    "interoperability",
}


def _text() -> str:
    return REPORT.read_text(encoding="utf-8")


def _rows() -> list[list[str]]:
    section = _text().split("## 3. Comparison", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == "id" or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def test_status_line() -> None:
    lines = [ln for ln in _text().splitlines() if ln.startswith("STATUS:")]
    assert len(lines) == 1
    value = lines[0].split(":", 1)[1].strip()
    assert value == "DRAFT" or value in TOKENS


def test_rows() -> None:
    rows = _rows()
    assert rows
    for cells in rows:
        assert len(cells) == 7, cells
        assert cells[3], f"empty evidence: {cells[0]}"
        assert cells[4] in STATUSES, f"bad status {cells[4]!r} in {cells[0]}"


def test_dimensions_present() -> None:
    assert DIMENSIONS <= {r[1] for r in _rows()}


def test_adopt_rows_have_follow_up() -> None:
    adopt = [r for r in _rows() if r[4] == "adopt"]
    for cells in adopt:
        assert cells[5] and cells[5] != "none", cells[0]
        assert cells[6] and cells[6] != "none", cells[0]
        assert "Rollback" in cells[6], cells[0]


def test_no_token_shaped_strings() -> None:
    assert not re.search(r"ghp_|gho_|github_pat_|sk-ant-|-----BEGIN", _text())
