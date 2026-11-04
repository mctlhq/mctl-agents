"""The release hold (`release-hold.json`) is fail-closed and cannot be wired out.

Two halves, because a correct checker nobody runs holds nothing:
- `tools/check_release_hold.py` passes only for an explicit boolean `false`;
- `.github/workflows/release-please.yml` makes the `release-please` job (the
  one that tags, publishes and dispatches the deploy) depend on the
  `release-hold` job, and keeps that job from being skipped around or
  softened.
"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release-please.yml"
CHECKER = ROOT / "tools" / "check_release_hold.py"
HOLD_FILE = ROOT / "release-hold.json"


def _run(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--file", str(path)], capture_output=True, text=True, check=False
    )


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "release-hold.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_explicit_false_releases(tmp_path):
    result = _run(_write(tmp_path, json.dumps({"hold": False})))
    assert result.returncode == 0, result.stdout


def test_hold_true_stops_and_prints_the_reason(tmp_path):
    result = _run(_write(tmp_path, json.dumps({"hold": True, "reason": "M3 open"})))
    assert result.returncode == 1 and "M3 open" in result.stdout


@pytest.mark.parametrize(
    "text",
    [
        '{"hold": "false"}',
        '{"hold": 0}',
        '{"hold": null}',
        '{"hold": []}',
        "{}",
        '{"reason": "no hold key"}',
        "[]",
        "false",
        "null",
        "",
        "{not json",
    ],
)
def test_anything_but_an_explicit_boolean_false_is_a_hold(tmp_path, text):
    """Could not tell is never 'no hold'."""
    assert _run(_write(tmp_path, text)).returncode == 1


def test_a_missing_file_is_a_hold(tmp_path):
    assert _run(tmp_path / "absent.json").returncode == 1


def test_a_directory_in_place_of_the_file_is_a_hold(tmp_path):
    assert _run(tmp_path).returncode == 1


def test_the_committed_hold_file_is_a_valid_answer():
    """It parses to an object with a boolean `hold`; the value itself is a
    decision for a reviewed PR, not for this test."""
    data = json.loads(HOLD_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data["hold"], bool)
    assert isinstance(data.get("reason"), str) and data["reason"]


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_the_release_job_cannot_start_without_the_hold_gate():
    release = _jobs()["release-please"]
    needs = release["needs"]
    assert "release-hold" in ([needs] if isinstance(needs, str) else needs)
    assert "binding-gate" in ([needs] if isinstance(needs, str) else needs)
    assert "if" not in release, "a condition on the release job could skip the dependency check"
    assert not release.get("continue-on-error")


def test_the_hold_gate_cannot_be_softened():
    gate = _jobs()["release-hold"]
    assert not gate.get("continue-on-error")
    assert "if" not in gate, "a job-level condition would let a skipped gate stand for a pass"
    checks = [s for s in gate["steps"] if "check_release_hold.py" in str(s.get("run", ""))]
    assert len(checks) == 1
    (check,) = checks
    assert not check.get("continue-on-error")
    assert check["run"].strip() == "python3 tools/check_release_hold.py"
    # The only condition allowed is "a release would be cut".
    assert check.get("if") == "${{ steps.pending.outputs.cut == 'true' }}"


def test_the_pending_probe_fails_closed_when_it_cannot_list():
    """If the listing fails the gate must run the check, not skip it."""
    script = next(s for s in _jobs()["release-hold"]["steps"] if s.get("id") == "pending")["run"]
    assert "cut=false" in script and script.count("cut=false") == 1
    fallback = script.split("else", 1)[1]
    assert "cut=true" in fallback and "cut=false" not in fallback
    # `cut=false` is written only inside the branch where the listing
    # succeeded and found nothing.
    assert script.index("cut=false") < script.index("else")


def test_the_checker_is_stdlib_only():
    namespace = runpy.run_path(str(CHECKER), run_name="not_main")
    assert callable(namespace["evaluate"])
