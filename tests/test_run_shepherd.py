"""Unit + state-machine tests for ``orchestrator.run_shepherd``.

Covers:
- Each branch of ``decide()``: wait, address-review, merge,
  flip-to-merged, flip-to-rejected (T1-T5).
- Findings anchored to head_sha — codex-P1 fix from PR #83's design,
  mirrors the docstring on ``CodexReview.findings_p1_p2``.
- T6: outer-loop 3-attempt cap on address-review before flipping the
  proposal to ``review-stuck``.
- T7: end-to-end happy path (clean review -> merge) and loop path
  (P1 finding -> followup -> re-evaluate -> merge).

GitHub API + the implementer subprocess are mocked at the module
boundary. ``.status.yaml`` round-trips through a real temp worktree
fixture so the YAML serialisation is exercised.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from orchestrator import run_implementer, run_shepherd
from orchestrator.run_shepherd import (
    CodexFinding,
    CodexReview,
    ProposalRef,
    PRSnapshot,
    decide,
    process_one,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
HEAD_SHA = "a" * 40
OLD_SHA = "b" * 40
HEAD_PUSHED_AT = "2026-04-29T10:00:00Z"
PR_URL = "https://github.com/mctlhq/mctl-web/pull/42"


def make_pr(
    *,
    merged: bool = False,
    closed_unmerged: bool = False,
    is_draft: bool = False,
    merge_commit: str | None = None,
    merge_state_status: str = "CLEAN",
    checks_green: bool = True,
    head_sha: str = HEAD_SHA,
    head_pushed_at: str | None = HEAD_PUSHED_AT,
    state: str | None = None,
    review_decision: str = "",
) -> PRSnapshot:
    """Build a PRSnapshot with sensible mergeable defaults."""
    if state is None:
        if merged:
            state = "MERGED"
        elif closed_unmerged:
            state = "CLOSED"
        else:
            state = "OPEN"
    return PRSnapshot(
        number=42,
        repo="mctlhq/mctl-web",
        state=state,
        merged=merged,
        closed_unmerged=closed_unmerged,
        merge_commit=merge_commit,
        close_comment_or_default=(
            "PR was closed without merging." if closed_unmerged else ""
        ),
        head_sha=head_sha,
        head_pushed_at=head_pushed_at,
        merge_state_status=merge_state_status,
        checks_green=checks_green,
        is_draft=is_draft,
        review_decision=review_decision,
    )


def make_finding(
    *,
    severity: str = "P1",
    commit_id: str | None = HEAD_SHA,
    body: str = "![P1 Badge] Pin tar to >=6.2.1",
    path: str | None = "package.json",
    line: int | None = 42,
    created_at: str | None = "2026-04-29T11:00:00Z",
) -> CodexFinding:
    return CodexFinding(
        body=body,
        path=path,
        line=line,
        commit_id=commit_id,
        created_at=created_at,
        severity=severity,
    )


def make_status_yaml(
    tmp_path: Path,
    *,
    service: str = "mctl-web",
    slug: str = "test-slug",
    status: str = "implemented",
    review_attempts: int = 0,
    pr: str | None = PR_URL,
) -> Path:
    """Create a `.status.yaml` on disk and return its proposal_dir."""
    proposal_dir = tmp_path / service / "proposals" / slug
    proposal_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "updated_at": "2026-04-29T09:00:00Z",
        "updated_by": "mctl-agents[bot]",
        "review_attempts": review_attempts,
    }
    if pr is not None:
        payload["pr"] = pr
    (proposal_dir / ".status.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    return proposal_dir


def make_ref(
    tmp_path: Path,
    *,
    service: str = "mctl-web",
    slug: str = "test-slug",
    status: str = "implemented",
    review_attempts: int = 0,
    pr_url: str | None = PR_URL,
) -> ProposalRef:
    proposal_dir = make_status_yaml(
        tmp_path,
        service=service,
        slug=slug,
        status=status,
        review_attempts=review_attempts,
        pr=pr_url,
    )
    return ProposalRef(
        service=service,
        slug=slug,
        proposal_dir=proposal_dir,
        status=status,
        review_attempts=review_attempts,
        pr_url=pr_url,
    )


def read_status(ref: ProposalRef) -> dict:
    with ref.status_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# T1-T5 + head-anchor: decide() branches
# ---------------------------------------------------------------------------
def test_decide_merge() -> None:
    """T1: clean review + green CI -> merge."""
    pr = make_pr(merged=False, checks_green=True, merge_state_status="CLEAN")
    review = CodexReview(has_responded=True, findings=[])
    # Pin now well after HEAD_PUSHED_AT so the settling window does not apply;
    # makes the test independent of wall-clock time and the fixture's date.
    now = datetime(2026, 4, 29, 11, 0, 0, tzinfo=UTC)
    assert decide(pr, review, now=now) == ("merge", None)


def test_decide_wait_within_settle_window() -> None:
    """Clean + green but head pushed inside the settling window -> wait.

    Guards against the mctl-telegram#115 failure: merging out from under a
    human/second reviewer who is still pushing fix-ups. Default window is 15m;
    here the head was pushed 5 minutes before `now`.
    """
    now = datetime(2026, 4, 29, 10, 5, 0, tzinfo=UTC)
    pr = make_pr(head_pushed_at="2026-04-29T10:00:00Z")
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review, now=now) == ("wait", None)


def test_decide_merge_after_settle_window() -> None:
    """Clean + green and head pushed before the settling window -> merge.

    Head pushed 30 minutes before `now`, outside the default 15m window.
    """
    now = datetime(2026, 4, 29, 10, 30, 0, tzinfo=UTC)
    pr = make_pr(head_pushed_at="2026-04-29T10:00:00Z")
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review, now=now) == ("merge", None)


def test_decide_merge_future_dated_head() -> None:
    """A future-dated head_pushed_at must not hold the PR forever -> merge.

    Guards the committedDate fallback: a commit authored with a skewed/future
    clock makes now - pushed negative; without the guard that reads as "still
    in the window" and would wedge the PR indefinitely.
    """
    now = datetime(2026, 4, 29, 10, 0, 0, tzinfo=UTC)
    pr = make_pr(head_pushed_at="2026-04-29T10:30:00Z")  # 30 min in the future
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review, now=now) == ("merge", None)


def test_settle_min_from_env_bad_value(monkeypatch) -> None:
    """A non-integer SHEPHERD_MERGE_SETTLE_MIN falls back to 15, not a crash."""
    monkeypatch.setenv("SHEPHERD_MERGE_SETTLE_MIN", "15m")
    assert run_shepherd._settle_min_from_env() == 15


# ---------------------------------------------------------------------------
# SHEPHERD_SKIP_SERVICES: per-service opt-out (pr-steward-owned repos)
# ---------------------------------------------------------------------------
def test_skip_services_from_env_unset(monkeypatch) -> None:
    """Unset env -> empty set -> shepherd discovers everything (default)."""
    monkeypatch.delenv("SHEPHERD_SKIP_SERVICES", raising=False)
    assert run_shepherd._skip_services_from_env() == frozenset()


def test_skip_services_from_env_parses_comma_and_space(monkeypatch) -> None:
    """Comma- or whitespace-separated names both parse, blanks dropped."""
    monkeypatch.setenv("SHEPHERD_SKIP_SERVICES", "mctl-design, mctl-web  mctl-api")
    assert run_shepherd._skip_services_from_env() == frozenset(
        {"mctl-design", "mctl-web", "mctl-api"}
    )


def test_skip_services_from_env_warns_on_unknown(monkeypatch, capsys) -> None:
    """A name not in SERVICES (typo) is kept but warned about, not silent."""
    monkeypatch.setenv("SHEPHERD_SKIP_SERVICES", "mctl-desig")
    names = run_shepherd._skip_services_from_env()
    assert names == frozenset({"mctl-desig"})
    assert "not in SERVICES" in capsys.readouterr().out


def test_discover_skips_listed_service(tmp_path, monkeypatch, capsys) -> None:
    """A skip-listed service is invisible to discovery and logged; others stay."""
    make_status_yaml(tmp_path, service="mctl-design", slug="icon-swap")
    make_status_yaml(tmp_path, service="mctl-web", slug="dep-bump")
    monkeypatch.setattr(
        run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset({"mctl-design"})
    )
    refs = run_shepherd._discover_refs(tmp_path)
    services = {r.service for r in refs}
    assert "mctl-design" not in services
    assert "mctl-web" in services
    assert "skipping mctl-design" in capsys.readouterr().out


def test_discover_no_skip_includes_all(tmp_path, monkeypatch) -> None:
    """With an empty skip-set, mctl-design is discovered like any other repo."""
    make_status_yaml(tmp_path, service="mctl-design", slug="icon-swap")
    monkeypatch.setattr(run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset())
    refs = run_shepherd._discover_refs(tmp_path)
    assert {r.service for r in refs} == {"mctl-design"}


# ---------------------------------------------------------------------------
# Reconcile mode: read-only status repair for skip-listed repos
# ---------------------------------------------------------------------------
def test_discover_reconcile_covers_all_services(tmp_path, monkeypatch) -> None:
    """GitHub projection covers shepherd- and steward-owned services."""
    make_status_yaml(tmp_path, service="mctl-design", slug="icon-swap")
    make_status_yaml(tmp_path, service="mctl-web", slug="dep-bump")
    monkeypatch.setattr(
        run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset({"mctl-design"})
    )
    refs = run_shepherd._discover_refs(tmp_path, reconcile=True)
    assert {r.service for r in refs} == {"mctl-design", "mctl-web"}


def test_reconcile_one_flips_merged(tmp_path) -> None:
    """A merged PR flips the proposal to merged with the merge commit."""
    ref = make_ref(tmp_path, service="mctl-design", slug="icon-swap")
    pr = make_pr(merged=True, merge_commit="abc123" + "0" * 34)
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "flip-to-merged"
    final = read_status(ref)
    assert final["status"] == "merged"
    assert final["merge_commit"] == "abc123" + "0" * 34


def test_reconcile_one_flips_rejected(tmp_path) -> None:
    """A closed-unmerged PR flips the proposal to rejected."""
    ref = make_ref(tmp_path, service="mctl-design", slug="icon-swap")
    pr = make_pr(closed_unmerged=True)
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "flip-to-rejected"
    assert read_status(ref)["status"] == "rejected"


def test_reconcile_one_open_pr_repairs_projection(tmp_path) -> None:
    """An open PR is projected even when steward owns the active loop."""
    ref = make_ref(tmp_path, service="mctl-design", slug="icon-swap")
    pr = make_pr(merged=False, closed_unmerged=False)
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "repair-open-pr"
    final = read_status(ref)
    assert final["status"] == "implemented"
    assert final["github"]["state"] == "open"


def test_reconcile_open_pr_overrides_stale_accepted(tmp_path) -> None:
    """An existing PR prevents an accepted proposal from re-entering Tier 2."""
    ref = make_ref(
        tmp_path,
        service="mctl-design",
        slug="icon-swap",
        status="accepted",
    )
    pr = make_pr(merged=False, closed_unmerged=False)
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "repair-open-pr"
    final = read_status(ref)
    assert final["status"] == "implemented"


def test_reconcile_dry_run_reports_without_writing(tmp_path) -> None:
    """Dry-run executes GitHub decisions but leaves YAML byte-for-byte intact."""
    ref = make_ref(tmp_path, service="mctl-design", slug="icon-swap")
    before = ref.status_path.read_text(encoding="utf-8")
    pr = make_pr(merged=False, closed_unmerged=False)
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr):
        result = run_shepherd.reconcile_one(ref, dry_run=True)
    assert result.decision == "repair-open-pr"
    assert ref.status_path.read_text(encoding="utf-8") == before


def test_reconcile_dry_run_reports_orphan_branch_without_writing(tmp_path) -> None:
    """A useful orphan branch is reported but no PR or YAML write occurs."""
    ref = make_ref(
        tmp_path,
        service="mctl-design",
        slug="icon-swap",
        status="in-progress",
        pr_url=None,
    )
    before = ref.status_path.read_text(encoding="utf-8")
    with patch.object(run_shepherd, "_find_pr_url_by_branch", return_value=None), \
         patch.object(run_shepherd, "find_pr_for_proposal", return_value=None), \
         patch.object(
             run_shepherd.run_implementer,
             "_preflight_existing_result",
             return_value=run_shepherd.run_implementer.ExistingResult(
                 action="branch-ready",
                 reason="useful branch exists without PR",
             ),
         ) as preflight:
        result = run_shepherd.reconcile_one(ref, dry_run=True)
    assert result.decision == "would-open-pr"
    assert preflight.call_args.kwargs["allow_pr_create"] is False
    assert ref.status_path.read_text(encoding="utf-8") == before


def test_discovery_dry_run_does_not_restore_pr_on_disk(tmp_path) -> None:
    """Normal shepherd dry-run may discover a PR but must not mutate YAML."""
    proposal = make_status_yaml(
        tmp_path,
        service="mctl-web",
        slug="lost-link",
        status="in-progress",
        pr=None,
    )
    status_path = proposal / ".status.yaml"
    before = status_path.read_text(encoding="utf-8")
    with patch.object(
        run_shepherd,
        "_find_pr_url_by_branch",
        return_value="https://github.com/mctlhq/mctl-web/pull/99",
    ):
        refs = run_shepherd._discover_refs(tmp_path, dry_run=True)
    assert len(refs) == 1
    assert refs[0].pr_url is not None
    assert refs[0].pr_url.endswith("/pull/99")
    assert status_path.read_text(encoding="utf-8") == before


def test_reconcile_one_recorded_pr_fetch_failure_waits(tmp_path) -> None:
    """A transient fetch failure must not overwrite durable state."""
    ref = make_ref(tmp_path, service="mctl-design", slug="icon-swap")
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=None):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "wait"
    assert result.error is not None
    assert read_status(ref)["status"] == "implemented"


def test_reconcile_one_missing_result_is_needs_triage(tmp_path) -> None:
    """Expired durable work with no PR or result branch is quarantined."""
    ref = make_ref(
        tmp_path,
        service="mctl-design",
        slug="icon-swap",
        pr_url=None,
    )
    with patch.object(run_shepherd, "_find_pr_url_by_branch", return_value=None), \
         patch.object(run_shepherd, "find_pr_for_proposal", return_value=None), \
         patch.object(
             run_shepherd.run_implementer,
             "_preflight_existing_result",
             return_value=run_shepherd.run_implementer.ExistingResult(action="none"),
         ):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "needs-triage"
    final = read_status(ref)
    assert final["status"] == "needs-triage"
    assert final["failure"]["code"] == "missing-pr"


@pytest.mark.parametrize("terminal_status", ["merged", "rejected"])
def test_reconcile_preserves_terminal_status_without_pr(
    tmp_path,
    terminal_status,
) -> None:
    """A missing PR cannot erase an existing terminal decision."""
    ref = make_ref(
        tmp_path,
        service="mctl-design",
        slug="finished-work",
        status=terminal_status,
        pr_url=None,
    )
    before = ref.status_path.read_text(encoding="utf-8")
    with patch.object(run_shepherd, "_find_pr_url_by_branch", return_value=None), \
         patch.object(run_shepherd, "find_pr_for_proposal", return_value=None), \
         patch.object(
             run_shepherd.run_implementer,
             "_preflight_existing_result",
         ) as preflight:
        result = run_shepherd.reconcile_one(ref)

    assert result.decision == "wait"
    assert f"terminal {terminal_status}" in (result.notes or "")
    assert ref.status_path.read_text(encoding="utf-8") == before
    preflight.assert_not_called()


def test_reconcile_preserves_existing_triage_failure_without_pr(tmp_path) -> None:
    """No-commit diagnostics must survive later reconciliation."""
    ref = make_ref(
        tmp_path,
        service="mctl-design",
        slug="icon-swap",
        status="needs-triage",
        pr_url=None,
    )
    status = read_status(ref)
    status["failure"] = {
        "code": "no-commits",
        "stage": "agent",
        "message": "implementer produced no commits",
    }
    ref.status_path.write_text(
        yaml.safe_dump(status, sort_keys=False),
        encoding="utf-8",
    )
    with patch.object(run_shepherd, "_find_pr_url_by_branch", return_value=None), \
         patch.object(run_shepherd, "find_pr_for_proposal", return_value=None), \
         patch.object(
             run_shepherd.run_implementer,
             "_preflight_existing_result",
             return_value=run_shepherd.run_implementer.ExistingResult(action="none"),
         ):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "needs-triage"
    assert read_status(ref)["failure"]["code"] == "no-commits"


def test_reconcile_never_adopts_recorded_branch_collision(tmp_path) -> None:
    """A non-canonical colliding PR cannot enter the shepherd merge loop."""
    ref = make_ref(
        tmp_path,
        service="mctl-design",
        slug="icon-swap",
        status="needs-triage",
        pr_url="https://github.com/mctlhq/mctl-design/pull/99",
    )
    status = read_status(ref)
    status["failure"] = {
        "code": "branch-collision",
        "stage": "preflight",
        "message": "canonical marker missing",
    }
    ref.status_path.write_text(
        yaml.safe_dump(status, sort_keys=False),
        encoding="utf-8",
    )
    with patch.object(
        run_shepherd,
        "find_pr_for_proposal",
        side_effect=AssertionError("colliding URL must not be trusted"),
    ):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "needs-triage"
    assert read_status(ref)["status"] == "needs-triage"


def test_reconcile_preserves_confirmed_conflict_when_github_is_unknown(
    tmp_path,
) -> None:
    """A transient UNKNOWN must not un-quarantine a confirmed conflict."""
    ref = make_ref(
        tmp_path,
        service="mctl-agents",
        slug="conflict",
        status="needs-triage",
    )
    status = read_status(ref)
    status["github"] = {
        "state": "open",
        "head_sha": HEAD_SHA,
        "blocking_reason": "conflict",
        "observed_at": "2026-04-29T12:00:00Z",
    }
    status["failure"] = {"code": "merge-conflict", "stage": "reconcile"}
    ref.status_path.write_text(
        yaml.safe_dump(status, sort_keys=False),
        encoding="utf-8",
    )
    unknown = make_pr(merge_state_status="UNKNOWN", head_sha=HEAD_SHA)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=unknown):
        result = run_shepherd.reconcile_one(ref)

    assert result.decision == "needs-triage"
    final = read_status(ref)
    assert final["status"] == "needs-triage"
    assert final["failure"]["code"] == "merge-conflict"
    assert final["github"]["blocking_reason"] == "conflict"


def test_reconcile_clears_conflict_for_changed_mergeable_head(tmp_path) -> None:
    """A rebased head in any mergeable state may re-enter the lifecycle."""
    ref = make_ref(
        tmp_path,
        service="mctl-agents",
        slug="conflict",
        status="needs-triage",
    )
    status = read_status(ref)
    status["github"] = {
        "state": "open",
        "head_sha": OLD_SHA,
        "blocking_reason": "conflict",
        "observed_at": "2026-04-29T12:00:00Z",
    }
    status["failure"] = {"code": "merge-conflict", "stage": "reconcile"}
    ref.status_path.write_text(
        yaml.safe_dump(status, sort_keys=False),
        encoding="utf-8",
    )
    clean = make_pr(merge_state_status="UNSTABLE", head_sha=HEAD_SHA)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=clean):
        result = run_shepherd.reconcile_one(ref)

    assert result.decision == "repair-open-pr"
    final = read_status(ref)
    assert final["status"] == "implemented"
    assert "failure" not in final
    assert "blocking_reason" not in final["github"]


def test_reconcile_review_stuck_requires_material_change(tmp_path) -> None:
    """Unchanged findings stay terminal; a clean approval reopens the state."""
    ref = make_ref(
        tmp_path,
        service="mctl-agents",
        slug="stuck",
        status="review-stuck",
    )
    status = read_status(ref)
    status["github"] = {
        "state": "open",
        "head_sha": HEAD_SHA,
        "observed_at": "2026-04-29T12:00:00Z",
    }
    status["failure"] = {"code": "review-attempts-exhausted"}
    ref.status_path.write_text(
        yaml.safe_dump(status, sort_keys=False),
        encoding="utf-8",
    )
    unchanged = make_pr(review_decision="CHANGES_REQUESTED")
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=unchanged):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "wait"
    assert read_status(ref)["status"] == "review-stuck"
    assert read_status(ref)["failure"]["code"] == "review-attempts-exhausted"

    approved = make_pr(review_decision="APPROVED")
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=approved):
        result = run_shepherd.reconcile_one(ref)
    assert result.decision == "repair-open-pr"
    final = read_status(ref)
    assert final["status"] == "implemented"
    assert "review_attempts" not in final


def test_decide_address_review() -> None:
    """T2: codex P1 finding on the head SHA -> address-review."""
    pr = make_pr()
    findings = [make_finding(severity="P1")]
    review = CodexReview(has_responded=True, findings=findings)
    decision, payload = decide(pr, review)
    assert decision == "address-review"
    assert payload == findings


def test_decide_wait_codex_pending() -> None:
    """T3: codex has not responded -> wait."""
    pr = make_pr()
    review = CodexReview(has_responded=False, findings=[])
    assert decide(pr, review) == ("wait", None)


def test_decide_wait_ci_pending() -> None:
    """Codex clean but CI still running -> wait."""
    pr = make_pr(checks_green=False)
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review) == ("wait", None)


def test_decide_wait_merge_state_blocked() -> None:
    """Codex clean + checks green but mergeStateStatus=BLOCKED -> wait."""
    pr = make_pr(merge_state_status="BLOCKED")
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review) == ("wait", None)


def test_decide_wait_draft() -> None:
    """Draft PR -> wait, regardless of codex/checks."""
    pr = make_pr(is_draft=True)
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review) == ("wait", None)


def test_decide_flip_to_merged() -> None:
    """T4: pr.merged=True (human merged out of band) -> flip-to-merged."""
    pr = make_pr(merged=True, merge_commit="deadbeef")
    review = CodexReview(has_responded=False, findings=[])
    decision, payload = decide(pr, review)
    assert decision == "flip-to-merged"
    assert payload == "deadbeef"


def test_decide_flip_to_rejected() -> None:
    """T5: PR closed without merging -> flip-to-rejected."""
    pr = make_pr(closed_unmerged=True)
    review = CodexReview(has_responded=False, findings=[])
    decision, payload = decide(pr, review)
    assert decision == "flip-to-rejected"
    assert "without merging" in (payload or "")


def test_decide_findings_anchored_to_head_sha() -> None:
    """Findings on an OLD commit must not block merge once codex re-reviewed clean.

    The implementer pushed a follow-up that fixes the issue; codex
    re-reviewed and is silent on the new head. ``findings_p1_p2(at=head_sha)``
    drops the stale finding so decide() returns merge.
    """
    pr = make_pr()
    stale = make_finding(severity="P1", commit_id=OLD_SHA)
    review = CodexReview(has_responded=True, findings=[stale])

    # Sanity: anchored filter drops the stale finding.
    assert review.findings_p1_p2(at=pr.head_sha) == []

    # decide() therefore returns merge instead of address-review.
    assert decide(pr, review) == ("merge", None)


def test_decide_merge_with_unstable_merge_state() -> None:
    """UNSTABLE mergeStateStatus + clean codex must merge, not wait.

    Per design.md L143-154, UNSTABLE means non-required CI is red but the
    required checks pass — GitHub still considers the PR mergeable. The
    rollup state in that case is typically not SUCCESS, so checks_green
    must be derived from mergeStateStatus (not the raw rollup) for these
    PRs to leave the wait loop.
    """
    pr = make_pr(merge_state_status="UNSTABLE", checks_green=True)
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review) == ("merge", None)


def test_fetch_pr_snapshot_unstable_yields_checks_green(monkeypatch) -> None:
    """_fetch_pr_snapshot must derive checks_green from mergeStateStatus
    when the raw rollup state is not SUCCESS but GitHub already
    classified the PR as UNSTABLE / HAS_HOOKS (mergeable, required checks
    pass). Otherwise PRs in hook-enabled repos or with non-required CI
    failures stall in `wait` forever.
    """
    def make_view(merge_state: str, rollup: str) -> dict:
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 99,
                        "state": "OPEN",
                        "merged": False,
                        "headRefOid": "f" * 40,
                        "mergeCommit": None,
                        "statusCheckRollup": {"state": rollup},
                        "mergeStateStatus": merge_state,
                        "commits": {"nodes": []},
                        "timelineItems": {"nodes": []},
                        "isDraft": False,
                    }
                }
            }
        }

    cases = {"UNSTABLE": True, "HAS_HOOKS": True, "BLOCKED": False, "CLEAN": False}
    for merge_state, expected in cases.items():
        with patch.object(
            run_shepherd, "_gh_api_json",
            return_value=make_view(merge_state, "FAILURE"),
        ):
            snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-web", 99)
        assert snap is not None
        assert snap.merge_state_status == merge_state
        assert snap.checks_green is expected, (
            f"merge_state={merge_state}: checks_green expected {expected} "
            f"got {snap.checks_green}"
        )

    # SUCCESS rollup must always yield checks_green=True regardless of merge state.
    with patch.object(
        run_shepherd, "_gh_api_json",
        return_value=make_view("BLOCKED", "SUCCESS"),
    ):
        snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-web", 99)
    assert snap is not None
    assert snap.checks_green is True


def test_checks_green_no_rollup_clean_merge_state(monkeypatch) -> None:
    """Empty/missing statusCheckRollup + mergeStateStatus=CLEAN must be
    treated as green so decide() returns merge.

    Regression for codex P1 on PR #12: repos with no CI configured at all
    (e.g. mctl-gitops merges have CI=SKIPPED with an empty rollup) still
    get classified as CLEAN by GitHub, but the previous logic only set
    checks_green when the raw rollup state was SUCCESS or
    mergeStateStatus was UNSTABLE/HAS_HOOKS. A genuinely mergeable PR
    therefore stalled in `wait` forever.
    """
    # Unit-level: decide() merges when the snapshot reports
    # checks_green=True from a CLEAN merge state with no rollup.
    pr = make_pr(merge_state_status="CLEAN", checks_green=True)
    review = CodexReview(has_responded=True, findings=[])
    assert decide(pr, review) == ("merge", None)

    # End-to-end through _fetch_pr_snapshot: an empty rollup must yield
    # checks_green=True only when mergeStateStatus is CLEAN.
    def make_view(merge_state: str, rollup: str | None) -> dict:
        rollup_obj: dict | None = (
            {"state": rollup} if rollup is not None else None
        )
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 101,
                        "state": "OPEN",
                        "merged": False,
                        "headRefOid": "e" * 40,
                        "mergeCommit": None,
                        "statusCheckRollup": rollup_obj,
                        "mergeStateStatus": merge_state,
                        "commits": {"nodes": []},
                        "timelineItems": {"nodes": []},
                        "isDraft": False,
                    }
                }
            }
        }

    # CLEAN + no rollup -> green.
    with patch.object(
        run_shepherd, "_gh_api_json",
        return_value=make_view("CLEAN", None),
    ):
        snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-gitops", 101)
    assert snap is not None
    assert snap.checks_green is True

    # CLEAN + empty-string rollup state (defensive — same effect).
    with patch.object(
        run_shepherd, "_gh_api_json",
        return_value=make_view("CLEAN", ""),
    ):
        snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-gitops", 101)
    assert snap is not None
    assert snap.checks_green is True

    # No rollup but a non-CLEAN merge state must NOT be treated as green;
    # the shepherd cannot conclude anything about CI from a BLOCKED PR.
    with patch.object(
        run_shepherd, "_gh_api_json",
        return_value=make_view("BLOCKED", None),
    ):
        snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-gitops", 101)
    assert snap is not None
    assert snap.checks_green is False


def test_decide_keeps_top_level_finding_without_commit_id() -> None:
    """Top-level issue comment findings have commit_id=None and are kept.

    They were already time-filtered against head_pushed_at upstream;
    findings_p1_p2 must not drop them.
    """
    pr = make_pr()
    issue_finding = make_finding(commit_id=None, path=None, line=None)
    review = CodexReview(has_responded=True, findings=[issue_finding])
    decision, payload = decide(pr, review)
    assert decision == "address-review"
    assert payload == [issue_finding]


def test_has_responded_set_on_issue_comment_findings() -> None:
    """Codex P1/P2 posted as a top-level issue comment must flip
    ``has_responded`` to True.

    Regression for codex P1 on PR #12: when codex emitted findings only
    via issue comments (no formal review, no line-anchored comment, no
    +1 reaction, no "Didn't find any major issues" sibling), the parser
    appended them to ``findings`` but left ``has_responded`` False.
    ``decide()`` checks ``has_responded`` before evaluating findings, so
    the shepherd would loop in ``wait`` instead of routing to
    ``address-review``. The fix sets the flag symmetrically with the
    other source branches.
    """
    pr = make_pr()

    issue_comment_body = (
        "![P1 Badge] Pin tar to >=6.2.1\n\n"
        "Top-level issue comment finding from codex."
    )
    issue_comments = [
        {
            "id": 9001,
            "user": {"login": run_shepherd.REVIEW_BOT},
            "body": issue_comment_body,
            # Strictly newer than HEAD_PUSHED_AT so _iso_gt returns True.
            "created_at": "2026-04-29T11:30:00Z",
        }
    ]

    def fake_gh_api_json(args: list[str]):
        # First arg is the endpoint path; route by suffix.
        endpoint = args[0]
        if endpoint.endswith("/reviews"):
            return []
        if endpoint.endswith(f"/pulls/{pr.number}/comments"):
            return []
        if endpoint.endswith(f"/issues/{pr.number}/comments"):
            return issue_comments
        if "/issues/comments/" in endpoint and endpoint.endswith("/reactions"):
            return []
        return []

    with patch.object(run_shepherd, "_gh_api_json", side_effect=fake_gh_api_json):
        review = run_shepherd.read_codex_review(pr)

    assert review.has_responded is True
    assert len(review.findings) == 1
    finding = review.findings[0]
    assert finding.severity == "P1"
    assert finding.commit_id is None
    assert finding.path is None
    assert finding.line is None

    # Sanity: feeding this review into decide() with a mergeable PR must
    # route to address-review, not wait.
    decision, payload = decide(pr, review)
    assert decision == "address-review"
    assert payload == [finding]


# ---------------------------------------------------------------------------
# T6: outer-loop 3-attempt cap
# ---------------------------------------------------------------------------
def test_outer_loop_review_stuck_after_three_attempts(tmp_path, monkeypatch) -> None:
    """Drive process_one with three consecutive address-review returns.

    Tick 1: counter=0 -> call -> counter=1
    Tick 2: counter=1 -> call -> counter=2
    Tick 3: counter=2 -> call -> counter=3
    Tick 4: counter=3 -> flip to review-stuck, NO call
    """
    ref = make_ref(tmp_path)

    findings = [make_finding()]
    pr = make_pr()
    review = CodexReview(has_responded=True, findings=findings)

    apply_calls: list[tuple] = []
    trigger_calls: list[PRSnapshot] = []

    def fake_apply_followup(service, slug, payload, skip_subprocess=False, state_dir=None):
        apply_calls.append((service, slug, payload, skip_subprocess, state_dir))
        return {"p1": True, "p2": False, "summaries": ["fix it"]}

    def fake_trigger_review(pr):
        trigger_calls.append(pr)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=fake_apply_followup), \
         patch.object(run_shepherd, "trigger_review", side_effect=fake_trigger_review):

        # Tick 1
        result = process_one(ref, skip_subprocess=True)
        assert result.decision == "address-review"
        assert read_status(ref)["review_attempts"] == 1
        assert read_status(ref)["status"] == "implemented"

        # Re-load ref counter from disk like the cron would.
        ref.review_attempts = read_status(ref)["review_attempts"]

        # Tick 2
        result = process_one(ref, skip_subprocess=True)
        assert result.decision == "address-review"
        assert read_status(ref)["review_attempts"] == 2
        ref.review_attempts = 2

        # Tick 3
        result = process_one(ref, skip_subprocess=True)
        assert result.decision == "address-review"
        assert read_status(ref)["review_attempts"] == 3
        ref.review_attempts = 3

        # Tick 4 — should flip to review-stuck, NOT call apply_followup
        # and NOT post `@claude review` (no fix-up push happened).
        result = process_one(ref, skip_subprocess=True)
        assert result.decision == "review-stuck"
        assert read_status(ref)["status"] == "review-stuck"

    # Three followup attempts, no fourth — and one `@claude review`
    # trigger per successful followup, no trigger on the review-stuck
    # flip.
    assert len(apply_calls) == 3
    assert len(trigger_calls) == 3


# ---------------------------------------------------------------------------
# T7a: happy-path E2E — clean review -> merge
# ---------------------------------------------------------------------------
def test_happy_path_clean_review_to_merge(tmp_path) -> None:
    """Tick 1: codex pending (wait). Tick 2: codex clean (merge)."""
    ref = make_ref(tmp_path)

    pr_pending = make_pr(checks_green=True)
    review_pending = CodexReview(has_responded=False, findings=[])

    pr_clean = make_pr(checks_green=True)
    review_clean = CodexReview(has_responded=True, findings=[])

    # First tick: codex pending.
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr_pending), \
         patch.object(run_shepherd, "read_codex_review", return_value=review_pending), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "wait"
    # Status not changed.
    assert read_status(ref)["status"] == "implemented"

    # Second tick: codex clean -> merge.
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr_clean), \
         patch.object(run_shepherd, "read_codex_review", return_value=review_clean), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "merge_pr",
                      return_value=(True, "deadbeef" + "0" * 32)):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "merge"
    final = read_status(ref)
    assert final["status"] == "merged"
    assert final["merge_commit"] == "deadbeef" + "0" * 32
    assert "merged_at" in final
    # review_attempts is cleared on merge.
    assert "review_attempts" not in final


# ---------------------------------------------------------------------------
# T7b: loop path — P1 -> followup -> re-eval -> merge
# ---------------------------------------------------------------------------
def test_loop_path_p1_then_followup_then_merge(tmp_path) -> None:
    """Tick 1: P1 finding -> address-review (counter 0->1, status implemented).
    Tick 2: codex pending on new head -> wait.
    Tick 3: codex clean on new head -> merge.
    """
    ref = make_ref(tmp_path)

    new_head_sha = "c" * 40

    # Tick 1: codex P1 on initial head -> address-review.
    pr_t1 = make_pr(head_sha=HEAD_SHA, checks_green=True)
    review_t1 = CodexReview(
        has_responded=True,
        findings=[make_finding(severity="P1", commit_id=HEAD_SHA)],
    )
    apply_calls: list[tuple] = []
    trigger_calls: list[PRSnapshot] = []

    def fake_apply_followup(service, slug, payload, skip_subprocess=False, state_dir=None):
        apply_calls.append((service, slug, len(payload), skip_subprocess, state_dir))
        return {"p1": True, "p2": False, "summaries": ["fix it"]}

    def fake_trigger_review(pr):
        trigger_calls.append(pr)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr_t1), \
         patch.object(run_shepherd, "read_codex_review", return_value=review_t1), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=fake_apply_followup), \
         patch.object(run_shepherd, "trigger_review", side_effect=fake_trigger_review):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "address-review"
    assert len(apply_calls) == 1
    # Each successful followup must trigger a fresh review.
    assert len(trigger_calls) == 1
    assert trigger_calls[0] is pr_t1
    after_t1 = read_status(ref)
    assert after_t1["status"] == "implemented"
    assert after_t1["review_attempts"] == 1

    # Reload counter the way the cron would.
    ref.review_attempts = after_t1["review_attempts"]

    # Tick 2: implementer pushed a new commit. Codex re-reviews are still
    # propagating, so codex.has_responded is False on the new head.
    pr_t2 = make_pr(head_sha=new_head_sha, checks_green=True)
    review_t2 = CodexReview(has_responded=False, findings=[])
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr_t2), \
         patch.object(run_shepherd, "read_codex_review", return_value=review_t2), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "wait"
    after_t2 = read_status(ref)
    # Counter unchanged on wait.
    assert after_t2["review_attempts"] == 1
    assert after_t2["status"] == "implemented"

    # Tick 3: codex re-reviewed clean on new head -> merge.
    pr_t3 = make_pr(head_sha=new_head_sha, checks_green=True)
    review_t3 = CodexReview(has_responded=True, findings=[])
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr_t3), \
         patch.object(run_shepherd, "read_codex_review", return_value=review_t3), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "merge_pr",
                      return_value=(True, "feedface" + "0" * 32)):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "merge"
    final = read_status(ref)
    assert final["status"] == "merged"
    assert final["merge_commit"] == "feedface" + "0" * 32


# ---------------------------------------------------------------------------
# Additional outer-loop branches for coverage
# ---------------------------------------------------------------------------
def test_process_one_flip_to_merged_writes_status(tmp_path) -> None:
    """Human merged the PR between ticks — proposal flips to merged."""
    ref = make_ref(tmp_path)
    pr = make_pr(merged=True, merge_commit="abc123" + "0" * 34)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review",
                      return_value=CodexReview(False, [])), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "flip-to-merged"
    final = read_status(ref)
    assert final["status"] == "merged"
    assert final["merge_commit"] == "abc123" + "0" * 34
    # review_attempts is cleared on terminal status.
    assert "review_attempts" not in final


def test_process_one_flip_to_rejected_writes_status(tmp_path) -> None:
    """Human closed the PR without merging — proposal flips to rejected."""
    ref = make_ref(tmp_path)
    pr = make_pr(closed_unmerged=True)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review",
                      return_value=CodexReview(False, [])), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "flip-to-rejected"
    final = read_status(ref)
    assert final["status"] == "rejected"
    assert "without merging" in (final.get("notes") or "")


def test_process_one_merge_failure_falls_back_to_wait(tmp_path) -> None:
    """gh pr merge non-zero (HEAD-SHA mismatch / branch protection) -> wait."""
    ref = make_ref(tmp_path)
    pr = make_pr(checks_green=True)
    review = CodexReview(has_responded=True, findings=[])

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "merge_pr", return_value=(False, None)):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "wait"
    # Status unchanged — merge will be retried next tick.
    assert read_status(ref)["status"] == "implemented"


def test_outer_loop_does_not_count_attempt_on_subprocess_failure(tmp_path) -> None:
    """Transient subprocess plumbing failures must NOT consume review_attempts.

    Regression for codex P1 on PR #12: ``process_one`` previously flipped
    status to ``review-fixing`` and incremented the counter BEFORE
    calling ``apply_followup`` — so a transient auth / network / branch
    protection error in the implementer push wasted one of the three
    cap slots without producing a new commit, and could falsely drive
    proposals to terminal ``review-stuck``.

    With the fix, ``apply_followup`` raises ``FollowupSubprocessError``
    on non-zero exit and the outer loop catches it: for ``transient=True``
    (auth/network/etc.), the attempt counter and on-disk status are left
    exactly as they were so the next tick retries cleanly.
    """
    ref = make_ref(tmp_path, review_attempts=1)
    pr = make_pr()
    findings = [make_finding()]
    review = CodexReview(has_responded=True, findings=findings)

    def boom(*_a, **_kw):
        # Default constructor → transient=True. Plain plumbing failure.
        raise run_shepherd.FollowupSubprocessError(
            "implementer follow-up exited non-zero (1)"
        )

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=boom):
        result = process_one(ref, skip_subprocess=True)

    # decide() said address-review, but the subprocess failed; the outer
    # loop treats it as a transient wait and leaves state alone.
    assert result.decision == "wait"
    final = read_status(ref)
    # Counter unchanged — subprocess failure must not burn the budget.
    assert final["review_attempts"] == 1
    # Status unchanged — next tick re-decides from the same starting point.
    assert final["status"] == "implemented"
    # In-memory ref state also untouched.
    assert ref.review_attempts == 1
    assert ref.status == "implemented"


def test_outer_loop_counts_attempt_on_deterministic_subprocess_failure(tmp_path) -> None:
    """Deterministic subprocess failures DO consume review_attempts.

    Codex P1 follow-up on PR #12: re-running an implementer that produced
    no commits (or whose PR branch was deleted on origin) yields the same
    failure every tick. The shepherd previously caught those exits as
    transient and retried forever. The fix: ``FollowupSubprocessError``
    carries a ``transient`` flag; deterministic failures (sentinel exit
    codes 42/43 from the implementer) increment the counter and either
    flip to ``review-fixing`` (still under the cap) or ``review-stuck``
    (cap exhausted).

    Below the cap: counter ticks up, status flips to ``review-fixing``
    then back to ``implemented`` so the next tick re-decides.
    """
    ref = make_ref(tmp_path, review_attempts=1)
    pr = make_pr()
    findings = [make_finding()]
    review = CodexReview(has_responded=True, findings=findings)

    def boom(*_a, **_kw):
        raise run_shepherd.FollowupSubprocessError(
            "implementer follow-up exited non-zero (42)",
            transient=False,
        )

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=boom):
        result = process_one(ref, skip_subprocess=True)

    # Deterministic failure under the cap → wait but counter incremented.
    assert result.decision == "wait"
    final = read_status(ref)
    assert final["review_attempts"] == 2
    # Status returned to `implemented` after the brief `review-fixing` flip
    # so the next tick can re-evaluate (same shape as a successful followup).
    assert final["status"] == "implemented"


def test_outer_loop_flips_to_review_stuck_on_deterministic_at_cap(tmp_path) -> None:
    """Deterministic failure at the cap flips the proposal to review-stuck.

    Counter starts at MAX_REVIEW_ATTEMPTS - 1 = 2. The subprocess fails
    deterministically; the new counter value (3) hits the cap, so the
    proposal flips terminal so a human can intervene instead of the
    shepherd spinning indefinitely.
    """
    ref = make_ref(tmp_path, review_attempts=run_shepherd.MAX_REVIEW_ATTEMPTS - 1)
    pr = make_pr()
    findings = [make_finding()]
    review = CodexReview(has_responded=True, findings=findings)

    def boom(*_a, **_kw):
        raise run_shepherd.FollowupSubprocessError(
            "implementer follow-up exited non-zero (43)",
            transient=False,
        )

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=boom):
        result = process_one(ref, skip_subprocess=True)

    assert result.decision == "review-stuck"
    final = read_status(ref)
    assert final["status"] == "review-stuck"
    assert final["review_attempts"] == run_shepherd.MAX_REVIEW_ATTEMPTS
    assert "deterministic" in (final.get("notes") or "").lower()


def test_apply_followup_raises_transient_on_generic_failure(monkeypatch) -> None:
    """returncode=1 (or any non-sentinel non-zero) -> transient=True.

    Generic failures (auth blip, network glitch, branch protection
    rejection) cannot be distinguished from each other by exit code alone,
    so the safer default is ``transient=True`` and the shepherd retries
    next tick without consuming a review_attempts slot.
    """
    findings = [make_finding()]

    async def fake_format(_findings):
        return {"p1": True, "p2": False, "summaries": ["fix"]}

    class _Result:
        returncode = 1

    def fake_run(cmd, check=False, text=False, **_kwargs):
        return _Result()

    with patch.object(run_shepherd, "_format_bundle_via_sdk", fake_format), \
         patch.object(run_shepherd.subprocess, "run", fake_run):
        with pytest.raises(run_shepherd.FollowupSubprocessError) as exc:
            run_shepherd.apply_followup(
                "mctl-web", "test-slug", findings,
            )
    assert exc.value.transient is True


def test_apply_followup_raises_deterministic_on_no_commits(monkeypatch) -> None:
    """returncode=42 -> transient=False (no follow-up commits sentinel).

    The implementer ran but committed nothing; re-running with the same
    findings will reproduce the same outcome, so the shepherd MUST count
    this as a real address-review attempt and eventually flip the proposal
    to ``review-stuck`` once the cap is hit. Same reasoning for code 43
    (PR branch missing on origin) and code 44 (bounded operation timed out).
    """
    findings = [make_finding()]

    async def fake_format(_findings):
        return {"p1": True, "p2": False, "summaries": ["fix"]}

    for code in (
        run_implementer.EXIT_NO_FOLLOWUP_COMMITS,
        run_implementer.EXIT_BRANCH_MISSING_ON_ORIGIN,
        run_implementer.EXIT_OPERATION_TIMEOUT,
    ):
        class _Result:
            returncode = code

        def fake_run(cmd, check=False, text=False, **_kwargs):
            return _Result()

        with patch.object(run_shepherd, "_format_bundle_via_sdk", fake_format), \
             patch.object(run_shepherd.subprocess, "run", fake_run):
            with pytest.raises(run_shepherd.FollowupSubprocessError) as exc:
                run_shepherd.apply_followup(
                    "mctl-web", "test-slug", findings,
                )
        assert exc.value.transient is False, (
            f"exit code {code} must be classified as deterministic"
        )


def test_process_one_pr_unfetchable_returns_wait(tmp_path) -> None:
    """find_pr_for_proposal -> None (network blip etc.) is a soft wait."""
    ref = make_ref(tmp_path)
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=None):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "wait"
    assert result.error
    # Status unchanged.
    assert read_status(ref)["status"] == "implemented"


def _git(*args: str, cwd: Path) -> None:
    """Tiny git wrapper for test fixtures (no logging, no capture)."""
    import subprocess as _sp
    _sp.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_has_new_commits_against_pre_sdk_head(tmp_path) -> None:
    """_has_new_commits(base=old_head) must distinguish a no-op SDK run
    from a real follow-up commit on an existing PR branch.

    Build a repo where HEAD already has commits ahead of origin/HEAD AND
    of origin/<branch> (the pre-existing implementer commit). Then:
      - capture HEAD via _capture_head_sha,
      - simulate "no SDK commit" -> _has_new_commits(base=old_head) is False,
      - simulate "SDK committed" -> _has_new_commits(base=old_head) is True.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)

    # Simulate the original implementer commit already on the PR branch.
    (repo / "feat.txt").write_text("v1\n")
    _git("add", "feat.txt", cwd=repo)
    _git("commit", "-q", "-m", "feat: original implementer commit", cwd=repo)

    old_head = run_implementer._capture_head_sha(repo)
    assert old_head and len(old_head) == 40

    # No-op SDK run: HEAD is unchanged -> _has_new_commits must be False.
    assert run_implementer._has_new_commits(repo, base=old_head) is False

    # SDK pushed a follow-up commit -> _has_new_commits must be True.
    (repo / "feat.txt").write_text("v2\n")
    _git("add", "feat.txt", cwd=repo)
    _git("commit", "-q", "-m", "fix: address codex P1", cwd=repo)
    assert run_implementer._has_new_commits(repo, base=old_head) is True


def test_apply_followup_skip_subprocess_does_not_fork(monkeypatch) -> None:
    """skip_subprocess=True in apply_followup must not call subprocess.run."""
    findings = [make_finding()]

    async def fake_format(_findings):
        return {"p1": True, "p2": False, "summaries": ["fix"]}

    calls: list = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("subprocess.run should not be called when skip_subprocess=True")

    with patch.object(run_shepherd, "_format_bundle_via_sdk", fake_format), \
         patch.object(run_shepherd.subprocess, "run", fake_run):
        bundle = run_shepherd.apply_followup(
            "mctl-web", "test-slug", findings, skip_subprocess=True,
        )
    assert bundle == {"p1": True, "p2": False, "summaries": ["fix"]}
    assert calls == []


def test_main_dry_run_skips_sdk_auth(tmp_path, monkeypatch) -> None:
    """`--dry-run` is documented as discovery-only and must run in
    environments without Claude credentials (read-only ops checks, CI
    inventory). ensure_auth_for_sdk() must therefore NOT be called.
    """
    # Empty state dir -> _discover_refs returns []. main() exits early
    # but only AFTER the auth gate; that gate must remain bypassed.
    state_dir = tmp_path / "agents-state"
    state_dir.mkdir()

    monkeypatch.setattr(
        "sys.argv",
        ["run_shepherd", "--dry-run", "--state-dir", str(state_dir)],
    )

    with patch.object(run_shepherd, "ensure_auth_for_sdk") as mocked_auth, \
         patch.object(run_shepherd, "_discover_refs", return_value=[]):
        run_shepherd.main()

    mocked_auth.assert_not_called()


def test_main_non_dry_run_calls_sdk_auth(tmp_path, monkeypatch) -> None:
    """Sanity counterpart: without --dry-run, ensure_auth_for_sdk IS called."""
    state_dir = tmp_path / "agents-state"
    state_dir.mkdir()

    monkeypatch.setattr(
        "sys.argv",
        ["run_shepherd", "--state-dir", str(state_dir)],
    )

    with patch.object(run_shepherd, "ensure_auth_for_sdk") as mocked_auth, \
         patch.object(run_shepherd, "_discover_refs", return_value=[]):
        run_shepherd.main()

    mocked_auth.assert_called_once()


def test_find_pr_uses_explicit_state_dir(tmp_path) -> None:
    """find_pr_for_proposal must read the proposal under the explicit
    state_dir argument, not under DEFAULT_STATE_DIR.

    Regression for codex P1 on PR #12: process_one() previously called
    find_pr_for_proposal(service, slug) without forwarding --state-dir,
    so a CLI invocation with a custom path silently re-read the env
    default and produced "could not fetch PR snapshot" warnings on
    valid proposals.
    """
    custom_dir = tmp_path / "custom-state"
    proposal_dir = custom_dir / "mctl-web" / "proposals" / "wrangler-cve"
    proposal_dir.mkdir(parents=True)
    pr_url = "https://github.com/mctlhq/mctl-web/pull/77"
    (proposal_dir / ".status.yaml").write_text(
        yaml.safe_dump(
            {
                "status": "implemented",
                "updated_at": "2026-04-29T09:00:00Z",
                "updated_by": "mctl-agents[bot]",
                "pr": pr_url,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Point DEFAULT_STATE_DIR at an unrelated tree to prove the helper
    # is NOT silently falling back to it.
    bogus_default = tmp_path / "bogus-default"
    bogus_default.mkdir()

    captured: dict = {}
    sentinel = object()

    def fake_fetch(repo: str, number: int):
        captured["repo"] = repo
        captured["number"] = number
        return sentinel

    with patch.object(run_shepherd, "DEFAULT_STATE_DIR", bogus_default), \
         patch.object(run_shepherd, "_fetch_pr_snapshot", side_effect=fake_fetch):
        result = run_shepherd.find_pr_for_proposal(
            "mctl-web", "wrangler-cve", state_dir=custom_dir,
        )

    assert result is sentinel
    assert captured == {"repo": "mctlhq/mctl-web", "number": 77}

    # Sanity: with no state_dir argument, the helper falls back to
    # DEFAULT_STATE_DIR, which we pointed at an empty tree -> None.
    with patch.object(run_shepherd, "DEFAULT_STATE_DIR", bogus_default), \
         patch.object(run_shepherd, "_fetch_pr_snapshot",
                      side_effect=AssertionError("must not fetch — no PR url")):
        fallback = run_shepherd.find_pr_for_proposal("mctl-web", "wrangler-cve")
    assert fallback is None


def test_process_one_forwards_state_dir(tmp_path) -> None:
    """process_one() must forward state_dir to find_pr_for_proposal.

    Without this plumbing, a CLI run with --state-dir <X> reads the env
    default in the lookup and reports valid proposals as wait/error.
    """
    ref = make_ref(tmp_path)
    custom_dir = tmp_path / "explicit-state"
    custom_dir.mkdir()

    captured: dict = {}

    def fake_find(service: str, slug: str, state_dir=None):
        captured["service"] = service
        captured["slug"] = slug
        captured["state_dir"] = state_dir
        return None  # short-circuit out of process_one with a wait

    with patch.object(run_shepherd, "find_pr_for_proposal", side_effect=fake_find):
        result = process_one(ref, skip_subprocess=True, state_dir=custom_dir)

    assert result.decision == "wait"
    assert captured["state_dir"] == custom_dir


def test_apply_followup_invokes_implementer_subprocess(monkeypatch) -> None:
    """skip_subprocess=False forks `python -m orchestrator.run_implementer`
    with --review-feedback pointed at the bundle JSON.

    The temp file is deleted by apply_followup after the subprocess returns,
    so we capture its contents from inside the fake subprocess call while it
    still exists on disk.
    """
    findings = [make_finding()]

    async def fake_format(_findings):
        return {"p1": True, "p2": False, "summaries": ["fix"]}

    captured: dict = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, check=False, text=False, **_kwargs):
        captured["cmd"] = list(cmd)
        # Read the bundle file while it still exists — apply_followup deletes
        # it after this call returns (try/finally cleanup).
        idx = list(cmd).index("--review-feedback")
        with open(cmd[idx + 1], encoding="utf-8") as f:
            captured["bundle_on_disk"] = json.load(f)
        return _Result()

    with patch.object(run_shepherd, "_format_bundle_via_sdk", fake_format), \
         patch.object(run_shepherd.subprocess, "run", fake_run):
        bundle = run_shepherd.apply_followup(
            "mctl-web", "test-slug", findings,
        )

    assert bundle == {"p1": True, "p2": False, "summaries": ["fix"]}
    cmd = captured["cmd"]
    # Last two args are --review-feedback <path>.
    assert "orchestrator.run_implementer" in cmd
    assert "--service" in cmd and "mctl-web" in cmd
    assert "--slug" in cmd and "test-slug" in cmd
    assert "--review-feedback" in cmd
    # The bundle was correctly serialised to disk and the JSON round-trips.
    assert captured["bundle_on_disk"] == bundle


def test_apply_followup_propagates_state_dir(tmp_path) -> None:
    """apply_followup must forward a non-default state_dir to the
    implementer subprocess as `--state-dir <path>`, and omit the flag
    when state_dir matches the implementer's DEFAULT_STATE_DIR (no-op
    noise on the command line) or is None.

    Regression for codex P2 on PR #12: a shepherd invocation with a
    custom --state-dir previously fired the implementer with no
    --state-dir, so the implementer resolved proposals from its own
    DEFAULT_STATE_DIR, failed to find the target in
    `implemented/review-fixing`, and exited non-zero.
    """
    findings = [make_finding()]
    custom_dir = tmp_path / "explicit-state"
    custom_dir.mkdir()

    async def fake_format(_findings):
        return {"p1": True, "p2": False, "summaries": ["fix"]}

    captured: dict = {}

    class _Result:
        returncode = 0

    def fake_run(cmd, check=False, text=False, **_kwargs):
        captured["cmd"] = list(cmd)
        return _Result()

    # Custom state_dir -> --state-dir is appended.
    with patch.object(run_shepherd, "_format_bundle_via_sdk", fake_format), \
         patch.object(run_shepherd.subprocess, "run", fake_run):
        run_shepherd.apply_followup(
            "mctl-web", "test-slug", findings,
            state_dir=custom_dir,
        )
    cmd = captured["cmd"]
    assert "--state-dir" in cmd
    idx = cmd.index("--state-dir")
    assert cmd[idx + 1] == str(custom_dir)

    # Default state_dir -> flag omitted (would be a no-op).
    captured.clear()
    with patch.object(run_shepherd, "_format_bundle_via_sdk", fake_format), \
         patch.object(run_shepherd.subprocess, "run", fake_run):
        run_shepherd.apply_followup(
            "mctl-web", "test-slug", findings,
            state_dir=run_implementer.DEFAULT_STATE_DIR,
        )
    assert "--state-dir" not in captured["cmd"]

    # No state_dir argument -> flag omitted.
    captured.clear()
    with patch.object(run_shepherd, "_format_bundle_via_sdk", fake_format), \
         patch.object(run_shepherd.subprocess, "run", fake_run):
        run_shepherd.apply_followup(
            "mctl-web", "test-slug", findings,
        )
    assert "--state-dir" not in captured["cmd"]


# ---------------------------------------------------------------------------
# Dead-letter `in-progress` recovery
#
# Tier 2 sets `in-progress` BEFORE pushing, then flips to `implemented`
# with `pr:` after `gh pr create`. If the second update_status_yaml is
# interrupted, the proposal stays `in-progress` with a real PR URL. If
# the operator then closes that PR without merging, shepherd needs to
# claim the dead-letter and flip to `rejected` — but only when the PR
# is CLOSED-unmerged. OPEN/MERGED PRs belong to Tier 2 mid-flight.
# Reference incident: mctl-openclaw/upgrade-to-2026-4-27 (PR
# mctlhq/mctl-openclaw#15, closed 2026-04-30).
# ---------------------------------------------------------------------------
def test_discover_refs_includes_in_progress_with_closed_pr(tmp_path) -> None:
    """Discovery picks up in-progress proposals that have a PR URL.

    Pins the `SHEPHERD_INPUT_STATUSES` scope expansion: an in-progress
    proposal with a recorded PR is enrolled, alongside the steady-state
    `implemented` case.
    """
    make_status_yaml(
        tmp_path,
        service="mctl-openclaw",
        slug="upgrade-to-2026-4-27",
        status="in-progress",
        pr="https://github.com/mctlhq/mctl-openclaw/pull/15",
    )
    make_status_yaml(
        tmp_path,
        service="mctl-web",
        slug="baseline",
        status="implemented",
    )

    refs = run_shepherd._discover_refs(tmp_path)

    by_slug = {r.slug: r for r in refs}
    assert set(by_slug) == {"upgrade-to-2026-4-27", "baseline"}
    assert by_slug["upgrade-to-2026-4-27"].status == "in-progress"
    assert by_slug["upgrade-to-2026-4-27"].pr_url == (
        "https://github.com/mctlhq/mctl-openclaw/pull/15"
    )


def test_discover_refs_skips_in_progress_with_no_pr(tmp_path) -> None:
    """The pre-PR Tier-2-mid-creation case is filtered at discovery.

    Pins the existing `if not pr_url: continue` guard against the new
    scope expansion: an in-progress proposal without a `pr:` field
    means Tier 2 is still constructing the PR — shepherd must not
    enrol it (no work to do, would race the implementer).
    """
    make_status_yaml(
        tmp_path,
        service="mctl-openclaw",
        slug="mid-creation",
        status="in-progress",
        pr=None,
    )

    refs = run_shepherd._discover_refs(tmp_path)

    assert refs == []


def test_process_one_in_progress_with_open_pr_repairs(tmp_path) -> None:
    """OPEN PR proves Tier 2 finished, so the dropped YAML write is healed."""
    ref = make_ref(tmp_path, status="in-progress")
    status = read_status(ref)
    status["attempt"] = {
        "id": "attempt-1",
        "started_at": "2026-04-29T09:00:00Z",
        "expires_at": "2026-04-29T11:10:00Z",
    }
    ref.status_path.write_text(
        yaml.safe_dump(status, sort_keys=False),
        encoding="utf-8",
    )
    pr = make_pr()  # default: state="OPEN", merged=False, closed_unmerged=False

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review",
                      return_value=CodexReview(False, [])), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)):
        result = process_one(ref, skip_subprocess=True)

    assert result.decision == "wait"
    final = read_status(ref)
    assert final["status"] == "implemented"
    assert final["attempt"]["finished_at"]


def test_process_one_in_progress_with_fresh_lease_waits(tmp_path) -> None:
    """Shepherd does not race a still-active implementer after PR creation."""
    ref = make_ref(tmp_path, status="in-progress")
    status = read_status(ref)
    status["attempt"] = {
        "id": "attempt-live",
        "started_at": "2999-01-01T00:00:00Z",
        "expires_at": "2999-01-01T02:10:00Z",
    }
    ref.status_path.write_text(
        yaml.safe_dump(status, sort_keys=False),
        encoding="utf-8",
    )
    pr = make_pr()
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "wait"
    assert "lease" in (result.notes or "")
    assert read_status(ref)["status"] == "in-progress"


def test_process_one_in_progress_with_merged_pr_repairs(tmp_path) -> None:
    """MERGED PR is authoritative even when YAML still says in-progress."""
    ref = make_ref(tmp_path, status="in-progress")
    pr = make_pr(merged=True, merge_commit="deadbeef" * 5)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review",
                      return_value=CodexReview(False, [])), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)):
        result = process_one(ref, skip_subprocess=True)

    assert result.decision == "flip-to-merged"
    assert read_status(ref)["status"] == "merged"


def test_process_one_in_progress_dead_letter_flips_to_rejected(tmp_path) -> None:
    """End-to-end: in-progress + closed-unmerged PR -> rejected.

    Mirrors the mctl-openclaw/upgrade-to-2026-4-27 real-world path:
    Tier 2 left status=in-progress with a PR URL, operator closed the
    PR without merging, shepherd flips to rejected. Verifies that
    update_status preserves `pr:` and clears `review_attempts`.
    """
    ref = make_ref(tmp_path, status="in-progress", review_attempts=2)
    pr = make_pr(closed_unmerged=True)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review",
                      return_value=CodexReview(False, [])), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)):
        result = process_one(ref, skip_subprocess=True)

    assert result.decision == "flip-to-rejected"
    final = read_status(ref)
    assert final["status"] == "rejected"
    assert "without merging" in (final.get("notes") or "")
    assert final["pr"] == PR_URL
    # review_attempts is cleared on terminal-status flip; either absent
    # or explicitly null is acceptable.
    assert not final.get("review_attempts")


# ---------------------------------------------------------------------------
# trigger_review — fix for the fix-loop stall observed on mctl-docs#7
# (memory: feedback_shepherd_codex_review_repost).
# ---------------------------------------------------------------------------
def test_trigger_review_posts_claude_review_comment(monkeypatch) -> None:
    """trigger_review must shell out to `gh pr comment` with `@claude review`.

    The review bot only re-reviews on an explicit @-mention. Without
    this comment after a fix-up push, the shepherd loop stalls forever
    on the new head SHA.
    """
    pr = make_pr()
    captured: list[list[str]] = []

    def fake_run(cmd, cwd=None, check=True):
        captured.append(cmd)
        class _Proc:
            stdout = ""
            stderr = ""
        return _Proc()

    monkeypatch.setattr(run_shepherd, "_run", fake_run)
    run_shepherd.trigger_review(pr)

    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[:3] == ["gh", "pr", "comment"]
    assert cmd[-2:] == ["--body", "@claude review"]
    # Must target the right PR URL.
    assert f"https://github.com/{pr.repo}/pull/{pr.number}" in cmd


def test_trigger_review_swallows_subprocess_failure(monkeypatch, capsys) -> None:
    """A failed `gh pr comment` must NOT raise — best-effort by design.

    If the trigger raised, a single transient GitHub blip would crash
    the entire shepherd tick after a successful implementer push,
    leaving .status.yaml in whatever state the partial run left.
    """
    import subprocess as sp

    pr = make_pr()

    def boom(cmd, cwd=None, check=True):
        raise sp.CalledProcessError(returncode=1, cmd=cmd, stderr="rate limit exceeded")

    monkeypatch.setattr(run_shepherd, "_run", boom)
    # Must not raise.
    run_shepherd.trigger_review(pr)

    out = capsys.readouterr().out
    assert "warn:" in out
    assert "rate limit exceeded" in out


def test_trigger_review_swallows_oserror(monkeypatch, capsys) -> None:
    """A missing `gh` binary (FileNotFoundError) must NOT raise either.

    The except clause widened to (CalledProcessError, OSError) so a
    stripped image without `gh` on PATH cannot crash the tick after a
    real fix-up push — the exact failure mode trigger_review exists to
    prevent. Regression for claude review P2 on PR #25.
    """
    pr = make_pr()

    def missing_gh(cmd, cwd=None, check=True):
        raise FileNotFoundError(2, "No such file or directory: 'gh'")

    monkeypatch.setattr(run_shepherd, "_run", missing_gh)
    # Must not raise.
    run_shepherd.trigger_review(pr)

    out = capsys.readouterr().out
    assert "warn:" in out
    assert "gh" in out


def test_process_one_does_not_trigger_review_on_followup_failure(tmp_path) -> None:
    """A failed apply_followup must NOT lead to a stray `@claude review`.

    Posting `@claude review` only makes sense after a successful fix-up
    push. On a transient failure the next tick retries the followup; on
    a deterministic failure the proposal moves toward review-stuck.
    Either way, no new head was pushed, so the review bot has nothing
    new to look at.
    """
    ref = make_ref(tmp_path)
    pr = make_pr()
    review = CodexReview(has_responded=True, findings=[make_finding()])
    trigger_calls: list = []

    def boom(*args, **kwargs):
        raise run_shepherd.FollowupSubprocessError("transient", transient=True)

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=boom), \
         patch.object(run_shepherd, "trigger_review",
                      side_effect=lambda pr_: trigger_calls.append(pr_)):
        result = process_one(ref, skip_subprocess=True)

    assert result.decision == "wait"
    assert trigger_calls == []


# ---------------------------------------------------------------------------
# _extract_severity — format compatibility tests
# ---------------------------------------------------------------------------
def test_extract_severity_codex_badge_format() -> None:
    """Legacy Codex badge format is still recognized."""
    from orchestrator.run_shepherd import _extract_severity
    assert _extract_severity("![P1 Badge](url) Pin tar to >=6.2.1") == "P1"
    assert _extract_severity("Some text\n![P2 Badge](url)\nmore") == "P2"
    assert _extract_severity("![P3 Badge]") == "P3"


def test_extract_severity_claude_bold_prefix() -> None:
    """Claude review inline comments use **P2 — title** (bold prefix)."""
    from orchestrator.run_shepherd import _extract_severity
    assert _extract_severity("**P2 — PeerCache never activated**\n\ndesc") == "P2"
    assert _extract_severity("**P1 — Security hole**") == "P1"
    assert _extract_severity("**P3 — Minor nit**") == "P3"
    # Bold prefix embedded mid-body (top-level review body format)
    body = "Has P1/P2 findings, changes requested: 1 P2.\n\n**P2 — Title (file:74)**\ndesc"
    assert _extract_severity(body) == "P2"


def test_extract_severity_claude_bold_prefix_hyphen() -> None:
    """Hyphen variant **P2 -** is also recognized."""
    from orchestrator.run_shepherd import _extract_severity
    assert _extract_severity("**P2 - Some finding**") == "P2"


def test_extract_severity_bare_prefix_at_body_start() -> None:
    """Bare P2 — at the very start of the body (no bold asterisks)."""
    from orchestrator.run_shepherd import _extract_severity
    assert _extract_severity("P2 — finding at top") == "P2"
    assert _extract_severity("P1 — critical") == "P1"


def test_extract_severity_bare_prefix_mid_body() -> None:
    """Bare P2 — at the start of a line in the middle of the body."""
    from orchestrator.run_shepherd import _extract_severity
    body = "Summary line.\n\nP2 — finding on second paragraph"
    assert _extract_severity(body) == "P2"


def test_extract_severity_no_match() -> None:
    """Bodies with no severity marker return None."""
    from orchestrator.run_shepherd import _extract_severity
    assert _extract_severity("No P1/P2 findings (2 P3). Good to merge.") is None
    assert _extract_severity("") is None
    assert _extract_severity("Some random review text") is None
    # P3 in prose does not count as a severity marker
    assert _extract_severity("There are P3 nits to consider") is None
