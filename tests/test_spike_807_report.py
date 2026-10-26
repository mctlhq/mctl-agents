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


def test_report_is_draft_or_has_exactly_one_token() -> None:
    text = REPORT.read_text()
    m = re.search(r"^STATUS:\s*(\w+)", text, re.MULTILINE)
    assert m, "report needs a STATUS line"
    if "STATUS: DRAFT" in text:
        return
    assert m.group(1) in TOKENS, f"unknown decision token {m.group(1)!r}"


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
