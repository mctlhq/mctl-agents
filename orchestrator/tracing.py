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
import subprocess
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
    if isinstance(exc, subprocess.CalledProcessError) and isinstance(exc.returncode, int):
        return f"exit_{exc.returncode}"
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
    if not _state.enabled or value is None or not value.strip():
        # Empty is the CWFT's default for "no parent" — not worth a warning.
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


ARTIFACT_WRITE_EVENT = "mctl.artifact.write"


def record_artifact(name: str, kind: str) -> None:
    """An artifact this execution produced, as an event on the current span.

    `name` must be a bounded file NAME (`requirements.md`), never a path or
    contents; `kind` a bounded class (`proposal`)."""
    if not _state.enabled:
        return
    current().event(ARTIFACT_WRITE_EVENT, {"mctl.artifact.name": name, "mctl.artifact.kind": kind})


# ---------------------------------------------------------------------------
# GitHub / git commands
# ---------------------------------------------------------------------------

_GH_GROUPS = frozenset({"issue", "pr", "api", "repo", "label", "release", "search", "run", "workflow"})
_GH_SUBCOMMANDS = frozenset({
    "view", "list", "status", "comment", "create", "edit", "close", "reopen", "merge", "review", "ready",
    "checks", "diff", "delete", "develop", "lock", "unlock", "transfer", "pin", "unpin", "clone", "fork",
    "issues", "prs", "code", "repos", "commits", "rerun", "cancel", "watch", "download",
})
_GH_MUTATING = frozenset({
    "comment", "create", "edit", "close", "reopen", "merge", "review", "ready", "delete", "develop", "lock",
    "unlock", "transfer", "pin", "unpin", "fork", "rerun", "cancel",
})
_GH_API_BODY_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})
_GIT_REMOTE = frozenset({"push", "clone", "fetch", "pull", "ls-remote"})
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_URL_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?"
    r"(?:/(issues|pull)/([0-9]+))?/?$"
)


def _gh_api_method(args: list[str]) -> str:
    for i, arg in enumerate(args):
        if arg in ("-X", "--method") and i + 1 < len(args):
            return args[i + 1].upper()
        if arg.startswith("--method="):
            return arg.split("=", 1)[1].upper()
    return "POST" if any(a in _GH_API_BODY_FLAGS for a in args) else "GET"


def _git_subcommand(args: list[str]) -> str:
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-C", "-c"):
            skip = True
            continue
        if not arg.startswith("-"):
            return arg
    return ""


def classify_command(cmd: list[str] | tuple[str, ...]) -> tuple[str, dict[str, Any]] | None:
    """(span name, attributes) for a GitHub or git-remote command, or None.

    Reads only a fixed vocabulary out of argv — the program, the subcommand,
    `--repo`, and an owner/repo or issue/PR number parsed from a github.com
    URL argument. NEVER the rest of argv: a `--body`, a commit message, a
    `-f` field or a token-bearing remote URL cannot reach an attribute,
    because nothing here copies an argument it has not matched against a
    closed pattern."""
    if not cmd:
        return None
    exe = os.path.basename(str(cmd[0]))
    args = [str(a) for a in cmd[1:]]
    attributes: dict[str, Any] = {}
    if exe == "gh" and args and args[0] in _GH_GROUPS:
        group = args[0]
        if group == "api":
            method = _gh_api_method(args[1:])
            mutation = method != "GET"
            operation = "api.write" if mutation else "api.read"
        else:
            sub = args[1] if len(args) > 1 and args[1] in _GH_SUBCOMMANDS else "other"
            mutation = sub in _GH_MUTATING
            operation = f"{group}.{sub}"
        name = f"github.{operation}"
    elif exe == "git":
        sub = _git_subcommand(args)
        if sub == "commit":
            operation, mutation = "commit", False
        elif sub in _GIT_REMOTE:
            operation, mutation = sub, sub == "push"
        else:
            return None
        name = f"git.{operation}"
    else:
        return None
    attributes["mctl.github.operation"] = operation
    attributes["mctl.github.mutation"] = mutation
    for i, arg in enumerate(args):
        if arg in ("--repo", "-R") and i + 1 < len(args) and _REPO_RE.match(args[i + 1]):
            attributes.setdefault(REPOSITORY_NAME, args[i + 1])
        match = _GITHUB_URL_RE.match(arg)
        if match:
            attributes.setdefault(REPOSITORY_NAME, match.group(1))
            if match.group(2) == "issues":
                attributes.setdefault(ISSUE_NUMBER, int(match.group(3)))
            elif match.group(2) == "pull":
                attributes.setdefault(PR_NUMBER, int(match.group(3)))
    return name, attributes


class _CommandSpan:
    __slots__ = ("handle",)

    def __init__(self, handle: SpanHandle) -> None:
        self.handle = handle

    def exited(self, returncode: Any) -> None:
        """Record a non-zero exit of a `check=False` command."""
        if isinstance(returncode, int) and returncode:
            self.handle.fail(f"exit_{returncode}")


@contextmanager
def command_span(cmd: list[str] | tuple[str, ...]) -> Iterator[_CommandSpan]:
    """A span around one GitHub/git command, or a no-op for anything else."""
    classified = classify_command(cmd) if _state.enabled else None
    if classified is None:
        yield _CommandSpan(NOOP)
        return
    name, attributes = classified
    with span(name, attributes, kind="client") as handle:
        yield _CommandSpan(handle)


# ---------------------------------------------------------------------------
# Claude Agent SDK message stream -> model and tool spans
# ---------------------------------------------------------------------------

PROVIDER = "anthropic"


def _usage_counts(usage: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(usage, Mapping):
        return out
    pairs = (("input_tokens", "gen_ai.usage.input_tokens"), ("output_tokens", "gen_ai.usage.output_tokens"))
    for source, target in pairs:
        value = usage.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            out[target] = value
    return out


def _bounded(value: Any, limit: int = 64) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


class AgentRunObserver:
    """Turns an SDK message stream into model (`chat`) and tool spans.

    Duck-typed on the SDK's message and block class names, so this module
    never imports `claude_agent_sdk` (the worker must not). Reads only:
    model names, message ids, usage counters, stop/error codes, tool names,
    tool-use ids and the `is_error` flag. Never a content block's text, a
    tool's `input`, or a tool result's `content`.

    Span shape, per agent run:

        invoke_agent <agent>                 usage summed over result frames
          chat <model>                       one per model message (message_id)
          execute_tool <name>                ToolUseBlock -> matching ToolResultBlock
            chat <model>                     a sub-agent's turns nest under its Task/Agent tool
            execute_tool <name>

    A chat span starts when the model's input was complete (the previous
    event in its scope: the query, a tool result, or its own previous
    message) and ends at the last block of its message, so its duration is
    model latency, not tool time. The top-level scope is seeded when the
    observer opens and re-seeded by `query_sent()`, which a driver calls once
    its prompt is sent; a sub-agent's scope is seeded when its Task/Agent tool
    starts. Without a seed the first chat span of a scope would be zero-length.
    """

    def __init__(self, root: SpanHandle, model: str | None, usage: Any = None) -> None:
        self._root = root
        self._model = model
        # The usage producer (orchestrator/usage_ledger.UsageRecorder) or
        # None. It sees every message whether this span records or not:
        # tracing is optional, the usage ledger is not.
        self._usage = usage
        self._tools: dict[str, SpanHandle] = {}
        self._chats: dict[str | None, tuple[str, SpanHandle, int]] = {}
        # Seeded with the observer's own start, so a driver that never calls
        # query_sent() still gets a non-zero first chat span.
        self._last_ns: dict[str | None, int] = {None: time.time_ns()}
        self._input_tokens = 0
        self._output_tokens = 0
        self._saw_usage = False

    # -- public -----------------------------------------------------------

    def query_sent(self) -> None:
        """Mark the prompt as sent: the first top-level chat span starts here,
        not at the client's connect (which can take seconds)."""
        if self._root.recording:
            self._last_ns[None] = time.time_ns()

    def observe(self, message: Any) -> None:
        if self._usage is not None:
            self._usage.observe(message)  # never raises
        if not self._root.recording:
            return
        try:
            kind = type(message).__name__
            if kind == "AssistantMessage":
                self._assistant(message)
            elif kind == "UserMessage":
                self._user(message)
            elif kind == "ResultMessage":
                self._result(message)
        except Exception as exc:  # noqa: BLE001 — observing must never break the stream it observes
            _warn_once("observe", "could not trace an SDK message (%s)", type(exc).__name__)

    def close(self, error: BaseException | None = None) -> None:
        try:
            for scope in list(self._chats):
                self._end_chat(scope)
            for handle in self._tools.values():
                handle.set_attributes({TOOL_STATUS: "incomplete"})
                handle.end()
            self._tools.clear()
            if self._saw_usage:
                self._root.set_attributes(
                    {"gen_ai.usage.input_tokens": self._input_tokens, "gen_ai.usage.output_tokens": self._output_tokens}
                )
            if error is not None:
                self._root.fail(_error_type(error))
        except Exception as exc:  # noqa: BLE001
            _warn_once("observe-close", "could not close agent spans (%s)", type(exc).__name__)

    # -- internals --------------------------------------------------------

    def _parent_for(self, scope: str | None) -> SpanHandle:
        if scope is not None and scope in self._tools:
            return self._tools[scope]
        return self._root

    def _end_chat(self, scope: str | None) -> None:
        open_chat = self._chats.pop(scope, None)
        if open_chat is not None:
            _message_id, handle, last_seen = open_chat
            handle.end(last_seen)

    def _assistant(self, message: Any) -> None:
        now = time.time_ns()
        scope = getattr(message, "parent_tool_use_id", None)
        message_id = getattr(message, "message_id", None) or f"anon-{id(message)}"
        open_chat = self._chats.get(scope)
        if open_chat is not None and open_chat[0] == message_id:
            handle = open_chat[1]
            self._chats[scope] = (message_id, handle, now)
        else:
            self._end_chat(scope)
            model = _bounded(getattr(message, "model", None), 128)
            attributes: dict[str, Any] = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": PROVIDER,
            }
            if self._model:
                attributes["gen_ai.request.model"] = self._model
            if model:
                attributes["gen_ai.response.model"] = model
            handle = start_span(
                f"chat {model or self._model or 'model'}",
                attributes,
                parent=self._parent_for(scope),
                start_time_ns=self._last_ns.get(scope, now),
                kind="client",
            )
            self._chats[scope] = (message_id, handle, now)
        usage = _usage_counts(getattr(message, "usage", None))
        if usage:
            handle.set_attributes(usage)
        error = _bounded(getattr(message, "error", None))
        if error:
            handle.fail(error)
        for block in getattr(message, "content", None) or ():
            if type(block).__name__ in ("ToolUseBlock", "ServerToolUseBlock"):
                self._start_tool(block, scope, now)
        self._last_ns[scope] = now

    def _start_tool(self, block: Any, scope: str | None, now: int) -> None:
        tool_id = getattr(block, "id", None)
        name = _bounded(getattr(block, "name", None), 128) or "unknown"
        if not isinstance(tool_id, str) or tool_id in self._tools:
            return
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            TOOL_NAME: name,
        }
        if name.startswith("mcp__"):
            attributes["mcp.method.name"] = "tools/call"
        self._tools[tool_id] = start_span(
            f"execute_tool {name}", attributes, parent=self._parent_for(scope), start_time_ns=now
        )
        # A sub-agent's first turn (parent_tool_use_id == tool_id) measures
        # from the moment its Task/Agent tool was invoked.
        self._last_ns[tool_id] = now

    def _user(self, message: Any) -> None:
        now = time.time_ns()
        scope = getattr(message, "parent_tool_use_id", None)
        self._end_chat(scope)
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                if type(block).__name__ != "ToolResultBlock":
                    continue
                tool_use_id = getattr(block, "tool_use_id", None)
                handle = self._tools.pop(tool_use_id, None) if isinstance(tool_use_id, str) else None
                if isinstance(tool_use_id, str):
                    self._last_ns.pop(tool_use_id, None)
                if handle is None:
                    continue
                if getattr(block, "is_error", None):
                    handle.set_attributes({TOOL_STATUS: "error"})
                    handle.fail("tool_error")
                else:
                    handle.set_attributes({TOOL_STATUS: "ok"})
                handle.end()
        self._last_ns[scope] = now

    def _result(self, message: Any) -> None:
        for scope in list(self._chats):
            self._end_chat(scope)
        usage = _usage_counts(getattr(message, "usage", None))
        if usage:
            self._saw_usage = True
            self._input_tokens += usage.get("gen_ai.usage.input_tokens", 0)
            self._output_tokens += usage.get("gen_ai.usage.output_tokens", 0)
        if getattr(message, "is_error", False):
            status = getattr(message, "api_error_status", None)
            if isinstance(status, int) and not isinstance(status, bool):
                self._root.fail(f"http_{status}")
            else:
                self._root.fail(_bounded(getattr(message, "subtype", None)) or "agent_error")


class _NoopObserver(AgentRunObserver):
    def __init__(self, usage: Any = None) -> None:
        super().__init__(NOOP, None, usage)

    def close(self, error: BaseException | None = None) -> None:
        return None


class agent_run:
    """The `invoke_agent` span around one SDK client session.

    Usable as `with` or `async with`, so a driver can open it in the same
    statement as its client — `async with tracing.agent_run(...) as obs,
    ClaudeSDKClient(...) as client:` — and feed `obs.observe` every message.
    Tracing off -> a no-op observer and no span.

    Either way the observer also feeds the model-usage producer
    (orchestrator/usage_ledger.py, mctlhq/.github#50): this is the one place
    every driver already hands its whole SDK stream to."""

    def __init__(self, agent: str, model: str | None) -> None:
        self._agent = agent
        self._model = model
        self._span_cm: Any = None
        self._usage: Any = None
        # Guarded like everything else here: usage recording must not fail
        # the run it records (rule 2 above). Deferred: usage_ledger imports
        # httpx, and this module keeps its top level to the standard library.
        try:
            from orchestrator import usage_ledger

            self._usage = usage_ledger.UsageRecorder.from_env(agent)
        except Exception as exc:  # noqa: BLE001
            _warn_once("usage", "usage recording unavailable for this run (%s)", type(exc).__name__)
        self._observer: AgentRunObserver = _NoopObserver(self._usage)

    def __enter__(self) -> AgentRunObserver:
        if not _state.enabled:
            return self._observer
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": PROVIDER,
            "gen_ai.agent.name": self._agent,
            AGENT_NAME: self._agent,
        }
        if self._model:
            attributes["gen_ai.request.model"] = self._model
        self._span_cm = span(f"invoke_agent {self._agent}", attributes, kind="client")
        handle = self._span_cm.__enter__()
        self._observer = AgentRunObserver(handle, self._model, self._usage)
        return self._observer

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        self._observer.close(exc)
        if self._span_cm is not None:
            # span() re-raises the block's exception itself; returning its
            # result (None/False) keeps it propagating exactly once.
            self._span_cm.__exit__(exc_type, exc, tb)
            self._span_cm = None

    async def __aenter__(self) -> AgentRunObserver:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        self.__exit__(exc_type, exc, tb)
