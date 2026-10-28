"""Claude Code safety-classifier interruptions are counted, and a stream
that was not seen whole reads `unobserved`, never 0 (mctl-agents#849)."""
from __future__ import annotations

import types

import anyio
import pytest
from claude_agent_sdk import AssistantMessage, TextBlock, ToolResultBlock, UserMessage

from orchestrator import classifier_notice, tracing
from orchestrator import run_service_agent as rsa
from tests.conftest import fake_mcp_client_factory, result_message

NOTICE_TEXT = (
    "Your response above was stopped by a safety classifier -- this is not a tool or API error. "
    "The rest of it was withheld, and tool calls in it that had not finished did not run."
)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-5-5")


def _count(messages, error: BaseException | None = None):
    counter = classifier_notice.InterruptionCounter()
    for message in messages:
        counter.observe(message)
    return counter.result(error)


def test_notice_in_assistant_text_counts_one():
    assert _count([_assistant("let me check"), _assistant(NOTICE_TEXT), result_message()]) == 1


def test_notice_in_user_text_and_tool_results_is_counted():
    messages = [
        UserMessage(content=NOTICE_TEXT),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content=NOTICE_TEXT)]),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content=[{"type": "text", "text": NOTICE_TEXT}])]),
        result_message(),
    ]
    assert _count(messages) == 3


def test_clean_stream_counts_zero():
    assert _count([_assistant("objdump says so"), result_message()]) == 0


def test_stream_without_a_result_is_unobserved():
    assert _count([_assistant(NOTICE_TEXT)]) == classifier_notice.UNOBSERVED


def test_stream_that_raised_is_unobserved():
    assert _count([_assistant("x"), result_message()], RuntimeError("cut")) == classifier_notice.UNOBSERVED


def test_unreadable_message_makes_the_count_unobserved():
    class Broken:
        @property
        def content(self):
            raise ValueError("boom")

    assert _count([Broken(), result_message()]) == classifier_notice.UNOBSERVED


def test_agent_run_reports_the_count_with_tracing_off(capsys):
    with tracing.agent_run("issue-investigator", "claude-opus-5-5") as run:
        run.observe(_assistant(NOTICE_TEXT))
        run.observe(result_message())
    assert run.classifier_interruptions == 1
    assert "info: issue-investigator classifier_interruptions=1" in capsys.readouterr().out


def test_agent_run_reports_unobserved_when_the_session_raised(capsys):
    with pytest.raises(RuntimeError), tracing.agent_run("implementer", "claude-opus-5-5") as run:
        run.observe(result_message())
        raise RuntimeError("stream broke")
    assert run.classifier_interruptions == classifier_notice.UNOBSERVED
    assert "info: implementer classifier_interruptions=unobserved" in capsys.readouterr().out


def test_service_agent_prints_the_count(monkeypatch, capsys):
    monkeypatch.setattr(
        rsa, "build_service_agent_options", lambda *a, **k: types.SimpleNamespace(mcp_servers={})
    )

    async def messages():
        for message in (_assistant(NOTICE_TEXT), result_message()):
            yield message

    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory(messages=messages))
    anyio.run(rsa.run_service_agent, "mctl-agent")
    assert "info: service-agent mctl-agent classifier_interruptions=1" in capsys.readouterr().out
