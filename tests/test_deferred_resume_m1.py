"""DevLoop M1 acceptance (mctlhq/mctl-agents#996): the tests the proposal's
T1, T4, T6 and T7 ask for, beyond the validator and `_deliver` cases that
`test_execution_request_resume.py` already carries.

- T1: a payload recorded before M1 is decoded through Temporal's own payload
  converter from raw JSON, not built from the dataclass.
- T4: a refused or deferred resume mints no `we_` (zero fulfils), keeps its
  request, and a re-dispatch after the state changed is delivered exactly once.
- T6: the real reject rules (the fake models them: `state=claimed`, the
  claimant's token, an unexpired lease), so a deferred request cannot be
  drained by id.
- T7: the age WARN reads only the row and survives a dispatcher restart.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.client import WorkflowUpdateFailedError
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from orchestrator.temporal import dispatcher as dx
from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.temporal.workflows.dev_loop import (
    RESUME_DEFERRAL_REASONS,
    RESUME_DEFERRED_ERROR_TYPE,
    MergeWatchResume,
    OpenDelivery,
    ResumeDelivery,
)
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context.client import WorkItemClient
from tests.test_execution_request_dispatch import FakeTemporal, _audit, api  # noqa: F401  (api: fixture)
from tests.test_execution_request_resume import _RunningPort, _UpdateClient
from tests.test_work_context_resume_acceptance import URL, WID

pytestmark = pytest.mark.anyio

_CONVERTER = DataConverter.default.payload_converter


def _decode(type_: type, obj: object):
    payload = Payload(metadata={"encoding": b"json/plain"}, data=json.dumps(obj).encode())
    return _CONVERTER.from_payloads([payload], [type_])[0]


# -- T1: raw pre-M1 JSON decodes through Temporal's converter ------------------

_PRE_M1_DELIVERY = {
    "execution_request_id": "xr_1",
    "work_item_id": WID,
    "surface": "telegram",
    "actor_kind": "human",
    "actor_id": "user:alice",
    "kind": "resume",
}


def test_a_pre_m1_resume_delivery_payload_decodes_with_the_identity_defaults():
    decoded = _decode(ResumeDelivery, _PRE_M1_DELIVERY)
    assert decoded.execution_request_id == "xr_1" and decoded.actor_id == "user:alice"
    assert (decoded.intent_id, decoded.expected_state_version, decoded.resumed_from_execution_id) == (None, 0, "")


def test_a_pre_m1_merge_watch_record_decodes_as_a_legacy_loop_with_its_open_delivery():
    """The record a pre-M1 run hopped with: no `deferred_resume_contract`, an
    `OpenDelivery` without `accepted_after_gates`. It must come back a legacy
    loop (False), the delivery it carries intact and not accepted-after-gates."""
    old = {
        "service": "mctl-telegram",
        "slug": "issue-1-x",
        "deadline": "2099-01-01T00:00:00Z",
        "work_item_id": WID,
        "accepted_request_ids": ["xr_1"],
        "open_deliveries": [{"delivery": _PRE_M1_DELIVERY, "surface_transition": True}],
    }
    decoded = _decode(MergeWatchResume, old)
    assert decoded.deferred_resume_contract is False
    (opened,) = decoded.open_deliveries
    assert opened.accepted_after_gates is False and opened.surface_transition is True
    assert opened.delivery.execution_request_id == "xr_1" and opened.delivery.intent_id is None


def test_the_identity_fields_survive_an_encode_decode_round_trip():
    sent = ResumeDelivery(
        execution_request_id="xr_2",
        work_item_id=WID,
        surface="telegram",
        actor_kind="human",
        actor_id="user:alice",
        intent_id=7,
        expected_state_version=3,
        resumed_from_execution_id="we_9",
    )
    carried = MergeWatchResume(
        deferred_resume_contract=True, open_deliveries=(OpenDelivery(delivery=sent, accepted_after_gates=True),)
    )
    back = _CONVERTER.from_payloads(_CONVERTER.to_payloads([carried]), [MergeWatchResume])[0]
    assert back == carried


# -- T4: refused and deferred resumes mint no execution -------------------------


@pytest.mark.parametrize("reason", ["work-merged", "nothing-to-resume"])
async def test_a_refused_post_gates_resume_is_rejected_and_mints_no_execution(api, reason):  # noqa: F811
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=dx.DeliveryAnswer(dx.DELIVERY_REFUSED, reason))
    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()
    assert outcome.action == dx.REJECTED
    assert api.request_state(rid)["state"] == "rejected"
    assert api.request_state(rid)["reason"] == f"{xr.RESUME_REFUSED}:{reason}"
    assert api.fulfils() == [] and api.executions == []


@pytest.mark.parametrize("reason", sorted(RESUME_DEFERRAL_REASONS))
async def test_a_deferred_resume_stays_claimed_mints_nothing_and_is_audited(api, capsys, reason):  # noqa: F811
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=dx.DeliveryAnswer(dx.DELIVERY_DEFERRED, reason))
    outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()
    assert outcome.action == dx.DEFERRED
    assert api.request_state(rid)["state"] == "claimed"
    assert api.fulfils() == [] and api.executions == []
    deferred = [a for a in _audit(capsys.readouterr().out) if a["event"] == "defer"]
    assert [a["reason"] for a in deferred] == [f"resume_deferred:{reason}"]


async def test_a_resume_deferred_then_deliverable_is_fulfilled_exactly_once(api):  # noqa: F811
    """The state changed between two claims (for example the loop became
    serviceable): the same request is re-delivered, fulfilled once, and a
    further dispatch finds nothing. No second delivery, no second execution."""
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=dx.DeliveryAnswer(dx.DELIVERY_DEFERRED, "feedback-not-ready"))
    dispatcher = dx.Dispatcher(WorkItemClient(), temporal, lease=60)
    assert (await dispatcher.dispatch_once()).action == dx.DEFERRED
    api.now += 120  # the lease lapses; the request is claimable again
    temporal.delivery = dx.DeliveryAnswer(dx.DELIVERED)
    assert (await dispatcher.dispatch_once()).action == dx.FULFILLED
    assert [f["execution_request_id"] if "execution_request_id" in f else rid for f in api.fulfils()] == [rid]
    assert len(api.executions) == 1
    assert (await dispatcher.dispatch_once()).action == dx.NOTHING
    assert len(temporal.delivered) == 2 and {d.execution_request_id for _, d in temporal.delivered} == {rid}


@pytest.mark.parametrize(
    ("details", "reason"),
    [((), "unspecified"), (("a-reason-from-a-newer-loop",), "unspecified"), (("legacy-loop",), "legacy-loop")],
    ids=["no-details", "unknown-reason", "known-reason"],
)
async def test_a_deferral_reaches_the_audit_only_in_the_closed_vocabulary(api, capsys, details, reason):  # noqa: F811
    loop = workflow_id_for(URL)
    api.create_request("resume")
    cause = ApplicationError("deferred: a sentence", *details, type=RESUME_DEFERRED_ERROR_TYPE)
    port = _RunningPort(_UpdateClient(WorkflowUpdateFailedError(cause)), loop)
    outcome = await dx.Dispatcher(WorkItemClient(), port, lease=60).dispatch_once()
    assert outcome.action == dx.DEFERRED and api.fulfils() == []
    deferred = [a for a in _audit(capsys.readouterr().out) if a["event"] == "defer"]
    assert [a["reason"] for a in deferred] == [f"resume_deferred:{reason}"]


# -- T6: the real reject rules --------------------------------------------------


def _claim(client: WorkItemClient):
    answer = client.claim_execution_request(60)
    assert answer.verdict == xr.CLAIMED
    return answer.request, answer.claim_token


def test_a_pending_request_cannot_be_rejected_by_id(api):  # noqa: F811
    rid = api.create_request("resume")
    request = xr.ExecutionRequest.from_payload(api.request_state(rid))
    answer = WorkItemClient().reject_execution_request(request, "any-token", xr.NO_RUNNABLE_TARGET)
    assert answer.verdict == xr.NOT_CLAIMED
    assert api.request_state(rid)["state"] == "pending"


def test_a_claimed_request_is_rejected_only_with_the_claimants_token(api):  # noqa: F811
    rid = api.create_request("resume")
    client = WorkItemClient()
    request, token = _claim(client)
    assert client.reject_execution_request(request, "not-the-token", xr.NO_RUNNABLE_TARGET).verdict == xr.NOT_CLAIMED
    assert client.reject_execution_request(request, "", xr.NO_RUNNABLE_TARGET).verdict == xr.NOT_CLAIMED
    assert api.request_state(rid)["state"] == "claimed"
    assert client.reject_execution_request(request, token, xr.NO_RUNNABLE_TARGET).verdict == xr.REJECTED


def test_a_lapsed_lease_makes_the_request_non_rejectable_and_never_rejects_another_holders_claim(api):  # noqa: F811
    """A paused and restarted dispatcher has lost its token. After the lease
    lapsed the old token no longer works, and a fresh claim by someone else
    holds the request: the old token must not reject that claim either."""
    rid = api.create_request("resume")
    client = WorkItemClient()
    request, old_token = _claim(client)
    api.now += 120  # lease lapsed
    assert client.reject_execution_request(request, old_token, xr.NO_RUNNABLE_TARGET).verdict == xr.NOT_CLAIMED
    assert api.request_state(rid)["state"] == "claimed"  # still the lapsed claim, not rejected
    other_request, other_token = _claim(client)  # a new holder re-claims it
    assert other_token != old_token
    assert client.reject_execution_request(other_request, old_token, xr.NO_RUNNABLE_TARGET).verdict == xr.NOT_CLAIMED
    assert api.request_state(rid)["state"] == "claimed"


# -- T7: the age WARN reads only the row ----------------------------------------


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == dx.logger.name and r.levelno == logging.WARNING]


async def _defer_once(api, created_at: str | None, caplog):  # noqa: F811
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    if created_at is None:
        api.xrs[rid].pop("created_at")
    else:
        api.xrs[rid]["created_at"] = created_at
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=dx.DeliveryAnswer(dx.DELIVERY_DEFERRED, "legacy-loop"))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=dx.logger.name):
        outcome = await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()
    assert outcome.action == dx.DEFERRED
    return rid, _warnings(caplog)


async def test_a_deferred_request_older_than_a_day_warns_with_its_age(api, caplog):  # noqa: F811
    old = (datetime.now(UTC) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    rid, warnings = await _defer_once(api, old, caplog)
    assert len(warnings) == 1
    assert rid in warnings[0] and "legacy-loop" in warnings[0] and "age=UNKNOWN" not in warnings[0]
    assert "age=1080" in warnings[0] or "age=108" in warnings[0]  # about 30h = 108000s


async def test_a_young_deferred_request_does_not_warn(api, caplog):  # noqa: F811
    young = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    _, warnings = await _defer_once(api, young, caplog)
    assert warnings == []


@pytest.mark.parametrize("created_at", ["", "not-a-timestamp"], ids=["empty", "unparsable"])
async def test_a_missing_or_unparsable_created_at_is_reported_unknown_never_zero(api, caplog, created_at):  # noqa: F811
    _, warnings = await _defer_once(api, created_at, caplog)
    assert len(warnings) == 1 and "age=UNKNOWN" in warnings[0]


async def test_the_warn_comes_back_after_a_dispatcher_restart_and_never_doubles(api, caplog):  # noqa: F811
    """No process memory: a brand-new Dispatcher on the next claim warns
    again, once per claim, from the row alone."""
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    loop = workflow_id_for(URL)
    rid = api.create_request("resume")
    api.xrs[rid]["created_at"] = old
    temporal = FakeTemporal({loop: dx.LOOP_RUNNING}, delivery=dx.DeliveryAnswer(dx.DELIVERY_DEFERRED, "legacy-loop"))
    counts = []
    for _ in range(2):  # two claims by two dispatcher instances (a restart between)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=dx.logger.name):
            await dx.Dispatcher(WorkItemClient(), temporal, lease=60).dispatch_once()
        counts.append(len(_warnings(caplog)))
        api.now += 120  # the lease lapses
    assert counts == [1, 1]


def test_created_at_is_parsed_from_the_request_body_with_an_empty_default():
    base = {"id": "xr_1", "work_item_id": WID, "kind": "resume", "state": "claimed", "expected_state_version": 1}
    assert xr.ExecutionRequest.from_payload({**base, "created_at": "2026-11-04T00:00:00Z"}).created_at == (
        "2026-11-04T00:00:00Z"
    )
    assert xr.ExecutionRequest.from_payload(base).created_at == ""
