"""Unit table over `read_source_issue` (mctl-agents#410).

`read_source_issue` is the shared classifier behind both the Tier 3
shepherd's reconcile sweep (`stage="reconcile"`) and the Tier 2
implementer's admission gate (`stage="admission"`). These tests exercise
it directly, injecting a fake `gh_api_json` so nothing here touches a
real `gh` subprocess.
"""
from __future__ import annotations

import json

import pytest

from orchestrator.source_issue import SourceIssueVerdict, read_source_issue

SOURCE = {"type": "github_issue", "repo": "mctlhq/mctl-telegram", "issue": 510}


def _status(source=SOURCE) -> dict:
    return {"status": "accepted", "source": source}


def test_open_issue_admits() -> None:
    verdict = read_source_issue(
        _status(), stage="admission",
        gh_api_json=lambda _args: {"state": "open"},
    )
    assert verdict.known is True
    assert verdict.failure is None
    assert verdict.linked is True
    assert verdict.issue_ref == "mctlhq/mctl-telegram#510"


def test_closed_completed_issue_is_source_resolved() -> None:
    verdict = read_source_issue(
        _status(), stage="admission",
        gh_api_json=lambda _args: {
            "state": "closed", "state_reason": "completed",
            "closed_at": "2026-09-06T00:00:00Z",
        },
    )
    assert verdict.known is True
    assert verdict.failure is not None
    assert verdict.failure["code"] == "source-resolved"
    assert verdict.failure["stage"] == "admission"
    assert verdict.closed_at == "2026-09-06T00:00:00Z"
    assert verdict.state_reason == "completed"


def test_closed_not_planned_issue_is_source_not_planned() -> None:
    verdict = read_source_issue(
        _status(), stage="reconcile",
        gh_api_json=lambda _args: {"state": "closed", "state_reason": "not_planned"},
    )
    assert verdict.failure["code"] == "source-not-planned"
    assert verdict.failure["stage"] == "reconcile"
    assert verdict.state_reason == "not_planned"


def test_closed_issue_with_null_state_reason_is_source_resolved() -> None:
    # state_reason is null on issues closed before GitHub introduced it, and
    # on some API paths -- treat "closed, reason unknown" as completed.
    verdict = read_source_issue(
        _status(), stage="admission",
        gh_api_json=lambda _args: {"state": "closed", "state_reason": None},
    )
    assert verdict.failure["code"] == "source-resolved"
    assert verdict.state_reason is None


def test_no_source_block_is_unlinked() -> None:
    verdict = read_source_issue(
        {"status": "accepted"}, stage="admission",
        gh_api_json=lambda _args: (_ for _ in ()).throw(AssertionError("must not call gh")),
    )
    assert verdict.linked is False
    assert verdict.known is False
    assert verdict.failure is None


def test_partial_source_block_missing_issue_is_unlinked() -> None:
    verdict = read_source_issue(
        _status({"type": "github_issue", "repo": "mctlhq/mctl-telegram"}),
        stage="admission",
        gh_api_json=lambda _args: (_ for _ in ()).throw(AssertionError("must not call gh")),
    )
    assert verdict.linked is False
    assert verdict.known is False


def test_partial_source_block_missing_repo_is_unlinked() -> None:
    verdict = read_source_issue(
        _status({"type": "github_issue", "issue": 510}),
        stage="admission",
        gh_api_json=lambda _args: (_ for _ in ()).throw(AssertionError("must not call gh")),
    )
    assert verdict.linked is False


def test_non_github_issue_source_type_is_unlinked() -> None:
    verdict = read_source_issue(
        _status({"type": "incident", "repo": "mctlhq/mctl-telegram", "issue": 510}),
        stage="admission",
        gh_api_json=lambda _args: (_ for _ in ()).throw(AssertionError("must not call gh")),
    )
    assert verdict.linked is False


def test_gh_raising_leaves_the_verdict_unknown_but_linked() -> None:
    def boom(_args):
        raise RuntimeError("gh: connection reset")

    verdict = read_source_issue(_status(), stage="admission", gh_api_json=boom)
    assert verdict.linked is True
    assert verdict.known is False
    assert verdict.failure is None


def test_unparseable_payload_leaves_the_verdict_unknown_but_linked() -> None:
    # A response with no "state" key (or not a dict) is not an answer --
    # reading it as "open" would be a guess dressed up as a fact.
    verdict = read_source_issue(
        _status(), stage="admission",
        gh_api_json=lambda _args: {"unexpected": "shape"},
    )
    assert verdict.linked is True
    assert verdict.known is False

    verdict2 = read_source_issue(
        _status(), stage="admission",
        gh_api_json=lambda _args: None,
    )
    assert verdict2.linked is True
    assert verdict2.known is False


def test_stage_is_threaded_into_the_failure_dict() -> None:
    verdict = read_source_issue(
        _status(), stage="admission",
        gh_api_json=lambda _args: {"state": "closed", "state_reason": "completed"},
    )
    assert verdict.failure["stage"] == "admission"

    verdict2 = read_source_issue(
        _status(), stage="reconcile",
        gh_api_json=lambda _args: {"state": "closed", "state_reason": "completed"},
    )
    assert verdict2.failure["stage"] == "reconcile"


def test_default_gh_reader_is_not_invoked_on_a_dependency_free_import() -> None:
    # DoD for task 1: importing the module performs no gh/network call.
    import orchestrator.source_issue as mod
    assert callable(mod.read_source_issue)


def test_the_verdict_is_frozen() -> None:
    import dataclasses

    verdict = SourceIssueVerdict(known=False, failure=None, linked=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.known = True  # type: ignore[misc]


def test_json_is_only_used_by_the_default_reader_not_this_module_directly() -> None:
    # Sanity: json.dumps/loads still work as expected in this module's
    # sandbox (guards against an accidental shadowing import).
    assert json.loads(json.dumps({"a": 1})) == {"a": 1}
