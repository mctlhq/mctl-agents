"""Guards for the #796 REA spike PR.

Guard ONLY the spike PR: the report is a draft or carries exactly one
decision token, the spike changed no runtime gate, and committed artefacts
hold no binaries. A follow-up that adopts or rejects REA deletes this file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "796-rea-investigation-evidence.md"
SPIKE = ROOT / "docs" / "spikes" / "796"
TOKENS = ("ADOPT_OPTIONAL_REA_CAPABILITY", "ADOPT_REA_CLI_ADAPTER_ONLY", "RESEARCH_ONLY", "REJECT")
DRAFT_MARKER = "STATUS: DRAFT"
MAX_FILE_BYTES = 256 * 1024
MAGICS = (b"\x7fELF", b"PK\x03\x04", b"dex\n")
GUARDED = ("orchestrator", "config", "agents/_manifests")


def test_report_is_draft_or_has_exactly_one_token() -> None:
    text = REPORT.read_text()
    if DRAFT_MARKER in text:
        return
    # The report documents all four tokens, so look only at the STATUS line.
    m = re.search(r"^STATUS:\s*(\w+)", text, re.MULTILINE)
    assert m, "report needs a STATUS line"
    found = [t for t in TOKENS if t == m.group(1)]
    assert len(found) == 1, f"exactly one decision token expected, got {found}"


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


def test_no_binaries_or_oversized_files_committed() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", str(SPIKE)], cwd=ROOT, text=True, capture_output=True,
    )
    if tracked.returncode != 0:
        pytest.skip("git ls-files unavailable")
    for rel in tracked.stdout.splitlines():
        path = ROOT / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        assert len(data) <= MAX_FILE_BYTES, path
        assert not any(data.startswith(m) for m in MAGICS), path


def test_oracles_parse() -> None:
    oracles = sorted((SPIKE / "fixtures").glob("*/oracle.yaml"))
    assert len(oracles) >= 2
    for oracle in oracles:
        facts = yaml.safe_load(oracle.read_text())["facts"]
        assert facts and all({"id", "statement", "kind"} <= set(f) for f in facts), oracle
