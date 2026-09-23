"""The OpenTelemetry-SDK half of `orchestrator/tracing.py` (mctl-agents#195).

Imported ONLY by `tracing.py`, and only lazily, once tracing is actually
configured: nothing here may be imported at module scope anywhere else, or
an unconfigured process would pay for the SDK and an exporter it never uses.

Three things live here:

- `redact_attributes` — the redaction guard. Applied at the source (every
  `SpanHandle.set_attributes`) AND at export (`GuardedExporter`), so a span
  some library created with a sensitive attribute is scrubbed too.
- `GuardedExporter` — wraps the real exporter: redacts, swallows every
  exception, logs once, and never exports a span it could not redact.
- `build_provider` / `bounded_shutdown` — a bounded, non-blocking pipeline.
"""
from __future__ import annotations

import atexit
import logging
import os
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind, Status, StatusCode

from orchestrator import tracing

logger = logging.getLogger(__name__)

# Bounds on the export pipeline. The queue drops rather than blocks when
# full (BatchSpanProcessor's documented behaviour), so a dead Collector costs
# at most this much memory and never a stalled agent.
MAX_QUEUE_SIZE = 2048
MAX_EXPORT_BATCH_SIZE = 256
SCHEDULE_DELAY_MILLIS = 2000

# An attribute value longer than this is a payload, not an identifier. It is
# DROPPED, not truncated: a truncated payload is still a payload.
MAX_ATTRIBUTE_CHARS = 256

# ---------------------------------------------------------------------------
# Redaction guard
# ---------------------------------------------------------------------------

# Allowlist: the ONLY keys that may leave the process. Everything else is
# dropped, including every key a future library invents — the failure mode
# of a denylist alone is the name nobody thought of.
_ALLOWED_KEY = re.compile(
    r"^(?:"
    r"mctl\.[a-z0-9_]+(?:\.[a-z0-9_]+)*"  # the mctl catalog namespace
    r"|gen_ai\.(?:operation\.name|provider\.name|request\.model|response\.model|agent\.name|tool\.name"
    r"|usage\.input_tokens|usage\.output_tokens)"
    r"|mcp\.method\.name"
    r"|error\.type"
    r"|exception\.(?:type|escaped)"
    r"|k8s\.pod\.name"
    # temporalio.contrib.opentelemetry's own correlation keys: ids only.
    r"|temporal(?:WorkflowID|RunID|ActivityID|ActivityType|SignalName|QueryName|UpdateID|UpdateName"
    r"|ChildWorkflowID|NexusService|NexusOperation)"
    r")$"
)
# Opt-in only (MCTL_TRACE_ERROR_DETAIL): still value-checked and length-capped.
_ERROR_DETAIL_KEY = re.compile(r"^exception\.(?:message|stacktrace)$")
# Denylist, applied ON TOP of the allowlist — so `mctl.issue.body` or
# `mctl.tool.arguments` is refused even though it is in the mctl namespace.
# Mirrors the Collector's patterns (mctl-docs telemetry-attributes.md,
# "Privacy") plus the payload-shaped suffixes this repo could plausibly
# produce.
_DENIED_KEY = re.compile(
    r"(?i)(?:authorization|cookie|api[-_]?key|secret|passw(?:or)?d|credential|private[-_]?key|session[-_]?key"
    r"|(?:^|[._])(?:prompts?|completions?|messages?|arguments?|args|argv|input|output|results?|body|content"
    r"|text|payload|stdout|stderr|command|cmd|diff|query|description|comment)$)"
)
_TOKEN_KEY = re.compile(r"(?i)token")
# The two usage counters are the only keys allowed to contain "token": they
# are integers, and an integer cannot hold a credential.
_USAGE_TOKEN_KEYS = frozenset({"gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"})
# Credential SHAPES, checked on every string value regardless of its key:
# GitHub tokens, fine-grained PATs, sk- keys (Anthropic/OpenAI), Vault
# tokens, JWTs, PEM private keys, bearer headers, basic-auth URLs.
_CREDENTIAL_VALUE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|hv[sbr]\.[A-Za-z0-9_-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:bearer\s+[A-Za-z0-9._~+/-]{12,})"
    r"|://[^/\s:@]+:[^/\s@]+@)"
)


def key_allowed(key: str, *, error_detail: bool = False) -> bool:
    if not isinstance(key, str) or not key:
        return False
    if key in _USAGE_TOKEN_KEYS:
        return True
    if error_detail and _ERROR_DETAIL_KEY.match(key):
        # Checked before the denylist, which would refuse `.message`; the
        # VALUE still goes through the credential and length checks.
        return True
    if _TOKEN_KEY.search(key) or _DENIED_KEY.search(key):
        return False
    return bool(_ALLOWED_KEY.match(key))


def _scalar_allowed(value: Any) -> bool:
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return 0 < len(value) <= MAX_ATTRIBUTE_CHARS and not _CREDENTIAL_VALUE.search(value)
    return False


def value_allowed(value: Any) -> bool:
    if isinstance(value, list | tuple):
        return len(value) <= 32 and all(_scalar_allowed(v) for v in value)
    return _scalar_allowed(value)


def redact_attributes(attributes: Mapping[str, Any] | None, *, error_detail: bool = False) -> dict[str, Any]:
    """Only allowlisted keys with safe, bounded, credential-free values.

    Dropped, never masked: a masked value is a present attribute that reads
    like data (the Collector's `****` problem, mctl-gitops#1332), an absent
    one is honest. `usage` counters must be ints — a string under a token
    key is refused even though the key is allowed."""
    if not attributes:
        return {}
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if not key_allowed(key, error_detail=error_detail):
            continue
        if key in _USAGE_TOKEN_KEYS and (not isinstance(value, int) or isinstance(value, bool)):
            continue
        if isinstance(value, list | tuple):
            value = list(value)
        if not value_allowed(value):
            continue
        out[key] = value
    return out


def _redacted_status(status: Status) -> Status:
    # The description is free text — temporalio's interceptor writes
    # `f"{type(exc).__name__}: {exc}"` into it — so it is dropped unless the
    # opt-in is on AND it passes the value check.
    if status.status_code is StatusCode.ERROR:
        return Status(StatusCode.ERROR)
    return Status(status.status_code)


def redacted_copy(span: ReadableSpan, *, error_detail: bool = False) -> ReadableSpan:
    """A copy of `span` carrying only what the guard allows."""
    events = [
        Event(event.name, redact_attributes(event.attributes, error_detail=error_detail), event.timestamp)
        for event in span.events
    ]
    status = span.status
    if not (error_detail and status.description and _scalar_allowed(status.description)):
        status = _redacted_status(status)
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redact_attributes(span.attributes, error_detail=error_detail),
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class _OnceFilter(logging.Filter):
    """Pass the first record from a noisy logger, drop the rest.

    The OTLP exporter and the batch processor log every failed batch; with a
    dead Collector that is one line every two seconds for the life of the
    worker. One line says the same thing."""

    def __init__(self) -> None:
        super().__init__()
        self._seen = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self._seen:
            return False
        self._seen = True
        return True


def _quiet_sdk_loggers() -> None:
    for name in (
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.exporter.otlp.proto.http",
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk._shared_internal",
    ):
        noisy = logging.getLogger(name)
        if not any(isinstance(f, _OnceFilter) for f in noisy.filters):
            noisy.addFilter(_OnceFilter())


class GuardedExporter(SpanExporter):
    """Redact, then export; never raise, never export unredacted."""

    def __init__(self, inner: SpanExporter, *, error_detail: bool = False) -> None:
        self._inner = inner
        self._error_detail = error_detail

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            clean = [redacted_copy(s, error_detail=self._error_detail) for s in spans]
        except Exception as exc:  # noqa: BLE001 — fail closed: a span we cannot redact is not exported
            tracing._warn_once("redact", "dropped a batch that could not be redacted (%s)", type(exc).__name__)
            return SpanExportResult.FAILURE
        try:
            result = self._inner.export(clean)
        except Exception as exc:  # noqa: BLE001 — an exporter failure must never reach the execution
            tracing._warn_once(
                "export", "span export failed (%s); later export failures are not logged", type(exc).__name__
            )
            return SpanExportResult.FAILURE
        if result is not SpanExportResult.SUCCESS:
            tracing._warn_once("export", "span export failed; later export failures are not logged")
        return result

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception as exc:  # noqa: BLE001
            tracing._warn_once("exporter-shutdown", "exporter shutdown failed (%s)", type(exc).__name__)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._inner.force_flush(timeout_millis))
        except Exception as exc:  # noqa: BLE001
            tracing._warn_once("exporter-flush", "exporter flush failed (%s)", type(exc).__name__)
            return False


def span_kind(kind: str) -> SpanKind:
    return {
        "server": SpanKind.SERVER,
        "client": SpanKind.CLIENT,
        "producer": SpanKind.PRODUCER,
        "consumer": SpanKind.CONSUMER,
    }.get(kind, SpanKind.INTERNAL)


def _otlp_exporter(env: Mapping[str, str]) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # The standard OTEL_EXPORTER_OTLP_* variables configure endpoint, headers
    # and compression. Only the timeout gets a tighter default than the SDK's
    # 10 s, because it bounds how long an unreachable Collector can hold the
    # flush at pod exit; an explicit OTEL_* timeout still wins.
    explicit_timeout = (
        env.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "").strip() or env.get("OTEL_EXPORTER_OTLP_TIMEOUT", "").strip()
    )
    if explicit_timeout:
        return OTLPSpanExporter()
    return OTLPSpanExporter(timeout=tracing.EXPORT_TIMEOUT_SECONDS)


def _resource(service_name: str, env: Mapping[str, str]) -> Resource:
    attributes: dict[str, str] = {
        "service.name": env.get("OTEL_SERVICE_NAME", "").strip() or service_name,
    }
    # Kubernetes sets HOSTNAME to the pod name. The Collector's k8sattributes
    # processor adds it too, but only for spans that reach it through the
    # pod's own IP; setting it here makes the pod <-> span join independent of
    # how the traffic is routed.
    pod_name = env.get("HOSTNAME", "").strip()
    if pod_name and env.get("KUBERNETES_SERVICE_HOST", "").strip():
        attributes["k8s.pod.name"] = pod_name
    # Resource.create() merges OTEL_RESOURCE_ATTRIBUTES from the real process
    # environment; that is the standard knob and stays so.
    return Resource.create(attributes)


def build_provider(
    service_name: str,
    *,
    exporter: SpanExporter | None,
    synchronous: bool,
    set_global: bool,
    error_detail: bool,
    environ: Mapping[str, str] | None,
) -> tuple[TracerProvider, Any]:
    env = os.environ if environ is None else environ
    _quiet_sdk_loggers()
    guarded = GuardedExporter(exporter if exporter is not None else _otlp_exporter(env), error_detail=error_detail)
    provider = TracerProvider(
        resource=_resource(service_name, env),
        # Our own bounded exit hook below replaces the SDK's, which would
        # call shutdown() with no bound on the batch thread's join.
        shutdown_on_exit=False,
        # No max_attribute_length: the SDK would TRUNCATE a long value before
        # the guard sees it, and a truncated payload passes a length check.
        span_limits=SpanLimits(max_attributes=64, max_events=128),
    )
    if synchronous:
        provider.add_span_processor(SimpleSpanProcessor(guarded))
    else:
        provider.add_span_processor(
            BatchSpanProcessor(
                guarded,
                max_queue_size=MAX_QUEUE_SIZE,
                max_export_batch_size=MAX_EXPORT_BATCH_SIZE,
                schedule_delay_millis=SCHEDULE_DELAY_MILLIS,
                export_timeout_millis=tracing.EXPORT_TIMEOUT_SECONDS * 1000,
            )
        )
        atexit.register(tracing.shutdown)
    if set_global:
        trace.set_tracer_provider(provider)
    return provider, provider.get_tracer("mctl-agents")


def bounded_shutdown(provider: TracerProvider, timeout_s: float) -> None:
    """Flush then shut down, giving up after `timeout_s` in total.

    Run on a daemon thread and joined with a timeout: the SDK's own
    shutdown joins the batch thread without a bound, and a pod must not sit
    in Terminating because a Collector stopped answering."""

    def _run() -> None:
        try:
            provider.force_flush(int(timeout_s * 1000))
            provider.shutdown()
        except Exception as exc:  # noqa: BLE001
            tracing._warn_once("shutdown", "flush on exit failed (%s)", type(exc).__name__)

    worker = threading.Thread(target=_run, name="mctl-trace-shutdown", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        tracing._warn_once(
            "shutdown-timeout", "flush on exit did not finish in %ss; remaining spans dropped", timeout_s
        )
