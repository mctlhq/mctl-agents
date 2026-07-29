"""Quota-safe batch policy tests for the Tier 2 implementer."""
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
