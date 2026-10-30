"""Guards for the #807 agent-isolation spike PR. A follow-up that adopts a decision deletes this file."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "807-agent-isolation-k8s.md"
TOKENS = ("NO_CHANGE", "HARDEN_IN_PLACE", "DEDICATED_AGENT_NAMESPACE", "SANDBOX_RUNTIME_REQUIRED")
GUARDED = ("orchestrator", "config", "agents/_manifests")


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


def test_report_is_draft_or_has_exactly_one_token() -> None:
    assert decision_errors(REPORT.read_text()) == []


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


def test_no_runtime_path_changed() -> None:
    base = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], cwd=ROOT, text=True, capture_output=True)
    if base.returncode != 0:
        pytest.skip("no origin/main to diff against")
    diff = subprocess.run(
        ["git", "diff", "--name-only", base.stdout.strip(), "HEAD", "--", *GUARDED],
        cwd=ROOT, text=True, capture_output=True,
    )
    if diff.returncode != 0:
        pytest.skip("git diff unavailable")
    assert diff.stdout.strip() == "", f"spike must not change guarded paths:\n{diff.stdout}"


def test_matrix_rows_have_source() -> None:
    text = REPORT.read_text()
    section = text.split("## Current-state matrix", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|")][2:]
    assert rows
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        assert cells[3], f"matrix row without source: {ln}"
