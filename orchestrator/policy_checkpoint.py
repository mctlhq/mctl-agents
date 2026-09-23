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
  action was spent for it. A changed argument changes the intent, so it can
  never reuse an approval, and a spent approval never authorizes again.
- Every decision is recorded — execution id, action identity, target,
  policy version, rule, decision, code, reason, approval ref — as one
  `POLICY_DECISION` line (the structured-log convention of
  `orchestrator/lifecycle/claim.py`'s `_emit`), shaped to be carried over
  unchanged into the execution trace of #195. Arguments are only ever
  recorded as a digest: no raw payload and no secret reaches the record.

The durable approval store is NOT part of this module: `ApprovalLookup` is
the seam. mctl-api is the approval authority (mctl-api#366); the lookup
backed by it lives in `orchestrator/action_approvals.py` and is enabled only
by `MCTL_POLICY_APPROVALS=mctl-api` (`configured_approvals`). Unset, the
lookup is `NO_APPROVALS` and REQUIRE_APPROVAL always blocks, as before.

Single use. A REQUIRE_APPROVAL decision is permitted only when the lookup
has just SPENT an approved receipt bound to exactly this action (an atomic
consume in the store): `decide()` consumes at decision time, and a failed or
uncertain consume is a refusal. There is no "approved but not yet consumed"
value a caller could mistake for permission, so every governed path keeps
its one rule: run the side effect immediately after a permitted decision,
and never otherwise.

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
# The orchestrator's own GitHub mutations (mctlhq/mctl-agents#197): the
# implementer's push and PR creation, the shepherd's merge, review trigger and
# CI rerun, and the issue poller's label removal. `GITHUB_ISSUE_COMMENT`
# above covers the investigator's and the directive poller's issue comments.
GITHUB_GIT_PUSH = "github.git.push"
GITHUB_PR_CREATE = "github.pull_request.create"
GITHUB_PR_MERGE = "github.pull_request.merge"
GITHUB_PR_COMMENT = "github.pull_request.comment"
GITHUB_RUN_RERUN = "github.actions.run.rerun"
GITHUB_ISSUE_LABEL = "github.issue.label"

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
# Durable approvals (mctl-api#366, #197/#198). Each keeps the rule's
# REQUIRE_APPROVAL verdict and refuses; only `approved` permits.
#: A request exists in mctl-api and waits for a human; `approval_ref` names it.
CODE_APPROVAL_PENDING = "approval_pending"
CODE_APPROVAL_DENIED = "approval_denied"
CODE_APPROVAL_EXPIRED = "approval_expired"
#: The receipt was already spent: it can never authorize a second effect.
CODE_APPROVAL_CONSUMED = "approval_consumed"
#: The receipt is bound to a different action than the one presented now.
CODE_APPROVAL_INTENT_MISMATCH = "approval_intent_mismatch"
#: The store refused the request itself (invalid, forbidden, not found).
CODE_APPROVAL_REFUSED = "approval_refused"

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

    @property
    def awaiting_approval(self) -> bool:
        """A human decision is pending on `approval_ref`: a caller (or the
        #198 Temporal wait) may wait on it and then decide again."""
        return self.code == CODE_APPROVAL_PENDING and bool(self.approval_ref)


class PolicyRefused(RuntimeError):
    """The checkpoint refused an action; its side effect did not run."""

    def __init__(self, decision: Decision) -> None:
        super().__init__(
            f"policy {decision.verdict} ({decision.code}, rule {decision.rule_id or '-'}): {decision.reason}"
        )
        self.decision = decision


# What an ApprovalLookup answers for one REQUIRE_APPROVAL action.
#: This call spent an approved, unexpired receipt whose intent is exactly
#: this action. The only outcome that permits.
APPROVAL_GRANTED = "granted"
#: No approval store: nothing was asked.
APPROVAL_NONE = "none"
APPROVAL_PENDING = "pending"
APPROVAL_DENIED = "denied"
APPROVAL_EXPIRED = "expired"
APPROVAL_CONSUMED = "consumed"
APPROVAL_MISMATCH = "mismatch"
APPROVAL_REFUSED = "refused"
#: The store could not answer (transport, 5xx, malformed answer).
APPROVAL_UNKNOWN = "unknown"

_APPROVAL_CODES = {
    APPROVAL_GRANTED: CODE_APPROVED,
    APPROVAL_NONE: CODE_APPROVAL_REQUIRED,
    APPROVAL_PENDING: CODE_APPROVAL_PENDING,
    APPROVAL_DENIED: CODE_APPROVAL_DENIED,
    APPROVAL_EXPIRED: CODE_APPROVAL_EXPIRED,
    APPROVAL_CONSUMED: CODE_APPROVAL_CONSUMED,
    APPROVAL_MISMATCH: CODE_APPROVAL_INTENT_MISMATCH,
    APPROVAL_REFUSED: CODE_APPROVAL_REFUSED,
    APPROVAL_UNKNOWN: CODE_APPROVAL_LOOKUP_ERROR,
}


@dataclass(frozen=True)
class ApprovalOutcome:
    status: str
    approval_ref: str = ""
    reason: str = ""


class ApprovalLookup(Protocol):
    def redeem(
        self, request: ActionRequest, *, rule_id: str, policy_version: str, approval_ref: str = "",
    ) -> ApprovalOutcome:
        """Called for a REQUIRE_APPROVAL action immediately before its side
        effect. Returns APPROVAL_GRANTED only after spending, in this call,
        an approved receipt bound to exactly this action under this rule
        and policy version; otherwise the typed reason it cannot. With
        `approval_ref`, revalidate that receipt only. Raising means
        "cannot tell" and is a refusal."""
        ...


@dataclass(frozen=True)
class _NoApprovals:
    def redeem(
        self, request: ActionRequest, *, rule_id: str, policy_version: str, approval_ref: str = "",
    ) -> ApprovalOutcome:
        return ApprovalOutcome(APPROVAL_NONE)


#: No approval store: REQUIRE_APPROVAL always blocks. The default.
NO_APPROVALS: ApprovalLookup = _NoApprovals()

#: Selects the approval store. Unset, empty or `none`: NO_APPROVALS.
#: `mctl-api`: the durable store of mctl-api#366
#: (`orchestrator/action_approvals.py`). Any other value is a
#: misconfiguration, and every REQUIRE_APPROVAL is then a lookup error.
APPROVALS_ENV = "MCTL_POLICY_APPROVALS"
APPROVALS_MCTL_API = "mctl-api"


@dataclass(frozen=True)
class _MisconfiguredApprovals:
    value: str

    def redeem(
        self, request: ActionRequest, *, rule_id: str, policy_version: str, approval_ref: str = "",
    ) -> ApprovalOutcome:
        return ApprovalOutcome(APPROVAL_UNKNOWN, reason=f"{APPROVALS_ENV}={self.value!r} is not a known store")


def configured_approvals() -> ApprovalLookup:
    """The approval store this process is configured for. Off by default:
    production behaviour changes only when gitops sets `MCTL_POLICY_APPROVALS`."""
    value = os.environ.get(APPROVALS_ENV, "").strip()
    if value in ("", "none"):
        return NO_APPROVALS
    if value == APPROVALS_MCTL_API:
        from orchestrator.action_approvals import MctlApiApprovals

        return MctlApiApprovals()
    return _MisconfiguredApprovals(value)


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
        # The orchestrator's own GitHub mutations (#197), each ALLOW: the
        # policy states today's behaviour, and a tighter policy (a merge
        # behind REQUIRE_APPROVAL, say) is one rule change, not a code change.
        # The implementer pushes only its own `feat/agents-<slug>` branch:
        # a new branch plainly, an existing one under `--force-with-lease`.
        Rule("github-push-new-branch", GITHUB_GIT_PUSH, "push:new-branch", ALLOW),
        Rule("github-push-with-lease", GITHUB_GIT_PUSH, "push:force-with-lease", ALLOW),
        Rule("github-pr-create", GITHUB_PR_CREATE, "create", ALLOW),
        # `gh pr merge --merge --match-head-commit`: bound to the reviewed head.
        Rule("github-pr-merge", GITHUB_PR_MERGE, "merge", ALLOW),
        # The shepherd's `@claude review` trigger after a fix-up push.
        Rule("github-pr-review-trigger", GITHUB_PR_COMMENT, "comment:review-trigger", ALLOW),
        Rule("github-run-rerun-failed", GITHUB_RUN_RERUN, "rerun:failed", ALLOW),
        Rule("github-issue-label-remove", GITHUB_ISSUE_LABEL, "remove", ALLOW),
        Rule("mctl-investigate", MCTL_OPERATION_EXECUTE, "execute:mctl-agents-investigate", ALLOW),
        # Sealing this execution's context snapshot (mctlhq/mctl-agents#431):
        # insert-only on the store side, so it can never overwrite anything.
        Rule("mctl-seal-context-snapshot", MCTL_WORK_ITEM_WRITE, "seal:context-snapshot", ALLOW),
        # Attaching this run's own engine run to its work item, or advancing
        # that execution's phase (mctlhq/mctl-agents#455). The store keys it
        # by (engine, engine_ref), so the run can only name its own; it
        # never creates a work item, resumes one or changes its state.
        Rule("mctl-attach-own-execution", MCTL_WORK_ITEM_WRITE, "attach:work-item-execution", ALLOW),
        # The execution-request dispatcher (mctlhq/mctl-agents#461, mctl-api#368):
        # claim a surface's request under a lease, fulfil it with the engine
        # run the dispatcher started, or reject it with a typed reason.
        # mctl-api admits only the service principal, fences fulfil/reject
        # by the claim token, and re-decides the item's state at fulfilment,
        # so none of these can start work on an item that did not ask for it.
        Rule("mctl-claim-execution-request", MCTL_WORK_ITEM_WRITE, "claim:execution-request", ALLOW),
        Rule("mctl-fulfil-execution-request", MCTL_WORK_ITEM_WRITE, "fulfil:execution-request", ALLOW),
        Rule("mctl-reject-execution-request", MCTL_WORK_ITEM_WRITE, "reject:execution-request", ALLOW),
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


_APPROVAL_FLOW_CODES = frozenset({
    CODE_APPROVAL_REQUIRED, CODE_APPROVED, CODE_APPROVAL_PENDING, CODE_APPROVAL_DENIED, CODE_APPROVAL_EXPIRED,
    CODE_APPROVAL_CONSUMED, CODE_APPROVAL_INTENT_MISMATCH, CODE_APPROVAL_REFUSED,
})


def _verdict_for(code: str) -> str:
    if code == CODE_ALLOWED:
        return ALLOW
    if code in _APPROVAL_FLOW_CODES:
        return REQUIRE_APPROVAL
    return DENY


def _redeem(
    approvals: ApprovalLookup, request: ActionRequest, rule_id: str, policy_version: str, approval_ref: str,
) -> tuple[str, str, str]:
    """(code, reason, approval ref) of the approval step. Fails closed:
    anything but a well-formed GRANTED with a ref is a refusal."""
    try:
        outcome = approvals.redeem(request, rule_id=rule_id, policy_version=policy_version,
                                   approval_ref=approval_ref)
    except Exception as exc:  # noqa: BLE001 — an unreadable approval store is no approval
        return CODE_APPROVAL_LOOKUP_ERROR, f"approval lookup failed: {type(exc).__name__}", ""
    status = getattr(outcome, "status", None)
    ref = str(getattr(outcome, "approval_ref", "") or "")
    detail = str(getattr(outcome, "reason", "") or "")
    code = _APPROVAL_CODES.get(status, CODE_APPROVAL_LOOKUP_ERROR) if isinstance(status, str) else \
        CODE_APPROVAL_LOOKUP_ERROR
    if code == CODE_APPROVED and not ref:
        return CODE_APPROVAL_LOOKUP_ERROR, "approval lookup granted without naming a receipt", ""
    if code == CODE_APPROVED:
        return code, f"rule {rule_id}: approval {ref} consumed for this action", ref
    if code == CODE_APPROVAL_REQUIRED:
        return code, f"rule {rule_id}: needs an approval bound to {request.action_digest()}", ref
    if code == CODE_APPROVAL_LOOKUP_ERROR:
        return code, f"approval lookup failed: {detail or status}", ref
    return code, f"rule {rule_id}: {code}{f' ({ref})' if ref else ''}{f': {detail}' if detail else ''}", ref


def decide(
    request: ActionRequest,
    *,
    policy: Policy = BUILTIN_POLICY,
    approvals: ApprovalLookup | None = None,
    approval_ref: str = "",
) -> Decision:
    """Decide and record. Never raises: every failure is a DENY decision.

    `approvals` defaults to `configured_approvals()`. For a REQUIRE_APPROVAL
    action the lookup is asked to spend a matching receipt NOW, so call
    this only immediately before the side effect it governs: a permitted
    decision has already used up its approval. `approval_ref` names the
    receipt to revalidate (the one a caller waited on)."""
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
    ref = ""
    if code == CODE_APPROVAL_REQUIRED:
        try:
            lookup = approvals if approvals is not None else configured_approvals()
        except Exception as exc:  # noqa: BLE001 — a store that cannot be built is no approval
            code, reason = CODE_APPROVAL_LOOKUP_ERROR, f"approval store unavailable: {type(exc).__name__}"
        else:
            code, reason, ref = _redeem(lookup, request, rule.rule_id if rule else "", policy.version, approval_ref)
    decision = Decision(_verdict_for(code), code, reason, policy.version, rule.rule_id if rule else "",
                        digest, ref)
    emit(request, decision)
    return decision


def enforce[T](
    request: ActionRequest,
    side_effect: Callable[[], T],
    *,
    policy: Policy = BUILTIN_POLICY,
    approvals: ApprovalLookup | None = None,
    approval_ref: str = "",
) -> T:
    """Run `side_effect` only if the decision permits it; otherwise raise
    `PolicyRefused` without calling it. The decision (and, for an approved
    action, the consume of its receipt) happens immediately before."""
    decision = decide(request, policy=policy, approvals=approvals, approval_ref=approval_ref)
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
    """Print one `POLICY_DECISION` line, and put the decision on the current
    trace span as a `mctl.policy.decision` event (mctl-agents#195). Never
    raises.

    The span event carries the bounded fields only — rule, verdict, code,
    policy version, action kind and operation. Not the target, not the free
    text `reason`, not even the args digest: the log line above is the audit
    record, the event is how a trace shows where in the run the decision
    fell. `orchestrator.tracing` is imported here rather than at module scope
    to keep this module's import stdlib-only by construction, and it is a
    no-op unless tracing is configured."""
    try:
        print(f"{DECISION_PREFIX} {json.dumps(decision_record(request, decision), sort_keys=True)}", flush=True)
    except Exception:  # noqa: BLE001, S110 — recording must never become the reason an action fails
        pass
    try:
        from orchestrator import tracing

        tracing.record_policy_decision(
            rule_id=decision.rule_id,
            decision=decision.verdict,
            code=decision.code,
            policy_version=decision.policy_version,
            action_kind=request.action_kind,
            operation=request.operation,
        )
    except Exception:  # noqa: BLE001, S110 — same rule: tracing never fails the action
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
    approvals: ApprovalLookup | None = None,
    approval_ref: str = "",
) -> Decision:
    """`request_for` + `decide`: the one call a governed path makes,
    immediately before its side effect. Never raises: every failure is a
    recorded refusal."""
    request = request_for(action_kind, operation, target, args, grants=grants, metadata=metadata, policy=policy)
    if isinstance(request, Decision):
        return request
    return decide(request, policy=policy, approvals=approvals, approval_ref=approval_ref)


def require(decision: Decision) -> None:
    """Raise `PolicyRefused` unless `decision` permits the action."""
    if not decision.permitted:
        raise PolicyRefused(decision)
