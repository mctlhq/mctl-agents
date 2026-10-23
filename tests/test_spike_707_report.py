"""Guards for the #707 Pyrefly spike PR.

These tests guard ONLY the spike PR: they assert the spike changed no gate
and that its report and mutation corpus are well-formed. Any approved
follow-up that adds Pyrefly to the repo must delete this file in the same PR.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "707-pyrefly-type-check.md"
MUTATIONS = ROOT / "docs" / "spikes" / "707" / "mutations"
PINNED_SHA = "68f10afcb8a96d7b1c54d5d56819d8e39979c275"
TOKENS = ("REPLACE_MYPY", "PYREFLY_INNER_LOOP_ONLY", "KEEP_MYPY")
DRAFT_MARKER = "STATUS: DRAFT"


def _git(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)


def test_report_has_single_recommendation_and_complete_metrics_table() -> None:
    text = REPORT.read_text()
    if DRAFT_MARKER in text:
        pytest.skip("report is an explicit draft: runner measurements not yet filled in")
    lines = [ln for ln in text.splitlines() if ln.startswith("Recommendation:")]
    assert len(lines) == 1, "exactly one 'Recommendation:' line expected"
    found = [t for t in TOKENS if t in lines[0]]
    assert len(found) == 1, f"exactly one decision token expected, got {found}"
    section = text.split("## Metrics table", 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|")][2:]
    assert rows, "metrics table is empty"
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert all(cells), f"empty cell in metrics row: {row}"
        assert not any(c.lower() in {"tbd", "pending", "todo"} for c in cells), row


def test_gate_files_unchanged() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    data = tomllib.loads(pyproject)
    assert data["tool"]["mypy"]["files"] == ["orchestrator", "config", "tools"]
    dev = data["dependency-groups"]["dev"]
    assert any(d.startswith("mypy") for d in dev)
    assert "pyrefly" not in pyproject.lower()
    assert "pyrefly" not in (ROOT / "uv.lock").read_text().lower()
    assert not (ROOT / "pyrefly.toml").exists()
    workflow = (ROOT / ".github" / "workflows" / "pr-validation.yml").read_text()
    assert "uv run --locked mypy" in workflow


def test_mutation_patches_apply_at_pinned_sha() -> None:
    patches = sorted(MUTATIONS.glob("*.patch"))
    assert len(patches) >= 7
    for patch in patches:
        paths = re.findall(r"^\+\+\+ b/(\S+)", patch.read_text(), re.M)
        assert paths, patch.name
        assert all(p.startswith(("orchestrator/", "config/", "tools/")) for p in paths), patch.name

    if _git("cat-file", "-e", f"{PINNED_SHA}^{{commit}}").returncode != 0:
        _git("fetch", "--depth=1", "origin", PINNED_SHA)
    if _git("cat-file", "-e", f"{PINNED_SHA}^{{commit}}").returncode != 0:
        msg = f"pinned SHA {PINNED_SHA} unavailable"
        if os.environ.get("CI") == "true":
            pytest.fail(msg)
        pytest.skip(msg)

    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt"
        added = _git("worktree", "add", "--detach", str(wt), PINNED_SHA)
        assert added.returncode == 0, added.stderr
        try:
            for patch in patches:
                res = _git("apply", "--check", str(patch), cwd=wt)
                assert res.returncode == 0, f"{patch.name}: {res.stderr}"
        finally:
            _git("worktree", "remove", "--force", str(wt))
