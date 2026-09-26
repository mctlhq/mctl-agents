"""Tests for tools/capability_bench.py — mctlhq/mctl-agents#242 slice 4
(task 5/13, T14). Every function exercised here is pure: recorded fixture
data in, a report dict or a markdown string out — no model call, no network,
no `claude_agent_sdk`/`mcp` import. The live halves (`_live_mctl_tools`,
`_cmd_schema_bytes`) are operator-run only and out of scope for this suite
(see the module's own docstring and docs/benchmarks/capability-discovery.md).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import capability_bench as cb

# ---------------------------------------------------------------------------
# schema-bytes
# ---------------------------------------------------------------------------


def test_eager_first_turn_bytes_sums_every_advertised_tool():
    tools = [
        SimpleNamespace(inputSchema={"type": "object", "properties": {"a": {"type": "string"}}}),
        SimpleNamespace(inputSchema={"type": "object"}),
    ]
    assert cb.eager_first_turn_bytes(tools) == sum(cb._schema_bytes(t.inputSchema) for t in tools)
    assert cb.eager_first_turn_bytes([]) == 0


def test_discovery_first_turn_bytes_sums_the_three_gateway_tools():
    tools = [
        SimpleNamespace(input_schema={"type": "object", "properties": {"query": {"type": "string"}}}),
        SimpleNamespace(input_schema={"type": "object", "properties": {"capability_ids": {"type": "array"}}}),
        SimpleNamespace(input_schema={"type": "object", "properties": {"capability_id": {"type": "string"}}}),
    ]
    assert cb.discovery_first_turn_bytes(tools) == sum(cb._schema_bytes(t.input_schema) for t in tools)


def test_schema_bytes_report_computes_the_saving():
    mctl_tools = [SimpleNamespace(inputSchema={"type": "object"}) for _ in range(70)]
    gateway_tools = [SimpleNamespace(input_schema={"type": "object"}) for _ in range(3)]

    report = cb.schema_bytes_report(mctl_tools, gateway_tools)

    assert report["eager_tool_count"] == 70
    assert report["discovery_tool_count"] == 3
    assert report["saved_bytes"] == report["eager_schema_bytes"] - report["discovery_schema_bytes"]
    assert report["saved_bytes"] > 0


def test_a_tool_with_no_schema_attribute_counts_as_an_empty_schema():
    """A `None`/missing schema must not raise — `_schema_bytes` treats it as
    `{}`, matching `orchestrator/capability_gateway.py`'s own `or {}`
    convention for the same field."""
    assert cb._schema_bytes(None) == cb._schema_bytes({})
    assert cb.eager_first_turn_bytes([SimpleNamespace(inputSchema=None)]) == cb._schema_bytes({})


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def _result(
    *, session_id: str, uuid: str, input_tokens: int, output_tokens: int,
    cache_read: int = 0, cache_write: int = 0, num_turns: int = 1, duration_api_ms: int = 100,
) -> dict:
    """One recorded ResultMessage, shaped exactly like the fields
    `orchestrator.usage_ledger.UsageRecorder._plan` reads off a real one."""
    return {
        "session_id": session_id,
        "uuid": uuid,
        "model_usage": {
            "claude-sonnet-5": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cacheReadInputTokens": cache_read,
                "cacheCreationInputTokens": cache_write,
            },
        },
        "num_turns": num_turns,
        "duration_api_ms": duration_api_ms,
        "is_error": False,
    }


def test_totals_for_transcript_takes_deltas_of_cumulative_usage_within_a_session():
    """claude P2 on #513: model_usage, num_turns and duration_api_ms are
    cumulative per session, so a second ResultMessage reporting 120 input
    tokens after 100 means 120 in total, not 220."""
    messages = [
        _result(session_id="s1", uuid="r1", input_tokens=100, output_tokens=50, num_turns=2, duration_api_ms=500),
        _result(session_id="s1", uuid="r2", input_tokens=120, output_tokens=60, num_turns=3, duration_api_ms=800),
    ]

    totals = cb._totals_for_transcript(messages)

    assert totals["input_tokens"] == 120
    assert totals["output_tokens"] == 60
    assert totals["num_turns"] == 3
    assert totals["duration_api_ms"] == 800


def test_totals_for_transcript_sums_across_sessions():
    messages = [
        _result(session_id="s1", uuid="r1", input_tokens=100, output_tokens=50, num_turns=2, duration_api_ms=500),
        _result(session_id="s2", uuid="r1", input_tokens=20, output_tokens=10, num_turns=3, duration_api_ms=300),
    ]

    totals = cb._totals_for_transcript(messages)

    assert totals["input_tokens"] == 120
    assert totals["output_tokens"] == 60
    assert totals["num_turns"] == 5
    assert totals["duration_api_ms"] == 800


def test_compare_rejects_a_transcript_that_is_not_an_array_of_objects(tmp_path):
    good = tmp_path / "good.json"
    good.write_text("[]", encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text('{"session_id": "s1"}', encoding="utf-8")

    with pytest.raises(SystemExit, match="expected a JSON array"):
        cb.main(["compare", str(good), str(bad)])


def test_compare_report_states_the_sample_size():
    eager = [_result(session_id="e1", uuid="r1", input_tokens=200, output_tokens=80, num_turns=4)]
    discovery = [_result(session_id="d1", uuid="r1", input_tokens=60, output_tokens=30, num_turns=2)]

    report = cb.compare_report(eager, discovery)

    assert report["sample_size"] == 1
    assert report["eager"]["input_tokens"] == 200
    assert report["discovery"]["input_tokens"] == 60


def test_render_markdown_table_includes_every_metric_and_the_sample_size():
    report = cb.compare_report(
        [_result(session_id="e1", uuid="r1", input_tokens=200, output_tokens=80)],
        [_result(session_id="d1", uuid="r1", input_tokens=60, output_tokens=30)],
    )

    table = cb.render_markdown_table(report)

    assert "| Input tokens | 200 | 60 |" in table
    assert "| Output tokens | 80 | 30 |" in table
    assert "Sample size: 1 run per mode." in table


def test_compare_report_handles_an_empty_transcript():
    """A run with no ResultMessage at all (e.g. it errored before completing
    a turn) must report zeros, not raise."""
    report = cb.compare_report([], [])
    assert report["eager"] == {field: 0 for field, _ in cb._ALL_FIELDS}
    assert report["discovery"] == {field: 0 for field, _ in cb._ALL_FIELDS}
