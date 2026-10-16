"""T12-T14: verdict vocabulary, import boundary, candidate schema (spike #655)."""

import ast
import dataclasses
from pathlib import Path

import pytest
import yaml

from orchestrator.sdd.verify import OUTCOMES, SpecVerdict, verify_change

ROOT = Path(__file__).parent.parent
SDD = ROOT / "orchestrator" / "sdd"
FORBIDDEN = (
    "orchestrator.run_shepherd",
    "orchestrator.proposal_state",
    "orchestrator.temporal",
    "orchestrator.run_implementer",
)


def test_t12_verdict_vocabulary_and_no_authority():
    assert OUTCOMES == ("pass", "fail", "unknown")
    with pytest.raises(ValueError):
        SpecVerdict("merge")
    names = {f.name for f in dataclasses.fields(SpecVerdict)} | set(dir(SpecVerdict))
    assert not [n for n in names if any(w in n.lower() for w in ("merge", "approv", "authoriz"))]
    assert verify_change({}, {}).outcome == "unknown"
    assert verify_change({}, {}, skip_specs=True).outcome == "pass"
    bad = verify_change({}, {"c": "## MODIFIED Requirements\n\n### Requirement: X\nx\n"})
    assert bad.outcome == "fail" and bad.findings


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            out.add(base)
            out |= {f"{base}.{a.name}" for a in node.names}
    return out


def test_t13_import_boundary():
    for f in SDD.glob("*.py"):
        for imp in _imports(f):
            assert not imp.startswith(FORBIDDEN), (f.name, imp)
    for f in (ROOT / "orchestrator").rglob("*.py"):
        if SDD in f.parents:
            continue
        for imp in _imports(f):
            assert not (imp == "orchestrator.sdd" or imp.startswith("orchestrator.sdd.")), (f, imp)


def test_t14_candidate_schema():
    d = ROOT / "tests" / "fixtures" / "sdd" / "schemas" / "mctl-devloop"
    schema = yaml.safe_load((d / "schema.yaml").read_text())
    assert schema["name"] == "mctl-devloop"
    assert isinstance(schema["version"], int) and schema["version"] > 0
    arts = {a["id"]: a for a in schema["artifacts"]}
    assert list(arts) == ["proposal", "specs", "design", "tasks"]
    assert arts["specs"]["generates"].startswith("specs/")
    assert schema["apply"] == {"requires": ["tasks"], "tracks": "tasks.md"}
    for a in arts.values():
        assert (d / "templates" / a["template"]).is_file()
