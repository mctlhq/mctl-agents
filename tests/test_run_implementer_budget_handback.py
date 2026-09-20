"""A budget-exhausted implement run must not be charged to the proposal.

mctl-agents#430 moved the "the runner ran out of budget" outcome into its own
blameless lane on the REVIEW path (exit 51, `.status.yaml` untouched) but left
the IMPLEMENT path writing `needs-triage` with `failure.code: no-commits` --
terminal by contract, since a retry needs an operator-reviewed gitops change
moving the proposal back to `accepted`. That is the same misattribution the
proposal exists to remove, on the other driver (claude P2 on `630ac27`).

These tests pin the hand-back: `accepted` restored, no `failure` block, and an
error carrying the verification-budget prefix so the summary says what
happened.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from orchestrator import run_implementer
from orchestrator.source_issue import SourceIssueVerdict

OPEN = SourceIssueVerdict(known=True, failure=None, issue_ref="mctlhq/mctl-telegram#510")


def _make_ref(tmp_path: Path) -> run_implementer.ProposalRef:
    d = tmp_path / "mctl-telegram" / "proposals" / "issue-510-x"
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(
        yaml.safe_dump({
            "status": "accepted",
            "source": {
                "type": "github_issue",
                "repo": "mctlhq/mctl-telegram",
                "issue": 510,
                "url": "https://github.com/mctlhq/mctl-telegram/issues/510",
            },
        }),
        encoding="utf-8",
    )
    return run_implementer.ProposalRef(
        service="mctl-telegram",
        slug="issue-510-x",
        proposal_dir=d,
        status="accepted",
        approval_ok=True,
    )


def _reach_the_sdk(monkeypatch, tmp_path: Path, *, on_run) -> None:
    """Stub everything between the admission gate and step 6."""
    monkeypatch.setattr(
        run_implementer,
        "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(run_implementer, "read_source_issue", lambda *_a, **_kw: OPEN)
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_acquire_claim", lambda *_a, **_kw: None)
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: clone)
    monkeypatch.setattr(run_implementer, "_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(run_implementer.anyio, "run", on_run)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: False)


def _read(ref) -> dict:
    return yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))


def test_an_exhausted_budget_hands_the_proposal_back_instead_of_triaging(
    monkeypatch, tmp_path: Path
) -> None:
    ref = _make_ref(tmp_path)

    def on_run(func, *_a, **_kw):
        func.keywords["budget_ledger"].record_denied_exhausted("go test ./...")
        return None

    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run)

    result = run_implementer.implement_one(ref, dry_run=False)

    status = _read(ref)
    assert status["status"] == "accepted", "a busy runner must not park the proposal"
    assert "failure" not in status or status["failure"] is None
    assert result.error is not None
    assert result.error.startswith(run_implementer.VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX)
    assert result.budget_ledger is not None
    assert result.budget_ledger.exhausted is True
    # The ledger summary rides along so an operator can tell a busy runner
    # apart from a reserve tuned too tight.
    assert "denied_exhausted=1" in result.error


def test_a_plain_no_commit_run_is_still_charged_to_the_proposal(
    monkeypatch, tmp_path: Path
) -> None:
    """The hand-back is scoped to an EXHAUSTED ledger. An ordinary no-commit
    run stays terminal -- it is deterministic, and re-running it burns spend
    for the same result."""
    ref = _make_ref(tmp_path)
    _reach_the_sdk(monkeypatch, tmp_path, on_run=lambda *_a, **_kw: None)

    result = run_implementer.implement_one(ref, dry_run=False)

    status = _read(ref)
    assert status["status"] == "needs-triage"
    assert status["failure"]["code"] == "no-commits"
    assert result.error is not None
    assert not result.error.startswith(
        run_implementer.VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX
    )


def test_the_hand_back_says_so_when_the_compare_and_swap_declines(
    monkeypatch, tmp_path: Path
) -> None:
    """Somebody else's attempt is in the file: nothing was handed back, and
    the summary must not read identically to the case where it was."""
    ref = _make_ref(tmp_path)

    def on_run(func, *_a, **_kw):
        func.keywords["budget_ledger"].record_denied_exhausted("go test ./...")
        return None

    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run)
    monkeypatch.setattr(run_implementer, "_hand_back_if_still_ours", lambda *_a, **_kw: False)

    result = run_implementer.implement_one(ref, dry_run=False)

    assert result.error is not None
    assert result.error.startswith(run_implementer.VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX)
    assert "attempt that now holds it" in result.error


def test_the_hand_back_is_bounded_and_turns_terminal_at_the_cap(
    monkeypatch, tmp_path: Path
) -> None:
    """Blameless is not the same as infinite.

    The implement driver has no `review_attempts`/`harness_failures` budget --
    the sibling orphaned-subagent arm stays terminal for exactly that reason
    -- so an unconditional hand-back would trade a wrong terminal state for an
    unbounded PAID retry loop (claude P2 on `624a433`). At the cap the run is
    recorded terminally, but under its own code, so the history still says
    what actually happened."""
    ref = _make_ref(tmp_path)
    # Two hand-backs already spent; this attempt is the third and last.
    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    status["budget_handbacks"] = run_implementer.IMPLEMENT_MAX_BUDGET_HANDBACKS - 1
    ref.status_path.write_text(yaml.safe_dump(status), encoding="utf-8")

    def on_run(func, *_a, **_kw):
        func.keywords["budget_ledger"].record_denied_exhausted("go test ./...")
        return None

    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run)

    result = run_implementer.implement_one(ref, dry_run=False)

    after = _read(ref)
    assert after["status"] == "needs-triage"
    assert after["failure"]["code"] == "verification-budget-exhausted"
    assert after["failure"]["code"] != "no-commits", "must not read as a bad proposal"
    assert result.error is not None
    assert "verification budget exhausted" in result.error


def test_each_hand_back_increments_the_tally(monkeypatch, tmp_path: Path) -> None:
    ref = _make_ref(tmp_path)

    def on_run(func, *_a, **_kw):
        func.keywords["budget_ledger"].record_denied_exhausted("go test ./...")
        return None

    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run)

    result = run_implementer.implement_one(ref, dry_run=False)

    after = _read(ref)
    assert after["status"] == "accepted"
    assert after["budget_handbacks"] == 1
    # The operator reading the batch summary can see how much rope is left.
    assert result.error is not None
    assert (
        f"attempt 1 of {run_implementer.IMPLEMENT_MAX_BUDGET_HANDBACKS}"
        in result.error
    )


def test_the_status_is_written_before_the_claim_is_released(
    monkeypatch, tmp_path: Path
) -> None:
    """Releasing first frees mutual exclusion while `.status.yaml` still
    names our live attempt, so a second executor can acquire the claim inside
    that window (agy P2 on `61595a0`). Every sibling arm writes first."""
    ref = _make_ref(tmp_path)
    order: list[str] = []

    def on_run(func, *_a, **_kw):
        func.keywords["budget_ledger"].record_denied_exhausted("go test ./...")
        return None

    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run)

    real_hand_back = run_implementer._hand_back_if_still_ours

    def spy_hand_back(*args, **kwargs):
        order.append("status")
        return real_hand_back(*args, **kwargs)

    monkeypatch.setattr(run_implementer, "_hand_back_if_still_ours", spy_hand_back)
    monkeypatch.setattr(
        run_implementer, "_release_claim",
        lambda *_a, **_kw: order.append("release"),
    )

    run_implementer.implement_one(ref, dry_run=False)

    assert order == ["status", "release"], order


def test_a_successful_run_clears_the_tally(monkeypatch, tmp_path: Path) -> None:
    """The cap bounds CONSECUTIVE exhausted attempts, not the proposal's
    lifetime -- a run that got through must not leave the next one closer to
    a terminal state."""
    ref = _make_ref(tmp_path)
    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    status["budget_handbacks"] = 2
    ref.status_path.write_text(yaml.safe_dump(status), encoding="utf-8")

    _reach_the_sdk(monkeypatch, tmp_path, on_run=lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_detect_chart_major_bumps", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        run_implementer, "_push_and_open_pr",
        lambda *_a, **_kw: "https://github.com/mctlhq/mctl-telegram/pull/1",
    )

    run_implementer.implement_one(ref, dry_run=False)

    after = _read(ref)
    assert after["status"] == "implemented"
    assert after.get("budget_handbacks") in (None, 0), after.get("budget_handbacks")


def test_the_hand_back_does_not_turn_the_tick_red(monkeypatch, tmp_path: Path) -> None:
    """The tally is the only durable bound on the retry loop, so the tick
    that writes it must exit 0.

    `main()` exits 1 whenever `_batch_outcome` reports anything under
    `failed`, and the module's own note at the exit-code block says a
    non-zero exit marks `implement` Failed and can skip the downstream
    gitops commit. If that commit is skipped the next tick reads
    `budget_handbacks: 0`, the cap never advances, and the paid loop the cap
    exists to bound runs forever (claude P2 on `4449024`). `stale_source` is
    excluded from `failed` for exactly this reason; so is this.
    """
    ref = _make_ref(tmp_path)

    def on_run(func, *_a, **_kw):
        func.keywords["budget_ledger"].record_denied_exhausted("go test ./...")
        return None

    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run)

    result = run_implementer.implement_one(ref, dry_run=False)

    assert result.budget_handback is True
    outcome = run_implementer._batch_outcome([result])
    assert outcome.budget_handback == 1
    assert outcome.failed == 0, "a hand-back must not force the batch red"


def test_a_declined_hand_back_is_still_a_failure(monkeypatch, tmp_path: Path) -> None:
    """Nothing durable was written on this tick, so there is no commit to
    protect -- and somebody else owns the proposal. Report it honestly."""
    ref = _make_ref(tmp_path)

    def on_run(func, *_a, **_kw):
        func.keywords["budget_ledger"].record_denied_exhausted("go test ./...")
        return None

    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run)
    monkeypatch.setattr(run_implementer, "_hand_back_if_still_ours", lambda *_a, **_kw: False)

    result = run_implementer.implement_one(ref, dry_run=False)

    assert result.budget_handback is False
    assert run_implementer._batch_outcome([result]).failed == 1
