"""DevLoop M2a acceptance (mctlhq/mctl-agents#1006): feedback state, the
read-only `contract_state` query and `contract-drain-check`, the closed-loop
primitives (not wired), and the T1-T4, T9b invariants through the real
validator boundary. M2a changes no live decision."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import inspect
import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter

from orchestrator.temporal import cli as tcli
from orchestrator.temporal import closed_loop as cl
from orchestrator.temporal import dispatcher as dx
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.temporal.workflows.dev_loop import (
    RESUME_DEFERRAL_REASONS,
    ContractState,
    DevLoopWorkflow,
    ImplementExecutionState,
    MergeWatchResume,
    OpenDelivery,
    ResumeDelivery,
)
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context.client import WorkItemClient
from tests.temporal_harness import Worker
from tests.test_deferred_resume_m1 import _ValidatorClient
from tests.test_dev_loop_workflow import MERGED_PR
from tests.test_execution_request_dispatch import (
    Crash,
    FakeTemporal,
    _dispatcher,
    _Gate,
    _loop_activities,
    _replace,
    _wait_for,
    api,  # noqa: F401  (fixture)
)
from tests.test_execution_request_resume import (
    _accepted_updates,
    _events,
    _park,
    _RunningPort,
    _submit_log,
    _validator,
    env,  # noqa: F401  (fixture)
)
from tests.test_execution_request_resume import api as resume_api  # noqa: F401  (fixture)
from tests.test_work_context_resume_acceptance import URL, WID

ROOT = Path(__file__).resolve().parent.parent
_CONVERTER = DataConverter.default.payload_converter


def _decode(type_: type, obj: object):
    payload = Payload(metadata={"encoding": b"json/plain"}, data=json.dumps(obj).encode())
    return _CONVERTER.from_payloads([payload], [type_])[0]


# -- T1: identity propagation, one field at a time -------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["intent_id", "expected_state_version", "resumed_from_execution_id"])
async def test_the_dispatcher_copies_each_identity_field_unchanged(api, field):  # noqa: F811
    loop = workflow_id_for(URL)
    rid = api.create_request("resume", resumed_from="we_distinct")
    api.state_version = 41
    api.xrs[rid]["expected_state_version"] = 37
    api.xrs[rid]["intent_id"] = 913
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=dx.DeliveryAnswer(dx.DELIVERY_DEFERRED, "legacy-loop"))
    await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()
    (_, delivery) = temporal.delivered[0]
    expected = {"intent_id": 913, "expected_state_version": 37, "resumed_from_execution_id": "we_distinct"}
    assert getattr(delivery, field) == expected[field]


# -- T2/T3/T4 at the real validator boundary ---------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state", "intent", "reason"),
    [
        ({"_last_pr": PRState(found=True, state="MERGED", merged=True)}, 7, "work_merged"),
        ({}, None, "no_actionable_intent"),
    ],
)
async def test_a_refused_resume_mints_nothing_keeps_identity_and_is_stable_on_redelivery(api, state, intent, reason):  # noqa: F811
    rid = api.create_request("resume")
    api.xrs[rid]["intent_id"] = intent
    before = {k: api.xrs[rid].get(k) for k in ("intent_id", "surface", "requested_by")}
    wf = _validator(_gates_passed=True, _deferred_resume_contract=True, **state)
    client = _ValidatorClient(wf)
    port = _RunningPort(client, workflow_id_for(URL))
    first = await dx.Dispatcher(WorkItemClient(), port, lease=60).dispatch_once()
    assert first.action == dx.REJECTED
    assert api.request_state(rid)["reason"] == f"{xr.RESUME_REFUSED}:{reason}"
    assert api.fulfils() == [] and api.executions == [] and client.accepted == []
    assert {k: api.xrs[rid].get(k) for k in before} == before
    # A redelivery of the same request (a second request, same facts) gives the same answer.
    rid2 = api.create_request("resume")
    api.xrs[rid2]["intent_id"] = intent
    await dx.Dispatcher(WorkItemClient(), port, lease=60).dispatch_once()
    assert api.request_state(rid2)["reason"] == f"{xr.RESUME_REFUSED}:{reason}"


@pytest.mark.anyio
async def test_a_legacy_loop_past_its_gates_defers_and_nothing_succeeds(api, capsys):  # noqa: F811
    rid = api.create_request("resume")
    api.xrs[rid]["intent_id"] = 7
    wf = _validator(_gates_passed=True, _deferred_resume_contract=False)
    client = _ValidatorClient(wf)
    port = _RunningPort(client, workflow_id_for(URL))
    outcome = await dx.Dispatcher(WorkItemClient(), port, lease=60).dispatch_once()
    assert outcome.action == dx.DEFERRED
    assert api.request_state(rid)["state"] == "claimed"
    assert api.fulfils() == [] and api.executions == [] and client.accepted == []
    assert "resume_deferred:legacy-loop" in capsys.readouterr().out


def test_a_contract_loop_with_an_intent_and_an_open_pr_defers_feedback_not_ready():
    from tests.test_execution_request_resume import _ivalidate

    wf = _validator(
        _gates_passed=True, _deferred_resume_contract=True, _last_pr=PRState(found=True, state="OPEN")
    )
    delivery = ResumeDelivery(
        execution_request_id="xr_1", work_item_id=WID, surface="telegram", actor_kind="human",
        actor_id="user:alice", kind="resume", intent_id=7,
    )
    assert _ivalidate(wf, delivery)[1] == "feedback-not-ready"


def test_the_deferred_resume_marker_is_evaluated_only_at_the_gates_passed_site():
    source = (ROOT / "orchestrator/temporal/workflows/dev_loop.py").read_text()
    assert source.count("workflow.patched(DEFERRED_RESUME_PATCH)") == 1


# -- T5: feedback state ------------------------------------------------------------


def test_a_pre_m2a_merge_watch_record_decodes_with_empty_feedback_state_and_its_contract():
    decoded = _decode(MergeWatchResume, {"service": "s", "slug": "x", "deferred_resume_contract": True})
    assert decoded.feedback_done == () and decoded.pending_feedback == ()
    assert decoded.deferred_resume_contract is True


def test_a_loop_starts_with_empty_feedback_state():
    wf = _validator()
    assert wf._feedback_done == set() and wf._pending_feedback == ()


def test_feedback_state_survives_carry_rehydrate_and_the_converter_in_order():
    src = _validator()
    src._feedback_done = {"xr_b", "xr_a"}
    src._pending_feedback = ("xr_z", "xr_c")
    carried = src._carry_work_context(MergeWatchResume())
    assert carried.feedback_done == ("xr_a", "xr_b") and carried.pending_feedback == ("xr_z", "xr_c")
    back = _CONVERTER.from_payloads([_CONVERTER.to_payloads([carried])[0]], [MergeWatchResume])[0]
    assert back == carried
    dst = _validator()
    dst._rehydrate_work_context(back)
    assert dst._feedback_done == {"xr_a", "xr_b"} and dst._pending_feedback == ("xr_z", "xr_c")


def test_an_unknown_key_in_a_merge_watch_payload_is_tolerated_and_dropped():
    """Forward compatibility, and what the rollback note in #1006's design
    depends on: a worker that does not know a field drops it instead of failing
    the hop, so a rollback does not need hops drained first. If the converter
    ever starts refusing unknown keys this fails, and that note has to change."""
    decoded = _decode(MergeWatchResume, {"service": "s", "not_a_field": 1})
    assert decoded.service == "s"
    assert not hasattr(decoded, "not_a_field")


# -- T6: contract_state ------------------------------------------------------------


def test_contract_state_reports_contract_legacy_and_pre_gates_loops():
    contract = _validator(
        _gates_passed=True, _deferred_resume_contract=True, _last_pr=PRState(found=True, state="OPEN")
    )
    assert contract.contract_state() == ContractState(
        contract=True, gates_passed=True, initialized=True, last_pr_state="OPEN"
    )
    legacy = _validator(_gates_passed=True, _deferred_resume_contract=False)
    assert (legacy.contract_state().contract, legacy.contract_state().gates_passed) == (False, True)
    pre = _validator(_implement_state=ImplementExecutionState(stage="implementer", outcome="failed"))
    state = pre.contract_state()
    assert (state.gates_passed, state.implement_stage, state.implement_outcome) == (False, "implementer", "failed")


def test_contract_state_never_answers_absent_for_an_unobserved_pr_and_lists_sorted_ids():
    wf = _validator(_last_pr=None)
    delivery = ResumeDelivery(execution_request_id="xr_2", work_item_id=WID)
    other = dataclasses.replace(delivery, execution_request_id="xr_1")
    wf._open_deliveries = {"xr_2": OpenDelivery(delivery=delivery), "xr_1": OpenDelivery(delivery=other)}
    wf._feedback_done = {"b", "a"}
    state = wf.contract_state()
    assert state.last_pr_state == "unknown"
    assert state.open_request_ids == ("xr_1", "xr_2") and state.feedback_done == ("a", "b")


# -- T7: contract-drain-check ------------------------------------------------------


class _DrainClient:
    def __init__(self, states: dict[str, object], *, listing_error: bool = False) -> None:
        self.states = states
        self.listing_error = listing_error
        self.calls: list[str] = []

    async def list_workflows(self, query: str):
        self.calls.append("list")
        if self.listing_error:
            raise RuntimeError("frontend down")
        for wid in self.states:
            yield SimpleNamespace(id=wid)

    def get_workflow_handle(self, wid: str):
        outer = self

        class _Handle:
            async def query(self, *_a, **_k):
                outer.calls.append(f"query:{wid}")
                value = outer.states[wid]
                if isinstance(value, Exception):
                    raise value
                return value

            def __getattr__(self, name):  # any signal/update/start would land here
                raise AssertionError(f"contract-drain-check must not call {name}")

        return _Handle()

    def __getattr__(self, name):
        raise AssertionError(f"contract-drain-check must not call {name}")


@pytest.mark.anyio
async def test_contract_drain_check_classifies_each_loop_and_exits_zero():
    client = _DrainClient({
        "a": ContractState(contract=True, gates_passed=True),
        "b": ContractState(gates_passed=True),
        "c": ContractState(),
    })
    lines, code = await tcli.contract_drain_check(client)
    assert code == 0
    assert lines[:3] == ["a: contract", "b: legacy", "c: pre-gates"]
    assert lines[-1] == "contract=1, legacy=1, pre-gates=1, UNKNOWN=0"


@pytest.mark.anyio
async def test_a_query_error_is_unknown_and_exits_two():
    lines, code = await tcli.contract_drain_check(_DrainClient({"a": RuntimeError("no handler")}))
    assert code == 2 and lines[0].startswith("a: UNKNOWN") and lines[-1].endswith("UNKNOWN=1")


@pytest.mark.anyio
async def test_a_failed_listing_is_unknown_never_zero_loops():
    lines, code = await tcli.contract_drain_check(_DrainClient({}, listing_error=True))
    assert code == 2 and any("UNKNOWN" in line for line in lines)


def test_the_cli_registers_contract_drain_check():
    assert tcli.build_parser().parse_args(["contract-drain-check"]).command == "contract-drain-check"


# -- T8: tri-state reads -----------------------------------------------------------


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(cl, "_resolve_token", lambda: "tok")


def _transport(routes: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        result = routes.get(request.url.path)
        if isinstance(result, Exception):
            raise result
        if result is None:
            return httpx.Response(404)
        status, body = result  # type: ignore[misc]
        return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body))

    return httpx.MockTransport(handler)


_PULLS = "/repos/mctlhq/svc/pulls"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        ((200, []), "absent"),
        ((200, [{"state": "closed", "merged_at": "2026-01-01T00:00:00Z"}]), "merged"),
        ((200, [{"state": "open", "merged_at": None}]), "open"),
        ((200, [{"state": "closed", "merged_at": None}]), "closed"),
        ((403, {}), "unknown"),
        ((404, {}), "unknown"),
        ((500, {}), "unknown"),
        ((200, b"not json"), "unknown"),
        ((200, {"not": "a list"}), "unknown"),
        (httpx.ReadTimeout("slow"), "unknown"),
    ],
)
async def test_read_pr_by_branch_is_tri_state(token, answer, verdict):
    read = await cl.read_pr_by_branch(
        "mctlhq/svc", "feat/agents-x", transport=_transport({_PULLS: answer})
    )
    assert read.verdict == verdict


@pytest.mark.anyio
async def test_read_pr_by_branch_without_a_token_is_unknown(monkeypatch):
    monkeypatch.setattr(cl, "_resolve_token", lambda: "")
    assert (await cl.read_pr_by_branch("mctlhq/svc", "b")).verdict == "unknown"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("routes", "verdict"),
    [
        ({"/repos/mctlhq/svc": (200, {}), "/repos/mctlhq/svc/branches/b": (200, {})}, "exists"),
        ({"/repos/mctlhq/svc": (200, {})}, "absent"),
        ({"/repos/mctlhq/svc": (404, {})}, "unknown"),
        ({"/repos/mctlhq/svc": (403, {})}, "unknown"),
        ({"/repos/mctlhq/svc": (200, {}), "/repos/mctlhq/svc/branches/b": (500, {})}, "unknown"),
        ({"/repos/mctlhq/svc": httpx.ConnectError("down")}, "unknown"),
    ],
)
async def test_read_branch_answers_absent_only_after_the_repository_read(token, routes, verdict):
    assert (await cl.read_branch("mctlhq/svc", "b", transport=_transport(routes))).verdict == verdict


@pytest.mark.anyio
async def test_read_branch_encodes_the_slash_of_a_real_implementer_branch(token):
    """The implementer's branch is `feat/agents-<slug>`. Unencoded, the slash
    routes elsewhere, GitHub answers 404, and an existing branch reads `absent`."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    read = await cl.read_branch("mctlhq/svc", "feat/agents-x", transport=httpx.MockTransport(handler))

    assert read.verdict == "exists"
    assert seen == ["/repos/mctlhq/svc", "/repos/mctlhq/svc/branches/feat%2Fagents-x"]


def _status_file(status: str) -> tuple[int, dict]:
    return 200, {"content": base64.b64encode(f"status: {status}\n".encode()).decode()}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        (_status_file("accepted"), "passed"),
        (_status_file("implementing"), "passed"),
        (_status_file("review-fixing"), "passed"),
        (_status_file("merged"), "passed"),
        (_status_file("proposed"), "not-passed"),
        (_status_file("needs-triage"), "unknown"),
        ((404, {}), "unknown"),
        ((500, {}), "unknown"),
        (httpx.ReadTimeout("slow"), "unknown"),
    ],
)
async def test_read_gates_is_tri_state(token, answer, verdict):
    path = f"/repos/{cl.GITOPS_REPO}/contents/{cl.AGENTS_STATE_PREFIX}/svc/proposals/slug/.status.yaml"
    assert (await cl.read_gates("svc", "slug", transport=_transport({path: answer}))).verdict == verdict


# -- T9: the pure decision table ---------------------------------------------------


@pytest.mark.parametrize(
    ("loop", "pr", "intent", "gates"),
    list(itertools.product(
        ["running", "closed", "absent"],
        ["merged", "open", "closed", "absent", "unknown"],
        [7, None],
        ["passed", "not-passed", "unknown"],
    )),
)
def test_closed_loop_decision_table(loop, pr, intent, gates):
    got = cl.closed_loop_decision(loop, intent, cl.PRRead(pr), cl.GatesRead(gates))
    if loop == "running":
        want = "pass"
    elif pr == "merged":
        want = "refuse:work_merged"
    elif pr == "unknown":
        want = "defer:closed-loop-unobserved"
    elif intent is None and gates == "passed":
        want = "refuse:no_actionable_intent"
    elif intent is None and gates == "unknown":
        want = "defer:closed-loop-unobserved"
    else:
        want = "pass"
    assert got == want


def test_the_decision_reasons_belong_to_the_vocabularies():
    assert {cl.REFUSE_WORK_MERGED, cl.REFUSE_NO_ACTIONABLE_INTENT} == {
        f"refuse:{r}" for r in (xr.WORK_MERGED, xr.NO_ACTIONABLE_INTENT)
    }
    assert cl.DEFER_UNOBSERVED.removeprefix("defer:") in dx.DEFERRAL_AUDITED
    assert dx.CLOSED_LOOP_UNOBSERVED not in RESUME_DEFERRAL_REASONS


# -- T10: vocabulary and non-wiring ------------------------------------------------


@pytest.mark.parametrize("name", ["dev_loop.py", "dispatcher.py", "closed_loop.py"])
def test_no_module_mints_a_legacy_refusal_spelling(name):
    path = next((ROOT / "orchestrator" / "temporal").rglob(name))
    for legacy in xr.LEGACY_RESUME_REFUSAL_REASONS:
        assert not re.search(rf"[\"']{re.escape(legacy)}[\"']", path.read_text())


def test_the_dispatcher_is_not_wired_to_the_closed_loop_reads():
    source = (ROOT / "orchestrator/temporal/dispatcher.py").read_text()
    for name in ("temporal.closed_loop", "closed_loop_decision", "ClosedLoopReadPort", "GitHubClosedLoopReads"):
        assert name not in source
    assert list(inspect.signature(dx.Dispatcher.__init__).parameters) == ["self", "api", "temporal", "lease"]


# -- T11 and R1-R3: owned by later slices -------------------------------------------


@pytest.mark.xfail(strict=True, reason="mctl-api list contract unconfirmed (task 4b dependency)")
def test_list_execution_requests_reads_a_complete_page():
    assert hasattr(WorkItemClient, "list_execution_requests")


@pytest.mark.xfail(strict=True, reason="M2c: wire closed_loop_decision into Dispatcher._dispatch (#993 task 4b)")
def test_r1_the_dispatcher_refuses_a_merged_closed_loop_before_deliver():
    assert "closed_loop_decision" in (ROOT / "orchestrator/temporal/dispatcher.py").read_text()


@pytest.mark.xfail(strict=True, reason="slice owning #993 tasks 5-8, then M2c: the accept path")
def test_r2_a_contract_loop_with_an_open_pr_and_an_intent_is_accepted():
    from tests.test_execution_request_resume import _ivalidate  # noqa: F401

    wf = _validator(_gates_passed=True, _deferred_resume_contract=True, _last_pr=PRState(found=True, state="OPEN"))
    wf._validate_execution_request(
        ResumeDelivery(
            execution_request_id="xr_1", work_item_id=WID, surface="telegram", actor_kind="human",
            actor_id="user:alice", kind="resume", intent_id=7,
        )
    )


@pytest.mark.xfail(strict=True, reason="#1005 (M3): recovery of a failed implement or closed PR")
def test_r3_a_failed_implement_with_an_intent_is_not_deferred_recovery_not_ready():
    wf = _validator(
        _gates_passed=True,
        _deferred_resume_contract=True,
        _implement_state=ImplementExecutionState(stage="implementer", outcome="failed"),
    )
    wf._validate_execution_request(
        ResumeDelivery(
            execution_request_id="xr_1", work_item_id=WID, surface="telegram", actor_kind="human",
            actor_id="user:alice", kind="resume", intent_id=7,
        )
    )


# -- T2 again, on the real workflow ------------------------------------------------


class _CrashBeforeReject:
    """A `WorkItemClient` whose process dies after the loop refused, before the
    refusal reached mctl-api: the request stays claimed and its lease lapses."""

    def __init__(self, inner: WorkItemClient) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def reject_execution_request(self, *_args: object, **_kwargs: object) -> object:
        raise Crash("dispatcher died between the refusal and the reject")


@pytest.mark.anyio
async def test_the_real_loop_refuses_work_merged_and_the_same_request_gets_the_same_answer(
    resume_api,  # noqa: F811
    env,  # noqa: F811
):
    """Real `DevLoopWorkflow`, gates passed, `_last_pr` MERGED. The loop is held
    right after the merge so the refusal comes from that state and not from a
    loop that has ended. The second dispatch is the SAME request after its lease
    lapses, which is the redelivery a crashed dispatcher really produces."""
    api_ = resume_api
    submit, _ = _submit_log()
    gate = _Gate()
    merged_seen = asyncio.Event()
    leave = asyncio.Event()

    @activity.defn(name="resolve_deploy_target")
    async def held_deploy_target(repo: str) -> None:
        merged_seen.set()
        await leave.wait()

    activities = _replace(
        _loop_activities(submit, pr_states=[MERGED_PR], approval_requests=gate.requests),
        gate.activity,
        held_deploy_target,
    )
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities):
        loop = await _park(api_, env, gate)
        await gate.release()
        await _wait_for(merged_seen.is_set)

        rid = api_.create_request("resume")
        api_.xrs[rid]["intent_id"] = 7
        before = {k: api_.xrs[rid].get(k) for k in ("intent_id", "surface", "requested_by")}
        executions = len(api_.executions)
        fulfils = len(api_.fulfils())

        with pytest.raises(Crash):
            await _dispatcher(env, _CrashBeforeReject(WorkItemClient())).dispatch_once()
        assert api_.request_state(rid)["state"] == "claimed"
        api_.now += 61  # the crashed holder's lease lapses

        outcome = await _dispatcher(env).dispatch_once()

        assert outcome.action == dx.REJECTED
        assert api_.request_state(rid)["reason"] == f"{xr.RESUME_REFUSED}:work_merged"
        assert len(api_.fulfils()) == fulfils and len(api_.executions) == executions
        assert {k: api_.xrs[rid].get(k) for k in before} == before
        assert rid not in _accepted_updates(await _events(env, loop))

        leave.set()
        await env.client.get_workflow_handle(loop).terminate("test over")
