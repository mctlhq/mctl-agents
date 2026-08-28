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
clone, and GitHub is the authoritative state anyway. No YAML parsing is
needed — ``run_issue_investigator`` derives every slug deterministically as
``issue-<N>-<kebab-title>``, so a directory-name prefix match on
``issue-<N>-`` is exact (the trailing dash rules out issue-9 matching
issue-98's directory).
"""
from __future__ import annotations

import os

import httpx
from temporalio import activity

from orchestrator.github_token import refresh_github_token

GITOPS_REPO = "mctlhq/mctl-gitops"
AGENTS_STATE_PREFIX = "platform-gitops/agents-state"
REQUEST_TIMEOUT_SECONDS = 20.0
CONTENTS_API_LISTING_CAP = 1000


class ProposalListingError(Exception):
    """Transient failure listing the proposals directory — retryable."""


@activity.defn
async def find_proposal_slug(service: str, issue_number: str) -> str | None:
    """Return the proposal slug for ``service``'s issue ``issue_number``.

    None means the proposals directory exists (or is absent) but holds no
    ``issue-<N>-*`` entry — a genuine "not there", not an error. Transport
    and non-404 HTTP failures raise so Temporal's retry policy re-runs the
    lookup instead of the caller mistaking an outage for a missing proposal.
    """
    refresh_github_token()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        # Never fall through to an unauthenticated request: mctl-gitops is
        # private, and GitHub answers unauthorized contents lookups with 404
        # — indistinguishable from "no proposal", which would surface as a
        # misleading permanent workflow failure instead of an auth problem.
        # Raise (retryable) so a token-file refresh between attempts can
        # heal a transient gap, mirroring mctl_client.py's loud mctl_token().
        raise ProposalListingError(
            "GITHUB_TOKEN is empty after refresh_github_token(); refusing an "
            "unauthenticated lookup (a private repo would 404 and masquerade "
            "as a missing proposal)"
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
    except httpx.TransportError as exc:
        raise ProposalListingError(f"listing {url} failed: {exc}") from exc

    if response.status_code == 404:
        # Service never had a proposal committed — same "not found" as an
        # empty listing, not an infrastructure failure.
        return None
    if response.status_code != 200:
        raise ProposalListingError(
            f"listing {url} returned HTTP {response.status_code}: {response.text[:200]}"
        )

    entries = response.json()
    if not isinstance(entries, list):
        raise ProposalListingError(f"unexpected non-directory response from {url}")
    if len(entries) >= CONTENTS_API_LISTING_CAP:
        # The contents API silently truncates directory listings at 1000
        # entries with no pagination — a missing match in a truncated
        # listing proves nothing. Refuse rather than misreport "no
        # proposal" (and prune old proposal dirs if this ever fires).
        raise ProposalListingError(
            f"{url} returned {len(entries)} entries — at or above the "
            f"contents-API listing cap; result would be unreliable"
        )

    matches = sorted(
        entry["name"]
        for entry in entries
        if entry.get("type") == "dir" and str(entry.get("name", "")).startswith(prefix)
    )
    if len(matches) > 1:
        # Two directories for one issue should be impossible (the slug is
        # deterministic); refuse to guess rather than implement the wrong one.
        raise ProposalListingError(
            f"multiple proposal dirs match {prefix}* under {service}: {matches}"
        )
    return matches[0] if matches else None
