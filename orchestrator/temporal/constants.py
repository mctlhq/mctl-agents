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

# Slot limits, deliberately below the SDK's defaults (100 activities).
# The goal is not throughput: it is that exhaustion shows up as a bounded
# backlog on one queue with a schedule-to-start latency you can alert on,
# rather than as an invisible global stall. Starting values — ADR-008 D3
# says to move them from metrics, not from first principles.
CONTROL_MAX_CONCURRENT_ACTIVITIES = 20
CONTROL_MAX_CONCURRENT_WORKFLOW_TASKS = 40
EXECUTION_MAX_CONCURRENT_ACTIVITIES = 40
