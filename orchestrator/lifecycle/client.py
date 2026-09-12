"""HTTP client for the mctl-api lifecycle ownership surface.

Two transports, because there are two callers with different constraints:

- ``OwnershipClient`` is synchronous urllib, for ``run_shepherd`` and the
  implementer, which are ordinary CLI processes inside an Argo pod. It reuses
  the same https pin and no-redirect opener ``run_shepherd`` already applies
  to its dev-loop probe.
The Temporal side does NOT use this module. Activities talk to the same
endpoints over httpx in ``orchestrator/temporal/activities/lifecycle.py``,
because an activity is async and this client is not — and workflow code must
never call either one: network I/O inside ``@workflow.defn`` breaks determinism
and replay, which is the property this whole contract depends on (ADR-010 §9).

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
    STATE_ACTIVE,
    STATE_HANDING_OFF,
    STATE_RELEASED,
    STATE_TERMINAL,
    UNKNOWN,
    UNOWNED,
    WROTE_NO_RECORD,
    EntityRef,
    Owner,
    Ownership,
    OwnershipAnswer,
)

DEFAULT_TIMEOUT_S = 10

# Ids per batch request.
#
# The URL carries one `id=` parameter per entity, so an unbounded sweep builds
# an unbounded query string and eventually earns a 414 or 431 — at which point
# EVERY id in that sweep turns UNKNOWN and the whole pass halts, in a way
# indistinguishable from the store being down. Chunking keeps one oversized
# sweep from looking like an outage. 100 ids is roughly 4 KB of query string
# against the server's own 500-id cap.
BATCH_CHUNK_SIZE = 100

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
                # The real code, not a hardcoded 200: a 204 on a mutating call
                # would otherwise parse as an empty body and be reported as an
                # unrecognised 200.
                status = resp.getcode() or 200
        except urllib.error.HTTPError as exc:
            # exc.read() is a SECOND network read, on a connection that has
            # already produced an error status. It can time out or reset, and
            # that exception would escape this handler entirely — the adjacent
            # `except Exception` is a sibling, not a wrapper. A client whose
            # contract is "uncertainty is a value" must not raise here.
            try:
                parsed_any = json.loads(exc.read() or b"{}")
            except Exception:  # noqa: BLE001 — a failed error-body read is still just an error
                parsed_any = {}
            return _HTTPResult(status=exc.code, payload=parsed_any, status_path=path)
        except Exception as exc:
            raise OwnershipUnavailable(str(exc)) from exc
        try:
            return _HTTPResult(status=status, payload=json.loads(body or b"{}"), status_path=path)
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
        return _answer(res, asking, is_read=True)

    def get_many(
        self, kind: str, phase: str, ids: list[str], asking: Owner | None = None
    ) -> dict[str, OwnershipAnswer]:
        """The whole sweep in as few round trips as the URL allows.

        The per-proposal fan-out this replaces needed a thread pool and a
        60-second wall-clock budget, and anything the budget did not answer was
        swept anyway — an unanswered probe read as "unowned". A single batched
        read removes the pool and that failure mode together.

        On failure EVERY id comes back UNKNOWN rather than absent, so a caller
        iterating the result cannot mistake a store outage for a clean sweep.
        """
        if not ids:
            return {}
        out: dict[str, OwnershipAnswer] = {}
        for chunk in _chunks(ids, BATCH_CHUNK_SIZE):
            out.update(self._get_chunk(kind, phase, chunk, asking))
        return out

    def _get_chunk(
        self, kind: str, phase: str, ids: list[str], asking: Owner | None
    ) -> dict[str, OwnershipAnswer]:
        query = f"/api/v1/lifecycle/ownership/batch?kind={_q(kind)}&phase={_q(phase)}"
        query += "".join(f"&id={_q(i)}" for i in ids)
        try:
            res = self._request("GET", query)
        except OwnershipUnavailable as exc:
            return {i: OwnershipAnswer(verdict=UNKNOWN, reason=str(exc)) for i in ids}
        if not (200 <= res.status < 300):
            reason = _error_of(res)
            return {i: OwnershipAnswer(verdict=UNKNOWN, reason=reason) for i in ids}
        raw_found = res.payload.get("ownership")
        if not isinstance(raw_found, dict):
            # A 200 without the envelope this endpoint documents is a surprise.
            # Reading it as "no records" would report every id UNOWNED, which
            # is the one wrong answer that licenses action.
            return {
                i: OwnershipAnswer(verdict=UNKNOWN, reason="unrecognised batch payload")
                for i in ids
            }
        out: dict[str, OwnershipAnswer] = {}
        for i in ids:
            raw = raw_found.get(i)
            if raw is None:
                out[i] = OwnershipAnswer(verdict=UNOWNED)
                continue
            own = Ownership.from_payload(raw)
            if own is None:
                out[i] = OwnershipAnswer(verdict=UNKNOWN, reason="unrecognised record")
                continue
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

    def progress(
        self, entity: EntityRef, phase: str, owner: Owner, epoch: int, evidence: str
    ) -> OwnershipAnswer:
        """Record that something was EFFECTED.

        Callers must not call this for a poll that observed nothing. Liveness
        is refreshed by any tick through ``acquire``; this field is the one that
        says the work moved, and a heartbeat writing it would let an owner prove
        usefulness forever while achieving nothing.
        """
        if not evidence:
            # _write filters empty strings out of the payload, so an empty
            # evidence would be dropped and the server would answer 400 with a
            # message the caller never sees. Fail here, where it is legible.
            raise ValueError("progress requires evidence: say what changed")
        return self._write(
            "/api/v1/lifecycle/ownership/progress", entity, phase, owner,
            epoch=epoch, evidence=evidence,
        )

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
    __slots__ = ("payload", "status", "status_path")

    def __init__(self, status: int, payload: Any, status_path: str = "") -> None:
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {}
        # The path is carried so a 404 can say WHICH endpoint produced it — the
        # difference between "no such row" and "no such route".
        self.status_path = status_path


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def _error_of(res: _HTTPResult) -> str:
    return str(res.payload.get("error") or f"HTTP {res.status}")


# The two states in which the record still holds the entity, and the two in
# which it has let go. Both lists are CLOSED, and a state in neither is
# deliberately not classified — see _verdict_for.
_HOLDING_STATES = frozenset({STATE_ACTIVE, STATE_HANDING_OFF})
_FREE_STATES = frozenset({STATE_RELEASED, STATE_TERMINAL})


def _verdict_for(own: Ownership, asking: Owner | None) -> str:
    """Turn a record into an answer.

    ``state`` is load-bearing, and this function has had it wrong twice in
    opposite directions.

    First it ignored state entirely. A released or terminal row still names an
    owner, so that answered OWNED_BY_OTHER for an entity explicitly handed
    back — and since UNKNOWN and OWNED_BY_OTHER both set ``blocks_others``, the
    next actor stood down forever on a PR nobody owned.

    The fix classified anything *outside* the holding set as free, which fails
    the other way: a holding state added server-side and unknown to this image
    — this client is deployed in a container image that lags mctl-api by a
    release — would read as UNOWNED, and a second actor would act alongside the
    true owner. That is worse than the bug it replaced: the first mistake made
    the system too timid, this one makes it act.

    So both sets are closed and a state in neither is UNKNOWN. Uncertainty
    resolves toward "do not act", which is the same rule the verdict itself
    encodes — an unrecognised state is exactly as much of an unknown as an
    unreachable store.
    """
    if own.state in _FREE_STATES:
        return UNOWNED
    if own.state not in _HOLDING_STATES:
        return UNKNOWN
    if asking is not None and own.owner == asking and own.healthy:
        return OWNED_BY_ME
    return OWNED_BY_OTHER


def _answer(res: _HTTPResult, asking: Owner | None, *, is_read: bool = False) -> OwnershipAnswer:
    if 200 <= res.status < 300:
        # A 200 whose body is not an ownership record is a surprise, not an
        # answer. Parsing it into an all-empty record would produce a confident
        # OWNED_BY_OTHER with no reason — a wrong answer stated as firmly as a
        # right one.
        own = Ownership.from_payload(res.payload)
        if own is None:
            if not is_read and not res.payload:
                # A body-less 2xx on a mutating call means the write SUCCEEDED
                # and told us nothing more. Reporting UNKNOWN would make it
                # indistinguishable from a 503, and a caller gating its local
                # state on the result could never record a successful release.
                return OwnershipAnswer(
                    verdict=WROTE_NO_RECORD, reason=f"{res.status} with no body"
                )
            return OwnershipAnswer(
                verdict=UNKNOWN, reason=f"no ownership record in a {res.status} response"
            )
        verdict = _verdict_for(own, asking)
        reason = "" if verdict != UNKNOWN else f"unrecognised ownership state {own.state!r}"
        return OwnershipAnswer(verdict=verdict, ownership=own, reason=reason)
    if res.status == 404 and is_read:
        return OwnershipAnswer(verdict=UNOWNED, reason="no record")
    if res.status == 404:
        # On a WRITE a 404 is not "no such row". POST /acquire has no
        # not-found semantics, so a 404 there is a missing route, a wrong base
        # path, or an ingress answering for something else — and answering
        # UNOWNED would set blocks_others False for EVERY entity asked. That is
        # fail-open, in the one place in this module that can produce it.
        return OwnershipAnswer(verdict=UNKNOWN, reason=f"404 from {res.status_path or 'a write'}")
    if res.status == 409:
        raw = res.payload.get("ownership")
        own = Ownership.from_payload(raw) if isinstance(raw, dict) else None
        return OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=own, reason=_error_of(res))
    # 412, 503, 5xx, 401/403 — every one of these means "I could not establish
    # ownership", which is UNKNOWN and never UNOWNED. A 412 in particular means
    # the record moved underneath the caller, which is the strongest possible
    # reason not to act on a stale belief.
    return OwnershipAnswer(verdict=UNKNOWN, reason=_error_of(res))
