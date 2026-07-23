"""Unit tests for the issue-poller (``orchestrator.run_issue_poller``).

The `gh` calls and the investigator itself are mocked — the tests exercise
the search-result filtering and the per-issue label / failure bookkeeping in
``poll`` (the parts unique to the poller; ``investigate`` has its own suite).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from orchestrator import run_issue_poller
from orchestrator.run_issue_investigator import InvestigateResult, IssueRef
from orchestrator.run_issue_poller import (
    RATE_LIMIT_EXIT_CODE,
    PollResult,
    poll,
    remove_label,
    search_labeled_issues,
)


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
    assert poll(state_dir=tmp_path).failures == 0


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
    assert poll(state_dir=tmp_path).failures == 1
    assert removed == []


def test_poll_dry_run_neither_investigates_nor_relabels(tmp_path, monkeypatch):
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [_ref()])
    investigated: list = []
    removed: list = []
    monkeypatch.setattr(run_issue_poller, "investigate",
                        lambda *a, **k: investigated.append(a))
    monkeypatch.setattr(run_issue_poller, "remove_label",
                        lambda url, label: removed.append(url))
    assert poll(state_dir=tmp_path, dry_run=True).failures == 0
    assert investigated == []
    assert removed == []


def test_poll_dry_run_unknown_service_does_not_count_as_failure(tmp_path, monkeypatch):
    """A dry-run is a side-effect-free preview — an unknown-service issue is
    reported but must not inflate the failure count."""
    monkeypatch.setattr(
        run_issue_poller, "search_labeled_issues",
        lambda label: [_ref(repo="not-a-service")],
    )
    assert poll(state_dir=tmp_path, dry_run=True).failures == 0


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
    assert poll(state_dir=tmp_path).failures == 0
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
    assert poll(state_dir=tmp_path).failures == 0
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
    assert poll(state_dir=tmp_path).failures == 1
    assert removed == []


def test_poll_rate_limited_error_counts_toward_rate_limited_failures(tmp_path, monkeypatch):
    """A rate_limited=True InvestigateResult must increment BOTH failures
    and rate_limited_failures — this is what main() uses to tell "the
    account is out of quota" apart from a routine per-issue failure."""
    ref = _ref(number=7)
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(
        run_issue_poller, "investigate",
        lambda url, state_dir: InvestigateResult(
            "mctl-telegram", "issue-7-x", tmp_path,
            error="SDK reported api_error_status=429 (rate/usage limit exhausted): ...",
            rate_limited=True,
        ),
    )
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)
    result = poll(state_dir=tmp_path)
    assert result.failures == 1
    assert result.rate_limited_failures == 1
    assert result.attempted == 1


def test_poll_mixed_failures_do_not_all_count_as_rate_limited(tmp_path, monkeypatch):
    """One rate-limited issue plus one unrelated failure → failures=2 but
    rate_limited_failures=1, so main()'s all-rate-limited check is False."""
    refs = [_ref(number=1), _ref(number=2)]
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    results = iter([
        InvestigateResult(
            "mctl-telegram", "issue-1-x", tmp_path,
            error="api_error_status=429", rate_limited=True,
        ),
        InvestigateResult(
            "mctl-telegram", "issue-2-x", tmp_path,
            error="agent did not write tasks.md",
        ),
    ])
    monkeypatch.setattr(run_issue_poller, "investigate",
                        lambda url, state_dir: next(results))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)
    result = poll(state_dir=tmp_path)
    assert result.failures == 2
    assert result.rate_limited_failures == 1
    assert result.attempted == 2


def test_poll_success_alongside_rate_limited_failure_leaves_attempted_higher(tmp_path, monkeypatch):
    """One issue succeeds, another is rate-limited → `attempted` (2) is
    strictly greater than `rate_limited_failures` (1), which is exactly the
    signal main() needs to NOT treat this as full-account exhaustion
    (Codex P2 on mctl-agents#63 — comparing against `failures` alone would
    have missed this, since failures == rate_limited_failures == 1 here
    too)."""
    refs = [_ref(number=1), _ref(number=2)]
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    results = iter([
        InvestigateResult("mctl-telegram", "issue-1-x", tmp_path),  # success
        InvestigateResult(
            "mctl-telegram", "issue-2-x", tmp_path,
            error="api_error_status=429", rate_limited=True,
        ),
    ])
    monkeypatch.setattr(run_issue_poller, "investigate",
                        lambda url, state_dir: next(results))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)
    result = poll(state_dir=tmp_path)
    assert result.failures == 1
    assert result.rate_limited_failures == 1
    assert result.attempted == 2
    assert result.attempted != result.rate_limited_failures


def test_poll_unknown_service_never_counts_as_rate_limited(tmp_path, monkeypatch):
    """An unknown-service skip never calls investigate() at all — it must
    not be mistaken for a rate-limit signal, and must not inflate
    `attempted` either."""
    monkeypatch.setattr(
        run_issue_poller, "search_labeled_issues",
        lambda label: [_ref(repo="not-a-service")],
    )
    result = poll(state_dir=tmp_path)
    assert result.failures == 1
    assert result.rate_limited_failures == 0
    assert result.attempted == 0


def test_poll_unknown_service_skip_coexists_with_full_rate_limit_exhaustion(tmp_path, monkeypatch):
    """Codex P3 on mctl-agents#63: an unrelated unknown-service skip can
    coexist with EVERY real (known-service) attempt being rate-limited in
    the same cycle. `attempted` only counts the known-service issue, so
    attempted == rate_limited_failures == 1 here — main() correctly still
    treats this as full exhaustion of the real SDK attempt, even though
    `failures` (2) includes the unrelated skip too."""
    refs = [_ref(repo="not-a-service", number=1), _ref(number=2)]
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    monkeypatch.setattr(
        run_issue_poller, "investigate",
        lambda url, state_dir: InvestigateResult(
            "mctl-telegram", "issue-2-x", tmp_path,
            error="api_error_status=429", rate_limited=True,
        ),
    )
    result = poll(state_dir=tmp_path)
    assert result.failures == 2
    assert result.rate_limited_failures == 1
    assert result.attempted == 1
    assert result.attempted == result.rate_limited_failures


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
    assert poll(state_dir=tmp_path).failures == 1


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

    assert poll(state_dir=tmp_path, max_issues=3).failures == 0
    assert investigated == [r.url for r in refs[:3]]
    assert removed == [r.url for r in refs[:3]]


def test_poll_max_issues_zero_disables_cap(tmp_path, monkeypatch):
    """`--max-issues 0` is the operator escape hatch — all issues run."""
    refs = [_ref(number=n) for n in range(1, 9)]  # 8 issues
    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: list(refs))
    investigated: list = []
    monkeypatch.setattr(run_issue_poller, "investigate", _investigate_ok(tmp_path, investigated))
    monkeypatch.setattr(run_issue_poller, "remove_label", lambda url, label: None)

    assert poll(state_dir=tmp_path, max_issues=0).failures == 0
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
    result = poll(state_dir=tmp_path, max_issues=3)
    assert investigated == [valid.url]
    assert result.failures == 5  # the 5 non-service issues, label kept


# ---------------------------------------------------------------------------
# main — rate-limit fallback exit code
# ---------------------------------------------------------------------------
def _run_main(monkeypatch, result: PollResult) -> int:
    """Run main() with poll() stubbed to return `result`; return the exit code."""
    monkeypatch.setattr(sys, "argv", ["run_issue_poller"])
    monkeypatch.setattr(run_issue_poller, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(run_issue_poller, "poll", lambda **kwargs: result)
    with pytest.raises(SystemExit) as exc_info:
        run_issue_poller.main()
    return exc_info.value.code


def test_main_exits_zero_when_nothing_labelled(monkeypatch):
    code = _run_main(monkeypatch, PollResult(failures=0, rate_limited_failures=0, attempted=0))
    assert code == 0


def test_main_exits_zero_when_all_attempts_succeed(monkeypatch):
    code = _run_main(monkeypatch, PollResult(failures=0, rate_limited_failures=0, attempted=3))
    assert code == 0


def test_main_exits_zero_on_partial_unrelated_failures(monkeypatch):
    """Some issues failed, none of them rate-limited — routine soft-failure,
    exit 0 so the cron does not flap on one bad issue."""
    code = _run_main(monkeypatch, PollResult(failures=2, rate_limited_failures=0, attempted=2))
    assert code == 0


def test_main_exits_zero_on_mixed_failures(monkeypatch):
    """Some failures rate-limited, some not — NOT every attempted issue hit
    quota exhaustion, so this must still be the routine soft-failure path."""
    code = _run_main(monkeypatch, PollResult(failures=3, rate_limited_failures=2, attempted=3))
    assert code == 0


def test_main_exits_zero_when_rate_limited_failure_mixed_with_success(monkeypatch):
    """Regression for Codex P2 on mctl-agents#63: `failures ==
    rate_limited_failures` alone is true whenever there are zero
    non-rate-limited FAILURES, even if most attempts actually succeeded —
    4 successes + 1 rate-limited failure must NOT trigger the fallback,
    since the account clearly isn't fully exhausted."""
    code = _run_main(
        monkeypatch,
        PollResult(failures=1, rate_limited_failures=1, attempted=5),
    )
    assert code == 0


def test_main_exits_nonzero_when_all_attempts_rate_limited(monkeypatch):
    """Every issue actually attempted this cycle failed specifically due to
    rate-limit exhaustion (zero successes, zero unrelated failures) →
    distinct non-zero exit so the CronWorkflow's poll-fallback step engages
    instead of masking an exhausted account as a clean run."""
    code = _run_main(
        monkeypatch,
        PollResult(failures=4, rate_limited_failures=4, attempted=4),
    )
    assert code == RATE_LIMIT_EXIT_CODE
    assert code != 0


# Keep explicit references so an accidental removal of a public helper trips
# the import at collection time.
assert callable(remove_label)
