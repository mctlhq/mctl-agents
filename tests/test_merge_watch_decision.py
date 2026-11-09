"""#1021 M2b-1d-2a (#1052): the merge watch's deadline decision, built inert.

`DevLoopWorkflow._decide_watch_end` and its helpers have no production caller
in this slice, so these tests reach them through a test-only subclass
(`_DecisionHarness`) that overrides `run`. The harness proves the decision's
reads, ordering, kinds and flag lifecycle against the real helpers and the
real `_apply_found_state`, `_terminal_claim` and `_settle_*`. It does NOT prove
the wiring in `_watch_pr`, the `finally` guards, `_deliver`, any cancellation
surface, or the real `ActionApprovalWaitWorkflow` (the standing-in gate is a
plain task). Those are slice 1d-2b.

`test_activation_pin` (D17) replaced the 1d-2a inertness pin in 1d-2b-i
(#1055): `_watch_pr` now calls the decision from its `while ... else:` arm.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.lifecycle import OwnershipRequest, OwnershipResult
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.workflows import dev_loop
from orchestrator.temporal.workflows.action_approval import (
    GATED_ACTIVITY_TIMEOUT,
    OUTCOME_DENIED,
    OUTCOME_RAN,
    ApprovalWaitResult,
)
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef, _MergeWatchRun
from tests.temporal_harness import Worker

pytestmark = pytest.mark.anyio

REPO = "mctl-telegram"
HEAD = "abc123"


def _pr(state: str, head: str = HEAD, number: int = 9) -> PRState:
    return PRState(
        found=True,
        pr_url=f"https://github.com/{REPO}/pull/{number}",
        repo=REPO,
        number=number,
        state=state,
        merged=state == "MERGED",
        head_sha=head,
    )


OPEN, MERGED, CLOSED = _pr("OPEN"), _pr("MERGED"), _pr("CLOSED")
NOT_FOUND = PRState(found=False)

# -- the scenario the harness runs (module-level: the harness is unsandboxed) ----


@dataclasses.dataclass
class Scenario:
    last: PRState | None = None
    track: bool = False
    owned: bool = False  # the loop already holds the claim for PR #9
    tick: str = ""  # "" | "plain"
    gate: str = ""  # "" | "plain" | "stubborn" (waits for `release`) | "slow" (3 min)
    # | "ran" (a cancel it swallows: waits for `release`, then returns a `ran` result)
    gate_head: str = ""  # the head `_record_gate_result` stamps when a gate that RAN is recorded
    ran_head: str = ""
    raise_in_apply: bool = False


_SC = Scenario()


@workflow.defn(name="MergeWatchDecisionHarness", sandboxed=False)
class _DecisionHarness(DevLoopWorkflow):
    """Test-only: runs the deadline decision once and reports what it did."""

    @workflow.run
    async def run(self, issue: IssueRef) -> dict[str, Any]:
        self._released = False
        self._snap: dict[str, Any] = {"kind": "", "raised": "", "last": "", "in_cancel": False}
        sc = _SC
        watch = _MergeWatchRun(
            last=sc.last,
            polls_without_pr=3,
            terminal_status_polls=2,
            saw_open_pr=False,
            track_ownership=sc.track,
            poll_index=7,
            terminal_reconcile=True,
        )
        if sc.owned:
            self._owned_entity_id = f"{REPO}#9"
            self._owned_head_sha = HEAD
        self._gate_ran_head = sc.ran_head
        self._gate_head = sc.gate_head

        async def tick() -> None:
            await workflow.wait_condition(lambda: False)

        async def gate() -> ApprovalWaitResult | None:
            try:
                await workflow.wait_condition(lambda: False)
            except asyncio.CancelledError:
                self._snap["in_cancel"] = True
                if sc.gate == "stubborn":
                    await workflow.wait_condition(lambda: self._released)
                elif sc.gate == "slow":
                    await workflow.sleep(timedelta(minutes=3))
                elif sc.gate == "ran":
                    await workflow.wait_condition(lambda: self._released)
                    return ApprovalWaitResult(outcome=OUTCOME_RAN)
                raise
            return None

        if sc.tick:
            watch.tick_task = asyncio.ensure_future(tick())
        if sc.gate:
            watch.gate_task = asyncio.ensure_future(gate())
        await asyncio.sleep(0)  # let the tasks reach their first await

        try:
            decision = await self._decide_watch_end("deadline", watch, "svc", "slug")
            self._snap["kind"] = decision.kind
            self._snap["last"] = decision.last.state if decision.last is not None else ""
        except Exception as exc:  # noqa: BLE001 -- the exception case reports it
            self._snap["raised"] = type(exc).__name__
        return self.snap() | {
            "watch_last": watch.last.state if watch.last is not None else "",
            "last_pr": self._last_pr.state if self._last_pr is not None else "",
            "saw_open_pr": watch.saw_open_pr,
            "polls_without_pr": watch.polls_without_pr,
            "terminal_status_polls": watch.terminal_status_polls,
            "tasks_cleared": watch.tick_task is None and watch.gate_task is None,
            "gate_ran_head": self._gate_ran_head,
        }

    async def _apply_found_state(self, state: PRState, watch: _MergeWatchRun) -> None:
        if _SC.raise_in_apply:
            raise ApplicationError("boom", non_retryable=True)
        await super()._apply_found_state(state, watch)

    @workflow.query
    def snap(self) -> dict[str, Any]:
        return {**self._snap, "flag": self._watch_unrecorded}

    @workflow.signal
    def release(self) -> None:
        self._released = True


# -- fake activities --------------------------------------------------------------


class Fakes:
    """`get_pr_state` answers `script` call by call (the last entry repeats).
    An entry is a PRState, "transient", "fatal", or ("block", PRState): the
    last waits for `unblock` before answering."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls = 0
        self.ops: list[str] = []
        self.unblock = asyncio.Event()

    def activities(self) -> list[Any]:
        @activity.defn(name="get_pr_state")
        async def get_pr_state(service: str, slug: str) -> PRState:
            item = self.script[min(self.calls, len(self.script) - 1)]
            self.calls += 1
            if isinstance(item, tuple):
                await self.unblock.wait()
                return item[1]  # type: ignore[no-any-return]
            if item == "transient":
                raise ApplicationError("github blip", type="ProposalListingError")
            if item == "fatal":
                raise ApplicationError("github unreachable", type="Bug", non_retryable=True)
            return item  # type: ignore[no-any-return]

        @activity.defn(name="lifecycle_ownership")
        async def lifecycle_ownership(req: OwnershipRequest) -> OwnershipResult:
            self.ops.append(req.op)
            return OwnershipResult(
                verdict="owned-by-me",
                owner_type=req.owner_type,
                owner_id=req.owner_id,
                epoch=1,
                state="active",
                healthy=True,
                accepted=True,
            )

        return [get_pr_state, lifecycle_ownership]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


class Run:
    def __init__(self, env_: WorkflowEnvironment, fakes: Fakes, scenario: Scenario) -> None:
        self.env, self.fakes, self.scenario = env_, fakes, scenario
        self.handle: Any = None

    async def events(self) -> list[Any]:
        return list((await self.handle.fetch_history()).events)

    async def until(self, pred: Any) -> None:
        for _ in range(400):
            if pred(await self.events()):
                return
            await anyio.sleep(0.05)
        raise AssertionError("condition never reached")

    async def snap(self) -> dict[str, Any]:
        return await self.handle.query(_DecisionHarness.snap)  # type: ignore[no-any-return]


def _reads(events: list[Any]) -> int:
    return sum(1 for e in _scheduled(events) if e == "get_pr_state")


def _scheduled(events: list[Any]) -> list[str]:
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


def _timers(events: list[Any]) -> list[timedelta]:
    return [
        e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
        for e in events
        if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    ]


def _window(events: list[Any]) -> list[timedelta]:
    return [t for t in _timers(events) if t > timedelta(minutes=5)]


class _running:
    def __init__(self, env_: WorkflowEnvironment, fakes: Fakes, scenario: Scenario, *, abandon_first: bool = False):
        self.run = Run(env_, fakes, scenario)
        self.abandon_first = abandon_first

    async def __aenter__(self) -> Run:
        global _SC
        _SC = self.run.scenario
        self.worker = Worker(
            self.run.env.client,
            task_queue=TASK_QUEUE,
            workflows=[_DecisionHarness],
            activities=self.run.fakes.activities(),
        )
        await self.worker.__aenter__()
        kwargs: dict[str, Any] = {"start_signal": "abandon"} if self.abandon_first else {}
        self.run.handle = await self.run.env.client.start_workflow(
            _DecisionHarness.run,
            IssueRef(issue_url="https://github.com/mctlhq/mctl-agents/issues/1052"),
            id=f"decision-{uuid.uuid4().hex[:8]}",
            task_queue=TASK_QUEUE,
            **kwargs,
        )
        return self.run

    async def __aexit__(self, *exc: Any) -> None:
        self.run.fakes.unblock.set()
        await self.worker.__aexit__(*exc)


async def _go(env_, fakes: Fakes, scenario: Scenario, **kw: Any) -> tuple[dict[str, Any], list[Any]]:
    async with _running(env_, fakes, scenario, **kw) as run:
        result = await run.handle.result()
        return result, await run.events()


# -- A1-A3: a readable final read ----------------------------------------------------


async def test_a1_final_read_merged_writes_terminal_once(env):
    fakes = Fakes(MERGED)
    result, events = await _go(env, fakes, Scenario(last=OPEN, track=True, owned=True))
    assert (result["kind"], result["last"], result["flag"]) == ("final", "MERGED", False)
    assert _reads(events) == 1
    assert fakes.ops == ["terminal"]
    # `terminal` follows the read that returned the terminal state (A5, non-vacuous part).
    assert _scheduled(events) == ["get_pr_state", "lifecycle_ownership"]
    assert _timers(events) == []


async def test_a2_final_read_open_returns_open_without_writes(env):
    fakes = Fakes(OPEN)
    result, events = await _go(env, fakes, Scenario(last=OPEN, track=True, owned=True))
    assert (result["kind"], result["last"], result["flag"]) == ("open", "OPEN", False)
    assert _reads(events) == 1 and _timers(events) == []
    assert "terminal" not in fakes.ops and "release" not in fakes.ops


async def test_a3_final_read_closed_writes_terminal(env):
    fakes = Fakes(CLOSED)
    result, _ = await _go(env, fakes, Scenario(last=OPEN, track=True, owned=True))
    assert (result["kind"], result["last"], result["flag"]) == ("final", "CLOSED", False)
    assert fakes.ops == ["terminal"]


# -- A4: an unreadable final read ----------------------------------------------------


@pytest.mark.parametrize("failure", ["transient", "fatal", NOT_FOUND], ids=["transient", "fatal", "not-found"])
async def test_a4_unreadable_final_read_is_unrecorded(env, failure):
    fakes = Fakes(failure)
    result, events = await _go(env, fakes, Scenario(track=True, owned=True))
    assert result["kind"] == "unrecorded" and result["flag"] is True and result["raised"] == ""
    assert _reads(events) == dev_loop.FINAL_READ_ATTEMPTS
    assert _timers(events) == [dev_loop.FINAL_READ_BACKOFF] * (dev_loop.FINAL_READ_ATTEMPTS - 1)
    assert fakes.ops == []
    assert set(_scheduled(events)) == {"get_pr_state"}  # no record, no confirm_no_pr


# -- A5: ordering ----------------------------------------------------------------------


async def test_a5_settle_runs_before_the_first_read(env):
    fakes = Fakes(MERGED)
    async with _running(env, fakes, Scenario(gate="stubborn")) as run:
        for _ in range(400):
            if (await run.snap())["in_cancel"]:
                break
            await anyio.sleep(0.05)
        snap = await run.snap()
        assert snap["in_cancel"] and snap["flag"] is True  # A9: True while blocked in the settle
        assert _reads(await run.events()) == 0 and fakes.calls == 0
        await run.handle.signal(_DecisionHarness.release)
        result = await run.handle.result()
        assert _reads(await run.events()) == 1
    assert result["kind"] == "final" and result["tasks_cleared"] is True


# -- D / WT / WS: regression tests for the mutations the first draft left alive ----------


@pytest.mark.parametrize("failure", ["transient", "fatal", NOT_FOUND], ids=["transient", "fatal", "not-found"])
async def test_d_unreadable_final_read_never_falls_back_to_the_last_seen_state(env, failure):
    # `watch.last` is OPEN, but the final read could not observe anything: that is
    # "could not observe", never "observed OPEN". Mutation (d): `fresh = read.fresh or watch.last`.
    fakes = Fakes(failure)
    result, events = await _go(env, fakes, Scenario(last=OPEN, track=True, owned=True))
    assert (result["kind"], result["flag"], result["raised"]) == ("unrecorded", True, "")
    assert result["last"] == "OPEN"  # the last observed state is still reported, as data
    assert _reads(events) == dev_loop.FINAL_READ_ATTEMPTS
    assert fakes.ops == []


async def test_wt_the_settle_waits_for_an_in_flight_gate_that_swallows_the_cancel(env):
    # A gate that swallows the cancel and finishes with `ran` must be RECORDED by the
    # settle: that is only so if the settle waits for the task (`workflow.wait`) and
    # then takes the finished-task path. Mutation (wt): drop `workflow.wait(in_flight)`.
    fakes = Fakes(OPEN)
    scenario = Scenario(last=OPEN, gate="ran", gate_head=HEAD)
    async with _running(env, fakes, scenario) as run:
        for _ in range(400):
            if (await run.snap())["in_cancel"]:
                break
            await anyio.sleep(0.05)
        await anyio.sleep(0.2)  # a second cancel, were it sent, would land by now
        snap = await run.snap()
        assert snap["in_cancel"] and snap["flag"] is True
        assert _reads(await run.events()) == 0 and fakes.calls == 0  # no read before the settle ends
        await run.handle.signal(_DecisionHarness.release)
        result = await run.handle.result()
        events = await run.events()
    assert result["gate_ran_head"] == HEAD  # the swallowed cancel's `ran` result was recorded
    assert (result["kind"], result["flag"]) == ("unrecorded", True)  # ran head == open head
    assert _reads(events) == 2 and len(_window(events)) == 1


async def test_ws_the_window_starts_after_the_settle_and_the_first_read(env):
    # Settle takes 3 minutes (slow gate), the first read set then spends one 2 minute
    # backoff before it sees OPEN. The window is the REST of GATED_ACTIVITY_TIMEOUT
    # counted from `settled_at`: T - 2 min. Mutation (ws): a full T from the wait
    # (10 min); the other wrong start, the decision's own start, would give T - 5 min.
    fakes = Fakes("fatal", OPEN)
    result, events = await _go(env, fakes, Scenario(last=OPEN, gate="slow"))
    assert (result["kind"], result["last"], result["flag"]) == ("unrecorded", "OPEN", True)
    assert _timers(events)[:2] == [timedelta(minutes=3), dev_loop.FINAL_READ_BACKOFF]  # order of events
    (window,) = _window(events)
    expected = GATED_ACTIVITY_TIMEOUT - dev_loop.FINAL_READ_BACKOFF
    assert expected - timedelta(seconds=5) <= window <= expected
    assert _reads(events) == 3  # fatal, OPEN, then the post-window read


# -- A6: abandon -----------------------------------------------------------------------


async def test_a6i_abandon_before_the_decision_settles_then_reads_nothing(env):
    fakes = Fakes(MERGED)
    result, events = await _go(env, fakes, Scenario(last=OPEN, tick="plain", gate="plain"), abandon_first=True)
    assert (result["kind"], result["last"], result["flag"]) == ("abandoned", "OPEN", False)
    assert result["tasks_cleared"] is True
    assert _reads(events) == 0


async def test_a6ii_abandon_during_a_backoff_wakes_at_once(env):
    fakes = Fakes("fatal")
    async with _running(env, fakes, Scenario(last=OPEN)) as run:
        await run.until(lambda e: len(_timers(e)) == 1)
        assert (await run.snap())["flag"] is True  # A9: True in a backoff
        await run.handle.signal(DevLoopWorkflow.abandon, "op")
        result = await run.handle.result()
        events = await run.events()
    assert (result["kind"], result["flag"]) == ("abandoned", False)
    assert _reads(events) == 1 and len(_timers(events)) == 1


async def test_a6iii_abandon_during_the_window_wakes_at_once(env):
    fakes = Fakes(OPEN)
    async with _running(env, fakes, Scenario(last=OPEN, gate="plain")) as run:
        await run.until(lambda e: len(_window(e)) == 1)
        assert (await run.snap())["flag"] is True  # A9: True in the window
        await run.handle.signal(DevLoopWorkflow.abandon, "op")
        result = await run.handle.result()
        events = await run.events()
    assert (result["kind"], result["last"], result["flag"]) == ("abandoned", "OPEN", False)
    assert _reads(events) == 1


async def test_a6iv_abandon_during_a_read_that_returns_open(env):
    fakes = Fakes(("block", OPEN))
    async with _running(env, fakes, Scenario()) as run:
        await run.until(lambda _e: fakes.calls == 1)
        await run.handle.signal(DevLoopWorkflow.abandon, "op")
        fakes.unblock.set()
        result = await run.handle.result()
    assert (result["kind"], result["last"], result["flag"]) == ("abandoned", "OPEN", False)


async def test_a6v_abandon_during_a_read_that_returns_merged_is_still_final(env):
    fakes = Fakes(("block", MERGED))
    async with _running(env, fakes, Scenario(track=True, owned=True)) as run:
        await run.until(lambda _e: fakes.calls == 1)
        await run.handle.signal(DevLoopWorkflow.abandon, "op")
        fakes.unblock.set()
        result = await run.handle.result()
    assert (result["kind"], result["last"], result["flag"]) == ("final", "MERGED", False)
    assert fakes.ops == ["terminal"]


# -- A7: own attempts ------------------------------------------------------------------


async def test_a7_in_flight_gate_is_settled_before_the_read_and_opens_the_window(env):
    fakes = Fakes(OPEN, MERGED)
    result, events = await _go(env, fakes, Scenario(last=OPEN, track=True, owned=True, gate="slow"))
    # A merge that lands during the window gives a second read and kind final.
    assert (result["kind"], result["last"], result["flag"]) == ("final", "MERGED", False)
    assert _reads(events) == 2 and fakes.ops == ["terminal"]
    # The window is measured from `settled_at`, after the 3 minute settle: ~10 minutes, not ~7.
    (window,) = _window(events)
    assert GATED_ACTIVITY_TIMEOUT - timedelta(seconds=5) <= window <= GATED_ACTIVITY_TIMEOUT


async def test_a7_open_after_the_window_is_unrecorded(env):
    fakes = Fakes(OPEN)
    result, events = await _go(env, fakes, Scenario(last=OPEN, tick="plain"))
    assert (result["kind"], result["last"], result["flag"]) == ("unrecorded", "OPEN", True)
    assert _reads(events) == 2 and len(_window(events)) == 1


async def test_a7_ran_head_equal_to_the_open_head_is_unrecorded(env):
    fakes = Fakes(OPEN)
    result, events = await _go(env, fakes, Scenario(last=OPEN, ran_head=HEAD))
    assert (result["kind"], result["flag"]) == ("unrecorded", True)
    assert _reads(events) == 2 and len(_window(events)) == 1


async def test_a7_a_different_ran_head_is_open_with_no_window(env):
    fakes = Fakes(OPEN)
    result, events = await _go(env, fakes, Scenario(last=OPEN, ran_head="other"))
    assert (result["kind"], result["flag"]) == ("open", False)
    assert _reads(events) == 1 and _timers(events) == []


# -- A8: bookkeeping -------------------------------------------------------------------


async def test_a8_final_read_open_books_the_state_and_takes_the_claim(env):
    fakes = Fakes(OPEN)
    result, _ = await _go(env, fakes, Scenario(track=True))
    assert result["kind"] == "open"
    assert (result["watch_last"], result["last_pr"], result["saw_open_pr"]) == ("OPEN", "OPEN", True)
    assert (result["polls_without_pr"], result["terminal_status_polls"]) == (0, 0)
    assert fakes.ops == ["acquire"]


async def test_a8_merged_first_seen_by_the_final_read_acquires_then_terminal(env):
    fakes = Fakes(MERGED)
    result, _ = await _go(env, fakes, Scenario(track=True))
    assert result["kind"] == "final"
    assert fakes.ops == ["acquire", "terminal"]


# -- A9: the flag --------------------------------------------------------------------


async def test_a9_the_flag_stays_true_after_an_exception_inside_the_decision(env):
    fakes = Fakes(OPEN)
    result, _ = await _go(env, fakes, Scenario(raise_in_apply=True))
    assert result["raised"] == "ApplicationError" and result["kind"] == "" and result["flag"] is True


# -- A10: `_record_gate_result` ------------------------------------------------------


def _bare() -> DevLoopWorkflow:
    wf = DevLoopWorkflow.__new__(DevLoopWorkflow)
    wf._gate_head = "head-1"
    wf._gate_settled_head = ""
    wf._gate_off_until_poll = 0
    wf._gate_ran_head = "earlier"
    return wf


def test_a10_gate_result_that_ran_records_its_head():
    wf = _bare()
    wf._record_gate_result(ApprovalWaitResult(outcome=OUTCOME_RAN), poll_index=1)
    assert wf._gate_ran_head == "head-1"


def test_a10_gate_result_that_did_not_run_leaves_the_head():
    wf = _bare()
    wf._record_gate_result(ApprovalWaitResult(outcome=OUTCOME_DENIED), poll_index=1)
    wf._record_gate_result(None, poll_index=1)
    assert wf._gate_ran_head == "earlier"


# -- D17a: the inertness pin -------------------------------------------------------------

HELPERS = {"_decide_watch_end", "_settle_watch_work", "_final_pr_read", "_wait_unless_abandoned"}
FLAG_OWNERS = {"__init__", "_decide_watch_end", "_settled", "_watch_pr", "_deliver"}
# `workflow.patched(` calls on main 55b4633, counted from the AST.
PATCHED_CALLS = 38  # 37 at 1d-2a; main gained DECIDE_NOTIFY_PATCH (#1054); #1055 adds none
FORBIDDEN = {
    "is_cancelled_exception",
    "_retain_claim",
    "tick_blocked_until",
    "record_merge_watch_reconcile",
    "confirm_no_pr",
}


def _mentions(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def test_activation_pin():
    path = Path(dev_loop.__file__)
    tree = ast.parse(path.read_text())
    owners: list[tuple[str, ast.AST]] = []
    for top in tree.body:
        if isinstance(top, ast.ClassDef):
            for item in top.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owners.append((item.name, item))
                else:
                    owners.append((f"<{top.name} body>", item))
        elif isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owners.append((top.name, top))
        else:
            owners.append(("<module>", top))
    by_name = {name: node for name, node in owners if name in {"_watch_pr", "_deliver"}}
    assert set(by_name) == {"_watch_pr", "_deliver"}

    helper_names = HELPERS | {"_settled"}
    # The helpers are called only from each other and `_watch_pr`.
    for name, node in owners:
        if name not in helper_names and name != "_watch_pr":
            assert not (_mentions(node) & HELPERS), f"{name} mentions a deadline-decision helper"
    # `_watch_pr` has exactly one `while ... else`, and it calls the decision.
    watch_pr = by_name["_watch_pr"]
    elses = [n for n in ast.walk(watch_pr) if isinstance(n, ast.While) and n.orelse]
    assert len(elses) == 1
    called = {
        n.func.attr
        for stmt in elses[0].orelse
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "_decide_watch_end" in called
    # The flag lives only where the design says (R9, R11, R12).
    for name, node in owners:
        if "_watch_unrecorded" in _mentions(node):
            assert name in FLAG_OWNERS, f"{name} mentions _watch_unrecorded"
    # The unrecorded marker has readers beyond its definition.
    readers = [
        n
        for name, node in owners
        if name != "<module>"
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and n.id == "MERGE_WATCH_RECONCILE_UNRECORDED"
    ]
    assert readers
    # Nothing of 1d-3/1d-4 exists yet.
    for name, node in owners:
        assert not (_mentions(node) & FORBIDDEN), f"{name} mentions a 1d-3/1d-4 name"
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Constant)
                and n.value.value is True
                and any(isinstance(t, ast.Attribute) and t.attr == "_reconcile_required" for t in n.targets)
            ):
                raise AssertionError(f"{name} sets _reconcile_required")
    patched = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "patched"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "workflow"
    )
    assert patched == PATCHED_CALLS
