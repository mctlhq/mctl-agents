from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.temporal.activities.gitops_state import ProposalStateRef
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
                          status_path="missing/.status.yaml")
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
