"""The execution-evidence producer (mctlhq/mctl-agents#544, ADR 018).

Covers, with a fake HTTP transport and no network:

- the bytes posted are exactly the sealed envelope's canonical bytes, and
  Tier A re-derives the same identity from them;
- sealing is deterministic (created_at never moves the identity) and a
  retry or replay re-sends identical bytes;
- every delivery failure (4xx, 5xx, timeout, transport error, a build
  error) is logged and counted, never raised, and never changes the run's
  own result or exit code;
- an unset token posts nothing;
- a source that could not be observed is an `observation_failed` gap and
  makes the envelope INCOMPLETE, never an empty block;
- the Amendment 2 blocks appear only behind MCTL_EVIDENCE_AMENDMENT_2;
- each call point (investigator, implementer, shepherd) posts one envelope.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from orchestrator import evidence_producer as ep
from orchestrator import execution_evidence as ee
from orchestrator import policy_checkpoint as pc
from orchestrator.context_snapshot import canonical_json, hash_bytes

RUNTIME_ID = "ex-0123456789abcdef"
WORK_ID = "we_01J9ZEVIDENCE"
SHA = "a" * 40
TOKEN = "evidence-writer-test-token"
ENV = {ep.TOKEN_ENV: TOKEN, ep.BASE_URL_ENV: "https://api.example.test"}
CREATED_AT = "2026-10-05T10:00:00.000000Z"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ep._reset_for_tests()
    for name in (ep.TOKEN_ENV, ep.AMENDMENT_2_ENV, ep.BASE_URL_ENV, "MCTL_EXECUTION_CONTEXT_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ep, "RETRY_DELAYS_SECONDS", (0.0, 0.0))
    yield
    ep._reset_for_tests()


class FakePost:
    """Answers with the queued responses (or raises queued exceptions) and
    records every call."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        self.calls.append((url, body, headers))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return httpx.Response(answer, json={"code": "evidence_invalid"} if answer >= 400 else {"evidence": {}})

    def envelope(self, index: int = -1) -> dict[str, Any]:
        raw = base64.b64decode(self.calls[index][1]["envelope_b64"])
        return json.loads(raw)

    def raw(self, index: int = -1) -> bytes:
        return base64.b64decode(self.calls[index][1]["envelope_b64"])


def _run(stage: str = ep.STAGE_IMPLEMENTER) -> ep.RunEvidence:
    evidence = ep.RunEvidence(stage)
    evidence.note_runtime_context(SimpleNamespace(
        context_id=RUNTIME_ID, trace_id="b" * 32, assertions=SimpleNamespace(asserted_by="control-plane"),
    ))
    return evidence


def _decision(evidence: ep.RunEvidence, code: str = "allowed", *, approval_ref: str = "") -> str:
    request = pc.ActionRequest(
        action_kind=pc.GITHUB_PR_MERGE, operation="merge", target="mctlhq/mctl-agents#1",
        args_digest=pc.args_digest_of({"n": 1}), execution_id=RUNTIME_ID,
    )
    digest = request.action_digest()
    verdict = pc.ALLOW if code == "allowed" else pc.REQUIRE_APPROVAL
    evidence.record_decision(request, pc.Decision(verdict, code, "r", "v1", "rule-1", digest, approval_ref))
    return digest


# ---------------------------------------------------------------------------
# Bytes, identity, determinism
# ---------------------------------------------------------------------------
def test_posted_bytes_are_exactly_the_sealed_canonical_bytes():
    evidence = _run()
    _decision(evidence)
    evidence.set_outcome("succeeded", "pr-opened")
    post = FakePost(201)
    assert ep.produce(evidence, environ=ENV, post=post, created_at=CREATED_AT) == ep.CREATED

    url, body, headers = post.calls[0]
    assert url == "https://api.example.test/api/v1/evidence/records"
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    assert set(body) == {"envelope_b64"}
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert post.raw() == canonical_json(sealed.to_dict())
    parsed = ee.ExecutionEvidence.from_dict(post.envelope())
    assert ee.recompute_content_hash(parsed) == parsed.content_hash == sealed.content_hash
    assert parsed.evidence_id == "ev-" + parsed.content_hash[7:23]
    assert parsed.execution.runtime_execution_id == RUNTIME_ID
    assert parsed.execution.trace_id == "b" * 32
    assert parsed.outcome == ee.Outcome("succeeded", "pr-opened")


def test_seal_is_deterministic_and_created_at_never_moves_the_identity():
    evidence = _run()
    _decision(evidence)
    evidence.set_outcome("succeeded")
    first = ep.build(evidence, amendment_2=False, created_at="2026-10-05T10:00:00Z")
    second = ep.build(evidence, amendment_2=False, created_at="2026-10-06T11:12:13.5Z")
    assert first.evidence_id == second.evidence_id
    assert first.content_hash == second.content_hash
    third = ep.build(evidence, amendment_2=False, created_at="2026-10-05T10:00:00Z")
    assert ep.envelope_bytes(first) == ep.envelope_bytes(third)


def test_a_retry_after_a_server_error_resends_identical_bytes_and_a_replay_is_success():
    evidence = _run()
    evidence.set_outcome("succeeded")
    post = FakePost(503, 200)
    assert ep.produce(evidence, environ=ENV, post=post, created_at=CREATED_AT) == ep.REPLAYED
    assert len(post.calls) == 2
    assert post.raw(0) == post.raw(1)
    assert ep.stats() == {ep.REPLAYED: 1}


def test_a_lost_answer_then_a_replay_counts_once():
    evidence = _run()
    evidence.set_outcome("succeeded")
    post = FakePost(httpx.ReadTimeout("lost"), 200)
    assert ep.produce(evidence, environ=ENV, post=post, created_at=CREATED_AT) == ep.REPLAYED
    assert post.raw(0) == post.raw(1)


# ---------------------------------------------------------------------------
# Never fatal
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("answers", "expected", "attempts"),
    [
        ((400,), ep.REFUSED, 1),
        ((401,), ep.REFUSED, 1),
        ((409,), ep.REFUSED, 1),
        ((500,), ep.UNDELIVERED, ep.ATTEMPTS),
        ((429,), ep.UNDELIVERED, ep.ATTEMPTS),
        ((httpx.ConnectError("down"),), ep.UNDELIVERED, ep.ATTEMPTS),
        ((httpx.ReadTimeout("slow"),), ep.UNDELIVERED, ep.ATTEMPTS),
        ((RuntimeError("broken transport"),), ep.UNDELIVERED, 1),
    ],
)
def test_delivery_failures_are_counted_and_never_raised(answers, expected, attempts, caplog):
    evidence = _run()
    evidence.set_outcome("succeeded")
    post = FakePost(*answers)
    assert ep.produce(evidence, environ=ENV, post=post, created_at=CREATED_AT) == expected
    assert len(post.calls) == attempts
    assert ep.stats() == {expected: 1}
    assert any("execution evidence" in r.getMessage() for r in caplog.records)


def test_a_build_failure_is_counted_and_never_raised(monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("cannot build")

    monkeypatch.setattr(ep, "build", boom)
    evidence = _run()
    post = FakePost(201)
    assert ep.produce(evidence, environ=ENV, post=post) == ep.BUILD_FAILED
    assert post.calls == []
    assert ep.stats() == {ep.BUILD_FAILED: 1}


@pytest.mark.parametrize("answer", [400, 500, httpx.ReadTimeout("slow")])
def test_run_returns_the_runs_own_result_whatever_delivery_does(answer):
    post = FakePost(answer)
    with ep.run(ep.STAGE_IMPLEMENTER, environ=ENV, post=post) as evidence:
        evidence.set_outcome("succeeded")
        result = "the run's own result"
    assert result == "the run's own result"
    assert post.calls


def test_an_escaping_exception_is_recorded_as_failed_and_reraised_unchanged():
    post = FakePost(201)
    with pytest.raises(KeyError, match="original"), ep.run(ep.STAGE_SHEPHERD, environ=ENV, post=post) as evidence:
        evidence.note_runtime_context(SimpleNamespace(context_id=RUNTIME_ID, trace_id="", assertions=None))
        raise KeyError("original")
    assert post.envelope()["outcome"] == {"code": "failed", "reason_code": "unhandled-exception"}


def test_a_nonzero_system_exit_is_failed_and_still_exits():
    post = FakePost(201)
    with pytest.raises(SystemExit) as exc_info, ep.run(ep.STAGE_INVESTIGATOR, environ=ENV, post=post) as evidence:
        evidence.note_runtime_context(SimpleNamespace(context_id=RUNTIME_ID, trace_id="", assertions=None))
        sys.exit(2)
    assert exc_info.value.code == 2
    assert post.envelope()["outcome"] == {"code": "failed", "reason_code": "system-exit"}


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------
def test_an_unset_token_posts_nothing_builds_nothing_and_logs_once(monkeypatch, caplog):
    built: list[Any] = []
    monkeypatch.setattr(ep, "build", lambda *a, **k: built.append(a))
    post = FakePost(201)
    for _ in range(3):
        assert ep.produce(_run(), environ={}, post=post) == ep.DISABLED
    assert post.calls == [] and built == []
    assert sum("execution evidence is off" in r.getMessage() for r in caplog.records) == 1
    assert ep.stats() == {ep.DISABLED: 3}


def test_a_non_https_base_url_never_receives_the_token():
    post = FakePost(201)
    env = {ep.TOKEN_ENV: TOKEN, ep.BASE_URL_ENV: "http://api.example.test"}
    assert ep.produce(_run(), environ=env, post=post) == ep.DISABLED
    assert post.calls == []


def test_a_discarded_run_posts_nothing():
    post = FakePost(201)
    with ep.run(ep.STAGE_SHEPHERD, environ=ENV, post=post) as evidence:
        evidence.discard()
    assert post.calls == []


def test_the_sdk_session_env_never_carries_the_evidence_token(monkeypatch):
    from claude_agent_sdk import ClaudeAgentOptions

    from orchestrator import options

    monkeypatch.setenv(ep.TOKEN_ENV, TOKEN)
    scrubbed = options._scrubbed(ClaudeAgentOptions())
    assert scrubbed.env[ep.TOKEN_ENV] == ""


# ---------------------------------------------------------------------------
# Unknown is not absence
# ---------------------------------------------------------------------------
def _gaps(envelope: ee.ExecutionEvidence) -> set[tuple[str, str, bool]]:
    return {(g.block, g.code, g.required) for g in envelope.gaps}


def test_a_complete_envelope_when_every_expected_source_was_observed():
    evidence = _run()
    _decision(evidence)
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert sealed.completeness == ee.COMPLETE
    assert len(sealed.policy_decisions) == 1


def test_an_unreadable_execution_request_is_an_observation_failed_gap():
    evidence = _run(ep.STAGE_INVESTIGATOR)
    evidence.note_work_execution(WORK_ID, "wi_1")
    evidence.expect_execution_request("xr_01REQ")
    evidence.set_outcome("succeeded")

    def failing_read(_wi, _xr):
        raise httpx.ConnectError("store down")

    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT, read_request=failing_read)
    assert sealed.execution_request is None
    assert ("execution_request", "observation_failed", True) in _gaps(sealed)
    assert sealed.completeness == ee.INCOMPLETE


def test_a_request_that_reads_back_found_is_the_reference():
    evidence = _run(ep.STAGE_INVESTIGATOR)
    evidence.note_work_execution(WORK_ID, "wi_1")
    evidence.expect_execution_request("xr_01REQ")
    evidence.set_outcome("succeeded")
    answer = SimpleNamespace(
        verdict="request-found",
        request=SimpleNamespace(request_id="xr_01REQ", work_item_id="wi_1", kind="resume", state="claimed"),
    )
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT, read_request=lambda *_: answer)
    assert sealed.execution_request == ee.ExecutionRequestRef("xr_01REQ", "resume", "claimed")
    assert sealed.execution.execution_id == WORK_ID
    assert not any(g.block == "execution_request" for g in sealed.gaps)


def test_a_request_for_another_work_item_is_not_this_runs_reference():
    evidence = _run(ep.STAGE_INVESTIGATOR)
    evidence.note_work_execution(WORK_ID, "wi_1")
    evidence.expect_execution_request("xr_01REQ")
    evidence.set_outcome("succeeded")
    answer = SimpleNamespace(
        verdict="request-found",
        request=SimpleNamespace(request_id="xr_01REQ", work_item_id="wi_OTHER", kind="resume", state="claimed"),
    )
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT, read_request=lambda *_: answer)
    assert sealed.execution_request is None
    assert ("execution_request", "observation_failed", True) in _gaps(sealed)


def test_no_execution_identity_is_an_unknown_not_a_crash():
    evidence = ep.RunEvidence(ep.STAGE_IMPLEMENTER)
    evidence.set_outcome("refused", "skipped")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert ("execution", "observation_failed", True) in _gaps(sealed)
    assert sealed.completeness == ee.INCOMPLETE


def test_usage_seen_but_not_recorded_is_a_store_unavailable_gap():
    evidence = _run()
    evidence.note_usage("sess-1", ["claude-opus"], recorded=False)
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert sealed.usage is None
    assert ("usage", "store_unavailable", True) in _gaps(sealed)


def test_usage_recorded_is_a_join_key_reference():
    evidence = _run()
    evidence.note_usage("sess-1", ["claude-opus", "claude-haiku"], recorded=True)
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert sealed.usage == ee.UsageRef(session_id="sess-1", model_key="claude-opus", devloop_stage="implementer")


def test_artifacts_are_hashed_and_a_missing_one_is_not_produced(tmp_path):
    (tmp_path / "requirements.md").write_text("req")
    evidence = _run(ep.STAGE_INVESTIGATOR)
    evidence.note_artifact_file(tmp_path / "requirements.md", "proposal")
    evidence.note_artifact_file(tmp_path / "design.md", "proposal")
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert sealed.artifacts == (ee.ArtifactRef("requirements.md", "proposal", hash_bytes(b"req")),)
    assert ("artifacts", "not_produced", True) in _gaps(sealed)


def test_no_policy_decision_is_observed_absent_not_unknown():
    evidence = _run()
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert sealed.policy_decisions == ()
    assert ("policy_decisions", "not_applicable", False) in _gaps(sealed)
    assert sealed.completeness == ee.COMPLETE


def test_an_approval_decision_links_its_receipt_and_an_unknown_lookup_is_a_gap():
    evidence = _run()
    digest = _decision(evidence, "approved", approval_ref="aar_01APPROVED")
    _decision(evidence, "approval_lookup_error", approval_ref="aar_01UNKNOWN")
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert sealed.approvals == (ee.ApprovalRef("aar_01APPROVED", digest, "consumed"),)
    assert sealed.policy_decisions[0].approval_ref == "aar_01APPROVED"
    assert ("approvals", "observation_failed", True) in _gaps(sealed)


def test_a_malformed_reference_degrades_to_explicit_gaps_instead_of_losing_the_envelope():
    evidence = _run()
    _decision(evidence)
    evidence.note_usage("sess-1", ["claude-opus"], recorded=True)
    evidence.set_outcome("succeeded")
    evidence.artifacts.append(ee.ArtifactRef(name="../escape", kind="file", content_hash="sha256:" + "0" * 64))
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert sealed.artifacts == ()
    assert ("artifacts", "observation_failed", True) in _gaps(sealed)
    assert sealed.execution.runtime_execution_id == RUNTIME_ID
    # Only the block that failed is lost (claude P3 on #577).
    assert len(sealed.policy_decisions) == 1 and sealed.usage is not None
    assert not any(g.block in ("policy_decisions", "usage") and g.code == "observation_failed" for g in sealed.gaps)
    assert ep.stats() == {ep.DEGRADED: 1}


# ---------------------------------------------------------------------------
# The decision sink
# ---------------------------------------------------------------------------
def test_policy_checkpoint_decisions_are_captured_only_inside_a_run():
    request = pc.ActionRequest(pc.GITHUB_PR_COMMENT, "comment", "t", pc.args_digest_of({}))
    pc.decide(request)  # outside every run: collected nowhere
    post = FakePost(201)
    with ep.run(ep.STAGE_SHEPHERD, environ=ENV, post=post) as evidence:
        evidence.note_runtime_context(SimpleNamespace(context_id=RUNTIME_ID, trace_id="", assertions=None))
        pc.decide(request)
        evidence.set_outcome("succeeded", "merge")
    assert len(evidence.decisions) == 1
    assert post.envelope()["policy_decisions"][0]["action_digest"] == request.action_digest()


def test_enforce_reports_whether_the_side_effect_finished():
    request = pc.ActionRequest(pc.GITHUB_PR_COMMENT, "comment", "t", pc.args_digest_of({}))
    allow = pc.Policy("test", (pc.Rule("allow-all", pc.GITHUB_PR_COMMENT, "*", pc.ALLOW),))

    def failing() -> None:
        raise OSError("push failed")

    with ep.run(ep.STAGE_SHEPHERD, environ={}) as evidence:
        assert pc.enforce(request, lambda: "done", policy=allow) == "done"
        assert evidence.tool_results == {request.action_digest(): "succeeded"}
        with pytest.raises(OSError):
            pc.enforce(request, failing, policy=allow)
        assert evidence.tool_results == {request.action_digest(): "failed"}


# ---------------------------------------------------------------------------
# Amendment 2 behind the flag
# ---------------------------------------------------------------------------
AMENDMENT_2_KEYS = {"versions", "subject", "tool_calls", "provenance"}


def _amendment_run() -> ep.RunEvidence:
    evidence = _run(ep.STAGE_SHEPHERD)
    _decision(evidence)
    evidence.note_subject_pr("mctlhq/mctl-agents", 524, SHA)
    evidence.set_outcome("succeeded", "merge")
    return evidence


def test_flag_off_posts_the_pre_amendment_shape():
    post = FakePost(201)
    ep.produce(_amendment_run(), environ=ENV, post=post, created_at=CREATED_AT)
    envelope = post.envelope()
    assert not AMENDMENT_2_KEYS & set(envelope)
    assert not {g["block"] for g in envelope["gaps"]} & AMENDMENT_2_KEYS


def test_flag_on_posts_subject_provenance_and_tool_calls():
    post = FakePost(201)
    ep.produce(_amendment_run(), environ={**ENV, ep.AMENDMENT_2_ENV: "true"}, post=post, created_at=CREATED_AT)
    envelope = post.envelope()
    assert envelope["subject"] == {"kind": "pull_request", "repository": "mctlhq/mctl-agents", "ref": "524",
                                   "revision": SHA}
    assert envelope["provenance"]["authority"] == "observed"
    assert envelope["tool_calls"][0]["kind"] == pc.GITHUB_PR_MERGE
    assert envelope["tool_calls"][0]["status"] == "unknown"
    parsed = ee.ExecutionEvidence.from_dict(envelope)
    assert ee.recompute_content_hash(parsed) == parsed.content_hash


def test_flag_on_a_pr_head_that_cannot_be_read_is_a_subject_gap():
    evidence = _run()
    evidence.note_subject_pr_url("https://github.com/mctlhq/mctl-agents/pull/7")
    evidence.set_outcome("succeeded", "pr-opened")

    def unreadable(_repo, _number):
        raise RuntimeError("gh failed")

    sealed = ep.build(evidence, amendment_2=True, created_at=CREATED_AT, read_pr_head=unreadable)
    assert sealed.subject is None and sealed.provenance is None
    assert ("subject", "observation_failed", True) in _gaps(sealed)
    assert sealed.completeness == ee.INCOMPLETE


def test_flag_on_a_pr_head_read_at_seal_time_binds_the_subject():
    evidence = _run()
    evidence.note_subject_pr_url("https://github.com/mctlhq/mctl-agents/pull/7")
    evidence.set_outcome("succeeded", "pr-opened")
    sealed = ep.build(evidence, amendment_2=True, created_at=CREATED_AT, read_pr_head=lambda *_: SHA)
    assert sealed.subject == ee.SubjectRef("pull_request", "mctlhq/mctl-agents", "7", SHA)


def test_flag_on_versions_come_from_the_snapshot_correlation():
    evidence = _run(ep.STAGE_INVESTIGATOR)
    evidence.note_versions(SimpleNamespace(
        agent="issue-investigator", environment="production", definition_version="1.2.0",
        definition_content_hash="sha256:" + "c" * 64, profile_version="", profile_content_hash="",
        release_revision=3,
    ))
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=True, created_at=CREATED_AT)
    assert sealed.versions is not None and sealed.versions.release_revision == 3


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_the_flag_defaults_off(value):
    assert not ep.amendment_2_enabled({ep.AMENDMENT_2_ENV: value})
    assert ep.amendment_2_enabled({ep.AMENDMENT_2_ENV: "true"})


# ---------------------------------------------------------------------------
# Call points
# ---------------------------------------------------------------------------
@pytest.fixture
def live_post(monkeypatch):
    """A token in the environment and a fake transport for code paths that
    call `run()` without options (the runners)."""
    post = FakePost(201)
    monkeypatch.setenv(ep.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(ep, "_default_post", post)
    return post


def test_investigator_call_point_posts_the_typed_outcome_and_the_triplet(monkeypatch, tmp_path, live_post):
    from orchestrator import run_issue_investigator as rii

    proposal_dir = tmp_path / "mctl-agents" / "proposals" / "issue-1"
    proposal_dir.mkdir(parents=True)
    for name in rii.TRIPLET:
        (proposal_dir / name).write_text(name)

    def fake_investigate(issue_url, state_dir, dry_run, **_kwargs):
        ep.note("note_runtime_context", SimpleNamespace(context_id=RUNTIME_ID, trace_id="", assertions=None))
        ep.note("note_work_execution", WORK_ID, "wi_1")
        return rii.InvestigateResult("mctl-agents", "issue-1", proposal_dir)

    monkeypatch.setattr(rii, "_investigate", fake_investigate)
    result = rii.investigate("https://github.com/mctlhq/mctl-agents/issues/1", tmp_path)
    assert result.outcome_code == "succeeded"
    envelope = live_post.envelope()
    assert envelope["execution"]["execution_id"] == WORK_ID
    assert envelope["execution"]["runtime_execution_id"] == RUNTIME_ID
    assert envelope["outcome"]["code"] == "succeeded"
    assert sorted(a["name"] for a in envelope["artifacts"]) == sorted(rii.TRIPLET)


def test_investigator_error_without_a_typed_outcome_is_failed(monkeypatch, tmp_path, live_post):
    from orchestrator import run_issue_investigator as rii

    monkeypatch.setattr(
        rii, "_investigate",
        lambda *a, **k: rii.InvestigateResult("mctl-agents", "issue-1", tmp_path, error="boom"),
    )
    rii.investigate("https://github.com/mctlhq/mctl-agents/issues/1", tmp_path)
    assert live_post.envelope()["outcome"] == {"code": "failed", "reason_code": "investigation-error"}


def test_investigator_dry_run_posts_nothing(monkeypatch, tmp_path, live_post):
    from orchestrator import run_issue_investigator as rii

    monkeypatch.setattr(
        rii, "_investigate",
        lambda *a, **k: rii.InvestigateResult("mctl-agents", "x", tmp_path, skipped_reason="dry-run",
                                              outcome_code="refused", outcome_reason="dry-run"),
    )
    rii.investigate("https://github.com/mctlhq/mctl-agents/issues/1", tmp_path, dry_run=True)
    assert live_post.calls == []


def _implementer_ref(tmp_path: Path):
    from orchestrator import run_implementer as ri

    return ri.ProposalRef(service="mctl-agents", slug="issue-1", proposal_dir=tmp_path, status="accepted")


@pytest.mark.parametrize(
    ("result_kwargs", "outcome"),
    [
        ({"pr_url": "https://github.com/mctlhq/mctl-agents/pull/9"}, {"code": "succeeded", "reason_code": "pr-opened"}),
        ({"pr_url": None, "error": "deliberate no-op: nothing to do"},
         {"code": "refused", "reason_code": "deliberate-no-op"}),
        ({"pr_url": None, "error": "push failed"}, {"code": "failed", "reason_code": "implementer-error"}),
        ({"pr_url": None, "blocked": "approval-missing", "skipped_reason": "x"},
         {"code": "refused", "reason_code": "blocked-approval-missing"}),
        ({"pr_url": None, "error": "rate limited: x", "rate_limited": True},
         {"code": "failed", "reason_code": "rate-limited"}),
    ],
)
def test_implementer_call_point_maps_the_result(monkeypatch, tmp_path, live_post, result_kwargs, outcome):
    from orchestrator import run_implementer as ri

    ref = _implementer_ref(tmp_path)

    def fake(ref_, dry_run=False):
        ep.note("note_runtime_context", SimpleNamespace(context_id=RUNTIME_ID, trace_id="", assertions=None))
        return ri.ImplementResult(ref=ref_, **result_kwargs)

    monkeypatch.setattr(ri, "_implement_one", fake)
    result = ri.implement_one(ref)
    assert result.ref is ref
    assert live_post.envelope()["outcome"] == outcome


def test_review_feedback_call_point_posts_one_envelope(monkeypatch, tmp_path, live_post):
    from orchestrator import run_implementer as ri

    monkeypatch.setattr(
        ri, "_review_feedback_one",
        lambda ref, bundle, dry_run=False, branch=None: ri.ImplementResult(
            ref=ref, pr_url="https://github.com/mctlhq/mctl-agents/pull/9"),
    )
    ri.review_feedback_one(_implementer_ref(tmp_path), {})
    assert len(live_post.calls) == 1
    assert live_post.envelope()["outcome"] == {"code": "succeeded", "reason_code": "review-addressed"}


@pytest.mark.parametrize("answer", [500, httpx.ConnectError("down"), RuntimeError("transport bug")])
@pytest.mark.parametrize(
    ("pr_url", "error", "exit_code"),
    [("https://github.com/mctlhq/mctl-agents/pull/9", None, None), (None, "push failed", 1)],
)
def test_implementer_exit_code_is_unchanged_by_a_failing_producer(
    monkeypatch, tmp_path, answer, pr_url, error, exit_code
):
    from orchestrator import run_implementer as ri

    post = FakePost(answer)
    monkeypatch.setenv(ep.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(ep, "_default_post", post)
    ref = _implementer_ref(tmp_path)
    monkeypatch.setattr(ri, "find_accepted_proposals", lambda *a, **k: [ref])
    monkeypatch.setattr(
        ri, "_implement_one",
        lambda ref_, dry_run=False: ri.ImplementResult(ref=ref_, pr_url=pr_url, error=error),
    )
    monkeypatch.setattr(sys, "argv", ["run_implementer", "--state-dir", str(tmp_path)])
    if exit_code is None:
        ri.main()
    else:
        with pytest.raises(SystemExit) as exc_info:
            ri.main()
        assert exc_info.value.code == exit_code
    assert post.calls, "the producer ran"
    assert sum(ep.stats().values()) == 1


def test_shepherd_call_point_posts_an_acting_tick_and_skips_an_idle_one(monkeypatch, tmp_path, live_post):
    from orchestrator import run_shepherd as rs
    from orchestrator.execution_identity import mint_local

    ref = rs.ProposalRef(service="mctl-agents", slug="issue-1", proposal_dir=tmp_path, status="implemented")
    context = mint_local(executor_type="shepherd", workflow_type="review-fix", agent="shepherd")

    monkeypatch.setattr(rs, "process_one", lambda *a, **k: rs.ShepherdResult(ref=ref, decision="wait"))
    rs._process_one_with_evidence(ref, state_dir=tmp_path, execution_context=context)
    assert live_post.calls == []

    def merging(*_a, **_k):
        ep.note("note_subject_pr", "mctlhq/mctl-agents", 9, SHA)
        pc.decide(pc.ActionRequest(pc.GITHUB_PR_MERGE, "merge", "t", pc.args_digest_of({})))
        return rs.ShepherdResult(ref=ref, decision="merge")

    monkeypatch.setattr(rs, "process_one", merging)
    result = rs._process_one_with_evidence(ref, state_dir=tmp_path, execution_context=context)
    assert result.decision == "merge"
    envelope = live_post.envelope()
    assert envelope["outcome"] == {"code": "succeeded", "reason_code": "merge"}
    assert envelope["execution"]["runtime_execution_id"] == context.context_id
    assert len(envelope["policy_decisions"]) == 1


def test_shepherd_error_is_posted_even_while_waiting(monkeypatch, tmp_path, live_post):
    from orchestrator import run_shepherd as rs
    from orchestrator.execution_identity import mint_local

    ref = rs.ProposalRef(service="mctl-agents", slug="issue-1", proposal_dir=tmp_path, status="implemented")
    monkeypatch.setattr(
        rs, "process_one",
        lambda *a, **k: rs.ShepherdResult(ref=ref, decision="wait", error="could not fetch PR snapshot"),
    )
    rs._process_one_with_evidence(
        ref, state_dir=tmp_path,
        execution_context=mint_local(executor_type="shepherd", workflow_type="review-fix", agent="shepherd"),
    )
    assert live_post.envelope()["outcome"] == {"code": "failed", "reason_code": "shepherd-error"}


def test_usage_ledger_hands_the_session_to_the_active_run():
    from orchestrator import usage_ledger

    recorder = usage_ledger.UsageRecorder("implementer", token="", submit=lambda job: None)
    message = type("ResultMessage", (), {"session_id": "sess-9", "model_usage": {"claude-opus": {}}})()
    with ep.run(ep.STAGE_IMPLEMENTER, environ={}) as evidence:
        recorder.observe(message)
    assert evidence.usage is None and evidence.usage_unrecorded


def test_two_proposals_on_one_shepherd_tick_post_two_distinct_evidence_ids(monkeypatch, tmp_path, live_post):
    """Claude P2 on #577: one tick shares one ex- id across proposals, so
    without a per-proposal record two proposals with the same outcome would
    seal byte-identical envelopes and collapse into one Tier B row."""
    from orchestrator import run_shepherd as rs
    from orchestrator.execution_identity import mint_local

    context = mint_local(executor_type="shepherd", workflow_type="review-fix", agent="shepherd")
    refs = []
    for name in ("issue-1", "issue-2"):
        proposal_dir = tmp_path / name
        proposal_dir.mkdir()
        (proposal_dir / ".status.yaml").write_text("status: implemented\n")
        refs.append(rs.ProposalRef(service="mctl-agents", slug=name, proposal_dir=proposal_dir, status="implemented"))
    for ref in refs:
        monkeypatch.setattr(
            rs, "process_one",
            lambda *a, _ref=ref, **k: rs.ShepherdResult(ref=_ref, decision="wait", error="could not fetch PR snapshot"),
        )
        rs._process_one_with_evidence(ref, state_dir=tmp_path, execution_context=context)
    envelopes = [live_post.envelope(i) for i in range(len(live_post.calls))]
    assert len(envelopes) == 2
    assert envelopes[0]["evidence_id"] != envelopes[1]["evidence_id"]
    assert [e["artifacts"][0]["name"] for e in envelopes] == ["mctl-agents.issue-1", "mctl-agents.issue-2"]


def test_identical_policy_decisions_are_deduplicated_and_the_list_is_capped():
    """Claude P2 on #577: a run that checkpoints the same call repeatedly
    must not grow an envelope past what mctl-api accepts."""
    evidence = _run()
    for _ in range(1000):
        _decision(evidence)
    evidence.set_outcome("succeeded")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert len(sealed.policy_decisions) == 1
    assert sealed.completeness == ee.COMPLETE

    many = _run()
    for index in range(ep.MAX_POLICY_DECISIONS + 5):
        request = pc.ActionRequest(pc.MCP_TOOL_CALL, f"tool-{index}", "t", pc.args_digest_of({}))
        many.record_decision(request, pc.Decision(pc.ALLOW, "allowed", "r", "v1", "rule", request.action_digest()))
    many.set_outcome("succeeded")
    capped = ep.build(many, amendment_2=False, created_at=CREATED_AT)
    assert len(capped.policy_decisions) == ep.MAX_POLICY_DECISIONS
    # The newest are kept: the last decision of the run survives the cap.
    newest = many.decisions[-1].action_digest
    assert capped.policy_decisions[-1].action_digest == newest
    assert ("policy_decisions", "observation_failed", True) in _gaps(capped)
    assert len(ep.envelope_bytes(capped)) < 256 * 1024


def test_the_implementer_subject_is_the_commit_it_pushed_not_the_head_at_seal_time():
    """Claude P2 on #577: a head that moved after the push (a follow-up
    commit during verification) must not become this run's subject."""
    evidence = _run()
    evidence.note_pushed_head(SHA)
    evidence.note_subject_pr_url("https://github.com/mctlhq/mctl-agents/pull/7")
    evidence.set_outcome("succeeded", "pr-opened")

    def moved_head(_repo, _number):
        raise AssertionError("the head must not be re-read when the run pushed one")

    sealed = ep.build(evidence, amendment_2=True, created_at=CREATED_AT, read_pr_head=moved_head)
    assert sealed.subject == ee.SubjectRef("pull_request", "mctlhq/mctl-agents", "7", SHA)


def test_note_pushed_head_reads_the_local_clone_whatever_the_flag(tmp_path):
    import subprocess as sp

    from orchestrator import run_implementer as ri

    sp.run(["git", "init", "-q", str(tmp_path)], check=True)
    sp.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "x"], check=True)
    head = sp.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    with ep.run(ep.STAGE_IMPLEMENTER, environ={}) as evidence:
        ri._note_pushed_head(tmp_path / "missing")  # never fatal
        assert evidence.pushed_head_sha == ""
        ri._note_pushed_head(tmp_path)
        assert evidence.pushed_head_sha == head


def test_retry_after_is_honoured_and_clamped():
    delays: list[float] = []

    class Limited:
        calls = 0

        def __call__(self, url, body, headers):
            Limited.calls += 1
            if Limited.calls == 1:
                return httpx.Response(429, headers={"Retry-After": "1.5"})
            if Limited.calls == 2:
                return httpx.Response(503, headers={"Retry-After": "120"})
            return httpx.Response(201, json={})

    result = ep.deliver(b"{}", url="https://x.test", token=TOKEN, post=Limited(), sleep=delays.append)
    assert result[0] == ep.CREATED
    assert delays == [1.5, ep.MAX_RETRY_AFTER_SECONDS]


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-5", "soon"])
def test_a_hostile_retry_after_falls_back_to_the_default_backoff(value):
    """Claude P3 on #577: NaN must never reach time.sleep, and 0 or a
    negative value must not remove the backoff."""
    resp = httpx.Response(429, headers={"Retry-After": value})
    delay = ep._retry_after(resp, default=1.0)
    assert delay == 1.0


def test_a_failing_result_mapper_never_reaches_the_run(monkeypatch, tmp_path, live_post):
    from orchestrator import run_implementer as ri

    def broken(*_a, **_k):
        raise AttributeError("result shape changed")

    monkeypatch.setattr(ri, "_note_implement_evidence", broken)
    monkeypatch.setattr(ri, "_implement_one", lambda ref_, dry_run=False: ri.ImplementResult(ref=ref_, pr_url="u"))
    result = ri.implement_one(_implementer_ref(tmp_path))
    assert result.pr_url == "u"
    assert live_post.envelope()["outcome"] == {"code": "failed", "reason_code": "no-outcome-recorded"}


def test_two_proposals_in_one_implementer_batch_post_two_distinct_evidence_ids(monkeypatch, tmp_path, live_post):
    """Claude P3 on #577: an implementer batch shares one ex- id too, so the
    proposal-status record must tell its envelopes apart."""
    from orchestrator import run_implementer as ri

    refs = []
    for name in ("issue-1", "issue-2"):
        proposal_dir = tmp_path / name
        proposal_dir.mkdir()
        (proposal_dir / ".status.yaml").write_text("status: accepted\n")
        refs.append(ri.ProposalRef(service="mctl-agents", slug=name, proposal_dir=proposal_dir, status="accepted"))

    def failing(ref_, dry_run=False):
        ep.note("note_runtime_context", SimpleNamespace(context_id=RUNTIME_ID, trace_id="", assertions=None))
        return ri.ImplementResult(ref=ref_, pr_url=None, error="push failed")

    monkeypatch.setattr(ri, "_implement_one", failing)
    for ref in refs:
        ri.implement_one(ref)
    ids = {live_post.envelope(i)["evidence_id"] for i in range(len(live_post.calls))}
    assert len(live_post.calls) == 2 and len(ids) == 2


def test_a_hostile_proposal_name_is_sanitised_not_a_degraded_seal(tmp_path):
    """Claude P3 on #577: the per-proposal record must not be lost to a
    name Tier A rejects."""
    status = tmp_path / ".status.yaml"
    status.write_text("status: implemented\n")
    evidence = _run(ep.STAGE_SHEPHERD)
    evidence.note_proposal_status("mctl-agents", "../Odd Slug", status)
    evidence.set_outcome("succeeded", "merge")
    sealed = ep.build(evidence, amendment_2=False, created_at=CREATED_AT)
    assert len(sealed.artifacts) == 1
    assert ep.stats() == {}


def test_a_head_pushed_after_the_pr_was_named_still_wins_over_a_read():
    """agy P3 on #577: ordering of note_subject_pr_url and note_pushed_head
    must not matter."""
    evidence = _run()
    evidence.note_subject_pr_url("https://github.com/mctlhq/mctl-agents/pull/7")
    evidence.note_pushed_head(SHA)
    evidence.set_outcome("succeeded", "pr-opened")

    def unexpected(_repo, _number):
        raise AssertionError("must not read the head")

    sealed = ep.build(evidence, amendment_2=True, created_at=CREATED_AT, read_pr_head=unexpected)
    assert sealed.subject == ee.SubjectRef("pull_request", "mctlhq/mctl-agents", "7", SHA)


def test_shepherd_process_one_binds_the_subject_to_the_head_it_read(monkeypatch, tmp_path):
    """agy P3 on #577: the real process_one, not a stub, feeds the PR head
    it read into the run's evidence."""
    from orchestrator import run_shepherd as rs

    ref = rs.ProposalRef(service="mctl-agents", slug="issue-1", proposal_dir=tmp_path, status="in-progress")
    snapshot = rs.PRSnapshot(
        number=9, repo="mctlhq/mctl-agents", state="OPEN", merged=False, closed_unmerged=False,
        merge_commit=None, close_comment_or_default="", head_sha=SHA, head_pushed_at=None,
        merge_state_status="CLEAN", checks_green=True, is_draft=False,
    )
    monkeypatch.setattr(rs, "find_pr_for_proposal", lambda *a, **k: snapshot)
    monkeypatch.setattr(rs, "_attempt_is_fresh", lambda *_a: True)
    with ep.run(ep.STAGE_SHEPHERD, environ={}) as evidence:
        result = rs.process_one(ref, state_dir=tmp_path)
    assert result.decision == "wait"
    assert evidence.subject == ee.SubjectRef("pull_request", "mctlhq/mctl-agents", "9", SHA)
