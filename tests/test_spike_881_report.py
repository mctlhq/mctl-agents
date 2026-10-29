"""Guard for the #881 pod sandbox runtime spike report and its collect.py."""

from __future__ import annotations

import importlib.util
import json
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
        assert len(cells) == 4, cells
        for c in cells[1:3]:
            assert CELL.fullmatch(c), c


def test_no_token_shaped_strings() -> None:
    assert not re.search(r"ghp_|gho_|github_pat_|sk-ant-|-----BEGIN", _text())


def _collect():
    spec = importlib.util.spec_from_file_location("collect881", COLLECT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(op: str, ok: bool, seconds: float, tail: str = "") -> str:
    return json.dumps({"op": op, "ok": ok, "seconds": seconds, "stderr_tail": tail})


def test_collect_canned_output() -> None:
    mod = _collect()
    runc = mod.parse_lines("noise\n" + _rec("go_test", True, 1.0) + "\n")
    gv = mod.parse_lines(_rec("go_test", False, 2.0, "ptrace: not permitted") + "\n")
    assert runc["go_test"][0]["ok"] is True
    table = mod.markdown({"runc": runc, "gvisor": gv}, runs=1)
    assert "| go_test | pass | fail:1/1 ptrace: not permitted |" in table
    assert mod.cell(None, 1) == "not-exercised"


def test_collect_keeps_every_pass() -> None:
    # A failure in pass 1 must survive nine later passing passes.
    mod = _collect()
    lines = [_rec("npm_ci", False, 3.0, "EACCES")] + [_rec("npm_ci", True, 1.0)] * 9
    recs = mod.parse_lines("\n".join(lines))
    assert len(recs["npm_ci"]) == 10
    assert mod.cell(recs["npm_ci"], 10) == "fail:1/10 EACCES"


def test_collect_missing_pass_is_a_failure() -> None:
    mod = _collect()
    recs = mod.parse_lines("\n".join([_rec("c_compile", True, 1.0)] * 7))
    assert mod.cell(recs["c_compile"], 10) == "fail:3/10 no record from the pass"


def test_collect_performance_medians_and_probes() -> None:
    mod = _collect()
    runc = mod.parse_lines("\n".join(_rec("go_test", True, s) for s in (1.0, 2.0, 3.0)))
    gv = mod.parse_lines(
        "\n".join(
            [_rec("go_test", True, s) for s in (2.0, 2.5, 9.0)] + ['{"probe": "kernel_release", "value": "4.4.0"}']
        )
    )
    assert gv["probe:kernel_release"][0]["value"] == "4.4.0"
    perf = mod.performance({"runc": runc, "gvisor": gv})
    assert "| go_test | 2.000 | 2.500 | +25.0% |" in perf
    # Probe and metric records never appear as scorecard ops.
    assert "kernel_release" not in mod.markdown({"runc": runc, "gvisor": gv}, runs=3)


def test_collect_resources_summary() -> None:
    mod = _collect()
    lines = [json.dumps({"metric": "startup", "seconds": s}) for s in range(1, 11)]
    lines.append(
        json.dumps(
            {
                "metric": "cgroup",
                "cpu_stat": "usage_usec 3000000 user_usec 2000000 ",
                "memory_peak": str(64 * 2**20),
                "io_stat": "8:0 rbytes=1048576 wbytes=1048576 rios=1 ",
            }
        )
    )
    gv = mod.parse_lines("\n".join(lines))
    # A runtime that exposes no cgroup files reports n/a, never zero.
    bare = mod.parse_lines(json.dumps({"metric": "cgroup", "cpu_stat": "unavailable", "memory_peak": "unavailable"}))
    table = mod.resources({"gvisor": gv, "runc": bare})
    assert "| gvisor | 5.0 | 10.0 | 3.0 | 64.0 | 2.0 |" in table
    assert "| runc | n/a | n/a | n/a | n/a | n/a |" in table
