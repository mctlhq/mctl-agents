"""A caller workflow for tests/test_action_approval_wait.py (#198): runs one
gated activity through `run_gated_action`, and re-requests deliberately, up
to `max_attempts`, only after an outcome `next_attempt` accepts.

Its own module, like tests/tracing_probe_workflow.py: the sandbox re-imports
the module a workflow is defined in.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.action_approval import GatedActionInput
    from orchestrator.temporal.workflows.action_approval import (
        REREQUESTABLE,
        ApprovalWaitResult,
        next_attempt,
        run_gated_action,
    )


@dataclass(frozen=True)
class ProbeInput:
    activity: str
    action: GatedActionInput
    max_attempts: int = 1
    poll_seconds: int = 60


@dataclass(frozen=True)
class ProbeResult:
    results: list[ApprovalWaitResult] = field(default_factory=list)


@workflow.defn
class ApprovalProbeWorkflow:
    @workflow.run
    async def run(self, inp: ProbeInput) -> ProbeResult:
        results: list[ApprovalWaitResult] = []
        action = inp.action
        for _ in range(inp.max_attempts):
            result = await run_gated_action(inp.activity, action, poll_seconds=inp.poll_seconds)
            results.append(result)
            if result.outcome not in REREQUESTABLE:
                break
            action = next_attempt(result, action)
        return ProbeResult(results)
