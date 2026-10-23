"""Pre-push type check (mctl-agents#785)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from config.settings import TYPECHECK_COMMANDS
from orchestrator import implementation_completion as ic
from orchestrator import run_implementer as ri
from orchestrator.exec_budget import CommandBudgetLedger


def _proc(rc: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git" / "info").mkdir(parents=True)
    return tmp_path


def _fake(monkeypatch, results):
    calls: list[list[str]] = []

    def run(cmd, **kw):
        calls.append(cmd)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ri.subprocess, "run", run)
    return calls


def test_seeded_error_is_fail(monkeypatch, repo):
    calls = _fake(monkeypatch, [_proc(0), _proc(1, "x.py:1: error: bad\nFound 1 error in 1 file\n")])
    tc = ri._run_pre_push_typecheck(repo, "mctl-agents", None)
    assert (tc.result, tc.classification, tc.error_count) == ("fail", "exit_nonzero", 1)
    assert calls[1] == ["uv", "run", "--locked", "--no-sync", "mypy"]


def test_child_env_carries_no_orchestrator_secrets(monkeypatch, repo):
    # `uv sync` and mypy run code from the clone; the orchestrator's tokens
    # must not reach them (claude P2 on #787).
    for k in ("GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SOME_FUTURE_SECRET"):
        monkeypatch.setenv(k, "s3cret")
    monkeypatch.setenv("VIRTUAL_ENV", "/app/.venv")
    monkeypatch.setenv("PATH", "/app/.venv/bin:/usr/local/bin:/usr/bin")
    monkeypatch.setenv("HOME", "/home/agent")
    envs: list[dict[str, str]] = []

    def run(cmd, **kw):
        envs.append(kw["env"])
        return _proc(0, "Success: no issues found")

    monkeypatch.setattr(ri.subprocess, "run", run)
    ri._run_pre_push_typecheck(repo, "mctl-agents", None)
    assert len(envs) == 2
    for env in envs:
        assert "s3cret" not in env.values()
        assert "VIRTUAL_ENV" not in env
        assert env["PATH"] == "/usr/local/bin:/usr/bin"
        assert env["HOME"] == "/home/agent"


def test_non_uv_command_runs_as_single_step(monkeypatch, repo):
    # A registered command that is not `uv run --locked` has no install step
    # (agy P2 on #787): its own exit code is the result.
    monkeypatch.setitem(TYPECHECK_COMMANDS, "svc", ("python", "-m", "mypy"))
    calls = _fake(monkeypatch, [_proc(1, "x.py:1: error: bad\nFound 2 errors in 1 file\n")])
    tc = ri._run_pre_push_typecheck(repo, "svc", None)
    assert calls == [["python", "-m", "mypy"]]
    assert (tc.result, tc.error_count) == ("fail", 2)
    _fake(monkeypatch, [_proc(0, "Success: no issues found")])
    assert ri._run_pre_push_typecheck(repo, "svc", None).result == "pass"


def test_error_count_comes_from_summary_line(monkeypatch, repo):
    out = 'x.py:1: error: bad  [misc]\n    s = "Found 9 errors in 9 files"\nFound 1 error in 1 file\n'
    _fake(monkeypatch, [_proc(0), _proc(1, out)])
    tc = ri._run_pre_push_typecheck(repo, "mctl-agents", None)
    assert (tc.error_count, tc.summary) == (1, "Found 1 error")


def test_venv_path_with_trailing_slash_is_dropped(monkeypatch):
    monkeypatch.setenv("PATH", "/app/.venv/bin/:/usr/bin")
    assert ri._typecheck_env()["PATH"] == "/usr/bin"


def test_clean_tree_is_pass(monkeypatch, repo):
    _fake(monkeypatch, [_proc(0), _proc(0, "Success: no issues found")])
    tc = ri._run_pre_push_typecheck(repo, "mctl-agents", None)
    assert (tc.result, tc.classification) == ("pass", "exit_zero")


def test_unregistered_service_runs_nothing(monkeypatch, repo):
    calls = _fake(monkeypatch, [])
    tc = ri._run_pre_push_typecheck(repo, "mctl-web", None)
    assert (tc.result, tc.classification) == ("not_run", "not_applicable")
    assert calls == []
    assert ri._typecheck_pr_line(tc) == ""


@pytest.mark.parametrize(
    "results,expected",
    [
        ([subprocess.TimeoutExpired("uv", 1)], ("unknown", "timeout")),
        ([_proc(2, "network down")], ("unknown", "observation_failed")),
        ([_proc(0), OSError("no uv")], ("unknown", "observation_failed")),
    ],
)
def test_incomplete_never_pass(monkeypatch, repo, results, expected):
    _fake(monkeypatch, list(results))
    tc = ri._run_pre_push_typecheck(repo, "mctl-agents", None)
    assert (tc.result, tc.classification) == expected
    assert "passed" not in ri._typecheck_pr_line(tc)


def test_budget_exhausted(monkeypatch, repo):
    calls = _fake(monkeypatch, [])
    ledger = CommandBudgetLedger()
    ledger.record_denied_exhausted("x")
    tc = ri._run_pre_push_typecheck(repo, "mctl-agents", ledger)
    assert (tc.result, tc.classification) == ("not_run", "budget_denied")
    assert calls == []


def test_excludes_artefacts(monkeypatch, repo):
    _fake(monkeypatch, [_proc(0), _proc(0)])
    ri._run_pre_push_typecheck(repo, "mctl-agents", None)
    lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".venv/" in lines and ".mypy_cache/" in lines


def test_pr_lines():
    assert ri._typecheck_pr_line(ri.TypecheckResult("pass", "exit_zero")) == "Pre-push mypy: passed"
    assert "FAILED (3 errors)" in ri._typecheck_pr_line(ri.TypecheckResult("fail", "exit_nonzero", 3))


def test_verification_record_is_non_required():
    obs = ri._AttemptObservations(phase="implement", branch="b")
    obs.typecheck = ri.TypecheckResult("fail", "exit_nonzero", 2)
    recs = ri._verification_records(ic.OUTCOME_COMPLETE, "", obs)
    tc = [r for r in recs if r.check_id == "typecheck"]
    assert len(tc) == 1 and tc[0].required is False and tc[0].result == "fail"
    assert "typecheck" not in ic.REQUIRED_CHECKS_V1ALPHA1


def test_registry_matches_ci():
    wf = (Path(__file__).resolve().parent.parent / ".github/workflows/pr-validation.yml").read_text()
    m = re.search(r"name: Run mypy\n(?:\s*#.*\n)*\s*run: (.+)", wf)
    assert m and tuple(m.group(1).split()) == TYPECHECK_COMMANDS["mctl-agents"]


def test_prompt_mentions_mypy_only_for_registered():
    def ref(service):
        return ri.ProposalRef(service=service, slug="s", proposal_dir=Path("/p"), status="accepted")

    assert "uv run --locked mypy" in ri._build_prompt(ref("mctl-agents"))
    assert "mypy" not in ri._build_prompt(ref("mctl-web"))
