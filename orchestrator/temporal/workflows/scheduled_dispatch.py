"""ScheduledDispatchWorkflow: dispatch a GitHub workflow and observe its run.

mctl-agents#559. Started weekly by a Temporal Schedule per
`scheduled_dispatch.WEEKLY_DISPATCH_TARGETS` entry. `not_before` is fixed ONCE
here, so every activity attempt reuses it and the activity's pre-dispatch check
makes a retry idempotent.

Errors are not swallowed: a terminal failure first files an alert issue in the
target repo (a Failed execution alone alerts nobody), then re-raises the
ORIGINAL error so the execution is still marked Failed. If the alert itself
cannot be delivered (for example a GitHub token outage) the workflow logs the
`ALERT_UNDELIVERED_MARKER` line at ERROR and increments a worker metric of the
same name, neither of which needs a GitHub credential.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.workflow_dispatch import (
        DispatchInput,
        DispatchResult,
        FailureReport,
        dispatch_and_observe,
        report_dispatch_failure,
    )

ALERT_UNDELIVERED_MARKER = "scheduled_dispatch_alert_undelivered"

DISPATCH_TIMEOUT = timedelta(minutes=8)
DISPATCH_HEARTBEAT_TIMEOUT = timedelta(minutes=1)
# Worst case: label lookup + ALERT_SEARCH_MAX_PAGES (10) search pages + one
# write = 12 requests at REQUEST_TIMEOUT_SECONDS (20s) = 240s, plus slack.
REPORT_TIMEOUT = timedelta(minutes=5)

DISPATCH_NON_RETRYABLE = ["DispatchRejected", "RunNotObserved", "InvalidDispatchTarget"]
REPORT_NON_RETRYABLE = ["AlertReportRejected", "InvalidDispatchTarget"]
DISPATCH_RETRY_POLICY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=DISPATCH_NON_RETRYABLE)
REPORT_RETRY_POLICY = RetryPolicy(maximum_attempts=3, non_retryable_error_types=REPORT_NON_RETRYABLE)


@dataclass
class ScheduledDispatchInput:
    repo: str = ""
    workflow_file: str = ""
    ref: str = "main"


def _root_error(exc: BaseException) -> tuple[str, str]:
    cause = exc.cause if isinstance(exc, ActivityError) and exc.cause is not None else exc
    if isinstance(cause, ApplicationError):
        return cause.type or "ApplicationError", cause.message
    return type(cause).__name__, str(cause)


@workflow.defn
class ScheduledDispatchWorkflow:
    @workflow.run
    async def run(self, inp: ScheduledDispatchInput) -> DispatchResult:
        not_before = workflow.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            result = await workflow.execute_activity(
                dispatch_and_observe,
                DispatchInput(
                    repo=inp.repo, workflow_file=inp.workflow_file, ref=inp.ref, not_before=not_before
                ),
                start_to_close_timeout=DISPATCH_TIMEOUT,
                heartbeat_timeout=DISPATCH_HEARTBEAT_TIMEOUT,
                retry_policy=DISPATCH_RETRY_POLICY,
            )
        except Exception as exc:
            error_type, message = _root_error(exc)
            try:
                await workflow.execute_activity(
                    report_dispatch_failure,
                    FailureReport(
                        repo=inp.repo,
                        workflow_file=inp.workflow_file,
                        workflow_id=workflow.info().workflow_id,
                        error_type=error_type,
                        message=message,
                    ),
                    start_to_close_timeout=REPORT_TIMEOUT,
                    retry_policy=REPORT_RETRY_POLICY,
                )
            except Exception as report_exc:  # noqa: BLE001 — never mask the original error
                report_type, report_message = _root_error(report_exc)
                workflow.logger.error(
                    "%s repo=%s workflow_file=%s workflow_id=%s error_type=%s report_error=%s: %s",
                    ALERT_UNDELIVERED_MARKER,
                    inp.repo,
                    inp.workflow_file,
                    workflow.info().workflow_id,
                    error_type,
                    report_type,
                    report_message,
                )
                workflow.metric_meter().create_counter(
                    ALERT_UNDELIVERED_MARKER,
                    "Scheduled dispatch failures whose alert issue could not be filed",
                ).add(1, {"repo": inp.repo, "workflow_file": inp.workflow_file})
            raise
        workflow.logger.info(
            "scheduled dispatch of %s %s observed run %s %s",
            inp.repo,
            inp.workflow_file,
            result.run_id,
            result.html_url,
        )
        return result
