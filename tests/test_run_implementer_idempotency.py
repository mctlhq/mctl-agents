"""Regression tests for GitHub-first implementer idempotency."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from orchestrator import run_implementer


def make_ref(tmp_path: Path, status: str = "accepted") -> run_implementer.ProposalRef:
    proposal = tmp_path / "mctl-agents" / "proposals" / "incident-example"
    proposal.mkdir(parents=True)
    (proposal / ".status.yaml").write_text(
        yaml.safe_dump({
            "status": status,
            "pr": "https://github.com/mctlhq/mctl-agents/pull/1",
            "notes": "keep me",
            "source": {"type": "github_issue", "issue": 10},
        }),
        encoding="utf-8",
    )
    return run_implementer.ProposalRef(
        service="mctl-agents",
        slug="incident-example",
        proposal_dir=proposal,
        status=status,
    )


def read_status(ref: run_implementer.ProposalRef) -> dict:
    return yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))


def test_status_transition_preserves_pr_notes_and_source(tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    run_implementer.update_status_yaml(ref, "in-progress")
    status = read_status(ref)
    assert status["pr"].endswith("/pull/1")
    assert status["notes"] == "keep me"
    assert status["source"]["issue"] == 10


def test_preflight_adopts_open_pr(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    marker = "agents-state/mctl-agents/proposals/incident-example/"
    monkeypatch.setattr(
        run_implementer,
        "_github_json",
        lambda _cmd: [{
            "number": 71,
            "url": "https://github.com/mctlhq/mctl-agents/pull/71",
            "state": "OPEN",
            "mergedAt": None,
            "headRefName": "feat/agents-incident-example",
            "headRefOid": "abc123",
            "body": f"Spec: {marker}",
        }],
    )
    existing = run_implementer._preflight_existing_result(ref)
    assert existing.action == "open"
    assert existing.pr_url.endswith("/pull/71")


def test_implement_one_never_calls_model_when_pr_exists(
    monkeypatch, tmp_path: Path
) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(
        run_implementer,
        "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(
            action="open",
            pr_url="https://github.com/mctlhq/mctl-agents/pull/71",
            head_sha="abc123",
        ),
    )
    monkeypatch.setattr(
        run_implementer,
        "_run_implementer_agent",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("model must not run")
        ),
    )
    result = run_implementer.implement_one(ref)
    assert result.pr_url.endswith("/pull/71")
    status = read_status(ref)
    assert status["status"] == "implemented"
    assert status["pr"].endswith("/pull/71")
    assert "notes" not in status


def test_github_failure_is_fail_closed(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)

    def fail(_ref):
        raise run_implementer.GitHubPreflightError("API unavailable")

    monkeypatch.setattr(run_implementer, "_preflight_existing_result", fail)
    result = run_implementer.implement_one(ref)
    assert "failed closed" in (result.error or "")
    assert read_status(ref)["status"] == "accepted"


def test_sdk_auth_failure_aborts_without_quarantining(
    monkeypatch, tmp_path: Path
) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(
        run_implementer,
        "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(
        run_implementer,
        "ensure_auth_for_sdk",
        lambda: (_ for _ in ()).throw(SystemExit("expired token")),
    )
    with pytest.raises(SystemExit, match="SDK authentication failed"):
        run_implementer.implement_one(ref)
    assert read_status(ref)["status"] == "accepted"


def test_remote_branch_with_no_commits_needs_triage(
    monkeypatch, tmp_path: Path
) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(run_implementer, "_github_json", lambda _cmd: [])
    responses = iter([
        subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=json.dumps({"sha": "abc"}), stderr=""
        ),
    ])
    monkeypatch.setattr(run_implementer, "_run", lambda *_a, **_kw: next(responses))
    monkeypatch.setattr(
        run_implementer,
        "_github_json",
        lambda cmd: [] if "pr" in cmd else {"ahead_by": 0},
    )
    existing = run_implementer._preflight_existing_result(ref)
    assert existing.action == "needs-triage"
    assert existing.reason == "branch-has-no-commits"


def test_mark_failure_quarantines_and_preserves_pr(tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    run_implementer._mark_needs_triage(
        ref,
        code="no-commits",
        stage="agent",
        message="implementer produced no commits",
    )
    status = read_status(ref)
    assert status["status"] == "needs-triage"
    assert status["failure"]["code"] == "no-commits"
    assert status["pr"].endswith("/pull/1")
