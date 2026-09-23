"""Activity: read a durable `HumanInputRequest` back out of gitops.

`DevLoopWorkflow` needs to learn whether the just-succeeded
`mctl-agents-investigate` step wrote a clarification request — but the
Temporal worker pod deliberately mounts no gitops clone (see
`orchestrator/temporal/activities/proposals.py`'s module docstring), and
`WorkflowResult` (`activities/argo.py`) exposes only `phase`, no outcome
channel from the Argo pod back to Temporal. This activity closes that gap
the same way `find_proposal_slug` does: a direct GitHub contents-API read of
mctl-gitops main, structurally identical down to the token resolution and
the retryable-vs-404 distinction (mctlhq/mctl-agents#333, ADR 013).

The request lives at
`platform-gitops/agents-state/<service>/proposals/<slug>/human-input/request.json`.
The producer that writes and seals it (the investigator's agent-container
side) does not exist yet — it lands with mctl-gitops#1277; until then this
path is only ever read. This activity returns its raw text; parsing
(`orchestrator.human_input.HumanInputRequest.from_dict`) happens in
workflow code, which is pure.
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

GITOPS_REPO = "mctlhq/mctl-gitops"
AGENTS_STATE_PREFIX = "platform-gitops/agents-state"
REQUEST_TIMEOUT_SECONDS = 20.0


class HumanInputListingError(Exception):
    """Transient failure reading the human-input request file — retryable."""


def _resolve_token() -> str:
    """Same source order and reasoning as proposals.py's `_resolve_token`:
    GITHUB_TOKEN_FILE first, env fallback, read-only so concurrent activities
    on the shared event loop each get their own local value."""
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
async def find_human_input_request(service: str, slug: str) -> str | None:
    """Return the raw text of `<slug>/human-input/request.json`, or None.

    None means the file genuinely does not exist (no clarification pending)
    — not that the read failed. Transport and non-404 HTTP failures raise so
    Temporal's retry policy re-runs the read instead of the caller mistaking
    an outage for "no request".
    """
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        # Never fall through to an unauthenticated request — see
        # find_proposal_slug's identical guard: mctl-gitops is private, and
        # GitHub answers unauthorized reads with 404, indistinguishable from
        # "no request".
        raise HumanInputListingError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated lookup (a "
            "private repo would 404 and masquerade as no pending request)"
        )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }
    url = (
        f"https://api.github.com/repos/{GITOPS_REPO}/contents/"
        f"{AGENTS_STATE_PREFIX}/{service}/proposals/{slug}/human-input/request.json"
    )
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params={"ref": "main"}, headers=headers)
    except httpx.RequestError as exc:
        raise HumanInputListingError(f"reading {url} failed: {exc}") from exc

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise HumanInputListingError(
            f"reading {url} returned HTTP {response.status_code}: {response.text[:200]}"
        )

    document = response.json()
    if not isinstance(document, dict) or document.get("type") != "file":
        raise HumanInputListingError(f"unexpected non-file response from {url}")
    encoding = document.get("encoding")
    content = document.get("content")
    if encoding == "none":
        # The contents API stops inlining `content` above 1 MB and returns
        # encoding "none". A request.json that large is malformed by
        # contract, not a transient condition — raising the retryable
        # listing error here would retry the identical read forever.
        raise ApplicationError(
            f"{url} exceeds the contents-API inline limit; a request.json "
            "this large is malformed",
            type="human_input_malformed",
            non_retryable=True,
        )
    if encoding != "base64" or not isinstance(content, str):
        raise HumanInputListingError(f"{url} returned an unexpected content encoding {encoding!r}")
    try:
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HumanInputListingError(f"{url} content could not be decoded: {exc}") from exc
