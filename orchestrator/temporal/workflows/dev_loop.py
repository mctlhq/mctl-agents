"""DevLoopWorkflow: the durable orchestrator for one GitHub issue's path
through investigate -> human approval -> implement.

This is the phase-4 "first vertical slice" from the plan, deliberately
narrower than the full issue -> ... -> deploy -> monitor pipeline: it covers
exactly the acceptance slice the plan defines ("mctl_trigger_issue ->
Temporal -> registry resolve -> Argo investigate -> proposal in gitops ->
approval signal -> implement -> PR"). The review/fix loop (shepherd) and
deploy/monitor stages are phase 5/6 work, layered on top of this workflow
once the slice is proven, not reimplemented here.

Approval is atomic as of the phase-5 cutover (mctl-agents#150): `approve()`
unblocks this workflow's wait_condition AND the workflow then submits the
`mctl-agents-approve` CWFT, which flips exactly this issue's proposal from
`proposed` to `accepted` as a gitops commit under the
`mctl-gitops-main-writes` mutex. The flip runs inside Argo because this
worker deliberately holds no gitops checkout or deploy key — every gitops
write must go through Argo. The signal optionally carries the approver's
identity, which lands in the proposal's approval block, the gitops commit
message, and the execution audit trail. The flip CWFT is idempotent
(already-accepted is a successful no-op), so a Temporal retry or a racing
manual approve never fails the loop. Histories recorded before this change
replay the legacy signal-only branch via workflow.patched("atomic-approve");
for those in-flight loops the manual gitops flip remains the affordance.

The implement step stays scoped to this issue's own repo AND its own
proposal slug (see `_target_repo` and `find_proposal_slug` below), so even
a mis-signalled approve can never implement a different repo's — or a
different issue's — proposal.

The approval park is bounded (mctl-agents#420): `run()` no longer parks at
an unbounded `wait_condition`. Under the `approval-watch` patch it polls
every `APPROVAL_POLL_INTERVAL`, re-reading the source issue's state and
ending the execution if the issue closed while parked, and gives up at
`APPROVAL_WAIT_DEADLINE` if nothing resolves the wait first. An `abandon`
signal (a graceful, cluster-access-free alternative to Temporal
`terminate`) ends either this park or an in-progress merge watch at their
next observation point. Every one of these paths records why it ended in
`DevLoopResult.ended`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
)
from temporalio.exceptions import (
    TimeoutError as TemporalTimeoutError,
)

with workflow.unsafe.imports_passed_through():
    from orchestrator import human_input
    from orchestrator.lifecycle.contract import (
        OWNED_BY_OTHER,
        UNKNOWN,
        UNOWNED,
        EntityRef,
    )
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.deploy_state import (
        DeployStatus,
        DeployTarget,
        ReleaseInfo,
        get_deploy_status,
        get_release_after,
        resolve_deploy_target,
    )
    from orchestrator.temporal.activities.execution_requests import (
        AdvanceInput,
        BindInput,
        BoundExecution,
        advance_dispatched_execution,
        bind_dispatched_execution,
    )
    from orchestrator.temporal.activities.human_input import find_human_input_request
    from orchestrator.temporal.activities.incidents import (
        Incident,
        IncidentQueryResult,
        list_service_incidents,
    )
    from orchestrator.temporal.activities.issue_state import IssueState, get_issue_state
    from orchestrator.temporal.activities.lifecycle import (
        OwnershipRequest,
        OwnershipResult,
        lifecycle_ownership,
    )
    from orchestrator.temporal.activities.pr_state import PRState, get_pr_state
    from orchestrator.temporal.activities.proposals import find_proposal_slug
    from orchestrator.temporal.activities.registry import ResolvedRelease, resolve_agent_release
    from orchestrator.temporal.activities.state import ExecutionRecord, record_execution
    from orchestrator.temporal.constants import (
        EXECUTION_TASK_QUEUE,
        IMPLEMENTATION_OPERATION,
        IMPLEMENTATION_TASK_QUEUE,
    )
    from orchestrator.temporal.implement_outcome import (
        Outcome,
        classify,
        finalization_evidence,
    )
    from orchestrator.temporal.implement_outcome import (
        pre_start_reason as render_pre_start_reason,
    )
    from orchestrator.temporal.issue_ref import parse_issue_url, request_engine_ref
    from orchestrator.work_context.contract import (
        ACTOR_KINDS,
        SURFACE_KINDS,
        ActorRef,
        ExecutionRef,
        SurfaceRef,
        execution_id_for,
    )
    from orchestrator.work_context.execution_requests import KIND_RESUME, KIND_START, KINDS

ENVIRONMENT = "production"

# Workflow-phase vocabulary (mctlhq/mctl-agents#333, ADR 013). Introduced
# alongside the durable clarification wait — before this there was no
# phase vocabulary at all; "WAITING_FOR_APPROVAL" merely NAMES the existing
# `wait_condition(lambda: self._approved)` below so the two durable gates
# are distinguishable by construction, not just by comment.
RUNNING = "RUNNING"
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
INPUT_TIMED_OUT = "INPUT_TIMED_OUT"

# Ceiling on queued-but-unvalidated `human_input_response` payloads. One
# valid answer resolves a wait, so anything past a small burst is a
# misbehaving surface, not a legitimate backlog — payloads beyond the cap
# (or arriving while no wait is pending) are counted and dropped, mirroring
# `resume`'s bounded-state invariant. Rejected payloads are pruned as they
# are rejected, so only genuinely pending entries count toward the cap.
HUMAN_INPUT_RESPONSE_QUEUE_LIMIT = 16

# How much older than this execution's own start a request's `created_at`
# may be before the request is retired as another execution's leftover.
# Generous against clock skew between the sealing container and Temporal,
# tiny against the hours-to-days age of a real leftover.
HUMAN_INPUT_PRIOR_RUN_SLACK = timedelta(minutes=10)

# The Argo CWFTs already retry within a run (second-OAuth-account fallback on
# a 429/five_hour limit) — see activities/argo.py's module docstring. A
# Temporal retry that re-submitted on top of that would multiply real SDK
# runs, so this bound only exists to let a retried attempt RESUME polling
# (via activity.info().heartbeat_details, see submit_and_wait) after a worker
# crash or missed heartbeat — submit_and_wait detects "already submitted" and
# refuses to re-POST once it has a workflow_name (including the "submitted
# but name unparseable" sentinel, which fails loudly instead of guessing).
# The only gap this doesn't close: a crash strictly between the Argo POST
# succeeding and the first activity.heartbeat() call actually landing at the
# Temporal server — vanishingly small (no I/O in between), but real. 3, not
# unlimited: still fail loudly on a genuinely wedged activity rather than
# retry forever.
SDK_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
SDK_STEP_TIMEOUT = timedelta(hours=2)
# Deliberately no schedule_to_start_timeout and no schedule_to_close_timeout
# on submit_and_wait — see _run_cwft. On the implementation queue (#395) the
# schedule-to-start wait IS the admission queue: bounding it would turn
# "waiting for capacity" into a failure, which is the shape of the bug
# admission exists to remove.

# How many times an implement submit that never started an implementer pod
# is re-submitted before the loop gives up (#395, implement_outcome.py).
# A pre-start failure attempted nothing — it waited on capacity or a mutex
# and was killed by a deadline, or Argo accepted the workflow and never
# scheduled the pod — so it is not an implementation attempt and needs no
# human. Bounded, because a cluster that cannot start pods at all must
# eventually surface as a failed loop rather than resubmit forever.
MAX_PRESTART_REQUEUES = 3
# ...and waited between, so the bound is a time budget and not a burst.
# A pre-start cause can be fast: Argo accepts the workflow and the pod is
# never scheduled (quota, taint, an admission webhook), which comes back in
# seconds. Requeueing straight away would spend all three tries before the
# transient condition could clear and fail a loop that a minute of patience
# would have saved. The sleep costs nothing — a requeue is not an attempt,
# and the wait does not touch the implementation slot, which the completed
# activity already released.
#
# The budget is also spent by the Argo-side mutex: admission lets N
# implement submits exist at once, so if the CWFT's `synchronization` mutex
# is narrower than N, the surplus queues inside Argo against its own
# deadline and comes back pre_start. That is requeued rather than lost, but
# N and the mutex capacity have to move together — ADR-008 D7 records that
# the gitops mutex is expected to be at least N.
PRESTART_REQUEUE_BACKOFF = timedelta(minutes=2)
SDK_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=2)

FAST_ACTIVITY_TIMEOUT = timedelta(seconds=30)
# Bounded, not the Temporal default of unlimited attempts: resolve/record are
# plain HTTP round trips to mctl-api, so a handful of retries covers a real
# transient blip. Unlimited retries on _record in particular would otherwise
# wedge wait_condition forever if mctl-api's executions endpoint were ever
# down — the real SDK work (submit_and_wait) already succeeded by the time
# _record runs, so its own failure must never block workflow progress (see
# _record's try/except below).
FAST_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=5)

# find_proposal_slug hits GitHub, not mctl-api: its worst realistic failure
# is a rate limit whose reset window is up to an hour. FAST (5 attempts,
# default backoff) burns through in under a minute and would permanently
# fail the workflow over a transient limit — reintroducing the manual
# re-trigger toil this fix exists to remove. Spread bounded retries across
# well over an hour instead; the activity is one cheap GET, so patience is
# free. Still bounded: a genuinely broken lookup must eventually surface.
SLUG_LOOKUP_TIMEOUT = timedelta(seconds=30)
SLUG_LOOKUP_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=15),
    maximum_attempts=12,
)

# The approve CWFT is clone + one-line flip + push — no agent, no SDK, and
# its own activeDeadlineSeconds is 600. Anything near SDK_STEP_TIMEOUT here
# would just mean a wedged mutex holding the loop for hours.
APPROVE_STEP_TIMEOUT = timedelta(minutes=15)

# mctl-agents#420: the approval park (`workflow.wait_condition` a few lines
# into `run()`) had no bound of its own -- every other stage in this module
# does. Matched to `MERGE_WATCH_DEADLINE` so the worst-case lifetime of an
# execution is two bounded fortnights, not infinity: 56 polls over the
# deadline is a negligible history footprint against Temporal's 50k event
# limit, two orders of magnitude below the ~1344 polls `_watch_pr` already
# budgets for its own 14-day watch.
APPROVAL_POLL_INTERVAL = timedelta(hours=6)
APPROVAL_WAIT_DEADLINE = timedelta(days=14)

# A dispatched loop (mctlhq/mctl-agents#461) waits for its execution request
# to be fulfilled before it runs anything: the dispatcher starts the loop
# first and fulfils second. The wait is the bind activity's retry policy,
# bounded by FULFILMENT_WAIT. Longer than mctl-api's longest claim lease
# (15 min), so a dispatcher that crashed between the start and the fulfil
# has one full lease to lapse and the next claim to converge on this loop
# before it gives up. A loop that gives up ends having run nothing; a later
# claim of its still-unfulfilled request starts the issue's loop again for it
# (#461 option A, `start.DISPATCH_ID_REUSE_POLICY`).
FULFILMENT_WAIT = timedelta(minutes=30)
FULFILMENT_POLL_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
)
# The success-path advance of a dispatched execution (mctlhq/mctl-agents#461).
# Patient, in the style of SLUG_LOOKUP_RETRY_POLICY: the loop is about to park
# at approval for up to APPROVAL_WAIT_DEADLINE, and while the `we_` stays
# Running mctl-api refuses every other request for the item
# (`execution_active`) — a state the dispatcher's reconciliation cannot heal,
# because the loop is still RUNNING. One cheap POST, retried over roughly an
# hour (10 s doubling to a 10-minute cap, 10 attempts), costs nothing
# against a multi-day park. Bounded, so a store that stays down still lets the
# loop reach the park; `run` then re-attempts once right before it. The
# unwind path (`_fail_dispatched_execution`) keeps FAST_ACTIVITY_RETRY_POLICY:
# that loop closes next, and a closed dispatched loop is reconciled.
DISPATCHED_ADVANCE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=10,
)
# The same re-attempt right before a HUMAN-INPUT park, deliberately short
# (about two minutes at worst: 4 attempts, 2 s doubling to a 15 s cap, each
# bounded by FAST_ACTIVITY_TIMEOUT). The request that park waits on is not
# read until `_await_human_input` runs, so its TTL is unknown here, and
# `human_input` bounds a TTL only from above (MAX_REQUEST_TTL_SECONDS, 7
# days): any `expires_at` after `created_at` is valid, so no minimum makes an
# hour-long attempt safe. The default TTL is a day, so two minutes costs the
# person answering nothing in practice; a pending advance is tried again
# before every later park, patiently before the approval park.
DISPATCHED_ADVANCE_BRIEF_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=4,
)
#: The patch id guarding the dispatched path. Consulted only for a loop
#: whose start input carries an execution request, so no other history
#: records it.
EXECUTION_REQUEST_PATCH = "execution-request-dispatch"
#: A dispatched loop whose bind is refused while an execution of its own
#: engine ref exists (`BoundExecution.stranded`) ends that execution `Failed`
#: before it ends. Consulted only on that path, so no history that did not
#: reach it records it; a history recorded before it (the loop ended without
#: the advance) replays unchanged.
EXECUTION_REQUEST_STRANDED_PATCH = "execution-request-stranded"

# A `resume` execution request delivered onto a LIVE loop (mctlhq/mctl-
# agents#461, ADR 011 §8). The dispatcher hands the loop the request through
# the `accept_execution_request` Update BEFORE it fulfils the request, so the
# loop's own history is the durable "accepted, not yet bound" record; the
# loop then binds the `we_` the fulfil mints (engine ref `<loop id>#<request
# id>`, `issue_ref.request_engine_ref`) and ends it. The patch id is recorded
# where a delivery starts, so no history without a delivery records it.
EXECUTION_REQUEST_RESUME_PATCH = "execution-request-resume"
ACCEPT_EXECUTION_REQUEST_UPDATE = "accept_execution_request"
# Option A of mctlhq/mctl-agents#461: every DevLoop is issue-keyed. The
# dispatcher no longer starts `dev-loop-xr_<request id>`: it starts or joins
# `dev-loop-<owner>-<repo>-<n>` through Update-with-Start, with the request
# delivered by `accept_execution_request` in the same call, so the intake
# poller and the dispatcher converge on one workflow atomically. A loop
# started that way binds its execution under `<loop id>#<request id>`
# (`issue_ref.request_engine_ref`), the ref every dispatched execution has;
# a history recorded before it bound under its own workflow id, and replays
# that way. Consulted only on the dispatched path.
ISSUE_KEYED_DISPATCH_PATCH = "issue-keyed-dispatch"
# Also option A: a dispatched run that ends having run nothing (its request
# was rejected, refused at fulfil, never fulfilled, or its bind refused) ends
# the workflow FAILED instead of COMPLETED. Its id is the issue's now, and the
# intake poller starts it with ALLOW_DUPLICATE_FAILED_ONLY: a COMPLETED run
# that did nothing would make every later `agents:intake` label a silent
# "already handled". Before option A that run had its own `dev-loop-xr_*` id
# and could not shadow the issue's. Recorded where the run ends.
DISPATCHED_NOT_RUN_FAILS_PATCH = "dispatched-not-run-fails"
#: The error type of that failure, for whoever reads why the run ended.
DISPATCHED_NOT_RUN_ERROR_TYPE = "DispatchedRequestNotRun"
#: The kinds of mctl-api execution request a delivery can carry: the
#: dispatcher's own vocabulary (`execution_requests.KINDS`), one source.
DELIVERY_KIND_START = KIND_START
DELIVERY_KIND_RESUME = KIND_RESUME
DELIVERY_KINDS = KINDS
#: The Update's only successful answer. A repeated request id answers it too.
DELIVERY_ACCEPTED = "accepted"
#: The validator's permanent refusal: the dispatcher rejects the request with
#: `resume_refused:<reason>`, before any fulfil, so no `we_` is minted.
RESUME_REFUSED_ERROR_TYPE = "ResumeRefused"
#: The validator's transient refusal (the loop is not ready, or is ending):
#: the dispatcher defers, and a later claim delivers or starts a continuation.
RESUME_DEFERRED_ERROR_TYPE = "ResumeDeferred"
#: The validator's answer to a `start` request delivered onto a loop that was
#: not started for it: the issue already has a DevLoop, so the dispatcher
#: rejects the request `loop_active`, as it always has.
LOOP_ACTIVE_ERROR_TYPE = "LoopActive"
# How long an exiting loop still waits for an accepted delivery's fulfilment.
# The dispatcher fulfils within seconds of the Update, and re-checks the loop
# after the fulfil (failing the execution itself when the loop has closed),
# so this only has to cover one fulfil round trip. A request still open after
# it is picked up by a later claim, which finds the loop closed and starts a
# continuation instead.
DELIVERY_EXIT_GRACE = timedelta(minutes=2)
# How long a delivery whose terminal advance did not land (every attempt of
# its retry policy went unanswered: an mctl-api outage) waits before it tries
# again. It never lets go while the loop runs: its execution would stay
# non-terminal, which the dispatcher's reconciliation cannot heal while this
# loop is RUNNING and which blocks every later request for the item.
DELIVERY_ADVANCE_RETRY_INTERVAL = timedelta(minutes=10)

# Stage 6.1 merge detection (ADR-006, #214): after implement, poll the PR's
# state until it merges/closes. Two cheap GitHub reads per poll — 15 min is
# responsive enough for a merge event nothing downstream reacts to in
# real time yet, and ~5.8k history events over the full 14-day deadline
# stays far under Temporal's 50k event limit. The deadline bounds the
# workflow's lifetime: a PR still open after two weeks is returned as-is
# (state="OPEN"), not waited on forever.
#
# It was 30 min until the `fast-shepherd-cadence` marker. The poll interval is
# the clock every other cadence in this module is expressed against, so
# halving it would silently halve four unrelated wall-clock intents; each of
# the poll COUNTS below is doubled in the same change to hold them steady, and
# both sets are carried in `_Cadence` so a reader sees they are one decision.
MERGE_POLL_INTERVAL = timedelta(minutes=15)
LEGACY_MERGE_POLL_INTERVAL = timedelta(minutes=30)
MERGE_WATCH_DEADLINE = timedelta(days=14)
# mctl-agents#404 v2: a complete 14-day watch at MERGE_POLL_INTERVAL is
# ~15,500 history events, crossing Temporal's default
# `limit.historyCount.warn` of 10,240. The watch hops via continue_as_new
# once its history is large enough -- normally decided by the server's own
# `workflow.info().is_continue_as_new_suggested()`, but the cluster's
# `temporal-dynamic-config` is empty, so this local floor is what makes the
# bound not depend on that ever being set. It matches Temporal's documented
# `limit.historyCount.suggestContinueAsNew` default (4096). A module
# constant, not a literal, so a test can force a hop deterministically by
# lowering it.
MERGE_WATCH_HISTORY_FLOOR = 4096
# How many times one merge watch is allowed to continue_as_new. The watch
# is observational, not correctness-critical, so crossing this must never
# fail the workflow -- past it the loop logs an error and keeps watching in
# the current run until MERGE_WATCH_DEADLINE, exactly as it did before this
# change existed.
MERGE_WATCH_MAX_HOPS = 16
# The implementer writes the pr: link into .status.yaml in the same commit
# that flips it to implemented, so the link should be visible on the first
# poll. A few polls of grace absorb gitops main lag; after that, a missing
# link means the status write failed — give up rather than poll for 14 days.
# Eight polls at the 15-min interval is the same ~2 h of grace four polls
# bought at 30 min.
PR_LOOKUP_GRACE_POLLS = 8
LEGACY_PR_LOOKUP_GRACE_POLLS = 4
PR_STATE_TIMEOUT = timedelta(minutes=2)
PR_STATE_RETRY_POLICY = RetryPolicy(maximum_attempts=5)

# Stage 6.1 review loop (#213): while the PR stays open, the workflow runs
# its OWN shepherd ticks instead of relying on the global cron (which
# narrows to a sweeper and skips slugs a running DevLoop owns — see
# run_shepherd._dev_loop_owns). Cadence: every poll ≈ 15 min.
#
# It was every 8th poll ≈ 4 h, justified as "each tick provisions a Hetzner
# volume, so hours not minutes (ADR-006 cost note)". That cost no longer
# exists: mctl-gitops `193dbe5c` (2026-09-05) took cwft-mctl-agents-shepherd
# off its workdir PVC, and no agent template has a volumeClaimTemplate any
# more — a tick is a clone and a few gh reads on an emptyDir.
#
# What a tick can still cost is an implementer run, and that is charged on the
# `address-review` decision, not on the tick: a tick that finds the review
# still pending decides `wait`, spends no MAX_REVIEW_ATTEMPTS slot and posts
# nothing. So this constant only sets how long a FINISHED review sits
# uncollected — four hours of dead time per round, on a review that lands in
# minutes.
#
# The first poll runs at t=0, before the loop's first sleep, so `% 1` alone
# would tick seconds after the PR opened — agy P2 round 1 caught that the
# "don't tick before claude review has even started" property the old `% 8`
# gave for free does NOT survive the change. `_watch_pr` therefore skips
# poll 1 explicitly; the first tick lands one interval (15 min) in. The guard
# is a no-op for LEGACY_CADENCE, whose first boundary is poll 8 either way,
# so it needs no patch marker.
SHEPHERD_TICK_EVERY_POLLS = 1
LEGACY_SHEPHERD_TICK_EVERY_POLLS = 8
# Capped: after SHEPHERD_TICKS_MAX active ticks the loop keeps watching
# passively — the shepherd itself flips review-stuck after
# MAX_REVIEW_ATTEMPTS address-review attempts, so a stuck PR must not tick
# for the rest of a fourteen-day watch. 96 ticks is ~24 h of active
# shepherding (it was 12 ticks ≈ 48 h at the old cadence): rounds that used
# to take a day now take an hour, and a PR still unresolved 24 h in needs a
# human, not a 97th tick.
SHEPHERD_TICKS_MAX = 96
LEGACY_SHEPHERD_TICKS_MAX = 12

# Ownership liveness is refreshed every 8th poll (~2 h at MERGE_POLL_INTERVAL),
# on its OWN cadence rather than riding the shepherd tick boundary.
#
# It cannot ride the ticks: those stop after SHEPHERD_TICKS_MAX (~24 h) while
# the watch runs up to MERGE_WATCH_DEADLINE (14 days), so a loop that is
# healthily watching a long-lived PR would stop proving liveness after two days
# and start reading as a crashed owner.
#
# It is not every poll either: a 14-day watch is ~1344 polls, and one extra
# activity per poll doubles the history of the longest-lived workflow in the
# system to record something that changes nothing. At 2 h against the 10 h
# liveness bound, four consecutive heartbeats can be lost before the owner
# looks dead.
LIFECYCLE_HEARTBEAT_EVERY_POLLS = 8
LEGACY_LIFECYCLE_HEARTBEAT_EVERY_POLLS = 4

# Consecutive ownership WRITES that answer neither "mine" nor "someone else's"
# before the loop drops back to the heartbeat cadence.
#
# Not a permanent give-up. A store that is down for three hours is usually back
# later in a fourteen-day watch, and a loop that stopped forever would hold no
# claim for the rest of it; retrying on the heartbeat boundary is the cheaper
# answer. Twelve polls is about three hours, and continuing to ask every poll
# for the remaining fortnight is ~1340 activities against an endpoint that is not
# answering, with the cron sweeper owning the PR throughout — the same outcome
# as before any of this existed.
#
# It applies to the acquire AND to the progress write. They are counted
# separately because they fail separately: a claimed loop whose /progress 500s
# still has a working acquire, and throttling one must not throttle the other.
# (Contrast the refusal a few lines into _track_ownership: "somebody else owns
# this" is an answer, not a failure to answer, so it is believed for far longer
# — LIFECYCLE_REFUSAL_BACKOFF_POLLS rather than one heartbeat boundary. It is
# not believed forever, though; see that constant for why.)
LIFECYCLE_UNKNOWN_WRITE_LIMIT = 12
LEGACY_LIFECYCLE_UNKNOWN_WRITE_LIMIT = 6

# Polls a refusal is believed for before the loop asks again.
#
# "Somebody else owns this" IS an answer, unlike the unanswered writes above —
# but it is an answer with an expiry date, and treating it as permanent is what
# this constant fixes. ADR-010 §5 force-releases a row whose owner stopped
# proving liveness, so the named owner can be reaped and the entity become
# claimable again inside the same watch. A loop holding a permanent refusal
# never finds out.
#
# 40 polls is 10 h at MERGE_POLL_INTERVAL, which is the liveness bound itself:
# a refusal must be re-tested at most one bound later, because past that point
# the owner it names may already be gone. Re-testing sooner would spend
# activities re-learning a fact that cannot yet have changed.
LIFECYCLE_REFUSAL_BACKOFF_POLLS = 40
LEGACY_LIFECYCLE_REFUSAL_BACKOFF_POLLS = 20

# Consecutive REFUSALS naming THE SAME owner before the loop stops asking for
# good.
#
# Refusals, not re-tests, and the distinction is the whole arithmetic:
# `_refuse_claim` counts the FIRST refusal too, so three of them is the initial
# refusal plus two re-tests — the loop asks twice more and then stops.
#
# Two re-tests is 20 h at LIFECYCLE_REFUSAL_BACKOFF_POLLS, i.e. two full
# liveness bounds during which one competitor kept holding and refreshing the
# row. That is not a reaped owner this loop could inherit; it is a live actor
# doing its job, which is the case the original permanent flag described
# correctly, and the reconciler is what resolves it.
#
# The owner identity is part of the condition: three refusals from three
# DIFFERENT owners is a busy entity, not a settled one, and restarts the count.
LIFECYCLE_REFUSAL_GIVE_UP = 3

# The owner type this workflow writes and reads back. Written once by
# `_ownership` and compared in two arms; as three separate literals they had to
# agree by inspection, and nothing failed if one of them changed.
OWNER_TYPE = "devloop-workflow"

# Consecutive failed HEARTBEATS before the loop stops believing it owns the
# entity. A SEPARATE constant, and a smaller number, because it counts a
# different thing.
#
# LIFECYCLE_UNKNOWN_WRITE_LIMIT's two counters advance once per POLL (15 min),
# so twelve of them is about three hours. `_unknown_heartbeats` advances
# inside the `% LIFECYCLE_HEARTBEAT_EVERY_POLLS` block, so it advances once
# per eight polls (~2 h) — and six of THOSE is twelve hours, past the bound the
# give-up exists to beat. Reusing the constant made the correction arrive after
# the event it is meant to precede: the reconciler force-releases at the 10 h
# liveness bound, and the loop went on believing it owned the row for another
# two.
#
# Three is the value the bound implies. ADR-010 derives 10 h as 2 x cadence —
# one missed tick survived — so three consecutive misses (~6 h) is inside it
# with room for the store to come back, and four (~8 h) is the last value that
# still is.
LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT = 3


@dataclass(frozen=True)
class _Cadence:
    """Every duration the watch loop measures in polls, as one value.

    The poll interval and the poll COUNTS are a single decision — halving the
    interval halves each count's wall-clock meaning — so they travel together
    rather than as seven module constants a future change can move one at a
    time. It is also what makes the `fast-shepherd-cadence` marker cheap: a
    replaying execution reads LEGACY_CADENCE and schedules exactly the timers
    and activities its history already records.
    """

    poll_interval: timedelta
    shepherd_tick_every_polls: int
    shepherd_ticks_max: int
    heartbeat_every_polls: int
    unknown_write_limit: int
    refusal_backoff_polls: int
    pr_lookup_grace_polls: int


CADENCE = _Cadence(
    poll_interval=MERGE_POLL_INTERVAL,
    shepherd_tick_every_polls=SHEPHERD_TICK_EVERY_POLLS,
    shepherd_ticks_max=SHEPHERD_TICKS_MAX,
    heartbeat_every_polls=LIFECYCLE_HEARTBEAT_EVERY_POLLS,
    unknown_write_limit=LIFECYCLE_UNKNOWN_WRITE_LIMIT,
    refusal_backoff_polls=LIFECYCLE_REFUSAL_BACKOFF_POLLS,
    pr_lookup_grace_polls=PR_LOOKUP_GRACE_POLLS,
)

# The cadence histories recorded before the `fast-shepherd-cadence` marker.
# Kept verbatim, not derived: an execution started under it must keep sleeping
# 30 min and ticking every 8th poll, because both the timer durations and the
# number of activities per poll are commands its history already holds.
LEGACY_CADENCE = _Cadence(
    poll_interval=LEGACY_MERGE_POLL_INTERVAL,
    shepherd_tick_every_polls=LEGACY_SHEPHERD_TICK_EVERY_POLLS,
    shepherd_ticks_max=LEGACY_SHEPHERD_TICKS_MAX,
    heartbeat_every_polls=LEGACY_LIFECYCLE_HEARTBEAT_EVERY_POLLS,
    unknown_write_limit=LEGACY_LIFECYCLE_UNKNOWN_WRITE_LIMIT,
    refusal_backoff_polls=LEGACY_LIFECYCLE_REFUSAL_BACKOFF_POLLS,
    pr_lookup_grace_polls=LEGACY_PR_LOOKUP_GRACE_POLLS,
)

# Activity bounds for ownership calls. Short and few: ownership is a
# coordination signal, and a loop must never stall on it.
LIFECYCLE_TIMEOUT = timedelta(seconds=30)
LIFECYCLE_RETRY_POLICY = RetryPolicy(maximum_attempts=3)

# Stages 6.2/6.3 (ADR-006, #215). After the PR merges, release-please cuts
# a release on the app repo, that dispatches mctl-gitops release-deploy,
# which bumps the image tag and ArgoCD auto-syncs within ~30 s. The loop
# only watches; it never drives any of it.
#
# Two separate waits, because they fail for different reasons. A release
# that never appears means release-please had nothing to release (a
# docs-only or non-conventional merge) — common, and settled within a few
# minutes, so a short window. A release that appeared but has not gone
# Healthy is a real rollout in progress: image build, gitops commit, sync,
# rollout, probes — minutes, occasionally much longer under a busy runner,
# so a wider window before giving up as "unverified".
RELEASE_LOOKUP_DEADLINE = timedelta(minutes=20)
RELEASE_POLL_INTERVAL = timedelta(minutes=2)
DEPLOY_VERIFY_DEADLINE = timedelta(minutes=45)
DEPLOY_POLL_INTERVAL = timedelta(minutes=1)
# A release can introduce a brand-new ArgoCD application, which takes a
# few reconciliations to appear. Wait that out, but not the full deadline:
# past this, a name that resolves to nothing is a wrong name.
NEW_APP_GRACE_POLLS = 5
DEPLOY_READ_TIMEOUT = timedelta(minutes=2)
DEPLOY_READ_RETRY_POLICY = RetryPolicy(maximum_attempts=5)

# Stage 6.4 (ADR-006, #216). After the rollout is observed, watch that
# service for incidents for a bounded window, then finish. Long enough for
# a bad rollout to announce itself through alert `for:` durations and the
# first real traffic; short enough that the loop still ends, and that what
# it reports is plausibly about THIS deploy rather than the day's news.
INCIDENT_WATCH_WINDOW = timedelta(minutes=30)
INCIDENT_POLL_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True)
class IssueRef:
    issue_url: str
    #: The canonical WorkItem this execution correlates to (mctlhq/mctl-
    #: agents#267). Defaulted so a history recorded before this field
    #: existed still deserializes — see workflow.patched's convention at
    #: `dev_loop.py:864-869`. None means the caller started this loop the
    #: old way, issue-url-only; the `resume` signal and `work_context`
    #: query still work in that case, just with no seeded first execution.
    work_item_id: str | None = None
    # mctl-agents#404 v2: carries a merge watch's resume record across a
    # continue_as_new boundary. Defaulted and always None on an ordinary
    # start, so `run`'s decoded argument list is `[IssueRef]` on EVERY
    # start -- external or continued (see design.md's "argument-shape
    # decision"; a second parameter on `run` was rejected for exactly this
    # reason). `MergeWatchResume` is defined further down this module
    # (after ImplementExecutionState, which one of its fields needs); that
    # is fine under `from __future__ import annotations` -- the annotation
    # is a string until something resolves it, and by then the whole
    # module has finished loading.
    resume: MergeWatchResume | None = None
    #: The mctl-api execution request (`xr_...`) this loop was dispatched
    #: for (mctlhq/mctl-agents#461). Set only by the dispatcher, always with
    #: `work_item_id`; None on every other start, and on every history
    #: recorded before this field existed, which therefore never enters the
    #: dispatched path. The loop learns its canonical `we_` execution from
    #: the fulfilled request, not from this input: see
    #: `activities/execution_requests.py`.
    execution_request_id: str | None = None


@dataclass(frozen=True)
class DeployObservation:
    """Outcome of watching this merge's release reach the cluster.

    ``outcome`` is one of:

    - ``healthy``    — the app reports Synced/Healthy on the released tag
    - ``unverified`` — the deadline passed first; ``detail`` says what the
      last read showed. NOT a workflow failure: the deploy may still be
      progressing, and this loop never rolls anything back
    - ``no-release`` — release-please cut nothing for this merge (docs-only
      or non-conventional commits); nothing to observe, and not a fault
    - ``no-target``  — the repo's release deploys no application (it only
      bumps cluster templates, or has no release-please dispatch)
    """

    outcome: str
    team: str | None = None
    app: str | None = None
    release_tag: str | None = None
    image_tag: str | None = None
    health: str | None = None
    sync_status: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class IncidentWatch:
    """Incidents seen for the deployed service after the rollout.

    Correlation is service + time window only — incidents carry no link to
    a release, PR or commit — so this is evidence for a human, not a
    causal claim and not a trigger for anything. ``watched=False`` means
    the stage did not run (no deploy target, or nothing released).
    """

    watched: bool
    service: str | None = None
    window_minutes: int = 0
    since: str | None = None
    incidents: list[Incident] = field(default_factory=list)
    # The store had more incidents than the query cap returned on at least
    # one poll — the list below is a sample, not the whole window.
    truncated: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class LifecycleClaim:
    """What this execution believes about the ownership row it held.

    Exists to give `_owned_entity_id` a reader OUTSIDE `_watch_pr`. The
    cleanup in that method's `finally` refuses to clear the claim when its own
    terminal/release write failed — correctly, because clearing it would record
    that the loop let go of a row the store still shows as active. But the
    workflow returns on the next line and the field is never read again, so no
    test could turn that guard red, and the code said so in a comment. A guard
    that can only pass by not running is not a guard.

    Temporal serves queries against COMPLETED executions, so the terminal value
    of these fields is observable after the watch ends — which is exactly when
    the question "did this loop abandon a live row?" is asked.
    """

    entity_id: str = ""
    epoch: int = 0
    #: The last relinquishing write attempted: "terminal", "release", or "".
    last_op: str = ""
    #: Whether that write landed. False with a non-empty `last_op` is the
    #: abandonment: the store still shows the row active and this loop is gone.
    last_op_landed: bool = False
    abandoned: bool = False


@dataclass(frozen=True)
class HumanInputState:
    """What `human_input_state` reports. Every field defaulted, so a loop
    that never entered `WAITING_FOR_INPUT` (no grant, or none requested)
    returns the all-empty/zero shape rather than None (mctlhq/mctl-
    agents#333, ADR 013) — `mctl-api#261` and the portal can poll this
    query unconditionally.

    `state` is one of RUNNING / WAITING_FOR_INPUT / INPUT_TIMED_OUT and is
    never equal to WAITING_FOR_APPROVAL — the two durable gates answer
    different questions ("is this authorized?" vs. "what is the answer?")
    and must stay distinguishable."""

    state: str = RUNNING
    request_id: str = ""
    request_hash: str = ""
    question_hash: str = ""
    expires_at: str = ""
    round: int = 0
    resume_count: int = 0
    # How many delivered payloads this execution rejected (invalid, dropped
    # while not waiting, or over the queue cap) — the observable counterpart
    # of `_human_input_rejected_count`, so a surface can see its answers are
    # being refused instead of silently swallowed.
    rejected_count: int = 0
    # The deadline the wait ACTUALLY uses: `expires_at` bounded against the
    # workflow clock at MAX_REQUEST_TTL_SECONDS. Differs from `expires_at`
    # exactly when a model-written far-future timestamp was clamped, so a
    # poller (and the clamp's test) can see the bound that is really in
    # force.
    effective_deadline: str = ""


@dataclass(frozen=True)
class HumanInputOutcome:
    """What `_await_human_input` returns when a request was actually seen
    (None means "no pending request, proceed exactly as today"). Carries
    just enough to (a) resubmit the continuation step with a
    `human_input_response` param and (b) record the outcome in
    `DevLoopResult` — never the question or the raw surface transcript."""

    outcome: str = ""  # "answered" | "timed_out" | "abandoned"
    request_id: str = ""
    request_hash: str = ""
    # `Any`, not bare `object`: temporalio's value_to_type special-cases Any
    # (returned as-is) but has no handler for object, so a recorded outcome
    # would fail to decode on handle.result() / replay with object here.
    value: Any = None
    respondent: str = ""
    surface: str = ""
    received_at: str = ""
    round: int = 0
    resume_count: int = 0


@dataclass(frozen=True)
class ResumeRejection:
    """A `resume` signal this workflow declined to apply (mctlhq/mctl-
    agents#267). Recorded, never silent: a caller polling `work_context`
    must be able to see that a resume was refused and why, rather than
    inferring it from the absence of a new execution."""

    execution_id: str = ""
    work_item_id: str = ""
    reason: str = ""
    #: The execution request (`xr_...`) a refused DELIVERY carried
    #: (mctlhq/mctl-agents#461); "" for a `resume` signal.
    execution_request_id: str = ""


@dataclass(frozen=True)
class ResumeDelivery:
    """A `resume` execution request, as the dispatcher delivers it onto a
    live loop through the `accept_execution_request` Update
    (mctlhq/mctl-agents#461). The request id, not a `we_`: the execution is
    minted by the fulfil that follows an accepted delivery, and the loop
    binds it itself. Provenance is the request's own: the surface it was
    made on, and the human it was made by (mctl-api accepts a request only
    from a person, or a surface relaying for its linked person)."""

    execution_request_id: str
    work_item_id: str
    surface: str = ""
    actor_kind: str = ""
    actor_id: str = ""
    #: The request's kind, `start` or `resume` (DELIVERY_KINDS). Defaulted to
    #: `resume`, the only kind delivered before #461 option A, so a payload
    #: recorded then still decodes to what it was.
    kind: str = DELIVERY_KIND_RESUME


@dataclass(frozen=True)
class OpenDelivery:
    """An accepted delivery whose execution has not ended yet: carried
    across continue-as-new, so a loop never forgets a request it said yes
    to."""

    delivery: ResumeDelivery
    #: Whether accepting it changed the surface or the actor (and so cleared
    #: the approval). Decided at acceptance, against the provenance then.
    surface_transition: bool = False
    #: The terminal phase its execution was decided to end in, while that
    #: advance has not landed yet ("" before the decision). A delivery with
    #: one only re-attempts the advance, in this run or the next.
    pending_phase: str = ""
    #: What `_end_delivery` records once that advance lands.
    pending_refusal: str = ""


@dataclass(frozen=True)
class AbandonState:
    """Whether an operator told this execution to end early, and why.

    mctl-agents#420: a small, dedicated query rather than folding this into
    `LifecycleClaim` -- that dataclass already uses `abandoned` for a
    different question (did this loop let go of a lifecycle-ownership row it
    still held), and conflating the two would make one field answer two
    unrelated questions depending on which caller is asking.
    """

    abandoned: bool = False
    reason: str = ""


@dataclass(frozen=True)
class WorkContextState:
    """The `work_context` query's answer: enough for a trace view to
    correlate every execution of one `WorkItem`, without exposing anything
    an authorization decision could read (ADR 009 sec. 5 boundary, extended
    by ADR 011)."""

    work_item_id: str = ""
    execution_id: str = ""
    execution_sequence: int = 0
    executions: tuple[ExecutionRef, ...] = ()
    last_surface: SurfaceRef = field(default_factory=SurfaceRef)
    last_actor: ActorRef = field(default_factory=ActorRef)
    resume_rejections: tuple[ResumeRejection, ...] = ()


@dataclass(frozen=True)
class DevLoopResult:
    investigate: WorkflowResult
    # None if approval was never signalled, investigate failed, or the
    # approve flip failed (see `approve` below to tell those apart).
    implement: WorkflowResult | None
    # None on pre-atomic-approve histories (legacy manual flip) and when the
    # workflow never reached the approve stage. Defaulted so results recorded
    # before this field existed still deserialize.
    approve: WorkflowResult | None = None
    # Stage 6.1 merge detection (ADR-006, #214): the last PR state observed
    # by the post-implement polling loop. None on pre-merge-detection
    # histories and when implement never produced a PR to watch; a PRState
    # with state="OPEN" means the watch deadline expired with the PR still
    # open (the workflow does not wait forever).
    pr: PRState | None = None
    # Stages 6.2/6.3 (ADR-006, #215): what happened to the release this
    # merge produced. None on histories predating the stage and whenever
    # the PR did not merge. See DeployObservation for the outcomes.
    deploy: DeployObservation | None = None
    # Stage 6.4 (ADR-006, #216): incidents raised against the deployed
    # service during the watch window. None when the stage did not run.
    incidents: IncidentWatch | None = None
    # The durable clarification outcome (mctlhq/mctl-agents#333, ADR 013).
    # None on histories predating the stage and whenever no request was ever
    # seen (no capability grant, or the agent never asked). Defaulted so
    # results recorded before this field existed still deserialize.
    human_input: HumanInputOutcome | None = None
    # mctl-agents#420: why this execution ended, when it ended for a reason
    # other than running the pipeline to the end -- "abandoned: ...",
    # "source issue closed ..." (pre- or mid-approval-park), or "approval
    # wait expired". Empty on the full-pipeline path. Defaulted so results
    # recorded before this field existed still deserialize.
    ended: str = ""


async def _resolve(agent: str) -> ResolvedRelease | None:
    return await workflow.execute_activity(
        resolve_agent_release,
        args=[agent, ENVIRONMENT],
        start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
        retry_policy=FAST_ACTIVITY_RETRY_POLICY,
    )


def _first_string(args: tuple[object, ...], key: str) -> str | None:
    """Best-effort extraction of a signal payload's message string.

    Mirrors `approve`'s own defensive parse: a bare non-empty string is
    used directly, a dict is probed for ``key``, and anything else -- or
    no args at all -- yields None. Signal handlers must never raise on an
    unexpected payload shape.
    """
    for arg in args:
        if isinstance(arg, dict):
            candidate = arg.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        elif isinstance(arg, str) and arg:
            return arg
    return None


async def _read_issue_state(issue: IssueRef) -> IssueState | None:
    """Read the source issue's current state, failing open on error.

    Same fail-open rule the `stale-issue-admission` gate below applies: an
    `ActivityError` after retries (a GitHub blip) must delay the caller's
    decision by one interval rather than wedge or fail a workflow that only
    wants to know whether to keep waiting.
    """
    issue_parts = parse_issue_url(issue.issue_url)
    issue_repo = f"{issue_parts.owner}/{issue_parts.repo}"
    issue_number_int = int(issue_parts.number)
    try:
        return await workflow.execute_activity(
            get_issue_state,
            args=[issue_repo, issue_number_int],
            start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
            retry_policy=FAST_ACTIVITY_RETRY_POLICY,
        )
    except ActivityError:
        workflow.logger.warning(
            "get_issue_state failed after retries for %s#%s -- proceeding "
            "without this issue-state check",
            issue_repo,
            issue_number_int,
        )
        return None


async def _run_cwft(
    operation: str, params: dict[str, str], *, step_timeout: timedelta = SDK_STEP_TIMEOUT
) -> WorkflowResult:
    # ADR-008 step 4: every CWFT submit moves to the execution queue. This
    # one funnel covers all four dev-loop submits (investigate, approve,
    # implement, the in-loop shepherd tick), so the check sits here and is
    # evaluated once per call, before the command is scheduled — never
    # inside a branch that has already scheduled one (#223).
    #
    # Migration is by ATTRITION, not mid-life. patched() memoizes per id
    # (temporalio/worker/_workflow_instance.py), so a loop whose history
    # lacks the marker returns False here for the rest of its life — every
    # one of its Argo polls stays on the control queue until it ends, up to
    # MERGE_WATCH_DEADLINE. Only executions started after this deploy route
    # to exec. Pinned by tests/test_patch_memoization.py; the opposite was
    # asserted in ADR-008 until agy caught it on #282.
    #
    # ADR-008 D7 (#395): the implement submit alone goes one step further,
    # to the admission queue. Its slot limit is the number of implementer
    # runs allowed to exist at once; with no free slot this activity stays
    # Scheduled in Temporal and no Argo workflow is created. The marker is
    # consulted only for that operation so other operations' histories do
    # not record a decision they never make, and it nests under exec-queue
    # in intent: a loop new enough to route implement is new enough to
    # route everything else to exec.
    if operation == IMPLEMENTATION_OPERATION and workflow.patched("implement-queue"):
        return await workflow.execute_activity(
            submit_and_wait,
            SubmitAndWaitInput(operation=operation, params=params),
            task_queue=IMPLEMENTATION_TASK_QUEUE,
            start_to_close_timeout=step_timeout,
            heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
            retry_policy=SDK_STEP_RETRY_POLICY,
        )
    if workflow.patched("exec-queue"):
        return await workflow.execute_activity(
            submit_and_wait,
            SubmitAndWaitInput(operation=operation, params=params),
            task_queue=EXECUTION_TASK_QUEUE,
            start_to_close_timeout=step_timeout,
            heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
            retry_policy=SDK_STEP_RETRY_POLICY,
        )
    # Unpatched replay branch, kept verbatim: no task_queue argument at all,
    # which schedules onto the workflow's own queue exactly as the recorded
    # history did.
    return await workflow.execute_activity(
        submit_and_wait,
        SubmitAndWaitInput(operation=operation, params=params),
        start_to_close_timeout=step_timeout,
        heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
        retry_policy=SDK_STEP_RETRY_POLICY,
    )


def _target_repo(issue: IssueRef) -> str:
    return parse_issue_url(issue.issue_url).repo


async def _record(
    agent: str,
    release: ResolvedRelease | None,
    result: WorkflowResult,
    target_repo: str,
    *,
    outcome: str | None = None,
    pre_start_reason: str | None = None,
) -> None:
    # A release with no image_ref means resolve_agent_release found nothing
    # to pin, so the CWFT ran its own baked-in default image instead (see
    # activities/registry.py) — record that as "no pinned version", not as
    # release.version, or the audit trail would claim a specific registry
    # version produced this result when it was never actually passed to the
    # run.
    pinned = release if release and release.image_ref else None

    # Best-effort: this is an audit-trail write, not the real work (the CWFT
    # this records already ran to completion by the time this is called).
    # Letting a persistent failure here fail the whole workflow would make
    # an mctl-api outage on this one endpoint block wait_condition/approval
    # indefinitely for work that already succeeded — worse than a missing
    # execution record.
    try:
        await workflow.execute_activity(
            record_execution,
            ExecutionRecord(
                temporal_workflow_id=workflow.info().workflow_id,
                agent=agent,
                environment=ENVIRONMENT,
                version=pinned.version if pinned else "",
                image_ref=pinned.image_ref if pinned else "",
                target_repo=target_repo,
                argo_workflow_name=result.workflow_name,
                phase=result.phase,
                outcome=outcome or "",
                pre_start_reason=pre_start_reason or "",
            ),
            start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
            retry_policy=FAST_ACTIVITY_RETRY_POLICY,
        )
    except ActivityError:
        workflow.logger.warning(
            "record_execution failed after retries for agent=%s argo_workflow=%s — "
            "continuing without a durable execution record for this step",
            agent,
            result.workflow_name,
        )


def _at_or_after(later: str | None, earlier: str) -> bool:
    """Is ``later`` at or after ``earlier``, comparing real instants?

    Not a string compare: GitHub emits whole seconds ("...:01Z") while
    ArgoCD may emit fractional ones ("...:01.123Z"), and "." sorts BEFORE
    "Z", so the fractional value would read as older than the very second
    it belongs to (agy P2). An unparseable value is treated as "not yet",
    which only ever delays a verdict.
    """
    if not later:
        return False
    try:
        return _as_utc(later) >= _as_utc(earlier)
    except (ValueError, TypeError):
        # TypeError as well as ValueError: this runs inside the workflow
        # loop, and an unhandled exception there is retried by Temporal
        # forever on identical input — a wedged state machine rather than
        # a wrong answer (agy P1). _as_utc already normalises the
        # naive/aware mix that would raise it, so this is the backstop.
        workflow.logger.warning("uncomparable timestamps %r and %r", later, earlier)
        return False


def _as_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp as UTC, tolerating a missing offset.

    A payload without an offset would otherwise parse naive, and
    comparing naive to aware raises TypeError. These are all UTC in
    practice — GitHub and ArgoCD both emit Zulu — so an absent offset is
    read as UTC rather than refused.

    A non-string value raises TypeError rather than the AttributeError
    ``.replace()`` would give, so callers' ``except (ValueError,
    TypeError)`` covers it. Belt-and-braces only: agy read this as a live
    P1 (an int epoch from the API wedging the workflow), but a non-str
    cannot actually get here — ``deploy_state`` isinstance-guards both
    ``updatedAt`` and ``published_at`` at the HTTP boundary, and Temporal's
    own converter refuses to decode a non-str into these ``str`` fields
    before the workflow ever sees them. This keeps the helper total for
    its declared contract rather than relying on those two distant checks.
    """
    if not isinstance(value, str):
        raise TypeError(f"expected an ISO-8601 string, got {type(value).__name__}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_transient(exc: ActivityError) -> bool:
    """Is this activity failure a known-transient one, or a bug?

    The read activities wrap every expected failure in
    ProposalListingError, and an activity timeout is transport by
    definition. Anything else escaping an activity is an unexpected defect
    — retrying THAT for the rest of a long deadline would silently mask
    it, which is the lesson _watch_pr already learned (agy P2, round 3 on
    #224).
    """
    cause = exc.cause
    return isinstance(cause, TemporalTimeoutError) or (
        isinstance(cause, ApplicationError) and cause.type == "ProposalListingError"
    )


async def _shepherd_is_pinned() -> bool:
    """Does the shepherd resolve to a pullable image right now?

    A question, not a gate: unlike the investigator and implementer, a
    missing shepherd pin must not fail the loop (see _watch_pr). Answers
    True when unpatched, so histories recorded before this marker keep
    their behaviour — the patch check comes BEFORE the resolve because
    they replay command-for-command, and an unconditional activity here
    would be a command in a position they never recorded.

    A registry outage answers False rather than propagating: this runs
    inside the watch, i.e. after implement already succeeded, and letting
    an ActivityError out here would fail the whole loop over a read — the
    exact fail-open breach this function was written to avoid (agy P2).
    Declining is also the safe direction, since the cron sweeper picks up
    any proposal the loop does not claim.
    """
    if not workflow.patched("registry-required"):
        return True
    try:
        release = await _resolve("shepherd")
    except ActivityError as exc:
        workflow.logger.warning(
            "could not resolve the shepherd release (%r) — declining the "
            "in-loop claim and leaving it to the cron sweeper",
            exc.cause,
        )
        return False
    return release is not None and bool(release.image_ref)


def _require_release(agent: str, release: ResolvedRelease | None) -> ResolvedRelease | None:
    """Enforce that ``agent`` resolves to a pinned image, once patched (A4).

    Until now an unresolvable agent silently fell back to whatever image
    the ClusterWorkflowTemplate had baked in — the phase-5 compatibility
    shim, which existed because the registry was not yet authoritative.
    It is now: every manifest is published and promoted by the release
    pipeline (tools/publish_agent_release.py), so a missing row is a real
    misconfiguration and running an unknown image instead of saying so
    defeats the point of pinning.

    The check lives here, not in resolve_agent_release: the activity's
    signature and return value must stay identical for histories recorded
    before this marker, and changing what it raises would break their
    replay just as surely as changing its arity would (the lesson from
    #223).
    """
    if not workflow.patched("registry-required"):
        return release
    if release is None or not release.image_ref:
        raise ApplicationError(
            f"no released image for {agent} in {ENVIRONMENT} — the agent registry must "
            "pin every agent this loop runs; publish and promote it "
            "(tools/publish_agent_release.py) and retry",
            type="AgentReleaseMissing",
            non_retryable=True,
        )
    return release


def _drain_tick(tick_task: asyncio.Task[None], service: str, slug: str) -> None:
    """Retrieve a finished tick's exception so it is never swallowed.

    _shepherd_tick catches its own failures, so this should find nothing —
    but a task whose exception is never retrieved disappears silently
    (with only an asyncio "exception was never retrieved" warning), and
    the watch loop drops the reference on the next poll. Reading it is the
    cheap way to guarantee that cannot happen.
    """
    if tick_task.cancelled():
        return
    exc = tick_task.exception()
    if exc is not None:
        workflow.logger.error(
            "in-loop shepherd tick for %s/%s ended with an unretrieved %s: %r",
            service,
            slug,
            type(exc).__name__,
            exc,
        )


@dataclass(frozen=True)
class ImplementExecutionState:
    """What THIS workflow knows about its implement step, for #389.

    Deliberately only the orchestrator's half. Whether the activity is
    still Scheduled (waiting on capacity), admitted, submitted or running
    is Temporal's and Argo's knowledge — `describe_workflow_execution`'s
    pending activity plus its heartbeat details carry it — and a query
    that guessed at it would be a second runtime-state surface.
    """

    # "" before the implement step; "implementer" from the first submit.
    stage: str = ""
    # When the current implement submit was scheduled. Admission wait is
    # measured from here; a pre-start requeue resets it.
    queued_at: str | None = None
    prestart_requeues: int = 0
    # success | pre_start | execution | finalization, once the step ended.
    outcome: str | None = None
    # Set only alongside outcome == "pre_start": lock_wait, unscheduled or
    # unknown (#418, implement_outcome.py). None otherwise.
    pre_start_reason: str | None = None


@dataclass(frozen=True)
class MergeWatchResume:
    """Everything a continue_as_new'd merge watch needs to pick up where the
    previous run left off (mctl-agents#404 v2).

    Every field is defaulted -- `IssueRef.resume` is None on an ordinary
    start, and this dataclass is only ever built by `_watch_pr` (the watch
    cursor, the decisions already taken, and the lifecycle claim) and then
    filled in by `run`/`_resume_merge_watch` (the prior stage results). All
    flat JSON-serialisable data, on the order of a kilobyte.
    """

    # --- Watch cursor: where _watch_pr left off. ---
    service: str = ""
    slug: str = ""
    # ISO-8601 Z string, not a datetime -- this is the ABSOLUTE merge-watch
    # deadline the FIRST run computed. A continued run parses it with
    # _as_utc and must never recompute `workflow.now() +
    # MERGE_WATCH_DEADLINE`, or the watch would restart its 14-day clock on
    # every hop instead of staying bounded from the first poll.
    deadline: str = ""
    last_pr: PRState | None = None
    polls_without_pr: int = 0
    poll_index: int = 0
    shepherd_ticks: int = 0

    # --- Decisions already taken. Carried rather than re-derived from the
    # patch markers at the top of _watch_pr, so a continued run cannot
    # adopt a different cadence/shepherd/ownership behaviour than the run
    # it replaces just because a marker happened to be re-evaluated. ---
    fast_cadence: bool = True
    shepherd_in_loop: bool = False
    concurrent_ticks: bool = False
    track_ownership: bool = False

    # --- Lifecycle claim: the ownership row this loop holds, or doesn't.
    # Must survive the boundary so a hop is invisible to the reconciler and
    # the cron sweeper. The claim's owner id is workflow_id, which is
    # stable across continue_as_new -- but every OTHER field describing the
    # claim lives only in instance state, set in __init__ and mutated
    # in-loop, and would silently reset to __init__'s empty values on a
    # fresh run if not carried here. ---
    owned_entity_id: str = ""
    owner_epoch: int = 0
    owned_head_sha: str = ""
    poll_index_for_heartbeat: int = 0
    claim_refused: bool = False
    claim_refused_until_poll: int = 0
    refused_by_type: str = ""
    refused_by_id: str = ""
    refusals_observed: int = 0
    unknown_acquires: int = 0
    unknown_progress: int = 0
    unknown_heartbeats: int = 0
    proposal_ref: str = ""
    policy_ref: str = ""
    last_lifecycle_op: str = ""
    last_lifecycle_op_landed: bool = False
    # Deliberately a DIFFERENT question from `abandoned` below -- whether
    # the ownership ROW was let go without its relinquishing write landing
    # (see AbandonState's docstring, and LifecycleClaim.abandoned above),
    # not whether an operator sent the `abandon` signal. Conflating the two
    # here would lose the same distinction those dataclasses keep apart.
    claim_abandoned: bool = False

    # --- Abandon state. New versus the superseded design: without these a
    # hop silently resets the `abandon` signal, and `abandon_state` would
    # answer False in the continued run while an operator believes the
    # execution is ending. ---
    abandoned: bool = False
    abandon_reason: str | None = None

    # --- Work-context binding (mctlhq/mctl-agents#267, ADR 011). The
    # `work_context` query's state: without these a hop resets the query
    # to empty for the rest of the watch, makes `resume`'s
    # `work-item-mismatch` guard vacuous (an empty binding accepts a
    # foreign work_item_id), and forgets `_seen_execution_ids`, so a
    # retrying surface callback's duplicate execution_id forks a new
    # execution instead of being the documented no-op.
    # `seen_execution_ids` is carried sorted, not as a set — set iteration
    # order depends on str hash randomisation, which a workflow must not
    # let into its recorded state. ---
    work_item_id: str = ""
    executions: tuple[ExecutionRef, ...] = ()
    seen_execution_ids: tuple[str, ...] = ()
    current_surface: SurfaceRef = field(default_factory=SurfaceRef)
    current_actor: ActorRef = field(default_factory=ActorRef)
    resume_rejections: tuple[ResumeRejection, ...] = ()
    # The "one resume already pending" window (`resume`'s
    # `resume-already-pending` rejection). Only `approve` ever closes it,
    # so a hop that dropped it would accept an overlapping resume the
    # previous run had promised to reject.
    resume_pending: bool = False
    # Execution requests delivered onto this loop (mctlhq/mctl-agents#461).
    # `accepted_request_ids` is every request id any run of this loop said
    # yes to, sorted (as `seen_execution_ids`): a re-sent Update reaches the
    # NEXT run with a fresh Temporal update registry, so this set is what
    # keeps it a no-op there. `open_deliveries` are the accepted ones still
    # waiting for their fulfilment; the next run binds them.
    accepted_request_ids: tuple[str, ...] = ()
    open_deliveries: tuple[OpenDelivery, ...] = ()

    # --- Prior stage results. A continued run never re-runs investigate,
    # approve or implement, so the final DevLoopResult can only report
    # their outcomes if they are carried here. Filled in by `run` /
    # `_resume_merge_watch` just before continue_as_new, not by
    # `_watch_pr` itself, which has no view of them. ---
    investigate: WorkflowResult | None = None
    implement: WorkflowResult | None = None
    approve: WorkflowResult | None = None
    implement_state: ImplementExecutionState = field(default_factory=ImplementExecutionState)
    approver: str | None = None
    # The clarification round's recorded outcome (mctl-agents#333), a prior
    # stage result like the three above: human input happens before the
    # watch ever starts, so a hop that dropped it would erase the round
    # from the final DevLoopResult.
    human_input: HumanInputOutcome | None = None

    # --- Bookkeeping: how many times THIS watch has already hopped, so
    # MERGE_WATCH_MAX_HOPS bounds the whole watch, not just one run. ---
    hops: int = 0


@dataclass(frozen=True)
class _WatchOutcome:
    """What `_watch_pr` decided when its loop ended.

    Either the watch is genuinely done (`resume` is None, `last` is its
    final observed PRState-or-None, exactly what the pre-#404-v2 return
    value meant) or it is hopping (`resume` carries everything the next
    run needs). A plain return value, never serialized -- `_watch_pr`
    returning a decision instead of raising `continue_as_new` itself is the
    load-bearing structural choice here: raising from inside would unwind
    through the `try`/`finally` below and release the lifecycle-ownership
    claim on every hop (see the `finally` block's `hopping` guard).
    """

    last: PRState | None
    resume: MergeWatchResume | None = None


@workflow.defn
class DevLoopWorkflow:
    @workflow.init
    def __init__(self, issue: IssueRef) -> None:
        # The start input, read here rather than in `run` (mctlhq/mctl-
        # agents#461 option A): the dispatcher starts this loop with
        # Update-with-Start, and the `accept_execution_request` Update that
        # comes with the start is validated and handled in the first
        # activation, before `run` has executed a line. The request this run
        # was started for is its own and needs none of `run`'s state; a
        # continued run's accepted requests come with its resume record.
        # Nothing here schedules a command, so no history changes shape.
        self._start_request_id = issue.execution_request_id or ""
        self._start_work_item_id = issue.work_item_id or ""
        # The engine ref this loop's own dispatched execution was bound under
        # (`_bind_dispatched_execution`); "" until then, and for every loop
        # that was not dispatched.
        self._own_engine_ref = ""
        self._implement_state = ImplementExecutionState()
        # Which cadence this execution runs at. Bound for real in _watch_pr,
        # once, off the `fast-shepherd-cadence` marker; CADENCE here so every
        # path that reads it before the watch starts (and every unit test that
        # drives a method directly) sees the current numbers rather than None.
        self._cadence = CADENCE
        self._approved = False
        self._approver: str | None = None
        self._shepherd_in_loop = False
        # Lifecycle ownership (mctlhq/.github#57). Set once the PR is known
        # and this execution has positively claimed it; empty means this loop
        # owns nothing and the cron sweeper is the owner.
        self._owned_entity_id = ""
        self._owner_epoch = 0
        self._owned_head_sha = ""
        self._poll_index_for_heartbeat = 0
        # Set once another actor is known to own this PR, so the loop stops
        # re-asking on every poll. Believed until `_claim_refused_until_poll`
        # rather than forever: the named owner can be force-released inside
        # this watch (ADR-010 §5), after which the entity is claimable again.
        self._claim_refused = False
        self._claim_refused_until_poll = 0
        # Who refused, and how many consecutive re-tests have named them. The
        # identity is what separates "a competitor is working" from "a row I
        # could have inherited three times over".
        self._refused_by_type = ""
        self._refused_by_id = ""
        self._refusals_observed = 0
        self._unknown_acquires = 0
        self._unknown_progress = 0
        self._unknown_heartbeats = 0
        self._proposal_ref = ""
        self._policy_ref = ""
        # The last relinquishing write and whether it landed, so the
        # abandonment branch in _watch_pr's finally has a reader.
        self._last_lifecycle_op = ""
        self._last_lifecycle_op_landed = False
        self._claim_abandoned = False
        # Durable clarification (mctlhq/mctl-agents#333, ADR 013). Raw signal
        # payloads, processed in delivery order inside _await_human_input —
        # never touched by the signal handler itself, so `human_input_response`
        # can never raise and never needs to know whether the payload it just
        # received will turn out to be valid.
        self._input_responses: list[object] = []
        self._human_input_state = HumanInputState()
        # question_hash values already answered in THIS execution, so a
        # continuation step that re-asks (despite being told the ambiguity is
        # resolved) is ignored rather than re-entering WAITING_FOR_INPUT.
        self._resolved_question_hashes: set[str] = set()
        self._human_input_resume_count = 0
        self._human_input_rejected_count = 0
        # Work-context resume state (mctlhq/mctl-agents#267, ADR 011).
        self._work_item_id = ""
        self._executions: list[ExecutionRef] = []
        self._seen_execution_ids: set[str] = set()
        # True from an accepted, surface/actor-changing resume until the
        # next `approve()` — see `resume`'s docstring for why this is the
        # window "already pending" means, rather than true concurrency
        # (Temporal serialises signal delivery to one workflow instance).
        self._resume_pending = False
        self._resume_rejections: list[ResumeRejection] = []
        self._current_surface = SurfaceRef()
        self._current_actor = ActorRef()
        # mctl-agents#420: set by the `abandon` signal. Observed by both long
        # waits (the approval park and _watch_pr's merge watch) so an operator
        # can end an execution gracefully without Temporal `terminate`, which
        # would skip _watch_pr's `finally` and its lifecycle-ownership release.
        self._abandoned = False
        self._abandon_reason: str | None = None
        # Resume delivery onto this live loop (mctlhq/mctl-agents#461). See
        # `accept_execution_request`. `_issue_url` and `_initialized` are set
        # by `run` before its first await (after the rehydration, on a
        # continued run), so the validator can tell a loop that has its state
        # from one still in the activation that starts it.
        self._issue_url = ""
        self._initialized = False
        self._accepted_request_ids: set[str] = set(issue.resume.accepted_request_ids) if issue.resume else set()
        self._open_deliveries: dict[str, OpenDelivery] = {}
        self._delivery_tasks: dict[str, asyncio.Task[None]] = {}
        # Set once the implement step is submitted: no approval gate is left
        # ahead, so a delivered resume has no decision left to wait for.
        self._gates_passed = False
        # Set by `_settle_deliveries` right before this run ends (`_exiting`)
        # or continues as new (`_hopping`).
        self._exiting = False
        self._hopping = False

    @workflow.query
    def holds_execution_request(self, request_id: str) -> bool:
        """Did THIS run (or a run it continued from) take `request_id`?

        The dispatcher's reconciliation asks it before failing a stranded
        `<loop>#<request>` execution of a RUNNING loop (#461 option A): the
        loop id is the issue's and shared by every run, so "the loop is
        running" no longer proves the run that holds the execution is. A
        later run that never took the request never ends its execution."""
        return request_id == self._start_request_id or request_id in self._accepted_request_ids

    @workflow.query
    def implement_execution(self) -> ImplementExecutionState:
        """The orchestrator's view of the implement step (#389, #395)."""
        return self._implement_state

    @workflow.query
    def shepherd_in_loop(self) -> bool:
        """Does THIS execution run its own shepherd ticks? (#213)

        The shepherd cron asks mctl-api, which asks this query, because
        "Running" alone cannot answer it: an execution that started before
        the `shepherd-in-loop` patch replays that branch as False and will
        never tick, yet stays Running until the merge-watch deadline. The
        cron must keep sweeping those proposals, so it needs the patch
        marker this workflow actually recorded — not its status.
        """
        return self._shepherd_in_loop

    @workflow.query
    def lifecycle_claim(self) -> LifecycleClaim:
        """The ownership row this execution holds, or abandoned.

        `entity_id` non-empty after the watch ended means the loop did NOT let
        go: either it never reached the cleanup, or the cleanup's own write
        failed and it deliberately kept the claim so the record does not show a
        row nobody believes they own.

        This is the reader that makes the cleanup's guard falsifiable. It is
        also useful on its own — the reconciler and an operator both need to
        know which live execution, if any, still asserts a row.
        """
        return LifecycleClaim(
            entity_id=self._owned_entity_id,
            epoch=self._owner_epoch,
            last_op=self._last_lifecycle_op,
            last_op_landed=self._last_lifecycle_op_landed,
            abandoned=self._claim_abandoned,
        )

    @workflow.query
    def work_context(self) -> WorkContextState:
        """Correlate every execution of this workflow's `WorkItem`
        (mctlhq/mctl-agents#267, ADR 011), so a trace view can show two
        executions started on different surfaces as one task without either
        execution's own record having been rewritten.
        """
        current = self._executions[-1] if self._executions else None
        return WorkContextState(
            work_item_id=self._work_item_id,
            execution_id=current.execution_id if current is not None else "",
            execution_sequence=current.sequence if current is not None else 0,
            executions=tuple(self._executions),
            last_surface=self._current_surface,
            last_actor=self._current_actor,
            resume_rejections=tuple(self._resume_rejections),
        )

    @workflow.query
    def abandon_state(self) -> AbandonState:
        """Was this execution told to end early by an operator, and why?

        mctl-agents#420: the `abandon` signal handler below sets this and
        never raises, so a status reader (`cli.py status`, and eventually
        mctl-api) can distinguish "still parked" from "an operator ended
        this" without waiting for the execution to complete.
        """
        return AbandonState(
            abandoned=self._abandoned, reason=self._abandon_reason or ""
        )

    @workflow.signal
    def approve(self, *args: object) -> None:
        # Optional payload for the audit trail: legacy senders signal with no
        # args (still valid), new senders pass {"approver": "..."} or a bare
        # approver string. Signals must never raise, so parse defensively and
        # treat anything unrecognized as an approve with no identity.
        for arg in args:
            if isinstance(arg, dict):
                approver = arg.get("approver")
                if isinstance(approver, str) and approver:
                    self._approver = approver
            elif isinstance(arg, str) and arg:
                self._approver = arg
        self._approved = True
        # A fresh approval closes the "one resume already pending" window
        # (see `resume`): the actor who just approved is the current one,
        # and a subsequent resume is free to open a new window of its own.
        self._resume_pending = False

    def _reject_resume(
        self, execution_id: str, work_item_id: str, reason: str, *, execution_request_id: str = ""
    ) -> None:
        """Record one rejection per (execution_id, request id, reason). A
        retrying surface callback re-delivering the same rejected payload —
        for the days this workflow can legitimately stay open — must not grow
        workflow state or every `work_context` query response without
        bound."""
        if any(
            r.execution_id == execution_id and r.execution_request_id == execution_request_id and r.reason == reason
            for r in self._resume_rejections
        ):
            return
        self._resume_rejections.append(
            ResumeRejection(
                execution_id=execution_id,
                work_item_id=work_item_id,
                reason=reason,
                execution_request_id=execution_request_id,
            )
        )

    @staticmethod
    def _resume_provenance(surface_raw: object, actor_kind_raw: object, actor_id_raw: object) -> tuple[
        SurfaceRef, ActorRef, str
    ]:
        """A resume's surface and actor, and why they refuse it ("" when they
        do not). Shared by the `resume` signal and the
        `accept_execution_request` Update, so both apply the same rules.

        Fail closed on provenance, not open: a resume that does not say
        where it comes from and who is acting cannot have its approval
        semantics evaluated at all — accepting it would record an execution
        while silently keeping the PREVIOUS actor's approval, the exact
        cross-surface privilege inheritance #267 forbids. And the same
        closed vocabularies the CLI enforces (_work_context_from_args): an
        out-of-vocabulary kind would land in `work_context` query responses
        and, mirrored back into a WorkItem, make `work_item_verdict_for` read
        the whole item as UNKNOWN."""
        surface = SurfaceRef(kind=surface_raw) if isinstance(surface_raw, str) and surface_raw else SurfaceRef()
        actor = (
            ActorRef(kind=actor_kind_raw, actor_id=actor_id_raw if isinstance(actor_id_raw, str) else "")
            if isinstance(actor_kind_raw, str) and actor_kind_raw
            else ActorRef()
        )
        if not surface.kind or not actor.kind:
            return surface, actor, "surface-or-actor-missing"
        if surface.kind not in SURFACE_KINDS or actor.kind not in ACTOR_KINDS:
            return surface, actor, "surface-or-actor-unrecognised"
        return surface, actor, ""

    def _take_resume_provenance(self, surface: SurfaceRef, actor: ActorRef) -> bool:
        """Adopt an accepted resume's provenance; True when it is a surface
        or actor transition, which re-arms both approval gates.

        No `self._executions` gate here: production starts
        (orchestrator/temporal/start.py) construct IssueRef without a
        work_item_id, so the seeded-execution list is empty for every real
        loop and a gate on it made this transition inert exactly where it
        matters. With no recorded baseline, `_current_surface`/
        `_current_actor` are empty and any declared surface/actor differs
        from them — the un-provable "same surface, same actor" case
        deliberately counts as a transition (fail closed)."""
        transition = (surface != self._current_surface) or (actor != self._current_actor)
        if surface.kind:
            self._current_surface = surface
        if actor.kind:
            self._current_actor = actor
        if transition:
            self._approved = False
            self._approver = None
            self._resume_pending = True
        return transition

    async def _await_reapproval(
        self,
        investigate_result: WorkflowResult,
        *,
        approve: WorkflowResult | None = None,
        human_input: HumanInputOutcome | None = None,
    ) -> DevLoopResult | None:
        """Park until a resume-cleared approval is re-granted — releasable
        and bounded, per #420's rule for every approval park in this
        workflow. Observes `_abandoned` and expires at APPROVAL_WAIT_DEADLINE
        so a resumed-but-never-re-approved loop never needs a Temporal
        `terminate` (which would skip _watch_pr's `finally` and leak the
        lifecycle-ownership row). Returns the terminal DevLoopResult to
        return, or None to proceed. No issue-state polling here: unlike the
        original pre-slug park, a closed source issue is caught by the
        stale-issue gate that already ran, and the flip/implement below are
        guarded by their own checks."""
        try:
            await workflow.wait_condition(
                lambda: self._approved or self._abandoned,
                timeout=APPROVAL_WAIT_DEADLINE,
            )
        except TimeoutError:
            # A signal landing while the timeout fired must not be
            # discarded — same late-signal rule as the original park.
            if not (self._approved or self._abandoned):
                return DevLoopResult(
                    investigate=investigate_result,
                    implement=None,
                    approve=approve,
                    human_input=human_input,
                    ended="re-approval wait expired",
                )
        if self._abandoned:
            return DevLoopResult(
                investigate=investigate_result,
                implement=None,
                approve=approve,
                human_input=human_input,
                ended=f"abandoned: {self._abandon_reason}",
            )
        return None

    @workflow.signal
    def resume(self, *args: object) -> None:
        """Pick up this work item's task from a possibly different surface
        or actor (mctlhq/mctl-agents#267, ADR 011).

        Parses defensively and never raises, exactly like `approve` above.
        Expected payload: a single dict with `work_item_id`, `execution_id`,
        `surface` and `actor_kind` (plus optional `actor_id`). Anything
        else — no args, a non-dict arg, a dict missing `work_item_id` or
        `execution_id` — is silently ignored: a signal handler that raised
        on a malformed payload would fail the workflow task, and a resume is
        exactly the kind of externally-triggered input that must never do
        that (the same reasoning `approve`'s docstring gives). A payload
        that parses but omits `surface` or `actor_kind` is REJECTED with
        `reason="surface-or-actor-missing"` rather than ignored: without
        provenance the approval semantics below cannot be evaluated, and
        accepting it would let the new execution silently inherit the
        previous actor's approval.

        Idempotent, never forking: a duplicate `execution_id` (the common
        case, since `execution_id_for` is deterministic) is a no-op; a
        DIFFERENT `execution_id` arriving while one accepted resume is still
        awaiting fresh approval, or while a delivered execution request is
        still open, is rejected with `reason="resume-already-pending"`; a
        `work_item_id` that disagrees with the one already bound is rejected
        with `reason="work-item-mismatch"`.
        Every rejection is recorded, never merely dropped, so `work_context`
        can surface it.

        An ACCEPTED resume that changes the surface or the actor clears
        `_approved`/`_approver`, so both of `run`'s `await
        workflow.wait_condition(lambda: self._approved)` calls — the
        original gate before the slug/approve-flip/implementer-release
        chain, and the second one immediately before the implement CWFT is
        submitted — re-arm, and approval is re-evaluated by the current
        actor. A same-surface, same-actor resume (e.g. a retry from the
        same place) never touches approval at all: that would be
        re-litigating a decision nobody changed. "Same" requires a recorded
        baseline: on a loop whose own launch carried no surface/actor (every
        production start today), the first accepted resume always counts as
        a transition — an unprovable "same" fails closed.
        """
        payload: dict[str, object] | None = None
        for arg in args:
            if isinstance(arg, dict):
                payload = arg
                break
        if payload is None:
            return
        # (rejections are deduplicated on (execution_id, reason) by
        # _reject_resume: a surface callback re-delivering the same bad
        # payload for days must not grow workflow state without bound)

        work_item_id = payload.get("work_item_id")
        execution_id = payload.get("execution_id")
        if not isinstance(work_item_id, str) or not work_item_id:
            return
        if not isinstance(execution_id, str) or not execution_id:
            return

        if self._work_item_id and work_item_id != self._work_item_id:
            self._reject_resume(execution_id, work_item_id, "work-item-mismatch")
            return

        if execution_id in self._seen_execution_ids:
            return  # idempotent no-op — the same execution resuming again

        # An open delivery (`accept_execution_request`) is a pending resume
        # too, as its validator says: never two at once, whichever way each
        # arrived. `_open_deliveries` is non-empty only in a history that took
        # a delivery, so no history recorded before it changes.
        if self._resume_pending or self._open_deliveries:
            self._reject_resume(execution_id, work_item_id, "resume-already-pending")
            return

        # Rejected and recorded, never merely dropped (see _resume_provenance).
        surface, actor, refusal = self._resume_provenance(
            payload.get("surface"), payload.get("actor_kind"), payload.get("actor_id")
        )
        if refusal:
            self._reject_resume(execution_id, work_item_id, refusal)
            return

        self._work_item_id = self._work_item_id or work_item_id
        self._seen_execution_ids.add(execution_id)
        surface_transition = self._take_resume_provenance(surface, actor)
        self._executions.append(
            ExecutionRef(
                execution_id=execution_id,
                sequence=len(self._executions) + 1,
                surface=surface,
                actor=actor,
                surface_transition=surface_transition,
            )
        )

    @workflow.update(name=ACCEPT_EXECUTION_REQUEST_UPDATE)
    def accept_execution_request(self, delivery: ResumeDelivery) -> str:
        """Accept a `resume` execution request onto this live loop
        (mctlhq/mctl-agents#461, ADR 011 §8).

        The dispatcher sends it with `update_id = the request id` BEFORE it
        fulfils the request. Once accepted, the Update is in this loop's
        history: that is the durable "accepted, not yet bound" record, so no
        crash of the dispatcher can lose the request. The handler is
        synchronous: it records the request and starts `_deliver`, which
        binds the `we_` once the fulfil has minted it and ends that
        execution. It must not wait for the bind itself: the dispatcher
        fulfils only after this Update has answered.

        Idempotent at two levels: Temporal answers a repeated update id in
        the same run from its own registry without calling this handler, and
        `_accepted_request_ids` (carried across continue-as-new, where that
        registry is fresh) makes a repeated request id a no-op here.

        The rules are the `resume` signal's (see `_validate_execution_request`),
        and so is the approval semantics: a surface or actor transition
        clears the approval AT ACCEPTANCE, so from the moment this loop says
        yes to another actor's resume it cannot proceed on the previous
        actor's approval. A resume never inherits an approval."""
        rid = delivery.execution_request_id
        if rid in self._accepted_request_ids:
            return DELIVERY_ACCEPTED
        if rid == self._start_request_id:
            # The request this run was started for (Update-with-Start): `run`
            # binds and runs it on the dispatched path; nothing to deliver.
            self._accepted_request_ids.add(rid)
            return DELIVERY_ACCEPTED
        surface, actor, _ = self._resume_provenance(delivery.surface, delivery.actor_kind, delivery.actor_id)
        self._accepted_request_ids.add(rid)
        self._work_item_id = self._work_item_id or delivery.work_item_id
        opened = OpenDelivery(delivery=delivery, surface_transition=self._take_resume_provenance(surface, actor))
        self._open_deliveries[rid] = opened
        self._start_delivery(opened)
        return DELIVERY_ACCEPTED

    @accept_execution_request.validator
    def _validate_execution_request(self, delivery: ResumeDelivery) -> None:
        """Refuse before anything is recorded (a rejected Update writes no
        history event), so the dispatcher can reject the request before a
        `we_` is minted for a resume this loop would not take.

        Two kinds of no, as `ApplicationError` types the dispatcher reads:
        `ResumeDeferred` while this loop cannot decide yet or is ending (the
        request stays claimed; a later claim delivers it, or finds the loop
        closed and starts a continuation), and `ResumeRefused` with the
        `resume` signal's own reasons, which reject it."""
        rid, wid = delivery.execution_request_id, delivery.work_item_id

        def refuse(error_type: str, reason: str) -> ApplicationError:
            return ApplicationError(
                f"execution request {rid}: {reason}", reason, type=error_type, non_retryable=True
            )

        if not isinstance(rid, str) or not rid.startswith("xr_") or not isinstance(wid, str) or not wid:
            raise refuse(RESUME_REFUSED_ERROR_TYPE, "malformed-delivery")
        if delivery.kind not in DELIVERY_KINDS:
            raise refuse(RESUME_REFUSED_ERROR_TYPE, "malformed-delivery")
        if rid in self._accepted_request_ids:
            return
        if rid == self._start_request_id:
            # This run was started for this request (#461 option A), so it is
            # this run's to take whatever its kind, and before `run` has its
            # state: that is exactly when Update-with-Start delivers it. Its
            # work item was part of the same start input; a delivery naming
            # another fails closed.
            if wid != self._start_work_item_id:
                raise refuse(RESUME_REFUSED_ERROR_TYPE, "work-item-mismatch")
            return
        if delivery.kind == DELIVERY_KIND_START:
            # A `start` for an issue whose DevLoop already runs (the intake
            # poller's, or another request's): the loop is the one arbiter,
            # so a start is never "delivered" to a loop it did not start.
            raise refuse(LOOP_ACTIVE_ERROR_TYPE, "loop-active")
        # Before `run` has its state (the activation that starts this run, or
        # the continue-as-new gap before the rehydration), every guard below
        # would be vacuous: an empty binding accepts a foreign item, an empty
        # set accepts a request the previous run already took.
        if not self._initialized:
            raise refuse(RESUME_DEFERRED_ERROR_TYPE, "loop-not-ready")
        if self._exiting or self._hopping or self._abandoned:
            raise refuse(RESUME_DEFERRED_ERROR_TYPE, "loop-ending")
        if self._work_item_id and wid != self._work_item_id:
            raise refuse(RESUME_REFUSED_ERROR_TYPE, "work-item-mismatch")
        if self._open_deliveries or self._resume_pending:
            raise refuse(RESUME_REFUSED_ERROR_TYPE, "resume-already-pending")
        _, _, refusal = self._resume_provenance(delivery.surface, delivery.actor_kind, delivery.actor_id)
        if refusal:
            raise refuse(RESUME_REFUSED_ERROR_TYPE, refusal)

    def _start_delivery(self, opened: OpenDelivery) -> None:
        rid = opened.delivery.execution_request_id
        self._delivery_tasks[rid] = asyncio.create_task(self._deliver(opened))

    async def _deliver(self, opened: OpenDelivery) -> None:
        """Bind an accepted delivery's `we_`, advance it through `Running`,
        and end it (mctlhq/mctl-agents#461).

        The execution is the resumed run of this loop up to its next
        approval decision: `Succeeded` once the (re-armed) approval is
        granted, or at once when no approval gate is left ahead (the
        implement step was already submitted); `Failed` when the loop is
        abandoned or ends any other way first (`_settle_deliveries`). A
        terminal phase on every exit: while it is non-terminal mctl-api
        refuses every other request for the item.

        The wait for the fulfil never gives up while this loop runs: a
        re-sent Update is answered from Temporal's registry, not by this
        loop, so a later claim of the request would fulfil it for this loop
        whatever this loop had decided. It polls in FULFILMENT_WAIT chunks
        until the request is fulfilled (bind) or rejected (drop), and stops
        only when the run hops (the delivery is carried) or ends (one last
        DELIVERY_EXIT_GRACE poll)."""
        delivery = opened.delivery
        rid = delivery.execution_request_id
        if not workflow.patched(EXECUTION_REQUEST_RESUME_PATCH):
            # Only a history that took a delivery before this path existed
            # could answer False, and none can: the Update is new with it.
            self._end_delivery(opened, "unsupported")
            return
        engine_ref = request_engine_ref(workflow.info().workflow_id, rid)
        if opened.pending_phase:
            # Decided by an earlier attempt (maybe an earlier run) whose
            # advance did not land: land it, nothing else.
            await self._land_delivery(opened, opened.pending_phase, opened.pending_refusal, engine_ref)
            return
        bind = BindInput(
            work_item_id=delivery.work_item_id,
            execution_request_id=rid,
            engine_ref=engine_ref,
            issue_url=self._issue_url,
        )
        bound: BoundExecution | None = None
        while bound is None:
            if self._hopping:
                return  # still open: carried across the hop, bound by the next run
            if self._exiting:
                bound = await self._poll_delivery_bind(bind, DELIVERY_EXIT_GRACE, interruptible=False)
                if bound is None:
                    self._end_delivery(opened, "request-not-fulfilled")
                    return
                break
            bound = await self._poll_delivery_bind(bind, FULFILMENT_WAIT, interruptible=True)
        if not bound.bound:
            if bound.stranded:
                # The fulfil minted an execution under this delivery's engine
                # ref, but the loop must not run it (the item is about another
                # issue, or its advance to Running was refused). End it: while
                # it is non-terminal mctl-api refuses every other request for
                # the item, and the dispatcher's reconciliation never touches
                # the execution of a loop that is still RUNNING.
                await self._land_delivery(opened, "Failed", bound.outcome, engine_ref)
                return
            # Otherwise rejected by the platform, or fulfilled for another
            # loop (a re-claim that delivered elsewhere): nothing of ours.
            self._end_delivery(opened, bound.outcome)
            return
        if bound.execution_id not in self._seen_execution_ids:  # (re-bound after a hop: already recorded)
            self._seen_execution_ids.add(bound.execution_id)
            self._executions.append(
                ExecutionRef(
                    execution_id=bound.execution_id,
                    sequence=bound.sequence,
                    temporal_workflow_id=engine_ref,
                    surface=SurfaceRef(kind=delivery.surface),
                    actor=ActorRef(kind=delivery.actor_kind, actor_id=delivery.actor_id),
                    surface_transition=opened.surface_transition,
                )
            )
        await workflow.wait_condition(
            lambda: self._abandoned or self._approved or self._gates_passed or self._exiting or self._hopping
        )
        decided = (self._approved or self._gates_passed) and not self._abandoned
        if self._hopping and not decided and not self._abandoned:
            return  # still open, undecided: carried across the hop, re-bound by the next run
        await self._land_delivery(opened, "Succeeded" if decided else "Failed", "", engine_ref)

    async def _land_delivery(self, opened: OpenDelivery, phase: str, refusal: str, engine_ref: str) -> None:
        """Advance a delivery's execution to its terminal `phase`, then close
        the delivery. An advance that does not land (no answer within its
        retry policy) keeps the delivery OPEN with the phase pending: it is
        re-attempted every DELIVERY_ADVANCE_RETRY_INTERVAL while this loop
        runs, carried across a hop (the next run lands it), and given one
        last attempt when the loop ends — after which a closed loop's `#xr_`
        execution is the dispatcher's reconciliation's to end."""
        delivery = opened.delivery
        rid = delivery.execution_request_id
        while True:
            patient = phase == "Succeeded" or bool(refusal)
            policy = DISPATCHED_ADVANCE_RETRY_POLICY if patient and not self._exiting else FAST_ACTIVITY_RETRY_POLICY
            landed = await self._advance_dispatched_execution(
                phase, retry_policy=policy, work_item_id=delivery.work_item_id, engine_ref=engine_ref
            )
            if landed or self._exiting:
                self._end_delivery(opened, refusal)
                return
            opened = dataclasses.replace(opened, pending_phase=phase, pending_refusal=refusal)
            self._open_deliveries[rid] = opened
            if self._hopping:
                return  # carried with its pending phase
            try:
                await workflow.wait_condition(
                    lambda: self._exiting or self._hopping, timeout=DELIVERY_ADVANCE_RETRY_INTERVAL
                )
            except TimeoutError:
                pass
            if self._hopping:
                return

    async def _poll_delivery_bind(
        self, bind: BindInput, wait: timedelta, *, interruptible: bool
    ) -> BoundExecution | None:
        """One bounded wait for the fulfil: the bind activity retries until
        the request is fulfilled or `wait` runs out (None). An interruptible
        wait is cancelled as soon as the run starts to hop or end, so
        neither waits out a whole FULFILMENT_WAIT."""
        handle = workflow.start_activity(
            bind_dispatched_execution,
            bind,
            start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=wait,
            retry_policy=FULFILMENT_POLL_RETRY_POLICY,
        )
        cancelled = False
        if interruptible:
            await workflow.wait_condition(lambda: handle.done() or self._exiting or self._hopping)
            if not handle.done():
                handle.cancel()
                cancelled = True
        try:
            return await handle
        except ActivityError:
            return None
        except asyncio.CancelledError:
            if not cancelled:
                raise
            return None

    def _end_delivery(self, opened: OpenDelivery, refusal: str) -> None:
        """Close a delivery. The approval it cleared stays cleared (fail
        closed); the pending window it opened ends with it, so a later
        resume can be accepted."""
        delivery = opened.delivery
        self._open_deliveries.pop(delivery.execution_request_id, None)
        if opened.surface_transition:
            self._resume_pending = False
        if refusal:
            self._reject_resume(
                "", delivery.work_item_id, f"delivery-{refusal}", execution_request_id=delivery.execution_request_id
            )

    async def _settle_deliveries(self, *, hopping: bool) -> None:
        """Let every delivery of this run finish before the run ends
        (`hopping=False`: each one binds within DELIVERY_EXIT_GRACE or lets
        go, and a bound one ends `Failed` unless its decision was already
        made) or continues as new (`hopping=True`: an unbound one stops and
        is carried). No command and no await when this run took no
        delivery, so no history without one changes shape."""
        if not self._delivery_tasks:
            return
        if hopping:
            self._hopping = True
        else:
            self._exiting = True
        await workflow.wait_condition(lambda: all(t.done() for t in self._delivery_tasks.values()))

    @workflow.signal
    def human_input_response(self, *args: object) -> None:
        """A candidate answer to the pending clarification request.

        Signals must never raise (Temporal delivers them outside any
        `try/except` the workflow author controls), so this only appends the
        raw payload — validation (`human_input.validate_response`) happens
        inside `_await_human_input`, where a rejection can be recorded and
        answered for rather than crashing signal delivery. Deliberately
        never touches `self._approved`: an answer is data with provenance,
        never an authorization (ADR 013's "clarification is not approval"
        invariant) — a value that reads as an approval, e.g. "use option B
        and merge it", resumes THIS wait and nothing else.

        Bounded like `resume` right above: a payload arriving while no wait
        is pending, or beyond the queue cap, is counted and dropped — a
        misbehaving surface re-delivering for days must not grow workflow
        state without bound (claude P2 on #450).
        """
        for arg in args:
            if (
                self._human_input_state.state != WAITING_FOR_INPUT
                or len(self._input_responses) >= HUMAN_INPUT_RESPONSE_QUEUE_LIMIT
            ):
                self._human_input_rejected_count += 1
                continue
            self._input_responses.append(arg)

    @workflow.query
    def human_input_state(self) -> HumanInputState:
        """The pending (or last) clarification state, for mctl-api#261 and
        the portal to poll without owning this workflow. Never equal to the
        approval wait's state string — see HumanInputState's docstring."""
        return self._human_input_state

    async def _await_human_input(self, service: str, slug: str) -> HumanInputOutcome | None:
        """Check for, and if present durably wait on, a pending
        `HumanInputRequest` for this execution's proposal.

        Returns None when there is nothing to wait on (no request written,
        or the model re-asked an already-resolved question) — the caller
        proceeds exactly as it does today. Returns a `HumanInputOutcome`
        when the wait actually concluded (answered or timed out).

        `find_human_input_request` is a plain GitHub contents-API read
        (ADR 013's "Transport" open question): no Argo workflow, no Claude
        Agent SDK session and no activity slot are held for any part of the
        wait itself — only this activity call before it, and one more when a
        continuation resubmits `mctl-agents-investigate`.
        """
        raw = await workflow.execute_activity(
            find_human_input_request,
            args=[service, slug],
            start_to_close_timeout=SLUG_LOOKUP_TIMEOUT,
            retry_policy=SLUG_LOOKUP_RETRY_POLICY,
        )
        if raw is None:
            # `is None` deliberately: None is the activity's "no file"
            # answer, while a zero-byte request.json is corruption and must
            # fall through to the malformed guard below, not be skipped.
            return None

        try:
            request = human_input.HumanInputRequest.from_dict(json.loads(raw))
        except (ValueError, human_input.HumanInputError, TypeError) as exc:
            # A corrupt request document is corruption in gitops main, not a
            # transient condition — fail loudly rather than silently skip
            # clarification (which would strand the agent's own instruction
            # to wait for an answer) or wedge on an unparseable document
            # forever.
            raise ApplicationError(
                f"malformed human-input request for {service}/{slug}: {exc}",
                type="human_input_malformed",
                non_retryable=True,
            ) from exc

        if request.question_hash in self._resolved_question_hashes:
            # Already answered earlier in this execution; the agent re-asked
            # despite being told the ambiguity is resolved. Ignore rather
            # than re-enter WAITING_FOR_INPUT for a question with a known
            # answer.
            return None

        # A request sealed by a DIFFERENT dev-loop execution is a leftover,
        # not this run's question: `_resolved_question_hashes` is in-memory
        # per-instance state, so without this check an answered-but-unexpired
        # request.json from an earlier execution of the same issue would park
        # a fresh execution on an already-answered question for the rest of
        # its TTL (claude P2 on #450). Three discriminators, any one retires:
        # a foreign workflow id (a different issue's document at this path),
        # a stamped run id that is not this run's (the producer receives this
        # run's id in investigate_params), and — the one that still fires
        # for same-issue re-runs whose producer stamps no run id, where
        # `workflow_id_for` makes the workflow id IDENTICAL — a `created_at`
        # that predates this execution's own start. A request this run's
        # investigator sealed cannot be older than the run; the slack absorbs
        # clock skew between the sealing container and Temporal. `created_at`
        # is not hash-covered, but a backdated forgery only retires itself.
        info = workflow.info()
        sealed_before_this_run = (
            _as_utc(request.created_at)
            < info.start_time.astimezone(UTC) - HUMAN_INPUT_PRIOR_RUN_SLACK
        )
        if (
            request.execution.temporal_workflow_id != info.workflow_id
            or (
                request.execution.temporal_run_id is not None
                and request.execution.temporal_run_id != info.run_id
            )
            or sealed_before_this_run
        ):
            workflow.logger.info(
                "human_input.foreign_execution",
                extra={"human_input": human_input.request_log_dict(request)},
            )
            return None

        effective_deadline_iso = ""

        def _state(state: str) -> HumanInputState:
            return HumanInputState(
                state=state,
                request_id=request.request_id,
                request_hash=request.request_hash,
                question_hash=request.question_hash,
                expires_at=request.expires_at,
                round=request.round,
                resume_count=self._human_input_resume_count,
                rejected_count=self._human_input_rejected_count,
                effective_deadline=effective_deadline_iso,
            )

        try:
            expires_at = _as_utc(request.expires_at)
        except (ValueError, TypeError) as exc:
            # Same failure mode as the malformed-request guard above: an
            # unhandled exception here is a workflow-task failure that
            # Temporal retries forever on identical input, not a workflow
            # failure — a wedged state machine rather than a loud error.
            raise ApplicationError(
                f"malformed expires_at for {service}/{slug}: {exc}",
                type="human_input_malformed",
                non_retryable=True,
            ) from exc

        if (expires_at - workflow.now()).total_seconds() <= 0:
            # Already expired when READ, not watched expiring: a leftover
            # request.json from an earlier execution that timed out. Treat it
            # as no pending request rather than as a fresh timeout — a
            # timed_out return here would end every later execution of the
            # same issue instantly, forever (the file is never rewritten by a
            # run that dies on reading it). If the re-run investigator still
            # needs an answer it seals a fresh request with a fresh
            # expires_at, overwriting this one; if it did not re-ask,
            # proceeding is exactly right. No gitops write, replay-safe.
            # Deliberately BEFORE the round guards below: an expired leftover
            # whose round is exhausted must be retired, not turned into a
            # permanent non-retryable failure for every later execution of
            # the issue (claude P2 on #450).
            self._human_input_state = _state(RUNNING)
            workflow.logger.info(
                "human_input.stale_expired",
                extra={"human_input": human_input.request_log_dict(request)},
            )
            return None

        if request.round > human_input.MAX_CLARIFICATION_ROUNDS:
            raise ApplicationError(
                f"clarification rounds exhausted for {service}/{slug}: "
                f"round={request.round} > MAX_CLARIFICATION_ROUNDS="
                f"{human_input.MAX_CLARIFICATION_ROUNDS}",
                type="clarification_rounds_exhausted",
                non_retryable=True,
            )

        # The real bound. `request.round` above is agent-written — a producer
        # that always writes round=1 with a fresh question each time would
        # never trip it. `_human_input_resume_count` is incremented by THIS
        # workflow, once per accepted answer, so it bounds the continuation
        # loop no matter what the producer writes.
        if self._human_input_resume_count >= human_input.MAX_CLARIFICATION_ROUNDS:
            raise ApplicationError(
                f"clarification rounds exhausted for {service}/{slug}: "
                f"resume_count={self._human_input_resume_count} >= "
                f"MAX_CLARIFICATION_ROUNDS={human_input.MAX_CLARIFICATION_ROUNDS}",
                type="clarification_rounds_exhausted",
                non_retryable=True,
            )

        workflow.logger.info("human_input.requested", extra={"human_input": human_input.request_log_dict(request)})
        workflow.logger.info("human_input.wait_started", extra={"human_input": human_input.request_log_dict(request)})

        # The TTL cap in `validate()` is RELATIVE (expires - created); neither
        # timestamp is compared against a clock there, and request.json is
        # model-written, so a hallucinated far-future year seals cleanly and
        # would park this loop for years (claude P1 on #450). Bound the actual
        # wait against the workflow clock: the request still gets its full
        # TTL, never more.
        horizon = workflow.now() + timedelta(seconds=human_input.MAX_REQUEST_TTL_SECONDS)
        if expires_at > horizon:
            workflow.logger.info(
                "human_input.expiry_clamped",
                extra={"human_input": human_input.request_log_dict(request)},
            )
            expires_at = horizon
        effective_deadline_iso = expires_at.isoformat()
        self._human_input_state = _state(WAITING_FOR_INPUT)

        def _abandoned_outcome() -> HumanInputOutcome:
            # mctl-agents#420's escape hatch, honoured inside this park too:
            # checked BEFORE any delivered answer is consumed, so an operator
            # abandon always wins over a response racing it. The projection
            # is reset OUT of WAITING_FOR_INPUT — queries are served on
            # closed workflows too, and a surface polling one must not keep
            # prompting for an answer nobody is waiting on any more.
            self._human_input_state = _state(RUNNING)
            self._input_responses.clear()
            workflow.logger.info(
                "human_input.abandoned", extra={"human_input": human_input.request_log_dict(request)}
            )
            return HumanInputOutcome(
                outcome="abandoned", request_id=request.request_id,
                round=request.round, resume_count=self._human_input_resume_count,
            )

        while True:
            if self._abandoned:
                return _abandoned_outcome()
            remaining = (expires_at - workflow.now()).total_seconds()
            if remaining <= 0:
                self._input_responses.clear()
                self._human_input_state = _state(INPUT_TIMED_OUT)
                workflow.logger.info(
                    "human_input.timed_out", extra={"human_input": human_input.request_log_dict(request)}
                )
                return HumanInputOutcome(
                    outcome="timed_out", request_id=request.request_id,
                    round=request.round, resume_count=self._human_input_resume_count,
                )
            try:
                def _answer_arrived() -> bool:
                    return bool(self._input_responses) or self._abandoned

                await workflow.wait_condition(_answer_arrived, timeout=remaining)
            except TimeoutError:
                self._input_responses.clear()
                self._human_input_state = _state(INPUT_TIMED_OUT)
                workflow.logger.info(
                    "human_input.timed_out", extra={"human_input": human_input.request_log_dict(request)}
                )
                return HumanInputOutcome(
                    outcome="timed_out", request_id=request.request_id,
                    round=request.round, resume_count=self._human_input_resume_count,
                )
            except asyncio.CancelledError:
                workflow.logger.info(
                    "human_input.cancelled", extra={"human_input": human_input.request_log_dict(request)}
                )
                raise

            if self._abandoned:
                return _abandoned_outcome()

            raw_response = self._input_responses[0]
            workflow.logger.info("human_input.delivered", extra={"request_id": request.request_id})

            try:
                payload = raw_response if isinstance(raw_response, dict) else json.loads(str(raw_response))
                response = human_input.HumanInputResponse.from_dict(payload)
                human_input.validate_response(request, response, now=workflow.now())
            except (ValueError, TypeError, human_input.HumanInputError):
                # PRUNE the rejected payload — a rejected entry left queued
                # would count toward HUMAN_INPUT_RESPONSE_QUEUE_LIMIT for the
                # rest of the wait, and enough typos would lock the one
                # correct answer out of the handler forever (claude+agy P2 on
                # #450). Rebuilding the projection here is what keeps
                # `rejected_count` live for a polling surface: the state
                # object is a frozen snapshot, not a view.
                del self._input_responses[0]
                self._human_input_rejected_count += 1
                self._human_input_state = _state(WAITING_FOR_INPUT)
                workflow.logger.info(
                    "human_input.responded",
                    extra={"request_id": request.request_id, "accepted": False},
                )
                continue

            self._resolved_question_hashes.add(request.question_hash)
            self._human_input_resume_count += 1
            # A round already answered must not linger for the NEXT wait
            # this execution enters — clear rather than let a stale queued
            # item be misread as an answer to a different, later request.
            self._input_responses.clear()
            self._human_input_state = _state(RUNNING)
            workflow.logger.info(
                "human_input.responded",
                extra={"human_input": human_input.response_log_dict(response), "accepted": True},
            )
            workflow.logger.info("human_input.resumed", extra={"human_input": human_input.response_log_dict(response)})
            return HumanInputOutcome(
                outcome="answered",
                request_id=response.request_id,
                request_hash=response.request_hash,
                value=response.value,
                respondent=response.respondent.reference(),
                surface=response.surface,
                received_at=response.received_at,
                round=request.round,
                resume_count=self._human_input_resume_count,
            )

    def _dispatched_engine_ref(self, issue: IssueRef) -> str:
        """The engine ref this dispatched loop's own execution runs under:
        `<loop id>#<request id>` since #461 option A (the loop is issue-keyed,
        so its bare id no longer names one request), the bare workflow id in
        a history recorded before (`dev-loop-xr_<id>` was the request)."""
        workflow_id = workflow.info().workflow_id
        if workflow.patched(ISSUE_KEYED_DISPATCH_PATCH):
            return request_engine_ref(workflow_id, str(issue.execution_request_id))
        return workflow_id

    async def _bind_dispatched_execution(self, issue: IssueRef) -> tuple[BoundExecution | None, str]:
        """Wait for this loop's execution request to be fulfilled with this
        workflow's own engine run, and adopt the `we_` it was given.

        (bound execution, "") to run; (None, why) to end without running
        anything: the request was rejected, the store's answer does not
        describe this loop (`work-item-mismatch`), or it was never fulfilled
        within FULFILMENT_WAIT."""
        if not issue.work_item_id:
            return None, "execution request without a work item: refused"
        self._work_item_id = issue.work_item_id
        engine_ref = self._dispatched_engine_ref(issue)
        self._own_engine_ref = engine_ref
        try:
            bound = await workflow.execute_activity(
                bind_dispatched_execution,
                BindInput(
                    work_item_id=issue.work_item_id,
                    execution_request_id=str(issue.execution_request_id),
                    engine_ref=engine_ref,
                    issue_url=issue.issue_url,
                ),
                start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
                schedule_to_close_timeout=FULFILMENT_WAIT,
                retry_policy=FULFILMENT_POLL_RETRY_POLICY,
            )
        except ActivityError:
            return None, f"execution request {issue.execution_request_id} was not fulfilled within {FULFILMENT_WAIT}"
        if not bound.bound:
            if bound.stranded and workflow.patched(EXECUTION_REQUEST_STRANDED_PATCH):
                # An execution of this loop's own engine ref exists that it
                # must not run: end it before the loop ends (the reconciliation
                # only runs once a later request for the item is claimed, and
                # mctl-api refuses to create one while it is non-terminal:
                # hence the patient policy).
                await self._advance_dispatched_execution("Failed", retry_policy=DISPATCHED_ADVANCE_RETRY_POLICY)
            return None, f"execution request {issue.execution_request_id}: {bound.outcome}: {bound.reason}"
        self._seen_execution_ids.add(bound.execution_id)
        self._executions.append(
            ExecutionRef(
                execution_id=bound.execution_id,
                sequence=bound.sequence,
                temporal_workflow_id=engine_ref,
            )
        )
        return bound, ""

    async def _fail_dispatched_execution(self) -> None:
        """Advance the dispatched execution to `Failed` while the loop is
        unwinding, including on a workflow cancellation.

        Awaited in place from the `except` handler, NOT wrapped in
        `asyncio.shield`: a Temporal cancellation cancels the workflow's task
        once, and an activity scheduled from the handler that caught it runs
        to completion (the SDK's cleanup idiom), which
        `test_a_cancelled_dispatched_loop_still_ends_its_execution` pins. A
        shield would add nothing for that, and costs a separate task that an
        EVICTION cannot account for: the SDK tears an evicted workflow down
        by cancelling its tasks, and a task created in that teardown outlives
        it and later runs on another event loop (measured: a terminated
        dispatched loop did exactly that). Awaiting in place, the eviction's
        own `_WorkflowBeingEvictedError` stops the attempt before any command
        is scheduled.

        A loop TERMINATED instead runs no code at all; the dispatcher closes
        that gap (`dispatcher._reconcile_closed_loops`)."""
        await self._advance_dispatched_execution("Failed")

    async def _land_pending_advance(self, pending: str, *, before: str) -> str:
        """Re-attempt a dispatched execution's advance that has not landed
        yet, right before a park that holds this loop RUNNING — the one state
        the dispatcher's reconciliation must not touch. Returns what is still
        pending ("" once it landed). No-op, and no command, when nothing is
        pending: always so for an undispatched loop and on the path where
        the first advance landed.

        `before` is "approval" (patient: that park lasts days and has no
        deadline of its own to protect) or "human-input" (brief: see
        DISPATCHED_ADVANCE_BRIEF_RETRY_POLICY)."""
        if not pending:
            return ""
        policy = DISPATCHED_ADVANCE_RETRY_POLICY if before == "approval" else DISPATCHED_ADVANCE_BRIEF_RETRY_POLICY
        landed = await self._advance_dispatched_execution(pending, retry_policy=policy)
        return "" if landed else pending

    async def _advance_dispatched_execution(
        self,
        phase: str,
        *,
        retry_policy: RetryPolicy = FAST_ACTIVITY_RETRY_POLICY,
        work_item_id: str | None = None,
        engine_ref: str | None = None,
    ) -> bool:
        """Best effort, like `_record`: a store that will not take the phase
        must not fail the loop. But an execution left non-terminal blocks
        EVERY later request for the item, not only a resume: mctl-api's
        attach rule refuses any new non-terminal execution while one is
        (`execution_active`). The dispatcher reconciles one it can prove
        dead (`dispatcher._reconcile_closed_loops`), but not one whose loop
        is still RUNNING: hence the patient policy on the success path, and
        `_land_pending_advance` before every park.

        True when the store answered (the phase landed, or a definite
        refusal that no retry would change); False when every attempt failed
        without an answer, so the caller can try again later.

        `work_item_id`/`engine_ref` default to this loop's own dispatched
        execution (the ref it was bound under); a delivered resume passes its
        own (`<loop id>#<xr id>`)."""
        own_ref = self._own_engine_ref
        if engine_ref is None and not own_ref:
            # Only a run that bound its own execution has one to advance; its
            # ref is never re-derived here (a continued run starts without
            # it, and the bare workflow id is no longer any execution's ref).
            workflow.logger.error("no dispatched execution of this run to advance to %s", phase)
            # Not "landed": a caller holding a pending advance keeps it.
            return False
        try:
            outcome = await workflow.execute_activity(
                advance_dispatched_execution,
                AdvanceInput(
                    work_item_id=self._work_item_id if work_item_id is None else work_item_id,
                    engine_ref=own_ref if engine_ref is None else engine_ref,
                    phase=phase,
                ),
                start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
                retry_policy=retry_policy,
            )
            workflow.logger.info("dispatched execution -> %s: %s", phase, outcome)
            return True
        except ActivityError:
            workflow.logger.warning("dispatched execution could not be advanced to %s", phase)
            return False

    @workflow.signal
    def abandon(self, *args: object) -> None:
        """Gracefully end this execution at its next observation point.

        mctl-agents#420: the operator-driven, cluster-access-free escape
        hatch for a wedged execution -- deliberately a signal rather than
        Temporal `terminate`, because `terminate` skips `_watch_pr`'s
        `finally`, which is where the lifecycle-ownership row is released
        (see `_ownership`). A signal handler is not a workflow command, so
        adding this one needs no `workflow.patched` marker and changes no
        recorded history. Same defensive parse as `approve`: signals must
        never raise, so an unrecognised payload shape just falls back to a
        generic reason instead of erroring.
        """
        self._abandon_reason = _first_string(args, "reason") or "abandoned by operator"
        self._abandoned = True

    @workflow.run
    async def run(self, issue: IssueRef) -> DevLoopResult:
        # Every exit of this run — a result, a failure, a cancellation —
        # first settles the resume deliveries it accepted
        # (mctlhq/mctl-agents#461), so each one's execution ends: none left
        # `Pending`/`Running` behind a loop that no longer runs. Not
        # BaseException: GeneratorExit and the SDK eviction teardown must
        # unwind untouched (see `_fail_dispatched_execution`), and a
        # continue-as-new (`ContinueAsNewError`, a BaseException) settled
        # its deliveries itself, as a hop, before raising. Without a
        # delivery nothing here awaits, so no history changes shape.
        self._issue_url = issue.issue_url
        if issue.resume is None:
            self._initialized = True
        try:
            result = await self._run_loop(issue)
        except (Exception, asyncio.CancelledError):
            await self._settle_deliveries(hopping=False)
            raise
        await self._settle_deliveries(hopping=False)
        return result

    async def _run_loop(self, issue: IssueRef) -> DevLoopResult:
        # mctl-agents#404 v2: a continued run of a hopped merge watch. Every
        # OTHER start (external, or `USE_EXISTING` attaching to a running
        # execution) has `issue.resume is None` and falls through to the
        # full pipeline below -- `run`'s decoded argument list stays
        # `[IssueRef]` either way (design.md's "argument-shape decision").
        if issue.resume is not None:
            return await self._resume_merge_watch(issue)

        target_repo = _target_repo(issue)

        # Work-context seeding (mctlhq/mctl-agents#267, ADR 011): when the
        # caller supplied a canonical WorkItem, this loop's OWN launch is
        # execution #1 of it. `workflow.info()` is a local, deterministic
        # read — no command is scheduled — so this seeding is safe on every
        # replay, unguarded, exactly like the rest of `resume`'s state
        # mutations (see that signal's docstring for why no
        # `workflow.patched` gate is needed here: nothing below schedules a
        # new command as a result).
        # A dispatched loop (mctlhq/mctl-agents#461) is bound to exactly one
        # work item and one execution request, and its execution #1 is the
        # store's canonical `we_`, never a locally derived id: it runs nothing
        # until the request is fulfilled with this workflow's own engine run.
        dispatched: BoundExecution | None = None
        if issue.execution_request_id and workflow.patched(EXECUTION_REQUEST_PATCH):
            dispatched, refused = await self._bind_dispatched_execution(issue)
            if dispatched is None:
                if workflow.patched(DISPATCHED_NOT_RUN_FAILS_PATCH):
                    raise ApplicationError(refused, type=DISPATCHED_NOT_RUN_ERROR_TYPE, non_retryable=True)
                return DevLoopResult(
                    investigate=WorkflowResult(workflow_name="", phase="NotStarted"),
                    implement=None,
                    ended=refused,
                )
        elif issue.work_item_id:
            self._work_item_id = issue.work_item_id
            seed_execution_id = execution_id_for(issue.work_item_id, 1, str(workflow.info().attempt))
            self._seen_execution_ids.add(seed_execution_id)
            self._executions.append(
                ExecutionRef(
                    execution_id=seed_execution_id,
                    sequence=1,
                    temporal_workflow_id=workflow.info().workflow_id,
                )
            )

        # A dispatched execution must end on EVERY exit from here to its
        # advance below, not only the successful one: the investigator
        # container, handed a `we_`, never writes it, so an exception here
        # (an unpinned release, an Argo submit that exhausted its retries or
        # its timeout, a cancelled workflow) would otherwise leave it
        # non-terminal forever, and mctl-api refuses every later request for
        # the item while it is (`execution_active`). No command is added on
        # the path that does not raise, and none at all for an undispatched
        # loop, so no history recorded before this changes shape.
        try:
            # Pin the investigator version ONCE, at the start of this step. A
            # later promote/rollback in the registry must not retroactively
            # change what an in-flight (or replayed) workflow already ran.
            investigator_release = _require_release(
                "issue-investigator", await _resolve("issue-investigator")
            )
            investigate_params = {"issue_url": issue.issue_url}
            if investigator_release and investigator_release.image_ref:
                investigate_params["agent_image"] = investigator_release.image_ref
                investigate_params["agent_version"] = f"issue-investigator@{investigator_release.version}"
            # This loop's own ids (mctlhq/mctl-agents#461, #451), declared on
            # the investigate CWFT by gitops#1345 and forwarded as
            # --temporal-workflow-id / --temporal-run-id. The investigator
            # names this loop in its approve instructions and stamps both on
            # what it seals, so `_await_human_input` recognises the request as
            # its own. Every loop is issue-keyed since #461 option A, so the
            # investigator could derive the workflow id itself, but not the run
            # id, which retires a same-id leftover that the `created_at` check
            # misses.
            # Inert until mctl-api declares them (mctl-api#372 strips
            # undeclared params). Replay-safe without a marker: activity
            # input is not compared on replay (tests/test_workflow_replay.py,
            # "an extra argument added to an existing activity"), and
            # `workflow.info()` schedules no command.
            loop_info = workflow.info()
            investigate_params["temporal_workflow_id"] = loop_info.workflow_id
            investigate_params["temporal_run_id"] = loop_info.run_id
            if dispatched is not None:
                # The investigate CWFT's declared `work_item_id`/`execution_id`
                # parameters (gitops#1279) become `--work-item-id`/`--execution-id`:
                # the investigator uses this `we_` as its execution identity and
                # refuses it unless it is in that item's ledger, and refuses an
                # item about another issue (`work-item mismatch`).
                investigate_params["work_item_id"] = self._work_item_id
                investigate_params["execution_id"] = dispatched.execution_id
                # Correlation only (gitops#1345's `execution_request_id`):
                # the execution identity is still `execution_id`.
                investigate_params["execution_request_id"] = str(issue.execution_request_id)

            investigate_result = await _run_cwft("mctl-agents-investigate", investigate_params)
            await _record("issue-investigator", investigator_release, investigate_result, target_repo)
        except (Exception, asyncio.CancelledError):
            # Not BaseException: GeneratorExit and the SDK eviction teardown
            # must unwind untouched (see `_fail_dispatched_execution`).
            if dispatched is not None:
                await self._fail_dispatched_execution()
            raise
        # The phase the dispatched execution still has to reach, when its
        # advance below did not land (mctlhq/mctl-agents#461); "" otherwise.
        dispatched_advance_pending = ""
        if dispatched is not None:
            # The dispatched execution IS this investigator run: it ends
            # here, whatever the loop does next, so the item's one
            # non-terminal slot is free again for a later request.
            advance_phase = "Succeeded" if investigate_result.succeeded else "Failed"
            policy = DISPATCHED_ADVANCE_RETRY_POLICY if investigate_result.succeeded else FAST_ACTIVITY_RETRY_POLICY
            if not await self._advance_dispatched_execution(advance_phase, retry_policy=policy):
                dispatched_advance_pending = advance_phase

        if not investigate_result.succeeded:
            return DevLoopResult(
                investigate=investigate_result,
                implement=None,
                ended=f"investigate ended {investigate_result.phase}",
            )

        # Durable clarification (mctlhq/mctl-agents#333, ADR 013), gated so
        # an in-flight history recorded before this change takes its old
        # command sequence verbatim: an unconditional find_proposal_slug/
        # find_human_input_request pair here would be exactly the command
        # mismatch that wedges a replaying execution (see _run_cwft's
        # exec-queue comment for the identical rule applied to #251).
        # Unlike atomic-approve/slug-scoped-implement below, this branch does
        # its OWN find_proposal_slug lookup rather than hoisting and reusing
        # the later one — one extra idempotent GitHub GET on a granted,
        # patched execution, traded for not touching that already-delicate
        # ordering at all.
        human_input_outcome: HumanInputOutcome | None = None
        accepted_answers: list[dict[str, Any]] = []
        if workflow.patched("human-input"):
            issue_number_for_input = parse_issue_url(issue.issue_url).number
            input_slug = await workflow.execute_activity(
                find_proposal_slug,
                args=[target_repo, issue_number_for_input],
                start_to_close_timeout=SLUG_LOOKUP_TIMEOUT,
                retry_policy=SLUG_LOOKUP_RETRY_POLICY,
            )
            while input_slug:
                # Every park, not only the approval one: a clarification wait
                # also holds the loop RUNNING, for up to a request's TTL.
                dispatched_advance_pending = await self._land_pending_advance(
                    dispatched_advance_pending, before="human-input"
                )
                hi_outcome = await self._await_human_input(target_repo, input_slug)
                if hi_outcome is None:
                    break
                human_input_outcome = hi_outcome
                if hi_outcome.outcome == "abandoned":
                    return DevLoopResult(
                        investigate=investigate_result, implement=None,
                        human_input=human_input_outcome,
                        ended=f"abandoned: {self._abandon_reason}",
                    )
                if hi_outcome.outcome == "timed_out":
                    return DevLoopResult(
                        investigate=investigate_result, implement=None,
                        human_input=human_input_outcome,
                        ended=f"human input wait expired for request {hi_outcome.request_id}",
                    )
                # "answered": resubmit investigate with EVERY answer accepted
                # this execution as a `human_input_responses` JSON array
                # (request_id/request_hash/value/respondent/surface/
                # received_at per entry — never a transcript), then loop back
                # to check whether a further request is pending. The full
                # history, not just the latest answer: a continuation that
                # only saw round N's answer would re-ask round 1's question,
                # which the `_resolved_question_hashes` skip then refuses to
                # re-answer — the pipeline would proceed on a run that was
                # still waiting (claude P2 on #450).
                # MAX_CLARIFICATION_ROUNDS (enforced inside
                # _await_human_input) bounds this loop.
                accepted_answers.append({
                    "request_id": hi_outcome.request_id,
                    "request_hash": hi_outcome.request_hash,
                    "value": hi_outcome.value,
                    "respondent": hi_outcome.respondent,
                    "surface": hi_outcome.surface,
                    "received_at": hi_outcome.received_at,
                })
                continuation_params = dict(investigate_params)
                if dispatched is not None:
                    # The dispatched `we_` ended with the first run and sealed
                    # that run's context; a continuation is a different
                    # context, which the store would refuse as a divergence
                    # under the same execution. It runs without the identity.
                    continuation_params.pop("work_item_id", None)
                    continuation_params.pop("execution_id", None)
                continuation_params["human_input_responses"] = json.dumps(accepted_answers)
                investigate_result = await _run_cwft("mctl-agents-investigate", continuation_params)
                await _record("issue-investigator", investigator_release, investigate_result, target_repo)
                if not investigate_result.succeeded:
                    return DevLoopResult(
                        investigate=investigate_result, implement=None,
                        human_input=human_input_outcome,
                        ended=f"investigate ended {investigate_result.phase}",
                    )

        # Durable wait: this workflow can sit here for days without costing
        # anything beyond Temporal's own history storage — exactly the
        # "durable per-issue state" the plan's problem statement calls out
        # as missing from the current polling-cron pipeline.
        #
        # mctl-agents#420: an unbounded wait_condition here never released on
        # its own -- not on the source issue closing, not on an `abandon`
        # signal, not ever, if `approve` never arrived (see the module
        # docstring addendum). `approval-watch` bounds the park with a poll
        # loop that re-reads the issue's state on every boundary and expires
        # at APPROVAL_WAIT_DEADLINE if nothing resolves it first. The
        # unpatched branch is byte-identical to the historical call apart
        # from also observing `_abandoned`, which is not itself a new
        # command (see `abandon`'s docstring): a parked execution has no
        # history event to diverge from at this position.
        # The success-path advance never got an answer (nor before any
        # clarification park): once more, patiently, before the approval
        # park, which may last days with the loop RUNNING. Still best effort.
        dispatched_advance_pending = await self._land_pending_advance(dispatched_advance_pending, before="approval")
        approval_ended: str | None = None
        if workflow.patched("approval-watch"):
            approval_deadline = workflow.now() + APPROVAL_WAIT_DEADLINE
            while workflow.now() < approval_deadline:
                try:
                    # wait_condition with a timeout raises asyncio.TimeoutError
                    # on expiry rather than returning False -- it never
                    # returns a value at all (see its own signature).
                    await workflow.wait_condition(
                        lambda: self._approved or self._abandoned,
                        timeout=APPROVAL_POLL_INTERVAL,
                    )
                except TimeoutError:
                    parked_state = await _read_issue_state(issue)
                    if parked_state is not None and parked_state.state == "closed":
                        approval_ended = (
                            "source issue closed while parked "
                            f"({parked_state.state_reason or 'completed'})"
                        )
                        break
                    continue
                break
            else:
                approval_ended = "approval wait expired"
        else:
            await workflow.wait_condition(lambda: self._approved or self._abandoned)

        # mctl-agents#420: an approve or abandon signal landing while the final
        # poll's in-flight get_issue_state activity was executing must not be
        # silently discarded just because the wait deadline was crossed.
        # But if the source issue was confirmed closed on GitHub, a late
        # approve must NOT resurrect it.
        if approval_ended is not None:
            if approval_ended == "approval wait expired" and (
                self._approved or self._abandoned
            ):
                pass
            else:
                return DevLoopResult(
                    investigate=investigate_result, implement=None,
                    human_input=human_input_outcome, ended=approval_ended
                )

        if self._abandoned:
            return DevLoopResult(
                investigate=investigate_result,
                implement=None,
                human_input=human_input_outcome,
                ended=f"abandoned: {self._abandon_reason}",
            )

        # mctl-agents#410: the issue that started this loop can close between
        # the approval signal and this point -- reopened elsewhere,
        # superseded, or resolved directly. Check BEFORE find_proposal_slug
        # and BEFORE the mctl-agents-approve CWFT below, so a closed issue is
        # never spent flipping a proposal to `accepted` (and then
        # implementing it) for a reason that is already gone.
        #
        # workflow.patched: get_issue_state is a brand-new command in every
        # position it could go, so an unpatched (pre-existing) history must
        # take the legacy branch untouched -- inserting it unconditionally
        # would be a command mismatch that wedges every in-flight approved
        # loop on replay, the same hazard slug-scoped-implement's own guard
        # exists to avoid two paragraphs down.
        if workflow.patched("stale-issue-admission"):
            issue_state = await _read_issue_state(issue)
            if issue_state is not None and issue_state.state == "closed":
                return DevLoopResult(
                    investigate=investigate_result,
                    implement=None,
                    approve=None,
                    human_input=human_input_outcome,
                    ended=f"source issue closed ({issue_state.state_reason or 'completed'})",
                )

        # Scoped to this issue's own proposal, not just its repo. Service
        # scoping alone left a same-repo race: two approved loops for the
        # same repo both discovered the full accepted list from their own
        # (stale) gitops clones, claimed overlapping proposals, and their
        # commit-and-push steps rebase-conflicted on each other's
        # .status.yaml (2026-08-28, mctl-portal 79/80 — mctl-agents#203).
        # With the slug pinned, concurrent same-repo loops touch disjoint
        # proposal dirs and the commit step's rebase-retry stays clean.
        #
        # Failing loudly beats falling back to an unscoped run: unscoped,
        # this loop could implement a DIFFERENT accepted proposal in the
        # repo — the exact wrong-proposal hazard scoping exists to prevent.
        # The proposal must exist by now (investigate committed it before
        # this workflow ever reached wait_condition), so a missing slug
        # after retries means agents-state is in a state a human needs to
        # look at anyway.
        # workflow.patched: histories recorded before this change scheduled
        # submit_and_wait directly after the implementer resolve — replaying
        # them through an unconditional find_proposal_slug would be a command
        # mismatch (nondeterminism) that permanently wedges every in-flight
        # approved loop at deploy time. Old histories take the legacy
        # unscoped branch; new executions record the patch marker and get
        # slug scoping. Drop to workflow.deprecate_patch once no pre-patch
        # execution can still be running.
        slug: str | None = None
        approve_result: WorkflowResult | None = None
        implementer_release: ResolvedRelease | None = None
        # Evaluate the atomic-approve patch ONCE, up front, because it also
        # decides the position of the implementer resolve. Histories from the
        # slug-scoped-but-pre-approve era (1.29.2..1.29.4) recorded
        # resolve → slug-lookup → implement; unconditionally moving the
        # resolve after the flip would be a command mismatch that wedges
        # every such in-flight loop on replay (codex P1 round 2, PR #212).
        # patched() returns a stable answer per execution, so both branches
        # below see the same value.
        atomic_approve = workflow.patched("atomic-approve")
        if not atomic_approve:
            # Legacy position: pre-atomic-approve histories resolved the
            # implementer before the slug lookup — keep their command order.
            implementer_release = _require_release("implementer", await _resolve("implementer"))
        if workflow.patched("slug-scoped-implement"):
            issue_number = parse_issue_url(issue.issue_url).number
            slug = await workflow.execute_activity(
                find_proposal_slug,
                args=[target_repo, issue_number],
                start_to_close_timeout=SLUG_LOOKUP_TIMEOUT,
                retry_policy=SLUG_LOOKUP_RETRY_POLICY,
            )
            if not slug:
                raise ApplicationError(
                    f"no proposal dir issue-{issue_number}-* found under "
                    f"agents-state/{target_repo}/proposals on gitops main; "
                    "refusing an unscoped implement run",
                    non_retryable=True,
                )
            # Atomic approve (phase-5 cutover, mctl-agents#150): flip THIS
            # proposal proposed → accepted as an Argo-executed gitops commit,
            # instead of relying on the operator's manual edit. Nested inside
            # the slug-scoped branch because the flip needs the slug, and any
            # execution new enough to record this patch marker records the
            # outer one too; old in-flight histories replay the legacy
            # signal-only path (manual flip stays their affordance). The CWFT
            # is idempotent on already-accepted, so a Temporal retry or a
            # racing manual approve is a successful no-op — but any other
            # failure (missing proposal dir, unexpected status, push failure)
            # stops the loop HERE: proceeding to implement without a durable
            # accepted status would just be a silent no-op run.
            if atomic_approve:
                # Re-check approval immediately before the durable flip
                # (mctlhq/mctl-agents#267, ADR 011). The slug lookup above
                # is a real activity await; a `resume` that changes surface
                # or actor can land in that gap, clearing `_approved` AND
                # `_approver` — without this gate the flip below would
                # commit a gitops approval attributed to "unknown" on the
                # new actor's behalf. Unlike the original park, this wait
                # also observes `_abandoned` and is bounded by
                # APPROVAL_WAIT_DEADLINE — a resumed-but-never-re-approved
                # loop must stay releasable by `abandon` and must expire the
                # way the first park does (#420), not park forever with
                # `terminate` as the only exit. The twin gate before the
                # implement CWFT covers the later gaps (implementer resolve,
                # the flip itself).
                if workflow.patched("work-context-resume"):
                    ended = await self._await_reapproval(investigate_result, human_input=human_input_outcome)
                    if ended is not None:
                        return ended
                approve_result = await _run_cwft(
                    "mctl-agents-approve",
                    {
                        "service": target_repo,
                        "slug": slug,
                        "approver": self._approver or "unknown",
                    },
                    step_timeout=APPROVE_STEP_TIMEOUT,
                )
                # No record_execution here: the executions ledger is for
                # SDK-backed agent runs (see docs/agent-inventory.yaml), and
                # this deterministic flip is already triply audited — the
                # .status.yaml approval block, the gitops commit message,
                # and this workflow's own history all carry the approver.
                if not approve_result.succeeded:
                    return DevLoopResult(
                        investigate=investigate_result,
                        implement=None,
                        approve=approve_result,
                        human_input=human_input_outcome,
                        ended=f"approve flip ended {approve_result.phase}",
                    )
        if atomic_approve:
            # Resolve the implementer only AFTER the approval flip is
            # durable: _resolve can fail permanently (registry outage
            # outlasting its five retries), and if that happened before the
            # flip, the operator's approval would evaporate with the failed
            # workflow (codex P1 on PR #212). The reverse case — the gate
            # below failing AFTER the flip — leaves an accepted proposal
            # that never implements. Recovery is to publish the missing
            # release and re-add the intake label: ALLOW_DUPLICATE_FAILED_ONLY
            # lets the issue start again, and the approve CWFT is a no-op on
            # an already-accepted proposal. It is a restart, not a resume —
            # the new run re-investigates and waits for a fresh approve
            # signal, and if the issue TITLE changed in between, the
            # investigator derives a different slug and find_proposal_slug
            # then refuses the ambiguous issue-<N>-* match (codex P2). Both
            # are acceptable for a misconfiguration that should not happen
            # once every release refreshes the registry, and neither is
            # silent. Pre-atomic-approve histories already resolved above,
            # in their recorded position.
            implementer_release = _require_release("implementer", await _resolve("implementer"))
        #
        # Depends on mctl-gitops's cwft-mctl-agents-implement.yaml already
        # declaring this `service` parameter and threading it to
        # `run_implementer.py --service <value>` — verified directly
        # (not assumed) as of this comment: see
        # cwft-mctl-agents-implement.yaml's `arguments.parameters` (name:
        # service) and its `implement-proposals` step, which does
        # `[ -n "$WORKFLOW_SERVICE" ] && set -- "$@" --service "$WORKFLOW_SERVICE"`
        # before invoking run_implementer.py. mctl-agents' CI can't check
        # this out to assert it directly (mctl-gitops is a sibling repo), so
        # if that CWFT is ever changed to drop/rename the parameter, this
        # scoping silently reverts to today's unscoped behavior with no
        # error on this side.
        implement_params: dict[str, str] = {"service": target_repo}
        if slug:
            implement_params["slug"] = slug
        if implementer_release and implementer_release.image_ref:
            implement_params["agent_image"] = implementer_release.image_ref
            implement_params["agent_version"] = f"implementer@{implementer_release.version}"

        # Re-check approval immediately before implementing (mctlhq/mctl-
        # agents#267, ADR 011) — the twin of the gate before the approve
        # flip above. That one closes the slug-lookup gap so a resume cannot
        # produce an "unknown"-attributed flip; this one closes the
        # remaining gaps (the flip itself and the implementer resolve are
        # both real activity awaits), so a resume landing after the flip
        # still forces re-approval before any implementer is released.
        # Same abandon/deadline semantics as its twin — see the comment
        # there and #420.
        if workflow.patched("work-context-resume"):
            ended = await self._await_reapproval(
                investigate_result, approve=approve_result, human_input=human_input_outcome
            )
            if ended is not None:
                return ended

        # No approval gate is left from here on: a resume delivered from now
        # (mctlhq/mctl-agents#461) has no decision to wait for.
        self._gates_passed = True
        implement_result = await self._implement(implementer_release, implement_params, target_repo)

        # mctl-agents#420: an abandon signal arriving while _implement was running
        # ends the loop immediately without entering the merge watch or deploy stages.
        if self._abandoned:
            return DevLoopResult(
                investigate=investigate_result,
                implement=implement_result,
                approve=approve_result,
                human_input=human_input_outcome,
                ended=f"abandoned: {self._abandon_reason}",
            )

        # Stage 6.1 merge detection (ADR-006, #214): watch the implement PR
        # until it merges/closes, bounded by MERGE_WATCH_DEADLINE. Requires
        # the slug (the PR is resolved from this proposal's .status.yaml);
        # any execution new enough to record this marker also recorded
        # slug-scoped-implement, so slug is set whenever the branch is taken.
        outcome = _WatchOutcome(last=None)
        if workflow.patched("merge-detection") and implement_result.succeeded and slug:
            outcome = await self._watch_pr(target_repo, slug)
            if outcome.resume is not None:
                # The watch's own history grew large enough to hop
                # (mctl-agents#404 v2). `_watch_pr` never re-runs investigate,
                # approve or implement, so their results have to be carried
                # here -- `_watch_pr` itself has no view of them. Deliveries
                # first: an unbound one is carried, not dropped (#461).
                await self._settle_deliveries(hopping=True)
                resume = self._carry_work_context(
                    dataclasses.replace(
                        outcome.resume,
                        investigate=investigate_result,
                        implement=implement_result,
                        approve=approve_result,
                        implement_state=self._implement_state,
                        approver=self._approver,
                        human_input=human_input_outcome,
                    )
                )
                workflow.continue_as_new(
                    IssueRef(issue_url=issue.issue_url, work_item_id=issue.work_item_id, resume=resume),
                )

        return await self._finish_after_watch(
            target_repo=target_repo,
            investigate_result=investigate_result,
            implement_result=implement_result,
            approve_result=approve_result,
            human_input_outcome=human_input_outcome,
            outcome=outcome,
        )

    def _carry_work_context(self, resume: MergeWatchResume) -> MergeWatchResume:
        """Fold the work-context instance state into a hop's resume record
        (mctlhq/mctl-agents#267) — the counterpart of the rehydration block
        in `_resume_merge_watch`, kept in one place so the two hop sites
        cannot drift."""
        return dataclasses.replace(
            resume,
            work_item_id=self._work_item_id,
            executions=tuple(self._executions),
            seen_execution_ids=tuple(sorted(self._seen_execution_ids)),
            current_surface=self._current_surface,
            current_actor=self._current_actor,
            resume_rejections=tuple(self._resume_rejections),
            resume_pending=self._resume_pending,
            accepted_request_ids=tuple(sorted(self._accepted_request_ids)),
            open_deliveries=tuple(self._open_deliveries[rid] for rid in sorted(self._open_deliveries)),
        )

    def _rehydrate_work_context(self, resume: MergeWatchResume) -> None:
        """The counterpart of `_carry_work_context`: restore the carried
        binding, then re-apply whatever `resume` signals delivered in the
        continue_as_new gap recorded against __init__'s empty state —
        re-deriving sequence and transition against the carried baseline,
        and re-checking the two guards the empty state made vacuous
        (work-item-mismatch, resume-already-pending).

        A transitioning gap resume also cleared `_approved`, which
        `_resume_merge_watch` has already force-set back to True before
        calling this — deliberately harmless: a continued run reaches no
        approval gate (investigate/approve/implement never re-run), so the
        cleared approval has nothing left to gate."""
        gap_work_item_id = self._work_item_id
        gap_executions = self._executions
        gap_rejections = self._resume_rejections
        self._work_item_id = resume.work_item_id or self._work_item_id
        self._executions = list(resume.executions)
        self._seen_execution_ids = set(resume.seen_execution_ids)
        self._current_surface = resume.current_surface
        self._current_actor = resume.current_actor
        self._resume_rejections = list(resume.resume_rejections)
        # Not OR-ed with the gap value: the only pre-rehydration setter is a
        # gap resume, and the loop below re-derives the pending window for
        # the gap executions it actually ACCEPTS — a rejected (foreign) gap
        # resume must not leave its window open.
        self._resume_pending = resume.resume_pending
        # Deliveries (#461) need no gap re-check: the Update's validator
        # defers until `_initialized`, so none was accepted in the gap.
        self._accepted_request_ids = set(resume.accepted_request_ids)
        self._open_deliveries = {d.delivery.execution_request_id: d for d in resume.open_deliveries}
        # A gap `resume` ran against an EMPTY binding, so the
        # work-item-mismatch guard was vacuous for it: a resume naming a
        # foreign work item was accepted there. Re-applying it here would
        # graft the foreign execution onto the carried work item — reject
        # it now, with the same reason the guard gives when the binding is
        # in place.
        gap_is_foreign = bool(
            gap_work_item_id and self._work_item_id and gap_work_item_id != self._work_item_id
        )
        for execution in gap_executions:
            if gap_is_foreign:
                self._reject_resume(execution.execution_id, gap_work_item_id, "work-item-mismatch")
                continue
            if execution.execution_id in self._seen_execution_ids:
                continue
            # The other guard the empty gap state made vacuous: the carried
            # record may say one accepted resume is still awaiting fresh
            # approval, and a gap resume must be rejected against that
            # window exactly as `resume` itself would have rejected it a
            # second earlier or later.
            if self._resume_pending:
                self._reject_resume(
                    execution.execution_id, self._work_item_id, "resume-already-pending"
                )
                continue
            self._seen_execution_ids.add(execution.execution_id)
            transition = (
                execution.surface != self._current_surface or execution.actor != self._current_actor
            )
            self._executions.append(
                dataclasses.replace(
                    execution,
                    sequence=len(self._executions) + 1,
                    surface_transition=transition,
                )
            )
            if execution.surface.kind:
                self._current_surface = execution.surface
            if execution.actor.kind:
                self._current_actor = execution.actor
            if transition:
                self._resume_pending = True
        for rejection in gap_rejections:
            if not any(
                r.execution_id == rejection.execution_id and r.reason == rejection.reason
                for r in self._resume_rejections
            ):
                self._resume_rejections.append(rejection)

    async def _resume_merge_watch(self, issue: IssueRef) -> DevLoopResult:
        """Continue a merge watch that hopped via continue_as_new
        (mctl-agents#404 v2).

        Rehydrates every piece of instance state carried in `issue.resume`
        BEFORE the first await, so all four `@workflow.query` handlers
        answer correctly from the very first workflow task of this run --
        including `_abandoned`/`_abandon_reason`, re-checked here so an
        `abandon` observed by the previous run still ends the watch instead
        of being silently lost at the continue_as_new boundary.

        Never re-runs investigate, the approval park, the stale-issue
        admission check, `find_proposal_slug`, the approve CWFT or the
        implement submit -- their results are already in `issue.resume`,
        produced by an earlier run of this same watch.
        """
        resume = issue.resume
        assert resume is not None  # only called when it is (see `run`)  # noqa: S101
        target_repo = _target_repo(issue)

        self._implement_state = resume.implement_state
        self._cadence = CADENCE if resume.fast_cadence else LEGACY_CADENCE
        self._shepherd_in_loop = resume.shepherd_in_loop
        self._approved = True
        self._approver = resume.approver
        self._owned_entity_id = resume.owned_entity_id
        self._owner_epoch = resume.owner_epoch
        self._owned_head_sha = resume.owned_head_sha
        self._poll_index_for_heartbeat = resume.poll_index_for_heartbeat
        self._claim_refused = resume.claim_refused
        self._claim_refused_until_poll = resume.claim_refused_until_poll
        self._refused_by_type = resume.refused_by_type
        self._refused_by_id = resume.refused_by_id
        self._refusals_observed = resume.refusals_observed
        self._unknown_acquires = resume.unknown_acquires
        self._unknown_progress = resume.unknown_progress
        self._unknown_heartbeats = resume.unknown_heartbeats
        self._proposal_ref = resume.proposal_ref
        self._policy_ref = resume.policy_ref
        self._last_lifecycle_op = resume.last_lifecycle_op
        self._last_lifecycle_op_landed = resume.last_lifecycle_op_landed
        self._claim_abandoned = resume.claim_abandoned
        # Re-checked before the first sleep of this run (`_watch_pr`'s own
        # first statement observes `_abandoned`): a signal that landed in
        # the previous run, or in the instant between the hop decision and
        # this run starting, still ends the watch. `resume.abandoned` is
        # always `False` by construction (`_merge_watch_hop_suggested`
        # never fires once `_abandoned` is set, so a resume record can
        # never carry `abandoned=True`) -- OR it in rather than assigning,
        # so an `abandon` signal delivered in the continue_as_new gap
        # (applied before `initialize_workflow` per temporalio's job
        # ordering) is never clobbered by this rehydration.
        self._abandoned = self._abandoned or resume.abandoned
        if self._abandoned and not self._abandon_reason:
            self._abandon_reason = resume.abandon_reason or "abandoned by operator"

        # Work-context binding (#267): same clobber hazard as `abandoned`
        # above — a `resume` signal delivered in the continue_as_new gap ran
        # against __init__'s empty state before this method did.
        self._rehydrate_work_context(resume)
        # A continued run is past every approval gate (it never re-runs
        # them), and from here its state is whole: the Update validator may
        # decide, and the deliveries the previous run left open resume
        # waiting for their fulfilment (#461).
        self._gates_passed = True
        self._initialized = True
        for rid in sorted(self._open_deliveries):
            self._start_delivery(self._open_deliveries[rid])

        outcome = await self._watch_pr(resume.service, resume.slug, resume=resume)
        if outcome.resume is not None:
            await self._settle_deliveries(hopping=True)
            next_resume = self._carry_work_context(
                dataclasses.replace(
                    outcome.resume,
                    investigate=resume.investigate,
                    implement=resume.implement,
                    approve=resume.approve,
                    implement_state=self._implement_state,
                    approver=self._approver,
                    human_input=resume.human_input,
                )
            )
            workflow.continue_as_new(
                IssueRef(issue_url=issue.issue_url, work_item_id=issue.work_item_id, resume=next_resume),
            )

        investigate_result = resume.investigate
        # A resume record is only ever built after investigate has already
        # succeeded (run() only reaches _watch_pr past that point), so this
        # is always set in practice -- asserted so the type checker (and a
        # reader) can see the invariant rather than infer it.
        assert investigate_result is not None  # noqa: S101
        return await self._finish_after_watch(
            target_repo=target_repo,
            investigate_result=investigate_result,
            implement_result=resume.implement,
            approve_result=resume.approve,
            human_input_outcome=resume.human_input,
            outcome=outcome,
        )

    async def _finish_after_watch(
        self,
        *,
        target_repo: str,
        investigate_result: WorkflowResult,
        implement_result: WorkflowResult | None,
        approve_result: WorkflowResult | None,
        human_input_outcome: HumanInputOutcome | None,
        outcome: _WatchOutcome,
    ) -> DevLoopResult:
        """Shared tail of `run`/`_resume_merge_watch`: deploy observation,
        incident watch, and the final `DevLoopResult` -- reached whether or
        not the merge watch hopped across a continue_as_new boundary along
        the way (mctl-agents#404 v2). Identical to the pre-#404-v2 tail of
        `run`, just factored out so the resume path does not duplicate it.
        """
        pr_state = outcome.last

        # Stages 6.2/6.3 (ADR-006, #215): only a merged PR produces a
        # release to observe. A closed-unmerged or still-open PR ends the
        # loop here, exactly as before.
        # Stage 6.4's window opens HERE, not after the deploy observation:
        # _observe_deploy can block for over an hour, and a rollout that
        # breaks the app does it immediately — those incidents fire while
        # the observation is still running, and a window opened afterwards
        # would miss exactly the ones worth catching (agy P2).
        watch_since = workflow.now().isoformat().replace("+00:00", "Z")
        deploy: DeployObservation | None = None
        if (
            workflow.patched("deploy-observation")
            and pr_state is not None
            and pr_state.merged
        ):
            deploy = await self._observe_deploy(target_repo, pr_state)

        # Stage 6.4 (ADR-006, #216): only worth asking when something
        # actually rolled out. no-release/no-target mean nothing shipped,
        # so any incident in the window belongs to someone else.
        incidents: IncidentWatch | None = None
        if (
            workflow.patched("incident-watch")
            and deploy is not None
            and deploy.outcome in ("healthy", "unverified")
            and deploy.app
        ):
            incidents = await self._watch_incidents(deploy.app, watch_since)

        return DevLoopResult(
            investigate=investigate_result,
            implement=implement_result,
            approve=approve_result,
            pr=pr_state,
            deploy=deploy,
            incidents=incidents,
            human_input=human_input_outcome,
            # mctl-agents#420: an `abandon` signal delivered during the merge
            # watch cuts _watch_pr short (its own `while` condition observes
            # `_abandoned`) rather than raising, so the only place left to
            # record it is here, on the result the watch's caller returns.
            # Correct across a hop too: `_abandoned`/`_abandon_reason` are
            # rehydrated by `_resume_merge_watch` before this is ever
            # reached (mctl-agents#404 v2).
            ended=f"abandoned: {self._abandon_reason}" if self._abandoned else "",
        )

    async def _implement(
        self,
        implementer_release: ResolvedRelease | None,
        params: dict[str, str],
        target_repo: str,
    ) -> WorkflowResult:
        """Submit the implementer, requeue pre-start failures, fail loudly.

        Before #395 this was one submit whose result was recorded and then
        carried to the end of the loop: an implementer that never ran and
        one that ran and failed both ended the workflow as Completed with
        `implement.phase == "Failed"`. On 2026-09-19 six such loops read as
        success while six approved proposals sat untouched.

        Now the outcome is classified (implement_outcome.py) and:

        - `pre_start` is resubmitted, up to MAX_PRESTART_REQUEUES, without
          counting an implementation attempt — nothing was attempted;
        - `execution` and `finalization` fail the workflow with a typed
          ApplicationError carrying the result, so the loop's terminal
          status is the truth and a human is pointed at the right layer.

        Guarded by `implement-outcome`: an execution that predates the
        marker keeps the recorded behaviour (single submit, Completed).
        """
        requeues = 0
        while True:
            self._implement_state = ImplementExecutionState(
                stage="implementer",
                queued_at=workflow.now().isoformat().replace("+00:00", "Z"),
                prestart_requeues=requeues,
            )
            result = await _run_cwft(IMPLEMENTATION_OPERATION, params)

            # classify() and render_pre_start_reason() are pure lookups over
            # `result` — no workflow command is issued — so computing them
            # here is safe. What must NOT move is `_record`'s activity
            # relative to `workflow.patched("implement-outcome")` below: every
            # workflow already past this point has that activity recorded in
            # history BEFORE the patch marker, and reordering them makes
            # replay diverge from history and wedges the workflow.
            outcome: Outcome = classify(
                result.phase,
                implementer_ran=result.implementer_ran,
                implementer_phase=result.implementer_phase,
                finalization_phase=result.finalization_phase,
            )
            # Only meaningful alongside a pre_start verdict: render() maps
            # `None` to "unknown", so a success/execution/finalization
            # outcome must not be given a reason it never had.
            reason = render_pre_start_reason(result.pre_start_reason) if outcome == "pre_start" else None
            await _record(
                "implementer", implementer_release, result, target_repo, outcome=outcome, pre_start_reason=reason
            )

            if not workflow.patched("implement-outcome"):
                return result

            self._implement_state = dataclasses.replace(
                self._implement_state, outcome=outcome, pre_start_reason=reason
            )

            if outcome == "success":
                return result
            if outcome == "pre_start" and requeues < MAX_PRESTART_REQUEUES:
                requeues += 1
                workflow.logger.warning(
                    "implementer for %s never started (%s, %s, %s); requeueing %d/%d without "
                    "counting an attempt",
                    target_repo,
                    result.workflow_name,
                    result.phase,
                    reason,
                    requeues,
                    MAX_PRESTART_REQUEUES,
                )
                await workflow.sleep(PRESTART_REQUEUE_BACKOFF)
                continue

            error_type = {
                "pre_start": "ImplementationNotStarted",
                "execution": "ImplementationFailed",
                "finalization": "ImplementationFinalizationFailed",
            }[outcome]
            raise ApplicationError(
                f"implementation of {target_repo} ended {result.phase} ({outcome}) in Argo "
                f"workflow {result.workflow_name}"
                + (f" after {requeues} pre-start requeues" if requeues else "")
                + (f" ({reason})" if outcome == "pre_start" else "")
                + (
                    f": {finalization_evidence(result.finalization_phase)}"
                    if outcome == "finalization"
                    else ""
                ),
                result,
                type=error_type,
                non_retryable=True,
            )

    async def _watch_incidents(self, service: str, since: str) -> IncidentWatch:
        """Collect incidents raised against ``service`` during the window.

        Observational and terminal: whatever it finds lands in the result
        for a human to read. No remediation, no rollback, and no failing
        the workflow — by this point implement, the merge and the rollout
        have all already happened, and an incident here is information
        about the platform, not about this loop's success.
        """
        deadline = workflow.now() + INCIDENT_WATCH_WINDOW
        # Derived, not the constant: `since` predates the deploy
        # observation, which can add an hour of its own, so reporting a
        # flat 30 would understate the span these incidents were drawn
        # from (claude P2).
        try:
            window_minutes = int((deadline - _as_utc(since)).total_seconds() // 60)
        except (ValueError, TypeError):
            window_minutes = int(INCIDENT_WATCH_WINDOW.total_seconds() // 60)
        seen: dict[str, Incident] = {}
        truncated = False
        detail: str | None = None
        while workflow.now() < deadline:
            await workflow.sleep(INCIDENT_POLL_INTERVAL)
            try:
                result: IncidentQueryResult = await workflow.execute_activity(
                    list_service_incidents,
                    args=[service, since],
                    start_to_close_timeout=DEPLOY_READ_TIMEOUT,
                    retry_policy=DEPLOY_READ_RETRY_POLICY,
                )
            except ActivityError as exc:
                # Keep watching: one failed read must not discard the
                # incidents already collected, nor end the window early.
                detail = f"at least one incident read failed: {exc.cause!r}"
                workflow.logger.warning(
                    "incident read failed for %s — continuing the watch: %r", service, exc.cause
                )
                continue
            truncated = truncated or result.truncated
            for incident in result.incidents:
                # Deduplicated by id across polls: an incident firing for
                # the whole window would otherwise be reported once per
                # poll. First sighting wins, so the recorded status is the
                # one it had when this loop first saw it.
                seen.setdefault(incident.id, incident)
        if seen:
            workflow.logger.warning(
                "incident watch for %s saw %d incident(s) within %d minute(s) "
                "of the rollout",
                service,
                len(seen),
                window_minutes,
            )
        return IncidentWatch(
            watched=True,
            service=service,
            window_minutes=window_minutes,
            since=since,
            incidents=list(seen.values()),
            truncated=truncated,
            detail=detail,
        )

    async def _observe_deploy(self, service: str, pr_state: PRState) -> DeployObservation:
        """Watch this merge's release land, then verify the rollout (#215).

        Read-only and non-fatal by construction: every unhappy path
        returns a DeployObservation describing what was seen. implement
        already succeeded and the PR already merged — an unobserved deploy
        must not turn that into a failed workflow, and this loop has no
        rollback to offer (wft-rollback-service stays human-invoked).
        """
        repo = pr_state.repo or f"mctlhq/{service}"
        try:
            target: DeployTarget | None = await workflow.execute_activity(
                resolve_deploy_target,
                args=[repo],
                start_to_close_timeout=DEPLOY_READ_TIMEOUT,
                retry_policy=DEPLOY_READ_RETRY_POLICY,
            )
        except ActivityError as exc:
            return DeployObservation(
                outcome="unverified",
                detail=f"could not resolve the deploy target for {repo}: {exc.cause!r}",
            )
        if target is None:
            return DeployObservation(
                outcome="no-target",
                detail=f"{repo} releases deploy no application",
            )

        # merged_at is absent on PRStates recorded before the field
        # existed; the merge commit is still minutes old at worst here, so
        # anchoring on "now" only risks missing a release cut in the same
        # instant, which the next poll picks up anyway.
        after = pr_state.merged_at or workflow.now().isoformat().replace("+00:00", "Z")
        release, failure = await self._await_release(repo, after)
        if failure is not None:
            # A broken read is not the same as nothing being released, and
            # labelling it no-release would hide a defect behind an
            # outcome that reads as normal (agy P2).
            return DeployObservation(
                outcome="unverified",
                team=target.team,
                app=target.app,
                detail=failure,
            )
        if release is None:
            return DeployObservation(
                outcome="no-release",
                team=target.team,
                app=target.app,
                detail=f"no release published for {repo} within {RELEASE_LOOKUP_DEADLINE}",
            )
        return await self._verify_rollout(target, release)

    async def _await_release(self, repo: str, after: str) -> tuple[ReleaseInfo | None, str | None]:
        """Poll until release-please publishes a release newer than ``after``.

        Returns (release, failure). A failure string means the lookup
        itself broke; the caller must not read that as "nothing was
        released", which is what a bare None would have looked like.
        """
        deadline = workflow.now() + RELEASE_LOOKUP_DEADLINE
        while workflow.now() < deadline:
            try:
                release: ReleaseInfo | None = await workflow.execute_activity(
                    get_release_after,
                    args=[repo, after],
                    start_to_close_timeout=DEPLOY_READ_TIMEOUT,
                    retry_policy=DEPLOY_READ_RETRY_POLICY,
                )
            except ActivityError as exc:
                if not _is_transient(exc):
                    workflow.logger.warning(
                        "release lookup for %s failed with a non-transient error — "
                        "giving up on the release watch: %r",
                        repo,
                        exc.cause,
                    )
                    return None, f"release lookup failed with a non-transient error: {exc.cause!r}"
                workflow.logger.warning(
                    "release lookup failed for %s — retrying next interval: %r", repo, exc.cause
                )
                await workflow.sleep(RELEASE_POLL_INTERVAL)
                continue
            if release is not None:
                return release, None
            await workflow.sleep(RELEASE_POLL_INTERVAL)
        return None, None

    async def _verify_rollout(self, target: DeployTarget, release: ReleaseInfo) -> DeployObservation:
        """Wait until the app reports Synced/Healthy on the released tag."""
        deadline = workflow.now() + DEPLOY_VERIFY_DEADLINE
        last: DeployStatus | None = None
        polls_without_app = 0
        while workflow.now() < deadline:
            try:
                status: DeployStatus = await workflow.execute_activity(
                    get_deploy_status,
                    args=[target.team, target.app],
                    start_to_close_timeout=DEPLOY_READ_TIMEOUT,
                    retry_policy=DEPLOY_READ_RETRY_POLICY,
                )
            except ActivityError as exc:
                if not _is_transient(exc):
                    return DeployObservation(
                        outcome="unverified",
                        team=target.team,
                        app=target.app,
                        release_tag=release.tag,
                        detail=f"deploy status read failed with a non-transient error: {exc.cause!r}",
                    )
                workflow.logger.warning(
                    "deploy status read failed for %s/%s — retrying next interval: %r",
                    target.team,
                    target.app,
                    exc.cause,
                )
                await workflow.sleep(DEPLOY_POLL_INTERVAL)
                continue
            if not status.found:
                # A PR can introduce a NEW application, which ArgoCD only
                # registers a little after the release lands (app-of-apps)
                # — so this is a pending state at first, not a verdict
                # (agy P2). Still bounded: after the grace polls a name
                # that resolves to nothing is a wrong name, and waiting
                # out the full deadline would only delay the same answer.
                polls_without_app += 1
                if polls_without_app >= NEW_APP_GRACE_POLLS:
                    return DeployObservation(
                        outcome="unverified",
                        team=target.team,
                        app=target.app,
                        release_tag=release.tag,
                        detail=(
                            f"no ArgoCD application {target.team}/{target.app} after "
                            f"{polls_without_app} polls"
                        ),
                    )
                await workflow.sleep(DEPLOY_POLL_INTERVAL)
                continue
            polls_without_app = 0
            last = status
            # mctl-api resolves no service record — and therefore no image
            # tag — for platform applications such as mctl-api itself.
            # Waiting for a tag that will never be reported would make
            # every such loop time out, so sync+health is the signal there.
            if status.image_tag is not None:
                landed = status.image_tag == release.tag
            else:
                # No service record, so no tag to compare (mctl-api's own
                # platform application). Healthy/Synced alone would be
                # satisfied by the state the app was ALREADY in before this
                # release synced, so the first poll would report success
                # without anything having happened (claude P3). ArgoCD's
                # own updatedAt is the freshness signal: it must be at
                # least as new as the release we are waiting for.
                landed = _at_or_after(status.updated_at, release.published_at)
            if landed and status.health == "Healthy" and status.sync_status == "Synced":
                return DeployObservation(
                    outcome="healthy",
                    team=target.team,
                    app=target.app,
                    release_tag=release.tag,
                    image_tag=status.image_tag,
                    health=status.health,
                    sync_status=status.sync_status,
                )
            await workflow.sleep(DEPLOY_POLL_INTERVAL)
        return DeployObservation(
            outcome="unverified",
            team=target.team,
            app=target.app,
            release_tag=release.tag,
            image_tag=last.image_tag if last else None,
            health=last.health if last else None,
            sync_status=last.sync_status if last else None,
            detail=f"still not Synced/Healthy on {release.tag} after {DEPLOY_VERIFY_DEADLINE}",
        )

    async def _shepherd_tick(self, service: str, slug: str) -> None:
        """One in-loop shepherd run for exactly this proposal (#213).

        The same one-shot the cron ran, scoped to service+slug (which puts
        run_shepherd in targeted mode, bypassing the ownership filter). A
        failed tick is logged, not raised: the watch itself is the durable
        part, and the next boundary retries.
        """
        try:
            # Pin the released shepherd image exactly like the
            # investigate/implement steps do, so a promotion or rollback in
            # the agent registry reaches in-loop ticks too — otherwise
            # DevLoop-owned proposals would silently run the CWFT's
            # baked-in default while cron-driven ones ran the intended
            # release.
            shepherd_release = _require_release("shepherd", await _resolve("shepherd"))
            tick_params = {"service": service, "slug": slug}
            if shepherd_release and shepherd_release.image_ref:
                tick_params["agent_image"] = shepherd_release.image_ref
                tick_params["agent_version"] = f"shepherd@{shepherd_release.version}"
            tick_result = await _run_cwft("mctl-agents-shepherd", tick_params)
            await _record("shepherd", shepherd_release, tick_result, service)
        except ActivityError as exc:
            workflow.logger.warning(
                "in-loop shepherd tick failed for %s/%s — the watch "
                "continues: %r",
                service,
                slug,
                exc.cause,
            )
        except ApplicationError as exc:
            if exc.type != "AgentReleaseMissing":
                raise
            # The watch gated this before claiming ownership, so getting
            # here means the pin disappeared mid-watch (a rollback that
            # unpublished the row). Not fatal at this point: the loop is
            # already the owner, and tearing it down would abandon merge
            # detection for a PR that is open and being reviewed. Skip the
            # tick loudly instead — the next boundary retries, and the
            # message says which agent to republish.
            workflow.logger.error(
                "in-loop shepherd tick for %s/%s skipped: %s", service, slug, exc.message
            )
        except Exception as exc:  # noqa: BLE001 — a bug in the tick must not sink silently
            # Running as a task means an escaping exception is stored on
            # the task instead of raised: nothing would surface it, and
            # the next poll overwrites the reference. Log it here, at the
            # only point that still has the context.
            workflow.logger.error(
                "in-loop shepherd tick for %s/%s raised an unexpected %s: %r",
                service,
                slug,
                type(exc).__name__,
                exc,
            )

    async def _settle_tick(self, tick_task: asyncio.Task[None] | None, service: str, slug: str) -> None:
        """Leave no pending tick behind when the watch ends (#231).

        A workflow that completes with an outstanding task is a Temporal
        error, and once the PR is merged/closed the tick's verdict is moot
        — so cancel rather than wait, which is the whole point of running
        it concurrently. Cancelling the activity does not stop the Argo
        workflow it submitted; that run finishes on its own, exactly as it
        would have if this loop had never waited for it.
        """
        if tick_task is None:
            return
        if tick_task.done():
            _drain_tick(tick_task, service, slug)
            return
        workflow.logger.info(
            "cancelling an in-flight shepherd tick for %s/%s — the watch is ending",
            service,
            slug,
        )
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            # The expected outcome of the cancel above. CancelledError is a
            # BaseException since 3.8, so `except Exception` does NOT catch
            # it: letting it escape here would propagate out of _watch_pr's
            # finally, discard the state the watch was about to return, and
            # fail a workflow whose implement and merge both succeeded —
            # in exactly the case this concurrency exists to handle.
            workflow.logger.debug("in-flight shepherd tick cancelled for %s/%s", service, slug)
        except Exception as exc:  # noqa: BLE001 — whatever the tick raised on its way out
            workflow.logger.warning(
                "in-flight shepherd tick for %s/%s ended with %r", service, slug, exc
            )

    async def _ownership(self, op: str, *, repo: str, number: int, head_sha: str = "",
                         evidence: str = "", reason: str = "") -> OwnershipResult | None:
        """Run one ownership operation as an ACTIVITY.

        Never raises. Ownership is a coordination signal, not the work: a
        workflow that died because it could not reach the ownership store
        would trade a bookkeeping outage for a delivery outage, and the loop
        has already produced a PR by this point.

        A failure returns None. On the unclaimed path that means "no claim" —
        the conservative direction, since the cron sweeper only stands down for
        a positive claim. On the two CLAIMED paths (progress, heartbeat) it
        means "this write did not land": the claim is kept, the counter
        advances, and the give-up below is what eventually drops it. Collapsing
        the two readings is how a transient 503 used to cost a claim.
        """
        info = workflow.info()
        req = OwnershipRequest(
            op=op,
            kind="pull-request",
            entity_id=EntityRef.for_pull_request(repo, number, head_sha).id,
            phase="review-remediation",
            version=head_sha,
            owner_type=OWNER_TYPE,
            owner_id=info.workflow_id,
            epoch=self._owner_epoch,
            evidence=evidence,
            reason=reason,
            proposal_ref=self._proposal_ref,
            policy_ref=self._policy_ref,
            temporal_workflow_id=info.workflow_id,
            # NOTE: `handoff` is the only op that reads these, and this
            # workflow never hands its claim to a named successor — it
            # releases and lets the next holder acquire. The parameter that
            # used to thread an `Owner` here had no caller passing it in any
            # of the five call sites, so it was a dead branch carrying an
            # import (claude P3 on `8ac2080`). Add it back with the caller
            # that needs it, not before.
            to_owner_type="",
            to_owner_id="",
        )
        try:
            return await workflow.execute_activity(
                lifecycle_ownership,
                req,
                start_to_close_timeout=LIFECYCLE_TIMEOUT,
                retry_policy=LIFECYCLE_RETRY_POLICY,
            )
        except asyncio.CancelledError:
            # CancelledError is a BaseException, so `except Exception` below
            # does NOT catch it. This method is called from _watch_pr's finally,
            # and letting it escape there would discard the state the watch was
            # about to return and fail a workflow whose implement and merge both
            # succeeded — the exact hazard _settle_tick documents one line
            # earlier. Swallowing it here does not swallow the workflow's own
            # cancellation: whatever triggered the unwind keeps propagating out
            # of the try block this finally belongs to.
            #
            # What this does NOT do is complete the write. On cancellation the
            # activity never runs, so the row is left `active` and the
            # reconciler's liveness bound is what recovers it once this
            # execution stops being seen. Shielding it is possible in principle
            # (asyncio.shield applies), and deliberately not done: it would
            # hold a cancelling workflow open on a store that may itself be why
            # the unwind started. Pretending the release lands would be worse
            # than saying it does not.
            workflow.logger.info(
                "lifecycle %s cancelled for %s#%s — the watch is ending", op, repo, number
            )
            return None
        except Exception as exc:  # noqa: BLE001 — ActivityError and friends
            workflow.logger.warning("lifecycle %s failed for %s#%s: %r", op, repo, number, exc)
            return None


    def _lost_to_someone_else(self, result: OwnershipResult) -> bool:
        """Is the OWNED_BY_OTHER record naming somebody other than this loop?

        It can name US. `verdict_for` answers OWNED_BY_OTHER for an `active`
        record whose owner IS the caller whenever `healthy` is False:

            if asking is not None and own.owner == asking and own.healthy:
                return OWNED_BY_ME
            return OWNED_BY_OTHER

        and both callers of `_lose_claim` sit on the CLAIMED path, which is
        precisely where the owner named back is us. Without this check the log
        read "lost mctlhq/mctl-web#99 to devloop-workflow/dev-loop-…-99" — this
        loop's own workflow_id — and the refusal then ended every
        ownership call for the remaining watch INCLUDING the heartbeat, which
        is the write whose absence made the row unhealthy in the first place.
        One unhealthy read of our own row permanently stopped the write that
        would have restored it.

        The reachable case does not depend on how mctl-api defines `healthy`:
        after a ~10 h gap — a pod restart, or the /acquire outage the heartbeat
        give-up below handles — the row is dead, and the first call that does
        reach the store returns OUR OWN record unhealthy. If `healthy` also
        excludes `stuck`, every PR quiet for longer than the 48 h progress
        bound trips it too, which ADR-010 §4 note 2 calls the correct
        behaviour of a healthy owner.

        Our own row read back unhealthy is handled where it belongs: the
        heartbeat block counts it, logs it, and gives up the claim WITHOUT
        recording a refusal, so the loop re-acquires under its own gate.
        """
        # A non-empty id is half the question. A 409 whose body is not a
        # record answers OWNED_BY_OTHER with `owner_id=""` (answer_from's
        # `record_of(payload) is None` branch), and "" != our workflow_id — so
        # that reached _lose_claim, logged `lost … to /`, and recorded a
        # refusal naming nobody, on a refusal that may well have been caused by
        # this loop's own stale belief.
        return bool(result.owner_id) and result.owner_id != workflow.info().workflow_id

    def _refuse_claim(self, result: OwnershipResult) -> None:
        """Record that a named owner refused this loop's claim, with an expiry.

        The expiry is the whole point. A refusal used to be permanent, and the
        flag was reachable from the PROGRESS branch — so a single
        OWNED_BY_OTHER on one /progress write ended every later ownership call
        for the rest of the watch, the liveness heartbeat included. The owner
        that refused could then be force-released at the 10 h bound (ADR-010
        §5) and the entity sit claimable for thirteen days with a live loop
        beside it that had stopped asking.
        """
        same_owner = (
            result.owner_type == self._refused_by_type
            and result.owner_id == self._refused_by_id
        )
        self._refusals_observed = self._refusals_observed + 1 if same_owner else 1
        self._refused_by_type = result.owner_type
        self._refused_by_id = result.owner_id
        self._claim_refused = True
        self._claim_refused_until_poll = (
            self._poll_index_for_heartbeat + self._cadence.refusal_backoff_polls
        )

    def _finish_claim(
        self, op: str, result: OwnershipResult | None, repo: str, number: int
    ) -> None:
        """Let go of the claim, but only if the relinquishing write landed.

        One writer for both relinquishing paths — the in-loop terminal on a
        MERGED/CLOSED poll, and the cleanup in _watch_pr's finally. They stated
        the same policy in two places and phrased it differently, which is how
        one of them came to clear the claim on a failed write while the other
        did not.

        Clearing on a write that did not land records that this loop let go of
        a row the store still shows as ACTIVE: the zero-owner state, produced
        by the cleanup meant to prevent it. Keeping the claim instead leaves
        the row to the reconciler's liveness bound, which is the mechanism that
        exists for it.
        """
        landed = result is not None and result.accepted
        self._last_lifecycle_op = op
        self._last_lifecycle_op_landed = landed
        # Reflects the LAST write, not "ever failed". The in-loop terminal can
        # fail and the cleanup's retry then land — at which point the claim is
        # not abandoned, and a sticky flag would report abandoned=True beside
        # entity_id="" and last_op_landed=True, contradicting the invariant
        # this query exists to make checkable.
        self._claim_abandoned = not landed
        if landed:
            self._owned_entity_id = ""
            # The epoch goes with it, as at every other site that drops the
            # claim (_lose_claim, the heartbeat's UNOWNED arm, the give-up).
            # It is a fencing generation for a row this loop no longer holds,
            # and `lifecycle_claim` exposes it — so leaving it set reports a
            # live epoch beside an empty entity_id, in the query added to make
            # that pair legible.
            self._owner_epoch = 0
            return
        # NOT the stable metric prefix. This call may not be the last one: the
        # in-loop terminal can fail here and the cleanup in _watch_pr's finally
        # then retries the same write. Counting an abandonment per failed
        # ATTEMPT would report one for an entity the retry goes on to release
        # — a false positive in the soak — and two for one that is genuinely
        # abandoned. The metric is emitted once, at the end of the watch, by
        # _report_claim_abandonment.
        workflow.logger.warning(
            "lifecycle: %s for %s#%s did not land; the claim is kept for now",
            op,
            repo,
            number,
        )

    def _report_claim_abandonment(self) -> None:
        """Emit the abandonment metric ONCE, after the last relinquishing write.

        Separate from _finish_claim because that runs per attempt and this
        counts entities. `lifecycle_claim_abandoned_total` is built from this
        prefix, and a counter that advances per retry answers a different
        question from the one an operator is asking.

        Called at the very end of the watch, where `_claim_abandoned` has its
        terminal value — the same point the query reads.
        """
        if not self._claim_abandoned:
            return
        # Stable prefix so this is countable in production, not only assertable
        # in a test: the worker's namespace is already inside the Promtail
        # metrics-stage selector, so one regex turns it into a counter.
        workflow.logger.warning(
            "LIFECYCLE-CLAIM-ABANDONED entity=%s op=%s reason=%s",
            self._owned_entity_id or "<unknown>",
            self._last_lifecycle_op or "<none>",
            "the relinquishing write did not land; the reconciler reaps the "
            "row at the liveness bound",
        )

    def _forget_refusal(self) -> None:
        """Clear the refusal and its streak.

        Called when this loop positively holds the entity: the streak counts
        one continuous hold by one competitor, and holding the entity ourselves
        ends any such run by definition.
        """
        self._claim_refused = False
        self._claim_refused_until_poll = 0
        self._refused_by_type = ""
        self._refused_by_id = ""
        self._refusals_observed = 0

    def _refusal_still_holds(self) -> bool:
        """Should the acquire be skipped because somebody else owns this?

        Returns False — i.e. ask again — once the backoff expires, which is how
        a force-released row is ever reclaimed by a loop that was refused once.
        """
        if not self._claim_refused:
            return False
        if not workflow.patched("lifecycle-refusal-backoff"):
            # Executions that started before this patch replay the refusal as
            # permanent. Not a policy choice: re-testing schedules an activity
            # their history does not contain.
            return True
        if self._refusals_observed >= LIFECYCLE_REFUSAL_GIVE_UP:
            # The same owner has held it across every re-test. This is the case
            # the permanent flag described correctly.
            return True
        if self._poll_index_for_heartbeat < self._claim_refused_until_poll:
            return True
        workflow.logger.info(
            "lifecycle: re-testing the claim refused by %s/%s (refusal %d of %d) "
            "— the owner may have been force-released since",
            self._refused_by_type or "<unnamed>",
            self._refused_by_id or "<unnamed>",
            self._refusals_observed,
            LIFECYCLE_REFUSAL_GIVE_UP,
        )
        self._claim_refused = False
        return False

    def _lose_claim(self, repo: str, number: int, result: OwnershipResult) -> None:
        """Another actor holds this PR now. Stop claiming to own it.

        Callers MUST gate on `_lost_to_someone_else` — the verdict alone does
        not mean a competitor, only that the record is not usable as ours.

        The epoch is NOT adopted. It belongs to the winner, and carrying it
        would make every later call from this loop assert a fencing generation
        it never held — which the server would reject, and which would read in
        the events as this workflow trying to act on somebody else's claim.
        """
        workflow.logger.info(
            "lifecycle: lost %s#%s to %s/%s — this loop no longer claims it",
            repo, number, result.owner_type, result.owner_id,
        )
        self._owned_entity_id = ""
        self._owner_epoch = 0
        self._owned_head_sha = ""
        self._unknown_progress = 0
        self._refuse_claim(result)

    async def _track_ownership(self, state: PRState) -> None:
        """Claim the PR, then keep the claim honest.

        Three distinct things, deliberately not collapsed into one call:

        - **Claim.** The first successful poll that resolves a PR acquires
          ownership. It cannot happen earlier: until then this loop knows a
          service and a slug, and the entity ownership attaches to is the pull
          request.
        - **Liveness.** Every LIFECYCLE_HEARTBEAT_EVERY_POLLS-th poll
          re-acquires, which is idempotent and refreshes ``last_seen_at``
          without touching the epoch or the progress timestamp. It says "this
          owner still exists", which is all a heartbeat is evidence of.
        - **Progress.** Only when the head SHA actually moved. A poll that
          observed nothing new must not write progress: the whole point of
          separating the two timestamps is that an owner cannot prove
          usefulness by continuing to breathe.

        A failed call leaves ``_owned_entity_id`` empty, so the loop simply
        holds no claim and the cron sweeper keeps the PR — the same
        fail-toward-the-sweeper direction the shepherd claim already takes.
        """
        repo = state.repo or ""
        number = state.number or 0
        head = state.head_sha or ""

        if not self._owned_entity_id:
            if self._backed_off(self._unknown_acquires):
                # The store has been unreachable, or answering something
                # unusable, for this many consecutive attempts. Re-asking on
                # every poll for the rest of a 14-day watch is ~1340 activities
                # against an endpoint that is not answering; the sweeper owns
                # the PR meanwhile, which is the same outcome as before any of
                # this existed.
                return
            if self._refusal_still_holds():
                # Somebody else owns this PR and said so. Re-asking on every
                # poll for the rest of a 14-day watch is ~1340 activities to
                # re-learn one fact; the reconciler is what resolves a
                # conflict, not this loop.
                #
                # Believed for LIFECYCLE_REFUSAL_BACKOFF_POLLS, not forever:
                # the owner named in the refusal can be force-released inside
                # this watch, and a loop that never asks again cannot notice.
                return
            result = await self._ownership(
                "acquire", repo=repo, number=number, head_sha=head
            )
            if result is not None and result.owned_by_caller:
                self._owned_entity_id = EntityRef.for_pull_request(repo, number).id
                self._owner_epoch = result.epoch
                self._owned_head_sha = head
                self._unknown_acquires = 0
                # The refusal EPISODE is over, so its streak ends with it.
                #
                # LIFECYCLE_REFUSAL_GIVE_UP counts CONSECUTIVE re-tests that
                # keep naming the same owner — one competitor holding the
                # entity across the whole window. A successful acquire in
                # between is proof of the opposite: this loop got the entity,
                # so whatever the earlier refusals were, they were not one
                # unbroken hold.
                #
                # Without this reset the counter survives the recovery, and a
                # fixed-identity actor (a steward, a sweeper) refusing this
                # loop once per episode across three SEPARATE episodes — each
                # interrupted by a genuine re-acquire — trips the permanent
                # give-up that is meant to describe a single continuous one.
                self._forget_refusal()
            elif (
                result is not None
                and result.verdict == OWNED_BY_OTHER
                and self._lost_to_someone_else(result)
            ):
                # Somebody else owns this PR. Nothing to escalate here: the
                # reconciler is what resolves a conflict, and this loop simply
                # does not record itself as the owner.
                workflow.logger.info(
                    "lifecycle: %s#%s is owned by %s/%s — this loop holds no claim",
                    repo, number, result.owner_type, result.owner_id,
                )
                self._refuse_claim(result)
            elif result is not None and result.accepted and result.verdict == UNKNOWN:
                # The server took the write and told us nothing more. It is not
                # a claim — no epoch came back, and a claim without an epoch is
                # a fencing generation this loop cannot assert — but it is also
                # not a failure, and silently taking neither branch is how a
                # loop ends up never claiming and never backing off.
                #
                # NOT on `verdict == WROTE_NO_RECORD`, which is what this arm
                # used to read. That verdict is UNREACHABLE from here:
                # `answer_from` answers it only for a RELINQUISHING route, and
                # `RELINQUISHING_PATH_SUFFIXES` is closed on ("/release",
                # "/terminal") — deliberately, because the verdict's
                # `blocks_others` is False and an acquire leaves the CALLER
                # holding the entity. A body-less 2xx on /acquire arrives here
                # as UNKNOWN with `accepted` True.
                #
                # So the predicate is the pair: `accepted` is the contract's
                # "mctl-api took the write", `verdict == UNKNOWN` is "and told
                # us nothing usable". Both halves are load-bearing — accepted
                # alone also covers a 2xx carrying a released record (UNOWNED)
                # or one naming somebody else, which the arms above and below
                # answer differently.
                workflow.logger.info(
                    "lifecycle: acquire for %s#%s was accepted but told us "
                    "nothing usable (state=%r, %s); no claim recorded this poll",
                    repo, number, result.state,
                    result.reason or "no reason given",
                )
                self._unknown_acquires += 1
            else:
                # A None result — the activity failed outright, or was
                # cancelled — OR a real result this loop cannot use: an UNKNOWN
                # the server did not accept (503, 412, a 404 on the route, a
                # missing MCTL_TOKEN, an unrecognised state) and UNOWNED, which
                # is the common case and is why the log names `result.reason`
                # when there is one. Dereferencing a None would raise
                # AttributeError INSIDE
                # workflow code, which is not a FailureError: the workflow task
                # fails, and because replay is deterministic it fails the same
                # way forever, wedging the loop until somebody terminates it.
                #
                # The likeliest trigger is this change's own rollout: a control
                # worker on the previous image has no lifecycle_ownership
                # registered, the activity retries out, and execute_activity
                # raises. The fakes never raise, so no test reached it.
                self._unknown_acquires += 1
                workflow.logger.warning(
                    "lifecycle: could not establish ownership of %s#%s (%s) — "
                    "this loop holds no claim and the sweeper keeps the PR",
                    repo, number,
                    result.reason if result is not None else "the activity failed outright",
                )
            return

        if head and head != self._owned_head_sha and not self._backed_off(self._unknown_progress):
            result = await self._ownership(
                "progress",
                repo=repo,
                number=number,
                head_sha=head,
                evidence=f"head moved to {head[:8]}",
            )
            # `is not None` is NOT "the write succeeded". The activity never
            # raises — that is its contract — so a 503, a timeout, a missing
            # token and a lost claim all come back as a real result carrying an
            # unknown or owned-by-other verdict. Advancing _owned_head_sha on
            # those would drop the progress signal permanently: the next poll
            # sees head == _owned_head_sha and never retries.
            if result is not None and result.owned_by_caller:
                # RecordProgress refreshes last_seen_at server-side, so a
                # landed progress write IS this poll's heartbeat.
                self._owned_head_sha = head
                self._owner_epoch = result.epoch or self._owner_epoch
                self._unknown_progress = 0
                # ...so it resets the HEARTBEAT counter too. Both landed arms
                # return, which skips the heartbeat block on this poll, so the
                # counter neither advanced nor decayed across a write that
                # refreshed last_seen_at — and the give-up reads it as a count
                # of CONSECUTIVE missed heartbeats. With /acquire answering 503
                # while /progress stays healthy (the mirror of the case this
                # loop already handles) every progress write would land, the
                # heartbeat would fail on every fourth poll, and the claim
                # would be dropped on the premise that last_seen_at had not
                # moved for six hours — which its own landed writes disprove.
                self._unknown_heartbeats = 0
                return
            if (
                result is not None
                and result.accepted
                and result.verdict == UNKNOWN
                and result.state
            ):
                # A record came back and its STATE is one this image does not
                # recognise — `verdict_for` classifies against two CLOSED sets
                # and answers UNKNOWN rather than guessing, because this
                # container lags mctl-api by a release.
                #
                # This arm is a LOG, and nothing else. Deleting it leaves every
                # test green, because the fall-through below produces exactly
                # the behaviour it wants: the head is not advanced, so the
                # evidence is re-sent rather than treated as recorded, and
                # `_unknown_progress` closes the `_backed_off` gate so the
                # re-sends drop to the heartbeat cadence. Said here rather than
                # asserted, because a predicate no test can turn red is not a
                # guard — and the reason to keep it is that an unrecognised
                # state is otherwise completely silent on this path, while the
                # heartbeat names it.
                #
                # Two earlier versions of this arm did have behaviour and both
                # were wrong. The first advanced the head, which is the
                # body-less arm's behaviour applied to a record that says
                # somebody may hold the row. The second incremented
                # `_unknown_heartbeats` and returned, which inverted the
                # constant it counted into: that counter is three HEARTBEATS —
                # about six hours, inside the 10h liveness bound — and this arm
                # runs once per POLL, so the claim was dropped after three
                # polls, about ninety minutes. (Found by agy, not by this
                # suite.)
                #
                # Falling through also gives the right answer, not merely a
                # safe one. If the row really has moved to a state this image
                # cannot read, the heartbeat's own /acquire answers the same
                # way and counts it at the heartbeat cadence, where the
                # give-up's number means what it says. If only /progress is
                # answering strangely while /acquire still says the row is
                # ours, then it is ours and the claim should stand.
                workflow.logger.warning(
                    "lifecycle: progress on %s#%s came back in state %r, which "
                    "this image does not recognise — the head is not advanced "
                    "and the evidence will be re-sent",
                    repo, number, result.state,
                )
            if (
                result is not None
                and result.accepted
                and result.verdict == UNKNOWN
                and not result.state
            ):
                # The write LANDED — mctl-api took it — and carried NO record
                # at all (an empty state is the body-less 2xx), so the head
                # must advance, and
                # counting it as unanswered would be the opposite of what the
                # gate is for.
                #
                # This arm read `verdict == WROTE_NO_RECORD` for two rounds,
                # and that predicate is UNREACHABLE for /progress: `answer_from`
                # answers WROTE_NO_RECORD only for a route in
                # `RELINQUISHING_PATH_SUFFIXES`, and that list is closed on
                # ("/release", "/terminal") — with /progress named in its own
                # comment as one of the two routes an earlier version got
                # wrong, because /progress leaves the record active and owned
                # by the caller. So the defect below was still live and the
                # test that claimed to pin it was a false guard: the fake
                # hand-built the verdict for whatever op it was handed.
                #
                # Without this arm the same evidence is re-sent for the rest of
                # the watch, because _owned_head_sha never advances. That
                # refreshes last_progress_at forever for a head that stopped
                # moving, so the stuck bound can NEVER fire — the one property
                # the liveness/progress split exists to provide. Throttling to
                # the heartbeat cadence does not help: a refresh every two
                # hours clears a stuck bound just as well as one every thirty
                # seconds.
                #
                # `not result.state` is explicit rather than implied by the
                # arm above, which no longer returns: a record whose state this
                # image cannot read must not fall out of that arm and into this
                # one, where the head would advance after all.
                #
                # No epoch came back, so the caller keeps the one it had.
                self._owned_head_sha = head
                self._unknown_progress = 0
                self._unknown_heartbeats = 0
                return
            if (
                result is not None
                and result.verdict == OWNED_BY_OTHER
                and self._lost_to_someone_else(result)
            ):
                self._lose_claim(repo, number, result)
                return
            if (
                result is not None
                and result.accepted
                and result.verdict == OWNED_BY_OTHER
                and result.owner_type == OWNER_TYPE
            ):
                # The owner TYPE too, because `_lost_to_someone_else` cannot
                # carry this alone.
                #
                # It answers "is this owner TYPE ours", not "is this record
                # ours", and under the same premise — mctl-api emitting an
                # owner with no id — a record naming another DEVLOOP execution
                # reads as ours here. Second order: two DevLoops on one PR is
                # what this record exists to prevent, and with the id empty
                # there is no better predicate available. Written down so the
                # next reader does not have to re-derive that it is a known
                # limit rather than an oversight. `Ownership.from_payload` does not require
                # `owner.id`, so a 2xx carrying `owner: {"type": "pr-steward"}`
                # parses, answers OWNED_BY_OTHER, and is declined as a loss by
                # the `bool(result.owner_id)` guard — which exists for the
                # `lost … to /` case and is right to be there. Without this
                # conjunct such a record landed on an arm whose comment says
                # "our OWN record read back unhealthy", and the arm kept the
                # claim and advanced the head against a row a real competitor
                # holds. Narrow — it needs mctl-api to emit an owner with no id
                # — but the arm should cover what its comment says it covers.
                #
                # BELOW the guard above, deliberately: a real competitor can
                # answer with `accepted` True too, and must still reach
                # _lose_claim.
                #
                # And narrowed to OWNED_BY_OTHER, which is the only verdict
                # this arm's reasoning covers. Gated on `accepted` alone it
                # also caught UNOWNED — a 2xx carrying a RELEASED record has
                # `accepted` True — and then did the opposite of what the
                # fall-through below documents for exactly that verdict:
                # it kept the claim, so the unclaimed re-acquire path stayed
                # unreachable; advanced the head, so the progress signal was
                # treated as recorded although it landed on a row this loop
                # does not hold; and returned, so neither _backed_off nor the
                # give-up could ever fire. The heartbeat block puts its UNOWNED
                # arm ABOVE its accepted arm for this reason, and the two
                # blocks then answered the same wire shape in opposite ways.
                #
                # The cost was the §5 recovery this epic is built on: the pod
                # stalls past the liveness bound, the reconciler force-releases
                # the row, the pod resumes pushing fixes, and because every
                # poll moves the head every poll takes this arm and returns —
                # the heartbeat is never reached, which is the starvation shape
                # an earlier commit removed. A live worker and a zero-owner row
                # is the #239 gap this PR closes, reached from inside it.
                #
                # What is left here is our OWN record read back unhealthy.
                # `verdict_for` answers OWNED_BY_OTHER for an active row whose
                # owner IS the caller whenever `healthy` is False, and
                # `_lost_to_someone_else` correctly declines to call that a
                # loss — but mctl-api still TOOK the write, and the heartbeat
                # block has enumerated this shape since 089cb6a while this
                # branch matched none of its guards on it and fell to the
                # counter.
                #
                # Three costs, and the second is the one this PR exists to
                # prevent: `_unknown_progress` counted a write the store
                # accepted, which is the defect 089cb6a removed from the
                # heartbeat; `_owned_head_sha` never advanced, so the identical
                # evidence was re-sent for the rest of the watch, every send
                # landed, and last_progress_at was refreshed forever for a head
                # that stopped moving — so the stuck bound could never fire,
                # which is the argument already written on the body-less arm
                # above, reached by the shape that arm does not cover; and it
                # was silent, where the heartbeat gives this same record a
                # warning because ADR-010 §4 makes `stuck` an escalation.
                workflow.logger.warning(
                    "lifecycle: progress on %s#%s landed but the record is not "
                    "usable as ours (verdict=%s, state=%r, healthy=%s) — the "
                    "claim stands and the head advances",
                    repo, number, result.verdict, result.state, result.healthy,
                )
                self._owned_head_sha = head
                self._unknown_progress = 0
                self._unknown_heartbeats = 0
                return
            self._unknown_progress += 1

            # Everything else falls THROUGH to the heartbeat below, and the
            # unconditional return that used to sit here was a liveness bug.
            #
            # A failed progress write correctly does not advance
            # _owned_head_sha, so `head != _owned_head_sha` stays true on every
            # later poll and control reaches this block every time. Returning
            # here therefore made the heartbeat unreachable for the rest of the
            # watch: a /progress answering 412 — the record moved, which is
            # exactly what the fencing epoch is for — while `acquire` would
            # still have succeeded stopped refreshing last_seen_at entirely.
            # The 10h liveness bound then expires and the reconciler declares
            # this owner dead and force-releases the row, while the workflow is
            # alive, polling and shepherding the PR.
            #
            # UNOWNED had the mirror problem: progress against a row the
            # reconciler already released matches neither branch above, so the
            # claim was never dropped — and, because of the same return, never
            # re-acquired either. The heartbeat's acquire re-establishes it.
            #
            # The retries are bounded by _backed_off on the way IN, with the
            # counter this arm advances. Without it a /progress that 500s from
            # poll 2 onward costs one activity per poll for the remaining ~1340
            # polls of a fourteen-day watch — precisely the history cost
            # LIFECYCLE_UNKNOWN_WRITE_LIMIT exists to refuse, arriving by the
            # path that falling through created. An earlier version of this
            # comment claimed the existing counter already covered it; it did
            # not, because that counter is only read and only written inside
            # the unclaimed branch.

        if self._poll_index_for_heartbeat % self._cadence.heartbeat_every_polls == 0:
            result = await self._ownership(
                "acquire", repo=repo, number=number, head_sha=head
            )
            if result is not None and result.owned_by_caller:
                self._owner_epoch = result.epoch or self._owner_epoch
                self._unknown_heartbeats = 0
                return
            if (
                result is not None
                and result.verdict == OWNED_BY_OTHER
                and self._lost_to_someone_else(result)
            ):
                # Discarding the verdict and keeping only the epoch meant this
                # loop could never notice it had LOST the PR — and it adopted
                # the winner's fencing generation while failing to notice,
                # so every later call carried a competitor's epoch.
                self._lose_claim(repo, number, result)
                return

            # Everything else: an UNKNOWN this loop cannot read as a landed
            # write, UNOWNED, and a None result all used to fall off the end of
            # this block in silence. (WROTE_NO_RECORD is not among them:
            # `answer_from` reserves it for /release and /terminal.)
            #
            # This is the path where silence costs most. Once the head stops
            # moving, the heartbeat is the ONLY liveness write a claimed loop
            # makes, so an /acquire answering 503 — or a worker that lost
            # MCTL_TOKEN, or a 404 from a wrong ingress path, each of which
            # `answer_from` gives its own reason — stopped refreshing
            # last_seen_at with zero log lines and zero history signal, and the
            # reconciler force-released the row at the 10h bound while the
            # workflow was alive and polling.
            if result is not None and result.verdict == UNOWNED:
                # The row this loop believed it held names nobody — the
                # reconciler released it, or it was never written. The acquire
                # did NOT take it (an acquire that took it would answer
                # owned-by-caller), so this is not a refreshed heartbeat.
                #
                # Drop the claim rather than counting toward the give-up: the
                # unclaimed path re-acquires on the next poll, which is the
                # recovery the progress branch's UNOWNED note already names.
                # NOT a refusal — nobody said they own this.
                workflow.logger.info(
                    "lifecycle: %s#%s is owned by nobody — this loop drops its "
                    "stale claim and re-acquires",
                    repo, number,
                )
                self._owned_entity_id = ""
                self._owner_epoch = 0
                self._owned_head_sha = ""
                self._unknown_heartbeats = 0
                self._unknown_progress = 0
                return

            if result is not None and result.accepted and result.verdict == UNKNOWN and result.state:
                # A record came back in a 2xx and its STATE is one this image
                # does not recognise — `verdict_for` classifies against two
                # CLOSED sets and answers UNKNOWN rather than guessing, because
                # this container lags mctl-api by a release.
                #
                # Counted, NOT reset, and it is the one accepted shape that
                # must be. mctl-api took the write, so liveness is refreshed —
                # but liveness is not the question here. A holding state added
                # server-side means the row may now be held by somebody else in
                # a state this image cannot read, and an arm that zeroed the
                # counter on every such heartbeat made the give-up UNFIREABLE:
                # the loop would hold its claim indefinitely, and invisibly,
                # against a row another actor owns. Uncertainty resolves toward
                # dropping the claim, the same direction `verdict_for` itself
                # takes.
                self._unknown_heartbeats += 1
                workflow.logger.warning(
                    "lifecycle: heartbeat for %s#%s came back in state %r, which "
                    "this image does not recognise — %d consecutive; the claim is "
                    "dropped at %d",
                    repo, number, result.state, self._unknown_heartbeats,
                    LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT,
                )
            elif (
                result is not None
                and result.accepted
                and (
                    result.verdict != OWNED_BY_OTHER
                    or result.owner_type == OWNER_TYPE
                )
            ):
                # The same competitor check the progress branch got, which this
                # block was missing — one branch over, which is where every
                # defect in this PR has turned out to live.
                #
                # `Ownership.from_payload` does not require `owner.id`, so a
                # 2xx carrying `owner: {"type": "pr-steward"}` answers
                # OWNED_BY_OTHER, is declined as a loss by
                # `_lost_to_someone_else`'s `bool(result.owner_id)` guard, and
                # then landed HERE — where `accepted` is True, so the loop read
                # a COMPETITOR's record as a successful refresh of its own
                # claim, reset `_unknown_heartbeats`, and kept the claim alive
                # indefinitely against a row it does not hold. The give-up
                # could never fire, which is the state this whole block exists
                # to prevent. (Found by agy.)
                #
                # mctl-api TOOK the write, so last_seen_at IS refreshed; this
                # loop simply learned nothing usable from the record that came
                # back. That is not a missed heartbeat, and counting it toward
                # a give-up whose entire premise is "liveness stopped" was the
                # opposite of what the gate is for.
                #
                # Two shapes, and neither is a reason to stop claiming: a
                # body-less 2xx on /ownership/acquire (the pair the claim and
                # progress arms were rewritten to read — the heartbeat IS an
                # acquire and got neither), which carries no record at all; and
                # our own record read back unhealthy, which
                # `_lost_to_someone_else` correctly declines to treat as a loss
                # and which the give-up must not act on either, since ADR-010
                # §4 gives `stuck` an ESCALATION and only `dead` a takeover.
                #
                # Getting this wrong was not cosmetic. Three of them dropped a
                # claim the loop still held, after which it writes no progress,
                # writes no terminal when the PR merges, and the finally
                # releases nothing — the zero-owner state this epic exists to
                # remove — on the strength of a warning that said liveness had
                # stopped when it had not.
                #
                # Logged rather than returning in silence, which is the defect
                # the previous commit fixed one block up and this arm
                # reintroduced. The unhealthy-own-record case is precisely the
                # condition ADR-010 wants an operator to see.
                if not result.state:
                    # No record at all — the body-less 2xx. Routine on a
                    # healthy watch, so info.
                    workflow.logger.info(
                        "lifecycle: heartbeat for %s#%s landed but carried no "
                        "record (verdict=%s) — the claim stands",
                        repo, number, result.verdict,
                    )
                elif not result.healthy:
                    # Our own record, read back unhealthy — and WHICH kind
                    # matters. ADR-010 §4 gives `stuck` an ESCALATION (a human
                    # is told and ownership does NOT move) and only `dead` a
                    # takeover, so an operator needs the distinction and not
                    # merely "unhealthy". The wire type carries both now.
                    workflow.logger.warning(
                        "lifecycle: %s#%s reads back as OUR record, unhealthy "
                        "(state=%r, dead=%s, stuck=%s) — the claim stands and "
                        "the reconciler %s",
                        repo, number, result.state, result.dead, result.stuck,
                        "may take it" if result.dead else "escalates rather than taking it",
                    )
                else:
                    # A record, healthy, our own owner TYPE — but not
                    # owned_by_caller, so it named a different id, or none.
                    # The arm above keeps the claim on the type alone and the
                    # log said "carried no record" for a response that carried
                    # one, which is the third shape neither branch described.
                    workflow.logger.warning(
                        "lifecycle: heartbeat for %s#%s came back as %s/%r in "
                        "state %r — same owner type, different identity; the "
                        "claim stands on the type alone",
                        repo, number, result.owner_type, result.owner_id, result.state,
                    )
                self._unknown_heartbeats = 0
                return
            else:
                self._unknown_heartbeats += 1
                workflow.logger.warning(
                    "lifecycle: heartbeat for %s#%s did not land (verdict=%s, %s) — "
                    "%d consecutive, last_seen_at is not being refreshed",
                    repo, number,
                    result.verdict if result is not None else "none",
                    # `reason` is "" for every 2xx that carried a record, so the
                    # verdict above is what makes those cases self-describing.
                    (result.reason or "no reason given") if result is not None
                    else "the activity failed outright",
                    self._unknown_heartbeats,
                )

            self._give_up_claim_if_unanswered(repo, number)

    def _give_up_claim_if_unanswered(self, repo: str, number: int) -> None:
        """Drop the claim once the store has stopped confirming it.

        One writer and one caller: the heartbeat block. It was extracted when
        a second path briefly counted into `_unknown_heartbeats`, and that
        path has since been removed — counting a per-POLL event into a
        per-HEARTBEAT constant was the defect, not the location of the check.
        The name is kept because the block is long enough to deserve one, not
        because anything else calls it.
        """
        if self._unknown_heartbeats < LIFECYCLE_UNKNOWN_HEARTBEAT_LIMIT:
            return
        # Deliberately NOT a back-off. Skipping the heartbeat is the one thing
        # that cannot help here — it is the write whose absence is the problem.
        #
        # What is wrong after this many is the BELIEF. At
        # LIFECYCLE_HEARTBEAT_EVERY_POLLS x MERGE_POLL_INTERVAL per heartbeat,
        # either last_seen_at has not moved for about six hours, or the record
        # has come back that many times in a state this image cannot read.
        # Either way the reconciler will take the row at the 10h bound whatever
        # this loop thinks, and the correction has to arrive BEFORE that, not
        # after. A loop that goes on believing it owns an entity the store is
        # about to hand to somebody else is the divergence this epic exists to
        # remove. Dropping the claim makes the belief match the outcome and
        # returns the loop to the unclaimed path, which re-acquires under its
        # own gate.
        workflow.logger.warning(
            "lifecycle: giving up the claim on %s#%s after %d unanswered "
            "liveness writes — the sweeper keeps the PR",
            repo, number, self._unknown_heartbeats,
        )
        self._owned_entity_id = ""
        self._owner_epoch = 0
        self._owned_head_sha = ""
        self._unknown_heartbeats = 0
        # _unknown_progress too: it counts failures under the claim being
        # dropped here, and carrying it forward would start the NEXT claim
        # already throttled on a write path never tried under it.
        self._unknown_progress = 0
        # NOT a refusal: nobody said they own this. A refusal means "somebody
        # else answered", which is the opposite of what just happened — and it
        # carries an owner identity this path has none of.

    def _backed_off(self, unanswered: int) -> bool:
        """Whether an ownership write should be skipped on this poll.

        The store has answered neither "mine" nor "someone else's" this many
        times running. Asking again on every remaining poll is the history cost
        LIFECYCLE_UNKNOWN_WRITE_LIMIT refuses; asking on the heartbeat boundary
        keeps the loop recovering when the store comes back. Deliberately not a
        give-up, and deliberately evaluated per counter so a failing /progress
        does not throttle the acquire that is still working.
        """
        return (
            unanswered >= self._cadence.unknown_write_limit
            and self._poll_index_for_heartbeat % self._cadence.heartbeat_every_polls != 0
        )

    def _merge_watch_hop_suggested(
        self,
        *,
        polls_this_run: int,
        tick_task: asyncio.Task[None] | None,
        hops: int,
    ) -> bool:
        """Should the merge watch end this run via continue_as_new right now?

        mctl-agents#404 v2. Pure enough to unit-test by monkeypatching
        `dev_loop.workflow` (the pattern `TestTickSettling` uses): it reads
        `workflow.info()` itself, so a test can substitute a fake whose
        `is_continue_as_new_suggested`/`get_current_history_length` are
        under its control. Does NOT itself check
        `workflow.patched("merge-watch-continue-as-new")` -- `_watch_pr`
        evaluates that once, beside its other markers, and only calls this
        at all when it is true, so an unpatched execution never reaches it.
        """
        if self._abandoned:
            # An abandon already observed must end the watch through the
            # existing abandon path below, never be traded for a
            # continuation that would silently lose it.
            return False
        if polls_this_run < 1:
            # At least one completed poll in THIS run, so a mis-set floor
            # (or a server that suggests continue-as-new immediately after
            # a hop) cannot produce a continue-as-new storm.
            return False
        if tick_task is not None and not tick_task.done():
            # Never hop with an in-loop shepherd tick in flight. The
            # suggestion stays true once crossed, so the hop simply happens
            # at the next clean boundary instead of here.
            return False
        if hops >= MERGE_WATCH_MAX_HOPS:
            workflow.logger.error(
                "merge watch has already hopped %d times (MERGE_WATCH_MAX_HOPS=%d) "
                "-- continuing to watch in this run until the deadline instead "
                "of hopping again",
                hops,
                MERGE_WATCH_MAX_HOPS,
            )
            return False
        info = workflow.info()
        # `is_continue_as_new_suggested` is a METHOD on `Info` in temporalio
        # 1.31.0 (temporalio/workflow/_context.py:186), not a property --
        # reading it without calling it is a bound method object, which is
        # always truthy, and would hop on the first poll of every watch.
        if info.is_continue_as_new_suggested():
            return True
        return info.get_current_history_length() >= MERGE_WATCH_HISTORY_FLOOR

    async def _watch_pr(
        self, service: str, slug: str, resume: MergeWatchResume | None = None
    ) -> _WatchOutcome:
        """Poll get_pr_state until the PR reaches a terminal state -- or,
        once this run's history is large enough, break out with a resume
        record for `run`/`_resume_merge_watch` to continue_as_new with
        (mctl-agents#404 v2).

        Observational, fail-open: a persistently failing read (GitHub outage
        outlasting the activity retries) returns the last known state
        instead of failing a loop whose implement already succeeded. Returns
        a `last` of None when no PR link ever appeared within the grace
        polls -- UNLESS this is a resumed run carrying an abandon, in which
        case the carried `resume.last_pr` is returned instead: task 5a's
        guard must not erase the PR state an earlier run already observed.

        An already-true `self._abandoned` on a RESUMED call is deliberately
        NOT an early return here (round 2 on mctl-agents#404 v2, claude +
        agy P2): an early return before `try` would skip this method's own
        `finally`, which is what releases the lifecycle-ownership claim a
        hop kept held. Instead, when `resume` carries the claim
        (`track_ownership`/`owned_entity_id`, rehydrated by
        `_resume_merge_watch` before this call), execution falls through to
        the `while` loop below, whose own `not self._abandoned` condition is
        already false on entry -- the loop body never runs, but `finally`
        still does, issuing the same relinquishing write a normal
        (non-hopped) watch end would. `last` stays `resume.last_pr`,
        matching what the old guard used to return directly.

        The narrow case that IS still an early return: `resume is None` and
        `self._abandoned` is already true. Unreachable in production --
        `run()` checks `self._abandoned` immediately before ever calling
        `_watch_pr(target_repo, slug)` with no resume (see `run`, just above
        the `merge-detection` patch check) -- so no ownership claim can be
        outstanding here to release; this call shape only exists as a direct
        unit-test entry point with no workflow context.
        """
        if resume is None and self._abandoned:
            return _WatchOutcome(last=None)
        if resume is not None:
            # The ABSOLUTE deadline the first run of this watch computed.
            # Never recomputed here -- doing so would restart the 14-day
            # clock on every hop instead of staying bounded from the first
            # poll of the watch.
            deadline = _as_utc(resume.deadline)
        else:
            deadline = workflow.now() + MERGE_WATCH_DEADLINE
        polls_without_pr = resume.polls_without_pr if resume is not None else 0
        last: PRState | None = resume.last_pr if resume is not None else None
        # mctl-agents#404 v2: evaluated once, beside the markers below,
        # exactly like them -- an execution whose history predates this
        # marker keeps polling in one run for the rest of its life
        # (migration by attrition, tests/test_patch_memoization.py).
        hop_enabled = workflow.patched("merge-watch-continue-as-new")
        if resume is not None:
            # Carried, not re-derived: a continued run must not adopt a
            # different cadence/shepherd/ownership behaviour than the run
            # it replaces just because a patch marker was re-evaluated.
            cadence = CADENCE if resume.fast_cadence else LEGACY_CADENCE
            self._cadence = cadence
            shepherd_in_loop = resume.shepherd_in_loop
            self._shepherd_in_loop = shepherd_in_loop
            concurrent_ticks = resume.concurrent_ticks
            track_ownership = resume.track_ownership
        else:
            # In-loop shepherd (#213): evaluated once — the marker also fixes
            # whether tick commands appear in this execution's history at all.
            # Poll interval, tick cadence and every poll COUNT below, as one
            # value (#213 follow-up). Its own marker because it changes both the
            # timer durations and the number of activities a poll schedules: an
            # execution recorded under the old 30-min/4-h cadence must keep
            # replaying it, and `_Cadence` is what lets it.
            cadence = CADENCE if workflow.patched("fast-shepherd-cadence") else LEGACY_CADENCE
            self._cadence = cadence
            shepherd_in_loop = workflow.patched("shepherd-in-loop")
            # Published to the sweeper via the shepherd_in_loop query the moment
            # the watch starts, not at the first tick a poll later: between those
            # two points this execution IS the owner, and the cron must already
            # be standing down.
            if shepherd_in_loop and not await _shepherd_is_pinned():
                # Decline the claim rather than fail (round 2 on #241). The
                # first fix raised here, which failed the whole workflow after
                # investigate, approve and implement had all succeeded —
                # contradicting this function's own fail-open contract and
                # throwing away merge detection and deploy observation over a
                # tick that is an optimisation, not correctness. But it cannot
                # simply be left to _shepherd_tick either: that runs as a
                # background task whose exceptions it swallows, so the loop
                # would keep answering shepherd_in_loop=True while the sweeper
                # stood down and nothing shepherded the PR for 14 days (codex
                # P1, claude P2). Declining gives the same protection with no
                # loss: nothing runs unpinned, and the cron sweeper picks the
                # proposal up exactly as it did before #213.
                workflow.logger.warning(
                    "no released shepherd image — %s/%s keeps watching but leaves "
                    "shepherding to the cron sweeper",
                    service,
                    slug,
                )
                shepherd_in_loop = False
            self._shepherd_in_loop = shepherd_in_loop
            # #231: run the tick concurrently so polling continues while it
            # runs. Awaiting it inline stalled merge detection for the tick's
            # whole duration — up to the 2 h SDK_STEP_TIMEOUT when the shepherd
            # spawns a follow-up implementation, against a 30-min poll
            # interval. Its own marker, because it changes the ORDER of
            # commands in history: executions that already recorded a
            # sequential tick must keep replaying one.
            concurrent_ticks = shepherd_in_loop and workflow.patched("concurrent-shepherd-tick")
            # Lifecycle ownership (mctlhq/.github#57). Gated on the same claim as
            # the in-loop shepherd: if this execution declined to shepherd, the
            # cron sweeper owns the PR and this loop must not record itself as the
            # owner. Its own marker, because it adds commands to history.
            track_ownership = shepherd_in_loop and workflow.patched("lifecycle-ownership")
            # NOTE (ADR-010 phase 2, #352): there is deliberately no
            # "lifecycle-claims" patch marker here. This PR reverted the watch-end
            # write to a bare `release` because nothing in this repository calls
            # `handoff/complete` yet, so the branch the marker would gate does not
            # exist. A `workflow.patched` call writes a marker into EVERY new
            # execution's history and can only be retired through
            # `deprecate_patch` plus a second deploy — a cost with no branch to
            # pay for. The marker belongs in the change that actually ships the
            # handoff (#353), where it will guard a real fork in behaviour.
            if track_ownership:
                # ADR-010 §12 asks for the resolved policy to be recorded on the
                # row, so "why does this actor own it" is answerable without
                # reconstructing a CWFT env var in another repository. Both are
                # derived here, once, because this is where service and slug are
                # in scope — and both are plain strings, so no policy lookup runs
                # inside workflow code.
                self._proposal_ref = f"{service}/{slug}"
                self._policy_ref = f"devloop:{service}"

        tick_task: asyncio.Task[None] | None = None
        poll_index = resume.poll_index if resume is not None else 0
        shepherd_ticks = resume.shepherd_ticks if resume is not None else 0
        hops = resume.hops if resume is not None else 0
        polls_this_run = 0
        # Set when the loop breaks out to continue_as_new rather than
        # because the watch genuinely ended. The `finally` block below reads
        # this to decide whether to issue the relinquishing lifecycle write
        # -- a hop must keep the claim, never release/terminal it.
        hopping = False
        resume_record: MergeWatchResume | None = None
        try:
            # mctl-agents#420: `and not self._abandoned` lets an `abandon`
            # signal cut a 14-day merge watch short at its next poll boundary
            # while still running this `finally` block, so the
            # lifecycle-ownership row is released rather than left active --
            # the reason `abandon` is a signal and not a Temporal `terminate`.
            while workflow.now() < deadline and not self._abandoned:
                if hop_enabled and self._merge_watch_hop_suggested(
                    polls_this_run=polls_this_run, tick_task=tick_task, hops=hops
                ):
                    hopping = True
                    remaining = deadline - workflow.now()
                    workflow.logger.info(
                        "merge watch for %s/%s hopping via continue_as_new: "
                        "history_length=%d polls_this_run=%d hop=%d remaining=%s",
                        service,
                        slug,
                        workflow.info().get_current_history_length(),
                        polls_this_run,
                        hops + 1,
                        remaining,
                    )
                    resume_record = MergeWatchResume(
                        service=service,
                        slug=slug,
                        deadline=deadline.isoformat().replace("+00:00", "Z"),
                        last_pr=last,
                        polls_without_pr=polls_without_pr,
                        poll_index=poll_index,
                        shepherd_ticks=shepherd_ticks,
                        fast_cadence=cadence is CADENCE,
                        shepherd_in_loop=shepherd_in_loop,
                        concurrent_ticks=concurrent_ticks,
                        track_ownership=track_ownership,
                        owned_entity_id=self._owned_entity_id,
                        owner_epoch=self._owner_epoch,
                        owned_head_sha=self._owned_head_sha,
                        poll_index_for_heartbeat=self._poll_index_for_heartbeat,
                        claim_refused=self._claim_refused,
                        claim_refused_until_poll=self._claim_refused_until_poll,
                        refused_by_type=self._refused_by_type,
                        refused_by_id=self._refused_by_id,
                        refusals_observed=self._refusals_observed,
                        unknown_acquires=self._unknown_acquires,
                        unknown_progress=self._unknown_progress,
                        unknown_heartbeats=self._unknown_heartbeats,
                        proposal_ref=self._proposal_ref,
                        policy_ref=self._policy_ref,
                        last_lifecycle_op=self._last_lifecycle_op,
                        last_lifecycle_op_landed=self._last_lifecycle_op_landed,
                        claim_abandoned=self._claim_abandoned,
                        abandoned=self._abandoned,
                        abandon_reason=self._abandon_reason,
                        hops=hops + 1,
                    )
                    break
                try:
                    state: PRState = await workflow.execute_activity(
                        get_pr_state,
                        args=[service, slug],
                        start_to_close_timeout=PR_STATE_TIMEOUT,
                        retry_policy=PR_STATE_RETRY_POLICY,
                    )
                except ActivityError as exc:
                    # A read outage longer than the activity's retries must not
                    # abort a 14-day watch — ride KNOWN-transient failures out
                    # and poll again next interval (the deadline still bounds
                    # the loop). But only those: get_pr_state wraps every
                    # expected read failure in ProposalListingError, and an
                    # activity timeout is transport by definition, so anything
                    # else here is an unexpected bug in the activity — retrying
                    # that for 14 days would silently mask the defect (agy P2
                    # round 3). End the watch with the last observed state
                    # instead; implement already succeeded, so the loop's
                    # outcome must still not become a workflow failure.
                    cause = exc.cause
                    if not _is_transient(exc):
                        workflow.logger.warning(
                            "get_pr_state failed with a non-transient error for "
                            "%s/%s — ending the merge watch with the last "
                            "observed state: %r",
                            service,
                            slug,
                            cause,
                        )
                        return _WatchOutcome(last=last)
                    workflow.logger.warning(
                        "get_pr_state failed after retries for %s/%s — retrying "
                        "next poll interval",
                        service,
                        slug,
                    )
                    await workflow.sleep(cadence.poll_interval)
                    continue
                if state.found:
                    last = state
                    polls_without_pr = 0
                    if track_ownership and state.repo and state.number is not None:
                        self._poll_index_for_heartbeat += 1
                        await self._track_ownership(state)
                    if state.state in ("MERGED", "CLOSED"):
                        if track_ownership and self._owned_entity_id:
                            done = await self._ownership(
                                "terminal",
                                # `or ""` like the head_sha below: the fields
                                # are Optional on PRState, and the block is
                                # guarded by _owned_entity_id, which is only
                                # set when repo was present. Narrowing it for
                                # the type checker rather than for the reader.
                                repo=state.repo or "",
                                number=state.number or 0,
                                head_sha=state.head_sha or "",
                                reason=f"pull request {(state.state or '').lower()}",
                            )
                            self._finish_claim(
                                "terminal", done, state.repo or "", state.number or 0
                            )
                        return _WatchOutcome(last=state)
                    # Counted only on a successful read, so a transient
                    # get_pr_state failure delays the next tick instead of
                    # consuming its boundary and dropping it for a whole
                    # tick period.
                    poll_index += 1
                    if (
                        shepherd_in_loop
                        # Poll 1 happens at t=0 (the sleep is at the bottom of
                        # the loop), so a tick on it would fire before claude
                        # review has started. Harmless — it would decide `wait`
                        # — but it burns a tick from the budget for nothing.
                        and poll_index > 1
                        and poll_index % cadence.shepherd_tick_every_polls == 0
                        and shepherd_ticks < cadence.shepherd_ticks_max
                    ):
                        # The tick is the same one-shot the cron ran, scoped to
                        # exactly this proposal (service+slug → run_shepherd's
                        # targeted mode, which bypasses the ownership filter).
                        # A failed tick is logged, not fatal: the watch itself
                        # is the durable part, and the next tick retries.
                        if not concurrent_ticks:
                            # Legacy sequential path. Histories recorded before
                            # the concurrent-shepherd-tick marker interleave the
                            # tick's commands with the poll's in exactly this
                            # order; running them as a task instead would be a
                            # command mismatch on replay.
                            shepherd_ticks += 1
                            await self._shepherd_tick(service, slug)
                        elif tick_task is not None and not tick_task.done():
                            # Never two shepherds on one proposal: they would
                            # race on the same .status.yaml, and the tick cap
                            # accounting assumes one at a time. A boundary that
                            # lands on a still-running tick is skipped, not
                            # queued — and does not consume the budget.
                            workflow.logger.info(
                                "in-loop shepherd tick still running for %s/%s — "
                                "skipping this tick boundary",
                                service,
                                slug,
                            )
                        else:
                            # Drain the previous, already-finished tick
                            # before dropping the reference: after this
                            # rebind nothing else can retrieve its
                            # exception, and only the final task is
                            # guaranteed to reach _settle_tick.
                            if tick_task is not None:
                                _drain_tick(tick_task, service, slug)
                            shepherd_ticks += 1
                            tick_task = asyncio.create_task(self._shepherd_tick(service, slug))
                else:
                    polls_without_pr += 1
                    if last is None and state.number is not None:
                        # A PR link IS recorded but cannot be resolved right now
                        # (repo/PR deleted, token lost access, wrong-repo link).
                        # Preserve the reference in the result — it is the only
                        # diagnostic pointer an operator gets. Only when nothing
                        # better exists: a previously RESOLVED state must not be
                        # downgraded to a found=False reference by a later 404.
                        last = state
                    # Give up after GRACE consecutive unresolvable polls — this
                    # covers the link never appearing, a recorded PR that stays
                    # unresolvable, AND a status file deleted after the PR was
                    # once found (agy P3's zombie loop). The counter resets on
                    # every successful resolve, so one transient blip never
                    # ends the watch.
                    if polls_without_pr >= cadence.pr_lookup_grace_polls:
                        workflow.logger.warning(
                            "merge watch for %s/%s giving up after %d consecutive "
                            "polls without a resolvable PR (last=%s)",
                            service,
                            slug,
                            polls_without_pr,
                            "none" if last is None else (last.pr_url or "unresolved"),
                        )
                        return _WatchOutcome(last=last)
                # Counted only after a poll that reached here -- i.e. one
                # that neither hopped nor returned above -- so "at least one
                # completed poll in this run" (the hop predicate's own
                # guard) means what it says.
                polls_this_run += 1
                await workflow.sleep(cadence.poll_interval)
        finally:
            await self._settle_tick(tick_task, service, slug)
            # A hop keeps the claim: this run is not the one relinquishing
            # it, the continued run is still watching, and the owner id
            # (workflow_id) plus the epoch both stay valid across
            # continue_as_new. Skipping this block on `hopping` is what
            # keeps `test_abandon_signal_cuts_short_a_merge_watch_and_releases_ownership`
            # (an ABANDON, which releases) independent of a HOP (which does
            # not) -- the two are mutually exclusive by construction, since
            # the hop predicate refuses to fire while `self._abandoned`.
            if not hopping and track_ownership and self._owned_entity_id:
                # The watch ended without the PR reaching a terminal state —
                # the deadline expired, or the PR stopped resolving. RELEASE,
                # not terminal: the work remains and somebody must be able to
                # pick it up, which is precisely the zero-owner gap #239
                # describes. Released is the state Acquire can take.
                repo, _, raw_number = self._owned_entity_id.partition("#")
                # Parsed once. The entity id is this loop's own construction
                # (`EntityRef.for_pull_request`), so the digits are there — but
                # the conversion used to sit inline in the _ownership call and
                # the raw string went on to the log, which is how the two
                # readings of `number` in this block drifted apart.
                number = int(raw_number) if raw_number.isdigit() else 0
                # Pick the op from what the PR actually reached. A terminal
                # write that failed leaves the claim behind deliberately (the
                # branch above says so), and the watch then ends on a MERGED
                # PR — releasing it here would mark a finished entity as
                # "somebody must take this", which is the one state this
                # contract defines as work remaining.
                terminal_state = last is not None and last.state in ("MERGED", "CLOSED")
                # Non-terminal end: still a bare RELEASE, not "handoff-start".
                # `handoff-start` writes a HOLDING `handing-off` state
                # (contract.py) that only `/handoff/complete` can resolve —
                # and nothing in this repository calls `handoff/complete`
                # anywhere (no reconciler yet, #353), so issuing
                # `handoff-start` here would leave the row stuck in
                # `handing-off` forever, which is worse than the zero-owner
                # gap a release leaves. Revert to `release` until a completer
                # exists; the flip back to `handoff-start` belongs to #353,
                # together with the patch marker that must gate it.
                op = "terminal" if terminal_state else "release"
                done = await self._ownership(
                    op,
                    repo=repo,
                    number=number,
                    # The head this watch last saw. `_payload` sends `version`
                    # unconditionally, so omitting it made the LAST write of
                    # the watch the only one carrying an empty one — and the
                    # version is what the row records about the entity it is
                    # letting go of.
                    head_sha=(last.head_sha or "") if last is not None else "",
                    reason=(
                        f"pull request {(last.state or '').lower()}"
                        if terminal_state and last is not None
                        else "merge watch ended without a terminal pull-request state"
                    ),
                )
                # Same policy as the in-loop terminal path, and now the same
                # code: two phrasings of one rule is how they came to disagree.
                #
                # The guard used to be unfalsifiable — the workflow returns on
                # the next line and `_owned_entity_id` was never read again, so
                # no test could turn it red, and the comment here said so. The
                # `lifecycle_claim` query is the reader that fixes that:
                # Temporal serves queries against completed executions, so the
                # terminal value of these fields is observable exactly when the
                # question is asked.
                self._finish_claim(op, done, repo, number)
            # After the LAST relinquishing write of this watch, whichever path
            # made it. One metric line per abandoned entity, not per failed
            # attempt — see _report_claim_abandonment. Also skipped on a hop:
            # a hop never attempts a relinquishing write, so `_claim_abandoned`
            # cannot have changed here, and reporting it again per hop would
            # turn a once-per-entity metric into one per continuation.
            if not hopping and track_ownership:
                self._report_claim_abandonment()
        if hopping:
            assert resume_record is not None  # noqa: S101 -- set right before every `break` above
            return _WatchOutcome(last=last, resume=resume_record)
        return _WatchOutcome(last=last)
