"""A one-activity workflow that runs the REAL `submit_and_wait`, for
tests/test_tracing_temporal.py (mctl-agents#195).

Its own module, with nothing but the workflow's imports, because the
sandbox re-imports the module a workflow is defined in: defining it inside
the test module drags pytest, httpx's mock transport and the OpenTelemetry
test exporter through the sandbox validator.
"""
from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, submit_and_wait


@workflow.defn
class ArgoProbeWorkflow:
    @workflow.run
    async def run(self, params: dict[str, str]) -> str:
        result = await workflow.execute_activity(
            submit_and_wait,
            SubmitAndWaitInput(operation="mctl-agents-investigate", params=params),
            start_to_close_timeout=timedelta(minutes=5),
        )
        return result.workflow_name
