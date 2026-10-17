"""Spike #661: two canonical capability specs and their replayed history.

`docs/spikes/661/specs/<cap>/spec.md` is the capability as the code stands;
`docs/spikes/661/history/<cap>/` holds the spec as it stood before the oldest
change (if any) and one delta per behaviour change since, in date order.
Replaying every delta through `archive_change` must land exactly on the
canonical spec, and every code path and test the specs cite must exist, so a
rename or a behaviour change that skips the spec turns this red.
"""

import datetime as dt
import re
import shutil
from pathlib import Path

import pytest

from orchestrator.sdd.apply import archive_change
from orchestrator.sdd.spec_model import normalize_block, parse_spec
from orchestrator.sdd.validate import errors, validate_spec

REPO = Path(__file__).resolve().parent.parent
SPIKE = REPO / "docs" / "spikes" / "661"
CAPS = sorted(p.name for p in (SPIKE / "specs").iterdir() if p.is_dir())
_TEST_REF = re.compile(r"`(tests/[\w/]+\.py)(?:::(\w+))?`")
_CODE_REF = re.compile(r"`(orchestrator/[\w/]+\.py)`(?: `(\w+)`)?")


def _blocks(text: str) -> dict[str, str]:
    return {r.name: normalize_block(r.text) for r in parse_spec(text).requirements}


def test_there_are_two_capabilities():
    assert CAPS == ["devloop-approval", "shepherd-merge-gate"]


@pytest.mark.parametrize("cap", CAPS)
def test_the_canonical_spec_is_valid(cap):
    assert errors(validate_spec(cap, (SPIKE / "specs" / cap / "spec.md").read_text())) == []


@pytest.mark.parametrize("cap", CAPS)
def test_replaying_the_history_lands_on_the_canonical_spec(cap, tmp_path):
    history = SPIKE / "history" / cap
    if (history / "baseline").is_dir():
        shutil.copytree(history / "baseline", tmp_path, dirs_exist_ok=True)
    (tmp_path / "specs").mkdir(exist_ok=True)
    changes = sorted(p.name for p in (history / "changes").iterdir() if p.is_dir())
    assert len(changes) >= 2
    for change in changes:
        shutil.copytree(history / "changes" / change, tmp_path / "changes" / change)
        archive_change(tmp_path, change, today=dt.date(2026, 10, 17))
    replayed = (tmp_path / "specs" / cap / "spec.md").read_text()
    assert _blocks(replayed) == _blocks((SPIKE / "specs" / cap / "spec.md").read_text())


@pytest.mark.parametrize("cap", CAPS)
def test_every_citation_resolves(cap):
    text = (SPIKE / "specs" / cap / "spec.md").read_text()
    missing = []
    for path, name in _TEST_REF.findall(text):
        source = REPO / path
        if not source.is_file() or (name and not re.search(rf"def {name}\b", source.read_text())):
            missing.append(f"{path}::{name}" if name else path)
    for path, symbol in _CODE_REF.findall(text):
        source = REPO / path
        if not source.is_file() or (symbol and symbol not in source.read_text()):
            missing.append(f"{path} {symbol}".strip())
    assert missing == []


@pytest.mark.parametrize("cap", CAPS)
def test_every_requirement_cites_its_code_and_every_scenario_a_test(cap):
    spec = parse_spec((SPIKE / "specs" / cap / "spec.md").read_text())
    for req in spec.requirements:
        assert "Enforced by `" in req.text, req.name
        for scenario in re.split(r"(?m)^#### ", req.text)[1:]:
            assert "Pinned by `tests/" in scenario, (req.name, scenario.splitlines()[0])
