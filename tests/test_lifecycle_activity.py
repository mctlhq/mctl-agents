"""The lifecycle activity's soft-fail contract, exercised over HTTP.

Every path must return an `unknown` verdict rather than raise. An activity that
hard-fails takes its retry budget and then the workflow step with it, and this
one is called from a loop that has already produced a PR — trading a bookkeeping
outage for a delivery outage is strictly worse than not knowing who owns
something.

These drive the real activity through an httpx MockTransport rather than
calling its helpers, because an earlier version of this file only ever reached
the unknown-op early return: the HTTP branches, which are where every finding
so far has been, went untested.
"""
from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest

from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    UNKNOWN,
    UNOWNED,
)
from orchestrator.temporal.activities import lifecycle as act


def _req(**kw: Any) -> act.OwnershipRequest:
    base = {
        "op": "acquire",
        "kind": "pull-request",
        "entity_id": "mctlhq/mctl-web#42",
        "phase": "review-remediation",
        "owner_type": "devloop-workflow",
        "owner_id": "dev-loop-mctlhq-mctl-web-7",
    }
    base.update(kw)
    return act.OwnershipRequest(**base)


def _record(**kw: Any) -> dict[str, Any]:
    rec = {
        "phase": "review-remediation",
        "owner": {"type": "devloop-workflow", "id": "dev-loop-mctlhq-mctl-web-7"},
        "epoch": 3,
        "state": "active",
        "healthy": True,
    }
    rec.update(kw)
    return rec


# Captured ONCE, at import. `act.httpx` is the same module object as the
# `httpx` imported here, so patching act.httpx.AsyncClient patches it globally
# — and a second _run in the same test that read the attribute back would wrap
# the first fake instead of the real class, quietly serving the previous test
# case's response. That cost one debugging round.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _run(monkeypatch: pytest.MonkeyPatch, handler: Any, req: act.OwnershipRequest | None = None):
    """Drive the activity with a faked transport."""
    monkeypatch.setattr(act, "auth_headers", lambda: {"Authorization": "Bearer test"})

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(**kwargs)

    monkeypatch.setattr(act.httpx, "AsyncClient", _factory)
    return anyio.run(act.lifecycle_ownership, req or _req())


def _respond(status: int, payload: Any) -> Any:
    body = b"" if payload is None else json.dumps(payload).encode()
    return lambda request: httpx.Response(status, content=body)


def test_my_own_healthy_record_is_owned_by_me(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _respond(200, _record()))
    assert result.verdict == OWNED_BY_ME
    assert result.owned_by_caller is True
    assert result.epoch == 3


def test_a_201_is_also_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call here is a mutation, and the sibling client accepts the whole
    2xx range. An activity that recognised only an exact 200 would make the
    writer half silently no-op against a server that answered 201."""
    result = _run(monkeypatch, _respond(201, _record()))
    assert result.verdict == OWNED_BY_ME


def test_released_record_is_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _respond(200, _record(state="released", healthy=False)))
    assert result.verdict == UNOWNED


def test_unrecognised_state_is_unknown_not_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drift this module had from the client: an unrecognised state fell
    into the free bucket after the client's copy had been fixed to fail closed.
    Sharing contract.answer_from is what makes that impossible now."""
    result = _run(monkeypatch, _respond(200, _record(state="quarantined")))
    assert result.verdict == UNKNOWN
    assert result.owned_by_caller is False


def test_unrecognised_200_body_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _respond(200, {"unexpected": "envelope"}))
    assert result.verdict == UNKNOWN
    assert result.owned_by_caller is False


def test_non_json_200_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML error page from a gateway still carries a 200."""
    handler = lambda request: httpx.Response(200, content=b"<html>nope</html>")  # noqa: E731
    result = _run(monkeypatch, handler)
    assert result.verdict == UNKNOWN


def test_409_reports_the_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"error": "owned", "ownership": _record(owner={"type": "pr-steward", "id": "steward"})}
    result = _run(monkeypatch, _respond(409, payload))
    assert result.verdict == OWNED_BY_OTHER
    assert result.owner_id == "steward"
    assert result.owned_by_caller is False


def test_404_on_a_write_is_unknown_not_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call this activity makes is a POST, so a 404 is a missing route or
    a wrong base path — never "no such row"."""
    result = _run(monkeypatch, _respond(404, {"error": "not found"}))
    assert result.verdict == UNKNOWN


def test_503_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(monkeypatch, _respond(503, {"error": "store not configured"}))
    assert result.verdict == UNKNOWN


def test_transport_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = _run(monkeypatch, _boom)
    assert result.verdict == UNKNOWN
    assert "refused" in result.reason


def test_missing_token_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_auth() -> dict[str, str]:
        raise RuntimeError("MCTL_TOKEN is not set")

    monkeypatch.setattr(act, "auth_headers", _no_auth)
    result = anyio.run(act.lifecycle_ownership, _req())
    assert result.verdict == UNKNOWN
    assert "MCTL_TOKEN" in result.reason


def test_unknown_op_never_reaches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a typo'd op must not be sent anywhere")

    result = _run(monkeypatch, _explode, _req(op="acqiure"))
    assert result.verdict == UNKNOWN
    assert "acqiure" in result.reason


def test_result_defaults_are_conservative() -> None:
    """A result deserialized from a history recorded before a field existed, or
    built from a failed call, must default to the direction that does not act."""
    empty = act.OwnershipResult()
    assert empty.verdict == UNKNOWN
    assert empty.owned_by_caller is False


def test_accepted_is_carried_from_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call this activity makes is a mutation, so `accepted` is what the
    workflow reads to know the write landed.

    Dropping it from _result_from keeps the whole suite green otherwise — the
    workflow fakes bypass this function entirely — and a 204 terminal would
    then keep the claim and release a merged PR.
    """
    # A 2xx carrying the record: accepted, and a claim.
    owned = _run(monkeypatch, _respond(200, _record()))
    assert owned.accepted is True
    assert owned.owned_by_caller is True

    # A body-less 2xx: accepted, but nothing learned about ownership.
    empty = _run(monkeypatch, _respond(204, None))
    assert empty.accepted is True
    assert empty.owned_by_caller is False

    # A failure is not accepted, whatever else it says.
    for status in (409, 503, 404):
        failed = _run(monkeypatch, _respond(status, {"error": "no"}))
        assert failed.accepted is False, status


def test_acquire_sends_no_epoch_and_the_other_ops_do(monkeypatch: pytest.MonkeyPatch) -> None:
    """The last place the two transports built the same request differently.

    `LifecycleClient.acquire` takes no epoch at all: the epoch is a fencing
    precondition on a write against an EXISTING claim, and an acquire asserting
    one asks the server to refuse unless the caller's belief about the
    generation still holds.

    That is wrong on the path the workflow now recovers by. An UNOWNED progress
    result falls through to the heartbeat acquire, which re-establishes the
    claim; asserting the epoch the reconciler already superseded turns it into
    a 412, so no re-acquire happens — and since `_owned_entity_id` is not
    cleared on UNOWNED the loop stays on the claimed path, where the heartbeat
    acquire is the only liveness write, and stops refreshing `last_seen_at` for
    the rest of the watch.

    `answer_from` normalises the RESPONSE and cannot see a divergence in the
    REQUEST, so this is the one axis the shared contract does not protect and
    the only one that needs a test comparing bodies.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = json.loads(request.content)
        return httpx.Response(200, content=json.dumps(_record()).encode())

    _run(monkeypatch, handler, _req(op="acquire", epoch=7))
    assert "epoch" not in seen["/api/v1/lifecycle/ownership/acquire"]

    # The other direction: every op for which the epoch IS the precondition
    # still sends it, or the fence stops fencing.
    for op, path in (
        ("progress", "/api/v1/lifecycle/ownership/progress"),
        ("release", "/api/v1/lifecycle/ownership/release"),
        ("terminal", "/api/v1/lifecycle/ownership/terminal"),
        ("handoff-start", "/api/v1/lifecycle/ownership/handoff/start"),
    ):
        _run(monkeypatch, handler, _req(op=op, epoch=7, evidence="x", to_owner_type="shepherd", to_owner_id="cron"))
        assert seen[path].get("epoch") == 7, op


def test_epoch_zero_and_an_empty_version_are_sent_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two more request-body divergences on the same axis as the epoch above.

    `LifecycleClient._write` filters with `v not in ("", None)`, so it sends
    `"epoch": 0` — an explicit "I hold no generation yet" — and it puts
    `version` in the base dict where no filter can reach it. This module
    filtered both on truthiness, so a zero epoch became a MISSING key, and the
    release and terminal the workflow issues from its `finally` (where no head
    SHA is carried) sent no `version` at all while the sync transport sent an
    empty one. `answer_from` normalises the RESPONSE and cannot see either.
    """
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = json.loads(request.content)
        return httpx.Response(200, content=json.dumps(_record()).encode())

    _run(monkeypatch, handler, _req(op="progress", epoch=0, evidence="x", version="abc"))
    body = seen["/api/v1/lifecycle/ownership/progress"]
    assert body.get("epoch") == 0, body

    _run(monkeypatch, handler, _req(op="release", epoch=4, version=""))
    body = seen["/api/v1/lifecycle/ownership/release"]
    assert "version" in body and body["version"] == "", body


def test_progress_without_evidence_is_answered_here_not_by_a_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard `LifecycleClient.progress` raises on, in the form this
    transport is allowed to take.

    Empty evidence is filtered out of the body, so mctl-api answers 400 with a
    message that dies in the activity's own logs and reaches the caller as an
    indistinguishable `unknown`. Answered here it carries a reason the caller
    can log, and it costs no request at all. Raising is not an option: this
    activity's contract is that it never does.
    """
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return httpx.Response(200, content=json.dumps(_record()).encode())

    result = _run(monkeypatch, handler, _req(op="progress", epoch=4, evidence=""))
    assert result.verdict == "unknown"
    assert "evidence" in result.reason
    assert called == [], called
