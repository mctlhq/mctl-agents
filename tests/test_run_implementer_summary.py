"""Truthful implementer batch-summary tests."""
from pathlib import Path

from orchestrator import run_implementer


def _ref(slug: str) -> run_implementer.ProposalRef:
    return run_implementer.ProposalRef(
        service="mctl-agents",
        slug=slug,
        proposal_dir=Path("/tmp") / slug,
        status="accepted",
    )


def test_partial_success_keeps_failure_visible() -> None:
    success = run_implementer.ImplementResult(
        ref=_ref("success"),
        pr_url="https://github.com/mctlhq/mctl-agents/pull/1",
    )
    failure = run_implementer.ImplementResult(
        ref=_ref("failure"),
        pr_url=None,
        error="shell step failed",
    )
    skipped = run_implementer.ImplementResult(
        ref=_ref("skipped"),
        pr_url=None,
        skipped_reason="dry-run",
    )

    assert run_implementer._batch_outcome(
        [success, failure, skipped]
    ) == run_implementer.BatchOutcome(succeeded=1, failed=1, skipped=1)


def test_result_without_outcome_is_failure() -> None:
    result = run_implementer.ImplementResult(
        ref=_ref("missing-outcome"),
        pr_url=None,
    )
    assert run_implementer._batch_outcome([result]).failed == 1


def test_explicit_skip_takes_precedence_over_pr_url() -> None:
    result = run_implementer.ImplementResult(
        ref=_ref("closed-pr"),
        pr_url="https://github.com/mctlhq/mctl-agents/pull/2",
        skipped_reason="existing PR is closed without merge",
    )

    assert run_implementer._batch_outcome(
        [result]
    ) == run_implementer.BatchOutcome(succeeded=0, failed=0, skipped=1)
