"""Read agents-state proposals from GitHub, not from a gitops checkout.

Reconcile's two activities (discovery, orphans) used to glob
``/workdir/mctl-gitops/platform-gitops/agents-state`` — a directory the Argo
CWFT's ``clone-gitops`` step creates and the Temporal worker deliberately
does not have (no checkout, no deploy key; see HANDOFF-RECONCILEWORKFLOW and
the OOMKill in #179). Both therefore hit ``not state_dir.is_dir()`` and
returned empty on every tick from 2026-08-06 — when the Argo reconcile cron
was deleted as "fully migrated to Temporal" — until #270. Nothing alerted,
because a sweep that found no drift and a sweep that never looked return the
same thing.

So this module reads the authoritative copy over HTTP, the same rule
``proposals.py`` and ``pr_state.py`` already follow. Writes are unaffected:
``.status.yaml`` commits stay inside the Argo CWFT under the
``mctl-gitops-main-writes`` mutex.

Cost, since this runs every 15 minutes over every proposal:

- one git-trees call enumerates every ``.status.yaml`` with its blob SHA
  (212 proposals / 2137 entries today, well inside one response)
- blob contents are cached by SHA. A blob's content cannot change without
  its SHA changing, so a hit is exact, and a steady-state tick reads only
  the proposals that actually moved
- the pulls API is called only for proposals that carry a ``pr:`` — a few
  dozen, and never cached: PR state is the thing being reconciled
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass

import httpx
import yaml
from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.temporal.activities.pr_state import _PR_API_URL_RE, _PR_URL_RE
from orchestrator.temporal.activities.proposals import (
    AGENTS_STATE_PREFIX,
    GITOPS_REPO,
    REQUEST_TIMEOUT_SECONDS,
    ProposalListingError,
    _resolve_token,
)

TREE_URL = (
    f"https://api.github.com/repos/{GITOPS_REPO}/git/trees/"
    f"main:{AGENTS_STATE_PREFIX}?recursive=1"
)

# Bound on the blob cache. Well above today's 212 proposals, and small
# enough that a worker that never restarts cannot grow it without limit.
_BLOB_CACHE_MAX = 4096
_blob_cache: OrderedDict[str, tuple[str, str | None]] = OrderedDict()

# How many blob/PR reads to have in flight at once. The worker shares one
# GitHub token with every dev loop; a burst of 200 parallel reads would
# spend the secondary rate limit on a background sweep.
_FETCH_CONCURRENCY = 8


@dataclass(frozen=True)
class ProposalStateRef:
    """One proposal's committed state on gitops main.

    The GitHub-backed counterpart of run_shepherd.ProposalRef, minus the
    on-disk paths — nothing here can open a file.
    """

    service: str
    slug: str
    status: str
    pr_url: str | None


@dataclass(frozen=True)
class PRSnapshot:
    repo: str
    number: int
    merged: bool
    closed_unmerged: bool


async def _gather_or_raise(coros: list) -> list:
    """asyncio.gather that lets every read finish before it raises.

    Plain `gather` propagates the first exception immediately but does NOT
    cancel its siblings. Here the caller's `async with httpx.AsyncClient`
    then closes the pool out from under those still-running reads, which
    fail into nobody's hands — "Task exception was never retrieved" once
    per stranded read, every 15 minutes, on any transient GitHub hiccup
    (claude P3 + agy P2 on #271).

    `return_exceptions=True` makes gather wait for all of them, so the
    client outlives every read that uses it; the first failure is then
    re-raised with its own type intact, which is what decides whether
    Temporal retries the activity.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }


def _cache_get(sha: str) -> tuple[str, str | None] | None:
    hit = _blob_cache.get(sha)
    if hit is not None:
        _blob_cache.move_to_end(sha)
    return hit


def _cache_put(sha: str, value: tuple[str, str | None]) -> None:
    _blob_cache[sha] = value
    _blob_cache.move_to_end(sha)
    while len(_blob_cache) > _BLOB_CACHE_MAX:
        _blob_cache.popitem(last=False)


def _parse_status_yaml(text: str) -> tuple[str, str | None]:
    """Return (status, pr_url) from a .status.yaml body.

    Mirrors run_shepherd._load_status: flat YAML written by the
    investigator, defaulting to "proposed" the way _discover_refs does.
    """
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("status file is not a mapping")
    status = str(data.get("status", "proposed"))
    pr = data.get("pr")
    return status, str(pr) if pr else None


async def _resolve_token_async() -> str:
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        # Same rule as find_proposal_slug: mctl-gitops is private and GitHub
        # answers unauthorized reads with 404, so an unauthenticated sweep
        # would report "no proposals anywhere" — the exact false-clean this
        # module exists to remove.
        raise ProposalListingError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated reconcile sweep"
        )
    return token


async def _get_json(client: httpx.AsyncClient, url: str, token: str) -> object:
    try:
        response = await client.get(url, headers=_headers(token))
    except httpx.RequestError as exc:
        raise ProposalListingError(f"reading {url} failed: {exc}") from exc
    if response.status_code != 200:
        raise ProposalListingError(
            f"reading {url} returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ProposalListingError(f"non-JSON payload from {url}") from exc


async def _read_blob(client: httpx.AsyncClient, sha: str, token: str) -> tuple[str, str | None]:
    cached = _cache_get(sha)
    if cached is not None:
        return cached
    payload = await _get_json(
        client, f"https://api.github.com/repos/{GITOPS_REPO}/git/blobs/{sha}", token
    )
    if not isinstance(payload, dict):
        raise ProposalListingError(f"unexpected blob payload for {sha}")
    try:
        text = base64.b64decode(payload.get("content", "")).decode("utf-8", "replace")
    except (ValueError, TypeError) as exc:
        raise ProposalListingError(f"undecodable blob payload for {sha}") from exc
    parsed = _parse_status_yaml(text)
    _cache_put(sha, parsed)
    return parsed


def _status_paths(tree: dict) -> list[tuple[str, str, str]]:
    """Extract (service, slug, blob_sha) for every proposal status file."""
    if tree.get("truncated"):
        # The trees API caps its response and says so. A truncated listing
        # cannot distinguish "no drift" from "did not look", which is the
        # failure this whole module is a fix for — so refuse the tick
        # rather than sweep a subset and report it as complete.
        raise ApplicationError(
            f"{TREE_URL} returned a truncated tree — the reconcile sweep would "
            "cover an unknown subset of proposals; prune old proposal dirs or "
            "page the tree before trusting this result",
            non_retryable=True,
        )
    entries = tree.get("tree")
    if not isinstance(entries, list):
        raise ProposalListingError(f"unexpected tree payload from {TREE_URL}")

    found: list[tuple[str, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = str(entry.get("path", ""))
        parts = path.split("/")
        # <service>/proposals/<slug>/.status.yaml — anything else (a
        # proposal's requirements.md, _mentor/, a stray file) is not state.
        if len(parts) != 4 or parts[1] != "proposals" or parts[3] != ".status.yaml":
            continue
        service, slug = parts[0], parts[2]
        if service.startswith("_"):
            continue
        sha = entry.get("sha")
        if not sha:
            continue
        found.append((service, slug, str(sha)))
    return found


async def list_proposal_refs() -> list[ProposalStateRef]:
    """Every proposal on gitops main, with its committed status and PR.

    Raises rather than returning a short list: a caller cannot tell an empty
    result from a failed one, and this sweep's whole purpose is to be the
    thing that notices drift.
    """
    token = await _resolve_token_async()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        tree = await _get_json(client, TREE_URL, token)
        if not isinstance(tree, dict):
            raise ProposalListingError(f"unexpected tree payload type from {TREE_URL}")
        paths = _status_paths(tree)

        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def one(service: str, slug: str, sha: str) -> ProposalStateRef | None:
            async with semaphore:
                try:
                    status, pr_url = await _read_blob(client, sha, token)
                except (ValueError, yaml.YAMLError) as exc:
                    # One unparseable status file must not blind the sweep to
                    # the other 200 — same tolerance _discover_refs has.
                    activity.logger.warning(
                        "reconcile: %s/%s has an unreadable .status.yaml (%s); skipping",
                        service,
                        slug,
                        exc,
                    )
                    return None
            return ProposalStateRef(service=service, slug=slug, status=status, pr_url=pr_url)

        results = await _gather_or_raise([one(s, g, sha) for s, g, sha in paths])

    refs = [r for r in results if r is not None]
    activity.logger.info(
        "reconcile: read %d proposal(s) from %s (%d unreadable)",
        len(refs),
        GITOPS_REPO,
        len(paths) - len(refs),
    )
    return refs


async def fetch_pr_snapshots(refs: list[ProposalStateRef]) -> dict[tuple[str, str], PRSnapshot]:
    """PR state for every ref that records one, keyed by (service, slug).

    Refs whose ``pr:`` is missing, unparseable, or points outside the
    proposal's own repository are simply absent from the result — the same
    "refuse to track someone else's PR" rule get_pr_state applies.
    """
    wanted: list[tuple[ProposalStateRef, str, int]] = []
    for ref in refs:
        if not ref.pr_url:
            continue
        match = _PR_URL_RE.search(ref.pr_url) or _PR_API_URL_RE.search(ref.pr_url)
        if not match:
            continue
        repo, number = match.group(1), int(match.group(2))
        if repo.lower() != f"mctlhq/{ref.service}".lower():
            activity.logger.warning(
                "reconcile: %s/%s records PR %s outside mctlhq/%s — not tracking it",
                ref.service,
                ref.slug,
                ref.pr_url,
                ref.service,
            )
            continue
        wanted.append((ref, repo, number))

    if not wanted:
        return {}

    snapshots: dict[tuple[str, str], PRSnapshot] = {}
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        token = await _resolve_token_async()

        async def one(ref: ProposalStateRef, repo: str, number: int) -> None:
            url = f"https://api.github.com/repos/{repo}/pulls/{number}"
            async with semaphore:
                try:
                    response = await client.get(url, headers=_headers(token))
                except httpx.RequestError as exc:
                    raise ProposalListingError(f"reading {url} failed: {exc}") from exc
                if response.status_code == 404:
                    # Recorded PR is gone or out of reach. Absent, not merged
                    # and not rejected: projecting a status from a PR we
                    # cannot see would be a guess.
                    return
                if response.status_code != 200:
                    raise ProposalListingError(
                        f"reading {url} returned HTTP {response.status_code}: {response.text[:200]}"
                    )
                data = response.json()
            if not isinstance(data, dict):
                raise ProposalListingError(f"unexpected payload type from {url}")
            merged = bool(data.get("merged"))
            closed = (data.get("state") or "").lower() == "closed"
            snapshots[(ref.service, ref.slug)] = PRSnapshot(
                repo=repo,
                number=number,
                merged=merged,
                closed_unmerged=closed and not merged,
            )

        await _gather_or_raise([one(ref, repo, number) for ref, repo, number in wanted])

    return snapshots
