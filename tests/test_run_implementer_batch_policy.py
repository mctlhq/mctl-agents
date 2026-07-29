"""Quota-safe batch policy tests for the Tier 2 implementer."""
from pathlib import Path

from orchestrator import run_implementer


def test_executable_run_is_fixed_to_one_proposal() -> None:
    assert run_implementer._max_proposals_error(1, dry_run=False) is None
    assert "must be 1" in (
        run_implementer._max_proposals_error(0, dry_run=False) or ""
    )
    assert "must be 1" in (
        run_implementer._max_proposals_error(4, dry_run=False) or ""
    )


def test_dry_run_can_inspect_unlimited_proposals() -> None:
    assert run_implementer._max_proposals_error(0, dry_run=True) is None


def test_negative_limit_is_always_invalid() -> None:
    assert "zero or positive" in (
        run_implementer._max_proposals_error(-1, dry_run=True) or ""
    )


def test_review_feedback_ignores_batch_limit(monkeypatch, tmp_path) -> None:
    ref = run_implementer.ProposalRef(
        service="mctl-agents",
        slug="review-me",
        proposal_dir=Path(tmp_path) / "review-me",
        status="implemented",
    )
    bundle = tmp_path / "feedback.json"
    bundle.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("IMPLEMENTER_MAX_PROPOSALS", "0")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_implementer.py",
            "--service",
            "mctl-agents",
            "--slug",
            "review-me",
            "--state-dir",
            str(tmp_path),
            "--review-feedback",
            str(bundle),
        ],
    )
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(run_implementer, "_load_review_feedback", lambda _p: {})
    monkeypatch.setattr(
        run_implementer,
        "find_accepted_proposals",
        lambda *_a, **_kw: [ref],
    )
    monkeypatch.setattr(
        run_implementer,
        "review_feedback_one",
        lambda *_a, **_kw: run_implementer.ImplementResult(
            ref=ref,
            pr_url=None,
            skipped_reason="dry-run",
        ),
    )

    run_implementer.main()
