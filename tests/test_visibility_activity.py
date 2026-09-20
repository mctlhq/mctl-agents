"""`VisibilityActivities` — the two visibility reads the sweep runs on (#412).

`count_swept_prestart_failures` had no test of its own: its query string and
its counting loop were exercised only through workflow tests that faked the
whole activity out (review P3). That loop carries the pre-start filter the
budget depends on, and every way it can be wrong is silent — counting too much
makes a proposal permanently unsweepable, counting too little removes the
bound.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from orchestrator.temporal.activities.visibility import (
    ACTIVE_DEV_LOOPS_QUERY,
    VisibilityActivities,
)
from orchestrator.temporal.implement_outcome import PRE_START_ERROR_TYPE

pytestmark = pytest.mark.anyio

CHILD_ID = "implement-sweep-mctl-web-issue-10-widget"


def _execution(error_type: str | None, *, unreadable: bool = False):
    """A listed failed execution whose recorded terminal error is `error_type`.

    `unreadable` models a cause the activity cannot classify at all (a
    history fetch that errors, a retention-expired run) — deliberately a
    different case from a failure with a recognisable non-pre-start type.
    """
    wf = MagicMock()
    wf.id, wf.run_id = CHILD_ID, f"run-{error_type}-{unreadable}"
    if unreadable:
        wf._raise = RuntimeError("history unavailable")
    elif error_type is None:
        wf._raise = WorkflowFailureError(cause=RuntimeError("not an ApplicationError"))
    else:
        wf._raise = WorkflowFailureError(cause=ApplicationError("boom", type=error_type))
    return wf


def _client(executions: list) -> MagicMock:
    """A client whose `list_workflows` records its query and yields `executions`,
    and whose handles replay each execution's recorded terminal failure."""
    client = MagicMock()
    seen: list[str] = []
    by_run = {wf.run_id: wf for wf in executions if hasattr(wf, "_raise")}

    def list_workflows(query: str):
        seen.append(query)

        async def _gen():
            for wf in executions:
                yield wf

        return _gen()

    def get_workflow_handle(wf_id: str, run_id: str | None = None):
        handle = MagicMock()
        handle.result = AsyncMock(side_effect=by_run[run_id]._raise)
        return handle

    client.list_workflows = list_workflows
    client.get_workflow_handle = get_workflow_handle
    client.queries = seen
    return client


@pytest.fixture
def env():
    return ActivityEnvironment()


class TestCountSweptPrestartFailures:
    async def test_only_prestart_outcomes_are_counted(self, env):
        """The budget is a PRE-START budget. An `execution` or `finalization`
        failure means the implementer ran and already wrote
        needs-triage/blocked, so the proposal leaves the candidate set on its
        own; charging it here made a triaged-and-re-accepted proposal arrive
        with strikes already spent."""
        client = _client([
            _execution(PRE_START_ERROR_TYPE),
            _execution("ImplementationFailed"),
            _execution("ImplementationFinalizationFailed"),
            _execution(PRE_START_ERROR_TYPE),
        ])
        acts = VisibilityActivities(client)

        counts = await env.run(acts.count_swept_prestart_failures, [CHILD_ID])
        assert counts == {CHILD_ID: 2}

    async def test_an_unclassifiable_failure_is_not_counted(self, env):
        """Of the two ways to be wrong about an unclassifiable execution,
        under-charging costs one extra resubmit while over-charging can make a
        proposal permanently unsweepable. A submit_and_wait that exhausted its
        retries against an Argo/mctl-api outage lands here too — an outage must
        not spend the budget meant for "nothing was attempted"."""
        client = _client([
            _execution(None),
            _execution(None, unreadable=True),
            _execution(PRE_START_ERROR_TYPE),
        ])
        acts = VisibilityActivities(client)

        counts = await env.run(acts.count_swept_prestart_failures, [CHILD_ID])
        assert counts == {CHILD_ID: 1}

    async def test_no_failed_executions_is_zero_for_every_id(self, env):
        acts = VisibilityActivities(_client([]))

        counts = await env.run(acts.count_swept_prestart_failures, [CHILD_ID, "other"])
        assert counts == {CHILD_ID: 0, "other": 0}, (
            "every candidate must get an entry — a missing key read as zero "
            "somewhere else would silently remove the bound"
        )

    async def test_an_empty_candidate_list_issues_no_query(self, env):
        client = _client([])
        acts = VisibilityActivities(client)

        assert await env.run(acts.count_swept_prestart_failures, []) == {}
        assert client.queries == []

    async def test_one_bulk_query_covers_the_whole_candidate_set(self, env):
        """One query per TICK, not per candidate.

        The per-candidate version fanned out with the size of the backlog (71),
        and the cap added to bound that starved the tail of the candidate list
        permanently — the list is rebuilt in stable order every tick and an
        over-budget candidate never leaves it (review P2).
        """
        client = _client([])
        acts = VisibilityActivities(client)

        await env.run(acts.count_swept_prestart_failures, [CHILD_ID, "implement-sweep-b"])

        assert len(client.queries) == 1
        assert client.queries == [
            f"WorkflowId IN ('{CHILD_ID}', 'implement-sweep-b') "
            "AND ExecutionStatus = 'Failed'"
        ]

    async def test_an_unqueryable_id_is_omitted_rather_than_reported_as_zero(self, env):
        """These ids come from gitops path segments, not user input, but a
        malformed slug carrying a quote would otherwise be spliced into the
        filter. Rather than guess the filter dialect's escaping, such an id is
        refused a query — and crucially it is OMITTED from the result, not
        reported as zero prior failures, because a zero would read as "never
        failed to start" and license an unbounded resubmit (review P3)."""
        client = _client([])
        acts = VisibilityActivities(client)

        counts = await env.run(
            acts.count_swept_prestart_failures, ["implement-sweep-o'brien", CHILD_ID]
        )

        assert "implement-sweep-o'brien" not in counts
        assert counts == {CHILD_ID: 0}
        assert client.queries == [
            f"WorkflowId IN ('{CHILD_ID}') AND ExecutionStatus = 'Failed'"
        ]

    async def test_every_id_being_unqueryable_asks_nothing_at_all(self, env):
        client = _client([])
        acts = VisibilityActivities(client)

        assert await env.run(acts.count_swept_prestart_failures, ["bad'id"]) == {}
        assert client.queries == []

    async def test_a_long_candidate_list_is_chunked(self, env):
        """The candidate list is one entry per stranded proposal and unbounded,
        while the whole filter is a single string — an unchunked query can be
        rejected outright, on a path that now fails the whole tick closed."""
        ids = [f"implement-sweep-svc-{n:03d}" for n in range(250)]
        client = _client([])
        acts = VisibilityActivities(client)

        counts = await env.run(acts.count_swept_prestart_failures, ids)

        assert counts == dict.fromkeys(ids, 0)
        assert len(client.queries) == 3
        assert sum(q.count("implement-sweep-svc-") for q in client.queries) == 250

    async def test_an_unrequested_id_in_the_results_is_ignored(self, env):
        stray = _execution(PRE_START_ERROR_TYPE)
        stray.id = "implement-sweep-somebody-else"
        client = _client([stray])
        acts = VisibilityActivities(client)

        assert await env.run(acts.count_swept_prestart_failures, [CHILD_ID]) == {CHILD_ID: 0}

    async def test_a_visibility_error_propagates(self, env):
        """The caller fails closed on this candidate; it can only do that if
        the LISTING error actually reaches it — unlike a single unreadable
        cause, which is swallowed above."""
        client = MagicMock()

        def boom(query: str):
            raise RuntimeError("visibility unavailable")

        client.list_workflows = boom
        acts = VisibilityActivities(client)

        with pytest.raises(RuntimeError):
            await env.run(acts.count_swept_prestart_failures, [CHILD_ID])


class TestListActiveDevLoopIds:
    async def test_it_returns_the_ids_and_pins_the_query(self, env):
        wf_a, wf_b = MagicMock(), MagicMock()
        wf_a.id, wf_b.id = "dev-loop-mctlhq-mctl-web-10", "dev-loop-mctlhq-mctl-api-20"
        client = _client([wf_a, wf_b])
        acts = VisibilityActivities(client)

        ids = await env.run(acts.list_active_dev_loop_ids)

        assert ids == [wf_a.id, wf_b.id]
        assert client.queries == [ACTIVE_DEV_LOOPS_QUERY]

    async def test_running_only_is_part_of_the_query(self):
        """A closed DevLoop IS the case the sweep exists to catch, so the
        status clause is load-bearing, not an optimisation."""
        assert "ExecutionStatus = 'Running'" in ACTIVE_DEV_LOOPS_QUERY
        assert "WorkflowType = 'DevLoopWorkflow'" in ACTIVE_DEV_LOOPS_QUERY
