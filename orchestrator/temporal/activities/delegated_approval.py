"""Activities: the DevLoop approval through a bound grant
(mctlhq/mctl-agents#636, mctl-api#571, #376 Slice C).

The legacy path was `human signal -> free-text approver -> direct
mctl-agents-approve CWFT`: the worker named the human. Here the worker names
nobody. Four identities stay distinct end to end:

- the agent actor: an execution-scoped run token (`art_`) minted for the
  DevLoop's `we_`;
- the human subject: resolved by mctl-api from the human's decision on the
  `aar_`, never supplied by the worker;
- the execution `we_` the grant is bound to;
- the grant `aar_` the human decided, named by `X-MCTL-On-Behalf-Of`.

`request_devloop_approval` creates (or finds, by a deterministic key) the
`ActionApprovalRequest` before the human is asked, so mctl-api can resolve the
loop's open `we_` to it when the human approves. `submit_delegated_approve`
mints the run token, exercises the grant through
`POST /api/v1/operations/mctl-agents-approve/execute`, and polls the CWFT it
submitted. Mint and exercise are ONE activity on purpose: the plaintext token
lives in a local variable for the length of one HTTP call and never crosses
the activity boundary, so it cannot reach workflow history, a memo, a search
attribute, a heartbeat or an exception. Error text carries the HTTP status and
mctl-api's typed `code` only.

Fail closed. A typed refusal is returned as a value and the workflow ends; no
refusal, and no uncertainty, is ever answered with `approver=<login>` or a
direct CWFT submit. Uncertainty (transport, an untyped 5xx, a typed 503) is
raised and bounded by the workflow's retry policy. Resume rule, as in
`submit_and_wait`: once the execute POST succeeded its workflow name is
heartbeated, and a retry only resumes polling; it never mints or exercises a
second time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator import action_approvals as aa
from orchestrator.temporal.activities.argo import (
    MAX_CONSECUTIVE_POLL_ERRORS,
    POLL_INTERVAL_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    TERMINAL_PHASES,
    WorkflowResult,
)
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

APPROVE_OPERATION = "mctl-agents-approve"
APPROVE_ACTION_KIND = APPROVE_OPERATION
#: mctl-api rebuilds the intent with these exact values (handlers_approval_grant.go);
#: any other makes the grant a 409 `approval_intent_mismatch`.
POLICY_RULE_ID = "dev-loop.human-approval"
POLICY_VERSION = "v1"

#: The registered agent the run token is minted for. mctl-api refuses an
#: unregistered name (400 `agent_unknown`).
RUN_TOKEN_AGENT = "issue-investigator"  # noqa: S105 - an agent name, not a secret
#: The token only has to outlive one execute call and the CWFT it submits is
#: polled with the service credential, so a short life bounds a leak.
RUN_TOKEN_TTL_SECONDS = 900

ON_BEHALF_OF_HEADER = "X-MCTL-On-Behalf-Of"

#: A retryable failure: the store (or the approval/delegation path) gave no
#: usable answer. Bounded by the workflow's retry policy.
UNAVAILABLE_TYPE = "DelegatedApprovalUnavailable"

#: mctl-api's typed codes that mean "not now", not "no": the gate answered 503
#: before anything was consumed, so asking again is safe.
_RETRYABLE_CODES = frozenset({"delegation_unavailable", "work_items_unavailable", "agent_run_tokens_unavailable"})


@dataclass(frozen=True)
class ApprovalRequestInput:
    #: The `we_` the grant is bound to.
    execution_id: str
    service: str
    slug: str
    #: How long the request stays decidable (clamped to mctl-api's cap).
    ttl_seconds: int = aa.DEFAULT_TTL_S


@dataclass(frozen=True)
class ApprovalRequestResult:
    ok: bool
    approval_ref: str = ""
    state: str = ""
    reason: str = ""


def approval_target(service: str, slug: str) -> str:
    return f"{service}/{slug}"


def approval_intent(execution_id: str, service: str, slug: str) -> aa.ActionIntent:
    """The intent the human decides: this execution, this operation, this
    `<service>/<slug>`, under the DevLoop approval policy. The digest covers
    exactly the operation parameters, which carry no identity."""
    params = json.dumps({"service": service, "slug": slug}, sort_keys=True, separators=(",", ":"))
    return aa.ActionIntent(
        execution_id=execution_id,
        action_kind=APPROVE_ACTION_KIND,
        target=approval_target(service, slug),
        args_digest="sha256:" + hashlib.sha256(params.encode()).hexdigest(),
        policy_rule_id=POLICY_RULE_ID,
        policy_version=POLICY_VERSION,
    )


@activity.defn
async def request_devloop_approval(input: ApprovalRequestInput) -> ApprovalRequestResult:
    """Create the `aar_` for this DevLoop's approval, or find it again.

    Deterministic idempotency (`aa.idempotency_key`, attempt 0): a Temporal
    retry, or a replayed workflow task, finds the request instead of opening a
    second one, so the human is asked once for one intent."""
    intent = approval_intent(input.execution_id, input.service, input.slug)
    ttl = min(max(input.ttl_seconds, aa.MIN_TTL_S), aa.MAX_TTL_S)
    answer = await asyncio.to_thread(
        aa.ActionApprovalClient().create,
        intent,
        key=aa.idempotency_key(intent),
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
    )
    if answer.status == aa.UNKNOWN or answer.record is None:
        raise ApplicationError(f"approval request for {intent.target}: {answer.reason}", type=UNAVAILABLE_TYPE)
    if answer.status not in (aa.PENDING, aa.APPROVED):
        # denied / expired / consumed / mismatch / refused: the request for
        # this intent exists in a state that cannot be asked again here.
        return ApprovalRequestResult(
            ok=False, approval_ref=answer.record.id, state=answer.status,
            reason=f"approval {answer.record.id} for {intent.target} is {answer.status}",
        )
    if answer.record.intent_hash != aa.intent_hash(intent):
        return ApprovalRequestResult(
            ok=False, approval_ref=answer.record.id, state=aa.MISMATCH,
            reason=f"approval {answer.record.id} is not bound to this intent",
        )
    return ApprovalRequestResult(ok=True, approval_ref=answer.record.id, state=answer.status)


@dataclass(frozen=True)
class DelegatedApproveInput:
    #: The `we_` the grant and the run token are bound to.
    execution_id: str
    #: The `aar_` the human decided, as signalled to the workflow. It is the
    #: only authority: no login, email or principal is carried anywhere.
    approval_ref: str
    service: str
    slug: str


@dataclass(frozen=True)
class DelegatedApproveResult:
    #: The approve CWFT's terminal result. None when it never ran.
    result: WorkflowResult | None = None
    #: A definite refusal, `HTTP <status> <typed code>`. Never retried and
    #: never answered with the legacy path.
    refusal: str = ""
    #: mctl-api's typed `code` of the refusal, when it sent one.
    code: str = ""


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _code_of(response: httpx.Response) -> str:
    code = _json_object(response).get("code")
    return code if isinstance(code, str) and len(code) <= 64 else ""


def _refusal_of(what: str, response: httpx.Response) -> DelegatedApproveResult | None:
    """Classify a non-2xx answer: a definite refusal (returned), or None when
    it is uncertainty the caller raises for a bounded retry. Only the status
    and the typed code are ever kept."""
    code = _code_of(response)
    status = response.status_code
    if status >= 500 and not code:
        return None
    if code in _RETRYABLE_CODES or status in (408, 425, 429):
        return None
    return DelegatedApproveResult(refusal=f"{what}: HTTP {status} {code}".strip(), code=code)


@activity.defn
async def submit_delegated_approve(input: DelegatedApproveInput) -> DelegatedApproveResult:
    """Mint the run token for `input.execution_id`, exercise `input.approval_ref`
    through the operation endpoint, and poll the CWFT it submitted."""
    heartbeat = activity.info().heartbeat_details
    workflow_name = heartbeat[0] if heartbeat else None
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        service_headers = auth_headers()
        if not workflow_name:
            outcome = await _mint_and_execute(client, service_headers, input)
            if isinstance(outcome, DelegatedApproveResult):
                return outcome
            workflow_name = outcome
            activity.logger.info("submitted %s -> %s", APPROVE_OPERATION, workflow_name)
            # The grant is spent from here on: record the name before the
            # first poll so a retry resumes instead of exercising it again.
            activity.heartbeat(workflow_name)
        else:
            activity.logger.info("resuming poll for %s -> %s (no re-exercise)", APPROVE_OPERATION, workflow_name)
        return DelegatedApproveResult(result=await _poll(client, service_headers, workflow_name))


async def _mint_and_execute(
    client: httpx.AsyncClient, service_headers: dict[str, str], input: DelegatedApproveInput
) -> DelegatedApproveResult | str:
    """The workflow name of the submitted CWFT, or a definite refusal."""
    minted = await client.post(
        "/api/v1/agent-run-tokens",
        json={
            "agent": RUN_TOKEN_AGENT,
            "execution_id": input.execution_id,
            "ttl_seconds": RUN_TOKEN_TTL_SECONDS,
            # The grant authorizes this operation; the token needs no permission.
            "permissions": [],
        },
        headers=service_headers,
    )
    if minted.status_code != 201:
        refusal = _refusal_of("agent run token", minted)
        if refusal is None:
            raise ApplicationError(
                f"agent run token: HTTP {minted.status_code} {_code_of(minted)}".strip(), type=UNAVAILABLE_TYPE
            )
        return refusal
    token = _json_object(minted).get("token")
    if not isinstance(token, str) or not token:
        # The mint succeeded but the answer is unusable: nothing was exercised.
        raise ApplicationError("agent run token: the response carried no token", type=UNAVAILABLE_TYPE)
    submitted = await client.post(
        f"/api/v1/operations/{APPROVE_OPERATION}/execute",
        # Operation parameters only. No `approver`, login, email or `prn_`:
        # mctl-api refuses the first and resolves the subject itself.
        json={"service": input.service, "slug": input.slug},
        headers={"Authorization": f"Bearer {token}", ON_BEHALF_OF_HEADER: input.approval_ref},
    )
    if submitted.status_code not in (200, 202):
        refusal = _refusal_of(APPROVE_OPERATION, submitted)
        if refusal is None:
            raise ApplicationError(
                f"{APPROVE_OPERATION}: HTTP {submitted.status_code} {_code_of(submitted)}".strip(),
                type=UNAVAILABLE_TYPE,
            )
        return refusal
    workflow = _json_object(submitted).get("workflow")
    name = workflow.get("workflowName") if isinstance(workflow, dict) else None
    if not isinstance(name, str) or not name:
        # Accepted but nameless: the grant is spent and the run exists. Refuse
        # to guess, and to retry into a consumed grant.
        return DelegatedApproveResult(refusal=f"{APPROVE_OPERATION}: accepted without a workflow name")
    return name


async def _poll(client: httpx.AsyncClient, headers: dict[str, str], workflow_name: str) -> WorkflowResult:
    errors = 0
    while True:
        activity.heartbeat(workflow_name)
        try:
            response = await client.get(f"/api/v1/workflows/{workflow_name}", headers=headers)
            response.raise_for_status()
        except (httpx.HTTPStatusError, httpx.TransportError):
            errors += 1
            if errors > MAX_CONSECUTIVE_POLL_ERRORS:
                raise
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            continue
        errors = 0
        status = (_json_object(response).get("live") or {}).get("status") or {}
        phase = status.get("phase", "")
        if phase in TERMINAL_PHASES:
            return WorkflowResult(
                workflow_name=workflow_name, phase=phase,
                started_at=status.get("startedAt"), finished_at=status.get("finishedAt"),
            )
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
