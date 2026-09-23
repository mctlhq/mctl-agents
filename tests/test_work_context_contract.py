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
import json
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


# Real mctl-api responses: captured from mctl-api's own work-items handlers
# (GetWorkItem / ListWorkItemExecutions at mctl-api 56d73f2, Postgres store),
# not written by hand, so the mirror is pinned to what mctl-api serves.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workitem"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _view(**item_overrides):
    view = _fixture("get-active-resumed.json")
    view["work_item"].update(item_overrides)
    return view


def test_real_view_parses_with_v1_field_mapping():
    view = _fixture("get-active-resumed.json")
    item = wc.record_of(view)
    assert item is not None
    assert item.work_item_id == view["work_item"]["id"]
    assert item.state == "active"
    assert item.state_version == 3 and item.revision == "3"
    assert item.origin == wc.SurfaceRef(kind="telegram")
    assert item.issue_url == "https://github.com/mctlhq/mctl-telegram/issues/443"
    assert item.executions == ()  # the view carries none; the client reads the route
    assert wc.latest_execution_id_of(view) == view["latest_execution"]["id"]
    assert wc.latest_execution_id_of(_fixture("get-active-no-executions.json")) == ""


def test_every_real_state_fixture_is_found_and_terminal_matches_mctl_api():
    for name, state, terminal in (
        ("get-active-no-executions.json", "active", False),
        ("get-waiting.json", "waiting", False),
        ("get-active-resumed.json", "active", False),
        ("get-completed.json", "completed", True),
    ):
        answer = wc.answer_from(200, _fixture(name))
        assert answer.verdict == wc.WORK_ITEM_FOUND, (name, answer.reason)
        assert answer.item.state == state
        assert (answer.item.state in wc.TERMINAL_WORK_ITEM_STATES) is terminal


def test_states_and_terminal_set_are_mctl_api_workitem_v1():
    assert wc.WORK_ITEM_STATES == {"active", "waiting", "completed", "superseded", "archived"}
    assert wc.TERMINAL_WORK_ITEM_STATES == {"completed", "superseded", "archived"}
    assert "resumed" not in wc.WORK_ITEM_STATES  # an event kind only
    for state in sorted(wc.WORK_ITEM_STATES):
        assert wc.answer_from(200, _view(state=state)).verdict == wc.WORK_ITEM_FOUND


def test_pre_v1_mirror_shape_and_old_states_are_never_found():
    old_shape = {
        "work_item_id": "wi-1", "revision": "1", "state": "open",
        "issue_url": "https://github.com/mctlhq/mctl-agents/issues/267", "executions": [],
    }
    assert wc.answer_from(200, old_shape).verdict == wc.WORK_ITEM_UNKNOWN
    for state in ("open", "in-progress", "completed-with-followup", "abandoned", "resumed"):
        answer = wc.answer_from(200, _view(state=state))
        assert answer.verdict == wc.WORK_ITEM_UNKNOWN, state
        assert state in answer.reason


def test_unknown_schema_version_fails_closed():
    for schema in (None, "", "workitem/v2", "workitem/v0"):
        view = _fixture("get-active-resumed.json")
        if schema is None:
            del view["schema_version"]
        else:
            view["schema_version"] = schema
        answer = wc.answer_from(200, view)
        assert answer.verdict == wc.WORK_ITEM_UNKNOWN and "schema_version" in answer.reason, schema
    inner = _view(schema_version="workitem/v2")
    assert wc.answer_from(200, inner).verdict == wc.WORK_ITEM_UNKNOWN


def test_view_without_a_usable_record_is_unknown():
    for broken in (
        {"schema_version": "workitem/v1"},
        _view(id=""),
        _view(state=""),
        _view(state_version=0),
        _view(state_version="3"),
        _view(state_version=True),
        _view(origin_surface=7),
    ):
        assert wc.answer_from(200, broken).verdict == wc.WORK_ITEM_UNKNOWN, broken
    assert wc.answer_from(200, {"ok": True}).verdict == wc.WORK_ITEM_UNKNOWN
    # The envelope's state_version must be a real int, not a bool that
    # happens to compare equal.
    true_envelope = _view(state_version=1)
    true_envelope["state_version"] = True
    assert wc.answer_from(200, true_envelope).verdict == wc.WORK_ITEM_UNKNOWN
    # A bool is not a state_version even when the envelope agrees with it.
    boolean = _view(state_version=True)
    boolean["state_version"] = True
    assert wc.answer_from(200, boolean).verdict == wc.WORK_ITEM_UNKNOWN


def test_envelope_state_version_must_be_the_items_own():
    view = _fixture("get-active-resumed.json")
    view["state_version"] = 2
    answer = wc.answer_from(200, view)
    assert answer.verdict == wc.WORK_ITEM_UNKNOWN and "state_version" in answer.reason
    del view["state_version"]
    assert wc.answer_from(200, view).verdict == wc.WORK_ITEM_UNKNOWN


def test_external_key_is_an_issue_url_only_when_it_is_one():
    assert wc.record_of(_view(external_key="tg:chat:42")).issue_url == ""
    assert wc.record_of(_view(external_key="https://github.com/mctlhq/x/pull/3")).issue_url == ""
    assert wc.record_of(_view(external_key="https://github.com/mctlhq/x/issues/3#c")).issue_url == ""
    item = wc.record_of(_view(external_key=None))
    assert item.issue_url == "" and item.external_key == ""
    # Whatever run_issue_investigator's own parser calls an issue URL is one
    # here too: http(s) and an optional trailing slash.
    for url in ("https://github.com/mctlhq/x/issues/3/", "http://github.com/mctlhq/x/issues/3"):
        assert wc.record_of(_view(external_key=url)).issue_url == url
    assert wc.record_of(_view(external_key="https://github.com/örg/x/issues/3")).issue_url == ""


def test_issue_url_rule_is_the_investigators_own():
    """Pinned against run_issue_investigator's pattern text, which this
    stdlib-only package cannot import."""
    investigator = Path(__file__).resolve().parent.parent / "orchestrator" / "run_issue_investigator.py"
    src = investigator.read_text(encoding="utf-8")
    urls = ("https://github.com/a/b/issues/1", "http://github.com/a/b/issues/1/", "https://github.com/örg/b/issues/1",
            "https://github.com/a/b/pull/1", "https://github.com/a/b/issues/1#x", "https://gitlab.com/a/b/issues/1")
    import re as _re

    m = _re.search(r"_ISSUE_URL_RE = re\.compile\(\s*r\"(.+?)\"\s*\)", src, _re.S)
    assert m, "run_issue_investigator._ISSUE_URL_RE not found, or it now takes flags"
    theirs = _re.compile(m.group(1))
    for url in urls:
        assert bool(theirs.match(url)) == bool(wc._ISSUE_URL_RE.match(url)), url


def test_a_null_origin_surface_is_no_origin_not_a_broken_record():
    item = wc.record_of(_view(origin_surface=None))
    assert item is not None and item.origin == wc.SurfaceRef()
    assert wc.answer_from(200, _view(origin_surface=None)).verdict == wc.WORK_ITEM_FOUND


def test_out_of_vocabulary_origin_surface_is_unknown():
    answer = wc.answer_from(200, _view(origin_surface="mcp"))
    assert answer.verdict == wc.WORK_ITEM_UNKNOWN
    assert "mcp" in answer.reason and "origin" in answer.reason
    assert wc.answer_from(200, _view(origin_surface="")).verdict == wc.WORK_ITEM_FOUND


# ---------------------------------------------------------------------------
# The executions route
# ---------------------------------------------------------------------------


def test_real_executions_listing_maps_attempt_to_sequence():
    listing = _fixture("executions-two.json")
    wid = listing["executions"][0]["work_item_id"]
    executions, why = wc.executions_from(200, listing, wid)
    assert why == ""
    assert [e.execution_id for e in executions] == [e["id"] for e in listing["executions"]]
    assert [e.sequence for e in executions] == [1, 2]
    assert executions[1].temporal_workflow_id == "dev-loop-mctlhq-mctl-telegram-443-r2"
    assert wc.executions_from(200, _fixture("executions-empty.json"), wid) == ((), "")


def test_executions_listing_is_all_or_nothing():
    listing = _fixture("executions-two.json")
    wid = listing["executions"][0]["work_item_id"]

    def mutated(**overrides):
        out = json.loads(json.dumps(listing))
        out["executions"][1].update(overrides)
        return out

    for bad in (
        mutated(id=""),
        mutated(work_item_id="wi_someone-else"),
        mutated(attempt=0),
        mutated(attempt="2"),
        mutated(engine="lambda"),
        mutated(attempt=1),
        mutated(attempt=3),
        mutated(id=listing["executions"][0]["id"]),
        {**listing, "schema_version": "workitem/v2"},
        {"schema_version": "workitem/v1"},
    ):
        executions, why = wc.executions_from(200, bad, wid)
        assert executions is None and why, bad
    # A phase this mirror has not seen is not a reason to refuse the ledger.
    executions, why = wc.executions_from(200, mutated(phase="Paused"), wid)
    assert executions is not None and len(executions) == 2, why
    executions, why = wc.executions_from(503, {"error": "unavailable"}, wid)
    assert executions is None and "unavailable" in why


def test_a_truncated_ledger_is_refused_even_with_the_latest_entry_present():
    listing = _fixture("executions-two.json")
    wid = listing["executions"][0]["work_item_id"]
    newest_only = {**listing, "executions": listing["executions"][1:]}
    executions, why = wc.executions_from(200, newest_only, wid)
    assert executions is None and "incomplete" in why


def test_argo_execution_carries_no_temporal_workflow_id():
    listing = _fixture("executions-two.json")
    listing["executions"][0]["engine"] = "argo"
    wid = listing["executions"][0]["work_item_id"]
    executions, _ = wc.executions_from(200, listing, wid)
    assert executions[0].temporal_workflow_id == ""


# ---------------------------------------------------------------------------
# HTTP-response classification (also exercised through client.py in
# tests/test_work_context_client.py — this file pins the pure function).
# ---------------------------------------------------------------------------


def test_answer_from_real_404_is_absent():
    assert wc.answer_from(404, _fixture("get-not-found.json")).verdict == wc.WORK_ITEM_ABSENT


def test_answer_from_404_without_the_work_items_code_is_unknown():
    for payload in ({}, {"error": "not found"}, {"error": "x", "code": "epic_not_found"}):
        assert wc.answer_from(404, payload).verdict == wc.WORK_ITEM_UNKNOWN, payload


def test_answer_from_409_is_conflict():
    answer = wc.answer_from(409, {"error": "conflict", "code": "state_version_conflict"})
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
    item = wc.WorkItem(work_item_id="wi-1", state="active", service="mctl-agents", slug="issue-267-x")
    state = wc.reconstruct_canonical_state(item, None, ())
    assert state.work_item_id == "wi-1"
    assert state.state == "active"
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

    item = wc.WorkItem(work_item_id="wi-1", state="active", service="mctl-agents", slug="issue-267-x")
    state = wc.reconstruct_canonical_state(item, proposal_dir, ())

    assert state.prior_status == "accepted"
    assert set(state.artifacts_present) == {"requirements.md", "design.md", ".status.yaml"}
    assert "proposal_dir" in state.reconstructed_from


def test_reconstruct_canonical_state_carries_prior_execution_ids_from_digests():
    item = wc.WorkItem(work_item_id="wi-1", state="active")
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
    item = wc.WorkItem(work_item_id="wi-1", state="active")
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
        state="active",
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
        state="active",
        executions=(wc.ExecutionRef(execution_id="e1", sequence=1),),
    )
    assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_FOUND

    pigeon = wc.WorkItem(
        work_item_id="wi-1",
        state="active",
        executions=(
            wc.ExecutionRef(execution_id="e1", sequence=1, surface=wc.SurfaceRef(kind="carrier-pigeon")),
        ),
    )
    assert wc.work_item_verdict_for(pigeon) == wc.WORK_ITEM_UNKNOWN


def test_unknown_reason_names_the_actual_cause():
    valid_state_bad_surface = wc.WorkItem(
        work_item_id="wi-1",
        state="active",
        executions=(
            wc.ExecutionRef(execution_id="e1", sequence=1, surface=wc.SurfaceRef(kind="carrier-pigeon")),
        ),
    )
    reason = wc.work_item_unknown_reason(valid_state_bad_surface)
    assert "carrier-pigeon" in reason and "'active'" not in reason

    bad_state = wc.WorkItem(work_item_id="wi-1", state="limbo")
    assert "limbo" in wc.work_item_unknown_reason(bad_state)


def test_status_scalar_reads_single_and_double_quotes(tmp_path: Path):
    for raw in ('status: accepted\n', 'status: "accepted"\n', "status: 'accepted'\n"):
        proposal_dir = tmp_path / raw.replace(":", "").replace(" ", "").replace('"', "d").replace("'", "s")
        proposal_dir.mkdir()
        (proposal_dir / ".status.yaml").write_text(raw, encoding="utf-8")
        item = wc.WorkItem(work_item_id="wi-1", state="active")
        state = wc.reconstruct_canonical_state(item, proposal_dir, ())
        assert state.prior_status == "accepted", raw


def test_kindless_surface_and_actor_blocks_parse_as_absent_not_malformed():
    """#408 round 4 (agy P2): a record serialized from a default
    SurfaceRef()/ActorRef() — dev_loop's own seed — arrives as
    `{"kind": ""}` (or `{}`). That is a legitimately absent block, not a
    malformed payload, in the dev-loop's local execution shape."""
    ref = wc.ExecutionRef.from_payload(
        {"execution_id": "e1", "sequence": 1, "surface": {"kind": ""}, "actor": {}}
    )
    assert ref is not None
    assert ref.surface == wc.SurfaceRef()
    assert ref.actor == wc.ActorRef()
    item = wc.WorkItem(work_item_id="wi-1", state="active", executions=(ref,))
    assert wc.work_item_verdict_for(item) == wc.WORK_ITEM_FOUND

    # PRESENT but genuinely malformed still refuses the record.
    for bad in ({"kind": 5}, "github", ["github"]):
        assert wc.ExecutionRef.from_payload({"execution_id": "e1", "sequence": 1, "surface": bad}) is None


def test_out_of_vocabulary_origin_is_unknown_with_a_named_reason():
    """#408 round 4 (agy P3): `origin.kind` seals as `origin_surface`, which
    ContextSnapshot.validate() rejects out of vocabulary — so the verdict
    must classify it UNKNOWN here instead of letting the seal crash at
    context mode `on`. Empty origin stays FOUND (a work item predating the
    field)."""
    bad_origin = wc.WorkItem(
        work_item_id="wi-1", state="active", origin=wc.SurfaceRef(kind="slack")
    )
    assert wc.work_item_verdict_for(bad_origin) == wc.WORK_ITEM_UNKNOWN
    assert "slack" in wc.work_item_unknown_reason(bad_origin)
    assert "origin" in wc.work_item_unknown_reason(bad_origin)

    no_origin = wc.WorkItem(work_item_id="wi-1", state="active")
    assert wc.work_item_verdict_for(no_origin) == wc.WORK_ITEM_FOUND
