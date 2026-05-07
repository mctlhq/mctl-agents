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


def test_detect_chart_double_quoted_version(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/eso.yaml", 'spec:\n  source:\n    targetRevision: "0.10.7"\n', "init")
    _commit(repo, "apps/eso.yaml", 'spec:\n  source:\n    targetRevision: "2.4.0"\n', "bump")

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == [("apps/eso.yaml", "0.10.7", "2.4.0")]


def test_detect_chart_single_quoted_version(tmp_path: Path):
    """Codex P1 on PR mctl-agents#14: single-quoted YAML values are a
    common style in the existing platform-gitops manifests; the guard
    must catch them too or the safety check has a hole."""
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: '0.10.7'\n", "init")
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: '2.4.0'\n", "bump")

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == [("apps/eso.yaml", "0.10.7", "2.4.0")]


def test_detect_no_diff_returns_empty(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _commit(repo, "README.md", "first", "init")
    _commit(repo, "README.md", "second", "noop")
    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == []


def test_detect_multiple_bumps_in_single_hunk(tmp_path: Path):
    """Codex P1 on PR mctl-agents#14: two `targetRevision` replacements
    inside a single diff hunk must be paired in order. With a scalar
    pending_old, the second `-` would clobber the first and the helper
    would mismatch (or miss) the real MAJOR bump.

    Layout: two top-level Applications in the same file, both bumped.
    The first edit is a NON-major bump (0.5 → 0.6 — same major 0); the
    second is a real MAJOR bump (1.x → 2.x). The previous scalar
    implementation paired old2 (1.x) with new1 (0.6) and missed the
    real bump entirely.
    """
    repo = _init_repo(tmp_path)
    initial = (
        "applications:\n"
        "  - name: a\n"
        "    targetRevision: 0.5.0\n"
        "  - name: b\n"
        "    targetRevision: 1.4.0\n"
    )
    bumped = (
        "applications:\n"
        "  - name: a\n"
        "    targetRevision: 0.6.0\n"
        "  - name: b\n"
        "    targetRevision: 2.0.0\n"
    )
    _commit(repo, "apps/multi.yaml", initial, "init")
    _commit(repo, "apps/multi.yaml", bumped, "bumps")

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    # First edit (0.5 → 0.6) is not MAJOR; second (1.4 → 2.0) is.
    assert bumps == [("apps/multi.yaml", "1.4.0", "2.0.0")]


def test_detect_ignores_yaml_comment_lines(tmp_path: Path):
    """Codex P2 on PR mctl-agents#14 (regex too permissive): a YAML
    comment that mentions `targetRevision` must NOT trigger the guard.
    Otherwise comment-only edits would block PR creation."""
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: 0.10.7\n", "init")
    _commit(
        repo,
        "apps/eso.yaml",
        "spec:\n"
        "  source:\n"
        "    # targetRevision: 0.10.7  # MAJOR bump pending — see CRD plan\n"
        "    # Was previously: targetRevision: 2.4.0\n"
        "    targetRevision: 0.10.7\n",
        "comment-only edit",
    )
    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == []


def test_detect_ignores_substring_key_match(tmp_path: Path):
    """Codex P2 on PR mctl-agents#14: a key whose name HAPPENS to end
    in `targetRevision:` (e.g. `customTargetRevision`) is not the
    Helm field we gate on. The anchored regex must reject it."""
    repo = _init_repo(tmp_path)
    _commit(
        repo,
        "apps/custom.yaml",
        "spec:\n  source:\n    customTargetRevision: 0.10.7\n",
        "init",
    )
    _commit(
        repo,
        "apps/custom.yaml",
        "spec:\n  source:\n    customTargetRevision: 2.4.0\n",
        "fake bump on substring key",
    )
    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == []


def test_detect_rejects_4component_pseudo_semver(tmp_path: Path):
    """Codex P2 on PR mctl-agents#14: `1.2.3.4` is not a valid SemVer.
    The previous regex would capture `1.2.3` and silently drop the
    trailing `.4`, which could misclassify a bump. The negative
    lookahead `(?![.0-9])` rejects this shape outright."""
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/x.yaml", "spec:\n  source:\n    targetRevision: 0.10.7\n", "init")
    _commit(repo, "apps/x.yaml", "spec:\n  source:\n    targetRevision: 1.2.3.4\n", "weird")
    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    # The new value is unparseable → cannot be classified as a bump.
    assert bumps == []


def test_detect_cross_hunk_moved_and_bumped(tmp_path: Path):
    """Codex P1 on PR mctl-agents#14: when `targetRevision` is moved
    AND bumped in the same commit, `git diff --unified=0` emits two
    hunks (delete at old line, add at new line). The previous version
    cleared `pending_olds` on every `@@`, so the old value was dropped
    before the add was seen and a real MAJOR bump silently bypassed
    the guard. With the per-file (not per-hunk) FIFO, the bump is
    correctly paired across hunk boundaries.
    """
    repo = _init_repo(tmp_path)
    # v1: targetRevision near top, lots of padding after.
    v1 = "header: x\n" + "targetRevision: 0.10.7\n" + ("padding: line\n" * 30)
    # v2: targetRevision near bottom, padding moved up. Same field,
    # different line — diff with unified=0 produces two hunks.
    v2 = "header: x\n" + ("padding: line\n" * 30) + "targetRevision: 2.4.0\n"
    _commit(repo, "apps/moved.yaml", v1, "init")
    _commit(repo, "apps/moved.yaml", v2, "move + bump")

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    assert bumps == [("apps/moved.yaml", "0.10.7", "2.4.0")]


def test_detect_ignores_uncommitted_working_tree_edits(tmp_path: Path):
    """Codex P2 on PR mctl-agents#14: the guard must reflect what would
    actually be pushed (HEAD), not the dirty working tree. An uncommitted
    scratch edit of `targetRevision` should not fire the guard, because
    `git push` would not deliver it.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: 0.10.7\n", "init")
    _commit(repo, "apps/eso.yaml", "spec:\n  source:\n    targetRevision: 0.11.0\n", "minor bump")
    # Uncommitted MAJOR-bump edit in the working tree only.
    (repo / "apps" / "eso.yaml").write_text(
        "spec:\n  source:\n    targetRevision: 2.4.0\n"
    )

    bumps = _detect_chart_major_bumps(repo, base="HEAD~1")
    # HEAD is 0.11.0 (minor bump, not flagged); the working-tree 2.4.0
    # is uncommitted and must be ignored.
    assert bumps == []
