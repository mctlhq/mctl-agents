"""B4 gate, item P-5 (mctlhq/mctl-agents#1007): M1 against pre-M1 state.

M1 (mctlhq/mctl-agents#996, #998) added `workflow.patched("deferred-resume-
contract")` to DevLoopWorkflow, unconditionally at the point the approval
gates pass, and new DEFAULTED fields on four dataclasses. This file is the
repository evidence for both directions of that change.

SUPPORTED: M1 code takes over state written by pre-M1 code.
  - Every workflow type the worker registers replays, under M1 code, a history
    RECORDED BY PRE-M1 CODE (a99c888, the 1.117.0 release line). The fixtures
    in `tests/fixtures/histories/pre_m1/` were produced by
    `tools/record_pre_m1_histories.py` run inside an a99c888 checkout.
  - Replay alone compares command TYPES, not command arguments (an activity
    scheduled with a different input replays green). So every pre-M1 history
    is also replayed through an outbound interceptor and each activity's and
    child workflow's input is compared with the recorded scheduled input.
    That is what catches M1 changing behaviour on the legacy path while
    keeping the command shape (e.g. a post-gates delivery advancing
    `Failed` instead of the recorded `Succeeded`).
  - Pre-M1 payloads (hand-built from the a99c888 field sets AND the real
    payloads inside the recorded histories) decode through Temporal's own
    data converter into the M1 dataclasses, the new fields taking defaults.
  - A pre-M1 continue-as-new chain is rehydrated by M1 as a legacy loop
    (`deferred_resume_contract` False -> validator defers `legacy-loop`); an
    M1 chain carries True; a run produced by continue-as-new records no marker
    of its own.

Provenance. Only the DevLoop histories that pass the gates could show the
M1 marker, so "no marker" alone cannot catch a swapped or re-recorded
fixture of the other types. `MANIFEST.json` therefore records, per file, the
sha256 of its canonical JSON and the workflow id it ran under, plus
`recorded_from` (a99c888); every fixture is checked against it, and a file
missing from it or extra to it fails. Stack traces and worker identities
(pid@hostname) are blanked by the recorder's scrub; replay reads neither.

What these fixtures do NOT detect: MOVING the marker. A history with no
marker replays `patched()` as False wherever the call sits, so the pre-M1
set is blind to its position; the position is asserted on a fresh M1 history
instead (`test_an_m1_chain_marks_only_its_first_run_and_carries_the_contract`:
the next activity scheduled after the marker is the implement submit).

UNSUPPORTED: pre-M1 code reading state written by M1 code.
  - A pre-M1 worker cannot replay a history that carries the marker
    (TMPRL1100, measured outside this suite on the a99c888 worker; not
    reproducible here without that code, so it is documented, not tested).
  - A pre-M1 dataclass decodes an M1 payload WITHOUT ERROR and silently drops
    the M1 fields. The `test_unsupported_direction_*` tests pin that hazard:
    a rollback does not fail loudly on payloads, it forgets. The sharpest
    case: an M1 continued run carries no marker, so a pre-M1 worker CAN pick
    it up, and it decodes `deferred_resume_contract=True` away; if that run
    hops again, the chain is a legacy loop from then on, even after the
    roll-forward.

Reuse: the older top-level fixtures (`dev_loop_full.prepatch.json`,
`dev_loop_resumed.json`, `implement_sweep.json`, `action_approval_wait.json`,
...) also predate M1 and are already replayed against today's code by
`test_workflow_replay.py`, `test_execution_request_replay.py`,
`test_implement_sweep_replay.py` and `test_action_approval_wait.py`; their
DevLoop payloads are decoded here too. They cover six of the eight types and
were recorded by releases older than a99c888, so this file adds a set from
a99c888 itself covering all eight (IssuePoll and ScheduledDispatch had none).

The pre-M1 dataclasses are rebuilt from `tests/fixtures/pre_m1/
dataclass_fields.json` (field names extracted with `ast` from
`git show a99c888:<path>`), with the M1 field types for the shared fields.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import types
import typing
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest
from temporalio.api.common.v1 import Payload
from temporalio.client import WorkflowHistory, WorkflowUpdateFailedError
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    Interceptor,
    Replayer,
    StartActivityInput,
    StartChildWorkflowInput,
    StartLocalActivityInput,
    UnsandboxedWorkflowRunner,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from orchestrator.temporal.activities.pr_state import PRState
from orchestrator.temporal.constants import IMPLEMENTATION_TASK_QUEUE
from orchestrator.temporal.worker import worker_plans
from orchestrator.temporal.workflows import dev_loop
from orchestrator.temporal.workflows.action_approval import ActionApprovalWaitWorkflow
from orchestrator.temporal.workflows.dev_loop import (
    RESUME_DEFERRED_ERROR_TYPE,
    DevLoopWorkflow,
    IssueRef,
    MergeWatchResume,
    OpenDelivery,
    ResumeDelivery,
)
from orchestrator.temporal.workflows.implement_sweep import ImplementSweepWorkflow, SweptImplementWorkflow
from orchestrator.temporal.workflows.incidents import IncidentLoopWorkflow
from orchestrator.temporal.workflows.issue_poll import IssuePollWorkflow
from orchestrator.temporal.workflows.reconcile import ReconcileWorkflow
from orchestrator.temporal.workflows.scheduled_dispatch import ScheduledDispatchWorkflow
from orchestrator.work_context import execution_requests as xr
from tests.test_worker_roles import visibility  # noqa: F401  (fixture)

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PRE_M1_DIR = FIXTURES / "histories" / "pre_m1"
FIELDS_FIXTURE = FIXTURES / "pre_m1" / "dataclass_fields.json"
PRE_M1_SHA = "a99c8881403f2bbe64fea9864d252bed4fe58a4d"
#: Spelled out, not imported: a renamed marker must fail this file.
M1_MARKER = "deferred-resume-contract"

_CONVERTER = DataConverter.default.payload_converter

#: Workflow type name -> M1 class, for every type the worker registers.
_BY_NAME: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        DevLoopWorkflow,
        ReconcileWorkflow,
        IssuePollWorkflow,
        IncidentLoopWorkflow,
        ImplementSweepWorkflow,
        SweptImplementWorkflow,
        ScheduledDispatchWorkflow,
        ActionApprovalWaitWorkflow,
    )
}

#: The fields M1 added, per class. `test_the_pre_m1_field_sets_are_m1_minus_exactly_these`
#: proves this table against both the a99c888 extraction and today's classes.
M1_ADDED: dict[str, set[str]] = {
    "ResumeDelivery": {"intent_id", "expected_state_version", "resumed_from_execution_id"},
    "OpenDelivery": {"accepted_after_gates"},
    "MergeWatchResume": {"deferred_resume_contract"},
    "IssueRef": set(),
    "ExecutionRequest": {"created_at"},
}
#: What a pre-M1 payload must decode to, spelled out rather than read off the
#: class, so a changed default is a failure here and not a silent new meaning
#: for every in-flight record (False contract = legacy loop).
M1_DEFAULTS: dict[str, Any] = {
    "intent_id": None,
    "expected_state_version": 0,
    "resumed_from_execution_id": "",
    "accepted_after_gates": False,
    "deferred_resume_contract": False,
    "created_at": "",
}
_M1_CLASSES: dict[str, type] = {
    "ResumeDelivery": ResumeDelivery,
    "OpenDelivery": OpenDelivery,
    "MergeWatchResume": MergeWatchResume,
    "IssueRef": IssueRef,
    "ExecutionRequest": xr.ExecutionRequest,
}


# -- helpers -------------------------------------------------------------------


def _pre_m1_fields() -> dict[str, list[str]]:
    data = json.loads(FIELDS_FIXTURE.read_text(encoding="utf-8"))
    assert data["recorded_from"] == PRE_M1_SHA
    return {name: spec["fields"] for name, spec in data["classes"].items()}


def _subst(tp: Any, mapping: dict[type, type]) -> Any:
    """`tp` with every M1 class in `mapping` replaced by its pre-M1 rebuild,
    through unions and generic containers."""
    if isinstance(tp, type) and tp in mapping:
        return mapping[tp]
    args = typing.get_args(tp)
    if not args:
        return tp
    new = tuple(_subst(a, mapping) if a is not Ellipsis else a for a in args)
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        return typing.Union[new]  # noqa: UP007
    if isinstance(tp, types.GenericAlias):
        return types.GenericAlias(origin, new)
    return tp.copy_with(new)


def _rebuild(m1_cls: type, names: list[str], mapping: dict[type, type]) -> type:
    hints = typing.get_type_hints(m1_cls)
    by_name = {f.name: f for f in dataclasses.fields(m1_cls)}
    specs = []
    for name in names:
        f = by_name[name]
        kw: dict[str, Any] = {}
        if f.default is not dataclasses.MISSING:
            kw["default"] = f.default
        if f.default_factory is not dataclasses.MISSING:
            kw["default_factory"] = f.default_factory
        specs.append((name, _subst(hints[name], mapping), dataclasses.field(**kw)))
    return dataclasses.make_dataclass(
        f"PreM1{m1_cls.__name__}",
        specs,
        frozen=m1_cls.__dataclass_params__.frozen,  # type: ignore[attr-defined]
    )


def _pre_m1_classes() -> dict[str, type]:
    """The a99c888 dataclasses, rebuilt innermost first so the nesting
    (IssueRef -> MergeWatchResume -> OpenDelivery -> ResumeDelivery) is
    pre-M1 all the way down."""
    fields = _pre_m1_fields()
    mapping: dict[type, type] = {}
    out: dict[str, type] = {}
    for name in ("ResumeDelivery", "OpenDelivery", "MergeWatchResume", "IssueRef"):
        cls = _rebuild(_M1_CLASSES[name], fields[name], mapping)
        mapping[_M1_CLASSES[name]] = cls
        out[name] = cls
    return out


def _roundtrip(value: Any, to_type: type) -> Any:
    return _CONVERTER.from_payloads(_CONVERTER.to_payloads([value]), [to_type])[0]


def _payload_from_history(raw: dict) -> Payload:
    return Payload(
        metadata={k: base64.b64decode(v) for k, v in raw.get("metadata", {}).items()},
        data=base64.b64decode(raw.get("data", "")),
    )


def _events(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["events"]


def _patch_ids(events: list[dict]) -> list[str]:
    ids: list[str] = []
    for event in events:
        attrs = event.get("markerRecordedEventAttributes")
        if not attrs or attrs.get("markerName") != "core_patch":
            continue
        for p in attrs.get("details", {}).get("patch-data", {}).get("payloads", []):
            ids.append(json.loads(base64.b64decode(p["data"]))["id"])
    return ids


def _workflow_type(events: list[dict]) -> str:
    return events[0]["workflowExecutionStartedEventAttributes"]["workflowType"]["name"]


def _start_input(events: list[dict]) -> dict:
    return events[0]["workflowExecutionStartedEventAttributes"]["input"]["payloads"][0]


def _pre_m1_histories() -> list[Path]:
    return sorted(p for p in PRE_M1_DIR.glob("*.json") if p.name != "MANIFEST.json")


def _hop_run(path: Path) -> bool:
    return path.name.startswith("dev_loop_hop_run")


# -- the fixtures are what they claim to be -------------------------------------


def _manifest() -> dict[str, Any]:
    return json.loads((PRE_M1_DIR / "MANIFEST.json").read_text(encoding="utf-8"))


def _canonical_sha256(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_the_histories_were_recorded_from_the_pre_m1_release_line():
    assert _manifest()["recorded_from"] == PRE_M1_SHA


def test_the_manifest_lists_exactly_the_fixtures_on_disk():
    assert set(_manifest()["files"]) == {p.name for p in _pre_m1_histories()}


@pytest.mark.parametrize("path", _pre_m1_histories(), ids=lambda p: p.stem)
def test_every_fixture_matches_its_manifest_hash(path):
    """A swapped, edited or re-recorded fixture fails here, whatever its type;
    the marker check below can only catch that for a DevLoop that passes its
    gates."""
    assert _canonical_sha256(path) == _manifest()["files"][path.name]["sha256"]


@pytest.mark.parametrize("path", _pre_m1_histories(), ids=lambda p: p.stem)
def test_no_fixture_carries_a_worker_identity_or_stack_trace(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r'"identity": "[^"]', text)
    assert not re.search(r'"stackTrace": "[^"]', text)


@pytest.mark.parametrize("path", _pre_m1_histories(), ids=lambda p: p.stem)
def test_no_pre_m1_history_carries_the_m1_marker(path):
    """A fixture re-recorded from a post-M1 checkout fails HERE: for a
    DevLoop that passes its gates it would carry the marker."""
    assert M1_MARKER not in _patch_ids(_events(path))


def test_every_registered_workflow_type_has_a_pre_m1_history(visibility):  # noqa: F811
    """Derived from the worker's own plan, so a ninth type registered later
    without a pre-M1 fixture fails here."""
    registered = {w.__name__ for plan in worker_plans("all", visibility) for w in plan.workflows}
    recorded = {_workflow_type(_events(p)) for p in _pre_m1_histories()}
    assert registered == set(_BY_NAME), registered
    assert registered <= recorded, registered - recorded


def test_the_pre_m1_dev_loop_histories_pass_the_gates_and_cover_both_resume_paths():
    """Not vacuous: `dev_loop_full` runs past the point where M1 calls
    `patched()` (it schedules the implement, which M1 calls it just before),
    so it is a history in which a pre-M1 run passed the gates without the
    marker. That says nothing about where the call sits: see the module
    docstring. And the post-gates resume (the behaviour M1 changes) and the
    pre-gates resume are both present as accepted Updates."""
    full = _events(PRE_M1_DIR / "dev_loop_full.json")
    scheduled = [
        (a["activityType"]["name"], a["taskQueue"]["name"])
        for a in (
            e["activityTaskScheduledEventAttributes"] for e in full if "activityTaskScheduledEventAttributes" in e
        )
    ]
    assert ("submit_and_wait", IMPLEMENTATION_TASK_QUEUE) in scheduled  # the implement: past the marker point
    for name in ("dev_loop_post_gates_resume", "dev_loop_resumed"):
        events = _events(PRE_M1_DIR / f"{name}.json")
        assert any(e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED" for e in events), name
    post = _events(PRE_M1_DIR / "dev_loop_post_gates_resume.json")
    assert json.loads(_payload_from_history(_start_input(post)).data)["resume"] is not None


# -- 3. replay: every registered type, pre-M1 history, M1 code -------------------


@pytest.mark.parametrize("path", [p for p in _pre_m1_histories() if not _hop_run(p)], ids=lambda p: p.stem)
async def test_a_pre_m1_history_replays_under_m1_code(path):
    """A NondeterminismError here means M1 (or a later change) would wedge an
    execution that was in flight on the 1.117.0 line."""
    cls = _BY_NAME[_workflow_type(_events(path))]
    history = WorkflowHistory.from_json(f"pre-m1-{path.stem}", path.read_text(encoding="utf-8"))
    await Replayer(workflows=[cls]).replay_workflow(history)


@pytest.mark.parametrize("run", [1, 2, 3])
async def test_a_pre_m1_continue_as_new_chain_replays_under_m1_code(run, monkeypatch):
    """Recorded with `MERGE_WATCH_HISTORY_FLOOR` forced to 1 and an
    unsandboxed worker (the only way to force a hop in the test server), so
    replayed the same way: the hop decision reads the floor. With the default
    floor runs 1 and 2 fail on a99c888's OWN code too (measured), i.e. that
    would be a property of the recording, not of M1."""
    monkeypatch.setattr(dev_loop, "MERGE_WATCH_HISTORY_FLOOR", 1)
    path = PRE_M1_DIR / f"dev_loop_hop_run{run}.json"
    history = WorkflowHistory.from_json(f"pre-m1-hop-{run}", path.read_text(encoding="utf-8"))
    await Replayer(workflows=[DevLoopWorkflow], workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(history)


# -- 3b. replay with arguments: what Replayer does not compare -------------------

#: Fields M1 may legitimately add to a scheduled activity or child-workflow
#: input, stripped from the replayed side only when the recorded side lacks
#: them. Empty: M1 adds no field to any argument these histories schedule
#: (measured: every fixture compares equal with nothing stripped). A future
#: entry must name the field and why it is benign.
ALLOWED_NEW_ARG_FIELDS: frozenset[str] = frozenset()


def _payload_json(raw: dict) -> Any:
    data = base64.b64decode(raw.get("data", ""))
    return json.loads(data) if data else None


def _recorded_inputs(events: list[dict]) -> list[tuple[str, str, list[Any]]]:
    out = []
    for e in events:
        if a := e.get("activityTaskScheduledEventAttributes"):
            out.append(("activity", a["activityType"]["name"], [_payload_json(p) for p in _args(a)]))
        if c := e.get("startChildWorkflowExecutionInitiatedEventAttributes"):
            out.append(("child", c["workflowType"]["name"], [_payload_json(p) for p in _args(c)]))
    return out


def _args(attrs: dict) -> list[dict]:
    return attrs.get("input", {}).get("payloads", [])


def _as_json(args: typing.Sequence[Any]) -> list[Any]:
    return [json.loads(p.data) if p.data else None for p in _CONVERTER.to_payloads(list(args))]


def _strip_allowed(replayed: Any, recorded: Any) -> Any:
    if isinstance(replayed, dict) and isinstance(recorded, dict):
        return {
            k: _strip_allowed(v, recorded.get(k))
            for k, v in replayed.items()
            if not (k in ALLOWED_NEW_ARG_FIELDS and k not in recorded)
        }
    if isinstance(replayed, list) and isinstance(recorded, list) and len(replayed) == len(recorded):
        return [_strip_allowed(a, b) for a, b in zip(replayed, recorded, strict=True)]
    return replayed


class _CaptureOutbound(WorkflowOutboundInterceptor):
    captured: typing.ClassVar[list[tuple[str, str, list[Any]]]] = []

    def start_activity(self, input: StartActivityInput) -> Any:
        _CaptureOutbound.captured.append(("activity", input.activity, _as_json(input.args)))
        return super().start_activity(input)

    def start_local_activity(self, input: StartLocalActivityInput) -> Any:
        _CaptureOutbound.captured.append(("local-activity", input.activity, _as_json(input.args)))
        return super().start_local_activity(input)

    async def start_child_workflow(self, input: StartChildWorkflowInput) -> Any:
        _CaptureOutbound.captured.append(("child", input.workflow, _as_json(input.args)))
        return await super().start_child_workflow(input)


class _CaptureInbound(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_CaptureOutbound(outbound))


class _Capture(Interceptor):
    def workflow_interceptor_class(self, input: WorkflowInterceptorClassInput) -> type[WorkflowInboundInterceptor]:
        return _CaptureInbound


@pytest.mark.parametrize("path", _pre_m1_histories(), ids=lambda p: p.stem)
async def test_a_pre_m1_history_replays_under_m1_code_with_the_same_arguments(path, monkeypatch):
    """Every activity and child workflow the M1 code schedules while replaying
    a pre-M1 history must carry the input that history recorded, in order.
    Run under the workflow id the history ran under (MANIFEST.json): activity
    inputs embed it. Where the manifest has none, no recorded input contains
    an id, and the placeholder below replays identically."""
    monkeypatch.setattr(dev_loop, "MERGE_WATCH_HISTORY_FLOOR", 1)  # only the hop runs read it
    events = _events(path)
    workflow_id = _manifest()["files"][path.name]["workflow_id"] or "pre-m1-no-id-in-any-input"
    _CaptureOutbound.captured = []
    await Replayer(
        workflows=[_BY_NAME[_workflow_type(events)]],
        interceptors=[_Capture()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ).replay_workflow(WorkflowHistory.from_json(workflow_id, path.read_text(encoding="utf-8")))
    recorded = _recorded_inputs(events)
    replayed = _CaptureOutbound.captured
    assert recorded, "a history that schedules nothing proves nothing here"
    assert [r[:2] for r in replayed] == [r[:2] for r in recorded]
    for i, (got, want) in enumerate(zip(replayed, recorded, strict=True)):
        args = _strip_allowed(got[2], want[2])
        assert args == want[2], f"command {i} ({want[1]}): replayed input differs from the recorded one"


# -- 1. decoding ---------------------------------------------------------------


def test_the_pre_m1_field_sets_are_m1_minus_exactly_these():
    fields = _pre_m1_fields()
    for name, m1_cls in _M1_CLASSES.items():
        m1 = {f.name for f in dataclasses.fields(m1_cls)}
        assert set(fields[name]) == m1 - M1_ADDED[name], name
        assert M1_ADDED[name] <= m1, name


@pytest.mark.parametrize("name", sorted(_M1_CLASSES))
def test_every_field_m1_added_has_a_default(name):
    """A new field without a default makes every pre-M1 payload undecodable."""
    for f in dataclasses.fields(_M1_CLASSES[name]):
        if f.name in M1_ADDED[name]:
            assert f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING, f.name
            assert f.default == M1_DEFAULTS[f.name], f.name


def _pre_m1_values(pre: dict[str, type]) -> dict[str, Any]:
    delivery = pre["ResumeDelivery"](
        execution_request_id="xr_1", work_item_id="wi-1", surface="api", actor_kind="human", actor_id="user:alice"
    )
    opened = pre["OpenDelivery"](delivery=delivery, surface_transition=True, pending_phase="Running")
    resume = pre["MergeWatchResume"](
        service="mctl-telegram",
        slug="issue-1-x",
        deadline="2099-01-01T00:00:00Z",
        work_item_id="wi-1",
        hops=2,
        accepted_request_ids=("xr_1",),
        open_deliveries=(opened,),
    )
    ref = pre["IssueRef"](
        issue_url="https://github.com/mctlhq/mctl-telegram/issues/1", work_item_id="wi-1", resume=resume
    )
    return {"ResumeDelivery": delivery, "OpenDelivery": opened, "MergeWatchResume": resume, "IssueRef": ref}


@pytest.mark.parametrize("name", ["ResumeDelivery", "OpenDelivery", "MergeWatchResume", "IssueRef"])
def test_a_pre_m1_payload_decodes_into_m1_with_the_new_fields_defaulted(name):
    pre = _pre_m1_classes()
    value = _pre_m1_values(pre)[name]
    raw = json.loads(_CONVERTER.to_payloads([value])[0].data)
    for added in M1_ADDED[name]:
        assert added not in raw  # the payload really is pre-M1-shaped
    decoded = _roundtrip(value, _M1_CLASSES[name])
    assert isinstance(decoded, _M1_CLASSES[name])
    # Every pre-M1 field survives (compared on the wire, so a nested pre-M1
    # value and its M1 decode compare equal); every M1 field is at its default.
    back = json.loads(_CONVERTER.to_payloads([decoded])[0].data)
    assert _without_m1_keys(back) == raw
    for added in M1_ADDED[name]:
        assert getattr(decoded, added) == M1_DEFAULTS[added], added
    if name == "IssueRef":
        assert decoded.resume.deferred_resume_contract is False
        assert decoded.resume.open_deliveries[0].accepted_after_gates is False
        assert decoded.resume.open_deliveries[0].delivery.intent_id is None


def _without_m1_keys(obj: Any) -> Any:
    added = set().union(*M1_ADDED.values())
    if isinstance(obj, dict):
        return {k: _without_m1_keys(v) for k, v in obj.items() if k not in added}
    if isinstance(obj, list):
        return [_without_m1_keys(v) for v in obj]
    return obj


def test_a_pre_m1_execution_request_row_parses_with_created_at_empty():
    """`ExecutionRequest` never crosses a Temporal payload (it is parsed from
    mctl-api JSON by `from_payload`), so its pre-M1 shape is a row without
    `created_at`."""
    row = {
        "id": "xr_1",
        "work_item_id": "wi-1",
        "kind": "resume",
        "state": "claimed",
        "expected_state_version": 3,
        "requested_by": "user:alice",
        "surface": "api",
    }
    parsed = xr.ExecutionRequest.from_payload(row)
    assert parsed is not None and parsed.request_id == "xr_1" and parsed.created_at == M1_DEFAULTS["created_at"]
    with_created = xr.ExecutionRequest.from_payload(row | {"created_at": "2026-11-01T00:00:00Z"})
    assert with_created is not None and with_created.created_at == "2026-11-01T00:00:00Z"


def test_a_recorded_delivery_without_kind_exists_and_decodes_as_a_resume():
    """Not vacuous: at least one committed pre-M1 history carries a delivery
    recorded before `kind` existed (`histories/dev_loop_resumed.json`)."""
    events = _events(FIXTURES / "histories" / "dev_loop_resumed.json")
    (accepted,) = [
        e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]["args"]["payloads"][0]
        for e in events
        if e["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED"
    ]
    payload = _payload_from_history(accepted)
    assert "kind" not in json.loads(payload.data)
    delivery = _CONVERTER.from_payloads([payload], [ResumeDelivery])[0]
    assert delivery.kind == "resume"
    assert (delivery.intent_id, delivery.expected_state_version, delivery.resumed_from_execution_id) == (None, 0, "")


def _recorded_dev_loop_histories() -> list[Path]:
    """Every committed DevLoop history recorded before M1: the pre_m1 set and
    the older standalone fixtures (dev_loop_deferred_resume.json is M1's own)."""
    older = [p for p in (FIXTURES / "histories").glob("dev_loop_*.json") if p.name != "dev_loop_deferred_resume.json"]
    pre = [p for p in _pre_m1_histories() if p.name.startswith("dev_loop_")]
    return sorted(older + pre)


@pytest.mark.parametrize("path", _recorded_dev_loop_histories(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_every_recorded_pre_m1_dev_loop_payload_decodes_into_m1(path):
    """The real payloads, not hand-built ones: the start input (IssueRef,
    with a MergeWatchResume on a continued run) and every accepted
    `accept_execution_request` argument (ResumeDelivery)."""
    events = _events(path)
    assert M1_MARKER not in _patch_ids(events)
    raw_start = _payload_from_history(_start_input(events))
    ref = _CONVERTER.from_payloads([raw_start], [IssueRef])[0]
    if ref.resume is not None:
        assert "deferred_resume_contract" not in json.loads(raw_start.data)["resume"]
        assert ref.resume.deferred_resume_contract is False
        assert all(o.accepted_after_gates is False for o in ref.resume.open_deliveries)
    for e in events:
        if e["eventType"] != "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED":
            continue
        req = e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]
        (arg,) = req["args"]["payloads"]
        payload = _payload_from_history(arg)
        raw = json.loads(payload.data)
        assert not {"intent_id", "expected_state_version", "resumed_from_execution_id"} & set(raw)
        delivery = _CONVERTER.from_payloads([payload], [ResumeDelivery])[0]
        assert delivery.execution_request_id.startswith("xr_")
        # A payload from before #461 option A has no `kind`: it was a resume.
        assert delivery.kind == raw.get("kind", "resume")
        if "kind" not in raw:
            assert delivery.kind == "resume"
        assert (delivery.intent_id, delivery.expected_state_version, delivery.resumed_from_execution_id) == (
            None,
            0,
            "",
        )


# -- 1b. the unsupported direction: a pre-M1 decoder silently drops M1 fields ---


def _m1_values() -> dict[str, Any]:
    delivery = ResumeDelivery(
        execution_request_id="xr_2",
        work_item_id="wi-1",
        surface="api",
        actor_kind="human",
        actor_id="user:alice",
        intent_id=7,
        expected_state_version=3,
        resumed_from_execution_id="we_9",
    )
    opened = OpenDelivery(delivery=delivery, accepted_after_gates=True)
    resume = MergeWatchResume(
        service="mctl-telegram",
        slug="issue-1-x",
        deadline="2099-01-01T00:00:00Z",
        work_item_id="wi-1",
        deferred_resume_contract=True,
        open_deliveries=(opened,),
    )
    ref = IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/1", work_item_id="wi-1", resume=resume)
    return {"ResumeDelivery": delivery, "OpenDelivery": opened, "MergeWatchResume": resume, "IssueRef": ref}


@pytest.mark.parametrize("name", ["ResumeDelivery", "OpenDelivery", "MergeWatchResume"])
def test_unsupported_direction_pre_m1_decoder_silently_drops_m1_fields(name):
    """HAZARD, pinned on purpose: Temporal's converter ignores unknown keys,
    so a pre-M1 worker decodes an M1 payload WITHOUT any error and the M1
    fields are simply gone. If this test ever fails because decoding raised,
    the rollback story changed: update the B4 rollback notes."""
    pre = _pre_m1_classes()
    value = _m1_values()[name]
    raw = json.loads(_CONVERTER.to_payloads([value])[0].data)
    assert M1_ADDED[name] <= set(raw)  # the M1 fields really are on the wire
    decoded = _roundtrip(value, pre[name])  # no exception: that is the hazard
    for added in M1_ADDED[name]:
        assert not hasattr(decoded, added), added
    # And a pre-M1 writer re-encodes it without them: back on M1 they are defaults.
    again = _roundtrip(decoded, _M1_CLASSES[name])
    for added in M1_ADDED[name]:
        assert getattr(again, added) != getattr(value, added), added


def test_unsupported_direction_a_continued_m1_run_downgrades_to_a_legacy_loop_through_a_pre_m1_hop():
    """The concrete consequence: an M1 continued run carries no marker (see
    the chain test below), so a pre-M1 worker can take it; its decode drops
    `deferred_resume_contract`, and the record it hops with says False. After
    the roll-forward that chain is a `legacy-loop` for the rest of its life."""
    pre = _pre_m1_classes()
    m1_ref = _m1_values()["IssueRef"]
    on_pre_m1 = _roundtrip(m1_ref, pre["IssueRef"])
    assert not hasattr(on_pre_m1.resume, "deferred_resume_contract")
    back = _roundtrip(on_pre_m1, IssueRef)
    assert m1_ref.resume.deferred_resume_contract is True
    assert back.resume.deferred_resume_contract is False
    assert back.resume.open_deliveries[0].accepted_after_gates is False
    assert back.resume.open_deliveries[0].delivery.intent_id is None


def test_unsupported_direction_a_pre_m1_execution_request_has_no_created_at():
    """A pre-M1 dispatcher's `ExecutionRequest` has no `created_at`: after a
    rollback the deferred-age WARN (M1) is simply absent, never wrong."""
    assert "created_at" not in _pre_m1_fields()["ExecutionRequest"]


# -- 2. continue-as-new ----------------------------------------------------------


def _hop_input(path: Path) -> tuple[dict, IssueRef]:
    raw = _payload_from_history(_start_input(_events(path)))
    return json.loads(raw.data), _CONVERTER.from_payloads([raw], [IssueRef])[0]


@pytest.mark.parametrize("run", [2, 3])
def test_a_pre_m1_continued_run_decodes_as_a_legacy_loop(run):
    raw, ref = _hop_input(PRE_M1_DIR / f"dev_loop_hop_run{run}.json")
    assert raw["resume"] is not None and "deferred_resume_contract" not in raw["resume"]
    assert ref.resume is not None and ref.resume.deferred_resume_contract is False
    assert ref.resume.hops == run - 1


_OPEN_PR = PRState(
    found=True,
    pr_url="https://github.com/mctlhq/mctl-telegram/pull/77",
    repo="mctlhq/mctl-telegram",
    number=77,
    state="OPEN",
    head_sha="deadbeef",
)


async def _validator_answer(env: WorkflowEnvironment, ref: IssueRef) -> tuple[str, str]:
    """Start an M1 run from a continue-as-new input and ask its validator
    about a post-gates resume (with an intent, so only the contract decides
    between `legacy-loop` and the M1 reasons)."""
    from tests.temporal_harness import Worker
    from tests.test_dev_loop_workflow import TASK_QUEUE, _fake_activities

    # The recorded deadline is the recording's clock + the watch window: move
    # it out of reach so the run is still watching when the Update lands. It
    # has no bearing on the contract.
    assert ref.resume is not None
    ref = dataclasses.replace(ref, resume=dataclasses.replace(ref.resume, deadline="2099-01-01T00:00:00Z"))
    activities, *_ = _fake_activities(released=True, pr_states=[_OPEN_PR] * 200)
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run, ref, id=f"p5-rehydrate-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        await handle.query(DevLoopWorkflow.work_context)  # the run has its state
        delivery = ResumeDelivery(
            execution_request_id=f"xr_{uuid.uuid4().hex[:8]}",
            work_item_id=ref.resume.work_item_id,
            surface="api",
            actor_kind="human",
            actor_id="user:alice",
            intent_id=7,
        )
        with pytest.raises(WorkflowUpdateFailedError) as ei:
            await handle.execute_update(DevLoopWorkflow.accept_execution_request, delivery)
        await handle.terminate("p5 over")
    cause = ei.value.cause
    assert isinstance(cause, ApplicationError)
    return cause.type or "", str(cause.details[0])


async def test_m1_rehydrates_a_pre_m1_chain_as_a_legacy_loop():
    """The record a pre-M1 run hopped with, from the recorded history, run by
    M1: `_resume_merge_watch` restores `deferred_resume_contract=False`, and
    the validator defers a post-gates resume `legacy-loop`."""
    _, ref = _hop_input(PRE_M1_DIR / "dev_loop_hop_run2.json")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        answer = await _validator_answer(env, ref)
    assert answer == (RESUME_DEFERRED_ERROR_TYPE, "legacy-loop")


async def test_an_m1_chain_marks_only_its_first_run_and_carries_the_contract(monkeypatch):
    """An M1 loop forced to hop: the run that passes the gates records the
    marker exactly once; every run produced by continue-as-new records NONE
    of its own and instead carries `deferred_resume_contract=True` in its
    input, which M1 rehydrates (the validator then answers an M1 reason, not
    `legacy-loop`). Each run replays under M1."""
    from tests.temporal_harness import Worker
    from tests.test_dev_loop_workflow import MERGED_PR, TASK_QUEUE, _fake_activities

    monkeypatch.setattr(dev_loop, "MERGE_WATCH_HISTORY_FLOOR", 1)
    activities, _calls, investigate_ran, _ops = _fake_activities(
        released=True, pr_states=[_OPEN_PR, _OPEN_PR, MERGED_PR]
    )
    wid = f"p5-m1-hop-{uuid.uuid4()}"
    histories: list[dict] = []
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[DevLoopWorkflow],
            activities=activities,
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await env.client.start_workflow(
                DevLoopWorkflow.run,
                IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/964", work_item_id="wi-hop-1"),
                id=wid,
                task_queue=TASK_QUEUE,
            )
            with anyio.fail_after(30):
                await investigate_ran.wait()
            await handle.signal(DevLoopWorkflow.approve, {"approver": "alice", "approval_ref": "aar_test"})
            with anyio.fail_after(30):
                result = await handle.result()
            run_id = handle.first_execution_run_id or handle.run_id
            while run_id:
                h = (await env.client.get_workflow_handle(wid, run_id=run_id).fetch_history()).to_json_dict()
                histories.append(h)
                run_id = (
                    h["events"][-1].get("workflowExecutionContinuedAsNewEventAttributes", {}).get("newExecutionRunId")
                )
        assert result.pr is not None and result.pr.state == "MERGED"
        assert len(histories) >= 2, "the floor forced no hop"

        first, *continued = histories
        assert _patch_ids(first["events"]).count(M1_MARKER) == 1
        # Position: the next activity scheduled after the marker is the
        # implement submit (the pre-M1 fixtures cannot see this; see above).
        evs = first["events"]
        marker_at = next(i for i, e in enumerate(evs) if M1_MARKER in _patch_ids([e]))
        after = next(
            e["activityTaskScheduledEventAttributes"]
            for e in evs[marker_at:]
            if "activityTaskScheduledEventAttributes" in e
        )
        assert (after["activityType"]["name"], after["taskQueue"]["name"]) == (
            "submit_and_wait",
            IMPLEMENTATION_TASK_QUEUE,
        )
        for h in continued:
            assert M1_MARKER not in _patch_ids(h["events"])
            ref = _CONVERTER.from_payloads([_payload_from_history(_start_input(h["events"]))], [IssueRef])[0]
            assert ref.resume is not None and ref.resume.deferred_resume_contract is True
        for i, h in enumerate(histories):
            await Replayer(workflows=[DevLoopWorkflow], workflow_runner=UnsandboxedWorkflowRunner()).replay_workflow(
                WorkflowHistory.from_json(f"p5-m1-hop-{i}", json.dumps(h))
            )

    monkeypatch.undo()
    m1_ref = _CONVERTER.from_payloads([_payload_from_history(_start_input(continued[0]["events"]))], [IssueRef])[0]
    async with await WorkflowEnvironment.start_time_skipping() as env:
        answer = await _validator_answer(env, m1_ref)
    assert answer[1] != "legacy-loop"
    assert answer == (RESUME_DEFERRED_ERROR_TYPE, "feedback-not-ready")
