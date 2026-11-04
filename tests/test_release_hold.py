"""The release hold (`.github/release/hold.json`) is fail-closed and cannot be wired out.

Two halves, because a correct checker nobody runs holds nothing:
- `.github/release/check_hold.py` passes only for an explicit boolean `false`;
- `.github/workflows/release-please.yml` reads the hold in the SAME job that
  runs the release action, as the step right before it, and feeds it to
  `skip-github-release`. There is no earlier "is a release pending?" probe whose
  answer could go stale (a release PR merged between that probe and the action
  would be cut regardless of the hold). The step is run here for real, with a
  fake `gh`, so the fail-closed behaviour is observed, not read off the YAML.
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "release-please.yml"
CHECKER = ROOT / ".github" / "release" / "check_hold.py"
HOLD_FILE = ROOT / ".github" / "release" / "hold.json"


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


def _release_steps() -> list[dict]:
    return _jobs()["release-please"]["steps"]


def _hold_step() -> dict:
    (step,) = [s for s in _release_steps() if s.get("id") == "hold"]
    return step


def _action_step() -> dict:
    (step,) = [s for s in _release_steps() if s.get("id") == "release"]
    return step


def test_the_hold_is_decided_in_the_job_that_runs_the_release_action():
    jobs = _jobs()
    assert "release-hold" not in jobs, "a separate early job is a time-of-check/time-of-use gap"
    release = jobs["release-please"]
    assert "if" not in release and not release.get("continue-on-error")
    steps = release["steps"]
    ids = [s.get("id") for s in steps]
    assert ids.index("hold") < ids.index("release")
    hold = _hold_step()
    assert "if" not in hold and not hold.get("continue-on-error")
    # The decision is unconditional: nothing in it asks whether a release PR is pending.
    assert "autorelease" not in hold["run"] and "gh pr list" not in hold["run"]


def test_the_hold_feeds_skip_github_release_and_nothing_else_is_skipped():
    action = _action_step()
    assert action["uses"].startswith("googleapis/release-please-action@")
    value = action["with"]["skip-github-release"]
    # Only the exact string 'false' lets the action create releases; an empty
    # or missing output (which the action reads as "not skipped") becomes 'true'.
    assert value == "${{ steps.hold.outputs.skip == 'false' && 'false' || 'true' }}"
    assert "skip-github-pull-request" not in action["with"], "release PRs keep updating under a hold"


def test_the_release_action_is_pinned_to_a_commit():
    ref = _action_step()["uses"].split("@", 1)[1]
    # A tag can move; the skip semantics were verified against one commit.
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)


def test_every_step_that_cuts_or_ships_is_gated_on_release_created():
    """With skip-github-release the action never sets release_created, so these stay off."""
    shippers = ("release-deploy.yaml", "publish_agent_release.py", "actions/checkout@v5")
    gated = [s for s in _release_steps() if any(k in str(s.get("run", "")) + str(s.get("uses", "")) for k in shippers)]
    assert len(gated) >= 3
    for step in gated:
        assert "steps.release.outputs.release_created == 'true'" in step["if"], step


def test_the_checkout_that_provides_the_checker_runs_before_the_hold_step():
    ids = [s.get("id") or s.get("uses", "") for s in _release_steps()]
    assert ids[0].startswith("actions/checkout@") and ids.index("hold") == 1


# --- the hold step, run for real ------------------------------------------------


def _run_hold_step(tmp_path: Path, *, tip: str | None, local: str | None, gh_exit: int = 0, checker: bool = True):
    """Run the workflow's own `Read the release hold` script.

    `tip` is what a fake `gh api` prints as hold.json on main's tip (None: the
    call fails), `local` is the checked-out file (None: absent).
    """
    work = tmp_path / "work"
    (work / ".github" / "release").mkdir(parents=True)
    if checker:
        shutil.copy(CHECKER, work / ".github" / "release" / "check_hold.py")
    if local is not None:
        (work / ".github" / "release" / "hold.json").write_text(local, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    body = "exit 1" if tip is None else f"cat <<'EOF_TIP'\n{tip}\nEOF_TIP\nexit {gh_exit}"
    gh.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "output"
    out.write_text("", encoding="utf-8")
    summary = tmp_path / "summary"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_REPOSITORY": "mctlhq/mctl-agents",
        "GH_TOKEN": "x",
    }
    subprocess.run(["bash", "-c", _hold_step()["run"]], cwd=work, env=env, check=True, capture_output=True, text=True)
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1 and lines[0].startswith("skip="), lines  # written exactly once
    return lines[0]


OFF = json.dumps({"hold": False})
ON = json.dumps({"hold": True, "reason": "x"})


def test_both_copies_saying_false_is_the_only_way_to_release(tmp_path):
    assert _run_hold_step(tmp_path, tip=OFF, local=OFF) == "skip=false"


@pytest.mark.parametrize(
    "tip, local",
    [
        (ON, ON),
        (ON, OFF),  # a run for an older commit, hold added on main since
        (OFF, ON),
        (None, OFF),  # tip unreadable (API error)
        ("", OFF),  # empty body
        ("{broken", OFF),
        ('{"hold": "false"}', OFF),
        ("[]", OFF),
        (OFF, None),  # checkout lacks the file
        (OFF, "{broken"),
        (OFF, ""),
    ],
)
def test_any_hold_or_unreadable_copy_skips_the_release(tmp_path, tip, local):
    assert _run_hold_step(tmp_path, tip=tip, local=local) == "skip=true"


def test_a_missing_checker_skips_the_release(tmp_path):
    assert _run_hold_step(tmp_path, tip=OFF, local=OFF, checker=False) == "skip=true"


def test_the_hold_wins_whatever_release_prs_exist(tmp_path):
    """The race: no release PR pending when the run starts, one merged before the
    action. The step does not look, so its answer is the same either way."""
    script = _hold_step()["run"]
    assert "pr list" not in script and "autorelease" not in script
    assert _run_hold_step(tmp_path, tip=ON, local=ON) == "skip=true"


def test_the_checker_is_stdlib_only():
    namespace = runpy.run_path(str(CHECKER), run_name="not_main")
    assert callable(namespace["evaluate"])


def test_a_non_utf8_hold_file_is_a_hold(tmp_path):
    path = tmp_path / "hold.json"
    path.write_bytes(b'{"hold": false, "reason": "\xff\xfe"}')
    result = _run(path)
    # A clean, reported hold, not an uncaught UnicodeDecodeError that merely exits non-zero.
    assert result.returncode == 1 and "Traceback" not in result.stderr and "::error::" in result.stdout
