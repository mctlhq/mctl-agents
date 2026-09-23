"""Temporal worker interceptors for execution traces (mctl-agents#195).

The worker registers, in this order (first is outermost):

1. `WorkflowRootInterceptor` — makes the run's deterministic virtual root
   (`orchestrator.tracing.workflow_trace_ids`) the current context before
   anything else runs, so an activity with no propagated trace context
   still lands in its workflow run's one trace.
2. `temporalio.contrib.opentelemetry.TracingInterceptor` — the SDK's own
   interceptor: `RunActivity:<type>` spans, and full header propagation
   whenever a caller DOES carry a span (a traced client start).
3. `CorrelationInterceptor` — stamps the catalog correlation attributes
   (`mctl.workflow.id`, `mctl.workflow.run_id`) onto the span (2) just made.

Why a deterministic root instead of the interceptor's own workflow spans:
the DevLoop is started by a schedule or by the poller, never under a client
span, and `TracingInterceptor` only creates workflow spans — and therefore
only propagates to activities — when such a parent exists
(`always_create_workflow_spans=False`). Turning that flag on instead would
mint random span ids per workflow task, which the SDK itself documents as
orphaning after replay; a loop that lives for days across worker restarts
would fragment into one trace per restart. A hash of the workflow id and run
id cannot fragment: every worker process derives the same trace id for the
same run, with no state to lose and nothing added to workflow code. The cost
is that the root span itself is virtual — no process exports it — which
backends render as a trace whose root is "not yet received"; see
docs/observability/execution-traces.md.

Workflow code is untouched: none of this runs inside the sandbox except the
SDK's own `TracingWorkflowInboundInterceptor`, which only emits spans when
not replaying and never adds a command, so histories replay unchanged
(tests/test_workflow_replay.py replays every recorded history with these
interceptors installed).

Only installed when tracing is on (`worker_interceptors()` returns `[]`
otherwise), so an unconfigured worker neither imports the OpenTelemetry SDK
nor changes a single header.
"""
from __future__ import annotations

from typing import Any

import temporalio.activity
import temporalio.worker

from orchestrator import tracing


class _RootActivityInbound(temporalio.worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:
        token = None
        try:
            info = temporalio.activity.info()
            context = tracing.workflow_root_context(info.workflow_id or "", info.workflow_run_id or "")
            if context is not None:
                from opentelemetry import context as otel_context

                token = otel_context.attach(context)
        except Exception as exc:  # noqa: BLE001 — a root we cannot set is a root we skip
            tracing._warn_once("activity-root", "could not set the workflow root (%s)", type(exc).__name__)
        try:
            return await super().execute_activity(input)
        finally:
            if token is not None:
                try:
                    from opentelemetry import context as otel_context

                    otel_context.detach(token)
                except Exception:  # noqa: BLE001, S110
                    pass


class WorkflowRootInterceptor(temporalio.worker.Interceptor):
    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _RootActivityInbound(next)


class _CorrelationActivityInbound(temporalio.worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:
        try:
            info = temporalio.activity.info()
            tracing.annotate(workflow_id=info.workflow_id, workflow_run_id=info.workflow_run_id)
        except Exception as exc:  # noqa: BLE001
            tracing._warn_once("activity-correlate", "could not annotate the activity span (%s)", type(exc).__name__)
        return await super().execute_activity(input)


class CorrelationInterceptor(temporalio.worker.Interceptor):
    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        return _CorrelationActivityInbound(next)


def worker_interceptors() -> list[temporalio.worker.Interceptor]:
    """The interceptors a worker registers: all three when tracing is on,
    none when it is off. Never raises — a contrib import failure logs once
    and leaves the worker untraced rather than unable to start."""
    tracer = tracing.tracer()
    if tracer is None:
        return []
    try:
        from temporalio.contrib.opentelemetry import TracingInterceptor
    except Exception as exc:  # noqa: BLE001
        tracing._warn_once("contrib", "temporalio.contrib.opentelemetry unavailable (%s)", type(exc).__name__)
        return []
    return [WorkflowRootInterceptor(), TracingInterceptor(tracer), CorrelationInterceptor()]
