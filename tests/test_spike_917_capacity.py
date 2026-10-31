"""Tests for tools/spike_917_capacity.py (spike mctlhq/mctl-agents#917)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("spike_917_capacity", ROOT / "tools" / "spike_917_capacity.py")
assert _spec and _spec.loader
cap = importlib.util.module_from_spec(_spec)
sys.modules["spike_917_capacity"] = cap
_spec.loader.exec_module(cap)

FIX = ROOT / "docs" / "spikes" / "917"


def _fixture():
    return (
        cap.load_records(FIX / "fixture-records.jsonl"),
        cap.load_manifest(FIX / "fixture-manifest.json"),
    )


def test_rejections_by_type_and_unknown():
    records, _ = _fixture()
    rej = cap.rejection_summary([r for r in records if r["window"] == "before"])
    assert rej == {"five_hour": (1, 1800.0), "unknown": (1, 600.0)}


def test_throughput_normalized_per_stage_and_model():
    def mk(stage):
        return {"stage": stage, "model": "m", "outcome": "completed"}

    a = cap.stage_rates([mk("x")] * 2, 2.0)
    b = cap.stage_rates([mk("x")] * 2 + [mk("y")] * 6, 2.0)
    assert a[("x", "m")]["per_hour"] == b[("x", "m")]["per_hour"] == "1.00"


def test_cost_labels_never_provider_billed():
    out = cap.report(*_fixture()).lower()
    assert "list-price estimate" in out and "pricing_version=fixture-1" in out
    assert "provider_reported" not in out and "invoice" not in out
    assert "billed" not in out


def test_missing_sources_not_zero(tmp_path):
    assert cap.load_records(tmp_path / "nope.jsonl") is None
    assert "not observed" in cap.report(None, None)
    out = cap.report([], {"windows": {"w": {}}})
    assert "hours: not observed" in out
    assert "weekly remaining all_models: not observed" in out
    assert "marginal throughput: not observed" in out


def test_weekly_series_separate_and_binding():
    _, manifest = _fixture()
    series = cap.weekly_series(manifest)
    assert series["all_models"][-1][1] == 40.0 and series["fable"][-1][1] == 15.0
    assert cap.binding_limit(series) == "fable"
    assert cap.binding_limit({"all_models": [], "fable": []}) == "not observed"


def test_marginal_throughput():
    out = cap.report(*_fixture())
    assert "marginal throughput: 1 stages" in out


def test_no_secret_prefix_in_artifacts():
    for p in FIX.parent.glob("917*"):
        files = [p] if p.is_file() else list(p.rglob("*"))
        for f in files:
            if f.is_file():
                assert "sk-ant-" not in f.read_text()
