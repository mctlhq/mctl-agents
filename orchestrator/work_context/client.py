"""HTTP client for the mctl-api `WorkItem` surface (mctl-api#227).

Synchronous `urllib`, structurally copied from
`orchestrator/lifecycle/client.py`'s `OwnershipClient`: same `MCTL_TOKEN`
bearer auth, same https-only refusal, same no-redirect opener, same
"uncertainty is a value, never an exception" contract. There is exactly one
caller shape today (an ordinary CLI process — `run_issue_investigator.py`
inside an Argo pod), so unlike `lifecycle/client.py` this module defines no
async counterpart; one can be added the same way `activities/lifecycle.py`
was, if a Temporal activity ever needs one.

Route strings live in one module-level table (`ROUTES`) so a rename is a
one-line change and every test monkeypatches the transport rather than a URL
(see the "Exact mctl-api route shape" open question in requirements.md).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

from orchestrator.work_context.contract import (
    WORK_ITEM_UNKNOWN,
    ExecutionRef,
    WorkItemAnswer,
    answer_from,
)

DEFAULT_TIMEOUT_S = 10

# Written by analogy with orchestrator/lifecycle/client.py's
# `/api/v1/lifecycle/ownership/...` table — mctl-api#227 is not readable from
# this clone (open question in requirements.md).
ROUTES = {
    "get_work_item": "/api/v1/work-items/{id}",
    "list_executions": "/api/v1/work-items/{id}/executions",
    "record_execution": "/api/v1/work-items/{id}/executions",
}


def api_base() -> str:
    return os.environ.get("MCTL_API_BASE_URL", "https://api.mctl.ai").rstrip("/")


class WorkItemUnavailable(RuntimeError):
    """The store could not be reached or is not configured.

    Raised only by the low-level transport. Every public method converts it
    into a `WORK_ITEM_UNKNOWN` answer, mirroring
    `orchestrator/lifecycle/client.py`'s `OwnershipUnavailable`.
    """


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """Opener that surfaces a 3xx as an HTTPError instead of following it —
    mirrors `orchestrator/lifecycle/client.py`'s `_no_redirect_opener`.
    Following a redirect would let a misconfigured MCTL_API_BASE_URL
    silently answer work-item questions from somewhere else."""

    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirects)


def _q(value: str) -> str:
    return quote(str(value), safe="")


class _HTTPResult:
    __slots__ = ("body_empty", "payload", "status", "status_path")

    def __init__(self, status: int, payload: Any, status_path: str = "", body_empty: bool = False) -> None:
        self.status = status
        self.body_empty = body_empty
        self.payload = payload if isinstance(payload, dict) else {}
        self.status_path = status_path


class WorkItemClient:
    """Synchronous client. Safe in Argo pods and CLI entry points."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        self._base = (base_url or api_base()).rstrip("/")
        self._token = token if token is not None else os.environ.get("MCTL_TOKEN", "").strip()
        self._timeout = timeout
        self._opener = _no_redirect_opener()

    # -- transport ------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> _HTTPResult:
        if not self._token:
            raise WorkItemUnavailable("MCTL_TOKEN is not set")
        url = f"{self._base}{path}"
        if not url.startswith("https://"):
            # Refuse non-https rather than sending a bearer token in the
            # clear (also satisfies ruff S310), mirroring
            # orchestrator/lifecycle/client.py.
            raise WorkItemUnavailable(f"refusing non-https MCTL_API_BASE_URL: {self._base}")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — scheme checked above
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                body = resp.read()
                status = resp.getcode() or 200
        except urllib.error.HTTPError as exc:
            try:
                parsed_any = json.loads(exc.read() or b"{}")
            except Exception:  # noqa: BLE001 — a failed error-body read is still just an error
                parsed_any = {}
            return _HTTPResult(status=exc.code, payload=parsed_any, status_path=path)
        except Exception as exc:
            raise WorkItemUnavailable(str(exc)) from exc
        try:
            return _HTTPResult(
                status=status, payload=json.loads(body or b"{}"), status_path=path, body_empty=not body,
            )
        except ValueError as exc:
            raise WorkItemUnavailable(f"malformed response: {exc}") from exc

    # -- reads ------------------------------------------------------------

    def get(self, work_item_id: str) -> WorkItemAnswer:
        try:
            res = self._request("GET", ROUTES["get_work_item"].format(id=_q(work_item_id)))
        except WorkItemUnavailable as exc:
            return WorkItemAnswer(verdict=WORK_ITEM_UNKNOWN, reason=str(exc))
        return answer_from(res.status, res.payload, path=res.status_path, body_empty=res.body_empty)

    def list_executions(self, work_item_id: str) -> WorkItemAnswer:
        try:
            res = self._request("GET", ROUTES["list_executions"].format(id=_q(work_item_id)))
        except WorkItemUnavailable as exc:
            return WorkItemAnswer(verdict=WORK_ITEM_UNKNOWN, reason=str(exc))
        return answer_from(res.status, res.payload, path=res.status_path, body_empty=res.body_empty)

    # -- writes -------------------------------------------------------------

    def record_execution(self, work_item_id: str, execution: ExecutionRef) -> WorkItemAnswer:
        payload = {
            "execution_id": execution.execution_id,
            "sequence": execution.sequence,
            "temporal_workflow_id": execution.temporal_workflow_id,
            "started_at": execution.started_at,
            "surface": {"kind": execution.surface.kind, "surface_id": execution.surface.surface_id,
                        "thread_ref": execution.surface.thread_ref},
            "actor": {"kind": execution.actor.kind, "actor_id": execution.actor.actor_id},
        }
        try:
            res = self._request("POST", ROUTES["record_execution"].format(id=_q(work_item_id)), payload)
        except WorkItemUnavailable as exc:
            return WorkItemAnswer(verdict=WORK_ITEM_UNKNOWN, reason=str(exc))
        return answer_from(res.status, res.payload, path=res.status_path, body_empty=res.body_empty)
