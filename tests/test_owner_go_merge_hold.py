"""A PR whose merge the owner reserved is never merged by automation.

`merge:owner-go` on the PR or on the proposal's source issue routes the merge
through the same human gate as an agent-definition PR (ADR 016 amendment 3).
It exists because `mctlhq/mctl-agents#1013` was merged by the shepherd 15
minutes after its approval while the owner's instruction, "return for a
separate owner GO-MERGE", lived only in prose. A label read that fails holds
the merge too: "could not observe" is never "observed absent".
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from orchestrator import policy_checkpoint as pc
from orchestrator import run_shepherd
from orchestrator.run_shepherd import CodexReview, MergeLabels, process_one
from orchestrator.source_issue import read_source_issue
from orchestrator.temporal.activities import pr_merge as act
from orchestrator.temporal.activities.action_approval import GatedActionInput, GatedActionResult
from tests.test_run_shepherd import HEAD_SHA, OLD_SHA, make_pr, make_ref, read_status

LABEL = run_shepherd.OWNER_GO_LABEL
RULE = "github-pr-merge-owner-go"
APPROVED_REVIEW = CodexReview(has_responded=True, findings=[], head_verdict="APPROVED")
SOURCE = {"type": "github_issue", "repo": "mctlhq/mctl-web", "issue": 7}
HELD = [
    pytest.param(MergeLabels.complete((LABEL,)), "owner-go", id="label"),
    pytest.param(MergeLabels.complete(("bug", LABEL)), "owner-go", id="label-among-others"),
    pytest.param(MergeLabels(run_shepherd.MERGE_LABELS_TRUNCATED, (), "more"), "merge-labels-truncated",
                 id="truncated"),
    pytest.param(MergeLabels(run_shepherd.MERGE_LABELS_UNREADABLE, (), "gh failed"), "merge-labels-unreadable",
                 id="unreadable"),
]


@pytest.fixture(autouse=True)
def _production_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pc.MERGE_APPROVAL_ENV, raising=False)
    monkeypatch.delenv(pc.APPROVALS_ENV, raising=False)
    monkeypatch.setattr(run_shepherd, "SHEPHERD_MERGE_APPROVAL_SERVICES", frozenset())


def _stub_gh_merge(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_shepherd.subprocess, "run", fake_run)
    monkeypatch.setattr(run_shepherd, "refresh_github_token", lambda: None)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: SimpleNamespace(merge_commit="m" * 40))
    return calls


def _ref_with_source(tmp_path: Path, source: Any = SOURCE) -> run_shepherd.ProposalRef:
    ref = make_ref(tmp_path)
    data = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    data["source"] = source
    ref.status_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return ref


def _issue(*labels: Any, state: str = "open") -> dict[str, Any]:
    return {"state": state, "labels": [{"name": name} for name in labels]}


def _tick(
    ref: run_shepherd.ProposalRef, pr: run_shepherd.PRSnapshot, issue: Any = None,
) -> run_shepherd.ShepherdResult:
    def source_read(_args: list[str]) -> Any:
        if isinstance(issue, Exception):
            raise issue
        return issue

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=pr), \
         patch.object(run_shepherd, "read_codex_review", return_value=APPROVED_REVIEW), \
         patch.object(run_shepherd, "read_copilot_review", return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "_gh_api_json", side_effect=source_read):
        return process_one(ref, skip_subprocess=True)


# ---------------------------------------------------------------------------
# The read: complete, truncated and unreadable stay three different answers
# ---------------------------------------------------------------------------
def _labels_connection(*names: str, has_next: Any = False) -> dict[str, Any]:
    return {"labels": {"nodes": [{"name": n} for n in names], "pageInfo": {"hasNextPage": has_next}}}


def test_the_snapshot_default_is_unreadable_never_no_labels() -> None:
    assert MergeLabels().state == run_shepherd.MERGE_LABELS_UNREADABLE


@pytest.mark.parametrize("names", [(), ("bug",), (LABEL, "bug")])
def test_a_whole_page_is_a_complete_read(names: tuple[str, ...]) -> None:
    read = run_shepherd._read_merge_labels(_labels_connection(*names))
    assert read.state == run_shepherd.MERGE_LABELS_COMPLETE
    assert set(read.names) == set(names)


def test_a_page_with_more_behind_it_is_truncated() -> None:
    read = run_shepherd._read_merge_labels(_labels_connection("bug", has_next=True))
    assert read.state == run_shepherd.MERGE_LABELS_TRUNCATED
    assert read.names == ("bug",)


@pytest.mark.parametrize("node", [
    pytest.param({}, id="no-connection"),
    pytest.param({"labels": None}, id="null-connection"),
    pytest.param({"labels": {"pageInfo": {"hasNextPage": False}}}, id="no-nodes"),
    pytest.param({"labels": {"nodes": "x", "pageInfo": {"hasNextPage": False}}}, id="nodes-not-a-list"),
    pytest.param({"labels": {"nodes": [{}], "pageInfo": {"hasNextPage": False}}}, id="node-without-name"),
    pytest.param({"labels": {"nodes": [None], "pageInfo": {"hasNextPage": False}}}, id="null-node"),
    pytest.param({"labels": {"nodes": []}}, id="no-page-info"),
    pytest.param(_labels_connection(has_next=None), id="has-next-not-a-bool"),
])
def test_a_malformed_answer_is_a_failed_read_not_an_empty_list(node: dict[str, Any]) -> None:
    assert run_shepherd._read_merge_labels(node).state == run_shepherd.MERGE_LABELS_UNREADABLE


def test_the_snapshot_query_really_asks_for_the_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the parser could be right and the query never carry the
    field, which would hold every PR forever."""
    seen: list[str] = []

    def fake(args: list[str]) -> dict[str, Any]:
        seen.extend(args)
        return {"data": {"repository": {"pullRequest": {
            "number": 42, "state": "OPEN", "merged": False, "isDraft": False, "headRefOid": HEAD_SHA,
            "mergeStateStatus": "CLEAN", **_labels_connection(LABEL),
        }}}}

    monkeypatch.setattr(run_shepherd, "_gh_api_json", fake)
    monkeypatch.setattr(run_shepherd, "_read_changed_paths", lambda *_a: run_shepherd.ChangedPaths.complete())

    snap = run_shepherd._fetch_pr_snapshot("mctlhq/mctl-web", 42)

    assert snap is not None
    assert snap.merge_labels == MergeLabels.complete((LABEL,))
    query = next(a for a in seen if a.startswith("query="))
    assert "labels(first:100){nodes{name} pageInfo{hasNextPage}}" in query


def test_including_keeps_the_names_of_both_and_the_worse_state() -> None:
    complete = MergeLabels.complete(("bug",))
    assert complete.including(MergeLabels.complete((LABEL,))).names == ("bug", LABEL)
    failed = MergeLabels(run_shepherd.MERGE_LABELS_UNREADABLE, (), "boom")
    assert complete.including(failed).state == run_shepherd.MERGE_LABELS_UNREADABLE
    assert failed.including(complete).state == run_shepherd.MERGE_LABELS_UNREADABLE
    truncated = MergeLabels(run_shepherd.MERGE_LABELS_TRUNCATED, (LABEL,), "more")
    assert truncated.including(failed).state == run_shepherd.MERGE_LABELS_UNREADABLE
    assert failed.including(truncated).state == run_shepherd.MERGE_LABELS_UNREADABLE
    assert complete.including(truncated).names == ("bug", LABEL)


# ---------------------------------------------------------------------------
# The source issue's labels
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("state", ["open", "closed"])
def test_read_source_issue_reports_the_labels_in_either_state(state: str) -> None:
    verdict = read_source_issue(
        {"source": SOURCE}, stage="reconcile", gh_api_json=lambda _a: _issue("bug", LABEL, state=state),
    )
    assert verdict.known
    assert verdict.labels == ("bug", LABEL)


@pytest.mark.parametrize("labels", ["x", None, [{}], [None], [{"name": 1}]])
def test_read_source_issue_says_none_when_the_labels_are_malformed(labels: Any) -> None:
    verdict = read_source_issue(
        {"source": SOURCE}, stage="reconcile", gh_api_json=lambda _a: {"state": "open", "labels": labels},
    )
    assert verdict.known, "the state was read, and the staleness answer depends only on it"
    assert verdict.labels is None


def test_read_source_issue_keeps_an_empty_list_distinct_from_no_answer() -> None:
    verdict = read_source_issue({"source": SOURCE}, stage="reconcile", gh_api_json=lambda _a: _issue())
    assert verdict.labels == ()
    assert verdict.labels is not None


# ---------------------------------------------------------------------------
# The classifier and the policy rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("labels", "kind"), HELD)
def test_a_held_pr_is_decided_under_its_own_operation(labels: MergeLabels, kind: str) -> None:
    pr = make_pr(merge_labels=labels)
    assert run_shepherd.merge_operation_for(pr) == pc.MERGE_OPERATION_OWNER_GO
    assert run_shepherd.needs_human_kind(pr) == kind


@pytest.mark.parametrize("names", [(), ("bug",), ("merge:owner-go-later",), ("MERGE:OWNER",)])
def test_any_other_label_changes_nothing(names: tuple[str, ...]) -> None:
    """Positive control: the gate must not hold every PR."""
    pr = make_pr(merge_labels=MergeLabels.complete(names))
    assert run_shepherd.merge_operation_for(pr) == pc.MERGE_OPERATION
    assert run_shepherd.needs_human_kind(pr) == ""


def test_a_definition_pr_with_the_label_stays_under_the_definition_rule() -> None:
    pr = make_pr(
        changed_paths=run_shepherd.ChangedPaths.complete(("agents/_mentor/CLAUDE.md",)),
        merge_labels=MergeLabels.complete((LABEL,)),
    )
    assert run_shepherd.merge_operation_for(pr) == pc.MERGE_OPERATION_AGENT_DEFINITION


def _merge_rule(policy: pc.Policy) -> pc.Rule:
    [rule] = [r for r in policy.rules if r.operation == pc.MERGE_OPERATION_OWNER_GO]
    return rule


def test_the_rule_requires_an_approval_in_every_enabled_variant() -> None:
    assert (_merge_rule(pc.BUILTIN_POLICY).rule_id, _merge_rule(pc.BUILTIN_POLICY).verdict) == (
        RULE, pc.REQUIRE_APPROVAL)
    assert (_merge_rule(pc.MERGE_APPROVAL_POLICY).rule_id, _merge_rule(pc.MERGE_APPROVAL_POLICY).verdict) == (
        RULE, pc.REQUIRE_APPROVAL)
    ids = [r.rule_id for r in pc.BUILTIN_POLICY.rules]
    assert ids.index(RULE) < ids.index("github-pr-merge"), "a later widening of `merge` must not shadow it"


def test_a_misconfigured_policy_denies_it_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pc.MERGE_APPROVAL_ENV, "sometimes")
    assert _merge_rule(pc.configured_policy()).verdict == pc.DENY


# ---------------------------------------------------------------------------
# Every merge path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("labels", "kind"), HELD)
def test_merge_pr_blocks_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], labels: MergeLabels, kind: str,
) -> None:
    calls = _stub_gh_merge(monkeypatch)

    assert run_shepherd.merge_pr(make_pr(merge_labels=labels)) == (False, None)

    assert calls == []
    out = capsys.readouterr().out
    assert f"{run_shepherd.MERGE_NEEDS_HUMAN} " in out
    assert f"reason={kind}" in out
    assert f'"rule_id": "{RULE}"' in out or f'"rule_id":"{RULE}"' in out


@pytest.mark.parametrize(("labels", "_kind"), HELD)
def test_the_transport_refuses_a_held_pr_decided_as_a_plain_merge(
    monkeypatch: pytest.MonkeyPatch, labels: MergeLabels, _kind: str,
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    assert run_shepherd.merge_pr_unchecked(make_pr(merge_labels=labels)) == (False, None)
    assert calls == []


def test_the_transport_runs_a_held_merge_decided_under_its_own_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_gh_merge(monkeypatch)
    pr = make_pr(merge_labels=MergeLabels.complete((LABEL,)))
    assert run_shepherd.merge_pr_unchecked(pr, operation=pc.MERGE_OPERATION_OWNER_GO) == (True, "m" * 40)
    assert len(calls) == 1


def _action() -> GatedActionInput:
    return GatedActionInput(
        payload={"repo": "mctlhq/mctl-web", "pr_number": 42, "head_sha": HEAD_SHA, "service": "mctl-web"},
        execution_id="ctx", actor="system:dev-loop-workflow", trace_id="tr", approval_ref="",
    )


def test_the_gated_activity_decides_a_held_pr_under_its_operation_with_the_gate_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate off, ordinary PR: nothing happens. Gate off, held PR: the activity
    is the one path that can carry the human decision, so it goes on."""
    gated: list[tuple[Any, ...]] = []

    def fake_gated(*args: Any, **_kw: Any) -> GatedActionResult:
        gated.append(args)
        return GatedActionResult(code="stub", reason="")

    held = make_pr(merge_labels=MergeLabels.complete((LABEL,)))
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: held)
    monkeypatch.setattr(run_shepherd, "read_codex_review", lambda _pr: APPROVED_REVIEW)
    monkeypatch.setattr(act, "read_required_checks", lambda _pr: None)
    monkeypatch.setattr(run_shepherd, "decide", lambda *_a, **_kw: ("merge", None))
    monkeypatch.setattr(act, "run_gated", fake_gated)

    act._sync_merge_pull_request_gated(_action())

    assert [args[2] for args in gated] == [pc.MERGE_OPERATION_OWNER_GO]


def test_the_gated_activity_keeps_gate_off_for_an_unlabelled_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda *_a, **_kw: make_pr())
    result = act._sync_merge_pull_request_gated(_action())
    assert result.code == act.CODE_MERGE_GATE_DISABLED


# ---------------------------------------------------------------------------
# process_one: the PR that started this, and what must keep working
# ---------------------------------------------------------------------------
def test_an_approved_clean_pr_with_the_label_is_not_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path, review_attempts=2)
    pr = make_pr(merge_labels=MergeLabels.complete((LABEL,)))

    result = _tick(ref, pr)

    assert result.decision == "defer-merge"
    assert result.error is None
    assert calls == []
    status = read_status(ref)
    assert (status["merge_needs_human"], status["merge_needs_human_head"]) == ("owner-go", HEAD_SHA)
    assert status["status"] == "implemented", "waiting for a person is not a failure"
    assert status["review_attempts"] == 2
    assert capsys.readouterr().out.count(run_shepherd.MERGE_NEEDS_HUMAN + " ") == 1


def test_the_label_on_the_source_issue_holds_a_pr_that_carries_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slice case: the owner labels the child issue, and the implementer
    opens a PR that knows nothing about it."""
    calls = _stub_gh_merge(monkeypatch)
    ref = _ref_with_source(tmp_path)

    result = _tick(ref, make_pr(), issue=_issue("roadmap", LABEL))

    assert result.decision == "defer-merge"
    assert calls == []
    assert read_status(ref)["merge_needs_human"] == "owner-go"


@pytest.mark.parametrize("issue", [
    pytest.param(RuntimeError("502"), id="transport"),
    pytest.param(None, id="empty-answer"),
    pytest.param({"message": "nope"}, id="no-state"),
    pytest.param({"state": "open"}, id="no-labels-field"),
    pytest.param({"state": "open", "labels": "x"}, id="malformed-labels"),
])
def test_a_source_issue_that_cannot_be_read_holds_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issue: Any,
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = _ref_with_source(tmp_path)

    result = _tick(ref, make_pr(), issue=issue)

    assert result.decision == "defer-merge"
    assert calls == []
    assert read_status(ref)["merge_needs_human"] == "merge-labels-unreadable"


def test_a_status_file_that_cannot_be_read_holds_the_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable `.status.yaml` must not read as "no source link". Only
    the read inside the hold is broken: the tick's own reads stay real."""
    calls = _stub_gh_merge(monkeypatch)
    ref = _ref_with_source(tmp_path)
    monkeypatch.setattr(run_shepherd, "_source_status_for_merge", lambda _ref: (_ for _ in ()).throw(ValueError("bad")))

    result = _tick(ref, make_pr(), issue=_issue())

    assert result.decision == "defer-merge"
    assert calls == []


def test_a_status_file_that_vanished_holds_the_merge_rather_than_reading_as_unlinked(
    tmp_path: Path,
) -> None:
    ref = _ref_with_source(tmp_path)
    ref.status_path.unlink()
    held = run_shepherd._with_source_issue_labels(ref, make_pr())
    assert held.merge_labels.state == run_shepherd.MERGE_LABELS_UNREADABLE


def test_an_unlabelled_pr_and_issue_merge_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = _ref_with_source(tmp_path)

    result = _tick(ref, make_pr(), issue=_issue("roadmap"))

    assert result.decision == "merge"
    assert len(calls) == 1
    assert read_status(ref)["status"] == "merged"


def test_a_proposal_with_no_source_link_merges_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An incident-responder proposal legitimately has none: observed absent."""
    calls = _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)

    assert _tick(ref, make_pr()).decision == "merge"
    assert len(calls) == 1


def test_a_failed_read_clears_on_a_later_tick_and_the_pr_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = _ref_with_source(tmp_path)

    assert _tick(ref, make_pr(), issue=RuntimeError("502")).decision == "defer-merge"
    assert read_status(ref)["merge_needs_human"] == "merge-labels-unreadable"
    assert _tick(ref, make_pr(), issue=_issue()).decision == "merge"

    assert len(calls) == 1
    assert "merge_needs_human" not in read_status(ref)


def test_removing_the_label_hands_the_merge_back_and_clears_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)

    assert _tick(ref, make_pr(merge_labels=MergeLabels.complete((LABEL,)))).decision == "defer-merge"
    assert calls == []
    assert _tick(ref, make_pr()).decision == "merge"

    assert len(calls) == 1
    assert "merge_needs_human" not in read_status(ref)


def test_a_new_head_signals_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)
    labels = MergeLabels.complete((LABEL,))

    _tick(ref, make_pr(merge_labels=labels, head_sha=OLD_SHA))
    _tick(ref, make_pr(merge_labels=labels, head_sha=OLD_SHA))
    assert capsys.readouterr().out.count(run_shepherd.MERGE_NEEDS_HUMAN + " ") == 1
    _tick(ref, make_pr(merge_labels=labels))
    assert capsys.readouterr().out.count(run_shepherd.MERGE_NEEDS_HUMAN + " ") == 1
    assert read_status(ref)["merge_needs_human_head"] == HEAD_SHA


def test_the_owner_merging_by_hand_closes_the_proposal_as_usual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_gh_merge(monkeypatch)
    ref = make_ref(tmp_path)
    _tick(ref, make_pr(merge_labels=MergeLabels.complete((LABEL,))))

    result = _tick(ref, make_pr(merged=True, merge_commit="c" * 40, merge_labels=MergeLabels.complete((LABEL,))))

    assert result.decision == "flip-to-merged"
    assert calls == []
    status = read_status(ref)
    assert status["status"] == "merged"
    assert "merge_needs_human" not in status
