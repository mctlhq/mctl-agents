"""Closed-loop contract primitives (mctlhq/mctl-agents#1006, DevLoop M2a).

Tri-state reads of a loop's PR, branch and gates, plus the pure
`closed_loop_decision` that maps them to one outcome. NOTHING here is called
from `Dispatcher._dispatch` in M2a: wiring is M2c. The one rule every read
follows is that a failed read answers `unknown`, never `absent` or "safe".
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import quote

import httpx

from orchestrator.temporal.activities.pr_state import _STATUS_FIELD_RE, _headers
from orchestrator.temporal.activities.proposals import (
    AGENTS_STATE_PREFIX,
    GITOPS_REPO,
    REQUEST_TIMEOUT_SECONDS,
    _resolve_token,
)
from orchestrator.work_context import execution_requests as xr

GITHUB_API = "https://api.github.com"
OWNER = "mctlhq"

PRVerdict = Literal["open", "merged", "closed", "absent", "unknown"]
BranchVerdict = Literal["exists", "absent", "unknown"]
GatesVerdict = Literal["passed", "not-passed", "unknown"]

#: Proposal `status:` values meaning the loop has passed its gates.
GATES_PASSED_STATUSES = frozenset({"accepted", "implementing", "review-fixing", "merged"})
#: Proposal `status:` values meaning it has not.
GATES_NOT_PASSED_STATUSES = frozenset({"proposed"})

#: `closed_loop_decision` outcomes.
PASS = "pass"  # noqa: S105 — a decision outcome, not a credential
REFUSE_WORK_MERGED = f"refuse:{xr.WORK_MERGED}"
REFUSE_NO_ACTIONABLE_INTENT = f"refuse:{xr.NO_ACTIONABLE_INTENT}"
DEFER_UNOBSERVED = "defer:closed-loop-unobserved"

LOOP_RUNNING = "running"


@dataclass(frozen=True)
class PRRead:
    verdict: PRVerdict
    url: str = ""
    reason: str = ""


@dataclass(frozen=True)
class BranchRead:
    verdict: BranchVerdict
    reason: str = ""


@dataclass(frozen=True)
class GatesRead:
    verdict: GatesVerdict
    reason: str = ""


class ClosedLoopReadPort(Protocol):
    async def pr_by_branch(self, repo: str, branch: str) -> PRRead: ...

    async def branch(self, repo: str, branch: str) -> BranchRead: ...

    async def gates(self, service: str, slug: str) -> GatesRead: ...


async def _token() -> str:
    try:
        return await asyncio.to_thread(_resolve_token)
    except Exception:  # noqa: BLE001 — a failed token read is an unobserved loop
        return ""


async def read_pr_by_branch(repo: str, branch: str, *, transport: httpx.AsyncBaseTransport | None = None) -> PRRead:
    """PR for `feat/agents-<slug>` in `repo` (`mctlhq/<service>`)."""
    token = await _token()
    if not token:
        return PRRead("unknown", reason="no github token")
    url = f"{GITHUB_API}/repos/{repo}/pulls"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.get(
                url, params={"head": f"{OWNER}:{branch}", "state": "all"}, headers=_headers(token)
            )
        if response.status_code != 200:
            return PRRead("unknown", reason=f"HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, list) or not all(isinstance(e, dict) for e in payload):
            return PRRead("unknown", reason="unexpected pulls payload")
        if not payload:
            return PRRead("absent")
        merged = [e for e in payload if e.get("merged_at")]
        if merged:
            return PRRead("merged", url=str(merged[0].get("html_url") or ""))
        opened = [e for e in payload if str(e.get("state") or "").lower() == "open"]
        if opened:
            return PRRead("open", url=str(opened[0].get("html_url") or ""))
        if all(str(e.get("state") or "").lower() == "closed" for e in payload):
            return PRRead("closed", url=str(payload[0].get("html_url") or ""))
        return PRRead("unknown", reason="unrecognised pull state")
    except (httpx.HTTPError, ValueError) as exc:
        return PRRead("unknown", reason=f"{type(exc).__name__}")


async def read_branch(repo: str, branch: str, *, transport: httpx.AsyncBaseTransport | None = None) -> BranchRead:
    """Branch existence. A 404 means `absent` only when the repository itself
    was readable in the same call: GitHub answers an unauthorised lookup and a
    missing one the same way."""
    token = await _token()
    if not token:
        return BranchRead("unknown", reason="no github token")
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, transport=transport) as client:
            repo_response = await client.get(f"{GITHUB_API}/repos/{repo}", headers=_headers(token))
            if repo_response.status_code != 200:
                return BranchRead("unknown", reason=f"repository HTTP {repo_response.status_code}")
            response = await client.get(
                f"{GITHUB_API}/repos/{repo}/branches/{quote(branch, safe='')}",
                headers=_headers(token),
            )
    except httpx.HTTPError as exc:
        return BranchRead("unknown", reason=f"{type(exc).__name__}")
    if response.status_code == 200:
        return BranchRead("exists")
    if response.status_code == 404:
        return BranchRead("absent")
    return BranchRead("unknown", reason=f"HTTP {response.status_code}")


async def read_gates(service: str, slug: str, *, transport: httpx.AsyncBaseTransport | None = None) -> GatesRead:
    """Whether the proposal passed its gates, from its `.status.yaml` `status:`."""
    token = await _token()
    if not token:
        return GatesRead("unknown", reason="no github token")
    url = f"{GITHUB_API}/repos/{GITOPS_REPO}/contents/{AGENTS_STATE_PREFIX}/{service}/proposals/{slug}/.status.yaml"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.get(url, params={"ref": "main"}, headers=_headers(token))
        if response.status_code != 200:
            return GatesRead("unknown", reason=f"HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            return GatesRead("unknown", reason="unexpected contents payload")
        content = base64.b64decode(payload.get("content", "")).decode("utf-8")
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        return GatesRead("unknown", reason=f"{type(exc).__name__}")
    field = _STATUS_FIELD_RE.search(content)
    status = field.group(1) if field else ""
    if status in GATES_PASSED_STATUSES:
        return GatesRead("passed")
    if status in GATES_NOT_PASSED_STATUSES:
        return GatesRead("not-passed")
    return GatesRead("unknown", reason=f"status {status or '<none>'}")


class GitHubClosedLoopReads:
    """The real `ClosedLoopReadPort`."""

    async def pr_by_branch(self, repo: str, branch: str) -> PRRead:
        return await read_pr_by_branch(repo, branch)

    async def branch(self, repo: str, branch: str) -> BranchRead:
        return await read_branch(repo, branch)

    async def gates(self, service: str, slug: str) -> GatesRead:
        return await read_gates(service, slug)


def closed_loop_decision(loop_state: str, intent_id: int | None, pr: PRRead, gates: GatesRead) -> str:
    """One outcome for a resume aimed at a closed or absent loop. Pure.

    First match wins. An `unknown` input that could change the outcome
    defers; it is never read as absent or safe.
    """
    if loop_state == LOOP_RUNNING:
        return PASS  # the loop's own validator decides
    if pr.verdict == "merged":
        return REFUSE_WORK_MERGED
    if pr.verdict == "unknown":
        return DEFER_UNOBSERVED
    if intent_id is None:
        if gates.verdict == "passed":
            return REFUSE_NO_ACTIONABLE_INTENT
        if gates.verdict == "unknown":
            return DEFER_UNOBSERVED
    return PASS
