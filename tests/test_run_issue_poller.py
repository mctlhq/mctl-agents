"""Unit tests for the issue-poller (``orchestrator.run_issue_poller``).

The `gh` calls and the investigator itself are mocked — the tests exercise
the search-result filtering and the per-issue label / failure bookkeeping in
``poll`` (the parts unique to the poller; ``investigate`` has its own suite).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from orchestrator import run_issue_poller
from orchestrator.run_issue_investigator import InvestigateResult, IssueRef
from orchestrator.run_issue_poller import poll, remove_label, search_labeled_issues


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _ref(repo: str = "mctl-telegram", number: int = 1) -> IssueRef:
    return IssueRef(
        owner="mctlhq",
        repo=repo,
        number=number,
        url=f"https://github.com/mctlhq/{repo}/issues/{number}",
    )


# ---------------------------------------------------------------------------
# search_labeled_issues
# ---------------------------------------------------------------------------
def test_search_labeled_issues_parses_and_filters(monkeypatch):
    """PRs, non-mctlhq issues and duplicates are dropped; issues survive."""
    payload = json.dumps([
        {"url": "https://github.com/mctlhq/mctl-telegram/issues/12"},
        {"url": "https://github.com/mctlhq/mctl-api/pull/9"},          # a PR
        {"url": "https://github.com/someone-else/other/issues/3"},     # non-mctlhq
        {"url": "https://github.com/mctlhq/mctl-telegram/issues/12"},  # duplicate
        {"url": "https://github.com/mctlhq/mctl-api/issues/4"},
    ])
    monkeypatch.setattr(run_issue_poller, "_run", lambda cmd: _completed(payload))
    refs = search_labeled_issues("agent:investigate")
    assert [(r.repo, r.number) for r in refs] == [
        ("mctl-telegram", 12),
        ("mctl-api", 4),
    ]


def test_search_labeled_issues_empty(monkeypatch):
    monkeypatch.setattr(run_issue_poller, "_run", lambda cmd: _completed("[]"))
    assert search_labeled_issues("agent:investigate") == []


# ---------------------------------------------------------------------------
# poll — global guards
# ---------------------------------------------------------------------------
def test_poll_missing_state_dir_raises(tmp_path):
    with pytest.raises(SystemExit):
        poll(state_dir=tmp_path / "does-not-exist")


def test_poll_no_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [])
    assert poll(state_dir=tmp_path) == 0


# ---------------------------------------------------------------------------
# poll — per-issue bookkeeping
# ---------------------------------------------------------------------------
def test_poll_unknown_service_keeps_label(tmp_path, monkeypatch):
    """A label on a non-service repo counts as a failure, label untouched."""
    monkeypatch.setattr(
        run_issue_poller, "search_labeled_issues",
        lambda label: [_ref(repo="not-a-service")],
    )
    removed: list[str] = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    assert poll(state_dir=tmp_path) == 1
    assert removed == []


def test_poll_dry_run_neither_investigates_nor_relabels(tmp_path, monkeypatch):
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [_ref()])
    investigated: list = []
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "investigate",
                        lambda *a, **k: investigated.append(a))
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    assert poll(state_dir=tmp_path, dry_run=True) == 0
    assert investigated == []
    assert removed == []


def test_poll_dry_run_unknown_service_does_not_count_as_failure(tmp_path, monkeypatch):
    """A dry-run is a side-effect-free preview — an unknown-service issue is
    reported but must not inflate the failure count."""
    monkeypatch.setattr(
        run_issue_poller, "search_labeled_issues",
        lambda label: [_ref(repo="not-a-service")],
    )
    assert poll(state_dir=tmp_path, dry_run=True) == 0


def test_poll_success_removes_label(tmp_path, monkeypatch):
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(
        run_issue_poller, "investigate",
        lambda url, state_dir: InvestigateResult("mctl-telegram", "issue-7-x", tmp_path),
    )
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append((url, label)))
    assert poll(state_dir=tmp_path) == 0
    assert removed == [(ref.url, "agent:investigate")]


def test_poll_skipped_in_flight_still_removes_label(tmp_path, monkeypatch):
    """A proposal already past `proposed` is handled — drop the label."""
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(
        run_issue_poller, "investigate",
        lambda url, state_dir: InvestigateResult(
            "mctl-telegram", "issue-7-x", tmp_path,
            skipped_reason="already at status 'implemented' — refusing to overwrite in-flight work",
        ),
    )
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    assert poll(state_dir=tmp_path) == 0
    assert removed == [ref.url]


def test_poll_error_keeps_label(tmp_path, monkeypatch):
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(
        run_issue_poller, "investigate",
        lambda url, state_dir: InvestigateResult(
            "mctl-telegram", "issue-7-x", tmp_path, error="agent did not write tasks.md",
        ),
    )
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    assert poll(state_dir=tmp_path) == 1
    assert removed == []


def test_poll_label_removal_failure_counts_as_failure(tmp_path, monkeypatch):
    """A failed `gh issue edit` leaves the label on the issue, so the next
    cycle re-investigates — count it as a failure so the broken permission
    surfaces instead of silently burning SDK budget."""
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(
        run_issue_poller, "investigate",
        lambda url, state_dir: InvestigateResult("mctl-telegram", "issue-7-x", tmp_path),
    )

    def _boom(url, label):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="label not found")

    monkeypatch.setattr(run_issue_poller, "remove_label", _boom)
    assert poll(state_dir=tmp_path) == 1


# Keep explicit references so an accidental removal of a public helper trips
# the import at collection time.
assert callable(remove_label)
