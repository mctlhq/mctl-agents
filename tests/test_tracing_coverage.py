"""Pin which agent runners are traced (spike #854).

The registry in docs/spikes/854-agent-observability-gaps.md must list every
orchestrator/run_*.py module, its `traced` value must match a static check for
a `tracing.agent_run` call, and every untraced agent runner must be named in a
gap-matrix row. `agent_call` is reviewed by hand. Stdlib only, no network.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "854-agent-observability-gaps.md"
RUNNERS = {p.stem: p for p in (ROOT / "orchestrator").glob("run_*.py")}


def _registry(text: str) -> dict[str, tuple[str, str]]:
    m = re.search(r"```coverage-registry\n(.*?)```", text, re.S)
    assert m, "coverage-registry block missing from the report"
    rows: dict[str, tuple[str, str]] = {}
    for line in m.group(1).splitlines()[1:]:
        if not line.strip():
            continue
        runner, agent_call, traced = (c.strip() for c in line.split("|"))
        assert runner not in rows, f"duplicate registry row: {runner}"
        assert agent_call in ("yes", "no"), f"{runner}: bad agent_call {agent_call!r}"
        assert traced in ("yes", "no"), f"{runner}: bad traced {traced!r}"
        rows[runner] = (agent_call, traced)
    return rows


def _calls_agent_run(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "agent_run":
                if isinstance(f.value, ast.Name) and f.value.id == "tracing":
                    return True
    return False


def test_registry_matches_runner_modules() -> None:
    rows = _registry(REPORT.read_text(encoding="utf-8"))
    missing = sorted(set(RUNNERS) - set(rows))
    stale = sorted(set(rows) - set(RUNNERS))
    assert not missing, f"runner modules missing from the registry: {missing}"
    assert not stale, f"registry names modules that do not exist: {stale}"


def test_traced_column_matches_ast() -> None:
    rows = _registry(REPORT.read_text(encoding="utf-8"))
    for runner, (_, traced) in rows.items():
        if runner not in RUNNERS:
            continue
        actual = "yes" if _calls_agent_run(RUNNERS[runner]) else "no"
        assert traced == actual, f"{runner}: registry traced={traced}, tracing.agent_run call found={actual}"


def test_untraced_agent_runners_have_a_gap() -> None:
    text = REPORT.read_text(encoding="utf-8")
    rows = _registry(text)
    gap_lines = [ln for ln in text.splitlines() if re.match(r"\|\s*G\d+\s*\|", ln)]
    for runner, (agent_call, traced) in rows.items():
        if agent_call == "yes" and traced == "no":
            assert any(runner in ln for ln in gap_lines), (
                f"{runner}: agent runner without tracing has no gap-matrix reference"
            )
