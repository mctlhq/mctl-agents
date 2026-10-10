"""Execution traces: the core facade (mctl-agents#195, orchestrator/tracing.py).

What these pin, in the order the design states it:

1. Inert unless configured — no exporter is ever built, no span recorded,
   and the OpenTelemetry SDK is not even imported.
2. A trace never fails an execution — a raising exporter, a raising tracer
   and a broken init all leave the traced code's result intact, logged once.
3. Nothing sensitive leaves the process — the export-side guard scrubs spans
   the producer did NOT filter, including exception text on error statuses.
4. Propagation — a traceparent round-trips into a pod root span's parent.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from orchestrator import tracing, tracing_sdk

REPO_ROOT = Path(__file__).resolve().parent.parent

_OTEL_ENV = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_SDK_DISABLED",
    "OTEL_TRACES_EXPORTER",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
    tracing.ARGO_PARAM_ENV,
    tracing.ERROR_DETAIL_ENV,
    tracing.TRACEPARENT_ENV,
    tracing.WORKFLOW_TYPES_ENV,
    tracing.REQUIRE_PARENT_ENV,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in _OTEL_ENV:
        monkeypatch.delenv(name, raising=False)
    tracing._reset_for_tests()
    yield
    tracing._reset_for_tests()


@pytest.fixture
def exported() -> InMemorySpanExporter:
    """Tracing on, synchronously, into memory — through the real guard."""
    exporter = InMemorySpanExporter()
    assert tracing.init_tracing("test", exporter=exporter, synchronous=True, set_global=False)
    return exporter


class _SpyExporter(SpanExporter):
    def __init__(self) -> None:
        self.calls = 0

    def export(self, spans):
        self.calls += 1
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


class _RaisingExporter(SpanExporter):
    def __init__(self) -> None:
        self.calls = 0

    def export(self, spans):
        self.calls += 1
        raise ConnectionError("collector unreachable")

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. Inert unless configured
# ---------------------------------------------------------------------------


def test_unconfigured_is_a_noop_with_zero_exporter_calls(monkeypatch):
    built: list[object] = []
    spy = _SpyExporter()

    def factory(env):
        built.append(env)
        return spy

    monkeypatch.setattr(tracing_sdk, "_otlp_exporter", factory)

    assert tracing.init_tracing("test", set_global=False) is False
    assert tracing.enabled() is False
    with tracing.span("x", {tracing.REPOSITORY_NAME: "mctlhq/a"}) as handle:
        handle.set(execution_id="we_1")
        handle.event("e")
    tracing.record_policy_decision(
        rule_id="r", decision="ALLOW", code="allowed", policy_version="1", action_kind="k", operation="o"
    )
    assert handle is tracing.NOOP
    assert tracing.current_traceparent() is None
    assert tracing.with_traceparent({"a": "b"}, {tracing.ARGO_PARAM_ENV: "true"}) == {"a": "b"}
    assert built == []
    assert spy.calls == 0


def test_an_unconfigured_process_never_imports_the_sdk():
    """Import is the cost that matters in the 256Mi worker: prove the whole
    no-op path runs without loading a single opentelemetry module."""
    code = (
        "import os, sys\n"
        "for k in list(os.environ):\n"
        "    if k.startswith('OTEL_'): del os.environ[k]\n"
        "from orchestrator import tracing\n"
        "tracing.init_tracing('x')\n"
        "with tracing.span('s') as h: h.set(execution_id='we_1')\n"
        "tracing.annotate(workflow_id='w')\n"
        "print(sorted(m for m in sys.modules if m.startswith('opentelemetry')))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}, True),
        ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces"}, True),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "OTEL_SDK_DISABLED": "true"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "OTEL_TRACES_EXPORTER": "none"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}, False),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf"}, True),
    ],
)
def test_endpoint_configured_follows_the_standard_variables(env, expected):
    assert tracing.endpoint_configured(env) is expected


def test_a_configured_endpoint_builds_the_exporter_once_and_exports(monkeypatch):
    """Control for the no-op test: the same spy IS called once configured."""
    spy = _SpyExporter()
    built: list[object] = []

    def factory(env):
        built.append(env)
        return spy

    monkeypatch.setattr(tracing_sdk, "_otlp_exporter", factory)
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}

    assert tracing.init_tracing("test", set_global=False, environ=env) is True
    with tracing.span("x"):
        pass
    tracing._state.provider.force_flush()

    assert len(built) == 1
    assert spy.calls == 1


# ---------------------------------------------------------------------------
# 2. A trace never fails an execution
# ---------------------------------------------------------------------------


def test_a_raising_exporter_never_fails_the_execution_and_logs_once(caplog):
    exporter = _RaisingExporter()
    assert tracing.init_tracing("test", exporter=exporter, synchronous=True, set_global=False)

    def execution() -> str:
        for _ in range(3):
            with tracing.span("step") as handle:
                handle.set(execution_id="we_1")
        return "done"

    with caplog.at_level(logging.WARNING, logger="orchestrator.tracing"):
        assert execution() == "done"

    assert exporter.calls == 3
    ours = [r for r in caplog.records if r.name == "orchestrator.tracing" and "export failed" in r.getMessage()]
    assert len(ours) == 1


def test_the_guarded_exporter_swallows_what_the_inner_exporter_raises():
    """Directly, not through the SDK processor — which has its own catch, and
    would otherwise hide a guard that stopped catching."""
    guarded = tracing_sdk.GuardedExporter(_RaisingExporter())
    assert guarded.export([]) is SpanExportResult.FAILURE


def test_a_tracer_that_raises_on_start_leaves_the_block_running(exported, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("tracer is broken")

    monkeypatch.setattr(tracing._state.tracer, "start_span", boom)

    ran = []
    with tracing.span("x") as handle:
        ran.append(True)
    assert ran == [True]
    assert handle is tracing.NOOP


def test_the_block_s_own_exception_propagates_unchanged_and_is_recorded_by_type(exported):
    with pytest.raises(KeyError, match="secret-key-name"), tracing.span("x"):
        raise KeyError("secret-key-name")

    (span,) = exported.get_finished_spans()
    assert span.attributes["error.type"] == "KeyError"
    assert span.status.status_code is StatusCode.ERROR
    assert "secret-key-name" not in json.dumps(json.loads(span.to_json()))


def test_a_non_zero_exit_is_recorded_as_its_exit_code(exported):
    with pytest.raises(SystemExit), tracing.span("pod"):
        sys.exit(44)
    with pytest.raises(SystemExit), tracing.span("pod-ok"):
        sys.exit(0)

    failed, clean = exported.get_finished_spans()
    assert failed.attributes["error.type"] == "exit_44"
    assert "error.type" not in clean.attributes


def test_an_init_that_cannot_build_the_pipeline_leaves_tracing_off(monkeypatch):
    def boom(*args, **kwargs):
        raise ImportError("no sdk in this image")

    monkeypatch.setattr(tracing_sdk, "build_provider", boom)
    assert tracing.init_tracing("test", exporter=_SpyExporter(), set_global=False) is False
    assert tracing.enabled() is False


def test_bounded_shutdown_returns_even_when_the_flush_hangs():
    import threading
    import time

    release = threading.Event()

    class _Hanging:
        def force_flush(self, timeout_millis):
            release.wait(10)

        def shutdown(self):
            pass

    started = time.monotonic()
    tracing_sdk.bounded_shutdown(_Hanging(), 0.2)
    release.set()
    assert time.monotonic() - started < 2


# ---------------------------------------------------------------------------
# 3. Redaction
# ---------------------------------------------------------------------------

_SENSITIVE_MARKERS = (
    "PROMPT-TEXT",
    "TOOL-ARGS",
    "TOOL-OUTPUT",
    "ISSUE-BODY",
    "ghp_",
    "sk-ant",
    "hvs.",
    "eyJ",
    "Bearer",
    "hunter2",
    "EXC-MESSAGE",
    "STATUS-DESCRIPTION",
)


def _raw_span_with_everything_sensitive() -> None:
    """A span created on the raw tracer, bypassing every source-side filter
    — the shape a third-party library (or a careless call site) produces."""
    raw = tracing.tracer()
    span = raw.start_span(
        "raw",
        attributes={
            "gen_ai.prompt": "PROMPT-TEXT",
            "gen_ai.input.messages": "PROMPT-TEXT",
            "mcp.tool.arguments": "TOOL-ARGS",
            "mctl.tool.arguments": "TOOL-ARGS",
            "mctl.tool.result": "TOOL-OUTPUT",
            "mctl.issue.body": "ISSUE-BODY",
            "mctl.github.token": "ghp_" + "a" * 36,
            "mctl.note": "ghp_" + "b" * 36,
            "mctl.model.key": "sk-ant-" + "c" * 30,
            "mctl.vault.ref": "hvs." + "d" * 30,
            "mctl.jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
            "http.request.header.authorization": "Bearer abcdefghijklmnop",
            "db.password": "hunter2",
            "mctl.long": "x" * 1000,
            "mctl.repository.name": "mctlhq/mctl-agents",
            "gen_ai.usage.input_tokens": 1200,
            "gen_ai.usage.output_tokens": 340,
            "mctl.execution.id": "we_0123",
        },
    )
    span.add_event("tool", {"mcp.tool.result": "TOOL-OUTPUT", "mctl.tool.name": "Read"})
    span.record_exception(ValueError("EXC-MESSAGE with ghp_" + "e" * 36))
    span.set_status(Status(StatusCode.ERROR, "STATUS-DESCRIPTION"))
    span.end()


def test_the_export_guard_scrubs_spans_the_producer_did_not_filter(exported):
    _raw_span_with_everything_sensitive()

    (span,) = exported.get_finished_spans()
    blob = span.to_json()
    for marker in _SENSITIVE_MARKERS:
        assert marker not in blob, marker
    assert dict(span.attributes) == {
        "mctl.repository.name": "mctlhq/mctl-agents",
        "gen_ai.usage.input_tokens": 1200,
        "gen_ai.usage.output_tokens": 340,
        "mctl.execution.id": "we_0123",
    }
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    exception_event = next(e for e in span.events if e.name == "exception")
    assert dict(exception_event.attributes) == {"exception.type": "ValueError", "exception.escaped": "False"}


@pytest.mark.parametrize("variable", ["OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT"])
def test_an_env_length_limit_cannot_truncate_a_payload_past_the_guard(monkeypatch, variable):
    """Review P3 on #466: with an SDK length limit a long value is truncated at
    record time and the truncated payload then passes the guard's length check."""
    monkeypatch.setenv(variable, "200")
    exporter = InMemorySpanExporter()
    assert tracing.init_tracing("test", exporter=exporter, synchronous=True, set_global=False)
    span = tracing.tracer().start_span("raw", attributes={"mctl.long": "PAYLOAD" + "x" * 400})
    span.end()
    (exported_span,) = exporter.get_finished_spans()
    assert "mctl.long" not in exported_span.attributes
    assert "PAYLOAD" not in exported_span.to_json()


def test_link_attributes_are_redacted_like_span_attributes():
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import Link, SpanContext, TraceFlags

    ctx = SpanContext(trace_id=1, span_id=2, is_remote=False, trace_flags=TraceFlags(1))
    raw = ReadableSpan(
        name="raw",
        context=ctx,
        links=[
            Link(ctx, {"gen_ai.prompt": "PROMPT-TEXT", "mctl.note": "ghp_" + "b" * 36, "mctl.execution.id": "we_1"})
        ],
    )
    (link,) = tracing_sdk.redacted_copy(raw).links
    assert dict(link.attributes) == {"mctl.execution.id": "we_1"}
    assert link.context == ctx


def test_the_sdk_log_filter_keeps_one_record_per_level():
    """Review P3 on #466: a first record of any level used to spend the whole
    budget, hiding the real export-failure line for the life of the worker."""
    once = tracing_sdk._OnceFilter()

    def record(level):
        return logging.LogRecord("opentelemetry.sdk._shared_internal", level, __file__, 1, "m", None, None)

    assert once.filter(record(logging.DEBUG))
    assert once.filter(record(logging.WARNING))
    assert once.filter(record(logging.ERROR))  # the export failure still gets through
    assert not once.filter(record(logging.ERROR))
    assert not once.filter(record(logging.WARNING))


def test_every_exported_key_is_on_the_allowlist(exported):
    _raw_span_with_everything_sensitive()
    with tracing.span("helper", {"mctl.workflow.type": "investigate"}) as handle:
        handle.set_attributes({"mctl.issue.body": "ISSUE-BODY", "mctl.tool.name": "Bash"})
        handle.event("e", {"mctl.policy.code": "allowed", "mctl.policy.reason": "x"})

    for span in exported.get_finished_spans():
        keys = list(span.attributes) + [k for e in span.events for k in e.attributes]
        for key in keys:
            assert tracing_sdk.key_allowed(key), key


@pytest.mark.parametrize(
    "key",
    [
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "mcp.tool.arguments",
        "mcp.tool.result",
        "mctl.tool.arguments",
        "mctl.tool.input",
        "mctl.tool.output",
        "mctl.issue.body",
        "mctl.github.command",
        "mctl.pr.comment",
        "mctl.github.token",
        "mctl.api_key",
        "mctl.client.secret",
        "mctl.db.password",
        "mctl.credentials.path",
        "http.request.header.authorization",
        "exception.message",
        "exception.stacktrace",
        "some.random.library.key",
    ],
)
def test_sensitive_or_unknown_keys_are_refused(key):
    assert tracing_sdk.key_allowed(key) is False


@pytest.mark.parametrize(
    "key",
    [
        "mctl.execution.id",
        "mctl.workflow.id",
        "mctl.workflow.run_id",
        "mctl.argo.workflow.name",
        "mctl.work_item.id",
        "mctl.tool.name",
        "mctl.tool.status",
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "error.type",
        "temporalWorkflowID",
        "k8s.pod.name",
    ],
)
def test_catalog_keys_are_allowed(key):
    assert tracing_sdk.key_allowed(key) is True


def test_a_string_under_a_usage_counter_is_refused():
    assert tracing_sdk.redact_attributes({"gen_ai.usage.input_tokens": "ghp_" + "a" * 36}) == {}


# ---------------------------------------------------------------------------
# T14 (mctlhq/mctl-agents#527 Slice B, tasks.md): the five names reserved in
# docs/observability/execution-traces.md must actually clear the export
# guard's allowlist/denylist, so a reserved catalog name is not silently
# dropped by the final-segment denylist (orchestrator/redaction.py:26) —
# `content_hash`'s final segment is `content_hash`, not `content`, and
# `context` is not `text`.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    [
        "mctl.context.strategy.name",
        "mctl.context.strategy.version",
        "mctl.context.strategy.content_hash",
        "mctl.context.binding.revision",
        "mctl.context.release.mode",
    ],
)
def test_reserved_context_release_attribute_names_are_allowed(key):
    assert tracing_sdk.key_allowed(key) is True


def test_error_detail_is_opt_in_and_still_credential_filtered(monkeypatch):
    monkeypatch.setenv(tracing.ERROR_DETAIL_ENV, "true")
    exporter = InMemorySpanExporter()
    assert tracing.init_tracing("test", exporter=exporter, synchronous=True, set_global=False)
    raw = tracing.tracer()
    span = raw.start_span("x")
    span.record_exception(ValueError("clone failed: 128"))
    span.record_exception(ValueError("auth ghp_" + "z" * 36))
    span.end()

    (exported_span,) = exporter.get_finished_spans()
    messages = [e.attributes.get("exception.message") for e in exported_span.events]
    assert messages == ["clone failed: 128", None]


# ---------------------------------------------------------------------------
# 4. Propagation
# ---------------------------------------------------------------------------

_PARENT_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_PARENT_SPAN = "00f067aa0ba902b7"
_TRACEPARENT = f"00-{_PARENT_TRACE}-{_PARENT_SPAN}-01"


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (_TRACEPARENT, True),
        (f"00-{'0' * 32}-{_PARENT_SPAN}-01", False),
        (f"00-{_PARENT_TRACE}-{'0' * 16}-01", False),
        ("garbage", False),
        ("", False),
        (None, False),
    ],
)
def test_valid_traceparent(value, valid):
    assert tracing.valid_traceparent(value) is valid


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        f"00-{'0' * 32}-{_PARENT_SPAN}-01",
        f"00-{_PARENT_TRACE}-{'0' * 16}-01",
    ],
)
def test_trace_ids_rejects_malformed_and_zero_traceparents(value):
    assert tracing.trace_ids(value) is None


def test_trace_ids_parses_a_valid_traceparent_into_its_hex_pair():
    assert tracing.trace_ids(_TRACEPARENT) == (_PARENT_TRACE, _PARENT_SPAN)


def test_current_trace_ids_is_none_when_tracing_is_off():
    assert tracing.enabled() is False
    assert tracing.current_trace_ids() is None


def test_current_trace_ids_matches_the_current_span(exported):
    with tracing.span("x"):
        ids = tracing.current_trace_ids()
        ctx = tracing.current().otel_span.get_span_context()
    assert ids == (f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}")


def test_pod_root_span_is_parented_on_the_traceparent_env(exported):
    env = {tracing.TRACEPARENT_ENV: _TRACEPARENT, tracing.ARGO_WORKFLOW_NAME_ENV: "mctl-agents-investigate-ab12"}
    with tracing.pod_root_span("investigator.run", {tracing.AGENT_NAME: "issue-investigator"}, environ=env):
        with tracing.span("child"):
            pass

    child, root = exported.get_finished_spans()
    assert f"{root.context.trace_id:032x}" == _PARENT_TRACE
    assert f"{root.parent.span_id:016x}" == _PARENT_SPAN
    assert root.parent.is_remote
    assert root.attributes["mctl.argo.workflow.name"] == "mctl-agents-investigate-ab12"
    assert root.attributes["mctl.agent.name"] == "issue-investigator"
    assert child.parent.span_id == root.context.span_id


def test_a_malformed_traceparent_starts_a_fresh_trace(exported):
    with tracing.pod_root_span("run", environ={tracing.TRACEPARENT_ENV: "not-a-traceparent"}):
        pass
    (root,) = exported.get_finished_spans()
    assert root.parent is None


def test_an_empty_traceparent_is_silent(exported, caplog):
    """The CWFT defaults the parameter to "" — that is "no parent", not an error."""
    with caplog.at_level(logging.WARNING, logger="orchestrator.tracing"):
        with tracing.pod_root_span("run", environ={tracing.TRACEPARENT_ENV: ""}):
            pass
    (root,) = exported.get_finished_spans()
    assert root.parent is None
    assert not [r for r in caplog.records if r.name == "orchestrator.tracing"]


def test_with_traceparent_needs_the_rollout_flag(exported):
    with tracing.span("argo"):
        assert tracing.with_traceparent({"issue_url": "u"}, {}) == {"issue_url": "u"}
        flagged = tracing.with_traceparent({"issue_url": "u"}, {tracing.ARGO_PARAM_ENV: "true"})
    assert tracing.valid_traceparent(flagged[tracing.TRACEPARENT_PARAM])
    assert flagged["issue_url"] == "u"


def test_workflow_trace_ids_are_deterministic_per_run():
    a = tracing.workflow_trace_ids("dev-loop-mctlhq-x-1", "run-a")
    assert a == tracing.workflow_trace_ids("dev-loop-mctlhq-x-1", "run-a")
    assert a[0] != tracing.workflow_trace_ids("dev-loop-mctlhq-x-1", "run-b")[0]
    assert a[0] != tracing.workflow_trace_ids("dev-loop-mctlhq-x-2", "run-a")[0]
    assert 0 < a[0] < 2**128 and 0 < a[1] < 2**64


# ---------------------------------------------------------------------------
# Rollout scope (#195 live rollout): MCTL_TRACE_WORKFLOW_TYPES and
# MCTL_TRACE_REQUIRE_PARENT
# ---------------------------------------------------------------------------

_SAMPLED = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
_UNSAMPLED = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"


@pytest.mark.parametrize(
    ("value", "sampled"),
    [(_SAMPLED, True), (_UNSAMPLED, False), ("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-03", True),
     ("", False), (None, False), ("garbage", False)],
)
def test_sampled_traceparent(value, sampled):
    assert tracing.sampled_traceparent(value) is sampled


@pytest.mark.parametrize(
    ("environ", "workflow_type", "traced"),
    [
        ({}, "AnyWorkflow", True),
        ({tracing.WORKFLOW_TYPES_ENV: "DevLoopWorkflow"}, "DevLoopWorkflow", True),
        ({tracing.WORKFLOW_TYPES_ENV: " DevLoopWorkflow , Other "}, "Other", True),
        ({tracing.WORKFLOW_TYPES_ENV: "DevLoopWorkflow"}, "ReconcileWorkflow", False),
        ({tracing.WORKFLOW_TYPES_ENV: "DevLoopWorkflow"}, "", False),
        ({tracing.WORKFLOW_TYPES_ENV: "DevLoopWorkflow"}, None, False),
        ({tracing.WORKFLOW_TYPES_ENV: ""}, "DevLoopWorkflow", False),
    ],
)
def test_workflow_type_traced(environ, workflow_type, traced):
    assert tracing.workflow_type_traced(workflow_type, environ) is traced


@pytest.mark.parametrize(
    ("traceparent", "on"),
    [(None, False), ("", False), ("garbage", False), (_UNSAMPLED, False), (_SAMPLED, True)],
)
def test_require_parent_keeps_a_process_without_a_sampled_parent_inert(traceparent, on):
    environ = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318", tracing.REQUIRE_PARENT_ENV: "true"}
    if traceparent is not None:
        environ[tracing.TRACEPARENT_ENV] = traceparent
    assert tracing.init_tracing("test", exporter=InMemorySpanExporter(), synchronous=True,
                                set_global=False, environ=environ) is on
    assert tracing.enabled() is on


def test_without_require_parent_a_parentless_process_still_traces():
    environ = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}
    assert tracing.init_tracing("test", exporter=InMemorySpanExporter(), synchronous=True,
                                set_global=False, environ=environ)


def test_an_unsampled_workflow_root_drops_everything_under_it(exported):
    context = tracing.workflow_root_context("wf", "run", sampled=False)
    assert context is not None
    with tracing.span("child", parent=context), tracing.span("grandchild"):
        pass
    assert exported.get_finished_spans() == ()


def test_an_unsampled_root_is_returned_even_without_ids(exported):
    """None would let the activity span open a sampled trace of its own."""
    assert tracing.workflow_root_context("", "", sampled=False) is not None
    assert tracing.workflow_root_context("", "") is None
