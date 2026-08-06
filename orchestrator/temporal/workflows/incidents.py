"""IncidentLoopWorkflow: durable incident responder loop on Temporal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.incidents import IncidentResponderActivityResult, respond_incidents_activity

ACTIVITY_TIMEOUT = timedelta(minutes=15)
ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass(frozen=True)
class IncidentLoopWorkflowResult:
    responder_result: IncidentResponderActivityResult


@workflow.defn
class IncidentLoopWorkflow:
    @workflow.run
    async def run(self) -> IncidentLoopWorkflowResult:
        responder_result: IncidentResponderActivityResult = await workflow.execute_activity(
            respond_incidents_activity,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )

        return IncidentLoopWorkflowResult(responder_result=responder_result)
