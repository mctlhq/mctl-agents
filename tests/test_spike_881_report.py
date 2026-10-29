"""Guard for the #881 pod sandbox runtime spike report and its collect.py."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "881-pod-sandbox-runtime.md"
COLLECT = ROOT / "experiments" / "sandbox-runtime-881" / "collect.py"
TOKENS = {"DRAFT", "KEEP_HARDENED_RUNC", "ADOPT_GVISOR"}
STATUSES = {"measured", "infeasible-no-kvm", "not-exercised"}
CELL = re.compile(r"pass|fail:.+|not-exercised")


def _text() -> str:
    return REPORT.read_text(encoding="utf-8")


def _rows(heading: str) -> list[list[str]]:
    section = _text().split(heading, 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] in ("id", "op") or set(cells[0]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def test_status_line() -> None:
    lines = [ln for ln in _text().splitlines() if ln.startswith("STATUS:")]
    assert len(lines) == 1
    assert lines[0].split(":", 1)[1].strip() in TOKENS


def test_matrix_rows() -> None:
    rows = _rows("## 2. Feasibility matrix")
    assert {"K2", "F1"} <= {r[0] for r in rows}
    for cells in rows:
        assert len(cells) == 8, cells
        assert cells[7], f"empty evidence: {cells[0]}"
        assert cells[6] in STATUSES, f"bad status in {cells[0]}"
        if cells[3] == "yes" and cells[4] != "yes":
            assert cells[6] != "measured", cells[0]


def test_threat_rows() -> None:
    rows = _rows("## 3. Threat-to-control map")
    assert [r[0] for r in rows] == [f"T{i}" for i in range(1, 9)]
    for cells in rows:
        assert len(cells) == 5, cells
        assert cells[3], f"empty sandbox_reduces: {cells[0]}"


def test_scorecard_cells() -> None:
    rows = _rows("## 4. Compatibility scorecard")
    assert rows
    for cells in rows:
        assert len(cells) == 6, cells
        for c in cells[1:5]:
            assert CELL.fullmatch(c), c


def test_no_token_shaped_strings() -> None:
    assert not re.search(r"ghp_|gho_|github_pat_|sk-ant-|-----BEGIN", _text())


def test_collect_canned_output() -> None:
    spec = importlib.util.spec_from_file_location("collect881", COLLECT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    runc = mod.parse_lines('noise\n{"op":"go_test","ok":true,"seconds":1.0,"stderr_tail":""}\n')
    gv = mod.parse_lines('{"op":"go_test","ok":false,"seconds":2.0,"stderr_tail":"ptrace: not permitted"}\n')
    assert runc["go_test"]["ok"] is True
    table = mod.markdown({"runc": runc, "gvisor": gv})
    assert "| go_test | pass | fail:ptrace: not permitted |" in table
    assert mod.cell(None) == "not-exercised"
