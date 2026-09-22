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


def test_surface_ref_kindless_block_is_absent_not_malformed():
    # #408 round 4 (agy P2): a kindless block is an absent surface — a
    # record serialized from a default SurfaceRef() — never a parse abort.
    assert wc.SurfaceRef.from_payload({"surface_id": "x"}) == wc.SurfaceRef(surface_id="x")
    assert wc.SurfaceRef.from_payload({"kind": ""}) == wc.SurfaceRef()
    assert wc.SurfaceRef.from_payload("not-a-dict") is None
    assert wc.SurfaceRef.from_payload({"kind": 5}) is None


def test_actor_ref_round_trips_and_ignores_unknown_keys():
    ref = wc.ActorRef.from_payload({"kind": "human", "actor_id": "octocat", "extra": "ignored"})
    assert ref == wc.ActorRef(kind="human", actor_id="octocat")


def test_actor_ref_kindless_block_is_absent_not_malformed():
    assert wc.ActorRef.from_payload({"actor_id": "octocat"}) == wc.ActorRef(actor_id="octocat")
    assert wc.ActorRef.from_payload({"kind": 5}) is None


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
    # Kindless is tolerated (see above); MALFORMED — a non-dict block or a
    # non-string kind — still refuses the record.
    assert wc.ExecutionRef.from_payload(_execution_payload(surface={"kind": 5})) is None
    assert wc.ExecutionRef.from_payload(_execution_payload(surface="github")) is None


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


# ---------------------------------------------------------------------------
# Round-2 review fixes on #408: priors from the store's own ledger, kindless
# executions stay FOUND, accurate UNKNOWN reasons, single-quoted status.
# ---------------------------------------------------------------------------
def test_reconstruct_priors_come_from_item_executions_and_merge_digests():
    """agy P1 / claude P2 on #408: deriving priors only from prior_digests
    reported every production run as execution #1. The store's ledger is
    primary; digests remain an additional source; order is by sequence,
    duplicates collapse."""
    item = wc.WorkItem(
        work_item_id="wi-1",
        state="in-progress",
        executions=(
            wc.ExecutionRef(execution_id="e2", sequence=2),
            wc.ExecutionRef(execution_id="e1", sequence=1),
        ),
    )
    state = wc.reconstruct_canonical_state(item, None, ({"execution_id": "e2"}, {"execution_id": "e3"}))
    assert state.prior_execution_ids == ("e1", "e2", "e3")

    bare = wc.reconstruct_canonical_state(item, None, ())
    assert bare.prior_execution_ids == ("e1", "e2")
    assert "prior_digests" not in bare.reconstructed_from


def test_kindless_executions_still_classify_found():
    """claude P2 / agy P2 on #408: an absent surface/actor block is an
    execution predating the fields (or dev_loop's own seed) — it must not
    turn the whole item UNKNOWN."""
    item = wc.WorkItem(
        work_item_id="wi-1",
        state="in-progress",
        executions=(wc.ExecutionRef(execution_id="e1", sequence=1),),
    )
    assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_FOUND

    pigeon = wc.WorkItem(
        work_item_id="wi-1",
        state="in-progress",
        executions=(
            wc.ExecutionRef(execution_id="e1", sequence=1, surface=wc.SurfaceRef(kind="carrier-pigeon")),
        ),
    )
    assert wc.work_item_verdict_for(pigeon) == wc.WORK_ITEM_UNKNOWN


def test_unknown_reason_names_the_actual_cause():
    valid_state_bad_surface = wc.WorkItem(
        work_item_id="wi-1",
        state="open",
        executions=(
            wc.ExecutionRef(execution_id="e1", sequence=1, surface=wc.SurfaceRef(kind="carrier-pigeon")),
        ),
    )
    reason = wc.work_item_unknown_reason(valid_state_bad_surface)
    assert "carrier-pigeon" in reason and "'open'" not in reason

    bad_state = wc.WorkItem(work_item_id="wi-1", state="limbo")
    assert "limbo" in wc.work_item_unknown_reason(bad_state)


def test_status_scalar_reads_single_and_double_quotes(tmp_path: Path):
    for raw in ('status: accepted\n', 'status: "accepted"\n', "status: 'accepted'\n"):
        proposal_dir = tmp_path / raw.replace(":", "").replace(" ", "").replace('"', "d").replace("'", "s")
        proposal_dir.mkdir()
        (proposal_dir / ".status.yaml").write_text(raw, encoding="utf-8")
        item = wc.WorkItem(work_item_id="wi-1", state="open")
        state = wc.reconstruct_canonical_state(item, proposal_dir, ())
        assert state.prior_status == "accepted", raw


def test_kindless_surface_and_actor_blocks_parse_as_absent_not_malformed():
    """#408 round 4 (agy P2): a record serialized from a default
    SurfaceRef()/ActorRef() — dev_loop's own seed — arrives as
    `{"kind": ""}` (or `{}`). That is a legitimately absent block, not a
    malformed payload: one such execution must not abort the whole WorkItem
    parse into WORK_ITEM_UNKNOWN."""
    payload = {
        "work_item_id": "wi-1",
        "state": "open",
        "executions": [
            {"execution_id": "e1", "sequence": 1, "surface": {"kind": ""}, "actor": {}},
        ],
    }
    item = wc.WorkItem.from_payload(payload)
    assert item is not None
    assert item.executions[0].surface == wc.SurfaceRef()
    assert item.executions[0].actor == wc.ActorRef()
    assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_FOUND

    # PRESENT but genuinely malformed still refuses the record.
    for bad in ({"kind": 5}, "github", ["github"]):
        assert wc.WorkItem.from_payload({**payload, "executions": [
            {"execution_id": "e1", "sequence": 1, "surface": bad},
        ]}) is None


def test_out_of_vocabulary_origin_is_unknown_with_a_named_reason():
    """#408 round 4 (agy P3): `origin.kind` seals as `origin_surface`, which
    ContextSnapshot.validate() rejects out of vocabulary — so the verdict
    must classify it UNKNOWN here instead of letting the seal crash at
    context mode `on`. Empty origin stays FOUND (a work item predating the
    field)."""
    bad_origin = wc.WorkItem(
        work_item_id="wi-1", state="open", origin=wc.SurfaceRef(kind="slack")
    )
    assert wc.work_item_verdict_for(bad_origin) == wc.WORK_ITEM_UNKNOWN
    assert "slack" in wc.work_item_unknown_reason(bad_origin)
    assert "origin" in wc.work_item_unknown_reason(bad_origin)

    no_origin = wc.WorkItem(work_item_id="wi-1", state="open")
    assert wc.work_item_verdict_for(no_origin) == wc.WORK_ITEM_FOUND
