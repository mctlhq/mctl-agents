"""Static checks for the #796 operator workflow and the REA env wrapper."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF_PATH = ROOT / ".github" / "workflows" / "rea-spike-796.yml"


def _wf() -> dict[Any, Any]:
    return yaml.safe_load(WF_PATH.read_text())  # type: ignore[no-any-return]


def _steps() -> list[dict[str, Any]]:
    return [s for job in _wf()["jobs"].values() for s in job["steps"]]


def test_rea_env_strips_credentials() -> None:
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret-value",
        "GITHUB_TOKEN": "gh-secret-value",
        "KEEP_ME": "kept-value",
    }
    out = subprocess.run(
        ["sh", str(ROOT / "docs/spikes/796/rea-env.sh"), "env"], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert "oauth-secret-value" not in out
    assert "gh-secret-value" not in out
    assert "kept-value" in out


def test_no_errexit_swallowed_exit_status_capture() -> None:
    """Default `bash -e` aborts before a bare `; rc=$?` capture ever runs."""
    for step in _steps():
        run = step.get("run", "")
        if "set +e" in run:
            continue
        assert not re.search(r"\)\s*;\s*\w+=\$\?|[^|]\s*;\s*rc=\$\?", run), step.get("name")


def test_npm_install_ignores_scripts() -> None:
    for step in _steps():
        for line in step.get("run", "").splitlines():
            if "npm install" in line:
                assert "--ignore-scripts" in line, line


def test_triggers_permissions_concurrency() -> None:
    wf = _wf()
    assert set(wf.get("on", wf.get(True))) == {"workflow_dispatch"}
    assert wf["permissions"] == {}
    for job in wf["jobs"].values():
        assert set(job.get("permissions", {}).items()) <= {("contents", "read")}
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_secrets_pins_checkout_retention() -> None:
    text = WF_PATH.read_text()
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) == {"CLAUDE_CODE_OAUTH_TOKEN"}
    for step in _steps():
        uses = step.get("uses")
        if uses:
            assert re.search(r"@[0-9a-f]{40}$", uses), uses
        if uses and uses.startswith("actions/checkout@"):
            assert step["with"]["persist-credentials"] is False
        if uses and uses.startswith("actions/upload-artifact@"):
            assert step["with"]["retention-days"] == 14


def test_harness_invocation_and_no_writes() -> None:
    runs = "\n".join(s.get("run", "") for s in _steps())
    assert "--allow-live" in runs
    for sub in ("run", "probe-stages", "schema-bytes"):
        assert re.search(rf"\b{sub}\b[^\n]*--allow-live", runs), sub
    assert "${{ inputs." not in runs
    assert "${{ github.event.inputs." not in runs
    for banned in ("git push", "gh pr", "gh issue"):
        assert banned not in runs


def test_no_store_text_and_no_retry() -> None:
    text = WF_PATH.read_text()
    assert "--store-text" not in text
    for step in _steps():
        if step.get("name", "").startswith("Measure"):
            assert "continue-on-error" not in step
            assert not re.search(r"\b(while|until)\b", step["run"])
            assert "retry" not in step["run"].lower().replace("no retries", "")
