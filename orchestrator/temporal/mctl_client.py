"""Shared mctl-api HTTP client setup for Temporal activities.

Separate from orchestrator/mcp_guard.py and MCTL_MCP_URL in config/settings.py
on purpose: those are the MCP surface a Claude Agent SDK session inside Argo
talks to (JSON-RPC over the streamable-HTTP transport). Activities in this
package are plain server-to-server REST calls against the same mctl-api
process's HTTP API — no MCP handshake, no SDK session, just httpx.
"""
from __future__ import annotations

import os

MCTL_API_BASE_URL = os.environ.get("MCTL_API_BASE_URL", "https://api.mctl.ai")


def mctl_token() -> str:
    """MCTL_TOKEN for this worker's own service identity — the same static
    admin service token backing mctl-agents' other mctl-api access (see
    reference_mctl_api_static_service_token). Raising here rather than
    silently sending an unauthenticated request turns a missing/rotated
    token into an immediate, legible activity failure instead of a stream
    of 401s discovered only from mctl-api's logs.
    """
    token = os.environ.get("MCTL_TOKEN", "").strip()
    if not token:
        raise RuntimeError("MCTL_TOKEN is not set — the dev-loop worker cannot call mctl-api")
    return token


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {mctl_token()}"}
