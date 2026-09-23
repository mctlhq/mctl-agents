"""The execution-request dispatcher, offline (mctlhq/mctl-agents#461).

Everything here runs against a fake mctl-api: `DispatchFakeApi` extends the
#431 acceptance module's `FakeMctlApi` (work item, executions, snapshots)
with mctl-api#368's execution-request routes, following the Go store's rules
(`internal/workitems/execution_requests.go`):

- claim takes the oldest `pending` request, or a `claimed` one whose lease
  lapsed, and mints a NEW claim token each time;
- fulfil and reject need the current token of an unexpired claim (409
  `execution_request_not_claimed` otherwise); a fulfilled request answers
  its holder repeating the same engine run with the same execution (200),
  and anything else with 409 `execution_request_closed`;
- fulfil re-decides the item: `start` attaches a Running execution under
  the `/executions` rule, `resume` inserts a Pending one under the
  `/resume` rule (every earlier execution terminal, a new engine run, the
  item's `state_version` raised).

Where Temporal matters (the workflow id, the reuse and conflict policies,
the loop binding its `we_`), the real `DevLoopWorkflow` runs in the
time-skipping test server with the real bind/advance activities; only the
Argo submit is faked. The end-to-end test runs the real investigator
inside that fake submit, so the snapshot it seals is the real one.
"""

from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from typing import Any

import anyio
import pytest
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal import dispatcher as dx
from orchestrator.temporal import worker as worker_module
from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.execution_requests import (
    advance_dispatched_execution,
    bind_dispatched_execution,
)
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.temporal.start import dispatched_workflow_id
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context.client import WorkItemClient, WorkItemUnavailable, _HTTPResult
from tests.temporal_harness import Worker
from tests.test_work_context_resume_acceptance import SV, TERMINAL_PHASES, URL, WID, FakeMctlApi

pytestmark = pytest.mark.anyio

OTHER_WID = "wi_461b0000-0000-4000-8000-000000000461"


class DispatchFakeApi(FakeMctlApi):
    """`FakeMctlApi` plus mctl-api#368's execution-request routes."""

    def __init__(self) -> None:
        super().__init__()
        self.external_key = URL
        self.state_version = 1
        #: Seconds on the store's clock; tests move it to lapse a lease.
        self.now = 0.0
        self.xrs: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, str] = {}
        self.tokens_minted: list[str] = []
        #: Commit the next fulfil, then lose its answer (a transport error).
        self.lose_next_fulfil_answer = False

    # -- tests' own writes ----------------------------------------------

    def create_request(self, kind: str, *, work_item_id: str = WID, resumed_from: str = "") -> str:
        rid = f"xr_{len(self.xrs) + 1:08d}-0000-4000-8000-000000000461"
        self.xrs[rid] = {
            "id": rid,
            "work_item_id": work_item_id,
            "kind": kind,
            "state": "pending",
            "expected_state_version": self.state_version,
            "surface": "telegram",
            "requested_by": "user:alice",
            "created_at": f"2026-09-23T12:00:{len(self.xrs):02d}Z",
            "resumed_from_execution_id": resumed_from,
            "schema_version": SV,
            "_expires": 0.0,
        }
        return rid

    def request_state(self, rid: str) -> dict[str, Any]:
        return self._public(self.xrs[rid])

    # -- routes -----------------------------------------------------------

    def _view(self) -> dict[str, Any]:
        view = super()._view()
        view["work_item"]["external_key"] = self.external_key
        view["work_item"]["state_version"] = self.state_version
        view["state_version"] = self.state_version
        return view

    def request(self, method: str, path: str, payload: dict | None = None) -> _HTTPResult:
        if (method, path) in self.fail and path.startswith("/api/v1/execution-requests/"):
            self.requests.append((method, path, copy.deepcopy(payload)))
            return self.fail[(method, path)]
        if method == "POST" and path == "/api/v1/execution-requests/claim":
            self.requests.append((method, path, copy.deepcopy(payload)))
            return self._claim(payload or {})
        if method == "POST" and path.startswith("/api/v1/execution-requests/"):
            self.requests.append((method, path, copy.deepcopy(payload)))
            rid, verb = path.split("/")[-2:]
            if rid not in self.xrs:
                return _HTTPResult(404, {"code": "execution_request_not_found", "error": "not found"})
            if verb == "fulfil":
                result = self._fulfil(self.xrs[rid], payload or {})
                if self.lose_next_fulfil_answer and result.status in (200, 201):
                    self.lose_next_fulfil_answer = False
                    raise WorkItemUnavailable("connection reset after the store committed")
                return result
            return self._reject(self.xrs[rid], payload or {})
        prefix = f"/api/v1/work-items/{WID}/execution-requests/"
        if method == "GET" and path.startswith(prefix):
            self.requests.append((method, path, None))
            x = self.xrs.get(path[len(prefix) :])
            if x is None:
                return _HTTPResult(404, {"code": "execution_request_not_found", "error": "not found"})
            return _HTTPResult(200, {"schema_version": SV, "execution_request": self._public(x)})
        return super().request(method, path, payload)

    @staticmethod
    def _public(x: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in x.items() if not k.startswith("_") and v != ""}

    def _claimable(self, x: dict[str, Any]) -> bool:
        return x["state"] == "pending" or (x["state"] == "claimed" and x["_expires"] <= self.now)

    def _claim(self, body: dict) -> _HTTPResult:
        lease = body.get("lease_seconds") or 60
        if not xr.MIN_LEASE_SECONDS <= lease <= xr.MAX_LEASE_SECONDS:
            return _HTTPResult(400, {"code": "invalid_request", "error": "lease_seconds must be from 5 to 900"})
        for x in sorted(self.xrs.values(), key=lambda r: (r["created_at"], r["id"])):
            if not self._claimable(x):
                continue
            token = f"xrc_{uuid.uuid4().hex}"
            self.tokens_minted.append(token)
            self._tokens[x["id"]] = token
            x.update(
                state="claimed",
                claimed_by="service:mctl-agents",
                _expires=self.now + lease,
                claim_expires_at=f"t+{self.now + lease}",
            )
            return _HTTPResult(200, {"schema_version": SV, "execution_request": self._public(x), "claim_token": token})
        return _HTTPResult(204, {}, body_empty=True)

    def _holds(self, x: dict[str, Any], body: dict) -> bool:
        return bool(body.get("claim_token")) and self._tokens.get(x["id"]) == body.get("claim_token")

    def _not_claimed(self, x: dict[str, Any]) -> _HTTPResult:
        return _HTTPResult(409, {"code": xr.NOT_CLAIMED_CODE, "error": f"{x['id']} is not claimed by this holder"})

    def _closed(self, x: dict[str, Any]) -> _HTTPResult:
        details = {"execution_request_id": x["id"], "state": x["state"]}
        if x.get("execution_id"):
            details["execution_id"] = x["execution_id"]
        return _HTTPResult(409, {"code": xr.CLOSED_CODE, "error": "closed", "details": details})

    def _fulfil(self, x: dict[str, Any], body: dict) -> _HTTPResult:
        engine, ref = body.get("engine"), body.get("engine_ref")
        if x["state"] == "fulfilled":
            if not self._holds(x, body):
                return self._not_claimed(x)
            ex = self._execution(x["execution_id"])
            if ex is None or (ex["engine"], ex["engine_ref"]) != (engine, ref):
                return self._closed(x)
            return self._fulfilled(x, ex, 200)
        if x["state"] == "rejected":
            return self._closed(x)
        if x["state"] != "claimed" or not self._holds(x, body) or x["_expires"] <= self.now:
            return self._not_claimed(x)
        if self.state_version != x["expected_state_version"]:
            return _HTTPResult(409, {"code": "state_version_conflict", "error": "the item moved"})
        if any((e["engine"], e["engine_ref"]) == (engine, ref) for e in self.executions):
            return _HTTPResult(400, {"code": "invalid_request", "error": "a resume starts a new run"})
        if x["kind"] == "start":
            answer = self._attach({"engine": engine, "engine_ref": ref, "phase": "Running"})
            if answer.status != 201:
                return answer
        else:
            if any(e["phase"] not in TERMINAL_PHASES for e in self.executions):
                return _HTTPResult(409, {"code": "execution_active", "error": "another execution is active"})
            answer = self._attach({"engine": engine, "engine_ref": ref, "phase": "Pending"})
            if answer.status != 201:
                return answer
            self.state_version += 1
        ex = self.by_ref(str(ref))
        x.update(state="fulfilled", execution_id=ex["id"])
        return self._fulfilled(x, ex, 201)

    def _fulfilled(self, x: dict[str, Any], ex: dict[str, Any], status: int) -> _HTTPResult:
        return _HTTPResult(
            status,
            {
                "schema_version": SV,
                "execution_request": self._public(x),
                "execution": copy.deepcopy(ex),
                "work_item": self._view()["work_item"],
                "state_version": self.state_version,
            },
        )

    def _reject(self, x: dict[str, Any], body: dict) -> _HTTPResult:
        if x["state"] in ("fulfilled", "rejected"):
            return self._closed(x)
        if x["state"] != "claimed" or not self._holds(x, body) or x["_expires"] <= self.now:
            return self._not_claimed(x)
        x.update(state="rejected", reason=body.get("reason", ""))
        return _HTTPResult(200, {"schema_version": SV, "execution_request": self._public(x)})

    def fulfils(self) -> list[dict]:
        return [b for m, p, b in self.requests if m == "POST" and p.endswith("/fulfil") and b is not None]


@pytest.fixture
def api(monkeypatch) -> DispatchFakeApi:
    fake = DispatchFakeApi()
    monkeypatch.setattr(
        WorkItemClient, "_request", lambda self, method, path, payload=None: fake.request(method, path, payload)
    )
    monkeypatch.delenv(dx.ENABLED_ENV_VAR, raising=False)
    return fake


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


class FakeTemporal:
    """`TemporalPort` for tests that must never reach Temporal."""

    def __init__(self, states: dict[str, str] | None = None) -> None:
        self.states = states or {}
        self.started: list[IssueRef] = []

    async def start(self, issue: Any) -> str:
        self.started.append(issue)
        return dx.START_STARTED

    async def loop_state(self, workflow_id: str) -> str:
        return self.states.get(workflow_id, dx.LOOP_ABSENT)


class Crash(BaseException):
    """A dispatcher process dying mid-dispatch: not an `Exception`, so no
    handler in the dispatcher can swallow it."""


class CrashBeforeFulfil:
    """A `WorkItemClient` whose process dies right before the fulfil."""

    def __init__(self, inner: WorkItemClient) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def fulfil_execution_request(self, *args: Any, **kwargs: Any) -> Any:
        raise Crash("dispatcher died between start and fulfil")


def _investigate_log() -> tuple[Any, list[dict]]:
    """A fake Argo submit that records every investigate's params."""
    seen: list[dict] = []

    @activity.defn(name="submit_and_wait")
    async def submit(input: SubmitAndWaitInput) -> WorkflowResult:
        if input.operation == "mctl-agents-investigate":
            seen.append(dict(input.params))
        return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

    return submit, seen


def _loop_activities(submit: Any, **fakes: Any) -> list[Any]:
    from tests.test_dev_loop_workflow import _fake_activities

    activities, *_ = _fake_activities(released=True, **fakes)
    activities = [a for a in activities if a.__temporal_activity_definition.name != "submit_and_wait"]
    return [*activities, submit, bind_dispatched_execution, advance_dispatched_execution]


def _worker(env: WorkflowEnvironment, submit: Any, **fakes: Any) -> Worker:
    return Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=_loop_activities(submit, **fakes)
    )


def _dispatcher(env: WorkflowEnvironment, client: Any = None) -> dx.Dispatcher:
    return dx.Dispatcher(client or WorkItemClient(), dx.TemporalClientPort(env.client), lease=60)


async def _wait_for(predicate, limit: float = 20) -> None:
    """Poll `predicate` (the fake store and the fake submit are plain
    in-memory state, with no event to wait on) until it holds."""
    for _ in range(int(limit / 0.05)):
        if predicate():
            return
        await anyio.sleep(0.05)
    raise AssertionError(f"condition not reached within {limit}s")


async def _end(env: WorkflowEnvironment, workflow_id: str) -> None:
    """End a parked loop gracefully (#420) and wait for it."""
    handle = env.client.get_workflow_handle(workflow_id)
    await handle.signal(DevLoopWorkflow.abandon, {"reason": "test over"})
    await handle.result()


def _audit(out: str) -> list[dict]:
    import json

    return [json.loads(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith(dx.AUDIT_PREFIX + " ")]


# -- off by default --------------------------------------------------------


def test_the_dispatcher_is_off_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv(dx.ENABLED_ENV_VAR, raising=False)
    assert dx.enabled() is False
    assert worker_module.runs_dispatcher("all") is False
    assert worker_module.start_dispatcher(object(), "all", anyio.Event()) is None  # type: ignore[arg-type]
    for typo in ("", "0", "false", "off", "no", "enabled?", "tru"):
        monkeypatch.setenv(dx.ENABLED_ENV_VAR, typo)
        assert dx.enabled() is False, typo
    monkeypatch.setenv(dx.ENABLED_ENV_VAR, "true")
    assert dx.enabled() is True
    # Only a role that runs the workflows it starts (as for schedules).
    assert worker_module.runs_dispatcher("control") is True
    assert worker_module.runs_dispatcher("all") is True
    assert worker_module.runs_dispatcher("execution") is False
    assert worker_module.runs_dispatcher("implementation") is False


async def test_the_operator_cli_refuses_while_the_dispatcher_is_off(monkeypatch, capsys):
    from orchestrator.temporal import cli

    monkeypatch.delenv(dx.ENABLED_ENV_VAR, raising=False)

    async def no_connect():
        raise AssertionError("an operator command must not reach Temporal while the dispatcher is off")

    monkeypatch.setattr(cli, "connect", no_connect)
    with pytest.raises(SystemExit) as exc:
        await cli.dispatch_once()
    assert exc.value.code == 2
    assert dx.ENABLED_ENV_VAR in capsys.readouterr().err
    assert cli.build_parser().parse_args(["dispatch-once"]).command == "dispatch-once"


# -- deterministic identity --------------------------------------------------


def test_workflow_id_and_engine_ref_are_pure_functions_of_the_request_id():
    rid = "xr_00000001-0000-4000-8000-000000000461"
    assert dispatched_workflow_id(rid) == dispatched_workflow_id(rid) == f"dev-loop-{rid}"
    assert dispatched_workflow_id(rid) != dispatched_workflow_id(rid.replace("1-", "2-", 1))


# -- no runnable target ------------------------------------------------------


@pytest.mark.parametrize(
    "external_key",
    ["", "tg:chat/1/message/2", "https://github.com/someone-else/repo/issues/3"],
    ids=["no-key", "not-a-github-issue", "not-a-mctlhq-issue"],
)
async def test_an_item_without_a_runnable_issue_is_rejected_never_guessed(api, capsys, external_key):
    api.external_key = external_key
    rid = api.create_request("start")
    temporal = FakeTemporal()

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED and outcome.reason == xr.NO_RUNNABLE_TARGET
    assert api.request_state(rid)["state"] == "rejected"
    assert api.request_state(rid)["reason"] == xr.NO_RUNNABLE_TARGET
    assert temporal.started == [] and api.fulfils() == [] and api.executions == []
    events = [(a["event"], a.get("reason")) for a in _audit(capsys.readouterr().out)]
    assert events == [("claim", None), ("reject", xr.NO_RUNNABLE_TARGET)]


async def test_an_unknown_kind_is_rejected_with_a_typed_reason(api):
    rid = api.create_request("migrate")
    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()
    assert outcome.action == dx.REJECTED and api.request_state(rid)["reason"] == xr.UNSUPPORTED_KIND


async def test_an_unreadable_item_leaves_the_request_claimed_for_a_later_claim(api):
    rid = api.create_request("start")
    api.fail[("GET", f"/api/v1/work-items/{WID}")] = _HTTPResult(503, {"code": "unavailable"})
    temporal = FakeTemporal()

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED
    assert api.request_state(rid)["state"] == "claimed" and temporal.started == []


async def test_nothing_claimable_is_nothing(api):
    assert (await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()).action == dx.NOTHING


async def test_a_start_is_refused_while_the_issue_loop_is_live(api):
    """Two investigations of one issue would race for one proposal."""
    rid = api.create_request("start")
    temporal = FakeTemporal({workflow_id_for(URL): dx.LOOP_RUNNING})

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED and api.request_state(rid)["reason"] == xr.LOOP_ACTIVE
    assert temporal.started == []


def test_the_claim_token_never_reaches_a_repr_or_the_policy_checkpoint(api, capsys):
    api.create_request("start")
    answer = WorkItemClient().claim_execution_request(60)
    assert answer.verdict == xr.CLAIMED and answer.claim_token
    assert answer.claim_token not in repr(answer)
    assert answer.request is not None
    WorkItemClient().reject_execution_request(answer.request, answer.claim_token, xr.NO_RUNNABLE_TARGET)
    assert answer.claim_token not in capsys.readouterr().out


# -- the Temporal half: start, fulfil, convergence ---------------------------


async def test_a_duplicate_claim_or_start_never_makes_a_second_run(api, env, capsys):
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = dispatched_workflow_id(rid)
    async with _worker(env, submit):
        first = await _dispatcher(env).dispatch_once()
        assert first.action == dx.FULFILLED and first.workflow_id == wid
        # A second claim finds nothing: the request is closed, not re-run.
        assert (await _dispatcher(env).dispatch_once()).action == dx.NOTHING
        # A duplicate start of the same request attaches to the same run.
        run_id = (await env.client.get_workflow_handle(wid).describe()).run_id
        issue = IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid)
        assert await dx.TemporalClientPort(env.client).start(issue) == dx.START_STARTED
        assert (await env.client.get_workflow_handle(wid).describe()).run_id == run_id
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, wid)
        # Once the run has ended, the reuse policy refuses to start it again.
        assert await dx.TemporalClientPort(env.client).start(issue) == dx.START_CLOSED

    assert len(seen) == 1
    assert [(e["engine"], e["engine_ref"]) for e in api.executions] == [("temporal", wid)]
    assert seen[0]["execution_id"] == first.execution_id == api.executions[0]["id"]
    audit = _audit(capsys.readouterr().out)
    assert [a["event"] for a in audit] == ["claim", "start", "fulfil"]
    # Every line carries the ids; the fulfil line carries the execution.
    assert all(a["execution_request_id"] == rid and a["work_item_id"] == WID and a["workflow_id"] == wid for a in audit)
    assert audit[-1]["execution_id"] == first.execution_id and audit[-1]["engine_ref"] == wid


async def test_a_lost_fulfil_answer_is_retried_onto_the_same_execution(api, env):
    """The store committed, the answer was lost: the same holder repeating
    the same engine run must get the same `we_`, never a second one."""
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    api.lose_next_fulfil_answer = True
    async with _worker(env, submit):
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, dispatched_workflow_id(rid))

    fulfils = api.fulfils()
    assert len(fulfils) == 2 and fulfils[0] == fulfils[1]
    assert len(api.executions) == 1 and outcome.execution_id == api.executions[0]["id"]


async def test_a_crash_between_start_and_fulfil_converges_on_the_same_run(api, env, capsys):
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = dispatched_workflow_id(rid)
    async with _worker(env, submit):
        with pytest.raises(Crash):
            await _dispatcher(env, CrashBeforeFulfil(WorkItemClient())).dispatch_once()
        run_id = (await env.client.get_workflow_handle(wid).describe()).run_id
        # The loop is up and waiting for its fulfilment; nothing has run.
        assert api.executions == [] and seen == []
        # Before the lease lapses, nobody else can claim it.
        assert (await _dispatcher(env).dispatch_once()).action == dx.NOTHING
        api.now += 61
        # The re-claim gets a NEW token and converges on the same run.
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.workflow_id == wid
        assert len(set(api.tokens_minted)) == 2
        assert (await env.client.get_workflow_handle(wid).describe()).run_id == run_id
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, wid)

    assert len(seen) == 1 and seen[0]["execution_id"] == outcome.execution_id
    assert [(e["engine"], e["engine_ref"]) for e in api.executions] == [("temporal", wid)]
    assert [a["event"] for a in _audit(capsys.readouterr().out)] == ["claim", "start", "claim", "start", "fulfil"]


async def test_an_expired_lease_is_fenced_and_the_next_claim_converges(api, env):
    submit, seen = _investigate_log()
    rid = api.create_request("start")

    class SlowFulfil(CrashBeforeFulfil):
        def fulfil_execution_request(self, *args: Any, **kwargs: Any) -> Any:
            api.now += 61  # the lease lapses while this holder is still working
            return self._inner.fulfil_execution_request(*args, **kwargs)

    async with _worker(env, submit):
        fenced = await _dispatcher(env, SlowFulfil(WorkItemClient())).dispatch_once()
        assert fenced.action == dx.FENCED
        assert api.executions == [] and api.request_state(rid)["state"] == "claimed"
        converged = await _dispatcher(env).dispatch_once()
        assert converged.action == dx.FULFILLED
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, dispatched_workflow_id(rid))

    assert len(api.executions) == 1 and seen[0]["execution_id"] == converged.execution_id


async def test_a_request_whose_run_already_ended_is_rejected_not_rerun(api, env):
    """The loop gave up waiting for its fulfilment and ended; a later claim
    must not bind the request to that dead run, nor start another."""
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = dispatched_workflow_id(rid)
    async with _worker(env, submit):
        with pytest.raises(Crash):
            await _dispatcher(env, CrashBeforeFulfil(WorkItemClient())).dispatch_once()
        # Nobody fulfils for longer than FULFILMENT_WAIT: the loop ends.
        await env.sleep(timedelta(minutes=31))
        result = await env.client.get_workflow_handle_for(DevLoopWorkflow.run, wid).result()
        assert "not fulfilled" in result.ended and result.investigate.phase == "NotStarted"
        api.now += 3600
        outcome = await _dispatcher(env).dispatch_once()

    assert outcome.action == dx.REJECTED and outcome.reason == xr.ENGINE_RUN_ENDED
    assert api.request_state(rid)["reason"] == xr.ENGINE_RUN_ENDED
    assert seen == [] and api.executions == []


# -- the loop binds to exactly its own request and item ----------------------


async def _run_loop(env: WorkflowEnvironment, submit: Any, issue: IssueRef, workflow_id: str) -> Any:
    async with _worker(env, submit):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            issue,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        return await handle.result()


async def test_a_loop_refuses_an_execution_that_is_not_its_own_engine_run(api, env):
    """The request was fulfilled, but with another engine run: the loop
    must not adopt that `we_` (nor attach one of its own)."""
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    claim = WorkItemClient().claim_execution_request(60)
    assert claim.request is not None
    WorkItemClient().fulfil_execution_request(claim.request, claim.claim_token, "temporal", "somebody-else")

    result = await _run_loop(
        env, submit, IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid), dispatched_workflow_id(rid)
    )

    assert "work-item-mismatch" in result.ended and seen == []
    assert [e["engine_ref"] for e in api.executions] == ["somebody-else"]


async def test_a_loop_refuses_a_request_of_another_work_item(api, env):
    submit, seen = _investigate_log()
    rid = api.create_request("start", work_item_id=OTHER_WID)

    result = await _run_loop(
        env, submit, IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid), dispatched_workflow_id(rid)
    )

    assert "work-item-mismatch" in result.ended and OTHER_WID in result.ended
    assert seen == [] and api.executions == []


async def test_a_loop_refuses_an_item_that_is_about_another_issue(api, env):
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()
    assert outcome.action == dx.FULFILLED
    other_issue = "https://github.com/mctlhq/mctl-telegram/issues/9999"

    result = await _run_loop(
        env,
        submit,
        IssueRef(issue_url=other_issue, work_item_id=WID, execution_request_id=rid),
        dispatched_workflow_id(rid),
    )

    assert "work-item-mismatch" in result.ended and seen == []


async def test_a_rejected_request_ends_its_loop_without_running_anything(api, env):
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    claim = WorkItemClient().claim_execution_request(60)
    assert claim.request is not None
    WorkItemClient().reject_execution_request(claim.request, claim.claim_token, xr.LOOP_ACTIVE)

    result = await _run_loop(
        env, submit, IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid), dispatched_workflow_id(rid)
    )

    assert xr.LOOP_ACTIVE in result.ended and seen == [] and api.executions == []


# -- resume: onto a live loop versus a finished one ---------------------------


async def test_resume_onto_a_live_loop_is_refused_and_onto_a_finished_loop_continues(api, env):
    submit, seen = _investigate_log()
    first = api.create_request("start")
    first_loop = dispatched_workflow_id(first)
    async with _worker(env, submit):
        started = await _dispatcher(env).dispatch_once()
        assert started.action == dx.FULFILLED
        await _wait_for(lambda: len(seen) == 1)
        # The dispatched execution ended with its investigator run...
        await _wait_for(lambda: api.executions[0]["phase"] == "Succeeded")
        # ...but the loop lives on, parked at approval.
        desc = await env.client.get_workflow_handle(first_loop).describe()
        assert desc.status == WorkflowExecutionStatus.RUNNING

        live = api.create_request("resume")
        refused = await _dispatcher(env).dispatch_once()
        assert refused.action == dx.REJECTED
        assert api.request_state(live)["reason"] == xr.RESUME_ONTO_LIVE_LOOP_UNSUPPORTED
        assert len(api.executions) == 1 and len(seen) == 1

        await _end(env, first_loop)
        finished = api.create_request("resume")
        continued = await _dispatcher(env).dispatch_once()
        assert continued.action == dx.FULFILLED
        assert continued.workflow_id == dispatched_workflow_id(finished) != first_loop
        await _wait_for(lambda: len(seen) == 2)
        await _wait_for(lambda: api.executions[1]["phase"] == "Succeeded")
        await _end(env, continued.workflow_id)

    first_we, second_we = (e["id"] for e in api.executions)
    assert [p["execution_id"] for p in seen] == [first_we, second_we]
    assert [(e["engine_ref"], e["attempt"]) for e in api.executions] == [
        (first_loop, 1),
        (continued.workflow_id, 2),
    ]
    # The continuation passed through Pending (the /resume rule) to Running
    # and ended; the item moved on under the resume.
    assert api.state_version == 2


# -- offline end to end ------------------------------------------------------


async def test_offline_end_to_end_request_to_a_snapshot_sealed_for_the_dispatched_execution(
    api, env, monkeypatch, tmp_path, capsys
):
    """request -> claim -> DevLoop start -> fulfil -> the investigator runs
    under that `we_` -> a ContextSnapshot is sealed for that execution.

    The investigate submit runs the REAL investigator with exactly the
    arguments the CWFT builds from these parameters (`--issue-url`,
    `--work-item-id`, `--execution-id`), against the same fake store."""
    from datetime import UTC, datetime

    from orchestrator import run_issue_investigator
    from orchestrator.run_issue_investigator import investigate
    from orchestrator.work_context import executions as ex
    from orchestrator.work_context import rollout
    from tests.test_run_issue_investigator import _investigate_harness
    from tests.test_work_context_resume_acceptance import NUMBER, _Clock, _write_triplet

    for var in (
        ex.ENGINE_ENV_VAR,
        ex.ENGINE_REF_ENV_VAR,
        ex.WORKFLOW_NAME_ENV_VAR,
        ex.FINAL_ATTEMPT_ENV_VAR,
        rollout.REQUIRED_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(rollout.ENV_VAR, rollout.ENFORCE)
    monkeypatch.setenv("ISSUE_INVESTIGATOR_CONTEXT_MODE", "on")
    monkeypatch.setattr(run_issue_investigator, "_target_repository_sha", lambda repo_dir: "a" * 40)
    monkeypatch.setattr(_Clock, "current", datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(run_issue_investigator.context_assembly, "datetime", _Clock)
    _investigate_harness(tmp_path, monkeypatch, number=NUMBER, title="Dispatch acceptance", agent=_write_triplet)

    runs: list[tuple[dict, Any]] = []

    @activity.defn(name="submit_and_wait")
    async def submit(input: SubmitAndWaitInput) -> WorkflowResult:
        if input.operation != "mctl-agents-investigate":
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")
        p = input.params
        result = investigate(
            p["issue_url"], state_dir=tmp_path, work_item_id=p.get("work_item_id"), execution_id=p.get("execution_id")
        )
        runs.append((dict(p), result))
        ok = result.error is None and result.skipped_reason is None
        return WorkflowResult(workflow_name="mctl-agents-investigate-e2e", phase="Succeeded" if ok else "Failed")

    rid = api.create_request("start")
    loop_id = dispatched_workflow_id(rid)
    async with _worker(env, submit):
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED
        await _wait_for(lambda: len(runs) == 1, limit=60)
        await _wait_for(lambda: api.executions[0]["phase"] == "Succeeded")
        state = await env.client.get_workflow_handle(loop_id).query(DevLoopWorkflow.work_context)
        await _end(env, loop_id)

    we = outcome.execution_id
    params, result = runs[0]
    # The investigator saw the dispatched identity, and nothing else.
    assert params["work_item_id"] == WID and params["execution_id"] == we
    assert result.error is None and result.skipped_reason is None, result
    # Exactly one execution: the dispatcher's, attached at fulfilment and
    # ended by the loop; the investigator attached none of its own.
    assert [(e["id"], e["engine"], e["engine_ref"], e["phase"]) for e in api.executions] == [
        (we, "temporal", loop_id, "Succeeded"),
    ]
    # A snapshot sealed for exactly that execution, naming it.
    assert list(api.snapshots) == [we]
    wc = api.document(we)["work_context"]
    assert wc["work_item_id"] == WID and wc["execution_id"] == we and wc["execution_sequence"] == 1
    # The loop carries the same identity.
    assert state.work_item_id == WID and state.execution_id == we and state.execution_sequence == 1
    audit = _audit(capsys.readouterr().out)
    assert [a["event"] for a in audit] == ["claim", "start", "fulfil"]
    assert audit[-1]["execution_id"] == we


async def test_the_investigator_refuses_a_dispatched_execution_of_another_item(api, monkeypatch, tmp_path):
    """The other half of the binding: handed a `we_` that is not in the
    work item's ledger, the investigator refuses it rather than adopting
    it (`resolve_identity`, the existing check this dispatcher relies on)."""
    from orchestrator.work_context.client import WorkItemClient as Client
    from orchestrator.work_context.executions import resolve_identity

    answer = Client().get(WID)
    assert answer.item is not None
    identity = resolve_identity(answer.item, "we_99999999-0000-4000-8000-000000000000", Client())
    assert identity.execution_id == "" and "is not an execution of work item" in identity.refusal


# -- review round 1 (PR #468) ------------------------------------------------
#
# P2-1: every exit after a successful bind ends the dispatched execution.


def _failing_submit(error: BaseException | None = None, *, block: bool = False) -> tuple[Any, anyio.Event]:
    """An investigate submit that raises (or never returns, to be cancelled)."""
    entered = anyio.Event()

    @activity.defn(name="submit_and_wait")
    async def submit(input: SubmitAndWaitInput) -> WorkflowResult:
        if input.operation != "mctl-agents-investigate":
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")
        entered.set()
        if block:
            await anyio.sleep(10_000)
        raise error or ApplicationError("argo unreachable", non_retryable=True)

    return submit, entered


async def _dispatch_and_fail(api, env, submit, *, expect_failure=True, **fakes) -> tuple[str, Any]:
    """Dispatch one start request into a loop that fails; the loop's history."""
    from temporalio.client import WorkflowFailureError

    rid = api.create_request("start")
    wid = dispatched_workflow_id(rid)
    async with _worker(env, submit, **fakes):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        with pytest.raises(WorkflowFailureError):
            await env.client.get_workflow_handle(wid).result()
        history = await env.client.get_workflow_handle(wid).fetch_history()
    return wid, history


async def test_a_dispatched_loop_whose_investigate_submit_fails_still_ends_its_execution(api, env):
    submit, _ = _failing_submit()
    wid, history = await _dispatch_and_fail(api, env, submit)

    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(wid, "Failed")]
    # The failure path replays: its extra command is on the dispatched path only.
    from temporalio.worker import Replayer

    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(history)


async def test_a_dispatched_loop_without_a_pinned_investigator_still_ends_its_execution(api, env):
    submit, entered = _failing_submit()
    wid, _ = await _dispatch_and_fail(api, env, submit, unpinned={"issue-investigator"})

    assert not entered.is_set()  # `_require_release` raised before any submit
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(wid, "Failed")]


async def test_a_cancelled_dispatched_loop_still_ends_its_execution(api, env):
    from temporalio.client import WorkflowFailureError

    submit, entered = _failing_submit(block=True)
    rid = api.create_request("start")
    wid = dispatched_workflow_id(rid)
    async with _worker(env, submit):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(entered.is_set)
        assert api.executions[0]["phase"] == "Running"
        handle = env.client.get_workflow_handle(wid)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        desc = await handle.describe()

    assert desc.status == WorkflowExecutionStatus.CANCELED
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(wid, "Failed")]


async def test_the_dispatcher_fails_the_execution_of_a_terminated_dispatched_loop(api, env, capsys):
    """A terminated loop runs no code, so its `we_` stays Running and every
    later request would meet `execution_active`. The next dispatch fails it,
    and only it, once Temporal reports that loop closed."""
    submit, entered = _failing_submit(block=True)
    first = api.create_request("start")
    first_loop = dispatched_workflow_id(first)
    async with _worker(env, submit):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(entered.is_set)
        await env.client.get_workflow_handle(first_loop).terminate("operator")
        assert api.executions[0]["phase"] == "Running"

        second = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED, outcome
        await env.client.get_workflow_handle(outcome.workflow_id).terminate("test over")

    assert (api.executions[0]["engine_ref"], api.executions[0]["phase"]) == (first_loop, "Failed")
    assert api.executions[1]["engine_ref"] == dispatched_workflow_id(second)
    reconcile = [a for a in _audit(capsys.readouterr().out) if a["event"] == "reconcile"]
    assert [(a["workflow_id"], a["phase_was"]) for a in reconcile] == [(first_loop, "Running")]


def _ledger_entry(api: DispatchFakeApi, engine: str, ref: str, phase: str) -> None:
    api._next += 1
    api.executions.append(
        {
            "id": f"we_{api._next:08d}-0000-4000-8000-000000000000",
            "work_item_id": WID,
            "engine": engine,
            "engine_ref": ref,
            "attempt": len(api.executions) + 1,
            "phase": phase,
            "started_at": "2026-09-23T00:00:00Z",
        }
    )


async def test_the_reconciliation_never_touches_another_engine_or_a_non_dispatched_loop(api, capsys):
    """Non-terminal executions of an Argo run and of an issue-keyed loop that
    Temporal says is closed are left alone; the fulfil then meets
    `execution_active` and defers (the request stays claimed)."""
    _ledger_entry(api, "argo", "mctl-agents-investigate-abcde", "Succeeded")
    _ledger_entry(api, "temporal", workflow_id_for(URL), "Running")
    _ledger_entry(api, "argo", "mctl-agents-investigate-fghij", "Running")
    rid = api.create_request("resume")
    temporal = FakeTemporal({workflow_id_for(URL): dx.LOOP_CLOSED})

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED and "execution_active" in outcome.reason
    assert api.request_state(rid)["state"] == "claimed"
    assert [e["phase"] for e in api.executions] == ["Succeeded", "Running", "Running"]
    assert not [a for a in _audit(capsys.readouterr().out) if a["event"] == "reconcile"]
    assert not [p for m, p, b in api.requests if m == "POST" and p.endswith("/executions")]


async def test_the_reconciliation_leaves_a_running_dispatched_loop_alone(api):
    live = dispatched_workflow_id("xr_99999999-0000-4000-8000-000000000461")
    _ledger_entry(api, "temporal", live, "Running")
    api.create_request("resume")
    temporal = FakeTemporal({live: dx.LOOP_RUNNING})

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED and outcome.reason == xr.RESUME_ONTO_LIVE_LOOP_UNSUPPORTED
    assert api.executions[0]["phase"] == "Running"


# P2-2: every loop in the ledger is checked, not only the latest.


async def test_an_older_live_loop_is_found_behind_a_newer_closed_one(api):
    older = dispatched_workflow_id("xr_00000009-0000-4000-8000-000000000461")
    _ledger_entry(api, "temporal", older, "Succeeded")  # ended its run, still parked at approval
    _ledger_entry(api, "temporal", workflow_id_for(URL), "Succeeded")  # the label loop, finished
    rid = api.create_request("resume")
    temporal = FakeTemporal({older: dx.LOOP_RUNNING, workflow_id_for(URL): dx.LOOP_CLOSED})

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED
    assert api.request_state(rid)["reason"] == xr.RESUME_ONTO_LIVE_LOOP_UNSUPPORTED
    assert temporal.started == [] and api.fulfils() == []


# P2-3: only mctl-api's typed re-decisions reject; everything else defers.


async def test_a_typed_re_decision_at_fulfil_rejects_the_request(api):
    rid = api.create_request("start")
    api.state_version += 1  # the item moved after the surface asked

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED
    assert api.request_state(rid)["reason"] == f"{xr.FULFIL_REFUSED}:state_version_conflict"


async def test_an_untyped_400_at_fulfil_defers_instead_of_rejecting(api):
    rid = api.create_request("start")
    api.fail[("POST", f"/api/v1/execution-requests/{rid}/fulfil")] = _HTTPResult(
        400, {"code": "invalid_request", "error": "unknown field engine_ref"}
    )

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED
    assert api.request_state(rid)["state"] == "claimed" and api.executions == []


async def test_a_codeless_4xx_at_fulfil_defers_instead_of_rejecting(api):
    rid = api.create_request("start")
    api.fail[("POST", f"/api/v1/execution-requests/{rid}/fulfil")] = _HTTPResult(422, {})

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED and api.request_state(rid)["state"] == "claimed"


async def test_a_policy_checkpoint_deny_at_fulfil_defers_instead_of_rejecting(api, monkeypatch):
    from orchestrator import policy_checkpoint as pc

    real = pc.checkpoint

    def deny_fulfil(action_kind, operation, *args, **kwargs):
        if operation == xr.FULFIL_OPERATION:
            return pc.Decision(pc.DENY, pc.CODE_IDENTITY_UNAVAILABLE, "no sealed context", "v", "", "")
        return real(action_kind, operation, *args, **kwargs)

    monkeypatch.setattr(pc, "checkpoint", deny_fulfil)
    rid = api.create_request("start")

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED
    assert api.request_state(rid)["state"] == "claimed" and api.fulfils() == []


# P3-4: a definite refusal of the request read is a mismatch, not a retry.


async def test_a_refused_request_read_is_a_mismatch_not_a_thirty_minute_retry(api, env):
    submit, seen = _investigate_log()
    missing = "xr_77777777-0000-4000-8000-000000000461"  # the scoped route answers 404

    result = await _run_loop(
        env,
        submit,
        IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=missing),
        dispatched_workflow_id(missing),
    )

    assert "work-item-mismatch" in result.ended and "not fulfilled" not in result.ended
    assert seen == []
    reads = [p for m, p, _ in api.requests if m == "GET" and p.endswith(missing)]
    assert len(reads) == 1


# P3-5: the dispatcher is stopped gracefully, then cancelled, and always awaited.


async def test_stop_dispatcher_lets_the_loop_finish_within_the_grace():
    import asyncio

    class Idle:
        async def dispatch_once(self) -> dx.DispatchOutcome:
            return dx.DispatchOutcome(dx.NOTHING)

    stop = asyncio.Event()
    task = asyncio.create_task(dx.run_dispatcher(Idle(), stop, interval=3600))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    await worker_module.stop_dispatcher(task, stop, grace=5)
    assert task.done() and not task.cancelled()


async def test_stop_dispatcher_cancels_and_awaits_a_loop_that_does_not_stop():
    import asyncio

    stop = asyncio.Event()
    task = asyncio.create_task(asyncio.sleep(3600))
    await worker_module.stop_dispatcher(task, stop, grace=0.05)  # type: ignore[arg-type]
    assert stop.is_set() and task.cancelled()
