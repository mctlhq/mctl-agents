"""Execution traces inside an agent pod (mctl-agents#195): model and tool
spans from the SDK message stream, GitHub/git command spans, policy decision
events and artifact events — and, for each, that the payload stays out.

The SDK stream is built from the REAL `claude_agent_sdk` message classes (the
observer is duck-typed on their names, so a fake class would prove nothing
about the real ones) and driven through the investigator's real `_run_agent`
with the suite's `FakeMcpClient`.
"""
from __future__ import annotations

import subprocess

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from orchestrator import policy_checkpoint, run_issue_investigator, tracing
from tests.conftest import fake_mcp_client_factory

MODEL = "claude-opus-5"
PROMPT_MARKER = "PROMPT-SECRET-TEXT"
ASSISTANT_MARKER = "ASSISTANT-REASONING-TEXT"
TOOL_ARG_MARKER = "/etc/TOOL-ARG-SECRET"
TOOL_OUTPUT_MARKER = "TOOL-OUTPUT-SECRET"
BODY_MARKER = "ISSUE-COMMENT-BODY-SECRET"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (tracing.TRACEPARENT_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT", tracing.ERROR_DETAIL_ENV):
        monkeypatch.delenv(name, raising=False)
    tracing._reset_for_tests()
    yield
    tracing._reset_for_tests()


@pytest.fixture
def exported() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    assert tracing.init_tracing("test-pod", exporter=exporter, synchronous=True, set_global=False)
    return exporter


def _assistant(message_id, blocks, *, usage=None, parent=None):
    return AssistantMessage(
        content=blocks, model=MODEL, parent_tool_use_id=parent, usage=usage, message_id=message_id
    )


def _result(*, input_tokens=1500, output_tokens=420, is_error=False, api_error_status=None):
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=800,
        is_error=is_error,
        num_turns=3,
        session_id="s",
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_input_tokens": 9},
        result=f"final answer quoting {PROMPT_MARKER}",
        api_error_status=api_error_status,
    )


def _stream() -> list:
    """A realistic run: text, a Bash tool call, a failing mcp tool call, a
    delegated sub-agent (Task) whose own model turn nests under it, a result."""
    return [
        _assistant(
            "m1",
            [TextBlock(text=ASSISTANT_MARKER), ToolUseBlock(id="t1", name="Bash", input={"command": TOOL_ARG_MARKER})],
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=TOOL_OUTPUT_MARKER, is_error=False)]),
        _assistant(
            "m2",
            [ToolUseBlock(id="t2", name="mcp__mctl__get_service_status", input={"service": TOOL_ARG_MARKER})],
            usage={"input_tokens": 130, "output_tokens": 25},
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content=TOOL_OUTPUT_MARKER, is_error=True)]),
        _assistant("m3", [ToolUseBlock(id="t3", name="Task", input={"prompt": PROMPT_MARKER})]),
        _assistant(
            "c1", [TextBlock(text=ASSISTANT_MARKER)], usage={"input_tokens": 50, "output_tokens": 5}, parent="t3"
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t3", content=TOOL_OUTPUT_MARKER)]),
        _assistant("m4", [TextBlock(text=ASSISTANT_MARKER)], usage={"input_tokens": 200, "output_tokens": 40}),
        _result(),
    ]


def _spans(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


def _assert_no_payload(exporter, *markers: str) -> None:
    for span in exporter.get_finished_spans():
        blob = span.to_json()
        for marker in markers:
            assert marker not in blob, (span.name, marker)


def _run_investigator_agent(tmp_path, monkeypatch, messages) -> None:
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", fake_mcp_client_factory(messages=messages))
    anyio.run(run_issue_investigator._run_agent, tmp_path, PROMPT_MARKER, tmp_path)


# ---------------------------------------------------------------------------
# Model and tool spans
# ---------------------------------------------------------------------------


def test_model_spans_carry_model_usage_and_latency_but_no_prompt(exported, tmp_path, monkeypatch):
    _run_investigator_agent(tmp_path, monkeypatch, _stream())
    spans = _spans(exported)

    root = spans["invoke_agent issue-investigator"]
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.provider.name"] == "anthropic"
    assert root.attributes["mctl.agent.name"] == "issue-investigator"
    assert root.attributes["gen_ai.usage.input_tokens"] == 1500
    assert root.attributes["gen_ai.usage.output_tokens"] == 420
    assert "error.type" not in root.attributes

    chats = [s for s in exported.get_finished_spans() if s.name == f"chat {MODEL}"]
    assert len(chats) == 5
    for chat in chats:
        assert chat.attributes["gen_ai.operation.name"] == "chat"
        assert chat.attributes["gen_ai.response.model"] == MODEL
        assert chat.end_time >= chat.start_time
    first = min(chats, key=lambda s: s.start_time)
    assert first.attributes["gen_ai.usage.input_tokens"] == 100
    assert first.attributes["gen_ai.usage.output_tokens"] == 20
    assert first.parent.span_id == root.context.span_id

    _assert_no_payload(exported, PROMPT_MARKER, ASSISTANT_MARKER, TOOL_ARG_MARKER, TOOL_OUTPUT_MARKER)


def test_tool_spans_carry_the_name_and_outcome_but_not_the_arguments(exported, tmp_path, monkeypatch):
    _run_investigator_agent(tmp_path, monkeypatch, _stream())
    spans = _spans(exported)
    root = spans["invoke_agent issue-investigator"]

    bash = spans["execute_tool Bash"]
    assert bash.attributes["mctl.tool.name"] == "Bash"
    assert bash.attributes["gen_ai.tool.name"] == "Bash"
    assert bash.attributes["mctl.tool.status"] == "ok"
    assert bash.parent.span_id == root.context.span_id
    assert set(bash.attributes) == {"gen_ai.operation.name", "gen_ai.tool.name", "mctl.tool.name", "mctl.tool.status"}

    mcp = spans["execute_tool mcp__mctl__get_service_status"]
    assert mcp.attributes["mcp.method.name"] == "tools/call"
    assert mcp.attributes["mctl.tool.status"] == "error"
    assert mcp.attributes["error.type"] == "tool_error"

    _assert_no_payload(exported, TOOL_ARG_MARKER, TOOL_OUTPUT_MARKER)


def test_a_sub_agent_s_model_turn_nests_under_its_task_tool_span(exported, tmp_path, monkeypatch):
    _run_investigator_agent(tmp_path, monkeypatch, _stream())
    task = _spans(exported)["execute_tool Task"]
    nested = [
        s for s in exported.get_finished_spans()
        if s.name.startswith("chat ") and s.parent.span_id == task.context.span_id
    ]
    assert len(nested) == 1
    assert nested[0].attributes["gen_ai.usage.input_tokens"] == 50


def test_a_rate_limited_run_is_recorded_as_http_429_and_still_raises(exported, tmp_path, monkeypatch):
    with pytest.raises(run_issue_investigator.RateLimitExhaustedError):
        _run_investigator_agent(tmp_path, monkeypatch, [_result(is_error=True, api_error_status=429)])
    root = _spans(exported)["invoke_agent issue-investigator"]
    assert root.attributes["error.type"] in {"http_429", "RateLimitExhaustedError"}
    _assert_no_payload(exported, PROMPT_MARKER)


def test_an_observer_that_breaks_never_breaks_the_agent_loop(exported, tmp_path, monkeypatch):
    def boom(self, message):
        raise RuntimeError("observer bug")

    monkeypatch.setattr(tracing.AgentRunObserver, "_assistant", boom)
    _run_investigator_agent(tmp_path, monkeypatch, _stream())  # must not raise
    assert "invoke_agent issue-investigator" in _spans(exported)


def test_agent_run_is_inert_when_tracing_is_off(tmp_path, monkeypatch):
    _run_investigator_agent(tmp_path, monkeypatch, _stream())  # no exporter at all: must just run
    assert tracing.enabled() is False


# ---------------------------------------------------------------------------
# GitHub / git command spans
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cmd", "name", "operation", "mutation", "extra"),
    [
        (
            ["gh", "issue", "comment", "https://github.com/mctlhq/mctl-telegram/issues/617", "--body", BODY_MARKER],
            "github.issue.comment", "issue.comment", True,
            {"mctl.repository.name": "mctlhq/mctl-telegram", "mctl.issue.number": 617},
        ),
        (
            ["gh", "pr", "create", "--repo", "mctlhq/mctl-agents", "--title", BODY_MARKER, "--body", BODY_MARKER],
            "github.pr.create", "pr.create", True, {"mctl.repository.name": "mctlhq/mctl-agents"},
        ),
        (
            ["gh", "pr", "view", "https://github.com/mctlhq/mctl-api/pull/348", "--json", "state"],
            "github.pr.view", "pr.view", False, {"mctl.repository.name": "mctlhq/mctl-api", "mctl.pr.number": 348},
        ),
        (["gh", "api", "repos/mctlhq/x/issues/1"], "github.api.read", "api.read", False, {}),
        (["gh", "api", "-X", "PATCH", "repos/mctlhq/x/issues/1", "-f", f"body={BODY_MARKER}"],
         "github.api.write", "api.write", True, {}),
        (["gh", "api", "repos/mctlhq/x/labels", "-f", "name=x"], "github.api.write", "api.write", True, {}),
        (["git", "push", "-u", "origin", "feat/agents-x"], "git.push", "push", True, {}),
        (["git", "-C", "/w/repo", "commit", "-m", BODY_MARKER], "git.commit", "commit", False, {}),
        (
            ["git", "clone", "https://x-access-token:ghs_SECRETSECRETSECRET@github.com/mctlhq/mctl-web.git", "/w"],
            "git.clone", "clone", False, {},
        ),
    ],
)
def test_classify_command_reads_only_a_fixed_vocabulary(cmd, name, operation, mutation, extra):
    got_name, attributes = tracing.classify_command(cmd)
    assert got_name == name
    assert attributes == {"mctl.github.operation": operation, "mctl.github.mutation": mutation, **extra}


@pytest.mark.parametrize(
    "cmd", [["git", "status"], ["git", "diff", "HEAD"], ["ls", "-la"], ["gh", "auth", "status"], []]
)
def test_other_commands_get_no_span(cmd):
    assert tracing.classify_command(cmd) is None


def test_the_investigator_s_github_comment_is_a_mutation_span_without_its_body(exported, monkeypatch):
    calls = []

    def fake_run_capturing(cmd, cwd=None, check=True):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(run_issue_investigator, "run_capturing", fake_run_capturing)
    monkeypatch.setattr(run_issue_investigator, "refresh_github_token", lambda: None)
    url = "https://github.com/mctlhq/mctl-telegram/issues/617"
    run_issue_investigator._run(["gh", "issue", "comment", url, "--body", BODY_MARKER])
    run_issue_investigator._run(["git", "status"])

    (span,) = exported.get_finished_spans()
    assert span.name == "github.issue.comment"
    assert span.attributes["mctl.github.mutation"] is True
    assert span.attributes["mctl.issue.number"] == 617
    assert len(calls) == 2
    _assert_no_payload(exported, BODY_MARKER)


def test_a_failed_command_is_recorded_by_exit_code_and_still_raises(exported, monkeypatch):
    def failing(cmd, cwd=None, check=True):
        raise subprocess.CalledProcessError(128, cmd, stderr=f"fatal: {BODY_MARKER}")

    monkeypatch.setattr(run_issue_investigator, "run_capturing", failing)
    monkeypatch.setattr(run_issue_investigator, "refresh_github_token", lambda: None)
    with pytest.raises(subprocess.CalledProcessError):
        run_issue_investigator._run(["git", "push", "origin", "x"])

    (span,) = exported.get_finished_spans()
    assert span.attributes["error.type"] == "exit_128"
    _assert_no_payload(exported, BODY_MARKER)


# ---------------------------------------------------------------------------
# Policy decisions and artifacts
# ---------------------------------------------------------------------------


def test_a_policy_decision_is_an_event_with_rule_decision_and_code(exported, capsys):
    with tracing.span("pod"):
        decision = policy_checkpoint.checkpoint(
            policy_checkpoint.GITHUB_ISSUE_COMMENT,
            "comment",
            "https://github.com/mctlhq/mctl-telegram/issues/617",
            {"body": BODY_MARKER},
        )

    (span,) = exported.get_finished_spans()
    (event,) = [e for e in span.events if e.name == tracing.POLICY_DECISION_EVENT]
    attributes = dict(event.attributes)
    assert attributes["mctl.policy.decision"] == decision.verdict
    assert attributes["mctl.policy.code"] == decision.code
    assert attributes["mctl.policy.action_kind"] == policy_checkpoint.GITHUB_ISSUE_COMMENT
    assert attributes["mctl.policy.operation"] == "comment"
    assert attributes["mctl.policy.version"] == decision.policy_version
    if decision.rule_id:
        assert attributes["mctl.policy.rule_id"] == decision.rule_id
    # The target and the free-text reason stay in the audit log line only.
    assert "mctlhq/mctl-telegram" not in span.to_json()
    assert decision.reason not in span.to_json() or not decision.reason
    _assert_no_payload(exported, BODY_MARKER)
    assert "POLICY_DECISION" in capsys.readouterr().out


def test_a_published_proposal_records_one_artifact_event_per_file(exported, tmp_path):
    for name in (*run_issue_investigator.TRIPLET, run_issue_investigator.STATUS_FILENAME):
        (tmp_path / name).write_text(PROMPT_MARKER)
    result = run_issue_investigator.InvestigateResult(
        service="mctl-telegram", slug="issue-617-x", proposal_dir=tmp_path
    )

    with tracing.span("pod"):
        run_issue_investigator._trace_published(result)
        run_issue_investigator._trace_published(
            run_issue_investigator.InvestigateResult("s", "x", tmp_path, skipped_reason="dry-run")
        )

    (span,) = exported.get_finished_spans()
    names = [e.attributes["mctl.artifact.name"] for e in span.events if e.name == tracing.ARTIFACT_WRITE_EVENT]
    assert names == ["requirements.md", "design.md", "tasks.md", ".status.yaml"]
    _assert_no_payload(exported, PROMPT_MARKER, str(tmp_path))


def test_the_pod_root_carries_the_correlation_ids(exported):
    env = {tracing.ARGO_WORKFLOW_NAME_ENV: "mctl-agents-investigate-ab12"}
    with tracing.pod_root_span("issue-investigator.run", environ=env):
        tracing.annotate(
            workflow_type="investigate",
            repository="mctlhq/mctl-telegram",
            issue_number=617,
            execution_id="we_0123",
            work_item_id="wi_4567",
        )
    (root,) = exported.get_finished_spans()
    assert root.attributes["mctl.execution.id"] == "we_0123"
    assert root.attributes["mctl.work_item.id"] == "wi_4567"
    assert root.attributes["mctl.argo.workflow.name"] == "mctl-agents-investigate-ab12"
    assert root.attributes["mctl.workflow.type"] == "investigate"


def test_a_span_name_carrying_a_credential_is_replaced_at_export(exported):
    tracing.tracer().start_span("execute_tool ghp_" + "a" * 36).end()
    (span,) = exported.get_finished_spans()
    assert span.name == "redacted"
