"""Unit tests for the chart MAJOR-version guard in
``orchestrator.run_implementer``. Pins behaviour added in B4
(~/.claude/plans/streamed-soaring-fox.md) — block PRs that bump a
Helm chart's MAJOR version unless the proposal explicitly
acknowledges the CRD migration plan via a ``crd-migration*`` file.

Doesn't exercise the SDK / git push paths — those are covered in the
shepherd tests via mocks. Here we hit the helpers directly so the
failure modes are easy to reason about.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.run_implementer import (
    _detect_chart_major_bumps,
    _is_major_bump,
    _proposal_acks_crd_migration,
)


# ---------------------------------------------------------------------------
# _is_major_bump
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "old,new,want",
    [
        ("0.10.7", "2.4.0", True),    # ESO incident shape
        ("1.2.3", "2.0.0", True),     # 1.x → 2.x
        ("1.2.3", "1.3.0", False),    # minor bump
        ("1.2.3", "1.2.4", False),    # patch bump
        ("0.1.0", "0.2.0", False),    # pre-1.0 minor — same major (0)
        ("2.0.0", "1.5.0", False),    # downgrade (revert) — not flagged
        ("garbage", "2.0.0", False),  # unparseable old → defensive False
        ("1.0.0", "garbage", False),  # unparseable new → defensive False
    ],
)
def test_is_major_bump(old, new, want):
    assert _is_major_bump(old, new) is want


# ---------------------------------------------------------------------------
# _proposal_acks_crd_migration
# ---------------------------------------------------------------------------
def test_proposal_acks_crd_migration_finds_marker(tmp_path: Path):
    (tmp_path / "requirements.md").write_text("# spec body")
    (tmp_path / "crd-migration-plan.md").write_text("ack")
    assert _proposal_acks_crd_migration(tmp_path) is True


def test_proposal_acks_crd_migration_case_insensitive(tmp_path: Path):
    (tmp_path / "CRD-MIGRATION.txt").write_text("")
    assert _proposal_acks_crd_migration(tmp_path) is True


def test_proposal_acks_crd_migration_empty_dir(tmp_path: Path):
    assert _proposal_acks_crd_migration(tmp_path) is False


def test_proposal_acks_crd_migration_other_files_only(tmp_path: Path):
    (tmp_path / "requirements.md").write_text("nope")
    (tmp_path / "tasks.md").write_text("nope")
    assert _proposal_acks_crd_migration(tmp_path) is False


def test_proposal_acks_crd_migration_missing_dir(tmp_path: Path):
    # _proposal_acks_crd_migration must not blow up on a missing dir;
    # the orchestrator gates on it before push and a missing proposal
    # dir is itself an error path, but the helper should report
    # "no ack" rather than raise.
    assert _proposal_acks_crd_migration(tmp_path / "does-not-exist") is False


# ---------------------------------------------------------------------------
# _detect_chart_major_bumps
# ---------------------------------------------------------------------------
def _init_repo(tmp_path: Path) -> Path:
    """Initialise a git repo with one committed file at version 0.10.7,
    then return the repo path. The test mutates the file and a second
    commit lands the new version on HEAD; `git diff HEAD~1` exercises
    the helper.
    """
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    return tmp_path


def _commit(repo: Path, path: str, content: str, msg: str) -> None:
    fp = repo / path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)


def test_detect_chart_major_bump_simple(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: 0.10.7\n", "init")
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: 2.4.0\n", "bump")

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == [("apps/eso.yaml", "0.10.7", "2.4.0")]


def test_detect_chart_minor_bump_not_flagged(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: 0.10.7\n", "init")
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: 0.11.0\n", "minor bump")

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == []


def test_detect_chart_quoted_version(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/eso.yaml", 'spec:\n  source:\n    targetRevision: "0.10.7"\n', "init")
    _commit(repo, "apps/eso.yaml", 'spec:\n  source:\n    targetRevision: "2.4.0"\n', "bump")

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == [("apps/eso.yaml", "0.10.7", "2.4.0")]


def test_detect_no_diff_returns_empty(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _commit(repo, "README.md", "first", "init")
    _commit(repo, "README.md", "second", "noop")
    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == []
