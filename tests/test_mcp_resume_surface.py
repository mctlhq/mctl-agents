"""mctlhq/mctl-agents#904 (with #573): `mcp` is a surface, and nothing more.

mctl-api labels every request made through its MCP tool `surface=mcp` and
names the authenticated principal as `requested_by`. Until #904 the DevLoop
refused such a resume (`surface-or-actor-unrecognised`). These tests pin the
vocabulary change, that the label carries no client/model identity, that a
same-principal MCP -> MCP continuation is not a transition, that unknown
provenance is never sealed as "no transition", and that an image without the
new vocabulary fails closed.

The end-to-end Telegram -> MCP seal and its mutation counterpart live in
tests/test_execution_request_dispatch.py (T5/T6), beside the harness they use.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from temporalio.exceptions import ApplicationError

from orchestrator import context_snapshot as cs
from orchestrator import run_issue_investigator
from orchestrator.temporal.workflows import dev_loop
from orchestrator.temporal.workflows.dev_loop import (
    DELIVERY_ACCEPTED,
    RESUME_REFUSED_ERROR_TYPE,
    DevLoopWorkflow,
    IssueRef,
    ResumeDelivery,
)
from orchestrator.work_context import contract as wc
from orchestrator.work_context.contract import ActorRef, SurfaceRef
from tests.test_run_issue_investigator import _args

URL = "https://github.com/mctlhq/mctl-agents/issues/904"
WID = "wi_00000000-0000-4000-8000-000000000904"
LEGACY_SURFACES = ("github", "telegram", "web", "cli", "api")


def _delivery(rid: str = "xr_1", *, surface: str = "mcp", actor_kind: str = "human", actor_id: str = "github:a"):
    return ResumeDelivery(
        execution_request_id=rid, work_item_id=WID, surface=surface, actor_kind=actor_kind, actor_id=actor_id
    )


def _loop(**state) -> DevLoopWorkflow:
    wf = DevLoopWorkflow(IssueRef(issue_url=URL))
    wf._initialized = True
    for name, value in state.items():
        setattr(wf, name, value)
    return wf


# -- T1/T2/T3: vocabulary --------------------------------------------------------


def test_both_vocabularies_carry_mcp_and_stay_identical():
    assert "mcp" in wc.SURFACE_KINDS
    assert cs.WORK_CONTEXT_SURFACE_KINDS == wc.SURFACE_KINDS


def test_the_loop_accepts_an_mcp_resume():
    _loop()._validate_execution_request(_delivery())
    assert DevLoopWorkflow._resume_provenance("mcp", "human", "github:a")[2] == ""


@pytest.mark.parametrize("surface", LEGACY_SURFACES)
def test_every_legacy_surface_is_still_accepted(surface):
    _loop()._validate_execution_request(_delivery(surface=surface))


@pytest.mark.parametrize(
    ("surface", "actor_kind"),
    [("carrier-pigeon", "human"), ("mcp", "alien"), ("mcp/claude", "human")],
    ids=["unknown-surface", "foreign-actor-kind", "client-qualified-surface"],
)
def test_out_of_vocabulary_provenance_is_still_refused(surface, actor_kind):
    with pytest.raises(ApplicationError) as exc:
        _loop()._validate_execution_request(_delivery(surface=surface, actor_kind=actor_kind))
    assert exc.value.type == RESUME_REFUSED_ERROR_TYPE
    assert exc.value.details == ("surface-or-actor-unrecognised",)


def test_the_investigator_cli_accepts_surface_mcp():
    run_issue_investigator._work_context_from_args(_args(surface="mcp", actor_kind="human", actor_id="github:a"))


# -- T4: no identity invented, same principal is not a transition ---------------


def test_provenance_records_no_client_session_or_model_identity():
    """The only identity is the authenticated principal (`actor_id`); a model
    or client name has no field to land in, so it can neither re-arm
    approval nor pose as a second reviewer."""
    forbidden = {"client", "client_id", "client_name", "model", "model_name", "session", "session_id"}
    assert [f.name for f in dataclasses.fields(ActorRef)] == ["kind", "actor_id"]
    for cls in (SurfaceRef, ResumeDelivery):
        assert not {f.name for f in dataclasses.fields(cls)} & forbidden, cls
    # And what a resume adopts is the label alone.
    surface, actor, _ = DevLoopWorkflow._resume_provenance("mcp", "human", "github:a")
    assert surface == SurfaceRef(kind="mcp") and actor == ActorRef(kind="human", actor_id="github:a")


def test_a_same_principal_mcp_to_mcp_resume_keeps_the_approval():
    """Claude then Codex, both through MCP as the same principal: the loop
    sees the same surface and the same actor, so this is not a transition and
    the standing approval is neither cleared nor re-granted. (The approval
    stays bound to the proposal's content digest, which the approve flip
    re-checks; a changed proposal is refused there whichever client asked.)"""
    same = ActorRef(kind="human", actor_id="github:a")
    wf = _loop(
        _current_surface=SurfaceRef(kind="mcp"),
        _current_actor=same,
        _approved=True,
        _approval_ref="aar_1",
        _approval_attempt=1,
    )
    assert wf._take_resume_provenance(SurfaceRef(kind="mcp"), same) is False
    assert (wf._approved, wf._approval_ref, wf._approval_attempt) == (True, "aar_1", 1)


def test_another_principal_on_mcp_is_a_transition_and_clears_the_approval():
    wf = _loop(
        _current_surface=SurfaceRef(kind="mcp"),
        _current_actor=ActorRef(kind="human", actor_id="github:a"),
        _approved=True,
        _approval_ref="aar_1",
        _approval_attempt=1,
    )
    assert wf._take_resume_provenance(SurfaceRef(kind="mcp"), ActorRef(kind="human", actor_id="github:b")) is True
    assert (wf._approved, wf._approval_ref, wf._approval_attempt) == (False, "", 2)


def test_a_telegram_to_mcp_resume_is_a_transition():
    actor = ActorRef(kind="human", actor_id="github:a")
    wf = _loop(_current_surface=SurfaceRef(kind="telegram"), _current_actor=actor, _approved=True)
    assert wf._take_resume_provenance(SurfaceRef(kind="mcp"), actor) is True
    assert wf._approved is False


# -- the run's own start request: forwarded, not adopted as the baseline -------


def test_the_own_start_request_records_its_provenance_for_forwarding_only():
    wf = DevLoopWorkflow(IssueRef(issue_url=URL, work_item_id=WID, execution_request_id="xr_1"))
    assert wf.accept_execution_request(_delivery("xr_1")) == DELIVERY_ACCEPTED
    assert (wf._start_surface, wf._start_actor) == (SurfaceRef(kind="mcp"), ActorRef(kind="human", actor_id="github:a"))
    # The approval baseline is untouched: adopting it would change when a
    # later resume re-arms approval.
    assert (wf._current_surface, wf._current_actor) == (SurfaceRef(), ActorRef())


@pytest.mark.parametrize(
    "delivery",
    [_delivery("xr_1", surface="carrier-pigeon"), _delivery("xr_1", surface=""), _delivery("xr_1", actor_kind="")],
    ids=["unknown-surface", "no-surface", "no-actor"],
)
def test_an_own_start_request_with_bad_provenance_forwards_nothing(delivery):
    wf = DevLoopWorkflow(IssueRef(issue_url=URL, work_item_id=WID, execution_request_id="xr_1"))
    assert wf.accept_execution_request(delivery) == DELIVERY_ACCEPTED
    assert (wf._start_surface, wf._start_actor) == (SurfaceRef(), ActorRef())


# -- T7: unknown is never false --------------------------------------------------


def _ref(**fields) -> cs.WorkContextRef:
    return cs.WorkContextRef(
        work_item_id=WID, work_item_revision="r1", execution_id="we_1", execution_sequence=1, **fields
    )


def test_an_unknown_transition_is_omitted_from_the_document():
    data = _ref().to_dict()
    assert "surface_transition" not in data
    assert cs.WorkContextRef.from_dict(data).surface_transition is None


@pytest.mark.parametrize("value", [True, False])
def test_a_measured_transition_round_trips_byte_identical(value):
    """Every snapshot sealed before #904 carries the key; reading and
    re-serialising it must not change a byte (or its snapshot_id)."""
    data = _ref(surface_transition=value).to_dict()
    assert data["surface_transition"] is value
    assert list(data).index("surface_transition") == list(data).index("actor_id") + 1
    again = cs.WorkContextRef.from_dict(json.loads(json.dumps(data))).to_dict()
    assert json.dumps(again, sort_keys=False) == json.dumps(data, sort_keys=False)


def test_an_explicit_null_transition_is_refused():
    data = _ref().to_dict()
    data["surface_transition"] = None
    with pytest.raises(cs.ContextSnapshotError):
        cs.WorkContextRef.from_dict(data)


def test_the_investigator_never_seals_an_unmeasured_side_as_no_transition():
    from orchestrator.work_context.contract import CanonicalState, ExecutionRef, WorkItem

    canonical = CanonicalState(work_item_id=WID, state="in-progress")
    item = WorkItem(
        work_item_id=WID,
        revision="r1",
        origin=SurfaceRef(kind="telegram"),
        executions=(
            ExecutionRef(
                execution_id="e1",
                sequence=1,
                surface=SurfaceRef(kind="mcp"),
                actor=ActorRef(kind="human", actor_id="github:a"),
            ),
        ),
    )

    def ref(**provenance):
        return run_issue_investigator._work_context_ref(
            canonical=canonical,
            item=item,
            execution_id="e2",
            execution_sequence=2,
            resume_from_execution_id=None,
            **provenance,
        ).surface_transition

    # Nothing forwarded (#573's C2): unknown, not False.
    assert ref(surface=None, actor_kind=None, actor_id=None) is None
    # Surface known and unchanged, actor never declared: still unknown.
    assert ref(surface="mcp", actor_kind=None, actor_id=None) is None
    # Both axes measured and unchanged (same principal, mcp -> mcp): False.
    assert ref(surface="mcp", actor_kind="human", actor_id="github:a") is False
    # Any measured change is a transition, even with the other axis unknown.
    assert ref(surface="telegram", actor_kind=None, actor_id=None) is True
    assert ref(surface=None, actor_kind="human", actor_id="github:b") is True


# -- T12: an image without `mcp` fails closed -----------------------------------


def test_an_old_vocabulary_loop_refuses_mcp(monkeypatch):
    monkeypatch.setattr(dev_loop, "SURFACE_KINDS", frozenset(LEGACY_SURFACES))
    with pytest.raises(ApplicationError) as exc:
        _loop()._validate_execution_request(_delivery())
    assert exc.value.details == ("surface-or-actor-unrecognised",)


def test_an_old_vocabulary_investigator_exits_on_surface_mcp(monkeypatch):
    monkeypatch.setattr(wc, "SURFACE_KINDS", frozenset(LEGACY_SURFACES))
    with pytest.raises(SystemExit):
        run_issue_investigator._work_context_from_args(_args(surface="mcp", actor_kind="human"))
