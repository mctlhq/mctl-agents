"""A durable parking ticket for the cron-driven approval wait (mctl-agents#198,
docs/adr/016-human-approval-checkpoints.md).

The Temporal wait (mctl-agents#479, `orchestrator/temporal/workflows/
action_approval.py`) holds its own state in workflow history. The shepherd
is cron-driven and holds nothing between ticks (`docs/temporal-flow.md`; the
shepherd stays on cron per ADR-006 phase 6, tracker #217), so its wait needs
one small piece of state it CAN hold durably: `.status.yaml`
(`orchestrator/proposal_state.py`).

`ApprovalTicket` is that state: everything a human-facing surface or an
operator reading `.status.yaml` needs to understand what is parked and why,
built once from an `awaiting_approval` `Decision`
(`orchestrator/policy_checkpoint.py`), the `ActionRequest` that produced it,
and the `ApprovalRecord` of one read-only `ActionApprovalClient.get()`
(`orchestrator/action_approvals.py`) of the receipt it names.

It carries no raw action arguments -- only identifiers, a policy reason and
an already-public reference (a PR's head SHA, say). Stdlib-only, like the
two modules it reads from, so it costs nothing to import anywhere the
checkpoint already loads.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from orchestrator import policy_checkpoint as pc
from orchestrator.action_approvals import ApprovalRecord

#: Bumped only if the shape of `to_json()`'s output changes incompatibly.
SCHEMA_VERSION = "mctl-agents/approval-ticket/v1"


@dataclass(frozen=True)
class ApprovalTicket:
    """The human-readable summary of one parked, awaiting-approval action.

    Every field is an identifier, a policy reason, or an already-public
    reference -- never a raw argument. `artifact_ref` is the one field a
    call site fills in itself (a head SHA, a proposal slug): the thing that
    makes THIS instance of the action concrete beyond what the checkpoint's
    own identity already covers.
    """

    approval_ref: str
    intent_hash: str
    expires_at: str
    action_kind: str
    operation: str
    target: str
    policy_rule_id: str
    policy_version: str
    reason: str
    trace_id: str
    execution_id: str
    actor: str
    artifact_ref: str = ""

    def to_json(self) -> dict[str, Any]:
        """A plain, JSON/YAML-safe mapping -- what `.status.yaml`'s
        `approval` block and any future surface store and read."""
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}


_FIELD_NAMES = tuple(f.name for f in fields(ApprovalTicket))


def ticket_from(
    decision: pc.Decision,
    request: pc.ActionRequest,
    record: ApprovalRecord,
    *,
    artifact_ref: str = "",
) -> ApprovalTicket:
    """Build the ticket for one parked action.

    `record` must be the answer of the ONE read-only
    `ActionApprovalClient.get(decision.approval_ref)` call the caller makes;
    this function performs no I/O of its own and never calls the store.
    Raises `ValueError` if `decision` is not `awaiting_approval`, or if
    `record` does not describe the receipt `decision.approval_ref` names --
    both programmer errors at the call site, never a runtime condition a
    parked action itself can trigger.
    """
    if not decision.awaiting_approval:
        raise ValueError("ticket_from requires an awaiting_approval decision")
    if record.id != decision.approval_ref:
        raise ValueError(
            f"record {record.id!r} does not match decision.approval_ref {decision.approval_ref!r}"
        )
    return ApprovalTicket(
        approval_ref=record.id,
        intent_hash=record.intent_hash,
        expires_at=record.expires_at,
        action_kind=request.action_kind,
        operation=request.operation,
        target=request.target,
        policy_rule_id=decision.rule_id,
        policy_version=decision.policy_version,
        reason=decision.reason,
        trace_id=request.trace_id,
        execution_id=request.execution_id,
        actor=request.actor,
        artifact_ref=artifact_ref,
    )


def from_json(data: Any) -> ApprovalTicket | None:
    """The ticket `to_json()` wrote, or `None` for anything absent, foreign
    or malformed.

    Every reader of a persisted ticket must tolerate `None`: a proposal's
    `.status.yaml` predates this field, was written by another version, or
    was hand-edited -- none of that may raise, because "nothing parked" is
    always a safe, quiet default.
    """
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    values: dict[str, str] = {}
    for name in _FIELD_NAMES:
        value = data.get(name, "" if name == "artifact_ref" else None)
        if not isinstance(value, str):
            return None
        values[name] = value
    return ApprovalTicket(**values)
