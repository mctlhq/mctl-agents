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
# Set above MAX_SWEEP_PRESTART_ATTEMPTS so a run of pre-start losses still
# reaches the budget with uncounted failures interleaved. State the limit of
# that plainly (review P3): it is a TOLERANCE of exactly
# `_MAX_EXAMINED_PER_ID - MAX_SWEEP_PRESTART_ATTEMPTS` (9) interleaved
# uncounted runs, not a guarantee. Beyond that the window evicts a pre-start
# loss and the verdict can fall back from exhausted to under-budget. The path
# is narrow — an over-budget id stops being submitted, so eviction needs the id
# to be under budget already — but it is real, and it errs toward one extra
# resubmit rather than toward a permanently unsweepable proposal.
_MAX_EXAMINED_PER_ID = 12

# Hard ceiling on how many listed rows one chunk's listing walks, examined or
# skipped. The per-id fetch cap alone does NOT bound the traversal: rows come
# back interleaved by recency, so an id that is capped and an id that has only
# a handful of runs coexist, and the second keeps the listing open while the
# first's ever-growing tail scrolls past. Bounding the FETCHES while leaving
# the WALK unbounded moves the wedge instead of removing it (review P2).
#
# A count short of the true total is the safe direction here and is the same
# trade the pre-start filter already makes: under-charging the budget costs one
# extra resubmit, over-charging can make a proposal permanently unsweepable.
#
# WHAT THIS DOES TO AN ID IT NEVER REACHED, settled deliberately, because the
# rest of this module states the opposite rule twice (`_SAFE_ID` above, and the
# docstring below: "an absent count is not a zero count"). The ceiling creates a
# THIRD category — queried, but not walked far enough to have looked — and it
# reports those ids as `0`, the permissive value, rather than omitting them.
#
# That is the right side here, and for a reason that does not apply to the
# other two: omitting would be STABLE starvation. The listing is most-recent
# first and the candidate list is rebuilt in the same order every tick, so an
# id whose rows all sit beyond the ceiling would be omitted on every tick
# forever and never submitted again — the exact anti-pattern this PR removed
# twice. Reporting `0` submits it once more, and that submit mints a fresh
# pre-start row which is the NEWEST row in the chunk, so the count self-corrects
# within a few ticks. One extra submit versus permanent starvation.
#
# Scaled by the ACTUAL chunk size, not `_ID_CHUNK` (its maximum), or the
# backstop is loosest exactly where it has to work. The shape `remaining`
# cannot catch is one quiet id holding the listing open while a noisy id's tail
# scrolls past — and a chunk with two ids is a SMALL chunk, which a ceiling
# sized for 100 ids would let walk 200x its own window instead of 4x.
_LISTED_PER_ID_HEADROOM = 4


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
            # Before the listing, not only inside it. `heartbeat_timeout` runs
            # from activity start, so a chunk that lists nothing at all — the
            # HEALTHY case — would otherwise spend the whole listing phase in
            # heartbeat silence under a 30s deadline instead of the 5-minute
            # start_to_close declared beside it (review P2).
            activity.heartbeat(f"listing {len(chunk)} id(s)")
            # Two different scopes, easy to misread as one: `examined` is per
            # id and persists ACROSS chunks, while `walked` and its ceiling are
            # per chunk and reset with each listing.
            remaining = set(chunk)
            walked = 0
            ceiling = len(chunk) * _MAX_EXAMINED_PER_ID * _LISTED_PER_ID_HEADROOM
            async for wf in self._client.list_workflows(
                f"WorkflowId IN ({quoted}) AND ExecutionStatus = 'Failed'"
            ):
                walked += 1
                if walked > ceiling:
                    # Name the ids: this line is the only signal this path
                    # produces, and "counts are lower bounds" is not actionable
                    # without knowing WHOSE budgets became lower bounds.
                    unwalked = sorted(c for c in chunk if examined[c] == 0)
                    activity.logger.warning(
                        "count_swept_prestart_failures: stopped walking a "
                        "%d-id chunk's listing after %d row(s); its counts are "
                        "lower bounds, and %d id(s) were never reached at all: "
                        "%s",
                        len(chunk),
                        ceiling,
                        len(unwalked),
                        unwalked,
                    )
                    break
                # Every listed execution beats, whether or not it is examined.
                # The skip paths below are exactly the ones that walk the long
                # tail, so keeping the beat above them is what makes the
                # traversal safe rather than merely cheap.
                activity.heartbeat(wf.id)
                if wf.id not in counts:
                    continue
                if examined[wf.id] >= _MAX_EXAMINED_PER_ID:
                    # Already seen this id's `_MAX_EXAMINED_PER_ID` most recent
                    # runs. Skipping the rest is what keeps the per-tick cost
                    # bounded; it costs only precision beyond the window, which
                    # the caller cannot use.
                    remaining.discard(wf.id)
                    if not remaining:
                        # Every id in this chunk is capped, so the rest of the
                        # listing can only be skipped. Bounding the FETCHES
                        # while still walking an unbounded tail moved the wedge
                        # instead of removing it (review P2).
                        break
                    continue
                examined[wf.id] += 1
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
