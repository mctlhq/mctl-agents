"""End-to-End integration test suite for Paid Capability Alpha (projects_search)
connecting mctl-agents CommerceRuntime / PaidInvocation with the live ephemeral
projects-mcp server fixture over Fastify HTTP/MCP transport.

Tests all acceptance criteria:
- Tool listing over Fastify /mcp transport
- Direct call origin bypass fail-closed protection
- Full paid cycle: terms -> quote -> reserve -> dispatch -> execution -> finalize
- Deduplication and idempotency on replay without double charge
- Tampering detection: altered arguments, expired deadline, bad signature, tenant mismatch
- Provider failure rollback (hold release)
- Budget exhaustion rejection before dispatch
- Outcome resolution and caller isolation on /commerce/outcome/{key}
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import select
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from orchestrator import capability as cap
from orchestrator import commerce as cm
from orchestrator import commerce_gateway as cg
from orchestrator import commerce_provider as cp
from orchestrator import policy_checkpoint as pc
from tests.commerce_fakes import (
    ALLOW_PAID,
    EXECUTION,
    KID,
    SEED,
    Clock,
    FakeApprovals,
    FakeLedger,
)


def _find_projects_mcp_dir() -> Path:
    env_dir = os.environ.get("PROJECTS_MCP_DIR")
    if env_dir:
        return Path(env_dir)
    for candidate in [
        Path(__file__).resolve().parent.parent.parent / "projects-mcp",
        Path(__file__).resolve().parent.parent.parent.parent / "projects-mcp",
        Path.cwd().parent / "projects-mcp",
        Path.cwd() / "projects-mcp",
    ]:
        if candidate.exists() and (candidate / "tools/serve-test.ts").exists():
            return candidate
    return Path("")


PROJECTS_MCP_DIR = _find_projects_mcp_dir()
HAVE_PROJECTS_MCP = (
    PROJECTS_MCP_DIR != Path("")
    and PROJECTS_MCP_DIR.exists()
    and (PROJECTS_MCP_DIR / "tools/serve-test.ts").exists()
    and shutil.which("npx") is not None
)


@pytest.fixture(scope="module")
def projects_mcp_server(tmp_path_factory):
    """Spawns an ephemeral projects-mcp server using tools/serve-test.ts."""
    if not HAVE_PROJECTS_MCP:
        pytest.skip(f"projects-mcp server fixture not available at {PROJECTS_MCP_DIR}")

    tmp_dir = tmp_path_factory.mktemp("projects_mcp")
    dedupe_db = tmp_dir / "dedupe.db"

    pub_b64 = base64.b64encode(cm.public_key_for(SEED)).decode()
    env = dict(
        os.environ,
        NODE_ENV="test",
        COMMERCE_DISPATCH_PUBLIC_KEYS=f"{KID}:{pub_b64}",
        COMMERCE_BOUND_TENANT_ID="acme",
        COMMERCE_DEDUPE_DB_PATH=str(dedupe_db),
    )

    proc = subprocess.Popen(
        ["npx", "tsx", "tools/serve-test.ts"],
        cwd=str(PROJECTS_MCP_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    server_url = ""
    captured_lines = []
    try:
        # Wait up to 10 seconds for READY line non-blockingly while draining merged stdout/stderr
        start_wait = time.time()
        while time.time() - start_wait < 10.0:
            if proc.poll() is not None:
                remaining = proc.stdout.read() if proc.stdout else ""
                diag = "".join(captured_lines) + remaining
                raise RuntimeError(f"projects-mcp server exited prematurely (code {proc.returncode}):\n{diag}")
            rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
            if not rlist:
                continue
            line = proc.stdout.readline()
            if not line:
                continue
            captured_lines.append(line)
            if line.startswith("READY "):
                server_url = line.strip().split(" ")[1]
                break

        if not server_url:
            proc.terminate()
            diag = "".join(captured_lines[-20:])
            raise TimeoutError(f"Timed out waiting for projects-mcp server to emit READY. Recent output:\n{diag}")

        yield SimpleNamespace(url=server_url)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def e2e_harness(projects_mcp_server, monkeypatch, request):
    """Sets up an end-to-end commerce test harness with live wall-clock time."""
    clock = Clock()
    clock.t = datetime.now(UTC)

    # Derive deterministic id seed per test from pytest nodeid to ensure disjoint
    # invocation ID spaces without global mutable state and safe under pytest-xdist.
    id_seed = int(hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:6], 16) * 1000
    ledger = FakeLedger(clock, budget=100, id_seed=id_seed)
    approvals = FakeApprovals(clock)
    ledger.publish(
        cp.PROJECTS_SEARCH_CAPABILITY_ID,
        cp.PROJECTS_SEARCH_SCHEMA_HASH,
        provider_id=cp.PROJECTS_MCP_PROVIDER_ID,
    )

    monkeypatch.setattr(
        pc,
        "current_identity",
        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-e2e-1", actor="agent:test"),
    )

    session = cp.HttpProjectsMcpPaidSession(base_url=projects_mcp_server.url, access_token="good")  # noqa: S106
    runtime = cg.CommerceRuntime(
        client=ledger,
        tenant_id="acme",
        admitted_providers=frozenset({cp.PROJECTS_MCP_PROVIDER_ID}),
        approvals=approvals,
        now=clock,
        call_timeout_s=5.0,
    )

    provider_ref = cap.ProviderRef(type="mcp-remote", id=cp.PROJECTS_MCP_PROVIDER_ID, alias="projects")
    descriptor = cap.CapabilityDescriptor(
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        tool_name="mcp__projects__projects_search",
        provider=provider_ref,
        title="projects_search",
        summary="Search project docs",
        keywords=("projects", "search"),
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        input_schema_bytes=100,
        consequence="safe",
        matched_tool_pattern="mcp__projects__*",
        commerce=cap.CommerceSummary(
            terms_id="ctm_01JZ8PZ0000000000000000000",
            terms_version=1,
            price_mode="per_successful_call",
            currency="EUR",
            amount_minor=12,
            funds_mode="simulated",
        ),
    )

    harness = SimpleNamespace(
        clock=clock,
        ledger=ledger,
        approvals=approvals,
        session=session,
        runtime=runtime,
        descriptor=descriptor,
        server_url=projects_mcp_server.url,
    )
    yield harness
    try:
        asyncio.run(session.close())
    except (RuntimeError, httpx.RequestError):
        pass


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


def test_tools_list_over_mcp_http(e2e_harness):
    """HttpProjectsMcpPaidSession discovers tools exposed by projects-mcp over /mcp."""
    async def run():
        tools = await e2e_harness.session.list_tools()
        assert len(tools) >= 5
        names = {t.name for t in tools}
        assert cp.PROJECTS_SEARCH_TOOL in names
        assert "projects_list" in names
        search_tool = next(t for t in tools if t.name == cp.PROJECTS_SEARCH_TOOL)
        assert search_tool.input_schema.get("type") == "object"
        assert search_tool.input_schema.get("required") == ["project", "query"]
        # Fastify / MCP SDK injects _mctl_dispatch_authorization in-band for transport
        # and adds "$schema": "http://json-schema.org/draft-07/schema#" upon tools/list;
        # both are stripped to reconstruct the canonical input schema for digest verification.
        clean_remote_props = {
            k: v for k, v in search_tool.input_schema["properties"].items()
            if k != "_mctl_dispatch_authorization"
        }
        assert set(clean_remote_props.keys()) == {"project", "query", "limit"}
        canonical_remote_schema = {
            k: (clean_remote_props if k == "properties" else v)
            for k, v in search_tool.input_schema.items()
            if k != "$schema"
        }
        derived_remote_hash = cp.hash_bytes(cp.canonical_json(canonical_remote_schema))
        assert derived_remote_hash == cp.PROJECTS_SEARCH_SCHEMA_HASH

    asyncio.run(run())


def test_direct_call_without_authorization_refused(e2e_harness):
    """Direct unauthenticated or unsigned call_tool fails closed with DispatchRefused."""
    async def run():
        with pytest.raises(cm.DispatchRefused, match="a paid capability requires a dispatch authorization"):
            await e2e_harness.session.call_tool("projects_search", {"project": "acme", "query": "auth"})

    asyncio.run(run())


def test_successful_paid_call_e2e(e2e_harness):
    """Full end-to-end governed execution cycle:
    terms -> quote -> accept -> reserve -> dispatch (Ed25519) -> execute -> finalize.
    """
    async def run():
        assert e2e_harness.ledger.budget == {"available": 100, "reserved": 0, "committed": 0}

        args = {"project": "acme", "query": "auth", "limit": 8}
        paid = cg.PaidInvocation(
            runtime=e2e_harness.runtime,
            descriptor=e2e_harness.descriptor,
            session=e2e_harness.session,
            tool=cp.PROJECTS_SEARCH_TOOL,
            arguments=args,
            grants=("mcp__projects__*",),
            policy=ALLOW_PAID,
            client_key="k_success_1",
            emit=lambda p, event: None,
        )

        res = await paid.run()
        assert res.reason_code == "ok"
        assert res.phase == "finalized"
        assert res.text is not None and "project" in res.text

        # 12 units committed, 0 reserved, budget available remains 100
        assert e2e_harness.ledger.budget == {"available": 100, "reserved": 0, "committed": 12}

        # Verify outcome lookup on Fastify endpoint
        inv_id = res.invocation_id
        assert inv_id
        op_key = cm.provider_operation_key(inv_id)
        outcome = await e2e_harness.session.outcome(op_key)
        assert outcome.status == "succeeded"
        assert not outcome.is_error
        assert "project" in outcome.text

    asyncio.run(run())


def test_idempotent_replay_deduplication(e2e_harness):
    """Replaying the same invocation key returns cached result without double execution
    and without charging an extra unit.
    """
    async def run():
        args = {"project": "acme", "query": "auth", "limit": 8}
        client_key = "k_replay_unique"

        paid1 = cg.PaidInvocation(
            runtime=e2e_harness.runtime,
            descriptor=e2e_harness.descriptor,
            session=e2e_harness.session,
            tool=cp.PROJECTS_SEARCH_TOOL,
            arguments=args,
            grants=("mcp__projects__*",),
            policy=ALLOW_PAID,
            client_key=client_key,
            emit=lambda p, event: None,
        )
        res1 = await paid1.run()
        assert res1.reason_code == "ok"
        assert e2e_harness.ledger.budget["committed"] == 12

        # Second call with identical client_key
        paid2 = cg.PaidInvocation(
            runtime=e2e_harness.runtime,
            descriptor=e2e_harness.descriptor,
            session=e2e_harness.session,
            tool=cp.PROJECTS_SEARCH_TOOL,
            arguments=args,
            grants=("mcp__projects__*",),
            policy=ALLOW_PAID,
            client_key=client_key,
            emit=lambda p, event: None,
        )
        res2 = await paid2.run()
        assert res2.reason_code == "ok"
        assert res2.phase == "finalized"
        assert res2.invocation_id == res1.invocation_id

        # Ledger committed units must NOT have increased!
        assert e2e_harness.ledger.budget["committed"] == 12
        assert e2e_harness.ledger.budget["reserved"] == 0

    asyncio.run(run())


def test_tampered_arguments_rejected(e2e_harness):
    """Ingress verifies args digest against signed Ed25519 authorization;
    altering arguments causes DispatchRefused.
    """
    async def run():
        inv_id = "inv_tamper_args_test"
        deadline = cm.hashed_time(datetime.now(UTC) + timedelta(seconds=60))
        valid_args = {"project": "acme", "query": "auth", "limit": 8}
        raw_auth = cm.DispatchAuthorization(
            key_id=KID,
            invocation_id=inv_id,
            provider_operation_key="mctl-op/v1:" + inv_id,
            provider_id="projects-mcp",
            capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
            input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
            args_digest=cm.args_digest(valid_args),
            tenant_id="acme",
            dispatch_deadline=deadline,
        )
        signed_auth = cm.sign_dispatch_authorization(SEED, raw_auth)

        # Alter arguments: query changed
        tampered_args = {"project": "acme", "query": "altered_payload", "limit": 8}
        with pytest.raises(cm.DispatchRefused, match="authorization was signed for other arguments"):
            await e2e_harness.session.call_paid_tool(cp.PROJECTS_SEARCH_TOOL, tampered_args, signed_auth)

    asyncio.run(run())


def test_expired_deadline_rejected(e2e_harness):
    """Ingress verifies dispatch deadline; expired token causes DispatchRefused."""
    async def run():
        inv_id = "inv_expired_deadline_test"
        # Deadline 10 minutes in the past
        past_deadline = cm.hashed_time(datetime.now(UTC) - timedelta(minutes=10))
        args = {"project": "acme", "query": "auth", "limit": 8}
        raw_auth = cm.DispatchAuthorization(
            key_id=KID,
            invocation_id=inv_id,
            provider_operation_key="mctl-op/v1:" + inv_id,
            provider_id="projects-mcp",
            capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
            input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
            args_digest=cm.args_digest(args),
            tenant_id="acme",
            dispatch_deadline=past_deadline,
        )
        signed_auth = cm.sign_dispatch_authorization(SEED, raw_auth)

        with pytest.raises(cm.DispatchRefused, match="dispatch deadline has passed"):
            await e2e_harness.session.call_paid_tool(cp.PROJECTS_SEARCH_TOOL, args, signed_auth)

    asyncio.run(run())


def test_bad_signature_rejected(e2e_harness):
    """Token signed with an untrusted seed fails cryptographic verification."""
    async def run():
        inv_id = "inv_bad_sig_test"
        deadline = cm.hashed_time(datetime.now(UTC) + timedelta(seconds=60))
        args = {"project": "acme", "query": "auth", "limit": 8}
        raw_auth = cm.DispatchAuthorization(
            key_id=KID,
            invocation_id=inv_id,
            provider_operation_key="mctl-op/v1:" + inv_id,
            provider_id="projects-mcp",
            capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
            input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
            args_digest=cm.args_digest(args),
            tenant_id="acme",
            dispatch_deadline=deadline,
        )
        other_seed = b"\x99" * 32
        forged_auth = cm.sign_dispatch_authorization(other_seed, raw_auth)

        with pytest.raises(cm.DispatchRefused, match="signature does not verify"):
            await e2e_harness.session.call_paid_tool(cp.PROJECTS_SEARCH_TOOL, args, forged_auth)

    asyncio.run(run())


def test_wrong_tenant_rejected(e2e_harness):
    """Ingress bound to tenant 'acme' rejects token naming tenant 'evil-corp'."""
    async def run():
        inv_id = "inv_wrong_tenant_test"
        deadline = cm.hashed_time(datetime.now(UTC) + timedelta(seconds=60))
        args = {"project": "acme", "query": "auth", "limit": 8}
        raw_auth = cm.DispatchAuthorization(
            key_id=KID,
            invocation_id=inv_id,
            provider_operation_key="mctl-op/v1:" + inv_id,
            provider_id="projects-mcp",
            capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
            input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
            args_digest=cm.args_digest(args),
            tenant_id="evil-corp",
            dispatch_deadline=deadline,
        )
        signed_auth = cm.sign_dispatch_authorization(SEED, raw_auth)

        with pytest.raises(cm.DispatchRefused, match="authorization names another tenant"):
            await e2e_harness.session.call_paid_tool(cp.PROJECTS_SEARCH_TOOL, args, signed_auth)

    asyncio.run(run())


def test_provider_refusal_releases_hold(e2e_harness):
    """When ingress refuses dispatch (e.g. tampering or bad key), PaidInvocation
    releases the reservation back to available budget without charging.
    """
    async def run():
        initial_available = e2e_harness.ledger.free()
        args = {"project": "acme", "query": "auth", "limit": 8}

        # Session configured with invalid token so ingress refuses or throws
        bad_session = cp.HttpProjectsMcpPaidSession(
            base_url=e2e_harness.server_url, access_token="invalid_jwt_token",  # noqa: S106
        )

        paid = cg.PaidInvocation(
            runtime=e2e_harness.runtime,
            descriptor=e2e_harness.descriptor,
            session=bad_session,
            tool=cp.PROJECTS_SEARCH_TOOL,
            arguments=args,
            grants=("mcp__projects__*",),
            policy=ALLOW_PAID,
            client_key="k_refusal_rollback",
            emit=lambda p, event: None,
        )

        res = await paid.run()
        assert res.reason_code == "provider-error"
        assert res.phase == "released"

        # Hold must be released: committed unchanged, reserved 0, available restored!
        assert e2e_harness.ledger.budget["reserved"] == 0
        assert e2e_harness.ledger.free() == initial_available
        await bad_session.close()

    asyncio.run(run())


def test_live_domain_refusal_releases_hold(e2e_harness):
    """When a caller requests a project they lack access to (e.g. nonexistent or orbit),
    Fastify returns pre-acceptance domain refusal 'no project ... is available to you',
    releases the accepted operation key from dedupe store, and PaidInvocation releases
    the budget hold without committing charges.
    """
    async def run():
        initial_available = e2e_harness.ledger.free()
        args = {"project": "nonexistent", "query": "auth", "limit": 8}
        paid = cg.PaidInvocation(
            runtime=e2e_harness.runtime,
            descriptor=e2e_harness.descriptor,
            session=e2e_harness.session,
            tool=cp.PROJECTS_SEARCH_TOOL,
            arguments=args,
            grants=("mcp__projects__*",),
            policy=ALLOW_PAID,
            client_key="k_live_domain_refusal",
            emit=lambda p, event: None,
        )
        res = await paid.run()
        assert res.reason_code == "provider-error"
        assert res.phase == "released"
        assert e2e_harness.ledger.budget["reserved"] == 0
        assert e2e_harness.ledger.budget["committed"] == 0
        assert e2e_harness.ledger.free() == initial_available

        # Verify outcome lookup confirms acceptance was released (status is not_found)
        op_key = cm.provider_operation_key(res.invocation_id)
        outcome = await e2e_harness.session.outcome(op_key)
        assert outcome.status == "not_found"

    asyncio.run(run())


def test_budget_exhaustion_before_dispatch(e2e_harness):
    """When budget is insufficient to cover amount_minor, call is refused before dispatch."""
    async def run():
        # Set budget available to less than 12
        e2e_harness.ledger.budget = {"available": 5, "reserved": 0, "committed": 0}
        args = {"project": "acme", "query": "auth", "limit": 8}

        paid = cg.PaidInvocation(
            runtime=e2e_harness.runtime,
            descriptor=e2e_harness.descriptor,
            session=e2e_harness.session,
            tool=cp.PROJECTS_SEARCH_TOOL,
            arguments=args,
            grants=("mcp__projects__*",),
            policy=ALLOW_PAID,
            client_key="k_exhausted_1",
            emit=lambda p, event: None,
        )

        res = await paid.run()
        assert res.reason_code == "budget-exhausted"
        # Precheck fails before accept, so no invocation row is created on the ledger
        assert res.phase == ""
        assert res.invocation_id == ""
        assert e2e_harness.ledger.budget == {"available": 5, "reserved": 0, "committed": 0}

    asyncio.run(run())


def test_outcome_endpoint_security_and_isolation(e2e_harness):
    """Outcome endpoint returns NOT_FOUND for unknown keys and isolates callers."""
    async def run():
        # 1. Non-existent key
        out_missing = await e2e_harness.session.outcome("mctl-op/v1:inv_non_existent")
        assert out_missing.status == "not_found"

        # 2. Existing key executed by 'good' (customer@example.com)
        args = {"project": "acme", "query": "auth", "limit": 8}
        paid = cg.PaidInvocation(
            runtime=e2e_harness.runtime,
            descriptor=e2e_harness.descriptor,
            session=e2e_harness.session,
            tool=cp.PROJECTS_SEARCH_TOOL,
            arguments=args,
            grants=("mcp__projects__*",),
            policy=ALLOW_PAID,
            client_key="k_iso_test",
            emit=lambda p, event: None,
        )
        res = await paid.run()
        assert res.reason_code == "ok"
        op_key = cm.provider_operation_key(res.invocation_id)

        # 3. Caller with different non-admin identity (owner@example.org) cannot see it
        foreign_session = cp.HttpProjectsMcpPaidSession(
            base_url=e2e_harness.server_url, access_token="owner",  # noqa: S106
        )
        foreign_outcome = await foreign_session.outcome(op_key)
        assert foreign_outcome.status == "not_found"

        # 4. Admin caller (admin@example.com) can observe it
        admin_session = cp.HttpProjectsMcpPaidSession(
            base_url=e2e_harness.server_url, access_token="admin",  # noqa: S106
        )
        admin_outcome = await admin_session.outcome(op_key)
        assert admin_outcome.status == "succeeded"
        assert not admin_outcome.is_error

        await foreign_session.close()
        await admin_session.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Unit tests for HttpProjectsMcpPaidSession (independent of live server)
# ---------------------------------------------------------------------------


def test_session_protocol_direct_call_raises_refused():
    """call_tool without authorization must immediately raise DispatchRefused."""
    session = cp.HttpProjectsMcpPaidSession("http://localhost:9999")
    async def run():
        with pytest.raises(cm.DispatchRefused, match="requires a dispatch authorization"):
            await session.call_tool("projects_search", {"project": "acme", "query": "auth"})
    asyncio.run(run())


def test_session_protocol_parse_mcp_response():
    """_parse_mcp_response parses both raw JSON and SSE data lines."""
    raw_json = '{"jsonrpc": "2.0", "result": {"ok": true}}'
    assert cp._parse_mcp_response(raw_json) == {"jsonrpc": "2.0", "result": {"ok": True}}

    sse_data = "event: message\ndata: {\"jsonrpc\": \"2.0\", \"id\": 1, \"result\": {\"tools\": []}}\n\n"
    parsed_sse = cp._parse_mcp_response(sse_data)
    assert parsed_sse == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


def _make_dummy_auth(args: dict[str, Any]) -> cm.DispatchAuthorization:
    deadline = cm.hashed_time(datetime.now(UTC) + timedelta(seconds=60))
    raw_auth = cm.DispatchAuthorization(
        key_id=KID,
        invocation_id="inv_unit_test",
        provider_operation_key="mctl-op/v1:inv_unit_test",
        provider_id="projects-mcp",
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        args_digest=cm.args_digest(args),
        tenant_id="acme",
        dispatch_deadline=deadline,
    )
    return cm.sign_dispatch_authorization(SEED, raw_auth)


def test_schema_hash_and_capability_id_derivation():
    """PROJECTS_SEARCH_SCHEMA_HASH is derived from canonical JSON of schema and capability ID matches ADR 017."""
    expected_hash = cp.hash_bytes(cp.canonical_json(dict(cp.PROJECTS_SEARCH_INPUT_SCHEMA)))
    assert cp.PROJECTS_SEARCH_SCHEMA_HASH == expected_hash
    assert cp.PROJECTS_SEARCH_CAPABILITY_ID == "mctl://mcp-remote/projects-mcp/projects_search"


def test_unit_session_list_tools_success():
    """list_tools parses ProviderTool entries from mock transport."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mcp"
        assert request.headers.get("cf-access-jwt-assertion") == "test-jwt"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [
                    {
                        "name": "projects_search",
                        "description": "Search docs",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        }
        return httpx.Response(200, json=body)

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        access_token="test-jwt",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )

    async def run():
        tools = await session.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "projects_search"
        assert tools[0].description == "Search docs"

    asyncio.run(run())


def test_unit_session_list_tools_error_cases():
    """list_tools handles HTTP errors, JSON-RPC errors, missing result, and network disconnects."""
    s500 = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="Internal Error")),
    )
    s_rpc_err = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "method not found"}}
            )
        ),
    )
    s_missing_result = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})
        ),
    )

    def raise_network_err(req: httpx.Request):
        raise httpx.ConnectError("connection refused")

    s_net_err = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(raise_network_err),
    )

    s_null_tools = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": None}})
        ),
    )
    s_invalid_tools = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": "not-a-list"}})
        ),
    )

    async def run():
        with pytest.raises(cm.OutcomeUnavailable, match="tools/list failed: HTTP 500"):
            await s500.list_tools()

        with pytest.raises(cm.OutcomeUnavailable, match="tools/list JSON-RPC error:"):
            await s_rpc_err.list_tools()

        with pytest.raises(cm.OutcomeUnavailable, match="neither 'result' nor 'error' present"):
            await s_missing_result.list_tools()

        with pytest.raises(cm.OutcomeUnavailable, match="tools/list network error:"):
            await s_net_err.list_tools()

        # result.tools == None raises OutcomeUnavailable per strict schema
        with pytest.raises(cm.OutcomeUnavailable, match="tools/list result 'tools' is not a list"):
            await s_null_tools.list_tools()

        # result.tools not a list raises OutcomeUnavailable
        with pytest.raises(cm.OutcomeUnavailable, match="tools/list result 'tools' is not a list"):
            await s_invalid_tools.list_tools()

    asyncio.run(run())


def test_unit_session_task_support_parsing():
    """list_tools parses MCP execution taskSupport metadata into ProviderTool.task_support,
    safely handling non-mapping execution, non-string taskSupport, and filtering nameless tools.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": "task_aware_tool",
                    "description": "Tool with task support",
                    "inputSchema": {"type": "object"},
                    "execution": {"taskSupport": "required"},
                },
                {
                    "name": "basic_tool",
                    "description": "Tool without task support",
                    "inputSchema": {"type": "object"},
                },
                {
                    "name": "non_mapping_exec",
                    "description": "Execution is string",
                    "inputSchema": {"type": "object"},
                    "execution": "invalid_structure",
                },
                {
                    "name": "non_string_task_support",
                    "description": "taskSupport is integer",
                    "inputSchema": {"type": "object"},
                    "execution": {"taskSupport": 42},
                },
                {
                    "name": "",
                    "description": "Nameless tool with empty string",
                },
                {
                    "name": "   ",
                    "description": "Nameless tool with whitespace",
                },
                {
                    "description": "Nameless tool missing name field",
                },
            ]
        },
    }
    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)),
    )

    async def run():
        tools = await session.list_tools()
        # Nameless tools must be filtered out
        assert len(tools) == 4
        assert tools[0].name == "task_aware_tool"
        assert tools[0].task_support == "required"
        assert tools[1].name == "basic_tool"
        assert tools[1].task_support == ""
        assert tools[2].name == "non_mapping_exec"
        assert tools[2].task_support == ""
        assert tools[3].name == "non_string_task_support"
        assert tools[3].task_support == ""
        await session.close()

    asyncio.run(run())


def test_unit_session_call_paid_tool_http_refusal():
    """call_paid_tool transforms 401 and 403 into DispatchRefused."""
    s401 = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(401, text="unauthorized access token")),
    )
    s403 = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(403, text="forbidden tenant")),
    )

    args = {"project": "acme", "query": "auth", "limit": 5}
    auth = _make_dummy_auth(args)

    async def run():
        with pytest.raises(cm.DispatchRefused, match="HTTP 401"):
            await s401.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.DispatchRefused, match="HTTP 403"):
            await s403.call_paid_tool("projects_search", args, auth)

    asyncio.run(run())


def test_unit_session_call_paid_tool_refusals_and_post_acceptance():
    """call_paid_tool differentiates pre-dispatch refusals from post-acceptance conditions."""
    args = {"project": "acme", "query": "auth", "limit": 5}
    auth = _make_dummy_auth(args)

    # 1. Pre-dispatch refusal via JSON-RPC error hints
    s_sig_err = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"message": "authorization rejected: signature does not verify"},
                },
            )
        ),
    )

    # 2. Pre-dispatch refusal via structured marker on result (_mctl_refusal)
    s_marker_pre = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "_mctl_refusal": "pre_acceptance",
                        "content": [{"type": "text", "text": "dispatch deadline has passed on provider clock"}],
                    },
                },
            )
        ),
    )

    # 3. Pre-dispatch refusal via structured marker in JSON-RPC error.data
    s_error_data_pre = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32602,
                        "message": "authorization carries unknown fields",
                        "data": {"mctl_refusal": "pre_acceptance"},
                    },
                },
            )
        ),
    )

    # 4. Ingress refusals raised before acceptance across all ProviderIngress validation checks
    real_rejection_messages: list[str] = []

    try:
        cm.DispatchAuthorization.from_dict("not an object")  # type: ignore[arg-type]
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))

    try:
        cm.DispatchAuthorization.from_dict({"schema_version": 1, "extra_unknown_field": True})
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))

    try:
        cm.DispatchAuthorization.from_dict({"schema_version": True})
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))

    try:
        cm.DispatchAuthorization.from_dict({"schema_version": 99})
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))

    try:
        cm.DispatchAuthorization.from_dict({"schema_version": 1})
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))

    bad_deadline_auth = cm.DispatchAuthorization(
        key_id=KID,
        invocation_id="inv_test",
        provider_operation_key="mctl-op/v1:inv_test",
        provider_id="projects-mcp",
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        args_digest=cm.args_digest(args),
        tenant_id="acme",
        dispatch_deadline="malformed-deadline-string",
        signature="dGVzdA",
    )
    try:
        cm.verify_dispatch_authorization({KID: cm.public_key_for(SEED)}, bad_deadline_auth)
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))

    bad_op_auth = cm.DispatchAuthorization(
        key_id=KID,
        invocation_id="inv_test",
        provider_operation_key="mctl-op/v1:foreign_inv",
        provider_id="projects-mcp",
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        args_digest=cm.args_digest(args),
        tenant_id="acme",
        dispatch_deadline=cm.hashed_time(datetime.now(UTC) + timedelta(seconds=60)),
        signature="dGVzdA",
    )
    try:
        cm.verify_dispatch_authorization({KID: cm.public_key_for(SEED)}, bad_op_auth)
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))

    good_auth = _make_dummy_auth(args)
    try:
        cm.verify_dispatch_authorization({"foreign_kid": cm.public_key_for(SEED)}, good_auth)
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))
        real_rejection_messages.append(f"authorization rejected: {exc}")

    tampered_auth = cm.DispatchAuthorization(**{**good_auth.to_dict(), "signature": "dGFtcGVyZWQ"})
    try:
        cm.verify_dispatch_authorization({KID: cm.public_key_for(SEED)}, tampered_auth)
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))
        real_rejection_messages.append(f"authorization rejected: {exc}")

    # Drive non-base64url signature rejection from _unb64url (commerce.py:264)
    bad_b64_auth = cm.DispatchAuthorization(**{**good_auth.to_dict(), "signature": "???not_b64url???"})
    try:
        cm.verify_dispatch_authorization({KID: cm.public_key_for(SEED)}, bad_b64_auth)
    except cm.AuthorizationRejected as exc:
        real_rejection_messages.append(str(exc))
        real_rejection_messages.append(f"authorization rejected: {exc}")

    real_rejection_messages.extend([
        "a paid capability requires a dispatch authorization",
        "invalid authorization json",
        "invalid authorization structure",
        "unknown authorization version 2",
        "invalid dispatch_deadline timestamp",
        "public key must be 32 bytes, got 16",
        "invalid signature length",
        "unknown tool 'projects_nonexistent'",
        "arguments cannot be digested",
        "provider operation key is bound to another invocation",
        "authorization names another provider",
        "authorization names another capability",
        "authorization names another input schema",
        "authorization was signed for other arguments",
        "authorization names another tenant",
        "dispatch deadline has passed",
        "a paid capability requires a valid dispatch authorization",
        'no project "nonexistent" is available to you',
        'no live status is configured for "test"',
        'project "test" is not configured for tenant',
    ])
    s_ingress_errors = [
        cp.HttpProjectsMcpPaidSession(
            "https://mock-provider",
            transport=httpx.MockTransport(
                lambda req, msg=msg: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": msg}})
            ),
        )
        for msg in real_rejection_messages
    ]
    s_tool_errors = [
        cp.HttpProjectsMcpPaidSession(
            "https://mock-provider",
            transport=httpx.MockTransport(
                lambda req, msg=msg: httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"isError": True, "content": [{"type": "text", "text": msg}]},
                    },
                )
            ),
        )
        for msg in real_rejection_messages
    ]

    # 5. Post-acceptance conditions via hints in JSON-RPC error
    in_prog_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"message": "the capability execution is still in progress"},
    }
    s_in_progress = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=in_prog_payload)),
    )

    # 6. Post-acceptance conditions via structured marker on result (_mctl_refusal)
    s_marker_post = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "_mctl_refusal": "post_acceptance",
                        "content": [{"type": "text", "text": "the capability failed on previous attempt"}],
                    },
                },
            )
        ),
    )

    # 7. Post-acceptance conditions via structured marker in JSON-RPC error.data
    s_error_data_post = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32000,
                        "message": "concurrent dispatch in progress",
                        "data": {"mctl_refusal": "post_acceptance"},
                    },
                },
            )
        ),
    )

    # 8. Response missing 'result' and 'error' -> OutcomeUnavailable
    s_missing_result = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})
        ),
    )

    # 9. Tool error without refusal marker -> raises OutcomeUnavailable (consults outcome)
    s_tool_fail = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "syntax error in lucene query"}],
                    },
                },
            )
        ),
    )

    # Application-level errors that must NOT be misclassified as pre-dispatch refusals
    app_error_messages = [
        "document index is missing",
        "parameter 'limit' must be an integer",
        "metadata is missing from passage",
        "cluster is configured for v2",
        "syntax error in lucene query",
        "failed to read docs/authorization.md",
        "syntax error in src/authorization.ts",
        "Query execution failed: backend service requires database authorization.",
        "Syntax error near token authorization.",
        "search error: no results for query 'unknown tool'",
        "invalid syntax in query: unknown tool",
        "timestamp validation failed: event must be timezone-aware",
        "webhook signature invalid for payload",
        'no passages matching "is available to you" in project acme',
        'query failed for "is not configured for tenant" in database',
    ]
    s_app_errors = [
        cp.HttpProjectsMcpPaidSession(
            "https://mock-provider",
            transport=httpx.MockTransport(
                lambda req, msg=msg: httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {"isError": True, "content": [{"type": "text", "text": msg}]},
                    },
                )
            ),
        )
        for msg in app_error_messages
    ]

    # 10. Captured real projects-mcp refusal fixture (with _mctl_refusal: "pre_acceptance")
    s_real_ingress_refusal = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "dispatch deadline has passed (deadline: 2026-10-13T12:00:00.000Z)",
                            }
                        ],
                        "_mctl_refusal": "pre_acceptance",
                    },
                },
            )
        ),
    )

    # 11. JSON-RPC error with _mctl_refusal in error.data
    s_error_data_pre_underscore = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32600,
                        "message": "custom authorization refusal",
                        "data": {"_mctl_refusal": "pre_acceptance"},
                    },
                },
            )
        ),
    )

    async def run():
        # Pre-dispatch refusals
        with pytest.raises(cm.DispatchRefused, match="signature does not verify"):
            await s_sig_err.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.DispatchRefused, match="dispatch deadline has passed"):
            await s_marker_pre.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.DispatchRefused, match="unknown fields"):
            await s_error_data_pre.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.DispatchRefused, match="dispatch deadline has passed"):
            await s_real_ingress_refusal.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.DispatchRefused, match="custom authorization refusal"):
            await s_error_data_pre_underscore.call_paid_tool("projects_search", args, auth)

        for s_ing in s_ingress_errors:
            with pytest.raises(cm.DispatchRefused):
                await s_ing.call_paid_tool("projects_search", args, auth)
            await s_ing.close()

        for s_tool in s_tool_errors:
            with pytest.raises(cm.DispatchRefused):
                await s_tool.call_paid_tool("projects_search", args, auth)
            await s_tool.close()

        # Application-level errors raise OutcomeUnavailable (to consult outcome store), never DispatchRefused
        for s_app in s_app_errors:
            with pytest.raises(cm.OutcomeUnavailable, match="provider operation state:"):
                await s_app.call_paid_tool("projects_search", args, auth)
            await s_app.close()

        # Post-acceptance states
        with pytest.raises(cm.OutcomeUnavailable, match="provider operation state:"):
            await s_in_progress.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.OutcomeUnavailable, match="provider operation state:"):
            await s_marker_post.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.OutcomeUnavailable, match="provider operation state:"):
            await s_error_data_post.call_paid_tool("projects_search", args, auth)

        # Missing result
        with pytest.raises(cm.OutcomeUnavailable, match="neither 'result' nor 'error' present"):
            await s_missing_result.call_paid_tool("projects_search", args, auth)

        # Tool execution error without refusal marker raises OutcomeUnavailable
        with pytest.raises(cm.OutcomeUnavailable, match="provider operation state:"):
            await s_tool_fail.call_paid_tool("projects_search", args, auth)

        await s_sig_err.close()
        await s_marker_pre.close()
        await s_error_data_pre.close()
        await s_real_ingress_refusal.close()
        await s_error_data_pre_underscore.close()
        await s_in_progress.close()
        await s_marker_post.close()
        await s_error_data_post.close()
        await s_missing_result.close()
        await s_tool_fail.close()

    asyncio.run(run())


def test_unit_session_call_paid_tool_network_and_http_errors():
    """call_paid_tool raises OutcomeUnavailable on network failure or HTTP 5xx."""
    args = {"project": "acme", "query": "auth", "limit": 5}
    auth = _make_dummy_auth(args)

    def raise_timeout(req: httpx.Request):
        raise httpx.ReadTimeout("read timed out")

    s_timeout = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(raise_timeout),
    )
    s_502 = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(502, text="Bad Gateway")),
    )

    async def run():
        with pytest.raises(cm.OutcomeUnavailable, match="tools/call network error:"):
            await s_timeout.call_paid_tool("projects_search", args, auth)

        with pytest.raises(cm.OutcomeUnavailable, match="tools/call failed: HTTP 502"):
            await s_502.call_paid_tool("projects_search", args, auth)

    asyncio.run(run())


def test_unit_session_outcome_cases():
    """outcome handles structured 404 not found, edge 404, succeeded, malformed JSON, and unknown status."""
    s404_provider = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(404, json={"status": "not_found"})
        ),
    )
    s404_edge_text = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(404, text="Not Found")),
    )
    s404_non_mapping = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(404, json=["not_found"])),
    )
    s404_wrong_status = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"status": "succeeded"})),
    )
    s200_ok = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"status": "succeeded", "is_error": False, "text": "done"})
        ),
    )
    s200_non_mapping = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=["succeeded"])),
    )
    s500 = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(500, text="Internal Error")),
    )
    s_malformed_json = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="<html>502 Bad Gateway</html>")
        ),
    )
    s_unknown_status = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"status": "invalid_status", "is_error": False})
        ),
    )

    def raise_connect_err(req: httpx.Request):
        raise httpx.ConnectError("connection failed")

    s_net_err = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(raise_connect_err),
    )

    async def run():
        out404 = await s404_provider.outcome("mctl-op/v1:test_key")
        assert out404.status == "not_found"

        with pytest.raises(cm.OutcomeUnavailable, match="HTTP 404"):
            await s404_edge_text.outcome("mctl-op/v1:test_key")

        with pytest.raises(cm.OutcomeUnavailable, match="outcome response is not a mapping"):
            await s404_non_mapping.outcome("mctl-op/v1:test_key")

        with pytest.raises(cm.OutcomeUnavailable, match="HTTP 404 returned unexpected status: 'succeeded'"):
            await s404_wrong_status.outcome("mctl-op/v1:test_key")

        out_ok = await s200_ok.outcome("mctl-op/v1:test_key")
        assert out_ok.status == "succeeded"
        assert not out_ok.is_error
        assert out_ok.text == "done"

        with pytest.raises(cm.OutcomeUnavailable, match="outcome response is not a mapping"):
            await s200_non_mapping.outcome("mctl-op/v1:test_key")

        with pytest.raises(cm.OutcomeUnavailable, match="outcome lookup failed: HTTP 500"):
            await s500.outcome("mctl-op/v1:test_key")

        with pytest.raises(cm.OutcomeUnavailable, match="malformed outcome JSON response"):
            await s_malformed_json.outcome("mctl-op/v1:test_key")

        with pytest.raises(cm.OutcomeUnavailable, match="unknown outcome status 'invalid_status'"):
            await s_unknown_status.outcome("mctl-op/v1:test_key")

        with pytest.raises(cm.OutcomeUnavailable, match="outcome lookup network error:"):
            await s_net_err.outcome("mctl-op/v1:test_key")

        with pytest.raises(cm.OutcomeUnavailable, match="invalid provider_operation_key"):
            await s200_ok.outcome("../traversal/key")

        with pytest.raises(cm.OutcomeUnavailable, match="invalid provider_operation_key"):
            await s200_ok.outcome("key?with=query")

    asyncio.run(run())


def test_unit_session_robust_sse_multiline_parsing():
    """_parse_mcp_response parses multiline SSE, handles CRLF, and ignores pings/notifications."""
    # 1. Standard multiline with ping preceding
    multiline_sse = (
        "event: ping\n"
        "data: {}\n\n"
        "event: message\n"
        "data: {\n"
        "data:   \"jsonrpc\": \"2.0\",\n"
        "data:   \"id\": 1,\n"
        "data:   \"result\": {\n"
        "data:     \"tools\": [{\"name\": \"projects_search\", \"description\": \"Search\", \"inputSchema\": {}}]\n"
        "data:   }\n"
        "data: }\n\n"
    )

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, text=multiline_sse)),
    )

    async def run():
        tools = await session.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "projects_search"

    asyncio.run(run())

    # 2. CRLF-delimited SSE stream
    crlf_sse = (
        "event: ping\r\n"
        "data: {}\r\n\r\n"
        "event: message\r\n"
        "data: {\"jsonrpc\": \"2.0\", \"id\": 1, \"result\": {\"tools\": []}}\r\n\r\n"
    )
    parsed_crlf = cp._parse_mcp_response(crlf_sse)
    assert parsed_crlf == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    # 3. Notification frame preceding response frame
    notification_and_result = (
        "event: message\n"
        "data: {\"jsonrpc\": \"2.0\", \"method\": \"notifications/progress\", \"params\": {}}\n\n"
        "event: message\n"
        "data: {\"jsonrpc\": \"2.0\", \"id\": 1, \"result\": {\"tools\": []}}\n\n"
    )
    parsed_notif = cp._parse_mcp_response(notification_and_result)
    assert parsed_notif == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    # 4. Stream containing only pings raises OutcomeUnavailable
    ping_only = "event: ping\ndata: {}\n\n"
    with pytest.raises(cm.OutcomeUnavailable, match="no JSON-RPC response found"):
        cp._parse_mcp_response(ping_only)

    # 5. Malformed SSE data line
    bad_sse = "event: message\ndata: {unclosed json\n\n"
    with pytest.raises(cm.OutcomeUnavailable, match="malformed SSE data payload:"):
        cp._parse_mcp_response(bad_sse)


def test_unit_session_tool_output_containing_authorization_keyword_not_refused():
    """Application-level tool error containing 'authorization' or 'mismatch' is not classified as DispatchRefused."""
    args = {"project": "acme", "query": "authorization", "limit": 5}
    auth = _make_dummy_auth(args)

    error_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "query failed: no passages matching 'authorization' due to type mismatch in filter",
                }
            ],
        },
    }

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=error_payload)),
    )

    async def run():
        # Application-level tool error containing 'authorization' raises OutcomeUnavailable
        # (deferring to outcome lookup) and is never falsely classified as DispatchRefused
        with pytest.raises(cm.OutcomeUnavailable, match="provider operation state:"):
            await session.call_paid_tool("projects_search", args, auth)
        await session.close()

    asyncio.run(run())


def test_unit_session_direct_json_with_data_substring_parsed():
    """_parse_mcp_response correctly parses direct JSON payloads that contain 'data:' in string content."""
    json_with_data_prefix = (
        '{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", '
        '"text": "Example snippet:\\ndata: { foo: 123 }\\nand data:image/png;base64,abc"}]}}'
    )
    res = cp._parse_mcp_response(json_with_data_prefix)
    assert res["id"] == 1
    assert "data: { foo: 123 }" in res["result"]["content"][0]["text"]


def test_unit_session_base_url_scheme_validation():
    """HttpProjectsMcpPaidSession enforces HTTPS scheme for non-loopback URLs."""
    # Plain HTTP for remote hosts fails validation
    with pytest.raises(ValueError, match="base_url must use https scheme"):
        cp.HttpProjectsMcpPaidSession("http://api.remote-provider.com/mcp")

    # HTTPS for remote hosts succeeds
    s_https = cp.HttpProjectsMcpPaidSession("https://api.remote-provider.com/mcp")
    assert s_https.base_url == "https://api.remote-provider.com/mcp"

    # Loopback hosts permit plain HTTP
    for host in ["localhost", "127.0.0.1", "[::1]"]:
        s_local = cp.HttpProjectsMcpPaidSession(f"http://{host}:8080/mcp")
        assert s_local.base_url == f"http://{host}:8080/mcp"

    # Non-loopback mock-provider requires HTTPS
    with pytest.raises(ValueError, match="base_url must use https scheme"):
        cp.HttpProjectsMcpPaidSession("http://mock-provider:8080/mcp")


def test_unit_session_array_response_parsing():
    """_parse_mcp_response successfully parses JSON-RPC batch/array responses."""
    batch_json = '[{"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "t1"}]}}]'
    res = cp._parse_mcp_response(batch_json)
    assert res["id"] == 1
    assert res["result"]["tools"][0]["name"] == "t1"


def test_unit_session_image_content_not_stringified():
    """call_paid_tool filters non-text content blocks (images/resources) from text output."""
    args = {"project": "acme", "query": "auth", "limit": 5}
    auth = _make_dummy_auth(args)
    mixed_content = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {"type": "text", "text": "Summary line"},
                {"type": "image", "data": "base64_raw_image_data==", "mimeType": "image/png"},
            ]
        },
    }
    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=mixed_content)),
    )
    async def run():
        res = await session.call_paid_tool("projects_search", args, auth)
        assert res.text == "Summary line"
        assert "base64" not in res.text

    asyncio.run(run())


def test_unit_session_response_id_validation():
    """call_paid_tool and list_tools accept integer 1 and string '1', but reject True, wrong id, or None."""
    args = {"project": "acme", "query": "auth", "limit": 5}
    auth = _make_dummy_auth(args)

    async def run():
        # Valid string id "1" succeeds
        str_id_resp = {"jsonrpc": "2.0", "id": "1", "result": {"tools": []}}
        s_str = cp.HttpProjectsMcpPaidSession(
            "https://mock-provider",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=str_id_resp)),
        )
        assert await s_str.list_tools() == []
        await s_str.close()

        # Boolean True (which evaluates to True == 1 in Python) must be rejected
        bool_id_resp = {"jsonrpc": "2.0", "id": True, "result": {"tools": []}}
        s_bool = cp.HttpProjectsMcpPaidSession(
            "https://mock-provider",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=bool_id_resp)),
        )
        with pytest.raises(cm.OutcomeUnavailable, match="response id mismatch"):
            await s_bool.list_tools()
        with pytest.raises(cm.OutcomeUnavailable, match="response id mismatch"):
            await s_bool.call_paid_tool("projects_search", args, auth)
        await s_bool.close()

        # Integer 999 rejected
        wrong_id_resp = {"jsonrpc": "2.0", "id": 999, "result": {"tools": []}}
        s_wrong = cp.HttpProjectsMcpPaidSession(
            "https://mock-provider",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=wrong_id_resp)),
        )
        with pytest.raises(cm.OutcomeUnavailable, match="response id mismatch"):
            await s_wrong.list_tools()
        with pytest.raises(cm.OutcomeUnavailable, match="response id mismatch"):
            await s_wrong.call_paid_tool("projects_search", args, auth)
        await s_wrong.close()

        # None / null id rejected
        null_id_resp = {"jsonrpc": "2.0", "id": None, "result": {"tools": []}}
        s_null = cp.HttpProjectsMcpPaidSession(
            "https://mock-provider",
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json=null_id_resp)),
        )
        with pytest.raises(cm.OutcomeUnavailable, match="response id mismatch"):
            await s_null.list_tools()
        await s_null.close()

    asyncio.run(run())


def test_unit_session_client_lifecycle():
    """HttpProjectsMcpPaidSession manages reusable httpx client lifecycle and closes cleanly."""
    lifecycle_payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=lifecycle_payload)),
    )
    async def run():
        async with session as s:
            tools = await s.list_tools()
            assert tools == []
            assert s._client is not None
            assert not s._client.is_closed
        assert session._client is None

    asyncio.run(run())


def test_unit_session_cross_loop_recreation():
    """HttpProjectsMcpPaidSession recreates its client when called across different event loops."""
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)),
    )

    loop1 = asyncio.new_event_loop()
    try:
        tools1 = loop1.run_until_complete(session.list_tools())
        assert tools1 == []
        assert session._client is not None
        assert session._client_loop is loop1
    finally:
        loop1.close()

    loop2 = asyncio.new_event_loop()
    try:
        tools2 = loop2.run_until_complete(session.list_tools())
        assert tools2 == []
        assert session._client is not None
        assert session._client_loop is loop2
    finally:
        loop2.run_until_complete(session.close())
        loop2.close()


def test_unit_session_unattributable_error_frame_not_refused():
    """Error frame with id: null or mismatched id raises OutcomeUnavailable, never DispatchRefused."""
    args = {"project": "acme", "query": "auth", "limit": 5}
    auth = _make_dummy_auth(args)
    # A payload that would otherwise match refusal keywords (unauthorized message)
    # but lacks valid request id (id: null)
    unattributable_err = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "unauthorized dispatch signature"},
    }
    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=unattributable_err)),
    )
    async def run():
        with pytest.raises(cm.OutcomeUnavailable, match="response id mismatch"):
            await session.call_paid_tool("projects_search", args, auth)
        await session.close()

    asyncio.run(run())


def test_unit_paid_invocation_raise_then_lookup_failure_route(monkeypatch):
    """PaidInvocation handles tool error raising OutcomeUnavailable during dispatch,
    then looks up durable outcome from provider. Under per_successful_call, budget hold is released.
    """
    clock = Clock()
    clock.t = datetime.now(UTC)
    ledger = FakeLedger(clock, budget=100)
    approvals = FakeApprovals(clock)
    ledger.publish(
        cp.PROJECTS_SEARCH_CAPABILITY_ID,
        cp.PROJECTS_SEARCH_SCHEMA_HASH,
        provider_id=cp.PROJECTS_MCP_PROVIDER_ID,
    )
    monkeypatch.setattr(
        pc,
        "current_identity",
        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-unit-fail", actor="agent:test"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            # Post-acceptance condition causes call_paid_tool to raise OutcomeUnavailable
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "_mctl_refusal": "post_acceptance",
                        "content": [{"type": "text", "text": "the capability execution is still in progress"}],
                    },
                },
            )
        if request.method == "GET" and "/commerce/outcome/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "is_error": True,
                    "text": "execution failed on worker node",
                },
            )
        return httpx.Response(404)

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        access_token="unit-jwt",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    runtime = cg.CommerceRuntime(
        client=ledger,
        tenant_id="acme",
        admitted_providers=frozenset({cp.PROJECTS_MCP_PROVIDER_ID}),
        approvals=approvals,
        now=clock,
        call_timeout_s=5.0,
    )
    provider_ref = cap.ProviderRef(type="mcp-remote", id=cp.PROJECTS_MCP_PROVIDER_ID, alias="projects")
    descriptor = cap.CapabilityDescriptor(
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        tool_name="mcp__projects__projects_search",
        provider=provider_ref,
        title="projects_search",
        summary="Search project docs",
        keywords=("projects", "search"),
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        input_schema_bytes=100,
        consequence="safe",
        matched_tool_pattern="mcp__projects__*",
        commerce=cap.CommerceSummary(
            terms_id="ctm_01JZ8PZ0000000000000000000",
            terms_version=1,
            price_mode="per_successful_call",
            currency="EUR",
            amount_minor=12,
            funds_mode="simulated",
        ),
    )

    args = {"project": "acme", "query": "auth", "limit": 8}
    paid = cg.PaidInvocation(
        runtime=runtime,
        descriptor=descriptor,
        session=session,
        tool=cp.PROJECTS_SEARCH_TOOL,
        arguments=args,
        grants=("mcp__projects__*",),
        policy=ALLOW_PAID,
        client_key="k_unit_fail_lookup",
        emit=lambda p, event: None,
    )

    async def run():
        res = await paid.run()
        assert res.reason_code == "provider-error"
        assert res.phase == "finalized"
        assert res.text == "execution failed on worker node"
        # Under per_successful_call, a failed execution releases hold without charging
        assert ledger.budget["reserved"] == 0
        assert ledger.budget["committed"] == 0
        assert ledger.free() == 100
        await session.close()

    asyncio.run(run())


def test_unit_paid_invocation_executed_tool_failure_finalizes(monkeypatch):
    """When an executed tool failure returns isError without a refusal hint, call_paid_tool raises
    OutcomeUnavailable, prompting PaidInvocation to consult session.outcome() and finalize FAILED.
    """
    clock = Clock()
    clock.t = datetime.now(UTC)
    ledger = FakeLedger(clock, budget=100)
    approvals = FakeApprovals(clock)
    ledger.publish(
        cp.PROJECTS_SEARCH_CAPABILITY_ID,
        cp.PROJECTS_SEARCH_SCHEMA_HASH,
        provider_id=cp.PROJECTS_MCP_PROVIDER_ID,
    )
    monkeypatch.setattr(
        pc,
        "current_identity",
        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-unit-exec-fail", actor="agent:test"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/commerce/outcome/"):
            return httpx.Response(
                200,
                json={
                    "provider_operation_key": request.url.path.split("/")[-1],
                    "status": "failed",
                    "is_error": True,
                    "text": "bad query syntax in lucene parser",
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "bad query syntax in lucene parser"}],
                },
            },
        )

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        access_token="unit-jwt",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    runtime = cg.CommerceRuntime(
        client=ledger,
        tenant_id="acme",
        admitted_providers=frozenset({cp.PROJECTS_MCP_PROVIDER_ID}),
        approvals=approvals,
        now=clock,
        call_timeout_s=5.0,
    )
    provider_ref = cap.ProviderRef(type="mcp-remote", id=cp.PROJECTS_MCP_PROVIDER_ID, alias="projects")
    descriptor = cap.CapabilityDescriptor(
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        tool_name="mcp__projects__projects_search",
        provider=provider_ref,
        title="projects_search",
        summary="Search project docs",
        keywords=("projects", "search"),
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        input_schema_bytes=100,
        consequence="safe",
        matched_tool_pattern="mcp__projects__*",
        commerce=cap.CommerceSummary(
            terms_id="ctm_01JZ8PZ0000000000000000000",
            terms_version=1,
            price_mode="per_successful_call",
            currency="EUR",
            amount_minor=12,
            funds_mode="simulated",
        ),
    )

    args = {"project": "acme", "query": "auth", "limit": 8}
    paid = cg.PaidInvocation(
        runtime=runtime,
        descriptor=descriptor,
        session=session,
        tool=cp.PROJECTS_SEARCH_TOOL,
        arguments=args,
        grants=("mcp__projects__*",),
        policy=ALLOW_PAID,
        client_key="k_unit_exec_fail",
        emit=lambda p, event: None,
    )

    async def run():
        res = await paid.run()
        assert res.reason_code == "provider-error"
        assert res.phase == "finalized"
        assert res.text == "bad query syntax in lucene parser"
        # Under per_successful_call, a failed execution releases hold without charging
        assert ledger.budget["reserved"] == 0
        assert ledger.budget["committed"] == 0
        assert ledger.free() == 100
        await session.close()

    asyncio.run(run())


def test_unit_paid_invocation_per_call_executed_failure(monkeypatch):
    """Under per_call (billable_on = accepted_dispatch), an executed tool failure on the provider
    is settled via outcome lookup and captured to committed budget.
    """
    clock = Clock()
    clock.t = datetime.now(UTC)
    ledger = FakeLedger(clock, budget=100, price_mode="per_call")
    approvals = FakeApprovals(clock)
    ledger.publish(
        cp.PROJECTS_SEARCH_CAPABILITY_ID,
        cp.PROJECTS_SEARCH_SCHEMA_HASH,
        provider_id=cp.PROJECTS_MCP_PROVIDER_ID,
        price_mode="per_call",
    )
    monkeypatch.setattr(
        pc,
        "current_identity",
        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-unit-per-call-fail", actor="agent:test"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/commerce/outcome/"):
            return httpx.Response(
                200,
                json={
                    "provider_operation_key": request.url.path.split("/")[-1],
                    "status": "failed",
                    "is_error": True,
                    "text": "bad query syntax in lucene parser",
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": "bad query syntax in lucene parser"}],
                },
            },
        )

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        access_token="unit-jwt",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    runtime = cg.CommerceRuntime(
        client=ledger,
        tenant_id="acme",
        admitted_providers=frozenset({cp.PROJECTS_MCP_PROVIDER_ID}),
        approvals=approvals,
        now=clock,
        call_timeout_s=5.0,
    )
    provider_ref = cap.ProviderRef(type="mcp-remote", id=cp.PROJECTS_MCP_PROVIDER_ID, alias="projects")
    descriptor = cap.CapabilityDescriptor(
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        tool_name="mcp__projects__projects_search",
        provider=provider_ref,
        title="projects_search",
        summary="Search project docs",
        keywords=("projects", "search"),
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        input_schema_bytes=100,
        consequence="safe",
        matched_tool_pattern="mcp__projects__*",
        commerce=cap.CommerceSummary(
            terms_id="ctm_01JZ8PZ0000000000000000000",
            terms_version=1,
            price_mode="per_call",
            currency="EUR",
            amount_minor=12,
            funds_mode="simulated",
        ),
    )

    args = {"project": "acme", "query": "auth", "limit": 8}
    paid = cg.PaidInvocation(
        runtime=runtime,
        descriptor=descriptor,
        session=session,
        tool=cp.PROJECTS_SEARCH_TOOL,
        arguments=args,
        grants=("mcp__projects__*",),
        policy=ALLOW_PAID,
        client_key="k_unit_per_call_fail",
        emit=lambda p, event: None,
    )

    async def run():
        res = await paid.run()
        assert res.phase == "finalized"
        assert res.text == "bad query syntax in lucene parser"
        assert res.reason_code == "provider-error"
        # Under per_call, the executed failure captures the committed price
        assert ledger.budget["reserved"] == 0
        assert ledger.budget["committed"] == 12
        assert ledger.free() == 88
        await session.close()

    asyncio.run(run())


def test_unit_paid_invocation_per_call_unmarked_refusal(monkeypatch):
    """Under per_call, an unclassified pre-dispatch refusal (outcome not found on provider)
    must NOT capture liability into committed budget.
    """
    clock = Clock()
    clock.t = datetime.now(UTC)
    ledger = FakeLedger(clock, budget=100, price_mode="per_call")
    approvals = FakeApprovals(clock)
    ledger.publish(
        cp.PROJECTS_SEARCH_CAPABILITY_ID,
        cp.PROJECTS_SEARCH_SCHEMA_HASH,
        provider_id=cp.PROJECTS_MCP_PROVIDER_ID,
        price_mode="per_call",
    )
    monkeypatch.setattr(
        pc,
        "current_identity",
        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-unit-unmarked-refuse", actor="agent:test"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/commerce/outcome/"):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "unmarked custom provider rejection"},
            },
        )

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        access_token="unit-jwt",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    runtime = cg.CommerceRuntime(
        client=ledger,
        tenant_id="acme",
        admitted_providers=frozenset({cp.PROJECTS_MCP_PROVIDER_ID}),
        approvals=approvals,
        now=clock,
        call_timeout_s=5.0,
    )
    provider_ref = cap.ProviderRef(type="mcp-remote", id=cp.PROJECTS_MCP_PROVIDER_ID, alias="projects")
    descriptor = cap.CapabilityDescriptor(
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        tool_name="mcp__projects__projects_search",
        provider=provider_ref,
        title="projects_search",
        summary="Search project docs",
        keywords=("projects", "search"),
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        input_schema_bytes=100,
        consequence="safe",
        matched_tool_pattern="mcp__projects__*",
        commerce=cap.CommerceSummary(
            terms_id="ctm_01JZ8PZ0000000000000000000",
            terms_version=1,
            price_mode="per_call",
            currency="EUR",
            amount_minor=12,
            funds_mode="simulated",
        ),
    )

    args = {"project": "acme", "query": "auth", "limit": 8}
    paid = cg.PaidInvocation(
        runtime=runtime,
        descriptor=descriptor,
        session=session,
        tool=cp.PROJECTS_SEARCH_TOOL,
        arguments=args,
        grants=("mcp__projects__*",),
        policy=ALLOW_PAID,
        client_key="k_unit_unmarked_refusal",
        emit=lambda p, event: None,
    )

    async def run():
        res = await paid.run()
        # Because outcome is not_found, the refusal is never committed to revenue
        assert res.reason_code == "outcome-unknown"
        assert ledger.budget["committed"] == 0
        assert ledger.budget["reserved"] == 12
        await session.close()

    asyncio.run(run())


def test_unit_paid_invocation_domain_refusal_releases_hold(monkeypatch):
    """In unit tests (runs in CI), pre-acceptance domain refusal ('no project ... is available to you')
    raises DispatchRefused and PaidInvocation releases the budget hold without charging.
    """
    clock = Clock()
    clock.t = datetime.now(UTC)
    ledger = FakeLedger(clock, budget=100)
    approvals = FakeApprovals(clock)
    ledger.publish(
        cp.PROJECTS_SEARCH_CAPABILITY_ID,
        cp.PROJECTS_SEARCH_SCHEMA_HASH,
        provider_id=cp.PROJECTS_MCP_PROVIDER_ID,
    )
    monkeypatch.setattr(
        pc,
        "current_identity",
        lambda: pc.ExecutionIdentity(execution_id=EXECUTION, trace_id="tr-unit-domain-refusal", actor="agent:test"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": 'no project "secret-proj" is available to you'}],
                },
            },
        )

    session = cp.HttpProjectsMcpPaidSession(
        "https://mock-provider",
        access_token="unit-jwt",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    runtime = cg.CommerceRuntime(
        client=ledger,
        tenant_id="acme",
        admitted_providers=frozenset({cp.PROJECTS_MCP_PROVIDER_ID}),
        approvals=approvals,
        now=clock,
        call_timeout_s=5.0,
    )
    provider_ref = cap.ProviderRef(type="mcp-remote", id=cp.PROJECTS_MCP_PROVIDER_ID, alias="projects")
    descriptor = cap.CapabilityDescriptor(
        capability_id=cp.PROJECTS_SEARCH_CAPABILITY_ID,
        tool_name="mcp__projects__projects_search",
        provider=provider_ref,
        title="projects_search",
        summary="Search project docs",
        keywords=("projects", "search"),
        input_schema_hash=cp.PROJECTS_SEARCH_SCHEMA_HASH,
        input_schema_bytes=100,
        consequence="safe",
        matched_tool_pattern="mcp__projects__*",
        commerce=cap.CommerceSummary(
            terms_id="ctm_01JZ8PZ0000000000000000000",
            terms_version=1,
            price_mode="per_successful_call",
            currency="EUR",
            amount_minor=12,
            funds_mode="simulated",
        ),
    )

    args = {"project": "secret-proj", "query": "auth", "limit": 8}
    paid = cg.PaidInvocation(
        runtime=runtime,
        descriptor=descriptor,
        session=session,
        tool=cp.PROJECTS_SEARCH_TOOL,
        arguments=args,
        grants=("mcp__projects__*",),
        policy=ALLOW_PAID,
        client_key="k_unit_domain_refusal",
        emit=lambda p, event: None,
    )

    async def run():
        res = await paid.run()
        assert res.reason_code == "provider-error"
        assert res.phase == "released"
        assert ledger.budget["reserved"] == 0
        assert ledger.budget["committed"] == 0
        assert ledger.free() == 100
        await session.close()

    asyncio.run(run())


def test_unit_refusal_hints_table_integrity():
    """PRE_DISPATCH_REFUSAL_HINTS and POST_ACCEPTANCE_HINTS don't false-positive on application errors."""
    sample_key = cm.provider_operation_key("inv_01j8m4k_abcdef")
    assert cp.PROVIDER_OPERATION_KEY_PATTERN.fullmatch(sample_key)
    uuid_key = cm.provider_operation_key("0192e428-1234-7abc-def0-000000000001")
    assert cp.PROVIDER_OPERATION_KEY_PATTERN.fullmatch(uuid_key)

    app_errors = [
        "document index is missing",
        "parameter 'limit' must be an integer",
        "metadata is missing from passage",
        "cluster is configured for v2",
        "syntax error in lucene query",
        "failed to read docs/authorization.md",
        "syntax error in src/authorization.ts",
        "Query execution failed: backend service requires database authorization.",
        "Syntax error near token authorization.",
        "search error: no results for query 'unknown tool'",
        "invalid syntax in query: unknown tool",
        "timestamp validation failed: event must be timezone-aware",
        "webhook signature invalid for payload",
        'no passages matching "is available to you" in project acme',
        'query failed for "is not configured for tenant" in database',
    ]
    for msg in app_errors:
        low = msg.lower()
        for hint in cp.PRE_DISPATCH_REFUSAL_HINTS:
            assert hint not in low, f"PRE_DISPATCH_REFUSAL_HINTS hint {hint!r} falsely matched {msg!r}"
        for hint in cp.POST_ACCEPTANCE_HINTS:
            assert hint not in low, f"POST_ACCEPTANCE_HINTS hint {hint!r} falsely matched {msg!r}"






