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
    for this (agent, environment) pair. That is the expected steady state
    for an agent the registry does not yet track (e.g. before its first
    `mctl_promote_agent`), not a failure: DevLoopWorkflow falls back to the
    target ClusterWorkflowTemplate's own baked-in default image, exactly
    the compatibility shim the plan's phase 5 describes.
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
                image_ref = f"{repo}@{digest}" if digest else (f"{repo}:{version}" if repo else "")
                break

        return ResolvedRelease(agent=agent, environment=environment, version=version, image_ref=image_ref)
