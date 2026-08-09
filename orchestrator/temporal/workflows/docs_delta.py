"""DocsDeltaWorkflow: Temporal workflow for processing documentation deltas
and triggering question authoring updates under clean-room rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.docs_delta import DocsDeltaActivityResult, process_docs_delta_activity

ACTIVITY_TIMEOUT = timedelta(minutes=10)
ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)


@dataclass(frozen=True)
class DocsDeltaWorkflowInput:
    source_id: str
    delta_classification: str = "capability_added"
    url: str = ""
    target_repo: str = "mctlhq/mctl-academy"
    source_sha256: str = "1ced0e48d880014ddcc5b1f91266fb0fe230fef380e414fb267ea8d5c20adc2b"
    excerpt: str = "Function calling allows LLMs to return JSON structured data matching a tool schema."
    objective: str = "domain-2/function-calling"
    course_id: str = "agentic-ai-builder"


@workflow.defn
class DocsDeltaWorkflow:
    @workflow.run
    async def run(self, input_data: DocsDeltaWorkflowInput) -> DocsDeltaActivityResult:
        return await workflow.execute_activity(
            process_docs_delta_activity,
            args=[
                input_data.source_id,
                input_data.delta_classification,
                input_data.url,
                input_data.target_repo,
                input_data.excerpt,
                input_data.objective,
                input_data.source_sha256,
                input_data.course_id,
            ],
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )
