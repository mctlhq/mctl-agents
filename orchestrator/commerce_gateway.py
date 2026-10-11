"""The paid branch of `capability_invoke` (ADR 020 secs. 7-9 and 11,
mctlhq/mctl-agents#619, C4).

`CapabilityGateway.invoke` hands a capability whose descriptor carries a
paid `commerce` summary to `PaidInvocation`. Nothing here holds state:
mctl-api's ledger is the only record of an invocation, and every step reads
it back, so a restarted or concurrent retry of the same call resumes the
invocation the first attempt opened instead of opening a second one.

The order is ADR 020 sec. 7's:

- steps 2-3: sealed-set membership and the policy checkpoint over the call's
  arguments PLUS `commerce_terms_binding` (sec. 8), so an approval binds the
  price, the payer and the capability version as well as the arguments;
- step 4: the current terms, the binding and a non-binding budget check;
- step 5: exact-action approval. The request is created or read, NEVER
  consumed here. No quote exists and no funds are held while it waits;
- step 6: mint and verify the quote, accept the invocation under a
  deterministic idempotency key, naming the approval;
- step 7: re-check policy, then consume the approval for that invocation;
- step 8: reserve;
- step 9: fence and sign the dispatch in mctl-api, then call the provider
  through its ingress with that authorization;
- steps 10-11: record what the provider says, by its result or by
  `outcome(key)`, and capture or release exactly once.

What could not be observed is never treated as absent: a terms read that
fails refuses the call, an outcome lookup that fails leaves the invocation
where it is and answers `outcome-unknown`, and only `not_found` after
`dispatch_deadline + max_clock_skew` releases a dispatched reservation.
Nothing is ever dispatched twice: a dispatch that mctl-api answers as a
replay goes to the outcome lookup, never to the provider.

No real money: mctl-api refuses `funds_mode: live` (C3), and this module
has no payment rail to call.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from orchestrator import action_approvals as aa
from orchestrator import commerce as cm
from orchestrator import policy_checkpoint as pc
from orchestrator.capability import REASON_CODES, CapabilityDescriptor

if TYPE_CHECKING:
    from orchestrator.capability_gateway import ToolCallResult

#: The ledger phases (ADR 020 sec. 11).
ACCEPTED, REFUSED, RESERVED = "accepted", "refused", "reserved"
DISPATCHING, DISPATCHED, OUTCOME_UNKNOWN = "dispatching", "dispatched", "outcome_unknown"
RELEASED, FINALIZED = "released", "finalized"
TERMINAL_PHASES = frozenset({REFUSED, RELEASED, FINALIZED})

#: How many times one call re-reads the ledger after a phase conflict before
#: it reports the state it could not settle. A conflict means another
#: attempt moved the row; a handful of re-reads always reaches either a
#: phase this attempt can act on or one it must leave alone.
_MAX_STEPS = 12

DEFAULT_CALL_TIMEOUT_S = 120


@runtime_checkable
class PaidProviderSession(Protocol):
    """A provider that meets ADR 020 sec. 9: it verifies a signed dispatch
    authorization at its own ingress and answers `outcome(key)`.
    `call_paid_tool` raises `commerce.DispatchRefused` only when the ingress
    refused before accepting; `outcome` raises `commerce.OutcomeUnavailable`
    when it cannot answer."""

    async def list_tools(self) -> list[Any]: ...

    async def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> ToolCallResult: ...

    async def call_paid_tool(
        self, tool: str, arguments: Mapping[str, Any], authorization: cm.DispatchAuthorization,
    ) -> ToolCallResult: ...

    async def outcome(self, provider_operation_key: str) -> cm.ProviderOutcome: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CommerceRuntime:
    """What a gateway needs to serve paid capabilities. Without one, every
    capability is legacy at discovery and a paid descriptor is refused with
    `commerce-terms-unknown` at invocation (fail closed).

    `admitted_providers` is the sec. 9 admission list: a provider id is on it
    only once its ingress has passed C4's fault tests. A paid capability of
    any other provider is refused with `unsupported-execution` before
    anything is created."""

    client: cm.CommerceClient
    tenant_id: str
    admitted_providers: frozenset[str] = frozenset()
    #: The #366 approval store. None: a paid call that policy routes to
    #: approval is refused, never run without one.
    approvals: aa.ActionApprovalClient | None = None
    now: Callable[[], datetime] = _utcnow
    max_clock_skew_s: int = cm.MAX_CLOCK_SKEW_S
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S

    @property
    def payer_account_id(self) -> str:
        return cm.payer_account_for(self.tenant_id)


@dataclass
class PaidResult:
    """The answer `capability_invoke` returns, plus what the trace records."""

    reason_code: str
    text: str | None = None
    error: str = ""
    invocation_id: str = ""
    approval_ref: str = ""
    phase: str = ""
    policy_checkpoint: str = "denied"
    result_hash_source: str | None = None

    def __post_init__(self) -> None:
        if self.reason_code not in REASON_CODES:
            raise ValueError(f"unknown reason code {self.reason_code!r}")

    def to_answer(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reason_code": self.reason_code}
        if self.text is not None:
            out["text"] = self.text
        if self.error:
            out["error"] = self.error
        if self.invocation_id:
            out["invocation_id"] = self.invocation_id
        if self.approval_ref:
            out["approval_ref"] = self.approval_ref
        return out


_LEDGER_UNREACHABLE = "the commerce ledger could not be reached; the call's state is unknown"


def _code(raw: str, fallback: str) -> str:
    return raw if raw in REASON_CODES else fallback


@dataclass
class PaidInvocation:
    """One paid `capability_invoke`. Built per call; never reused."""

    runtime: CommerceRuntime
    descriptor: CapabilityDescriptor
    session: Any
    tool: str
    arguments: Mapping[str, Any]
    grants: tuple[str, ...]
    policy: pc.Policy
    client_key: str = ""
    emit: Callable[[Mapping[str, Any], str], None] = lambda payload, event: None

    # set while running
    _binding: str = ""
    _request: pc.ActionRequest | None = None
    _decision: pc.Decision | None = None
    _authorization: cm.DispatchAuthorization | None = None
    _result: ToolCallResult | None = None
    _approval_ref: str = ""
    #: The invocation being resumed already spent its approval in an
    #: earlier attempt (it is past `accepted`).
    _approved_earlier: bool = False
    #: The approval record this attempt consumed, for the audit line.
    _approval_record: aa.ApprovalRecord | None = None
    #: A row resumed without terms (`_terms_refused`) is past the
    #: pre-dispatch recheck of an earlier attempt.
    _past_recheck: bool = False
    _trace: dict[str, Any] = field(default_factory=dict)

    # -- plumbing ------------------------------------------------------------

    async def _ledger(self, method: str, *args: Any, **kwargs: Any) -> cm.CommerceAnswer:
        return await asyncio.to_thread(getattr(self.runtime.client, method), *args, **kwargs)

    @property
    def _tenant(self) -> str:
        return self.runtime.tenant_id

    def _refusal(self, code: str, error: str, **extra: Any) -> PaidResult:
        return PaidResult(code, error=error, policy_checkpoint=self.policy_status(), **extra)

    def policy_status(self) -> str:
        d = self._decision
        if d is None:
            # No decision this attempt: a row resumed without terms reports
            # the verdict of the attempt that took it past the recheck.
            return "allowed" if self._past_recheck else "denied"
        if d.code in (pc.CODE_ALLOWED, pc.CODE_APPROVED):
            return "allowed"
        return "allowed" if d.code == pc.CODE_APPROVAL_REQUIRED and self._approved_earlier else "denied"

    def _note(self, inv: Mapping[str, Any], event: str) -> None:
        # Ids, phases and reason codes only: never arguments, results or money.
        self.emit({
            "capability_id": self.descriptor.capability_id,
            "invocation_id": inv.get("invocation_id", ""),
            "phase": inv.get("phase", ""),
            "reason_code": inv.get("reason_code", ""),
            "execution_outcome": inv.get("execution_outcome", ""),
        }, event)

    # -- policy ----------------------------------------------------------------

    def _decide(self) -> pc.Decision | PaidResult:
        """`request_for` + `decide` over the arguments plus the terms
        binding, with no approval store: an approval is handled separately,
        because `decide` would consume it before the invocation exists."""
        checked = {**self.arguments, cm.TERMS_BINDING_ARGUMENT: self._binding}
        request = pc.request_for(
            pc.MCP_TOOL_CALL, self.descriptor.tool_name, self.descriptor.provider.alias, checked,
            grants=self.grants, policy=self.policy,
            metadata={"capability_id": self.descriptor.capability_id, "commerce": "paid"},
        )
        if isinstance(request, pc.Decision):
            self._decision = request
            code = "identity-unresolved" if request.code == pc.CODE_IDENTITY_UNAVAILABLE else "policy-denied"
            return self._refusal(code, request.reason)
        if not request.execution_id:
            self._decision = pc.Decision(pc.DENY, pc.CODE_IDENTITY_UNAVAILABLE, "no execution identity",
                                         self.policy.version, "", "")
            return self._refusal("identity-unresolved", "a paid call needs a resolved execution identity")
        self._request = request
        decision = pc.decide(request, policy=self.policy, approvals=pc.NO_APPROVALS)
        self._decision = decision
        return decision

    def _decision_permits(self, decision: pc.Decision) -> bool:
        return decision.code in (pc.CODE_ALLOWED, pc.CODE_APPROVAL_REQUIRED)

    def _intent(self) -> aa.ActionIntent:
        if not (self._request is not None and self._decision is not None):
            raise RuntimeError("the policy request was not built")
        return aa.intent_for(self._request, rule_id=self._decision.rule_id,
                             policy_version=self._decision.policy_version)

    # -- entry -----------------------------------------------------------------

    async def run(self) -> PaidResult:
        if cm.TERMS_BINDING_ARGUMENT in self.arguments:
            return self._refusal("invalid-arguments", f"{cm.TERMS_BINDING_ARGUMENT} is reserved")

        # Step 4 (terms): resolved now, from mctl-api, never from the sealed
        # summary. Unknown or unreadable terms refuse: never treated as free.
        terms = await self._ledger("current_terms", self.descriptor.capability_id,
                                   self.descriptor.input_schema_hash)
        if not terms.ok:
            return await self._terms_refused("the terms in force could not be resolved")
        doc = terms.body
        if (doc.get("capability_id") != self.descriptor.capability_id
                or doc.get("input_schema_hash") != self.descriptor.input_schema_hash
                or doc.get("provider_id") != self.descriptor.provider.id):
            return await self._terms_refused("the terms in force name another capability version")
        if doc.get("price_mode") == "free":
            return await self._terms_refused("the terms in force changed to free; rediscover")
        try:
            self._binding = cm.terms_binding(doc, tenant_id=self._tenant,
                                             payer_account_id=self.runtime.payer_account_id)
            price = doc["price"]
            currency, amount = price["currency"], price["amount_minor"]
        except (cm.CommerceError, KeyError, TypeError):
            return await self._terms_refused("the terms in force are malformed")

        # Step 3: the checkpoint, over the arguments AND the binding.
        decided = self._decide()
        if isinstance(decided, PaidResult):
            return decided
        if self._request is None:
            raise RuntimeError("the policy request was not built")
        try:
            arguments_digest = cm.args_digest(self.arguments)
        except Exception:  # noqa: BLE001
            return self._refusal("invalid-arguments", "arguments cannot be digested")
        key = cm.gateway_idempotency_key(
            execution_id=self._request.execution_id, capability_id=self.descriptor.capability_id,
            arguments_digest=arguments_digest, client_key=self.client_key,
        )

        # Resume: the same call in the same execution already has a row.
        found = await self._find(key, arguments_digest, self._request.execution_id)
        if isinstance(found, PaidResult):
            return found
        if found is not None:
            return await self._advance(found)

        if not self._decision_permits(decided):
            return self._refusal("policy-denied", decided.reason)

        # Step 4 (budget): non-binding; the reservation is what binds. It
        # runs before the approval so an exhausted budget never opens a
        # human request that could not be honoured.
        pre = await self._ledger("precheck", self._tenant, currency=currency, amount_minor=amount)
        if pre.status == cm.UNKNOWN:
            return self._refusal("outcome-unknown", _LEDGER_UNREACHABLE)
        if not pre.ok or pre.body.get("sufficient") is not True:
            return self._refusal("budget-exhausted", "the tenant budget cannot cover this call")

        # Step 5: approval, read or created, never consumed here.
        approval_id = ""
        if decided.code == pc.CODE_APPROVAL_REQUIRED:
            checked = await self._await_approval()
            if isinstance(checked, PaidResult):
                return checked
            approval_id = checked

        # Step 6: quote, verified locally, then accept.
        quote = await self._ledger(
            "mint_quote", self._tenant, capability_id=self.descriptor.capability_id,
            input_schema_hash=self.descriptor.input_schema_hash, args_digest=arguments_digest,
            execution_id=self._request.execution_id,
        )
        if quote.status == cm.UNKNOWN:
            return self._refusal("outcome-unknown", _LEDGER_UNREACHABLE)
        if not quote.ok:
            return self._refusal(_code(quote.code, "commerce-terms-unknown"), "no quote could be minted")
        checked_quote = self._verify_quote(quote.body, arguments_digest)
        if isinstance(checked_quote, PaidResult):
            return checked_quote

        accepted = await self._ledger(
            "accept", self._tenant, quote_doc=quote.body, idempotency_key=key, approval_id=approval_id,
            terms_binding_value=self._binding,
        )
        if accepted.ok:
            # A replayed accept hands back an existing row: it gets the same
            # identity check a row found by key does.
            other = self._names_another_call(accepted.body, arguments_digest, self._request.execution_id)
            if other is not None:
                return other
            self._note(accepted.body, "commerce_accepted")
            return await self._advance(dict(accepted.body))
        # A concurrent attempt accepted first (idempotency-conflict), or the
        # answer was lost: the ledger says which.
        if accepted.status == cm.UNKNOWN or accepted.code == "idempotency-conflict":
            again = await self._find(key, arguments_digest, self._request.execution_id)
            if isinstance(again, PaidResult):
                return again
            if again is not None:
                return await self._advance(again)
            if accepted.status == cm.UNKNOWN:
                return self._refusal("outcome-unknown", _LEDGER_UNREACHABLE)
        return self._refusal(_code(accepted.code, "policy-denied"), "the invocation was not accepted",
                             approval_ref=approval_id)

    async def _terms_refused(self, why: str) -> PaidResult:
        """The terms in force cannot be used. A first attempt is refused with
        nothing created. A retry may find this call's row already in flight:
        a row past dispatch is settled from the provider's record, which needs
        no terms and no policy decision, and a row before dispatch is named in
        the refusal instead of reading as "nothing happened". Neither is
        advanced toward dispatch: that needs a recheck over the terms."""
        refusal = self._refusal("commerce-terms-unknown", why)
        try:
            execution_id = pc.current_identity().execution_id
            arguments_digest = cm.args_digest(self.arguments)
        except Exception:  # noqa: BLE001 — no key can be built, so no row can be named
            return refusal
        if not execution_id:
            return refusal
        key = cm.gateway_idempotency_key(
            execution_id=execution_id, capability_id=self.descriptor.capability_id,
            arguments_digest=arguments_digest, client_key=self.client_key,
        )
        found = await self._find(key, arguments_digest, execution_id)
        if found is None:
            return refusal
        if isinstance(found, PaidResult):
            # The ledger could not be read, or holds a row naming another
            # call: both outrank "the terms are unknown".
            return found
        phase = found.get("phase")
        if phase in (ACCEPTED, RESERVED):
            refusal.invocation_id = str(found.get("invocation_id", ""))
            refusal.phase = str(phase)
            refusal.error = f"{why}; this call's invocation is {phase} and was not advanced"
            return refusal
        # Past the pre-dispatch recheck: that attempt's verdict stands.
        self._past_recheck = _past_recheck(found)
        return await self._advance(found)

    def _names_another_call(self, row: Mapping[str, Any], arguments_digest: str,
                            execution_id: str) -> PaidResult | None:
        """A row is resumed only if it names exactly this call: the same
        identity checks a fresh quote gets."""
        expected = {
            "capability_id": self.descriptor.capability_id, "args_digest": arguments_digest,
            "execution_id": execution_id, "tenant_id": self._tenant,
        }
        if any(row.get(field) != value for field, value in expected.items()):
            return self._refusal("outcome-unknown", "the ledger's record for this key names another call",
                                 invocation_id=str(row.get("invocation_id", "")))
        return None

    async def _find(self, key: str, arguments_digest: str,
                    execution_id: str) -> dict[str, Any] | PaidResult | None:
        ans = await self._ledger("find_invocation", self._tenant, key)
        if not ans.ok:
            # Could not observe whether a row exists: never "absent".
            return self._refusal("outcome-unknown", _LEDGER_UNREACHABLE)
        rows = ans.body.get("invocations")
        if not isinstance(rows, list):
            return self._refusal("outcome-unknown", _LEDGER_UNREACHABLE)
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0], dict):
            return self._refusal("outcome-unknown", "the ledger holds an ambiguous record for this call")
        row = rows[0]
        other = self._names_another_call(row, arguments_digest, execution_id)
        return other if other is not None else row

    def _verify_quote(self, quote: Mapping[str, Any], arguments_digest: str) -> PaidResult | None:
        if self._request is None:
            raise RuntimeError("the policy request was not built")
        try:
            digest = cm.quote_digest(quote)
        except cm.CommerceError:
            return self._refusal("quote-mismatch", "the quote is malformed")
        if digest != quote.get("quote_digest"):
            return self._refusal("quote-mismatch", "the quote digest does not reproduce")
        if (quote.get("capability_id") != self.descriptor.capability_id
                or quote.get("input_schema_hash") != self.descriptor.input_schema_hash
                or quote.get("args_digest") != arguments_digest
                or quote.get("execution_id") != self._request.execution_id
                or quote.get("tenant_id") != self._tenant
                or quote.get("payer_account_id") != self.runtime.payer_account_id):
            return self._refusal("quote-mismatch", "the quote names another call")
        try:
            rebound = cm.terms_binding(quote, tenant_id=self._tenant, payer_account_id=self.runtime.payer_account_id)
        except cm.CommerceError:
            return self._refusal("quote-mismatch", "the quote is malformed")
        if rebound != self._binding:
            # The price changed between the approval/check and the quote.
            return self._refusal("quote-stale", "the price changed; the call was not accepted")
        return None

    async def _await_approval(self) -> str | PaidResult:
        """ADR 020 sec. 8, step 5: find or open the approval request for
        this exact intent (arguments + terms binding). Returns its id only
        when it is approved and unexpired; consumes nothing."""
        store = self.runtime.approvals
        if store is None:
            return self._refusal("policy-denied", "this call needs an approval and no approval store is configured")
        intent = self._intent()
        fresh = aa.intent_hash(intent)
        answer = await asyncio.to_thread(
            store.create, intent, key=aa.idempotency_key(intent, 0),
            expires_at=self.runtime.now() + timedelta(seconds=aa.ttl_s()),
        )
        rec = answer.record
        if rec is None:
            return self._refusal("policy-denied", "the approval store could not be read")
        self._approval_ref = rec.id
        if rec.intent_hash != fresh:
            return self._refusal("policy-denied", "the approval is bound to another action", approval_ref=rec.id)
        if answer.status == aa.PENDING:
            return self._refusal("policy-denied", "awaiting human approval; nothing is held", approval_ref=rec.id)
        if answer.status != aa.APPROVED:
            return self._refusal("policy-denied", f"approval {answer.status}", approval_ref=rec.id)
        expires = aa.parse_time(rec.expires_at)
        if expires is None or expires <= self.runtime.now():
            return self._refusal("policy-denied", "approval expired", approval_ref=rec.id)
        return rec.id

    # -- the state machine -------------------------------------------------------

    async def _advance(self, inv: dict[str, Any]) -> PaidResult:
        for _ in range(_MAX_STEPS):
            phase = inv.get("phase")
            if _approval_spent(inv):
                # Past reserved, the approval was consumed and the policy
                # rechecked by the attempt that moved the row: a resumed or
                # replayed call reports that verdict, not "denied".
                self._approved_earlier = True
            if phase in TERMINAL_PHASES:
                return await self._settled(inv)
            if phase == ACCEPTED:
                nxt = await self._from_accepted(inv)
            elif phase == RESERVED:
                nxt = await self._from_reserved(inv)
            elif phase in (DISPATCHING, DISPATCHED, OUTCOME_UNKNOWN):
                nxt = await self._from_dispatch(inv)
            else:
                return self._refusal("outcome-unknown", f"unknown ledger phase {phase!r}",
                                     invocation_id=str(inv.get("invocation_id", "")))
            if isinstance(nxt, PaidResult):
                return nxt
            inv = nxt
        return self._refusal("outcome-unknown", "the invocation did not settle",
                             invocation_id=str(inv.get("invocation_id", "")), phase=str(inv.get("phase", "")))

    async def _reload(self, inv: Mapping[str, Any]) -> dict[str, Any] | PaidResult:
        ans = await self._ledger("get_invocation", self._tenant, inv["invocation_id"])
        if not ans.ok:
            return self._refusal("outcome-unknown", _LEDGER_UNREACHABLE, invocation_id=inv["invocation_id"],
                                 phase=str(inv.get("phase", "")))
        return dict(ans.body)

    async def _recheck(self, inv: Mapping[str, Any]) -> PaidResult | None:
        """Step 7: policy, re-decided now. A revocation stops the call
        before funds move (accepted -> refused) or before dispatch
        (reserved -> released)."""
        decided = self._decide()
        if isinstance(decided, PaidResult):
            refusal = decided
        elif self._decision_permits(decided):
            if decided.code == pc.CODE_APPROVAL_REQUIRED and not inv.get("approval_id"):
                refusal = self._refusal("policy-denied", "policy now requires an approval")
            else:
                if decided.code == pc.CODE_APPROVAL_REQUIRED and inv.get("phase") != ACCEPTED:
                    # Past accepted the approval is spent for this row: the
                    # decision in force before dispatch is the approved one,
                    # so the audit trail does not end on approval_required.
                    self._approved_earlier = True
                    if self._approval_record is not None:
                        # Only the attempt that consumed the approval knows
                        # who decided it; a resumed attempt writes no
                        # approval line it cannot attribute.
                        self._record_approved(str(inv["approval_id"]), self._approval_record)
                return None
        else:
            refusal = self._refusal("policy-denied", decided.reason)
        phase = inv.get("phase")
        if phase == ACCEPTED:
            await self._ledger("transition", self._tenant, inv["invocation_id"], expected_phase=ACCEPTED,
                               to_phase=REFUSED, reason_code=refusal.reason_code)
        elif phase == RESERVED:
            await self._ledger("release", self._tenant, inv["invocation_id"], reason_code=refusal.reason_code)
        refusal.invocation_id = inv["invocation_id"]
        return refusal

    async def _consume_approval(self, inv: Mapping[str, Any]) -> PaidResult | None:
        approval_id = inv.get("approval_id") or ""
        if not approval_id:
            return None
        store = self.runtime.approvals
        if store is None:
            return self._refusal("policy-denied", "no approval store is configured", invocation_id=inv["invocation_id"])
        fresh = aa.intent_hash(self._intent())
        spent = await asyncio.to_thread(store.consume, approval_id, fresh)
        if spent.status == aa.SPENT:
            self._approval_record = spent.record
            self._record_approved(approval_id, spent.record)
            return None
        if spent.status in (aa.CONSUMED, aa.UNKNOWN):
            # Consumed already: by THIS invocation if its intent is this one
            # (the ledger binds an approval to one invocation, and only this
            # path builds an intent carrying the terms binding). A lost
            # consume answer reads back the same way.
            got = await asyncio.to_thread(store.get, approval_id)
            rec = got.record
            if got.status == aa.CONSUMED and rec is not None and rec.intent_hash == fresh:
                # Spent for this invocation, so this attempt is the one that
                # consumed it: the recheck before dispatch writes its line too.
                self._approval_record = rec
                self._record_approved(approval_id, rec)
                return None
            if got.status == aa.UNKNOWN or (spent.status == aa.UNKNOWN and got.status == aa.APPROVED):
                # Not observable now: nothing is held, the row stays
                # accepted, and a retry resumes it.
                return self._refusal("policy-denied", "the approval could not be confirmed; retry",
                                     invocation_id=inv["invocation_id"], approval_ref=approval_id)
        await self._ledger("transition", self._tenant, inv["invocation_id"], expected_phase=ACCEPTED,
                           to_phase=REFUSED, reason_code="policy-denied")
        return self._refusal("policy-denied", f"approval {spent.status}", invocation_id=inv["invocation_id"],
                             approval_ref=approval_id)

    def _record_approved(self, approval_id: str, rec: aa.ApprovalRecord | None) -> None:
        """The audit line for the permitted decision, now that the approval
        is spent for this invocation (what `decide` would have written had
        it consumed it)."""
        if self._request is None or self._decision is None:
            return
        # The verdict is the rule's REQUIRE_APPROVAL (`pc._verdict_for` maps
        # every approval-flow code to it, never to DENY), and it stays that:
        # `pc.decision_record`'s contract is that a permitted REQUIRE_APPROVAL
        # keeps the rule's verdict and carries `code: approved`, which is
        # exactly what `pc.decide` writes when it redeems an approval itself.
        # Whether the action ran is read from `code`, not from `decision`.
        # `decided_at` is set only alongside an approver, as `pc.decide` does.
        approver = rec.decided_by if rec is not None else ""
        approved = pc.Decision(
            self._decision.verdict, pc.CODE_APPROVED, f"approval {approval_id} consumed for this invocation",
            self._decision.policy_version, self._decision.rule_id, self._request.action_digest(), approval_id,
            approver, self.runtime.now().isoformat() if approver else "",
        )
        self._decision = approved
        pc.emit(self._request, approved)

    async def _from_accepted(self, inv: dict[str, Any]) -> dict[str, Any] | PaidResult:
        stop = await self._recheck(inv)
        if stop is not None:
            return stop
        stop = await self._consume_approval(inv)
        if stop is not None:
            return stop
        res = await self._ledger("reserve", self._tenant, inv["invocation_id"])
        if res.ok:
            nxt = res.body.get("invocation")
            return dict(nxt) if isinstance(nxt, dict) else await self._reload(inv)
        if res.code == "payment-unknown":
            return self._refusal("outcome-unknown", "the reservation's state is unknown; nothing was dispatched",
                                 invocation_id=inv["invocation_id"], phase=ACCEPTED)
        # A 422 is final and mctl-api has already moved the row to refused;
        # a phase conflict means another attempt moved it. Either way the
        # ledger says what happened.
        reloaded = await self._reload(inv)
        if isinstance(reloaded, dict) and reloaded.get("phase") == ACCEPTED:
            # Still accepted after a failed reserve: the answer was lost or
            # mctl-api could not decide. Nothing is held; a retry resumes.
            return self._refusal("outcome-unknown", "the reservation could not be made; nothing was dispatched",
                                 invocation_id=inv["invocation_id"], phase=ACCEPTED)
        return reloaded

    async def _from_reserved(self, inv: dict[str, Any]) -> dict[str, Any] | PaidResult:
        stop = await self._recheck(inv)
        if stop is not None:
            return stop
        res = await self._ledger("dispatch", self._tenant, inv["invocation_id"])
        if not res.ok:
            if res.code == "payment-unknown":
                return self._refusal("outcome-unknown", "the reservation's state is unknown; nothing was dispatched",
                                     invocation_id=inv["invocation_id"], phase=RESERVED)
            reloaded = await self._reload(inv)
            if isinstance(reloaded, PaidResult) or reloaded.get("phase") != RESERVED:
                return reloaded
            # Still reserved and mctl-api cannot sign (dispatch-unavailable,
            # or an answer that never came back): nothing was dispatched.
            # The reservation stays held until mctl-api's reconciler
            # releases it at its deadline; releasing here could race a
            # concurrent attempt that did get an authorization.
            return self._refusal("outcome-unknown", "dispatch could not be authorized; nothing was dispatched",
                                 invocation_id=inv["invocation_id"], phase=RESERVED)
        body = res.body
        nxt = body.get("invocation")
        if not isinstance(nxt, dict):
            return await self._reload(inv)
        if body.get("replayed") is True:
            # Another attempt holds this dispatch: never call the provider.
            return dict(nxt)
        try:
            self._authorization = cm.DispatchAuthorization.from_dict(body.get("authorization"))
        except cm.AuthorizationRejected:
            return dict(nxt)  # cannot dispatch; the outcome lookup settles it
        return await self._dispatch(dict(nxt))

    async def _dispatch(self, inv: dict[str, Any]) -> dict[str, Any] | PaidResult:
        """Step 9: the one place a paid provider is called."""
        auth = self._authorization
        if auth is None:
            raise RuntimeError("dispatch without an authorization")
        self._authorization = None  # one call per authorization, ever
        try:
            result = await asyncio.wait_for(
                self.session.call_paid_tool(self.tool, self.arguments, auth), timeout=self.runtime.call_timeout_s,
            )
        except cm.DispatchRefused:
            # Refused before acceptance: nothing ran, nothing to charge.
            await self._ledger("release", self._tenant, inv["invocation_id"], reason_code="provider-error")
            self._note(inv, "commerce_dispatch_refused")
            reloaded = await self._reload(inv)
            if isinstance(reloaded, PaidResult):
                return reloaded
            if reloaded.get("phase") == RELEASED:
                return self._refusal("provider-error", "the provider refused the dispatch; nothing was charged",
                                     invocation_id=inv["invocation_id"], phase=RELEASED)
            return reloaded
        except Exception:  # noqa: BLE001 — timeout, transport, anything: ambiguous, ask the provider
            return inv  # the dispatch branch looks the outcome up
        self._result = result
        return await self._record_outcome(inv, cm.FAILED if result.is_error else cm.SUCCEEDED)

    async def _from_dispatch(self, inv: dict[str, Any]) -> dict[str, Any] | PaidResult:
        """The outcome lookup: dispatching, dispatched or outcome_unknown.
        Never dispatches again."""
        if self._result is not None:
            return await self._record_outcome(inv, cm.FAILED if self._result.is_error else cm.SUCCEEDED)
        try:
            outcome = await self.session.outcome(inv["provider_operation_key"])
        except Exception:  # noqa: BLE001 — could not observe: unknown, never not_found
            return await self._hold_unknown(inv, "the provider's outcome could not be read")
        if outcome.status in (cm.SUCCEEDED, cm.FAILED):
            from orchestrator.capability_gateway import ToolCallResult

            self._result = ToolCallResult(is_error=outcome.status == cm.FAILED or outcome.is_error, text=outcome.text)
            return await self._record_outcome(inv, outcome.status)
        if outcome.status == cm.IN_PROGRESS:
            if inv.get("phase") == DISPATCHING:
                moved = await self._ledger("transition", self._tenant, inv["invocation_id"],
                                           expected_phase=DISPATCHING, to_phase=DISPATCHED,
                                           reason_code="provider-accepted")
                if moved.ok:
                    inv = dict(moved.body)
            return await self._hold_unknown(inv, "the provider is still running this call")
        # not_found: definitive only past dispatch_deadline + max_clock_skew,
        # and only while the provider never confirmed acceptance.
        deadline_text = inv.get("dispatch_deadline")
        try:
            deadline = cm.parse_time(deadline_text) if isinstance(deadline_text, str) else None
        except cm.CommerceError:
            deadline = None
        margin = timedelta(seconds=self.runtime.max_clock_skew_s)
        if inv.get("phase") == DISPATCHING and deadline is not None and self.runtime.now() > deadline + margin:
            # Whatever the release answered, the ledger says what happened.
            await self._ledger("release", self._tenant, inv["invocation_id"], reason_code="provider-error")
            return await self._reload(inv)
        return await self._hold_unknown(inv, "the provider has not accepted this call yet")

    async def _hold_unknown(self, inv: dict[str, Any], why: str) -> PaidResult:
        if inv.get("phase") == DISPATCHED:
            moved = await self._ledger("transition", self._tenant, inv["invocation_id"], expected_phase=DISPATCHED,
                                       to_phase=OUTCOME_UNKNOWN, reason_code="outcome-unknown")
            if moved.ok:
                inv = dict(moved.body)
        self._note(inv, "commerce_outcome_unknown")
        return self._refusal("outcome-unknown", f"{why}; liability is held, the call is not repeated",
                             invocation_id=inv["invocation_id"], phase=str(inv.get("phase", "")))

    async def _record_outcome(self, inv: dict[str, Any], outcome: str) -> dict[str, Any] | PaidResult:
        """Steps 10-11: dispatching -> dispatched, then finalize."""
        phase = inv.get("phase")
        if phase == DISPATCHING:
            moved = await self._ledger("transition", self._tenant, inv["invocation_id"], expected_phase=DISPATCHING,
                                       to_phase=DISPATCHED, reason_code="provider-accepted")
            if not moved.ok:
                return await self._reload_or_answer(inv)
            inv = dict(moved.body)
            phase = inv.get("phase")
        if phase in (DISPATCHED, OUTCOME_UNKNOWN):
            done = await self._ledger("finalize", self._tenant, inv["invocation_id"], execution_outcome=outcome)
            if not done.ok:
                return await self._reload_or_answer(inv)
            nxt = done.body.get("invocation")
            return dict(nxt) if isinstance(nxt, dict) else await self._reload_or_answer(inv)
        return await self._reload_or_answer(inv)

    async def _reload_or_answer(self, inv: Mapping[str, Any]) -> dict[str, Any] | PaidResult:
        """The ledger did not take the outcome. If it cannot be read back,
        the provider's result still goes to the caller: mctl-api's
        reconciler settles the charge from the provider's record."""
        reloaded = await self._reload(inv)
        if isinstance(reloaded, PaidResult) and self._result is not None:
            return self._answer(inv, settled=False)
        return reloaded

    async def _settled(self, inv: dict[str, Any]) -> PaidResult:
        phase = inv.get("phase")
        if phase == FINALIZED:
            recorded = inv.get("execution_outcome")
            if self._result is None:
                try:
                    outcome = await self.session.outcome(inv["provider_operation_key"])
                except Exception:  # noqa: BLE001
                    outcome = None
                if outcome is not None and outcome.status in (cm.SUCCEEDED, cm.FAILED):
                    from orchestrator.capability_gateway import ToolCallResult

                    self._result = ToolCallResult(is_error=outcome.status == cm.FAILED or outcome.is_error,
                                                  text=outcome.text)
            elif (recorded == cm.SUCCEEDED) == self._result.is_error:
                # A result that contradicts the recorded outcome: audited,
                # the charge stays what it is (ADR 020 sec. 11).
                self._note(inv, "commerce_late_result")
            return self._answer(inv, settled=True)
        code = _code(str(inv.get("reason_code") or ""), "provider-error")
        return self._refusal(code, f"the invocation was {phase}; nothing was charged",
                             invocation_id=str(inv.get("invocation_id", "")), phase=str(phase))

    def _answer(self, inv: Mapping[str, Any], *, settled: bool) -> PaidResult:
        result = self._result
        recorded = inv.get("execution_outcome") if settled else None
        failed = (recorded == cm.FAILED) if recorded in (cm.SUCCEEDED, cm.FAILED) else (
            result is None or result.is_error)
        text = result.text if result is not None else None
        out = PaidResult(
            "provider-error" if failed else "ok", text=text,
            error=(_FAILED_ERROR if failed else _RESULT_UNAVAILABLE) if text is None else "",
            invocation_id=str(inv.get("invocation_id", "")), phase=str(inv.get("phase", "")),
            policy_checkpoint=self.policy_status(), result_hash_source=text,
        )
        self._note(inv, "commerce_settled" if settled else "commerce_result_unsettled")
        return out


_FAILED_ERROR = "the provider failed to execute this capability"
#: A settled success whose result the provider can no longer produce: the
#: charge stands, but an absent text must not read as the capability's output.
_RESULT_UNAVAILABLE = "the capability succeeded, but its result could not be retrieved"

#: Phases a row reaches only after its approval was consumed and the policy
#: rechecked before dispatch (`reserved` is excluded: its recheck still runs).
_APPROVAL_SPENT_PHASES = frozenset({DISPATCHING, DISPATCHED, OUTCOME_UNKNOWN, FINALIZED})
#: A row with an approval reaches `released` only after the approval was
#: consumed (`reserved` is reached through the consume). The one release
#: whose verdict is not "allowed" is the revocation at the recheck, which
#: carries one of these codes; any other release, including one written by
#: mctl-api's reconciler, keeps the spent approval's verdict.
_RELEASED_BY_REVOCATION = frozenset({"policy-denied", "identity-unresolved"})


def _past_recheck(inv: Mapping[str, Any]) -> bool:
    """Whether an earlier attempt took this row past the pre-dispatch policy
    recheck (and, for a row with an approval, consumed it)."""
    phase = inv.get("phase")
    return phase in _APPROVAL_SPENT_PHASES or (
        phase == RELEASED and inv.get("reason_code") not in _RELEASED_BY_REVOCATION)


def _approval_spent(inv: Mapping[str, Any]) -> bool:
    """Whether this row's approval was consumed and its policy rechecked
    by the attempt that moved it here."""
    return bool(inv.get("approval_id")) and _past_recheck(inv)
