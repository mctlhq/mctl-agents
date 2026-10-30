"""Guard for the #830 runtime egress detection report (docs-only spike)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "830-runtime-egress-detection.md"
TOKENS = ("ADOPT_PLAN", "ADOPT_SUBSET", "RESEARCH_ONLY")
STATES = {"emitted", "partial", "missing", "blind", "out-of-scope"}


def _text() -> str:
    return REPORT.read_text(encoding="utf-8")


def _rows() -> list[list[str]]:
    section = _text().split("## 2. Escape-path matrix", 1)[1].split("\n## ", 1)[0]
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


def test_matrix_rows() -> None:
    rows = _rows()
    for cells in rows:
        assert len(cells) == 9, cells
        assert cells[4], f"empty producer: {cells[0]}"
        assert cells[5], f"empty attribution: {cells[0]}"
        assert cells[6] in STATES, f"bad state {cells[6]!r} in {cells[0]}"
    assert {f"X{i}" for i in range(1, 8)} <= {r[0] for r in rows}


def test_cross_references() -> None:
    text = _text()
    for needle in ("#852", "N1", "#807", "G1"):
        assert needle in text
    x2 = next(r for r in _rows() if r[0] == "X2")
    assert "stage C" in x2[7]


def test_follow_ups() -> None:
    text = _text()
    parts = re.split(r"^### (F\d+)\b", text, flags=re.M)[1:]
    assert len(parts) >= 2
    for i in range(0, len(parts), 2):
        body = parts[i + 1].split("\n### ", 1)[0]
        assert re.search(r"Repo: (mctl-agents|mctl-gitops)\b", body), parts[i]
        assert "Rollback" in body, parts[i]


def test_no_token_shaped_strings() -> None:
    assert not re.search(r"ghp_|gho_|github_pat_|sk-ant-|-----BEGIN", _text())
