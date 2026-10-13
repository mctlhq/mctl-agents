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
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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

PROJECTS_MCP_DIR = Path(os.environ.get("PROJECTS_MCP_DIR", "/Users/dmitriimashkov/PycharmProjects/mctlhq/projects-mcp"))
if not PROJECTS_MCP_DIR.exists():
    for candidate in [
        Path(__file__).resolve().parent.parent.parent / "projects-mcp",
        Path.cwd().parent / "projects-mcp",
    ]:
        if candidate.exists():
            PROJECTS_MCP_DIR = candidate
            break

HAVE_PROJECTS_MCP = (
    PROJECTS_MCP_DIR.exists()
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
        stderr=subprocess.PIPE,
        text=True,
    )

    server_url = ""
    try:
        # Wait up to 10 seconds for READY line
        start_wait = time.time()
        while time.time() - start_wait < 10.0:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    err = proc.stderr.read()
                    raise RuntimeError(f"projects-mcp server exited prematurely: {err}")
                time.sleep(0.05)
                continue
            if line.startswith("READY "):
                server_url = line.strip().split(" ")[1]
                break

        if not server_url:
            proc.terminate()
            raise TimeoutError("Timed out waiting for projects-mcp server to emit READY")

        yield SimpleNamespace(url=server_url, dedupe_db=dedupe_db)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def e2e_harness(projects_mcp_server, monkeypatch):
    """Sets up an end-to-end commerce test harness with live wall-clock time."""
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

    return SimpleNamespace(
        clock=clock,
        ledger=ledger,
        approvals=approvals,
        session=session,
        runtime=runtime,
        descriptor=descriptor,
        server_url=projects_mcp_server.url,
    )


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
        assert "properties" in search_tool.input_schema
        assert "project" in search_tool.input_schema["properties"]
        assert "query" in search_tool.input_schema["properties"]

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

        # 12 units committed, 0 reserved, budget available = 100 - 12 = 88
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
            capability_id="mctl://projects/projects_search",
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
            capability_id="mctl://projects/projects_search",
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
            capability_id="mctl://projects/projects_search",
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
            capability_id="mctl://projects/projects_search",
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

