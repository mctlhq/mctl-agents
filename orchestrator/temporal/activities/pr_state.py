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
# The proposal's own `status:` field, same flat-YAML shape as `_PR_FIELD_RE`
# (mctl-agents#516). Read from the SAME decoded `.status.yaml` content the PR
# field already came from — no extra HTTP request.
_STATUS_FIELD_RE = re.compile(r"^status:\s*['\"]?(\S+?)['\"]?\s*$", re.MULTILINE)

# confirm_no_pr (M2b-1c, mctl-agents#1041): deliberately separate, stricter
# patterns so get_pr_state stays byte-identical.
_ANY_PR_KEY_RE = re.compile(r"""^\s*["']?pr["']?\s*:""", re.MULTILINE)
_STRICT_STATUS_RE = re.compile(r"^status:\s*\S", re.MULTILINE)
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_GITHUB_API = "https://api.github.com"
MAX_PULL_PAGES = 10


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
    # Merge timestamp (ISO-8601, GitHub's own). Stage 6.2 needs it to tell
    # the release this merge produced from one cut moments earlier by an
    # unrelated merge. Defaulted so results recorded before this field
    # existed still deserialize.
    merged_at: str | None = None
    # Current head SHA. Lifecycle ownership pins its decisions to the version
    # of the entity the owner actually observed, and a head change is the
    # PR-shaped definition of "something was effected" (mctlhq/.github#57).
    # Defaulted so results recorded before this field existed still
    # deserialize, same as merged_at above.
    head_sha: str | None = None
    # The proposal's own `status:` field from the same `.status.yaml` read
    # (mctl-agents#516) — DevLoopWorkflow's merge watch reads this to tell a
    # proposal parked in a terminal status (no machine will advance it
    # further) from one still progressing. `None` means "could not be read"
    # (the file itself 404'd or was undecodable), NEVER "live" — set on every
    # OTHER return path below, including the found=False ones, because the
    # file was decoded even when no usable `pr:` field came out of it.
    # Defaulted so results recorded before this field existed still
    # deserialize, same as merged_at/head_sha above.
    proposal_status: str | None = None


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
            payload = response.json()
        except ValueError as exc:
            raise ProposalListingError(f"non-JSON contents payload from {status_url}") from exc
        if not isinstance(payload, dict):
            # A directory listing (JSON list) instead of a file payload.
            raise ProposalListingError(f"unexpected contents payload type from {status_url}")
        try:
            content = base64.b64decode(payload.get("content", "")).decode("utf-8", "replace")
        except (ValueError, TypeError) as exc:
            raise ProposalListingError(f"undecodable contents payload from {status_url}") from exc

        # The file is decoded from here on, so every return below can carry
        # the proposal's own status alongside its PR-lookup verdict — no
        # extra HTTP request (mctl-agents#516).
        status_field = _STATUS_FIELD_RE.search(content)
        proposal_status = status_field.group(1) if status_field else None

        field = _PR_FIELD_RE.search(content)
        if not field:
            return PRState(found=False, proposal_status=proposal_status)
        recorded_pr_url = field.group(1)
        pr_match = _PR_URL_RE.search(recorded_pr_url) or _PR_API_URL_RE.search(recorded_pr_url)
        if not pr_match:
            return PRState(found=False, proposal_status=proposal_status)
        repo, number = pr_match.group(1), int(pr_match.group(2))

        # The recorded PR must live in this proposal's own repository: a
        # stale or hand-edited .status.yaml pointing at a valid PR in some
        # OTHER repo must not complete this loop's merge detection with an
        # unrelated PR's state.
        if repo.lower() != f"mctlhq/{service}".lower():
            activity.logger.warning(
                "pr_state service=%s slug=%s: recorded PR %s is outside "
                "mctlhq/%s — refusing to track it",
                service,
                slug,
                recorded_pr_url,
                service,
            )
            return PRState(
                found=False, pr_url=recorded_pr_url, repo=repo, number=number, proposal_status=proposal_status
            )

        pr_api = f"https://api.github.com/repos/{repo}/pulls/{number}"
        try:
            pr_response = await client.get(pr_api, headers=_headers(token))
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading {pr_api} failed: {exc}") from exc
        if pr_response.status_code == 404:
            # Recorded PR vanished (repo/PR deleted, token lost access).
            return PRState(
                found=False, pr_url=recorded_pr_url, repo=repo, number=number, proposal_status=proposal_status
            )
        if pr_response.status_code != 200:
            raise ProposalListingError(
                f"reading {pr_api} returned HTTP {pr_response.status_code}: {pr_response.text[:200]}"
            )
        try:
            data = pr_response.json()
        except ValueError as exc:
            raise ProposalListingError(f"non-JSON payload from {pr_api}") from exc
        if not isinstance(data, dict):
            # Same guard the contents read above already has: without it a
            # list or string payload raises AttributeError, which is not a
            # ProposalListingError and so reads as a defect to the merge
            # watch's transient check rather than a retryable read.
            raise ProposalListingError(f"unexpected payload type from {pr_api}")

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
        merged_at=data.get("merged_at") if merged else None,
        head_sha=(data.get("head") or {}).get("sha") if isinstance(data.get("head"), dict) else None,
        proposal_status=proposal_status,
    )
    activity.logger.info(
        "pr_state service=%s slug=%s pr=%s#%s state=%s", service, slug, repo, number, state
    )
    return result


@dataclass(frozen=True)
class NoPrProof:
    """Proof-of-absence verdict. ``reason`` is ``no-link-no-pull`` when
    confirmed, else the first thing that blocked the proof."""

    confirmed: bool
    reason: str


def _strict_status_content(payload: object) -> str:
    """Strictly decode a contents-API file payload, or raise."""
    if not isinstance(payload, dict):
        raise ProposalListingError("unexpected contents payload type")
    if payload.get("encoding") != "base64":
        raise ProposalListingError("contents payload is not base64-encoded")
    content = payload.get("content")
    if not isinstance(content, str):
        raise ProposalListingError("contents payload has no string content")
    try:
        raw = base64.b64decode(content.replace("\n", ""), validate=True)
        return raw.decode("utf-8")
    except (ValueError, TypeError) as exc:  # binascii.Error and UnicodeDecodeError are ValueErrors
        raise ProposalListingError("undecodable contents payload") from exc


@activity.defn
async def confirm_no_pr(service: str, slug: str) -> NoPrProof:
    """Prove that ``service``/``slug`` has no implementation PR and no
    implementation branch. Fails closed: any read that fails raises, and
    ``confirmed=True`` is returned only when every read positively shows
    absence."""
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        raise ProposalListingError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated lookup"
        )
    if not (
        isinstance(service, str) and isinstance(slug, str)
        and _NAME_RE.fullmatch(service) and _NAME_RE.fullmatch(slug)
    ):
        raise ProposalListingError("service and slug must be plain [A-Za-z0-9._-]+ names")

    headers = _headers(token)
    status_url = (
        f"{_GITHUB_API}/repos/{GITOPS_REPO}/contents/"
        f"{AGENTS_STATE_PREFIX}/{service}/proposals/{slug}/.status.yaml"
    )
    branch = f"feat/agents-{slug}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(status_url, params={"ref": "main"}, headers=headers)
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading {status_url} failed: {exc}") from exc
        if response.status_code == 404:
            return NoPrProof(False, "status-file-missing")
        if response.status_code != 200:
            raise ProposalListingError(f"reading {status_url} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProposalListingError(f"non-JSON contents payload from {status_url}") from exc
        content = _strict_status_content(payload)
        if not content.strip():
            return NoPrProof(False, "status-file-empty")
        if not _STRICT_STATUS_RE.search(content):
            return NoPrProof(False, "status-line-missing")
        if _ANY_PR_KEY_RE.search(content):
            return NoPrProof(False, "pr-line-present")

        pulls_url = f"{_GITHUB_API}/repos/mctlhq/{service}/pulls"
        url: str | None = pulls_url
        params: dict[str, str | int] | None = {
            "head": f"mctlhq:{branch}", "state": "all", "per_page": 100,
        }
        pages = 0
        while url:
            pages += 1
            if pages > MAX_PULL_PAGES:
                raise ProposalListingError(f"listing {pulls_url} exceeded {MAX_PULL_PAGES} pages")
            try:
                page_resp = await client.get(url, params=params, headers=headers)
            except httpx.RequestError as exc:
                raise ProposalListingError(f"listing {pulls_url} failed: {exc}") from exc
            if page_resp.status_code != 200:
                raise ProposalListingError(
                    f"listing {pulls_url} returned HTTP {page_resp.status_code}"
                )
            try:
                items = page_resp.json()
            except ValueError as exc:
                raise ProposalListingError(f"non-JSON payload from {pulls_url}") from exc
            if not isinstance(items, list):
                raise ProposalListingError(f"unexpected payload shape from {pulls_url}")
            if items:
                return NoPrProof(False, "pull-request-listed")
            nxt = page_resp.links.get("next", {}).get("url")
            if nxt:
                if not str(nxt).startswith(_GITHUB_API + "/"):
                    raise ProposalListingError(f"next link outside the GitHub API: {nxt}")
                url, params = str(nxt), None
            else:
                url = None

        branch_url = f"{_GITHUB_API}/repos/mctlhq/{service}/branches/{branch}"
        try:
            branch_resp = await client.get(branch_url, headers=headers)
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading {branch_url} failed: {exc}") from exc
        if branch_resp.status_code == 200:
            return NoPrProof(False, "branch-exists")
        if branch_resp.status_code != 404:
            raise ProposalListingError(f"reading {branch_url} returned HTTP {branch_resp.status_code}")

    activity.logger.info("confirm_no_pr service=%s slug=%s reason=no-link-no-pull", service, slug)
    return NoPrProof(True, "no-link-no-pull")
