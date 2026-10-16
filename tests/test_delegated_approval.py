"""The DevLoop approval through a bound grant (mctlhq/mctl-agents#636).

`request_devloop_approval` asks mctl-api for the `aar_` of exactly one flip of
one `we_`. `submit_delegated_approve` mints an execution-scoped run token and
exercises the grant the human decided. These tests pin the properties the
issue calls out: the run token (not the service token) is the bearer, the
header names the signalled `aar_`, no human identity travels, the plaintext
token never leaves the activity, a typed refusal is a value (never a legacy
fallback), and a retry after the grant was spent only polls.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from orchestrator import action_approvals as aa
from orchestrator.temporal.activities import delegated_approval as da
from tests.test_action_approvals import FakeMctlApi, _Clock

pytestmark = pytest.mark.anyio

SERVICE_TOKEN = "service-token-not-the-run-token"
RUN_TOKEN = "mctl_art_PLAINTEXT_RUN_TOKEN_0123456789"
WE = "we_11111111-1111-4111-8111-111111111111"
AAR = "aar_00000000000000000000000000000001"
INPUT = da.DelegatedApproveInput(execution_id=WE, approval_ref=AAR, service="mctl-telegram", slug="issue-7-title")


@pytest.fixture(autouse=True)
def _service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCTL_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(da, "POLL_INTERVAL_SECONDS", 0)


@pytest.fixture
def env() -> ActivityEnvironment:
    return ActivityEnvironment()


def _route(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class _Api:
    """A scripted mctl-api: records every request, answers the mint, the
    operation and the poll as configured."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.mint: httpx.Response = httpx.Response(201, json={"token_id": "art_1", "token": RUN_TOKEN})
        self.execute: httpx.Response = httpx.Response(202, json={"workflow": {"workflowName": "approve-ab12"}})
        self.phases: list[str] = ["Running", "Succeeded"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/agent-run-tokens":
            return self.mint
        if path == "/api/v1/operations/mctl-agents-approve/execute":
            return self.execute
        if path.startswith("/api/v1/workflows/"):
            phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
            return httpx.Response(200, json={"live": {"status": {"phase": phase}}})
        raise AssertionError(f"unexpected request {request.method} {path}")

    def to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> _Api:
    scripted = _Api()
    _route(monkeypatch, scripted)
    return scripted


EXECUTE = "/api/v1/operations/mctl-agents-approve/execute"
MINT = "/api/v1/agent-run-tokens"


# ---------------------------------------------------------------------------
# The happy path: what is sent, and as whom
# ---------------------------------------------------------------------------


async def test_the_run_token_is_the_bearer_and_the_header_names_the_signalled_grant(env, api):
    """Mutation pins 2 and 3: drop the header, or send the service token."""
    result = await env.run(da.submit_delegated_approve, INPUT)

    assert result.refusal == "" and result.result is not None and result.result.succeeded
    mint, execute = api.to(MINT)[0], api.to(EXECUTE)[0]
    # The mint is the service's own act, for exactly this `we_`.
    assert mint.headers["authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert json.loads(mint.content) == {
        "agent": da.RUN_TOKEN_AGENT, "execution_id": WE, "ttl_seconds": da.RUN_TOKEN_TTL_SECONDS, "permissions": [],
    }
    # The operation is exercised as the minted agent, on behalf of the grant.
    assert execute.headers["authorization"] == f"Bearer {RUN_TOKEN}"
    assert execute.headers["authorization"] != f"Bearer {SERVICE_TOKEN}"
    assert execute.headers[da.ON_BEHALF_OF_HEADER] == AAR


async def test_the_operation_body_is_service_and_slug_and_nothing_else(env, api):
    """Mutation pin 4: no `approver`, login, e-mail, `prn_` or subject, in the
    body or anywhere else in any request."""
    await env.run(da.submit_delegated_approve, INPUT)

    assert json.loads(api.to(EXECUTE)[0].content) == {"service": "mctl-telegram", "slug": "issue-7-title"}
    for request in api.requests:
        wire = request.content.decode() + json.dumps(dict(request.headers))
        for forbidden in ("approver", "login", "email", "prn_", "subject", "legacy_relay"):
            assert forbidden not in wire.lower(), (request.url.path, forbidden)


async def test_the_poll_uses_the_service_credential_not_the_run_token(env, api):
    await env.run(da.submit_delegated_approve, INPUT)

    polls = [r for r in api.requests if r.url.path.startswith("/api/v1/workflows/")]
    assert polls and all(r.headers["authorization"] == f"Bearer {SERVICE_TOKEN}" for r in polls)
    assert all(r.url.path == "/api/v1/workflows/approve-ab12" for r in polls)


async def test_the_workflow_name_is_heartbeated_before_the_first_poll(env, api):
    heartbeats: list[tuple] = []
    env.on_heartbeat = lambda *details: heartbeats.append(details)

    await env.run(da.submit_delegated_approve, INPUT)

    assert heartbeats[0] == ("approve-ab12",)


# ---------------------------------------------------------------------------
# The plaintext token never leaves the activity
# ---------------------------------------------------------------------------


async def test_the_plaintext_token_is_in_no_result_heartbeat_or_log(env, api, caplog):
    heartbeats: list[tuple] = []
    env.on_heartbeat = lambda *details: heartbeats.append(details)

    with caplog.at_level("DEBUG"):
        result = await env.run(da.submit_delegated_approve, INPUT)

    assert RUN_TOKEN not in repr(result)
    assert RUN_TOKEN not in repr(heartbeats)
    assert RUN_TOKEN not in caplog.text and SERVICE_TOKEN not in caplog.text


@pytest.mark.parametrize("status", [403, 404, 409, 500, 503])
async def test_the_plaintext_token_is_in_no_failure_text_either(env, api, status):
    api.execute = httpx.Response(status, json={"error": f"nope {RUN_TOKEN}", "code": "grant_not_bound"})

    try:
        outcome = await env.run(da.submit_delegated_approve, INPUT)
        text = repr(outcome)
    except ApplicationError as exc:
        text = f"{exc} {exc.type}"
    assert RUN_TOKEN not in text and SERVICE_TOKEN not in text


# ---------------------------------------------------------------------------
# Fail closed: typed refusals are values, never a fallback
# ---------------------------------------------------------------------------

#: (HTTP status, typed code) of every refusal the issue names.
EXECUTE_REFUSALS = [
    (403, "grant_not_bound"),
    (403, "grant_subject_unresolved"),
    (409, "approval_intent_mismatch"),
    (409, "approval_consumed"),
    (409, "approval_expired"),
    (409, "approval_denied"),
    (409, "approval_not_approved"),
    (403, "approval_grant_missing"),
    (409, "execution_ambiguous"),
    (403, "delegation_not_supported"),
    (400, "actor_not_accepted"),
    (401, "agent_token_invalid"),
]


@pytest.mark.parametrize(("status", "code"), EXECUTE_REFUSALS)
async def test_a_typed_refusal_of_the_operation_is_a_value_and_nothing_is_polled(env, api, status, code):
    api.execute = httpx.Response(status, json={"error": "refused", "code": code})

    result = await env.run(da.submit_delegated_approve, INPUT)

    assert result.result is None
    assert result.code == code and f"HTTP {status}" in result.refusal
    assert api.to(MINT) and len(api.to(EXECUTE)) == 1
    assert not [r for r in api.requests if r.url.path.startswith("/api/v1/workflows/")]


@pytest.mark.parametrize(("status", "code"), [(400, "agent_unknown"), (403, "execution_not_running"), (404, "")])
async def test_a_refused_mint_is_a_value_and_the_operation_is_never_called(env, api, status, code):
    api.mint = httpx.Response(status, json={"error": "refused", "code": code} if code else {})

    result = await env.run(da.submit_delegated_approve, INPUT)

    assert result.result is None and result.refusal.startswith("agent run token")
    assert api.to(EXECUTE) == []


RETRYABLE = [
    (503, "delegation_unavailable"),
    (503, "work_items_unavailable"),
    (503, "agent_run_tokens_unavailable"),
    (502, ""),
    (500, ""),
    # A typed 5xx is uncertainty too, whatever its code says.
    (500, "internal_error"),
    (504, "gateway_timeout"),
    (429, ""),
]


@pytest.mark.parametrize(("status", "code"), RETRYABLE)
async def test_uncertainty_at_the_operation_is_raised_for_a_bounded_retry(env, api, status, code):
    api.execute = httpx.Response(status, json={"code": code} if code else {})

    with pytest.raises(ApplicationError) as caught:
        await env.run(da.submit_delegated_approve, INPUT)

    assert caught.value.type == da.UNAVAILABLE_TYPE
    assert not caught.value.non_retryable


@pytest.mark.parametrize(("status", "code"), RETRYABLE)
async def test_uncertainty_at_the_mint_is_raised_and_the_operation_is_never_called(env, api, status, code):
    api.mint = httpx.Response(status, json={"code": code} if code else {})

    with pytest.raises(ApplicationError) as caught:
        await env.run(da.submit_delegated_approve, INPUT)

    assert caught.value.type == da.UNAVAILABLE_TYPE
    assert api.to(EXECUTE) == []


async def test_a_mint_that_carries_no_token_is_uncertainty_not_a_call_without_one(env, api):
    api.mint = httpx.Response(201, json={"token_id": "art_1"})

    with pytest.raises(ApplicationError):
        await env.run(da.submit_delegated_approve, INPUT)

    assert api.to(EXECUTE) == []


async def test_an_accepted_operation_without_a_workflow_name_is_a_refusal_not_a_retry(env, api):
    """The grant is spent; a retry would meet `approval_consumed`. Say so."""
    api.execute = httpx.Response(202, json={})

    result = await env.run(da.submit_delegated_approve, INPUT)

    assert result.result is None and "without a workflow name" in result.refusal


async def test_a_transport_failure_at_the_operation_is_not_answered_by_the_legacy_path(env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MINT:
            return httpx.Response(201, json={"token": RUN_TOKEN})
        raise httpx.ConnectError("boom")

    _route(monkeypatch, handler)

    with pytest.raises(httpx.ConnectError):
        await env.run(da.submit_delegated_approve, INPUT)


# ---------------------------------------------------------------------------
# A retry after the grant was spent only polls
# ---------------------------------------------------------------------------


async def test_a_retry_with_a_heartbeated_name_polls_and_never_mints_or_exercises_again(env, api):
    env.info = dataclasses.replace(env.info, heartbeat_details=["approve-ab12"])

    result = await env.run(da.submit_delegated_approve, INPUT)

    assert result.result is not None and result.result.workflow_name == "approve-ab12"
    assert api.to(MINT) == [] and api.to(EXECUTE) == []


async def test_a_failed_cwft_is_a_failed_result_not_a_refusal(env, api):
    api.phases = ["Failed"]

    result = await env.run(da.submit_delegated_approve, INPUT)

    assert result.refusal == "" and result.result is not None and result.result.phase == "Failed"


# ---------------------------------------------------------------------------
# request_devloop_approval
# ---------------------------------------------------------------------------


@pytest.fixture
def aar_api(monkeypatch: pytest.MonkeyPatch) -> FakeMctlApi:
    clock = _Clock()
    clock.now = datetime.now(UTC)
    fake = FakeMctlApi(clock)
    real = aa.ActionApprovalClient

    def factory() -> aa.ActionApprovalClient:
        client = real(base_url="https://api.example.test", token="test-token")  # noqa: S106
        client._opener = fake  # type: ignore[assignment]
        return client

    monkeypatch.setattr(da.aa, "ActionApprovalClient", factory)
    return fake


REQUEST = da.ApprovalRequestInput(execution_id=WE, service="mctl-telegram", slug="issue-7-title", ttl_seconds=3600)


async def test_the_grant_is_bound_to_the_exact_we_and_the_flips_target(env, aar_api):
    result = await env.run(da.request_devloop_approval, REQUEST)

    assert result.ok and result.approval_ref.startswith("aar_") and result.state == aa.PENDING
    record = aar_api.only()
    assert record["execution_id"] == WE
    assert record["action_kind"] == "mctl-agents-approve"
    assert record["target"] == "mctl-telegram/issue-7-title"
    assert record["policy_rule_id"] == "dev-loop.human-approval" and record["policy_version"] == "v1"
    assert record["args_digest"] == da.approval_intent(WE, "mctl-telegram", "issue-7-title").args_digest


async def test_asking_twice_finds_the_same_grant(env, aar_api):
    first = await env.run(da.request_devloop_approval, REQUEST)
    second = await env.run(da.request_devloop_approval, REQUEST)

    assert first.approval_ref == second.approval_ref and len(aar_api.records) == 1


async def test_another_execution_or_target_is_another_grant(env, aar_api):
    one = await env.run(da.request_devloop_approval, REQUEST)
    other_we = await env.run(
        da.request_devloop_approval,
        dataclasses.replace(REQUEST, execution_id="we_22222222-2222-4222-8222-222222222222"),
    )
    other_slug = await env.run(da.request_devloop_approval, dataclasses.replace(REQUEST, slug="issue-8-title"))

    assert len({one.approval_ref, other_we.approval_ref, other_slug.approval_ref}) == 3


async def test_a_new_attempt_asks_for_a_new_decision(env, aar_api):
    """agy P2 on #651: after a surface/actor transition the loop asks again
    under the next attempt; that must be a fresh grant, not the old one."""
    first = await env.run(da.request_devloop_approval, REQUEST)
    aar_api.records[first.approval_ref]["state"] = "denied"

    again = await env.run(da.request_devloop_approval, dataclasses.replace(REQUEST, attempt=1))

    assert again.ok and again.approval_ref != first.approval_ref and len(aar_api.records) == 2


@pytest.mark.parametrize("state", ["denied", "expired", "consumed"])
async def test_a_request_already_in_a_final_state_is_not_asked_again(env, aar_api, state):
    first = await env.run(da.request_devloop_approval, REQUEST)
    aar_api.records[first.approval_ref]["state"] = state

    again = await env.run(da.request_devloop_approval, REQUEST)

    assert not again.ok and again.state == state and again.approval_ref == first.approval_ref


async def test_an_unreadable_store_is_raised_not_read_as_a_grant(env, aar_api):
    aar_api.override["create"] = lambda req: FakeMctlApi._err(503, "work_items_unavailable")

    with pytest.raises(ApplicationError) as caught:
        await env.run(da.request_devloop_approval, REQUEST)

    assert caught.value.type == da.UNAVAILABLE_TYPE


async def test_the_ttl_is_clamped_to_what_mctl_api_accepts(env, aar_api):
    await env.run(da.request_devloop_approval, dataclasses.replace(REQUEST, ttl_seconds=30 * 24 * 3600))

    expires = datetime.fromisoformat(aar_api.only()["expires_at"])
    assert (expires - datetime.now(UTC)).total_seconds() <= aa.MAX_TTL_S + 5
