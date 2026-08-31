"""Shared Temporal constants to break circular imports between worker and start/poller modules.
"""
from __future__ import annotations

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
