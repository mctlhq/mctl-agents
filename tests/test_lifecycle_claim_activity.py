"""The execution_claim activity's soft-fail contract, exercised over HTTP.

Mirrors tests/test_lifecycle_activity.py's approach for lifecycle_ownership:
drive the real activity through an httpx MockTransport, because the interesting
defects live in the HTTP branches, not the unknown-op early return.
"""
from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest

from orchestrator.lifecycle import rollout
from orchestrator.lifecycle.contract import CLAIM_HELD_BY_ME, CLAIM_UNKNOWN
from orchestrator.temporal.activities import lifecycle as act

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _req(**kw: Any) -> act.ExecutionClaimRequest:
    base = {
        "op": "acquire",
        "kind": "pull-request",
        "entity_id": "mctlhq/mctl-web#42",
        "phase": "review-remediation",
        "owner_epoch": 3,
        "entity_version": "sha-a",
        "executor_type": "shepherd",
        "executor_id": "attempt-1",
        "attempt": "attempt-1",
        "lease_seconds": 1800,
    }
    base.update(kw)
    return act.ExecutionClaimRequest(**base)


def _claim(**kw: Any) -> dict[str, Any]:
    rec = {
        "claim_id": "c1",
        "entity": {"kind": "pull-request", "id": "mctlhq/mctl-web#42", "version": "sha-a"},
        "phase": "review-remediation",
        "owner_epoch": 3,
        "entity_version": "sha-a",
        "executor": {"type": "shepherd", "id": "attempt-1"},
        "attempt": "attempt-1",
        "state": "active",
    }
    rec.update(kw)
    return rec


def _run(monkeypatch: pytest.MonkeyPatch, handler: Any, req: act.ExecutionClaimRequest | None = None):
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)
    monkeypatch.setattr(act, "auth_headers", lambda: {"Authorization": "Bearer test"})

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(**kwargs)

    monkeypatch.setattr(act.httpx, "AsyncClient", _factory)
    return anyio.run(act.execution_claim, req or _req())


def _respond(status: int, payload: Any) -> Any:
    body = b"" if payload is None else json.dumps(payload).encode()
    return lambda request: httpx.Response(status, content=body)


def test_a_2xx_naming_the_caller_is_held_by_me(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _respond(200, _claim()))
    assert result.verdict == CLAIM_HELD_BY_ME
    assert result.may_execute is True
    assert result.claim_id == "c1"


def test_unknown_op_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _respond(200, _claim()), req=_req(op="bogus"))
    assert result.verdict == CLAIM_UNKNOWN


def test_off_mode_skips_the_http_call_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OFF)
    monkeypatch.setattr(act, "auth_headers", lambda: {"Authorization": "Bearer test"})

    def _boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made at rollout=off")

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(_boom)
        return _REAL_ASYNC_CLIENT(**kwargs)

    monkeypatch.setattr(act.httpx, "AsyncClient", _factory)
    result = anyio.run(act.execution_claim, _req())
    assert result.verdict == CLAIM_UNKNOWN
    assert result.may_execute is False


def test_empty_attempt_is_refused_before_any_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("an empty attempt id must never reach the store")

    result = _run(monkeypatch, _boom, req=_req(attempt=""))
    assert result.verdict == CLAIM_UNKNOWN


def test_unreachable_store_is_unknown_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = _run(monkeypatch, _boom)
    assert result.verdict == CLAIM_UNKNOWN
    assert result.may_execute is False


def test_missing_credentials_is_unknown_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rollout.ENV_VAR, rollout.OBSERVE)

    def _no_auth() -> dict[str, str]:
        raise RuntimeError("no token")

    monkeypatch.setattr(act, "auth_headers", _no_auth)
    result = anyio.run(act.execution_claim, _req())
    assert result.verdict == CLAIM_UNKNOWN


def test_a_retaken_409_reaches_the_workflow_as_a_retake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLAIM_HELD_BY_ME arrives identically whether the store GRANTED the
    acquire or REFUSED it with a record naming this attempt, and only the
    second obliges the caller to renew before leaning on the lease. Dropping
    `retaken` from the wire type makes that distinction unrepresentable for
    the Temporal transport — the omission `OwnershipResult.accepted` already
    shipped once (claude P3 on `0af3b38`)."""
    result = _run(monkeypatch, _respond(409, {"code": "claim-held", "claim": _claim()}))
    assert result.verdict == CLAIM_HELD_BY_ME
    assert result.retaken is True


def test_a_granted_acquire_is_not_marked_retaken_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _run(monkeypatch, _respond(200, _claim()))
    assert result.retaken is False


def test_a_recordless_2xx_renew_reaches_the_workflow_as_a_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The activity shares one classifier with the sync client, so the renew
    body tolerance must not be a client-only property."""
    result = _run(monkeypatch, _respond(204, None), req=_req(op="renew", claim_id="c1"))
    assert result.verdict == CLAIM_HELD_BY_ME
    assert result.claim_id == ""
    assert result.accepted is True


def test_a_non_json_2xx_renew_is_unknown_not_a_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activity is the transport that CAN reach the contract with an
    unparseable body — `ClaimClient._request` raises before the contract sees
    one — so the gateway case belongs here: an HTML error page served with a
    200 arrives as an empty mapping with a non-empty raw body, and must not be
    read as a renewed hold (claude P2/P3 on `b362b5e`)."""

    def _html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>502 Bad Gateway</html>")

    result = _run(monkeypatch, _html, req=_req(op="renew", claim_id="c1"))
    assert result.verdict == CLAIM_UNKNOWN


def test_a_recordless_renew_is_marked_recordless_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claim_id`, `state` and `lease_until` all come back empty, which from
    the fields alone is indistinguishable from a record that arrived empty."""
    result = _run(monkeypatch, _respond(204, None), req=_req(op="renew", claim_id="c1"))
    assert result.has_record is False
    granted = _run(monkeypatch, _respond(200, _claim()))
    assert granted.has_record is True
