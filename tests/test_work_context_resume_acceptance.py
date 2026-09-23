"""Offline end-to-end acceptance for resume snapshots (mctlhq/mctl-agents#431).

The issue's target model, driven through the real investigator path:

    WorkItem W1 ── E1 (engine ref A) ── C1
               └─ resume ── E2 (engine ref B) ── C2
                                 provenance: W1, prior E1, prior snapshot C1

`investigate()` runs for real: the rollout gate, the attach through the
policy checkpoint, the ledger read, context assembly, `seal()`, the prior
snapshot lookup and the persist. Only `WorkItemClient._request` is replaced,
by `FakeMctlApi`, an in-memory mctl-api that keeps the rules of the Go store
(mctl-api `internal/workitems/store.go` `AttachExecution` and
`internal/workitems/snapshots.go` `SealSnapshot`/`checkPrior`):

- an execution is keyed by `(engine, engine_ref)`: the same run again is the
  same `we_` id (200), a new run is a new one (201) numbered `len(ledger)+1`;
  at most one execution is non-terminal (409 `execution_active`), and an
  ended execution is never reopened (409 `invalid_transition`);
- a snapshot row is inserted once per execution and never updated; the hash
  is recomputed from the bytes on write (400) and on every read (500); the
  same bytes and claims again are a replay (200), anything else for the same
  execution is 409 `snapshot_divergence`; a prior execution or snapshot that
  is missing, or does not precede this execution, is 409
  `prior_snapshot_invalid`.

What this module does NOT prove, because it cannot be proven offline, is
listed in the #431 acceptance report: the live store, a surface-initiated
resume (mctl-api#368, mctl-agents#461), and a non-`off` rollout in
production.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator import context_snapshot as cs
from orchestrator import policy_checkpoint as pc
from orchestrator import run_issue_investigator
from orchestrator.run_issue_investigator import IssueData, IssueRef, investigate
from orchestrator.work_context import executions as ex
from orchestrator.work_context import rollout
from orchestrator.work_context import snapshots as ws
from orchestrator.work_context.client import WorkItemClient, _HTTPResult
from tests.test_run_issue_investigator import _investigate_harness

WID = "wi_431a0000-0000-4000-8000-000000000431"
NUMBER = 431
URL = f"https://github.com/mctlhq/mctl-telegram/issues/{NUMBER}"
REF_A = "mctl-agents-investigate-a1b2c"
REF_B = "mctl-agents-investigate-d3e4f"
UNKNOWN_PRIOR = "we_99999999-9999-4999-8999-999999999999"
TERMINAL_PHASES = {"Succeeded", "Failed", "Error"}
TERMINAL_STATES = {"completed", "superseded", "archived"}
SV = "workitem/v1"

ISSUE_BODY = "Body text"
NEW_COMMENT = ("IC_resume_1", "alice", "2026-09-23T11:00:00Z", "please also cover the telegram surface")


def _hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _snapshot_id_for(execution_id: str, content_hash: str) -> str:
    """mctl-api's `SnapshotIDFor`."""
    return "cs_" + hashlib.sha256(f"{execution_id}\x00{content_hash}".encode()).hexdigest()[:32]


class FakeMctlApi:
    """mctl-api's work-item, execution and snapshot routes for one work
    item, in memory, following the Go store's rules (module docstring)."""

    def __init__(self) -> None:
        self.state = "active"
        self.executions: list[dict[str, Any]] = []
        #: execution id -> the one row sealed for it. Rows are inserted, never
        #: replaced: `_insert` refuses a second row for an execution.
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.inserts = 0
        self.requests: list[tuple[str, str, dict | None]] = []
        #: Transport-level overrides: path -> an _HTTPResult to answer instead.
        self.fail: dict[tuple[str, str], _HTTPResult] = {}
        self._next = 0

    # -- helpers the tests read -------------------------------------------

    def by_ref(self, ref: str) -> dict[str, Any]:
        return next(e for e in self.executions if e["engine_ref"] == ref)

    def seals(self, execution_id: str) -> list[dict]:
        path = f"/api/v1/work-items/{WID}/executions/{execution_id}/snapshot"
        return [b for m, p, b in self.requests if m == "POST" and p == path and b is not None]

    def document(self, execution_id: str) -> dict[str, Any]:
        return json.loads(self.snapshots[execution_id]["canonical"])

    # -- the routes ---------------------------------------------------------

    def request(self, method: str, path: str, payload: dict | None = None) -> _HTTPResult:
        self.requests.append((method, path, copy.deepcopy(payload)))
        if (method, path) in self.fail:
            return self.fail[(method, path)]
        base = f"/api/v1/work-items/{WID}"
        if method == "GET" and path == base:
            return _HTTPResult(200, self._view())
        if method == "GET" and path == f"{base}/executions":
            return _HTTPResult(200, {"schema_version": SV, "executions": copy.deepcopy(self.executions)})
        if method == "POST" and path == f"{base}/executions":
            return self._attach(payload or {})
        if path.startswith(f"{base}/executions/") and path.endswith("/snapshot"):
            eid = path.split("/")[-2]
            if method == "GET":
                return self._read(eid)
            return self._seal(eid, payload or {})
        return _HTTPResult(404, {"code": "not_a_route", "error": path})

    def _view(self) -> dict[str, Any]:
        item = {"id": WID, "state": self.state, "state_version": 1, "origin_surface": "github",
                "external_key": URL, "schema_version": SV}
        view: dict[str, Any] = {"schema_version": SV, "work_item": item, "state_version": 1}
        if self.executions:
            view["latest_execution"] = copy.deepcopy(self.executions[-1])
        return view

    def _attach(self, body: dict) -> _HTTPResult:
        engine, ref, phase = body["engine"], body["engine_ref"], body.get("phase") or "Pending"
        existing = next((e for e in self.executions if (e["engine"], e["engine_ref"]) == (engine, ref)), None)
        if existing is not None:
            if existing["phase"] != phase:
                if existing["phase"] in TERMINAL_PHASES:
                    return _HTTPResult(409, {"code": "invalid_transition", "error": "already ended"})
                existing["phase"] = phase
            return _HTTPResult(200, {"schema_version": SV, "execution": copy.deepcopy(existing)})
        if self.state in TERMINAL_STATES:
            return _HTTPResult(409, {"code": "invalid_transition", "error": f"attach to {self.state}"})
        if phase not in TERMINAL_PHASES and any(e["phase"] not in TERMINAL_PHASES for e in self.executions):
            return _HTTPResult(409, {"code": "execution_active", "error": "another execution is active"})
        self._next += 1
        created = {"id": f"we_{self._next:08d}-0000-4000-8000-000000000000", "work_item_id": WID,
                   "engine": engine, "engine_ref": ref, "attempt": len(self.executions) + 1, "phase": phase,
                   "started_at": "2026-09-23T00:00:00Z"}
        self.executions.append(created)
        return _HTTPResult(201, {"schema_version": SV, "execution": copy.deepcopy(created)})

    def _execution(self, eid: str) -> dict[str, Any] | None:
        return next((e for e in self.executions if e["id"] == eid), None)

    def _served(self, row: dict[str, Any]) -> _HTTPResult | dict[str, Any]:
        # Verified on every read, as `scanSnapshot` does.
        if _hash(row["canonical"]) != row["content_hash"]:
            return _HTTPResult(500, {"code": "internal", "error": "stored bytes do not hash to the stored hash"})
        out = {k: v for k, v in row.items() if k != "canonical"}
        out["canonical_b64"] = base64.b64encode(row["canonical"]).decode("ascii")
        return {k: v for k, v in out.items() if v != ""}

    def _read(self, eid: str) -> _HTTPResult:
        row = self.snapshots.get(eid)
        if row is None:
            return _HTTPResult(404, {"code": "snapshot_not_found", "error": "context snapshot not found"})
        served = self._served(row)
        if isinstance(served, _HTTPResult):
            return served
        return _HTTPResult(200, {"schema_version": SV, "snapshot": served})

    def _by_snapshot_id(self, sid: str) -> dict[str, Any] | None:
        return next((r for r in self.snapshots.values() if r["id"] == sid), None)

    def _seal(self, eid: str, body: dict) -> _HTTPResult:
        def invalid(msg: str) -> _HTTPResult:
            return _HTTPResult(400, {"code": "invalid_request", "error": msg})

        def prior_invalid(msg: str) -> _HTTPResult:
            return _HTTPResult(409, {"code": "prior_snapshot_invalid", "error": msg})

        try:
            canonical = base64.b64decode(body.get("canonical_b64", ""), validate=True)
        except (binascii.Error, ValueError):
            return invalid("canonical_b64 is not standard base64")
        seq = body.get("execution_sequence")
        if not isinstance(seq, int) or seq < 1:
            return invalid("execution_sequence must be >= 1")
        try:
            obj = json.loads(canonical)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            return invalid("snapshot must be one JSON object")
        if body.get("content_hash") != _hash(canonical):
            return invalid("content_hash is not the sha256 of the snapshot bytes")
        prior_exec, prior_snap = body.get("prior_execution_id", ""), body.get("prior_snapshot_id", "")
        if prior_snap and not prior_snap.startswith("cs_"):
            return invalid("prior_snapshot_id must be a cs_ id")
        if prior_exec and not prior_exec.startswith("we_"):
            return invalid("prior_execution_id must be a we_ id")
        execution = self._execution(eid)
        if execution is None:
            return _HTTPResult(404, {"code": "execution_not_found", "error": "execution not found on this work item"})
        if execution["attempt"] != seq:
            return invalid(f"execution_sequence {seq} is not execution {eid}'s attempt {execution['attempt']}")
        existing = self.snapshots.get(eid)
        if existing is not None:
            if existing["content_hash"] != body["content_hash"]:
                return _HTTPResult(409, {"code": "snapshot_divergence", "error": (
                    f"execution already sealed a different context snapshot: execution {eid} sealed "
                    f"{existing['content_hash']}, not {body['content_hash']}")})
            resolved = prior_exec
            if not prior_exec and prior_snap:
                prior_row = self._by_snapshot_id(prior_snap)
                resolved = prior_row["execution_id"] if prior_row else ""
            if (existing["strategy"], existing["strategy_version"], existing["prior_execution_id"],
                    existing["prior_snapshot_id"]) != (body.get("strategy"), body.get("strategy_version"),
                                                       resolved, prior_snap):
                return _HTTPResult(409, {"code": "snapshot_divergence", "error": "different strategy or prior"})
            return _HTTPResult(200, {"schema_version": SV, "snapshot": self._served(existing)})
        # checkPrior: a missing or stale continuity reference fails explicitly.
        if prior_snap:
            prior_row = self._by_snapshot_id(prior_snap)
            if prior_row is None:
                return prior_invalid(f"no snapshot {prior_snap} on {WID}")
            if prior_exec and prior_exec != prior_row["execution_id"]:
                return prior_invalid(f"snapshot {prior_snap} belongs to {prior_row['execution_id']}, not {prior_exec}")
            prior_exec = prior_row["execution_id"]
        if prior_exec:
            pe = self._execution(prior_exec)
            if pe is None:
                return prior_invalid(f"prior execution or snapshot reference is missing or stale: "
                                     f"no execution {prior_exec} on {WID}")
            if pe["attempt"] >= execution["attempt"]:
                return prior_invalid(f"execution {prior_exec} does not precede {eid}")
        row = {
            "id": _snapshot_id_for(eid, body["content_hash"]), "work_item_id": WID, "execution_id": eid,
            "execution_sequence": execution["attempt"], "content_hash": body["content_hash"],
            "canonical": canonical, "strategy": body.get("strategy", ""),
            "strategy_version": body.get("strategy_version", ""), "prior_execution_id": prior_exec,
            "prior_snapshot_id": prior_snap, "produced_by": "service:mctl-agents",
            "created_at": "2026-09-23T12:00:00Z", "schema_version": SV,
        }
        self._insert(eid, row)
        return _HTTPResult(201, {"schema_version": SV, "snapshot": self._served(row)})

    def _insert(self, eid: str, row: dict[str, Any]) -> None:
        # The table's UNIQUE(execution_id) and its no-UPDATE trigger.
        assert eid not in self.snapshots, f"a second snapshot row for {eid}"
        self.snapshots[eid] = row
        self.inserts += 1


class _Clock(datetime):
    """`context_assembly`'s clock, so a retry can be placed at a chosen
    distance from the attempt it retries."""

    current = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def _write_triplet(repo_dir, prompt, proposal_dir):
    for name in ("requirements.md", "design.md", "tasks.md"):
        (proposal_dir / name).write_text(f"proposal {name}")


def _write_nothing(repo_dir, prompt, proposal_dir):
    return None


@pytest.fixture
def api(monkeypatch, tmp_path) -> FakeMctlApi:
    fake = FakeMctlApi()
    monkeypatch.setattr(WorkItemClient, "_request", lambda self, method, path, payload=None: fake.request(
        method, path, payload))
    for var in (ex.ENGINE_ENV_VAR, ex.ENGINE_REF_ENV_VAR, ex.WORKFLOW_NAME_ENV_VAR, ex.FINAL_ATTEMPT_ENV_VAR,
                rollout.REQUIRED_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    # `on`: a snapshot that is not persisted where the rollout says it must
    # be fails the run instead of being logged (`_assemble_context`).
    monkeypatch.setenv("ISSUE_INVESTIGATOR_CONTEXT_MODE", "on")
    monkeypatch.setattr(run_issue_investigator, "_target_repository_sha", lambda repo_dir: "a" * 40)
    monkeypatch.setattr(_Clock, "current", datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(run_issue_investigator.context_assembly, "datetime", _Clock)
    _investigate_harness(tmp_path, monkeypatch, number=NUMBER, title="Resume acceptance", agent=_write_triplet)
    return fake


@pytest.fixture
def sealed(monkeypatch) -> list[cs.ContextSnapshot | None]:
    """The snapshot every assembly sealed, in order (None when none was)."""
    seen: list[cs.ContextSnapshot | None] = []
    real = run_issue_investigator.context_assembly.assemble_investigator_context

    def capture(**kwargs):
        try:
            result = real(**kwargs)
        except Exception:
            seen.append(None)
            raise
        seen.append(result.snapshot if result is not None else None)
        return result

    monkeypatch.setattr(run_issue_investigator.context_assembly, "assemble_investigator_context", capture)
    return seen


def _new_input(monkeypatch) -> None:
    """New surface input between E1 and E2: a comment on the issue."""
    issue = IssueData(
        ref=IssueRef(owner="mctlhq", repo="mctl-telegram", number=NUMBER, url=URL),
        title="Resume acceptance", body=ISSUE_BODY, state="OPEN", comments=(NEW_COMMENT,),
    )
    monkeypatch.setattr(run_issue_investigator, "gh_issue_view", lambda url: issue)


def _snapshot_log(out: str) -> list[dict[str, Any]]:
    return [json.loads(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith("WORK_CONTEXT_SNAPSHOT ")]


def _run_one(tmp_path, monkeypatch, api: FakeMctlApi):
    """E1 under engine ref A, sealing C1."""
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, REF_A)
    result = investigate(URL, state_dir=tmp_path, work_item_id=WID, surface="github", actor_kind="human",
                         actor_id="mashkovd")
    assert result.error is None and result.skipped_reason is None, result
    e1 = api.by_ref(REF_A)
    assert e1["phase"] == "Succeeded"
    return e1["id"], api.snapshots[e1["id"]]


def _resume(tmp_path, monkeypatch, *, resume_from, **kwargs):
    """Run 2: a resume from `resume_from` under engine ref B, arriving from
    another surface as another actor."""
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, REF_B)
    return investigate(URL, state_dir=tmp_path, work_item_id=WID, resume_from_execution_id=resume_from,
                       surface="telegram", actor_kind="human", actor_id="alice", **kwargs)


# -- 1-5, 7, retry: E1 -> C1, resume -> E2 -> C2 ----------------------------


@pytest.mark.parametrize("mode", [rollout.OBSERVE, rollout.ENFORCE])
@pytest.mark.parametrize("retry_after", [timedelta(0), timedelta(seconds=90)], ids=["same-second", "90s-later"])
def test_a_resume_derives_a_new_immutable_snapshot_linked_to_the_prior_one(
    tmp_path, monkeypatch, api, sealed, capsys, mode, retry_after
):
    monkeypatch.setenv(rollout.ENV_VAR, mode)

    # 1. E1 produces C1 for W1.
    e1, c1_row = _run_one(tmp_path, monkeypatch, api)
    c1_bytes, c1_hash, c1_id = c1_row["canonical"], c1_row["content_hash"], c1_row["id"]
    c1 = api.document(e1)
    assert c1["work_context"]["work_item_id"] == WID
    assert (c1["work_context"]["execution_id"], c1["work_context"]["execution_sequence"]) == (e1, 1)
    assert c1["work_context"]["prior_execution_ids"] == []
    assert c1["work_context"]["resumed_from_snapshot_id"] is None

    # New input arrives between the executions (requirement 7).
    _new_input(monkeypatch)
    _Clock.current += timedelta(minutes=30)

    # 2. The resume: a new engine run is a new execution of the same W1.
    # Its first attempt fails AFTER sealing (the CWFT's primary step), so
    # the engine retries it under the same engine ref.
    monkeypatch.setenv(ex.FINAL_ATTEMPT_ENV_VAR, "false")
    monkeypatch.setattr(run_issue_investigator, "_run_agent", _write_nothing)
    capsys.readouterr()
    first = _resume(tmp_path, monkeypatch, resume_from=e1)
    assert first.error is not None and "SnapshotNotPersisted" not in first.error
    e2_row = api.by_ref(REF_B)
    e2 = e2_row["id"]
    assert e2 != e1 and e2_row["attempt"] == 2 and e2_row["phase"] == "Running"

    # 3. C2: a distinct immutable snapshot identity, in the store under E2.
    c2_row = api.snapshots[e2]
    assert c2_row["id"] != c1_id and c2_row["content_hash"] != c1_hash
    assert c2_row["id"] == _snapshot_id_for(e2, c2_row["content_hash"])
    c2 = api.document(e2)
    assert c2["snapshot_id"] != c1["snapshot_id"]

    # 4. C2's provenance points back to W1 / E1 / C1.
    wc = c2["work_context"]
    assert wc["work_item_id"] == WID
    assert (wc["execution_id"], wc["execution_sequence"]) == (e2, 2)
    assert wc["prior_execution_ids"] == [e1]
    assert wc["resumed_from_snapshot_id"] == c1_id
    assert (wc["origin_surface"], wc["current_surface"], wc["actor_id"]) == ("github", "telegram", "alice")
    # ...and the store row records the same continuity, checked by the store.
    assert (c2_row["prior_execution_id"], c2_row["prior_snapshot_id"]) == (e1, c1_id)
    # The pointer was resolved BEFORE sealing: it is inside the hashed bytes.
    assert cs.ContextSnapshot.from_dict(c2).work_context.resumed_from_snapshot_id == c1_id

    # 7. The new input is in C2, with provenance; it is not in C1. The prior
    # execution's evidence is reintroduced by assembly from its durable home
    # (the published proposal), never copied out of C1.
    comment_id = f"issue-comment-{NEW_COMMENT[0]}"
    c2_sources = {s["source_id"]: s for s in c2["sources"]}
    assert comment_id in c2_sources and comment_id not in {s["source_id"] for s in c1["sources"]}
    assert c2_sources[comment_id]["locator"] == f"{URL}#issuecomment-{NEW_COMMENT[0]}"
    assert {k for k in c2_sources if k.startswith("proposal-dir-")} == {
        "proposal-dir-requirements.md", "proposal-dir-design.md", "proposal-dir-tasks.md"}
    assert not any(s["source_id"].startswith("proposal-dir-") for s in c1["sources"])
    # 6. No transcript and no raw surface text: neither C2's stored bytes nor
    # the snapshot telemetry carries the issue body or the comment text.
    out = capsys.readouterr().out
    for text in (ISSUE_BODY, NEW_COMMENT[3]):
        assert text.encode() not in api.snapshots[e2]["canonical"]
        assert all(text not in json.dumps(entry) for entry in _snapshot_log(out))
    assert [(e["step"], e["verdict"]) for e in _snapshot_log(out)] == [
        ("resumed_from", ws.SNAPSHOT_REPLAYED), ("persist", ws.SNAPSHOT_SEALED)]

    # Retry of run 2 under the same engine ref: the same E2, and a replay.
    monkeypatch.setenv(ex.FINAL_ATTEMPT_ENV_VAR, "true")
    monkeypatch.setattr(run_issue_investigator, "_run_agent", _write_triplet)
    _Clock.current += retry_after
    retry = _resume(tmp_path, monkeypatch, resume_from=e1)
    assert retry.error is None and retry.skipped_reason is None, retry
    assert api.by_ref(REF_B)["id"] == e2 and len(api.executions) == 2
    assert api.by_ref(REF_B)["phase"] == "Succeeded"
    [(step_r, verdict_r), (step_p, verdict_p)] = [(e["step"], e["verdict"]) for e in _snapshot_log(
        capsys.readouterr().out)]
    assert (step_r, verdict_r, step_p, verdict_p) == (
        "resumed_from", ws.SNAPSHOT_REPLAYED, "persist", ws.SNAPSHOT_REPLAYED)
    # Assembly is deterministic: the retry's snapshot is C2's content again.
    first_c2, retry_c2 = sealed[-2], sealed[-1]
    assert first_c2 is not None and retry_c2 is not None
    assert ws.differing_fields(first_c2.to_dict(), retry_c2.to_dict()) == []
    if retry_after == timedelta(0):
        # Same second: byte-identical, so the store itself answered a replay.
        assert retry_c2.content_hash == first_c2.content_hash
    # Either way, still exactly one row per execution, and C2 unchanged.
    assert api.inserts == 2 and api.snapshots[e2] is c2_row and api.document(e2) == c2

    # 5. C1's bytes and hash are unchanged after run 2 and its retry, and
    # the store still serves them verified.
    assert api.snapshots[e1]["canonical"] == c1_bytes and api.snapshots[e1]["content_hash"] == c1_hash
    read = WorkItemClient().execution_snapshot(WID, e1)
    assert (read.verdict, read.snapshot_id, read.content_hash) == (ws.SNAPSHOT_REPLAYED, c1_id, c1_hash)
    assert cs.canonical_json(read.stored_document) == c1_bytes
    assert cs.recompute_content_hash(cs.ContextSnapshot.from_dict(read.stored_document)) == c1["content_hash"]


# -- missing / unknown / stale prior references ---------------------------


@pytest.mark.parametrize("mode", [rollout.OBSERVE, rollout.ENFORCE])
def test_a_resume_from_an_unknown_prior_is_refused_explicitly(tmp_path, monkeypatch, api, capsys, mode):
    """A `--resume-from-execution-id` the store never recorded is carried as
    a prior, so the seal names it and the store refuses it (409
    `prior_snapshot_invalid`): C2 is never stored with a continuity claim
    the store cannot check. At `observe` that is logged and the issue path
    still decides; from `enforce` up it fails the run."""
    monkeypatch.setenv(rollout.ENV_VAR, mode)
    _run_one(tmp_path, monkeypatch, api)
    capsys.readouterr()

    result = _resume(tmp_path, monkeypatch, resume_from=UNKNOWN_PRIOR)
    e2 = api.by_ref(REF_B)["id"]
    [body] = api.seals(e2)
    assert body["prior_execution_id"] == UNKNOWN_PRIOR and "prior_snapshot_id" not in body
    assert e2 not in api.snapshots and api.inserts == 1

    log = _snapshot_log(capsys.readouterr().out)
    assert [(e["step"], e["verdict"]) for e in log] == [
        ("resumed_from", ws.SNAPSHOT_ABSENT), ("persist", ws.SNAPSHOT_REFUSED)]
    assert "prior_snapshot_invalid" in log[1]["reason"] and UNKNOWN_PRIOR in log[1]["reason"]
    if mode == rollout.OBSERVE:
        assert result.error is None and result.skipped_reason is None
    else:
        assert "SnapshotNotPersisted" in (result.error or "")
        assert "prior_snapshot_invalid" in (result.error or "")


def test_a_prior_that_sealed_nothing_is_reported_and_its_execution_still_linked(tmp_path, monkeypatch, api, capsys):
    """E1 ran before snapshots were persisted (rollout `off`): nothing to
    point at. The lookup says so (`snapshot-absent`), C2 carries no snapshot
    pointer, and the prior EXECUTION is still in its provenance, checked by
    the store."""
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OFF)
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, REF_A)
    assert investigate(URL, state_dir=tmp_path, work_item_id=WID).error is None
    assert api.requests == []
    # The work-item layer recorded E1 without a snapshot.
    api._attach({"engine": "argo", "engine_ref": REF_A, "phase": "Succeeded"})
    e1 = api.by_ref(REF_A)["id"]

    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    capsys.readouterr()
    result = _resume(tmp_path, monkeypatch, resume_from=e1)
    assert result.error is None and result.skipped_reason is None
    e2 = api.by_ref(REF_B)["id"]
    wc = api.document(e2)["work_context"]
    assert (wc["prior_execution_ids"], wc["resumed_from_snapshot_id"]) == ([e1], None)
    assert (api.snapshots[e2]["prior_execution_id"], api.snapshots[e2]["prior_snapshot_id"]) == (e1, "")
    log = _snapshot_log(capsys.readouterr().out)
    assert [(e["step"], e["verdict"]) for e in log] == [
        ("resumed_from", ws.SNAPSHOT_ABSENT), ("persist", ws.SNAPSHOT_SEALED)]
    assert log[0]["reason"] == "the prior execution sealed no snapshot"


def test_an_unreadable_prior_snapshot_is_unknown_never_a_guessed_link(tmp_path, monkeypatch, api, capsys):
    """The prior snapshot exists but cannot be read (the store answers 5xx):
    the lookup is logged UNKNOWN and C2 names no snapshot rather than a
    guessed one. The pointer is a convenience (ADR 011 §2), so this does not
    block; the prior execution is still linked."""
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    e1, _ = _run_one(tmp_path, monkeypatch, api)
    api.fail[("GET", f"/api/v1/work-items/{WID}/executions/{e1}/snapshot")] = _HTTPResult(
        503, {"code": "work_items_unavailable", "error": "down"})
    capsys.readouterr()
    assert _resume(tmp_path, monkeypatch, resume_from=e1).error is None
    e2 = api.by_ref(REF_B)["id"]
    assert api.document(e2)["work_context"]["resumed_from_snapshot_id"] is None
    assert api.document(e2)["work_context"]["prior_execution_ids"] == [e1]
    log = _snapshot_log(capsys.readouterr().out)
    assert [(e["step"], e["verdict"]) for e in log] == [
        ("resumed_from", ws.SNAPSHOT_UNKNOWN), ("persist", ws.SNAPSHOT_SEALED)]


def test_the_fake_store_refuses_a_stale_prior_like_mctl_api(api):
    """The fake's `checkPrior`: a prior that does not precede the execution
    is refused, never stored. (The investigator never sends one: its priors
    are the ledger entries below its own attempt, see
    test_work_context_execution_identity.py
    test_resume_from_never_names_this_or_a_later_execution_as_prior.)"""
    for ref in ("one", "two"):
        api._attach({"engine": "argo", "engine_ref": ref, "phase": "Succeeded"})
    one, two = api.by_ref("one")["id"], api.by_ref("two")["id"]
    raw = b'{"a":1}'
    body = {"execution_sequence": 1, "canonical_b64": base64.b64encode(raw).decode(), "content_hash": _hash(raw),
            "strategy": "s", "strategy_version": "1", "prior_execution_id": two}
    answer = api._seal(one, body)
    assert (answer.status, answer.payload["code"]) == (409, "prior_snapshot_invalid")
    assert api._seal(one, {**body, "content_hash": "sha256:" + "0" * 64}).status == 400
    assert api.snapshots == {}


# -- 8. approval / policy is re-resolved, never inherited -------------------

_AUTHORITY_WORDS = ("approv", "authoriz", "grant", "permit", "policy", "allow", "deny", "verdict")


def _keys(node: Any, path: str = "") -> list[str]:
    if isinstance(node, dict):
        return [k for key, value in node.items() for k in (f"{path}.{key}", *_keys(value, f"{path}.{key}"))]
    if isinstance(node, list):
        return [k for value in node for k in _keys(value, f"{path}[]")]
    return []


@pytest.mark.parametrize("mode", [rollout.OBSERVE, rollout.ENFORCE])
def test_authority_is_re_resolved_on_resume_and_never_read_from_the_prior_snapshot(
    tmp_path, monkeypatch, api, mode
):
    """What the snapshot holds: provenance and evidence references only (the
    ContextSnapshot schema has no field for an approval or a policy
    decision). What the resume does: every write is decided again by the
    policy checkpoint for THIS execution, whatever C1's did.

    The code path: `snapshots.resumed_from` takes only `answer.snapshot_id`
    from the prior snapshot's read, and `persist` reads a stored document
    back only for its own execution (`client.execution_snapshot(wid, eid)`),
    to classify a retry. Nothing from C1's content reaches C2's assembly,
    the attach or the seal decision."""
    monkeypatch.setenv(rollout.ENV_VAR, mode)
    e1, _ = _run_one(tmp_path, monkeypatch, api)
    c1 = api.document(e1)
    assert set(c1) == cs._SNAPSHOT_KEYS
    assert set(c1["work_context"]) == set(cs.WorkContextRef(
        work_item_id="w", work_item_revision="", execution_id="e", execution_sequence=1).to_dict())
    offending = [k for k in _keys(c1) if any(w in k.lower() for w in _AUTHORITY_WORDS)]
    assert offending == []

    # The same work item, the same actor kinds, and C1 sealed under the
    # built-in policy. Now the policy no longer allows a seal: E2 is still
    # attached (that rule is unchanged), but C2 is refused, because the
    # decision is made again for E2, not carried over from C1.
    no_seal = pc.Policy(version="no-seal", rules=tuple(
        r for r in pc.BUILTIN_POLICY.rules if r.operation != ws.SEAL_SNAPSHOT_OPERATION))
    monkeypatch.setattr(pc, "BUILTIN_POLICY", no_seal)
    monkeypatch.setattr(pc.checkpoint, "__kwdefaults__", {**pc.checkpoint.__kwdefaults__, "policy": no_seal})
    result = _resume(tmp_path, monkeypatch, resume_from=e1)
    e2 = api.by_ref(REF_B)["id"]
    assert api.seals(e2) == [] and e2 not in api.snapshots
    if mode == rollout.OBSERVE:
        assert result.error is None
    else:
        assert "SnapshotNotPersisted" in (result.error or "") and "policy" in (result.error or "")
    # C1 is untouched by the refused resume.
    assert api.document(e1) == c1
