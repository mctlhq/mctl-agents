"""The investigator's execution identity comes from the work-item store
(mctlhq/mctl-agents#455 item 1, owner decision B on #431).

Driven end to end through `investigate()` against an in-memory stand-in for
mctl-api's work-item routes, behind the real `WorkItemClient` (its
`_request` is the only thing replaced), so the policy checkpoint, the attach
classification, the ledger read and the snapshot persistence all run for
real. The stand-in keeps mctl-api's attach rules (internal/workitems/
store.go `AttachExecution`): idempotent on `(engine, engine_ref)`, attempts
numbered `len(ledger)+1`, 409 `execution_active` while another execution is
non-terminal, and an ended execution is never reopened.
"""
from __future__ import annotations

import pytest

from orchestrator import policy_checkpoint as pc
from orchestrator import run_issue_investigator
from orchestrator.run_issue_investigator import investigate
from orchestrator.work_context import executions as ex
from orchestrator.work_context import rollout
from orchestrator.work_context.client import WorkItemClient, WorkItemUnavailable, _HTTPResult
from orchestrator.work_context.contract import CanonicalState, ExecutionRef, WorkItem
from tests.test_run_issue_investigator import _investigate_harness

WID = "wi_0a70acb2-94c3-440b-bd1f-a898e8bee4a5"
E1 = "we_11111111-1111-4111-8111-111111111111"
SHA = "c" * 64  # the dev_loop's seed-hash shape (`execution_id_for`)
NUMBER = 455
URL = f"https://github.com/mctlhq/mctl-telegram/issues/{NUMBER}"
TERMINAL = {"Succeeded", "Failed", "Error"}


class FakeStore:
    """mctl-api's work-item routes for one work item, in memory."""

    def __init__(self, *, executions=(), state="active") -> None:
        self.state = state
        self.executions: list[dict] = [
            {"id": eid, "work_item_id": WID, "engine": engine, "engine_ref": ref, "attempt": i + 1,
             "phase": phase, "started_at": "2026-09-23T00:00:00Z"}
            for i, (eid, engine, ref, phase) in enumerate(executions)
        ]
        self.requests: list[tuple[str, str, dict | None]] = []
        self.snapshots: dict[str, dict] = {}
        self._next = 100

    @property
    def writes(self) -> list[tuple[str, str, dict | None]]:
        return [r for r in self.requests if r[0] != "GET"]

    def attaches(self) -> list[dict]:
        return [body for method, path, body in self.writes if path.endswith("/executions")]

    def by_ref(self, ref: str) -> dict:
        return next(e for e in self.executions if e["engine_ref"] == ref)

    # -- the routes -------------------------------------------------------

    def request(self, method: str, path: str, payload: dict | None = None) -> _HTTPResult:
        self.requests.append((method, path, payload))
        base = f"/api/v1/work-items/{WID}"
        if method == "GET" and path == base:
            return _HTTPResult(200, self._view())
        if method == "GET" and path == f"{base}/executions":
            return _HTTPResult(200, {"schema_version": "workitem/v1", "executions": list(self.executions)})
        if method == "POST" and path == f"{base}/executions":
            return self._attach(payload or {})
        if path.startswith(f"{base}/executions/") and path.endswith("/snapshot"):
            eid = path.split("/")[-2]
            if method == "GET":
                return _HTTPResult(404, {"code": "snapshot_not_found", "error": "none"})
            snap = {"id": f"cs_{eid[3:11]}", "execution_id": eid, "content_hash": (payload or {})["content_hash"],
                    "schema_version": "workitem/v1"}
            self.snapshots[eid] = snap
            return _HTTPResult(201, {"schema_version": "workitem/v1", "snapshot": snap})
        return _HTTPResult(404, {"code": "not_a_route", "error": path})

    def _view(self) -> dict:
        item = {"id": WID, "state": self.state, "state_version": 1, "origin_surface": "github",
                "external_key": URL, "schema_version": "workitem/v1"}
        view = {"schema_version": "workitem/v1", "work_item": item, "state_version": 1}
        if self.executions:
            view["latest_execution"] = self.executions[-1]
        return view

    def _attach(self, body: dict) -> _HTTPResult:
        engine, ref, phase = body["engine"], body["engine_ref"], body.get("phase") or "Pending"
        existing = next((e for e in self.executions if (e["engine"], e["engine_ref"]) == (engine, ref)), None)
        if existing is not None:
            if existing["phase"] != phase:
                if existing["phase"] in TERMINAL:
                    return _HTTPResult(409, {"code": "invalid_transition", "error": "already ended"})
                existing["phase"] = phase
            return _HTTPResult(200, {"schema_version": "workitem/v1", "execution": existing})
        if self.state in {"completed", "superseded", "archived"}:
            return _HTTPResult(409, {"code": "invalid_transition", "error": f"attach to {self.state}"})
        if phase not in TERMINAL and any(e["phase"] not in TERMINAL for e in self.executions):
            return _HTTPResult(409, {"code": "execution_active", "error": "active"})
        self._next += 1
        created = {"id": f"we_{self._next:08d}-0000-4000-8000-000000000000", "work_item_id": WID,
                   "engine": engine, "engine_ref": ref, "attempt": len(self.executions) + 1, "phase": phase,
                   "started_at": "2026-09-23T00:00:00Z"}
        self.executions.append(created)
        return _HTTPResult(201, {"schema_version": "workitem/v1", "execution": created})


def _write_triplet(repo_dir, prompt, proposal_dir):
    for name in ("requirements.md", "design.md", "tasks.md"):
        (proposal_dir / name).write_text(f"x {name}")


def _write_nothing(repo_dir, prompt, proposal_dir):
    return None


@pytest.fixture
def store(monkeypatch, tmp_path) -> FakeStore:
    fake = FakeStore(executions=((E1, "temporal", "dev-loop-455", "Failed"),))
    monkeypatch.setattr(WorkItemClient, "_request", lambda self, method, path, payload=None: fake.request(
        method, path, payload))
    for var in (ex.ENGINE_ENV_VAR, ex.ENGINE_REF_ENV_VAR, ex.WORKFLOW_NAME_ENV_VAR, ex.FINAL_ATTEMPT_ENV_VAR,
                rollout.REQUIRED_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    monkeypatch.setenv("ISSUE_INVESTIGATOR_CONTEXT_MODE", "shadow")
    monkeypatch.setattr(run_issue_investigator, "_target_repository_sha", lambda repo_dir: "a" * 40)
    _investigate_harness(tmp_path, monkeypatch, number=NUMBER, title="Execution identity", agent=_write_triplet)
    return fake


@pytest.fixture
def sealed(monkeypatch) -> list:
    """Every WorkContextRef the assembly entry point was handed."""
    seen: list = []
    real = run_issue_investigator.context_assembly.assemble_investigator_context

    def capture(**kwargs):
        seen.append(kwargs.get("work_context"))
        return real(**kwargs)

    monkeypatch.setattr(run_issue_investigator.context_assembly, "assemble_investigator_context", capture)
    return seen


def _run(tmp_path, **kwargs):
    return investigate(URL, state_dir=tmp_path, work_item_id=WID, **kwargs)


def _deny_all(monkeypatch):
    deny = pc.Policy(version="deny-all", rules=())
    monkeypatch.setattr(pc, "BUILTIN_POLICY", deny)
    monkeypatch.setattr(pc.checkpoint, "__kwdefaults__", {**pc.checkpoint.__kwdefaults__, "policy": deny})


# -- attach -> identity ---------------------------------------------------


def test_the_attached_store_execution_is_the_work_context_identity(tmp_path, monkeypatch, store, sealed):
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-abc12")
    result = _run(tmp_path)
    assert result.error is None and result.skipped_reason is None

    mine = store.by_ref("mctl-agents-investigate-abc12")
    (wc,) = sealed
    assert wc.execution_id == mine["id"] and wc.execution_id.startswith("we_")
    assert (wc.execution_sequence, wc.prior_execution_ids) == (2, (E1,))
    # Attached as an argo run, Running; advanced to Succeeded at the end.
    assert store.attaches() == [
        {"engine": "argo", "engine_ref": "mctl-agents-investigate-abc12", "phase": "Running"},
        {"engine": "argo", "engine_ref": "mctl-agents-investigate-abc12", "phase": "Succeeded"},
    ]
    assert mine["phase"] == "Succeeded"
    # And persistence is live: the snapshot was sealed under the store id.
    assert list(store.snapshots) == [mine["id"]]


def test_mctl_engine_ref_and_engine_override_the_workflow_name(tmp_path, monkeypatch, store, sealed):
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "ignored")
    monkeypatch.setenv(ex.ENGINE_REF_ENV_VAR, "dev-loop-455-r2")
    monkeypatch.setenv(ex.ENGINE_ENV_VAR, "temporal")
    assert _run(tmp_path).error is None
    assert store.attaches()[0] == {"engine": "temporal", "engine_ref": "dev-loop-455-r2", "phase": "Running"}
    assert sealed[0].execution_id == store.by_ref("dev-loop-455-r2")["id"]


def test_a_retry_of_the_same_workflow_is_the_same_execution(tmp_path, monkeypatch, store, sealed):
    """The CWFT's fallback step retries a failed primary under the same
    {{workflow.name}}: the same execution, the same sequence, and its own
    ledger row is never counted as its prior (the #455 self-exclusion)."""
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-retry")
    monkeypatch.setenv(ex.FINAL_ATTEMPT_ENV_VAR, "false")  # the primary attempt
    monkeypatch.setattr(run_issue_investigator, "_run_agent", _write_nothing)
    first = _run(tmp_path)
    assert first.error is not None
    mine = store.by_ref("mctl-agents-investigate-retry")
    # Not ended: the store never reopens an ended execution for the retry.
    assert mine["phase"] == "Running"

    monkeypatch.setenv(ex.FINAL_ATTEMPT_ENV_VAR, "true")  # the fallback attempt
    monkeypatch.setattr(run_issue_investigator, "_run_agent", _write_triplet)
    second = _run(tmp_path)
    assert second.error is None

    assert [wc.execution_id for wc in sealed] == [mine["id"], mine["id"]]
    assert [wc.execution_sequence for wc in sealed] == [2, 2]
    assert [wc.prior_execution_ids for wc in sealed] == [(E1,), (E1,)]
    assert len(store.executions) == 2 and mine["phase"] == "Succeeded"


def test_a_final_failed_attempt_ends_the_execution_failed(tmp_path, monkeypatch, store):
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-fail")
    monkeypatch.setattr(run_issue_investigator, "_run_agent", _write_nothing)
    assert _run(tmp_path).error is not None
    assert store.by_ref("mctl-agents-investigate-fail")["phase"] == "Failed"


def test_a_failed_terminal_advance_never_changes_the_result(tmp_path, monkeypatch, store, capsys):
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-flaky")
    real = store.request

    def down_on_terminal(method, path, payload=None):
        if payload and payload.get("phase") == "Succeeded":
            raise WorkItemUnavailable("connection reset")
        return real(method, path, payload)

    monkeypatch.setattr(WorkItemClient, "_request", lambda self, m, p, b=None: down_on_terminal(m, p, b))
    result = _run(tmp_path)
    assert result.error is None and result.skipped_reason is None
    assert "could not advance execution" in capsys.readouterr().out


def test_a_new_run_is_a_new_execution(tmp_path, monkeypatch, store, sealed):
    for name in ("mctl-agents-investigate-one", "mctl-agents-investigate-two"):
        monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, name)
        assert _run(tmp_path).error is None
    one, two = store.by_ref("mctl-agents-investigate-one"), store.by_ref("mctl-agents-investigate-two")
    assert one["id"] != two["id"]
    assert [wc.execution_id for wc in sealed] == [one["id"], two["id"]]
    assert [wc.execution_sequence for wc in sealed] == [2, 3]
    assert sealed[1].prior_execution_ids == (E1, one["id"])


# -- a `we_` --execution-id from the work-item layer ------------------------


def test_a_store_execution_in_the_ledger_is_used_without_attaching(tmp_path, monkeypatch, store, sealed):
    """E2 created by the resume route: used as-is, nothing attached, and its
    phase is left to the layer that created it."""
    e2 = "we_22222222-2222-4222-8222-222222222222"
    store.executions.append({"id": e2, "work_item_id": WID, "engine": "temporal", "engine_ref": "dev-loop-455-r2",
                             "attempt": 2, "phase": "Running", "started_at": "2026-09-23T00:00:00Z"})
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-resume")
    assert _run(tmp_path, execution_id=e2).error is None
    (wc,) = sealed
    assert (wc.execution_id, wc.execution_sequence, wc.prior_execution_ids) == (e2, 2, (E1,))
    assert store.attaches() == []
    assert list(store.snapshots) == [e2]


@pytest.mark.parametrize("mode", [rollout.OBSERVE, rollout.ENFORCE, rollout.ONLY])
def test_a_store_execution_not_in_the_ledger_is_refused(tmp_path, monkeypatch, store, sealed, capsys, mode):
    foreign = "we_99999999-9999-4999-8999-999999999999"
    monkeypatch.setenv(rollout.ENV_VAR, mode)
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-foreign")
    result = _run(tmp_path, execution_id=foreign)
    assert store.writes == []
    if mode == rollout.OBSERVE:
        assert result.error is None and result.skipped_reason is None
        assert sealed == [None]
        assert f"{foreign} is not an execution of work item {WID}" in capsys.readouterr().out
    else:
        assert result.skipped_reason is not None
        assert f"{foreign} is not an execution of work item {WID}" in result.skipped_reason
        assert sealed == []


# -- no identity is ever invented -------------------------------------------


def test_a_legacy_execution_id_is_never_the_identity(tmp_path, monkeypatch, store, sealed, capsys):
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-legacy")
    assert _run(tmp_path, execution_id=SHA).error is None
    (wc,) = sealed
    assert wc.execution_id == store.by_ref("mctl-agents-investigate-legacy")["id"]
    assert SHA not in repr(store.requests)
    assert f"--execution-id {SHA} is not a store execution" in capsys.readouterr().out

    # And with no engine run to attach, it is still not the identity.
    monkeypatch.delenv(ex.WORKFLOW_NAME_ENV_VAR)
    assert _run(tmp_path, execution_id=SHA).error is None
    assert sealed[-1] is None
    assert SHA not in repr(store.requests)


@pytest.mark.parametrize("mode", [rollout.OBSERVE, rollout.ENFORCE])
def test_no_engine_ref_means_no_identity(tmp_path, monkeypatch, store, sealed, capsys, mode):
    monkeypatch.setenv(rollout.ENV_VAR, mode)
    result = _run(tmp_path)
    assert store.writes == []
    out = capsys.readouterr().out
    assert "no execution identity: neither MCTL_ENGINE_REF nor WORKFLOW_NAME is set" in out
    if mode == rollout.OBSERVE:
        assert result.error is None and result.skipped_reason is None
        assert sealed == [None]
        assert "no snapshot is persisted" in out
    else:
        # A definite refusal: not lifted by the WORK_CONTEXT_REQUIRED break-glass.
        monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, "false")
        again = _run(tmp_path)
        assert "no execution identity" in (result.skipped_reason or "")
        assert "no execution identity" in (again.skipped_reason or "")


def test_an_unknown_engine_is_no_identity(tmp_path, monkeypatch, store, sealed):
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "wf")
    monkeypatch.setenv(ex.ENGINE_ENV_VAR, "jenkins")
    assert _run(tmp_path).error is None
    assert sealed == [None] and store.writes == []


# -- gates -----------------------------------------------------------------


@pytest.mark.parametrize("mode", [rollout.OBSERVE, rollout.ENFORCE])
def test_a_policy_deny_attaches_nothing(tmp_path, monkeypatch, store, sealed, capsys, mode):
    monkeypatch.setenv(rollout.ENV_VAR, mode)
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-denied")
    _deny_all(monkeypatch)
    result = _run(tmp_path)
    assert store.attaches() == []
    assert '"operation": "attach:work-item-execution"' in capsys.readouterr().out
    if mode == rollout.OBSERVE:
        assert result.error is None and sealed == [None]
    else:
        assert "policy DENY" in (result.skipped_reason or "")


def test_mode_off_writes_nothing(tmp_path, monkeypatch, store, sealed):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OFF)
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-off")
    assert _run(tmp_path, execution_id=SHA).error is None
    assert store.requests == []
    assert sealed == [None]


def test_a_dry_run_attaches_nothing(tmp_path, monkeypatch, store):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-dry")
    assert _run(tmp_path, dry_run=True).skipped_reason == "dry-run"
    assert store.writes == []


@pytest.mark.parametrize(("mode", "required", "blocked"), [
    (rollout.OBSERVE, "true", False),
    (rollout.ENFORCE, "true", True),
    (rollout.ENFORCE, "false", False),  # the break-glass applies: an UNKNOWN, not a no
])
def test_another_active_execution_is_unknown_per_mode(tmp_path, monkeypatch, store, sealed, mode, required, blocked):
    store.executions[0]["phase"] = "Running"  # E1 still holds the item
    monkeypatch.setenv(rollout.ENV_VAR, mode)
    monkeypatch.setenv(rollout.REQUIRED_ENV_VAR, required)
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-busy")
    result = _run(tmp_path)
    assert len(store.executions) == 1
    if blocked:
        assert "execution-active" in (result.skipped_reason or "")
    else:
        assert result.error is None and result.skipped_reason is None and sealed == [None]


def test_a_terminal_work_item_is_vetoed_before_anything_is_attached(tmp_path, monkeypatch, store):
    store.state = "completed"
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-late")
    assert "terminal state" in (_run(tmp_path).skipped_reason or "")
    assert store.writes == []


# -- the prior list and the sequence ----------------------------------------


def test_prior_ids_exclude_self_and_later_executions_and_the_sequence_is_the_stores():
    e2, e3 = "we_2", "we_3"
    item = WorkItem(work_item_id=WID, executions=(
        ExecutionRef(execution_id=E1, sequence=1),
        ExecutionRef(execution_id=e2, sequence=2),
        ExecutionRef(execution_id=e3, sequence=3),
    ))
    canonical = CanonicalState(work_item_id=WID, prior_execution_ids=(E1, e2, e3))
    ref = run_issue_investigator._work_context_ref(
        canonical=canonical, item=item, execution_id=e2, execution_sequence=2,
        resume_from_execution_id=None, surface=None, actor_kind=None, actor_id=None,
    )
    assert (ref.execution_sequence, ref.prior_execution_ids) == (2, (E1,))
