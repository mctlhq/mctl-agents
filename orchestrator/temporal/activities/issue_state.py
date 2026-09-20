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
