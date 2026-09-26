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
        checkpoint=checkpoint if checkpoint is not None else cap.AbsentPolicyCheckpoint(),
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


# ---------------------------------------------------------------------------
# Review follow-ups on #508.
# ---------------------------------------------------------------------------


def test_parse_capability_id_rejects_extra_path_segments():
    """`split("/", 2)` folded "provider/tool" into the tool part (agy P3)."""
    with pytest.raises(cap.CapabilityError, match="must have the shape"):
        cap._parse_capability_id("mctl://mcp-remote/ns/provider/tool", where="test")
    assert cap._parse_capability_id("mctl://mcp-remote/provider/tool", where="test") == (
        "mcp-remote", "provider", "tool",
    )


def test_two_providers_sharing_type_and_id_under_different_aliases_raise_collision():
    """Distinct aliases give distinct tool_names but ONE capability_id per
    shared tool; dispatch is keyed by capability_id, so the last provider
    would silently win (claude P2 on #508)."""
    provider_a = cap.ProviderRef(type="mcp-remote", id="same", alias="a", endpoint_ref="https://a")
    provider_b = cap.ProviderRef(type="mcp-remote", id="same", alias="b", endpoint_ref="https://b")
    session = FakeSession(tools=[_tool("T")])
    connector = _fake_connector({"same": session})

    with pytest.raises(cap.CapabilityCollisionError, match="share type"):
        anyio.run(partial(
            gw.resolve_eligible, _plan(("mcp__a__*", "mcp__b__*")), _execution(), [provider_a, provider_b],
            connector=connector, headers={},
        ))


def test_oversized_or_unserializable_annotations_degrade_only_their_own_descriptor():
    """Annotations are advisory (ADR 017 sec. 8): one chatty or odd blob
    must not abort discovery for every provider (claude P2 on #508)."""
    from datetime import UTC, datetime

    big = gw.ProviderTool(name="mctl_whoami", input_schema={"type": "object"},
                          annotations={"blob": "x" * (cap.MAX_ANNOTATIONS_JSON_LENGTH + 1)})
    small = gw.ProviderTool(name="mctl_list_services", input_schema={"type": "object"},
                            annotations={"readOnlyHint": True})
    session = FakeSession(tools=[big, small])

    capability_set = anyio.run(partial(
        gw.resolve_eligible, _plan(("mcp__mctl__*",)), _execution(), [REMOTE_PROVIDER],
        connector=_fake_connector({REMOTE_PROVIDER.id: session}), headers={},
    ))
    by_name = {c.tool_name: c for c in capability_set.capabilities}
    assert by_name["mcp__mctl__mctl_whoami"].annotations == {}
    assert by_name["mcp__mctl__mctl_list_services"].annotations == {"readOnlyHint": True}

    coerced = gw._annotations_to_dict({"at": datetime(2026, 9, 26, tzinfo=UTC)})
    assert coerced == {"at": "2026-09-26 00:00:00+00:00"}


def test_discovery_carries_correlation_headers_without_a_set_id():
    """`list_tools` reaches the provider with the same #196 fields an
    invocation sends, except the set id that does not exist yet (claude P3
    on #508)."""
    execution = _execution()
    header_calls: list[dict] = []
    connector = _fake_connector({REMOTE_PROVIDER.id: FakeSession(tools=[_tool("mctl_whoami")])},
                                header_sink=header_calls)

    anyio.run(partial(_build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER], connector=connector,
                      correlation=execution))

    assert len(header_calls) == 1
    assert header_calls[0]["X-Mctl-Agent"] == execution.agent
    assert header_calls[0]["X-Mctl-Temporal-Workflow-Id"] == execution.temporal_workflow_id
    assert "X-Mctl-Capability-Set-Id" not in header_calls[0]


def _whoami_gateway(**session_kwargs):
    session = FakeSession(tools=[_tool("mctl_whoami")], **session_kwargs)
    gateway = anyio.run(partial(_build, ("mcp__mctl__*",), providers=[REMOTE_PROVIDER],
                                connector=_fake_connector({REMOTE_PROVIDER.id: session})))
    return gateway, session


@pytest.mark.parametrize("limit", ["abc", 0, -1, True, 2.5])
def test_search_rejects_a_bad_limit_as_invalid_arguments(limit):
    gateway, _ = _whoami_gateway()
    result = gateway.search("whoami", limit)
    assert result["reason_code"] == "invalid-arguments"
    assert result["results"] == []


def test_search_rejects_a_non_string_query_and_marks_success_ok():
    gateway, _ = _whoami_gateway()
    assert gateway.search(["whoami"])["reason_code"] == "invalid-arguments"
    ok = gateway.search("whoami")
    assert ok["reason_code"] == "ok"
    assert [row["capability_id"] for row in ok["results"]] == ["mctl://mcp-remote/mctl-api/mctl_whoami"]


@pytest.mark.parametrize("capability_ids", [[{"a": 1}], [["nested"]], "mctl://x/y/z", None, 5])
def test_describe_rejects_anything_but_a_list_of_strings(capability_ids):
    """An unhashable element used to raise TypeError out of the tool
    handler (claude P2 on #508)."""
    gateway, _ = _whoami_gateway()
    result = gateway.describe(capability_ids)
    assert result["reason_code"] == "invalid-arguments"


def test_invoke_rejects_a_non_string_capability_id():
    gateway, session = _whoami_gateway()
    result = anyio.run(partial(gateway.invoke, {"a": 1}, {}))
    assert result["reason_code"] == "invalid-arguments"
    assert session.calls == []


def test_provider_exception_text_never_reaches_the_model():
    """A transport error can carry URLs or upstream bodies; the model gets
    the reason code and a fixed message (claude P3 on #508)."""
    gateway, _ = _whoami_gateway(raise_on_call=RuntimeError("GET https://internal.example/secret failed: body"))
    result = anyio.run(partial(gateway.invoke, "mctl://mcp-remote/mctl-api/mctl_whoami", {}))
    assert result["reason_code"] == "provider-error"
    assert "secret" not in result["error"] and "internal.example" not in result["error"]


def test_build_requires_an_explicit_checkpoint():
    """No silent AbsentPolicyCheckpoint default (claude P2 on #508)."""
    connector = _fake_connector({REMOTE_PROVIDER.id: FakeSession(tools=[_tool("mctl_whoami")])})
    with pytest.raises(TypeError, match="checkpoint"):
        anyio.run(partial(
            gw.CapabilityGateway.build, _plan(("mcp__mctl__*",)), _execution(), [REMOTE_PROVIDER],
            connector=connector, headers={},
        ))


# -- the default remote connector -------------------------------------------


class _FakeClientSession:
    def __init__(self, *_streams, fail_initialize: Exception | None = None):
        self.fail_initialize = fail_initialize

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        if self.fail_initialize is not None:
            raise self.fail_initialize


def _patch_mcp_transport(monkeypatch, *, fail_initialize: Exception | None = None):
    import mcp
    import mcp.client.streamable_http as streamable_http

    @asynccontextmanager
    async def _client(_url, headers=None):
        yield (object(), object(), lambda: None)

    monkeypatch.setattr(streamable_http, "streamablehttp_client", _client)
    monkeypatch.setattr(mcp, "ClientSession",
                        lambda *streams: _FakeClientSession(*streams, fail_initialize=fail_initialize))


def test_default_connector_lets_errors_inside_the_session_propagate_unchanged(monkeypatch):
    """An exception raised in the caller's `async with` body is thrown into
    the generator at `yield`; relabelling it provider-unavailable made every
    call_tool timeout and error look like a connection failure (agy P2 on
    #508)."""
    _patch_mcp_transport(monkeypatch)

    async def _use(exc):
        async with gw._default_remote_connector(REMOTE_PROVIDER, {}):
            raise exc

    with pytest.raises(gw.ProviderTimeoutError):
        anyio.run(_use, gw.ProviderTimeoutError("call timed out"))
    with pytest.raises(gw.GatewayError) as caught:
        anyio.run(_use, gw.GatewayError("call failed"))
    assert type(caught.value) is gw.GatewayError


def test_default_connector_still_classifies_connection_failures(monkeypatch):
    _patch_mcp_transport(monkeypatch, fail_initialize=RuntimeError("refused"))

    async def _connect():
        async with gw._default_remote_connector(REMOTE_PROVIDER, {}):
            pass

    with pytest.raises(gw.ProviderUnavailableError):
        anyio.run(_connect)


# -- the SDK tool layer ------------------------------------------------------


def _sdk_tools_by_name(gateway):
    return {t.name: t for t in gateway.sdk_tools()}


def test_sdk_tool_schemas_require_only_what_each_handler_needs():
    """The `{"key": type}` shorthand marks every key required, which made
    capability_search uncallable without a limit (claude P2 on #508)."""
    tools = _sdk_tools_by_name(_whoami_gateway()[0])
    assert set(tools) == {"capability_search", "capability_describe", "capability_invoke"}
    assert "required" not in tools["capability_search"].input_schema
    assert tools["capability_describe"].input_schema["required"] == ["capability_ids"]
    assert tools["capability_invoke"].input_schema["required"] == ["capability_id"]


def _call_sdk_tool(tool, args):
    result = anyio.run(tool.handler, args)
    return json.loads(result["content"][0]["text"])


def test_sdk_tool_handlers_serve_the_gateway_and_report_bad_input_as_reason_codes():
    gateway, session = _whoami_gateway()
    tools = _sdk_tools_by_name(gateway)
    capability_id = "mctl://mcp-remote/mctl-api/mctl_whoami"

    assert _call_sdk_tool(tools["capability_search"], {"query": "whoami"})["reason_code"] == "ok"
    assert _call_sdk_tool(tools["capability_search"], {"limit": "abc"})["reason_code"] == "invalid-arguments"
    assert _call_sdk_tool(tools["capability_describe"], {"capability_ids": [{"a": 1}]})["reason_code"] == (
        "invalid-arguments"
    )
    described = _call_sdk_tool(tools["capability_describe"], {"capability_ids": [capability_id]})
    assert described["results"][capability_id]["reason_code"] == "ok"
    invoked = _call_sdk_tool(tools["capability_invoke"], {"capability_id": capability_id})
    assert invoked == {"reason_code": "ok", "text": "ok"}
    assert session.calls == [("mctl_whoami", {})]


def test_sdk_server_wraps_the_three_tools():
    server = _whoami_gateway()[0].sdk_server()
    assert server["type"] == "sdk"
    assert server["name"] == "capability"
