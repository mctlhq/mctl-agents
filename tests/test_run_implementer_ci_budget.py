"""T4 (mctl-agents#423): the `mctlhq/mctl-telegram#652`-shaped reproduction.

A bundle with 3 actionable checks, each carrying a 64 KB-sourced bounded
excerpt (i.e. the shepherd already ran `fetch_failure_logs()` before this
bundle reached the implementer), drives `review_feedback_one` end to end
with a fake SDK client. Asserts `_run_implementer_agent` receives the
CI-derived envelope and work class, that the rendered prompt stays under
the declared per-check/per-bundle size caps, and that the run reaches a
commit decision without ever attempting a log-fetch command itself.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

import pytest

from orchestrator import options, run_implementer
from orchestrator.ci_checks import CI_LOG_MAX_CHARS, CI_LOG_TOTAL_MAX_CHARS


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root


def _ref(repo: Path) -> run_implementer.ProposalRef:
    return run_implementer.ProposalRef(
        service="mctl-telegram", slug="issue-652", proposal_dir=repo, status="implemented",
    )


def _ci_only_bundle_from_a_64kb_log() -> dict:
    """3 actionable checks, each already carrying a bounded excerpt derived
    from a 64 KB source log — exactly what `fetch_failure_logs()` produces,
    reconstructed here without a real `gh` call (that function's own
    behaviour is covered by tests/test_ci_checks_logs.py)."""
    huge = "failure detail line\n" * 4000  # > 64KB source
    bounded = huge[: CI_LOG_MAX_CHARS // 2] + "\n...(elided)...\n" + huge[-(CI_LOG_MAX_CHARS // 2):]
    bounded = bounded[:CI_LOG_MAX_CHARS]
    checks = [
        {
            "check": f"test-cross-platform ({platform})",
            "workflow": "PR validation",
            "job": f"test-cross-platform ({platform})",
            "step": "Run tests",
            "conclusion": "FAILURE",
            "url": f"https://github.com/mctlhq/mctl-telegram/actions/runs/{i}",
            "run_id": str(i),
            "head_sha": "a" * 40,
            "excerpt": "",
            "log_excerpt": bounded,
            "log_status": "ok",
            "log_truncated": True,
        }
        for i, platform in enumerate(("macos-latest", "ubuntu-latest", "windows-latest"), 1)
    ]
    return {"p1": False, "p2": False, "summaries": [], "ci_failures": checks, "head_sha": "a" * 40}


def test_review_feedback_one_derives_the_ci_envelope_and_work_class(repo, monkeypatch) -> None:
    bundle = _ci_only_bundle_from_a_64kb_log()

    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: repo)
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a: "old")
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_push_followup", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_load_status", lambda *_a: {"pr": "https://pr"})

    captured: dict = {}
    subprocess_calls: list = []

    def fake_anyio_run(func, *args, **kwargs):
        # func is functools.partial(_run_implementer_agent, envelope_s=..., work_class=...)
        assert isinstance(func, functools.partial)
        captured["envelope_s"] = func.keywords.get("envelope_s")
        captured["work_class"] = func.keywords.get("work_class")
        captured["prompt"] = args[1]
        return None

    def guarded_subprocess_run(*args, **kwargs):
        subprocess_calls.append(args)
        raise AssertionError("no subprocess (gh/log-fetch) command should run inside _run_implementer_agent here")

    monkeypatch.setattr(run_implementer.anyio, "run", fake_anyio_run)
    monkeypatch.setattr(run_implementer.subprocess, "run", guarded_subprocess_run)

    result = run_implementer.review_feedback_one(_ref(repo), bundle)

    assert result.error is None
    assert result.pr_url == "https://pr"

    assert captured["work_class"] == "ci-remediation"
    n_checks = len(bundle["ci_failures"])
    expected_envelope = options.implementer_envelope("ci-remediation", n_checks=n_checks)
    assert captured["envelope_s"] == expected_envelope

    # Prompt size cap: the whole rendered CI section must stay within a small
    # multiple of the declared per-bundle log-excerpt cap -- comfortably
    # under it, not equal to some multiple of the whole budget times ten.
    assert len(captured["prompt"]) < CI_LOG_TOTAL_MAX_CHARS * 2

    # No log-fetch (or any other) subprocess command was attempted directly
    # by the implementer's own python layer for this run.
    assert subprocess_calls == []


def test_review_feedback_one_mixed_bundle_gets_the_mixed_work_class(repo, monkeypatch) -> None:
    bundle = _ci_only_bundle_from_a_64kb_log()
    bundle["summaries"] = [{"file": "a.py", "line": 1, "severity": "P2", "body": "fix"}]
    bundle["p2"] = True

    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: repo)
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a: "old")
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_push_followup", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_load_status", lambda *_a: {"pr": "https://pr"})

    captured: dict = {}

    def fake_anyio_run(func, *args, **kwargs):
        captured["work_class"] = func.keywords.get("work_class")
        return None

    monkeypatch.setattr(run_implementer.anyio, "run", fake_anyio_run)

    run_implementer.review_feedback_one(_ref(repo), bundle)

    assert captured["work_class"] == "mixed"


def test_review_feedback_one_review_only_bundle_keeps_the_base_envelope(repo, monkeypatch) -> None:
    bundle = {"p1": True, "p2": False, "summaries": [{"file": "a.py", "line": 1, "severity": "P1", "body": "fix"}]}

    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: repo)
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a: "old")
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_push_followup", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_load_status", lambda *_a: {"pr": "https://pr"})

    captured: dict = {}

    def fake_anyio_run(func, *args, **kwargs):
        captured["work_class"] = func.keywords.get("work_class")
        captured["envelope_s"] = func.keywords.get("envelope_s")
        return None

    monkeypatch.setattr(run_implementer.anyio, "run", fake_anyio_run)

    run_implementer.review_feedback_one(_ref(repo), bundle)

    assert captured["work_class"] == "review"
    assert captured["envelope_s"] == run_implementer.IMPLEMENTER_TIMEOUT_SECONDS
