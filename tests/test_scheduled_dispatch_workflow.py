"""ScheduledDispatchWorkflow against the time-skipping environment (#559)."""
from __future__ import annotations

import uuid

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.workflow_dispatch import DispatchInput, DispatchResult, FailureReport
from orchestrator.temporal.workflows.scheduled_dispatch import ScheduledDispatchInput, ScheduledDispatchWorkflow
from tests.temporal_harness import Worker

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-scheduled-dispatch"
INPUT = ScheduledDispatchInput(repo="mctlhq/portfolio", workflow_file="weekly-refresh.yml", ref="main")
OK = DispatchResult(dispatched=True, run_id=1, html_url="u")


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _acts(*, dispatch_error: tuple[str, bool] | None = None, report_error: tuple[str, bool] | None = None):
    seen: dict = {"dispatch": [], "report": []}

    @activity.defn(name="dispatch_and_observe")
    async def dispatch(inp: DispatchInput) -> DispatchResult:
        seen["dispatch"].append(inp)
        if dispatch_error:
            raise ApplicationError("dispatch broke", type=dispatch_error[0], non_retryable=dispatch_error[1])
        return OK

    @activity.defn(name="report_dispatch_failure")
    async def report(rep: FailureReport):
        seen["report"].append(rep)
        if report_error:
            raise ApplicationError("report broke", type=report_error[0], non_retryable=report_error[1])
        return None

    return seen, [dispatch, report]


async def _run(env, activities):
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=[ScheduledDispatchWorkflow], activities=activities
    ):
        return await env.client.execute_workflow(
            ScheduledDispatchWorkflow.run, INPUT, id=f"wf-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )


def _original(exc: WorkflowFailureError) -> ApplicationError:
    cause = exc.cause.cause
    assert isinstance(cause, ApplicationError)
    return cause


async def test_success_returns_activity_result_and_never_reports(env):
    seen, acts = _acts()
    assert await _run(env, acts) == OK
    assert seen["report"] == []


async def test_run_not_observed_fails_once_and_reports_once(env):
    seen, acts = _acts(dispatch_error=("RunNotObserved", True))
    with pytest.raises(WorkflowFailureError) as ei:
        await _run(env, acts)
    assert len(seen["dispatch"]) == 1
    assert len(seen["report"]) == 1
    assert seen["report"][0].error_type == "RunNotObserved"
    assert _original(ei.value).type == "RunNotObserved"


async def test_retryable_error_stops_at_three_attempts_with_same_not_before(env):
    seen, acts = _acts(dispatch_error=("DispatchFailed", False))
    with pytest.raises(WorkflowFailureError) as ei:
        await _run(env, acts)
    assert len(seen["dispatch"]) == 3
    assert len({i.not_before for i in seen["dispatch"]}) == 1
    assert len(seen["report"]) == 1
    assert _original(ei.value).type == "DispatchFailed"


@pytest.mark.parametrize(
    "report_error,attempts",
    [(("AlertReportRejected", False), 1), (("NoGitHubToken", False), 3), (("AlertReportFailed", False), 3)],
)
async def test_report_retry_policy_and_original_error_survives(env, report_error, attempts):
    seen, acts = _acts(dispatch_error=("RunNotObserved", True), report_error=report_error)
    with pytest.raises(WorkflowFailureError) as ei:
        await _run(env, acts)
    assert len(seen["report"]) == attempts
    assert _original(ei.value).type == "RunNotObserved"
