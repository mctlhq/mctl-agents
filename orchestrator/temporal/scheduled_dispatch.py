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

Ownership: the `dispatch-` id prefix and `-schedule` suffix are owned by this
code. To retire a target, remove it from `WEEKLY_DISPATCH_TARGETS` AND add its
`schedule_id` to `RETIRED_DISPATCH_SCHEDULE_IDS`; `worker._gc_dispatch_schedules`
deletes only those tombstoned ids and merely logs any other undeclared
`dispatch-*-schedule`. Deleting by absence alone would let an older image
delete a newer image's schedule during a rollback or mid-rollout restart.
`repo` and `workflow_file` go into GitHub URL paths and are validated here
(at import time) and again in the activities.

This module imports nothing from the Temporal worker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import ScheduleIntervalSpec

EPOCH_WEEKDAY = 3  # 1970-01-01 was a Thursday (Monday=0)

DISPATCH_SCHEDULE_PREFIX = "dispatch-"
DISPATCH_SCHEDULE_SUFFIX = "-schedule"

_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_WORKFLOW_FILE_RE = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml")
_BAD_REF_RE = re.compile(r"[\s\x00-\x1f\x7f]")


def validate_repo(value: str) -> str:
    """Return `value` if it is a safe `<owner>/<name>`, else raise ValueError."""
    if (
        not isinstance(value, str)
        or not _REPO_RE.fullmatch(value)
        or any(seg in (".", "..") for seg in value.split("/"))
    ):
        raise ValueError(f"invalid repo {value!r}: expected <owner>/<name> of [A-Za-z0-9_.-]")
    return value


def validate_workflow_file(value: str) -> str:
    """Return `value` if it is a bare workflow file name, else raise ValueError."""
    if (
        not isinstance(value, str)
        or not _WORKFLOW_FILE_RE.fullmatch(value)
        or value in (".yml", ".yaml")
    ):
        raise ValueError(f"invalid workflow_file {value!r}: expected a bare [A-Za-z0-9_.-]+.yml/.yaml name")
    return value


@dataclass(frozen=True)
class DispatchTarget:
    repo: str
    workflow_file: str
    ref: str
    weekday: int  # Monday=0 ... Sunday=6, UTC
    hour: int
    minute: int

    def __post_init__(self) -> None:
        validate_repo(self.repo)
        validate_workflow_file(self.workflow_file)
        if not self.ref or _BAD_REF_RE.search(self.ref):
            raise ValueError(f"invalid ref {self.ref!r}: must be non-empty without whitespace or control characters")
        if not 0 <= self.weekday <= 6:
            raise ValueError(f"invalid weekday {self.weekday!r}: expected 0..6")
        if not 0 <= self.hour <= 23:
            raise ValueError(f"invalid hour {self.hour!r}: expected 0..23")
        if not 0 <= self.minute <= 59:
            raise ValueError(f"invalid minute {self.minute!r}: expected 0..59")

    @property
    def _slug(self) -> str:
        return f"{self.repo.replace('/', '-')}-{self.workflow_file.rsplit('.', 1)[0]}"

    @property
    def schedule_id(self) -> str:
        return f"{DISPATCH_SCHEDULE_PREFIX}{self._slug}{DISPATCH_SCHEDULE_SUFFIX}"

    @property
    def workflow_id(self) -> str:
        return f"{DISPATCH_SCHEDULE_PREFIX}{self._slug}"

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
    # Sunday 10:01 UTC (owner's slot 2026-10-04: 12:01 CEST / 11:01 CET). :01 because :00 is a forbidden Argo cron
    # minute; it also clears every Temporal schedule minute.
    DispatchTarget(
        repo="mctlhq/portfolio",
        workflow_file="weekly-refresh.yml",
        ref="main",
        weekday=6,
        hour=10,
        minute=1,
    ),
)

# Tombstones: schedule ids of retired targets, deleted on worker boot. An id
# here must never also be declared above (a unit test pins that).
RETIRED_DISPATCH_SCHEDULE_IDS: tuple[str, ...] = ()
