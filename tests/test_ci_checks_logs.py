"""Unit tests for `orchestrator.ci_checks`'s bounded CI-log retrieval
(mctl-agents#423): T1.

Covers the per-fetch timeout being passed through to `run_capturing`, a
timed-out fetch degrading to `log_status="timeout"` without raising, the
per-bundle time/count budget marking later blockers `"skipped-budget"`, the
head+tail truncation of an oversized payload, the bundle-wide character cap,
and the `SHEPHERD_CI_LOG_FETCH` kill switch.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from orchestrator import ci_checks
from orchestrator.ci_checks import CheckBlocker, fetch_failure_logs

HEAD_SHA = "a" * 40


def _blocker(*, name: str = "lint", check_run_id: str | None = "555", run_id: str | None = "999") -> CheckBlocker:
    return CheckBlocker(
        name=name,
        workflow="PR validation",
        job=name,
        step="Run mypy",
        conclusion="FAILURE",
        url="https://github.com/mctlhq/mctl-web/actions/runs/999",
        run_id=run_id,
        head_sha=HEAD_SHA,
        excerpt="orchestrator/x.py:1: error: bad",
        kind="actionable",
        required=True,
        check_run_id=check_run_id,
    )


def test_fetch_uses_the_job_scoped_route_with_an_explicit_timeout() -> None:
    calls = []

    def fake_run_capturing(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="log body", stderr="")

    with patch.object(ci_checks, "run_capturing", side_effect=fake_run_capturing):
        result = fetch_failure_logs("mctlhq/mctl-web", (_blocker(),))

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["gh", "api", "repos/mctlhq/mctl-web/actions/jobs/555/logs"]
    assert kwargs["timeout"] == ci_checks.SHEPHERD_CI_LOG_TIMEOUT_SECONDS
    assert result[0].log_status == "ok"
    assert result[0].log_excerpt == "log body"


def test_fetch_falls_back_to_run_view_when_there_is_no_check_run_id() -> None:
    calls = []

    def fake_run_capturing(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="fallback log", stderr="")

    blocker = _blocker(check_run_id=None, run_id="999")
    with patch.object(ci_checks, "run_capturing", side_effect=fake_run_capturing):
        result = fetch_failure_logs("mctlhq/mctl-web", (blocker,))

    assert calls == [["gh", "run", "view", "999", "--log-failed"]]
    assert result[0].log_status == "ok"


def test_a_status_context_blocker_with_neither_id_is_unavailable_with_no_gh_call() -> None:
    calls = []
    blocker = _blocker(check_run_id=None, run_id=None)
    with patch.object(ci_checks, "run_capturing", side_effect=lambda *a, **k: calls.append(a) or None):
        result = fetch_failure_logs("mctlhq/mctl-web", (blocker,))
    assert calls == []
    assert result[0].log_status == "unavailable"
    assert result[0].log_excerpt == ""


def test_timeout_degrades_without_raising() -> None:
    def fake_run_capturing(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    with patch.object(ci_checks, "run_capturing", side_effect=fake_run_capturing):
        result = fetch_failure_logs("mctlhq/mctl-web", (_blocker(),))

    assert result[0].log_status == "timeout"
    assert result[0].log_excerpt == ""


def test_a_nonzero_gh_exit_degrades_to_unavailable_and_tries_the_fallback() -> None:
    from orchestrator.proc import CommandFailed

    calls = []

    def fake_run_capturing(cmd, **kwargs):
        calls.append(cmd)
        if "api" in cmd:
            raise CommandFailed(1, cmd, output="", stderr="404")
        return subprocess.CompletedProcess(cmd, 0, stdout="fallback ok", stderr="")

    with patch.object(ci_checks, "run_capturing", side_effect=fake_run_capturing):
        result = fetch_failure_logs("mctlhq/mctl-web", (_blocker(),))

    assert len(calls) == 2
    assert result[0].log_status == "ok"
    assert result[0].log_excerpt == "fallback ok"


def test_per_bundle_check_count_budget_marks_later_blockers_skipped() -> None:
    blockers = tuple(_blocker(name=f"check-{i}", check_run_id=str(i)) for i in range(ci_checks.CI_LOG_MAX_CHECKS + 2))

    with patch.object(
        ci_checks, "run_capturing",
        side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr=""),
    ):
        result = fetch_failure_logs("mctlhq/mctl-web", blockers)

    fetched = [b for b in result if b.log_status == "ok"]
    skipped = [b for b in result if b.log_status == "skipped-budget"]
    assert len(fetched) == ci_checks.CI_LOG_MAX_CHECKS
    assert len(skipped) == 2


def test_per_bundle_wall_clock_budget_marks_later_blockers_skipped() -> None:
    """A fake clock that jumps past SHEPHERD_CI_LOG_BUDGET_SECONDS after the
    first fetch — the second blocker must never even attempt a fetch."""
    blockers = (_blocker(name="a", check_run_id="1"), _blocker(name="b", check_run_id="2"))
    clock = iter([0.0, 0.0, ci_checks.SHEPHERD_CI_LOG_BUDGET_SECONDS + 1])
    calls = []

    def fake_run_capturing(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    with patch.object(ci_checks, "run_capturing", side_effect=fake_run_capturing):
        result = fetch_failure_logs("mctlhq/mctl-web", blockers, now=lambda: next(clock))

    assert len(calls) == 1
    assert result[0].log_status == "ok"
    assert result[1].log_status == "skipped-budget"


def test_a_64kb_payload_is_bounded_with_head_tail_and_an_elision_marker() -> None:
    huge = "HEAD" * 100 + "MIDDLE" * 15000 + "TAIL" * 100  # well over 64KB
    assert len(huge) > 64_000

    with patch.object(
        ci_checks, "run_capturing",
        side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=huge, stderr=""),
    ):
        result = fetch_failure_logs("mctlhq/mctl-web", (_blocker(),))

    blocker = result[0]
    assert blocker.log_truncated is True
    assert len(blocker.log_excerpt) <= ci_checks.CI_LOG_MAX_CHARS
    assert blocker.log_excerpt.startswith("HEAD")
    assert blocker.log_excerpt.rstrip().endswith("TAIL")
    assert "bytes elided" in blocker.log_excerpt
    assert blocker.log_bytes == len(huge.encode("utf-8"))


def test_bundle_wide_total_char_cap_is_never_exceeded() -> None:
    payload = "x" * (ci_checks.CI_LOG_MAX_CHARS)
    blockers = tuple(_blocker(name=f"c{i}", check_run_id=str(i)) for i in range(ci_checks.CI_LOG_MAX_CHECKS))

    with patch.object(
        ci_checks, "run_capturing",
        side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr=""),
    ):
        result = fetch_failure_logs("mctlhq/mctl-web", blockers)

    total = sum(len(b.log_excerpt) for b in result)
    assert total <= ci_checks.CI_LOG_TOTAL_MAX_CHARS


def test_shepherd_ci_log_fetch_kill_switch_skips_retrieval_entirely(monkeypatch) -> None:
    monkeypatch.setattr(ci_checks, "SHEPHERD_CI_LOG_FETCH", False)
    calls = []
    blockers = (_blocker(),)

    with patch.object(ci_checks, "run_capturing", side_effect=lambda *a, **k: calls.append(a)):
        result = fetch_failure_logs("mctlhq/mctl-web", blockers)

    assert calls == []
    assert result == blockers


def test_fetch_failure_logs_on_an_empty_tuple_is_a_no_op() -> None:
    assert fetch_failure_logs("mctlhq/mctl-web", ()) == ()


def test_bound_log_never_exceeds_max_chars_even_for_a_tiny_cap() -> None:
    excerpt, truncated = ci_checks._bound_log("x" * 1000, 10)
    assert len(excerpt) <= 10
    assert truncated is True


def test_bound_log_returns_the_text_unchanged_when_it_already_fits() -> None:
    excerpt, truncated = ci_checks._bound_log("short", 1000)
    assert excerpt == "short"
    assert truncated is False


def test_annotations_fetch_now_passes_an_explicit_timeout() -> None:
    """The pre-#423 defect this proposal also closes one layer down: an
    unbounded `_fetch_annotations` call."""
    calls = []

    def fake_run_capturing(cmd, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    with patch.object(ci_checks, "run_capturing", side_effect=fake_run_capturing):
        ci_checks._fetch_annotations("mctlhq/mctl-web", 555)

    assert calls[0]["timeout"] == ci_checks.SHEPHERD_CI_LOG_TIMEOUT_SECONDS
