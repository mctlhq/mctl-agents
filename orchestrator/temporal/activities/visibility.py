"""Activity: list active DevLoopWorkflow IDs via Temporal visibility.

ReconcileWorkflow's orphan detection needs the set of currently-running
DevLoop workflow IDs to compare against actionable proposals. This lives in
its own class because it needs the connected Temporal client, which plain
function activities don't have — the worker constructs one instance with
the client it already holds and registers the bound method.
"""
from __future__ import annotations

import re

from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError

from orchestrator.temporal.implement_outcome import PRE_START_ERROR_TYPE

# Visibility query for the active DevLoop set. WorkflowType is the
# @workflow.defn class name; ExecutionStatus 'Running' deliberately excludes
# terminal and continued-as-new-completed runs — a proposal whose workflow
# closed IS the orphan case detect_orphans exists to catch.
ACTIVE_DEV_LOOPS_QUERY = "WorkflowType = 'DevLoopWorkflow' AND ExecutionStatus = 'Running'"

# How many workflow ids go into one `WorkflowId IN (...)` filter. The candidate
# list is unbounded (one entry per stranded proposal, ~212 proposals in gitops
# today) and the whole filter is one string, so an unchunked query can be
# rejected outright — on a path that now fails the WHOLE tick closed, which
# turns a size limit into "the sweep never runs again" (review P3).
_ID_CHUNK = 100

# Workflow ids are `implement-sweep-{service}-{slug}`, built from gitops path
# segments. Rather than guess how the visibility filter's parser wants a quote
# escaped — a dialect this code does not own — ids outside this charset are
# refused a query at all. An id that cannot be asked about is NOT reported as
# zero prior failures: it is omitted from the result, and the caller treats a
# missing entry as an unknown budget and declines to submit (review P3).
_SAFE_ID = re.compile(r"\A[A-Za-z0-9._-]+\Z")

# How many of one child id's Failed executions this reads before it stops
# asking. `list_workflows` returns most-recent-first, so these are the N most
# recent runs of that id, and the count this returns is "pre-start losses among
# the last N runs" rather than "since the retention window began".
#
# Two reasons it must be bounded, not just a bigger timeout (review P2):
#
#  1. Nothing bounds how many Failed executions pile up under one child id.
#     `ALLOW_DUPLICATE` keeps every closed run, and the two failure classes
#     this deliberately does NOT count — an `ActivityError` cause from a submit
#     that exhausted its retries, and a cause it cannot read — touch no
#     `.status.yaml` field, so the candidate is re-derived and a new Failed
#     execution minted EVERY tick. A prolonged outage is ~480 uncounted
#     executions/day, each of them re-fetched on every later tick. Unbounded
#     work to compute a bounded control value is a monotonic self-wedge: once
#     the cost crosses this activity's start_to_close the tick fails closed,
#     and the next tick's input is strictly larger, so it never recovers.
#  2. The caller only ever compares against MAX_SWEEP_PRESTART_ATTEMPTS, so
#     lifetime precision buys nothing a recency window does not.
#
# Set above MAX_SWEEP_PRESTART_ATTEMPTS so that a run of consecutive pre-start
# losses still reaches the budget even with uncounted failures interleaved.
_MAX_EXAMINED_PER_ID = 12


class VisibilityActivities:
    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn
    async def list_active_dev_loop_ids(self) -> list[str]:
        """Return the workflow IDs of all running DevLoopWorkflow executions.

        Raises on visibility errors — the caller (ReconcileWorkflow) treats a
        failure as "active set unknown" and skips orphan detection for the
        tick rather than reporting every proposal as an orphan.
        """
        ids: list[str] = []
        async for wf in self._client.list_workflows(ACTIVE_DEV_LOOPS_QUERY):
            ids.append(wf.id)
        activity.logger.info("visibility: %d active DevLoopWorkflow run(s)", len(ids))
        return ids

    @activity.defn
    async def count_swept_prestart_failures(self, workflow_ids: list[str]) -> dict[str, int]:
        """Per child id, how many prior executions died BEFORE the implementer ran.

        mctl-agents#412 review: `ImplementSweepWorkflow` is a fresh execution
        every 15-minute tick and keeps no state of its own, so a `pre_start`
        outcome — the one class that touches no `.status.yaml` field, because
        nothing ever ran — would otherwise be resubmitted forever. The child's
        workflow id is deterministic per (service, slug) and reused
        (`id_reuse_policy=ALLOW_DUPLICATE`), so Temporal's own visibility store
        is the only durable record of how many times a given proposal has
        already failed to start; `ImplementSweepWorkflow` uses this to stop
        resubmitting past a bound, the same way `MAX_PRESTART_REQUEUES` bounds
        `dev_loop._implement`.

        BULK, and that matters. The per-id version fanned out one visibility
        query per candidate — 71 on today's backlog — and the cap added to
        bound that starved the tail of the candidate list permanently, because
        the list is rebuilt in stable order every tick and an over-budget
        candidate never leaves it. One `IN (...)` query per tick has neither
        problem.

        PRE-START ONLY (review P2). Counting every Failed execution also
        charged the budget for `execution` and `finalization` outcomes — runs
        where the implementer DID run and left needs-triage/blocked behind, so
        the proposal drops out of the candidate set on its own. A proposal
        triaged and re-accepted then arrived with strikes already charged and
        became permanently unsweepable after two real pre-start kills.
        `ExecutionStatus = 'Failed'` alone cannot say which, and visibility
        cannot filter on an `ApplicationError.type`, so each failed execution's
        own terminal error is read back and only `PRE_START_ERROR_TYPE` counts.
        A `submit_and_wait` that exhausted its retries against an Argo/mctl-api
        outage carries a different shape and is left out, as is any cause that
        cannot be read at all: of the two ways to be wrong there,
        under-charging costs one extra resubmit while over-charging can make a
        proposal permanently unsweepable.

        BOUNDED, and that matters as much as the bulk query. Only the
        `_MAX_EXAMINED_PER_ID` most recent runs of each id are read, so what
        this returns is "pre-start losses among the last N runs", not a
        lifetime total. The caller only ever compares against
        MAX_SWEEP_PRESTART_ATTEMPTS, so it cannot use the extra precision —
        while the unbounded version wedged itself permanently under a long
        outage (see the constant for the mechanism). The loop heartbeats, and
        the caller sets a heartbeat timeout, because one RPC per examined
        execution is exactly the shape that looks hung rather than slow.

        Raises on a visibility failure — the caller treats an unknown budget as
        a reason to skip the tick, not to submit on a count of zero. Every id
        this can query gets an entry; an id it cannot query is OMITTED, and the
        caller must treat a missing entry the same way (an absent count is not
        a zero count).
        """
        safe = [wf_id for wf_id in workflow_ids if _SAFE_ID.match(wf_id)]
        queryable = set(safe)
        for wf_id in workflow_ids:
            if wf_id not in queryable:
                activity.logger.warning(
                    "count_swept_prestart_failures: %r is not a queryable "
                    "workflow id; omitting it rather than reporting zero prior "
                    "failures for it",
                    wf_id,
                )
        counts = {wf_id: 0 for wf_id in safe}
        if not safe:
            return counts

        examined: dict[str, int] = dict.fromkeys(safe, 0)
        for start in range(0, len(safe), _ID_CHUNK):
            chunk = safe[start:start + _ID_CHUNK]
            quoted = ", ".join(f"'{wf_id}'" for wf_id in chunk)
            async for wf in self._client.list_workflows(
                f"WorkflowId IN ({quoted}) AND ExecutionStatus = 'Failed'"
            ):
                if wf.id not in counts:
                    continue
                if examined[wf.id] >= _MAX_EXAMINED_PER_ID:
                    # Already seen this id's `_MAX_EXAMINED_PER_ID` most recent
                    # runs. Skipping the rest is what keeps the per-tick cost
                    # bounded; it costs only precision beyond the window, which
                    # the caller cannot use.
                    continue
                examined[wf.id] += 1
                # This loop makes one RPC per examined execution, so it must
                # report progress or a slow Temporal makes it look hung.
                activity.heartbeat(wf.id)
                handle = self._client.get_workflow_handle(wf.id, run_id=wf.run_id)
                try:
                    await handle.result()
                except WorkflowFailureError as exc:
                    cause = exc.cause
                    if isinstance(cause, ApplicationError) and cause.type == PRE_START_ERROR_TYPE:
                        counts[wf.id] += 1
                except Exception:  # noqa: BLE001 — an unreadable cause is not counted, not raised
                    activity.logger.warning(
                        "count_swept_prestart_failures: could not read the failure "
                        "cause for %s run_id=%s; not counted as a pre-start loss",
                        wf.id,
                        wf.run_id,
                    )
        capped = sorted(k for k, n in examined.items() if n >= _MAX_EXAMINED_PER_ID)
        if capped:
            activity.logger.info(
                "count_swept_prestart_failures: stopped at the %d most recent "
                "run(s) for %d id(s): %s",
                _MAX_EXAMINED_PER_ID,
                len(capped),
                capped,
            )
        activity.logger.info(
            "visibility: pre-start failures across %d candidate id(s) in %d "
            "query/queries: %s",
            len(safe),
            (len(safe) + _ID_CHUNK - 1) // _ID_CHUNK,
            {k: v for k, v in counts.items() if v},
        )
        return counts
