"""Activity side of the durable approval wait (mctlhq/mctl-agents#198,
docs/adr/014-policy-checkpoint.md §7).

Two things live here, both stdlib + temporalio only:

- **The gated-action contract.** A Temporal activity that performs a side
  effect governed by a REQUIRE_APPROVAL rule takes a `GatedActionInput` and
  returns a `GatedActionResult`, and does its work through `run_gated()`.
  `run_gated` builds the `ActionRequest` from the identity the workflow
  passed in (the worker has no `MCTL_EXECUTION_CONTEXT_FILE`, so the
  process-wide `current_identity()` would be empty and no approval could be
  bound), decides through `policy_checkpoint.decide()`, and calls the side
  effect only on a permitted decision. For a REQUIRE_APPROVAL action that
  means only after mctl-api has confirmed, in this call, the consume of an
  approved receipt bound to exactly this intent. On `approval_pending` the
  activity returns the decision (with the receipt id) instead of raising,
  and ends: nothing is held while the human decides.

- **`read_action_approval`**, the read-only poll the wait falls back to when
  mctl-api's wake-up signal is lost. It reports what the store says, and the
  workflow uses it only to decide whether a re-check is worth running. It
  never authorizes anything: only the re-run of the gated activity, which
  redeems the receipt through the checkpoint, can.

Off by default. With `MCTL_POLICY_APPROVALS` unset the lookup is
`NO_APPROVALS`, a REQUIRE_APPROVAL action is refused with
`approval_required`, never `approval_pending`, and no wait ever starts.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from orchestrator import action_approvals as aa
from orchestrator import policy_checkpoint as pc


@dataclass(frozen=True)
class GatedActionInput:
    """What a gated activity receives. `payload` is the activity's own
    business (JSON); the rest is the approval binding.

    - `execution_id`, `actor`, `trace_id`: the identity the approval binds
      to, supplied by the calling workflow (for a DevLoop step, the sealed
      execution context it minted). Without an `execution_id` mctl-api has
      nothing to bind a request to, and the checkpoint refuses.
    - `attempt`: which request for this intent. 0 is the first; a higher one
      is a deliberate re-request after a denial or an expiry, and opens a
      new request that needs a new human decision.
    - `approval_ref`: the receipt to revalidate. Empty on the first call
      (find or create by key); set by the wait on every wake, so a wake
      redeems exactly the receipt it waited on."""

    payload: dict[str, Any] = field(default_factory=dict)
    execution_id: str = ""
    actor: str = ""
    trace_id: str = ""
    attempt: int = 0
    approval_ref: str = ""


@dataclass(frozen=True)
class GatedActionResult:
    """What a gated activity answers. `code` is the checkpoint's decision
    code; `ran` is true only when the side effect was called, which
    `run_gated` does only on `allowed` or `approved`."""

    code: str
    verdict: str = ""
    approval_ref: str = ""
    ran: bool = False
    result: dict[str, Any] | None = None
    reason: str = ""
    #: Set when the decision permitted and `side_effect()` raised: the
    #: exception type and message. `ran` stays false.
    effect_error: str = ""

    @property
    def awaiting_approval(self) -> bool:
        return self.code == pc.CODE_APPROVAL_PENDING and bool(self.approval_ref)


@dataclass(frozen=True)
class ApprovalPoll:
    """What `read_action_approval` saw. `state` is the store's state
    (`pending`, `approved`, `denied`, `expired`, `consumed`) or `unknown`
    when it could not tell; `expires_at` is the receipt's own expiry
    (RFC 3339), empty when unknown."""

    approval_id: str
    state: str
    expires_at: str = ""
    reason: str = ""


#: The poll's answer when the store could not tell.
POLL_UNKNOWN = "unknown"


def approvals_for_attempt(attempt: int) -> pc.ApprovalLookup:
    """The configured approval store, asking with `attempt`. Only the
    mctl-api store knows about attempts; `NO_APPROVALS` and a misconfigured
    store refuse every attempt the same way."""
    lookup = pc.configured_approvals()
    if isinstance(lookup, aa.MctlApiApprovals):
        return lookup.for_attempt(attempt)
    return lookup


def _result(decision: pc.Decision, *, ran: bool = False, result: dict[str, Any] | None = None) -> GatedActionResult:
    return GatedActionResult(
        code=decision.code, verdict=decision.verdict, approval_ref=decision.approval_ref,
        ran=ran, result=result, reason=decision.reason,
    )


def gated_request(
    inp: GatedActionInput,
    action_kind: str,
    operation: str,
    target: str,
    args: Any,
    *,
    grants: tuple[str, ...] = (),
    metadata: Mapping[str, str] | None = None,
    policy: pc.Policy = pc.BUILTIN_POLICY,
) -> pc.ActionRequest | pc.Decision:
    """The `ActionRequest` for this call, stamped with the identity the
    workflow passed in, or the recorded DENY when `args` cannot be
    digested."""
    try:
        digest = pc.args_digest_of(args)
    except Exception as exc:  # noqa: BLE001 — arguments that cannot be digested are refused, not raised
        probe = pc.ActionRequest(action_kind, operation, target, "", grants=grants,
                                 execution_id=inp.execution_id, trace_id=inp.trace_id, actor=inp.actor)
        decision = pc.Decision(pc.DENY, pc.CODE_INVALID_REQUEST,
                               f"arguments cannot be digested: {type(exc).__name__}", policy.version, "", "")
        pc.emit(probe, decision)
        return decision
    return pc.ActionRequest(
        action_kind, operation, target, digest,
        execution_id=inp.execution_id, trace_id=inp.trace_id, actor=inp.actor, grants=grants,
        metadata=tuple(sorted((metadata or {}).items())),
    )


def run_gated(
    inp: GatedActionInput,
    action_kind: str,
    operation: str,
    target: str,
    args: Any,
    side_effect: Callable[[], dict[str, Any] | None],
    *,
    grants: tuple[str, ...] = (),
    metadata: Mapping[str, str] | None = None,
    policy: pc.Policy = pc.BUILTIN_POLICY,
    approvals: pc.ApprovalLookup | None = None,
) -> GatedActionResult:
    """Decide, and call `side_effect` only on a permitted decision.

    `args` must be everything the side effect depends on, recomputed from
    the world on every call (a head SHA, a rendered body): the intent is
    hashed from them, so a receipt approved for one set of arguments is
    `approval_intent_mismatch` for any other and nothing is consumed.

    The side effect should be idempotent and retry its own transient errors
    before raising. If it raises anyway after an approved receipt was
    consumed, the exception is caught and answered as `effect_error` with
    the decision's code (the wait ends `effect_failed`, which a caller may
    re-request, with a new human decision): the receipt is spent, so a
    Temporal retry could only answer `approval_consumed`. A worker that dies
    between the consume and the side effect reports nothing; the retried
    activity answers `approval_consumed` and the wait ends `consumed`. It
    never acts twice on one receipt."""
    request = gated_request(inp, action_kind, operation, target, args, grants=grants, metadata=metadata,
                            policy=policy)
    if isinstance(request, pc.Decision):
        return _result(request)
    lookup = approvals if approvals is not None else approvals_for_attempt(inp.attempt)
    decision = pc.decide(request, policy=policy, approvals=lookup, approval_ref=inp.approval_ref)
    if not decision.permitted:
        return _result(decision)
    try:
        result = side_effect()
    except Exception as exc:  # noqa: BLE001 — the receipt is spent: report, never retry on it
        failed = _result(decision)
        return GatedActionResult(
            code=failed.code, verdict=failed.verdict, approval_ref=failed.approval_ref, ran=False,
            reason=failed.reason, effect_error=f"{type(exc).__name__}: {exc}",
        )
    return _result(decision, ran=True, result=result)


def _read(approval_id: str) -> ApprovalPoll:
    answer = aa.ActionApprovalClient().get(approval_id)
    rec = answer.record
    if rec is None or answer.status == aa.UNKNOWN:
        return ApprovalPoll(approval_id, POLL_UNKNOWN, reason=answer.reason or answer.code or answer.status)
    return ApprovalPoll(approval_id, rec.state, expires_at=rec.expires_at)


@activity.defn
async def read_action_approval(approval_id: str) -> ApprovalPoll:
    """`GET /api/v1/action-approvals/{id}`: read-only, never raises. An
    answer the store could not give is `unknown`, and the wait simply polls
    again at its next tick."""
    try:
        return await asyncio.to_thread(_read, approval_id)
    except Exception as exc:  # noqa: BLE001 — a poll that fails is a poll that could not tell
        return ApprovalPoll(approval_id, POLL_UNKNOWN, reason=f"{type(exc).__name__}: {exc}")
