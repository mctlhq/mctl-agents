"""Tests for orchestrator/work_context/contract.py (mctlhq/mctl-agents#267).

T1-T3 in the proposal's tasks.md:

- T1: round-trip of every dataclass; a payload with an unknown key parses; a
  payload missing a required key returns None; an out-of-vocabulary
  surface.kind / actor.kind / state classifies as WORK_ITEM_UNKNOWN.
- T2: execution_id_for is deterministic across calls and distinct for
  distinct inputs.
- T3: reconstruct_canonical_state rebuilds service/slug/prior status/prior
  execution ids from a WorkItem plus a tmp_path proposal dir alone; a
  companion test asserts the function signature and CanonicalState field
  set carry no transcript-shaped parameter or field.
"""
from __future__ import annotations

import inspect
from dataclasses import fields
from pathlib import Path

from orchestrator.work_context import contract as wc

# ---------------------------------------------------------------------------
# T1 — round trip, tolerant unknown keys, fail-closed on a missing key
# ---------------------------------------------------------------------------


def test_surface_ref_round_trips_and_ignores_unknown_keys():
    ref = wc.SurfaceRef.from_payload({"kind": "github", "surface_id": "mctlhq/x#1", "extra": "ignored"})
    assert ref == wc.SurfaceRef(kind="github", surface_id="mctlhq/x#1")


def test_surface_ref_missing_kind_is_none():
    assert wc.SurfaceRef.from_payload({"surface_id": "x"}) is None
    assert wc.SurfaceRef.from_payload("not-a-dict") is None


def test_actor_ref_round_trips_and_ignores_unknown_keys():
    ref = wc.ActorRef.from_payload({"kind": "human", "actor_id": "octocat", "extra": "ignored"})
    assert ref == wc.ActorRef(kind="human", actor_id="octocat")


def test_actor_ref_missing_kind_is_none():
    assert wc.ActorRef.from_payload({"actor_id": "octocat"}) is None


def test_work_item_ref_round_trips_and_ignores_unknown_keys():
    ref = wc.WorkItemRef.from_payload({"work_item_id": "wi-1", "revision": "3", "extra": "ignored"})
    assert ref == wc.WorkItemRef(work_item_id="wi-1", revision="3")


def test_work_item_ref_missing_id_is_none():
    assert wc.WorkItemRef.from_payload({"revision": "3"}) is None


def _execution_payload(**overrides):
    payload = {
        "execution_id": "e1",
        "sequence": 1,
        "temporal_workflow_id": "dev-loop-mctlhq-mctl-agents-267",
        "started_at": "2026-09-19T00:00:00Z",
        "surface": {"kind": "github", "surface_id": "mctlhq/x#1"},
        "actor": {"kind": "human", "actor_id": "octocat"},
    }
    payload.update(overrides)
    return payload


def test_execution_ref_round_trips_and_ignores_unknown_keys():
    ref = wc.ExecutionRef.from_payload({**_execution_payload(), "extra": "ignored"})
    assert ref == wc.ExecutionRef(
        execution_id="e1",
        sequence=1,
        temporal_workflow_id="dev-loop-mctlhq-mctl-agents-267",
        started_at="2026-09-19T00:00:00Z",
        surface=wc.SurfaceRef(kind="github", surface_id="mctlhq/x#1"),
        actor=wc.ActorRef(kind="human", actor_id="octocat"),
    )


def test_execution_ref_missing_execution_id_is_none():
    payload = _execution_payload()
    del payload["execution_id"]
    assert wc.ExecutionRef.from_payload(payload) is None


def test_execution_ref_missing_sequence_is_none():
    payload = _execution_payload()
    del payload["sequence"]
    assert wc.ExecutionRef.from_payload(payload) is None


def test_execution_ref_tolerates_absent_surface_and_actor():
    payload = _execution_payload()
    del payload["surface"]
    del payload["actor"]
    ref = wc.ExecutionRef.from_payload(payload)
    assert ref is not None
    assert ref.surface == wc.SurfaceRef()
    assert ref.actor == wc.ActorRef()


def test_execution_ref_malformed_surface_is_none():
    payload = _execution_payload(surface={"surface_id": "no-kind"})
    assert wc.ExecutionRef.from_payload(payload) is None


def _work_item_payload(**overrides):
    payload = {
        "work_item_id": "wi-1",
        "revision": "1",
        "state": "open",
        "origin": {"kind": "github"},
        "executions": [_execution_payload()],
        "issue_url": "https://github.com/mctlhq/mctl-agents/issues/267",
        "service": "mctl-agents",
        "slug": "issue-267-feat-work-context",
    }
    payload.update(overrides)
    return payload


def test_work_item_round_trips_and_ignores_unknown_keys():
    item = wc.WorkItem.from_payload({**_work_item_payload(), "extra": "ignored"})
    assert item is not None
    assert item.work_item_id == "wi-1"
    assert item.state == "open"
    assert len(item.executions) == 1
    assert item.executions[0].execution_id == "e1"


def test_work_item_missing_work_item_id_is_none():
    payload = _work_item_payload()
    del payload["work_item_id"]
    assert wc.WorkItem.from_payload(payload) is None


def test_work_item_missing_state_is_none():
    payload = _work_item_payload()
    del payload["state"]
    assert wc.WorkItem.from_payload(payload) is None


def test_work_item_with_malformed_execution_is_none():
    payload = _work_item_payload(executions=[{"sequence": 1}])
    assert wc.WorkItem.from_payload(payload) is None


def test_work_item_tolerates_absent_origin_and_executions():
    payload = _work_item_payload()
    del payload["origin"]
    del payload["executions"]
    item = wc.WorkItem.from_payload(payload)
    assert item is not None
    assert item.origin == wc.SurfaceRef()
    assert item.executions == ()


def test_out_of_vocabulary_state_classifies_as_unknown():
    item = wc.WorkItem.from_payload(_work_item_payload(state="quantum-superposition"))
    assert item is not None  # parsing tolerates it — never a permissive default
    assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_UNKNOWN


def test_out_of_vocabulary_surface_kind_classifies_as_unknown():
    payload = _work_item_payload(executions=[_execution_payload(surface={"kind": "carrier-pigeon"})])
    item = wc.WorkItem.from_payload(payload)
    assert item is not None
    assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_UNKNOWN


def test_out_of_vocabulary_actor_kind_classifies_as_unknown():
    payload = _work_item_payload(executions=[_execution_payload(actor={"kind": "alien"})])
    item = wc.WorkItem.from_payload(payload)
    assert item is not None
    assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_UNKNOWN


def test_every_documented_state_classifies_as_found():
    for state in sorted(wc.WORK_ITEM_STATES):
        item = wc.WorkItem.from_payload(_work_item_payload(state=state, executions=[]))
        assert item is not None
        assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_FOUND


# ---------------------------------------------------------------------------
# HTTP-response classification (also exercised through client.py in
# tests/test_work_context_client.py — this file pins the pure function).
# ---------------------------------------------------------------------------


def test_answer_from_200_found():
    answer = wc.answer_from(200, _work_item_payload())
    assert answer.verdict == wc.WORK_ITEM_FOUND
    assert answer.item is not None


def test_answer_from_200_with_no_record_is_unknown():
    answer = wc.answer_from(200, {"ok": True})
    assert answer.verdict == wc.WORK_ITEM_UNKNOWN


def test_answer_from_404_with_error_envelope_is_absent():
    answer = wc.answer_from(404, {"error": "no such work item"})
    assert answer.verdict == wc.WORK_ITEM_ABSENT


def test_answer_from_404_without_error_envelope_is_unknown():
    answer = wc.answer_from(404, {})
    assert answer.verdict == wc.WORK_ITEM_UNKNOWN


def test_answer_from_409_is_conflict():
    answer = wc.answer_from(409, {"error": "conflict", **_work_item_payload()})
    assert answer.verdict == wc.WORK_ITEM_CONFLICT


def test_answer_from_5xx_is_unknown():
    answer = wc.answer_from(503, {"error": "unavailable"})
    assert answer.verdict == wc.WORK_ITEM_UNKNOWN


# ---------------------------------------------------------------------------
# T2 — execution_id_for
# ---------------------------------------------------------------------------
def test_execution_id_for_is_deterministic():
    assert wc.execution_id_for("wi-1", 1, "0") == wc.execution_id_for("wi-1", 1, "0")


def test_execution_id_for_differs_for_distinct_inputs():
    base = wc.execution_id_for("wi-1", 1, "0")
    assert base != wc.execution_id_for("wi-2", 1, "0")
    assert base != wc.execution_id_for("wi-1", 2, "0")
    assert base != wc.execution_id_for("wi-1", 1, "1")


def test_execution_id_for_has_no_uuid_fallback_documented():
    assert "UUID" in (wc.execution_id_for.__doc__ or "")


# ---------------------------------------------------------------------------
# T3 — reconstruct_canonical_state
# ---------------------------------------------------------------------------
def test_reconstruct_canonical_state_from_work_item_alone():
    item = wc.WorkItem(work_item_id="wi-1", state="open", service="mctl-agents", slug="issue-267-x")
    state = wc.reconstruct_canonical_state(item, None, ())
    assert state.work_item_id == "wi-1"
    assert state.state == "open"
    assert state.service == "mctl-agents"
    assert state.slug == "issue-267-x"
    assert state.artifacts_present == ()
    assert state.prior_status == ""
    assert state.reconstructed_from == ("work_item",)


def test_reconstruct_canonical_state_reads_proposal_dir_and_status(tmp_path: Path):
    proposal_dir = tmp_path / "issue-267-x"
    proposal_dir.mkdir()
    (proposal_dir / "requirements.md").write_text("# req\n")
    (proposal_dir / "design.md").write_text("# design\n")
    (proposal_dir / ".status.yaml").write_text("status: accepted\nsource:\n  issue: 267\n")

    item = wc.WorkItem(work_item_id="wi-1", state="in-progress", service="mctl-agents", slug="issue-267-x")
    state = wc.reconstruct_canonical_state(item, proposal_dir, ())

    assert state.prior_status == "accepted"
    assert set(state.artifacts_present) == {"requirements.md", "design.md", ".status.yaml"}
    assert "proposal_dir" in state.reconstructed_from


def test_reconstruct_canonical_state_carries_prior_execution_ids_from_digests():
    item = wc.WorkItem(work_item_id="wi-1", state="open")
    digests = [{"execution_id": "e1", "snapshot_id": "cs-abc"}, {"execution_id": "e2"}]
    state = wc.reconstruct_canonical_state(item, None, digests)
    assert state.prior_execution_ids == ("e1", "e2")
    assert "prior_digests" in state.reconstructed_from


def test_reconstruct_canonical_state_signature_carries_no_transcript_parameter():
    """The `no raw transcript` requirement, as a checkable type property
    rather than a promise — mirroring how ContextSource is defended by
    having no payload field to put one in (context_snapshot.py:277-281)."""
    signature = inspect.signature(wc.reconstruct_canonical_state)
    transcript_tokens = ("message", "transcript", "conversation", "prompt", "body", "text")
    for name in signature.parameters:
        lowered = name.lower()
        for token in transcript_tokens:
            assert token not in lowered, f"parameter {name!r} looks transcript-shaped"


def test_canonical_state_has_no_free_text_message_field():
    transcript_tokens = ("message", "transcript", "conversation", "prompt", "body", "text")
    for f in fields(wc.CanonicalState):
        lowered = f.name.lower()
        for token in transcript_tokens:
            assert token not in lowered, f"field {f.name!r} looks transcript-shaped"


# ---------------------------------------------------------------------------
# work_context_params — pure, no call site (see contract.py's docstring)
# ---------------------------------------------------------------------------
def test_work_context_params_is_a_pure_string_dict():
    item = wc.WorkItem(work_item_id="wi-1", state="open")
    execution = wc.ExecutionRef(execution_id="e1", sequence=2)
    params = wc.work_context_params(item, execution)
    assert params == {"work_item_id": "wi-1", "execution_id": "e1", "execution_sequence": "2"}
    assert all(isinstance(v, str) for v in params.values())


# ---------------------------------------------------------------------------
# Import discipline — this package must stay stdlib-only (mirrors
# tests/test_context_snapshot.py's test_module_import_is_stdlib_only).
# ---------------------------------------------------------------------------
def test_module_import_is_stdlib_only():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.work_context, sys; print(chr(10).join(sorted(sys.modules)))",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr[-2000:]}"
    loaded = set(result.stdout.split("\n"))
    third_party_prefixes = ("claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio", "mcp")
    leaked = sorted(
        name for name in loaded
        if any(name == prefix or name.startswith(prefix + ".") for prefix in third_party_prefixes)
    )
    assert not leaked, f"orchestrator.work_context pulled in third-party modules: {leaked}"
