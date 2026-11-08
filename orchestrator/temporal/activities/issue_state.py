"""Activity: read a GitHub issue's lifecycle state.

`DevLoopWorkflow` parks at the approval wait for as long as a human takes to
review the proposal — durably, and (mctl-agents#420) bounded rather than
forever: it polls this activity on its own cadence while parked, and this is
also the same activity the gate immediately after the wait resolves uses.
The issue that started the loop can be closed while it waits (reopened
elsewhere, superseded, or resolved directly); either read lets the workflow
notice, so a loop is not spent approving and implementing a proposal whose
reason for existing is already gone (mctl-agents#410).

Same worker-side pattern as `activities/proposals.py` and
`activities/pr_state.py`: `_resolve_token()` off the event loop, `httpx`,
and a dedicated retryable exception for transport/non-404 failures — never
an unauthenticated request against `mctlhq/*` (private).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from temporalio import activity

from orchestrator.temporal.activities.proposals import (
    REQUEST_TIMEOUT_SECONDS,
    ProposalListingError,
    _resolve_token,
)


class IssueStateError(ProposalListingError):
    """Transient failure reading an issue's state — retryable.

    Subclasses `ProposalListingError` rather than introducing a sibling
    exception class: the name is a misnomer for this call site, but the
    workflow's retry policy (and any test asserting on it) already keys
    off that type across every activity in this package.
    """


@dataclass(frozen=True)
class IssueState:
    """Snapshot of a GitHub issue's lifecycle state.

    ``state`` is GitHub's own ``"open"`` / ``"closed"``. ``state_reason``
    is ``None`` while open, and while closed on issues predating GitHub's
    introduction of the field (or on some API paths) — callers treat a
    ``None`` reason on a closed issue as "completed", the same rule
    `orchestrator.source_issue.read_source_issue` applies.
    """

    state: str
    state_reason: str | None = None
    closed_at: str | None = None


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }


@activity.defn
async def get_issue_state(repo: str, issue_number: int) -> IssueState:
    """Return the current `IssueState` for `repo#issue_number`.

    Transport and unexpected HTTP failures raise `IssueStateError`
    (retryable) so Temporal's retry policy re-runs the read. A 404 also
    raises rather than being read as "open" — an issue this activity was
    told to check that GitHub cannot find is a defect worth surfacing, not
    a fact to guess at silently.
    """
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        # Same rule as find_proposal_slug / get_pr_state: never fall
        # through to an unauthenticated request against a private repo.
        raise IssueStateError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated lookup"
        )

    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_headers(token))
    except httpx.RequestError as exc:
        raise IssueStateError(f"reading {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise IssueStateError(
            f"reading {url} returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise IssueStateError(f"non-JSON payload from {url}") from exc
    if not isinstance(data, dict) or "state" not in data:
        raise IssueStateError(f"unexpected payload shape from {url}")

    result = IssueState(
        state=data["state"],
        state_reason=data.get("state_reason"),
        closed_at=data.get("closed_at"),
    )
    activity.logger.info(
        "issue_state repo=%s issue=%s state=%s state_reason=%s",
        repo, issue_number, result.state, result.state_reason,
    )
    return result


@activity.defn
async def post_issue_comment(repo: str, issue_number: int, body: str) -> bool:
    """Best-effort comment on `repo#issue_number` (mctl-agents#701). Returns
    whether it was posted. A missing token or a 4xx answer is a logged quiet
    `False`; transport errors, 429 and 5xx raise `IssueStateError` so the
    caller's bounded retry policy can re-run it (the workflow treats a final
    failure as best effort, so it can never fail the loop)."""
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        activity.logger.warning(
            "post_issue_comment repo=%s issue=%s skipped: no GitHub token", repo, issue_number
        )
        return False
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, headers=_headers(token), json={"body": body})
    except httpx.RequestError as exc:
        activity.logger.warning(
            "post_issue_comment repo=%s issue=%s transport error: %s", repo, issue_number, exc
        )
        raise IssueStateError(f"posting {url} failed: {exc}") from exc
    if response.status_code == 201:
        return True
    activity.logger.warning(
        "post_issue_comment repo=%s issue=%s returned HTTP %s: %s",
        repo, issue_number, response.status_code, response.text[:200],
    )
    if response.status_code == 429 or response.status_code >= 500:
        raise IssueStateError(f"posting {url} returned HTTP {response.status_code}")
    return False


# --- merge-watch reconcile record (M2b-1c, mctl-agents#1041) ----------------

RECONCILE_MARKER_PREFIX = "merge-watch-reconcile-required"
RECORD_WRITER_LOGIN = "mctl-agents[bot]"
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
MAX_COMMENT_PAGES = 50  # 5,000 comments at per_page=100
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class ReconcileRecordInput:
    repo: str
    issue_number: int
    workflow_id: str
    deadline: str
    exit_name: str
    last_observed: str  # caller-formatted "<STATE> <url> head=<sha>" or ""
    open_request_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileRecordResult:
    status: str  # "recorded" | "already-recorded" | "issue-gone"
    url: str = ""


def _marker_part(name: str, value: str) -> str:
    if not isinstance(value, str) or not value or "--" in value or any(c.isspace() for c in value):
        raise IssueStateError(f"invalid {name} for the reconcile marker")
    return value


def reconcile_marker(workflow_id: str, deadline: str) -> str:
    """The HTML-comment marker identifying one watch's reconcile record."""
    wf = _marker_part("workflow_id", workflow_id)
    dl = _marker_part("deadline", deadline)
    return f"<!-- {RECONCILE_MARKER_PREFIX} workflow={wf} deadline={dl} -->"


def _is_trusted_author(comment: dict) -> bool:
    user = comment["user"]
    if user.get("login") == RECORD_WRITER_LOGIN and user.get("type") == "Bot":
        return True
    return comment.get("author_association") in TRUSTED_ASSOCIATIONS


def reconcile_comment_body(inp: ReconcileRecordInput) -> str:
    marker = reconcile_marker(inp.workflow_id, inp.deadline)
    return (
        f"{marker}\n"
        "### Merge watch needs manual reconciliation\n\n"
        f"- Workflow: {inp.workflow_id}\n"
        f"- Exit: {inp.exit_name}\n"
        f"- Last observed PR: {inp.last_observed or 'none'}\n"
        f"- Open request ids: {', '.join(inp.open_request_ids) or 'none'}\n\n"
        "Read the pull request by hand, then re-trigger or close this loop deliberately."
    )


def _read_comment_page(response: httpx.Response, url: str) -> list[dict] | str:
    """Validate one comment page. Returns the items, or "gone" on 410."""
    if response.status_code == 410:
        return "gone"
    if response.status_code != 200:
        raise IssueStateError(f"listing {url} returned HTTP {response.status_code}")
    try:
        items = response.json()
    except ValueError as exc:
        raise IssueStateError(f"non-JSON payload from {url}") from exc
    if not isinstance(items, list):
        raise IssueStateError(f"unexpected payload shape from {url}")
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("body"), str)
            or not isinstance(item.get("user"), dict)
        ):
            raise IssueStateError(f"malformed comment item from {url}")
    return items


@activity.defn
async def record_merge_watch_reconcile(inp: ReconcileRecordInput) -> ReconcileRecordResult:
    """Write one idempotent "reconciliation required" comment on the source
    issue. Every attempt lists all comments first, so a retry after a lost
    answer finds its own marker. Any read it cannot trust raises."""
    marker = reconcile_marker(inp.workflow_id, inp.deadline)
    body = reconcile_comment_body(inp)
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        raise IssueStateError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated write"
        )
    base = f"{GITHUB_API}/repos/{inp.repo}/issues/{inp.issue_number}/comments"
    headers = _headers(token)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        url: str | None = base
        params: dict[str, int] | None = {"per_page": 100}
        pages = 0
        while url:
            pages += 1
            if pages > MAX_COMMENT_PAGES:
                raise IssueStateError(f"listing {base} exceeded {MAX_COMMENT_PAGES} pages")
            try:
                response = await client.get(url, params=params, headers=headers)
            except httpx.RequestError as exc:
                raise IssueStateError(f"listing {base} failed: {exc}") from exc
            page = _read_comment_page(response, base)
            if isinstance(page, str):  # "gone" (HTTP 410)
                return ReconcileRecordResult("issue-gone")
            for comment in page:
                if marker in comment["body"] and _is_trusted_author(comment):
                    found = comment.get("html_url")
                    if not isinstance(found, str) or not found:
                        raise IssueStateError(f"trusted marker comment at {base} has no html_url")
                    return ReconcileRecordResult("already-recorded", found)
            nxt = response.links.get("next", {}).get("url")
            if nxt:
                if not str(nxt).startswith(GITHUB_API + "/"):
                    raise IssueStateError(f"next link outside the GitHub API: {nxt}")
                url, params = str(nxt), None
            else:
                url = None

        try:
            post = await client.post(base, headers=headers, json={"body": body})
        except httpx.RequestError as exc:
            raise IssueStateError(f"posting {base} failed: {exc}") from exc
    if post.status_code != 201:
        activity.logger.warning(
            "record_merge_watch_reconcile repo=%s issue=%s post returned HTTP %s",
            inp.repo, inp.issue_number, post.status_code,
        )
        raise IssueStateError(f"posting {base} returned HTTP {post.status_code}")
    try:
        posted = post.json()
    except ValueError as exc:
        raise IssueStateError(f"non-JSON post answer from {base}") from exc
    html_url = posted.get("html_url") if isinstance(posted, dict) else None
    if not isinstance(html_url, str) or not html_url:
        raise IssueStateError(f"post to {base} answered 201 without html_url")
    return ReconcileRecordResult("recorded", html_url)
