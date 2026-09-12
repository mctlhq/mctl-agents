"""Lifecycle ownership client.

The assertions that matter here are all one assertion in different clothes:
UNKNOWN is not UNOWNED. `run_shepherd._dev_loop_owns` returns a bool and
collapses a missing token, a 404, a network error and a budget timeout into
False, which its caller reads as "not owned" and therefore "safe to act". Every
test below exists to make that collapse impossible to express.
"""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from orchestrator.lifecycle import client as lifecycle_client
from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    UNKNOWN,
    UNOWNED,
    EntityRef,
    Owner,
)

ENTITY = EntityRef.for_pull_request("mctlhq/mctl-web", 42, "sha-a")
PHASE = "review-remediation"
ME = Owner(type="devloop-workflow", id="dev-loop-mctlhq-mctl-web-7")
OTHER = Owner(type="shepherd", id="cron")


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> lifecycle_client.OwnershipClient:
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    c = lifecycle_client.OwnershipClient()

    class _Opener:
        def open(self, req: Any, timeout: int | None = None) -> Any:
            return handler(req)

    c._opener = _Opener()
    return c


def _ok(payload: dict[str, Any]) -> Any:
    return lambda req: _FakeResponse(json.dumps(payload).encode())


def _http_error(status: int, payload: dict[str, Any]) -> Any:
    def _raise(req: Any) -> Any:
        raise urllib.error.HTTPError(
            req.full_url, status, "err", {}, io.BytesIO(json.dumps(payload).encode())
        )

    return _raise


def _owned_payload(owner: Owner, *, healthy: bool = True, epoch: int = 1) -> dict[str, Any]:
    return {
        "entity": {"kind": ENTITY.kind, "id": ENTITY.id, "version": "sha-a"},
        "phase": PHASE,
        "owner": {"type": owner.type, "id": owner.id},
        "epoch": epoch,
        "state": "active",
        "healthy": healthy,
        "dead": not healthy,
        "stuck": False,
    }


def test_unreachable_store_is_unknown_not_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The central invariant. A network failure must never read as 'free'."""

    def _boom(req: Any) -> Any:
        raise OSError("connection refused")

    answer = _client(monkeypatch, _boom).get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNKNOWN
    assert answer.verdict != UNOWNED
    # Uncertainty resolves toward "do not act", in both directions.
    assert answer.may_mutate is False
    assert answer.blocks_others is True


def test_missing_token_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today a missing MCTL_TOKEN makes _dev_loop_owns return False, i.e.
    'nobody owns this'. A credential problem is not an ownership answer."""
    monkeypatch.delenv("MCTL_TOKEN", raising=False)
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    answer = lifecycle_client.OwnershipClient(token="").get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNKNOWN
    assert answer.may_mutate is False


def test_non_https_base_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bearer token must not leave over plaintext, and a misconfigured base
    URL is an UNKNOWN rather than a silent downgrade."""
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    answer = lifecycle_client.OwnershipClient(base_url="http://api.example.test").get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNKNOWN
    assert "https" in answer.reason


def test_503_is_unknown_not_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured store on the API side is the same class of answer as an
    unreachable one — never 'nobody owns this'."""
    c = _client(monkeypatch, _http_error(503, {"error": "lifecycle ownership store not configured"}))
    answer = c.get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNKNOWN
    assert answer.may_mutate is False


def test_412_is_unknown_because_the_record_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A precondition failure is the STRONGEST reason not to act on a stale
    belief, so it must not be mistaken for either ownership or absence."""
    c = _client(monkeypatch, _http_error(412, {"error": "owner epoch is stale"}))
    answer = c.progress(ENTITY, PHASE, ME, epoch=1, evidence="pushed")
    assert answer.verdict == UNKNOWN
    assert answer.may_mutate is False


def test_404_is_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one case that genuinely means 'no record exists'."""
    c = _client(monkeypatch, _http_error(404, {"error": "no ownership record"}))
    answer = c.get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNOWNED
    assert answer.may_mutate is False
    assert answer.blocks_others is False


def test_409_names_the_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loser that only learns it lost cannot tell a healthy owner from one it
    should escalate."""
    c = _client(monkeypatch, _http_error(409, {"error": "owned", "ownership": _owned_payload(OTHER)}))
    answer = c.acquire(ENTITY, PHASE, ME)
    assert answer.verdict == OWNED_BY_OTHER
    assert answer.ownership is not None
    assert answer.ownership.owner == OTHER
    assert answer.may_mutate is False


def test_owned_by_me_permits_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, _ok(_owned_payload(ME)))
    answer = c.acquire(ENTITY, PHASE, ME)
    assert answer.verdict == OWNED_BY_ME
    assert answer.may_mutate is True


def test_my_own_unhealthy_record_does_not_permit_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A record that names me but is not healthy — a dead owner whose epoch has
    been superseded — must not license a push."""
    c = _client(monkeypatch, _ok(_owned_payload(ME, healthy=False)))
    answer = c.acquire(ENTITY, PHASE, ME)
    assert answer.verdict == OWNED_BY_OTHER
    assert answer.may_mutate is False


def test_batch_failure_marks_every_id_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep's failure mode. If a batch read fails and ids simply went
    missing from the result, a caller iterating it would see a clean sweep and
    act on every one of them."""

    def _boom(req: Any) -> Any:
        raise OSError("timeout")

    ids = ["mctlhq/a#1", "mctlhq/b#2", "mctlhq/c#3"]
    out = _client(monkeypatch, _boom).get_many(ENTITY.kind, PHASE, ids, asking=ME)
    assert set(out) == set(ids)
    assert all(a.verdict == UNKNOWN for a in out.values())
    assert not any(a.may_mutate for a in out.values())


def test_batch_absent_id_is_unowned_not_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful batch that omits an id genuinely means 'no record'."""
    payload = {"ownership": {"mctlhq/a#1": _owned_payload(OTHER)}, "count": 1}
    out = _client(monkeypatch, _ok(payload)).get_many(ENTITY.kind, PHASE, ["mctlhq/a#1", "mctlhq/b#2"], asking=ME)
    assert out["mctlhq/a#1"].verdict == OWNED_BY_OTHER
    assert out["mctlhq/b#2"].verdict == UNOWNED


def test_unknown_response_fields_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """mctl-api may add a field before this image is rebuilt. A client that
    hard-failed on that would turn a routine API deploy into an ownership
    outage — and an ownership outage now fails mutations closed."""
    payload = _owned_payload(ME)
    payload["a_field_from_the_future"] = {"nested": True}
    answer = _client(monkeypatch, _ok(payload)).get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == OWNED_BY_ME


def test_ownership_required_break_glass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed is the default; the escape hatch is explicit and spelled."""
    monkeypatch.delenv("LIFECYCLE_OWNERSHIP_REQUIRED", raising=False)
    assert lifecycle_client.ownership_required() is True
    for value in ("false", "FALSE", "no", "0", "off"):
        monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", value)
        assert lifecycle_client.ownership_required() is False
    # Anything unrecognised keeps the gate ON. The default must be the safe
    # direction, not the spelled one.
    monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", "maybe")
    assert lifecycle_client.ownership_required() is True
