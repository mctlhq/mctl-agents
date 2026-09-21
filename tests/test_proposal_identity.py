"""The shared rule for "which proposal directory owns this issue number".

Both `run_issue_investigator.resolve_slug` and the `find_proposal_slug`
activity delegate here, so these tests are the single statement of the
semantics; the per-call-site suites then check each surface applies it.
"""
from __future__ import annotations

import pytest

from orchestrator.proposal_identity import (
    IGNORABLE_STATUSES,
    AmbiguousProposalError,
    ProposalCandidate,
    select_proposal_slug,
)

V1 = "issue-404-devloopworkflow-never-calls-continue-as"
V2 = "issue-404-devloopworkflow-never-calls-continue-as-v2"


def _c(slug: str, status: str | None) -> ProposalCandidate:
    return ProposalCandidate(slug=slug, status=status)


# ---------------------------------------------------------------------------
# No candidates / one candidate — unchanged behaviour
# ---------------------------------------------------------------------------
def test_no_candidates_resolves_to_nothing():
    assert select_proposal_slug([]) is None


def test_a_single_proposal_is_returned_whatever_its_status():
    """One directory is not a choice, so no status can change the answer."""
    for status in ("accepted", "proposed", "merged", "rejected", None):
        assert select_proposal_slug([_c("issue-7-only", status)]) == "issue-7-only"


# ---------------------------------------------------------------------------
# The narrow new rule: `rejected` loses, and only `rejected`
# ---------------------------------------------------------------------------
def test_rejected_beside_accepted_resolves_to_the_accepted_one():
    chosen = select_proposal_slug([_c(V1, "rejected"), _c(V2, "accepted")])
    assert chosen == V2


def test_order_does_not_matter():
    """The rule is about status, never about position or slug spelling."""
    assert select_proposal_slug([_c(V2, "accepted"), _c(V1, "rejected")]) == V2


def test_rejected_is_matched_case_and_whitespace_insensitively():
    assert select_proposal_slug([_c(V1, " Rejected\n"), _c(V2, "accepted")]) == V2


def test_two_rejected_proposals_are_refused_not_resurrected():
    with pytest.raises(AmbiguousProposalError) as excinfo:
        select_proposal_slug([_c(V1, "rejected"), _c(V2, "rejected")])

    message = str(excinfo.value)
    assert "every proposal directory for this issue is rejected" in message
    assert V1 in message
    assert V2 in message


def test_two_accepted_proposals_stay_ambiguous():
    with pytest.raises(AmbiguousProposalError) as excinfo:
        select_proposal_slug([_c(V1, "accepted"), _c(V2, "accepted")])

    assert "refusing to guess which one is real" in str(excinfo.value)


def test_merged_beside_accepted_stays_ambiguous():
    """`merged` is deliberately NOT ignorable.

    A merged proposal beside an accepted one is a real question about what
    a reopened or continued issue means, and it must not be answered by a
    resolver written for the closed-unmerged case.
    """
    with pytest.raises(AmbiguousProposalError) as excinfo:
        select_proposal_slug([_c(V1, "merged"), _c(V2, "accepted")])

    message = str(excinfo.value)
    assert V1 in message
    assert V2 in message


def test_merged_is_not_in_the_ignorable_set():
    """Guards the rule itself, not just one call of it."""
    assert IGNORABLE_STATUSES == frozenset({"rejected"})


# ---------------------------------------------------------------------------
# Missing evidence never retires a proposal
# ---------------------------------------------------------------------------
def test_an_unreadable_status_counts_as_live():
    with pytest.raises(AmbiguousProposalError):
        select_proposal_slug([_c(V1, None), _c(V2, "accepted")])


def test_rejected_beside_unreadable_resolves_to_the_unreadable_one():
    """The unreadable one is the only candidate not positively retired."""
    assert select_proposal_slug([_c(V1, "rejected"), _c(V2, None)]) == V2


# ---------------------------------------------------------------------------
# No implicit tie-breaks
# ---------------------------------------------------------------------------
def test_three_way_with_one_survivor_resolves():
    chosen = select_proposal_slug(
        [_c("issue-9-a", "rejected"), _c("issue-9-b", "rejected"), _c("issue-9-c", "proposed")]
    )
    assert chosen == "issue-9-c"


def test_a_v2_suffix_is_not_a_tie_break():
    """Two live proposals stay ambiguous even when one looks like a successor.

    The `-v2` spelling is a human convention, not evidence, so it must not
    decide anything on its own.
    """
    with pytest.raises(AmbiguousProposalError):
        select_proposal_slug([_c(V1, "proposed"), _c(V2, "proposed")])
