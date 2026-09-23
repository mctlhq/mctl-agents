"""Shared Temporal constants to break circular imports between worker and start/poller modules.
"""
from __future__ import annotations

import logging
import os

from orchestrator.temporal import implement_outcome

logger = logging.getLogger(__name__)

TASK_QUEUE = "mctl-dev-loop"

# The second queue from ADR-008. Split by HOLDING TIME, not by importance:
# submit_and_wait polls an Argo run every 15 s for up to two hours, and a
# burst of those in a shared slot pool stalls every short activity behind
# them — reconcile, intake and the dev-loop's own lookups (#152).
#
# Nothing schedules onto this queue yet. The routing flip is a later,
# separately-releasable step guarded by workflow.patched("exec-queue"),
# because a worker must be POLLING this queue before any workflow targets
# it — otherwise the activity waits on a queue nobody reads.
EXECUTION_TASK_QUEUE = "mctl-dev-loop-exec"

# Slot limits. Explicit values rather than the SDK's implicit defaults, so
# capacity is something configuration states and metrics can be read
# against — ADR-008 D3 says to move them from real numbers, not from first
# principles.
#
# The control limit deliberately MATCHES the SDK default it replaces, and
# stays there until the routing flip. Between step 2 (both roles deployed)
# and the routing flip (submit_and_wait routed away), the control worker still
# carries every long Argo poll; tightening it to 20 in that window would
# reintroduce the starvation this split exists to remove, at a threshold
# five times lower than today's — with 9-11 concurrent loops in
# production, a self-inflicted outage during the soak. The tightening comes
# after the flip AND after the soak, when the workload has moved off this
# queue and the exporter can show what the new number did.
CONTROL_MAX_CONCURRENT_ACTIVITIES = 100

# Left unbounded for the same reason, and it is the sharper case: with
# max_concurrent_workflow_tasks unset the SDK does not fall back to 100 —
# it builds a thread pool of 500. So ANY number here is a new ceiling far
# below current behaviour, imposed on a control worker still running all
# five workflow types against 9-11 concurrent loops. A value is picked
# once D5's numbers exist; guessing one during the soak is how a capacity
# limit becomes an outage.
CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS: int | None = None

EXECUTION_MAX_CONCURRENT_ACTIVITIES = 40

# The third queue (#395, #396). Split by ADMISSION, not by holding time:
# it carries exactly one operation, the implementer submit, and its slot
# limit IS the implementation capacity. An activity scheduled here with no
# free slot stays Scheduled in Temporal — no Argo workflow is created, no
# Argo deadline starts — which is the whole point. On 2026-09-19 nine
# approvals reached Argo at once and queued on a capacity-1 mutex INSIDE
# the execution layer; six of them died of their own deadline before ever
# reaching the head of that queue.
#
# `mctl-dev-loop-exec` keeps investigate, reconcile and incidents at 40.
# Lowering that to N would re-couple the workloads ADR-008 separated.
#
# The implement submit routes here behind workflow.patched("implement-queue")
# in dev_loop._run_cwft, one release after the worker that polls it was
# deployed (mctl-gitops#1287) — the worker has to be polling before any
# workflow targets it, the same order as EXECUTION_TASK_QUEUE. Migration is
# by attrition: a loop that predates the marker keeps its old routing.
IMPLEMENTATION_TASK_QUEUE = "mctl-dev-loop-implement"

# The one operation that routes to it. Named once so the routing branch,
# the dev-loop's submit and the tests all spell the same string.
IMPLEMENTATION_OPERATION = "mctl-agents-implement"

# Mirror of the CWFT lock that guards the timed implementer step, in the
# OTHER repository: cwft-mctl-agents-implement.yaml in mctl-gitops
# (platform-gitops/argo-workflows/cluster-templates/). Not read live — the
# worker deployment has no gitops checkout, only the implement CWFT mounts
# one (see run_implementer.py's AGENTS_STATE note) — so it is mirrored here,
# the same shape orchestrator/resolver.py already uses for
# validate-agent-platform.py's COMPAT_RE. What keeps the mirror honest is
# `orchestrator/validate_manifest.py::check_implement_admission_is_safe`,
# which fails mctl-agents' own PR-validation CI the moment this drifts from
# the real file (mctl-agents#418).
#
# On 2026-09-19 admission (N=3 by default, below) let three implement
# submits reach Argo while only ONE of them could hold this mutex; the other
# two queued INSIDE run-implementer's activeDeadlineSeconds=7200 and were
# killed having executed nothing. ARGO_IMPLEMENT_MUTEX_TEMPLATE names which
# step actually carries the deadline the mutex wait counts against —
# `run-implementer` today. It moves to `commit-and-push` once the
# mctl-gitops PR that relocates the mutex (task 10 of #418) merges, at which
# point admission is no longer bound by this lock at all: waiting on a
# seconds-long commit step does not burn the implementer's two-hour budget.
ARGO_IMPLEMENT_MUTEX_NAME = "mctl-agents-proposal-claims"
ARGO_IMPLEMENT_MUTEX_TEMPLATE = "run-implementer"
ARGO_IMPLEMENT_MUTEX_WIDTH = 1


def argo_admission_width() -> int | None:
    """The ceiling admission must respect, or None once the lock no longer
    guards timed work.

    Compares against `implement_outcome.IMPLEMENTER_TEMPLATE` rather than a
    second literal: two independent spellings of "run-implementer" is
    exactly the kind of drift this mirror exists to prevent. Importing
    implement_outcome introduces no cycle — that module imports nothing
    from this one.
    """
    if ARGO_IMPLEMENT_MUTEX_TEMPLATE == implement_outcome.IMPLEMENTER_TEMPLATE:
        return ARGO_IMPLEMENT_MUTEX_WIDTH
    return None


def _int_env(name: str, default: int) -> int:
    """A positive integer from the environment, or the default.

    A malformed or non-positive value is a configuration error, not a
    reason to run with whatever capacity the parse happens to yield —
    zero here would be a worker that admits nothing, forever, silently.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise SystemExit(f"{name} must be at least 1, got {value}")
    return value


# Implementation capacity, N. Read from the environment rather than fixed
# here because it is the one number an operator is expected to move, and
# ADR-008 D5 says capacity is something configuration states and metrics
# are read against — the gitops values file is where N sits next to the
# schedule-to-start alert that tells you it is wrong.
#
# A function, not a module constant, on purpose: a constant would be
# evaluated on import by EVERY role, so a typo in a shared env would take
# down control and execution workers that never touch this queue. Only the
# role that builds the implementation plan pays for a bad value.
#
# It is a per-PROCESS limit, not a distributed semaphore: capacity is
# replicas times N. That is why the implementation deployment is pinned to one
# replica and mctl-gitops fails CI if that changes (#1285).
IMPLEMENTATION_CAPACITY_ENV = "IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES"
# 1, not 3: while ARGO_IMPLEMENT_MUTEX_TEMPLATE still names "run-implementer",
# argo_admission_width() clamps N to the mutex width, and a default above that
# ceiling would log a clamp warning on every start where nothing overrides it. Restore
# to 3 in the same one-line commit that flips the mirror to
# "commit-and-push" after the mctl-gitops PR (task 10 of #418) lands.
DEFAULT_IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES = ARGO_IMPLEMENT_MUTEX_WIDTH


def implementation_max_concurrent_activities() -> int:
    """Effective admission width N for the implementation worker.

    While the Argo mutex still guards `run-implementer`
    (`argo_admission_width()` is not None), a configured N above the mutex
    width is CLAMPED to that width, with one warning naming both numbers.
    It is deliberately not a refusal: mctl-gitops pins
    IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES="3" against a width of 1
    today, and refusing would crash-loop the one worker that does
    implementation work, admitting zero instead of one. Admitting more than
    the width only moves the surplus into Argo, where it queues against
    run-implementer's own activeDeadlineSeconds — the 2026-09-19 shape
    (mctl-agents#418).
    """
    configured = _int_env(IMPLEMENTATION_CAPACITY_ENV, DEFAULT_IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES)
    ceiling = argo_admission_width()
    if ceiling is not None and configured > ceiling:
        logger.warning(
            "%s=%d exceeds the Argo mutex width %s=%d, which still guards %s; "
            "clamping implement admission to %d so the surplus does not queue inside "
            "Argo against its own activeDeadlineSeconds (mctl-agents#418)",
            IMPLEMENTATION_CAPACITY_ENV,
            configured,
            ARGO_IMPLEMENT_MUTEX_NAME,
            ceiling,
            ARGO_IMPLEMENT_MUTEX_TEMPLATE,
            ceiling,
        )
        return ceiling
    return configured


# Implement-sweep tunables (mctl-agents#412). Read the same way as
# IMPLEMENTATION_CAPACITY_ENV above and for the same reason: a function
# called once at worker startup (worker.setup_schedules, gated by
# owns_schedules), not a module constant every role would evaluate on
# import.
#
# Grace period: how long a freshly-`updated_at` `accepted` proposal is left
# alone before the sweep will consider it stranded. 20 minutes because it
# exceeds dev_loop.APPROVE_STEP_TIMEOUT (15 min), so a DevLoopWorkflow that
# flipped the approve but has not yet submitted its own implement step is
# always still inside the window, even if the visibility query is stale.
IMPLEMENT_SWEEP_GRACE_MINUTES_ENV = "IMPLEMENT_SWEEP_GRACE_MINUTES"
DEFAULT_IMPLEMENT_SWEEP_GRACE_MINUTES = 20

# Per-tick submit cap: how many children one ImplementSweepWorkflow tick may
# start. Matches the issue poller's and the incident responder's per-run
# caps. The real concurrency bound is the admission queue
# (IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES); this cap only limits how many
# children one tick mints after a long outage.
IMPLEMENT_SWEEP_MAX_SUBMITS_ENV = "IMPLEMENT_SWEEP_MAX_SUBMITS"
DEFAULT_IMPLEMENT_SWEEP_MAX_SUBMITS = 5


def implement_sweep_grace_minutes() -> int:
    return _int_env(IMPLEMENT_SWEEP_GRACE_MINUTES_ENV, DEFAULT_IMPLEMENT_SWEEP_GRACE_MINUTES)


def implement_sweep_max_submits() -> int:
    return _int_env(IMPLEMENT_SWEEP_MAX_SUBMITS_ENV, DEFAULT_IMPLEMENT_SWEEP_MAX_SUBMITS)


# Where the Temporal SDK's Prometheus exporter binds (ADR-008 D5, #252).
#
# The starvation this split addresses was invisible: a full slot pool looks
# exactly like "nothing is happening". The number that names it is
# schedule-to-start latency per queue, and the SDK already records it — it
# just had nowhere to publish it, because Client.connect ran on the default
# runtime with no TelemetryConfig.
#
# 8080 rather than a dedicated 9090 because it is the port the deployment
# ALREADY declares and nothing listens on. mctl-gitops's base-service chart
# renders containerPort `http` and Service port `http` from
# .Values.service.port (default 8080) unconditionally; the metrics port is
# rendered only under `metrics.enabled`, which ALSO renders a
# monitoring.coreos.com/v1 ServiceMonitor. This cluster runs VictoriaMetrics,
# whose operator auto-converts such an object and leaves the original
# orphaned — an ArgoCD drift already paid for twice (mctl-gitops incidents
# 43d9e608 and 992434e2, recorded in services/labs/openclaw/values.yaml).
# Binding here makes the declared port real and lets a native VMServiceScrape
# target `port: http` with no chart change at all.
METRICS_PORT = 8080
