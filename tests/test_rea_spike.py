"""Unit tests for tools/rea_spike.py (#796). No network, REA or model calls."""

from __future__ import annotations

import subprocess
import sys

import pytest

from tools import rea_spike as rs


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        ({"tools_registered": False}, rs.CLIENT_TOOL_ADMISSION),
        ({"schema_error": True}, rs.MCP_SCHEMA_OR_CONTEXT_FAILURE),
        ({"context_overflow": True}, rs.MCP_SCHEMA_OR_CONTEXT_FAILURE),
        ({"rea_exit_code": 2}, rs.REA_TOOL_FAILURE),
        ({"tool_error": True, "declined": True}, rs.REA_TOOL_FAILURE),
        ({"transport_error": True}, rs.PROVIDER_RUNTIME_FAILURE),
        ({"declined": True}, rs.MODEL_POLICY_REFUSAL),
        ({}, rs.UNKNOWN),
    ],
)
def test_classify(signals: dict[str, object], expected: str) -> None:
    assert rs.classify(signals) == expected


def test_score_counts_and_unobserved() -> None:
    oracle = {"N1": {}, "N2": {}, "N3": {}}
    grading = {
        "claims": [
            {"label": "oracle:N1", "cites_rea": True},
            {"label": "oracle:N2", "cites_rea": False},
            {"label": "oracle:N9", "cites_rea": False},
            {"label": "unsupported"},
        ]
    }
    out = rs.score({"arm": "rea-cli", "turns": 4, "usage": {"input_tokens": 10}}, oracle, grading)
    assert out["oracle_facts_recovered"] == "2/3"
    assert out["unsupported_claims"] == 1
    assert out["evidence_grounded_claims"] == 1
    assert out["turns"] == 4
    assert out["output_tokens"] == "unobserved"
    assert out["wall_clock_s"] == "unobserved"
    assert out["plan_actionable"] == "unobserved"


def test_report_renders_unrun_models_as_not_run() -> None:
    text = rs.model_matrix([])
    for model in rs.MATRIX_MODELS:
        line = next(ln for ln in text.splitlines() if ln.startswith(f"| {model} "))
        assert "not-run (" in line
        assert "| 0 " not in line


def test_probe_single_attempt_and_stops_at_refusal() -> None:
    calls: list[str] = []

    def runner(stage: str) -> dict[str, object]:
        calls.append(stage)
        if stage == "2-static":
            return {"ok": False, "declined": True, "text": "I can't help with that"}
        return {"ok": True}

    out = rs.run_probe(runner)
    assert calls == ["1-discovery", "2-static"]
    assert out["earliest_failing_stage"] == "2-static"
    assert out["failure_class"] == rs.MODEL_POLICY_REFUSAL
    assert [s["outcome"] for s in out["stages"]] == ["ok", "failed", "not-run", "not-run", "not-run"]
    failed = out["stages"][1]
    assert "text" not in failed["text"] and len(failed["text"]["sha256"]) == 64


def test_probe_stores_text_only_on_request() -> None:
    out = rs.run_probe(lambda _s: {"ok": False, "text": "x"}, store_text=True)
    assert out["stages"][0]["text"]["text"] == "x"


def test_bounded_summary() -> None:
    s = rs.bounded_summary("a" * 5000)
    assert s["truncated"] and len(s["summary"]) == rs.MAX_SUMMARY_BYTES and s["bytes"] == 5000


@pytest.mark.parametrize(
    "sub", [["run", "--fixture", "native-codec"], ["probe-stages", "--fixture", "x"], ["schema-bytes"]]
)
def test_live_requires_flag(sub: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("no subprocess or network without --allow-live")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert rs.main(sub) == 2


def test_help_works_offline() -> None:
    with pytest.raises(SystemExit) as exc:
        rs.main(["--help"])
    assert exc.value.code == 0
    assert "rea_spike" in sys.modules["tools.rea_spike"].__name__


class _Msg:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


async def _stream(*msgs: object):  # type: ignore[no-untyped-def]
    for m in msgs:
        yield m


def _collect(*msgs: object) -> dict[str, object]:
    import anyio

    async def go() -> dict[str, object]:
        return await rs._collect(_stream(*msgs), {"ok": True, "tools_registered": True, "text": ""})

    return anyio.run(go)


def test_collect_maps_refusal_stop_reason_to_declined() -> None:
    sig = _collect(_Msg(content=[_Msg(text="I cannot help")]),
                   _Msg(is_error=False, num_turns=1, usage={}, stop_reason="refusal"))
    assert sig["ok"] is False and sig["declined"] is True
    assert rs.classify(sig) == rs.MODEL_POLICY_REFUSAL


def test_collect_maps_tool_error_and_clean_run() -> None:
    sig = _collect(
        _Msg(content=[_Msg(id="t1", name="mcp__rea__query", input={})]),
        _Msg(content=[_Msg(tool_use_id="t1", is_error=True)]),
        _Msg(is_error=False, num_turns=1, usage={}),
    )
    assert sig["ok"] is False and rs.classify(sig) == rs.REA_TOOL_FAILURE
    ok = _collect(_Msg(content=[_Msg(text="done")]), _Msg(is_error=False, num_turns=1, usage={}))
    assert ok["ok"] is True and ok["text"] == "done"


def test_collect_non_rea_tool_error_does_not_fail_stage() -> None:
    sig = _collect(
        _Msg(content=[_Msg(id="t1", name="Bash", input={"command": "grep foo bar"})]),
        _Msg(content=[_Msg(tool_use_id="t1", is_error=True)]),
        _Msg(is_error=False, num_turns=1, usage={}),
    )
    assert sig["ok"] is True and sig["other_tool_errors"] == 1
    assert rs.classify(sig) != rs.REA_TOOL_FAILURE


def test_rea_mcp_without_command_fails_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.delenv("REA_SPIKE_MCP_CMD", raising=False)
    sig = rs._live_stage("p", "m", tmp_path, "rea-mcp")  # type: ignore[arg-type]
    assert sig["ok"] is False and rs.classify(sig) == rs.CLIENT_TOOL_ADMISSION


def test_collect_counts_tool_calls_and_leaves_broken_stream_unobserved() -> None:
    sig = _collect(
        _Msg(content=[_Msg(id="t1", name="Bash", input={}), _Msg(text="x")]),
        _Msg(content=[_Msg(id="t2", name="mcp__rea__query", input={})]),
        _Msg(is_error=False, num_turns=2, usage={}),
    )
    assert sig["tool_calls"] == 2

    async def broken():  # type: ignore[no-untyped-def]
        yield _Msg(content=[_Msg(id="t1", name="Bash", input={})])
        raise RuntimeError("stream dropped")

    import anyio

    async def go() -> dict[str, object]:
        return await rs._collect(broken(), {"ok": True, "text": ""})

    assert "tool_calls" not in anyio.run(go)
