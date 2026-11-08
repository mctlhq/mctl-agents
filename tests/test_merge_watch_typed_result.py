"""#1021 M2b-1d-1 (#1046): the typed merge-watch result, the delivery refusal
flag and the CLI summary lines. All plumbing: nothing in production sets
`_reconcile_required` or a `watch_end`, so these tests drive the new branches
through test-only probes and stubs."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import logging
import typing
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.converter import DataConverter

from orchestrator.temporal import cli
from orchestrator.temporal.activities.argo import WorkflowResult
from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.constants import TASK_QUEUE
from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.temporal.workflows import dev_loop
from orchestrator.temporal.workflows.dev_loop import (
    MERGE_WATCH_RECONCILE_ENDED_FALLBACK,
    MERGE_WATCH_RECONCILE_REFUSAL,
    MERGE_WATCH_RECONCILE_REQUIRED_OUTCOME,
    RESUME_DEFERRAL_REASONS,
    WATCH_END_ABANDONED_UNVERIFIED,
    WATCH_END_NO_PR_CONFIRMED,
    WATCH_END_RECONCILE_REQUIRED,
    DevLoopResult,
    DevLoopWorkflow,
    IssueRef,
    MergeWatchResume,
    OpenDelivery,
    _WatchOutcome,
)
from tests.temporal_harness import Worker
from tests.test_execution_request_dispatch import _dispatcher, _ledger_entry, _loop_activities, _wait_for
from tests.test_execution_request_resume import (
    OPEN_PR,
    _delivery,
    _row,
    _stop,
    _submit_log,
    api,  # noqa: F401  (fixture)
    env,  # noqa: F401  (fixture)
)
from tests.test_work_context_resume_acceptance import URL, WID

pytestmark = pytest.mark.anyio

_CONVERTER = DataConverter.default
_OK = WorkflowResult(workflow_name="w", phase="Succeeded")
_NEW_FIELDS = ("watch_end", "last_observed_pr", "reconcile_record")


def _roundtrip(value: Any, to_type: type) -> Any:
    return _CONVERTER.payload_converter.from_payloads(_CONVERTER.payload_converter.to_payloads([value]), [to_type])[0]


# -- T1: inertness pins ---------------------------------------------------------


def test_the_flag_and_the_refusal_are_inert_plumbing():
    assert MERGE_WATCH_RECONCILE_REFUSAL == "merge-watch-reconcile-required"
    assert MERGE_WATCH_RECONCILE_REFUSAL not in RESUME_DEFERRAL_REASONS
    assert "reconcile_required" not in {f.name for f in dataclasses.fields(MergeWatchResume)}
    assert dataclasses.fields(_WatchOutcome)[-2].name == "watch_end"


def test_nothing_in_production_sets_the_flag_or_a_watch_end():
    """1d-1-only scan: 1d-2 and 1d-3 remove it explicitly."""
    tree = ast.parse(inspect.getsource(dev_loop))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and t.attr == "_reconcile_required":
                    assert not (isinstance(node.value, ast.Constant) and node.value.value is True)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_WatchOutcome":
            assert "watch_end" not in {k.arg for k in node.keywords}


# -- T3 / T4: payload compatibility ----------------------------------------------


def _old_payload() -> dict[str, Any]:
    full = _CONVERTER.payload_converter.to_payloads([DevLoopResult(investigate=_OK, implement=_OK, ended="x")])[0]
    import json

    data = json.loads(full.data)
    for name in _NEW_FIELDS:
        data.pop(name)
    return data


def test_an_old_result_payload_decodes_with_the_new_fields_defaulted():
    import json

    from temporalio.api.common.v1 import Payload

    data = _old_payload()
    payload = Payload(metadata={"encoding": b"json/plain"}, data=json.dumps(data).encode())
    result = _CONVERTER.payload_converter.from_payloads([payload], [DevLoopResult])[0]
    assert (result.watch_end, result.last_observed_pr, result.reconcile_record) == ("", None, "")
    assert result.ended == "x" and result.implement == _OK


def test_a_new_result_decodes_into_the_pre_change_class_dropping_the_new_fields():
    """The pinned rollback hazard: an older reader silently drops the three
    fields (Temporal ignores unknown keys)."""
    hints = typing.get_type_hints(DevLoopResult)
    specs = []
    for f in dataclasses.fields(DevLoopResult):
        if f.name in _NEW_FIELDS:
            continue
        kw: dict[str, Any] = {}
        if f.default is not dataclasses.MISSING:
            kw["default"] = f.default
        specs.append((f.name, hints[f.name], dataclasses.field(**kw)))
    old_cls = dataclasses.make_dataclass("OldDevLoopResult", specs, frozen=True)
    new = DevLoopResult(
        investigate=_OK,
        implement=_OK,
        watch_end=WATCH_END_RECONCILE_REQUIRED,
        last_observed_pr=OPEN_PR,
        reconcile_record="rec",
    )
    old = _roundtrip(new, old_cls)
    assert not any(hasattr(old, n) for n in _NEW_FIELDS)
    back = _roundtrip(old, DevLoopResult)
    assert (back.watch_end, back.last_observed_pr, back.reconcile_record) == ("", None, "")


# -- T5: the result tail ---------------------------------------------------------


@workflow.defn(name="FinishAfterWatchProbe", sandboxed=False)
class _FinishProbe:
    @workflow.run
    async def run(self, args: dict[str, Any]) -> DevLoopResult:
        loop = DevLoopWorkflow.__new__(DevLoopWorkflow)
        loop._abandoned = bool(args.get("abandoned"))  # type: ignore[attr-defined]
        loop._abandon_reason = "op"  # type: ignore[attr-defined]
        return await loop._finish_after_watch(  # type: ignore[attr-defined]
            target_repo="mctl-telegram",
            investigate_result=_OK,
            implement_result=_OK,
            approve_result=None,
            human_input_outcome=None,
            outcome=_WatchOutcome(
                last=PRState(**args["last"]), ended=args.get("ended", ""), watch_end=args.get("watch_end", ""),
                reconcile_record=args.get("record", ""),
            ),
        )


MERGED = PRState(found=True, pr_url="https://github.com/o/r/pull/1", state="MERGED", merged=True, merge_commit="abc")


async def _finish(env_: Any, **args: Any) -> tuple[DevLoopResult, list[str]]:
    result, scheduled, _ = await _finish_full(env_, **args)
    return result, scheduled


async def _finish_full(env_: Any, **args: Any) -> tuple[DevLoopResult, list[str], list[str]]:
    async with Worker(env_.client, task_queue=TASK_QUEUE, workflows=[_FinishProbe], activities=[]):
        handle = await env_.client.start_workflow(
            _FinishProbe.run, args, id=f"finish-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        result = await handle.result()
        events = (await handle.fetch_history()).events
    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    markers = [
        e.marker_recorded_event_attributes.marker_name
        for e in events
        if e.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]
    return result, scheduled, markers


async def test_a_reconcile_required_end_returns_the_typed_result(env, caplog):  # noqa: F811
    caplog.set_level(logging.WARNING)
    result, scheduled = await _finish(
        env, last=OPEN_PR, watch_end=WATCH_END_RECONCILE_REQUIRED, record="rec-1", ended="deadline: x"
    )
    assert result.pr is None and result.last_observed_pr == OPEN_PR
    assert result.reconcile_record == "rec-1" and result.watch_end == WATCH_END_RECONCILE_REQUIRED
    assert result.outcome == MERGE_WATCH_RECONCILE_REQUIRED_OUTCOME
    assert result.ended == "deadline: x"
    assert result.deploy is None and result.security is None and result.incidents is None
    assert scheduled == []


async def test_reconcile_required_beats_abandon_and_falls_back_on_empty_ended(env):  # noqa: F811
    result, _ = await _finish(
        env, last=OPEN_PR, watch_end=WATCH_END_RECONCILE_REQUIRED, ended="why", abandoned=True
    )
    assert result.ended == "why"
    result, _ = await _finish(env, last=OPEN_PR, watch_end=WATCH_END_RECONCILE_REQUIRED, abandoned=True)
    assert result.ended == MERGE_WATCH_RECONCILE_ENDED_FALLBACK


async def test_a_reconcile_required_end_schedules_no_stage_even_for_a_merged_pr(env):  # noqa: F811
    result, scheduled = await _finish(env, last=MERGED, watch_end=WATCH_END_RECONCILE_REQUIRED)
    assert scheduled == []
    assert result.deploy is None and result.security is None and result.incidents is None
    assert result.pr is None and result.last_observed_pr == MERGED


async def test_the_patched_markers_do_not_depend_on_the_watch_end(env):  # noqa: F811
    _, _, reconcile = await _finish_full(env, last=OPEN_PR, watch_end=WATCH_END_RECONCILE_REQUIRED)
    _, _, plain = await _finish_full(env, last=OPEN_PR)
    assert reconcile == plain
    assert reconcile  # the three `patched` markers were recorded


async def test_every_other_watch_end_keeps_todays_result(env):  # noqa: F811
    plain, _ = await _finish(env, last=OPEN_PR, abandoned=True)
    assert plain.pr == OPEN_PR and plain.ended == "abandoned: op" and plain.outcome == ""
    assert (plain.watch_end, plain.last_observed_pr, plain.reconcile_record) == ("", None, "")
    unverified, _ = await _finish(env, last=OPEN_PR, abandoned=True, watch_end=WATCH_END_ABANDONED_UNVERIFIED)
    assert unverified.pr == OPEN_PR and unverified.ended == "abandoned: op" and unverified.outcome == ""
    assert unverified.watch_end == WATCH_END_ABANDONED_UNVERIFIED and unverified.last_observed_pr is None


# -- T6: the delivery refusal (check 1, and the pre-existing early exits) --------


@workflow.defn(name="DevLoopWorkflow", sandboxed=False)
class _FlaggedLoop(DevLoopWorkflow):
    """Test-only: the flag is set by a signal, because nothing sets it."""

    @workflow.run
    async def run(self, issue: IssueRef) -> Any:
        return await super().run(issue)

    @workflow.signal
    def flag(self) -> None:
        self._reconcile_required = True


def _in_delivery_wait() -> bool:
    """True when the caller of `_is_approved` is the wait predicate of
    `DevLoopWorkflow._deliver` (a lambda defined inside `_deliver`). Located by
    code position, not by line number, so it follows edits of `_deliver`."""
    lines, first = inspect.getsourcelines(DevLoopWorkflow._deliver)
    caller = inspect.currentframe().f_back.f_back.f_code  # type: ignore[union-attr]
    return caller.co_name == "<lambda>" and first < caller.co_firstlineno < first + len(lines)


@workflow.defn(name="DevLoopWorkflow", sandboxed=False)
class _FlagDuringWaitLoop(DevLoopWorkflow):
    """Test-only (T6b): the flag turns true when the delivery's wait predicate
    is evaluated, i.e. AFTER check 1 saw it false. Only check 2 can refuse."""

    @workflow.run
    async def run(self, issue: IssueRef) -> Any:
        return await super().run(issue)

    def _is_approved(self, *, needs_grant: bool = True) -> bool:
        if _in_delivery_wait():
            self._reconcile_required = True
        return super()._is_approved(needs_grant=needs_grant)


@workflow.defn(name="DevLoopWorkflow", sandboxed=False)
class _FlagClearedAtWaitLoop(DevLoopWorkflow):
    """Test-only (T6c): the flag is true at check 1 and false again by the time
    the wait resolves. Only check 1 can refuse."""

    @workflow.run
    async def run(self, issue: IssueRef) -> Any:
        return await super().run(issue)

    @workflow.signal
    def flag(self) -> None:
        self._reconcile_required = True

    def _is_approved(self, *, needs_grant: bool = True) -> bool:
        if _in_delivery_wait():
            self._reconcile_required = False
        return super()._is_approved(needs_grant=needs_grant)


async def _deliver_with(
    fake, env_, *, flagged: bool, loop_cls: type | None = None, **open_kwargs: Any
) -> tuple[str, list[str]]:
    submit, _ = _submit_log()
    loop = workflow_id_for(URL)
    rid = fake.create_request("resume")
    ref = f"{loop}#{rid}"
    _ledger_entry(fake, "temporal", loop, "Succeeded")
    carried = MergeWatchResume(
        service="mctl-telegram",
        slug="issue-431-dispatch-acceptance",
        deadline="2099-01-01T00:00:00Z",
        investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
        implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
        work_item_id=WID,
        accepted_request_ids=(rid,),
        deferred_resume_contract=True,
        open_deliveries=(OpenDelivery(delivery=_delivery(rid), **open_kwargs),),
    )
    activities = _loop_activities(submit, pr_states=[OPEN_PR])
    cls = loop_cls or (_FlaggedLoop if flagged else DevLoopWorkflow)
    async with Worker(env_.client, task_queue=TASK_QUEUE, workflows=[cls], activities=activities):
        extra: dict[str, Any] = {"start_signal": "flag"} if flagged else {}
        handle = await env_.client.start_workflow(
            cls.run,
            IssueRef(issue_url=URL, work_item_id=WID, resume=carried),
            id=loop,
            task_queue=TASK_QUEUE,
            **extra,
        )
        await _dispatcher(env_).dispatch_once()
        await _wait_for(lambda: _row(fake, ref)["phase"] in ("Succeeded", "Failed"))
        phase: str = _row(fake, ref)["phase"]
        # The fake row turns terminal when the advance activity returns, one
        # workflow task BEFORE `_end_delivery` pops the open delivery and
        # records the refusal. Wait for that close so a query in the gap
        # cannot read `[]` on a slow runner.
        for _ in range(400):
            if not (await handle.query(DevLoopWorkflow.contract_state)).open_request_ids:
                break
            await anyio.sleep(0.05)
        assert not (await handle.query(DevLoopWorkflow.contract_state)).open_request_ids, "delivery never closed"
        state = await handle.query(DevLoopWorkflow.work_context)
        reasons = [r.reason for r in state.resume_rejections]
        await _stop(env_, loop)
    return phase, reasons


REFUSED = ["delivery-" + MERGE_WATCH_RECONCILE_REFUSAL]


async def test_a_flagged_loop_refuses_a_bound_delivery(api, env):  # noqa: F811
    assert await _deliver_with(api, env, flagged=True) == ("Failed", REFUSED)


async def test_an_unflagged_loop_still_lands_the_same_delivery_succeeded(api, env):  # noqa: F811
    assert await _deliver_with(api, env, flagged=False) == ("Succeeded", [])


async def test_the_flag_does_not_change_accepted_after_the_gates(api, env):  # noqa: F811
    # The pre-existing early exit returns before check 1: its reason stays
    # `feedback-not-ready`, never the new refusal.
    assert await _deliver_with(api, env, flagged=True, accepted_after_gates=True) == (
        "Failed",
        ["delivery-feedback-not-ready"],
    )


async def test_a_flag_set_while_the_delivery_waits_is_caught_by_the_check_after_the_wait(api, env):  # noqa: F811
    # Check 1 saw the flag false; only check 2 (after `wait_condition`) can refuse.
    assert await _deliver_with(api, env, flagged=False, loop_cls=_FlagDuringWaitLoop) == ("Failed", REFUSED)


async def test_a_flag_that_is_gone_by_the_wait_is_still_refused_by_the_check_after_the_bind(api, env):  # noqa: F811
    # The flag is true at check 1 and false again by check 2; only check 1 can refuse.
    assert await _deliver_with(api, env, flagged=True, loop_cls=_FlagClearedAtWaitLoop) == ("Failed", REFUSED)


# -- T7: CLI ---------------------------------------------------------------------


def _status(monkeypatch, capsys, result: DevLoopResult) -> str:
    handle = SimpleNamespace(
        describe=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(name="COMPLETED"))),
        result=AsyncMock(return_value=result),
    )
    client = SimpleNamespace(get_workflow_handle=lambda _wid: handle)

    async def _connect(*_a: Any, **_kw: Any) -> Any:
        return client

    monkeypatch.setattr(cli, "connect", _connect)
    import asyncio

    asyncio.run(cli.status("wf"))
    return capsys.readouterr().out


def test_cli_prints_a_reconcile_required_watch_with_its_record(monkeypatch, capsys):
    out = _status(
        monkeypatch,
        capsys,
        DevLoopResult(investigate=_OK, implement=_OK, watch_end=WATCH_END_RECONCILE_REQUIRED, reconcile_record="R1"),
    )
    assert "  pr:          reconcile required (R1)" in out and "not watched" not in out
    out = _status(
        monkeypatch, capsys, DevLoopResult(investigate=_OK, implement=_OK, watch_end=WATCH_END_RECONCILE_REQUIRED)
    )
    assert "reconcile required (no record)" in out and "not watched" not in out


def test_cli_prints_a_confirmed_absent_pr(monkeypatch, capsys):
    out = _status(
        monkeypatch, capsys, DevLoopResult(investigate=_OK, implement=_OK, watch_end=WATCH_END_NO_PR_CONFIRMED)
    )
    assert "  pr:          none (confirmed)" in out and "not watched" not in out


def test_cli_marks_an_unverified_abandoned_pr(monkeypatch, capsys):
    out = _status(
        monkeypatch,
        capsys,
        DevLoopResult(investigate=_OK, implement=_OK, pr=OPEN_PR, watch_end=WATCH_END_ABANDONED_UNVERIFIED),
    )
    assert f"  pr:          OPEN ({OPEN_PR.pr_url}) (last observed before abandon, not verified current)" in out


def test_cli_output_is_unchanged_without_a_watch_end(monkeypatch, capsys):
    out = _status(monkeypatch, capsys, DevLoopResult(investigate=_OK, implement=_OK, pr=OPEN_PR))
    assert f"  pr:          OPEN ({OPEN_PR.pr_url})\n" in out
    out = _status(monkeypatch, capsys, DevLoopResult(investigate=_OK, implement=_OK))
    assert "  pr:          not watched" in out
