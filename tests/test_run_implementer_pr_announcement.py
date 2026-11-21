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
