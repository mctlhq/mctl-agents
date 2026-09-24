"""Persisting sealed ContextSnapshots to mctl-api (mctlhq/mctl-agents#431)."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator import context_assembly as ca
from orchestrator import context_snapshot as cs
from orchestrator import policy_checkpoint as pc
from orchestrator.run_issue_investigator import IssueData, IssueRef
from orchestrator.work_context import snapshots as ws
from orchestrator.work_context.client import WorkItemClient, WorkItemUnavailable, _HTTPResult

WID = "wi_0a70acb2-94c3-440b-bd1f-a898e8bee4a5"
E1 = "we_11111111-1111-4111-8111-111111111111"
E2 = "we_22222222-2222-4222-8222-222222222222"


def _work_context(**overrides) -> cs.WorkContextRef:
    base = cs.WorkContextRef(
        work_item_id=WID, work_item_revision="2", execution_id=E2, execution_sequence=2, prior_execution_ids=(E1,),
    )
    return replace(base, **overrides)


def _assemble_kwargs(tmp_path, work_context, client):
    issue = IssueData(
        ref=IssueRef(owner="mctlhq", repo="mctl-agents", number=431, url="https://github.com/mctlhq/mctl-agents/issues/431"),
        title="t", body="b", state="OPEN",
    )
    return dict(
        mode="shadow", issue=issue, issue_url=issue.ref.url, full_repo="mctlhq/mctl-agents",
        repo_dir=tmp_path / "repo", target_repo_sha="a" * 40, proposal_dir=tmp_path / "proposal",
        service="mctl-agents", slug="issue-431-x", prompt_template="PROMPT", resolver_mode="legacy",
        legacy_model="claude-sonnet-5", legacy_allowed_tools=("Read",), legacy_budget_usd=8.0,
        work_context=work_context, work_item_client=client,
    )


def _sealed(tmp_path, work_context=None) -> cs.ContextSnapshot:
    """A really sealed snapshot, assembled with the rollout off (no store)."""
    saved = os.environ.pop("WORK_CONTEXT_ROLLOUT_MODE", None)
    try:
        result = ca.assemble_investigator_context(
            **_assemble_kwargs(tmp_path, work_context or _work_context(), _NoCalls())
        )
    finally:
        if saved is not None:
            os.environ["WORK_CONTEXT_ROLLOUT_MODE"] = saved
    assert result is not None
    return result.snapshot


class _NoCalls:
    def execution_snapshot(self, *a):
        raise AssertionError("no store call expected")

    seal_snapshot = execution_snapshot


class _Store:
    """An in-memory stand-in for mctl-api's seal/read routes."""

    def __init__(self, *, seal_status=None, stored=None, prior=None):
        self.sealed: list[tuple[str, str, dict]] = []
        self.seal_status = seal_status
        self.stored = stored  # bytes already sealed for the execution
        self.prior = prior    # {"id": ...} of the prior execution's snapshot

    def execution_snapshot(self, work_item_id, execution_id):
        if execution_id == E1 and self.prior:
            return _read(200, _env({**self.prior, "execution_id": E1}), execution_id=E1)
        if execution_id == E2 and self.stored is not None:
            snap = {"id": "cs_stored", "execution_id": E2, "content_hash": cs.hash_bytes(self.stored),
                    "canonical_b64": base64.b64encode(self.stored).decode()}
            return _read(200, _env(snap), execution_id=E2)
        return _read(404, {"code": "snapshot_not_found"}, execution_id=execution_id)

    def seal_snapshot(self, work_item_id, execution_id, body):
        self.sealed.append((work_item_id, execution_id, body))
        if self.seal_status is not None:
            return _seal(self.seal_status[0], self.seal_status[1],
                                       content_hash=body["content_hash"], execution_id=execution_id)
        if self.stored is not None and cs.hash_bytes(self.stored) != body["content_hash"]:
            return _seal(409, {"code": "snapshot_divergence", "error": "x"},
                                       content_hash=body["content_hash"], execution_id=execution_id)
        snap = {"id": "cs_new", "execution_id": execution_id, "content_hash": body["content_hash"]}
        return _seal(
            201, _env(snap), content_hash=body["content_hash"], execution_id=execution_id
        )


def _seal(status: int, payload: dict, *, content_hash: str, execution_id: str) -> ws.SnapshotAnswer:
    """`answer_from_seal` for a snapshot of `WID`."""
    return ws.answer_from_seal(status, payload, work_item_id=WID, content_hash=content_hash, execution_id=execution_id)


def _read(status: int, payload: dict, *, execution_id: str) -> ws.SnapshotAnswer:
    """`answer_from_read` for a snapshot of `WID`."""
    return ws.answer_from_read(status, payload, work_item_id=WID, execution_id=execution_id)


def _env(snap: dict) -> dict:
    """A `workitem/v1` snapshot answer, as mctl-api shapes it (of `WID`
    unless the snapshot says otherwise)."""
    return {"schema_version": "workitem/v1",
            "snapshot": {"schema_version": "workitem/v1", "work_item_id": WID, **snap}}


FIXTURES = Path(__file__).parent / "fixtures" / "workitem"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def observe(monkeypatch):
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "observe")


def test_seal_body_carries_the_exact_canonical_document(tmp_path):
    snap = _sealed(tmp_path, _work_context(resumed_from_snapshot_id="cs_prior"))
    body = ws.seal_body(snap, snap.work_context)
    raw = base64.b64decode(body["canonical_b64"])
    assert raw == cs.canonical_json(snap.to_dict())
    assert body["content_hash"] == cs.hash_bytes(raw)
    assert json.loads(raw)["strategy"] == snap.strategy.to_dict()  # the version travels in the bytes
    assert (body["execution_sequence"], body["strategy"], body["strategy_version"]) == (
        2, snap.strategy.name, snap.strategy.version)
    assert (body["prior_execution_id"], body["prior_snapshot_id"]) == (E1, "cs_prior")
    # Local correlation ids are never sent as store references.
    other = ws.seal_body(snap, _work_context(prior_execution_ids=("sha-local",), resumed_from_snapshot_id="sha256:x"))
    assert "prior_execution_id" not in other and "prior_snapshot_id" not in other


def test_seal_answers_are_classified_and_a_2xx_must_describe_what_was_sent():
    ok = _env({"id": "cs_1", "execution_id": E2, "content_hash": "sha256:a"})
    assert _seal(201, ok, content_hash="sha256:a", execution_id=E2).verdict == ws.SNAPSHOT_SEALED
    assert _seal(200, ok, content_hash="sha256:a", execution_id=E2).verdict == ws.SNAPSHOT_REPLAYED
    for status, payload, hash_, eid in (
        (201, ok, "sha256:b", E2),                                   # other bytes
        (201, ok, "sha256:a", E1),                                   # other execution
        (201, _env({**ok["snapshot"], "id": "x"}), "sha256:a", E2),
        (201, _env({**ok["snapshot"], "id": "cs_" + "a" * 300}), "sha256:a", E2),
        (201, {**ok, "schema_version": "workitem/v2"}, "sha256:a", E2),
        (201, {}, "sha256:a", E2),
        (500, {"error": "x"}, "sha256:a", E2),
    ):
        assert _seal(status, payload, content_hash=hash_, execution_id=eid).verdict == ws.SNAPSHOT_UNKNOWN
    assert _seal(409, {"code": "snapshot_divergence"}, content_hash="h", execution_id=E2).verdict == (
        ws.SNAPSHOT_DIVERGED)
    for code in ("prior_snapshot_invalid", "snapshot_writer_forbidden", "invalid_request"):
        answer = _seal(409, {"code": code}, content_hash="h", execution_id=E2)
        assert answer.verdict == ws.SNAPSHOT_REFUSED


def test_read_answers_are_classified():
    snap = {"id": "cs_1", "execution_id": E1, "content_hash": "sha256:a"}
    assert _read(200, _env(snap), execution_id=E1).snapshot_id == "cs_1"
    assert _read(200, _env(snap), execution_id=E2).verdict == ws.SNAPSHOT_UNKNOWN
    # An id the sealed document could not carry is not an answer.
    assert _read(200, _env({**snap, "id": "cs_" + "a" * 300}), execution_id=E1).verdict == (
        ws.SNAPSHOT_UNKNOWN)
    assert _read(404, {"code": "snapshot_not_found"}, execution_id=E1).verdict == ws.SNAPSHOT_ABSENT
    # Any other 404 (the work item itself, or the execution) is not "none sealed".
    assert _read(404, {"code": "work_item_not_found"}, execution_id=E1).verdict == ws.SNAPSHOT_UNKNOWN


def test_nothing_is_sent_below_observe_or_for_a_local_execution(tmp_path, monkeypatch):
    store = _Store()
    monkeypatch.delenv("WORK_CONTEXT_ROLLOUT_MODE", raising=False)
    ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), store))
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "observe")
    ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(execution_id="sha-local"), store))
    ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, None, store))
    assert store.sealed == []


def test_observe_links_the_prior_snapshot_and_persists(tmp_path, observe, capsys):
    store = _Store(prior={"id": "cs_prior", "content_hash": "sha256:p"})
    result = ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), store))
    # The pointer is set BEFORE sealing, so it is part of the sealed content.
    assert result.snapshot.work_context.resumed_from_snapshot_id == "cs_prior"
    [(wid, eid, body)] = store.sealed
    assert (wid, eid, body["prior_snapshot_id"]) == (WID, E2, "cs_prior")
    assert json.loads(base64.b64decode(body["canonical_b64"]))["work_context"]["resumed_from_snapshot_id"] == "cs_prior"
    logged = [json.loads(line.split(" ", 1)[1]) for line in capsys.readouterr().out.splitlines()
              if line.startswith("WORK_CONTEXT_SNAPSHOT ")]
    assert [(e["step"], e["verdict"]) for e in logged] == [
        ("resumed_from", ws.SNAPSHOT_REPLAYED), ("persist", ws.SNAPSHOT_SEALED)]


def test_a_retry_with_the_same_content_is_not_a_divergence(tmp_path, observe):
    first = _sealed(tmp_path)
    stored = cs.canonical_json({**first.to_dict(), "created_at": "2020-01-01T00:00:00Z"})
    answer = ws.persist(first, _Store(stored=stored))
    assert answer.verdict == ws.SNAPSHOT_REPLAYED and answer.snapshot_id == "cs_stored"


def test_a_different_context_for_the_same_execution_is_a_divergence(tmp_path, monkeypatch):
    snap = _sealed(tmp_path)
    other = cs.canonical_json({**snap.to_dict(), "sources": [], "content_hash": "sha256:other"})
    assert ws.persist(snap, _Store(stored=other)).verdict == ws.SNAPSHOT_DIVERGED

    # At observe it is logged; from enforce up it blocks the run.
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "observe")
    ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), _Store(stored=other)))
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "enforce")
    monkeypatch.setenv("WORK_CONTEXT_REQUIRED", "false")  # a divergence blocks even under break-glass
    with pytest.raises(ca.SnapshotNotPersisted, match=ws.SNAPSHOT_DIVERGED):
        ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), _Store(stored=other)))


def test_an_unreachable_store_blocks_only_where_unknown_blocks(tmp_path, monkeypatch):
    down = _Store(seal_status=(503, {"error": "down"}))
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "enforce")
    with pytest.raises(ca.SnapshotNotPersisted, match=ws.SNAPSHOT_UNKNOWN):
        ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), down))
    monkeypatch.setenv("WORK_CONTEXT_REQUIRED", "false")
    ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), down))
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "observe")
    monkeypatch.delenv("WORK_CONTEXT_REQUIRED")
    ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), down))


def test_client_seal_goes_through_the_policy_checkpoint(monkeypatch):
    client = WorkItemClient(base_url="https://api.example", token="t")  # noqa: S106 — a test token
    sent: list[tuple[str, str, dict]] = []

    def _request(method, path, payload=None):
        sent.append((method, path, payload))
        snap = {"id": "cs_1", "execution_id": E2, "content_hash": payload["content_hash"]}
        return _HTTPResult(201, _env(snap))

    monkeypatch.setattr(client, "_request", _request)
    body = {"content_hash": "sha256:a", "canonical_b64": "e30="}
    assert client.seal_snapshot(WID, E2, body).verdict == ws.SNAPSHOT_SEALED
    assert sent == [("POST", f"/api/v1/work-items/{WID}/executions/{E2}/snapshot", body)]

    # A refusal sends nothing.
    monkeypatch.setattr(pc, "BUILTIN_POLICY", pc.Policy(version="deny-all", rules=()))
    monkeypatch.setattr(pc.checkpoint, "__kwdefaults__", {**pc.checkpoint.__kwdefaults__, "policy": pc.BUILTIN_POLICY})
    assert client.seal_snapshot(WID, E2, body).verdict == ws.SNAPSHOT_REFUSED
    assert len(sent) == 1


def test_the_seal_rule_allows_only_the_seal():
    assert pc.checkpoint(pc.MCTL_WORK_ITEM_WRITE, ws.SEAL_SNAPSHOT_OPERATION, WID, {"a": 1}).code == pc.CODE_ALLOWED
    assert pc.checkpoint(pc.MCTL_WORK_ITEM_WRITE, "transition:complete", WID, {"a": 1}).code == pc.CODE_NO_RULE


def test_persist_itself_never_sends_a_local_execution(tmp_path):
    for work_context in (_work_context(execution_id="sha-local"), None):
        snap = replace(_sealed(tmp_path), work_context=work_context)
        assert ws.persist(snap, _NoCalls()).verdict == ws.SNAPSHOT_SKIPPED
    linked, answer = ws.resumed_from(_work_context(execution_id="sha-local"), _NoCalls())
    assert answer.verdict == ws.SNAPSHOT_SKIPPED and linked.resumed_from_snapshot_id is None


@pytest.mark.parametrize("read", ["down", "undecodable"])
def test_an_unverifiable_divergence_is_unknown_not_diverged(tmp_path, monkeypatch, read):
    snap = _sealed(tmp_path)
    store = _Store(stored=b'{"other":1}')
    if read == "down":
        store.execution_snapshot = lambda *a: _read(503, {"error": "down"}, execution_id=E2)
    else:
        bad = {"id": "cs_stored", "execution_id": E2, "content_hash": "sha256:x", "canonical_b64": "not base64!"}
        store.execution_snapshot = lambda *a: _read(200, _env(bad), execution_id=E2)
    assert ws.persist(snap, store).verdict == ws.SNAPSHOT_UNKNOWN
    # So the break-glass governs it, as for any unreachable store.
    monkeypatch.setenv("WORK_CONTEXT_ROLLOUT_MODE", "enforce")
    monkeypatch.setenv("WORK_CONTEXT_REQUIRED", "false")
    ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), store))


def test_real_mctl_api_answers_classify_as_intended():
    """Captured from mctl-api#362's handlers (seal twice, diverge, read, absent)."""
    created = _fixture("snapshot-seal-created.json")
    snap = created["snapshot"]
    raw = base64.b64decode(snap["canonical_b64"])
    assert snap["content_hash"] == cs.hash_bytes(raw)  # the store's hash rule is ours
    kw = {"content_hash": snap["content_hash"], "execution_id": snap["execution_id"],
          "work_item_id": snap["work_item_id"]}
    assert ws.answer_from_seal(201, created, **kw).verdict == ws.SNAPSHOT_SEALED
    assert ws.answer_from_seal(200, _fixture("snapshot-seal-replayed.json"), **kw).verdict == ws.SNAPSHOT_REPLAYED
    assert ws.answer_from_seal(409, _fixture("snapshot-seal-diverged.json"), **kw).verdict == ws.SNAPSHOT_DIVERGED
    read = ws.answer_from_read(
        200, _fixture("snapshot-read.json"), work_item_id=snap["work_item_id"], execution_id=snap["execution_id"]
    )
    assert (read.verdict, read.snapshot_id, read.stored_document) == (ws.SNAPSHOT_REPLAYED, snap["id"], json.loads(raw))
    absent = ws.answer_from_read(
        404, _fixture("snapshot-read-absent.json"), work_item_id=snap["work_item_id"], execution_id=snap["execution_id"]
    )
    assert absent.verdict == ws.SNAPSHOT_ABSENT


@pytest.mark.parametrize("first_pointer,retry_pointer", [(None, "cs_prior"), ("cs_prior", None)])
def test_a_prior_lookup_that_differs_between_attempts_is_not_a_divergence(tmp_path, first_pointer, retry_pointer):
    first = _sealed(tmp_path, _work_context(resumed_from_snapshot_id=first_pointer))
    retry = _sealed(tmp_path, _work_context(resumed_from_snapshot_id=retry_pointer))
    stored = cs.canonical_json(first.to_dict())
    assert ws.persist(retry, _Store(stored=stored)).verdict == ws.SNAPSHOT_REPLAYED


def test_a_divergence_names_what_differs(tmp_path):
    snap = _sealed(tmp_path)
    other = cs.canonical_json({**snap.to_dict(), "sources": [{"x": 1}]})
    answer = ws.persist(snap, _Store(stored=other))
    assert answer.verdict == ws.SNAPSHOT_DIVERGED and "differs in: sources" in answer.reason


def test_a_divergence_inside_any_block_names_the_moved_field(tmp_path):
    """#455 item 7: every dict block is expanded one level, not only
    `work_context`."""
    snap = _sealed(tmp_path)
    doc = snap.to_dict()
    assert isinstance(doc["execution"], dict) and doc["execution"]
    field_name = sorted(doc["execution"])[0]
    moved = {**doc, "execution": {**doc["execution"], field_name: "moved"}}
    assert ws.differing_fields(moved, doc) == [f"execution.{field_name}"]
    answer = ws.persist(snap, _Store(stored=cs.canonical_json(moved)))
    assert answer.verdict == ws.SNAPSHOT_DIVERGED and f"differs in: execution.{field_name}" in answer.reason
    # A block that is a dict on one side only is named whole.
    assert ws.differing_fields({**doc, "execution": None}, doc) == ["execution"]


def test_null_on_one_side_and_absent_on_the_other_is_a_difference(tmp_path):
    """PR #492 review: presence counts inside a block, so field growth
    (an older image's document lacks a key this image emits as null) is
    never read as "same context"."""
    snap = _sealed(tmp_path)
    doc = snap.to_dict()
    grown = {**doc, "execution": {**doc["execution"], "new_field": None}}
    assert ws.differing_fields(doc, grown) == ["execution.new_field"]
    assert ws.differing_fields(grown, doc) == ["execution.new_field"]
    answer = ws.persist(snap, _Store(stored=cs.canonical_json(grown)))
    assert answer.verdict == ws.SNAPSHOT_DIVERGED and "execution.new_field" in answer.reason
    # Unequal blocks never yield an empty answer.
    for a, b in (({"x": {}}, {"x": {"k": None}}), ({"x": {"k": 1}}, {"x": {"k": 1.0, "j": None}})):
        assert ws.differing_fields(a, b), (a, b)


def test_a_replay_by_comparison_logs_both_snapshots(tmp_path, observe, capsys):
    """#455 item 9: the stored snapshot's id and hash, and this attempt's
    own, in one log line."""
    first = _sealed(tmp_path)
    stored = cs.canonical_json({**first.to_dict(), "created_at": "2020-01-01T00:00:00Z"})
    answer = ws.persist(first, _Store(stored=stored))
    assert answer.verdict == ws.SNAPSHOT_REPLAYED
    assert (answer.snapshot_id, answer.content_hash) == ("cs_stored", cs.hash_bytes(stored))
    assert answer.local_snapshot_id == first.snapshot_id
    assert answer.local_content_hash == cs.hash_bytes(cs.canonical_json(first.to_dict()))
    capsys.readouterr()
    ca._emit_snapshot_answer("persist", answer)
    line = json.loads(capsys.readouterr().out.split(" ", 1)[1])
    assert (line["snapshot_id"], line["local_snapshot_id"]) == ("cs_stored", first.snapshot_id)
    assert (line["content_hash"], line["local_content_hash"]) == (answer.content_hash, answer.local_content_hash)
    # Any other answer carries no local pair.
    ca._emit_snapshot_answer("persist", ws.SnapshotAnswer(ws.SNAPSHOT_SEALED, snapshot_id="cs_1"))
    assert "local_snapshot_id" not in json.loads(capsys.readouterr().out.split(" ", 1)[1])


def test_a_seal_answer_about_another_work_item_is_unknown():
    """The work item check on the seal side too (PR #492 review)."""
    ok = _env({"id": "cs_1", "execution_id": E2, "content_hash": "sha256:a"})
    assert _seal(201, ok, content_hash="sha256:a", execution_id=E2).verdict == ws.SNAPSHOT_SEALED
    other = _env({"id": "cs_1", "execution_id": E2, "content_hash": "sha256:a", "work_item_id": "wi_else"})
    for status in (200, 201):
        assert _seal(status, other, content_hash="sha256:a", execution_id=E2).verdict == ws.SNAPSHOT_UNKNOWN


def test_a_read_about_another_work_item_is_unknown():
    """#455 item 6: "asked for X, the store answered Y"."""
    snap = {"id": "cs_1", "execution_id": E1, "content_hash": "sha256:a"}
    assert _read(200, _env(snap), execution_id=E1).verdict == ws.SNAPSHOT_REPLAYED
    for other in ({**snap, "work_item_id": "wi_someone-else"}, {**snap, "work_item_id": None}):
        answer = _read(200, _env(other), execution_id=E1)
        assert answer.verdict == ws.SNAPSHOT_UNKNOWN, other
    missing = {"schema_version": "workitem/v1", "snapshot": {"schema_version": "workitem/v1", **snap}}
    assert _read(200, missing, execution_id=E1).verdict == ws.SNAPSHOT_UNKNOWN


def test_an_oversized_prior_id_is_ignored_and_never_breaks_assembly(tmp_path, observe):
    store = _Store(prior={"id": "cs_" + "a" * 300, "content_hash": "sha256:p"})
    result = ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), store))
    assert result.snapshot.work_context.resumed_from_snapshot_id is None
    result.snapshot.validate()


def test_client_reads_a_snapshot_through_the_real_route(monkeypatch):
    client = WorkItemClient(base_url="https://api.example", token="t")  # noqa: S106 — a test token
    asked: list[tuple[str, str]] = []

    def _request(method, path, payload=None):
        asked.append((method, path))
        return _HTTPResult(404, {"code": "snapshot_not_found"})

    monkeypatch.setattr(client, "_request", _request)
    assert client.execution_snapshot("wi/1", "we 2").verdict == ws.SNAPSHOT_ABSENT
    assert asked == [("GET", "/api/v1/work-items/wi%2F1/executions/we%202/snapshot")]

    def _down(method, path, payload=None):
        raise WorkItemUnavailable("connection refused")

    monkeypatch.setattr(client, "_request", _down)
    assert client.execution_snapshot(WID, E1).verdict == ws.SNAPSHOT_UNKNOWN


def test_a_retry_at_a_later_second_is_a_replay_and_new_content_still_diverges(tmp_path, monkeypatch):
    """An engine retry of the same execution runs minutes later, so every
    source's `retrieved_at` / `freshness.observed_at` is restamped: not a
    divergence (#431 acceptance, found by
    tests/test_work_context_resume_acceptance.py). What a source observed
    still counts: a different content hash is a divergence."""
    monkeypatch.delenv("WORK_CONTEXT_ROLLOUT_MODE", raising=False)  # assemble only, no store
    t0 = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    first, retry = (
        ca.assemble_investigator_context(**_assemble_kwargs(tmp_path, _work_context(), _NoCalls()), now=now).snapshot
        for now in (t0, t0 + timedelta(seconds=90))
    )
    assert first.sources and first.sources[0].retrieved_at != retry.sources[0].retrieved_at
    answer = ws.persist(retry, _Store(stored=cs.canonical_json(first.to_dict())))
    assert answer.verdict == ws.SNAPSHOT_REPLAYED and answer.snapshot_id == "cs_stored"

    changed = first.to_dict()
    changed["sources"][0]["content_hash"] = "sha256:" + "0" * 64
    answer = ws.persist(retry, _Store(stored=cs.canonical_json(changed)))
    assert answer.verdict == ws.SNAPSHOT_DIVERGED and "differs in: sources" in answer.reason


def test_persist_documents_everything_a_retry_may_change():
    """#455 item 8: the docstring lists what `_retry_stable` drops, so the
    two cannot drift apart silently."""
    doc = ws.persist.__doc__ or ""
    for name in (*ws._RETRY_VOLATILE, "retrieved_at", "observed_at", "resumed_from_snapshot_id"):
        assert f"`{name}`" in doc or f".{name}`" in doc, name


def test_the_snapshot_log_line_imports_nothing_at_call_time():
    """#455 item 10: `json` is a module-level import of context_assembly."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ca._emit_snapshot_answer))
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)]
