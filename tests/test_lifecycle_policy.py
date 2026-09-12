"""Lifecycle ownership policy.

These pin one distinction: who OWNS an entity and who may MERGE it are
different questions with different answers. Today they are answered by the same
env-var list, which is how a steward-owned repository ended up with no path
from a review finding back to code (#292), and how a reviewer read `merge_owner`
as a merge authorization (#344).
"""
from __future__ import annotations

import pytest

from orchestrator import run_shepherd
from orchestrator.lifecycle import policy
from orchestrator.lifecycle.contract import (
    OWNER_HUMAN_CODEOWNER,
    OWNER_PR_STEWARD,
    OWNER_SHEPHERD,
)


def test_skip_listed_service_is_owned_by_the_steward(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service the shepherd discovers nothing for belongs to another PR
    lifecycle, and the ownership record should say so by name rather than by
    the shepherd's absence."""
    monkeypatch.setattr(run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset({"mctl-design"}))
    monkeypatch.setattr(run_shepherd, "SHEPHERD_FIX_ONLY_SERVICES", frozenset())
    assert policy.default_owner_for("mctl-design") == OWNER_PR_STEWARD


def test_fix_only_service_is_owned_by_the_shepherd_but_merged_by_the_steward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #292 shape, and the reason the two functions exist separately: the
    shepherd owns getting the findings fixed, the steward owns the merge."""
    monkeypatch.setattr(run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset())
    monkeypatch.setattr(run_shepherd, "SHEPHERD_FIX_ONLY_SERVICES", frozenset({"mctl-telegram"}))
    assert policy.default_owner_for("mctl-telegram") == OWNER_SHEPHERD
    assert policy.merge_authority_for("mctl-telegram") == OWNER_PR_STEWARD


def test_full_service_is_owned_and_merged_by_the_shepherd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset())
    monkeypatch.setattr(run_shepherd, "SHEPHERD_FIX_ONLY_SERVICES", frozenset())
    assert policy.default_owner_for("mctl-web") == OWNER_SHEPHERD
    assert policy.merge_authority_for("mctl-web") == OWNER_SHEPHERD


def test_never_merge_services_keep_merge_with_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """No environment value may move merge authority for these, whoever owns
    the remediation. mctl-academy because a merge publishes content;
    mctl-gitops because a merge is a live cluster change."""
    monkeypatch.setattr(run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset())
    monkeypatch.setattr(run_shepherd, "SHEPHERD_FIX_ONLY_SERVICES", frozenset())
    for service in ("mctl-academy", "mctl-gitops"):
        assert policy.merge_authority_for(service) == OWNER_HUMAN_CODEOWNER
        # Ownership of the FIX stage is unaffected — these repos still get
        # their findings addressed; only the merge stays with a human.
        assert policy.default_owner_for(service) == OWNER_SHEPHERD


def test_policy_ref_records_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ownership row has to answer 'why does this actor own it' without an
    operator reconstructing a CWFT env var in another repository."""
    monkeypatch.setattr(run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset({"mctl-design"}))
    monkeypatch.setattr(run_shepherd, "SHEPHERD_FIX_ONLY_SERVICES", frozenset())
    assert policy.policy_ref_for("mctl-design") == "service-mode:mctl-design=skip"
