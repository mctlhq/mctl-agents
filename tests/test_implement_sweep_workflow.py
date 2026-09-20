"""ImplementSweepWorkflow / SweptImplementWorkflow orchestration tests (#412).

Same harness as test_reconcile_workflow.py: the real workflow definitions
against temporalio's time-skipping test environment, with fake activities.
"""
from __future__ import annotations

import uuid

import anyio
import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult
from orchestrator.temporal.activities.state import ExecutionRecord
from orchestrator.temporal.activities.stranded import StrandedProposal, StrandedScanResult
from orchestrator.temporal.constants import IMPLEMENTATION_TASK_QUEUE
from orchestrator.temporal.workflows.implement_sweep import (
    ImplementSweepWorkflow,
    ImplementSweepWorkflowInput,
    SweptImplementInput,
    SweptImplementWorkflow,
)
from tests.temporal_harness import Worker  # polls the admission queue too — see #395

pytestmark = pytest.mark.anyio

TASK_QUEUE = "test-mctl-implement-sweep"


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def _candidate(service="mctl-web", slug="issue-10-test") -> StrandedProposal:
    return StrandedProposal(
        service=service,
        slug=slug,
        updated_at="2026-09-19T18:00:00Z",
        reason="accepted, no PR, no live DevLoopWorkflow",
    )


def _fake_activities(
    *,
    visibility_fails: bool = False,
    scan_fails: bool = False,
    budget_query_fails: bool = False,
    unauthorized: list[tuple[str, str]] | None = None,
    prior_failures_by_id: dict[str, int] | None = None,
    active_ids: list[str] | None = None,
    stranded: list[StrandedProposal] | None = None,
    submit_gate: anyio.Event | None = None,
    submit_result: WorkflowResult | None = None,
    prior_failures: int = 0,
):
    received: dict = {"submits": [], "record_execution": []}

    @activity.defn(name="list_active_dev_loop_ids")
    async def fake_list_active_dev_loop_ids() -> list[str]:
        if visibility_fails:
            raise ApplicationError("visibility unavailable", non_retryable=True)
        return active_ids or []

    @activity.defn(name="count_swept_prestart_failures")
    async def fake_count_swept_prestart_failures(workflow_ids: list[str]) -> dict[str, int]:
        received.setdefault("budget_queries", []).append(list(workflow_ids))
        if budget_query_fails:
            raise ApplicationError("visibility unavailable", non_retryable=True)
        if prior_failures_by_id is not None:
            # `None` means the activity refused to vouch for this id and
            # OMITTED it, which is not the same as reporting zero.
            return {
                w: prior_failures_by_id.get(w, 0)
                for w in workflow_ids
                if prior_failures_by_id.get(w, 0) is not None
            }
        return {w: prior_failures for w in workflow_ids}

    @activity.defn(name="find_stranded_accepted")
    async def fake_find_stranded_accepted(
        active_workflow_ids: list[str], grace_minutes: int
    ) -> StrandedScanResult:
        received["active_workflow_ids"] = active_workflow_ids
        received["grace_minutes"] = grace_minutes
        if scan_fails:
            raise ApplicationError("gitops listing failed", non_retryable=True)
        candidates = stranded if stranded is not None else [_candidate()]
        return StrandedScanResult(
            total_accepted=len(candidates),
            stranded=candidates,
            skipped=list(unauthorized or []),
            unauthorized=list(unauthorized or []),
        )

    @activity.defn(name="submit_and_wait")
    async def fake_submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
        if submit_gate is not None:
            await submit_gate.wait()
        received["submits"].append(input)
        if submit_result is not None:
            return submit_result
        return WorkflowResult(workflow_name=f"{input.operation}-fake", phase="Succeeded")

    @activity.defn(name="record_execution")
    async def fake_record_execution(record: ExecutionRecord) -> None:
        received["record_execution"].append(record)

    return [
        fake_list_active_dev_loop_ids,
        fake_count_swept_prestart_failures,
        fake_find_stranded_accepted,
        fake_submit_and_wait,
        fake_record_execution,
    ], received


async def _run(
    env,
    activities,
    cfg: ImplementSweepWorkflowInput | None = None,
    *,
    await_children: list[str] = (),
):
    """Run one tick and return its result.

    `await_children` names the child workflow ids (service-slug pairs the
    tick is expected to have started) to wait on BEFORE the Worker context
    exits: ImplementSweepWorkflow returns as soon as it starts its
    ABANDONed children, without waiting for them, so exiting the Worker
    right after would drain it while a just-started child has not even been
    dispatched a workflow task yet — a real, observed flake, not a
    hypothetical one.
    """
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
        activities=activities,
    ):
        result = await env.client.execute_workflow(
            ImplementSweepWorkflow.run,
            cfg,
            id=f"implement-sweep-test-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        for child_id in await_children:
            await env.client.get_workflow_handle(child_id).result()
        return result


class TestVisibilityFailure:
    async def test_a_failed_visibility_query_starts_zero_children(self, env):
        activities, received = _fake_activities(visibility_fails=True)

        result = await _run(env, activities)

        assert "active_workflow_ids" not in received, "find_stranded_accepted must not run"
        assert received["submits"] == []
        assert result.candidates == 0
        assert result.submitted == 0
        assert result.skipped == 0
        assert result.skipped_reason is not None
        assert "visibility" in result.skipped_reason


class TestSubmitScoping:
    async def test_the_implement_submit_is_scoped_to_service_and_slug(self, env):
        """An unscoped submit would let one sweep implement a DIFFERENT
        proposal — the hazard dev_loop.py documents for #203."""
        activities, received = _fake_activities(
            stranded=[_candidate(service="mctl-api", slug="issue-20-widget")]
        )

        result = await _run(
            env, activities, await_children=["implement-sweep-mctl-api-issue-20-widget"]
        )

        assert result.submitted == 1
        assert len(received["submits"]) == 1
        submitted = received["submits"][0]
        assert submitted.operation == "mctl-agents-implement"
        assert submitted.params == {"service": "mctl-api", "slug": "issue-20-widget"}

    async def test_the_submit_reaches_the_admission_queue(self, env):
        """Asserted from recorded history, since a fake activity has no
        notion of which queue it was scheduled on — mirrors
        test_workflow_replay.py's own routing check.

        `record_execution` deliberately stays off this assertion's queue set:
        exactly like dev_loop._record, it carries no `task_queue` override,
        so it runs on the child's own (default/control) queue while only
        `submit_and_wait` is explicitly routed to admission."""
        activities, _ = _fake_activities()

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                ImplementSweepWorkflow.run,
                None,
                id=f"implement-sweep-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            await handle.result()
            child = env.client.get_workflow_handle("implement-sweep-mctl-web-issue-10-test")
            await child.result()
            history = (await child.fetch_history()).to_json_dict()

        queues_by_activity = {
            e["activityTaskScheduledEventAttributes"]["activityType"]["name"]: e[
                "activityTaskScheduledEventAttributes"
            ]["taskQueue"]["name"]
            for e in history["events"]
            if e["eventType"] == "EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"
        }
        assert queues_by_activity["submit_and_wait"] == IMPLEMENTATION_TASK_QUEUE
        assert queues_by_activity["record_execution"] == TASK_QUEUE


class TestGraceAndConfig:
    async def test_grace_minutes_flows_from_input_to_the_activity(self, env):
        activities, received = _fake_activities()

        await _run(env, activities, ImplementSweepWorkflowInput(grace_minutes=42, max_submits=5))

        assert received["grace_minutes"] == 42

    async def test_a_default_input_uses_the_module_defaults(self, env):
        activities, received = _fake_activities()

        await _run(env, activities, None)

        from orchestrator.temporal.constants import DEFAULT_IMPLEMENT_SWEEP_GRACE_MINUTES

        assert received["grace_minutes"] == DEFAULT_IMPLEMENT_SWEEP_GRACE_MINUTES


class TestPerTickCap:
    async def test_only_up_to_the_cap_is_submitted_and_the_rest_are_counted(self, env):
        candidates = [_candidate(service="mctl-web", slug=f"issue-{n}-test") for n in range(1, 4)]
        activities, received = _fake_activities(stranded=candidates)

        result = await _run(
            env,
            activities,
            ImplementSweepWorkflowInput(grace_minutes=20, max_submits=2),
            # Only the first two candidates survive the cap — see below.
            await_children=[
                "implement-sweep-mctl-web-issue-1-test",
                "implement-sweep-mctl-web-issue-2-test",
            ],
        )

        assert result.candidates == 3
        assert result.submitted == 2
        assert result.skipped == 1
        assert len(received["submits"]) == 2


class TestDedup:
    async def test_a_second_tick_does_not_start_a_second_child(self, env):
        """USE_EXISTING-equivalent dedup: a second start against a still-
        RUNNING child id is a no-op, caught as WorkflowAlreadyStartedError.

        One shared Worker context spans both ticks AND the eventual child
        completion, deliberately: exiting a Worker context drains in-flight
        activities, and the fake submit_and_wait for the first child is
        parked on `gate` until this test releases it — draining a second
        time (via a fresh `_run`) would hang waiting for a gate nothing has
        set yet.
        """
        gate = anyio.Event()
        activities, received = _fake_activities(submit_gate=gate)

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            first = await env.client.execute_workflow(
                ImplementSweepWorkflow.run,
                None,
                id=f"implement-sweep-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            assert first.submitted == 1

            second = await env.client.execute_workflow(
                ImplementSweepWorkflow.run,
                None,
                id=f"implement-sweep-test-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            assert second.submitted == 0
            assert second.skipped == 1

            gate.set()
            child = env.client.get_workflow_handle("implement-sweep-mctl-web-issue-10-test")
            await child.result()

        assert len(received["submits"]) == 1


class TestPrestartRetryBudget:
    """mctl-agents#412 review, P1: a `pre_start` outcome touches no
    `.status.yaml` field, so nothing else removes a proposal stuck that way
    from the next tick's candidate set — an unbounded resubmit loop unless
    this tick itself stops after MAX_SWEEP_PRESTART_ATTEMPTS."""

    async def test_a_proposal_over_the_prestart_budget_is_not_resubmitted(self, env):
        from orchestrator.temporal.workflows.implement_sweep import (
            MAX_SWEEP_PRESTART_ATTEMPTS,
        )

        activities, received = _fake_activities(prior_failures=MAX_SWEEP_PRESTART_ATTEMPTS)

        result = await _run(env, activities)

        assert result.submitted == 0
        assert received["submits"] == []

    async def test_a_proposal_under_the_prestart_budget_is_still_submitted(self, env):
        from orchestrator.temporal.workflows.implement_sweep import (
            MAX_SWEEP_PRESTART_ATTEMPTS,
        )

        activities, received = _fake_activities(prior_failures=MAX_SWEEP_PRESTART_ATTEMPTS - 1)

        result = await _run(env, activities, await_children=["implement-sweep-mctl-web-issue-10-test"])

        assert result.submitted == 1
        assert len(received["submits"]) == 1


class TestOutcomeClassification:
    """mctl-agents#412 review, finding 2: the terminal-phase guard must not
    collapse to `result.phase != "Succeeded"` — that is exactly the
    reduction implement_outcome.py exists to reject. It has to classify the
    same way dev_loop._implement does, so a pre-start failure (nothing ever
    ran), a plain execution failure and a finalization failure each raise
    their own distinguishable `ApplicationError.type`, not one generic
    "ImplementationFailed" for every non-Succeeded phase."""

    async def test_a_pre_start_failure_is_reported_as_not_started(self, env):
        activities, _ = _fake_activities(
            submit_result=WorkflowResult(
                workflow_name="mctl-agents-implement-fake",
                phase="Failed",
                implementer_ran=False,
            )
        )

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                SweptImplementWorkflow.run,
                SweptImplementInput(service="mctl-web", slug="issue-10-test"),
                id=f"swept-prestart-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with pytest.raises(WorkflowFailureError) as excinfo:
                await handle.result()

        cause = excinfo.value.cause
        assert isinstance(cause, ApplicationError)
        assert cause.type == "ImplementationNotStarted"

    async def test_a_finalization_failure_is_distinguished_from_execution(self, env):
        activities, _ = _fake_activities(
            submit_result=WorkflowResult(
                workflow_name="mctl-agents-implement-fake",
                phase="Failed",
                implementer_ran=True,
                implementer_phase="Succeeded",
                finalization_phase="Failed",
            )
        )

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                SweptImplementWorkflow.run,
                SweptImplementInput(service="mctl-web", slug="issue-10-test"),
                id=f"swept-finalization-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with pytest.raises(WorkflowFailureError) as excinfo:
                await handle.result()

        cause = excinfo.value.cause
        assert isinstance(cause, ApplicationError)
        assert cause.type == "ImplementationFinalizationFailed"

    async def test_a_plain_execution_failure_still_raises(self, env):
        activities, _ = _fake_activities(
            submit_result=WorkflowResult(
                workflow_name="mctl-agents-implement-fake",
                phase="Failed",
                implementer_ran=True,
                implementer_phase="Failed",
            )
        )

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                SweptImplementWorkflow.run,
                SweptImplementInput(service="mctl-web", slug="issue-10-test"),
                id=f"swept-execution-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with pytest.raises(WorkflowFailureError) as excinfo:
                await handle.result()

        cause = excinfo.value.cause
        assert isinstance(cause, ApplicationError)
        assert cause.type == "ImplementationFailed"


class TestRecordExecution:
    """mctl-agents#412 review, finding 3: a swept implement run must write
    the same executions-ledger record DevLoopWorkflow's own implement step
    does (dev_loop._record), so it is visible to
    mctl_list_recent_agent_runs. Before this, no class of swept run wrote
    one at all."""

    async def test_a_successful_swept_run_records_its_execution(self, env):
        activities, received = _fake_activities()

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                SweptImplementWorkflow.run,
                SweptImplementInput(service="mctl-web", slug="issue-10-test"),
                id=f"swept-record-ok-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            result = await handle.result()

        assert result.phase == "Succeeded"
        assert len(received["record_execution"]) == 1
        record = received["record_execution"][0]
        assert record.agent == "implementer"
        assert record.target_repo == "mctl-web"
        assert record.phase == "Succeeded"
        assert record.argo_workflow_name == result.workflow_name

    async def test_a_failed_swept_run_still_records_its_execution(self, env):
        activities, received = _fake_activities(
            submit_result=WorkflowResult(
                workflow_name="mctl-agents-implement-fake",
                phase="Failed",
                implementer_ran=True,
                implementer_phase="Failed",
            )
        )

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                SweptImplementWorkflow.run,
                SweptImplementInput(service="mctl-web", slug="issue-10-test"),
                id=f"swept-record-failed-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with pytest.raises(WorkflowFailureError):
                await handle.result()

        assert len(received["record_execution"]) == 1
        record = received["record_execution"][0]
        assert record.target_repo == "mctl-web"
        assert record.phase == "Failed"


class TestStrandedScanFailure:
    """review P2: `list_active_dev_loop_ids` was wrapped and this sibling was
    not, so a GitHub 5xx or a malformed listing failed the whole SCHEDULED
    workflow every 15 minutes instead of reporting a skipped tick."""

    async def test_a_failed_scan_reports_a_skipped_tick_instead_of_failing(self, env):
        activities, received = _fake_activities(scan_fails=True)

        result = await _run(env, activities)

        assert received["submits"] == []
        assert result.candidates == 0
        assert result.submitted == 0
        assert result.skipped_reason is not None, (
            "the field that exists to say 'did not look' must actually be assigned"
        )
        assert "stranded scan" in result.skipped_reason


class TestBudgetQueryFailure:
    async def test_an_unknown_budget_skips_the_whole_tick(self, env):
        """An unknown prior-failure count must not read as zero — that removes
        the bound exactly when visibility is unhealthy. The read covers the
        whole candidate set in one query, so there is no partial answer to
        carry on with: the tick reports a skipped_reason and submits nothing."""
        activities, received = _fake_activities(
            budget_query_fails=True,
            stranded=[_candidate(slug="issue-1-a"), _candidate(slug="issue-2-b")],
        )

        result = await _run(env, activities)

        assert received["submits"] == []
        assert result.submitted == 0
        assert result.candidates == 2
        assert result.skipped == 2
        assert result.skipped_reason is not None
        assert "pre-start budget" in result.skipped_reason


class TestBudgetQueryFanOut:
    """review P2: the budget was read one visibility query per candidate, which
    fanned out with the size of the backlog (71 today). Capping the number of
    queries was worse — `scan.stranded` is rebuilt in stable tree order every
    tick and an over-budget candidate never leaves it, so once the cap's worth
    of over-budget proposals precede candidate N, N was never queried or
    submitted again on ANY tick."""

    async def test_one_query_covers_every_candidate(self, env):
        candidates = [_candidate(slug=f"issue-{i}-x") for i in range(40)]
        activities, received = _fake_activities(stranded=candidates)

        result = await _run(
            env,
            activities,
            await_children=[f"implement-sweep-mctl-web-issue-{i}-x" for i in range(5)],
        )

        assert len(received["budget_queries"]) == 1, (
            "one query per tick, not one per candidate"
        )
        assert len(received["budget_queries"][0]) == 40, (
            "and it must cover the whole candidate list, not a truncated head"
        )
        assert result.submitted == 5

    async def test_a_late_candidate_behind_many_over_budget_ones_is_still_reachable(
        self, env
    ):
        """The starvation this replaced: with a query cap, candidate 30 sat
        behind 30 over-budget ones in a list that never reorders, so it was
        never reached on any tick."""
        candidates = [_candidate(slug=f"issue-{i}-x") for i in range(31)]
        activities, received = _fake_activities(
            stranded=candidates,
            prior_failures_by_id={
                f"implement-sweep-mctl-web-issue-{i}-x": 99 for i in range(30)
            },
        )

        result = await _run(
            env, activities, await_children=["implement-sweep-mctl-web-issue-30-x"]
        )

        assert result.over_budget == 30
        assert result.submitted == 1
        assert received["submits"][0].params["slug"] == "issue-30-x"


class TestOverBudgetIsReported:
    async def test_exhausting_the_prestart_budget_lands_on_the_result(self, env):
        """review P2: exhaustion was log-only — no result field, tick green —
        so "needs a human" reached no human and the candidate was re-derived
        and re-queried every 15 minutes forever."""
        from orchestrator.temporal.workflows.implement_sweep import MAX_SWEEP_PRESTART_ATTEMPTS

        activities, received = _fake_activities(
            prior_failures=MAX_SWEEP_PRESTART_ATTEMPTS,
            stranded=[_candidate(slug="issue-1-a"), _candidate(slug="issue-2-b")],
        )

        result = await _run(env, activities)

        assert received["submits"] == []
        assert result.over_budget == 2
        assert result.submitted == 0

    async def test_over_budget_writes_no_execution_ledger_row(self, env):
        """review P1: an earlier round recorded an ExecutionRecord per
        over-budget candidate per tick. The branch touches no gitops field and
        does not consume `max_submits`, so the candidate is re-derived every
        tick — 96 rows/day/proposal carrying `agent="implementer"`,
        `phase="Failed"` and a Temporal child id in `argo_workflow_name`:
        indistinguishable from a real implementer failure, naming no Argo
        workflow. `over_budget` above is the report; the ledger is not."""
        from orchestrator.temporal.workflows.implement_sweep import MAX_SWEEP_PRESTART_ATTEMPTS

        activities, received = _fake_activities(
            prior_failures=MAX_SWEEP_PRESTART_ATTEMPTS,
            stranded=[_candidate(slug="issue-1-a"), _candidate(slug="issue-2-b")],
        )

        result = await _run(env, activities)

        assert result.over_budget == 2
        assert received["record_execution"] == [], (
            "a run that never happened must not appear in the executions ledger"
        )


class TestUnknownBudgetIsNotZero:
    """The consumer half of the omission contract (review P2 on `51ce44f`).

    `count_swept_prestart_failures` OMITS an id it could not query rather than
    reporting zero for it. Until now the producer half was pinned in
    `test_visibility_activity.py` and the consumer half only by prose in two
    docstrings — the fake answered for every requested id, so this branch was
    dead in every workflow test. If it regresses, `.get(child_id, 0)` yields 0,
    the id reads as "never failed to start", and the unbounded pre-start
    resubmit the budget exists to stop returns for exactly the ids the activity
    refused to vouch for — with the tick green and `over_budget` at 0.
    """

    async def test_an_omitted_id_is_not_submitted(self, env):
        activities, received = _fake_activities(
            stranded=[_candidate(slug="issue-1-a")],
            prior_failures_by_id={"implement-sweep-mctl-web-issue-1-a": None},
        )

        result = await _run(env, activities)

        assert received["submits"] == []
        assert result.submitted == 0

    async def test_an_omitted_id_is_not_counted_as_over_budget(self, env):
        """An unreadable budget is not an exhausted one: it has a different
        remedy (a slug that fails the id charset fails it every tick forever),
        so folding it into `over_budget` would misreport it."""
        activities, _ = _fake_activities(
            stranded=[_candidate(slug="issue-1-a")],
            prior_failures_by_id={"implement-sweep-mctl-web-issue-1-a": None},
        )

        result = await _run(env, activities)

        assert result.over_budget == 0
        assert result.unknown_budget == 1, (
            "an unreadable budget needs its own report: unlike an exhausted "
            "one it never clears on its own"
        )

    async def test_an_omitted_id_does_not_starve_its_neighbours(self, env):
        """The omitted id must not consume a submit slot either — the same
        ordering rule the rest of this PR enforces."""
        activities, received = _fake_activities(
            stranded=[_candidate(slug="issue-1-a"), _candidate(slug="issue-2-b")],
            prior_failures_by_id={
                "implement-sweep-mctl-web-issue-1-a": None,
                "implement-sweep-mctl-web-issue-2-b": 0,
            },
        )

        result = await _run(env, activities)

        assert result.submitted == 1
        assert result.candidates == 2
        assert received["budget_queries"] == [
            [
                "implement-sweep-mctl-web-issue-1-a",
                "implement-sweep-mctl-web-issue-2-b",
            ]
        ], "both candidates must be asked about in the one bulk query"


class TestUnauthorizedIsReported:
    async def test_quarantined_proposals_land_on_the_result(self, env):
        """The 2026-09-19 product decision on #412 requires the legacy
        auto-accepted records reach a human. A log-only count is
        indistinguishable from a clean tick to everything that reads a sweep."""
        activities, _ = _fake_activities(
            stranded=[],
            unauthorized=[
                ("mctl-web/incident-2026-08-01-a", "legacy auto-accepted / unreviewed"),
                ("mctl-api/incident-2026-08-02-b", "legacy auto-accepted / unreviewed"),
            ],
        )

        result = await _run(env, activities)

        assert result.unauthorized == 2
        assert result.candidates == 0
        assert result.submitted == 0
        assert result.skipped_reason is None, (
            "a quarantine is a tick that looked, not one that declined to look"
        )


class TestPrestartErrorType:
    """review P2: the pre-start budget must be charged for pre-start losses
    ONLY. `count_swept_prestart_failures` reads each failed execution's own
    terminal error back and counts just `PRE_START_ERROR_TYPE`, so the three
    error types are not cosmetic — the bound is enforced through them. If a
    classification stops producing its own type the counter silently counts
    zero and the bound is gone."""

    @pytest.mark.parametrize(
        ("result_kwargs", "expected_type"),
        [
            ({"implementer_ran": False}, "ImplementationNotStarted"),
            (
                {"implementer_ran": True, "implementer_phase": "Failed"},
                "ImplementationFailed",
            ),
            (
                {
                    "implementer_ran": True,
                    "implementer_phase": "Succeeded",
                    "finalization_phase": "Failed",
                },
                "ImplementationFinalizationFailed",
            ),
        ],
    )
    async def test_each_outcome_raises_its_own_readable_error_type(
        self, env, result_kwargs, expected_type
    ):
        from orchestrator.temporal.implement_outcome import PRE_START_ERROR_TYPE

        activities, _ = _fake_activities(
            submit_result=WorkflowResult(
                workflow_name="mctl-agents-implement-fake",
                phase="Failed",
                **result_kwargs,
            )
        )

        async with Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                SweptImplementWorkflow.run,
                SweptImplementInput(service="mctl-web", slug="issue-10-test"),
                id=f"swept-type-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
            with pytest.raises(WorkflowFailureError) as excinfo:
                await handle.result()

            assert excinfo.value.cause.type == expected_type
            # The exact shape count_swept_prestart_failures reads back.
            assert (excinfo.value.cause.type == PRE_START_ERROR_TYPE) is (
                result_kwargs.get("implementer_ran") is False
            )
