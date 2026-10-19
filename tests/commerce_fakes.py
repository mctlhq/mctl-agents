"""Shared fakes for the ADR 020 commerce suite and the C5 proof harness
(mctlhq/mctl-agents#619, #620): `FakeLedger`, `FakeApprovals`, `Clock` and the
`World` harness. Moved out of tests/test_commerce_gateway.py unchanged.

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
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

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
                 budget: int | None = 100, id_seed: int = 0) -> None:
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
        #: mctl-api's accept-only kill switch (mctlhq/mctl-api#570): new
        #: acceptances are refused `unsupported-execution`; replays, reserve,
        #: dispatch, finalize, release and the reconciler keep working.
        self.accepting = True
        #: The next N captures leave `settlement_status: pending` (the
        #: simulated authorizer answered `unknown`); `settle_pending` retries.
        self.settlement_unknown = 0
        self.tenant = TENANT
        self.dispatch_unavailable = False
        self._n = id_seed
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
        return [c for c in self.calls if c not in (
            "current_terms", "find_invocation", "get_invocation", "precheck", "get_budget", "get_receipt",
            "list_invocations")]

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
            if self.budget is None or tenant_id != self.tenant:
                return _no(404, "not-found")
            free = self.budget["available"] - self.budget["reserved"] - self.budget["committed"]
            return _ok({"sufficient": free >= amount_minor, "reason_code": "", "balance": dict(self.budget)})
        return self._route("precheck", op)

    def mint_quote(self, tenant_id: str, *, capability_id: str, input_schema_hash: str, args_digest: str,
                   execution_id: str) -> cm.CommerceAnswer:
        def op():
            if not self.accepting:
                # mctl-api#570: COMMERCE_ACCEPT_DISABLED refuses new quotes too.
                return _no(422, "unsupported-execution")
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
            if not self.accepting:
                return _no(422, "unsupported-execution")
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
                "settlement_status": "none",
                "approval_id": approval_id, "provider_operation_key": cm.provider_operation_key(inv_id),
                "reason_code": "", "input_schema_hash": quote["input_schema_hash"],
                "created_at": cm.hashed_time(self.clock()),
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
            return _ok(dict(inv)) if inv and inv["tenant_id"] == tenant_id else _no(404, "not-found")
        return self._route("get_invocation", op)

    def _owned(self, tenant_id: str, invocation_id: str) -> dict | None:
        """The invocation, only for the tenant that owns it: a foreign
        tenant's id and a missing one are the same `None`."""
        inv = self.invocations.get(invocation_id)
        return inv if inv is not None and inv["tenant_id"] == tenant_id else None

    def _move(self, inv: dict, to: str, reason: str = "") -> None:
        assert to in _EDGES[inv["phase"]], (inv["phase"], to)
        inv["phase"] = to
        if reason:
            inv["reason_code"] = reason

    def reserve(self, tenant_id: str, invocation_id: str) -> cm.CommerceAnswer:
        def op():
            inv = self._owned(tenant_id, invocation_id)
            if inv is None:
                return _no(404, "not-found")
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
            inv = self._owned(tenant_id, invocation_id)
            if inv is None:
                return _no(404, "not-found")
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
            inv = self._owned(tenant_id, invocation_id)
            if inv is None:
                return _no(404, "not-found")
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
            inv = self._owned(tenant_id, invocation_id)
            if inv is None:
                return _no(404, "not-found")
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
            if capture and self.settlement_unknown:
                self.settlement_unknown -= 1
                inv["settlement_status"] = "pending"
            elif capture:
                inv["settlement_status"] = "settled"
            inv["execution_outcome"] = execution_outcome
            self._move(inv, "finalized")
            return _ok({"invocation": dict(inv), "replayed": False})
        return self._route("finalize", op)

    def release(self, tenant_id: str, invocation_id: str, *, reason_code: str) -> cm.CommerceAnswer:
        def op():
            inv = self._owned(tenant_id, invocation_id)
            if inv is None:
                return _no(404, "not-found")
            if inv["phase"] not in ("reserved", "dispatching"):
                return _no(409, "phase-conflict")
            self.budget["reserved"] -= inv["price"]["amount_minor"]
            inv["charge_status"] = "released"
            self._move(inv, "released", reason_code)
            return _ok({"invocation": dict(inv), "replayed": False})
        return self._route("release", op)

    # -- tenant reads (mctl-api#569) and the reconciler --------------------------

    def free(self) -> int:
        assert self.budget is not None
        return self.budget["available"] - self.budget["reserved"] - self.budget["committed"]

    def get_budget(self, tenant_id: str) -> cm.CommerceAnswer:
        """mctl-api `commerce.Balance`, parsed by the production client's
        `BudgetView` so a drift between the two fails here, not live."""
        def op():
            if self.budget is None or tenant_id != self.tenant:
                return _no(404, "not-found")
            # mctl-api counts only held reservations whose outcome is unknown.
            pending = sum(i["price"]["amount_minor"] for i in self.invocations.values()
                          if i["phase"] == "outcome_unknown")
            return _ok({"schema_version": 1, "tenant_id": tenant_id, "payer_account_id": f"pa_{tenant_id}",
                        "currency": "EUR", "funds_mode": "simulated", "period": "2026-10",
                        "monthly_limit_minor": self.budget["available"], "per_request_max_minor": 100,
                        "committed_minor": self.budget["committed"], "reserved_minor": self.budget["reserved"],
                        "pending_liability_minor": pending, "available_minor": self.free(),
                        "as_of": "2026-10-11T00:00:00Z"})
        return cm._parsed(self._route("get_budget", op), lambda body: cm.BudgetView.from_dict(body).to_dict())

    def _row(self, inv: dict) -> dict:
        """One mctl-api `commerce.Invocation` row as the list endpoint sends
        it, including fields the client must not copy."""
        return {
            "schema_version": 1, **{k: inv[k] for k in (
                "invocation_id", "capability_id", "terms_id", "terms_version", "quote_id", "quote_digest",
                "price_mode", "funds_mode", "provider_id", "execution_outcome", "charge_status",
                "settlement_status", "phase", "reason_code", "approval_id", "execution_id", "price",
                "tenant_id", "created_at")},
            **({"usage_quantity": 1} if inv["charge_status"] == "captured" else {}),
            "payment_status": "authorized" if inv["charge_status"] in ("reserved", "captured") else "none",
            "idempotency_key": inv.get("idempotency_key", ""), "args_digest": inv.get("args_digest", ""),
            "provider_operation_key": inv.get("provider_operation_key", ""),
        }

    def get_receipt(self, tenant_id: str, invocation_id: str) -> cm.CommerceAnswer:
        """mctl-api#569 `commerce.Receipt`, parsed by `ReceiptView`."""
        def op():
            inv = self.invocations.get(invocation_id)
            if inv is None or inv["tenant_id"] != tenant_id or tenant_id != self.tenant:
                return _no(404, "not-found")
            policy = ({"status": "recorded", "approval_id": inv["approval_id"]} if inv["approval_id"]
                      else {"status": "none"})
            return _ok({**self._row(inv), "policy_decision": policy})
        return cm._parsed(self._route("get_receipt", op), lambda body: cm.ReceiptView.from_dict(body).to_dict())

    def list_invocations(self, tenant_id: str, *, capability_id: str | None = None, phase: str | None = None,
                         since: str | None = None, limit: int = 50) -> cm.CommerceAnswer:
        def op():
            if tenant_id != self.tenant:
                return _no(404, "not-found")
            rows = [self._row(i) for i in self.invocations.values() if i["tenant_id"] == tenant_id
                    and (not capability_id or i["capability_id"] == capability_id)
                    and (not phase or i["phase"] == phase)
                    and (not since or cm.parse_time(i["created_at"]) >= cm.parse_time(since))]
            return _ok({"invocations": rows[:limit]})

        def parse(body):
            return {"invocations": [cm.ReceiptView.from_dict(row).to_dict() for row in body["invocations"]]}
        return cm._parsed(self._route("list_invocations", op), parse)

    def settle_pending(self) -> int:
        """Settlement-only retry on the same derived key: moves no funds a
        second time and never touches a provider."""
        with self.lock:
            n = 0
            for inv in self.invocations.values():
                if inv.get("settlement_status") == "pending":
                    inv["settlement_status"] = "settled"
                    n += 1
            return n

    def reconcile(self, outcome_of: Callable[[str], str | None]) -> int:
        """mctl-api's reconciler: resolve every `outcome_unknown` invocation
        from the provider's own outcome record. `outcome_of(operation_key)`
        answers `succeeded`, `failed` or None (still unknown)."""
        resolved = 0
        for inv in list(self.invocations.values()):
            if inv["phase"] != "outcome_unknown":
                continue
            outcome = outcome_of(inv["provider_operation_key"])
            if outcome in ("succeeded", "failed") and self.finalize(
                    inv["tenant_id"], inv["invocation_id"], execution_outcome=outcome).ok:
                resolved += 1
        return resolved

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
        #: The decision context each create sent (mctl-api#700).
        self.contexts: list[aa.DecisionContext | None] = []

    def _put(self, rec: aa.ApprovalRecord) -> aa.ApprovalRecord:
        self.by_id[rec.id] = rec
        return rec

    def create(self, intent, *, key: str, expires_at: datetime,
               context: aa.DecisionContext | None = None) -> aa.ApprovalAnswer:
        self.contexts.append(context)
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
    tenant: str = TENANT
    public_keys: dict | None = None

    def new_ingress(self) -> cp.ProviderIngress:
        """A (re)started provider on the same durable store."""
        world = self
        ingress = cp.pilot_lookup_status_ingress(
            public_keys=self.public_keys or {KID: cm.public_key_for(SEED)}, db_path=str(self.tmp / "pilot.db"),
            statuses=self.statuses, tenant_id=self.tenant, now=self.clock,
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
            client=self.ledger, tenant_id=self.tenant, admitted_providers=self.admitted,  # type: ignore[arg-type]
            approvals=self.approvals, now=self.clock, call_timeout_s=0.5,  # type: ignore[arg-type]
        )
        return await gw.CapabilityGateway.build(
            SimpleNamespace(tools=("mcp__pilot__*", "mcp__free__*")), _correlation(), [PILOT, FREE],
            checkpoint=gw.PolicyDecidePolicyCheckpoint(grants=GRANTS, policy=self.policy),
            local_sessions={cp.PILOT_PROVIDER_ID: cp.PaidLocalProviderSession(ingress), "free-provider": self.free},
            headers={}, consequence_table={}, created_at="2026-10-11T12:00:00Z",
            commerce=runtime if commerce else None,
        )


def run(coro):
    return asyncio.run(coro)


async def _invoke(world: World, arguments=None, *, key=None, gateway=None):
    g = gateway or await world.gateway()
    return await g.invoke(PILOT_CAP, {"ticket": "OPS-1"} if arguments is None else arguments, key)


def _restart(world: World, arguments=None, *, key=None):
    """A fresh process: new gateway, new provider object, same ledger and
    same provider store."""
    return run(_invoke(world, arguments, key=key))


def make_world(tmp_path: Any, monkeypatch: Any) -> World:
    """The `world` fixture's body, for suites that cannot import a fixture."""
    clock = Clock()
    w = World(clock=clock, ledger=FakeLedger(clock), approvals=FakeApprovals(clock), tmp=tmp_path,
              statuses={f"OPS-{i}": "open" for i in range(1, 60)} | {"OPS-2": "closed"})
    w.ledger.publish(PILOT_CAP, w.schema_hash())
    monkeypatch.setattr(pc, "current_identity",
                        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-1", actor="agent:test"))
    monkeypatch.setattr(aa, "ttl_s", lambda: 3600)
    return w
