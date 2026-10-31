"""entrypoint.sh must exec without writing under /app (readOnlyRootFilesystem)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores read-only mode bits")

# Pre-change script body, kept as a fixture to prove the test detects the regression.
OLD_ENTRYPOINT = """#!/bin/sh
set -e
if [ -n "${STATE_DIR}" ]; then
  mkdir -p "${STATE_DIR}/_mentor/digest"
  for svc_dir in /app/agents/*/; do
    name=$(basename "$svc_dir")
    [ "$name" = "_mentor" ] && continue
    mkdir -p "${STATE_DIR}/${name}/inbox" "${STATE_DIR}/${name}/proposals"
    rm -rf "${svc_dir}inbox" "${svc_dir}proposals"
    ln -s "${STATE_DIR}/${name}/inbox" "${svc_dir}inbox"
    ln -s "${STATE_DIR}/${name}/proposals" "${svc_dir}proposals"
  done
  rm -rf /app/agents/_mentor/digest
  ln -s "${STATE_DIR}/_mentor/digest" /app/agents/_mentor/digest
fi
exec "$@"
"""


def _snapshot(root: Path) -> list[tuple[str, str]]:
    out = []
    for p in sorted(root.rglob("*")):
        kind = f"link:{os.readlink(p)}" if p.is_symlink() else ("dir" if p.is_dir() else "file")
        out.append((str(p.relative_to(root)), kind))
    return out


def _chmod_tree(root: Path, *, dir_mode: int, file_mode: int) -> None:
    for p in [root, *root.rglob("*")]:
        if p.is_symlink():
            continue
        p.chmod(dir_mode if p.is_dir() else file_mode)


def _run(tmp_path: Path, script_text: str) -> tuple[subprocess.CompletedProcess[str], list, list]:
    app = tmp_path / "app"
    shutil.copytree(REPO / "agents", app / "agents", symlinks=True)
    _chmod_tree(app, dir_mode=0o555, file_mode=0o444)
    script = tmp_path / "entrypoint.sh"
    script.write_text(script_text.replace("/app", str(app)))
    state = tmp_path / "state"
    state.mkdir()
    before = _snapshot(app)
    try:
        proc = subprocess.run(
            ["sh", str(script), "sh", "-c", "echo started"],
            env={**os.environ, "STATE_DIR": str(state)},
            capture_output=True, text=True, check=False,
        )
        after = _snapshot(app)
    finally:
        _chmod_tree(app, dir_mode=0o755, file_mode=0o644)
    return proc, before, after


def test_new_entrypoint_execs_without_touching_app(tmp_path):
    proc, before, after = _run(tmp_path, (REPO / "entrypoint.sh").read_text())
    assert proc.returncode == 0, proc.stderr
    assert "started" in proc.stdout
    assert before == after


def test_pre_change_entrypoint_fails_on_read_only_app(tmp_path):
    proc, before, after = _run(tmp_path, OLD_ENTRYPOINT)
    assert proc.returncode != 0 or before != after


def test_entrypoint_has_no_filesystem_writes():
    body = "\n".join(
        line for line in (REPO / "entrypoint.sh").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    for cmd in ("rm ", "ln ", "mkdir"):
        assert cmd not in body
