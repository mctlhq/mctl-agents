"""ImplementationCompletion emission in the Tier 2 implementer
(mctl-agents#428, ADR 021).

Pins: the one mapping from an attempt's end to (outcome, stop_reason); the
primary / fallback authority rule driven through `_implement_one` twice; the
replay and staleness guards; and that emission changes nothing an existing
consumer reads.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from orchestrator import implementation_completion as ic
from orchestrator import run_implementer as ri
from orchestrator.rate_limit import RateLimitExhaustedError
from orchestrator.source_issue import SourceIssueVerdict

HEAD = "c" * 40
PR_URL = "https://github.com/mctlhq/mctl-telegram/pull/7"
OPEN = SourceIssueVerdict(known=True, failure=None, issue_ref="mctlhq/mctl-telegram#510")


# ---------------------------------------------------------------------------
# T2: every exit code and every ImplementResult arm maps to exactly one pair
# ---------------------------------------------------------------------------
def _error_for(code: int) -> str:
    return {
        ri.EXIT_GENERIC_FAILURE: "RuntimeError: boom",
        ri.EXIT_NO_FOLLOWUP_COMMITS: "implementer produced no follow-up commits",
        ri.EXIT_BRANCH_MISSING_ON_ORIGIN: "branch feat/agents-x not found on origin; refusing",
        ri.EXIT_OPERATION_TIMEOUT: "operation timed out: operation exceeded 600s",
        ri.EXIT_ORPHANED_SUBAGENT: "orphaned sub-agent: 1 task still running",
        ri.EXIT_DELIBERATE_NO_OP: f"{ri.REFUSAL_ERROR_PREFIX} already addressed",
        ri.EXIT_FENCED: f"{ri.FENCED_ERROR_PREFIX} epoch moved",
        ri.EXIT_CLAIM_REFUSED: f"{ri.CLAIM_REFUSED_ERROR_PREFIX} another executor",
        ri.EXIT_CI_EVIDENCE_INSUFFICIENT: f"{ri.CI_EVIDENCE_INSUFFICIENT_ERROR_PREFIX} excerpt too short",
        ri.EXIT_VERIFICATION_BUDGET_EXHAUSTED: f"{ri.VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX} denied=1",
        ri.EXIT_RATE_LIMITED: f"{ri.RATE_LIMITED_ERROR_PREFIX} window exhausted",
        ri.EXIT_POLICY_REFUSED: f"{ri.POLICY_REFUSED_ERROR_PREFIX} denied",
        ri.EXIT_POLICY_UNDECIDED: f"{ri.POLICY_UNDECIDED_ERROR_PREFIX} evaluator down",
    }[code]


def _ref(tmp_path: Path) -> ri.ProposalRef:
    d = tmp_path / "mctl-telegram" / "proposals" / "issue-510-x"
    d.mkdir(parents=True, exist_ok=True)
    (d / "requirements.md").write_text("r\n", encoding="utf-8")
    status = d / ".status.yaml"
    if not status.exists():
        status.write_text(yaml.safe_dump({"status": "accepted"}), encoding="utf-8")
    return ri.ProposalRef(
        service="mctl-telegram", slug="issue-510-x", proposal_dir=d, status="accepted", approval_ok=True
    )


def _obs(**kw: Any) -> ri._AttemptObservations:
    base: dict[str, Any] = {
        "phase": "implement",
        "branch": "feat/agents-issue-510-x",
        "attempt_id": "attempt-1",
        "proposal_digest": "sha256:" + "d" * 64,
        "head_sha": HEAD,
        "commits_present": True,
        "chart_guard_passed": True,
        "pushed": True,
    }
    base.update(kw)
    return ri._AttemptObservations(**base)


def test_every_exit_constant_has_a_completion_mapping() -> None:
    """Fails when an `EXIT_*` constant is added without deciding its completion."""
    constants = {v for k, v in vars(ri).items() if k.startswith("EXIT_") and isinstance(v, int)}
    assert constants == set(ri._EXIT_CODE_COMPLETIONS)
    # 45 is batch-only and pre-claim: no attempt, so no completion.
    assert ri._EXIT_CODE_COMPLETIONS[ri.EXIT_BLOCKED_ONLY] is None


@pytest.mark.parametrize(
    "code,expected",
    [(c, m) for c, m in ri._EXIT_CODE_COMPLETIONS.items() if m is not None and c != ri.EXIT_OK],
)
def test_each_error_arm_maps_to_exactly_one_pair(code: int, expected: tuple[str, str], tmp_path: Path) -> None:
    error = _error_for(code)
    assert ri._review_feedback_exit_code(error) == code
    result = ri.ImplementResult(ref=_ref(tmp_path), pr_url=None, error=error)
    outcome, stop, exit_code = ri._classify_result(result, remaining=False)
    assert (outcome, stop) == expected
    assert exit_code == code
    # ...and the pair seals: outcome and code agree, invariants hold.
    record = ri.completion_from_result(result.ref, result, _obs(pushed=False, head_sha=None, commits_present=None))
    assert (record.outcome, record.stop_reason.code) == expected


def test_the_success_arm_is_complete_and_names_its_gates(tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    result = ri.ImplementResult(ref=ref, pr_url=PR_URL)
    record = ri.completion_from_result(ref, result, _obs())
    assert (record.outcome, record.stop_reason.code) == ("complete", "gates_passed")
    assert {v.check_id for v in record.verification if v.required} == ic.REQUIRED_CHECKS_V1ALPHA1
    assert all(v.result == "pass" for v in record.verification if v.required)
    assert record.source.head_sha == HEAD
    assert record.proposal.proposal_digest == "sha256:" + "d" * 64


def test_a_push_with_no_nameable_head_is_never_sealed_complete(tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    record = ri.completion_from_result(ref, ri.ImplementResult(ref=ref, pr_url=PR_URL), _obs(head_sha="not-a-sha"))
    assert (record.outcome, record.stop_reason.code) == ("harness_failure", "unexpected_error")


def test_implement_mode_arms_without_a_sentinel_prefix(tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    cases: list[tuple[ri.ImplementResult, tuple[str, str]]] = [
        (
            ri.ImplementResult(ref=ref, pr_url=None, error="implementer produced no commits"),
            ("deterministic_failure", "no_commits"),
        ),
        (
            ri.ImplementResult(ref=ref, pr_url=None, error="chart MAJOR-version bump detected without ack"),
            ("deterministic_failure", "policy_gate"),
        ),
        (
            ri.ImplementResult(ref=ref, pr_url=None, error="stale", stale_source=("source-resolved", "o/r#1")),
            ("deterministic_failure", "stale_source"),
        ),
        (
            ri.ImplementResult(ref=ref, pr_url=None, error="x", rate_limited=True),
            ("budget_exhausted", "rate_limited"),
        ),
        (
            ri.ImplementResult(ref=ref, pr_url=None, skipped_reason=f"{ri.POLICY_UNDECIDED_ERROR_PREFIX} down"),
            ("harness_failure", "policy_undecided"),
        ),
        (
            ri.ImplementResult(ref=ref, pr_url=None, skipped_reason=f"{ri.CLAIM_REFUSED_ERROR_PREFIX} held"),
            ("harness_failure", "claim_refused"),
        ),
        (
            ri.ImplementResult(ref=ref, pr_url=None, error="shell step failed: git push\nstdout: x"),
            ("harness_failure", "unexpected_error"),
        ),
    ]
    for result, expected in cases:
        outcome, stop, _ = ri._classify_result(result, remaining=False)
        assert (outcome, stop) == expected, result


def test_remaining_work_on_a_successful_push_is_incomplete(tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    obs = _obs(remaining=(("t2", "wire the flag"), ("t3", "add the test")))
    record = ri.completion_from_result(ref, ri.ImplementResult(ref=ref, pr_url=PR_URL), obs)
    assert (record.outcome, record.stop_reason.code) == ("incomplete", "known_work_remains")
    assert [r.item_id for r in record.remaining_work] == ["t2", "t3"]


def test_the_error_detail_is_one_bounded_line(tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    error = "shell step failed: git push\nstdout: " + "x" * 5000 + "\nstderr: ghp_" + "A" * 30
    record = ri.completion_from_result(
        ref, ri.ImplementResult(ref=ref, pr_url=None, error=error), _obs(pushed=False, head_sha=None)
    )
    assert record.stop_reason.detail == "shell step failed: git push"
    assert "ghp_" not in record.to_json()


# ---------------------------------------------------------------------------
# Driving `_implement_one`
# ---------------------------------------------------------------------------
def _reach_the_sdk(monkeypatch: Any, tmp_path: Path, *, on_run: Any, commits: bool, label: str) -> None:
    monkeypatch.setattr(ri, "_preflight_existing_result", lambda _ref: ri.ExistingResult(action="none"))
    monkeypatch.setattr(ri, "read_source_issue", lambda *_a, **_kw: OPEN)
    monkeypatch.setattr(ri, "ensure_auth_for_sdk", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_acquire_claim", lambda *_a, **_kw: None)
    clone = tmp_path / f"clone-{label}"
    clone.mkdir(exist_ok=True)
    monkeypatch.setattr(ri, "_clone_target", lambda *_a, **_kw: clone)
    monkeypatch.setattr(ri, "_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(ri.anyio, "run", on_run)
    monkeypatch.setattr(ri, "_has_new_commits", lambda *_a, **_kw: commits)
    monkeypatch.setattr(ri, "_detect_chart_major_bumps", lambda *_a, **_kw: [])
    monkeypatch.setattr(ri, "_push_and_open_pr", lambda *_a, **_kw: PR_URL)
    monkeypatch.setattr(ri, "_capture_head_sha", lambda *_a, **_kw: HEAD)


def _no_op(*_a: Any, **_kw: Any) -> None:
    return None


def _boom(*_a: Any, **_kw: Any) -> None:
    raise RuntimeError("boom")


def _rate_limited(*_a: Any, **_kw: Any) -> None:
    raise RateLimitExhaustedError("quota window exhausted")


# scenario name -> (on_run, commits, expected (outcome, stop_reason))
_SCENARIOS: dict[str, tuple[Any, bool, tuple[str, str]]] = {
    "no_commits": (_no_op, False, ("deterministic_failure", "no_commits")),
    "harness": (_boom, False, ("harness_failure", "unexpected_error")),
    "rate_limited": (_rate_limited, False, ("budget_exhausted", "rate_limited")),
    "complete": (_no_op, True, ("complete", "gates_passed")),
}


def _run_step(
    monkeypatch: Any, tmp_path: Path, *, label: str, scenario: str, fallback: bool, uid: str | None
) -> ri.ProposalRef:
    """One CWFT step: a FRESH gitops clone (the fallback cannot see the
    primary's writes), then `_implement_one` under that step's environment."""
    on_run, commits, _expected = _SCENARIOS[scenario]
    monkeypatch.setenv("WORKFLOW_NAME", "mctl-agents-implement-abc12")
    monkeypatch.setenv("IS_FALLBACK", "true" if fallback else "false")
    monkeypatch.setenv("HOSTNAME", f"pod-{label}")
    if uid is None:
        monkeypatch.delenv("WORKFLOW_UID", raising=False)
    else:
        monkeypatch.setenv("WORKFLOW_UID", uid)
    step_dir = tmp_path / label
    step_dir.mkdir()
    ref = _ref(step_dir)
    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run, commits=commits, label=label)
    ri._implement_one(ref, dry_run=False)
    return ref


def _status(ref: ri.ProposalRef) -> dict[str, Any]:
    return yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))


def _stage_prior_state(monkeypatch: Any, tmp_path: Path, primary: ri.ProposalRef) -> Path:
    prior = tmp_path / "prior-state"
    target = prior / primary.service / "proposals" / primary.slug
    target.mkdir(parents=True)
    (target / ".status.yaml").write_text(primary.status_path.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(ri, "PRIOR_STATE_DIR", prior)
    return prior


def test_the_success_path_projects_a_bounded_completion(monkeypatch: Any, tmp_path: Path) -> None:
    ref = _run_step(monkeypatch, tmp_path, label="a", scenario="complete", fallback=False, uid=None)
    status = _status(ref)
    assert status["status"] == "implemented"
    assert status["pr"] == PR_URL
    block = status["completion"]
    assert block["api_version"] == "completion.mctl.ai/v1alpha1"
    assert block["run_id"] == "mctl-agents-implement-abc12"
    assert block["primary_observed"] is True
    auth = block["authoritative"]
    assert (auth["step"], auth["outcome"], auth["stop_reason"]) == ("primary", "complete", "gates_passed")
    assert auth["head_sha"] == HEAD
    assert auth["verification"] == {"required_passed": 4, "required_total": 4, "optional_total": 1}
    # Bounded: no per-check details, no telemetry.
    assert set(auth) == {
        "completion_id", "content_hash", "attempt_id", "step", "outcome", "stop_reason", "head_sha", "verification",
    }
    assert "other" not in block


def test_a_failed_attempt_projects_beside_its_existing_status(monkeypatch: Any, tmp_path: Path) -> None:
    ref = _run_step(monkeypatch, tmp_path, label="a", scenario="no_commits", fallback=False, uid=None)
    status = _status(ref)
    assert status["status"] == "needs-triage"
    assert status["failure"]["code"] == "no-commits"
    assert status["completion"]["authoritative"]["stop_reason"] == "no_commits"
    ref_rl = _run_step(monkeypatch, tmp_path, label="b", scenario="rate_limited", fallback=False, uid=None)
    status_rl = _status(ref_rl)
    assert status_rl["status"] == "accepted"
    assert "attempt" not in status_rl
    assert status_rl["completion"]["authoritative"]["outcome"] == "budget_exhausted"


@pytest.mark.parametrize("uid", [None, "wf-uid-1"])
def test_t3a_primary_fails_then_fallback_fails_keeps_the_primary(
    monkeypatch: Any, tmp_path: Path, uid: str | None
) -> None:
    primary = _run_step(monkeypatch, tmp_path, label="p", scenario="no_commits", fallback=False, uid=uid)
    _stage_prior_state(monkeypatch, tmp_path, primary)
    for scenario, expected in (("harness", "harness_failure"), ("rate_limited", "budget_exhausted")):
        fallback = _run_step(monkeypatch, tmp_path, label=f"f-{scenario}", scenario=scenario, fallback=True, uid=uid)
        block = _status(fallback)["completion"]
        assert block["authoritative"]["step"] == "primary"
        assert block["authoritative"]["outcome"] == "deterministic_failure"
        assert block["authoritative"]["stop_reason"] == "no_commits"
        assert block["other"]["relation"] == "fallback"
        assert block["other"]["step"] == "fallback"
        assert block["other"]["outcome"] == expected
        assert block["primary_observed"] is True
        # The status TRANSITION is the fallback's own, unchanged behaviour.
        assert _status(fallback)["status"] == ("needs-triage" if scenario == "harness" else "accepted")


@pytest.mark.parametrize("uid", [None, "wf-uid-1"])
def test_t3b_primary_fails_then_fallback_succeeds_supersedes_the_primary(
    monkeypatch: Any, tmp_path: Path, uid: str | None
) -> None:
    primary = _run_step(monkeypatch, tmp_path, label="p", scenario="harness", fallback=False, uid=uid)
    _stage_prior_state(monkeypatch, tmp_path, primary)
    fallback = _run_step(monkeypatch, tmp_path, label="f", scenario="complete", fallback=True, uid=uid)
    block = _status(fallback)["completion"]
    assert block["authoritative"]["step"] == "fallback"
    assert block["authoritative"]["outcome"] == "complete"
    assert block["other"]["relation"] == "superseded"
    assert block["other"]["step"] == "primary"
    assert block["other"]["outcome"] == "harness_failure"
    assert block["primary_observed"] is True
    # Distinct ids even when WORKFLOW_UID makes the attempt ids coincide.
    assert block["authoritative"]["completion_id"] != block["other"]["completion_id"]
    if uid is not None:
        assert block["authoritative"]["attempt_id"] == block["other"]["attempt_id"]
    else:
        assert block["authoritative"]["attempt_id"] != block["other"]["attempt_id"]


@pytest.mark.parametrize("uid", [None, "wf-uid-1"])
def test_t3c_a_fallback_with_no_prior_state_speaks_alone(monkeypatch: Any, tmp_path: Path, uid: str | None) -> None:
    monkeypatch.setattr(ri, "PRIOR_STATE_DIR", tmp_path / "does-not-exist")
    fallback = _run_step(monkeypatch, tmp_path, label="f", scenario="harness", fallback=True, uid=uid)
    block = _status(fallback)["completion"]
    assert block["primary_observed"] is False
    assert block["authoritative"]["step"] == "fallback"
    assert "other" not in block


def test_t3d_prior_state_from_another_run_is_treated_as_absent(monkeypatch: Any, tmp_path: Path) -> None:
    primary = _run_step(monkeypatch, tmp_path, label="p", scenario="no_commits", fallback=False, uid=None)
    _stage_prior_state(monkeypatch, tmp_path, primary)
    monkeypatch.setenv("WORKFLOW_NAME", "some-other-workflow")
    fallback = _run_step_without_env_reset(monkeypatch, tmp_path, label="f", scenario="harness")
    block = _status(fallback)["completion"]
    assert block["primary_observed"] is False
    assert block["authoritative"]["step"] == "fallback"


def _run_step_without_env_reset(monkeypatch: Any, tmp_path: Path, *, label: str, scenario: str) -> ri.ProposalRef:
    on_run, commits, _ = _SCENARIOS[scenario]
    monkeypatch.setenv("IS_FALLBACK", "true")
    monkeypatch.setenv("HOSTNAME", f"pod-{label}")
    monkeypatch.delenv("WORKFLOW_UID", raising=False)
    step_dir = tmp_path / label
    step_dir.mkdir()
    ref = _ref(step_dir)
    _reach_the_sdk(monkeypatch, tmp_path, on_run=on_run, commits=commits, label=label)
    ri._implement_one(ref, dry_run=False)
    return ref


def test_a_prior_handoff_that_is_not_the_primary_is_absent(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    monkeypatch.setenv("IS_FALLBACK", "true")
    ref = _ref(tmp_path)
    forged = ri.completion_from_result(
        ref, ri.ImplementResult(ref=ref, pr_url=None, error="RuntimeError: boom"), _obs(pushed=False, head_sha=None)
    )
    assert forged.attempt.step == "fallback"
    prior = tmp_path / "prior"
    d = prior / ref.service / "proposals" / ref.slug
    d.mkdir(parents=True)
    # A fallback summary dressed up as the primary's handoff: step != primary.
    (d / ".status.yaml").write_text(
        yaml.safe_dump({"completion": ic.project_primary(forged).to_dict()}), encoding="utf-8"
    )
    assert ri._load_prior_handoff(prior, ref, "wf-1") is None


def test_an_oversize_or_garbled_prior_handoff_is_absent(monkeypatch: Any, tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    prior = tmp_path / "prior"
    d = prior / ref.service / "proposals" / ref.slug
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text("completion: [not, a, mapping", encoding="utf-8")
    assert ri._load_prior_handoff(prior, ref, "wf") is None
    (d / ".status.yaml").write_text("x: " + "y" * (ri.MAX_PRIOR_HANDOFF_BYTES + 10), encoding="utf-8")
    assert ri._load_prior_handoff(prior, ref, "wf") is None
    assert ri._load_prior_handoff(tmp_path / "missing", ref, "wf") is None


# ---------------------------------------------------------------------------
# T4: replay and staleness
# ---------------------------------------------------------------------------
def _completion(
    ref: ri.ProposalRef, error: str | None, *, attempt_id: str = "attempt-1"
) -> ic.ImplementationCompletion:
    result = ri.ImplementResult(ref=ref, pr_url=None if error else PR_URL, error=error)
    obs = _obs(attempt_id=attempt_id, **({"pushed": False, "head_sha": None} if error else {}))
    return ri.completion_from_result(ref, result, obs)


def test_resealing_the_same_step_yields_the_same_id_and_reprojecting_is_a_noop(
    monkeypatch: Any, tmp_path: Path
) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    ref = _ref(tmp_path)
    first = _completion(ref, f"{ri.RATE_LIMITED_ERROR_PREFIX} window")
    second = _completion(ref, f"{ri.RATE_LIMITED_ERROR_PREFIX} window")
    assert first.completion_id == second.completion_id
    assert ri._project_completion(ref, first) is True
    before = ref.status_path.read_bytes()
    assert ri._project_completion(ref, second) is False
    assert ref.status_path.read_bytes() == before


@pytest.mark.parametrize(
    "first_error",
    [f"{ri.VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX} denied=1", "RuntimeError: boom"],
)
def test_a_non_complete_record_is_never_replaced_by_complete(
    monkeypatch: Any, tmp_path: Path, first_error: str
) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    ref = _ref(tmp_path)
    failed = _completion(ref, first_error)
    assert ri._project_completion(ref, failed) is True
    complete = _completion(ref, None)
    assert complete.outcome == "complete"
    assert ri._project_completion(ref, complete) is False
    held = _status(ref)["completion"]["authoritative"]
    assert held["completion_id"] == failed.completion_id
    assert held["outcome"] != "complete"


def test_a_record_for_another_run_may_replace_the_held_one(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    ref = _ref(tmp_path)
    failed = _completion(ref, "RuntimeError: boom", attempt_id="attempt-1")
    assert ri._project_completion(ref, failed) is True
    held = _status(ref)["completion"]
    same_key_complete = _completion(ref, None, attempt_id="attempt-1")
    assert ri._completion_write_allowed(held, same_key_complete) is False
    monkeypatch.setenv("WORKFLOW_NAME", "wf-2")
    assert ri._completion_write_allowed(held, _completion(ref, None, attempt_id="attempt-1")) is True
    assert ri._completion_write_allowed({"junk": True}, same_key_complete) is True
    assert ri._completion_write_allowed(None, same_key_complete) is True


def test_an_older_attempt_is_refused_when_status_holds_a_newer_one(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    ref = _ref(tmp_path)
    ref.status_path.write_text(
        yaml.safe_dump({"status": "in-progress", "attempt": {"id": "newer"}}), encoding="utf-8"
    )
    before = ref.status_path.read_bytes()
    assert ri._project_completion(ref, _completion(ref, "RuntimeError: boom", attempt_id="older")) is False
    assert ref.status_path.read_bytes() == before
    assert "completion" not in _status(ref)


def test_a_stale_claim_at_emission_is_reported_but_not_projected(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    ref = _ref(tmp_path)
    ctx = SimpleNamespace(claim_id="claim-1", owner_epoch=0)

    def stale(_ctx: Any, **_kw: Any) -> None:
        raise ri.ImplementerFenced(f"{ri.FENCED_ERROR_PREFIX} epoch moved")

    monkeypatch.setattr(ri, "_check_claim_or_raise", stale)
    before = ref.status_path.read_bytes()
    result = ri.ImplementResult(ref=ref, pr_url=PR_URL)
    completion = ri._finish_attempt(ref, result, _obs(claim_ctx=ctx), project=True)
    assert completion is not None
    assert (completion.outcome, completion.stop_reason.code) == ("harness_failure", "fenced")
    assert ref.status_path.read_bytes() == before, "no projection on a stale claim"
    assert result.completion is completion
    out = tmp_path / "completion.json"
    ri._write_completion_out(out, result.completion)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["outcome"] == "harness_failure"
    assert written["stop_reason"]["code"] == "fenced"
    assert ic.ImplementationCompletion.from_dict(written) == completion


def test_a_released_claim_is_not_a_stale_claim(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    ref = _ref(tmp_path)

    def gone(_ctx: Any, **_kw: Any) -> None:
        raise AssertionError("a released claim must not be re-checked")

    monkeypatch.setattr(ri, "_check_claim_or_raise", gone)

    class _Client:
        def release(self, *_a: Any, **_kw: Any) -> None:
            return None

    ctx = ri._ClaimContext(
        client=_Client(), claim_id="claim-9", entity=None, phase="implement",  # type: ignore[arg-type]
        owner_epoch=0, entity_version="", executor=None, attempt="a",  # type: ignore[arg-type]
    )
    ri._release_claim(ctx, reason="done")
    completion = ri._finish_attempt(ref, ri.ImplementResult(ref=ref, pr_url=PR_URL), _obs(claim_ctx=ctx), project=True)
    assert completion is not None
    assert completion.outcome == "complete"


def test_emission_errors_are_swallowed_and_never_change_the_result(monkeypatch: Any, tmp_path: Path) -> None:
    ref = _ref(tmp_path)

    def explode(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("seal failed")

    monkeypatch.setattr(ri, "completion_from_result", explode)
    result = ri.ImplementResult(ref=ref, pr_url=PR_URL)
    assert ri._finish_attempt(ref, result, _obs(), project=True) is None
    assert result.completion is None
    assert result.pr_url == PR_URL


def test_an_exception_past_the_attempt_arms_still_emits_then_propagates(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "wf-1")
    ref = _ref(tmp_path)
    seen: list[ic.ImplementationCompletion | None] = []
    real = ri._finish_attempt

    def spy(*a: Any, **kw: Any) -> Any:
        seen.append(real(*a, **kw))
        return seen[-1]

    monkeypatch.setattr(ri, "_finish_attempt", spy)

    def escape() -> ri.ImplementResult:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ri._complete_attempt(ref, _obs(pushed=False, head_sha=None), escape, project=True)
    assert seen and seen[0] is not None
    assert (seen[0].outcome, seen[0].stop_reason.code) == ("harness_failure", "unexpected_error")


# ---------------------------------------------------------------------------
# Pre-start exits emit nothing
# ---------------------------------------------------------------------------
def test_pre_claim_exits_emit_no_completion(monkeypatch: Any, tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    ref.approval_ok = False
    monkeypatch.setattr(ri, "_mark_blocked", lambda *_a, **_kw: True)
    called: list[int] = []
    monkeypatch.setattr(ri, "_complete_attempt", lambda *_a, **_kw: called.append(1))
    result = ri._implement_one(ref, dry_run=False)
    assert result.blocked and result.completion is None
    assert not called
    assert "completion" not in _status(ref)


# ---------------------------------------------------------------------------
# Review-feedback mode: hand back on the result, never touch `.status.yaml`
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "init")
    return root


def _stub_review(monkeypatch: Any, repo: Path, *, commits: bool) -> None:
    monkeypatch.setattr(ri, "_clone_target", lambda *_a, **_kw: repo)
    monkeypatch.setattr(ri, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(ri, "_checkout_existing_branch", lambda *_a: None)
    monkeypatch.setattr(ri, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(ri, "_capture_head_sha", lambda *_a, **_kw: HEAD)
    monkeypatch.setattr(ri, "_has_new_commits", lambda *_a, **_kw: commits)
    monkeypatch.setattr(ri.anyio, "run", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_push_followup", lambda *_a, **_kw: None)
    monkeypatch.setattr(ri, "_acquire_claim", lambda *_a, **_kw: None)


def _review_ref(repo: Path) -> ri.ProposalRef:
    return ri.ProposalRef(service="mctl-web", slug="test-slug", proposal_dir=repo, status="implemented")


def test_review_feedback_success_hands_back_a_complete_record(monkeypatch: Any, repo: Path) -> None:
    monkeypatch.setenv("WORKFLOW_NAME", "shepherd-wf")
    _stub_review(monkeypatch, repo, commits=True)
    monkeypatch.setattr(ri, "_load_status", lambda *_a: {"pr": "https://pr"})
    result = ri.review_feedback_one(_review_ref(repo), {"summaries": []})
    assert result.error is None
    completion = result.completion
    assert completion is not None
    assert (completion.outcome, completion.attempt.phase, completion.attempt.step) == (
        "complete", "review-remediation", "primary",
    )
    assert completion.attempt.run_id == "shepherd-wf"
    assert completion.source.base_sha == HEAD
    assert completion.proposal.review_bundle_digest is not None
    assert not (repo / ".status.yaml").exists(), "review mode never projects into .status.yaml"


def test_review_feedback_no_commits_and_refusal_arms(monkeypatch: Any, repo: Path) -> None:
    _stub_review(monkeypatch, repo, commits=False)
    result = ri.review_feedback_one(_review_ref(repo), {"summaries": []})
    assert result.error == "implementer produced no follow-up commits"
    assert result.completion is not None
    assert (result.completion.outcome, result.completion.stop_reason.code) == (
        "deterministic_failure", "no_commits",
    )
    (repo / ri.REFUSAL_MARKER_FILENAME).write_text(
        json.dumps({"refused": True, "reason": "already addressed"}), encoding="utf-8"
    )
    refused = ri.review_feedback_one(_review_ref(repo), {"summaries": []})
    assert refused.completion is not None
    assert (refused.completion.outcome, refused.completion.stop_reason.code) == (
        "bounded_refusal", "deliberate_no_op",
    )


def test_review_feedback_branch_missing_is_deterministic(monkeypatch: Any, repo: Path) -> None:
    _stub_review(monkeypatch, repo, commits=False)
    monkeypatch.setattr(ri, "_branch_exists_on_origin", lambda *_a: False)
    result = ri.review_feedback_one(_review_ref(repo), {"summaries": []})
    assert result.completion is not None
    assert result.completion.stop_reason.code == "branch_missing"


# ---------------------------------------------------------------------------
# Task 4: the refusal marker's optional `remaining` list
# ---------------------------------------------------------------------------
def _write_marker(repo: Path, payload: Any) -> None:
    (repo / ri.REFUSAL_MARKER_FILENAME).write_text(json.dumps(payload), encoding="utf-8")


def test_a_marker_without_remaining_behaves_as_before(repo: Path) -> None:
    _write_marker(repo, {"refused": True, "reason": "already addressed"})
    marker = ri._read_refusal_marker(repo)
    assert marker is not None
    assert marker.remaining == ()
    assert marker == ri.RefusalMarker(reason="already addressed")


def test_remaining_is_parsed_bounded_and_filtered(repo: Path) -> None:
    items = [{"item_id": f"t{i}", "summary": "s" * 400} for i in range(40)]
    items += [{"item_id": "bad id!", "summary": "x"}, {"item_id": "t99"}, "junk"]
    _write_marker(repo, {"refused": True, "reason": "partial", "remaining": items})
    marker = ri._read_refusal_marker(repo)
    assert marker is not None
    assert len(marker.remaining) == ic.MAX_LIST_ITEMS
    assert all(len(summary) <= ic.MAX_SUMMARY_CHARS for _id, summary in marker.remaining)
    _write_marker(repo, {"refused": True, "reason": "partial", "remaining": "not a list"})
    assert ri._read_refusal_marker(repo).remaining == ()  # type: ignore[union-attr]


def test_remaining_on_a_successful_push_yields_incomplete(monkeypatch: Any, repo: Path) -> None:
    _stub_review(monkeypatch, repo, commits=True)
    monkeypatch.setattr(ri, "_load_status", lambda *_a: {"pr": "https://pr"})
    _write_marker(
        repo,
        {"refused": True, "reason": "partial", "remaining": [{"item_id": "t3", "summary": "add the test"}]},
    )
    result = ri.review_feedback_one(_review_ref(repo), {"summaries": []})
    # The push still stands: nothing about the exit code or result changes.
    assert result.error is None
    assert result.completion is not None
    assert (result.completion.outcome, result.completion.stop_reason.code) == ("incomplete", "known_work_remains")
    assert [(r.item_id, r.summary) for r in result.completion.remaining_work] == [("t3", "add the test")]


def test_a_marker_without_remaining_does_not_make_a_push_incomplete(monkeypatch: Any, repo: Path) -> None:
    _stub_review(monkeypatch, repo, commits=True)
    monkeypatch.setattr(ri, "_load_status", lambda *_a: {"pr": "https://pr"})
    _write_marker(repo, {"refused": True, "reason": "should be ignored"})
    result = ri.review_feedback_one(_review_ref(repo), {"summaries": []})
    assert result.completion is not None
    assert result.completion.outcome == "complete"


# ---------------------------------------------------------------------------
# T7: backward compatibility
# ---------------------------------------------------------------------------
def test_a_status_without_a_completion_block_still_parses(tmp_path: Path) -> None:
    from orchestrator.proposal_state import load_status

    ref = _ref(tmp_path)
    assert "completion" not in load_status(ref.status_path)
    assert ri._completion_write_allowed(load_status(ref.status_path).get("completion"), _completion(ref, None))


def test_completion_out_flag_is_wired_and_best_effort(monkeypatch: Any, tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    completion = _completion(ref, None)
    out = tmp_path / "nested" / "missing-dir" / "c.json"
    ri._write_completion_out(out, completion)  # unwritable path: a warning, not a failure
    ri._write_completion_out(tmp_path / "none.json", None)
    assert not (tmp_path / "none.json").exists()
    good = tmp_path / "c.json"
    ri._write_completion_out(good, completion)
    assert ic.ImplementationCompletion.from_dict(json.loads(good.read_text(encoding="utf-8"))) == completion
    monkeypatch.setattr(ri, "MAX_COMPLETION_OUT_BYTES", 10)
    small = tmp_path / "small.json"
    ri._write_completion_out(small, completion)
    assert not small.exists()


def test_the_cli_accepts_completion_out_and_writes_the_record(monkeypatch: Any, tmp_path: Path) -> None:
    ref = _ref(tmp_path)
    completion = _completion(ref, None)
    bundle = tmp_path / "feedback.json"
    bundle.write_text("{}", encoding="utf-8")
    out = tmp_path / "completion.json"
    monkeypatch.setattr("sys.argv", [
        "run_implementer.py", "--service", "mctl-telegram", "--slug", "issue-510-x",
        "--state-dir", str(tmp_path), "--review-feedback", str(bundle), "--completion-out", str(out),
    ])
    monkeypatch.setattr(ri, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(ri, "_load_review_feedback", lambda _p: {})
    monkeypatch.setattr(ri, "find_accepted_proposals", lambda *_a, **_kw: [ref])
    monkeypatch.setattr(
        ri, "review_feedback_one",
        lambda *_a, **_kw: ri.ImplementResult(ref=ref, pr_url=PR_URL, completion=completion),
    )
    ri.main()
    assert json.loads(out.read_text(encoding="utf-8"))["completion_id"] == completion.completion_id
