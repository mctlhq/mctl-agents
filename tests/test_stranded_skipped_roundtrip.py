"""Regression for mctl-agents#412 code review finding 1.

`StrandedScanResult.skipped` (stranded.py) crosses the `find_stranded_accepted`
activity -> workflow boundary typed `list[tuple[str, str]]`. Every existing
test exercises it as `[]` (test_stranded_activity.py calls the activity
in-process via `ActivityEnvironment`, which never serializes anything;
test_implement_sweep_workflow.py's fake always returns `skipped=[]`) — so
nothing has ever put a real entry through Temporal's actual JSON payload
conversion to check whether it survives as a genuine `tuple` or quietly
degrades to a two-element `list` once the workflow decodes it.

A tiny purpose-built child workflow, calling the real (typed)
`find_stranded_accepted` reference so the SDK's decode uses its true
`StrandedScanResult` return-type annotation — with a fake activity standing
in for the body — is the smallest harness that exercises exactly that
conversion with a non-empty value.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.stranded import StrandedScanResult, find_stranded_accepted

from tests.temporal_harness import Worker

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-stranded-skipped-roundtrip"

NON_EMPTY_SKIPPED: list[tuple[str, str]] = [
    ("mctl-api/issue-2-widget", "carries a pr: url; left to detect_orphans and the shepherd"),
]


@activity.defn(name="find_stranded_accepted")
async def _fake_find_stranded_accepted(
    active_workflow_ids: list[str], grace_minutes: int
) -> StrandedScanResult:
    return StrandedScanResult(total_accepted=1, stranded=[], skipped=list(NON_EMPTY_SKIPPED))


@workflow.defn
class _SkippedRoundTripProbe:
    """Not part of the production graph: exists only to observe what a real
    Temporal activity boundary does to a non-empty `skipped` entry."""

    @workflow.run
    async def run(self) -> list:
        result: StrandedScanResult = await workflow.execute_activity(
            find_stranded_accepted,
            args=[[], 20],
            start_to_close_timeout=timedelta(seconds=10),
        )
        entry = result.skipped[0]
        return [isinstance(entry, tuple), entry[0], entry[1]]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


async def test_a_non_empty_skipped_entry_survives_the_activity_boundary_as_a_tuple(env):
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[_SkippedRoundTripProbe],
        activities=[_fake_find_stranded_accepted],
    ):
        outcome = await env.client.execute_workflow(
            _SkippedRoundTripProbe.run,
            id=f"skipped-roundtrip-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )

    assert outcome == [True, *NON_EMPTY_SKIPPED[0]]
