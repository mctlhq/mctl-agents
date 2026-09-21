"""Activity: resolve the agents-state proposal slug for a GitHub issue.

DevLoopWorkflow scopes its implement step with the CWFT's ``service``
parameter, but that alone is not enough when two loops target the same repo:
both implement runs discover the same accepted-proposal list from their own
(possibly stale) gitops clones, claim overlapping proposals, and their final
commit-and-push steps then rebase-conflict on each other's ``.status.yaml``
(observed 2026-08-28, mctl-portal issues 79/80 — see mctlhq/mctl-agents#203).
Passing the proposal's ``slug`` as well makes each loop touch only its own
proposal directory, so concurrent same-repo loops write disjoint files and
the commit step's existing rebase-retry resolves cleanly.

The slug is resolved from GitHub (mctl-gitops main), not from a local
agents-state checkout: the Temporal worker pod deliberately mounts no gitops
clone, and GitHub is the authoritative state anyway. ``run_issue_investigator``
derives every slug deterministically as ``issue-<N>-<kebab-title>``, so a
directory-name prefix match on ``issue-<N>-`` is exact (the trailing dash
rules out issue-9 matching issue-98's directory).

One prefix match needs no more than that. Only when SEVERAL directories
match does this activity read their ``.status.yaml`` files, to retire the
``rejected`` ones through ``proposal_identity.select_proposal_slug`` — the
same decision ``run_issue_investigator.resolve_slug`` makes locally. Those
extra reads happen only on the path that used to fail outright, so the
steady-state cost of a lookup is unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import os
from pathlib import Path

import httpx
import yaml
from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.proposal_identity import (
    AmbiguousProposalError,
    ProposalCandidate,
    select_proposal_slug,
)

GITOPS_REPO = "mctlhq/mctl-gitops"
AGENTS_STATE_PREFIX = "platform-gitops/agents-state"
REQUEST_TIMEOUT_SECONDS = 20.0
CONTENTS_API_LISTING_CAP = 1000


class ProposalListingError(Exception):
    """Transient failure listing the proposals directory — retryable."""


def _resolve_token() -> str:
    """Read the freshest GitHub token without touching process globals.

    Same source order as refresh_github_token (GITHUB_TOKEN_FILE first,
    env fallback), but read-only: concurrent activities on the shared
    event loop each get their own local value instead of racing writes to
    os.environ. Runs in a thread (file I/O) via asyncio.to_thread.
    """
    path = os.environ.get("GITHUB_TOKEN_FILE", "").strip()
    if path:
        try:
            token = Path(path).read_text().strip()
        except (OSError, ValueError):
            token = ""
        if token:
            return token
    return os.environ.get("GITHUB_TOKEN", "").strip()


@activity.defn
async def find_proposal_slug(service: str, issue_number: str) -> str | None:
    """Return the proposal slug for ``service``'s issue ``issue_number``.

    None means the proposals directory exists (or is absent) but holds no
    ``issue-<N>-*`` entry — a genuine "not there", not an error. Transport
    and non-404 HTTP failures raise so Temporal's retry policy re-runs the
    lookup instead of the caller mistaking an outage for a missing proposal.
    """
    # A local mounted-Secret file read (never network), but kept off the
    # event loop anyway — a slow kubelet volume must not stall the worker.
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        # Never fall through to an unauthenticated request: mctl-gitops is
        # private, and GitHub answers unauthorized contents lookups with 404
        # — indistinguishable from "no proposal", which would surface as a
        # misleading permanent workflow failure instead of an auth problem.
        # Raise (retryable) so a token-file refresh between attempts can
        # heal a transient gap, mirroring mctl_client.py's loud mctl_token().
        raise ProposalListingError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated lookup (a "
            "private repo would 404 and masquerade as a missing proposal)"
        )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }

    url = (
        f"https://api.github.com/repos/{GITOPS_REPO}/contents/"
        f"{AGENTS_STATE_PREFIX}/{service}/proposals"
    )
    # int() round-trip: a manually started workflow can carry an otherwise
    # valid URL like /issues/007, but run_issue_investigator built the dir
    # from the canonical number — issue-7-, never issue-007-.
    prefix = f"issue-{int(issue_number)}-"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params={"ref": "main"}, headers=headers)

            if response.status_code == 404:
                # Service never had a proposal committed — same "not found"
                # as an empty listing, not an infrastructure failure.
                return None
            if response.status_code != 200:
                raise ProposalListingError(
                    f"listing {url} returned HTTP {response.status_code}: {response.text[:200]}"
                )

            entries = response.json()
            if not isinstance(entries, list):
                raise ProposalListingError(f"unexpected non-directory response from {url}")
            if len(entries) >= CONTENTS_API_LISTING_CAP:
                # The contents API silently truncates directory listings at
                # 1000 entries with no pagination — a missing match in a
                # truncated listing proves nothing. Refuse rather than
                # misreport "no proposal" (and prune old proposal dirs if
                # this ever fires).
                raise ApplicationError(
                    f"{url} returned {len(entries)} entries — at or above the "
                    "contents-API listing cap; result would be unreliable until old "
                    "proposal dirs are pruned",
                    non_retryable=True,
                )

            matches = sorted(
                entry["name"]
                for entry in entries
                if entry.get("type") == "dir"
                and str(entry.get("name", "")).startswith(prefix)
            )
            if len(matches) <= 1:
                # The common path: no status read at all, exactly as before.
                return matches[0] if matches else None

            # Several directories claim this issue. Read each one's status
            # so a `rejected` leftover — the shape mctl-agents#438 leaves
            # behind after a closed-unmerged PR — stops blocking its
            # replacement. Statuses are fetched concurrently because this
            # sits on DevLoopWorkflow's critical path.
            # `return_exceptions=True` so a failing read does not unwind out
            # of the `async with` while its siblings are still in flight: the
            # client would close under them and the worker's event loop would
            # then log "Task exception was never retrieved" once per orphan.
            # The first exception is re-raised unchanged, so the failure a
            # caller sees is identical.
            statuses = await asyncio.gather(
                *(_read_proposal_status(client, headers, service, slug) for slug in matches),
                return_exceptions=True,
            )
            for status in statuses:
                if isinstance(status, BaseException):
                    raise status
            candidates = [
                ProposalCandidate(slug=slug, status=status)
                for slug, status in zip(matches, statuses, strict=True)
                if not isinstance(status, BaseException)
            ]
            try:
                return select_proposal_slug(candidates)
            except AmbiguousProposalError as exc:
                # Non-retryable for the same reason the flat refusal was:
                # no number of retries turns two live proposals into one.
                raise ApplicationError(
                    f"cannot resolve {prefix}* under {service}: {exc}",
                    non_retryable=True,
                ) from exc
    except httpx.RequestError as exc:
        raise ProposalListingError(f"listing {url} failed: {exc}") from exc


async def _read_proposal_status(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    service: str,
    slug: str,
) -> str | None:
    """The ``status:`` in one proposal's ``.status.yaml``, or None.

    None means "could not be read", which
    ``proposal_identity.select_proposal_slug`` treats as live — a proposal
    is never retired on missing evidence. A transport error or an
    unexpected HTTP status raises instead, so a GitHub outage cannot look
    like an unreadable status and silently change which slug wins.

    Transport failures are caught here rather than by the caller's own
    ``httpx.RequestError`` handler: that one names the listing URL, which
    answered fine, and would point an operator at the wrong request.
    """
    url = (
        f"https://api.github.com/repos/{GITOPS_REPO}/contents/"
        f"{AGENTS_STATE_PREFIX}/{service}/proposals/{slug}/.status.yaml"
    )
    try:
        response = await client.get(url, params={"ref": "main"}, headers=headers)
    except httpx.RequestError as exc:
        raise ProposalListingError(f"reading {url} failed: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ProposalListingError(
            f"reading {url} returned HTTP {response.status_code}: {response.text[:200]}"
        )

    payload = response.json()
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    try:
        text = base64.b64decode(payload.get("content", "")).decode("utf-8")
        data = yaml.safe_load(text)
    except (binascii.Error, ValueError, yaml.YAMLError):
        # A hand-edited or half-written status file is missing evidence,
        # not a reason to fail the whole lookup.
        return None
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    return status if isinstance(status, str) else None
