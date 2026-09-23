"""The orchestrator's own GitHub mutations go through the policy checkpoint
(mctlhq/mctl-agents#197, ADR 014 §5).

One pair per mutation site:

- allowed: the built-in policy permits it, the decision is recorded under the
  site's own rule, and the side effect runs;
- refused: a policy that says no is honoured before the transport. The side
  effect never runs, and the site fails closed the way its other failures do.

The refusing policy is injected by wrapping `policy_checkpoint.checkpoint`,
so each site is exercised through the real decision, record and refusal
path, not through a stub of it.
"""
from __future__ import annotations

import functools
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from orchestrator import (
    policy_checkpoint as pc,
)
from orchestrator import (
    run_implementer,
    run_issue_investigator,
    run_issue_poller,
    run_shepherd,
)
from tests.test_run_implementer_budget_handback import _make_ref, _reach_the_sdk
from tests.test_run_implementer_refusal import _ref as _feedback_ref
from tests.test_run_implementer_refusal import _stub_review_feedback, repo  # noqa: F401 — `repo` is a fixture
from tests.test_run_issue_investigator import _investigate_harness
from tests.test_run_shepherd import make_pr

GITHUB_KINDS = (
    pc.GITHUB_GIT_PUSH, pc.GITHUB_PR_CREATE, pc.GITHUB_PR_MERGE, pc.GITHUB_PR_COMMENT,
    pc.GITHUB_RUN_RERUN, pc.GITHUB_ISSUE_LABEL, pc.GITHUB_ISSUE_COMMENT,
)
DENY_GITHUB = pc.Policy("test/deny-github", tuple(
    pc.Rule(f"deny-{kind}", kind, "*", pc.DENY) for kind in GITHUB_KINDS
))
#: A policy the evaluator cannot read (unknown verdict): every decision is
#: `evaluator_error`, i.e. undecided — the checkpoint could not answer.
UNDECIDED_GITHUB = pc.Policy("test/broken-github", tuple(
    pc.Rule(f"broken-{kind}", kind, "*", "MAYBE") for kind in GITHUB_KINDS
))
GATE_GITHUB = pc.Policy("test/gate-github", tuple(
    pc.Rule(f"gate-{kind}", kind, "*", pc.REQUIRE_APPROVAL) for kind in GITHUB_KINDS
))


@pytest.fixture(autouse=True)
def _no_execution_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MCTL_EXECUTION_CONTEXT_FILE", "MCTL_REQUIRE_EXECUTION_CONTEXT", pc.APPROVALS_ENV):
        monkeypatch.delenv(name, raising=False)


def _refuse_with(monkeypatch: pytest.MonkeyPatch, policy: pc.Policy = DENY_GITHUB) -> None:
    """Every checkpoint in this process decides under `policy`, with no
    approval store (REQUIRE_APPROVAL then always blocks)."""
    real = pc.checkpoint
    monkeypatch.setattr(pc, "checkpoint", functools.partial(real, policy=policy, approvals=pc.NO_APPROVALS))


def _decisions(out: str) -> list[dict[str, Any]]:
    return [json.loads(line.split(" ", 1)[1]) for line in out.splitlines()
            if line.startswith(pc.DECISION_PREFIX + " ")]


def _only_decision(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    decisions = _decisions(capsys.readouterr().out)
    assert len(decisions) == 1, decisions
    return decisions[0]


def _proc(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["x"], returncode=0, stdout=stdout, stderr="")


REF = run_implementer.ProposalRef(
    service="mctl-web", slug="slug", proposal_dir=Path("/tmp/proposal"), status="accepted",
)
LEASE = "a" * 40


# ---------------------------------------------------------------------------
# The built-in policy states today's behaviour.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("kind", "operation", "rule_id"), [
    (pc.GITHUB_GIT_PUSH, "push:new-branch", "github-push-new-branch"),
    (pc.GITHUB_GIT_PUSH, "push:force-with-lease", "github-push-with-lease"),
    (pc.GITHUB_PR_CREATE, "create", "github-pr-create"),
    (pc.GITHUB_PR_MERGE, "merge", "github-pr-merge"),
    (pc.GITHUB_PR_COMMENT, "comment:review-trigger", "github-pr-review-trigger"),
    (pc.GITHUB_RUN_RERUN, "rerun:failed", "github-run-rerun-failed"),
    (pc.GITHUB_ISSUE_LABEL, "remove", "github-issue-label-remove"),
    (pc.GITHUB_ISSUE_COMMENT, "comment", "github-issue-comment"),
])
def test_builtin_policy_allows_each_github_mutation(kind: str, operation: str, rule_id: str) -> None:
    decision = pc.checkpoint(kind, operation, "mctlhq/x", {"a": 1})
    assert decision.permitted
    assert decision.rule_id == rule_id


@pytest.mark.parametrize(("kind", "operation"), [
    (pc.GITHUB_GIT_PUSH, "push:force"),
    (pc.GITHUB_GIT_PUSH, "push:delete"),
    (pc.GITHUB_PR_MERGE, "merge:admin"),
    (pc.GITHUB_PR_COMMENT, "comment"),
    (pc.GITHUB_RUN_RERUN, "rerun:all"),
    (pc.GITHUB_ISSUE_LABEL, "add"),
])
def test_builtin_policy_denies_what_no_site_does(kind: str, operation: str) -> None:
    """The rules name the exact operations the sites perform; a variant
    none of them performs falls through to `no_matching_rule`."""
    decision = pc.checkpoint(kind, operation, "mctlhq/x", {"a": 1})
    assert not decision.permitted
    assert decision.code == pc.CODE_NO_RULE


# ---------------------------------------------------------------------------
# run_implementer: git push (follow-up, adopt-existing, new branch)
# ---------------------------------------------------------------------------
def test_followup_push_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))

    run_implementer._push_followup(Path("/tmp/repo"), "feat/agents-slug", LEASE, repo="mctlhq/mctl-web")

    assert calls == [["git", "push", f"--force-with-lease=feat/agents-slug:{LEASE}", "origin", "feat/agents-slug"]]
    record = _only_decision(capsys)
    assert (record["rule_id"], record["code"]) == ("github-push-with-lease", pc.CODE_ALLOWED)
    assert record["action_kind"] == pc.GITHUB_GIT_PUSH
    assert record["target"] == "mctlhq/mctl-web:feat/agents-slug"
    assert record["args_digest"] == pc.args_digest_of(
        {"remote": "origin", "branch": "feat/agents-slug", "lease": LEASE})


@pytest.mark.parametrize("policy", [DENY_GITHUB, GATE_GITHUB], ids=["deny", "require-approval"])
def test_followup_push_refused_does_not_run(monkeypatch, policy) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    _refuse_with(monkeypatch, policy)

    with pytest.raises(pc.PolicyRefused):
        run_implementer._push_followup(Path("/tmp/repo"), "feat/agents-slug", LEASE, repo="mctlhq/mctl-web")

    assert calls == []


def test_existing_branch_push_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: LEASE)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    monkeypatch.setattr(run_implementer, "_open_pr_for_branch", lambda *_a, **_kw: "https://pr")

    assert run_implementer._push_and_open_pr(Path("/tmp/repo"), REF) == "https://pr"

    assert calls == [["git", "push", f"--force-with-lease=feat/agents-slug:{LEASE}", "origin", "feat/agents-slug"]]
    assert _only_decision(capsys)["rule_id"] == "github-push-with-lease"


def test_existing_branch_push_refused_does_not_run(monkeypatch) -> None:
    calls: list[list[str]] = []
    opened: list[str] = []
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: LEASE)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    monkeypatch.setattr(run_implementer, "_open_pr_for_branch", lambda *_a, **_kw: opened.append("pr"))
    _refuse_with(monkeypatch)

    with pytest.raises(pc.PolicyRefused):
        run_implementer._push_and_open_pr(Path("/tmp/repo"), REF)

    assert calls == []
    assert opened == []


def test_new_branch_push_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    monkeypatch.setattr(run_implementer, "_open_pr_for_branch", lambda *_a, **_kw: "https://pr")

    run_implementer._push_and_open_pr(Path("/tmp/repo"), REF)

    assert calls == [["git", "push", "-u", "origin", "feat/agents-slug"]]
    assert _only_decision(capsys)["rule_id"] == "github-push-new-branch"


def test_new_branch_push_refused_does_not_run(monkeypatch) -> None:
    calls: list[list[str]] = []
    opened: list[str] = []
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    monkeypatch.setattr(run_implementer, "_open_pr_for_branch", lambda *_a, **_kw: opened.append("pr"))
    _refuse_with(monkeypatch)

    with pytest.raises(pc.PolicyRefused):
        run_implementer._push_and_open_pr(Path("/tmp/repo"), REF)

    assert calls == []
    assert opened == []


def test_push_policy_is_decided_after_the_claim_check(monkeypatch) -> None:
    """A claim refusal must not spend an approval on a push that was never
    going to happen: the claim is checked first, the policy second."""
    order: list[str] = []
    monkeypatch.setattr(run_implementer, "_check_claim_or_raise", lambda *_a, **_kw: order.append("claim"))
    monkeypatch.setattr(run_implementer, "_require_push_policy", lambda *_a, **_kw: order.append("policy"))
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: order.append("git"))

    run_implementer._push_followup(Path("/tmp/repo"), "b", LEASE, claim_context=SimpleNamespace(), repo="r")

    assert order == ["claim", "policy", "git"]


def test_implement_one_triages_a_refused_push_as_policy(monkeypatch, tmp_path: Path) -> None:
    """The new-branch driver: the refusal is recorded under its own triage
    code, and git never pushed."""
    ref = _make_ref(tmp_path)
    _reach_the_sdk(monkeypatch, tmp_path, on_run=lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_detect_chart_major_bumps", lambda *_a, **_kw: [])
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: None)
    _refuse_with(monkeypatch)

    result = run_implementer.implement_one(ref, dry_run=False)

    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "needs-triage"
    assert status["failure"]["code"] == "policy-refused"
    assert result.pr_url is None
    assert result.error is not None and result.error.startswith(run_implementer.POLICY_REFUSED_ERROR_PREFIX)
    assert not [c for c in calls if c[:2] == ["git", "push"]]


def test_review_feedback_one_reports_a_refused_push_as_policy(repo, monkeypatch) -> None:  # noqa: F811
    """The review-feedback driver: the agent committed, the push is refused,
    git never pushed, and the error carries the policy prefix."""
    _stub_review_feedback(monkeypatch, repo)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    _refuse_with(monkeypatch)

    result = run_implementer.review_feedback_one(_feedback_ref(repo), {"summaries": []})

    assert result.error is not None and result.error.startswith(run_implementer.POLICY_REFUSED_ERROR_PREFIX)
    assert run_implementer._review_feedback_exit_code(result.error) == run_implementer.EXIT_POLICY_REFUSED
    assert not [c for c in calls if c[:2] == ["git", "push"]]


def test_review_feedback_one_reports_an_undecided_push_as_harness(repo, monkeypatch) -> None:  # noqa: F811
    """An undecided checkpoint is a platform failure, not an answer about the
    findings: EXIT_POLICY_UNDECIDED, a harness code (uncharged but bounded),
    never 53 and never the counter-less transient exit 1."""
    _stub_review_feedback(monkeypatch, repo)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    _refuse_with(monkeypatch, UNDECIDED_GITHUB)

    result = run_implementer.review_feedback_one(_feedback_ref(repo), {"summaries": []})

    assert result.error is not None
    assert result.error.startswith(run_implementer.POLICY_UNDECIDED_ERROR_PREFIX)
    code = run_implementer._review_feedback_exit_code(result.error)
    assert code == run_implementer.EXIT_POLICY_UNDECIDED == 54
    deterministic, harness = run_shepherd._followup_code_sets()
    assert code in harness
    assert code not in deterministic
    # Exclusively harness: in either set below it would move under
    # MAX_REFUSALS or the fence arm, with every other assertion still green.
    assert code not in run_shepherd._refusal_codes()
    assert code not in run_shepherd._fenced_codes()
    assert not [c for c in calls if c[:2] == ["git", "push"]]


def _followup_exits(code: int, monkeypatch) -> None:
    """The shepherd's real `apply_followup`, with the implementer subprocess
    answering `code` (the fake it runs in `test_run_shepherd`)."""
    real = run_shepherd.apply_followup

    async def fake_format(_findings):
        return {"p1": True, "p2": False, "summaries": ["fix"]}

    monkeypatch.setattr(run_shepherd, "_format_bundle_via_sdk", fake_format)
    monkeypatch.setattr(run_shepherd.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=code))
    monkeypatch.setattr(run_shepherd, "apply_followup",
                        lambda *a, **kw: real(*a, **{**kw, "skip_subprocess": False}))


def test_shepherd_does_not_charge_an_undecided_push(tmp_path, monkeypatch) -> None:
    from tests.test_run_shepherd import make_finding, make_ref, read_status

    ref = make_ref(tmp_path, review_attempts=1)
    _followup_exits(run_implementer.EXIT_POLICY_UNDECIDED, monkeypatch)
    monkeypatch.setattr(run_shepherd, "find_pr_for_proposal", lambda *_a, **_kw: make_pr())
    monkeypatch.setattr(run_shepherd, "read_codex_review",
                        lambda *_a, **_kw: run_shepherd.CodexReview(has_responded=True, findings=[make_finding()]))
    monkeypatch.setattr(run_shepherd, "read_copilot_review", lambda *_a, **_kw: run_shepherd.CopilotReview(False, 0))

    result = run_shepherd.process_one(ref, skip_subprocess=True)

    final = read_status(ref)
    assert result.decision == "wait"
    assert final["review_attempts"] == 1, "an undecided checkpoint is never charged to the proposal"
    assert final["harness_failures"] == 1


def test_shepherd_bounds_an_undecided_push_at_max_harness_failures(tmp_path, monkeypatch) -> None:
    from tests.test_run_shepherd import make_finding, make_ref, read_status

    ref = make_ref(tmp_path, review_attempts=1)
    ref.harness_failures = run_shepherd.MAX_HARNESS_FAILURES - 1
    _followup_exits(run_implementer.EXIT_POLICY_UNDECIDED, monkeypatch)
    monkeypatch.setattr(run_shepherd, "find_pr_for_proposal", lambda *_a, **_kw: make_pr())
    monkeypatch.setattr(run_shepherd, "read_codex_review",
                        lambda *_a, **_kw: run_shepherd.CodexReview(has_responded=True, findings=[make_finding()]))
    monkeypatch.setattr(run_shepherd, "read_copilot_review", lambda *_a, **_kw: run_shepherd.CopilotReview(False, 0))

    result = run_shepherd.process_one(ref, skip_subprocess=True)

    final = read_status(ref)
    assert result.decision == "review-stuck"
    assert final["status"] == "review-stuck"
    assert final["harness_failures"] == run_shepherd.MAX_HARNESS_FAILURES
    assert final["review_attempts"] == 1


def _undecided_push(monkeypatch, tmp_path: Path, *, prior_handbacks: int = 0):
    """`implement_one` up to a push whose checkpoint cannot decide. Returns
    (ref, recorded git calls)."""
    ref = _make_ref(tmp_path)
    if prior_handbacks:
        status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
        status["policy_handbacks"] = prior_handbacks
        ref.status_path.write_text(yaml.safe_dump(status), encoding="utf-8")
    _reach_the_sdk(monkeypatch, tmp_path, on_run=lambda *_a, **_kw: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_detect_chart_major_bumps", lambda *_a, **_kw: [])
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: None)
    _refuse_with(monkeypatch, UNDECIDED_GITHUB)
    return ref, calls


def test_implement_one_hands_back_an_undecided_push(monkeypatch, tmp_path: Path) -> None:
    """The new-branch driver: an undecided checkpoint is never recorded as
    the proposal's failure. It is handed back to `accepted` for a later tick,
    with no triage record, the tally goes up by one, and git never pushed."""
    ref, calls = _undecided_push(monkeypatch, tmp_path)

    result = run_implementer.implement_one(ref, dry_run=False)

    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "accepted"
    assert not status.get("failure")
    assert not status.get("attempt")
    assert status["policy_handbacks"] == 1
    assert "budget_handbacks" not in status, "its own counter, not the budget one"
    assert result.pr_url is None
    # A skip, not an error: the tick stays green, so the gitops commit is not
    # skipped and the implement-fallback account is not spent on it.
    assert result.error is None
    assert result.skipped_reason is not None
    assert result.skipped_reason.startswith(run_implementer.POLICY_UNDECIDED_ERROR_PREFIX)
    outcome = run_implementer._batch_outcome([result])
    assert (outcome.failed, outcome.skipped) == (0, 1)
    assert not [c for c in calls if c[:2] == ["git", "push"]]


def test_undecided_hand_back_tally_increments_below_the_cap(monkeypatch, tmp_path: Path) -> None:
    ref, _calls = _undecided_push(
        monkeypatch, tmp_path, prior_handbacks=run_implementer.IMPLEMENT_MAX_POLICY_HANDBACKS - 2)

    result = run_implementer.implement_one(ref, dry_run=False)

    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "accepted"
    assert status["policy_handbacks"] == run_implementer.IMPLEMENT_MAX_POLICY_HANDBACKS - 1
    assert run_implementer._batch_outcome([result]).failed == 0


def test_undecided_hand_back_goes_terminal_at_the_cap(monkeypatch, tmp_path: Path) -> None:
    """A persistent undecided checkpoint stops spending: the Nth consecutive
    one is `needs-triage` under its own code, the tally is reset for the
    human's retry, and the tick stays green so that ending write lands."""
    ref, calls = _undecided_push(
        monkeypatch, tmp_path, prior_handbacks=run_implementer.IMPLEMENT_MAX_POLICY_HANDBACKS - 1)

    result = run_implementer.implement_one(ref, dry_run=False)

    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "needs-triage"
    assert status["failure"]["code"] == "policy-undecided"
    assert status["failure"]["stage"] == "policy"
    assert "policy_handbacks" not in status
    assert result.error is None
    assert result.skipped_reason is not None
    assert result.skipped_reason.startswith(run_implementer.POLICY_UNDECIDED_ERROR_PREFIX)
    outcome = run_implementer._batch_outcome([result])
    assert (outcome.failed, outcome.skipped) == (0, 1)
    assert not [c for c in calls if c[:2] == ["git", "push"]]


def test_a_successful_run_clears_the_undecided_tally(monkeypatch, tmp_path: Path) -> None:
    """The cap bounds CONSECUTIVE undecided attempts, not the proposal's
    lifetime."""
    ref, _calls = _undecided_push(monkeypatch, tmp_path, prior_handbacks=1)
    monkeypatch.setattr(pc, "checkpoint", functools.partial(pc.checkpoint.func))  # the built-in policy again
    monkeypatch.setattr(run_implementer, "_open_pr_for_branch", lambda *_a, **_kw: "https://pr")

    run_implementer.implement_one(ref, dry_run=False)

    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "implemented"
    assert "policy_handbacks" not in status


def test_a_refused_followup_push_is_charged_not_transient() -> None:
    """The review-feedback driver: EXIT_POLICY_REFUSED, which the shepherd
    charges, so a policy that says no cannot loop a paid model turn."""
    code = run_implementer._review_feedback_exit_code(f"{run_implementer.POLICY_REFUSED_ERROR_PREFIX} no")
    assert code == run_implementer.EXIT_POLICY_REFUSED == 53
    deterministic, harness = run_shepherd._followup_code_sets()
    assert code in deterministic
    assert code not in harness


# ---------------------------------------------------------------------------
# run_implementer: gh pr create
# ---------------------------------------------------------------------------
def test_pr_create_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _proc("https://github.com/mctlhq/mctl-web/pull/9\n")

    monkeypatch.setattr(run_implementer, "_run", fake_run)

    assert run_implementer._open_pr_for_branch(REF, "feat/agents-slug") == "https://github.com/mctlhq/mctl-web/pull/9"

    assert len(calls) == 1 and calls[0][:3] == ["gh", "pr", "create"]
    record = _only_decision(capsys)
    assert (record["rule_id"], record["target"]) == ("github-pr-create", "mctlhq/mctl-web")
    title, body = run_implementer._pr_title_and_body(REF)
    assert body not in json.dumps(record), "the PR body is recorded only as a digest"
    assert record["args_digest"] == pc.args_digest_of(
        {"title": title, "body": body, "head": "feat/agents-slug", "base": "main"})


def test_pr_create_refused_does_not_run(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **_kw: calls.append(cmd))
    _refuse_with(monkeypatch)

    with pytest.raises(pc.PolicyRefused):
        run_implementer._open_pr_for_branch(REF, "feat/agents-slug")

    assert calls == []


def test_preflight_pr_create_refused_fails_the_preflight_closed(monkeypatch, tmp_path: Path) -> None:
    """The preflight opens a PR for an orphaned result branch. A refusal
    there is a closed preflight (no model run), not an unhandled crash."""
    ref = _make_ref(tmp_path)
    github_results = iter([[], {"ahead_by": 1}])
    monkeypatch.setattr(run_implementer, "_github_json", lambda _cmd: next(github_results))
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        return _proc(json.dumps({"sha": "abc"}))

    monkeypatch.setattr(run_implementer, "_run", fake_run)
    _refuse_with(monkeypatch)

    with pytest.raises(run_implementer.GitHubPreflightError, match="policy DENY"):
        run_implementer._preflight_existing_result(ref)

    assert not [c for c in calls if c[:3] == ["gh", "pr", "create"]]


# ---------------------------------------------------------------------------
# run_shepherd: gh pr merge
# ---------------------------------------------------------------------------
def _stub_merge_transport(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kw):
        calls.append(cmd)
        return _proc()

    monkeypatch.setattr(run_shepherd.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(run_shepherd, "refresh_github_token", lambda: None)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot",
                        lambda *_a, **_kw: SimpleNamespace(merge_commit="m" * 40))
    return calls


def test_merge_allowed_runs(monkeypatch, capsys) -> None:
    calls = _stub_merge_transport(monkeypatch)
    pr = make_pr()

    assert run_shepherd.merge_pr(pr) == (True, "m" * 40)

    assert len(calls) == 1 and calls[0][:3] == ["gh", "pr", "merge"]
    record = _only_decision(capsys)
    assert (record["rule_id"], record["code"]) == ("github-pr-merge", pc.CODE_ALLOWED)
    assert record["target"] == f"https://github.com/{pr.repo}/pull/{pr.number}"
    assert record["metadata"]["head_sha"] == pr.head_sha


@pytest.mark.parametrize("policy", [DENY_GITHUB, GATE_GITHUB], ids=["deny", "require-approval"])
def test_merge_refused_does_not_run(monkeypatch, capsys, policy) -> None:
    calls = _stub_merge_transport(monkeypatch)
    _refuse_with(monkeypatch, policy)

    assert run_shepherd.merge_pr(make_pr()) == (False, None)

    assert calls == []
    assert "warn: not merging" in capsys.readouterr().out


def test_merge_approval_binds_the_head_sha() -> None:
    """An approval for one head never merges another: the head SHA is in
    the arguments, so a new head is a new action."""
    a = pc.request_for(pc.GITHUB_PR_MERGE, "merge", "u", {"match_head_commit": "a" * 40})
    b = pc.request_for(pc.GITHUB_PR_MERGE, "merge", "u", {"match_head_commit": "b" * 40})
    assert isinstance(a, pc.ActionRequest) and isinstance(b, pc.ActionRequest)
    assert a.action_digest() != b.action_digest()


# ---------------------------------------------------------------------------
# run_shepherd: gh pr comment (`@claude review`)
# ---------------------------------------------------------------------------
def test_review_trigger_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_shepherd, "_run", lambda cmd, **_kw: calls.append(cmd) or _proc())

    run_shepherd.trigger_review(make_pr())

    assert len(calls) == 1 and calls[0][:3] == ["gh", "pr", "comment"]
    assert _only_decision(capsys)["rule_id"] == "github-pr-review-trigger"


def test_review_trigger_refused_does_not_run(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_shepherd, "_run", lambda cmd, **_kw: calls.append(cmd) or _proc())
    _refuse_with(monkeypatch)

    run_shepherd.trigger_review(make_pr())  # best-effort: must not raise

    assert calls == []
    assert "warn: not posting `@claude review`" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_shepherd: gh run rerun --failed
# ---------------------------------------------------------------------------
def test_rerun_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_shepherd, "_run", lambda cmd, **_kw: calls.append(cmd) or _proc())

    assert run_shepherd._rerun_check_run("mctlhq/mctl-web", "123") is True

    assert calls == [["gh", "run", "rerun", "123", "--failed", "--repo", "mctlhq/mctl-web"]]
    record = _only_decision(capsys)
    assert (record["rule_id"], record["target"]) == ("github-run-rerun-failed", "mctlhq/mctl-web/actions/runs/123")


def test_rerun_refused_does_not_run(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_shepherd, "_run", lambda cmd, **_kw: calls.append(cmd) or _proc())
    _refuse_with(monkeypatch)

    assert run_shepherd._rerun_check_run("mctlhq/mctl-web", "123") is False

    assert calls == []


# ---------------------------------------------------------------------------
# run_issue_investigator: gh issue comment
# ---------------------------------------------------------------------------
ISSUE = "https://github.com/mctlhq/mctl-telegram/issues/123"


def test_investigator_comment_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_issue_investigator, "_run", lambda cmd, **_kw: calls.append(cmd))

    run_issue_investigator.post_proposal_comment(ISSUE, "mctl-telegram", "issue-123-x")

    assert len(calls) == 1 and calls[0][:3] == ["gh", "issue", "comment"]
    record = _only_decision(capsys)
    assert (record["rule_id"], record["target"]) == ("github-issue-comment", ISSUE)


def test_investigator_comment_refused_does_not_run(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_issue_investigator, "_run", lambda cmd, **_kw: calls.append(cmd))
    _refuse_with(monkeypatch)

    with pytest.raises(pc.PolicyRefused):
        run_issue_investigator.post_proposal_comment(ISSUE, "mctl-telegram", "issue-123-x")

    assert calls == []


# ---------------------------------------------------------------------------
# run_issue_poller: gh issue edit --remove-label
# ---------------------------------------------------------------------------
def test_label_removal_allowed_runs(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_issue_poller, "_run", lambda cmd, **_kw: calls.append(cmd))

    run_issue_poller.remove_label(ISSUE, "agents:intake")

    assert calls == [["gh", "issue", "edit", ISSUE, "--remove-label", "agents:intake"]]
    assert _only_decision(capsys)["rule_id"] == "github-issue-label-remove"


def test_label_removal_refused_does_not_run(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_issue_poller, "_run", lambda cmd, **_kw: calls.append(cmd))
    _refuse_with(monkeypatch)

    with pytest.raises(pc.PolicyRefused):
        run_issue_poller.remove_label(ISSUE, "agents:intake")

    assert calls == []


def test_poll_counts_a_refused_label_removal_as_a_failure(monkeypatch) -> None:
    """The workflow is started, the label stays, and the refusal is
    counted rather than silently repeated every cycle."""
    import asyncio

    from orchestrator.run_issue_investigator import IssueRef

    ref = IssueRef(owner="mctlhq", repo="mctl-telegram", number=123, url=ISSUE)
    calls: list[list[str]] = []

    async def start(url: str, client=None):
        return SimpleNamespace(id="dev-loop-x", result_run_id="run-1")

    async def connect(*_a, **_kw):
        return SimpleNamespace()

    monkeypatch.setattr(run_issue_poller, "search_labeled_issues", lambda label: [ref])
    monkeypatch.setattr(run_issue_poller, "connect", connect)
    monkeypatch.setattr(run_issue_poller, "start_dev_loop_workflow", start)
    monkeypatch.setattr(run_issue_poller, "_run", lambda cmd, **_kw: calls.append(cmd))
    _refuse_with(monkeypatch)

    result = asyncio.run(run_issue_poller.poll())

    assert (result.started, result.failures) == (1, 1)
    assert calls == []


def test_investigate_survives_a_refused_proposal_comment(tmp_path, monkeypatch, capsys) -> None:
    """The proposal is already written when the comment is refused: the
    investigation still succeeds, as it does when `gh issue comment` fails."""
    issue = _investigate_harness(
        tmp_path, monkeypatch,
        agent=lambda repo_dir, prompt, proposal_dir: [
            (proposal_dir / name).write_text(name) for name in ("requirements.md", "design.md", "tasks.md")
        ],
    )
    monkeypatch.setattr(run_issue_investigator, "post_proposal_comment",
                        lambda *_a, **_kw: pc.require(pc.checkpoint(pc.GITHUB_ISSUE_COMMENT, "comment", ISSUE, {})))
    _refuse_with(monkeypatch)

    result = run_issue_investigator.investigate(issue.ref.url, state_dir=tmp_path)

    assert result.error is None
    assert "the policy checkpoint refused the issue comment" in capsys.readouterr().out
