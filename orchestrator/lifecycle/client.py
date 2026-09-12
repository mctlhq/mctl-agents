"""HTTP client for the mctl-api lifecycle ownership surface.

Two transports, because there are two callers with different constraints:

- ``OwnershipClient`` is synchronous urllib, for ``run_shepherd`` and the
  implementer, which are ordinary CLI processes inside an Argo pod. It reuses
  the same https pin and no-redirect opener ``run_shepherd`` already applies
  to its dev-loop probe.
- ``AsyncOwnershipClient`` is httpx, for Temporal ACTIVITIES. It must never be
  called from workflow code: network I/O inside ``@workflow.defn`` breaks
  determinism and replay, which is the property this whole contract depends on
  (ADR-010 §9).

A denial is data, not an exception. ``acquire`` returning "someone else owns
this" is the system working, so it comes back as an ``OwnershipAnswer`` rather
than raising — a client that raised would push every caller into a try/except
whose except branch is the most safety-critical path in the loop.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    UNKNOWN,
    UNOWNED,
    EntityRef,
    Owner,
    Ownership,
    OwnershipAnswer,
)

DEFAULT_TIMEOUT_S = 10

# When true, an unreachable store blocks mutating steps instead of letting the
# old mechanism decide. Documented break-glass: set false to restore
# fail-open behaviour during an mctl-api outage.
def ownership_required() -> bool:
    return os.environ.get("LIFECYCLE_OWNERSHIP_REQUIRED", "true").strip().lower() not in {
        "false",
        "no",
        "0",
        "off",
    }


def api_base() -> str:
    return os.environ.get("MCTL_API_BASE_URL", "https://api.mctl.ai").rstrip("/")


class OwnershipUnavailable(RuntimeError):
    """The store could not be reached or is not configured.

    Raised only by the low-level transport. Every public method converts it
    into an ``UNKNOWN`` answer, because the callers that matter must handle
    uncertainty as a value rather than as an exception.
    """


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """Opener that surfaces a 3xx as an HTTPError instead of following it.

    Mirrors ``run_shepherd._no_redirect_opener``. Following a redirect would
    let a misconfigured MCTL_API_BASE_URL silently answer ownership questions
    from somewhere else.
    """

    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirects)


class OwnershipClient:
    """Synchronous client. Safe in Argo pods and CLI entry points."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        self._base = (base_url or api_base()).rstrip("/")
        self._token = token if token is not None else os.environ.get("MCTL_TOKEN", "").strip()
        self._timeout = timeout
        self._opener = _no_redirect_opener()

    # -- transport ------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not self._token:
            raise OwnershipUnavailable("MCTL_TOKEN is not set")
        url = f"{self._base}{path}"
        if not url.startswith("https://"):
            # Operator-provided env; refuse non-https rather than sending a
            # bearer token in the clear (also satisfies ruff S310).
            raise OwnershipUnavailable(f"refusing non-https MCTL_API_BASE_URL: {self._base}")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — scheme checked above
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                parsed = json.loads(body or b"{}")
            except ValueError:
                parsed = {}
            return _HTTPResult(status=exc.code, payload=parsed)
        except Exception as exc:
            raise OwnershipUnavailable(str(exc)) from exc
        try:
            return _HTTPResult(status=200, payload=json.loads(body or b"{}"))
        except ValueError as exc:
            raise OwnershipUnavailable(f"malformed response: {exc}") from exc

    # -- reads ----------------------------------------------------------

    def get(self, entity: EntityRef, phase: str, asking: Owner | None = None) -> OwnershipAnswer:
        try:
            res = self._request(
                "GET",
                f"/api/v1/lifecycle/ownership?kind={_q(entity.kind)}&id={_q(entity.id)}&phase={_q(phase)}",
            )
        except OwnershipUnavailable as exc:
            return OwnershipAnswer(verdict=UNKNOWN, reason=str(exc))
        return _answer(res, asking)

    def get_many(
        self, kind: str, phase: str, ids: list[str], asking: Owner | None = None
    ) -> dict[str, OwnershipAnswer]:
        """One round trip for the whole sweep.

        The per-proposal fan-out this replaces needed a thread pool and a
        60-second wall-clock budget, and anything the budget did not answer was
        swept anyway — an unanswered probe read as "unowned". A single batched
        read removes the pool and that failure mode together.

        On failure EVERY id comes back UNKNOWN rather than absent, so a caller
        iterating the result cannot mistake a store outage for a clean sweep.
        """
        if not ids:
            return {}
        query = f"/api/v1/lifecycle/ownership/batch?kind={_q(kind)}&phase={_q(phase)}"
        query += "".join(f"&id={_q(i)}" for i in ids)
        try:
            res = self._request("GET", query)
        except OwnershipUnavailable as exc:
            return {i: OwnershipAnswer(verdict=UNKNOWN, reason=str(exc)) for i in ids}
        if res.status != 200:
            reason = _error_of(res)
            return {i: OwnershipAnswer(verdict=UNKNOWN, reason=reason) for i in ids}
        found = res.payload.get("ownership") or {}
        out: dict[str, OwnershipAnswer] = {}
        for i in ids:
            raw = found.get(i)
            if raw is None:
                out[i] = OwnershipAnswer(verdict=UNOWNED)
                continue
            own = Ownership.from_payload(raw)
            out[i] = OwnershipAnswer(verdict=_verdict_for(own, asking), ownership=own)
        return out

    # -- writes ---------------------------------------------------------

    def acquire(
        self,
        entity: EntityRef,
        phase: str,
        owner: Owner,
        *,
        proposal_ref: str = "",
        policy_ref: str = "",
        temporal_workflow_id: str = "",
    ) -> OwnershipAnswer:
        return self._write(
            "/api/v1/lifecycle/ownership/acquire",
            entity,
            phase,
            owner,
            proposal_ref=proposal_ref,
            policy_ref=policy_ref,
            temporal_workflow_id=temporal_workflow_id,
        )

    def progress(self, entity: EntityRef, phase: str, owner: Owner, epoch: int, evidence: str) -> OwnershipAnswer:
        """Record that something was EFFECTED.

        Callers must not call this for a poll that observed nothing. Liveness
        is refreshed by any tick through ``acquire``; this field is the one that
        says the work moved, and a heartbeat writing it would let an owner prove
        usefulness forever while achieving nothing.
        """
        return self._write("/api/v1/lifecycle/ownership/progress", entity, phase, owner, epoch=epoch, evidence=evidence)

    def handoff_start(
        self, entity: EntityRef, phase: str, owner: Owner, epoch: int, to: Owner, reason: str = ""
    ) -> OwnershipAnswer:
        return self._write(
            "/api/v1/lifecycle/ownership/handoff/start",
            entity,
            phase,
            owner,
            epoch=epoch,
            reason=reason,
            to_owner_type=to.type,
            to_owner_id=to.id,
        )

    def handoff_complete(self, entity: EntityRef, phase: str, incoming: Owner) -> OwnershipAnswer:
        return self._write("/api/v1/lifecycle/ownership/handoff/complete", entity, phase, incoming)

    def release(self, entity: EntityRef, phase: str, owner: Owner, epoch: int, reason: str = "") -> OwnershipAnswer:
        return self._write("/api/v1/lifecycle/ownership/release", entity, phase, owner, epoch=epoch, reason=reason)

    def terminal(self, entity: EntityRef, phase: str, owner: Owner, epoch: int, reason: str = "") -> OwnershipAnswer:
        return self._write("/api/v1/lifecycle/ownership/terminal", entity, phase, owner, epoch=epoch, reason=reason)

    def _write(self, path: str, entity: EntityRef, phase: str, owner: Owner, **extra: Any) -> OwnershipAnswer:
        payload: dict[str, Any] = {
            "kind": entity.kind,
            "id": entity.id,
            "version": entity.version,
            "phase": phase,
            "owner_type": owner.type,
            "owner_id": owner.id,
        }
        payload.update({k: v for k, v in extra.items() if v not in ("", None)})
        try:
            res = self._request("POST", path, payload)
        except OwnershipUnavailable as exc:
            return OwnershipAnswer(verdict=UNKNOWN, reason=str(exc))
        return _answer(res, owner)


class _HTTPResult:
    __slots__ = ("payload", "status")

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def _error_of(res: _HTTPResult) -> str:
    return str(res.payload.get("error") or f"HTTP {res.status}")


def _verdict_for(own: Ownership, asking: Owner | None) -> str:
    if asking is not None and own.owner == asking and own.healthy:
        return OWNED_BY_ME
    return OWNED_BY_OTHER


def _answer(res: _HTTPResult, asking: Owner | None) -> OwnershipAnswer:
    if res.status == 200:
        own = Ownership.from_payload(res.payload)
        return OwnershipAnswer(verdict=_verdict_for(own, asking), ownership=own)
    if res.status == 404:
        return OwnershipAnswer(verdict=UNOWNED, reason="no record")
    if res.status == 409:
        raw = res.payload.get("ownership")
        own = Ownership.from_payload(raw) if isinstance(raw, dict) else None
        return OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=own, reason=_error_of(res))
    # 412, 503, 5xx, 401/403 — every one of these means "I could not establish
    # ownership", which is UNKNOWN and never UNOWNED. A 412 in particular means
    # the record moved underneath the caller, which is the strongest possible
    # reason not to act on a stale belief.
    return OwnershipAnswer(verdict=UNKNOWN, reason=_error_of(res))
