"""Tests for orchestrator/evidence_store.py — Tier A persistence and
retrieval for `ExecutionEvidence` (mctlhq/mctl-agents#199, ADR 015)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator import evidence_store as store
from orchestrator import execution_evidence as ee


def _identity() -> ee.IdentityBlock:
    return ee.IdentityBlock(
        actor=ee.Actor(type="github_user", id="octocat", verification="control-plane-verified"),
        executor=ee.Executor(type="implementer", agent="implementer", version="1.0.0"),
        environment="production", tenant="mctlhq", repository="mctlhq/mctl-agents",
        target_repository_sha="a" * 40, context_trust="control-plane",
    )


def _execution(**overrides) -> ee.ExecutionBlock:
    fields = {
        "trace_id": "b" * 32,
        "workflow_type": "implement",
        "temporal_workflow_id": "dev-loop-mctlhq-mctl-agents-199",
        "attempt": 1,
    }
    fields.update(overrides)
    return ee.ExecutionBlock(**fields)


def _record(**overrides) -> ee.ExecutionEvidence:
    fields = {
        "execution": _execution(),
        "identity": _identity(),
        "completeness": ee.Completeness(status="COMPLETE", gaps=()),
        "outcome": ee.Outcome(status="SUCCESS", code="merged"),
        "retention": ee.Retention(class_="gitops", expires_after_days=3650),
        "created_at": "2026-09-23T00:05:01Z",
    }
    fields.update(overrides)
    return ee.seal(**fields)


# ---------------------------------------------------------------------------
# T9 — round trip: seal, write, retrieve by workflow id / trace id / evidence id
# ---------------------------------------------------------------------------


def test_write_creates_record_file_and_by_trace_pointer(tmp_path: Path):
    record = _record()
    path = store.write(record, state_dir=tmp_path)
    assert path.exists()
    assert path.name == f"1-{record.evidence_id}.json"

    pointer = store.pointer_path(store.evidence_root(tmp_path), trace_id=record.execution.trace_id,
                                  evidence_id=record.evidence_id)
    assert pointer.exists()
    assert Path(pointer.read_text(encoding="utf-8")) == path


def test_read_by_evidence_id_round_trips(tmp_path: Path):
    record = _record()
    store.write(record, state_dir=tmp_path)
    loaded = store.read_by_evidence_id(record.evidence_id, state_dir=tmp_path)
    assert loaded == record


def test_read_by_workflow_id_returns_newest_attempt_first(tmp_path: Path):
    first = _record(execution=_execution(attempt=1))
    second = _record(execution=_execution(attempt=2))
    store.write(first, state_dir=tmp_path)
    store.write(second, state_dir=tmp_path)
    loaded = store.read_by_workflow_id("dev-loop-mctlhq-mctl-agents-199", state_dir=tmp_path)
    assert [r.execution.attempt for r in loaded] == [2, 1]


def test_read_by_trace_id_round_trips(tmp_path: Path):
    record = _record()
    store.write(record, state_dir=tmp_path)
    loaded = store.read_by_trace_id(record.execution.trace_id, state_dir=tmp_path)
    assert len(loaded) == 1
    assert loaded[0] == record


def test_read_by_evidence_id_returns_none_when_absent(tmp_path: Path):
    assert store.read_by_evidence_id("ev-doesnotexist00000", state_dir=tmp_path) is None


def test_read_by_workflow_id_returns_empty_list_when_absent(tmp_path: Path):
    assert store.read_by_workflow_id("no-such-workflow", state_dir=tmp_path) == []


def test_exported_bytes_re_hash_to_content_hash(tmp_path: Path):
    record = _record()
    path = store.write(record, state_dir=tmp_path)
    raw = json.loads(path.read_bytes())
    reloaded = ee.ExecutionEvidence.from_dict(raw)
    assert ee.is_trustworthy(reloaded)
    assert ee.recompute_content_hash(reloaded) == record.content_hash


def test_write_is_idempotent_for_identical_inputs(tmp_path: Path):
    record = _record()
    path_a = store.write(record, state_dir=tmp_path)
    mtime_a = path_a.stat().st_mtime_ns
    path_b = store.write(record, state_dir=tmp_path)
    assert path_a == path_b
    # Second write must not touch the file (task 11 DoD: "a re-seal of
    # identical inputs overwrites nothing new").
    assert path_b.stat().st_mtime_ns == mtime_a


# ---------------------------------------------------------------------------
# CLI (task 12)
# ---------------------------------------------------------------------------


def test_cli_show_by_evidence_id_prints_canonical_json(tmp_path: Path, capsys):
    record = _record()
    store.write(record, state_dir=tmp_path)
    exit_code = store.main(["show", "--evidence-id", record.evidence_id, "--state-dir", str(tmp_path)])
    assert exit_code == 0
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    assert printed["evidence_id"] == record.evidence_id
    reloaded = ee.ExecutionEvidence.from_dict(printed)
    assert ee.is_trustworthy(reloaded)


def test_cli_show_reports_missing_evidence(tmp_path: Path, capsys):
    exit_code = store.main(["show", "--evidence-id", "ev-doesnotexist00000", "--state-dir", str(tmp_path)])
    assert exit_code == 1
    assert "no evidence found" in capsys.readouterr().err


def test_cli_show_reports_untrusted_document_and_does_not_print_it_as_evidence(tmp_path: Path, capsys):
    record = _record()
    path = store.write(record, state_dir=tmp_path)
    tampered = json.loads(path.read_bytes())
    tampered["outcome"]["status"] = "FAILURE"
    path.write_text(json.dumps(tampered), encoding="utf-8")

    exit_code = store.main(["show", "--evidence-id", record.evidence_id, "--state-dir", str(tmp_path)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "UNTRUSTED" in captured.err
    assert captured.out == ""


def test_cli_requires_exactly_one_selector(tmp_path: Path):
    with pytest.raises(SystemExit):
        store.main(["show", "--state-dir", str(tmp_path)])
