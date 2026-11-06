"""mctl-api calls of the review-worker, all under the service credential.

``temporal.mctl_client.auth_headers()`` is the static service principal, so
every call here is made by the service acting directly: no relay header and
never ``run_token.mctl_api_bearer`` (an attach made through a run token is
not ``attached_direct_service``, and mctl-api refuses such a reviewer). The
only calls that carry another bearer are the submit and the revoke, in
``submitter``.

Every method either returns what it was asked for or raises
``contract.Stop`` carrying a typed ``Outcome``. Nothing here retries.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from orchestrator import execution_provenance as ep
from orchestrator import policy_checkpoint
from orchestrator import review_receipts as rr
from orchestrator.review_worker import contract as c
from orchestrator.temporal import mctl_client
from orchestrator.work_context import creation
from orchestrator.work_context import executions as wx

REQUEST_TIMEOUT_SECONDS = 15.0
PHASE_RUNNING = wx.PHASE_RUNNING


@dataclass(frozen=True)
class AuthoringFacts:
    """The authoring execution's server-recorded facts."""

    attempt_ids: tuple[str, ...]
    lineage_root: str


def _q(value: str) -> str:
    return quote(str(value), safe="")


def _record(payload: Any) -> Mapping[str, Any]:
    """The provenance record, flat or under ``provenance``."""
    if isinstance(payload, Mapping):
        inner = payload.get("provenance")
        return inner if isinstance(inner, Mapping) else payload
    return {}


class ReviewApi:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        service_headers: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._headers = service_headers or mctl_client.auth_headers
        self._client = httpx.Client(
            base_url=(base_url or mctl_client.MCTL_API_BASE_URL).rstrip("/"),
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
            follow_redirects=False,
        )

    @property
    def client(self) -> httpx.Client:
        return self._client

    def close(self) -> None:
        self._client.close()

    def service_headers(self) -> dict[str, str]:
        try:
            return dict(self._headers())
        except RuntimeError as exc:
            # No service credential: nothing was sent. Not a statement about the review.
            raise c.Stop(c.refused("service_credential_missing")) from exc

    def request(
        self, method: str, path: str, *, json: Any = None, headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        """``(status, payload)``; status 0 is a transport error. The payload
        is whatever the body parsed to, never an exception text."""
        try:
            response = self._client.request(
                method, path, json=json, params=params,
                headers=dict(headers) if headers is not None else self.service_headers(),
            )
        except httpx.HTTPError:
            return 0, {}
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    # -- authoring facts ----------------------------------------------------
    def _provenance(self, execution_id: str) -> tuple[int, Mapping[str, Any]]:
        status, payload = self.request("GET", f"/api/v1/executions/{_q(execution_id)}/provenance")
        return status, _record(payload)

    def authoring_facts(self, execution_id: str) -> AuthoringFacts:
        """The attempt ids and lineage root of the authoring execution, read
        from mctl-api, never from the caller. An empty id, an ``ex-`` runtime
        id (mctl-api holds no provenance for it) or any read failure is
        ``review_authoring_unresolved``."""
        unresolved = c.Stop(c.refused(c.CODE_AUTHORING_UNRESOLVED))
        if not isinstance(execution_id, str) or not execution_id.startswith("we_"):
            raise unresolved
        try:
            # The Tier A execution-id grammar is the single source of truth.
            rr.ReviewRequest(
                subject=rr.ReviewSubject(kind=rr.SUBJECT_PROPOSAL), rubric=rr.RUBRIC_PROPOSAL, mode="worker",
                author_actors=(), nonce="n", authoring_execution_id=execution_id,
            ).validate()
        except rr.ReviewError as exc:
            raise unresolved from exc
        status, record = self._provenance(execution_id)
        attempt, lineage = record.get("attempt_id"), record.get("lineage_root")
        if (
            status != 200
            or not isinstance(attempt, str) or not ep.ATTEMPT_ID_RE.fullmatch(attempt)
            or not isinstance(lineage, str) or not ep.LINEAGE_ROOT_RE.fullmatch(lineage)
        ):
            raise unresolved
        return AuthoringFacts((attempt,), lineage)

    # -- the request ---------------------------------------------------------
    def open_request(self, request: rr.ReviewRequest) -> None:
        """``POST /api/v1/review-requests``. The server's ``request_id`` must
        be the locally sealed one."""
        body = request.to_dict()
        body.pop("created_at", None)
        status, payload = self.request("POST", "/api/v1/review-requests", json=body)
        if status in (200, 201):
            inner = payload.get("request") if isinstance(payload, Mapping) else None
            got = (payload.get("request_id") if isinstance(payload, Mapping) else None) or (
                inner.get("request_id") if isinstance(inner, Mapping) else None
            )
            if got == request.request_id:
                return
            if isinstance(got, str):
                raise c.Stop(c.refused(c.CODE_REQUEST_CONFLICT, status))
            raise c.Stop(c.unobserved(c.CODE_RESPONSE_UNVERIFIED, status))
        raise c.Stop(c.classify_failure(status, payload))

    # -- the reviewer's own work item and execution --------------------------
    def tenant_of(self, work_item_id: str) -> str:
        status, payload = self.request("GET", f"/api/v1/work-items/{_q(work_item_id)}")
        item = payload.get("work_item") if isinstance(payload, Mapping) else None
        tenant = item.get("tenant") if isinstance(item, Mapping) else None
        if status == 200 and isinstance(tenant, str) and tenant:
            return tenant
        if status == 200:
            raise c.Stop(c.unobserved(c.CODE_RESPONSE_UNVERIFIED, status))
        raise c.Stop(c.classify_failure(status, payload))

    @staticmethod
    def _governed(operation: str, target: str, body: Mapping[str, Any]) -> bool:
        decision = policy_checkpoint.checkpoint(
            policy_checkpoint.MCTL_WORK_ITEM_WRITE, operation, target, dict(body),
        )
        return bool(decision.permitted)

    def reviewer_work_item(self, request: rr.ReviewRequest, tenant: str) -> str:
        """The reviewer's own work item, ``review:<request_id>`` in the
        authoring item's tenant. A replay of the same key is this request's
        item; ``external_key_in_use`` for a foreign item is a refusal."""
        key = f"review:{request.request_id}"
        body = {
            "tenant": tenant,
            "title": f"Independent review {request.request_id}",
            "external_key": key,
            "idempotency_key": f"review-worker:{request.request_id}",
            "visibility": "tenant",
            "origin_surface": "api",
        }
        if not self._governed(creation.CREATE_OPERATION, key, body):
            raise c.Stop(c.refused("policy_denied"))
        status, payload = self.request("POST", "/api/v1/work-items", json=body)
        answer = creation.answer_from_create(
            status, payload if isinstance(payload, dict) else {}, tenant=tenant, external_key=key,
        )
        if answer.usable:
            return answer.work_item_id
        if answer.verdict == creation.REFUSED:
            raise c.Stop(c.refused(answer.code or "work_item_refused", status))
        raise c.Stop(c.classify_failure(status, payload) if status else c.unobserved("work_item_unobserved"))

    def attach_reviewer(self, work_item_id: str, run: wx.EngineRun) -> str:
        """Attach a NEW execution for the reviewer run to ``work_item_id`` and
        return its id. Only ``EXECUTION_ATTACHED`` is usable: an existing
        execution is never reused.

        mctl-api keys an execution on ``(work_item_id, engine, engine_ref)``
        and every run opens a fresh request (a fresh nonce, so a fresh
        ``request_id`` and its own ``review:<request_id>`` item). An Argo
        retry of the same Workflow therefore attaches to a new item and gets
        ``EXECUTION_ATTACHED``; an existing execution on this item was put
        there by someone else, and is refused as the design says."""
        body = wx.attach_body(run, PHASE_RUNNING)
        if not self._governed(wx.ATTACH_EXECUTION_OPERATION, work_item_id, body):
            raise c.Stop(c.refused("policy_denied"))
        status, payload = self.request("POST", f"/api/v1/work-items/{_q(work_item_id)}/executions", json=body)
        answer = wx.answer_from_attach(
            status, payload if isinstance(payload, dict) else {}, work_item_id=work_item_id, run=run,
            phase=PHASE_RUNNING,
        )
        if answer.verdict == wx.EXECUTION_EXISTING:
            raise c.Stop(c.refused(c.CODE_NOT_INDEPENDENT, status))
        if answer.verdict == wx.EXECUTION_ACTIVE:
            raise c.Stop(c.refused(wx.EXECUTION_ACTIVE_CODE, status))
        if answer.verdict == wx.EXECUTION_REFUSED:
            raise c.Stop(c.refused(c.code_of(payload) or "execution_attach_refused", status))
        if answer.verdict != wx.EXECUTION_ATTACHED:
            raise c.Stop(c.unobserved("execution_attach_unobserved", status))
        return answer.execution_id

    def reviewer_provenance(self, execution_id: str) -> c.Provenance:
        """What mctl-api recorded for the reviewer execution just attached."""
        pstatus, record = self._provenance(execution_id)
        if pstatus == 404:
            raise c.Stop(c.refused(c.CODE_EXECUTION_UNPROVEN, pstatus))
        if pstatus != 200:
            raise c.Stop(c.classify_failure(pstatus, record))
        attempt, lineage = record.get("attempt_id"), record.get("lineage_root")
        return c.Provenance(
            execution_id=execution_id,
            attempt_id=attempt if isinstance(attempt, str) else "",
            lineage_root=lineage if isinstance(lineage, str) else "",
            attached_direct_service=record.get("attached_direct_service") is True,
        )

    def finish_reviewer(self, work_item_id: str, run: wx.EngineRun, phase: str) -> str:
        """Advance the reviewer execution to the terminal ``phase``. Best
        effort: returns "" when the store confirmed it, else why not. Never
        raises, and never changes the run's outcome."""
        body = wx.attach_body(run, phase)
        try:
            if not self._governed(wx.ATTACH_EXECUTION_OPERATION, work_item_id, body):
                return "policy_denied"
            status, payload = self.request(
                "POST", f"/api/v1/work-items/{_q(work_item_id)}/executions", json=body,
            )
        except c.Stop as stop:  # no service credential
            return stop.outcome.label
        except Exception as exc:  # noqa: BLE001 - best effort, never the run's outcome
            return type(exc).__name__
        answer = wx.answer_from_attach(
            status, payload if isinstance(payload, dict) else {}, work_item_id=work_item_id, run=run, phase=phase,
        )
        return "" if answer.usable else (c.code_of(payload) or answer.verdict)

    # -- reconciliation ------------------------------------------------------
    def receipt_pool(self, request_id: str) -> list[Mapping[str, Any]] | None:
        """The receipts mctl-api holds for ``request_id``, or None when the
        pool could not be read (an unknown, never an empty pool)."""
        status, payload = self.request("GET", "/api/v1/review-receipts", params={"request_id": request_id})
        if status != 200:
            return None
        items = payload if isinstance(payload, list) else (
            payload.get("receipts", payload.get("items")) if isinstance(payload, Mapping) else None
        )
        if not isinstance(items, list):
            return None
        return [i for i in items if isinstance(i, Mapping)]


def reviewer_engine_run(workflow_name: str) -> wx.EngineRun:
    """The reviewer's own engine run. The part before ``#`` is the review
    Workflow's own name, so its attempt id differs from every authoring one."""
    return wx.EngineRun(engine=wx.ENGINE_ARGO, engine_ref=f"{workflow_name}#review")
