"""A PR that changes agent definitions is never merged by automation without
a human decision (mctlhq/mctl-agents#470 acceptance row 4, owner decision
2026-10-05; docs/adr/016-shepherd-merge-approval.md amendment 1).

Four layers, each tested here against the real code below it:

- the changed-path read (`run_shepherd._read_changed_paths`): complete,
  truncated and unreadable stay three different answers;
- the classifier (`touches_agent_definitions`, `merge_operation_for`);
- the policy rule (`github-pr-merge-agent-definition`);
- every merge path: the in-pod `merge_pr`, the transport
  `merge_pr_unchecked`, the gated activity, and `process_one`'s signal.
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from temporalio.testing import ActivityEnvironment

from orchestrator import action_approvals as aa
from orchestrator import policy_checkpoint as pc
from orchestrator import run_shepherd
from orchestrator.run_shepherd import ChangedPaths, CodexReview, process_one
from orchestrator.temporal.activities import pr_merge as act
from orchestrator.temporal.activities.action_approval import GatedActionInput
from tests.test_action_approvals import FakeMctlApi
from tests.test_run_shepherd import HEAD_SHA, OLD_SHA, make_pr, make_ref, read_status

MANIFEST = "agents/_manifests/implementer/agent.yaml"
GITOPS_DEFINITION = "platform-gitops/agent-platform/releases/implementer.yaml"
ORDINARY = "orchestrator/run_shepherd.py"
DEFINITION_RULE = "github-pr-merge-agent-definition"
APPROVED_REVIEW = CodexReview(has_responded=True, findings=[], head_verdict="APPROVED")


def _definition_pr(**kwargs: Any) -> run_shepherd.PRSnapshot:
    return make_pr(changed_paths=ChangedPaths.complete((ORDINARY, MANIFEST)), **kwargs)


def _decisions(out: str) -> list[dict[str, Any]]:
    return [json.loads(line.split(" ", 1)[1]) for line in out.splitlines()
            if line.startswith(pc.DECISION_PREFIX + " ")]


def _signals(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(run_shepherd.MERGE_NEEDS_HUMAN + " ")]


@pytest.fixture(autouse=True)
def _production_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production today: no merge-approval policy, no approval store, no
    service opted into the #519 gate."""
    monkeypatch.delenv(pc.MERGE_APPROVAL_ENV, raising=False)
    monkeypatch.delenv(pc.APPROVALS_ENV, raising=False)
    monkeypatch.setattr(run_shepherd, "SHEPHERD_MERGE_APPROVAL_SERVICES", frozenset())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    MANIFEST,
    "agents/_manifests/new-agent/prompts/system.md",
    GITOPS_DEFINITION,
    "platform-gitops/agent-platform/policy.yaml",
])
def test_a_protected_path_touches_agent_definitions(path: str) -> None:
    pr = make_pr(changed_paths=ChangedPaths.complete((path,)))
    assert run_shepherd.touches_agent_definitions(pr)
    assert run_shepherd.merge_operation_for(pr) == pc.MERGE_OPERATION_AGENT_DEFINITION


@pytest.mark.parametrize("path", [
    ORDINARY,
    "agents/_shepherd/shepherd.md",
    # A sibling whose name merely starts like the protected directory.
    "agents/_manifests-old/implementer/agent.yaml",
    "agents/_manifests.md",
    # The prefix names a tree at the repository root, not a path fragment.
    "docs/agents/_manifests/implementer/agent.yaml",
    "tests/fixtures/platform-gitops/agent-platform/policy.yaml",
])
def test_an_unprotected_path_does_not_touch_agent_definitions(path: str) -> None:
    pr = make_pr(changed_paths=ChangedPaths.complete((path,)))
    assert not run_shepherd.touches_agent_definitions(pr)
    assert run_shepherd.merge_operation_for(pr) == pc.MERGE_OPERATION


def test_a_pr_that_changes_no_file_does_not_touch_agent_definitions() -> None:
    """A COMPLETE empty list is GitHub's own "nothing changed" answer."""
    assert not run_shepherd.touches_agent_definitions(make_pr(changed_paths=ChangedPaths.complete(())))


def test_a_mixed_pr_touches_agent_definitions() -> None:
    pr = make_pr(changed_paths=ChangedPaths.complete((ORDINARY, "README.md", MANIFEST)))
    assert run_shepherd.touches_agent_definitions(pr)
    assert run_shepherd.agent_definition_paths(pr.changed_paths) == (MANIFEST,)


def test_a_truncated_list_counts_as_touching_even_with_no_protected_path_seen() -> None:
    pr = make_pr(changed_paths=ChangedPaths(run_shepherd.CHANGED_PATHS_TRUNCATED, (ORDINARY,), "read 100 of 250"))
    assert run_shepherd.agent_definition_paths(pr.changed_paths) == ()
    assert run_shepherd.touches_agent_definitions(pr)
    assert run_shepherd.merge_operation_for(pr) == pc.MERGE_OPERATION_AGENT_DEFINITION


def test_an_unreadable_list_counts_as_touching() -> None:
    pr = make_pr(changed_paths=ChangedPaths(run_shepherd.CHANGED_PATHS_UNREADABLE, (), "gh failed"))
    assert run_shepherd.touches_agent_definitions(pr)
    assert run_shepherd.merge_operation_for(pr) == pc.MERGE_OPERATION_AGENT_DEFINITION


def test_a_snapshot_built_without_reading_paths_is_unreadable_not_empty() -> None:
    """The dataclass default: a never-attempted read is not "no paths"."""
    snap = run_shepherd.PRSnapshot(
        number=1, repo="mctlhq/mctl-web", state="OPEN", merged=False, closed_unmerged=False,
        merge_commit=None, close_comment_or_default="", head_sha=HEAD_SHA, head_pushed_at=None,
        merge_state_status="CLEAN", checks_green=True, is_draft=False,
    )
    assert snap.changed_paths.state == run_shepherd.CHANGED_PATHS_UNREADABLE
    assert run_shepherd.touches_agent_definitions(snap)


# ---------------------------------------------------------------------------
# The changed-path read
# ---------------------------------------------------------------------------
def _node(*paths: str, has_next: Any = False, total: Any = "len", change_type: str = "MODIFIED") -> dict[str, Any]:
    node: dict[str, Any] = {
        "files": {
            "nodes": [{"path": p, "changeType": change_type} for p in paths],
            "pageInfo": {"hasNextPage": has_next},
        },
    }
    if total != "absent":
        node["changedFiles"] = len(paths) if total == "len" else total
    return node


def _read(node: dict[str, Any]) -> ChangedPaths:
    def _no_rest(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("the REST files read is only for renames")

    with patch.object(run_shepherd, "_gh_api_json", _no_rest):
        return run_shepherd._read_changed_paths("mctlhq/mctl-agents", 7, node)


def test_read_complete() -> None:
    changed = _read(_node(ORDINARY, MANIFEST))
    assert changed.state == run_shepherd.CHANGED_PATHS_COMPLETE
    assert set(changed.paths) == {ORDINARY, MANIFEST}


def test_read_empty_pr_is_complete_and_empty() -> None:
    changed = _read(_node())
    assert (changed.state, changed.paths) == (run_shepherd.CHANGED_PATHS_COMPLETE, ())


def test_read_has_next_page_is_truncated() -> None:
    changed = _read(_node(ORDINARY, has_next=True, total=250))
    assert changed.state == run_shepherd.CHANGED_PATHS_TRUNCATED
    assert changed.paths == (ORDINARY,)


def test_read_more_changed_files_than_nodes_is_truncated() -> None:
    """`hasNextPage` says no but the PR's own count says more: not complete."""
    changed = _read(_node(ORDINARY, has_next=False, total=2))
    assert changed.state == run_shepherd.CHANGED_PATHS_TRUNCATED


@pytest.mark.parametrize(("node", "label"), [
    ({"changedFiles": 0}, "no files connection"),
    ({"files": None, "changedFiles": 0}, "null files connection"),
    ({"files": {"nodes": None, "pageInfo": {"hasNextPage": False}}, "changedFiles": 0}, "null nodes"),
    ({"files": {"nodes": [None], "pageInfo": {"hasNextPage": False}}, "changedFiles": 1}, "null node"),
    ({"files": {"nodes": [{"changeType": "MODIFIED"}], "pageInfo": {"hasNextPage": False}}, "changedFiles": 1},
     "node without a path"),
    ({"files": {"nodes": [{"path": "", "changeType": "MODIFIED"}], "pageInfo": {"hasNextPage": False}},
      "changedFiles": 1}, "empty path"),
    ({"files": {"nodes": []}, "changedFiles": 0}, "no pageInfo"),
    ({"files": {"nodes": [], "pageInfo": {}}, "changedFiles": 0}, "no hasNextPage"),
    ({"files": {"nodes": [], "pageInfo": {"hasNextPage": None}}, "changedFiles": 0}, "null hasNextPage"),
    (_node(ORDINARY, total="absent"), "no changedFiles"),
    (_node(ORDINARY, total=None), "null changedFiles"),
    (_node(ORDINARY, total=True), "boolean changedFiles"),
    (_node(ORDINARY, "README.md", total=1), "fewer changedFiles than nodes"),
])
def test_read_malformed_is_unreadable_never_empty(node: dict[str, Any], label: str) -> None:
    changed = _read(node)
    assert changed.state == run_shepherd.CHANGED_PATHS_UNREADABLE, label
    assert run_shepherd.touches_agent_definitions(make_pr(changed_paths=changed)), label


def _read_with_rest(node: dict[str, Any], rest: Any) -> tuple[ChangedPaths, list[list[str]]]:
    calls: list[list[str]] = []

    def _rest(args: list[str]) -> Any:
        calls.append(args)
        if isinstance(rest, Exception):
            raise rest
        return rest

    with patch.object(run_shepherd, "_gh_api_json", _rest):
        return run_shepherd._read_changed_paths("mctlhq/mctl-agents", 7, node), calls


def test_read_a_rename_out_of_the_protected_tree_is_seen_at_its_source() -> None:
    """GraphQL names only a rename's destination. Moving a manifest out of
    `agents/_manifests/` deletes a definition, so the source must count."""
    destination = "attic/implementer.yaml"
    changed, calls = _read_with_rest(
        _node(destination, change_type="RENAMED"),
        [{"filename": destination, "status": "renamed", "previous_filename": MANIFEST}],
    )
    assert changed.state == run_shepherd.CHANGED_PATHS_COMPLETE
    assert set(changed.paths) == {destination, MANIFEST}
    assert run_shepherd.touches_agent_definitions(make_pr(changed_paths=changed))
    assert calls == [["repos/mctlhq/mctl-agents/pulls/7/files?per_page=100"]]


def test_read_an_ordinary_rename_stays_ordinary() -> None:
    changed, _ = _read_with_rest(
        _node("docs/b.md", change_type="RENAMED"),
        [{"filename": "docs/b.md", "status": "renamed", "previous_filename": "docs/a.md"}],
    )
    assert changed.state == run_shepherd.CHANGED_PATHS_COMPLETE
    assert not run_shepherd.touches_agent_definitions(make_pr(changed_paths=changed))


def test_read_an_unknown_change_type_asks_for_the_second_path() -> None:
    """A PatchStatus value this code does not know may carry a second path."""
    _, calls = _read_with_rest(_node("docs/b.md", change_type="SOMETHING_NEW"),
                               [{"filename": "docs/b.md", "status": "modified"}])
    assert len(calls) == 1


@pytest.mark.parametrize(("rest", "label"), [
    (subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"), "the REST read failed"),
    (json.JSONDecodeError("bad", "", 0), "the REST body is not JSON"),
    (None, "empty REST body"),
    ({"message": "Not Found"}, "REST body is not a list"),
    ([{"filename": "docs/b.md", "status": "renamed"}], "a rename without previous_filename"),
    ([{"filename": "docs/other.md", "status": "renamed", "previous_filename": "docs/a.md"}],
     "REST names different files than GraphQL"),
    ([], "REST returned fewer files"),
    (["docs/b.md"], "REST row is not an object"),
])
def test_read_a_rename_whose_source_cannot_be_established_is_unreadable(rest: Any, label: str) -> None:
    changed, _ = _read_with_rest(_node("docs/b.md", change_type="RENAMED"), rest)
    assert changed.state == run_shepherd.CHANGED_PATHS_UNREADABLE, label


def _snapshot_payload(**files: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": 42, "state": "OPEN", "merged": False, "isDraft": False,
        "mergeStateStatus": "CLEAN", "reviewDecision": "", "headRefOid": HEAD_SHA,
        "baseRefName": "main", "mergeCommit": None,
        "commits": {"nodes": [{"commit": {"oid": HEAD_SHA, "committedDate": "2026-04-29T10:00:00Z"}}]},
        "timelineItems": {"nodes": []},
        "statusCheckRollup": {"state": "SUCCESS"},
    }
    payload.update(files)
    return {"data": {"repository": {"pullRequest": payload}}}


def test_the_snapshot_query_asks_for_the_changed_files_and_carries_them() -> None:
    queries: list[str] = []

    def _gh(args: list[str]) -> Any:
        queries.append(" ".join(args))
        return _snapshot_payload(**_node(ORDINARY, MANIFEST))

    with patch.object(run_shepherd, "_gh_api_json", _gh), \
         patch.object(run_shepherd, "_fetch_required_status_check_contexts", return_value=()):
        snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-agents", 42)

    assert snap is not None
    assert "changedFiles files(first:100){nodes{path changeType} pageInfo{hasNextPage}}" in queries[0]
    assert snap.changed_paths.state == run_shepherd.CHANGED_PATHS_COMPLETE
    assert run_shepherd.merge_operation_for(snap) == pc.MERGE_OPERATION_AGENT_DEFINITION


def test_a_snapshot_whose_answer_has_no_files_field_is_unreadable() -> None:
    """The whole snapshot stays usable (review and fixes go on); only the
    merge fails closed."""
    with patch.object(run_shepherd, "_gh_api_json", return_value=_snapshot_payload()), \
         patch.object(run_shepherd, "_fetch_required_status_check_contexts", return_value=()):
        snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-agents", 42)

    assert snap is not None and snap.head_sha == HEAD_SHA
    assert snap.changed_paths.state == run_shepherd.CHANGED_PATHS_UNREADABLE
    assert run_shepherd.merge_operation_for(snap) == pc.MERGE_OPERATION_AGENT_DEFINITION


# ---------------------------------------------------------------------------
# The policy rule
# ---------------------------------------------------------------------------
def _merge_request(operation: str, head: str = HEAD_SHA) -> pc.ActionRequest:
    return pc.ActionRequest(pc.GITHUB_PR_MERGE, operation, "https://github.com/mctlhq/mctl-agents/pull/1",
                            pc.args_digest_of({"match_head_commit": head}), execution_id="ctx-470")


def test_the_definition_rule_is_listed_before_the_plain_merge_rule() -> None:
    ids = [r.rule_id for r in pc.BUILTIN_POLICY.rules]
    assert ids.index(DEFINITION_RULE) < ids.index("github-pr-merge")


@pytest.mark.parametrize("env_value", [None, "none", pc.MERGE_APPROVAL_REQUIRE])
def test_the_definition_merge_requires_approval_under_every_valid_policy(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None,
) -> None:
    """Regardless of MCTL_POLICY_MERGE_APPROVAL; and with no approval store
    (production today) REQUIRE_APPROVAL blocks."""
    if env_value is not None:
        monkeypatch.setenv(pc.MERGE_APPROVAL_ENV, env_value)
    policy = pc.configured_policy()
    rule, _code, _reason = pc.evaluate(policy, _merge_request(pc.MERGE_OPERATION_AGENT_DEFINITION))
    assert rule is not None
    assert (rule.rule_id, rule.verdict) == (DEFINITION_RULE, pc.REQUIRE_APPROVAL)

    decision = pc.decide(_merge_request(pc.MERGE_OPERATION_AGENT_DEFINITION), policy=policy)
    assert not decision.permitted
    assert (decision.rule_id, decision.code) == (DEFINITION_RULE, pc.CODE_APPROVAL_REQUIRED)


def test_the_definition_merge_is_denied_under_a_misconfigured_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pc.MERGE_APPROVAL_ENV, "sometimes")
    decision = pc.decide(_merge_request(pc.MERGE_OPERATION_AGENT_DEFINITION), policy=pc.configured_policy())
    assert (decision.verdict, decision.code) == (pc.DENY, pc.CODE_DENIED)


def test_the_plain_merge_rule_is_unchanged() -> None:
    decision = pc.decide(_merge_request(pc.MERGE_OPERATION), policy=pc.BUILTIN_POLICY)
    assert (decision.rule_id, decision.code) == ("github-pr-merge", pc.CODE_ALLOWED)


def test_a_definition_merge_is_a_different_action_per_head_and_from_a_plain_merge() -> None:
    definition = _merge_request(pc.MERGE_OPERATION_AGENT_DEFINITION)
    assert definition.action_digest() != _merge_request(pc.MERGE_OPERATION).action_digest()
    assert definition.action_digest() != _merge_request(pc.MERGE_OPERATION_AGENT_DEFINITION, OLD_SHA).action_digest()


# ---------------------------------------------------------------------------
# The in-pod merge path
# ---------------------------------------------------------------------------
def _stub_gh_merge(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_shepherd.subprocess, "run", fake_run)
    monkeypatch.setattr(run_shepherd, "refresh_github_token", lambda: None)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot",
                        lambda *_a, **_kw: SimpleNamespace(merge_commit="m" * 40))
    return calls


BLOCKED_PATHS = [
    pytest.param(ChangedPaths.complete((ORDINARY, MANIFEST)), id="definition"),
    pytest.param(ChangedPaths.complete((GITOPS_DEFINITION,)), id="gitops-definition"),
    pytest.param(ChangedPaths(run_shepherd.CHANGED_PATHS_TRUNCATED, (ORDINARY,), "read 100 of 250"), id="truncated"),
    pytest.param(ChangedPaths(run_shepherd.CHANGED_PATHS_UNREADABLE, (), "gh failed"), id="unreadable"),
]


@pytest.mark.parametrize("changed", BLOCKED_PATHS)
def test_merge_pr_blocks_without_an_approval_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], changed: ChangedPaths,
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    pr = make_pr(changed_paths=changed)

    assert run_shepherd.merge_pr(pr) == (False, None)

    assert calls == []
    out = capsys.readouterr().out
    [record] = _decisions(out)
    assert (record["rule_id"], record["operation"], record["code"]) == (
        DEFINITION_RULE, pc.MERGE_OPERATION_AGENT_DEFINITION, pc.CODE_APPROVAL_REQUIRED)
    assert record["metadata"]["head_sha"] == pr.head_sha
    [signal] = _signals(out)
    assert f"pr={pr.repo}#{pr.number} head={pr.head_sha}" in signal


@pytest.mark.parametrize("merge_approval", [None, pc.MERGE_APPROVAL_REQUIRE])
def test_merge_pr_never_asks_the_approval_store_from_a_pod(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], merge_approval: str | None,
) -> None:
    """With MCTL_POLICY_APPROVALS=mctl-api in the pod's own env the merge is
    still refused and no request is created: a pod's execution id changes
    every tick, so a request made here could never be redeemed (ADR 016).
    Regardless of MCTL_POLICY_MERGE_APPROVAL, for a service not opted in."""
    monkeypatch.setenv(pc.APPROVALS_ENV, pc.APPROVALS_MCTL_API)
    if merge_approval:
        monkeypatch.setenv(pc.MERGE_APPROVAL_ENV, merge_approval)

    def _no_store() -> Any:
        raise AssertionError("a shepherd pod asked the approval store")

    monkeypatch.setattr(pc, "configured_approvals", _no_store)
    calls = _stub_gh_merge(monkeypatch)

    assert run_shepherd.merge_pr(_definition_pr()) == (False, None)

    assert calls == []
    [record] = _decisions(capsys.readouterr().out)
    assert (record["rule_id"], record["code"]) == (DEFINITION_RULE, pc.CODE_APPROVAL_REQUIRED)


def test_merge_pr_of_an_ordinary_pr_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    pr = make_pr(changed_paths=ChangedPaths.complete((ORDINARY, "agents/_shepherd/shepherd.md")))

    assert run_shepherd.merge_pr(pr) == (True, "m" * 40)

    assert calls == [["gh", "pr", "merge", "--merge", "--delete-branch", "--match-head-commit", pr.head_sha,
                      f"https://github.com/{pr.repo}/pull/{pr.number}"]]
    out = capsys.readouterr().out
    [record] = _decisions(out)
    assert (record["rule_id"], record["operation"], record["code"]) == (
        "github-pr-merge", pc.MERGE_OPERATION, pc.CODE_ALLOWED)
    assert _signals(out) == []


def test_merge_pr_calls_the_transport_as_before_for_an_ordinary_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    """No new argument reaches `merge_pr_unchecked` for an ordinary PR."""
    seen: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(run_shepherd, "merge_pr_unchecked",
                        lambda *a, **kw: (seen.append((a, kw)), (True, "m" * 40))[1])
    pr = make_pr()

    assert run_shepherd.merge_pr(pr) == (True, "m" * 40)
    assert seen == [((pr,), {})]


@pytest.mark.parametrize("changed", BLOCKED_PATHS)
def test_the_transport_refuses_a_definition_pr_decided_as_a_plain_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], changed: ChangedPaths,
) -> None:
    """The sweep: any caller of `merge_pr_unchecked`, present or future,
    that did not decide the agent-definition operation is refused."""
    calls = _stub_gh_merge(monkeypatch)

    assert run_shepherd.merge_pr_unchecked(make_pr(changed_paths=changed)) == (False, None)

    assert calls == []
    assert "error: refusing to merge" in capsys.readouterr().out


def test_the_transport_refuses_an_operation_the_pr_does_not_call_for(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_gh_merge(monkeypatch)
    result = run_shepherd.merge_pr_unchecked(make_pr(), operation=pc.MERGE_OPERATION_AGENT_DEFINITION)
    assert result == (False, None)
    assert calls == []


def test_the_transport_runs_a_definition_merge_decided_under_its_own_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    pr = _definition_pr()

    result = run_shepherd.merge_pr_unchecked(pr, operation=pc.MERGE_OPERATION_AGENT_DEFINITION)

    assert result == (True, "m" * 40)
    assert len(calls) == 1 and pr.head_sha in calls[0]


# ---------------------------------------------------------------------------
# process_one: the signal, its dedupe, and what it must not cost
# ---------------------------------------------------------------------------
def _tick(ref: run_shepherd.ProposalRef, pr: run_shepherd.PRSnapshot) -> run_shepherd.ShepherdResult:
    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=APPROVED_REVIEW), \
         patch.object(run_shepherd, "read_copilot_review", return_value=run_shepherd.CopilotReview(False, 0)):
        return process_one(ref, skip_subprocess=True)


def test_process_one_signals_a_definition_pr_once_per_head(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path, review_attempts=2)
    pr = _definition_pr()

    first = _tick(ref, pr)

    assert first.decision == "defer-merge"
    assert first.error is None
    out = capsys.readouterr().out
    assert len(_signals(out)) == 1
    assert len(_decisions(out)) == 1
    status = read_status(ref)
    assert status["merge_needs_human_head"] == pr.head_sha
    assert status["merge_needs_human"] == run_shepherd.MERGE_NEEDS_HUMAN_AGENT_DEFINITION
    # Not a failure, not an attempt: nothing that leads to review-stuck or
    # rejected moved.
    assert status["status"] == "implemented"
    assert status["review_attempts"] == 2
    for counter in ("harness_failures", "refusals", "ci_infra_retries", "ci_probe_failures", "failure"):
        assert counter not in status
    written = ref.status_path.read_text()

    for _ in range(3):
        again = _tick(ref, pr)
        assert again.decision == "defer-merge"
    out = capsys.readouterr().out
    assert _signals(out) == []
    assert _decisions(out) == []
    assert ref.status_path.read_text() == written, "a repeated tick must not rewrite .status.yaml"
    assert calls == []


def test_process_one_signals_again_for_a_new_head(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)
    _tick(ref, _definition_pr())
    capsys.readouterr()

    _tick(ref, _definition_pr(head_sha=OLD_SHA))

    [signal] = _signals(capsys.readouterr().out)
    assert f"head={OLD_SHA}" in signal
    assert read_status(ref)["merge_needs_human_head"] == OLD_SHA


def test_process_one_observes_a_human_merge_as_usual(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)
    _tick(ref, _definition_pr())

    result = _tick(ref, _definition_pr(merged=True, merge_commit="c" * 40))

    assert result.decision == "flip-to-merged"
    status = read_status(ref)
    assert (status["status"], status["merge_commit"]) == ("merged", "c" * 40)
    assert "merge_needs_human" not in status
    assert "merge_needs_human_head" not in status


def test_process_one_clears_the_signal_when_the_pr_is_closed(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)
    _tick(ref, _definition_pr())

    result = _tick(ref, _definition_pr(closed_unmerged=True))

    assert result.decision == "flip-to-rejected"
    status = read_status(ref)
    assert "merge_needs_human" not in status and "merge_needs_human_head" not in status


@pytest.mark.parametrize(("terminal", "decision"), [
    ({"merged": True, "merge_commit": "c" * 40}, "flip-to-merged"),
    ({"closed_unmerged": True}, "flip-to-rejected"),
])
def test_the_reconciler_clears_the_signal_on_its_terminal_flips(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, terminal: dict[str, Any], decision: str,
) -> None:
    """The reconciler can be the one that observes the human's merge."""
    _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)
    _tick(ref, _definition_pr())
    assert read_status(ref)["merge_needs_human_head"] == HEAD_SHA

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=_definition_pr(**terminal)):
        result = run_shepherd.reconcile_one(ref)

    assert result.decision == decision
    status = read_status(ref)
    assert "merge_needs_human" not in status and "merge_needs_human_head" not in status


def test_process_one_merges_an_ordinary_pr_as_before(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)

    result = _tick(ref, make_pr())

    assert result.decision == "merge"
    assert len(calls) == 1 and calls[0][:3] == ["gh", "pr", "merge"]
    status = read_status(ref)
    assert status["status"] == "merged"
    assert "merge_needs_human" not in status and "merge_needs_human_head" not in status
    assert _signals(capsys.readouterr().out) == []


def test_process_one_ordinary_merge_failure_is_still_a_wait(tmp_path: Any) -> None:
    """A refused ordinary merge keeps its own arm: `wait`, no signal fields."""
    ref = make_ref(tmp_path)
    with patch.object(run_shepherd, "merge_pr", return_value=(False, None)):
        result = _tick(ref, make_pr())
    assert result.decision == "wait"
    assert "merge_needs_human_head" not in read_status(ref)


# ---------------------------------------------------------------------------
# The gated activity
# ---------------------------------------------------------------------------
pytest_anyio = pytest.mark.anyio


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _action(approval_ref: str = "") -> GatedActionInput:
    return GatedActionInput(
        payload={"repo": "mctlhq/mctl-web", "pr_number": 42, "head_sha": HEAD_SHA, "service": "mctl-web"},
        execution_id="ctx-470", actor="system:dev-loop-workflow", trace_id="tr-470", approval_ref=approval_ref,
    )


def _serve(monkeypatch: pytest.MonkeyPatch, pr: run_shepherd.PRSnapshot) -> list[tuple[tuple, dict]]:
    """The activity's reads answer `pr` and an approving review; returns the
    list the stubbed transport records its calls in."""
    merges: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: pr)
    monkeypatch.setattr(run_shepherd, "read_codex_review", lambda _pr: APPROVED_REVIEW)
    monkeypatch.setattr(run_shepherd, "merge_pr_unchecked",
                        lambda *a, **kw: (merges.append((a, kw)), (True, "m" * 40))[1])
    return merges


def _approval_store(monkeypatch: pytest.MonkeyPatch) -> FakeMctlApi:
    monkeypatch.setenv(pc.APPROVALS_ENV, pc.APPROVALS_MCTL_API)
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    fake = FakeMctlApi(_Clock())  # type: ignore[arg-type]
    monkeypatch.setattr(aa, "_no_redirect_opener", lambda: fake)
    return fake


@pytest_anyio
@pytest.mark.parametrize("changed", BLOCKED_PATHS)
async def test_activity_blocks_a_definition_pr_without_an_approval_store(
    monkeypatch: pytest.MonkeyPatch, changed: ChangedPaths,
) -> None:
    """Production today (gate off, no store): `approval_required`, no merge
    -- not `merge_gate_disabled`, which would say the PR is not gated."""
    merges = _serve(monkeypatch, make_pr(changed_paths=changed))

    result = await ActivityEnvironment().run(act.merge_pull_request_gated, _action())

    assert result.code == pc.CODE_APPROVAL_REQUIRED
    assert not result.ran
    assert merges == []


@pytest_anyio
async def test_activity_gate_off_ordinary_pr_is_gate_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    merges = _serve(monkeypatch, make_pr())

    result = await ActivityEnvironment().run(act.merge_pull_request_gated, _action())

    assert result.code == act.CODE_MERGE_GATE_DISABLED
    assert merges == []


@pytest_anyio
async def test_activity_gate_off_unreadable_snapshot_is_not_gate_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether the PR changes definitions could not be observed: that is
    not "the gate is off for it"."""
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: None)

    result = await ActivityEnvironment().run(act.merge_pull_request_gated, _action())

    assert result.code == act.CODE_MERGE_PRECONDITION_UNMET


@pytest_anyio
@pytest.mark.parametrize("gate_on", [False, True], ids=["gate-off", "gate-on"])
async def test_activity_merges_a_definition_pr_only_on_a_receipt_bound_to_the_head(
    monkeypatch: pytest.MonkeyPatch, gate_on: bool,
) -> None:
    """With an approval store: a pending request under the definition rule,
    nothing merged until a human approves, then exactly one merge, decided
    and transported under the agent-definition operation. The same whether
    or not the service is behind the #519 gate."""
    if gate_on:
        monkeypatch.setenv(pc.MERGE_APPROVAL_ENV, pc.MERGE_APPROVAL_REQUIRE)
        monkeypatch.setattr(run_shepherd, "SHEPHERD_MERGE_APPROVAL_SERVICES", frozenset({"mctl-web"}))
    fake = _approval_store(monkeypatch)
    pr = _definition_pr()
    merges = _serve(monkeypatch, pr)
    env = ActivityEnvironment()

    first = await env.run(act.merge_pull_request_gated, _action())

    assert first.code == pc.CODE_APPROVAL_PENDING
    assert first.approval_ref and not first.ran
    assert merges == []
    record = fake.only()
    assert record["policy_rule_id"] == DEFINITION_RULE
    assert record["args_digest"] == pc.args_digest_of(
        {"method": "merge", "delete_branch": True, "match_head_commit": pr.head_sha})

    fake.decide(first.approval_ref, "approve")
    second = await env.run(act.merge_pull_request_gated, _action(first.approval_ref))

    assert second.code == pc.CODE_APPROVED and second.ran
    assert merges == [((pr,), {"operation": pc.MERGE_OPERATION_AGENT_DEFINITION})]

    third = await env.run(act.merge_pull_request_gated, _action(first.approval_ref))
    assert not third.ran
    assert len(merges) == 1


@pytest_anyio
async def test_activity_receipt_for_one_head_does_not_merge_another(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _approval_store(monkeypatch)
    _serve(monkeypatch, _definition_pr())
    env = ActivityEnvironment()
    first = await env.run(act.merge_pull_request_gated, _action())
    fake.decide(first.approval_ref, "approve")

    moved = _definition_pr(head_sha=OLD_SHA)
    merges = _serve(monkeypatch, moved)
    inp = GatedActionInput(
        payload={**_action().payload, "head_sha": OLD_SHA}, execution_id="ctx-470",
        actor="system:dev-loop-workflow", trace_id="tr-470", approval_ref=first.approval_ref,
    )
    result = await env.run(act.merge_pull_request_gated, inp)

    assert not result.ran
    assert result.code == pc.CODE_APPROVAL_INTENT_MISMATCH
    assert merges == []


@pytest_anyio
async def test_activity_ordinary_pr_behind_the_gate_keeps_the_plain_merge_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(pc.MERGE_APPROVAL_ENV, pc.MERGE_APPROVAL_REQUIRE)
    monkeypatch.setattr(run_shepherd, "SHEPHERD_MERGE_APPROVAL_SERVICES", frozenset({"mctl-web"}))
    fake = _approval_store(monkeypatch)
    pr = make_pr()
    merges = _serve(monkeypatch, pr)
    env = ActivityEnvironment()

    first = await env.run(act.merge_pull_request_gated, _action())
    assert fake.only()["policy_rule_id"] == "github-pr-merge-approval"
    fake.decide(first.approval_ref, "approve")
    second = await env.run(act.merge_pull_request_gated, _action(first.approval_ref))

    assert second.ran
    assert merges == [((pr,), {})]
