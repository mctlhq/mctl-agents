"""Mint a ``review``-scoped run token for the reviewer execution and submit.

The one function that holds the plaintext token (``mint_and_submit``). It
keeps it in a local for the length of the submit, never in the environment,
a file, a log line or an exception, and revokes it in ``finally``. Errors
carry the HTTP status and mctl-api's typed ``code`` only.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from orchestrator import review_receipts as rr
from orchestrator import run_token
from orchestrator.review_worker import contract as c
from orchestrator.review_worker.api import ReviewApi
from orchestrator.work_context.snapshots import is_store_execution

MINT_PATH = "/api/v1/agent-run-tokens"
SUBMIT_PATH = "/api/v1/review-receipts"


def _mint(api: ReviewApi, execution_id: str) -> tuple[run_token.RunToken | None, c.Outcome | None]:
    status, payload = api.request(
        "POST", MINT_PATH,
        json={
            "agent": c.AGENT_NAME,
            "scope": c.SCOPE,
            "execution_id": execution_id,
            "ttl_seconds": c.TOKEN_TTL_SECONDS,
            "permissions": [],
        },
    )
    body = payload if isinstance(payload, Mapping) else {}
    token = body.get("token")
    if status != 201:
        return None, c.classify_failure(status, payload)
    if not isinstance(token, str) or not token.startswith("art_"):
        return None, c.unobserved(c.CODE_TOKEN_UNVERIFIED, status)
    minted = run_token.RunToken(
        token=token,
        token_id=str(body.get("token_id") or body.get("id") or ""),
        scope=str(body.get("scope") or ""),
        execution_id=str(body.get("execution_id") or ""),
        expires_at=str(body.get("expires_at") or ""),
    )
    if minted.scope != c.SCOPE or minted.execution_id != execution_id:
        # A token for another scope or execution is never used, only revoked.
        return minted, c.refused(c.CODE_TOKEN_UNVERIFIED, status)
    return minted, None


def _as_submit_answer(item: Mapping[str, Any]) -> dict[str, Any]:
    """One pool entry in the shape of a 201 submit answer, for ``classify``.

    ``GET /api/v1/review-receipts`` (mctl-api ``ListReviewReceipts``) answers
    ``{"receipts": [<stored receipt document>, ...], "count", "complete"}``:
    each entry is the flat sealed receipt, with ``receipt_id``,
    ``request_id``, ``mode``, ``assurance`` and ``content_hash`` at its top
    level. The submit answer instead wraps that same document as
    ``receipt`` next to envelope fields. An entry in the envelope shape is
    read the same way: envelope fields from the entry, the document's own
    fields only where the entry has none, and ``content_hash`` from the
    document."""
    inner = item.get("receipt")
    doc: Mapping[str, Any] = inner if isinstance(inner, Mapping) else {}

    def field(name: str) -> Any:
        value = item.get(name)
        return value if value is not None else doc.get(name)

    content_hash = doc.get("content_hash") if isinstance(inner, Mapping) else item.get("content_hash")
    return {
        "request_id": field("request_id"), "mode": field("mode"), "assurance": field("assurance"),
        "receipt_id": field("receipt_id"), "receipt": {"content_hash": content_hash},
    }


def _reconcile(api: ReviewApi, request_id: str) -> c.Outcome:
    """After an unobserved submit and a consumed nonce: ``stored`` only if the
    pool holds an independent worker receipt for this request. An unreadable
    pool is ``unobserved``, never an empty one."""
    pool = api.receipt_pool(request_id)
    for item in pool or []:
        outcome = c.classify(201, _as_submit_answer(item), request_id=request_id)
        if outcome.stored:
            return outcome
    return c.unobserved(c.CODE_RECEIPT_UNOBSERVED)


#: The pause before the one same-nonce retry. A 429 or a store blip answered
#: again at once almost always repeats itself.
RETRY_DELAY_SECONDS = 2.0


def mint_and_submit(
    api: ReviewApi,
    execution_id: str,
    request: rr.ReviewRequest,
    draft: rr.ReviewReceipt,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> c.Outcome:
    """Submit ``draft`` as the reviewer execution ``execution_id``.

    Raises ``ValueError`` before any HTTP call when ``execution_id`` is not a
    work-item store execution id: a token is only ever minted for an
    execution the service principal attached."""
    if not is_store_execution(execution_id) or len(execution_id) <= len("we_"):
        raise ValueError("a review token needs a store execution id (we_...)")
    body = c.worker_body(request, draft)
    token, problem = _mint(api, execution_id)
    try:
        if problem is not None or token is None:
            return problem or c.unobserved(c.CODE_TOKEN_UNVERIFIED)
        headers = {"Authorization": f"Bearer {token.token}"}
        outcome = c.unobserved("not_submitted")
        for attempt in (1, 2):
            status, payload = api.request("POST", SUBMIT_PATH, json=body, headers=headers)
            outcome = c.classify(status, payload, request_id=request.request_id)
            if outcome.state != c.STATE_UNOBSERVED:
                break
            if attempt == 1:
                sleep(RETRY_DELAY_SECONDS)
        # `outcome` is from the last attempt. A consumed nonce after an
        # unobserved first attempt may be our own receipt: only the pool says.
        if outcome.code == c.CODE_NONCE_CONSUMED and attempt == 2:
            return _reconcile(api, request.request_id)
        return outcome
    finally:
        if token is not None:
            run_token.revoke(token, post=lambda _url, bearer: api.client.post(
                run_token.REVOKE_PATH, headers={"Authorization": f"Bearer {bearer}"}))

