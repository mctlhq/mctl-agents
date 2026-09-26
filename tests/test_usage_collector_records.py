"""Record sanitising (orchestrator.run_usage_collector.sanitise).

The fixture (tests/fixtures/model-usage-records.json) reconstructs the
documented characteristics of the real `mctlhq/.github` artifact 10894371011
(run 36206815658, PR 139) from design.md: two records sharing one
session_id/result_uuid, one Haiku and one Opus, `agent: claude-review`,
`devloop_stage: reviewer`, `target_repo: mctlhq/.github`, `pr_number: 139`,
`retry_attempt: 0`, and Opus's measured 3,123,860 cache-read tokens.
"""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.run_usage_collector import _ALLOWED_FIELDS, sanitise

FIXTURE = Path(__file__).parent / "fixtures" / "model-usage-records.json"


def _fixture_records() -> list[dict]:
    return json.loads(FIXTURE.read_text())["records"]


def test_fixture_records_round_trip_unchanged():
    records = _fixture_records()
    assert len(records) == 2
    for raw in records:
        sanitised = sanitise(raw)
        assert sanitised == raw


def test_opus_record_carries_the_measured_cache_read_tokens():
    opus = next(r for r in _fixture_records() if "opus" in r["model_key"])
    sanitised = sanitise(opus)
    assert sanitised is not None
    assert sanitised["cache_read_tokens"] == 3123860
    assert sanitised["devloop_stage"] == "reviewer"
    assert sanitised["agent"] == "claude-review"
    assert sanitised["target_repo"] == "mctlhq/.github"
    assert sanitised["pr_number"] == 139
    assert sanitised["retry_attempt"] == 0


def test_injected_id_is_dropped():
    raw = {**_fixture_records()[0], "id": "sha256:deadbeef"}
    sanitised = sanitise(raw)
    assert sanitised is not None
    assert "id" not in sanitised


def test_injected_cost_fields_are_dropped():
    raw = {
        **_fixture_records()[0],
        "calculated_cost": 12.34,
        "pricing_version": "2026-09-01",
        "provider_reported_cost": 99.99,
    }
    sanitised = sanitise(raw)
    assert sanitised is not None
    assert "calculated_cost" not in sanitised
    assert "pricing_version" not in sanitised
    assert "provider_reported_cost" not in sanitised


def test_unknown_prose_shaped_key_is_dropped():
    raw = {**_fixture_records()[0], "review_text": "This PR looks great, minor nit on line 42."}
    sanitised = sanitise(raw)
    assert sanitised is not None
    assert "review_text" not in sanitised
    assert set(sanitised) <= _ALLOWED_FIELDS


def test_schema_version_2_is_dropped_with_a_warning(capsys):
    raw = {**_fixture_records()[0], "schema_version": 2}
    assert sanitise(raw) is None
    assert "schema_version" in capsys.readouterr().out


def test_blank_session_id_is_dropped():
    raw = {**_fixture_records()[0], "session_id": "   "}
    assert sanitise(raw) is None


def test_missing_session_id_is_dropped():
    raw = dict(_fixture_records()[0])
    del raw["session_id"]
    assert sanitise(raw) is None


def test_missing_model_key_is_dropped():
    raw = dict(_fixture_records()[0])
    del raw["model_key"]
    assert sanitise(raw) is None


def test_blank_model_key_is_dropped():
    raw = {**_fixture_records()[0], "model_key": ""}
    assert sanitise(raw) is None


def test_absent_counter_stays_absent_rather_than_becoming_zero():
    raw = dict(_fixture_records()[0])
    del raw["cache_read_tokens"]
    sanitised = sanitise(raw)
    assert sanitised is not None
    assert "cache_read_tokens" not in sanitised


def test_no_correlation_added_from_this_processs_own_environment(monkeypatch):
    monkeypatch.setenv("WORKFLOW_NAME", "usage-collector-run-abc")
    monkeypatch.setenv("WORKFLOW_TEMPORAL_WORKFLOW_ID", "collector-wf-1")
    monkeypatch.setenv("WORKFLOW_WORK_ITEM_ID", "we_collector")
    sanitised = sanitise(_fixture_records()[0])
    assert sanitised is not None
    assert "argo_workflow_name" not in sanitised
    assert "temporal_workflow_id" not in sanitised
    assert "work_item_id" not in sanitised
    assert "execution_id" not in sanitised
