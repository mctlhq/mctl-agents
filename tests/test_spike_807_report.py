"""Guards for the #807 agent-isolation spike report, decided 2026-10-31.

The spike PR's check that no runtime path changed was removed once #810 merged (#916): on any later
branch it could only fail a legitimate change, or skip where origin/main is not fetched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "807-agent-isolation-k8s.md"
TOKENS = ("NO_CHANGE", "HARDEN_IN_PLACE", "DEDICATED_AGENT_NAMESPACE", "SANDBOX_RUNTIME_REQUIRED")


def decision_errors(text: str) -> list[str]:
    """Empty when the report is DRAFT, or names exactly one decision token in its Decision section."""
    statuses = re.findall(r"^STATUS:\s*(\w+)", text, re.MULTILINE)
    if not statuses:
        return ["report needs a STATUS line"]
    if len(set(statuses)) != 1:
        return [f"STATUS lines disagree: {statuses}"]
    if "## Decision" not in text:
        return ["report needs a ## Decision section"]
    section = text.split("## Decision", 1)[1].split("\n## ", 1)[0]
    sec_status = re.findall(r"^STATUS:\s*(\w+)", section, re.MULTILINE)
    if not sec_status:
        return ["Decision section needs a STATUS line"]
    if statuses[0] == "DRAFT":
        return []
    named = sorted({t for t in TOKENS if re.search(rf"\b{t}\b", section)})
    if statuses[0] not in TOKENS:
        return [f"unknown decision token {statuses[0]!r}"]
    if named != [statuses[0]]:
        return [f"Decision section must name exactly one token, found {named}"]
    return []


def test_report_records_exactly_one_token() -> None:
    text = REPORT.read_text()
    assert decision_errors(text) == []
    status = re.search(r"^STATUS:\s*(\w+)", text, re.MULTILINE)
    assert status and status.group(1) in TOKENS, "the decision is recorded; the report must not return to DRAFT"


@pytest.mark.parametrize(("text", "ok"), [
    ("STATUS: DRAFT\n## Decision\nSTATUS: DRAFT. Proposed: `HARDEN_IN_PLACE`, later `NO_CHANGE`.\n", True),
    ("STATUS: HARDEN_IN_PLACE\n## Decision\nSTATUS: HARDEN_IN_PLACE\n", True),
    ("STATUS: HARDEN_IN_PLACE\n## Decision\nSTATUS: NO_CHANGE\n", False),
    ("STATUS: HARDEN_IN_PLACE\n## Decision\nSTATUS: HARDEN_IN_PLACE, then NO_CHANGE\n", False),
    ("STATUS: DRAFT\n## Decision\nSTATUS: NO_CHANGE\n", False),
    ("STATUS: BOGUS\n## Decision\nSTATUS: BOGUS\n", False),
    ("STATUS: NO_CHANGE\n## Decision\nnothing here\n", False),
])
def test_decision_guard_cases(text: str, ok: bool) -> None:
    assert (decision_errors(text) == []) is ok


def test_matrix_rows_have_source() -> None:
    text = REPORT.read_text()
    section = text.split("## Current-state matrix", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|")][2:]
    assert rows
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        assert cells[3], f"matrix row without source: {ln}"
