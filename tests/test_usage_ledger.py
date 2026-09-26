"""The model-usage producer (orchestrator/usage_ledger.py, mctlhq/.github#50).

Messages are the REAL `claude_agent_sdk.ResultMessage`: the recorder is
duck-typed on its class name and field names, so a fake class would prove
nothing about the real one. The per-turn numbers in the multi-turn cases are
the ones measured on claude-agent-sdk 0.2.136 (see the module docstring):
cumulative `model_usage`, 53 output tokens after turn one and 100 after two.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from claude_agent_sdk import ResultMessage

from orchestrator import options, run_shepherd, tracing, usage_ledger
from tests.test_tracing_agents import (
    PROMPT_MARKER,
    _run_implementer_agent,
    _run_investigator_agent,
)

TOKEN = "usage-writer-token-for-tests-0123456789"
ADMIN_TOKEN = "admin-mctl-token-must-never-be-used"
HAIKU = "claude-haiku-4-5-20251001"
OPUS = "claude-opus-5"


def _usage(input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0, **extra: Any) -> dict:
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadInputTokens": cache_read,
        "cacheCreationInputTokens": cache_write,
        "webSearchRequests": 0,
        "costUSD": 0.05,
        "contextWindow": 200000,
        "maxOutputTokens": 32000,
        **extra,
    }


def _result(
    uuid: str | None,
    model_usage: dict | None,
    *,
    session: str = "session-1",
    is_error: bool = False,
    api_error_status: int | None = None,
    num_turns: int = 1,
) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=800,
        is_error=is_error,
        num_turns=num_turns,
        session_id=session,
        total_cost_usd=0.05,
        usage={"input_tokens": 10, "output_tokens": 41},
        result=f"final answer quoting {PROMPT_MARKER}",
        model_usage=model_usage,
        api_error_status=api_error_status,
        uuid=uuid,
        stop_reason="end_turn",
    )


class FakeApi:
    """Records every POST; answers with the queued statuses, then 200."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url: str, body: dict, headers: dict) -> httpx.Response:
        self.calls.append((url, body, headers))
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={}, request=httpx.Request("POST", url))

    @property
    def records(self) -> list[dict]:
        return [r for _, body, _ in self.calls for r in body["records"]]


def _recorder(api: FakeApi, agent: str = "implementer", **correlation: Any) -> usage_ledger.UsageRecorder:
    # Jobs run inline here so each test reads its outcome at once; the real
    # delivery thread has its own tests below.
    return usage_ledger.UsageRecorder(
        agent, token=TOKEN, base_url="https://api.example.test/", correlation=correlation,
        post=api, sleep=lambda _s: None, submit=lambda job: job(),
    )


# ---------------------------------------------------------------------------
# Record shape and credential
# ---------------------------------------------------------------------------


def test_one_record_per_model_posted_with_the_usage_writer_token_only():
    api = FakeApi()
    rec = _recorder(api)
    opus_usage = _usage(900, 400, provider="firstParty", canonicalModel=OPUS)
    rec.observe(_result("u1", {OPUS: opus_usage, HAIKU: _usage(50, 5)}))

    assert len(api.calls) == 1
    url, _, headers = api.calls[0]
    assert url == "https://api.example.test/api/v1/usage/records"
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    by_model = {r["model_key"]: r for r in api.records}
    assert set(by_model) == {OPUS, HAIKU}
    opus = by_model[OPUS]
    assert opus["session_id"] == "session-1"
    assert opus["result_uuid"] == "u1"
    assert opus["schema_version"] == 1
    assert opus["agent"] == "implementer"
    assert opus["provider"] == "firstParty"
    assert opus["canonical_model"] == OPUS
    assert (opus["input_tokens"], opus["output_tokens"]) == (900, 400)
    assert opus["outcome"] == "success"
    assert opus["recorded_at"].endswith("Z")


def test_a_record_carries_no_cost_no_id_and_no_text():
    """The server derives the id and prices the tokens; the producer must not
    pre-empt either (a client id is refused, and the SDK's costUSD is an
    estimate). And no field may carry prompt or completion text (ADR-012
    invariant 8)."""
    api = FakeApi()
    _recorder(api).observe(_result("u1", {OPUS: _usage(1, 2)}))
    (record,) = api.records
    for field in ("id", "calculated_cost", "provider_reported_cost", "pricing_version", "ingested_by"):
        assert field not in record
    assert PROMPT_MARKER not in json.dumps(api.calls[0][1])


def test_an_error_result_is_recorded_as_an_error_with_its_status():
    api = FakeApi()
    _recorder(api).observe(_result("u1", {OPUS: _usage(1, 2)}, is_error=True, api_error_status=429))
    (record,) = api.records
    assert record["outcome"] == "error"
    assert record["api_error_status"] == "429"


def test_correlation_comes_from_the_runner_pod_environment():
    env = {
        usage_ledger.TOKEN_ENV: TOKEN,
        "MCTL_TOKEN": ADMIN_TOKEN,
        "WORKFLOW_TEMPORAL_WORKFLOW_ID": "dev-loop-mctlhq-mctl-api-7",
        "WORKFLOW_TEMPORAL_RUN_ID": "run-abc-123",
        "WORKFLOW_NAME": "mctl-agents-investigate-abcde",
        "WORKFLOW_WORK_ITEM_ID": "wi_123",
    }
    rec = usage_ledger.UsageRecorder.from_env("issue-investigator", env)
    (record,) = rec.records_for(_result("u1", {OPUS: _usage(1, 2)}))
    assert record["agent"] == "investigator"
    assert record["temporal_workflow_id"] == "dev-loop-mctlhq-mctl-api-7"
    assert record["temporal_run_id"] == "run-abc-123"
    assert record["argo_workflow_name"] == "mctl-agents-investigate-abcde"
    assert record["work_item_id"] == "wi_123"
    assert rec._token == TOKEN


def test_correlation_omits_temporal_run_id_when_the_env_var_is_absent():
    """mctlhq/mctl-agents#505: a pod without WORKFLOW_TEMPORAL_RUN_ID (every
    runner outside a DevLoop, and every DevLoop pod before mctl-gitops#1408
    deploys) must keep recording usage exactly as before -- no warning, no
    dropped record, just an absent field."""
    env = {
        usage_ledger.TOKEN_ENV: TOKEN,
        "MCTL_TOKEN": ADMIN_TOKEN,
        "WORKFLOW_TEMPORAL_WORKFLOW_ID": "dev-loop-mctlhq-mctl-api-7",
        "WORKFLOW_NAME": "mctl-agents-implement-abcde",
        "WORKFLOW_WORK_ITEM_ID": "wi_123",
    }
    rec = usage_ledger.UsageRecorder.from_env("implementer", env)
    (record,) = rec.records_for(_result("u1", {OPUS: _usage(1, 2)}))
    assert record["temporal_workflow_id"] == "dev-loop-mctlhq-mctl-api-7"
    assert "temporal_run_id" not in record
    assert rec._token == TOKEN


def test_without_the_writer_token_nothing_is_sent_even_with_an_admin_token(monkeypatch, caplog):
    """No fallback to the admin MCTL_TOKEN: variant B keeps ingestion off the
    admin principal, so no writer token means no records."""
    monkeypatch.delenv(usage_ledger.TOKEN_ENV, raising=False)
    monkeypatch.setenv("MCTL_TOKEN", ADMIN_TOKEN)
    sent: list[Any] = []
    monkeypatch.setattr(usage_ledger, "_default_post", lambda *a: sent.append(a))
    rec = usage_ledger.UsageRecorder.from_env("implementer")
    rec.observe(_result("u1", {OPUS: _usage(1, 2)}))
    rec.observe(_result("u2", {OPUS: _usage(2, 3)}))
    assert sent == []
    assert rec.enabled is False
    assert sum("usage recording is off" in r.getMessage() for r in caplog.records) == 1


def test_messages_other_than_results_are_ignored():
    api = FakeApi()
    rec = _recorder(api)
    rec.observe(object())
    rec.observe(_result("u1", None))  # no per-model usage: nothing attributable
    rec.observe(_result("u2", {OPUS: _usage(1, 2)}, session=""))
    assert api.calls == []


# ---------------------------------------------------------------------------
# Deltas and dedupe
# ---------------------------------------------------------------------------


def test_cumulative_session_counters_are_recorded_as_per_turn_deltas():
    """Measured shape: turn two of one session reports the running total.
    Recording it as-is would count turn one twice."""
    api = FakeApi()
    rec = _recorder(api)
    rec.observe(_result("u1", {HAIKU: _usage(533, 53, 0, 26199)}))
    rec.observe(_result("u2", {HAIKU: _usage(543, 100, 26199, 34487)}))

    first, second = api.records
    assert (second["input_tokens"], second["output_tokens"]) == (10, 47)
    assert (second["cache_read_tokens"], second["cache_write_tokens"]) == (26199, 8288)
    # The rows of a session sum to what the SDK reported for it.
    assert first["output_tokens"] + second["output_tokens"] == 100
    assert first["cache_write_tokens"] + second["cache_write_tokens"] == 34487


def test_deltas_are_kept_per_session_and_per_model():
    api = FakeApi()
    rec = _recorder(api)
    rec.observe(_result("a1", {OPUS: _usage(100, 10)}, session="A"))
    rec.observe(_result("b1", {OPUS: _usage(7, 1)}, session="B"))
    rec.observe(_result("a2", {OPUS: _usage(150, 30), HAIKU: _usage(5, 5)}, session="A"))
    got = [(r["session_id"], r["model_key"], r["output_tokens"]) for r in api.records]
    assert got == [("A", OPUS, 10), ("B", OPUS, 1), ("A", OPUS, 20), ("A", HAIKU, 5)]


def test_each_turn_keeps_its_own_idempotency_identity():
    """The server keys a row on (session_id, result_uuid, model_key). Two turns
    of one session must reach it as two identities, or the second is dropped
    as a duplicate of the first."""
    api = FakeApi()
    rec = _recorder(api)
    rec.observe(_result("u1", {OPUS: _usage(10, 1)}))
    rec.observe(_result("u2", {OPUS: _usage(20, 2)}))
    identities = [(r["session_id"], r.get("result_uuid"), r["model_key"]) for r in api.records]
    assert identities == [("session-1", "u1", OPUS), ("session-1", "u2", OPUS)]


def test_results_without_a_uuid_are_told_apart_by_session():
    """An older CLI sends no uuid; the key then falls back to the turn count,
    which every session starts at 1. Only the session keeps two runs apart."""
    api = FakeApi()
    rec = _recorder(api)
    rec.observe(_result(None, {OPUS: _usage(100, 10)}, session="A"))
    rec.observe(_result(None, {OPUS: _usage(200, 20)}, session="B"))
    assert [(r["session_id"], r["output_tokens"]) for r in api.records] == [("A", 10), ("B", 20)]
    assert all("result_uuid" not in r and r["num_turns"] == 1 for r in api.records)


def test_the_same_result_observed_twice_is_sent_once_and_does_not_move_the_baseline():
    """The stream reaches the observer from the turn loop and again from the
    drain; a replayed ResultMessage must be a no-op, not a zero delta that
    advances nothing, and above all not a second copy of its tokens."""
    api = FakeApi()
    rec = _recorder(api)
    turn_one = _result("u1", {OPUS: _usage(100, 10)})
    rec.observe(turn_one)
    rec.observe(turn_one)
    rec.observe(_result("u2", {OPUS: _usage(160, 25)}))
    assert [r["result_uuid"] for r in api.records] == ["u1", "u2"]
    assert api.records[1]["output_tokens"] == 15


def test_a_redelivered_batch_is_byte_for_byte_the_same_identity():
    """What makes a retry safe server-side: the retry carries the same
    session/result/model, so it lands on the same row id."""
    api = FakeApi(500)
    rec = _recorder(api)
    rec.observe(_result("u1", {OPUS: _usage(100, 10)}))
    first, retry = (body["records"][0] for _, body, _ in api.calls)
    key = ("session_id", "result_uuid", "model_key", "input_tokens", "output_tokens")
    assert [first[k] for k in key] == [retry[k] for k in key]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_a_server_error_is_retried_once():
    api = FakeApi(503)
    _recorder(api).observe(_result("u1", {OPUS: _usage(1, 2)}))
    assert len(api.calls) == 2


def test_a_client_error_is_not_retried(caplog):
    api = FakeApi(400)
    _recorder(api).observe(_result("u1", {OPUS: _usage(1, 2)}))
    assert len(api.calls) == 1
    assert any("not delivered (HTTP 400" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param([500, 500], id="http-error"),
        pytest.param([httpx.ConnectError("down"), httpx.ConnectError("down")], id="no-connection"),
        pytest.param([httpx.PoolTimeout("pool"), httpx.PoolTimeout("pool")], id="pool-timeout"),
        pytest.param([httpx.ProxyError("proxy"), httpx.ProxyError("proxy")], id="proxy"),
        pytest.param([httpx.UnsupportedProtocol("ftp"), httpx.UnsupportedProtocol("ftp")], id="protocol"),
        pytest.param([httpx.WriteError("half"), httpx.WriteError("half")], id="write-error"),
        pytest.param([httpx.ConnectError("down"), 503], id="no-connection-then-http-error"),
    ],
)
def test_a_batch_that_certainly_did_not_land_is_carried_by_the_next_turn(failure):
    api = FakeApi(*failure)
    rec = _recorder(api)
    rec.observe(_result("u1", {OPUS: _usage(100, 10)}))
    rec.observe(_result("u2", {OPUS: _usage(160, 25)}))
    delivered = api.calls[-1][1]["records"]
    assert [(r["result_uuid"], r["output_tokens"]) for r in delivered] == [("u2", 25)]


def test_a_batch_that_may_have_landed_is_not_counted_again():
    """A lost answer may mean a stored row: carrying its tokens into the next
    turn would count them twice, the worse error for a ledger."""
    api = FakeApi(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    rec = _recorder(api)
    rec.observe(_result("u1", {OPUS: _usage(100, 10)}))
    rec.observe(_result("u2", {OPUS: _usage(160, 25)}))
    assert api.calls[-1][1]["records"][0]["output_tokens"] == 15


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param([httpx.ReadTimeout("slow"), httpx.ConnectError("down")], id="lost-answer-then-no-connection"),
        pytest.param([httpx.ReadTimeout("slow"), 503], id="lost-answer-then-http-error"),
        pytest.param([httpx.RemoteProtocolError("cut"), httpx.PoolTimeout("pool")], id="cut-then-pool"),
    ],
)
def test_once_an_attempt_may_have_landed_a_later_plain_failure_does_not_undo_it(failure):
    """The first attempt may have stored the batch. The next turn is sent
    under another result_uuid, so no server dedupe would catch a carried
    copy of these tokens: the verdict must stick."""
    api = FakeApi(*failure)
    rec = _recorder(api)
    rec.observe(_result("u1", {OPUS: _usage(100, 10)}))
    rec.observe(_result("u2", {OPUS: _usage(160, 25)}))
    assert api.calls[-1][1]["records"][0]["output_tokens"] == 15


def test_every_failed_delivery_is_logged_with_the_running_count(caplog):
    api = FakeApi(500, 500, 500, 500)
    rec = _recorder(api)
    rec.observe(_result("u1", {OPUS: _usage(100, 10), HAIKU: _usage(1, 1)}))
    rec.observe(_result("u2", {OPUS: _usage(160, 25)}))
    lines = [r.getMessage() for r in caplog.records if "not delivered" in r.getMessage()]
    assert len(lines) == 2
    assert "2 record(s) undelivered" in lines[0]
    assert "3 record(s) undelivered" in lines[1]


def test_a_padded_base_url_is_normalised_not_refused():
    """A trailing newline from a template must not read as "not https" and
    silently switch recording off everywhere."""
    rec = usage_ledger.UsageRecorder.from_env(
        "implementer", {usage_ledger.TOKEN_ENV: TOKEN, usage_ledger.BASE_URL_ENV: "  https://api.mctl.ai/\n"}
    )
    assert rec.enabled is True
    assert rec._url == "https://api.mctl.ai/api/v1/usage/records"


def test_a_recorder_that_cannot_be_built_never_fails_the_run(monkeypatch, tmp_path):
    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("usage ledger bug")

    monkeypatch.setattr(usage_ledger.UsageRecorder, "from_env", broken)
    _run_investigator_agent(tmp_path, monkeypatch, [_result("u1", {OPUS: _usage(1, 2)})])  # must not raise


@pytest.mark.parametrize("base_url", ["http://api.mctl.ai", "api.mctl.ai", "ftp://api.mctl.ai"])
def test_the_token_is_never_sent_to_a_non_https_url(monkeypatch, caplog, base_url):
    sent: list[Any] = []
    monkeypatch.setattr(usage_ledger, "_default_post", lambda *a: sent.append(a))
    rec = usage_ledger.UsageRecorder.from_env(
        "implementer", {usage_ledger.TOKEN_ENV: TOKEN, usage_ledger.BASE_URL_ENV: base_url}
    )
    rec.observe(_result("u1", {OPUS: _usage(1, 2)}))
    assert usage_ledger.flush(5)
    assert sent == []
    assert rec.enabled is False
    assert any("is not https" in r.getMessage() for r in caplog.records)


def test_observe_returns_at_once_while_delivery_is_slow():
    """observe runs inside the drivers' async loops: a slow POST must stall
    the delivery thread, never the caller."""
    release = threading.Event()
    api = FakeApi()

    def slow_post(url: str, body: dict, headers: dict) -> httpx.Response:
        release.wait(10)
        return api(url, body, headers)

    rec = usage_ledger.UsageRecorder("implementer", token=TOKEN, post=slow_post, sleep=lambda _s: None)
    started = time.monotonic()
    rec.observe(_result("u1", {OPUS: _usage(100, 10)}))
    rec.observe(_result("u2", {OPUS: _usage(160, 25)}))
    assert time.monotonic() - started < 0.5
    assert api.calls == []
    release.set()
    assert usage_ledger.flush(5)
    # In order, on one thread: the second delta is still right.
    assert [r["output_tokens"] for r in api.records] == [10, 15]


def test_a_runner_that_exits_right_after_its_last_turn_still_delivers_it(tmp_path):
    """The atexit flush: a process that observes and exits at once."""
    out = tmp_path / "delivered.json"
    script = f"""
import json, time, httpx
from claude_agent_sdk import ResultMessage
from orchestrator import usage_ledger

def post(url, body, headers):
    time.sleep(0.5)
    open({str(out)!r}, "w").write(json.dumps(body))
    return httpx.Response(200, json={{}}, request=httpx.Request("POST", url))

rec = usage_ledger.UsageRecorder("implementer", token="t" * 40, post=post)
rec.observe(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
    num_turns=1, session_id="s", uuid="u1",
    model_usage={{"m": {{"inputTokens": 3, "outputTokens": 4}}}}))
"""
    subprocess.run([sys.executable, "-c", script], check=True, cwd=Path(options.__file__).parents[1], timeout=60)
    assert json.loads(out.read_text())["records"][0]["output_tokens"] == 4


def test_recording_never_raises_into_the_run():
    def boom(*_a: Any) -> httpx.Response:
        raise RuntimeError("bug in delivery")

    rec = usage_ledger.UsageRecorder("implementer", token=TOKEN, post=boom)
    rec.observe(_result("u1", {OPUS: _usage(1, 2)}))  # must not raise
    assert usage_ledger.flush(5)


# ---------------------------------------------------------------------------
# The token stays out of the agent's own environment
# ---------------------------------------------------------------------------


def test_every_options_builder_goes_through_the_scrubbing_constructor():
    """Structural, so a future builder cannot opt out: every
    `ClaudeAgentOptions(...)` in orchestrator/options.py must be the direct
    argument of `_scrubbed(...)`."""
    tree = ast.parse(Path(options.__file__).read_text())
    wrapped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_scrubbed":
            wrapped.update(id(arg) for arg in node.args)
    built = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ClaudeAgentOptions"
    ]
    assert len(built) >= 7
    assert [node.lineno for node in built if id(node) not in wrapped] == []


def test_a_session_env_blanks_the_writer_token_even_where_a_builder_set_none(monkeypatch, tmp_path):
    """The mentor passed no env at all, so its CLI child inherited the whole
    parent environment, token included."""
    monkeypatch.setenv(usage_ledger.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    built = [
        options.build_mentor_options(tmp_path / "digest", OPUS),
        options.build_implementer_agent_options(tmp_path, OPUS, tmp_path),
        options.build_issue_investigator_options(tmp_path, OPUS, tmp_path),
        options.build_shepherd_options(tmp_path, OPUS),
        options.build_service_agent_options(tmp_path, OPUS),
    ]
    for opts in built:
        assert opts.env[usage_ledger.TOKEN_ENV] == ""
        assert opts.env["SOME_OTHER_VAR"] == "kept"


# ---------------------------------------------------------------------------
# Through the real drivers: investigator, implementer, shepherd
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(monkeypatch) -> FakeApi:
    monkeypatch.setenv(usage_ledger.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("MCTL_TOKEN", ADMIN_TOKEN)
    api = FakeApi()
    monkeypatch.setattr(usage_ledger, "_default_post", api)
    tracing._reset_for_tests()
    yield api
    tracing._reset_for_tests()


@pytest.mark.parametrize(
    ("run", "agent"),
    [(_run_investigator_agent, "investigator"), (_run_implementer_agent, "implementer")],
    ids=["investigator", "implementer"],
)
def test_each_sdk_driver_records_its_usage_with_tracing_off(ledger, tmp_path, monkeypatch, run, agent):
    assert tracing.enabled() is False
    run(tmp_path, monkeypatch, [_result("u1", {OPUS: _usage(1500, 420)})])
    assert usage_ledger.flush(5)
    (record,) = ledger.records
    assert (record["agent"], record["model_key"], record["output_tokens"]) == (agent, OPUS, 420)
    # Each ledger agent name is its own devloop_stage default (mctlhq/.github#50).
    assert record["devloop_stage"] == agent
    assert ledger.calls[0][2]["Authorization"] == f"Bearer {TOKEN}"


def test_the_shepherd_records_the_usage_of_its_normalising_call(ledger, monkeypatch, tmp_path):
    async def fake_query(*, prompt: str, options: Any):
        yield _result("s1", {HAIKU: _usage(300, 60)})

    monkeypatch.setattr("claude_agent_sdk.query", fake_query)
    monkeypatch.setattr(options, "build_shepherd_options", lambda *_a: None)
    finding = run_shepherd.CodexFinding(
        body="**P1** a real bug", path="a.py", line=1, commit_id="abc", created_at=None, severity="P1"
    )
    anyio.run(run_shepherd._format_bundle_via_sdk, [finding])
    assert usage_ledger.flush(5)
    (record,) = ledger.records
    assert (record["agent"], record["model_key"], record["output_tokens"]) == ("shepherd", HAIKU, 60)
    assert record["devloop_stage"] == "shepherd"


# ---------------------------------------------------------------------------
# devloop_stage (mctlhq/.github#50)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["reviewing", "Shepherd", "", 42, None])
def test_an_out_of_vocabulary_stage_is_dropped_with_a_warning(stage, caplog):
    """Free text, wrong case, a non-string, empty and None all fail the
    `DEVLOOP_STAGES` membership check the same way `target_repo` and
    `execution_id` are clamped today. `mentor` has no per-agent default, so
    a dropped value leaves the key truly absent rather than defaulted."""
    api = FakeApi()
    _recorder(api, "mentor", devloop_stage=stage).observe(_result("u1", {OPUS: _usage(1, 2)}))
    (record,) = api.records
    assert "devloop_stage" not in record
    if stage not in (None, ""):
        assert "usage ledger: not sending devloop_stage" in caplog.text


@pytest.mark.parametrize("stage", usage_ledger.DEVLOOP_STAGES)
def test_a_vocabulary_stage_passes_through(stage):
    api = FakeApi()
    _recorder(api, devloop_stage=stage).observe(_result("u1", {OPUS: _usage(1, 2)}))
    (record,) = api.records
    assert record["devloop_stage"] == stage


def test_an_agent_absent_from_the_default_table_records_no_stage():
    """A future `mentor`/`service-agent`/`incident-responder` path must not
    inherit a guessed stage."""
    api = FakeApi()
    _recorder(api, "mentor").observe(_result("u1", {OPUS: _usage(1, 2)}))
    (record,) = api.records
    assert "devloop_stage" not in record


def test_devloop_stage_precedence_explicit_over_scope_over_default():
    """Mirrors `test_explicit_correlation_wins_over_the_scope_and_the_scope_over_the_environment`:
    an explicit argument beats a `correlate` scope, which beats the
    per-agent default."""
    with usage_ledger.correlate({"devloop_stage": "shepherd"}):
        scoped = usage_ledger.UsageRecorder.from_env("implementer", {usage_ledger.TOKEN_ENV: TOKEN})
        explicit = usage_ledger.UsageRecorder.from_env(
            "implementer", {usage_ledger.TOKEN_ENV: TOKEN}, devloop_stage="reviewer",
        )
    assert scoped._correlation["devloop_stage"] == "shepherd"
    assert explicit._correlation["devloop_stage"] == "reviewer"
    # No scope, no explicit argument: the per-agent default fills in.
    defaulted = usage_ledger.UsageRecorder.from_env("implementer", {usage_ledger.TOKEN_ENV: TOKEN})
    assert defaulted._correlation["devloop_stage"] == "implementer"


def test_the_devloop_stage_vocabulary_is_closed_and_covers_every_default():
    assert usage_ledger.DEVLOOP_STAGES == frozenset({"investigator", "implementer", "reviewer", "shepherd"})
    assert set(usage_ledger._AGENT_STAGES.values()) <= usage_ledger.DEVLOOP_STAGES
