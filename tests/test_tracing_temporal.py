"""Execution traces across Temporal -> Argo -> pod (mctl-agents#195).

The propagation chain, end to end, on a real Temporal worker:

    virtual workflow root (hash of workflow id + run id)
      └── RunActivity:submit_and_wait        (temporalio TracingInterceptor)
            └── argo.workflow <operation>    (the activity's own span)
                  └── pod root span          (TRACEPARENT in the pod env)

and the replay half: every recorded DevLoop / reconcile / incidents history
still replays with the tracing interceptors installed, so wiring them into
the worker cannot wedge a running execution.
"""
from __future__ import annotations

import uuid

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.client import WorkflowHistory
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from orchestrator import tracing
from orchestrator.temporal.activities.argo import submit_and_wait
from orchestrator.temporal.tracing import CorrelationInterceptor, WorkflowRootInterceptor, worker_interceptors
from tests.replay_scenarios import SCENARIOS
from tests.tracing_probe_workflow import ArgoProbeWorkflow

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-tracing"
ARGO_NAME = "mctl-agents-investigate-ab12cd34"
ISSUE_URL = "https://github.com/mctlhq/mctl-telegram/issues/617"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in (tracing.ARGO_PARAM_ENV, tracing.TRACEPARENT_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    tracing._reset_for_tests()
    yield
    tracing._reset_for_tests()


@pytest.fixture
def exported() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    assert tracing.init_tracing("test-worker", exporter=exporter, synchronous=True, set_global=False)
    return exporter


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _fake_mctl_api(monkeypatch) -> list[dict]:
    """mctl-api over a MockTransport: accept the submit, report Succeeded."""
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json

            posted.append(json.loads(request.content))
            return httpx.Response(202, json={"workflow": {"workflowName": ARGO_NAME}})
        return httpx.Response(200, json={"live": {"status": {"phase": "Succeeded"}}})

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr("orchestrator.temporal.activities.argo.asyncio.sleep", no_sleep)
    return posted


async def _run_probe(env, params: dict[str, str]) -> tuple[str, str]:
    workflow_id = f"dev-loop-probe-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ArgoProbeWorkflow],
        activities=[submit_and_wait],
        interceptors=worker_interceptors(),
    ):
        handle = await env.client.start_workflow(ArgoProbeWorkflow.run, params, id=workflow_id, task_queue=TASK_QUEUE)
        assert await handle.result() == ARGO_NAME
        return workflow_id, handle.result_run_id or ""


def _by_name(exporter: InMemorySpanExporter, name: str):
    matches = [s for s in exporter.get_finished_spans() if s.name == name]
    assert len(matches) == 1, [s.name for s in exporter.get_finished_spans()]
    return matches[0]


# ---------------------------------------------------------------------------


def test_no_interceptors_when_tracing_is_off():
    assert worker_interceptors() == []


def test_the_worker_registers_root_then_sdk_then_correlation(exported):
    interceptors = worker_interceptors()
    assert [type(i) for i in interceptors] == [WorkflowRootInterceptor, TracingInterceptor, CorrelationInterceptor]


def test_build_worker_passes_the_interceptors_through(exported, monkeypatch):
    """main() hands worker_interceptors() to build_worker; build_worker must
    hand them to the SDK Worker, and pass nothing at all when tracing is off
    (so an untraced worker is constructed exactly as before)."""
    from orchestrator.temporal import worker as worker_module

    captured: list[dict] = []

    class _Recorder:
        def __init__(self, client, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(worker_module, "Worker", _Recorder)
    plan = worker_module.WorkerPlan(task_queue="q", workflows=[], activities=[])
    interceptors = worker_interceptors()

    worker_module.build_worker(object(), plan, interceptors)
    worker_module.build_worker(object(), plan, [])

    assert captured[0]["interceptors"] == interceptors
    assert "interceptors" not in captured[1]


async def test_traceparent_round_trips_temporal_to_argo_params_to_pod_env(env, exported, monkeypatch):
    monkeypatch.setenv(tracing.ARGO_PARAM_ENV, "true")
    posted = _fake_mctl_api(monkeypatch)

    workflow_id, run_id = await _run_probe(
        env, {"issue_url": ISSUE_URL, "execution_id": "we_0123", "work_item_id": "wi_4567"}
    )

    # --- Temporal side --------------------------------------------------
    trace_id, root_span_id = tracing.workflow_trace_ids(workflow_id, run_id)
    run_activity = _by_name(exported, "RunActivity:submit_and_wait")
    argo = _by_name(exported, "argo.workflow mctl-agents-investigate")

    assert run_activity.context.trace_id == trace_id
    assert run_activity.parent.span_id == root_span_id
    assert argo.parent.span_id == run_activity.context.span_id

    # Correlation attributes: workflow id / run id on the activity span, the
    # execution, work item, Argo name and target on the Argo span.
    assert run_activity.attributes["mctl.workflow.id"] == workflow_id
    assert run_activity.attributes["mctl.workflow.run_id"] == run_id
    assert argo.attributes["mctl.workflow.type"] == "investigate"
    assert argo.attributes["mctl.execution.id"] == "we_0123"
    assert argo.attributes["mctl.work_item.id"] == "wi_4567"
    assert argo.attributes["mctl.argo.workflow.name"] == ARGO_NAME
    assert argo.attributes["mctl.repository.name"] == "mctlhq/mctl-telegram"
    assert argo.attributes["mctl.issue.number"] == 617
    assert argo.attributes["mctl.argo.workflow.phase"] == "Succeeded"

    # --- Argo parameter -------------------------------------------------
    (body,) = posted
    traceparent = body[tracing.TRACEPARENT_PARAM]
    assert traceparent == f"00-{trace_id:032x}-{argo.context.span_id:016x}-01"
    assert body["issue_url"] == ISSUE_URL

    # --- Pod side: the CWFT maps the parameter onto TRACEPARENT ---------
    exported.clear()
    pod_env = {tracing.TRACEPARENT_ENV: traceparent, tracing.ARGO_WORKFLOW_NAME_ENV: ARGO_NAME}
    with tracing.pod_root_span("issue-investigator.run", environ=pod_env), tracing.span("model"):
        pass
    model, pod_root = exported.get_finished_spans()

    assert pod_root.context.trace_id == trace_id
    assert pod_root.parent.span_id == argo.context.span_id
    assert pod_root.attributes["mctl.argo.workflow.name"] == ARGO_NAME
    assert model.parent.span_id == pod_root.context.span_id


async def test_no_traceparent_parameter_without_the_rollout_flag(env, exported, monkeypatch):
    posted = _fake_mctl_api(monkeypatch)
    await _run_probe(env, {"issue_url": ISSUE_URL})
    (body,) = posted
    assert body == {"issue_url": ISSUE_URL}


async def test_an_untraced_worker_sends_the_params_unchanged(env, monkeypatch):
    monkeypatch.setenv(tracing.ARGO_PARAM_ENV, "true")
    posted = _fake_mctl_api(monkeypatch)
    await _run_probe(env, {"issue_url": ISSUE_URL})
    (body,) = posted
    assert body == {"issue_url": ISSUE_URL}


_REPLAY_CASES = [(s, kind) for s in SCENARIOS for kind in ("prepatch", "patched")]


@pytest.mark.parametrize(
    ("scenario", "kind"), _REPLAY_CASES, ids=[f"{s.name}-{kind}" for s, kind in _REPLAY_CASES]
)
async def test_recorded_histories_replay_with_tracing_interceptors(scenario, kind, exported):
    """The interceptors change headers and spans, never commands: every
    recorded history must replay unchanged with them installed. Uses the
    same fixtures as tests/test_workflow_replay.py, not re-recorded."""
    interceptors = worker_interceptors()
    assert interceptors, "tracing must be on for this test to mean anything"
    path = scenario.path_for(kind)
    history = WorkflowHistory.from_json(f"trace-replay-{scenario.name}-{kind}", path.read_text(encoding="utf-8"))
    await Replayer(workflows=scenario.workflows, interceptors=interceptors).replay_workflow(history)
