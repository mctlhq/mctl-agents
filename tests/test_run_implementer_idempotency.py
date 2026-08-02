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


def test_main_reports_error_even_when_result_has_colliding_pr_url(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    ref = make_ref(tmp_path)
    result = run_implementer.ImplementResult(
        ref=ref,
        pr_url="https://github.com/mctlhq/mctl-agents/pull/71",
        error="branch-collision",
    )
    monkeypatch.setattr(
        run_implementer,
        "find_accepted_proposals",
        lambda *_args, **_kwargs: [ref],
    )
    monkeypatch.setattr(
        run_implementer,
        "_implement_refs",
        lambda *_args, **_kwargs: [result],
    )
    monkeypatch.setattr(
        "sys.argv",
        ["run_implementer.py", "--state-dir", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        run_implementer.main()

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "fail mctl-agents/incident-example: branch-collision" in output
    assert "-> https://github.com/mctlhq/mctl-agents/pull/71" not in output


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


def test_missing_remote_branch_422_is_not_a_preflight_failure(
    monkeypatch, tmp_path: Path
) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(run_implementer, "_github_json", lambda _cmd: [])
    monkeypatch.setattr(
        run_implementer,
        "_run",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            args=["gh", "api"],
            returncode=1,
            stdout="",
            stderr=(
                "gh: No commit found for SHA: feat/agents-incident-example "
                "(HTTP 422)"
            ),
        ),
    )

    existing = run_implementer._preflight_existing_result(ref)

    assert existing.action == "none"


def test_unrelated_remote_branch_422_fails_closed(
    monkeypatch, tmp_path: Path
) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(run_implementer, "_github_json", lambda _cmd: [])
    monkeypatch.setattr(
        run_implementer,
        "_run",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            args=["gh", "api"],
            returncode=1,
            stdout="",
            stderr="gh: Validation Failed (HTTP 422)",
        ),
    )

    with pytest.raises(
        run_implementer.GitHubPreflightError,
        match="Validation Failed",
    ):
        run_implementer._preflight_existing_result(ref)


def test_failed_orphan_pr_creation_is_preflight_error(
    monkeypatch, tmp_path: Path
) -> None:
    ref = make_ref(tmp_path)
    github_results = iter([[], {"ahead_by": 1}, []])
    monkeypatch.setattr(
        run_implementer,
        "_github_json",
        lambda _cmd: next(github_results),
    )
    monkeypatch.setattr(
        run_implementer,
        "_run",
        lambda *_a, **_kw: subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=json.dumps({"sha": "abc"}), stderr=""
        ),
    )
    monkeypatch.setattr(
        run_implementer,
        "_open_pr_for_branch",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            subprocess.CalledProcessError(
                1,
                ["gh", "pr", "create"],
                stderr="permission denied",
            )
        ),
    )
    with pytest.raises(
        run_implementer.GitHubPreflightError,
        match="failed to open PR",
    ):
        run_implementer._preflight_existing_result(ref)


def test_failed_preflight_does_not_starve_later_proposal(
    monkeypatch, tmp_path: Path
) -> None:
    first = make_ref(tmp_path)
    second_dir = tmp_path / "mctl-agents" / "proposals" / "second"
    second_dir.mkdir(parents=True)
    (second_dir / ".status.yaml").write_text("status: accepted\n", encoding="utf-8")
    second = run_implementer.ProposalRef(
        service="mctl-agents",
        slug="second",
        proposal_dir=second_dir,
        status="accepted",
    )
    calls: list[str] = []

    def fake_implement(ref, dry_run=False):
        calls.append(ref.slug)
        if ref is first:
            return run_implementer.ImplementResult(
                ref=ref,
                pr_url=None,
                error="GitHub preflight failed closed",
                counts_toward_limit=False,
            )
        return run_implementer.ImplementResult(
            ref=ref,
            pr_url="https://github.com/mctlhq/mctl-agents/pull/99",
        )

    monkeypatch.setattr(run_implementer, "implement_one", fake_implement)
    results = run_implementer._implement_refs(
        [first, second],
        max_proposals=1,
        dry_run=False,
    )
    assert calls == ["incident-example", "second"]
    assert len(results) == 2


def test_terminal_closed_projection_has_no_blocking_reason() -> None:
    projection = run_implementer._github_projection(
        run_implementer.ExistingResult(
            action="closed",
            reason="closed-unmerged",
        )
    )
    assert projection["state"] == "closed"
    assert "blocking_reason" not in projection


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
