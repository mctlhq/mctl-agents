"""Activity: read the merge state of a proposal's implementation PR.

Stage 6.1 merge detection (ADR-006, #214): after implement, DevLoopWorkflow
polls this activity until the PR merges or closes. Polling, not a webhook —
no new ingress/HMAC surface in mctl-api for slice 1.

The PR is resolved from the proposal's ``.status.yaml`` in mctl-gitops
(``pr:`` field, written by the implementer's commit step) via the GitHub
contents API — same "GitHub is the authoritative state, the worker mounts
no gitops clone" rule as ``find_proposal_slug``. The PR itself is then read
from the GitHub pulls API.
"""
from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass

import httpx
from temporalio import activity

from orchestrator.temporal.activities.proposals import (
    AGENTS_STATE_PREFIX,
    GITOPS_REPO,
    REQUEST_TIMEOUT_SECONDS,
    ProposalListingError,
    _resolve_token,
)

# Both canonical PR-URL forms the proposal state supports (mirrors
# run_shepherd._parse_pr_url): the web form and the API form a repaired
# .status.yaml may carry.
_PR_URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_API_URL_RE = re.compile(r"https://api\.github\.com/repos/([\w.-]+/[\w.-]+)/pulls/(\d+)")
# .status.yaml is flat investigator-written YAML; the pr field is a bare URL
# on its own line (see run_shepherd's reader, which does data.get("pr")).
_PR_FIELD_RE = re.compile(r"^pr:\s*['\"]?(\S+?)['\"]?\s*$", re.MULTILINE)


@dataclass(frozen=True)
class PRState:
    """Snapshot of the implementation PR's lifecycle state.

    ``found=False`` means the proposal's .status.yaml has no readable PR
    reference (not committed yet, or the file/PR is gone) — the caller
    decides how long to keep waiting for one to appear.
    ``state`` is "OPEN", "MERGED" or "CLOSED" (closed without merging).
    """

    found: bool
    pr_url: str | None = None
    repo: str | None = None
    number: int | None = None
    state: str | None = None
    merged: bool = False
    merge_commit: str | None = None


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }


@activity.defn
async def get_pr_state(service: str, slug: str) -> PRState:
    """Return the current PRState for ``service``/``slug``'s proposal.

    Transport and unexpected HTTP failures raise (retryable) so Temporal's
    retry policy re-runs the read; a 404 on the status file or the PR
    returns ``found=False`` rather than failing the loop — GitHub answers
    unauthorized/missing lookups identically, and the polling caller
    treats "nothing to see" as "keep waiting within its own deadline".
    """
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        # Same rule as find_proposal_slug: never fall through to an
        # unauthenticated request against a private repo — the 404 would
        # masquerade as "no PR yet" forever.
        raise ProposalListingError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated lookup"
        )

    status_url = (
        f"https://api.github.com/repos/{GITOPS_REPO}/contents/"
        f"{AGENTS_STATE_PREFIX}/{service}/proposals/{slug}/.status.yaml"
    )
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(status_url, params={"ref": "main"}, headers=_headers(token))
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading {status_url} failed: {exc}") from exc
        if response.status_code == 404:
            return PRState(found=False)
        if response.status_code != 200:
            raise ProposalListingError(
                f"reading {status_url} returned HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            content = base64.b64decode(response.json().get("content", "")).decode("utf-8", "replace")
        except (ValueError, TypeError) as exc:
            raise ProposalListingError(f"undecodable contents payload from {status_url}") from exc

        field = _PR_FIELD_RE.search(content)
        if not field:
            return PRState(found=False)
        recorded_pr_url = field.group(1)
        pr_match = _PR_URL_RE.search(recorded_pr_url) or _PR_API_URL_RE.search(recorded_pr_url)
        if not pr_match:
            return PRState(found=False)
        repo, number = pr_match.group(1), int(pr_match.group(2))

        # The recorded PR must live in this proposal's own repository: a
        # stale or hand-edited .status.yaml pointing at a valid PR in some
        # OTHER repo must not complete this loop's merge detection with an
        # unrelated PR's state.
        if repo != f"mctlhq/{service}":
            activity.logger.warning(
                "pr_state service=%s slug=%s: recorded PR %s is outside "
                "mctlhq/%s — refusing to track it",
                service,
                slug,
                recorded_pr_url,
                service,
            )
            return PRState(found=False, pr_url=recorded_pr_url, repo=repo, number=number)

        pr_api = f"https://api.github.com/repos/{repo}/pulls/{number}"
        try:
            pr_response = await client.get(pr_api, headers=_headers(token))
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading {pr_api} failed: {exc}") from exc
        if pr_response.status_code == 404:
            # Recorded PR vanished (repo/PR deleted, token lost access).
            return PRState(found=False, pr_url=recorded_pr_url, repo=repo, number=number)
        if pr_response.status_code != 200:
            raise ProposalListingError(
                f"reading {pr_api} returned HTTP {pr_response.status_code}: {pr_response.text[:200]}"
            )
        data = pr_response.json()

    merged = bool(data.get("merged"))
    if merged:
        state = "MERGED"
    elif (data.get("state") or "").lower() == "closed":
        state = "CLOSED"
    else:
        state = "OPEN"
    result = PRState(
        found=True,
        pr_url=data.get("html_url") or recorded_pr_url,
        repo=repo,
        number=number,
        state=state,
        merged=merged,
        merge_commit=data.get("merge_commit_sha") if merged else None,
    )
    activity.logger.info(
        "pr_state service=%s slug=%s pr=%s#%s state=%s", service, slug, repo, number, state
    )
    return result
