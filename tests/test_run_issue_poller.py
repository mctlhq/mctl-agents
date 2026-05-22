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
    refs = search_labeled_issues("agents:intake")
    assert [(r.repo, r.number) for r in refs] == [
        ("mctl-telegram", 12),
        ("mctl-api", 4),
    ]


def test_search_labeled_issues_empty(monkeypatch):
    monkeypatch.setattr(run_issue_poller, "_run", lambda cmd: _completed("[]"))
    assert search_labeled_issues("agents:intake") == []


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
    assert removed == [(ref.url, "agents:intake")]


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


# ---------------------------------------------------------------------------
# poll — --max-issues cap
# ---------------------------------------------------------------------------
def _investigate_ok(tmp_path, calls: list):
    """An ``investigate`` stub that records each URL and reports success."""
    def _inv(url, state_dir):
        calls.append(url)
        return InvestigateResult("mctl-telegram", "issue-x", tmp_path)
    return _inv


def test_poll_max_issues_caps_cycle(tmp_path, monkeypatch):
    """More labelled issues than the cap → only the first --max-issues are
    investigated; the rest are never touched, so their label survives."""
    refs = [_ref(number=n) for n in range(1, 9)]  # 8 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    investigated: list = []
    monkeypatch.setattr(run_issue_poller, "investigate", _investigate_ok(tmp_path, investigated))
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))

    assert poll(state_dir=tmp_path, max_issues=3) == 0
    assert investigated == [r.url for r in refs[:3]]
    assert removed == [r.url for r in refs[:3]]


def test_poll_max_issues_zero_disables_cap(tmp_path, monkeypatch):
    """`--max-issues 0` is the operator escape hatch — all issues run."""
    refs = [_ref(number=n) for n in range(1, 9)]  # 8 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    investigated: list = []
    monkeypatch.setattr(run_issue_poller, "investigate", _investigate_ok(tmp_path, investigated))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    assert poll(state_dir=tmp_path, max_issues=0) == 0
    assert investigated == [r.url for r in refs]


def test_poll_max_issues_warns_when_capped(tmp_path, monkeypatch, capsys):
    refs = [_ref(number=n) for n in range(1, 6)]  # 5 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    monkeypatch.setattr(run_issue_poller, "investigate", _investigate_ok(tmp_path, []))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    poll(state_dir=tmp_path, max_issues=2)
    out = capsys.readouterr().out
    assert "WARN:" in out
    assert "--max-issues=2" in out


def test_poll_under_cap_does_not_warn(tmp_path, monkeypatch, capsys):
    """Issue count below the cap → no cap WARN line."""
    refs = [_ref(number=n) for n in range(1, 4)]  # 3 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    monkeypatch.setattr(run_issue_poller, "investigate", _investigate_ok(tmp_path, []))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    poll(state_dir=tmp_path, max_issues=5)
    assert "capping this cycle" not in capsys.readouterr().out


def test_poll_at_cap_does_not_warn(tmp_path, monkeypatch, capsys):
    """Exactly cap issues → no truncation, no warning (guard is strict `>`)."""
    refs = [_ref(number=n) for n in range(1, 4)]  # 3 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    monkeypatch.setattr(run_issue_poller, "investigate", _investigate_ok(tmp_path, []))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    poll(state_dir=tmp_path, max_issues=3)  # exactly at cap
    assert "capping this cycle" not in capsys.readouterr().out


def test_poll_cap_excludes_non_service_issues(tmp_path, monkeypatch):
    """Non-service issues cost no SDK budget — they must not consume the
    --max-issues cap and starve a valid service issue out of the cycle."""
    non_service = [_ref(repo="not-a-service", number=n) for n in range(1, 6)]  # 5
    valid = _ref(repo="mctl-telegram", number=99)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues",
                        lambda label: non_service + [valid])
    investigated: list = []
    monkeypatch.setattr(run_issue_poller, "investigate", _investigate_ok(tmp_path, investigated))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    # cap of 3: the 5 non-service issues must not consume it, so the lone
    # service issue still gets investigated.
    failures = poll(state_dir=tmp_path, max_issues=3)
    assert investigated == [valid.url]
    assert failures == 5  # the 5 non-service issues, label kept


# Keep explicit references so an accidental removal of a public helper trips
# the import at collection time.
assert callable(remove_label)
