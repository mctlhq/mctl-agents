"""Static checks for the #796 operator workflow and the REA env wrapper."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
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


def _step_run(prefix: str) -> str:
    for step in _steps():
        if step.get("name", "").startswith(prefix):
            return str(step["run"])
    raise AssertionError(prefix)


def test_grading_template_entries_top_level(tmp_path: Path) -> None:
    run = _step_run("Report and grading template")
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", run.split("grading-template.yaml", 1)[1], re.S)
    assert m
    out = subprocess.run(["python3", "-I", "-"], input=m.group(1), capture_output=True, text=True, check=True).stdout
    grading = yaml.safe_load(out)
    assert "entries" in grading and "grading" not in grading
    assert len(grading["entries"]) == 6
    assert all({"fixture", "arm"} <= set(e) for e in grading["entries"])
    from tools.rea_spike import _score_rows

    (tmp_path / "g.yaml").write_text(out)
    (tmp_path / "r.jsonl").write_text("")
    assert _score_rows(str(tmp_path / "r.jsonl"), str(tmp_path / "g.yaml")) == []
    rec = '{"kind": "run", "fixture": "native-codec", "arm": "baseline", "final": "", "outcome": "failed"}'
    (tmp_path / "r.jsonl").write_text(rec + "\n")
    rows = _score_rows(str(tmp_path / "r.jsonl"), str(tmp_path / "g.yaml"))
    assert len(rows) == 1


def test_rea_identity_bound_to_sha() -> None:
    run = _step_run("Resolve and install REA")
    assert 'npm view "rea-agents@$VERSION" gitHead dist.integrity --json' in run
    assert "rea-identity-mismatch" in run
    assert '"$GITHEAD" != "$SHA"' in run
    assert "npm_git_head" in run and "npm_dist_integrity" in run


def test_rea_bin_on_path_only_for_rea_cli() -> None:
    run = _step_run("Measure")
    assert not re.search(r'^\s*export PATH=', run, re.M)
    paths = [ln for ln in run.splitlines() if "rea-bin" in ln]
    assert len(paths) == 1
    assert 'rpath="$RUNNER_TEMP/rea-bin:$PATH"' in paths[0]
    assert '[ "$arm" = rea-cli ]' in paths[0]
    assert 'PATH="$rpath" uv run' in run
    assert run.count("--fixture-root") == 2
    assert not re.search(r'export\s+REA_SPIKE_MCP_CMD', run)
    assert 'REA_SPIKE_MCP_CMD="$mcp" PATH="$rpath" uv run' in run
    assert '[ "$arm" = rea-mcp ] || [ "$step" = schema-bytes ]' in run
    assert "fixture-stage-failed" in run


def test_stage_fixtures_missing_artifact_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from tools import rea_spike

    src = tmp_path / "src" / "native-codec"
    src.mkdir(parents=True)
    (src / "task.md").write_text("task")  # artifact `codec` missing
    orig = rea_spike.SPIKE_DIR
    rea_spike.SPIKE_DIR = tmp_path / "spike"
    (tmp_path / "spike").mkdir()
    (tmp_path / "spike" / "fixtures").symlink_to(tmp_path / "src")
    try:
        rc = rea_spike._cmd_stage_fixture(argparse.Namespace(dest=str(tmp_path / "d"), fixture="native-codec"))
    finally:
        rea_spike.SPIKE_DIR = orig
    assert rc == 1
    assert "native-codec" in capsys.readouterr().err


def test_staged_runtime_fixture_has_no_oracle_or_sources(tmp_path: Path) -> None:
    from tools import rea_spike

    src = tmp_path / "src"
    for fx, art in rea_spike.RUNTIME_ARTIFACTS.items():
        d = src / fx
        (d / art).parent.mkdir(parents=True)
        (d / art).write_text("bin")
        (d / "task.md").write_text("task")
        (d / "oracle.yaml").write_text("secret")
        (d / "codec.c").write_text("src")
    orig = rea_spike.SPIKE_DIR
    rea_spike.SPIKE_DIR = tmp_path / "spike"
    (tmp_path / "spike").mkdir()
    (tmp_path / "spike" / "fixtures").symlink_to(src)
    try:
        for fx in rea_spike.RUNTIME_ARTIFACTS:
            out = rea_spike.stage_runtime_fixture(fx, tmp_path / "dest")
            names = sorted(p.name for p in out.rglob("*") if p.is_file())
            assert "oracle.yaml" not in names
            assert not any(n.endswith((".c", ".java")) for n in names)
            assert "task.md" in names
    finally:
        rea_spike.SPIKE_DIR = orig


def test_answer_key_removed_before_first_invoke() -> None:
    run = _step_run("Measure")
    rm = run.index('rm -rf "$GITHUB_WORKSPACE/docs/spikes/796/fixtures" "$GITHUB_WORKSPACE/.git"')
    assert run.index("stage-fixtures") < rm
    first_call = re.search(r"^\s*invoke (schema-bytes|run|probe-stages) ", run, re.M)
    assert first_call and rm < first_call.start()
    assert run.index("schema-bytes --allow-live") > rm
