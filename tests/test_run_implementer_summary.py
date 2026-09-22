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


def test_blocked_result_is_counted_separately_from_skipped() -> None:
    # A blocked result also carries `skipped_reason` (for consumers of that
    # older channel alone), so `_batch_outcome` must classify it as
    # `blocked` before it ever reaches the `skipped_reason` branch.
    blocked = run_implementer.ImplementResult(
        ref=_ref("blocked"),
        pr_url=None,
        blocked=run_implementer.BLOCKED_APPROVAL_MISSING,
        skipped_reason="requires_human_approval is set but no verified approval is recorded",
        counts_toward_limit=False,
    )

    outcome = run_implementer._batch_outcome([blocked])

    assert outcome == run_implementer.BatchOutcome(
        succeeded=0, failed=0, skipped=0, blocked=1
    )


def test_stale_source_result_is_not_counted_as_failed() -> None:
    # A stale-source admission refusal also carries `error` (the older
    # channel), so `_batch_outcome` must classify it via `stale_source`
    # before it ever reaches the `error` branch -- otherwise a healthy
    # refusal alongside a real success would still report the batch as
    # having a failure (codex P2 follow-up on mctl-agents#410).
    stale = run_implementer.ImplementResult(
        ref=_ref("stale"),
        pr_url=None,
        error="source-resolved",
        counts_toward_limit=False,
        stale_source=("source-resolved", "mctlhq/mctl-telegram#510"),
    )
    success = run_implementer.ImplementResult(
        ref=_ref("success"),
        pr_url="https://github.com/mctlhq/mctl-agents/pull/1",
    )

    outcome = run_implementer._batch_outcome([stale, success])

    assert outcome == run_implementer.BatchOutcome(
        succeeded=1, failed=0, skipped=0, blocked=0, stale_source=1
    )


def test_stale_source_only_batch_is_still_a_failure() -> None:
    # requirements.md's EARS: "WHEN a batch run's only outcome is an
    # admission refusal THE SYSTEM SHALL count it as a failed result
    # (exit 1)". `_batch_outcome` keeps that fact visible via
    # `stale_source` even though it is no longer folded into `failed`.
    stale = run_implementer.ImplementResult(
        ref=_ref("stale"),
        pr_url=None,
        error="source-resolved",
        counts_toward_limit=False,
        stale_source=("source-resolved", "mctlhq/mctl-telegram#510"),
    )

    outcome = run_implementer._batch_outcome([stale])

    assert outcome == run_implementer.BatchOutcome(
        succeeded=0, failed=0, skipped=0, blocked=0, stale_source=1
    )


def test_explicit_skip_takes_precedence_over_pr_url() -> None:
    result = run_implementer.ImplementResult(
        ref=_ref("closed-pr"),
        pr_url="https://github.com/mctlhq/mctl-agents/pull/2",
        skipped_reason="existing PR is closed without merge",
    )

    assert run_implementer._batch_outcome(
        [result]
    ) == run_implementer.BatchOutcome(succeeded=0, failed=0, skipped=1)
