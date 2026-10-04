"""Declared targets for scheduled GitHub workflow dispatch (mctl-agents#559).

GitHub's `schedule:` trigger is best-effort and silent when a slot is dropped.
Each target here gets a Temporal Schedule that fires weekly and starts
`ScheduledDispatchWorkflow`, which dispatches the workflow and does not count
the fire as done until a `workflow_dispatch` run has been observed.

The cadence is an interval, not a cron string, on purpose: `_ensure_schedule`'s
`_converge_spec` and the minute-collision tests in tests/test_worker_schedules.py
read only `spec.intervals`. A cron or calendar spec would never be converged on
redeploy and would be invisible to those tests.

Epoch arithmetic: Temporal interval schedules are aligned to the Unix epoch,
1970-01-01 00:00 UTC, which was a THURSDAY. With `every=7d`, an `offset` of
`((weekday - 3) % 7)` days (Monday=0, so Thursday=3) plus the hour and minute
lands on the wanted weekday and time. Sunday (6) gives 3 days.

This module imports nothing from the Temporal worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import ScheduleIntervalSpec

EPOCH_WEEKDAY = 3  # 1970-01-01 was a Thursday (Monday=0)


@dataclass(frozen=True)
class DispatchTarget:
    repo: str
    workflow_file: str
    ref: str
    weekday: int  # Monday=0 ... Sunday=6, UTC
    hour: int
    minute: int

    @property
    def _slug(self) -> str:
        return f"{self.repo.replace('/', '-')}-{self.workflow_file.rsplit('.', 1)[0]}"

    @property
    def schedule_id(self) -> str:
        return f"dispatch-{self._slug}-schedule"

    @property
    def workflow_id(self) -> str:
        return f"dispatch-{self._slug}"

    def interval(self) -> ScheduleIntervalSpec:
        return ScheduleIntervalSpec(
            every=timedelta(days=7),
            offset=timedelta(
                days=(self.weekday - EPOCH_WEEKDAY) % 7,
                hours=self.hour,
                minutes=self.minute,
            ),
        )


WEEKLY_DISPATCH_TARGETS: tuple[DispatchTarget, ...] = (
    # Sunday 09:01 UTC (11:01 CEST). :01 because :00 is a forbidden Argo cron
    # minute; it also clears every Temporal schedule minute.
    DispatchTarget(
        repo="mctlhq/portfolio",
        workflow_file="weekly-refresh.yml",
        ref="main",
        weekday=6,
        hour=9,
        minute=1,
    ),
)
