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
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
import yaml

from orchestrator import run_shepherd
from orchestrator.run_shepherd import (
    CodexFinding,
    CodexReview,
    PRSnapshot,
    ProposalRef,
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
    merge_commit: Optional[str] = None,
    merge_state_status: str = "CLEAN",
    checks_green: bool = True,
    head_sha: str = HEAD_SHA,
    head_pushed_at: Optional[str] = HEAD_PUSHED_AT,
    state: Optional[str] = None,
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
    )


def make_finding(
    *,
    severity: str = "P1",
    commit_id: Optional[str] = HEAD_SHA,
    body: str = "![P1 Badge] Pin tar to >=6.2.1",
    path: Optional[str] = "package.json",
    line: Optional[int] = 42,
    created_at: Optional[str] = "2026-04-29T11:00:00Z",
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
    pr: Optional[str] = PR_URL,
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
    pr_url: Optional[str] = PR_URL,
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
    assert decide(pr, review) == ("merge", None)


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

    def fake_apply_followup(service, slug, payload, skip_subprocess=False):
        apply_calls.append((service, slug, payload, skip_subprocess))
        return {"p1": True, "p2": False, "summaries": ["fix it"]}

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=fake_apply_followup):

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

        # Tick 4 — should flip to review-stuck, NOT call apply_followup.
        result = process_one(ref, skip_subprocess=True)
        assert result.decision == "review-stuck"
        assert read_status(ref)["status"] == "review-stuck"

    # Three followup attempts, no fourth.
    assert len(apply_calls) == 3


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

    def fake_apply_followup(service, slug, payload, skip_subprocess=False):
        apply_calls.append((service, slug, len(payload), skip_subprocess))
        return {"p1": True, "p2": False, "summaries": ["fix it"]}

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr_t1), \
         patch.object(run_shepherd, "read_codex_review", return_value=review_t1), \
         patch.object(run_shepherd, "read_copilot_review",
                      return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=fake_apply_followup):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "address-review"
    assert len(apply_calls) == 1
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


def test_process_one_pr_unfetchable_returns_wait(tmp_path) -> None:
    """find_pr_for_proposal -> None (network blip etc.) is a soft wait."""
    ref = make_ref(tmp_path)
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=None):
        result = process_one(ref, skip_subprocess=True)
    assert result.decision == "wait"
    assert result.error
    # Status unchanged.
    assert read_status(ref)["status"] == "implemented"


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
        with open(cmd[idx + 1], "r", encoding="utf-8") as f:
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
