"""The implementer names its PR to the run evidence as soon as it knows it.

mctlhq/mctl-agents#744: a run that opened (or adopted, or pushed to) a PR and
then errored sealed `subject: not_applicable`, which reads as "this run had no
PR". Those two states are different: not observing a subject is a gap, never
an absence. #750 added `_note_subject_pr` at four call sites; the tests added
with it replaced `_implement_one` with a fake that called the helper itself,
so none of them reached a real call site. These do: each drives the real
`implement_one` / `review_feedback_one` and stubs only the steps around the
failure, so reverting a call site turns exactly its test red.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from orchestrator import evidence_producer as ep
from orchestrator import run_implementer as ri
from orchestrator.source_issue import SourceIssueVerdict

SHA = "a" * 40
OLD_HEAD = "b" * 40
PR_URL = "https://github.com/mctlhq/mctl-telegram/pull/7"
PR_SUBJECT = {"kind": "pull_request", "repository": "mctlhq/mctl-telegram", "ref": "7"}
OPEN = SourceIssueVerdict(known=True, failure=None, issue_ref="mctlhq/mctl-telegram#510")
NOT_APPLICABLE = {"block": "subject", "code": "not_applicable", "required": False}
OBSERVATION_FAILED = {"block": "subject", "code": "observation_failed", "required": True}


class _Post:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, _url: str, body: dict[str, Any], _headers: dict[str, str]) -> httpx.Response:
        self.bodies.append(body)
        return httpx.Response(201, json={"evidence": {}})

    def envelope(self) -> dict[str, Any]:
        assert self.bodies, "the run posted no evidence"
        return json.loads(base64.b64decode(self.bodies[-1]["envelope_b64"]))


@pytest.fixture
def posted(monkeypatch):
    ep._reset_for_tests()
    for name in (ep.TOKEN_ENV, ep.AMENDMENT_2_ENV, ep.BASE_URL_ENV, "MCTL_EXECUTION_CONTEXT_FILE"):
        monkeypatch.delenv(name, raising=False)
    post = _Post()
    monkeypatch.setenv(ep.TOKEN_ENV, "evidence-writer-test-token")
    monkeypatch.setenv(ep.AMENDMENT_2_ENV, "true")
    monkeypatch.setattr(ep, "_default_post", post)
    monkeypatch.setattr(ep, "RETRY_DELAYS_SECONDS", (0.0, 0.0))
    yield post
    ep._reset_for_tests()


def _assert_bound(envelope: dict[str, Any], revision: str) -> None:
    assert envelope.get("subject") == {**PR_SUBJECT, "revision": revision}, envelope.get("subject")
    assert NOT_APPLICABLE not in envelope["gaps"]
    assert OBSERVATION_FAILED not in envelope["gaps"]


# --- a proposal that has not opened a PR yet --------------------------------


def _new_ref(tmp_path: Path) -> ri.ProposalRef:
    d = tmp_path / "mctl-telegram" / "proposals" / "issue-510-x"
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(
        yaml.safe_dump({
            "status": "accepted",
            "source": {
                "type": "github_issue",
                "repo": "mctlhq/mctl-telegram",
                "issue": 510,
                "url": "https://github.com/mctlhq/mctl-telegram/issues/510",
            },
        }),
        encoding="utf-8",
    )
    return ri.ProposalRef(
        service="mctl-telegram", slug="issue-510-x", proposal_dir=d, status="accepted", approval_ok=True
    )


def _reach_the_push(monkeypatch, tmp_path: Path, *, head=SHA, commits: bool = True) -> None:
    """Stub everything between the admission gate and the push of a new PR."""
    monkeypatch.setattr(ri, "_preflight_existing_result", lambda _ref: ri.ExistingResult(action="none"))
    monkeypatch.setattr(ri, "read_source_issue", lambda *_a, **_kw: OPEN)
    monkeypatch.setattr(ri, "ensure_auth_for_sdk", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_acquire_claim", lambda *_a, **_kw: None)
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr(ri, "_clone_target", lambda *_a, **_kw: clone)
    monkeypatch.setattr(ri, "_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(ri.anyio, "run", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_has_new_commits", lambda *_a, **_kw: commits)
    monkeypatch.setattr(ri, "_detect_chart_major_bumps", lambda *_a, **_kw: [])
    monkeypatch.setattr(ri, "_push_and_open_pr", lambda *_a, **_kw: PR_URL)

    def capture(*_a, **_kw):
        if isinstance(head, BaseException):
            raise head
        return head

    monkeypatch.setattr(ri, "_capture_head_sha", capture)


def _implemented_write_fails(monkeypatch) -> None:
    """The write that follows a successful push fails (a gitops conflict)."""
    real = ri.update_status_yaml

    def flaky(ref, new_status, *args, **kwargs):
        if new_status == "implemented":
            raise RuntimeError("gitops write conflict")
        return real(ref, new_status, *args, **kwargs)

    monkeypatch.setattr(ri, "update_status_yaml", flaky)


def test_t1_pr_opened_then_the_status_write_fails_binds_the_pushed_head(monkeypatch, tmp_path, posted):
    ref = _new_ref(tmp_path)
    _reach_the_push(monkeypatch, tmp_path)
    _implemented_write_fails(monkeypatch)

    result = ri.implement_one(ref)

    assert result.pr_url is None and result.error, "the arm under test returns no pr_url"
    envelope = posted.envelope()
    assert envelope["outcome"]["code"] == "failed"
    _assert_bound(envelope, SHA)


def test_t2_pr_opened_then_the_head_cannot_be_read_is_a_required_gap(monkeypatch, tmp_path, posted):
    ref = _new_ref(tmp_path)
    _reach_the_push(monkeypatch, tmp_path, head=RuntimeError("git rev-parse timed out"))
    _implemented_write_fails(monkeypatch)

    ri.implement_one(ref)

    envelope = posted.envelope()
    assert envelope.get("subject") is None
    assert OBSERVATION_FAILED in envelope["gaps"]
    assert NOT_APPLICABLE not in envelope["gaps"]


def test_t5_a_run_that_opens_no_pr_still_seals_subject_not_applicable(monkeypatch, tmp_path, posted):
    ref = _new_ref(tmp_path)
    _reach_the_push(monkeypatch, tmp_path, commits=False)

    ri.implement_one(ref)

    envelope = posted.envelope()
    assert envelope.get("subject") is None
    assert NOT_APPLICABLE in envelope["gaps"]
    assert OBSERVATION_FAILED not in envelope["gaps"]


def test_t6_the_success_path_binds_the_pushed_head(monkeypatch, tmp_path, posted):
    ref = _new_ref(tmp_path)
    _reach_the_push(monkeypatch, tmp_path)

    result = ri.implement_one(ref)

    assert result.pr_url == PR_URL
    envelope = posted.envelope()
    assert envelope["outcome"] == {"code": "succeeded", "reason_code": "pr-opened"}
    _assert_bound(envelope, SHA)


# --- a proposal whose PR already exists -------------------------------------


def _existing_ref(tmp_path: Path) -> ri.ProposalRef:
    d = tmp_path / "mctl-telegram" / "proposals" / "issue-510-x"
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(
        yaml.safe_dump({"status": "accepted", "pr": PR_URL, "source": {"type": "github_issue", "issue": 510}}),
        encoding="utf-8",
    )
    return ri.ProposalRef(
        service="mctl-telegram", slug="issue-510-x", proposal_dir=d, status="accepted", approval_ok=True
    )


@pytest.mark.parametrize(
    ("action", "failing_write"),
    [
        ("open", "update_status_yaml"),
        ("merged", "update_status_yaml"),
        ("closed", "update_status_yaml"),
        ("needs-triage", "_mark_needs_triage"),
    ],
)
def test_t4_a_preflight_arm_whose_write_fails_keeps_the_observed_head(
    monkeypatch, tmp_path, posted, action, failing_write
):
    ref = _existing_ref(tmp_path)
    monkeypatch.setattr(
        ri,
        "_preflight_existing_result",
        lambda _ref: ri.ExistingResult(action=action, pr_url=PR_URL, head_sha=SHA, reason="existing-result-invalid"),
    )

    def broken(*_a, **_kw):
        raise RuntimeError("gitops write conflict")

    monkeypatch.setattr(ri, failing_write, broken)

    with pytest.raises(RuntimeError, match="gitops write conflict"):
        ri.implement_one(ref)

    _assert_bound(posted.envelope(), SHA)


# --- a review follow-up pushed to an existing PR ----------------------------


def _review_ref(tmp_path: Path) -> ri.ProposalRef:
    d = tmp_path / "mctl-telegram" / "proposals" / "issue-510-x"
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(
        yaml.safe_dump({"status": "implemented", "pr": PR_URL, "review_attempts": 0}), encoding="utf-8"
    )
    return ri.ProposalRef(
        service="mctl-telegram", slug="issue-510-x", proposal_dir=d, status="implemented", approval_ok=True
    )


def _reach_the_followup_push(monkeypatch, tmp_path: Path) -> None:
    heads = iter([OLD_HEAD] + [SHA] * 8)
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr(ri, "_clone_target", lambda *_a, **_kw: clone)
    monkeypatch.setattr(ri, "_branch_exists_on_origin", lambda *_a, **_kw: True)
    monkeypatch.setattr(ri, "_checkout_existing_branch", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_capture_head_sha", lambda *_a, **_kw: next(heads))
    monkeypatch.setattr(ri, "_acquire_claim", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_resolve_attempt_id", lambda *_a, **_kw: "attempt-1")
    monkeypatch.setattr(
        ri, "_resolve_implementer_service_skills",
        lambda *_a, **_kw: SimpleNamespace(to_prompt_block=lambda **_k: ""),
    )
    monkeypatch.setattr(ri, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(ri.anyio, "run", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(ri, "_push_followup", lambda *_a, **_kw: None)


def test_t3_a_followup_pushed_then_a_later_step_fails_binds_the_pushed_head(monkeypatch, tmp_path, posted):
    ref = _review_ref(tmp_path)
    _reach_the_followup_push(monkeypatch, tmp_path)

    def broken(*_a, **_kw):
        raise RuntimeError("post-push bookkeeping failed")

    monkeypatch.setattr(ri, "_note_pushed", broken)

    result = ri.review_feedback_one(ref, {})

    assert result.pr_url is None and result.error, "the arm under test returns no pr_url"
    envelope = posted.envelope()
    assert envelope["outcome"]["code"] == "failed"
    _assert_bound(envelope, SHA)
