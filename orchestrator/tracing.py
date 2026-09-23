"""Execution traces for agent workflows (mctlhq/mctl-agents#195).

One facade every producer in this repo goes through: the Temporal worker,
the investigator and implementer pods, the policy checkpoint. The design
and the attribute catalogue are in docs/observability/execution-traces.md;
this docstring only states the three rules the code below is built around.

1. **Inert unless configured.** Tracing turns on only when a standard OTLP
   endpoint variable is set (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or
   `OTEL_EXPORTER_OTLP_ENDPOINT`) and nothing disables it
   (`OTEL_SDK_DISABLED=true`, `OTEL_TRACES_EXPORTER=none`). Until then every
   helper here returns a no-op after one boolean check, and the OpenTelemetry
   SDK is never even imported — this module is stdlib-only at import time,
   like `orchestrator/policy_checkpoint.py` and
   `orchestrator/execution_identity.py`, so it is safe in the long-lived
   Temporal worker and in the agent sandbox alike.

2. **A trace can never fail an execution.** Every helper swallows its own
   errors (logged once per kind, never per call). Export runs on a bounded
   background queue with a bounded per-request timeout; a full queue drops
   spans rather than blocking; process exit flushes for at most
   `SHUTDOWN_TIMEOUT_SECONDS`. The body of a `span()` block is never
   affected: its exceptions propagate unchanged, and a failure to START a
   span yields a no-op handle rather than an error.

3. **Nothing sensitive leaves the process.** Prompts, completions, tool
   arguments and results, issue bodies, command lines, tokens and secrets are
   never set on a span by this code, and an export-side guard
   (`orchestrator/tracing_sdk.py`) enforces it for every span the process
   emits — including the ones `temporalio.contrib.opentelemetry` creates,
   whose error status carries the raw exception text. The guard is an
   allowlist of attribute keys plus a denylist and a credential-shape value
   check on top; see `tracing_sdk.redact_attributes`. The one opt-in,
   `MCTL_TRACE_ERROR_DETAIL=true`, keeps (still credential-filtered, still
   length-capped) exception messages; it is off by default and there is no
   opt-in for prompts or tool payloads at all.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# W3C trace context, as carried across the Temporal -> Argo -> pod boundary.
# The Argo workflow parameter and the pod env var are the SAME value; the
# CWFT maps one onto the other (docs/observability/execution-traces.md,
# "gitops follow-up").
TRACEPARENT_ENV = "TRACEPARENT"
TRACEPARENT_PARAM = "traceparent"
# Off by default: mctl-api drops undeclared operation parameters with a
# warning log (internal/api/handlers_write.go, StripUndeclared) and intends to
# tighten that to a rejection, so sending `traceparent` before the operation
# and the CWFT declare it would be noise at best and a failed submit at
# worst. Fail-closed ordering: gitops CWFT, then mctl-api registry, then this.
ARGO_PARAM_ENV = "MCTL_TRACE_ARGO_PARAM"
# The only opt-in for richer attributes. See rule 3 above.
ERROR_DETAIL_ENV = "MCTL_TRACE_ERROR_DETAIL"
# Set by the CWFT from `{{workflow.name}}` (gitops follow-up). Read, never
# required: absent, the pod's spans simply do not carry the attribute.
ARGO_WORKFLOW_NAME_ENV = "ARGO_WORKFLOW_NAME"

EXPORT_TIMEOUT_SECONDS = 5.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_HTTP_PROTOBUF = "http/protobuf"

# Catalog names (mctl-docs docs/reference/telemetry-attributes.md). Kept as
# constants so a typo is a NameError, not a silently different key.
EXECUTION_ID = "mctl.execution.id"
WORKFLOW_ID = "mctl.workflow.id"
WORKFLOW_RUN_ID = "mctl.workflow.run_id"
WORKFLOW_TYPE = "mctl.workflow.type"
ARGO_WORKFLOW_NAME = "mctl.argo.workflow.name"
WORK_ITEM_ID = "mctl.work_item.id"
AGENT_NAME = "mctl.agent.name"
REPOSITORY_NAME = "mctl.repository.name"
ISSUE_NUMBER = "mctl.issue.number"
PR_NUMBER = "mctl.pr.number"
TOOL_NAME = "mctl.tool.name"
TOOL_STATUS = "mctl.tool.status"
ERROR_TYPE = "error.type"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(name, "").strip().lower() in _TRUTHY


class _State:
    enabled: bool = False
    tracer: Any = None
    provider: Any = None
    error_detail: bool = False


_state = _State()
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: object) -> None:
    """Log a tracing problem the first time it happens, and never again.

    Per key rather than globally, so a broken exporter does not also hide a
    broken propagation — but a failure that repeats on every span is one
    log line for the life of the process, not one per span."""
    if key in _warned:
        return
    _warned.add(key)
    try:
        logger.warning("tracing: " + message, *args)
    except Exception:  # noqa: BLE001, S110 — logging must not be the thing that fails
        pass


def enabled() -> bool:
    return _state.enabled


def endpoint_configured(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the standard `OTEL_*` variables ask for OTLP trace export.

    Only the standard variables, deliberately: replacing the backend must be
    a Collector change, never a producer change (observability epic success
    criteria), so there is no mctl-specific endpoint knob to drift."""
    env = os.environ if environ is None else environ
    if env.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return False
    exporter = env.get("OTEL_TRACES_EXPORTER", "").strip().lower()
    if exporter and exporter != "otlp":
        return False
    protocol = (
        env.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "").strip()
        or env.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip()
    )
    if protocol and protocol != _HTTP_PROTOBUF:
        _warn_once(
            "protocol",
            "OTLP protocol %r is not supported here (only %s is shipped); tracing stays off",
            protocol,
            _HTTP_PROTOBUF,
        )
        return False
    return bool(
        env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
        or env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )


def init_tracing(
    service_name: str,
    *,
    exporter: Any = None,
    synchronous: bool = False,
    set_global: bool = True,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Install the tracer for this process. Returns whether tracing is on.

    Idempotent, and never raises. `exporter`, `synchronous` and
    `set_global` exist for tests (an in-memory exporter, a synchronous
    processor, no process-global provider); production calls pass only the
    service name, and then nothing happens unless `endpoint_configured()`.
    """
    if _state.enabled:
        return True
    if exporter is None and not endpoint_configured(environ):
        return False
    try:
        from orchestrator import tracing_sdk

        error_detail = _flag(ERROR_DETAIL_ENV, environ)
        provider, tracer = tracing_sdk.build_provider(
            service_name,
            exporter=exporter,
            synchronous=synchronous,
            set_global=set_global,
            error_detail=error_detail,
            environ=environ,
        )
    except Exception as exc:  # noqa: BLE001 — a tracer that cannot start is a tracer that is off
        _warn_once("init", "could not start (%s: %s); continuing without traces", type(exc).__name__, exc)
        return False
    _state.provider = provider
    _state.tracer = tracer
    _state.error_detail = error_detail
    _state.enabled = True
    return True


def shutdown(timeout_s: float = SHUTDOWN_TIMEOUT_SECONDS) -> None:
    """Flush and stop, bounded. Registered at exit by `build_provider`."""
    provider = _state.provider
    _state.enabled = False
    if provider is None:
        return
    try:
        from orchestrator import tracing_sdk

        tracing_sdk.bounded_shutdown(provider, timeout_s)
    except Exception as exc:  # noqa: BLE001
        _warn_once("shutdown", "flush on exit failed (%s)", type(exc).__name__)


def _reset_for_tests() -> None:
    """Forget the installed tracer. Tests only — production never resets."""
    provider = _state.provider
    _state.enabled = False
    _state.tracer = None
    _state.provider = None
    _state.error_detail = False
    _warned.clear()
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001, S110
            pass


def tracer() -> Any:
    """The installed `opentelemetry.trace.Tracer`, or None when off."""
    return _state.tracer if _state.enabled else None


# ---------------------------------------------------------------------------
# Spans
# ---------------------------------------------------------------------------


class SpanHandle:
    """What a `span()` block gets: set attributes, add events, mark failure.

    Every method swallows its own errors. Attributes and events go through
    the same redaction as export (so a caller's mistake is dropped at the
    source too, not only at the exporter)."""

    __slots__ = ("_span",)

    def __init__(self, otel_span: Any) -> None:
        self._span = otel_span

    @property
    def recording(self) -> bool:
        return self._span is not None

    @property
    def otel_span(self) -> Any:
        return self._span

    def set(self, **attributes: Any) -> None:
        self.set_attributes(_kwargs_to_attributes(attributes))

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        if self._span is None or not attributes:
            return
        try:
            from orchestrator import tracing_sdk

            clean = tracing_sdk.redact_attributes(attributes, error_detail=_state.error_detail)
            if clean:
                self._span.set_attributes(clean)
        except Exception as exc:  # noqa: BLE001
            _warn_once("set", "could not set span attributes (%s)", type(exc).__name__)

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        if self._span is None:
            return
        try:
            from orchestrator import tracing_sdk

            clean = tracing_sdk.redact_attributes(attributes or {}, error_detail=_state.error_detail)
            self._span.add_event(name, clean)
        except Exception as exc:  # noqa: BLE001
            _warn_once("event", "could not add span event (%s)", type(exc).__name__)

    def fail(self, error: BaseException | str) -> None:
        """Mark the span failed with a bounded `error.type`, never a message."""
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            error_type = error if isinstance(error, str) else type(error).__name__
            self._span.set_attribute(ERROR_TYPE, error_type)
            self._span.set_status(Status(StatusCode.ERROR))
        except Exception as exc:  # noqa: BLE001
            _warn_once("fail", "could not mark span failed (%s)", type(exc).__name__)

    def end(self, end_time_ns: int | None = None) -> None:
        if self._span is None:
            return
        try:
            self._span.end(end_time=end_time_ns)
        except Exception as exc:  # noqa: BLE001
            _warn_once("end", "could not end span (%s)", type(exc).__name__)


NOOP = SpanHandle(None)


def _kwargs_to_attributes(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """`execution_id=...` style keywords to catalog keys, for the handful of
    correlation attributes callers set by name. Anything else must be passed
    with its full dotted key via `set_attributes`."""
    mapping = {
        "execution_id": EXECUTION_ID,
        "workflow_id": WORKFLOW_ID,
        "workflow_run_id": WORKFLOW_RUN_ID,
        "workflow_type": WORKFLOW_TYPE,
        "argo_workflow_name": ARGO_WORKFLOW_NAME,
        "work_item_id": WORK_ITEM_ID,
        "agent": AGENT_NAME,
        "repository": REPOSITORY_NAME,
        "issue_number": ISSUE_NUMBER,
        "pr_number": PR_NUMBER,
    }
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if value is None or value == "":
            # Omitted, never written empty (catalog: "an attribute that was
            # not captured is omitted rather than written empty").
            continue
        target = mapping.get(key)
        if target is None:
            _warn_once(f"kw:{key}", "unknown correlation keyword %r ignored", key)
            continue
        out[target] = value
    return out


def start_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    parent: SpanHandle | Any = None,
    start_time_ns: int | None = None,
    kind: str = "internal",
) -> SpanHandle:
    """Start a span WITHOUT making it current; the caller must `end()` it.

    For spans whose lifetime does not nest with the call stack — a tool call
    opened by one SDK message and closed by another. `parent` is a
    `SpanHandle`, an OpenTelemetry `Context`, or None (the current context).
    """
    if not _state.enabled:
        return NOOP
    try:
        from opentelemetry import trace

        from orchestrator import tracing_sdk

        context = None
        if isinstance(parent, SpanHandle):
            if parent.otel_span is not None:
                context = trace.set_span_in_context(parent.otel_span)
        elif parent is not None:
            context = parent
        otel_span = _state.tracer.start_span(
            name,
            context=context,
            attributes=tracing_sdk.redact_attributes(attributes or {}, error_detail=_state.error_detail),
            start_time=start_time_ns,
            kind=tracing_sdk.span_kind(kind),
        )
        return SpanHandle(otel_span)
    except Exception as exc:  # noqa: BLE001
        _warn_once("start", "could not start span (%s: %s)", type(exc).__name__, exc)
        return NOOP


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    parent: SpanHandle | Any = None,
    kind: str = "internal",
) -> Iterator[SpanHandle]:
    """A span around a block, made current for the block's duration.

    The block's own exceptions propagate unchanged; the span records only
    their type (`error.type`). A span that cannot be started or ended never
    turns into an exception here.
    """
    handle = start_span(name, attributes, parent=parent, kind=kind)
    token = None
    if handle.recording:
        try:
            from opentelemetry import context as otel_context
            from opentelemetry import trace

            token = otel_context.attach(trace.set_span_in_context(handle.otel_span))
        except Exception as exc:  # noqa: BLE001
            _warn_once("attach", "could not make span current (%s)", type(exc).__name__)
    try:
        yield handle
    except BaseException as exc:
        if not _is_clean_exit(exc):
            handle.fail(_error_type(exc))
        raise
    finally:
        if token is not None:
            try:
                from opentelemetry import context as otel_context

                otel_context.detach(token)
            except Exception:  # noqa: BLE001, S110 — a detach mismatch must not mask the block's own outcome
                pass
        handle.end()


def _is_clean_exit(exc: BaseException) -> bool:
    return isinstance(exc, SystemExit) and exc.code in (0, None)


def _error_type(exc: BaseException) -> str:
    """A bounded `error.type`: the class name, or `exit_<n>` for a process
    exit code (the drivers' exit codes are their outcome vocabulary)."""
    if isinstance(exc, SystemExit) and isinstance(exc.code, int):
        return f"exit_{exc.code}"
    return type(exc).__name__


def current() -> SpanHandle:
    """The current span, as a handle (no-op when off or when there is none)."""
    if not _state.enabled:
        return NOOP
    try:
        from opentelemetry import trace

        otel_span = trace.get_current_span()
        if not otel_span.is_recording():
            return NOOP
        return SpanHandle(otel_span)
    except Exception as exc:  # noqa: BLE001
        _warn_once("current", "could not read the current span (%s)", type(exc).__name__)
        return NOOP


def annotate(**correlation: Any) -> None:
    """Set correlation attributes (see `_kwargs_to_attributes`) on the
    current span. The call sites that learn an id late — the investigator
    resolving its `we_` execution, the Argo activity learning its workflow
    name — use this rather than threading a handle through."""
    current().set(**correlation)


# ---------------------------------------------------------------------------
# Propagation: Temporal -> Argo parameter -> pod env -> pod spans
# ---------------------------------------------------------------------------


def valid_traceparent(value: str | None) -> bool:
    """A W3C version-00 traceparent with non-zero trace and span ids."""
    if not value:
        return False
    match = _TRACEPARENT_RE.match(value.strip())
    if not match:
        return False
    trace_id, span_id, _flags = match.groups()
    return trace_id != "0" * 32 and span_id != "0" * 16


def current_traceparent() -> str | None:
    """The W3C traceparent of the current span, or None when there is none
    (tracing off, or no recording span in context)."""
    if not _state.enabled:
        return None
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        value = carrier.get("traceparent")
        return value if valid_traceparent(value) else None
    except Exception as exc:  # noqa: BLE001
        _warn_once("inject", "could not serialise the trace context (%s)", type(exc).__name__)
        return None


def context_from_traceparent(value: str | None) -> Any:
    """An OpenTelemetry Context whose remote parent is `value`, or None when
    tracing is off or the value is not a valid traceparent. Invalid input is
    ignored (logged once), never raised: a malformed env var must not stop a
    pod."""
    if not _state.enabled or value is None:
        return None
    if not valid_traceparent(value):
        _warn_once("traceparent", "ignoring malformed %s", TRACEPARENT_ENV)
        return None
    try:
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

        return TraceContextTextMapPropagator().extract({"traceparent": value.strip()})
    except Exception as exc:  # noqa: BLE001
        _warn_once("extract", "could not parse the trace context (%s)", type(exc).__name__)
        return None


def argo_param_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return _flag(ARGO_PARAM_ENV, environ)


def with_traceparent(params: Mapping[str, str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """`params` plus the current `traceparent`, when tracing is on AND the
    rollout flag says the operation declares it. Never overwrites a caller's
    own value, and returns an unchanged copy otherwise."""
    out = dict(params)
    if TRACEPARENT_PARAM in out or not argo_param_enabled(environ):
        return out
    value = current_traceparent()
    if value:
        out[TRACEPARENT_PARAM] = value
    return out


def _derived_id(material: str, bits: int) -> int:
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    value = int.from_bytes(digest[: bits // 8], "big")
    return value or 1  # all-zero ids are invalid in W3C trace context


def workflow_trace_ids(workflow_id: str, run_id: str) -> tuple[int, int]:
    """The (trace_id, root span_id) of one Temporal workflow run.

    Deterministic, so every activity of a run lands in ONE trace no matter
    which worker process executes it, how often the worker restarts, or
    whether the workflow is being replayed: nothing about it is state that
    a crash can lose. The root span id names a virtual root (no process
    exports a span with that id); see the docs page for why.
    """
    trace_id = _derived_id(f"mctl-agents/temporal/{workflow_id}/{run_id}/trace", 128)
    span_id = _derived_id(f"mctl-agents/temporal/{workflow_id}/{run_id}/root", 64)
    return trace_id, span_id


def workflow_root_context(workflow_id: str, run_id: str) -> Any:
    """A Context whose remote parent is the run's virtual root, or None."""
    if not _state.enabled or not workflow_id or not run_id:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        trace_id, span_id = workflow_trace_ids(workflow_id, run_id)
        parent = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return trace.set_span_in_context(NonRecordingSpan(parent))
    except Exception as exc:  # noqa: BLE001
        _warn_once("root", "could not derive the workflow root context (%s)", type(exc).__name__)
        return None


@contextmanager
def pod_root_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Iterator[SpanHandle]:
    """The root span of an agent pod, parented on `TRACEPARENT` if present.

    Also stamps the Argo workflow name (from `ARGO_WORKFLOW_NAME`) so the pod
    side of a run joins the Temporal side on `mctl.argo.workflow.name` even
    when no traceparent was propagated. The pod name is a resource attribute
    (`k8s.pod.name`, from `HOSTNAME`), set once in `build_provider`.
    """
    env = os.environ if environ is None else environ
    parent = context_from_traceparent(env.get(TRACEPARENT_ENV))
    attrs = dict(attributes or {})
    argo_name = env.get(ARGO_WORKFLOW_NAME_ENV, "").strip()
    if argo_name:
        attrs.setdefault(ARGO_WORKFLOW_NAME, argo_name)
    with span(name, attrs, parent=parent, kind="server") as handle:
        yield handle


def now_ns() -> int:
    return time.time_ns()


# ---------------------------------------------------------------------------
# Domain events
# ---------------------------------------------------------------------------


POLICY_DECISION_EVENT = "mctl.policy.decision"


def record_policy_decision(
    *,
    rule_id: str,
    decision: str,
    code: str,
    policy_version: str,
    action_kind: str,
    operation: str,
) -> None:
    """A policy checkpoint decision, as an event on the current span.

    Only the bounded fields: the rule, the verdict, the reason CODE (never
    the free-form reason text, which can quote an exception), the policy
    version and the action's kind and operation. Never the target (it can
    be a URL with a path) and never the arguments (only ever a digest in
    the audit record, and not even that here)."""
    if not _state.enabled:
        return
    current().event(
        POLICY_DECISION_EVENT,
        {
            "mctl.policy.rule_id": rule_id or "",
            "mctl.policy.decision": decision,
            "mctl.policy.code": code,
            "mctl.policy.version": policy_version,
            "mctl.policy.action_kind": action_kind,
            "mctl.policy.operation": operation,
        },
    )
