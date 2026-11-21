"""One idempotent comment on the source issue after the implementer opens a
PR (mctl-agents#314)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator import run_implementer as ri

PR_URL = "https://github.com/mctlhq/mctl-telegram/pull/7"


def _ref(tmp_path: Path, source: dict[str, Any] | None) -> ri.ProposalRef:
    d = tmp_path / "mctl-telegram" / "proposals" / "issue-510-x"
    d.mkdir(parents=True)
    status: dict[str, Any] = {"status": "accepted"}
    if source is not None:
        status["source"] = source
    (d / ".status.yaml").write_text(yaml.safe_dump(status), encoding="utf-8")
    return ri.ProposalRef(
        service="mctl-telegram", slug="issue-510-x", proposal_dir=d,
        status="accepted", approval_ok=True,
    )


SOURCE = {"type": "github_issue", "repo": "mctlhq/mctl-telegram", "issue": 510}


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"comments": [], "posted": [], "read_error": None}

    def fake_json(cmd: list[str]) -> Any:
        if state["read_error"]:
            raise ri.GitHubPreflightError(state["read_error"])
        return {"comments": [{"body": b} for b in state["comments"]]}

    def fake_run(cmd: list[str], **kw: Any) -> Any:
        state["posted"].append(cmd)
        return None

    monkeypatch.setattr(ri, "_github_json", fake_json)
    monkeypatch.setattr(ri, "_run", fake_run)
    monkeypatch.setattr(ri.policy_checkpoint, "require", lambda d: None)
    monkeypatch.setattr(ri.policy_checkpoint, "checkpoint", lambda *a, **k: None)
    return state


def test_posts_once_with_marker(tmp_path: Path, gh: dict[str, Any]) -> None:
    ref = _ref(tmp_path, SOURCE)
    assert ri._announce_pr_on_source_issue(ref, PR_URL) is True
    (cmd,) = gh["posted"]
    assert cmd[:4] == ["gh", "issue", "comment", "https://github.com/mctlhq/mctl-telegram/issues/510"]
    body = cmd[-1]
    assert PR_URL in body and ri._pr_announcement_marker(ref, PR_URL) in body


def test_rerun_does_not_duplicate(tmp_path: Path, gh: dict[str, Any]) -> None:
    ref = _ref(tmp_path, SOURCE)
    gh["comments"] = ["unrelated", f"text\n\n{ri._pr_announcement_marker(ref, PR_URL)}"]
    assert ri._announce_pr_on_source_issue(ref, PR_URL) is False
    assert gh["posted"] == []


def test_unreadable_comments_do_not_post(tmp_path: Path, gh: dict[str, Any]) -> None:
    ref = _ref(tmp_path, SOURCE)
    gh["read_error"] = "boom"
    assert ri._announce_pr_on_source_issue(ref, PR_URL) is False
    assert gh["posted"] == []


def test_non_issue_proposal_is_skipped(tmp_path: Path, gh: dict[str, Any]) -> None:
    ref = _ref(tmp_path, None)
    assert ri._announce_pr_on_source_issue(ref, PR_URL) is False
    assert gh["posted"] == []


def test_comment_failure_is_swallowed(tmp_path: Path, gh: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    ref = _ref(tmp_path, SOURCE)

    def boom(cmd: list[str], **kw: Any) -> Any:
        raise RuntimeError("gh down")

    monkeypatch.setattr(ri, "_run", boom)
    assert ri._announce_pr_on_source_issue(ref, PR_URL) is False


def test_open_pr_adoption_on_rerun_announces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash after PR creation but before step 9 reruns through the 'open'
    adoption path, which must announce (the marker keeps it idempotent)."""
    ref = _ref(tmp_path, SOURCE)
    monkeypatch.setattr(
        ri, "_preflight_existing_result",
        lambda _ref: ri.ExistingResult(action="open", pr_url=PR_URL, head_sha="abc"),
    )
    monkeypatch.setattr(ri, "_run_implementer_agent",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not run")))
    seen: list[str] = []
    monkeypatch.setattr(ri, "_announce_pr_on_source_issue", lambda r, url: seen.append(url) or True)
    result = ri.implement_one(ref)
    assert result.pr_url == PR_URL
    assert seen == [PR_URL]


def test_implement_attempt_announces_after_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wiring: step 9 of _implement_attempt calls the announcer with the PR."""
    from types import SimpleNamespace

    ref = _ref(tmp_path, SOURCE)
    noop = lambda *a, **k: None  # noqa: E731
    monkeypatch.setattr(ri, "_clone_target", lambda *a, **k: tmp_path)
    monkeypatch.setattr(ri, "_run", noop)
    monkeypatch.setattr(ri, "_stage_implementer_agent", noop)
    monkeypatch.setattr(ri, "_resolve_implementer_service_skills",
                        lambda *a, **k: SimpleNamespace(to_prompt_block=lambda **kw: ""))
    monkeypatch.setattr(ri, "_build_prompt", lambda *a, **k: "")
    monkeypatch.setattr(ri.anyio, "run", noop)
    monkeypatch.setattr(ri, "_has_new_commits", lambda *a, **k: True)
    monkeypatch.setattr(ri, "_detect_chart_major_bumps", lambda *a, **k: [])
    monkeypatch.setattr(ri, "_run_pre_push_typecheck", noop)
    monkeypatch.setattr(ri, "_push_and_open_pr", lambda *a, **k: PR_URL)
    for name in ("_note_pushed_head", "_note_subject_pr", "_note_pushed", "update_status_yaml", "_release_claim"):
        monkeypatch.setattr(ri, name, noop)
    seen: list[str] = []
    monkeypatch.setattr(ri, "_announce_pr_on_source_issue", lambda r, url: seen.append(url) or True)
    obs = ri._AttemptObservations(phase="implement", branch="feat/agents-issue-510-x")
    ctx = SimpleNamespace(context_id="c", trace_id="t", executor=SimpleNamespace(agent="implementer", version="1"))
    result = ri._implement_attempt(
        ref, obs, attempt={}, attempt_id="a", claim_ctx=None,
        execution_context=ctx, prior_rate_limited=None,
    )
    assert result.pr_url == PR_URL
    assert seen == [PR_URL]
