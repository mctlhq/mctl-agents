"""Activity: resolve an agent's released version from the mctl-api agent
registry (plan phase 2), and look up that version's image reference.

Runs in the Temporal worker, not in Argo — it's a fast, deterministic-enough
HTTP round trip, exactly the class of step the plan calls out as wasting a
pod start + repo clone on every cron tick today.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from temporalio import activity

from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

REQUEST_TIMEOUT_SECONDS = 30.0


def _image_ref(repo: str, version: str, digest: str) -> str:
    """Build the image reference the CWFT pulls.

    image_repository is supposed to be bare (mctl-api validates this on
    publish as of 2026-08-06), but a row published before that validation
    existed can still carry an embedded tag or digest. Blindly appending
    ":{version}" or "@{digest}" to one of those doubles it into an invalid
    reference the pod can never pull — incident 2026-08-06, caught by the
    mctl-academy smoke test (mctl-agents-investigate-2b91b916 stuck on
    InvalidImageName with "...:1.22.0:1.22.0"). The already-tagged/digested
    check must run before either append, not just the tag one — a legacy
    repo carrying its own "@sha256:..." plus a separately populated
    image_digest field would otherwise still double into "...@sha256:x@sha256:y".
    Trust an already-tagged/digested repo as-is instead.
    """
    if not repo:
        return ""
    last_segment = repo.rsplit("/", 1)[-1]
    if "@" in repo or ":" in last_segment:
        return repo
    if digest:
        return f"{repo}@{digest}"
    return f"{repo}:{version}"


@dataclass(frozen=True)
class ResolvedRelease:
    """None-image_ref means the release exists but no version metadata could
    be found for it (should not normally happen — a published version and
    its release both live in the same store — but callers must not assume
    an image_ref is always present)."""

    agent: str
    environment: str
    version: str
    image_ref: str


@activity.defn
async def resolve_agent_release(agent: str, environment: str) -> ResolvedRelease | None:
    """Resolve which version is released to `environment` for `agent`.

    Returns None — not an exception — when nothing has ever been promoted
    for this (agent, environment) pair. This activity stays neutral about
    what that means, deliberately: its signature and return value must not
    change, or histories recorded before the A4 marker would fail to
    replay.

    What None MEANS is now the caller's decision, and it is no longer a
    fallback. Since A4 (#241) DevLoopWorkflow fails non-retryably for the
    investigator and implementer rather than running the CWFT's baked-in
    default image, and declines in-loop shepherd ticks (leaving them to
    the cron sweeper). The registry is authoritative — every release
    publishes and promotes every manifest via
    tools/publish_agent_release.py — so an unresolvable agent is a real
    misconfiguration, not the steady state it was during phase 5.
    """
    headers = auth_headers()
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        resolve_resp = await client.get(
            f"/api/v1/agents/{agent}/resolve",
            params={"environment": environment},
            headers=headers,
        )
        if resolve_resp.status_code == 404:
            return None
        resolve_resp.raise_for_status()
        version = resolve_resp.json()["version"]

        # GET /api/v1/agents/{name}/versions is unpaginated as of this
        # writing (ListVersions in mctl-api's agentregistry store returns
        # every row for the agent in one response) — if that ever changes,
        # a version published past the first page would silently fail to be
        # found here and image_ref would fall back to "" (the CWFT's own
        # default), not an error.
        versions_resp = await client.get(f"/api/v1/agents/{agent}/versions", headers=headers)
        versions_resp.raise_for_status()
        image_ref = ""
        for item in versions_resp.json().get("items", []):
            if item.get("version") == version:
                repo = item.get("image_repository", "")
                digest = item.get("image_digest") or ""
                # Prefer the immutable digest once the registry supplies one
                # (plan phase 3's "publish by digest, not a mutable tag"
                # note) — fall back to the version as a tag otherwise.
                image_ref = _image_ref(repo, version, digest)
                break

        return ResolvedRelease(agent=agent, environment=environment, version=version, image_ref=image_ref)
