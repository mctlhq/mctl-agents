"""Runtime policy checkpoint for consequential agent actions (mctlhq/
mctl-agents#197, docs/adr/014-policy-checkpoint.md).

One decision point, placed at mctl-agents' own action-execution boundary
immediately before the external side effect — not in the MCP gateway and not
in each business function:

    ActionRequest -> decide() -> ALLOW | DENY | REQUIRE_APPROVAL
                  -> the side effect runs only on a permitted decision

Rules:

- Fail closed. An evaluator error, an identity that cannot be read in
  require mode, or an approval lookup that fails is a DENY, never an ALLOW.
- No side effect before ALLOW, or before an approval bound to the exact
  action digest. A changed argument changes the digest, so it can never
  reuse an approval.
- Every decision is recorded — execution id, action identity, target,
  policy version, rule, decision, code, reason, approval ref — as one
  `POLICY_DECISION` line (the structured-log convention of
  `orchestrator/lifecycle/claim.py`'s `_emit`), shaped to be carried over
  unchanged into the execution trace of #195. Arguments are only ever
  recorded as a digest: no raw payload and no secret reaches the record.

The durable approval store is NOT part of this module: `ApprovalLookup` is
the seam, and the only implementation today (`NO_APPROVALS`) never finds
one, so REQUIRE_APPROVAL currently always blocks. Where approvals live (the
mctl-api approval projection of the work-item contract is the candidate) is
an open decision recorded in the ADR.

Stdlib-only, like `orchestrator/context_snapshot.py` and
`orchestrator/execution_identity.py`, so the Temporal worker, the pollers and
the SDK hooks can all import it.
"""
from __future__ import annotations

import fnmatch
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestrator.context_snapshot import canonical_json, hash_bytes

ALLOW = "ALLOW"
DENY = "DENY"
REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
VERDICTS = frozenset({ALLOW, DENY, REQUIRE_APPROVAL})

# Action kinds governed today.
GITHUB_ISSUE_COMMENT = "github.issue.comment"
MCTL_OPERATION_EXECUTE = "mctl.operation.execute"
#: A write to the mctl-api work-item store made by the orchestrator itself.
MCTL_WORK_ITEM_WRITE = "mctl.work_item.write"
MCP_TOOL_CALL = "mcp.tool.call"

# Decision codes. `allowed`/`approved` permit; every other code refuses.
CODE_ALLOWED = "allowed"
CODE_APPROVED = "approved"
CODE_DENIED = "denied_by_rule"
CODE_APPROVAL_REQUIRED = "approval_required"
CODE_NO_RULE = "no_matching_rule"
CODE_GRANT_MISSING = "grant_missing"
CODE_EVALUATOR_ERROR = "evaluator_error"
CODE_IDENTITY_UNAVAILABLE = "identity_unavailable"
CODE_APPROVAL_LOOKUP_ERROR = "approval_lookup_error"
CODE_INVALID_REQUEST = "invalid_request"

#: Codes meaning the checkpoint could not decide, not that it said no: a
#: caller may retry these within its own bound. Every other refusal is an
#: answer (`invalid_request` included: the same arguments are refused again).
UNDECIDED_CODES = frozenset({CODE_EVALUATOR_ERROR, CODE_IDENTITY_UNAVAILABLE, CODE_APPROVAL_LOOKUP_ERROR})

DECISION_PREFIX = "POLICY_DECISION"


def args_digest_of(args: Any) -> str:
    """The digest an ActionRequest carries instead of its arguments."""
    return hash_bytes(canonical_json(args))


@dataclass(frozen=True)
class ActionRequest:
    """One consequential action, described without its payload.

    `action_kind` is the class (`github.issue.comment`, ...), `operation`
    the normalized operation within it (a tool name, an operation id),
    `target` what it acts on. `args_digest` stands in for the arguments;
    `metadata` carries only safe, already-public correlation values.
    """

    action_kind: str
    operation: str
    target: str
    args_digest: str
    execution_id: str = ""
    trace_id: str = ""
    actor: str = ""
    grants: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()

    def action_digest(self) -> str:
        """The exact identity an approval binds to: who, in which
        execution, does what, to what, with which arguments."""
        return hash_bytes(canonical_json({
            "action_kind": self.action_kind,
            "operation": self.operation,
            "target": self.target,
            "args_digest": self.args_digest,
            "execution_id": self.execution_id,
            "actor": self.actor,
        }))


@dataclass(frozen=True)
class Rule:
    """First matching rule wins. `operation` is an fnmatch pattern over
    `ActionRequest.operation`. With `requires_grant`, the operation must
    also match one of the request's grants, or the decision is DENY."""

    rule_id: str
    action_kind: str
    operation: str
    verdict: str
    requires_grant: bool = False


@dataclass(frozen=True)
class Policy:
    version: str
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class Decision:
    verdict: str
    code: str
    reason: str
    policy_version: str
    rule_id: str
    action_digest: str
    approval_ref: str = ""

    @property
    def permitted(self) -> bool:
        return self.code in (CODE_ALLOWED, CODE_APPROVED)

    @property
    def undecided(self) -> bool:
        return self.code in UNDECIDED_CODES


class PolicyRefused(RuntimeError):
    """The checkpoint refused an action; its side effect did not run."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(
            f"policy {decision.verdict} ({decision.code}, rule {decision.rule_id or '-'}): {decision.reason}"
        )
        self.decision = decision


class ApprovalLookup(Protocol):
    def find(self, action_digest: str, policy_version: str) -> str | None:
        """The approval reference bound to exactly this action digest under
        this policy version, or None. Raising means "cannot tell"."""


@dataclass(frozen=True)
class _NoApprovals:
    def find(self, action_digest: str, policy_version: str) -> str | None:
        return None


#: No approval store is wired yet: REQUIRE_APPROVAL always blocks.
NO_APPROVALS: ApprovalLookup = _NoApprovals()

# The mctl MCP tool set lives in mctl-api, not here, so the default for it
# is REQUIRE_APPROVAL: a tool this list does not know — including one added
# to mctl-api tomorrow — is gated, never allowed. Only these are ALLOW:
# reads, by verb prefix, and the few named mutations agents are built to
# make on their own. Tool names appear both as `mctl_<verb>_...` (mctl-api's
# own naming) and as a bare `<verb>_...`, so both spellings are listed.
_READ_VERBS = ("get_", "list_", "read_", "search_", "describe_")
_READ_TOOLS = ("whoami", "incident_summary", "resolve_agent")
#: Mutations an agent performs by design: the incident responder resolves
#: and acknowledges incidents. Nothing here deploys, deletes, grants,
#: approves or starts a run: `trigger_issue` starts a paid investigator
#: run (and could fan out recursively), so like every other trigger it is
#: gated.
_ALLOWED_MUTATIONS = ("resolve_incident", "acknowledge_incident")


def _mctl_tool_patterns(names: tuple[str, ...], *, prefix: bool) -> tuple[str, ...]:
    tail = "*" if prefix else ""
    return tuple(f"mcp__mctl__{p}{n}{tail}" for n in names for p in ("mctl_", ""))


BUILTIN_POLICY = Policy(
    version="mctl-agents/policy/v1",
    rules=(
        Rule("github-issue-comment", GITHUB_ISSUE_COMMENT, "comment", ALLOW),
        Rule("mctl-investigate", MCTL_OPERATION_EXECUTE, "execute:mctl-agents-investigate", ALLOW),
        # Sealing this execution's context snapshot (mctlhq/mctl-agents#431):
        # insert-only on the store side, so it can never overwrite anything.
        Rule("mctl-seal-context-snapshot", MCTL_WORK_ITEM_WRITE, "seal:context-snapshot", ALLOW),
        # Attaching this run's own engine run to its work item, or advancing
        # that execution's phase (mctlhq/mctl-agents#455). The store keys it
        # by (engine, engine_ref), so the run can only name its own; it
        # never creates a work item, resumes one or changes its state.
        Rule("mctl-attach-own-execution", MCTL_WORK_ITEM_WRITE, "attach:work-item-execution", ALLOW),
        *(
            Rule("mctl-mcp-read", MCP_TOOL_CALL, pattern, ALLOW, requires_grant=True)
            for pattern in (*_mctl_tool_patterns(_READ_VERBS, prefix=True),
                            *_mctl_tool_patterns(_READ_TOOLS, prefix=False))
        ),
        *(
            Rule("mctl-mcp-agent-mutation", MCP_TOOL_CALL, pattern, ALLOW, requires_grant=True)
            for pattern in _mctl_tool_patterns(_ALLOWED_MUTATIONS, prefix=False)
        ),
        Rule("mctl-mcp-default-approval", MCP_TOOL_CALL, "mcp__mctl__*", REQUIRE_APPROVAL, requires_grant=True),
    ),
)


def evaluate(policy: Policy, request: ActionRequest) -> tuple[Rule | None, str, str]:
    """(matching rule, code, reason) for request under policy. Pure; raises
    only on a malformed policy, which `decide` turns into a DENY."""
    for rule in policy.rules:
        if rule.verdict not in VERDICTS:
            raise ValueError(f"rule {rule.rule_id!r} has unknown verdict {rule.verdict!r}")
        if rule.action_kind != request.action_kind or not fnmatch.fnmatchcase(request.operation, rule.operation):
            continue
        if rule.requires_grant and not any(fnmatch.fnmatchcase(request.operation, g) for g in request.grants):
            return rule, CODE_GRANT_MISSING, f"{request.operation} is not granted to this execution"
        code = {ALLOW: CODE_ALLOWED, DENY: CODE_DENIED, REQUIRE_APPROVAL: CODE_APPROVAL_REQUIRED}[rule.verdict]
        return rule, code, f"rule {rule.rule_id}"
    return None, CODE_NO_RULE, f"no rule covers {request.action_kind} {request.operation}"


def _verdict_for(code: str) -> str:
    if code == CODE_ALLOWED:
        return ALLOW
    if code in (CODE_APPROVAL_REQUIRED, CODE_APPROVED):
        return REQUIRE_APPROVAL
    return DENY


def decide(
    request: ActionRequest,
    *,
    policy: Policy = BUILTIN_POLICY,
    approvals: ApprovalLookup = NO_APPROVALS,
) -> Decision:
    """Decide and record. Never raises: every failure is a DENY decision."""
    try:
        digest = request.action_digest()
    except Exception as exc:  # noqa: BLE001 — an undescribable action is refused, not raised
        decision = Decision(DENY, CODE_INVALID_REQUEST, f"action cannot be described: {type(exc).__name__}",
                            policy.version, "", "")
        emit(request, decision)
        return decision
    if not request.action_kind or not request.operation or not request.args_digest:
        decision = Decision(DENY, CODE_INVALID_REQUEST, "action kind, operation and args digest are required",
                            policy.version, "", digest)
        emit(request, decision)
        return decision
    try:
        rule, code, reason = evaluate(policy, request)
    except Exception as exc:  # noqa: BLE001 — fail closed on any evaluator error
        decision = Decision(DENY, CODE_EVALUATOR_ERROR, f"policy evaluation failed: {type(exc).__name__}: {exc}",
                            policy.version, "", digest)
        emit(request, decision)
        return decision
    approval_ref = ""
    if code == CODE_APPROVAL_REQUIRED:
        try:
            approval_ref = approvals.find(digest, policy.version) or ""
        except Exception as exc:  # noqa: BLE001 — an unreadable approval store is no approval
            code, reason = CODE_APPROVAL_LOOKUP_ERROR, f"approval lookup failed: {type(exc).__name__}"
        else:
            if approval_ref:
                code, reason = CODE_APPROVED, f"rule {rule.rule_id if rule else ''}: approved"
            else:
                reason = f"rule {rule.rule_id if rule else ''}: needs an approval bound to {digest}"
    decision = Decision(_verdict_for(code), code, reason, policy.version, rule.rule_id if rule else "",
                        digest, approval_ref)
    emit(request, decision)
    return decision


def enforce[T](
    request: ActionRequest,
    side_effect: Callable[[], T],
    *,
    policy: Policy = BUILTIN_POLICY,
    approvals: ApprovalLookup = NO_APPROVALS,
) -> T:
    """Run `side_effect` only if the decision permits it; otherwise raise
    `PolicyRefused` without calling it."""
    decision = decide(request, policy=policy, approvals=approvals)
    if not decision.permitted:
        raise PolicyRefused(decision)
    return side_effect()


def decision_record(request: ActionRequest, decision: Decision) -> dict[str, Any]:
    """The audit record of one decision. Only identifiers, the args digest
    and safe metadata: never the arguments themselves.

    Whether the action ran is `code in {allowed, approved}` — never the
    `decision` field alone: a permitted REQUIRE_APPROVAL keeps the rule's
    verdict and carries `code: approved`."""
    return {
        "execution_id": request.execution_id,
        "trace_id": request.trace_id,
        "actor": request.actor,
        "action_kind": request.action_kind,
        "operation": request.operation,
        "target": request.target,
        "action_digest": decision.action_digest,
        "args_digest": request.args_digest,
        "metadata": dict(request.metadata),
        "policy_version": decision.policy_version,
        "rule_id": decision.rule_id,
        "decision": decision.verdict,
        "code": decision.code,
        "reason": decision.reason,
        "approval_ref": decision.approval_ref,
    }


def emit(request: ActionRequest, decision: Decision) -> None:
    """Print one `POLICY_DECISION` line. Never raises."""
    try:
        print(f"{DECISION_PREFIX} {json.dumps(decision_record(request, decision), sort_keys=True)}", flush=True)
    except Exception:  # noqa: BLE001, S110 — recording must never become the reason an action fails
        pass


@dataclass(frozen=True)
class ExecutionIdentity:
    execution_id: str = ""
    trace_id: str = ""
    actor: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)


def current_identity() -> ExecutionIdentity:
    """The sealed execution context the CWFT wrote, if any.

    Only a context read from `MCTL_EXECUTION_CONTEXT_FILE` counts: a
    locally-minted fallback gets a fresh id per call, which would make the
    action digest unstable. Absent file -> empty identity. In require mode
    (`MCTL_REQUIRE_EXECUTION_CONTEXT`) a missing or broken context raises,
    and callers must turn that into a DENY (see `identity_or_refusal`)."""
    from orchestrator.execution_identity import (
        MCTL_EXECUTION_CONTEXT_FILE_ENV,
        MCTL_REQUIRE_EXECUTION_CONTEXT_ENV,
        ExecutionIdentityError,
        load_from_environment,
    )

    if not os.environ.get(MCTL_EXECUTION_CONTEXT_FILE_ENV, "").strip():
        if os.environ.get(MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, "").strip():
            load_from_environment(executor_type="system")  # raises ExecutionContextRequiredError
        return ExecutionIdentity()
    try:
        ctx = load_from_environment(executor_type="system")
    except ExecutionIdentityError:
        # Only reachable outside require mode: there, a broken file raises
        # ExecutionContextRequiredError (a RuntimeError, not caught here).
        return ExecutionIdentity()
    return ExecutionIdentity(execution_id=ctx.context_id, trace_id=ctx.trace_id, actor=_actor_of(ctx))


def _actor_of(ctx: Any) -> str:
    """`type:id`, e.g. `human:mashkovd` — the principal the context names."""
    return f"{ctx.actor.type}:{ctx.actor.id}"


def request_for(
    action_kind: str,
    operation: str,
    target: str,
    args: Any,
    *,
    grants: tuple[str, ...] = (),
    metadata: Mapping[str, str] | None = None,
    policy: Policy = BUILTIN_POLICY,
) -> ActionRequest | Decision:
    """Build a request stamped with the current execution identity, or the
    DENY decision (already recorded) when require mode has no identity."""
    try:
        args_digest = args_digest_of(args)
    except Exception as exc:  # noqa: BLE001 — arguments that cannot be digested are refused, not raised
        probe = ActionRequest(action_kind, operation, target, "", grants=grants)
        decision = Decision(DENY, CODE_INVALID_REQUEST, f"arguments cannot be digested: {type(exc).__name__}",
                            policy.version, "", "")
        emit(probe, decision)
        return decision
    probe = ActionRequest(action_kind, operation, target, args_digest, grants=grants,
                          metadata=tuple(sorted((metadata or {}).items())))
    try:
        ident = current_identity()
    except Exception as exc:  # noqa: BLE001 — require mode without a context: refuse
        decision = Decision(DENY, CODE_IDENTITY_UNAVAILABLE, f"execution identity unavailable: {type(exc).__name__}",
                            policy.version, "", probe.action_digest())
        emit(probe, decision)
        return decision
    return ActionRequest(
        action_kind, operation, target, probe.args_digest,
        execution_id=ident.execution_id, trace_id=ident.trace_id, actor=ident.actor,
        grants=grants, metadata=probe.metadata,
    )


def checkpoint(
    action_kind: str,
    operation: str,
    target: str,
    args: Any,
    *,
    grants: tuple[str, ...] = (),
    metadata: Mapping[str, str] | None = None,
    policy: Policy = BUILTIN_POLICY,
    approvals: ApprovalLookup = NO_APPROVALS,
) -> Decision:
    """`request_for` + `decide`: the one call a governed path makes.
    Never raises: every failure is a recorded DENY."""
    request = request_for(action_kind, operation, target, args, grants=grants, metadata=metadata, policy=policy)
    if isinstance(request, Decision):
        return request
    return decide(request, policy=policy, approvals=approvals)


def require(decision: Decision) -> None:
    """Raise `PolicyRefused` unless `decision` permits the action."""
    if not decision.permitted:
        raise PolicyRefused(decision)
