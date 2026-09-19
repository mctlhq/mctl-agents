"""Unit tests for ``orchestrator.ci_checks`` (mctl-agents#411).

Covers the classification table (T15), the annotation-first excerpt builder
(T4-adjacent — the #409 mypy fixture), staleness discarding (T4), requiredness
resolution (per-context signal, branch-protection fallback,
SHEPHERD_CI_REQUIRED_OVERRIDE, advisory default), pending detection (T9), the
contexts(last:100) truncation guard, and never-raises probe failure (T8-style).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from orchestrator import ci_checks
from orchestrator.ci_checks import CheckBlocker, CIStatus, _classify, read_required_checks

HEAD_SHA = "a" * 40
OLD_SHA = "b" * 40


@dataclass
class _FakePR:
    """Minimal stand-in for run_shepherd.PRSnapshot — the structural subset
    ci_checks._PRLike actually reads."""

    repo: str = "mctlhq/mctl-web"
    number: int = 42
    head_sha: str = HEAD_SHA
    check_contexts: tuple = field(default_factory=tuple)
    required_contexts: tuple = field(default_factory=tuple)


def _check_run_node(
    *,
    name: str = "lint",
    status: str = "COMPLETED",
    conclusion: str = "FAILURE",
    is_required: bool | None = True,
    commit_oid: str = HEAD_SHA,
    check_run_id: int = 555,
    workflow_run_id: int = 999,
    workflow_name: str = "PR validation",
    details_url: str = "https://github.com/mctlhq/mctl-web/actions/runs/999",
    title: str = "",
    summary: str = "",
) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": details_url,
        "isRequired": is_required,
        "title": title,
        "summary": summary,
        "databaseId": check_run_id,
        "checkSuite": {
            "databaseId": 111,
            "workflowRun": {
                "databaseId": workflow_run_id,
                "url": details_url,
                "workflow": {"name": workflow_name},
            },
        },
        "_commit_oid": commit_oid,
    }


def _status_context_node(
    *,
    context: str = "ci/legacy",
    state: str = "FAILURE",
    is_required: bool | None = True,
    commit_oid: str = HEAD_SHA,
    target_url: str | None = "https://ci.example/build/1",
) -> dict:
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": target_url,
        "isRequired": is_required,
        "_commit_oid": commit_oid,
    }


# ---------------------------------------------------------------------------
# _classify — T15
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("conclusion", sorted(ci_checks.CI_INFRA_CONCLUSIONS))
def test_classify_infra_conclusions_are_always_infrastructure(conclusion: str) -> None:
    assert _classify(conclusion, "anything at all", has_annotations=True) == "infrastructure"
    assert _classify(conclusion, "", has_annotations=False) == "infrastructure"


def test_classify_failure_with_annotations_is_actionable() -> None:
    assert _classify("FAILURE", "orchestrator/x.py:1: error: bad", has_annotations=True) == "actionable"


@pytest.mark.parametrize(
    "pattern_text",
    [
        "Error: The runner has lost communication with the server",
        "The runner has received a shutdown signal",
        "connection reset by peer",
        "TLS handshake timeout",
        "429 Too Many Requests",
        "you are being rate limited",
        "no space left on device",
        "The operation was cancelled",
    ],
)
def test_classify_failure_no_annotations_matching_infra_pattern(pattern_text: str) -> None:
    assert _classify("FAILURE", pattern_text, has_annotations=False) == "infrastructure"


def test_classify_failure_no_annotations_unmatched_excerpt_is_actionable() -> None:
    """Unclassifiable defaults to actionable — the deliberate direction."""
    assert _classify("FAILURE", "AssertionError: expected 1 got 2", has_annotations=False) == "actionable"
    assert _classify("FAILURE", "", has_annotations=False) == "actionable"


def test_classify_unknown_conclusion_defaults_actionable() -> None:
    assert _classify("SOMETHING_NEW", "whatever", has_annotations=False) == "actionable"


# ---------------------------------------------------------------------------
# read_required_checks — staleness, requiredness, pending, truncation
# ---------------------------------------------------------------------------
def test_stale_head_produces_empty_blocker_set() -> None:
    """T4: a failing check node anchored to an OLD commit oid is discarded."""
    pr = _FakePR(
        head_sha=HEAD_SHA,
        check_contexts=(_check_run_node(commit_oid=OLD_SHA),),
    )
    status = read_required_checks(pr)
    assert status.known is True
    assert status.blockers == ()


def test_advisory_failing_check_does_not_block() -> None:
    """T7: not required (no per-context signal, absent from required_contexts)."""
    pr = _FakePR(
        check_contexts=(_check_run_node(is_required=False),),
        required_contexts=(),
    )
    status = read_required_checks(pr)
    assert status.blockers == ()


def test_branch_protection_fallback_marks_required() -> None:
    """No per-context isRequired signal (None) — falls back to
    pr.required_contexts."""
    pr = _FakePR(
        check_contexts=(_check_run_node(is_required=None, name="lint"),),
        required_contexts=("lint",),
    )
    with patch.object(ci_checks, "_fetch_annotations", return_value=[]):
        status = read_required_checks(pr)
    assert len(status.blockers) == 1
    assert status.blockers[0].required is True


def test_required_override_env_marks_required(monkeypatch) -> None:
    monkeypatch.setenv("SHEPHERD_CI_REQUIRED_OVERRIDE", "lint, other-check")
    pr = _FakePR(check_contexts=(_check_run_node(is_required=None, name="lint"),))
    with patch.object(ci_checks, "_fetch_annotations", return_value=[]):
        status = read_required_checks(pr)
    assert len(status.blockers) == 1


def test_no_requiredness_signal_at_all_is_advisory() -> None:
    pr = _FakePR(check_contexts=(_check_run_node(is_required=None, name="lint"),))
    status = read_required_checks(pr)
    assert status.blockers == ()


def test_pending_required_check_sets_pending_and_no_blocker() -> None:
    """T9: QUEUED/IN_PROGRESS -> pending, no remediation evidence."""
    pr = _FakePR(check_contexts=(_check_run_node(status="IN_PROGRESS", conclusion=""),))
    status = read_required_checks(pr)
    assert status.pending is True
    assert status.blockers == ()


def test_passing_check_is_not_a_blocker() -> None:
    pr = _FakePR(check_contexts=(_check_run_node(conclusion="SUCCESS"),))
    status = read_required_checks(pr)
    assert status.blockers == ()
    assert status.pending is False


def test_cancelled_conclusion_is_infrastructure_not_actionable() -> None:
    """T5."""
    pr = _FakePR(check_contexts=(_check_run_node(conclusion="CANCELLED"),))
    status = read_required_checks(pr)
    assert len(status.blockers) == 1
    assert status.blockers[0].kind == "infrastructure"
    assert status.actionable == ()
    assert status.infrastructure == status.blockers


def test_contexts_page_cap_truncation_fails_closed() -> None:
    nodes = tuple(_check_run_node(name=f"check-{i}", conclusion="SUCCESS") for i in range(100))
    pr = _FakePR(check_contexts=nodes)
    status = read_required_checks(pr)
    assert status.known is False
    assert status.blockers == ()


def test_status_context_failure_normalises_into_a_blocker() -> None:
    pr = _FakePR(check_contexts=(_status_context_node(state="ERROR"),))
    status = read_required_checks(pr)
    assert len(status.blockers) == 1
    b = status.blockers[0]
    assert b.name == "ci/legacy"
    assert b.job is None
    assert b.conclusion == "FAILURE"


def test_status_context_pending_state_sets_pending() -> None:
    pr = _FakePR(check_contexts=(_status_context_node(state="PENDING"),))
    status = read_required_checks(pr)
    assert status.pending is True
    assert status.blockers == ()


def test_probe_never_raises_on_unexpected_node_shape() -> None:
    """A malformed/unexpected node must degrade to known=False, not raise."""
    pr = _FakePR(check_contexts=({"__typename": "CheckRun", "_commit_oid": HEAD_SHA},))

    with patch.object(ci_checks, "_normalize_node", side_effect=TypeError("boom")):
        status = read_required_checks(pr)
    assert status.known is False
    assert status.blockers == ()


# ---------------------------------------------------------------------------
# Excerpt builder — the #409 mypy annotation fixture
# ---------------------------------------------------------------------------
def test_annotation_excerpt_matches_the_409_fixture() -> None:
    annotations = [
        {
            "path": "orchestrator/run_implementer.py",
            "start_line": 3185,
            "message": (
                "error: Incompatible types in assignment (expression has type "
                '"dict[str, Any] | None", variable has type "bool")'
            ),
            "title": "Run mypy",
        }
    ]
    pr = _FakePR(check_contexts=(_check_run_node(name="lint", workflow_name="PR validation"),))
    with patch.object(ci_checks, "_fetch_annotations", return_value=annotations):
        status = read_required_checks(pr)
    assert len(status.blockers) == 1
    b = status.blockers[0]
    assert b.kind == "actionable"
    assert b.workflow == "PR validation"
    assert b.name == "lint"
    assert b.step == "Run mypy"
    assert b.excerpt == (
        "orchestrator/run_implementer.py:3185: error: Incompatible types in "
        'assignment (expression has type "dict[str, Any] | None", variable '
        'has type "bool")'
    )


def test_annotations_api_failure_degrades_to_check_output_text() -> None:
    pr = _FakePR(
        check_contexts=(
            _check_run_node(title="lint", summary="mypy found 1 error"),
        )
    )
    with patch.object(
        ci_checks, "run_capturing", side_effect=subprocess.CalledProcessError(1, ["gh"]),
    ):
        status = read_required_checks(pr)
    assert len(status.blockers) == 1
    assert "mypy found 1 error" in status.blockers[0].excerpt
    assert status.blockers[0].kind == "actionable"


def test_excerpt_is_bounded_to_max_chars() -> None:
    long_message = "x" * (ci_checks.CI_MAX_EXCERPT_CHARS * 2)
    annotations = [{"path": "f.py", "start_line": 1, "message": long_message}]
    pr = _FakePR(check_contexts=(_check_run_node(),))
    with patch.object(ci_checks, "_fetch_annotations", return_value=annotations):
        status = read_required_checks(pr)
    assert len(status.blockers[0].excerpt) <= ci_checks.CI_MAX_EXCERPT_CHARS + len(" ...(truncated)")


# ---------------------------------------------------------------------------
# CIStatus properties
# ---------------------------------------------------------------------------
def test_cistatus_actionable_and_infrastructure_filter_by_kind() -> None:
    actionable_blocker = CheckBlocker(
        name="lint", workflow=None, job=None, step=None, conclusion="FAILURE",
        url=None, run_id=None, head_sha=HEAD_SHA, excerpt="x", kind="actionable",
        required=True,
    )
    infra_blocker = CheckBlocker(
        name="build", workflow=None, job=None, step=None, conclusion="CANCELLED",
        url=None, run_id=None, head_sha=HEAD_SHA, excerpt="", kind="infrastructure",
        required=True,
    )
    status = CIStatus(known=True, head_sha=HEAD_SHA, pending=False, blockers=(actionable_blocker, infra_blocker))
    assert status.actionable == (actionable_blocker,)
    assert status.infrastructure == (infra_blocker,)
