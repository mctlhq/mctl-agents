"""Attributable progress-log tests for implementer runs."""
from pathlib import Path

import pytest

from orchestrator import run_implementer


def _ref(slug: str) -> run_implementer.ProposalRef:
    return run_implementer.ProposalRef(
        service="mctl-agents",
        slug=slug,
        proposal_dir=Path("/tmp") / slug,
        status="accepted",
    )


def test_progress_markers_identify_each_proposal(monkeypatch, capsys) -> None:
    refs = [_ref("first"), _ref("second"), _ref("closed")]

    def fake_implement(ref, dry_run=False):
        if ref.slug == "first":
            return run_implementer.ImplementResult(
                ref=ref,
                pr_url="https://github.com/mctlhq/mctl-agents/pull/1",
                counts_toward_limit=False,
            )
        if ref.slug == "second":
            return run_implementer.ImplementResult(
                ref=ref,
                pr_url=None,
                error="preflight unavailable",
                counts_toward_limit=False,
            )
        return run_implementer.ImplementResult(
            ref=ref,
            pr_url="https://github.com/mctlhq/mctl-agents/pull/2",
            skipped_reason="existing PR is closed without merge",
            counts_toward_limit=False,
        )

    monkeypatch.setattr(run_implementer, "implement_one", fake_implement)
    run_implementer._implement_refs(refs, max_proposals=1, dry_run=False)

    output = capsys.readouterr().out
    assert "[mctl-agents/first] Implementing" in output
    assert "[mctl-agents/first] Finished: ready" in output
    assert "[mctl-agents/second] Implementing" in output
    assert "[mctl-agents/second] Finished: failed" in output
    assert "[mctl-agents/closed] Implementing" in output
    assert "[mctl-agents/closed] Finished: skipped" in output


def test_progress_marker_prints_blocked_not_skipped(monkeypatch, capsys) -> None:
    ref = _ref("blocked")

    def fake_implement(ref, dry_run=False):
        return run_implementer.ImplementResult(
            ref=ref,
            pr_url=None,
            blocked=run_implementer.BLOCKED_APPROVAL_MISSING,
            skipped_reason="requires_human_approval is set but no verified approval is recorded",
            counts_toward_limit=False,
        )

    monkeypatch.setattr(run_implementer, "implement_one", fake_implement)
    run_implementer._implement_refs([ref], max_proposals=1, dry_run=False)

    output = capsys.readouterr().out
    assert "[mctl-agents/blocked] Finished: blocked" in output
    assert "[mctl-agents/blocked] Finished: skipped" not in output


def test_progress_marker_closes_when_implementation_aborts(
    monkeypatch,
    capsys,
) -> None:
    ref = _ref("auth-failure")

    def abort_implementation(_ref, dry_run=False):
        raise SystemExit("SDK authentication failed")

    monkeypatch.setattr(
        run_implementer,
        "implement_one",
        abort_implementation,
    )

    with pytest.raises(SystemExit, match="SDK authentication failed"):
        run_implementer._implement_refs([ref], max_proposals=1, dry_run=False)

    output = capsys.readouterr().out
    assert "[mctl-agents/auth-failure] Implementing" in output
    assert "[mctl-agents/auth-failure] Finished: aborted" in output


# --- main()'s exit-code table for a batch that includes a blocked result ---

def _blocked_result(slug: str) -> run_implementer.ImplementResult:
    return run_implementer.ImplementResult(
        ref=_ref(slug),
        pr_url=None,
        blocked=run_implementer.BLOCKED_APPROVAL_MISSING,
        skipped_reason="requires_human_approval is set but no verified approval is recorded",
        counts_toward_limit=False,
    )


def _run_main(monkeypatch, tmp_path, results, extra_argv=()):
    refs = [result.ref for result in results]
    monkeypatch.setattr(run_implementer, "find_accepted_proposals", lambda *_a, **_kw: refs)
    monkeypatch.setattr(run_implementer, "_implement_refs", lambda *_a, **_kw: results)
    monkeypatch.setattr(
        "sys.argv",
        ["run_implementer.py", "--state-dir", str(tmp_path), *extra_argv],
    )
    return run_implementer.main()


def test_main_exits_45_for_a_blocked_only_batch(monkeypatch, tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, tmp_path, [_blocked_result("blocked-only")])

    assert exc_info.value.code == run_implementer.EXIT_BLOCKED_ONLY
    output = capsys.readouterr().out
    assert "=== Blocked ===" in output
    assert "1 blocked" in output


def test_main_exits_zero_when_a_blocked_result_coexists_with_a_pr(
    monkeypatch, tmp_path
) -> None:
    succeeded = run_implementer.ImplementResult(
        ref=_ref("succeeded"),
        pr_url="https://github.com/mctlhq/mctl-agents/pull/9",
    )
    # main() returns normally (exit 0) rather than raising SystemExit.
    _run_main(monkeypatch, tmp_path, [_blocked_result("blocked"), succeeded])


def test_main_exits_one_when_a_blocked_result_coexists_with_an_error(
    monkeypatch, tmp_path
) -> None:
    failed = run_implementer.ImplementResult(
        ref=_ref("failed"), pr_url=None, error="shell step failed",
    )
    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, tmp_path, [_blocked_result("blocked"), failed])

    assert exc_info.value.code == 1


def test_main_exits_zero_with_neither_blocked_nor_failed(monkeypatch, tmp_path) -> None:
    skipped = run_implementer.ImplementResult(
        ref=_ref("skipped"), pr_url=None, skipped_reason="dry-run",
    )
    _run_main(monkeypatch, tmp_path, [skipped])


def test_dry_run_never_exits_45_even_when_blocked_only(monkeypatch, tmp_path) -> None:
    _run_main(
        monkeypatch, tmp_path, [_blocked_result("blocked-only")], extra_argv=["--dry-run"]
    )
