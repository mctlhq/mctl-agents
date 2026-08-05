"""Unit tests for the issue-poller (``orchestrator.run_issue_poller``).

Phase-5 cutover: the poller no longer calls ``investigate()`` in-process —
it only searches for labelled issues and starts (or attaches to) each one's
DevLoopWorkflow via ``orchestrator.temporal.start.start_dev_loop_workflow``.
The `gh` calls and that Temporal start call are mocked here; DevLoopWorkflow
and the Argo CWFTs it submits have their own suites.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from orchestrator import run_issue_poller
from orchestrator.run_issue_investigator import IssueRef
from orchestrator.run_issue_poller import PollResult, poll, remove_label, search_labeled_issues


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=stdout, stderr="")


def _ref(repo: str = "mctl-telegram", number: int = 1) -> IssueRef:
    return IssueRef(
        owner="mctlhq",
        repo=repo,
        number=number,
        url=f"https://github.com/mctlhq/{repo}/issues/{number}",
    )


def _run(coro):
    return asyncio.run(coro)


async def _start_ok(url: str):
    """A start_dev_loop_workflow stub returning a fake, successful handle."""
    n = url.rstrip("/").rsplit("/", 1)[-1]
    return SimpleNamespace(id=f"dev-loop-mctlhq-x-{n}", result_run_id="run-1")


def _start_ok_recording(calls: list):
    async def _inner(url: str):
        calls.append(url)
        return await _start_ok(url)
    return _inner


async def _start_already_started(url: str):
    raise WorkflowAlreadyStartedError(
        workflow_id="dev-loop-mctlhq-x-1", run_id="run-1", workflow_type="DevLoopWorkflow"
    )


async def _start_boom(url: str):
    raise RuntimeError("temporal frontend unreachable")


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
def test_poll_no_issues(monkeypatch):
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [])
    assert _run(poll()).started == 0
    assert _run(poll()).failures == 0


# ---------------------------------------------------------------------------
# poll — per-issue bookkeeping
# ---------------------------------------------------------------------------
def test_poll_unknown_service_keeps_label(monkeypatch):
    """A label on a non-service repo counts as a failure, label untouched."""
    monkeypatch.setattr(
        run_issue_poller, "search_labeled_issues",
        lambda label: [_ref(repo="not-a-service")],
    )
    removed: list[str] = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    result = _run(poll())
    assert result.failures == 1
    assert result.started == 0
    assert removed == []


def test_poll_dry_run_neither_starts_nor_relabels(monkeypatch):
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [_ref()])
    started: list = []
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok_recording(started))
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    result = _run(poll(dry_run=True))
    assert result.failures == 0
    assert result.started == 0
    assert started == []
    assert removed == []


def test_poll_dry_run_unknown_service_does_not_count_as_failure(monkeypatch):
    """A dry-run is a side-effect-free preview — an unknown-service issue is
    reported but must not inflate the failure count."""
    monkeypatch.setattr(
        run_issue_poller, "search_labeled_issues",
        lambda label: [_ref(repo="not-a-service")],
    )
    assert _run(poll(dry_run=True)).failures == 0


def test_poll_success_removes_label(monkeypatch):
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    started: list = []
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok_recording(started))
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append((url, label)))
    result = _run(poll())
    assert result.failures == 0
    assert result.started == 1
    assert started == [ref.url]
    assert removed == [(ref.url, "agents:intake")]


def test_poll_already_started_still_removes_label(monkeypatch):
    """A prior DevLoopWorkflow for this issue already ran to completion
    (REJECT_DUPLICATE rejects id-reuse against a CLOSED run) — treated as
    already-handled, not a failure, and the label is still dropped."""
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_already_started)
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    result = _run(poll())
    assert result.failures == 0
    assert result.started == 0
    assert removed == [ref.url]


def test_poll_start_error_keeps_label(monkeypatch):
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_boom)
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    result = _run(poll())
    assert result.failures == 1
    assert result.started == 0
    assert removed == []


def test_poll_label_removal_failure_counts_as_failure(monkeypatch):
    """A failed `gh issue edit` leaves the label on the issue — count it as
    a failure so a broken label-write permission surfaces instead of
    silently repeating every cycle."""
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok)

    def _boom(url, label):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="label not found")

    monkeypatch.setattr(run_issue_poller, "remove_label", _boom)
    result = _run(poll())
    assert result.failures == 1
    assert result.started == 1


# ---------------------------------------------------------------------------
# poll — --max-issues cap
# ---------------------------------------------------------------------------
def test_poll_max_issues_caps_cycle(monkeypatch):
    """More labelled issues than the cap → only the first --max-issues are
    started; the rest are never touched, so their label survives."""
    refs = [_ref(number=n) for n in range(1, 9)]  # 8 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    started: list = []
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok_recording(started))
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))

    result = _run(poll(max_issues=3))
    assert result.failures == 0
    assert result.started == 3
    assert started == [r.url for r in refs[:3]]
    assert removed == [r.url for r in refs[:3]]


def test_poll_max_issues_zero_disables_cap(monkeypatch):
    """`--max-issues 0` is the operator escape hatch — all issues run."""
    refs = [_ref(number=n) for n in range(1, 9)]  # 8 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    started: list = []
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok_recording(started))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    result = _run(poll(max_issues=0))
    assert result.failures == 0
    assert started == [r.url for r in refs]


def test_poll_max_issues_warns_when_capped(monkeypatch, capsys):
    refs = [_ref(number=n) for n in range(1, 6)]  # 5 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok)
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    _run(poll(max_issues=2))
    out = capsys.readouterr().out
    assert "WARN:" in out
    assert "--max-issues=2" in out


def test_poll_under_cap_does_not_warn(monkeypatch, capsys):
    """Issue count below the cap → no cap WARN line."""
    refs = [_ref(number=n) for n in range(1, 4)]  # 3 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok)
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    _run(poll(max_issues=5))
    assert "capping this cycle" not in capsys.readouterr().out


def test_poll_at_cap_does_not_warn(monkeypatch, capsys):
    """Exactly cap issues → no truncation, no warning (guard is strict `>`)."""
    refs = [_ref(number=n) for n in range(1, 4)]  # 3 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok)
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    _run(poll(max_issues=3))  # exactly at cap
    assert "capping this cycle" not in capsys.readouterr().out


def test_poll_cap_excludes_non_service_issues(monkeypatch):
    """Non-service issues cost nothing to dispatch, but must still not
    consume the --max-issues cap and starve a valid service issue out of the
    cycle."""
    non_service = [_ref(repo="not-a-service", number=n) for n in range(1, 6)]  # 5
    valid = _ref(repo="mctl-telegram", number=99)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues",
                        lambda label: [*non_service, valid])
    started: list = []
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", _start_ok_recording(started))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    # cap of 3: the 5 non-service issues must not consume it, so the lone
    # service issue still gets dispatched.
    result = _run(poll(max_issues=3))
    assert started == [valid.url]
    assert result.failures == 5  # the 5 non-service issues, label kept
    assert result.started == 1


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _run_main(monkeypatch, result: PollResult) -> int:
    """Run main() with poll() stubbed to return `result`; return the exit code."""
    import sys
    monkeypatch.setattr(sys, "argv", ["run_issue_poller"])

    async def _fake_poll(**kwargs):
        return result

    monkeypatch.setattr(run_issue_poller, "poll", _fake_poll)
    with pytest.raises(SystemExit) as exc_info:
        run_issue_poller.main()
    return exc_info.value.code


def test_main_exits_zero_when_nothing_labelled(monkeypatch):
    assert _run_main(monkeypatch, PollResult(started=0, failures=0)) == 0


def test_main_exits_zero_when_all_started(monkeypatch):
    assert _run_main(monkeypatch, PollResult(started=3, failures=0)) == 0


def test_main_exits_zero_on_partial_failures(monkeypatch):
    """Some issues failed to dispatch — routine soft-failure, exit 0 so the
    cron does not flap on one bad issue."""
    assert _run_main(monkeypatch, PollResult(started=1, failures=2)) == 0


# Keep an explicit reference so an accidental removal of a public helper
# trips the import at collection time.
assert callable(remove_label)
