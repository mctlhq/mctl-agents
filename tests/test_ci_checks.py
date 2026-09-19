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
    """Unit-level only. NOTE: `SKIPPED`/`NEUTRAL` are members of this set but
    the pipeline short-circuits them in `_read_required_checks` before
    `_classify` is ever reached, so this parametrisation does NOT cover them
    end to end — `test_skipped_and_neutral_required_checks_are_never_blockers`
    is the test that pins the reachable behaviour.
    """
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


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL"])
def test_skipped_and_neutral_required_checks_are_never_blockers(conclusion: str) -> None:
    """The short-circuit in `_read_required_checks`, pinned at the pipeline level.

    GitHub does not gate required-check mergeability on `SKIPPED` or
    `NEUTRAL`, so a required check in either state must produce no blocker at
    all — not an `infrastructure` one. An infrastructure blocker is not inert:
    it feeds the shepherd's `ci-infra` re-run arm and spends review attempts,
    so classifying these as infrastructure wedges a PR GitHub already
    considers green.

    This repo emits skipped required checks routinely (`diagrams.yml`,
    `pr-validation.yml`), which is why this is the regression that matters.
    Deleting the short-circuit leaves every `_classify` test green, so this
    is the only assertion standing between that line and a silent wedge
    (mctl-agents#411 review rounds 2-4, carried P2).
    """
    pr = _FakePR(check_contexts=(_check_run_node(conclusion=conclusion, is_required=True),))
    status = read_required_checks(pr)
    assert status.known is True
    assert status.blockers == (), (
        f"a required {conclusion} check must not become a blocker of any kind"
    )
    assert status.infrastructure == ()
    assert status.actionable == ()
    assert status.pending is False


@pytest.mark.parametrize("conclusion", ["SKIPPED", "NEUTRAL"])
def test_skipped_and_neutral_do_not_mask_a_real_failure(conclusion: str) -> None:
    """The short-circuit must skip only its own node, not end the walk."""
    pr = _FakePR(check_contexts=(
        _check_run_node(name="diagrams", conclusion=conclusion),
        _check_run_node(name="lint", conclusion="FAILURE"),
    ))
    with patch.object(ci_checks, "_fetch_annotations", return_value=[]):
        status = read_required_checks(pr)
    assert [b.name for b in status.blockers] == ["lint"]


def test_probe_never_raises_on_a_non_dict_node() -> None:
    """A scalar where a node should be raises AttributeError inside the walk.

    `_normalize_node` calls `node.get(...)` unguarded, so this escaped the old
    enumerated catch tuple and crashed the shepherd tick for every OTHER
    proposal in the run, rather than failing closed on this one PR.
    """
    pr = _FakePR(check_contexts=("not-a-dict",))
    status = read_required_checks(pr)
    assert status.known is False
    assert status.blockers == ()


def test_probe_never_raises_on_a_scalar_check_suite() -> None:
    """`checkSuite` arriving as a scalar — same AttributeError, one level in."""
    node = _check_run_node()
    node["checkSuite"] = "unexpected"
    pr = _FakePR(check_contexts=(node,))
    status = read_required_checks(pr)
    assert status.known is False
    assert status.blockers == ()


def test_probe_never_raises_when_gh_is_missing() -> None:
    """A missing/non-executable `gh` raises OSError, not CalledProcessError."""
    pr = _FakePR(check_contexts=(_check_run_node(conclusion="FAILURE"),))
    with patch.object(ci_checks, "_fetch_annotations", side_effect=FileNotFoundError("gh")):
        status = read_required_checks(pr)
    assert status.known is False
    assert status.blockers == ()


def test_annotations_degrade_to_the_check_output_rather_than_the_whole_probe() -> None:
    """`_fetch_annotations` has its own "never raises" contract, and it is the
    RECOVERABLE one: falling back to title/summary costs an excerpt, while
    letting the error reach `read_required_checks`' outer catch costs the
    whole probe — the PR reads `known=False` and stops being mergeable at all
    over one check's annotations. `OSError` escaped the enumerated tuple here
    too (review P3).
    """
    pr = _FakePR(check_contexts=(_check_run_node(
        conclusion="FAILURE", title="Run mypy", summary="1 error",
    ),))
    with patch.object(ci_checks, "run_capturing", side_effect=FileNotFoundError("gh")):
        status = read_required_checks(pr)

    assert status.known is True, "one check's annotations must not fail the probe"
    assert len(status.blockers) == 1
    assert status.blockers[0].excerpt, "it must have fallen back to title/summary"


def test_annotations_degrade_when_refresh_github_token_fails() -> None:
    pr = _FakePR(check_contexts=(_check_run_node(conclusion="FAILURE", title="Run mypy"),))
    with patch.object(
        ci_checks, "refresh_github_token", side_effect=RuntimeError("no token")
    ):
        status = read_required_checks(pr)

    assert status.known is True
    assert len(status.blockers) == 1


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
