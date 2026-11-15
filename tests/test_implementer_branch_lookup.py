"""Exact, fail-loud origin branch lookup in ``run_implementer``.

Bug roadmap W1, agents ch. 3 and 6: ``git ls-remote --heads origin <branch>``

- ignored the exit code, so a failed read (network, auth, a missing remote)
  answered "no such branch" -- in review-feedback mode that is exit 43,
  a deterministic failure charged against the proposal's bounded attempts;
- matched by path suffix, so ``feat/agents-x`` also matched
  ``bob/feat/agents-x`` and returned that branch's SHA.

These run real git against a local bare "origin" rather than mocking the
command, so the matching rule under test is git's own.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import run_implementer as ri
from orchestrator.proc import CommandFailed

BRANCH = "feat/agents-x"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone whose origin is a local bare repo with only `main`."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hi\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return work


def _push_branch(work: Path, name: str, content: str) -> str:
    (work / "f.txt").write_text(content, encoding="utf-8")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-qm", content)
    _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{name}")
    return _git(work, "rev-parse", "HEAD")


def test_exact_branch_is_found(clone: Path) -> None:
    sha = _push_branch(clone, BRANCH, "mine")
    assert ri._remote_head_sha(clone, BRANCH) == sha
    assert ri._branch_exists_on_origin(clone, BRANCH) is True


def test_absent_branch_is_none(clone: Path) -> None:
    assert ri._remote_head_sha(clone, BRANCH) is None
    assert ri._branch_exists_on_origin(clone, BRANCH) is False


def test_suffix_match_is_not_the_branch(clone: Path) -> None:
    """Only `bob/feat/agents-x` exists: `feat/agents-x` does NOT."""
    _push_branch(clone, f"bob/{BRANCH}", "bob's")
    assert ri._remote_head_sha(clone, BRANCH) is None
    assert ri._branch_exists_on_origin(clone, BRANCH) is False


def test_suffix_branch_does_not_shadow_the_real_one(clone: Path) -> None:
    """Both exist: the answer is our branch's SHA, never bob's."""
    bobs = _push_branch(clone, f"bob/{BRANCH}", "bob's")
    mine = _push_branch(clone, BRANCH, "mine")
    assert bobs != mine
    assert ri._remote_head_sha(clone, BRANCH) == mine


def test_unreachable_origin_raises_instead_of_reading_absent(clone: Path, tmp_path: Path) -> None:
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    with pytest.raises(CommandFailed):
        ri._remote_head_sha(clone, BRANCH)
    with pytest.raises(CommandFailed):
        ri._branch_exists_on_origin(clone, BRANCH)


def test_review_feedback_failed_lookup_is_transient_not_branch_missing(
    monkeypatch: Any, clone: Path, tmp_path: Path,
) -> None:
    """End to end in review-feedback mode: a failed `ls-remote` must not map
    to EXIT_BRANCH_MISSING_ON_ORIGIN (43, charged), but to a generic shell
    failure the shepherd treats as transient."""
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    monkeypatch.setattr(ri, "_clone_target", lambda *_a, **_kw: clone)
    monkeypatch.setattr(ri, "_acquire_claim", lambda *_a, **_kw: None)
    ref = ri.ProposalRef(service="mctl-web", slug="x", proposal_dir=clone, status="implemented")
    result = ri.review_feedback_one(ref, {"summaries": []})
    assert result.error
    assert "not found on origin" not in result.error
    assert ri._review_feedback_exit_code(result.error) == ri.EXIT_GENERIC_FAILURE
