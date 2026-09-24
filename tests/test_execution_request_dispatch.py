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

import asyncio
import copy
import json
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
from orchestrator.temporal.issue_ref import request_engine_ref, workflow_id_for
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
        #: phase -> how many attaches advancing to it answer 503 first.
        self.unavailable_advances: dict[str, int] = {}

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
        phase = (payload or {}).get("phase")
        if method == "POST" and path == f"/api/v1/work-items/{WID}/executions" and self.unavailable_advances.get(phase):
            self.unavailable_advances[phase] -= 1
            self.requests.append((method, path, copy.deepcopy(payload)))
            return _HTTPResult(503, {"code": "unavailable", "error": "mctl-api restarting"})
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

    def __init__(
        self,
        states: dict[str, str] | None = None,
        *,
        delivery: dx.DeliveryAnswer | None = None,
        closes_after_delivery: bool = False,
        held: bool | None = True,
    ) -> None:
        self.states = states or {}
        #: What `holds_request` answers for every running loop.
        self.held = held
        #: The run that accepted the request closes before the fulfil.
        self.closes_after_delivery = closes_after_delivery
        #: What every `deliver` answers (default: accepted).
        self.delivery = delivery or dx.DeliveryAnswer(dx.DELIVERED)
        #: Every (IssueRef, ResumeDelivery) handed to Update-with-Start.
        self.delivered: list[tuple[IssueRef, Any]] = []

    async def deliver(self, issue: Any, delivery: Any) -> dx.DeliveryAnswer:
        self.delivered.append((issue, delivery))
        if self.delivery.verdict == dx.DELIVERED:
            # Update-with-Start reaches a running loop or starts one: a loop
            # that accepted a request runs, unless the test says it closed.
            closed = dx.LOOP_CLOSED if self.closes_after_delivery else dx.LOOP_RUNNING
            self.states[workflow_id_for(issue.issue_url)] = closed
        return self.delivery

    async def loop_state(self, workflow_id: str) -> str:
        return self.states.get(workflow_id, dx.LOOP_ABSENT)

    async def holds_request(self, workflow_id: str, request_id: str) -> bool | None:
        return self.held


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


def _delivery(rid: str, kind: str, work_item_id: str = WID) -> Any:
    from orchestrator.temporal.workflows.dev_loop import ResumeDelivery

    return ResumeDelivery(
        execution_request_id=rid,
        work_item_id=work_item_id,
        surface="telegram",
        actor_kind=dx.RESUME_ACTOR_KIND,
        actor_id="alice",
        kind=kind,
    )


def _audit(out: str) -> list[dict]:
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


def test_the_loop_is_the_issue_s_and_the_engine_ref_a_pure_function_of_loop_and_request():
    """#461 option A: one DevLoop per issue, whatever started it; the engine
    ref names the request on that loop, so a re-claim fulfils the same one."""
    rid = "xr_00000001-0000-4000-8000-000000000461"
    loop = workflow_id_for(URL)
    assert loop.startswith("dev-loop-mctlhq-mctl-telegram-") and "xr_" not in loop
    assert request_engine_ref(loop, rid) == request_engine_ref(loop, rid) == f"{loop}#{rid}"
    assert request_engine_ref(loop, rid) != request_engine_ref(loop, rid.replace("1-", "2-", 1))


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
    assert temporal.delivered == [] and api.fulfils() == [] and api.executions == []
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
    assert api.request_state(rid)["state"] == "claimed" and temporal.delivered == []


async def test_nothing_claimable_is_nothing(api):
    assert (await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()).action == dx.NOTHING


# -- engine ref too long: kind-neutral, before the loop (mctlhq/mctl-agents#488) --


def _over_long_issue_url() -> str:
    """A repo name long enough that `<loop>#<request id>` (the loop id
    `dev-loop-mctlhq-<repo>-<n>`, 16 fixed bytes, plus the 40-byte `#<xr_...>`
    suffix) overflows `MAX_ENGINE_REF_BYTES` (256) well past the ~199-char
    threshold design.md derives — GitHub's own 100-char repo cap keeps this
    unreachable in production, but the fake store enforces no such cap."""
    return f"https://github.com/mctlhq/{'r' * 220}/issues/1"


async def test_an_over_long_engine_ref_rejects_a_start_kind_neutrally(api):
    api.external_key = _over_long_issue_url()
    rid = api.create_request("start")

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED and outcome.reason == xr.ENGINE_REF_TOO_LONG
    assert not outcome.reason.startswith(xr.RESUME_REFUSED)
    assert api.request_state(rid)["reason"] == xr.ENGINE_REF_TOO_LONG


async def test_an_over_long_engine_ref_rejects_a_resume_with_the_same_reason(api):
    """Since #461 option A, this guard runs for `resume` exactly as for
    `start` — the same top-level, kind-neutral reason proves it."""
    api.external_key = _over_long_issue_url()
    rid = api.create_request("resume")

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED and outcome.reason == xr.ENGINE_REF_TOO_LONG
    assert not outcome.reason.startswith(xr.RESUME_REFUSED)
    assert api.request_state(rid)["reason"] == xr.ENGINE_REF_TOO_LONG


async def test_an_over_long_engine_ref_never_delivers_or_fulfils(api):
    api.external_key = _over_long_issue_url()
    api.create_request("start")
    temporal = FakeTemporal()

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED
    assert temporal.delivered == [] and api.fulfils() == [] and api.executions == []


async def test_a_reject_refused_by_an_older_mctl_api_falls_back_to_the_legacy_reason(api, capsys):
    """A version-skewed mctl-api that does not yet know `engine_ref_too_long`
    (the #488 companion vocabulary change not yet deployed) refuses the new
    reason; the dispatcher retries once with the legacy
    `resume_refused:engine-ref-too-long` spelling instead of leaving the
    request claimed forever."""
    api.external_key = _over_long_issue_url()
    rid = api.create_request("start")
    serve = api.request
    reject_path = f"/api/v1/execution-requests/{rid}/reject"
    calls: list[dict] = []

    def route(method: str, path: str, payload: dict | None = None) -> _HTTPResult:
        if method == "POST" and path == reject_path:
            body = payload or {}
            calls.append(body)
            if body.get("reason") == xr.ENGINE_REF_TOO_LONG:
                return _HTTPResult(400, {"code": "invalid_request", "error": "reason not recognised"})
        return serve(method, path, payload)

    api.request = route  # type: ignore[method-assign]

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    legacy = f"{xr.RESUME_REFUSED}:engine-ref-too-long"
    assert len(calls) == 2
    assert calls[0]["reason"] == xr.ENGINE_REF_TOO_LONG and calls[1]["reason"] == legacy
    assert outcome.action == dx.REJECTED and outcome.reason == legacy
    assert api.request_state(rid)["reason"] == legacy
    reject_lines = [a for a in _audit(capsys.readouterr().out) if a["event"] == "reject"]
    assert len(reject_lines) == 2


async def test_a_reject_refused_twice_defers_without_a_third_attempt(api):
    """Both spellings refused (some other problem, not a version skew): the
    fallback is one-shot, never a retry loop — the outcome defers, and
    exactly two reject attempts were made."""
    api.external_key = _over_long_issue_url()
    rid = api.create_request("start")
    serve = api.request
    reject_path = f"/api/v1/execution-requests/{rid}/reject"
    calls: list[dict] = []

    def route(method: str, path: str, payload: dict | None = None) -> _HTTPResult:
        if method == "POST" and path == reject_path:
            calls.append(payload or {})
            return _HTTPResult(400, {"code": "invalid_request", "error": "reason not recognised"})
        return serve(method, path, payload)

    api.request = route  # type: ignore[method-assign]

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED
    assert len(calls) == 2


def test_the_legacy_engine_ref_too_long_spelling_stays_retained_vocabulary():
    """#488: the dispatcher no longer mints `engine-ref-too-long` itself,
    but old rows written with it must still read back, never normalise to
    `unspecified`."""
    assert "engine-ref-too-long" in xr.RESUME_REFUSAL_REASONS


async def test_a_start_the_loop_refuses_is_rejected_loop_active(api, capsys):
    """Two investigations of one issue would race for one proposal: the
    loop's `LoopActive` answer rejects the request, before any fulfil."""
    rid = api.create_request("start")
    temporal = FakeTemporal(delivery=dx.DeliveryAnswer(dx.DELIVERY_LOOP_ACTIVE, "loop-active"))

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.REJECTED and api.request_state(rid)["reason"] == xr.LOOP_ACTIVE
    assert api.fulfils() == [] and api.executions == []
    [(issue, delivery)] = temporal.delivered
    assert issue.execution_request_id == rid and delivery.kind == "start"
    reject = _audit(capsys.readouterr().out)[-1]
    assert reject["event"] == "reject" and reject["live_loop"] == workflow_id_for(URL)


async def test_intake_first_a_start_request_joins_its_loop_and_is_refused_loop_active(api, env):
    """Acceptance 3 (#461 option A): the intake poller started the issue's
    loop; the request's Update-with-Start reaches THAT run (no second one)
    and its validator refuses a start it was not started for."""
    from orchestrator.temporal.start import start_dev_loop_workflow

    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        handle = await start_dev_loop_workflow(URL, client=env.client)
        run_id = (await handle.describe()).run_id
        await _wait_for(lambda: len(seen) == 1)
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.REJECTED and outcome.reason == xr.LOOP_ACTIVE
        assert (await env.client.get_workflow_handle(wid).describe()).run_id == run_id
        await _end(env, wid)

    assert len(seen) == 1 and "execution_id" not in seen[0]
    assert api.fulfils() == [] and api.executions == []
    assert api.request_state(rid)["reason"] == xr.LOOP_ACTIVE


async def test_dispatcher_first_the_intake_start_attaches_to_the_same_run(api, env):
    """Acceptance 2 (#461 option A): the dispatcher started the issue's loop;
    the intake poller's start of the same issue is a no-op that hands back
    that run (USE_EXISTING), so the issue still has one loop, one run, one
    investigation."""
    from orchestrator.temporal.start import start_dev_loop_workflow

    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.workflow_id == wid
        run_id = (await env.client.get_workflow_handle(wid).describe()).run_id
        joined = await start_dev_loop_workflow(URL, client=env.client)
        assert joined.id == wid and (await joined.describe()).run_id == run_id
        await _wait_for(lambda: len(seen) == 1)
        await anyio.sleep(0.5)
        await _end(env, wid)

    assert len(seen) == 1 and seen[0]["execution_request_id"] == rid
    assert [e["engine_ref"] for e in api.executions] == [request_engine_ref(wid, rid)]


async def test_intake_and_dispatcher_starting_one_issue_at_once_make_exactly_one_loop(api, env):
    """Acceptance 1 (#461 option A): the intake poller and the dispatcher
    start the same issue concurrently. Whichever reaches Temporal first owns
    the run; the other joins it: the dispatcher's `start` is then refused
    `loop_active` (never a second loop), or the intake's start is a no-op.
    Exactly one run, one investigation, and a request that is either
    fulfilled by that run or rejected, never left dangling."""
    from orchestrator.temporal.start import start_dev_loop_workflow

    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        outcome, handle = await asyncio.gather(
            _dispatcher(env).dispatch_once(), start_dev_loop_workflow(URL, client=env.client)
        )
        run_id = (await env.client.get_workflow_handle(wid).describe()).run_id
        assert (await handle.describe()).run_id == run_id
        await _wait_for(lambda: len(seen) == 1)
        await anyio.sleep(0.5)
        await _end(env, wid)

    assert len(seen) == 1
    if outcome.action == dx.FULFILLED:
        assert seen[0]["execution_request_id"] == rid
        assert [e["engine_ref"] for e in api.executions] == [request_engine_ref(wid, rid)]
    else:
        assert outcome.action == dx.REJECTED and api.request_state(rid)["reason"] == xr.LOOP_ACTIVE
        assert "execution_request_id" not in seen[0] and api.executions == []


async def test_intake_first_a_resume_request_is_delivered_to_that_same_run(api, env):
    """Acceptance 3, `resume` half: the loop the intake poller started takes
    a resume request through Update-with-Start (USE_EXISTING: the start
    input is ignored), binds its execution under `<loop>#<request>`, and no
    second run exists."""
    from orchestrator.temporal.start import start_dev_loop_workflow

    submit, seen = _investigate_log()
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        handle = await start_dev_loop_workflow(URL, client=env.client)
        run_id = (await handle.describe()).run_id
        await _wait_for(lambda: len(seen) == 1)
        rid = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == request_engine_ref(wid, rid), outcome
        await _wait_for(lambda: bool(api.executions) and api.executions[0]["phase"] == "Running")
        assert (await env.client.get_workflow_handle(wid).describe()).run_id == run_id
        await _end(env, wid)

    assert len(seen) == 1
    assert [e["engine_ref"] for e in api.executions] == [request_engine_ref(wid, rid)]


async def test_after_a_completed_loop_the_dispatcher_starts_a_new_run_and_the_intake_does_not(api, env):
    """Acceptance 8, the agreed reuse policy. A request after the issue's
    loop COMPLETED starts a new run of the same id (ALLOW_DUPLICATE: a
    resume of a finished loop is its continuation, and the request is an
    explicit ask); the intake poller keeps ALLOW_DUPLICATE_FAILED_ONLY, so a
    re-added label on a completed issue is still "already handled"."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from orchestrator.temporal.start import start_dev_loop_workflow

    submit, seen = _investigate_log()
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        first = await start_dev_loop_workflow(URL, client=env.client)
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, wid)
        assert (await first.describe()).status == WorkflowExecutionStatus.COMPLETED
        with pytest.raises(WorkflowAlreadyStartedError):
            await start_dev_loop_workflow(URL, client=env.client)

        rid = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.workflow_id == wid, outcome
        await _wait_for(lambda: len(seen) == 2)
        second = (await env.client.get_workflow_handle(wid).describe()).run_id
        await _end(env, wid)

    assert second != first.result_run_id
    assert seen[1]["execution_request_id"] == rid


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
    wid = workflow_id_for(URL)
    ref = request_engine_ref(wid, rid)
    async with _worker(env, submit):
        first = await _dispatcher(env).dispatch_once()
        assert first.action == dx.FULFILLED and first.workflow_id == wid and first.engine_ref == ref
        # A second claim finds nothing: the request is closed, not re-run.
        assert (await _dispatcher(env).dispatch_once()).action == dx.NOTHING
        # A duplicate Update-with-Start of the same request joins the same
        # run: the update id is the request id.
        run_id = (await env.client.get_workflow_handle(wid).describe()).run_id
        issue = IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid)
        again = await dx.TemporalClientPort(env.client).deliver(issue, _delivery(rid, "start"))
        assert again.verdict == dx.DELIVERED
        assert (await env.client.get_workflow_handle(wid).describe()).run_id == run_id
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, wid)
        # The request is fulfilled, so closed for good: never claimed again.
        assert (await _dispatcher(env).dispatch_once()).action == dx.NOTHING

    assert len(seen) == 1
    assert [(e["engine"], e["engine_ref"]) for e in api.executions] == [("temporal", ref)]
    assert seen[0]["execution_id"] == first.execution_id == api.executions[0]["id"]
    audit = _audit(capsys.readouterr().out)
    assert [a["event"] for a in audit] == ["claim", "deliver", "fulfil"]
    # Every line carries the ids; the fulfil line carries the execution.
    assert all(a["execution_request_id"] == rid and a["work_item_id"] == WID for a in audit)
    assert all(a["workflow_id"] == wid for a in audit[1:])
    assert audit[-1]["execution_id"] == first.execution_id and audit[-1]["engine_ref"] == ref


async def test_a_lost_fulfil_answer_is_retried_onto_the_same_execution(api, env):
    """The store committed, the answer was lost: the same holder repeating
    the same engine run must get the same `we_`, never a second one."""
    submit, seen = _investigate_log()
    api.create_request("start")
    api.lose_next_fulfil_answer = True
    async with _worker(env, submit):
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, workflow_id_for(URL))

    fulfils = api.fulfils()
    assert len(fulfils) == 2 and fulfils[0] == fulfils[1]
    assert len(api.executions) == 1 and outcome.execution_id == api.executions[0]["id"]


async def test_a_crash_between_start_and_fulfil_converges_on_the_same_run(api, env, capsys):
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
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
    assert [(e["engine"], e["engine_ref"]) for e in api.executions] == [("temporal", request_engine_ref(wid, rid))]
    assert [a["event"] for a in _audit(capsys.readouterr().out)] == ["claim", "deliver", "claim", "deliver", "fulfil"]


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
        await _end(env, workflow_id_for(URL))

    assert len(api.executions) == 1 and seen[0]["execution_id"] == converged.execution_id


async def test_a_request_whose_first_run_gave_up_runs_once_on_a_new_run(api, env, capsys):
    """The loop gave up waiting for its fulfilment and ended having run
    nothing. A later claim of the (still unfulfilled) request starts the
    issue's loop again and runs it there: exactly one investigator and one
    execution for the request, never bound to the dead run."""
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        with pytest.raises(Crash):
            await _dispatcher(env, CrashBeforeFulfil(WorkItemClient())).dispatch_once()
        first_run = (await env.client.get_workflow_handle(wid).describe()).run_id
        # Nobody fulfils for longer than FULFILMENT_WAIT: the loop ends.
        await env.sleep(timedelta(minutes=31))
        result = await ended_without_running(env.client.get_workflow_handle(wid, run_id=first_run))
        assert "not fulfilled" in result.ended
        assert seen == [] and api.executions == []
        api.now += 3600
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == request_engine_ref(wid, rid)
        assert (await env.client.get_workflow_handle(wid).describe()).run_id != first_run
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, wid)

    assert len(seen) == 1 and seen[0]["execution_id"] == outcome.execution_id
    assert [e["engine_ref"] for e in api.executions] == [request_engine_ref(wid, rid)]
    assert api.request_state(rid)["state"] == "fulfilled"


async def test_a_dispatched_run_that_ran_nothing_leaves_the_issue_startable_by_the_intake_label(api, env):
    """Review P2 on #487: the dispatched run shares the issue's id, and the
    intake poller starts it with ALLOW_DUPLICATE_FAILED_ONLY. A run that
    ended having run nothing must therefore not end COMPLETED, or every
    later `agents:intake` label on the issue is a silent "already handled".
    It ends FAILED, and the label starts the issue's loop again."""
    from orchestrator.temporal.start import start_dev_loop_workflow

    submit, seen = _investigate_log()
    rid = "xr_77777777-0000-4000-8000-000000000461"  # never fulfilled: the read is refused
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid),
            id=wid,
            task_queue=TASK_QUEUE,
        )
        await ended_without_running(handle)
        assert (await handle.describe()).status == WorkflowExecutionStatus.FAILED
        relabelled = await start_dev_loop_workflow(URL, client=env.client)
        await _wait_for(lambda: len(seen) == 1)
        assert (await relabelled.describe()).run_id != handle.result_run_id
        await _end(env, wid)

    assert "execution_request_id" not in seen[0]


# -- the loop binds to exactly its own request and item ----------------------


async def ended_without_running(handle: Any) -> Any:
    """The end of a dispatched run that ran nothing: FAILED with
    `DispatchedRequestNotRun` (#461 option A), so the issue's id stays
    startable by the intake label. Its message is the reason, as `ended`."""
    from types import SimpleNamespace

    from temporalio.client import WorkflowFailureError

    from orchestrator.temporal.workflows.dev_loop import DISPATCHED_NOT_RUN_ERROR_TYPE

    with pytest.raises(WorkflowFailureError) as failed:
        await handle.result()
    cause = failed.value.cause
    assert isinstance(cause, ApplicationError) and cause.type == DISPATCHED_NOT_RUN_ERROR_TYPE, cause
    return SimpleNamespace(ended=cause.message)


async def _run_loop(env: WorkflowEnvironment, submit: Any, issue: IssueRef, workflow_id: str) -> Any:
    async with _worker(env, submit):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            issue,
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        return await ended_without_running(handle)


async def test_a_loop_refuses_an_execution_that_is_not_its_own_engine_run(api, env):
    """The request was fulfilled, but with another engine run: the loop
    must not adopt that `we_` (nor attach one of its own)."""
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    claim = WorkItemClient().claim_execution_request(60)
    assert claim.request is not None
    WorkItemClient().fulfil_execution_request(claim.request, claim.claim_token, "temporal", "somebody-else")

    result = await _run_loop(
        env, submit, IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid), workflow_id_for(URL)
    )

    assert "work-item-mismatch" in result.ended and seen == []
    assert [e["engine_ref"] for e in api.executions] == ["somebody-else"]


async def test_a_loop_refuses_a_request_of_another_work_item(api, env):
    submit, seen = _investigate_log()
    rid = api.create_request("start", work_item_id=OTHER_WID)

    result = await _run_loop(
        env, submit, IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid), workflow_id_for(URL)
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
        workflow_id_for(URL),
    )

    assert "work-item-mismatch" in result.ended and seen == []
    # The fulfil minted it under this loop's own ref, so the loop ends it
    # rather than leave the item wedged behind a Running execution.
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [
        (request_engine_ref(workflow_id_for(URL), rid), "Failed")
    ]


async def test_a_loop_whose_advance_to_running_is_refused_ends_its_execution(api, env):
    """Its own `we_`, proven by the ledger, but the advance to Running is a
    definite no: the loop runs nothing, and ends the execution `Failed`."""
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    ref = request_engine_ref(wid, rid)
    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()
    assert outcome.action == dx.FULFILLED
    serve = api.request

    def refuse_running(method: str, path: str, payload: dict | None = None) -> _HTTPResult:
        body = payload or {}
        if method == "POST" and (body.get("engine_ref"), body.get("phase")) == (ref, "Running"):
            return _HTTPResult(422, {"code": "policy_denied", "error": "not this one"})
        return serve(method, path, payload)

    api.request = refuse_running  # type: ignore[method-assign]
    result = await _run_loop(env, submit, IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid), wid)

    assert "execution-refused" in result.ended and "to Running" in result.ended and seen == []
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(ref, "Failed")]


async def test_a_rejected_request_ends_its_loop_without_running_anything(api, env):
    submit, seen = _investigate_log()
    rid = api.create_request("start")
    claim = WorkItemClient().claim_execution_request(60)
    assert claim.request is not None
    WorkItemClient().reject_execution_request(claim.request, claim.claim_token, xr.LOOP_ACTIVE)

    result = await _run_loop(
        env, submit, IssueRef(issue_url=URL, work_item_id=WID, execution_request_id=rid), workflow_id_for(URL)
    )

    assert xr.LOOP_ACTIVE in result.ended and seen == [] and api.executions == []


# -- resume: onto a live loop versus a finished one ---------------------------


async def test_resume_onto_a_live_loop_is_delivered_and_onto_a_finished_loop_continues(api, env):
    submit, seen = _investigate_log()
    first = api.create_request("start")
    first_loop = workflow_id_for(URL)
    async with _worker(env, submit):
        started = await _dispatcher(env).dispatch_once()
        assert started.action == dx.FULFILLED
        first_run = (await env.client.get_workflow_handle(first_loop).describe()).run_id
        await _wait_for(lambda: len(seen) == 1)
        # The dispatched execution ended with its investigator run...
        await _wait_for(lambda: api.executions[0]["phase"] == "Succeeded")
        # ...but the loop lives on, parked at approval.
        desc = await env.client.get_workflow_handle(first_loop).describe()
        assert desc.status == WorkflowExecutionStatus.RUNNING

        live = api.create_request("resume")
        delivered = await _dispatcher(env).dispatch_once()
        # Delivered to the live loop and fulfilled for it: no second loop,
        # no second investigation; the loop binds the `we_` itself.
        assert delivered.action == dx.FULFILLED and delivered.engine_ref == f"{first_loop}#{live}"
        await _wait_for(lambda: api.executions[1]["phase"] == "Running")
        assert len(seen) == 1

        # The loop ends before its re-approval: the resumed execution fails.
        await _end(env, first_loop)
        assert api.executions[1]["phase"] == "Failed"

        finished = api.create_request("resume")
        continued = await _dispatcher(env).dispatch_once()
        assert continued.action == dx.FULFILLED
        # A continuation is a new run of the SAME issue-keyed loop.
        assert continued.workflow_id == first_loop
        assert (await env.client.get_workflow_handle(first_loop).describe()).run_id != first_run
        await _wait_for(lambda: len(seen) == 2)
        await _wait_for(lambda: api.executions[2]["phase"] == "Succeeded")
        await _end(env, continued.workflow_id)

    first_we, _resumed_we, continued_we = (e["id"] for e in api.executions)
    assert [p["execution_id"] for p in seen] == [first_we, continued_we]
    assert [(e["engine_ref"], e["attempt"]) for e in api.executions] == [
        (f"{first_loop}#{first}", 1),
        (f"{first_loop}#{live}", 2),
        (f"{first_loop}#{finished}", 3),
    ]
    # Both resumes passed through Pending (the /resume rule) and moved the item.
    assert api.state_version == 3


# -- offline end to end ------------------------------------------------------


# The investigate CWFT's optional parameters, as it forwards them: each one
# becomes its flag only when non-empty (gitops#1279, gitops#1345).
_CWFT_FORWARDED = ("work_item_id", "execution_id", "temporal_workflow_id", "temporal_run_id", "execution_request_id")


def _run_real_investigator(params: dict, state_dir: Any) -> Any:
    """What the investigate pod does with `params`: the real investigator,
    handed exactly the flags the CWFT would build from them."""
    from orchestrator.run_issue_investigator import investigate

    forwarded = {name: params[name] for name in _CWFT_FORWARDED if params.get(name)}
    return investigate(params["issue_url"], state_dir=state_dir, **forwarded)


def _real_investigator_setup(monkeypatch, tmp_path) -> list[dict]:
    """Enforce-mode work context, context assembly `on`, a stubbed clone and
    agent; returns the list every posted proposal comment is appended to
    (the real `post_proposal_comment`, with only `gh` captured)."""
    from datetime import UTC, datetime

    from orchestrator import run_issue_investigator
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
    real_comment = run_issue_investigator.post_proposal_comment
    _investigate_harness(tmp_path, monkeypatch, number=NUMBER, title="Dispatch acceptance", agent=_write_triplet)
    comments: list[dict] = []

    def capture_gh(cmd, **kw):
        comments.append({"body": cmd[cmd.index("--body") + 1]})

    def post(issue_url, service, slug, **kwargs):
        # `_run` swapped only for the comment itself: everything else the
        # investigator runs is untouched.
        real_run = run_issue_investigator._run
        run_issue_investigator._run = capture_gh
        try:
            real_comment(issue_url, service, slug, **kwargs)
        finally:
            run_issue_investigator._run = real_run

    monkeypatch.setattr(run_issue_investigator, "post_proposal_comment", post)
    return comments


async def test_offline_end_to_end_request_to_a_snapshot_sealed_for_the_dispatched_execution(
    api, env, monkeypatch, tmp_path, capsys
):
    """request -> claim -> DevLoop start -> fulfil -> the investigator runs
    under that `we_` -> a ContextSnapshot is sealed for that execution.

    The investigate submit runs the REAL investigator with exactly the
    arguments the CWFT builds from these parameters (`--issue-url`,
    `--work-item-id`, `--execution-id`, and the loop's own
    `--temporal-workflow-id` / `--temporal-run-id` / `--execution-request-id`),
    against the same fake store."""
    comments = _real_investigator_setup(monkeypatch, tmp_path)
    runs: list[tuple[dict, Any]] = []

    @activity.defn(name="submit_and_wait")
    async def submit(input: SubmitAndWaitInput) -> WorkflowResult:
        if input.operation != "mctl-agents-investigate":
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")
        p = input.params
        result = _run_real_investigator(p, tmp_path)
        runs.append((dict(p), result))
        ok = result.error is None and result.skipped_reason is None
        return WorkflowResult(workflow_name="mctl-agents-investigate-e2e", phase="Succeeded" if ok else "Failed")

    rid = api.create_request("start")
    loop_id = workflow_id_for(URL)
    async with _worker(env, submit):
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED
        await _wait_for(lambda: len(runs) == 1, limit=60)
        await _wait_for(lambda: api.executions[0]["phase"] == "Succeeded")
        handle = env.client.get_workflow_handle(loop_id)
        state = await handle.query(DevLoopWorkflow.work_context)
        run_id = (await handle.describe()).run_id
        await _end(env, loop_id)

    we = outcome.execution_id
    params, result = runs[0]
    # The investigator saw the dispatched identity, and its loop's own ids.
    assert params["work_item_id"] == WID and params["execution_id"] == we
    assert params["temporal_workflow_id"] == loop_id and params["temporal_run_id"] == run_id
    assert params["execution_request_id"] == rid
    assert result.error is None and result.skipped_reason is None, result
    # Exactly one execution: the dispatcher's, attached at fulfilment and
    # ended by the loop; the investigator attached none of its own.
    assert [(e["id"], e["engine"], e["engine_ref"], e["phase"]) for e in api.executions] == [
        (we, "temporal", request_engine_ref(loop_id, rid), "Succeeded"),
    ]
    # A snapshot sealed for exactly that execution, naming it.
    assert list(api.snapshots) == [we]
    wc = api.document(we)["work_context"]
    assert wc["work_item_id"] == WID and wc["execution_id"] == we and wc["execution_sequence"] == 1
    # ...correlated to the loop that really ran it (#461 gap 2, #451).
    sealed = api.document(we)["execution"]
    assert sealed["temporal_workflow_id"] == loop_id and sealed["temporal_run_id"] == run_id
    # The approve instructions name that same loop.
    assert len(comments) == 1
    assert f"/dev-loop/{loop_id}/approve" in comments[0]["body"]
    assert f"cli approve {loop_id} " in comments[0]["body"]
    # The loop carries the same identity.
    assert state.work_item_id == WID and state.execution_id == we and state.execution_sequence == 1
    audit = _audit(capsys.readouterr().out)
    assert [a["event"] for a in audit] == ["claim", "deliver", "fulfil"]
    assert audit[-1]["execution_id"] == we


async def test_a_dispatched_loop_recognises_the_clarification_its_own_investigator_sealed(
    api, env, monkeypatch, tmp_path
):
    """#461 gap 2 / #451, end to end: the real investigator, run with the
    flags the CWFT builds from the dispatched loop's params, seals the
    correlation a clarification request carries; the dispatched loop must
    take that request as its own and park on it.

    Before the loop passed its ids, the investigator derived them itself,
    and `_await_human_input` retired a request whose correlation named
    another run as a foreign execution's: the clarification was silently
    lost and the loop went on to the approval park."""
    from datetime import UTC, datetime

    from orchestrator import human_input as hi
    from orchestrator.context_snapshot import ExecutionCorrelation

    _real_investigator_setup(monkeypatch, tmp_path)
    requests: list[str | None] = [None]
    runs: list[dict] = []

    @activity.defn(name="submit_and_wait")
    async def submit(input: SubmitAndWaitInput) -> WorkflowResult:
        if input.operation != "mctl-agents-investigate":
            return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")
        p = dict(input.params)
        runs.append(p)
        result = _run_real_investigator(p, tmp_path)
        assert result.error is None and result.skipped_reason is None, result
        if len(runs) == 1:
            # The clarification the agent asked for in this run, sealed
            # under the correlation this run's snapshot carries.
            execution = ExecutionCorrelation.from_dict(api.document(p["execution_id"])["execution"])
            now = datetime.now(UTC)
            request = hi.seal_request(
                work_item_id=WID,
                execution=execution,
                question="Library A or library B?",
                reason="the issue names both",
                response=hi.ResponseSpec(type="free_text"),
                requested_from=hi.RequestedFrom(audience="work_item_owner", actor_refs=("github:alice",)),
                created_at=now.isoformat(),
                expires_at=(now + timedelta(days=7)).isoformat(),
            )
            requests[0] = json.dumps(request.to_dict())
        return WorkflowResult(workflow_name="mctl-agents-investigate-e2e", phase="Succeeded")

    api.create_request("start")
    loop_id = workflow_id_for(URL)
    async with _worker(env, submit, human_input_requests=requests):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(lambda: len(runs) == 1, limit=60)
        handle = env.client.get_workflow_handle(loop_id)
        await _wait_for(lambda: requests[0] is not None, limit=60)
        state = None
        for _ in range(400):
            state = await handle.query(DevLoopWorkflow.human_input_state)
            if state.state == "WAITING_FOR_INPUT":
                break
            await anyio.sleep(0.05)
        await _end(env, loop_id)

    # The loop took the request as its own and parked on it...
    assert state is not None and state.state == "WAITING_FOR_INPUT", state
    assert state.request_id == json.loads(requests[0])["request_id"]
    # ...because it carries this loop's own ids.
    sealed = json.loads(requests[0])["execution"]
    assert sealed["temporal_workflow_id"] == loop_id


async def test_an_issue_keyed_loop_passes_its_own_ids_and_no_request_id(api, env):
    """Every loop names itself to its investigator (#451: the run id retires a
    same-id leftover); only a dispatched one has a request to name."""
    submit, seen = _investigate_log()
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run, IssueRef(issue_url=URL), id=wid, task_queue=TASK_QUEUE
        )
        await _wait_for(lambda: len(seen) == 1)
        await _end(env, wid)
    assert seen[0]["temporal_workflow_id"] == wid
    assert seen[0]["temporal_run_id"] == handle.result_run_id
    assert "execution_request_id" not in seen[0] and "execution_id" not in seen[0]


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
    """Dispatch one start request into a loop that fails: the request's
    engine ref, and the loop's history."""
    from temporalio.client import WorkflowFailureError

    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    async with _worker(env, submit, **fakes):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        with pytest.raises(WorkflowFailureError):
            await env.client.get_workflow_handle(wid).result()
        history = await env.client.get_workflow_handle(wid).fetch_history()
    return request_engine_ref(wid, rid), history


async def test_a_dispatched_loop_whose_investigate_submit_fails_still_ends_its_execution(api, env):
    submit, _ = _failing_submit()
    ref, history = await _dispatch_and_fail(api, env, submit)

    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(ref, "Failed")]
    # The failure path replays: its extra command is on the dispatched path only.
    from temporalio.worker import Replayer

    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(history)


async def test_a_dispatched_loop_without_a_pinned_investigator_still_ends_its_execution(api, env):
    submit, entered = _failing_submit()
    ref, _ = await _dispatch_and_fail(api, env, submit, unpinned={"issue-investigator"})

    assert not entered.is_set()  # `_require_release` raised before any submit
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(ref, "Failed")]


async def test_a_cancelled_dispatched_loop_still_ends_its_execution(api, env):
    from temporalio.client import WorkflowFailureError

    submit, entered = _failing_submit(block=True)
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
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
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(request_engine_ref(wid, rid), "Failed")]


async def test_the_dispatcher_fails_the_execution_of_a_terminated_dispatched_loop(api, env, capsys):
    """A terminated loop runs no code, so its `we_` stays Running and every
    later request would meet `execution_active`. The next dispatch fails it,
    and only it, once Temporal reports that loop closed; the resume then
    starts a new run of the same issue loop."""
    submit, entered = _failing_submit(block=True)
    first = api.create_request("start")
    loop = workflow_id_for(URL)
    async with _worker(env, submit):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(entered.is_set)
        await env.client.get_workflow_handle(loop).terminate("operator")
        assert api.executions[0]["phase"] == "Running"

        second = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.workflow_id == loop, outcome
        await env.client.get_workflow_handle(loop).terminate("test over")

    first_ref = request_engine_ref(loop, first)
    assert (api.executions[0]["engine_ref"], api.executions[0]["phase"]) == (first_ref, "Failed")
    assert api.executions[1]["engine_ref"] == request_engine_ref(loop, second)
    reconcile = [a for a in _audit(capsys.readouterr().out) if a["event"] == "reconcile"]
    assert [(a["engine_ref"], a["phase_was"]) for a in reconcile] == [(first_ref, "Running")]


async def test_a_later_run_of_the_same_loop_does_not_mask_a_terminated_run_s_execution(api, env, capsys):
    """Review P2 on #487: the loop id is the issue's, shared by every run. An
    operator terminates the run that holds `xr_A`'s execution; the intake
    label starts a NEW run of the same id, which never took `xr_A` and will
    never end it. "The loop is running" must not shield that execution: the
    reconciliation asks the running run whether it holds the request, and
    fails the execution when it does not."""
    from orchestrator.temporal.start import start_dev_loop_workflow

    submit, entered = _failing_submit(block=True)
    first = api.create_request("start")
    loop = workflow_id_for(URL)
    first_ref = request_engine_ref(loop, first)
    async with _worker(env, submit):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await _wait_for(entered.is_set)
        await env.client.get_workflow_handle(loop).terminate("operator")
        relabelled = await start_dev_loop_workflow(URL, client=env.client)
        assert (await relabelled.describe()).status == WorkflowExecutionStatus.RUNNING
        assert await dx.TemporalClientPort(env.client).holds_request(loop, first) is False
        assert api.executions[0]["phase"] == "Running"

        api.create_request("resume")
        await _dispatcher(env).dispatch_once()
        await env.client.get_workflow_handle(loop).terminate("test over")

    assert (api.executions[0]["engine_ref"], api.executions[0]["phase"]) == (first_ref, "Failed")
    reconcile = [a for a in _audit(capsys.readouterr().out) if a["event"] == "reconcile"]
    assert [(a["engine_ref"], a["phase_was"]) for a in reconcile] == [(first_ref, "Running")]


@pytest.mark.parametrize(("held", "reconciled"), [(True, False), (None, False), (False, True)])
async def test_the_reconciliation_of_a_running_loop_follows_whether_its_run_holds_the_request(api, held, reconciled):
    """Only a definite "this run never took it" fails the execution of a
    RUNNING loop; a run that holds it, or one that cannot say (the query
    failed, or the run predates it), is left alone."""
    loop = workflow_id_for(URL)
    ref = request_engine_ref(loop, "xr_99999999-0000-4000-8000-000000000461")
    _ledger_entry(api, "temporal", ref, "Running")
    api.create_request("resume")
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING}, held=held)

    await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert (api.executions[0]["phase"] == "Failed") is reconciled


class _HoldsInTurn(FakeTemporal):
    """A FakeTemporal whose `holds_request` answers from `answers` in turn."""

    def __init__(self, answers: list[bool | None], **kwargs: Any) -> None:
        super().__init__({workflow_id_for(URL): dx.LOOP_RUNNING}, **kwargs)
        self.answers = answers

    async def holds_request(self, workflow_id: str, request_id: str) -> bool | None:
        return self.answers.pop(0)


async def test_a_new_run_started_after_the_accepting_run_closed_is_not_taken_for_it(api, capsys):
    """Review round 2 P2 on #487, before the fulfil: the run that accepted
    the request closed and another starter began a new run of the shared id.
    The loop is RUNNING, but its run never took the request: the delivery
    is stale, nothing is fulfilled, the request stays claimed."""
    rid = api.create_request("start")
    temporal = _HoldsInTurn([False])

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.DEFERRED and "closed after accepting" in outcome.reason
    assert api.fulfils() == [] and api.request_state(rid)["state"] == "claimed"
    assert "deliver_stale" in [a["event"] for a in _audit(capsys.readouterr().out)]


async def test_an_execution_minted_after_the_accepting_run_was_replaced_is_ended_by_the_dispatcher(api, capsys):
    """Review round 2 P2 on #487, after the fulfil: the accepting run was
    replaced by a new run of the shared id between the check and the
    fulfil. The new run will never bind the minted execution, so the
    dispatcher ends it, as it does for a loop that closed."""
    rid = api.create_request("start")
    temporal = _HoldsInTurn([True, False])

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert outcome.action == dx.FULFILLED
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [
        (request_engine_ref(workflow_id_for(URL), rid), "Failed")
    ]
    assert [a["event"] for a in _audit(capsys.readouterr().out)][-1] == "orphan_failed"


@pytest.mark.parametrize(
    ("state", "reconciled"), [(dx.LOOP_RUNNING, False), (dx.LOOP_CLOSED, True), (dx.LOOP_ABSENT, True)]
)
async def test_a_pre_option_a_dispatched_loop_s_execution_is_still_reconciled(api, capsys, state, reconciled):
    """An execution bound under a pre-option-A `dev-loop-xr_<id>` loop's
    bare id (agy round 2 on #487): no such loop is started any more, but one
    that closed without ending its execution must not wedge the item."""
    old = "dev-loop-xr_00000009-0000-4000-8000-000000000461"
    _ledger_entry(api, "temporal", old, "Running")
    api.create_request("resume")
    temporal = FakeTemporal({old: state}, held=False)

    await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert (api.executions[0]["phase"] == "Failed") is reconciled
    reconcile = [a for a in _audit(capsys.readouterr().out) if a["event"] == "reconcile"]
    assert [a["engine_ref"] for a in reconcile] == ([old] if reconciled else [])


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
    live = workflow_id_for(URL)
    _ledger_entry(api, "temporal", request_engine_ref(live, "xr_99999999-0000-4000-8000-000000000461"), "Running")
    api.create_request("resume")
    temporal = FakeTemporal({live: dx.LOOP_RUNNING})

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    # Delivered to the live loop, whose execution is untouched; mctl-api's
    # fulfil then refuses while that execution runs, which defers.
    assert [workflow_id_for(issue.issue_url) for issue, _ in temporal.delivered] == [live]
    assert outcome.action == dx.DEFERRED and "execution_active" in outcome.reason
    assert api.executions[0]["phase"] == "Running"


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
        workflow_id_for(URL),
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


# -- review round 2 (PR #468) ------------------------------------------------
#
# P2: the success-path advance is patient, and re-attempted before the park.


def _advance_attempts(api: DispatchFakeApi, phase: str) -> int:
    path = f"/api/v1/work-items/{WID}/executions"
    return len([b for m, p, b in api.requests if m == "POST" and p == path and (b or {}).get("phase") == phase])


def _scheduled_advances(history: Any) -> int:
    return sum(
        1
        for e in history.to_json_dict()["events"]
        if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
        and e["activityTaskScheduledEventAttributes"]["activityType"]["name"] == "advance_dispatched_execution"
    )


async def _run_to_the_park(api, env, unavailable: int) -> tuple[str, Any]:
    submit, seen = _investigate_log()
    api.unavailable_advances["Succeeded"] = unavailable
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    async with _worker(env, submit):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        # Skip past every retry backoff; the loop then sits at approval.
        await env.sleep(timedelta(hours=3))
        handle = env.client.get_workflow_handle(wid)
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        history = await handle.fetch_history()
        await _end(env, wid)
    assert len(seen) == 1
    return request_engine_ref(wid, rid), history


async def test_a_transient_advance_failure_on_the_success_path_ends_succeeded_before_the_park(api, env):
    """mctl-api unavailable for longer than FAST's five attempts: the patient
    policy still lands the advance, in ONE activity, before the park."""
    ref, history = await _run_to_the_park(api, env, unavailable=7)

    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(ref, "Succeeded")]
    assert _advance_attempts(api, "Succeeded") == 8
    assert _scheduled_advances(history) == 1


async def test_an_advance_that_outlasts_its_retries_is_re_attempted_before_the_park(api, env):
    """Unavailable past the whole patient policy: the loop goes on (best
    effort), re-attempts once before the approval park, and that lands."""
    ref, history = await _run_to_the_park(api, env, unavailable=12)

    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(ref, "Succeeded")]
    assert _scheduled_advances(history) == 2
    from temporalio.worker import Replayer

    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(history)


# P3: an absent dispatched loop is reconciled like a closed one.


async def test_the_reconciliation_fails_the_execution_of_a_loop_temporal_no_longer_knows(api, capsys):
    gone = request_engine_ref(workflow_id_for(URL), "xr_99999999-0000-4000-8000-000000000461")
    _ledger_entry(api, "temporal", gone, "Running")
    api.create_request("resume")
    temporal = FakeTemporal()  # every loop ABSENT: retention expired

    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()

    assert api.executions[0]["phase"] == "Failed"
    assert outcome.action == dx.FULFILLED
    reconcile = [a for a in _audit(capsys.readouterr().out) if a["event"] == "reconcile"]
    assert [a["engine_ref"] for a in reconcile] == [gone]


# P3: a reject answered `execution_request_closed` is CLOSED, not DEFERRED.


async def test_a_reject_that_finds_the_request_closed_reports_closed(api):
    api.external_key = ""  # no runnable target: the dispatcher rejects
    rid = api.create_request("start")
    api.fail[("POST", f"/api/v1/execution-requests/{rid}/reject")] = _HTTPResult(
        409, {"code": xr.CLOSED_CODE, "error": "closed", "details": {"execution_id": "we_x"}}
    )

    outcome = await dx.Dispatcher(WorkItemClient(), FakeTemporal(), lease=60).dispatch_once()

    assert outcome.action == dx.CLOSED and outcome.execution_id == "we_x"


# -- review round 3 (PR #468) ------------------------------------------------
#
# P2: a pending advance is re-attempted before EVERY park, including the
# human-input one.


async def test_an_advance_that_outlasts_its_retries_lands_before_a_human_input_park(api, env):
    from datetime import UTC, datetime

    from tests.test_dev_loop_workflow import _request_json, _sealed_request

    submit, seen = _investigate_log()
    # More than the whole patient policy (10), fewer than it plus the brief
    # pre-park attempt (4): only that attempt can land it.
    api.unavailable_advances["Succeeded"] = 12
    rid = api.create_request("start")
    wid = workflow_id_for(URL)
    # The clarification this run's investigator sealed, for THIS loop.
    request = _sealed_request(workflow_id=wid, created=datetime.now(UTC), ttl_seconds=7 * 24 * 3600)
    async with _worker(env, submit, human_input_requests=[_request_json(request), None]):
        assert (await _dispatcher(env).dispatch_once()).action == dx.FULFILLED
        await env.sleep(timedelta(hours=3))
        handle = env.client.get_workflow_handle(wid)
        state = await handle.query(DevLoopWorkflow.human_input_state)
        history = await handle.fetch_history()
        await _end(env, wid)

    # Parked on the clarification, and the execution already ended.
    assert state.state == "WAITING_FOR_INPUT" and state.request_id == request.request_id
    assert [(e["engine_ref"], e["phase"]) for e in api.executions] == [(request_engine_ref(wid, rid), "Succeeded")]
    assert len(seen) == 1 and _scheduled_advances(history) == 2
    from temporalio.worker import Replayer

    await Replayer(workflows=[DevLoopWorkflow]).replay_workflow(history)
