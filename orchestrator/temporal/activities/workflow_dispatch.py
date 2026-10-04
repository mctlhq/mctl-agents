"""Activities: dispatch a GitHub workflow and observe that a run appeared.

mctl-agents#559. The dispatch API answers 204 with no run id, so "done" means
a `workflow_dispatch` run of the workflow was OBSERVED, created at or after the
fire's `not_before` minus `SKEW_SLACK`. Every attempt (first or retry) checks
for such a run BEFORE dispatching, so retries cannot double-dispatch.

An unreadable runs listing is never read as "no run", and a non-2xx dispatch
is never read as success.

`report_dispatch_failure` files (or comments on) one alert issue in the target
repo, because a failed Temporal execution alerts nobody.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.temporal.activities.proposals import _resolve_token

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 20.0
POLL_INTERVAL = 10.0
OBSERVE_TIMEOUT = 300.0
SKEW_SLACK = timedelta(seconds=60)

ALERT_LABEL = "scheduled-dispatch-failed"
ALERT_LABEL_COLOR = "d73a4a"

NON_RETRYABLE_TYPES = ("NoGitHubToken", "DispatchRejected", "RunNotObserved", "AlertReportRejected")


class NoGitHubToken(ApplicationError):
    def __init__(self) -> None:
        super().__init__(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and GITHUB_TOKEN unset); "
            "refusing an unauthenticated request",
            type="NoGitHubToken",
            non_retryable=True,
        )


class DispatchRejected(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="DispatchRejected", non_retryable=True)


class DispatchFailed(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="DispatchFailed")


class RunsListingUnreadable(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="RunsListingUnreadable")


class RunNotObserved(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="RunNotObserved", non_retryable=True)


class AlertReportRejected(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="AlertReportRejected", non_retryable=True)


class AlertIssueSearchUnreadable(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="AlertIssueSearchUnreadable")


class AlertReportFailed(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="AlertReportFailed")


@dataclass(frozen=True)
class DispatchInput:
    repo: str = ""
    workflow_file: str = ""
    ref: str = "main"
    not_before: str = ""  # RFC3339 UTC, fixed once per fire by the workflow


@dataclass(frozen=True)
class DispatchResult:
    dispatched: bool = False
    run_id: int = 0
    html_url: str = ""


@dataclass(frozen=True)
class FailureReport:
    repo: str = ""
    workflow_file: str = ""
    workflow_id: str = ""
    error_type: str = ""
    message: str = ""


@dataclass(frozen=True)
class FailureReportResult:
    issue_number: int = 0
    html_url: str = ""
    created: bool = False


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _token() -> str:
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        raise NoGitHubToken()
    return token


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _find_run(
    client: httpx.AsyncClient, inp: DispatchInput, since: datetime
) -> DispatchResult | None:
    created = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        resp = await client.get(
            f"/repos/{inp.repo}/actions/workflows/{inp.workflow_file}/runs",
            params={
                "event": "workflow_dispatch",
                "branch": inp.ref,
                "created": f">={created}",
                "per_page": 20,
            },
        )
    except httpx.HTTPError as exc:
        raise RunsListingUnreadable(f"runs listing unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise RunsListingUnreadable(f"runs listing returned HTTP {resp.status_code}")
    try:
        runs = resp.json()["workflow_runs"]
        if not isinstance(runs, list):
            raise TypeError("workflow_runs is not a list")
        if not runs:
            return None
        run = max(runs, key=lambda r: int(r["id"]))
        return DispatchResult(dispatched=False, run_id=int(run["id"]), html_url=str(run.get("html_url", "")))
    except (ValueError, KeyError, TypeError) as exc:
        raise RunsListingUnreadable(f"runs listing malformed: {exc}") from exc


@activity.defn
async def dispatch_and_observe(inp: DispatchInput) -> DispatchResult:
    token = await _token()
    since = _parse_utc(inp.not_before) - SKEW_SLACK
    async with httpx.AsyncClient(
        base_url=GITHUB_API, headers=_headers(token), timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        existing = await _find_run(client, inp, since)
        if existing is not None:
            activity.logger.info(
                "dispatch %s/%s already satisfied by run %s", inp.repo, inp.workflow_file, existing.run_id
            )
            return existing

        try:
            resp = await client.post(
                f"/repos/{inp.repo}/actions/workflows/{inp.workflow_file}/dispatches",
                json={"ref": inp.ref},
            )
        except httpx.HTTPError as exc:
            raise DispatchFailed(f"dispatch transport error: {exc}") from exc
        if not 200 <= resp.status_code < 300:
            if resp.status_code == 429 or resp.status_code >= 500:
                raise DispatchFailed(f"dispatch returned HTTP {resp.status_code}")
            raise DispatchRejected(f"dispatch returned HTTP {resp.status_code}: {resp.text[:300]}")

        deadline = time.monotonic() + OBSERVE_TIMEOUT
        while True:
            found = await _find_run(client, inp, since)
            if found is not None:
                result = DispatchResult(dispatched=True, run_id=found.run_id, html_url=found.html_url)
                activity.logger.info(
                    "dispatched %s %s: run %s %s", inp.repo, inp.workflow_file, result.run_id, result.html_url
                )
                return result
            if time.monotonic() >= deadline:
                raise RunNotObserved(
                    f"dispatch of {inp.repo} {inp.workflow_file} was accepted but no "
                    f"workflow_dispatch run appeared within {int(OBSERVE_TIMEOUT)}s"
                )
            activity.heartbeat()
            await asyncio.sleep(POLL_INTERVAL)


def _check(resp: httpx.Response, what: str, ok: tuple[int, ...] = (200, 201)) -> None:
    if resp.status_code in ok:
        return
    if resp.status_code == 429 or resp.status_code >= 500:
        raise AlertReportFailed(f"{what} returned HTTP {resp.status_code}")
    raise AlertReportRejected(f"{what} returned HTTP {resp.status_code}: {resp.text[:300]}")


async def _call(what: str, coro: Any) -> httpx.Response:
    try:
        return await coro
    except httpx.HTTPError as exc:
        raise AlertReportFailed(f"{what} transport error: {exc}") from exc


@activity.defn
async def report_dispatch_failure(rep: FailureReport) -> FailureReportResult:
    token = await _token()
    title = f"Scheduled dispatch failed: {rep.workflow_file}"
    body = (
        f"Scheduled dispatch of `{rep.workflow_file}` failed.\n\n"
        f"- Temporal workflow id: `{rep.workflow_id}`\n"
        f"- Error type: `{rep.error_type}`\n"
        f"- Message: {rep.message}\n"
        f"- Reported at: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%SZ')}\n"
    )
    async with httpx.AsyncClient(
        base_url=GITHUB_API, headers=_headers(token), timeout=REQUEST_TIMEOUT_SECONDS
    ) as client:
        resp = await _call("label lookup", client.get(f"/repos/{rep.repo}/labels/{ALERT_LABEL}"))
        if resp.status_code == 404:
            resp = await _call(
                "label create",
                client.post(
                    f"/repos/{rep.repo}/labels",
                    json={"name": ALERT_LABEL, "color": ALERT_LABEL_COLOR},
                ),
            )
            _check(resp, "label create")
        else:
            _check(resp, "label lookup", ok=(200,))

        resp = await _call(
            "issue search",
            client.get(
                f"/repos/{rep.repo}/issues",
                params={"state": "open", "labels": ALERT_LABEL, "per_page": 100},
            ),
        )
        if resp.status_code != 200:
            if resp.status_code == 429 or resp.status_code >= 500:
                raise AlertIssueSearchUnreadable(f"issue search returned HTTP {resp.status_code}")
            raise AlertReportRejected(f"issue search returned HTTP {resp.status_code}")
        try:
            issues = resp.json()
            if not isinstance(issues, list):
                raise TypeError("issues body is not a list")
            match = next(
                (i for i in issues if i.get("title") == title and "pull_request" not in i), None
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise AlertIssueSearchUnreadable(f"issue search malformed: {exc}") from exc

        if match is not None:
            number = int(match["number"])
            resp = await _call(
                "issue comment",
                client.post(f"/repos/{rep.repo}/issues/{number}/comments", json={"body": body}),
            )
            _check(resp, "issue comment", ok=(201,))
            return FailureReportResult(
                issue_number=number, html_url=str(match.get("html_url", "")), created=False
            )

        resp = await _call(
            "issue create",
            client.post(
                f"/repos/{rep.repo}/issues",
                json={"title": title, "body": body, "labels": [ALERT_LABEL]},
            ),
        )
        _check(resp, "issue create", ok=(201,))
        try:
            data = resp.json()
            return FailureReportResult(
                issue_number=int(data["number"]), html_url=str(data.get("html_url", "")), created=True
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise AlertReportFailed(f"issue create response malformed: {exc}") from exc
