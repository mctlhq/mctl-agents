"""Tests for orchestrator/capability_gateway.py — mctlhq/mctl-agents#242's
gateway runtime (slice 2, ADR 017:
docs/adr/017-capability-discovery-and-gateway-contract.md). Task list
(tasks.md): T3, T5, T6, T7, T9, T10 (options.py side, see
tests/test_options.py), T12 (see tests/test_worker_isolation.py), T16, plus
task 6b's PolicyDecidePolicyCheckpoint DoD.

No real network and no real `mcp`/`claude_agent_sdk` server: every provider
is a fake satisfying `capability_gateway.ProviderSession`, and every remote
connection goes through an injected fake `ProviderConnector` — the point of
that seam.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace

import anyio
import pytest

from orchestrator import capability as cap
from orchestrator import capability_gateway as gw
from orchestrator.context_snapshot import ExecutionCorrelation

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _execution(**overrides) -> ExecutionCorrelation:
    fields = {
        "agent": "issue-investigator",
        "environment": "production",
        "temporal_workflow_id": "dev-loop-mctlhq-mctl-agents-242",
        "target_repository_sha": "a" * 40,
        "definition_version": "3",
        "definition_content_hash": "sha256:" + "1a" * 32,
        "profile_version": "5",
        "profile_content_hash": "sha256:" + "2b" * 32,
        "release_revision": 12,
    }
    fields.update(overrides)
    return ExecutionCorrelation(**fields)


def _plan(tools: tuple[str, ...]):
    """`resolve_eligible`/`CapabilityGateway.build` only ever read
    `plan.tools` — a bare namespace is enough and keeps this file decoupled
    from orchestrator.resolver's much larger ExecutionPlan shape."""
    return SimpleNamespace(tools=tools)


@dataclass
class FakeSession:
    """A recording fake `ProviderSession` — no network, ever."""

    tools: list[gw.ProviderTool] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    call_result: gw.ToolCallResult | None = None
    raise_on_call: Exception | None = None
    raise_on_list: Exception | None = None

    async def list_tools(self) -> list[gw.ProviderTool]:
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return self.tools

    async def call_tool(self, tool: str, arguments) -> gw.ToolCallResult:
        self.calls.append((tool, dict(arguments)))
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return self.call_result or gw.ToolCallResult(is_error=False, text="ok")


def _fake_connector(sessions_by_provider_id: dict[str, FakeSession], *, header_sink: list[dict] | None = None):
    """A `ProviderConnector` that hands back a pre-built `FakeSession` for
    the given `provider.id`, recording the headers it was called with when
    `header_sink` is supplied (T7: correlation propagation)."""

    @asynccontextmanager
    async def _connect(provider, headers):
        if header_sink is not None:
            header_sink.append(dict(headers))
        yield sessions_by_provider_id[provider.id]

    return _connect


def _raising_connector(message: str = "the provider must not be contacted"):
    """A transport fake that fails on use (T6): asserts a remote connector
    is never invoked for a capability that should dispatch locally, or
    after a policy denial."""

    @asynccontextmanager
    async def _connect(provider, headers):
        raise AssertionError(message)
        yield  # pragma: no cover — unreachable, only shapes the generator

    return _connect


def _tool(name: str, description: str = "", input_schema: dict | None = None) -> gw.ProviderTool:
    return gw.ProviderTool(name=name, description=description, input_schema=input_schema or {"type": "object"})


REMOTE_PROVIDER = cap.ProviderRef(type="mcp-remote", id="mctl-api", alias="mctl", endpoint_ref="https://api.mctl.ai/mcp")
LOCAL_PROVIDER = cap.ProviderRef(type="mcp-local", id="local-fs", alias="local")


async def _build(
    plan_tools: tuple[str, ...],
    *,
    providers,
    connector=None,
    local_sessions=None,
    checkpoint=None,
    correlation=None,
) -> gw.CapabilityGateway:
    return await gw.CapabilityGateway.build(
        _plan(plan_tools),
        correlation or _execution(),
        providers,
        local_sessions=local_sessions or {},
        connector=connector,
        checkpoint=checkpoint,
        headers={},
    )


# ---------------------------------------------------------------------------
# resolve_eligible / discovery basics
# ---------------------------------------------------------------------------


def test_resolve_eligible_matches_and_counts_exclusions():
    session = FakeSession(tools=[_tool("mctl_whoami"), _tool("mctl_deploy_service")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})

    capability_set = anyio.run(partial(
        gw.resolve_eligible,
        _plan(("Read", "mcp__mctl__mctl_whoami")),
        _execution(),
        [REMOTE_PROVIDER],
        connector=connector,
        headers={},
    ))

    assert capability_set.excluded_count == 1
    assert len(capability_set.capabilities) == 1
    only = capability_set.capabilities[0]
    assert only.capability_id == "mctl://mcp-remote/mctl-api/mctl_whoami"
    assert only.tool_name == "mcp__mctl__mctl_whoami"
    assert only.matched_tool_pattern == "mcp__mctl__mctl_whoami"
    assert only.consequence == "read-only"  # config/capability-consequence.yaml


def test_resolve_eligible_wildcards_match_every_advertised_tool():
    session = FakeSession(tools=[_tool("mctl_whoami"), _tool("mctl_deploy_service")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})

    capability_set = anyio.run(partial(
        gw.resolve_eligible, _plan(("mcp__mctl__*",)), _execution(), [REMOTE_PROVIDER],
        connector=connector, headers={},
    ))

    assert capability_set.excluded_count == 0
    assert {c.tool_name for c in capability_set.capabilities} == {
        "mcp__mctl__mctl_whoami", "mcp__mctl__mctl_deploy_service",
    }


# ---------------------------------------------------------------------------
# T3 — Exclusion is total.
# ---------------------------------------------------------------------------


def test_excluded_capability_is_never_named_and_invoke_refuses_it_without_a_provider_call():
    session = FakeSession(tools=[_tool("mctl_whoami"), _tool("mctl_deploy_service")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})

    gateway = anyio.run(partial(_build, ("mcp__mctl__mctl_whoami",), providers=[REMOTE_PROVIDER], connector=connector))

    assert gateway.capability_set.excluded_count == 1
    excluded_id = "mctl://mcp-remote/mctl-api/mctl_deploy_service"
    assert excluded_id not in gateway.descriptors_by_id
    assert not gateway.search("deploy")["results"]
    assert gateway.describe([excluded_id])["results"][excluded_id] == {"reason_code": "not-found"}

    session.calls.clear()
    result = anyio.run(partial(gateway.invoke, excluded_id, {}))
    assert result["reason_code"] == "not-eligible"
    assert session.calls == []


# ---------------------------------------------------------------------------
# T5 — Collision safety.
# ---------------------------------------------------------------------------


def test_two_providers_claiming_the_same_alias_raise_collision_at_sealing():
    provider_a = cap.ProviderRef(type="mcp-remote", id="provider-a", alias="dup", endpoint_ref="https://a")
    provider_b = cap.ProviderRef(type="mcp-remote", id="provider-b", alias="dup", endpoint_ref="https://b")
    session_a = FakeSession(tools=[_tool("tool_a")])
    session_b = FakeSession(tools=[_tool("tool_b")])
    connector = _fake_connector({"provider-a": session_a, "provider-b": session_b})

    with pytest.raises(cap.CapabilityCollisionError, match="claim alias"):
        anyio.run(partial(
            gw.resolve_eligible, _plan(("mcp__dup__*",)), _execution(), [provider_a, provider_b],
            connector=connector, headers={},
        ))


def test_two_providers_resolving_to_the_same_tool_name_raise_collision_at_sealing():
    # Two DISTINCT sdk-builtin providers (no alias in their tool_name at
    # all) that happen to advertise identically-named tools: the only
    # naturally-reachable "same tool_name, different alias" collision,
    # since mcp-remote/mcp-local's tool_name is alias-qualified and two
    # different aliases would never coincide (see capability_id/tool_name
    # derivation in orchestrator/capability.py's validate()).
    provider_a = cap.ProviderRef(type="sdk-builtin", id="sdk-a", alias="sdk-a")
    provider_b = cap.ProviderRef(type="sdk-builtin", id="sdk-b", alias="sdk-b")
    session_a = FakeSession(tools=[_tool("toolX")])
    session_b = FakeSession(tools=[_tool("toolX")])

    with pytest.raises(cap.CapabilityCollisionError, match="resolve to tool_name"):
        anyio.run(partial(
            gw.resolve_eligible, _plan(("toolX",)), _execution(), [provider_a, provider_b],
            local_sessions={"sdk-a": session_a, "sdk-b": session_b}, headers={},
        ))


# ---------------------------------------------------------------------------
# T6 — Remote and execution-local capabilities share one descriptor shape;
# a local invocation performs zero network calls.
# ---------------------------------------------------------------------------


def test_local_capability_has_the_same_descriptor_shape_and_never_touches_the_network():
    remote_session = FakeSession(tools=[_tool("mctl_whoami")])
    local_session = FakeSession(tools=[_tool("read_file")], call_result=gw.ToolCallResult(is_error=False, text="hi"))
    discovery_connector = _fake_connector({REMOTE_PROVIDER.id: remote_session})

    gateway = anyio.run(partial(
        _build, ("mcp__mctl__*", "mcp__local__*"),
        providers=[REMOTE_PROVIDER, LOCAL_PROVIDER],
        connector=discovery_connector,
        local_sessions={"local-fs": local_session},
    ))

    remote = gateway.descriptors_by_id["mctl://mcp-remote/mctl-api/mctl_whoami"]
    local = gateway.descriptors_by_id["mctl://mcp-local/local-fs/read_file"]
    assert {f.name for f in _fields(remote)} == {f.name for f in _fields(local)}
    assert local.tool_name == "mcp__local__read_file"

    # Poison the remote connector AFTER discovery (which legitimately opens
    # one remote connection) so only the invocation path below is asserted
    # never to reach the network — a local capability must dispatch with
    # zero network hops (ADR 017 sec. 5).
    gateway.connector = _raising_connector("a local invocation must never open a remote connector")

    result = anyio.run(partial(gateway.invoke, local.capability_id, {}))
    assert result == {"reason_code": "ok", "text": "hi"}
    assert local_session.calls == [("read_file", {})]


def _fields(descriptor: cap.CapabilityDescriptor):
    import dataclasses

    return dataclasses.fields(descriptor)


# ---------------------------------------------------------------------------
# T7 — Correlation survives discovery to invocation.
# ---------------------------------------------------------------------------


def test_remote_invocation_propagates_execution_correlation_as_provider_metadata(capsys):
    execution = _execution(agent="issue-investigator", environment="staging")
    session = FakeSession(tools=[_tool("mctl_whoami")])
    header_calls: list[dict] = []
    connector = _fake_connector({REMOTE_PROVIDER.id: session}, header_sink=header_calls)

    gateway = anyio.run(partial(
        _build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector, correlation=execution,
    ))
    header_calls.clear()  # drop the discovery-time connection; only the invocation call matters here

    capability_id = "mctl://mcp-remote/mctl-api/mctl_whoami"
    result = anyio.run(partial(gateway.invoke, capability_id, {"probe": "value"}))
    assert result["reason_code"] == "ok"

    assert len(header_calls) == 1
    sent = header_calls[0]
    assert sent["X-Mctl-Agent"] == execution.agent
    assert sent["X-Mctl-Environment"] == execution.environment
    assert sent["X-Mctl-Temporal-Workflow-Id"] == execution.temporal_workflow_id
    assert sent["X-Mctl-Target-Repository-Sha"] == execution.target_repository_sha
    assert sent["X-Mctl-Definition-Version"] == execution.definition_version
    assert sent["X-Mctl-Profile-Version"] == execution.profile_version
    assert sent["X-Mctl-Capability-Set-Id"] == gateway.capability_set.capability_set_id

    # No argument or result text anywhere in the emitted trace lines.
    captured = capsys.readouterr().out
    assert "probe" not in captured
    assert "value" not in captured
    assert "ok" not in captured.split("CAPABILITY_")[0]  # sanity: trace lines are the only "ok" producer here


# ---------------------------------------------------------------------------
# T9 — Failure taxonomy: distinct reason codes, never collapsed into an
# empty-but-successful discovery or invocation.
# ---------------------------------------------------------------------------


def test_a_provider_that_fails_to_list_raises_instead_of_sealing_a_shorter_set():
    session = FakeSession(tools=[], raise_on_list=gw.ProviderUnavailableError("mctl-api: connection refused"))
    connector = _fake_connector({REMOTE_PROVIDER.id: session})

    with pytest.raises(gw.ProviderUnavailableError):
        anyio.run(partial(
            gw.resolve_eligible, _plan(("mcp__mctl__*",)), _execution(), [REMOTE_PROVIDER],
            connector=connector, headers={},
        ))


def test_invoke_reason_codes_are_distinct_for_each_failure_mode():
    ok_session = FakeSession(tools=[_tool("mctl_whoami")])
    connector = _fake_connector({REMOTE_PROVIDER.id: ok_session})
    gateway = anyio.run(partial(_build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector))
    capability_id = "mctl://mcp-remote/mctl-api/mctl_whoami"

    # not-eligible: id outside the sealed set, no provider contacted at all.
    assert anyio.run(partial(gateway.invoke, "mctl://mcp-remote/mctl-api/does-not-exist", {}))[
        "reason_code"
    ] == "not-eligible"

    # invalid-arguments: arguments is not a JSON object.
    assert anyio.run(partial(gateway.invoke, capability_id, "not-a-mapping"))["reason_code"] == "invalid-arguments"

    # timeout, provider-unavailable, provider-error: three distinct codes
    # for three distinct provider failures on dispatch.
    for exc, expected in (
        (gw.ProviderTimeoutError("timed out"), "timeout"),
        (gw.ProviderUnavailableError("unavailable"), "provider-unavailable"),
        (RuntimeError("boom"), "provider-error"),
    ):
        failing_session = FakeSession(tools=[_tool("mctl_whoami")], raise_on_call=exc)
        failing_gateway = anyio.run(partial(
            _build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER],
            connector=_fake_connector({REMOTE_PROVIDER.id: failing_session}),
        ))
        result = anyio.run(partial(failing_gateway.invoke, capability_id, {}))
        assert result["reason_code"] == expected

    # not-found: describe() on an id outside the sealed set.
    assert gateway.describe(["mctl://mcp-remote/mctl-api/does-not-exist"])["results"][
        "mctl://mcp-remote/mctl-api/does-not-exist"
    ] == {"reason_code": "not-found"}

    # invalid-arguments: describe() asked for more than MAX_DESCRIBE_IDS.
    too_many = [f"mctl://mcp-remote/mctl-api/tool-{i}" for i in range(gw.MAX_DESCRIBE_IDS + 1)]
    assert gateway.describe(too_many)["reason_code"] == "invalid-arguments"


# ---------------------------------------------------------------------------
# T16 — Consequential invocation calls PolicyCheckpoint.check before
# dispatch; a denied verdict returns policy-denied and performs no provider
# call; membership -> checkpoint -> dispatch ordering.
# ---------------------------------------------------------------------------


class _RecordingCheckpoint:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.calls: list[str] = []

    def check(self, descriptor, correlation):
        self.calls.append(descriptor.capability_id)
        return cap.CheckpointVerdict(decision=self.decision)


def test_consequential_invocation_checks_policy_before_dispatch_and_denial_skips_the_call():
    session = FakeSession(tools=[_tool("mctl_deploy_service")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})
    checkpoint = _RecordingCheckpoint("denied")

    gateway = anyio.run(partial(
        _build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector, checkpoint=checkpoint,
    ))
    capability_id = "mctl://mcp-remote/mctl-api/mctl_deploy_service"
    assert gateway.descriptors_by_id[capability_id].consequence == "mutating"  # config/capability-consequence.yaml

    result = anyio.run(partial(gateway.invoke, capability_id, {}))

    assert result["reason_code"] == "policy-denied"
    assert checkpoint.calls == [capability_id]
    assert session.calls == []


def test_read_only_invocation_never_reaches_the_checkpoint():
    session = FakeSession(tools=[_tool("mctl_whoami")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})
    checkpoint = _RecordingCheckpoint("denied")  # would refuse everything, if ever asked

    gateway = anyio.run(partial(
        _build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector, checkpoint=checkpoint,
    ))
    capability_id = "mctl://mcp-remote/mctl-api/mctl_whoami"
    assert gateway.descriptors_by_id[capability_id].consequence == "read-only"

    result = anyio.run(partial(gateway.invoke, capability_id, {}))

    assert result["reason_code"] == "ok"
    assert checkpoint.calls == []  # never consulted


# ---------------------------------------------------------------------------
# Task 6b — PolicyDecidePolicyCheckpoint, the #197 adapter.
# ---------------------------------------------------------------------------


def _trace_lines(captured_out: str, event: str) -> list[dict]:
    prefix = f"CAPABILITY_{event.upper()} "
    return [json.loads(line[len(prefix):]) for line in captured_out.splitlines() if line.startswith(prefix)]


def test_policy_decide_checkpoint_denied_yields_policy_denied_and_no_provider_call(monkeypatch):
    from orchestrator import policy_checkpoint as pc

    def fake_decide(request, **kwargs):
        return pc.Decision(
            verdict=pc.DENY, code=pc.CODE_DENIED, reason="denied for test", policy_version="test-policy",
            rule_id="test-rule", action_digest="digest",
        )

    monkeypatch.setattr(pc, "decide", fake_decide)
    session = FakeSession(tools=[_tool("mctl_deploy_service")])
    connector = _raising_connector("a denied verdict must never contact the provider")
    checkpoint = gw.PolicyDecidePolicyCheckpoint(grants=("mcp__mctl__*",))

    gateway = anyio.run(partial(
        _build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER],
        connector=_fake_connector({REMOTE_PROVIDER.id: session}), checkpoint=checkpoint,
    ))
    # Swap the connector AFTER discovery (which must succeed) so only the
    # invocation path is asserted never to reach the provider.
    gateway.connector = connector
    capability_id = "mctl://mcp-remote/mctl-api/mctl_deploy_service"

    result = anyio.run(partial(gateway.invoke, capability_id, {}))
    assert result["reason_code"] == "policy-denied"


def test_policy_decide_checkpoint_allowed_records_allowed_status(monkeypatch, capsys):
    from orchestrator import policy_checkpoint as pc

    def fake_decide(request, **kwargs):
        return pc.Decision(
            verdict=pc.ALLOW, code=pc.CODE_ALLOWED, reason="ok for test", policy_version="test-policy",
            rule_id="test-rule", action_digest="digest",
        )

    monkeypatch.setattr(pc, "decide", fake_decide)
    session = FakeSession(tools=[_tool("mctl_deploy_service")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})
    checkpoint = gw.PolicyDecidePolicyCheckpoint(grants=("mcp__mctl__*",))

    gateway = anyio.run(partial(
        _build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector, checkpoint=checkpoint,
    ))
    capability_id = "mctl://mcp-remote/mctl-api/mctl_deploy_service"

    result = anyio.run(partial(gateway.invoke, capability_id, {}))
    assert result["reason_code"] == "ok"

    lines = _trace_lines(capsys.readouterr().out, "invocation")
    assert lines and lines[-1]["policy_checkpoint"] == "allowed"


def test_absent_checkpoint_still_records_absent_for_a_mutating_capability(capsys):
    session = FakeSession(tools=[_tool("mctl_deploy_service")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})

    gateway = anyio.run(partial(_build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector))
    capability_id = "mctl://mcp-remote/mctl-api/mctl_deploy_service"
    assert isinstance(gateway.checkpoint, cap.AbsentPolicyCheckpoint)

    result = anyio.run(partial(gateway.invoke, capability_id, {}))
    assert result["reason_code"] == "ok"

    lines = _trace_lines(capsys.readouterr().out, "invocation")
    assert lines and lines[-1]["policy_checkpoint"] == "absent"


# ---------------------------------------------------------------------------
# capability_search / capability_describe basics
# ---------------------------------------------------------------------------


def test_search_returns_compact_rows_without_schemas():
    session = FakeSession(tools=[_tool("mctl_whoami", description="Return the caller identity.")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})
    gateway = anyio.run(partial(_build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector))

    rows = gateway.search("identity")["results"]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"capability_id", "title", "summary", "consequence"}
    assert "input_schema" not in row


def test_describe_returns_full_schema_only_for_eligible_ids():
    session = FakeSession(tools=[_tool("mctl_whoami", input_schema={"type": "object", "properties": {}})])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})
    gateway = anyio.run(partial(_build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector))
    capability_id = "mctl://mcp-remote/mctl-api/mctl_whoami"

    described = gateway.describe([capability_id])["results"][capability_id]
    assert described["reason_code"] == "ok"
    assert described["input_schema"] == {"type": "object", "properties": {}}


def test_set_sealing_trace_line_carries_no_capability_name(capsys):
    session = FakeSession(tools=[_tool("mctl_whoami")])
    connector = _fake_connector({REMOTE_PROVIDER.id: session})
    anyio.run(partial(_build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector))

    lines = _trace_lines(capsys.readouterr().out, "set_sealed")
    assert lines
    rendered = json.dumps(lines[-1])
    assert "mctl_whoami" not in rendered
    assert "tool_name" not in rendered
