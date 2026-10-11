"""ADR 020 fault suite for governed paid invocation (mctlhq/mctl-agents#619,
C4): the gateway's paid branch, the provider ingress and the pilot.

The fakes (`FakeLedger`, `FakeApprovals`, `Clock`, `World`) live in
tests/commerce_fakes.py. `FakeLedger` is a thread-safe, in-memory stand-in for mctl-api's commerce
routes that keeps mctl-api's rules: the transition map, idempotent accept by
(tenant, principal, key), the approval bound to one invocation, reserve and
dispatch replays, and the dispatch fence with a signed authorization. Each
route can lose its answer before or after applying it, or "crash" the
calling process after applying it, which is how restarts are simulated.
Money is checked as the ledger's own reserved/committed totals.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator import action_approvals as aa
from orchestrator import capability as cap
from orchestrator import capability_gateway as gw
from orchestrator import commerce as cm
from orchestrator import commerce_gateway as cg
from orchestrator import commerce_provider as cp
from orchestrator import policy_checkpoint as pc
from tests.commerce_fakes import (
    APPROVE_PAID,
    EXECUTION,
    GRANTS,
    KID,
    PILOT_CAP,
    SEED,
    TENANT,
    Clock,
    Crash,
    FakeApprovals,
    FakeLedger,
    World,
    _correlation,
    _invoke,
    _no,
    _ok,
    _restart,
    run,
)


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    clock = Clock()
    w = World(clock=clock, ledger=FakeLedger(clock), approvals=FakeApprovals(clock), tmp=tmp_path,
              statuses={"OPS-1": "open", "OPS-2": "closed"})
    w.ledger.publish(PILOT_CAP, w.schema_hash())
    monkeypatch.setattr(pc, "current_identity",
                        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-1", actor="agent:test"))
    monkeypatch.setattr(aa, "ttl_s", lambda: 3600)
    return w



# ---------------------------------------------------------------------------
# Discovery and the legacy path
# ---------------------------------------------------------------------------


def test_discovery_puts_the_terms_in_force_on_the_descriptor(world):
    g = run(world.gateway())
    paid = g.descriptors_by_id[PILOT_CAP]
    assert paid.commerce == cap.CommerceSummary("ctm_01JZ8PZ0000000000000000000", 1, "per_successful_call",
                                                "EUR", 12, "simulated")
    assert g.descriptors_by_id["mctl://mcp-local/free-provider/echo"].commerce is None
    assert g.capability_set.capabilities[0].to_dict()["commerce"]["price"] == {"currency": "EUR", "amount_minor": 12}
    assert world.ledger.writes() == []


def test_free_capability_is_unchanged_and_never_touches_the_ledger(world):
    g = run(world.gateway())
    before = list(world.ledger.calls)
    out = run(g.invoke("mctl://mcp-local/free-provider/echo", {"x": 1}))
    assert out == {"reason_code": "ok", "text": "echo"}
    assert world.ledger.calls == before and world.free.calls == [{"x": 1}]


def test_without_a_commerce_runtime_every_capability_is_legacy_at_discovery(world):
    g = run(world.gateway(commerce=False))
    assert all(d.commerce is None for d in g.capability_set.capabilities)
    assert world.ledger.calls == []


def test_a_terms_read_failure_fails_discovery_instead_of_sealing_it_free(world):
    world.ledger.terms_failure = True
    with pytest.raises(gw.GatewayError, match="could not be read"):
        run(world.gateway())


def test_terms_naming_another_provider_fail_discovery(world):
    world.ledger.publish(PILOT_CAP, world.schema_hash(), provider_id="someone-else")
    with pytest.raises(gw.GatewayError, match="another capability version"):
        run(world.gateway())


# ---------------------------------------------------------------------------
# Happy path, replay, idempotency
# ---------------------------------------------------------------------------


def test_paid_call_is_accepted_reserved_dispatched_and_captured_once(world):
    out = run(_invoke(world))
    assert out["reason_code"] == "ok" and out["text"] == "OPS-1: open"
    inv = world.ledger.only()
    assert out["invocation_id"] == inv["invocation_id"]
    assert (inv["phase"], inv["execution_outcome"], inv["charge_status"]) == ("finalized", "succeeded", "captured")
    assert world.ledger.budget == {"available": 100, "reserved": 0, "committed": 12}
    assert len(world.executions) == 1
    assert world.ingress.outcome(inv["provider_operation_key"]).status == cm.SUCCEEDED


def test_the_same_call_again_is_the_same_invocation_never_a_second_charge(world):
    first = run(_invoke(world))
    again = _restart(world)
    assert again["reason_code"] == "ok" and again["text"] == first["text"]
    assert again["invocation_id"] == first["invocation_id"]
    assert len(world.executions) == 1 and world.ledger.budget["committed"] == 12


def test_a_new_client_key_is_a_deliberate_second_invocation(world):
    run(_invoke(world))
    second = _restart(world, key="again")
    assert second["reason_code"] == "ok"
    assert len(world.ledger.invocations) == 2 and len(world.executions) == 2
    assert world.ledger.budget["committed"] == 24


def test_a_lost_accept_answer_resumes_the_accepted_invocation(world):
    world.ledger.inject("accept", "lose-after")
    out = run(_invoke(world))
    assert out["reason_code"] == "ok"
    assert len(world.ledger.invocations) == 1 and len(world.executions) == 1


def test_an_unreachable_ledger_is_unknown_not_absent(world):
    g = run(world.gateway())
    world.ledger.inject("find_invocation", "lose-before")
    out = run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
    assert out["reason_code"] == "outcome-unknown"
    assert world.ledger.invocations == {} and "mint_quote" not in world.ledger.calls


def test_concurrent_retries_execute_and_charge_exactly_once(world):
    async def both():
        g = await world.gateway()
        world.gate = asyncio.Event()

        async def release_later():
            await asyncio.sleep(0.05)
            world.gate.set()

        results = await asyncio.gather(
            g.invoke(PILOT_CAP, {"ticket": "OPS-1"}), g.invoke(PILOT_CAP, {"ticket": "OPS-1"}), release_later())
        return results[:2]

    results = run(both())
    codes = sorted(r["reason_code"] for r in results)
    assert "ok" in codes and set(codes) <= {"ok", "outcome-unknown"}
    assert len(world.ledger.invocations) == 1 and len(world.executions) == 1
    assert world.ledger.budget["committed"] == 12


# ---------------------------------------------------------------------------
# Refusals before anything is created
# ---------------------------------------------------------------------------


def test_terms_withdrawn_after_discovery_refuse_the_call(world):
    g = run(world.gateway())
    world.ledger.terms.clear()
    out = run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
    assert out["reason_code"] == "commerce-terms-unknown"
    assert world.ledger.writes() == [] and world.executions == []


def test_a_terms_read_failure_at_invocation_refuses_never_free(world):
    g = run(world.gateway())
    world.ledger.terms_failure = True
    out = run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
    assert out["reason_code"] == "commerce-terms-unknown" and world.executions == []


def test_a_paid_descriptor_without_a_runtime_is_refused(world):
    g = run(world.gateway())
    g.commerce = None
    assert run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))["reason_code"] == "commerce-terms-unknown"
    assert world.ledger.writes() == []


def test_a_provider_not_admitted_is_unsupported(world):
    world.admitted = frozenset()
    out = run(_invoke(world))
    assert out["reason_code"] == "unsupported-execution" and world.ledger.writes() == []


def test_a_tool_that_requires_task_execution_is_unsupported(world):
    g = run(world.gateway())
    g.task_support_by_id = {PILOT_CAP: "required"}
    assert run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))["reason_code"] == "unsupported-execution"
    assert world.ledger.writes() == []


def test_the_terms_binding_argument_cannot_be_supplied(world):
    out = run(_invoke(world, {"ticket": "OPS-1", cm.TERMS_BINDING_ARGUMENT: "sha256:" + "0" * 64}))
    assert out["reason_code"] == "invalid-arguments" and world.ledger.writes() == []


@pytest.mark.parametrize("key", ["", "has space", "x" * 65, 7])
def test_a_malformed_client_key_is_invalid(world, key):
    assert run(_invoke(world, key=key))["reason_code"] == "invalid-arguments"


def test_no_execution_identity_is_identity_unresolved(world, monkeypatch):
    monkeypatch.setattr(pc, "current_identity", lambda: pc.ExecutionIdentity())
    out = run(_invoke(world))
    assert out["reason_code"] == "identity-unresolved" and world.ledger.writes() == []


def test_budget_exhausted_at_precheck_creates_nothing(world):
    world.ledger.budget["available"] = 5
    out = run(_invoke(world))
    assert out["reason_code"] == "budget-exhausted"
    assert "mint_quote" not in world.ledger.calls and world.ledger.invocations == {}


def test_no_budget_at_all_is_budget_exhausted(tmp_path, world):
    world.ledger.budget = None
    assert run(_invoke(world))["reason_code"] == "budget-exhausted"


def test_budget_exhausted_at_reserve_refuses_the_row_and_holds_nothing(world):
    world.ledger.hooks["precheck"] = lambda: world.ledger.budget.update(committed=95)
    out = run(_invoke(world))
    assert out["reason_code"] == "budget-exhausted"
    inv = world.ledger.only()
    assert inv["phase"] == "refused" and world.ledger.budget["reserved"] == 0 and world.executions == []


def test_a_price_change_between_check_and_quote_is_stale(world):
    world.ledger.hooks["precheck"] = lambda: world.ledger.publish(PILOT_CAP, world.schema_hash(), amount=15,
                                                                  version=2)
    out = run(_invoke(world))
    assert out["reason_code"] == "quote-stale"
    assert world.ledger.invocations == {} and world.executions == []


# ---------------------------------------------------------------------------
# Approval (ADR 020 sec. 8)
# ---------------------------------------------------------------------------


def test_approval_pending_holds_nothing_then_approve_and_retry_runs_once(world):
    world.policy = APPROVE_PAID
    first = run(_invoke(world))
    assert first["reason_code"] == "policy-denied" and first["approval_ref"]
    assert world.ledger.writes() == [] and world.ledger.budget["reserved"] == 0
    world.approvals.decide(first["approval_ref"], "approved")
    second = _restart(world)
    assert second["reason_code"] == "ok"
    inv = world.ledger.only()
    assert inv["approval_id"] == first["approval_ref"]
    assert world.approvals.by_id[first["approval_ref"]].state == "consumed"
    assert len(world.executions) == 1 and world.ledger.budget["committed"] == 12


@pytest.mark.parametrize("state", ["denied", "expired"])
def test_a_denied_or_expired_approval_refuses(world, state):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, state)
    assert _restart(world)["reason_code"] == "policy-denied"
    assert world.ledger.writes() == [] and world.executions == []


def test_an_approval_past_its_expiry_refuses(world):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.clock.advance(7200)
    assert _restart(world)["reason_code"] == "policy-denied"
    assert world.ledger.invocations == {}


def test_an_approval_binds_the_price_a_reprice_needs_a_new_approval(world):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.ledger.publish(PILOT_CAP, world.schema_hash(), amount=15, version=2)
    out = _restart(world)
    assert out["reason_code"] == "policy-denied" and out["approval_ref"] != ref
    assert world.approvals.by_id[ref].state == "approved"  # never consumed for the new price
    assert world.ledger.invocations == {}


def test_a_consumed_approval_never_authorizes_a_second_invocation(world):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    assert _restart(world)["reason_code"] == "ok"
    second = _restart(world, key="second")
    assert second["reason_code"] == "policy-denied"
    assert len(world.ledger.invocations) == 1 and len(world.executions) == 1


def test_a_lost_consume_answer_reads_back_as_spent_for_this_invocation(world):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.approvals.lose_consume = 1
    assert _restart(world)["reason_code"] == "ok"
    assert len(world.executions) == 1


def test_an_approval_denied_after_accept_refuses_the_row(world):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.ledger.hooks["accept"] = lambda: world.approvals.decide(ref, "denied")
    assert _restart(world)["reason_code"] == "policy-denied"
    inv = world.ledger.only()
    assert inv["phase"] == "refused" and world.executions == [] and world.ledger.budget["reserved"] == 0


def test_no_approval_store_refuses_a_call_that_needs_one(world):
    world.policy = APPROVE_PAID
    world.approvals = None  # type: ignore[assignment]
    assert run(_invoke(world))["reason_code"] == "policy-denied"
    assert world.ledger.writes() == []


# ---------------------------------------------------------------------------
# Revocation between steps (ADR 020 sec. 7, step 7)
# ---------------------------------------------------------------------------


def _revoke_after(world: World, monkeypatch, route: str) -> None:
    revoked = {"on": False}
    real = pc.evaluate

    def evaluate(policy, request):
        if revoked["on"]:
            return None, pc.CODE_NO_RULE, "revoked"
        return real(policy, request)

    monkeypatch.setattr(pc, "evaluate", evaluate)
    world.ledger.hooks[route] = lambda: revoked.update(on=True)


def test_policy_revoked_after_accept_refuses_before_funds_move(world, monkeypatch):
    _revoke_after(world, monkeypatch, "accept")
    assert run(_invoke(world))["reason_code"] == "policy-denied"
    inv = world.ledger.only()
    assert inv["phase"] == "refused" and "reserve" not in world.ledger.calls and world.executions == []


def test_policy_revoked_after_reserve_releases_before_dispatch(world, monkeypatch):
    _revoke_after(world, monkeypatch, "reserve")
    assert run(_invoke(world))["reason_code"] == "policy-denied"
    inv = world.ledger.only()
    assert inv["phase"] == "released" and "dispatch" not in world.ledger.calls
    assert world.ledger.budget["reserved"] == 0 and world.executions == []


# ---------------------------------------------------------------------------
# Crashes and restarts (ADR 020 sec. 11)
# ---------------------------------------------------------------------------


def test_crash_after_accept_resumes_and_runs_once(world):
    world.ledger.inject("accept", "crash-after")
    with pytest.raises(Crash):
        run(_invoke(world))
    assert world.ledger.only()["phase"] == "accepted"
    assert _restart(world)["reason_code"] == "ok"
    assert len(world.executions) == 1 and world.ledger.budget == {"available": 100, "reserved": 0, "committed": 12}


def test_crash_after_reserve_resumes_and_runs_once(world):
    world.ledger.inject("reserve", "crash-after")
    with pytest.raises(Crash):
        run(_invoke(world))
    assert world.ledger.only()["phase"] == "reserved"
    assert _restart(world)["reason_code"] == "ok"
    assert len(world.executions) == 1 and world.ledger.budget["committed"] == 12


def test_crash_after_the_fence_never_redispatches_until_not_found_is_definitive(world):
    world.ledger.inject("dispatch", "crash-after")
    with pytest.raises(Crash):
        run(_invoke(world))
    assert world.ledger.only()["phase"] == "dispatching" and world.executions == []

    # Before deadline + skew, not_found is not proof of absence.
    world.clock.advance(60 + 30)
    out = _restart(world)
    assert out["reason_code"] == "outcome-unknown"
    assert world.ledger.only()["phase"] == "dispatching" and world.ledger.budget["reserved"] == 12

    # Past deadline + max_clock_skew it is: release, nothing charged, still
    # never executed.
    world.clock.advance(31)
    out = _restart(world)
    assert out["reason_code"] == "provider-error"
    inv = world.ledger.only()
    assert inv["phase"] == "released" and world.ledger.budget == {"available": 100, "reserved": 0, "committed": 0}
    assert world.executions == [] and world.ledger.calls.count("dispatch") == 1


def test_crash_after_the_provider_ran_settles_from_its_record(world):
    world.ledger.inject("transition", "crash-before")
    with pytest.raises(Crash):
        run(_invoke(world))
    assert len(world.executions) == 1 and world.ledger.only()["phase"] == "dispatching"
    out = _restart(world)
    assert out["reason_code"] == "ok" and out["text"] == "OPS-1: open"
    assert len(world.executions) == 1 and world.ledger.budget["committed"] == 12


def test_a_disconnect_mid_call_is_captured_on_resume_without_reexecution(world):
    async def disconnect():
        g = await world.gateway()
        world.gate, world.entered = asyncio.Event(), asyncio.Event()
        task = asyncio.ensure_future(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
        await world.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        world.gate.set()
        await asyncio.sleep(0.01)  # the accepted operation runs to its outcome
        return await g.invoke(PILOT_CAP, {"ticket": "OPS-1"})

    out = run(disconnect())
    assert out["reason_code"] == "ok" and out["text"] == "OPS-1: open"
    assert len(world.executions) == 1 and world.ledger.budget["committed"] == 12


def test_a_slow_provider_is_outcome_unknown_then_settles_on_retry(world):
    async def slow():
        g = await world.gateway()
        world.gate = asyncio.Event()
        first = await g.invoke(PILOT_CAP, {"ticket": "OPS-1"})
        phase = world.ledger.only()["phase"]
        world.gate.set()
        await asyncio.sleep(0.01)
        return first, phase, await g.invoke(PILOT_CAP, {"ticket": "OPS-1"})

    first, phase, second = run(slow())
    assert first["reason_code"] == "outcome-unknown" and phase == "outcome_unknown"
    assert second["reason_code"] == "ok" and len(world.executions) == 1
    assert world.ledger.budget["committed"] == 12


# ---------------------------------------------------------------------------
# Provider outcomes (ADR 020 sec. 9)
# ---------------------------------------------------------------------------


def test_provider_failure_per_successful_call_is_released(world):
    out = run(_invoke(world, {"ticket": "OPS-404"}))
    assert out["reason_code"] == "provider-error" and out["text"] == "unknown ticket"
    inv = world.ledger.only()
    assert (inv["execution_outcome"], inv["charge_status"]) == ("failed", "released")
    assert world.ledger.budget == {"available": 100, "reserved": 0, "committed": 0}


def test_provider_failure_per_call_is_captured(tmp_path, world):
    world.ledger.publish(PILOT_CAP, world.schema_hash(), price_mode="per_call")
    out = run(_invoke(world, {"ticket": "OPS-404"}))
    assert out["reason_code"] == "provider-error"
    assert world.ledger.only()["charge_status"] == "captured" and world.ledger.budget["committed"] == 12


def test_an_ingress_refusal_releases_and_never_executes(world):
    async def refused():
        g = await world.gateway()
        world.ingress.tenant_id = "another-tenant"
        return await g.invoke(PILOT_CAP, {"ticket": "OPS-1"})

    out = run(refused())
    assert out["reason_code"] == "provider-error"
    assert world.ledger.only()["phase"] == "released" and world.executions == []
    assert world.ledger.budget["reserved"] == 0


def test_payment_unknown_at_reserve_never_dispatches(world):
    world.ledger.payment_unknown = True
    out = run(_invoke(world))
    assert out["reason_code"] == "outcome-unknown"
    assert "dispatch" not in world.ledger.calls and world.executions == []


def test_dispatch_unavailable_leaves_the_reservation_for_the_reconciler(world):
    world.ledger.dispatch_unavailable = True
    out = run(_invoke(world))
    assert out["reason_code"] == "outcome-unknown"
    assert world.ledger.only()["phase"] == "reserved" and world.executions == []


def test_a_lost_finalize_answer_still_returns_the_result(world):
    world.ledger.inject("finalize", "lose-before")
    world.ledger.inject("get_invocation", "lose-before")
    out = run(_invoke(world))
    assert out["reason_code"] == "ok" and out["text"] == "OPS-1: open"
    assert world.ledger.only()["phase"] == "dispatched" and len(world.executions) == 1


def test_a_late_contradicting_result_is_audited_and_changes_no_charge(world, capsys):
    def settle_as_failed():
        inv = world.ledger.only()
        inv["phase"] = "finalized"
        inv["execution_outcome"] = "failed"
        inv["charge_status"] = "released"
        world.ledger.budget["reserved"] -= 12

    world.ledger.hooks["dispatch"] = settle_as_failed
    out = run(_invoke(world))
    assert out["reason_code"] == "provider-error"
    assert world.ledger.budget["committed"] == 0
    assert "CAPABILITY_COMMERCE_LATE_RESULT" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Bypass attempts (ADR 020 sec. 9, requirement 1)
# ---------------------------------------------------------------------------


def test_a_paid_capability_needs_the_policy_checkpoint(world):
    g = run(world.gateway())
    g.checkpoint = cap.AbsentPolicyCheckpoint()
    assert run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))["reason_code"] == "policy-denied"
    assert world.ledger.writes() == [] and world.executions == []


def test_the_paid_tool_cannot_be_called_without_an_authorization(world):
    run(world.gateway())
    session = cp.PaidLocalProviderSession(world.ingress)
    with pytest.raises(cm.DispatchRefused):
        run(session.call_tool(cp.PILOT_TOOL, {"ticket": "OPS-1"}))
    assert world.executions == []


def test_traces_carry_no_money_and_no_payload(world, capsys):
    run(_invoke(world))
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("CAPABILITY_")]
    assert lines and "OPS-1" not in out and "amount_minor" not in out and "EUR" not in out


# ---------------------------------------------------------------------------
# Provider ingress on its own (ADR 020 sec. 9, requirements 2-5)
# ---------------------------------------------------------------------------


def _auth(world: World, *, ticket="OPS-1", invocation="inv_01JZ8Q0000000000000000000D", seconds=60,
          **override) -> cm.DispatchAuthorization:
    fields = dict(
        key_id=KID, invocation_id=invocation, provider_operation_key=cm.provider_operation_key(invocation),
        provider_id=cp.PILOT_PROVIDER_ID, capability_id=PILOT_CAP, input_schema_hash=world.schema_hash(),
        args_digest=cm.args_digest({"ticket": ticket}), tenant_id=TENANT,
        dispatch_deadline=cm.hashed_time(world.clock() + timedelta(seconds=seconds)),
    )
    fields.update(override)
    return cm.sign_dispatch_authorization(SEED, cm.DispatchAuthorization(**fields))


def test_ingress_dedupes_a_key_and_survives_a_restart(world):
    auth = _auth(world)
    first = run(world.new_ingress().call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
    again = run(world.new_ingress().call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
    assert first == again and len(world.executions) == 1
    assert world.ingress.outcome(auth.provider_operation_key) == cm.ProviderOutcome(cm.SUCCEEDED, text="OPS-1: open")


def test_ingress_records_acceptance_before_the_side_effect(world):
    ingress = world.new_ingress()
    auth = _auth(world)
    seen = {}
    spec = ingress._tools[cp.PILOT_TOOL]

    async def observing(arguments):
        seen["during"] = ingress.outcome(auth.provider_operation_key).status
        return await spec.handler(arguments)

    ingress._tools[cp.PILOT_TOOL] = dataclasses.replace(spec, handler=observing)
    run(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
    assert seen["during"] == cm.IN_PROGRESS


def test_ingress_checks_the_deadline_only_for_a_new_key(world):
    auth = _auth(world)
    run(world.new_ingress().call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
    world.clock.advance(3600)
    assert run(world.ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth)).text == "OPS-1: open"
    late = _auth(world, invocation="inv_01JZ8Q0000000000000000000E", seconds=-1)
    with pytest.raises(cm.DispatchRefused, match="deadline"):
        run(world.ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, late))
    assert world.ingress.outcome(late.provider_operation_key).status == cm.NOT_FOUND
    assert len(world.executions) == 1


def test_ingress_records_a_raising_handler_as_failed_without_its_text(world):
    ingress = world.new_ingress()
    spec = ingress._tools[cp.PILOT_TOOL]

    async def boom(arguments):
        raise RuntimeError("secret internals")

    ingress._tools[cp.PILOT_TOOL] = dataclasses.replace(spec, handler=boom)
    auth = _auth(world)
    out = run(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
    assert out.is_error and "secret" not in out.text
    assert ingress.outcome(auth.provider_operation_key).status == cm.FAILED


def test_ingress_reports_a_concurrent_duplicate_as_in_progress(world):
    async def both():
        ingress = world.new_ingress()
        world.gate = asyncio.Event()
        auth = _auth(world)
        first = asyncio.ensure_future(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
        await asyncio.sleep(0.01)
        with pytest.raises(cm.OutcomeUnavailable):
            await ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth)
        world.gate.set()
        return await first

    assert run(both()).text == "OPS-1: open" and len(world.executions) == 1


@pytest.mark.parametrize("change", [
    {"ticket": "OPS-2"},
    {"tenant_id": "other"},
    {"provider_id": "other-provider"},
    {"capability_id": "mctl://mcp-local/pilot-provider/other"},
    {"input_schema_hash": "sha256:" + "cd" * 32},
    {"key_id": "unknown-kid"},
])
def test_ingress_refuses_an_authorization_for_anything_else(world, change):
    ingress = world.new_ingress()
    ticket = change.pop("ticket", "OPS-1")
    auth = _auth(world, ticket=ticket, **change)
    with pytest.raises(cm.DispatchRefused):
        run(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
    assert world.executions == [] and ingress.outcome(auth.provider_operation_key).status == cm.NOT_FOUND


def test_ingress_refuses_a_key_reused_for_other_arguments(world):
    ingress = world.new_ingress()
    run(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, _auth(world)))
    with pytest.raises(cm.DispatchRefused, match="another invocation"):
        run(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-2"}, _auth(world, ticket="OPS-2")))
    assert len(world.executions) == 1


# ---------------------------------------------------------------------------
# Guards the provider's own dedupe would otherwise mask
# ---------------------------------------------------------------------------


def test_a_replayed_dispatch_never_calls_the_provider(world):
    """Another attempt won the fence between our reserve and our dispatch.
    Its authorization comes back to us as a replay: we must not use it."""
    def concurrent_fence():
        world.ledger.dispatch(TENANT, world.ledger.only()["invocation_id"])

    world.ledger.hooks["reserve"] = concurrent_fence
    out = run(_invoke(world))
    assert out["reason_code"] == "outcome-unknown"
    assert world.executions == [] and world.ledger.only()["phase"] == "dispatching"


def test_not_found_after_the_provider_confirmed_acceptance_is_never_a_release(world):
    world.ledger.inject("dispatch", "crash-after")
    with pytest.raises(Crash):
        run(_invoke(world))
    inv = world.ledger.only()
    inv["phase"] = "dispatched"  # the provider once answered in_progress
    world.clock.advance(3600)
    out = _restart(world)
    assert out["reason_code"] == "outcome-unknown"
    assert world.ledger.only()["phase"] == "outcome_unknown" and "release" not in world.ledger.calls
    assert world.ledger.budget["reserved"] == 12


def test_terms_repriced_to_free_after_discovery_refuse_until_rediscovered(world):
    g = run(world.gateway())
    world.ledger.publish(PILOT_CAP, world.schema_hash(), price_mode="free", amount=0, version=2)
    out = run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
    assert out["reason_code"] == "commerce-terms-unknown"
    assert world.ledger.writes() == [] and world.executions == []


def test_a_cancelled_handler_stays_in_progress_never_failed(world):
    """Shutdown mid-handler: the effect is unknown, so the provider must not
    claim `failed`, which would release a per_successful_call charge."""
    async def shutdown():
        ingress = world.new_ingress()
        world.gate, world.entered = asyncio.Event(), asyncio.Event()
        auth = _auth(world)
        task = asyncio.ensure_future(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
        await world.entered.wait()
        for running in list(ingress._running):
            running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return ingress.outcome(auth.provider_operation_key).status

    assert run(shutdown()) == cm.IN_PROGRESS


def test_a_stored_outcome_is_never_handed_to_another_tenant(world):
    ingress = world.new_ingress()
    ingress.tenant_id = ""  # a multi-tenant provider
    run(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, _auth(world)))
    with pytest.raises(cm.DispatchRefused, match="another invocation"):
        run(ingress.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, _auth(world, tenant_id="other")))
    assert len(world.executions) == 1


def test_a_gateway_bug_after_approval_audits_the_policy_verdict(world, monkeypatch, capsys):
    async def boom(self, inv):
        raise RuntimeError("bug")

    monkeypatch.setattr(cg.PaidInvocation, "_from_reserved", boom)
    out = run(_invoke(world))
    assert out["reason_code"] == "outcome-unknown"
    record = [line for line in capsys.readouterr().out.splitlines() if line.startswith("CAPABILITY_INVOCATION ")]
    assert record and '"policy_checkpoint": "allowed"' in record[-1]



# ---------------------------------------------------------------------------
# Review of 0152cb9
# ---------------------------------------------------------------------------


def _invocation_records(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("CAPABILITY_INVOCATION ")]


def test_a_resumed_approved_call_is_audited_as_allowed(world, capsys):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.ledger.inject("transition", "crash-before")
    with pytest.raises(Crash):
        _restart(world)
    capsys.readouterr()
    out = _restart(world)
    assert out["reason_code"] == "ok" and len(world.executions) == 1
    records = _invocation_records(capsys.readouterr().out)
    assert records and '"policy_checkpoint": "allowed"' in records[-1]
    # A plain replay of the finalized invocation reports the same verdict.
    _restart(world)
    assert '"policy_checkpoint": "allowed"' in _invocation_records(capsys.readouterr().out)[-1]


def test_an_exhausted_budget_never_opens_an_approval_request(world):
    world.policy = APPROVE_PAID
    world.ledger.budget["available"] = 5
    out = run(_invoke(world))
    assert out["reason_code"] == "budget-exhausted" and world.approvals.by_id == {}


@pytest.mark.parametrize("field", ["capability_id", "args_digest", "execution_id", "tenant_id"])
def test_a_resumed_row_naming_another_call_is_not_adopted(world, field):
    world.ledger.inject("reserve", "crash-after")
    with pytest.raises(Crash):
        run(_invoke(world))
    world.ledger.only()[field] = "something-else"
    out = _restart(world)
    assert out["reason_code"] == "outcome-unknown" and world.executions == []
    assert "dispatch" not in world.ledger.calls


def test_two_provider_processes_racing_one_key_never_both_run(world):
    async def race():
        first = world.new_ingress()
        world.gate, world.entered = asyncio.Event(), asyncio.Event()
        auth = _auth(world)
        running = asyncio.ensure_future(first.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth))
        await world.entered.wait()
        second = world.new_ingress()  # another process on the same store
        real_row = second._row
        calls = {"n": 0}

        def stale_row(key):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_row(key)  # lost the check-then-insert race

        second._row = stale_row  # type: ignore[method-assign]
        with pytest.raises(cm.OutcomeUnavailable):
            await second.call(cp.PILOT_TOOL, {"ticket": "OPS-1"}, auth)
        world.gate.set()
        return await running

    assert run(race()).text == "OPS-1: open" and len(world.executions) == 1


# ---------------------------------------------------------------------------
# agy review of 9051e6f
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arguments", [None, 123, "OPS-1", ["ticket"]])
def test_non_object_arguments_to_a_paid_capability_are_refused_before_commerce(world, arguments):
    g = run(world.gateway())
    out = run(g.invoke(PILOT_CAP, arguments, None))
    assert out["reason_code"] == "invalid-arguments"
    assert world.ledger.writes() == [] and world.executions == []


def _policy_decisions(out: str) -> list[dict]:
    prefix = pc.DECISION_PREFIX + " "
    return [json.loads(line[len(prefix):]) for line in out.splitlines() if line.startswith(prefix)]


def test_a_consumed_approval_is_audited_like_policy_checkpoint_audits_it(world, capsys):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    capsys.readouterr()
    assert run(_invoke(world))["reason_code"] == "ok"
    decisions = _policy_decisions(capsys.readouterr().out)
    approved = [d for d in decisions if d["code"] == pc.CODE_APPROVED]
    # Once at the consume and once at the recheck before dispatch: the
    # decision in force when the provider runs is the approved one.
    assert len(approved) == 2 and decisions[-1]["code"] == pc.CODE_APPROVED
    for d in approved:
        # The same shape `policy_checkpoint.decide` writes when it redeems
        # an approval itself: the rule's REQUIRE_APPROVAL verdict, code
        # approved, the receipt and the approver. Never DENY, nor ALLOW.
        assert d["decision"] == pc.REQUIRE_APPROVAL == pc._verdict_for(pc.CODE_APPROVED)
        assert d["approval_ref"] == ref and d["approver"]


def test_a_settled_success_whose_result_is_lost_says_so(world):
    first = run(_invoke(world))
    assert first["reason_code"] == "ok" and first["text"]
    (world.tmp / "pilot.db").unlink()  # the provider's records are gone
    out = _restart(world)
    assert out["reason_code"] == "ok" and "text" not in out
    assert out["error"] == cg._RESULT_UNAVAILABLE
    assert len(world.executions) == 1


def test_a_retry_of_a_released_approved_call_keeps_its_verdict(world, capsys):
    """Released after dispatch (the not_found margin): the approval was spent,
    so every later read of the row reports the same verdict."""
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.ledger.inject("dispatch", "crash-after")
    with pytest.raises(Crash):
        _restart(world)
    world.clock.advance(3600)
    capsys.readouterr()
    assert _restart(world)["reason_code"] == "provider-error"
    assert '"policy_checkpoint": "allowed"' in _invocation_records(capsys.readouterr().out)[-1]
    assert _restart(world)["reason_code"] == "provider-error"
    assert '"policy_checkpoint": "allowed"' in _invocation_records(capsys.readouterr().out)[-1]
    assert world.executions == []


def test_a_call_revoked_before_dispatch_stays_denied_on_retry(world, capsys):
    """Released from reserved by a revocation: the verdict is denied, and a
    retry of the released row must not report it as allowed."""
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.ledger.inject("reserve", "crash-after")
    with pytest.raises(Crash):
        _restart(world)
    world.policy = pc.Policy("test/paid-deny/v1", (
        pc.Rule("paid-deny", pc.MCP_TOOL_CALL, "mcp__pilot__*", pc.DENY, requires_grant=True),
    ))
    capsys.readouterr()
    assert _restart(world)["reason_code"] == "policy-denied"
    inv = world.ledger.only()
    assert inv["phase"] == "released" and inv["approval_id"] == ref
    world.policy = APPROVE_PAID
    _restart(world)
    assert '"policy_checkpoint": "denied"' in _invocation_records(capsys.readouterr().out)[-1]
    assert world.executions == []


@pytest.mark.parametrize("arguments", [None, 7, ["ticket"]])
def test_the_paid_path_refuses_non_object_arguments_on_its_own(world, arguments):
    g = run(world.gateway())
    out = run(g._invoke_paid(g.descriptors_by_id[PILOT_CAP], arguments, "", 0.0))
    assert out["reason_code"] == "invalid-arguments" and world.ledger.writes() == []


# ---------------------------------------------------------------------------
# Review of 9a49358
# ---------------------------------------------------------------------------


def test_a_resumed_attempt_writes_no_unattributed_approval_line(world, capsys):
    """Resumed from reserved, this attempt did not consume the approval: it
    writes no `approved` line it cannot attribute, and every approved line
    pairs an approver with its timestamp."""
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.ledger.inject("reserve", "crash-after")
    with pytest.raises(Crash):
        _restart(world)
    first = _policy_decisions(capsys.readouterr().out)
    assert any(d["code"] == pc.CODE_APPROVED for d in first)
    assert _restart(world)["reason_code"] == "ok"
    out = capsys.readouterr().out
    resumed = _policy_decisions(out)
    assert not [d for d in resumed if d["code"] == pc.CODE_APPROVED]
    assert '"policy_checkpoint": "allowed"' in _invocation_records(out)[-1]
    for d in first:
        assert bool(d["approver"]) == bool(d["decided_at"])


def test_a_release_by_any_other_writer_keeps_the_spent_approval_verdict(world, capsys):
    """A release whose reason is not a revocation (the reconciler may write
    its own code) reports the verdict of the approval it spent."""
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    assert _restart(world)["reason_code"] == "ok"
    inv = world.ledger.only()
    inv["phase"], inv["reason_code"] = "released", "dispatch-expired"
    capsys.readouterr()
    _restart(world)
    assert '"policy_checkpoint": "allowed"' in _invocation_records(capsys.readouterr().out)[-1]


def test_an_approval_spent_without_its_record_carries_no_timestamp(world, capsys, monkeypatch):
    """A store that confirms the spend but returns no record: the approved
    line names no approver, so it carries no observation time either."""
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    real = world.approvals.consume

    def spent_without_record(approval_id, presented_hash):
        answer = real(approval_id, presented_hash)
        return aa.ApprovalAnswer(aa.SPENT) if answer.status == aa.SPENT else answer

    monkeypatch.setattr(world.approvals, "consume", spent_without_record)
    capsys.readouterr()
    assert _restart(world)["reason_code"] == "ok"
    approved = [d for d in _policy_decisions(capsys.readouterr().out) if d["code"] == pc.CODE_APPROVED]
    assert approved and all(d["approver"] == "" and d["decided_at"] == "" for d in approved)


# ---------------------------------------------------------------------------
# mctlhq/mctl-agents#626: retries without usable terms, replayed accepts
# ---------------------------------------------------------------------------


_UNUSABLE_TERMS = ["unreadable", "withdrawn", "mismatched", "free", "malformed"]


def _terms_unusable(world: World, how: str) -> None:
    """Each refusal site in `run()` before the checkpoint: the read failed,
    no terms are in force, the terms name another capability version, the
    capability was repriced to free, or the terms are malformed."""
    if how == "unreadable":
        world.ledger.terms_failure = True
    elif how == "withdrawn":
        world.ledger.terms.clear()
    elif how == "mismatched":
        world.ledger.publish(PILOT_CAP, world.schema_hash(), provider_id="another-provider")
    elif how == "free":
        world.ledger.publish(PILOT_CAP, world.schema_hash(), price_mode="free", amount=0)
    else:
        world.ledger.publish(PILOT_CAP, world.schema_hash())["price"] = None


@pytest.mark.parametrize("how", _UNUSABLE_TERMS)
def test_a_retry_without_terms_settles_a_dispatched_call(world, capsys, how):
    async def slow():
        g = await world.gateway()
        world.gate = asyncio.Event()
        first = await g.invoke(PILOT_CAP, {"ticket": "OPS-1"})
        world.gate.set()
        await asyncio.sleep(0.01)
        _terms_unusable(world, how)
        capsys.readouterr()
        return first, await g.invoke(PILOT_CAP, {"ticket": "OPS-1"})

    first, second = run(slow())
    assert first["reason_code"] == "outcome-unknown"
    assert second["reason_code"] == "ok" and second["text"] == "OPS-1: open"
    assert second["invocation_id"] == world.ledger.only()["invocation_id"]
    assert len(world.executions) == 1 and world.ledger.budget["committed"] == 12
    # The verdict is the one the dispatching attempt reached, not "denied".
    assert '"policy_checkpoint": "allowed"' in _invocation_records(capsys.readouterr().out)[-1]


@pytest.mark.parametrize("how", _UNUSABLE_TERMS)
@pytest.mark.parametrize("stage", ["accept", "reserve"])
def test_a_retry_without_terms_names_the_held_invocation_and_never_advances_it(world, how, stage):
    world.ledger.inject(stage, "crash-after")
    with pytest.raises(Crash):
        run(_invoke(world))
    held = dict(world.ledger.only())
    g = run(world.gateway())  # a fresh process discovered the terms, then lost them
    _terms_unusable(world, how)
    world.ledger.calls.clear()
    out = run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
    assert out["reason_code"] == "commerce-terms-unknown"
    assert out["invocation_id"] == held["invocation_id"] and held["phase"] in out["error"]
    assert world.ledger.writes() == [] and world.executions == []
    assert world.ledger.only()["phase"] == held["phase"]


def test_a_first_call_without_terms_names_no_invocation(world):
    g = run(world.gateway())
    world.ledger.terms_failure = True
    out = run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
    assert out["reason_code"] == "commerce-terms-unknown" and "invocation_id" not in out
    assert world.ledger.writes() == [] and world.ledger.invocations == {}


def test_a_retry_without_terms_does_not_adopt_a_row_naming_another_call(world):
    world.ledger.inject("reserve", "crash-after")
    with pytest.raises(Crash):
        run(_invoke(world))
    world.ledger.only()["args_digest"] = "something-else"
    g = run(world.gateway())
    world.ledger.terms_failure = True
    out = run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))
    assert out["reason_code"] == "outcome-unknown" and world.executions == []
    assert "dispatch" not in world.ledger.calls


@pytest.mark.parametrize("field", ["capability_id", "args_digest", "execution_id", "tenant_id"])
def test_a_replayed_accept_naming_another_call_is_not_adopted(world, monkeypatch, field):
    accept = world.ledger.accept

    def replayed_elsewhere(*args, **kwargs):
        answer = accept(*args, **kwargs)
        return _ok({**answer.body, field: "something-else", "replayed": True})

    monkeypatch.setattr(world.ledger, "accept", replayed_elsewhere)
    out = run(_invoke(world))
    assert out["reason_code"] == "outcome-unknown"
    assert out["invocation_id"] == world.ledger.only()["invocation_id"]
    assert "reserve" not in world.ledger.calls and world.executions == []


# ---------------------------------------------------------------------------
# mctlhq/mctl-agents#626: discovery reads, the lost consume, task support
# ---------------------------------------------------------------------------


class _ManyTools:
    """A free provider listing `n` tools, each with paid terms."""

    def __init__(self, n: int) -> None:
        self.names = [f"tool{i}" for i in range(n)]

    async def list_tools(self):
        return [gw.ProviderTool(name=n, description="", input_schema={"type": "object"}) for n in self.names]

    async def call_tool(self, tool, arguments):
        raise AssertionError("discovery only")


class _MeetingTerms:
    """Answers a terms read only once `parties` reads are in flight together,
    so sequential reads time out."""

    def __init__(self, ledger: FakeLedger, parties: int) -> None:
        self.ledger = ledger
        self.barrier = threading.Barrier(parties, timeout=2)

    def current_terms(self, capability_id: str, input_schema_hash: str) -> cm.CommerceAnswer:
        self.barrier.wait()
        return self.ledger.current_terms(capability_id, input_schema_hash)


def _discover_many(world: World, client: Any, n: int) -> gw.CapabilityGateway:
    many = _ManyTools(n)
    schema = gw.hash_bytes(gw.canonical_json({"type": "object"}))
    for name in many.names:
        world.ledger.publish(f"mctl://mcp-local/many/{name}", schema, provider_id="many")
    runtime = cg.CommerceRuntime(client=client, tenant_id=TENANT, admitted_providers=frozenset(),
                                 approvals=world.approvals, now=world.clock)  # type: ignore[arg-type]
    provider = cap.ProviderRef(type="mcp-local", id="many", alias="many")
    return run(gw.CapabilityGateway.build(
        SimpleNamespace(tools=("mcp__many__*",)), _correlation(), [provider],
        checkpoint=gw.PolicyDecidePolicyCheckpoint(grants=GRANTS, policy=world.policy),
        local_sessions={"many": many}, headers={}, consequence_table={}, created_at="2026-10-11T12:00:00Z",
        commerce=runtime,
    ))


def test_discovery_reads_the_terms_of_every_matched_tool_concurrently(world):
    g = _discover_many(world, _MeetingTerms(world.ledger, 3), 3)
    summaries = [d.commerce for d in g.descriptors_by_id.values()]
    assert len(summaries) == 3 and all(s is not None and s.amount_minor == 12 for s in summaries)


def test_discovery_fails_on_the_first_unreadable_terms_in_discovery_order(world):
    class FailSome:
        def current_terms(self, capability_id, input_schema_hash):
            if capability_id.endswith(("tool1", "tool2")):
                return _no(503, capability_id.rsplit("/", 1)[1])
            return world.ledger.current_terms(capability_id, input_schema_hash)

    with pytest.raises(gw.GatewayError, match="tool1"):
        _discover_many(world, FailSome(), 3)


def test_a_lost_consume_answer_still_writes_the_approved_line_before_dispatch(world, capsys):
    world.policy = APPROVE_PAID
    ref = run(_invoke(world))["approval_ref"]
    world.approvals.decide(ref, "approved")
    world.approvals.lose_consume = 1
    capsys.readouterr()
    assert run(_invoke(world))["reason_code"] == "ok"
    decisions = _policy_decisions(capsys.readouterr().out)
    approved = [d for d in decisions if d["code"] == pc.CODE_APPROVED]
    # As on the answered path: once at the consume, once at the recheck.
    assert len(approved) == 2 and decisions[-1]["code"] == pc.CODE_APPROVED
    assert all(d["approval_ref"] == ref and d["approver"] for d in approved)


def test_a_paid_tool_that_lists_required_task_support_is_unsupported(world):
    real = world.new_ingress

    def required() -> cp.ProviderIngress:
        ingress = real()
        spec = ingress._tools[cp.PILOT_TOOL]
        ingress._tools[cp.PILOT_TOOL] = dataclasses.replace(spec, task_support="required")
        return ingress

    world.new_ingress = required  # type: ignore[method-assign]
    g = run(world.gateway())
    assert g.task_support_by_id[PILOT_CAP] == "required"
    assert run(g.invoke(PILOT_CAP, {"ticket": "OPS-1"}))["reason_code"] == "unsupported-execution"
    assert world.ledger.writes() == [] and world.executions == []
