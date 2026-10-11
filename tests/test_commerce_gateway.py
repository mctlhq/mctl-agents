"""ADR 020 fault suite for governed paid invocation (mctlhq/mctl-agents#619,
C4): the gateway's paid branch, the provider ingress and the pilot.

`FakeLedger` is a thread-safe, in-memory stand-in for mctl-api's commerce
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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
from orchestrator.context_snapshot import ExecutionCorrelation

T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
TENANT = "acme"
PRINCIPAL = "prn_01JZ8Q0000000000000000000A"
EXECUTION = "we_01JZ8Q0000000000000000000C"
SEED = b"\x09" * 32
KID = "kid-gw-test"
PILOT = cap.ProviderRef(type="mcp-local", id=cp.PILOT_PROVIDER_ID, alias="pilot")
FREE = cap.ProviderRef(type="mcp-local", id="free-provider", alias="free")
PILOT_CAP = f"mctl://mcp-local/{cp.PILOT_PROVIDER_ID}/{cp.PILOT_TOOL}"
GRANTS = ("mcp__pilot__*", "mcp__free__*")

ALLOW_PAID = pc.Policy("test/paid-allow/v1", (
    pc.Rule("paid-allow", pc.MCP_TOOL_CALL, "mcp__*", pc.ALLOW, requires_grant=True),
))
APPROVE_PAID = pc.Policy("test/paid-approve/v1", (
    pc.Rule("paid-approve", pc.MCP_TOOL_CALL, "mcp__pilot__*", pc.REQUIRE_APPROVAL, requires_grant=True),
    pc.Rule("free-allow", pc.MCP_TOOL_CALL, "mcp__free__*", pc.ALLOW, requires_grant=True),
))


class Crash(BaseException):
    """The gateway process dying mid-call: not an Exception, so nothing in
    the gateway can catch it and carry on."""


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def _ok(body: dict, status: int = 200) -> cm.CommerceAnswer:
    return cm.CommerceAnswer(cm.OK, status, body=body)


def _no(status: int, code: str) -> cm.CommerceAnswer:
    return cm.CommerceAnswer(cm.REFUSED, status, code)


_LOST = cm.CommerceAnswer(cm.UNKNOWN, reason="answer lost")

_EDGES = {
    "accepted": {"refused", "reserved"},
    "reserved": {"dispatching", "released"},
    "dispatching": {"dispatched", "released", "outcome_unknown"},
    "dispatched": {"finalized", "outcome_unknown"},
    "outcome_unknown": {"finalized"},
}
_GENERIC_EDGES = {("accepted", "refused"), ("dispatching", "dispatched"), ("dispatching", "outcome_unknown"),
                  ("dispatched", "outcome_unknown")}


class FakeLedger:
    def __init__(self, clock: Clock, *, price_mode: str = "per_successful_call", amount: int = 12,
                 budget: int | None = 100) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.terms: dict[tuple[str, str], dict] = {}
        self.budget = {"available": budget, "reserved": 0, "committed": 0} if budget is not None else None
        self.quotes: dict[str, dict] = {}
        self.invocations: dict[str, dict] = {}
        self.by_key: dict[tuple[str, str], str] = {}
        self.auths: dict[str, dict] = {}
        self.calls: list[str] = []
        self.faults: dict[str, list[str]] = {}
        self.hooks: dict[str, Callable[[], None]] = {}
        self.terms_failure = False
        self.payment_unknown = False
        self.dispatch_unavailable = False
        self._n = 0
        self.default_price_mode, self.default_amount = price_mode, amount

    # -- set-up ----------------------------------------------------------------

    def publish(self, capability_id: str, schema_hash: str, *, provider_id: str = cp.PILOT_PROVIDER_ID,
                price_mode: str | None = None, amount: int | None = None, version: int = 1) -> dict:
        terms = {
            "schema_version": 1, "terms_id": "ctm_01JZ8PZ0000000000000000000", "terms_version": version,
            "capability_id": capability_id, "input_schema_hash": schema_hash, "provider_id": provider_id,
            "price_mode": price_mode or self.default_price_mode, "meter": "invocation",
            "price": {"currency": "EUR", "amount_minor": self.default_amount if amount is None else amount},
            "funds_mode": "simulated", "quote_ttl_seconds": 300,
        }
        self.terms[(capability_id, schema_hash)] = terms
        return terms

    def inject(self, route: str, *modes: str) -> None:
        self.faults.setdefault(route, []).extend(modes)

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._n:026d}"

    def _route(self, name: str, op: Callable[[], cm.CommerceAnswer]) -> cm.CommerceAnswer:
        with self.lock:
            self.calls.append(name)
            mode = self.faults.get(name, []).pop(0) if self.faults.get(name) else ""
        if mode == "lose-before":
            return _LOST
        if mode == "crash-before":
            raise Crash(name)
        with self.lock:
            answer = op()
        hook = self.hooks.pop(name, None)
        if hook is not None:
            hook()
        if mode == "lose-after":
            return _LOST
        if mode == "crash-after":
            raise Crash(name)
        return answer

    def writes(self) -> list[str]:
        return [c for c in self.calls if c not in ("current_terms", "find_invocation", "get_invocation", "precheck")]

    # -- routes ----------------------------------------------------------------

    def current_terms(self, capability_id: str, input_schema_hash: str) -> cm.CommerceAnswer:
        def op():
            if self.terms_failure:
                return _LOST
            terms = self.terms.get((capability_id, input_schema_hash))
            return _ok(dict(terms)) if terms else _no(422, "commerce-terms-unknown")
        return self._route("current_terms", op)

    def precheck(self, tenant_id: str, *, currency: str, amount_minor: int) -> cm.CommerceAnswer:
        def op():
            if self.budget is None:
                return _no(404, "not-found")
            free = self.budget["available"] - self.budget["reserved"] - self.budget["committed"]
            return _ok({"sufficient": free >= amount_minor, "reason_code": "", "balance": dict(self.budget)})
        return self._route("precheck", op)

    def mint_quote(self, tenant_id: str, *, capability_id: str, input_schema_hash: str, args_digest: str,
                   execution_id: str) -> cm.CommerceAnswer:
        def op():
            terms = self.terms.get((capability_id, input_schema_hash))
            if terms is None:
                return _no(422, "commerce-terms-unknown")
            quote = {
                "schema_version": 1, "quote_id": self._id("cq"), **{k: terms[k] for k in (
                    "terms_id", "terms_version", "capability_id", "input_schema_hash", "price_mode", "funds_mode")},
                "price": dict(terms["price"]), "tenant_id": tenant_id, "principal_id": PRINCIPAL,
                "subject_principal_id": PRINCIPAL, "payer_account_id": cm.payer_account_for(tenant_id),
                "execution_id": execution_id, "args_digest": args_digest,
                "expires_at": cm.hashed_time(self.clock() + timedelta(seconds=300)),
            }
            quote["quote_digest"] = cm.quote_digest(quote)
            self.quotes[quote["quote_id"]] = quote
            return _ok(dict(quote), 201)
        return self._route("mint_quote", op)

    def accept(self, tenant_id: str, *, quote_doc: dict, idempotency_key: str, approval_id: str,
               terms_binding_value: str) -> cm.CommerceAnswer:
        def op():
            assert cm.IDEMPOTENCY_KEY_PATTERN.match(idempotency_key)
            existing = self.by_key.get((tenant_id, idempotency_key))
            if existing is not None:
                inv = self.invocations[existing]
                if inv["quote_id"] != quote_doc["quote_id"]:
                    return _no(409, "idempotency-conflict")
                return _ok({**inv, "replayed": True})
            quote = self.quotes.get(quote_doc["quote_id"])
            if quote is None or quote["quote_digest"] != quote_doc["quote_digest"]:
                return _no(409, "quote-mismatch")
            if cm.parse_time(quote["expires_at"]) <= self.clock():
                return _no(409, "quote-expired")
            terms = self.terms.get((quote["capability_id"], quote["input_schema_hash"]))
            binding = cm.terms_binding(quote, tenant_id=tenant_id, payer_account_id=quote["payer_account_id"])
            if terms is None or terms["terms_version"] != quote["terms_version"] or (
                    terms_binding_value and terms_binding_value != binding):
                return _no(409, "quote-stale")
            if approval_id and any(i["approval_id"] == approval_id for i in self.invocations.values()):
                return _no(409, "policy-denied")
            inv_id = self._id("inv")
            inv = {
                **{k: quote[k] for k in ("quote_id", "quote_digest", "terms_id", "terms_version", "capability_id",
                                         "tenant_id", "principal_id", "execution_id", "args_digest",
                                         "price_mode", "price", "funds_mode", "payer_account_id")},
                "invocation_id": inv_id, "provider_id": terms["provider_id"], "idempotency_key": idempotency_key,
                "phase": "accepted", "execution_outcome": "not_dispatched", "charge_status": "none",
                "approval_id": approval_id, "provider_operation_key": cm.provider_operation_key(inv_id),
                "reason_code": "", "input_schema_hash": quote["input_schema_hash"],
            }
            self.invocations[inv_id] = inv
            self.by_key[(tenant_id, idempotency_key)] = inv_id
            return _ok({**inv, "replayed": False}, 201)
        return self._route("accept", op)

    def find_invocation(self, tenant_id: str, idempotency_key: str) -> cm.CommerceAnswer:
        def op():
            inv_id = self.by_key.get((tenant_id, idempotency_key))
            return _ok({"invocations": [dict(self.invocations[inv_id])] if inv_id else []})
        return self._route("find_invocation", op)

    def get_invocation(self, tenant_id: str, invocation_id: str) -> cm.CommerceAnswer:
        def op():
            inv = self.invocations.get(invocation_id)
            return _ok(dict(inv)) if inv else _no(404, "not-found")
        return self._route("get_invocation", op)

    def _move(self, inv: dict, to: str, reason: str = "") -> None:
        assert to in _EDGES[inv["phase"]], (inv["phase"], to)
        inv["phase"] = to
        if reason:
            inv["reason_code"] = reason

    def reserve(self, tenant_id: str, invocation_id: str) -> cm.CommerceAnswer:
        def op():
            inv = self.invocations[invocation_id]
            if inv["phase"] == "reserved":
                return _ok({"invocation": dict(inv), "reservation": {}, "replayed": True})
            if inv["phase"] == "refused":
                return _no(422, inv["reason_code"])
            if inv["phase"] != "accepted":
                return _no(409, "phase-conflict")
            if self.payment_unknown:
                return _no(409, "payment-unknown")
            amount = inv["price"]["amount_minor"]
            if self.budget is None or (self.budget["available"] - self.budget["reserved"]
                                       - self.budget["committed"]) < amount:
                self._move(inv, "refused", "budget-exhausted")
                return _no(422, "budget-exhausted")
            self.budget["reserved"] += amount
            inv["charge_status"] = "reserved"
            self._move(inv, "reserved")
            return _ok({"invocation": dict(inv), "reservation": {}, "replayed": False})
        return self._route("reserve", op)

    def dispatch(self, tenant_id: str, invocation_id: str) -> cm.CommerceAnswer:
        def op():
            inv = self.invocations[invocation_id]
            if self.dispatch_unavailable:
                return cm.CommerceAnswer(cm.UNKNOWN, 503, "dispatch-unavailable")
            if inv["phase"] == "dispatching" and invocation_id in self.auths:
                return _ok({"invocation": dict(inv), "authorization": self.auths[invocation_id], "replayed": True})
            if inv["phase"] != "reserved":
                return _no(409, "phase-conflict")
            deadline = cm.hashed_time(self.clock() + timedelta(seconds=60))
            auth = cm.sign_dispatch_authorization(SEED, cm.DispatchAuthorization(
                key_id=KID, invocation_id=invocation_id, provider_operation_key=inv["provider_operation_key"],
                provider_id=inv["provider_id"], capability_id=inv["capability_id"],
                input_schema_hash=inv["input_schema_hash"], args_digest=inv["args_digest"], tenant_id=tenant_id,
                dispatch_deadline=deadline,
            )).to_dict()
            self.auths[invocation_id] = auth
            inv["dispatch_deadline"] = deadline
            self._move(inv, "dispatching")
            return _ok({"invocation": dict(inv), "authorization": auth, "replayed": False})
        return self._route("dispatch", op)

    def transition(self, tenant_id: str, invocation_id: str, *, expected_phase: str, to_phase: str,
                   reason_code: str = "", note: str = "") -> cm.CommerceAnswer:
        def op():
            inv = self.invocations[invocation_id]
            if (expected_phase, to_phase) not in _GENERIC_EDGES:
                return _no(400, "invalid-request")
            if inv["phase"] != expected_phase:
                return _no(409, "phase-conflict")
            if to_phase == "refused" and not reason_code:
                return _no(400, "invalid-request")
            self._move(inv, to_phase, reason_code if to_phase in ("refused", "outcome_unknown") else "")
            return _ok(dict(inv))
        return self._route("transition", op)

    def finalize(self, tenant_id: str, invocation_id: str, *, execution_outcome: str) -> cm.CommerceAnswer:
        def op():
            inv = self.invocations[invocation_id]
            if inv["phase"] == "finalized":
                if inv["execution_outcome"] == execution_outcome:
                    return _ok({"invocation": dict(inv), "replayed": True})
                return _no(409, "phase-conflict")
            if inv["phase"] not in ("dispatched", "outcome_unknown"):
                return _no(409, "phase-conflict")
            amount = inv["price"]["amount_minor"]
            self.budget["reserved"] -= amount
            capture = execution_outcome == "succeeded" or inv["price_mode"] == "per_call"
            if capture:
                self.budget["committed"] += amount
            inv["charge_status"] = "captured" if capture else "released"
            inv["execution_outcome"] = execution_outcome
            self._move(inv, "finalized")
            return _ok({"invocation": dict(inv), "replayed": False})
        return self._route("finalize", op)

    def release(self, tenant_id: str, invocation_id: str, *, reason_code: str) -> cm.CommerceAnswer:
        def op():
            inv = self.invocations[invocation_id]
            if inv["phase"] not in ("reserved", "dispatching"):
                return _no(409, "phase-conflict")
            self.budget["reserved"] -= inv["price"]["amount_minor"]
            inv["charge_status"] = "released"
            self._move(inv, "released", reason_code)
            return _ok({"invocation": dict(inv), "replayed": False})
        return self._route("release", op)

    # -- assertions --------------------------------------------------------------

    def only(self) -> dict:
        assert len(self.invocations) == 1, self.invocations
        return next(iter(self.invocations.values()))


class FakeApprovals:
    """mctl-api's action-approval routes, with the real client's answers."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.by_key: dict[str, aa.ApprovalRecord] = {}
        self.by_id: dict[str, aa.ApprovalRecord] = {}
        self.consumes: list[str] = []
        self.lose_consume = 0

    def _put(self, rec: aa.ApprovalRecord) -> aa.ApprovalRecord:
        self.by_id[rec.id] = rec
        return rec

    def create(self, intent, *, key: str, expires_at: datetime) -> aa.ApprovalAnswer:
        rec = self.by_key.get(key)
        if rec is None:
            rec = self._put(aa.ApprovalRecord(f"aar_{len(self.by_id) + 1}", "pending", aa.intent_hash(intent),
                                              cm.hashed_time(expires_at)))
            self.by_key[key] = rec
        rec = self.by_id[rec.id]
        return aa.ApprovalAnswer(aa._STATES[rec.state], record=rec)

    def get(self, approval_id: str) -> aa.ApprovalAnswer:
        rec = self.by_id.get(approval_id)
        if rec is None:
            return aa.ApprovalAnswer(aa.NOT_FOUND)
        return aa.ApprovalAnswer(aa._STATES[rec.state], record=rec)

    def decide(self, approval_id: str, state: str) -> None:
        rec = self.by_id[approval_id]
        self._put(dataclasses.replace(rec, state=state, decided_by="prn_human"))

    def consume(self, approval_id: str, presented_hash: str) -> aa.ApprovalAnswer:
        self.consumes.append(approval_id)
        rec = self.by_id.get(approval_id)
        if rec is None:
            return aa.ApprovalAnswer(aa.NOT_FOUND)
        if rec.intent_hash != presented_hash:
            return aa.ApprovalAnswer(aa.MISMATCH)
        if rec.state != "approved":
            return aa.ApprovalAnswer(aa._STATES[rec.state] if rec.state != "approved" else aa.PENDING)
        if cm.parse_time(rec.expires_at) <= self.clock():
            return aa.ApprovalAnswer(aa.EXPIRED)
        spent = self._put(dataclasses.replace(rec, state="consumed"))
        if self.lose_consume:
            self.lose_consume -= 1
            return aa.ApprovalAnswer(aa.UNKNOWN, reason="lost")
        return aa.ApprovalAnswer(aa.SPENT, record=spent)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _correlation() -> ExecutionCorrelation:
    return ExecutionCorrelation(
        agent="issue-investigator", environment="production", temporal_workflow_id="wf-619",
        target_repository_sha="a" * 40, definition_version="3", definition_content_hash="sha256:" + "1a" * 32,
        profile_version="5", profile_content_hash="sha256:" + "2b" * 32, release_revision=12,
    )


class FreeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def list_tools(self):
        return [gw.ProviderTool(name="echo", description="Echo.", input_schema={"type": "object"})]

    async def call_tool(self, tool, arguments):
        self.calls.append(dict(arguments))
        return gw.ToolCallResult(is_error=False, text="echo")


@dataclasses.dataclass
class World:
    clock: Clock
    ledger: FakeLedger
    approvals: FakeApprovals
    tmp: Any
    statuses: dict
    executions: list = dataclasses.field(default_factory=list)
    gate: asyncio.Event | None = None
    entered: asyncio.Event | None = None
    policy: pc.Policy = ALLOW_PAID
    admitted: frozenset = frozenset({cp.PILOT_PROVIDER_ID})
    ingress: cp.ProviderIngress | None = None
    free: FreeSession = dataclasses.field(default_factory=FreeSession)
    traces: list = dataclasses.field(default_factory=list)

    def new_ingress(self) -> cp.ProviderIngress:
        """A (re)started provider on the same durable store."""
        world = self
        ingress = cp.pilot_lookup_status_ingress(
            public_keys={KID: cm.public_key_for(SEED)}, db_path=str(self.tmp / "pilot.db"),
            statuses=self.statuses, tenant_id=TENANT, now=self.clock,
        )
        spec = ingress._tools[cp.PILOT_TOOL]
        inner = spec.handler

        async def counted(arguments):
            world.executions.append(dict(arguments))
            if world.entered is not None:
                world.entered.set()
            if world.gate is not None:
                await world.gate.wait()
            return await inner(arguments)

        ingress._tools[cp.PILOT_TOOL] = dataclasses.replace(spec, handler=counted)
        self.ingress = ingress
        return ingress

    def schema_hash(self) -> str:
        return cp.PaidTool("x", "", cp.PILOT_INPUT_SCHEMA, None).input_schema_hash  # type: ignore[arg-type]

    async def gateway(self, *, commerce: bool = True) -> gw.CapabilityGateway:
        ingress = self.new_ingress()
        runtime = cg.CommerceRuntime(
            client=self.ledger, tenant_id=TENANT, admitted_providers=self.admitted,  # type: ignore[arg-type]
            approvals=self.approvals, now=self.clock, call_timeout_s=0.5,  # type: ignore[arg-type]
        )
        return await gw.CapabilityGateway.build(
            SimpleNamespace(tools=("mcp__pilot__*", "mcp__free__*")), _correlation(), [PILOT, FREE],
            checkpoint=gw.PolicyDecidePolicyCheckpoint(grants=GRANTS, policy=self.policy),
            local_sessions={cp.PILOT_PROVIDER_ID: cp.PaidLocalProviderSession(ingress), "free-provider": self.free},
            headers={}, consequence_table={}, created_at="2026-10-11T12:00:00Z",
            commerce=runtime if commerce else None,
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


def run(coro):
    return asyncio.run(coro)


async def _invoke(world: World, arguments=None, *, key=None, gateway=None):
    g = gateway or await world.gateway()
    return await g.invoke(PILOT_CAP, {"ticket": "OPS-1"} if arguments is None else arguments, key)


def _restart(world: World, arguments=None, *, key=None):
    """A fresh process: new gateway, new provider object, same ledger and
    same provider store."""
    return run(_invoke(world, arguments, key=key))


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
