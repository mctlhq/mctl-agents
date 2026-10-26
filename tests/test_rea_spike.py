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
