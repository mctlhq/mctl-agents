"""The durable approval wait (mctlhq/mctl-agents#198, ADR-014 §7).

Runs the real `ActionApprovalWaitWorkflow`, the real `run_gated_action`
helper (through tests/approval_probe_workflow.py), the real
`read_action_approval` poll and the real `MctlApiApprovals` store client,
against the independent fake mctl-api of tests/test_action_approvals.py and
temporalio's time-skipping test environment.

The gated activity below is the contract a real one follows: it recomputes
its arguments from the world (`WORLD`) on every call and performs its side
effect only through `run_gated`. The side effect is `mcp__mctl__mctl_deploy_
service`, which the built-in policy gates behind REQUIRE_APPROVAL.

Invariant checked after every scenario: the side effect ran exactly as many
times as the store confirmed a consume (`effects == api.spent`), so no side
effect ever runs without a consumed receipt.

Regenerating the replay fixture (only when the wait's command shape is MEANT
to change, never to turn a red run green):
`uv run python -m tests.test_action_approval_wait`.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowHandle, WorkflowHistory
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from orchestrator import action_approvals as aa
from orchestrator import policy_checkpoint as pc
from orchestrator.temporal.activities import action_approval as act
from orchestrator.temporal.activities.action_approval import (
    GatedActionInput,
    GatedActionResult,
    read_action_approval,
)
from orchestrator.temporal.workflows import action_approval as wf
from orchestrator.temporal.workflows.action_approval import (
    ActionApprovalWaitWorkflow,
    ApprovalWaitInput,
    ApprovalWaitResult,
    ApprovalWaitState,
)
from tests.approval_probe_workflow import ApprovalProbeWorkflow, ProbeInput
from tests.temporal_harness import Worker
from tests.test_action_approvals import FakeMctlApi

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-action-approval-wait"
DEPLOY = "mcp__mctl__mctl_deploy_service"
GRANTS = ("mcp__mctl__*",)
GATED = "gated_deploy"
HISTORY = Path(__file__).resolve().parent / "fixtures" / "histories" / "action_approval_wait.json"

#: The world the gated activity reads its arguments from, and what it did.
WORLD: dict[str, Any] = {}


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


def _track_workflow_time() -> None:
    """With `WORLD["track"]`, the fake store's clock (and the lookup's, see
    the `tracked` fixture) follows Temporal's skipped time, so a receipt
    really expires while the workflow waits."""
    if WORLD.get("track"):
        WORLD["api"].clock.now = activity.info().current_attempt_scheduled_time


@activity.defn(name=GATED)
async def gated_deploy(inp: GatedActionInput) -> GatedActionResult:
    WORLD["calls"].append(inp)
    _track_workflow_time()
    args = {"service": inp.payload["service"], "head": WORLD["head"]}
    if WORLD.get("crash_after_consume") and inp.approval_ref:
        # The consume lands, then the worker dies before the side effect:
        # the activity never returns anything.
        WORLD["crash_after_consume"] = False
        request = act.gated_request(inp, pc.MCP_TOOL_CALL, DEPLOY, inp.payload["service"], args, grants=GRANTS)
        assert isinstance(request, pc.ActionRequest)
        decision = pc.decide(request, approvals=act.approvals_for_attempt(inp.attempt), approval_ref=inp.approval_ref)
        assert decision.code == pc.CODE_APPROVED
        raise RuntimeError("worker died between the consume and the side effect")

    def _deploy() -> dict[str, Any]:
        if WORLD.get("effect_raises"):
            raise RuntimeError("GitHub answered 502")
        WORLD["effects"] += 1
        return {"deployed": WORLD["head"]}

    return act.run_gated(inp, pc.MCP_TOOL_CALL, DEPLOY, inp.payload["service"], args, _deploy, grants=GRANTS)


@activity.defn(name="read_action_approval")
async def tracked_read(approval_id: str) -> act.ApprovalPoll:
    """The real poll, on workflow time, with a hook after each read."""
    _track_workflow_time()
    poll = act._read(approval_id)
    WORLD["reads"] += 1
    hook = WORLD.get("after_read")
    if hook is not None:
        hook(WORLD["reads"])
    return poll


@pytest.fixture
def env_vars(monkeypatch):
    monkeypatch.setenv(pc.APPROVALS_ENV, pc.APPROVALS_MCTL_API)
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    monkeypatch.delenv(aa.TTL_ENV, raising=False)


@pytest.fixture
def api(monkeypatch, env_vars) -> FakeMctlApi:
    fake = FakeMctlApi(_Clock())  # type: ignore[arg-type]
    monkeypatch.setattr(aa, "_no_redirect_opener", lambda: fake)
    WORLD.clear()
    WORLD.update({"head": "sha-1", "effects": 0, "calls": [], "reads": 0, "api": fake})
    return fake


@pytest.fixture
def tracked(api, monkeypatch) -> FakeMctlApi:
    """Store and lookup on the workflow's clock, with a short receipt TTL
    (600s, so the wait ends CONSUME_MARGIN_SECONDS = 300s after it starts)."""
    monkeypatch.setenv(aa.TTL_ENV, "600")
    monkeypatch.setattr(act, "approvals_for_attempt",
                        lambda attempt: aa.MctlApiApprovals(aa.ActionApprovalClient(), now=api.clock, attempt=attempt))
    WORLD["track"] = True
    return api


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _action(attempt: int = 0) -> GatedActionInput:
    return GatedActionInput(
        payload={"service": "mctl-web"}, execution_id="ctx-198", actor="system:dev-loop", trace_id="tr-198",
        attempt=attempt,
    )


def _worker(env: WorkflowEnvironment, activities: list | None = None, **kw: Any) -> Worker:
    return Worker(
        env.client, task_queue=TASK_QUEUE,
        workflows=[ActionApprovalWaitWorkflow, ApprovalProbeWorkflow],
        activities=activities if activities is not None else [gated_deploy, read_action_approval],
        **kw,
    )


def _pending_receipt(api: FakeMctlApi) -> str:
    """Run the gated activity's first decision outside Temporal, as a caller
    would: it opens the request and answers `approval_pending`."""
    args = {"service": "mctl-web", "head": WORLD["head"]}
    first = act.run_gated(_action(), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", args,
                          lambda: pytest.fail("the side effect ran before any approval"), grants=GRANTS)
    assert first.awaiting_approval, first
    return first.approval_ref


async def _start_wait(env: WorkflowEnvironment, approval_id: str, **kw: Any) -> WorkflowHandle:
    inp = ApprovalWaitInput(approval_id=approval_id, activity=GATED, action=_action(), poll_seconds=60, **kw)
    return await env.client.start_workflow(
        ActionApprovalWaitWorkflow.run, inp, id=wf.approval_workflow_id(approval_id), task_queue=TASK_QUEUE,
    )


async def _state(handle: WorkflowHandle) -> ApprovalWaitState:
    return await handle.query(wf.STATE_QUERY, result_type=ApprovalWaitState)


async def _until(check: Callable[[], Awaitable[bool]], what: str) -> None:
    for _ in range(400):
        if await check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _waiting(handle: WorkflowHandle) -> None:
    async def ready() -> bool:
        s = await _state(handle)
        return bool(s.deadline) and s.state == wf.WAITING_FOR_APPROVAL
    await _until(ready, "the wait to start")


async def _signal(handle: WorkflowHandle, approval_id: str) -> None:
    await handle.signal(wf.DECIDED_SIGNAL, {"approval_id": approval_id})


async def _result(handle: WorkflowHandle) -> ApprovalWaitResult:
    return await handle.result()


def _single_use(api: FakeMctlApi) -> None:
    assert WORLD["effects"] == api.spent, (WORLD["effects"], api.spent)


# ---------------------------------------------------------------------------
# The decision outcomes
# ---------------------------------------------------------------------------


async def test_approve_by_signal_consumes_then_runs_once(env, api):
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        assert WORLD["effects"] == 0 and api.consume_calls() == 0
        api.decide(rid, "approve")
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_RAN and result.ran
    assert result.code == pc.CODE_APPROVED
    assert result.result == {"deployed": "sha-1"}
    assert WORLD["effects"] == 1 and api.spent == 1
    assert api.records[rid]["state"] == "consumed"
    # The wake redeemed exactly the receipt it waited on.
    assert [c.approval_ref for c in WORLD["calls"]] == [rid]
    assert (result.signal_wakes, result.poll_wakes, result.rechecks) == (1, 0, 1)
    _single_use(api)


async def test_reject_ends_denied_without_the_side_effect(env, api):
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "deny")
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_DENIED
    assert result.code == pc.CODE_APPROVAL_DENIED
    assert WORLD["effects"] == 0 and api.consume_calls() == 0
    _single_use(api)


async def test_an_expired_receipt_ends_expired(env, api):
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "approve")
        api.clock.now += timedelta(days=2)  # past the 24h expiry, unconsumed
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_EXPIRED
    assert WORLD["effects"] == 0 and api.consume_calls() == 0
    _single_use(api)


async def test_nobody_answers_times_out_at_the_receipts_expiry(env, api):
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        state = await _state(handle)
        # The durable timer is bounded by the receipt's own expiry (less the
        # consume margin), not by the 7-day ceiling.
        expires = datetime.fromisoformat(api.records[rid]["expires_at"])
        assert datetime.fromisoformat(state.deadline) == expires - timedelta(seconds=wf.CONSUME_MARGIN_SECONDS)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_TIMED_OUT
    assert result.poll_wakes > 0 and result.rechecks == 0
    assert WORLD["effects"] == 0 and api.consume_calls() == 0
    _single_use(api)


async def test_the_max_wait_ceiling_bounds_a_long_receipt(env, api):
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid, max_wait_seconds=300)
        await _waiting(handle)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_TIMED_OUT
    assert result.poll_wakes == 5  # 60s ticks to a 300s ceiling
    _single_use(api)


# ---------------------------------------------------------------------------
# Wakes: signal, poll, duplicates, foreign ids
# ---------------------------------------------------------------------------


async def test_a_lost_signal_is_recovered_by_the_poll(env, api):
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "approve")  # decided, but no signal ever arrives
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_RAN
    assert result.signal_wakes == 0 and result.poll_wakes == 1 and result.rechecks == 1
    assert WORLD["effects"] == 1
    _single_use(api)


async def test_a_pending_poll_never_runs_the_activity(env, api):
    """The poll only decides whether a re-check is worth it: while the
    store says pending, the gated activity is not called at all."""
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid, max_wait_seconds=180)
        await _waiting(handle)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_TIMED_OUT
    assert WORLD["calls"] == []
    _single_use(api)


async def test_duplicate_signals_run_the_side_effect_once(env, api):
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "approve")
        for _ in range(3):
            await _signal(handle, rid)
        result = await _result(handle)
        # A signal after the step ended reaches nothing.
        with pytest.raises(RPCError):
            await _signal(handle, rid)
    assert result.outcome == wf.OUTCOME_RAN
    assert WORLD["effects"] == 1 and api.spent == 1 and api.consume_calls() == 1
    _single_use(api)


async def test_a_spurious_signal_rechecks_and_keeps_waiting(env, api):
    """A signal is a wake-up, never an approval: sent before anyone
    decided, it costs one re-check that answers pending."""
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        await _signal(handle, rid)
        await _until(lambda: _rechecked(handle, 1), "the spurious re-check")
        assert WORLD["effects"] == 0
        api.decide(rid, "approve")
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_RAN and result.rechecks == 2
    _single_use(api)


async def _rechecked(handle: WorkflowHandle, n: int) -> bool:
    s = await _state(handle)
    return s.rechecks >= n and s.state == wf.WAITING_FOR_APPROVAL


async def test_a_signal_naming_another_receipt_is_ignored(env, api, caplog):
    caplog.set_level("INFO", logger="temporalio.workflow")
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid, max_wait_seconds=120)
        await _waiting(handle)
        await _signal(handle, "aar_" + "f" * 32)
        await handle.signal(wf.DECIDED_SIGNAL, "aar_" + "e" * 32)
        await handle.signal(wf.DECIDED_SIGNAL)  # no payload names no receipt
        await _until(lambda: _ignored(handle, 3), "the three foreign signals")
        state = await _state(handle)
        assert state.rechecks == 0 and state.signal_wakes == 0
        ignored = [r for r in caplog.records if r.getMessage().startswith("action_approval.signal_ignored")]
        assert [getattr(r, "reason", "")[:28] for r in ignored] == [
            "the signal names another rec", "the signal names another rec", "the signal names no receipt ",
        ]
        # Decided only now, so no tick could have raced the checks above;
        # the poll still finds it.
        api.decide(rid, "approve")
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_RAN and result.signal_wakes == 0 and result.poll_wakes >= 1
    _single_use(api)


async def _ignored(handle: WorkflowHandle, n: int) -> bool:
    return (await _state(handle)).ignored_signals >= n


async def test_an_unreachable_store_on_recheck_is_retried_not_final(env, api):
    rid = _pending_receipt(api)
    failures = {"left": 2}

    def flaky_get(req: Any) -> Any:
        failures["left"] -= 1
        if failures["left"] == 0:
            del api.override["get"]
        return api._err(503, "unavailable")

    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "approve")
        api.override["get"] = flaky_get
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_RAN
    assert result.rechecks == 3  # two lookup errors, then the redeem
    assert result.poll_wakes == 0  # retried on backoff, not left to the poll
    _single_use(api)


# ---------------------------------------------------------------------------
# Crash, replay, single use
# ---------------------------------------------------------------------------


async def test_a_worker_crash_mid_wait_resumes_from_history(env, api):
    """No workflow cache (and so no sticky queue): every workflow task is
    rebuilt from history, as it is on a worker that replaced a dead one."""
    rid = _pending_receipt(api)
    async with _worker(env, max_cached_workflows=0):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
    # The worker is gone: nothing runs, nothing is held, the wait is only
    # history. Replay it as a fresh worker would.
    mid = await handle.fetch_history()
    await Replayer(workflows=[ActionApprovalWaitWorkflow]).replay_workflow(mid)
    api.decide(rid, "approve")
    async with _worker(env, max_cached_workflows=0):
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_RAN
    assert WORLD["effects"] == 1
    _single_use(api)


async def test_a_crash_after_the_consume_never_runs_the_effect(env, api):
    """The consume succeeds and the worker dies before the side effect: the
    retried activity finds the receipt spent and the wait ends `consumed`.
    The approval is burned; the action never runs twice (or at all), and
    `consumed` is not re-requestable."""
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "approve")
        WORLD["crash_after_consume"] = True
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_CONSUMED
    assert api.spent == 1 and WORLD["effects"] == 0
    assert len(WORLD["calls"]) == 2  # the crashed attempt and its retry
    with pytest.raises(ValueError):
        wf.next_attempt(result, _action())


async def test_a_side_effect_that_raises_is_effect_failed_and_rerequestable(env, api):
    """The consume lands and the side effect raises: run_gated reports it
    instead of letting a Temporal retry answer `approval_consumed`. The
    receipt is spent and never retried; a re-request is allowed, and needs
    a new human decision."""
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "approve")
        WORLD["effect_raises"] = True
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_EFFECT_FAILED
    assert "GitHub answered 502" in result.reason
    assert api.spent == 1 and WORLD["effects"] == 0
    assert len(WORLD["calls"]) == 1  # not retried on the spent receipt
    assert wf.next_attempt(result, _action()).attempt == 1


async def test_a_changed_action_is_refused_and_nothing_is_consumed(env, api):
    """The receipt binds the intent hash. The world moved while the human
    decided (a new head), so the action the activity would perform now is
    not the one approved."""
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        api.decide(rid, "approve")
        WORLD["head"] = "sha-2"
        await _signal(handle, rid)
        result = await _result(handle)
    assert result.outcome == wf.OUTCOME_MISMATCH
    assert result.code == pc.CODE_APPROVAL_INTENT_MISMATCH
    assert WORLD["effects"] == 0 and api.consume_calls() == 0
    assert api.records[rid]["state"] == "approved"  # still unspent
    _single_use(api)


# ---------------------------------------------------------------------------
# The deadline: a margin before expiry, and the final read
# ---------------------------------------------------------------------------


def _at_read(n: int, act_on: Callable[[], None]) -> None:
    """Run `act_on` right after the n-th poll read has answered."""
    def hook(reads: int) -> None:
        if reads == n:
            act_on()
    WORLD["after_read"] = hook


async def _wait_to_deadline(env: WorkflowEnvironment, rid: str) -> ApprovalWaitResult:
    """One wait with no regular tick before its deadline (poll 3600s, a
    300s window): read 1 is the first poll, read 2 the timer firing at the
    deadline (still a regular tick), read 3 the final read."""
    async with _worker(env, activities=[gated_deploy, tracked_read]):
        inp = ApprovalWaitInput(approval_id=rid, activity=GATED, action=_action(), poll_seconds=3600)
        handle = await env.client.start_workflow(
            ActionApprovalWaitWorkflow.run, inp, id=wf.approval_workflow_id(rid), task_queue=TASK_QUEUE,
        )
        return await handle.result()


async def test_an_approval_after_the_last_tick_still_runs_before_expiry(env, tracked):
    """The human approves after the last regular tick and the signal is
    lost: the final read finds it, and because the wait ends
    CONSUME_MARGIN_SECONDS before the receipt expires, the re-check can
    still redeem it."""
    rid = _pending_receipt(tracked)
    _at_read(2, lambda: tracked.decide(rid, "approve"))
    result = await _wait_to_deadline(env, rid)
    assert result.outcome == wf.OUTCOME_RAN, result
    assert (result.signal_wakes, result.poll_wakes, result.rechecks) == (0, 1, 1)
    assert WORLD["reads"] == 3
    expires = datetime.fromisoformat(tracked.records[rid]["expires_at"])
    # The redeem ran on workflow time at the deadline, with the margin left.
    assert expires - tracked.clock.now >= timedelta(seconds=wf.CONSUME_MARGIN_SECONDS - 5)
    assert WORLD["effects"] == 1
    _single_use(tracked)


async def test_a_denial_found_only_at_the_final_read_is_denied(env, tracked):
    rid = _pending_receipt(tracked)
    _at_read(2, lambda: tracked.decide(rid, "deny"))
    result = await _wait_to_deadline(env, rid)
    assert result.outcome == wf.OUTCOME_DENIED and result.code == pc.CODE_APPROVAL_DENIED
    assert WORLD["effects"] == 0
    _single_use(tracked)


async def test_a_receipt_consumed_elsewhere_at_the_final_read_is_consumed_not_rerequestable(env, tracked):
    rid = _pending_receipt(tracked)

    def spent_elsewhere() -> None:
        tracked.decide(rid, "approve")
        tracked.records[rid]["state"] = "consumed"

    _at_read(2, spent_elsewhere)
    result = await _wait_to_deadline(env, rid)
    assert result.outcome == wf.OUTCOME_CONSUMED and result.code == pc.CODE_APPROVAL_CONSUMED
    assert WORLD["effects"] == 0
    with pytest.raises(ValueError):
        wf.next_attempt(result, _action())


async def test_a_window_shorter_than_the_margin_still_gets_its_final_read(env, tracked, monkeypatch):
    """A 60s receipt is already inside the margin: the wait ends at once,
    but still reads and redeems while the receipt is live."""
    monkeypatch.setenv(aa.TTL_ENV, "60")
    rid = _pending_receipt(tracked)
    _at_read(1, lambda: tracked.decide(rid, "approve"))  # after the first poll said pending
    result = await _wait_to_deadline(env, rid)
    assert result.outcome == wf.OUTCOME_RAN, result
    assert result.poll_wakes == 0 and WORLD["reads"] == 2
    _single_use(tracked)


# ---------------------------------------------------------------------------
# The caller helper: first call, child wait, deliberate re-request
# ---------------------------------------------------------------------------


async def _probe(env: WorkflowEnvironment, max_attempts: int = 1) -> WorkflowHandle:
    return await env.client.start_workflow(
        ApprovalProbeWorkflow.run, ProbeInput(activity=GATED, action=_action(), max_attempts=max_attempts),
        id=f"approval-probe-{uuid.uuid4()}", task_queue=TASK_QUEUE,
    )


async def _receipt(api: FakeMctlApi, n: int) -> str:
    async def made() -> bool:
        return len(api.records) >= n
    await _until(made, f"receipt #{n}")
    return sorted(api.records)[n - 1]


async def test_the_helper_waits_in_a_child_keyed_by_the_receipt(env, api):
    async with _worker(env):
        probe = await _probe(env)
        rid = await _receipt(api, 1)
        child = env.client.get_workflow_handle(wf.approval_workflow_id(rid))
        await _until(lambda: _started(child), "the child wait")
        await _waiting(child)
        api.decide(rid, "approve")
        await _signal(child, rid)
        out = await probe.result()
    [result] = out.results
    assert result.outcome == wf.OUTCOME_RAN and result.approval_id == rid
    assert [c.approval_ref for c in WORLD["calls"]] == ["", rid]
    _single_use(api)


async def _started(handle: WorkflowHandle) -> bool:
    try:
        await handle.describe()
    except RPCError:
        return False
    return True


async def test_a_rerequest_after_denial_opens_a_new_request(env, api):
    """Nothing decided for attempt 0 carries over: attempt 1 is a new
    request, with a new receipt, that waits for a new human decision."""
    async with _worker(env):
        probe = await _probe(env, max_attempts=2)
        first = await _receipt(api, 1)
        c1 = env.client.get_workflow_handle(wf.approval_workflow_id(first))
        await _until(lambda: _started(c1), "the first wait")
        await _waiting(c1)
        api.decide(first, "deny")
        await _signal(c1, first)
        second = await _receipt(api, 2)
        assert second != first
        assert api.records[second]["state"] == "pending"
        assert api.records[second]["intent_hash"] == api.records[first]["intent_hash"]
        c2 = env.client.get_workflow_handle(wf.approval_workflow_id(second))
        await _until(lambda: _started(c2), "the second wait")
        await _waiting(c2)
        assert WORLD["effects"] == 0
        api.decide(second, "approve")
        await _signal(c2, second)
        out = await probe.result()
    assert [r.outcome for r in out.results] == [wf.OUTCOME_DENIED, wf.OUTCOME_RAN]
    assert [r.attempt for r in out.results] == [0, 1]
    assert api.records[first]["state"] == "denied"
    assert api.records[second]["state"] == "consumed"
    assert WORLD["effects"] == 1
    # The re-request's first call named no receipt: it did not reuse attempt 0's.
    assert [(c.attempt, c.approval_ref) for c in WORLD["calls"]] == [
        (0, ""), (0, first), (1, ""), (1, second),
    ]
    _single_use(api)


async def test_a_second_waiter_on_the_same_receipt_is_already_waiting(env, api):
    """Two callers asking for the same intent (same execution, same attempt)
    share one receipt. The one already waiting owns it; the other gets
    `already_waiting`, does not act and cannot re-request."""
    rid = _pending_receipt(api)
    async with _worker(env):
        owner = await _start_wait(env, rid)
        await _waiting(owner)
        probe = await _probe(env, max_attempts=2)
        out = await probe.result()
        [result] = out.results
        assert result.outcome == wf.OUTCOME_ALREADY_WAITING and result.approval_id == rid
        assert len(api.records) == 1  # the same receipt, found by key
        with pytest.raises(ValueError):
            wf.next_attempt(result, _action())
        api.decide(rid, "approve")
        await _signal(owner, rid)
        owned = await _result(owner)
    assert owned.outcome == wf.OUTCOME_RAN
    assert WORLD["effects"] == 1
    _single_use(api)


async def test_approvals_off_by_default_never_wait(env, api, monkeypatch):
    monkeypatch.delenv(pc.APPROVALS_ENV)
    async with _worker(env):
        probe = await _probe(env, max_attempts=3)
        out = await probe.result()
    [result] = out.results
    assert result.outcome == wf.OUTCOME_BLOCKED and result.code == pc.CODE_APPROVAL_REQUIRED
    assert api.calls == [] and WORLD["effects"] == 0


def test_next_attempt_only_after_a_denial_expiry_or_timeout():
    action = GatedActionInput(payload={"service": "x"}, execution_id="e", attempt=2, approval_ref="aar_old")
    for outcome in wf.REREQUESTABLE:
        nxt = wf.next_attempt(ApprovalWaitResult(outcome=outcome, attempt=2), action)
        assert (nxt.attempt, nxt.approval_ref) == (3, "")
    for outcome in (wf.OUTCOME_RAN, wf.OUTCOME_CONSUMED, wf.OUTCOME_MISMATCH, wf.OUTCOME_BLOCKED,
                    wf.OUTCOME_REFUSED, wf.OUTCOME_UNDECIDED, wf.OUTCOME_ALREADY_WAITING):
        with pytest.raises(ValueError):
            wf.next_attempt(ApprovalWaitResult(outcome=outcome, attempt=2), action)


@pytest.mark.parametrize(("code", "outcome"), [
    (pc.CODE_APPROVAL_PENDING, None),
    (pc.CODE_APPROVAL_LOOKUP_ERROR, None),
    (pc.CODE_EVALUATOR_ERROR, None),
    (pc.CODE_APPROVAL_DENIED, wf.OUTCOME_DENIED),
    (pc.CODE_APPROVAL_EXPIRED, wf.OUTCOME_EXPIRED),
    (pc.CODE_APPROVAL_CONSUMED, wf.OUTCOME_CONSUMED),
    (pc.CODE_APPROVAL_INTENT_MISMATCH, wf.OUTCOME_MISMATCH),
    (pc.CODE_APPROVAL_REFUSED, wf.OUTCOME_REFUSED),
    (pc.CODE_APPROVAL_REQUIRED, wf.OUTCOME_BLOCKED),
    (pc.CODE_DENIED, wf.OUTCOME_BLOCKED),
    (pc.CODE_APPROVED, wf.OUTCOME_BLOCKED),  # "approved" but did not run: not a success
])
def test_outcome_of_a_call_that_did_not_run(code, outcome):
    assert wf.outcome_of(GatedActionResult(code=code)) == outcome


# ---------------------------------------------------------------------------
# run_gated: the side effect runs only on a permitted decision
# ---------------------------------------------------------------------------


class _Lookup:
    def __init__(self, status: str, ref: str = "aar_" + "1" * 32) -> None:
        self.status, self.ref = status, ref

    def redeem(self, request, *, rule_id, policy_version, approval_ref=""):
        return pc.ApprovalOutcome(self.status, approval_ref=self.ref)


@pytest.mark.parametrize("status", [
    pc.APPROVAL_NONE, pc.APPROVAL_PENDING, pc.APPROVAL_DENIED, pc.APPROVAL_EXPIRED, pc.APPROVAL_CONSUMED,
    pc.APPROVAL_MISMATCH, pc.APPROVAL_REFUSED, pc.APPROVAL_UNKNOWN, "granted-ish",
])
def test_run_gated_never_calls_the_side_effect_without_a_spent_receipt(status):
    called: list[int] = []
    out = act.run_gated(_action(), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", {"a": 1}, lambda: called.append(1) or {},
                        grants=GRANTS, approvals=_Lookup(status))
    assert not out.ran and called == []


def test_run_gated_runs_after_a_spent_receipt():
    out = act.run_gated(_action(), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", {"a": 1}, lambda: {"ok": True},
                        grants=GRANTS, approvals=_Lookup(pc.APPROVAL_GRANTED))
    assert out.ran and out.code == pc.CODE_APPROVED and out.result == {"ok": True}


def test_run_gated_refuses_arguments_it_cannot_digest(capsys):
    out = act.run_gated(_action(), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", {"a": object()},
                        lambda: pytest.fail("ran"), grants=GRANTS, approvals=_Lookup(pc.APPROVAL_GRANTED))
    assert not out.ran and out.code == pc.CODE_INVALID_REQUEST
    # The audit line names who attempted it.
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(pc.DECISION_PREFIX))
    record = json.loads(line.removeprefix(pc.DECISION_PREFIX))
    assert (record["execution_id"], record["trace_id"], record["actor"]) == ("ctx-198", "tr-198", "system:dev-loop")


def test_run_gated_reports_a_side_effect_that_raises():
    def boom() -> dict[str, Any]:
        raise RuntimeError("502")
    out = act.run_gated(_action(), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", {"a": 1}, boom,
                        grants=GRANTS, approvals=_Lookup(pc.APPROVAL_GRANTED))
    assert not out.ran and out.code == pc.CODE_APPROVED and out.effect_error == "RuntimeError: 502"
    assert wf.outcome_of(out) == wf.OUTCOME_EFFECT_FAILED


def test_run_gated_binds_the_identity_the_workflow_passed(api):
    """The worker has no execution-context file: the request must carry
    the identity from the input, or mctl-api has nothing to bind."""
    first = act.run_gated(_action(), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", {"a": 1}, lambda: {}, grants=GRANTS)
    assert first.awaiting_approval
    rec = api.records[first.approval_ref]
    assert rec["execution_id"] == "ctx-198"
    anon = act.run_gated(GatedActionInput(payload={}), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", {"a": 1},
                         lambda: {}, grants=GRANTS)
    assert anon.code == pc.CODE_APPROVAL_REFUSED and len(api.records) == 1


def test_attempts_are_distinct_requests_for_the_same_intent(api):
    lookup = pc.configured_approvals()
    assert isinstance(lookup, aa.MctlApiApprovals) and lookup.attempt == 0
    assert act.approvals_for_attempt(3).attempt == 3  # type: ignore[attr-defined]
    refs = {
        act.run_gated(_action(n), pc.MCP_TOOL_CALL, DEPLOY, "mctl-web", {"a": 1}, lambda: {},
                      grants=GRANTS).approval_ref
        for n in (0, 1, 0)
    }
    assert len(refs) == 2 and len(api.records) == 2
    with pytest.raises(ValueError):
        aa.MctlApiApprovals(attempt=-1)


# ---------------------------------------------------------------------------
# The read-only poll
# ---------------------------------------------------------------------------


def test_the_poll_is_a_read(api):
    rid = _pending_receipt(api)
    api.calls.clear()
    poll = act._read(rid)
    assert (poll.state, poll.expires_at) == ("pending", api.records[rid]["expires_at"])
    assert api.calls == [("GET", f"/api/v1/action-approvals/{rid}")]
    api.override["get"] = lambda req: api._err(503, "unavailable")
    assert act._read(rid).state == act.POLL_UNKNOWN


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def test_the_recorded_wait_replays_against_current_definitions():
    """A NondeterminismError here means an edit would wedge an in-flight
    approval wait: guard it with `workflow.patched()`; do not re-record to
    make it pass."""
    history = WorkflowHistory.from_json("replay-action-approval-wait", HISTORY.read_text(encoding="utf-8"))
    await Replayer(workflows=[ActionApprovalWaitWorkflow]).replay_workflow(history)


def test_the_fixture_records_a_poll_wake_and_a_signal_wake():
    events = json.loads(HISTORY.read_text(encoding="utf-8"))["events"]
    scheduled = [e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
                 for e in events if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"]
    signals = [e for e in events if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED"]
    timers = [e for e in events if e["eventType"] == "EVENT_TYPE_TIMER_FIRED"]
    # first poll, a pending poll tick, the spurious-signal re-check, the
    # approved re-check
    assert scheduled == ["read_action_approval", "read_action_approval", GATED, GATED]
    assert len(signals) == 2 and len(timers) == 1


async def record(env: WorkflowEnvironment, api: FakeMctlApi) -> dict[str, Any]:
    """A wait with one pending poll tick, a spurious signal, then an
    approval and its signal."""
    rid = _pending_receipt(api)
    async with _worker(env):
        handle = await _start_wait(env, rid)
        await _waiting(handle)
        await env.sleep(61)  # one poll tick: still pending
        await _until(lambda: _polled(handle, 1), "the poll tick")
        await _signal(handle, rid)
        await _until(lambda: _rechecked(handle, 1), "the spurious re-check")
        api.decide(rid, "approve")
        await _signal(handle, rid)
        result = await _result(handle)
        assert result.outcome == wf.OUTCOME_RAN
        history = await handle.fetch_history()
    return history.to_json_dict()


async def _polled(handle: WorkflowHandle, n: int) -> bool:
    return (await _state(handle)).poll_wakes >= n


async def test_record_produces_the_fixture_shape(env, api):
    history = await record(env, api)
    scheduled = [e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
                 for e in history["events"] if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"]
    assert scheduled == ["read_action_approval", "read_action_approval", GATED, GATED]


async def _main() -> None:
    import os
    from unittest import mock

    os.environ.update({pc.APPROVALS_ENV: pc.APPROVALS_MCTL_API, "MCTL_TOKEN": "test-token",
                       "MCTL_API_BASE_URL": "https://api.example.test"})
    fake = FakeMctlApi(_Clock())  # type: ignore[arg-type]
    WORLD.clear()
    WORLD.update({"head": "sha-1", "effects": 0, "calls": []})
    with mock.patch.object(aa, "_no_redirect_opener", lambda: fake):
        async with await WorkflowEnvironment.start_time_skipping() as env:
            history = await record(env, fake)
    HISTORY.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {HISTORY}")


if __name__ == "__main__":
    asyncio.run(_main())
