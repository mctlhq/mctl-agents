"""Regression tests: shepherd JSON parsing, status overwrite, status lock,
snapshot per-source ceiling (mctl-agents#338, #335, #308, #347)."""
from __future__ import annotations

import multiprocessing
from dataclasses import replace as dc_replace

import pytest
import yaml

from orchestrator import context_snapshot as cs
from orchestrator import proposal_state, run_shepherd
from orchestrator import run_issue_investigator as inv
from orchestrator.temporal.workflows import dev_loop
from tests.test_context_snapshot import _budget, _minimal_snapshot, _source


# --- #338 -----------------------------------------------------------------
def test_try_parse_json_takes_last_object_not_first_to_last_brace():
    blob = 'draft {"summaries": [1]} then prose {braces} final {"summaries": [2], "p1": true}'
    assert run_shepherd._try_parse_json(blob) == {"summaries": [2], "p1": True}


def test_try_parse_json_single_and_fenced_and_garbage():
    assert run_shepherd._try_parse_json('```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}
    assert run_shepherd._try_parse_json("no json {") is None
    assert run_shepherd._try_parse_json("") is None


# --- #335 -----------------------------------------------------------------
@pytest.mark.parametrize("status", ["needs-triage", "review-stuck"])
def test_failure_states_rewritable_only_without_pr(status):
    assert inv._rewrite_allowed({"status": status})
    assert not inv._rewrite_allowed({"status": status, "pr": "https://github.com/o/r/pull/1"})


@pytest.mark.parametrize("status", ["accepted", "implemented", "merged"])
def test_in_flight_states_stay_protected(status):
    assert not inv._rewrite_allowed({"status": status})
    assert inv._rewrite_allowed({}) and inv._rewrite_allowed({"status": "proposed"})


def test_workflow_set_is_not_widened():
    # Replay safety and "refused rewrite keeps ending early" (see dev_loop).
    assert set(dev_loop._OVERWRITABLE_PROPOSAL_STATUSES) == {"proposed"}


def test_reopened_from_is_persisted(tmp_path):
    from tests.test_run_issue_investigator import _issue

    marker = {"status": "review-stuck", "updated_by": "shepherd", "updated_at": "t0"}
    path = inv.write_status_yaml(tmp_path / "p", _issue(), reopened_from=marker)
    assert yaml.safe_load(path.read_text())["reopened_from"] == marker
    plain = inv.write_status_yaml(tmp_path / "q", _issue())
    assert "reopened_from" not in yaml.safe_load(plain.read_text())


# --- #308 -----------------------------------------------------------------
def _bump(args):
    path, i = args
    proposal_state.update_status_file(path, "proposed", **{f"k{i}": i})


def test_concurrent_status_updates_do_not_lose_fields(tmp_path):
    path = tmp_path / "p" / ".status.yaml"
    n = 12
    ctx = multiprocessing.get_context("fork")
    with ctx.Pool(6) as pool:
        pool.map(_bump, [(path, i) for i in range(n)])
    data = yaml.safe_load(path.read_text())
    assert {f"k{i}" for i in range(n)} <= set(data)
    assert list(path.parent.iterdir()) == [path]  # no sidecar lock/tmp left


# --- #347 -----------------------------------------------------------------
def test_per_source_ceiling_ignores_excluded_sources():
    big_excluded = _source(
        source_id="s2",
        content_hash="sha256:" + "4d" * 32,
        byte_count=900,
        selection=cs.Selection(rank=2, reason_code="target-repository-tree", included=False),
    )
    snap = _minimal_snapshot(
        sources=[_source(), big_excluded],
        budget=_budget(used_sources=1, used_bytes=100, max_bytes_per_source=500),
    )
    snap.validate()


def test_per_source_ceiling_still_rejects_included_overrun():
    snap = _minimal_snapshot(sources=[_source()], budget=_budget(used_sources=1, used_bytes=100))
    bad = dc_replace(snap, budget=_budget(used_sources=1, used_bytes=100, max_bytes_per_source=50))
    with pytest.raises(cs.ContextSnapshotError, match="max_bytes_per_source"):
        bad.validate()
