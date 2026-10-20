"""The durable decision-record producer (mctlhq/mctl-api#701,
orchestrator/decision_records.py).

With a fake mctl-api and no network, covers:

- success: an allowed decision, one attempt, `started` then `succeeded`;
- an approved decision carries its `aar_` and the exact binding fields the
  approval store holds (`action_approvals.intent_for`);
- refusal: the decision is recorded (with the `aar_` it names) and no
  attempt is ever opened;
- retry: each run is a new attempt, numbered by the server;
- partial failure, then recovery on a second attempt;
- idempotent replay: a retried write re-sends the same key and a 200 counts
  as recorded;
- 503 is `unobservable`, 429 `rate_limited`, other 4xx `refused`: each
  counted and logged, and the attempts of an unrecorded decision are
  dropped, never sent against a guessed id;
- never fatal: the decision, the side effect's result or exception, and the
  MCP hook's answer (approval receipt included) are unchanged;
- off by default; digests and references only, never a payload.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from orchestrator import action_approvals as aa
from orchestrator import decision_records as dr
from orchestrator import evidence_producer as ep
from orchestrator import options
from orchestrator import policy_checkpoint as pc

EXECUTION_ID = "ex-0123456789abcdef"
ACTOR = "human:mashkovd"
SECRET_ARG = "very-secret-argument-value"
TARGET = "mctlhq/mctl-api#701-target-text"
TOKEN = "decision-writer-test-token"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(dr.ENABLE_ENV, raising=False)
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.delenv("MCTL_POLICY_APPROVALS", raising=False)
    dr._install_for_tests(None)
    ep._reset_for_tests()
    yield
    dr._install_for_tests(None)


class FakeApi:
    """mctl-api's two decision routes. `script` maps a write kind
    (`decision`, `started`, `succeeded`, ...) to the statuses to answer
    before falling back to success."""

    def __init__(self, **script: list[int]) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self.attempts: dict[str, int] = {}
        self.decision_id = "dec_" + "0" * 31 + "1"
        self.retry_after = ""
        self.decision_answer: dict[str, Any] | None = None

    def kind(self, url: str, body: dict[str, Any]) -> str:
        return "decision" if url.endswith(dr.DECISIONS_PATH) else body["phase"]

    def __call__(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        self.calls.append((url, body, headers))
        kind = self.kind(url, body)
        queued = self.script.get(kind) or []
        request = httpx.Request("POST", url)
        if queued:
            status = queued.pop(0)
            if status not in (200, 201):
                hdrs = {"Retry-After": self.retry_after} if self.retry_after else {}
                return httpx.Response(status, json={"code": f"code_{status}"}, headers=hdrs, request=request)
        else:
            status = 201
        if kind == "decision":
            answer = self.decision_answer
            if answer is None:
                answer = {"decision": {"id": self.decision_id}}
            return httpx.Response(status, json=answer, request=request)
        if kind == "started":
            if status == 201:
                self.attempts[url] = self.attempts.get(url, 0) + 1
            return httpx.Response(status, json={"event": {"attempt_no": self.attempts.get(url, 1)}}, request=request)
        return httpx.Response(status, json={"event": {"attempt_no": body["attempt_no"]}}, request=request)

    def bodies(self, kind: str) -> list[dict[str, Any]]:
        return [b for u, b, _ in self.calls if self.kind(u, b) == kind]


@pytest.fixture
def api():
    fake = FakeApi()
    rec = dr.DecisionRecorder(token=TOKEN, base_url="https://api.example.test", post=fake,
                              sleep=lambda _s: None, start_thread=False)
    dr._install_for_tests(rec)
    fake.rec = rec
    return fake


def _request(**overrides: Any) -> pc.ActionRequest:
    fields: dict[str, Any] = {
        "action_kind": pc.GITHUB_ISSUE_COMMENT,
        "operation": "comment",
        "target": TARGET,
        "args_digest": pc.args_digest_of({"body": SECRET_ARG}),
        "execution_id": EXECUTION_ID,
        "trace_id": "0" * 31 + "a",
        "actor": ACTOR,
    }
    fields.update(overrides)
    return pc.ActionRequest(**fields)


APPROVAL_POLICY = pc.Policy("test/policy/v1", (
    pc.Rule("needs-approval", pc.GITHUB_ISSUE_COMMENT, "comment", pc.REQUIRE_APPROVAL),
))


class Lookup:
    def __init__(self, status: str, ref: str = "aar_01JAPPROVAL") -> None:
        self.status, self.ref = status, ref

    def redeem(self, request, *, rule_id, policy_version, approval_ref=""):
        return pc.ApprovalOutcome(self.status, approval_ref=self.ref, decided_by="mashkovd")


def _drain(api: FakeApi) -> None:
    api.rec.run_pending()


# ---------------------------------------------------------------------------
# success / approval / refusal
# ---------------------------------------------------------------------------
def test_allowed_decision_records_one_attempt_started_then_succeeded(api):
    result = pc.enforce(_request(), lambda: "done")
    _drain(api)
    assert result == "done"
    (decision,) = api.bodies("decision")
    request = _request()
    assert decision == {
        "execution_id": EXECUTION_ID,
        "policy_version": pc.BUILTIN_POLICY.version,
        "rule_id": "github-issue-comment",
        "verdict": "allow",
        "code": "allowed",
        "actor": ACTOR,
        "action_kind": "github.issue.comment:comment",
        "operation": "comment",
        "target_digest": dr.target_digest(TARGET),
        "args_digest": request.args_digest,
        "action_digest": request.action_digest(),
        "decided_at": decision["decided_at"],
        "idempotency_key": decision["idempotency_key"],
        "trace_id": request.trace_id,
    }
    (started,) = api.bodies("started")
    assert "attempt_no" not in started
    assert started["runtime_execution_id"] == EXECUTION_ID
    (succeeded,) = api.bodies("succeeded")
    assert succeeded["attempt_no"] == 1
    assert "error_code" not in succeeded
    attempts_url = f"https://api.example.test/api/v1/decisions/{api.decision_id}/attempts"
    assert [u for u, _b, _h in api.calls[1:]] == [attempts_url] * 2
    assert api.calls[0][2] == {"Authorization": f"Bearer {TOKEN}"}
    assert api.rec.stats() == {dr.CREATED: 3}


def test_approved_decision_names_its_aar_and_the_exact_binding(api):
    request = _request()
    decision = pc.decide(request, policy=APPROVAL_POLICY, approvals=Lookup(pc.APPROVAL_GRANTED))
    _drain(api)
    assert decision.code == pc.CODE_APPROVED
    (body,) = api.bodies("decision")
    intent = aa.intent_for(request, rule_id="needs-approval", policy_version="test/policy/v1")
    assert body["approval_id"] == "aar_01JAPPROVAL"
    assert body["verdict"] == "require_approval"
    # mctl-api refuses an approved decision unless these equal the aar_.
    assert body["execution_id"] == intent.execution_id
    assert body["action_kind"] == intent.action_kind
    assert body["args_digest"] == intent.args_digest
    assert body["rule_id"] == intent.policy_rule_id
    assert body["policy_version"] == intent.policy_version
    assert body["target_digest"] == dr.target_digest(intent.target)
    assert body["action_digest"] == intent.artifact_hash


def test_refusal_is_recorded_with_its_aar_and_never_opens_an_attempt(api):
    with pytest.raises(pc.PolicyRefused):
        pc.enforce(_request(), lambda: pytest.fail("side effect ran"), policy=APPROVAL_POLICY,
                   approvals=Lookup(pc.APPROVAL_MISMATCH))
    _drain(api)
    (body,) = api.bodies("decision")
    assert body["code"] == "approval_intent_mismatch"
    assert body["verdict"] == "require_approval"
    assert body["approval_id"] == "aar_01JAPPROVAL"
    assert not api.bodies("started")


def test_a_lookup_error_refusal_names_its_aar(api):
    pc.decide(_request(), policy=APPROVAL_POLICY, approvals=Lookup(pc.APPROVAL_UNKNOWN))
    _drain(api)
    (body,) = api.bodies("decision")
    assert body["code"] == "approval_lookup_error"
    assert body["approval_id"] == "aar_01JAPPROVAL"


def test_only_approval_codes_carry_an_approval_id(api):
    decision = pc.Decision(pc.DENY, pc.CODE_DENIED, "no", "v1", "r", _request().action_digest(), "aar_stale")
    dr.record_decision(_request(), decision)
    _drain(api)
    assert "approval_id" not in api.bodies("decision")[0]


def test_starting_a_refused_decision_sends_nothing(api):
    decision = pc.decide(_request(), policy=APPROVAL_POLICY, approvals=Lookup(pc.APPROVAL_DENIED))
    attempt = dr.start(decision)
    attempt.succeeded()
    _drain(api)
    assert not api.bodies("started") and not api.bodies("succeeded")
    assert api.rec.stats()[dr.SKIPPED_NOT_PERMITTED] == 1


# ---------------------------------------------------------------------------
# retry, partial, recovery
# ---------------------------------------------------------------------------
def test_each_retry_is_a_new_server_numbered_attempt(api):
    decision = pc.decide(_request())
    with pytest.raises(OSError):
        with dr.start(decision):
            raise OSError("gh: connection reset by https://secret.example/token=abc")
    with dr.start(decision):
        pass
    _drain(api)
    started = api.bodies("started")
    assert [b["idempotency_key"] for b in started] == [
        f"{api.bodies('decision')[0]['idempotency_key']}:s1", f"{api.bodies('decision')[0]['idempotency_key']}:s2"]
    assert all("attempt_no" not in b for b in started)
    (failed,) = api.bodies("failed")
    assert failed["attempt_no"] == 1
    assert failed["error_code"] == "exception:oserror"
    (succeeded,) = api.bodies("succeeded")
    assert succeeded["attempt_no"] == 2


def test_partial_failure_is_partial_and_a_later_attempt_recovers(api):
    decision = pc.decide(_request())
    first = dr.start(decision)
    first.partial("push:branch-pushed-pr-not-opened")
    second = dr.start(decision)
    second.succeeded()
    _drain(api)
    (partial,) = api.bodies("partial")
    assert partial["attempt_no"] == 1
    assert partial["error_code"] == "push:branch-pushed-pr-not-opened"
    assert not [b for b in api.bodies("succeeded") if b["attempt_no"] == 1]
    (succeeded,) = api.bodies("succeeded")
    assert succeeded["attempt_no"] == 2


def test_a_partial_inside_the_context_manager_is_not_overwritten_by_success(api):
    decision = pc.decide(_request())
    with dr.start(decision) as attempt:
        attempt.partial("partial-apply")
    _drain(api)
    assert len(api.bodies("partial")) == 1
    assert not api.bodies("succeeded")
    assert api.rec.stats()[dr.DUPLICATE_TERMINAL] == 1


def test_a_second_terminal_call_is_ignored_and_counted(api):
    attempt = dr.start(pc.decide(_request()))
    attempt.failed("first")
    attempt.succeeded()
    _drain(api)
    assert len(api.bodies("failed")) == 1 and not api.bodies("succeeded")
    assert api.rec.stats()[dr.DUPLICATE_TERMINAL] == 1


# ---------------------------------------------------------------------------
# replay, 503, 429, refusal
# ---------------------------------------------------------------------------
def test_a_retried_write_replays_the_same_key_and_200_counts_as_recorded(api):
    api.script["decision"] = [503, 200]
    pc.enforce(_request(), lambda: None)
    _drain(api)
    first, second = api.bodies("decision")
    assert first == second
    assert api.bodies("succeeded")[0]["attempt_no"] == 1
    assert api.rec.stats() == {dr.REPLAYED: 1, dr.CREATED: 2}


def test_persistent_503_is_unobservable_and_drops_the_attempts(api, capsys):
    api.script["decision"] = [503, 503, 503]
    assert pc.enforce(_request(), lambda: "ran") == "ran"
    _drain(api)
    assert len(api.bodies("decision")) == dr.ATTEMPTS
    assert not api.bodies("started") and not api.bodies("succeeded")
    stats = api.rec.stats()
    assert stats[dr.UNOBSERVABLE] == 1
    assert stats[dr.DROPPED_DECISION_UNRECORDED] == 2
    assert dr.REFUSED not in stats and dr.CREATED not in stats
    lines = [json.loads(line.split(" ", 1)[1]) for line in capsys.readouterr().out.splitlines()
             if line.startswith(dr.LOG_PREFIX)]
    assert {"result": "unobservable", "phase": "decision", "http_status": 503, "detail": "code_503",
            "key": api.bodies("decision")[0]["idempotency_key"]} in lines
    assert any(line["result"] == "dropped_decision_unrecorded" and line["detail"] == "unobservable" for line in lines)


def test_429_is_retried_after_its_retry_after_then_counted_rate_limited(api, monkeypatch):
    slept: list[float] = []
    api.rec._sleep = slept.append
    api.retry_after = "1.5"
    api.script["decision"] = [429, 429, 429]
    pc.decide(_request())
    _drain(api)
    assert len(api.bodies("decision")) == dr.ATTEMPTS
    assert slept == [1.5, 2.0]
    assert api.rec.stats() == {dr.RATE_LIMITED: 1}


def test_429_then_success_is_recorded(api):
    api.script["started"] = [429]
    pc.enforce(_request(), lambda: None)
    _drain(api)
    assert len(api.bodies("started")) == 2
    assert api.bodies("succeeded")[0]["attempt_no"] == 1


def test_a_4xx_is_refused_once_and_never_retried(api):
    api.script["decision"] = [400]
    pc.enforce(_request(), lambda: None)
    _drain(api)
    assert len(api.bodies("decision")) == 1
    assert api.rec.stats()[dr.REFUSED] == 1
    assert api.rec.stats()[dr.DROPPED_DECISION_UNRECORDED] == 2


def test_a_started_that_did_not_land_drops_its_terminal(api):
    api.script["started"] = [503, 503, 503]
    pc.enforce(_request(), lambda: None)
    _drain(api)
    assert not api.bodies("succeeded")
    assert api.rec.stats()[dr.DROPPED_ATTEMPT_UNOPENED] == 1
    assert api.rec.stats()[dr.UNOBSERVABLE] == 1


def test_a_stored_decision_whose_id_cannot_be_read_is_not_recorded(api):
    api.decision_answer = {"decision": {"id": "not-a-decision-id"}}
    pc.enforce(_request(), lambda: None)
    _drain(api)
    assert not api.bodies("started")
    assert api.rec.stats()[dr.UNOBSERVABLE] == 1
    assert dr.CREATED not in api.rec.stats()


def test_a_started_answer_without_a_usable_attempt_no_drops_its_terminal(api, monkeypatch):
    real = api.__call__

    def zero_attempt(url, body, headers):
        resp = real(url, body, headers)
        if body.get("phase") == "started":
            return httpx.Response(201, json={"event": {"attempt_no": 0}}, request=resp.request)
        return resp
    api.rec._post = zero_attempt
    pc.enforce(_request(), lambda: None)
    _drain(api)
    assert not api.bodies("succeeded")
    assert api.rec.stats()[dr.UNOBSERVABLE] == 1
    assert api.rec.stats()[dr.DROPPED_ATTEMPT_UNOPENED] == 1


def test_enforce_records_a_raising_side_effect_as_failed_and_reraises_it(api):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        pc.enforce(_request(), lambda: (_ for _ in ()).throw(Boom(SECRET_ARG)))
    _drain(api)
    (failed,) = api.bodies("failed")
    assert failed["attempt_no"] == 1 and failed["error_code"] == "exception:boom"
    assert not api.bodies("succeeded")


def test_transport_errors_are_undelivered(api):
    def boom(url, body, headers):
        raise httpx.ConnectError("refused")
    api.rec._post = boom
    pc.decide(_request())
    _drain(api)
    assert api.rec.stats() == {dr.UNDELIVERED: 1}


# ---------------------------------------------------------------------------
# what is (not) recorded
# ---------------------------------------------------------------------------
def test_no_payload_target_or_reason_leaves_the_process(api):
    decision = pc.decide(_request(), policy=APPROVAL_POLICY, approvals=Lookup(pc.APPROVAL_GRANTED))
    with pytest.raises(RuntimeError):
        with dr.start(decision):
            raise RuntimeError(f"failed with {SECRET_ARG}")
    _drain(api)
    sent = json.dumps([b for _u, b, _h in api.calls])
    assert SECRET_ARG not in sent
    assert TARGET not in sent
    assert decision.reason not in sent
    assert "consumed" not in sent


def test_a_decision_without_an_execution_identity_is_skipped_not_sent(api):
    pc.enforce(_request(execution_id="", actor=""), lambda: None)
    _drain(api)
    assert api.calls == []
    assert api.rec.stats()[dr.SKIPPED_NO_EXECUTION] == 1


def test_an_undescribable_decision_is_skipped(api):
    pc.decide(_request(args_digest=""))
    _drain(api)
    assert api.calls == []
    assert api.rec.stats()[dr.SKIPPED_UNDESCRIBABLE] == 1


def test_the_work_item_comes_from_the_run_and_only_when_canonical(api):
    with ep.run(ep.STAGE_INVESTIGATOR, environ={}) as run:
        run.note_work_execution("we_01J", "wi_01JWORK")
        pc.decide(_request())
        run.note_work_execution("we_01J", "not-a-work-item")
        pc.decide(_request())
    _drain(api)
    first, second = api.bodies("decision")
    assert first["work_item_id"] == "wi_01JWORK"
    assert "work_item_id" not in second


def test_error_codes_are_codes_never_messages():
    assert dr.error_code("Connection reset by peer: https://x/y") == "connection-reset-by-peer:-https:-x-y"
    assert dr.error_code("") == "unspecified"
    assert len(dr.error_code("x" * 200)) == 64
    assert dr.exception_code(ValueError("secret")) == "exception:valueerror"


# ---------------------------------------------------------------------------
# never fatal
# ---------------------------------------------------------------------------
def test_a_broken_recorder_changes_neither_the_decision_nor_the_effect(api, monkeypatch):
    monkeypatch.setattr(api.rec, "record_decision", lambda *_a: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(api.rec, "start", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert pc.enforce(_request(), lambda: 42) == 42
    with pytest.raises(KeyError):
        pc.enforce(_request(), lambda: {}["missing"])
    decision = pc.decide(_request(), policy=APPROVAL_POLICY, approvals=Lookup(pc.APPROVAL_GRANTED))
    assert decision.permitted and decision.approval_ref == "aar_01JAPPROVAL"


def test_the_module_api_never_raises_on_a_broken_recorder(api, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("x")
    for name in ("record_decision", "start", "start_tool_use", "finish_tool_use", "abandon_tool_use", "flush"):
        monkeypatch.setattr(api.rec, name, broken)
    decision = pc.decide(_request())
    dr.record_decision(_request(), decision)
    attempt = dr.start(decision)
    assert not attempt.tracked
    attempt.succeeded()
    dr.start_tool_use("toolu_1", decision)
    dr.finish_tool_use("toolu_1", dr.PHASE_SUCCEEDED)
    dr.abandon_tool_use("toolu_1")
    assert dr.flush(0.1) is True


def test_a_full_queue_drops_and_counts(api, monkeypatch):
    monkeypatch.setattr(dr, "MAX_QUEUE", 1)
    rec = dr.DecisionRecorder(token=TOKEN, base_url="https://api.example.test", post=api, start_thread=False)
    dr._install_for_tests(rec)
    pc.decide(_request())
    pc.decide(_request())
    assert rec.stats()[dr.DROPPED_QUEUE_FULL] == 1


def test_the_background_thread_delivers_in_order_and_flush_waits():
    fake = FakeApi()
    rec = dr.DecisionRecorder(token=TOKEN, base_url="https://api.example.test", post=fake, sleep=lambda _s: None)
    dr._install_for_tests(rec)
    for _ in range(5):
        pc.enforce(_request(), lambda: None)
    assert rec.flush(10.0)
    kinds = [fake.kind(u, b) for u, b, _h in fake.calls]
    assert kinds == ["decision", "started", "succeeded"] * 5


def test_unflushed_writes_at_exit_are_counted(capsys, monkeypatch):
    monkeypatch.setattr(dr, "EXIT_FLUSH_SECONDS", 0.01)
    rec = dr.DecisionRecorder(token=TOKEN, base_url="https://api.example.test", post=FakeApi())
    # A writer that never runs: the queued write cannot drain.
    rec._queue.put_nowait(lambda: None)
    rec.drain_at_exit()
    assert rec.stats()[dr.DROPPED_UNFLUSHED] == 1
    assert '"result": "dropped_unflushed"' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# off by default; credentials
# ---------------------------------------------------------------------------
def test_off_by_default_nothing_is_built(monkeypatch):
    monkeypatch.setenv(ep.TOKEN_ENV, "tok")
    calls: list[Any] = []
    monkeypatch.setattr(dr, "_default_post", lambda *a: calls.append(a))
    assert not dr.enabled()
    assert dr.recorder() is None
    pc.enforce(_request(), lambda: None)
    assert calls == []
    for value in ("", "off", "0", "maybe"):
        assert not dr.enabled({dr.ENABLE_ENV: value})
    assert dr.enabled({dr.ENABLE_ENV: "on"})


def test_on_reuses_the_evidence_writer_credentials_https_only(monkeypatch, capsys):
    monkeypatch.setenv(dr.ENABLE_ENV, "on")
    monkeypatch.setenv(ep.BASE_URL_ENV, "http://plain.example")
    monkeypatch.setenv(ep.TOKEN_ENV, "tok")
    assert dr.recorder() is None
    assert '"result": "disabled"' in capsys.readouterr().out
    monkeypatch.setenv(ep.BASE_URL_ENV, "https://api.example.test/")
    rec = dr.recorder()
    assert rec is not None
    assert rec._token == "tok" and rec._base_url == "https://api.example.test"


# ---------------------------------------------------------------------------
# the MCP tool path (options.py hooks) and the Temporal gated activity
# ---------------------------------------------------------------------------
def _permitted_mcp_call(api, monkeypatch, *, approved: bool = False) -> dict[str, Any]:
    monkeypatch.setattr(pc, "current_identity", lambda: pc.ExecutionIdentity(EXECUTION_ID, "", ACTOR))
    if approved:
        monkeypatch.setattr(pc, "configured_approvals", lambda: Lookup(pc.APPROVAL_GRANTED))
    tool = "mcp__mctl__deploy_service" if approved else "mcp__mctl__list_services"
    hook = options._PolicyCheckpointHook((tool,))
    return asyncio.run(hook({"tool_name": tool, "tool_input": {"service": "x"}}, "toolu_1", None))


def test_mcp_call_opens_on_pre_and_closes_on_post(api, monkeypatch):
    answer = _permitted_mcp_call(api, monkeypatch)
    assert answer == {}
    asyncio.run(options._DecisionAttemptHook("PostToolUse")({"tool_response": {"content": []}}, "toolu_1", None))
    _drain(api)
    assert len(api.bodies("started")) == 1
    assert api.bodies("succeeded")[0]["attempt_no"] == 1


def test_mcp_error_result_and_failure_are_failed_interrupt_is_left_open(api, monkeypatch):
    _permitted_mcp_call(api, monkeypatch)
    asyncio.run(options._DecisionAttemptHook("PostToolUse")({"tool_response": {"isError": True}}, "toolu_1", None))
    _permitted_mcp_call(api, monkeypatch)
    asyncio.run(options._DecisionAttemptHook("PostToolUseFailure")({"error": "boom"}, "toolu_1", None))
    _permitted_mcp_call(api, monkeypatch)
    asyncio.run(options._DecisionAttemptHook("PostToolUseFailure")({"is_interrupt": True}, "toolu_1", None))
    _drain(api)
    assert [b["error_code"] for b in api.bodies("failed")] == ["tool_error", "tool_failure"]
    assert len(api.bodies("started")) == 3
    assert not api.bodies("succeeded")
    assert api.rec.stats()[dr.LEFT_OPEN_INTERRUPTED] == 1
    assert "boom" not in json.dumps([b for _u, b, _h in api.calls])


def test_post_hook_for_an_untracked_call_sends_nothing(api):
    asyncio.run(options._DecisionAttemptHook("PostToolUse")({}, "toolu_unknown", None))
    _drain(api)
    assert api.calls == []


def test_the_approval_receipt_answer_is_unchanged_by_recording(api, monkeypatch):
    with_recording = _permitted_mcp_call(api, monkeypatch, approved=True)
    dr._install_for_tests(None)
    without = _permitted_mcp_call(api, monkeypatch, approved=True)
    assert with_recording == without
    assert with_recording["hookSpecificOutput"]["updatedInput"][options.RECEIPT_REF_ARG] == "aar_01JAPPROVAL"


def test_post_hooks_are_registered_only_when_on(monkeypatch):
    assert set(options._policy_hooks(["mcp__mctl__x"])) == {"PreToolUse"}
    monkeypatch.setenv(dr.ENABLE_ENV, "on")
    hooks = options._policy_hooks(["mcp__mctl__x"])
    assert set(hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert hooks == options._policy_hooks(["mcp__mctl__x"])


def test_gated_activity_records_its_attempt(api):
    from orchestrator.temporal.activities import action_approval as act

    inp = act.GatedActionInput(execution_id=EXECUTION_ID, actor=ACTOR)
    ok = act.run_gated(inp, pc.GITHUB_ISSUE_COMMENT, "comment", TARGET, {"b": 1}, lambda: {"ok": True})
    failed = act.run_gated(inp, pc.GITHUB_ISSUE_COMMENT, "comment", TARGET, {"b": 2},
                           lambda: (_ for _ in ()).throw(TimeoutError()))
    _drain(api)
    assert ok.ran and not failed.ran and failed.effect_error
    assert len(api.bodies("succeeded")) == 1
    assert [b["error_code"] for b in api.bodies("failed")] == ["exception:timeouterror"]
