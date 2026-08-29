"""Activities: observe a merged PR's release reaching the cluster.

Stages 6.2 (release observation) and 6.3 (post-deploy verify) of ADR-006
(#215). Read-only throughout: release-please and ArgoCD already drive the
deploy, and this loop only watches it land. Nothing here can start,
retry, or roll back a deploy.

Three reads, each its own activity so the workflow can poll them
independently:

- ``resolve_deploy_target`` — which (team, app) a repo's release deploys
  to. That mapping exists nowhere as data; it is only implied by the
  arguments each repo's ``release-please.yml`` passes to mctl-gitops'
  ``release-deploy.yaml``, so this reads that workflow file from GitHub.
- ``get_release_after`` — the tag release-please cut for this merge, if
  it cut one at all (a docs-only or non-conventional merge produces no
  release, and that is not a failure).
- ``get_deploy_status`` — mctl-api's existing status endpoint, i.e. what
  ArgoCD reports for that app.
"""
from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass

import httpx
from temporalio import activity

from orchestrator.temporal.activities.proposals import (
    REQUEST_TIMEOUT_SECONDS,
    ProposalListingError,
    _resolve_token,
)
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

RELEASE_PLEASE_WORKFLOW = ".github/workflows/release-please.yml"

# The dispatch arguments release-deploy.yaml is called with. Written as
# `-f name=value` (optionally quoted) inside the workflow's run block.
_DISPATCH_ARG_RE = r"-f\s+{name}=[\"']?([^\s\"'\\]+)"
# values_path names the gitops file whose image tag the release bumps.
# `platform-gitops/services/<team>/<app>/values.yaml` is the deployed
# app's own values file, and its <app> segment is authoritative: for
# mctl-agents the dispatch's component_name is "mctl-agents" while the
# deployed ArgoCD application is "admins-mctl-agents-worker", so
# component_name alone resolves to an application that does not exist.
_SERVICE_VALUES_RE = re.compile(
    r"platform-gitops/services/(?P<team>[\w.-]+)/(?P<app>[\w.-]+)/values\.ya?ml"
)


@dataclass(frozen=True)
class DeployTarget:
    """The ArgoCD application a repo's releases deploy to."""

    team: str
    app: str


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    published_at: str


@dataclass(frozen=True)
class DeployStatus:
    """What ArgoCD reports for one application.

    ``image_tag`` is None for platform applications that mctl-api does not
    resolve a service record for (mctl-api itself, for one) — there the
    sync/health pair is the only signal available, and the caller must not
    wait for a tag that will never appear.
    """

    found: bool
    image_tag: str | None = None
    health: str | None = None
    sync_status: str | None = None


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    }


async def _github_token() -> str:
    token = await asyncio.to_thread(_resolve_token)
    if not token:
        # Same rule as find_proposal_slug/get_pr_state: never fall through
        # to an unauthenticated request, whose 404 would masquerade as
        # "nothing released yet" forever.
        raise ProposalListingError(
            "no GitHub token available (GITHUB_TOKEN_FILE unreadable and "
            "GITHUB_TOKEN unset); refusing an unauthenticated lookup"
        )
    return token


@activity.defn
async def resolve_deploy_target(repo: str) -> DeployTarget | None:
    """Return the (team, app) ``repo``'s releases deploy to, or None.

    None means "this repo's release deploys no application" — either it
    has no release-please dispatch at all, or it only bumps cluster
    templates via values_glob (mctl-agents' CWFTs, before the worker
    values_path was added in #220). Both are ordinary, not failures.
    """
    token = await _github_token()
    url = f"https://api.github.com/repos/{repo}/contents/{RELEASE_PLEASE_WORKFLOW}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(url, params={"ref": "main"}, headers=_github_headers(token))
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading {url} failed: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ProposalListingError(
            f"reading {url} returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProposalListingError(f"non-JSON contents payload from {url}") from exc
    if not isinstance(payload, dict):
        raise ProposalListingError(f"unexpected contents payload type from {url}")
    try:
        content = base64.b64decode(payload.get("content", "")).decode("utf-8", "replace")
    except (ValueError, TypeError) as exc:
        raise ProposalListingError(f"undecodable contents payload from {url}") from exc

    values_path = _find_arg(content, "values_path")
    if values_path:
        match = _SERVICE_VALUES_RE.search(values_path)
        if match:
            return DeployTarget(team=match.group("team"), app=match.group("app"))
        # A values_path outside services/ (bootstrap templates, for
        # instance) names no service directory; fall through to the
        # dispatch's own team/component, which is what release-deploy
        # would have defaulted to anyway.

    team = _find_arg(content, "team_name")
    component = _find_arg(content, "component_name")
    if not team or not component:
        return None
    return DeployTarget(team=team, app=component)


def _find_arg(content: str, name: str) -> str | None:
    match = re.search(_DISPATCH_ARG_RE.format(name=re.escape(name)), content)
    return match.group(1) if match else None


@activity.defn
async def get_release_after(repo: str, after: str) -> ReleaseInfo | None:
    """Newest published release of ``repo`` published strictly after ``after``.

    ``after`` is an ISO-8601 timestamp (the PR's merge time). None means
    release-please has not cut a release for this merge — which is the
    normal outcome for a docs-only or non-conventional merge, so the
    caller treats it as "nothing to observe", not as a failure.
    """
    token = await _github_token()
    url = f"https://api.github.com/repos/{repo}/releases"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(url, params={"per_page": 20}, headers=_github_headers(token))
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading {url} failed: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ProposalListingError(
            f"reading {url} returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        releases = response.json()
    except ValueError as exc:
        raise ProposalListingError(f"non-JSON payload from {url}") from exc
    if not isinstance(releases, list):
        raise ProposalListingError(f"unexpected releases payload type from {url}")

    newest: ReleaseInfo | None = None
    for entry in releases:
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        published = entry.get("published_at")
        tag = entry.get("tag_name")
        # String compare is exact for GitHub's Zulu-normalised ISO-8601
        # timestamps, which are fixed-width and lexicographically ordered.
        if not isinstance(published, str) or not isinstance(tag, str) or published <= after:
            continue
        if newest is None or published > newest.published_at:
            newest = ReleaseInfo(tag=tag, published_at=published)
    return newest


@activity.defn
async def get_deploy_status(team: str, app: str) -> DeployStatus:
    """Read ArgoCD's view of one application through mctl-api.

    ``found=False`` means mctl-api knows no such ArgoCD application — the
    app is not deployed under that name. The caller stops waiting rather
    than polling a name that will never resolve.
    """
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(f"/api/v1/status/{team}/{app}", headers=auth_headers())
        except httpx.RequestError as exc:
            raise ProposalListingError(f"reading status for {team}/{app} failed: {exc}") from exc
    if response.status_code == 404:
        return DeployStatus(found=False)
    if response.status_code != 200:
        raise ProposalListingError(
            f"status for {team}/{app} returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProposalListingError(f"non-JSON status payload for {team}/{app}") from exc
    if not isinstance(payload, dict):
        raise ProposalListingError(f"unexpected status payload type for {team}/{app}")

    argocd = payload.get("argocd")
    if not isinstance(argocd, dict):
        # mctl-api answers 200 with argocd:null and a note when the
        # application does not exist under this name.
        return DeployStatus(found=False)
    service = payload.get("service")
    image_tag = service.get("imageTag") if isinstance(service, dict) else None
    status = DeployStatus(
        found=True,
        image_tag=image_tag if isinstance(image_tag, str) and image_tag else None,
        health=argocd.get("health"),
        sync_status=argocd.get("syncStatus"),
    )
    activity.logger.info(
        "deploy_status %s/%s tag=%s health=%s sync=%s",
        team,
        app,
        status.image_tag,
        status.health,
        status.sync_status,
    )
    return status
