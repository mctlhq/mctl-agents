from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator.temporal.activities.gitops_state import (
    ProposalStateRef,
    _parse_status_yaml,
    parse_iso,
)
from orchestrator.temporal.activities.orphans import _classify_never_started

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _ref(**kw) -> ProposalStateRef:
    base = dict(
        service="mctl-agents",
        slug="issue-5-x",
        status="accepted",
        pr_url=None,
        updated_at=None,
        approved_at=(NOW - timedelta(minutes=120)).isoformat(),
        attempt_expires_at=None,
        unrunnable=None,
        blocked=None,
        execution_authorization={"ok": True},
        unauthorized_reason=None,
    )
    base.update(kw)
    return ProposalStateRef(**base)


def _ref_from_parsed(parsed, slug="issue-5-x") -> ProposalStateRef:
    (status, pr_url, updated_at, attempt_expires_at, unrunnable, blocked,
     authorization, unauthorized_reason, approved_at) = parsed
    return ProposalStateRef(
        service="mctl-agents", slug=slug, status=status, pr_url=pr_url,
        updated_at=updated_at, approved_at=approved_at,
        attempt_expires_at=attempt_expires_at, unrunnable=unrunnable,
        blocked=blocked, execution_authorization=authorization,
        unauthorized_reason=unauthorized_reason,
    )


def test_old_authorized_flagged():
    sigs, unauth = _classify_never_started([_ref()], set(), 45, NOW)
    assert len(sigs) == 1 and sigs[0].age_minutes == 120 and unauth == 0


def test_young_skipped():
    r = _ref(approved_at=(NOW - timedelta(minutes=5)).isoformat())
    assert _classify_never_started([r], set(), 45, NOW) == ([], 0)


def test_unknown_age_reported_with_none():
    r = _ref(approved_at=None, updated_at=None)
    sigs, unauth = _classify_never_started([r], set(), 45, NOW)
    assert len(sigs) == 1 and sigs[0].age_minutes is None and unauth == 0


def test_active_workflow_skipped():
    active = {"dev-loop-mctlhq-mctl-agents-5"}
    assert _classify_never_started([_ref()], active, 45, NOW) == ([], 0)


def test_unauthorized_counted_before_active_and_age_checks():
    old = _ref(execution_authorization=None)
    young = _ref(
        slug="issue-6-y",
        execution_authorization=None,
        approved_at=(NOW - timedelta(minutes=1)).isoformat(),
    )
    live = _ref(slug="issue-7-z", execution_authorization=None)
    sigs, unauth = _classify_never_started(
        [old, young, live], {"dev-loop-mctlhq-mctl-agents-7"}, 45, NOW
    )
    assert sigs == [] and unauth == 3


def test_unreadable_status_counted(tmp_path):
    from types import SimpleNamespace

    from orchestrator.temporal.activities.orphans import _local_state_refs

    ref = SimpleNamespace(service="s", slug="issue-1-a", status="accepted",
                          status_path=tmp_path / "missing" / ".status.yaml")
    out, unreadable = _local_state_refs(tmp_path, [ref])
    assert out == [] and unreadable == 1


def test_pr_present_skipped():
    assert _classify_never_started([_ref(pr_url="https://x/pull/1")], set(), 45, NOW) == ([], 0)


def test_sync_unknown_active_set_not_classified(tmp_path, monkeypatch):
    from orchestrator.temporal.activities import orphans

    called = []
    monkeypatch.setattr(orphans, "_discover_refs", lambda *a, **k: [])
    monkeypatch.setattr(
        orphans, "_classify_never_started", lambda *a, **k: called.append(1) or ([], 0)
    )
    orphans._sync_detect_orphans(tmp_path, None)
    assert not called
    orphans._sync_detect_orphans(tmp_path, set())
    assert called


# --- T3: every exclusion yields no signal ---------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"attempt_expires_at": (NOW + timedelta(minutes=5)).isoformat()},
        {"unrunnable": True},
        {"blocked": True},
        {"status": "implemented"},
        {"status": "review-fixing"},
        {"status": "proposed"},
    ],
    ids=["live-lease", "unrunnable", "blocked", "implemented", "review-fixing", "proposed"],
)
def test_exclusions_yield_no_signal(overrides):
    assert _classify_never_started([_ref(**overrides)], set(), 45, NOW) == ([], 0)


def test_an_expired_lease_does_not_hide_the_proposal():
    r = _ref(attempt_expires_at=(NOW - timedelta(minutes=5)).isoformat())
    sigs, _ = _classify_never_started([r], set(), 45, NOW)
    assert [s.slug for s in sigs] == ["issue-5-x"]


# --- T4: both unauthorized shapes are counted, never listed ---------------


@pytest.mark.parametrize(
    "body",
    [
        # legacy auto-accepted: no approval block at all
        "status: accepted\n",
        # anonymous approver: an approval block with no usable identity
        "status: accepted\napproval:\n  approved_by: unknown\n  approved_at: '2026-10-17T10:00:00Z'\n",
    ],
    ids=["legacy", "anonymous-approver"],
)
def test_unauthorized_shapes_are_counted(body):
    parsed = _parse_status_yaml(body)
    ref = _ref_from_parsed(parsed)
    assert ref.execution_authorization is None and ref.unauthorized_reason
    assert _classify_never_started([ref], set(), 45, NOW) == ([], 1)


# --- T5: age source -------------------------------------------------------


def test_age_falls_back_to_updated_at():
    r = _ref(approved_at=None, updated_at=(NOW - timedelta(minutes=90)).isoformat())
    sigs, _ = _classify_never_started([r], set(), 45, NOW)
    assert sigs[0].age_minutes == 90


def test_an_unparseable_approved_at_falls_back_to_updated_at():
    r = _ref(approved_at="not-a-date", updated_at=(NOW - timedelta(minutes=10)).isoformat())
    # updated_at is young, so the fallback must apply the threshold to it.
    assert _classify_never_started([r], set(), 45, NOW) == ([], 0)


# --- T6: in-progress is classified like accepted ---------------------------


def test_in_progress_is_classified_like_accepted():
    sigs, _ = _classify_never_started([_ref(status="in-progress")], set(), 45, NOW)
    assert [s.status for s in sigs] == ["in-progress"]


# --- task 1: approved_at extraction ----------------------------------------


@pytest.mark.parametrize(
    "value", ["'2026-10-17T10:00:00Z'", "2026-10-17T10:00:00Z"], ids=["quoted", "unquoted"]
)
def test_approved_at_round_trips_as_a_string(value):
    parsed = _parse_status_yaml(
        f"status: accepted\napproval:\n  approved_by: mashkovd\n  approved_at: {value}\n"
    )
    approved_at = parsed[-1]
    assert isinstance(approved_at, str)
    # PyYAML turns the unquoted form into a datetime; whatever str() made of
    # it must still parse to the same instant.
    assert parse_iso(approved_at) == datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


# --- task 3: threshold env --------------------------------------------------


def test_threshold_default_and_override(monkeypatch):
    from orchestrator.temporal import constants

    monkeypatch.delenv(constants.NEVER_STARTED_THRESHOLD_MINUTES_ENV, raising=False)
    assert constants.never_started_threshold_minutes() == 45
    monkeypatch.setenv(constants.NEVER_STARTED_THRESHOLD_MINUTES_ENV, "120")
    assert constants.never_started_threshold_minutes() == 120


# --- T10: results recorded before the new fields still deserialize --------


def test_an_old_result_payload_deserializes_with_defaults():
    from temporalio.converter import DataConverter

    from orchestrator.temporal.activities.orphans import OrphanDetectionResult

    old = {
        "total_actionable": 1,
        "orphans": [
            {
                "service": "mctl-web",
                "slug": "issue-10-test",
                "status": "implemented",
                "pr_url": None,
                "reason": "No active DevLoopWorkflow found for open PR proposal",
            }
        ],
        "skipped_reason": None,
    }
    conv = DataConverter.default.payload_converter
    payload = conv.to_payloads([old])[0]
    result = conv.from_payloads([payload], [OrphanDetectionResult])[0]
    assert result.never_started == []
    assert result.never_started_unauthorized == 0
    assert result.never_started_unreadable == 0
    assert result.orphans[0].slug == "issue-10-test"


# --- T8 / T9: the local-checkout path ---------------------------------------


def _write_state(root, service, slug, body):
    d = root / service / "proposals" / slug
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(body)


AUTHORIZED_OLD = (
    "status: accepted\n"
    "approval:\n  approved_by: mashkovd\n  approved_at: '2026-10-01T10:00:00Z'\n"
)
LEGACY = "status: accepted\n"


def _local_fixture(root):
    _write_state(root, "mctl-agents", "issue-5-never-started", AUTHORIZED_OLD)
    _write_state(root, "mctl-agents", "issue-6-legacy", LEGACY)


def _no_pr_lookups(monkeypatch):
    from orchestrator.temporal.activities import orphans

    # A PR-less proposal would otherwise fall back to a `gh` branch lookup.
    monkeypatch.setattr(orphans, "find_pr_for_proposal", lambda *a, **k: None)


def test_local_path_none_vs_empty_active_set(tmp_path, monkeypatch):
    from orchestrator.temporal.activities import orphans

    _no_pr_lookups(monkeypatch)
    _local_fixture(tmp_path)
    unknown = orphans._sync_detect_orphans(tmp_path, None)
    assert unknown.never_started == [] and unknown.never_started_unauthorized == 0
    known = orphans._sync_detect_orphans(tmp_path, set())
    assert [s.slug for s in known.never_started] == ["issue-5-never-started"]
    assert known.never_started_unauthorized == 1


def test_local_path_with_a_relative_state_dir(tmp_path, monkeypatch):
    """status_path is rooted at state_dir already; joining it again doubled a
    relative state_dir and reported every proposal as unreadable."""
    from orchestrator.temporal.activities import orphans

    _no_pr_lookups(monkeypatch)
    _local_fixture(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    result = orphans._sync_detect_orphans(Path(tmp_path.name), set())
    assert result.never_started_unreadable == 0
    assert [s.slug for s in result.never_started] == ["issue-5-never-started"]


def test_local_path_never_reports_an_open_pr_as_never_started(tmp_path, monkeypatch):
    """A proposal whose .status.yaml lost its `pr:` but whose implementer
    branch has an open PR is an orphan, not never-started: reporting it as
    both would claim "no PR" for a proposal that demonstrably has one."""
    from types import SimpleNamespace

    from orchestrator.temporal.activities import orphans

    _local_fixture(tmp_path)
    open_pr = SimpleNamespace(
        repo="mctlhq/mctl-agents", number=7, closed_unmerged=False, merged=False
    )
    monkeypatch.setattr(
        orphans,
        "find_pr_for_proposal",
        lambda service, slug, **k: open_pr if slug == "issue-5-never-started" else None,
    )
    result = orphans._sync_detect_orphans(tmp_path, set())
    assert [o.slug for o in result.orphans] == ["issue-5-never-started"]
    assert result.never_started == []
