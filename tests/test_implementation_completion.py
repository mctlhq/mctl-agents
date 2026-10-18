"""`ImplementationCompletion` schema contract (mctl-agents#428, ADR 021)."""
from __future__ import annotations

import json
from typing import Any

import pytest

from orchestrator import implementation_completion as ic

DIGEST = "sha256:" + "a" * 64
HEAD = "b" * 40


def _checks(result: str = "pass") -> list[ic.VerificationRecord]:
    return [
        ic.VerificationRecord(check_id, result, "exit_zero" if result == "pass" else "exit_nonzero", True)
        for check_id in sorted(ic.REQUIRED_CHECKS_V1ALPHA1)
    ]


def _seal(
    outcome: str = "complete",
    code: str = "gates_passed",
    *,
    step: str = "primary",
    attempt_id: str = "attempt-1",
    run_id: str = "wf-1",
    head: str | None = HEAD,
    verification: list[ic.VerificationRecord] | None = None,
    remaining: list[ic.RemainingWorkItem] | None = None,
    detail: str | None = None,
    created_at: str | None = None,
) -> ic.ImplementationCompletion:
    return ic.seal(
        subject=ic.CompletionSubject("mctl-web", "issue-1-x"),
        attempt=ic.AttemptBinding(attempt_id=attempt_id, run_id=run_id, step=step, phase="implement"),
        source=ic.SourceBinding("mctlhq/mctl-web", "origin/HEAD", "feat/agents-issue-1-x", head_sha=head),
        proposal=ic.ProposalBinding(DIGEST),
        outcome=outcome,
        stop_reason=ic.stop_reason(code, detail),
        verification=_checks() if verification is None else verification,
        remaining_work=remaining or [],
        created_at=created_at,
    )


def _failure(outcome: str, code: str, **kw: Any) -> ic.ImplementationCompletion:
    return _seal(outcome, code, head=None, verification=[], **kw)


# One valid record per outcome.
_PER_OUTCOME: dict[str, Any] = {
    "complete": lambda: _seal(),
    "incomplete": lambda: _seal(
        "incomplete", "known_work_remains", remaining=[ic.RemainingWorkItem("t1", "finish task 1")]
    ),
    "bounded_refusal": lambda: _failure("bounded_refusal", "deliberate_no_op"),
    "budget_exhausted": lambda: _failure("budget_exhausted", "rate_limited"),
    "deterministic_failure": lambda: _failure("deterministic_failure", "no_commits"),
    "harness_failure": lambda: _failure("harness_failure", "fenced"),
}


def test_every_outcome_is_covered() -> None:
    assert set(_PER_OUTCOME) == ic.OUTCOMES


@pytest.mark.parametrize("outcome", sorted(_PER_OUTCOME))
def test_round_trip_for_every_outcome(outcome: str) -> None:
    record = _PER_OUTCOME[outcome]()
    again = ic.ImplementationCompletion.from_dict(json.loads(record.to_json()))
    assert again == record
    assert again.api_version == "completion.mctl.ai/v1alpha1"
    assert again.kind == "ImplementationCompletion"
    assert record.content_hash.startswith("sha256:")
    assert record.completion_id == "ic-" + record.content_hash[7:23]
    assert len(record.completion_id) == 3 + 16


def test_hash_is_stable_across_key_order_and_created_at() -> None:
    a = _seal(created_at="2026-01-01T00:00:00Z")
    b = _seal(created_at="2027-06-01T12:00:00Z")
    assert a.content_hash == b.content_hash
    assert a.completion_id == b.completion_id
    shuffled = dict(reversed(list(a.to_dict().items())))
    assert ic.ImplementationCompletion.from_dict(shuffled).content_hash == a.content_hash


def test_run_id_and_step_are_part_of_the_hash() -> None:
    primary = _seal(step="primary", attempt_id="same")
    fallback = _seal(step="fallback", attempt_id="same")
    other_run = _seal(step="primary", attempt_id="same", run_id="wf-2")
    assert primary.completion_id != fallback.completion_id
    assert primary.completion_id != other_run.completion_id


@pytest.mark.parametrize(
    "key", ["approved", "review_verdict", "merge", "release", "owner", "extend_budget", "extend_claim"]
)
def test_forbidden_and_unknown_keys_are_rejected(key: str) -> None:
    data = _seal().to_dict()
    data[key] = True
    with pytest.raises(ic.ImplementationCompletionError, match="unknown key"):
        ic.ImplementationCompletion.from_dict(data)
    for block in ("subject", "attempt", "source", "proposal", "stop_reason"):
        nested = _seal().to_dict()
        nested[block][key] = True
        with pytest.raises(ic.ImplementationCompletionError, match="unknown key"):
            ic.ImplementationCompletion.from_dict(nested)


def test_unsupported_api_version_is_rejected() -> None:
    data = _seal().to_dict()
    data["api_version"] = "completion.mctl.ai/v2"
    with pytest.raises(ic.ImplementationCompletionError, match="unsupported api_version"):
        ic.ImplementationCompletion.from_dict(data)


def test_a_tampered_record_fails_its_hash() -> None:
    data = _seal().to_dict()
    data["source"]["branch"] = "feat/agents-other"
    with pytest.raises(ic.ImplementationCompletionError, match="content_hash"):
        ic.ImplementationCompletion.from_dict(data)


def test_outcome_set_is_closed() -> None:
    assert ic.OUTCOMES == {
        "complete", "incomplete", "bounded_refusal", "budget_exhausted", "deterministic_failure", "harness_failure",
    }
    with pytest.raises(ic.ImplementationCompletionError):
        _failure("approved", "no_commits")


def test_stop_reason_must_belong_to_the_outcome() -> None:
    with pytest.raises(ic.ImplementationCompletionError, match="does not belong"):
        _failure("harness_failure", "no_commits")
    assert "scope_done" not in ic.STOP_REASON_CODES
    assert ic.STOP_REASON_OUTCOMES["gates_passed"] == "complete"


def test_list_bounds_are_enforced() -> None:
    too_many = [ic.RemainingWorkItem(f"t{i}", "x") for i in range(33)]
    with pytest.raises(ic.ImplementationCompletionError, match="at most 32"):
        _seal("incomplete", "known_work_remains", remaining=too_many)
    ok = [ic.RemainingWorkItem(f"t{i}", "x") for i in range(32)]
    assert len(_seal("incomplete", "known_work_remains", remaining=ok).remaining_work) == 32
    data = _seal().to_dict()
    data["verification"] = data["verification"] * 9
    with pytest.raises(ic.ImplementationCompletionError, match="at most 32"):
        ic.ImplementationCompletion.from_dict(data)


def test_leaf_caps_are_enforced() -> None:
    with pytest.raises(ic.ImplementationCompletionError):
        ic.RemainingWorkItem("t1", "x" * 257)
    with pytest.raises(ic.ImplementationCompletionError):
        ic.StopReason("no_commits", "x" * 601)
    # ...while the building helpers bound rather than refuse.
    assert len(ic.stop_reason("no_commits", "x" * 601).detail or "") == ic.MAX_DETAIL_CHARS
    assert len(ic.remaining_item("t1", "x" * 300).summary) == ic.MAX_SUMMARY_CHARS
    with pytest.raises(ic.ImplementationCompletionError):
        ic.SourceBinding("mctlhq/mctl-web", "origin/HEAD", "b", head_sha="not-a-sha")


def test_complete_requires_every_required_check_to_pass() -> None:
    checks = _checks()
    checks[0] = ic.VerificationRecord(checks[0].check_id, "fail", "exit_nonzero", True)
    with pytest.raises(ic.ImplementationCompletionError, match="required check"):
        _seal(verification=checks)
    with pytest.raises(ic.ImplementationCompletionError, match="required check"):
        _seal(verification=_checks()[:3])


def test_complete_rejects_remaining_work_and_a_null_head() -> None:
    with pytest.raises(ic.ImplementationCompletionError, match="remaining_work"):
        _seal(remaining=[ic.RemainingWorkItem("t1", "more")])
    with pytest.raises(ic.ImplementationCompletionError, match="head_sha"):
        _seal(head=None)


def test_incomplete_requires_remaining_work() -> None:
    with pytest.raises(ic.ImplementationCompletionError, match="remaining_work"):
        _seal("incomplete", "known_work_remains")


def test_a_non_required_check_may_fail_without_blocking_complete() -> None:
    extra = ic.VerificationRecord("command_budget", "fail", "budget_denied", False)
    assert _seal(verification=[*_checks(), extra]).outcome == "complete"


def test_required_flag_is_a_property_of_the_contract_version() -> None:
    assert ic.REQUIRED_CHECKS_V1ALPHA1 == {
        "commits_present", "chart_major_guard", "push_succeeded", "claim_valid_at_push",
    }
    with pytest.raises(ic.ImplementationCompletionError, match="required"):
        ic.VerificationRecord("command_budget", "pass", "exit_zero", True)
    with pytest.raises(ic.ImplementationCompletionError, match="required"):
        ic.VerificationRecord("commits_present", "pass", "exit_zero", False)


def test_verification_record_has_no_free_form_output_field() -> None:
    assert ic.VerificationRecord._KEYS == {"check_id", "result", "classification", "required", "evidence_ref"}
    with pytest.raises(ic.ImplementationCompletionError, match="unknown key"):
        ic.VerificationRecord.from_dict(
            {"check_id": "x", "result": "pass", "classification": "exit_zero", "required": False, "stdout": "hi"}
        )


def test_free_text_is_redacted_before_sealing() -> None:
    token = "ghp_" + "A" * 30
    record = _failure(
        "deterministic_failure", "no_commits", detail=f"push failed with {token}\nsecond line"
    )
    assert record.stop_reason.detail == "[redacted]"
    assert token not in record.to_json()
    item = ic.remaining_item("t1", f"use {token}")
    sealed = _seal("incomplete", "known_work_remains", remaining=[item])
    assert sealed.remaining_work[0].summary == "[redacted]"
    # A hostile record that skipped sealing is refused outright.
    data = record.to_dict()
    data["stop_reason"]["detail"] = token
    with pytest.raises(ic.ImplementationCompletionError, match="credential"):
        ic.ImplementationCompletion.from_dict(data)


def test_detail_is_normalised_and_bounded() -> None:
    record = _failure("deterministic_failure", "no_commits", detail="a\n  b\t" + "c" * 900)
    assert record.stop_reason.detail is not None
    assert len(record.stop_reason.detail) <= ic.MAX_DETAIL_CHARS
    assert "\n" not in record.stop_reason.detail


def test_module_states_what_complete_means() -> None:
    doc = " ".join((ic.__doc__ or "").split())
    assert "the attempt pushed a head that passed the orchestrator gates" in doc
    assert 'does NOT mean "the approved scope is implemented"' in doc
    assert "F1" in doc


def test_module_imports_nothing_from_the_runners() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ic))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {m for m in imported if "run_implementer" in m or "run_shepherd" in m}


# ---------------------------------------------------------------------------
# select_authoritative: the four-row truth table
# ---------------------------------------------------------------------------
def test_authority_row_1_a_complete_fallback_supersedes_any_primary() -> None:
    for primary in (_failure("harness_failure", "unexpected_error"), _seal(step="primary")):
        fallback = _seal(step="fallback", attempt_id="attempt-2")
        projection = ic.select_authoritative(ic.summarize(primary), fallback)
        assert projection.authoritative.completion_id == fallback.completion_id
        assert projection.other is not None
        assert projection.other.relation == "superseded"
        assert projection.other.completion_id == primary.completion_id
        assert projection.primary_observed is True


def test_authority_row_2_a_failing_fallback_never_replaces_the_primary() -> None:
    primary = _failure("deterministic_failure", "no_commits")
    for outcome, code in (
        ("harness_failure", "unexpected_error"),
        ("budget_exhausted", "rate_limited"),
        ("bounded_refusal", "deliberate_no_op"),
    ):
        fallback = _failure(outcome, code, step="fallback", attempt_id="attempt-2")
        projection = ic.select_authoritative(ic.summarize(primary), fallback)
        assert projection.authoritative.completion_id == primary.completion_id
        assert projection.authoritative.relation is None
        assert projection.other is not None
        assert projection.other.relation == "fallback"
        assert projection.other.outcome == outcome
        assert projection.primary_observed is True


def test_authority_row_3_without_a_primary_handoff_the_fallback_speaks_alone() -> None:
    for fallback in (
        _seal(step="fallback"),
        _failure("harness_failure", "unexpected_error", step="fallback"),
    ):
        projection = ic.select_authoritative(None, fallback)
        assert projection.authoritative.completion_id == fallback.completion_id
        assert projection.other is None
        assert projection.primary_observed is False


def test_authority_row_4_the_primary_always_speaks_for_itself() -> None:
    primary = _failure("harness_failure", "fenced")
    projection = ic.project_primary(primary)
    assert projection.authoritative.completion_id == primary.completion_id
    assert projection.other is None
    assert projection.primary_observed is True


def test_select_authoritative_refuses_misplaced_steps() -> None:
    primary = _seal(step="primary")
    with pytest.raises(ic.ImplementationCompletionError):
        ic.select_authoritative(None, primary)
    with pytest.raises(ic.ImplementationCompletionError):
        ic.select_authoritative(ic.summarize(_seal(step="fallback")), _seal(step="fallback"))


def test_projection_round_trips_and_is_bounded() -> None:
    primary = _failure("deterministic_failure", "no_commits")
    fallback = _failure("harness_failure", "unexpected_error", step="fallback", attempt_id="b")
    projection = ic.select_authoritative(ic.summarize(primary), fallback)
    data = projection.to_dict()
    assert data["authoritative"]["verification"] == {
        "required_passed": 0, "required_total": 0, "optional_total": 0,
    }
    assert ic.CompletionProjection.from_dict(json.loads(json.dumps(data))) == projection
    data["verification_details"] = []
    with pytest.raises(ic.ImplementationCompletionError, match="unknown key"):
        ic.CompletionProjection.from_dict(data)


def test_the_summary_carries_the_records_proposal_digest() -> None:
    record = _seal()
    summary = ic.summarize(record)
    assert summary.proposal_digest == record.proposal.proposal_digest
    assert summary.to_dict()["proposal_digest"] == record.proposal.proposal_digest
    assert ic.CompletionSummary.from_dict(summary.to_dict()) == summary


def test_a_projection_written_before_the_proposal_digest_still_reads() -> None:
    data = ic.project_primary(_seal()).to_dict()
    del data["authoritative"]["proposal_digest"]
    projection = ic.CompletionProjection.from_dict(data)
    assert projection.authoritative.proposal_digest is None
    assert "proposal_digest" not in projection.to_dict()["authoritative"]


@pytest.mark.parametrize("value", ["", "sha256:short", "md5:" + "a" * 64, 7])
def test_a_malformed_projected_proposal_digest_is_refused(value: object) -> None:
    data = ic.project_primary(_seal()).to_dict()
    data["authoritative"]["proposal_digest"] = value
    with pytest.raises(ic.ImplementationCompletionError):
        ic.CompletionProjection.from_dict(data)


@pytest.mark.parametrize("key", ["approval", "content_digest", "approved_by"])
def test_the_projection_still_rejects_approval_shaped_keys(key: str) -> None:
    data = ic.project_primary(_seal()).to_dict()
    data["authoritative"][key] = "sha256:" + "a" * 64
    with pytest.raises(ic.ImplementationCompletionError, match="unknown key"):
        ic.CompletionProjection.from_dict(data)
