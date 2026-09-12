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
    WROTE_NO_RECORD,
    EntityRef,
    Owner,
)

ENTITY = EntityRef.for_pull_request("mctlhq/mctl-web", 42, "sha-a")
PHASE = "review-remediation"
ME = Owner(type="devloop-workflow", id="dev-loop-mctlhq-mctl-web-7")
OTHER = Owner(type="shepherd", id="cron")


class _FakeResponse(io.BytesIO):
    """Mimics the parts of a urllib response the client actually uses.

    getcode() is one of them: the client reads the real status rather than
    assuming 200, so a fake without it would make every success look like a
    transport failure — and the test would pass for the wrong reason.
    """

    def __init__(self, data: bytes, status: int = 200) -> None:
        super().__init__(data)
        self._status = status

    def getcode(self) -> int:
        return self._status

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


def test_released_record_is_unowned_not_owned_by_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """`state` is load-bearing, and the first version of the client ignored it.

    A released or terminal row still NAMES an owner. Reading it as
    `owned-by-other` blocks the next actor — and since both that verdict and
    `unknown` set `blocks_others`, it blocks them forever on a PR nobody owns.
    That is the zero-owner gap this package exists to close, reintroduced from
    the other side.
    """
    for state in ("released", "terminal"):
        payload = _owned_payload(OTHER)
        payload["state"] = state
        payload["healthy"] = False
        answer = _client(monkeypatch, _ok(payload)).get(ENTITY, PHASE, asking=ME)
        assert answer.verdict == UNOWNED, state
        assert answer.blocks_others is False, state


def test_handing_off_record_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handing off is not unowned: something still holds it, and a third actor
    must not walk in mid-handoff."""
    payload = _owned_payload(OTHER)
    payload["state"] = "handing-off"
    answer = _client(monkeypatch, _ok(payload)).get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == OWNED_BY_OTHER
    assert answer.blocks_others is True


def test_malformed_200_is_unknown_not_a_confident_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 whose body is not a record must not parse into an all-empty one.

    An empty record carries an empty owner and `healthy=False`, which reads as
    a confident "somebody else owns this" — a wrong answer stated with the same
    confidence as a right one.
    """
    for payload in ({}, {"unexpected": "envelope"}, {"entity": "not-a-dict", "owner": ["x"]}):
        answer = _client(monkeypatch, _ok(payload)).get(ENTITY, PHASE, asking=ME)
        assert answer.verdict == UNKNOWN, payload
        assert answer.may_mutate is False


def test_error_body_read_failure_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    """`HTTPError.read()` is a second network read on an already-failed
    connection, and it can time out. That exception would escape the handler —
    the adjacent `except Exception` is a sibling, not a wrapper — and crash a
    caller whose contract is that uncertainty is a value."""

    class _ExplodingHTTPError(urllib.error.HTTPError):
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            raise TimeoutError("connection reset while reading the error body")

    def _raise(req: Any) -> Any:
        raise _ExplodingHTTPError(req.full_url, 503, "err", {}, None)

    answer = _client(monkeypatch, _raise).get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNKNOWN
    assert answer.may_mutate is False


def test_batch_is_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    """One `id=` per entity means an unbounded sweep builds an unbounded query
    string and eventually earns a 414 — at which point every id turns UNKNOWN
    and the whole pass halts, indistinguishable from an outage."""
    seen: list[int] = []

    def _handler(req: Any) -> Any:
        seen.append(req.full_url.count("&id="))
        return _FakeResponse(json.dumps({"ownership": {}, "count": 0}).encode())

    ids = [f"mctlhq/repo#{i}" for i in range(250)]
    out = _client(monkeypatch, _handler).get_many(ENTITY.kind, PHASE, ids, asking=ME)
    assert len(out) == 250
    assert len(seen) == 3, f"expected 3 chunks of <=100, got {seen}"
    assert max(seen) <= lifecycle_client.BATCH_CHUNK_SIZE


def test_batch_unrecognised_payload_is_unknown_not_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a surprise 200 as "no records" would report every id UNOWNED,
    which is the one wrong answer that licenses action."""
    out = _client(monkeypatch, _ok({"totally": "different"})).get_many(
        ENTITY.kind, PHASE, ["mctlhq/a#1", "mctlhq/b#2"], asking=ME
    )
    assert all(a.verdict == UNKNOWN for a in out.values())


def test_unrecognised_state_is_unknown_not_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """The direction this function got wrong the second time.

    Classifying anything outside the holding set as free fails OPEN: a holding
    state added server-side and unknown to this image — which lags mctl-api by
    a release — would read as UNOWNED and a second actor would act alongside
    the true owner. Worse than the bug it replaced, which only made the system
    too timid.
    """
    for state in ("blocked", "pending", "paused", ""):
        payload = _owned_payload(OTHER)
        payload["state"] = state
        answer = _client(monkeypatch, _ok(payload)).get(ENTITY, PHASE, asking=ME)
        assert answer.verdict == UNKNOWN, state
        assert answer.may_mutate is False, state
        assert answer.blocks_others is True, state


def test_owner_liveness_is_visible_even_when_it_is_not_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead owner still answers OWNED_BY_OTHER, because this client cannot
    recover — that needs the guarded recovery surface (mctlhq/mctl-api#294) and
    the reconciler that drives it (mctlhq/mctl-agents#353).

    What it must not do is HIDE the fact, or a caller could never tell a
    healthy owner from one worth escalating.
    """
    payload = _owned_payload(OTHER, healthy=False)
    payload["dead"] = True
    answer = _client(monkeypatch, _ok(payload)).get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == OWNED_BY_OTHER
    assert answer.may_mutate is False
    assert answer.ownership is not None
    assert answer.ownership.dead is True
    assert answer.ownership.healthy is False


def test_body_less_2xx_write_is_a_success_not_an_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutating call that answers 2xx with no body SUCCEEDED.

    Reporting UNKNOWN there made it indistinguishable from a 503, so a caller
    gating its local state on the result could never record a successful
    release — and an earlier version of this test asserted UNKNOWN while its
    own docstring claimed the opposite.
    """
    handler = lambda req: _FakeResponse(b"", status=204)  # noqa: E731
    answer = _client(monkeypatch, handler).release(ENTITY, PHASE, ME, epoch=1, reason="done")
    assert answer.verdict == WROTE_NO_RECORD
    assert answer.wrote is True
    # It is still not a claim: nothing was learned about who owns the entity.
    assert answer.may_mutate is False
    assert "204" in answer.reason


def test_a_503_is_not_a_successful_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the distinction above."""
    c = _client(monkeypatch, _http_error(503, {"error": "store down"}))
    answer = c.release(ENTITY, PHASE, ME, epoch=1, reason="done")
    assert answer.verdict == UNKNOWN
    assert answer.wrote is False


def test_404_on_a_write_is_unknown_not_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /acquire has no not-found semantics.

    A 404 there is a missing route, a wrong base path, or an ingress answering
    for something else — and answering UNOWNED would set blocks_others False
    for EVERY entity asked. That is fail-open, in the one place in the module
    that can produce it.
    """
    c = _client(monkeypatch, _http_error(404, {"error": "not found"}))
    answer = c.acquire(ENTITY, PHASE, ME)
    assert answer.verdict == UNKNOWN
    assert answer.blocks_others is True
    assert answer.may_mutate is False


def test_404_on_a_read_is_still_unowned(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a GET it genuinely means "no record", which is the one case where
    UNOWNED is the right answer."""
    c = _client(monkeypatch, _http_error(404, {"error": "no ownership record"}))
    answer = c.get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNOWNED
    assert answer.blocks_others is False


def test_handoff_and_terminal_send_the_fields_the_server_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These three writers had no test at all.

    The assertion that matters is the payload: an empty-string filter in
    _write silently drops a field the server requires, and the failure would
    surface as a 400 in production rather than here.
    """
    sent: list[dict[str, Any]] = []

    def _handler(req: Any) -> Any:
        sent.append(json.loads(req.data.decode()))
        return _FakeResponse(json.dumps(_owned_payload(ME)).encode())

    c = _client(monkeypatch, _handler)
    c.handoff_start(ENTITY, PHASE, ME, epoch=2, to=OTHER, reason="exiting")
    c.handoff_complete(ENTITY, PHASE, OTHER)
    c.terminal(ENTITY, PHASE, ME, epoch=2, reason="merged")

    start, complete, terminal = sent
    assert start["to_owner_type"] == OTHER.type
    assert start["to_owner_id"] == OTHER.id
    assert start["epoch"] == 2
    # The INCOMING owner names itself on complete; the server matches it
    # against the recorded handoff target.
    assert complete["owner_type"] == OTHER.type
    assert complete["owner_id"] == OTHER.id
    assert terminal["reason"] == "merged"


def test_progress_without_evidence_is_not_silently_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_write filters empty strings, which would drop `evidence` — a field the
    server requires and rejects with a 400. Better to fail here, where the
    caller can see why, than to send a request that cannot succeed."""
    with pytest.raises(ValueError, match="evidence"):
        _client(monkeypatch, _ok(_owned_payload(ME))).progress(
            ENTITY, PHASE, ME, epoch=1, evidence=""
        )


def test_missing_healthy_is_not_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """`healthy` is required as hard as `state`, because the verdict derives
    from it just as directly.

    Defaulting it to False made the acquirer answer OWNED_BY_OTHER for the
    record it had just created: may_mutate False for the actual owner,
    blocks_others True for everyone else, and an empty reason. Every fixture
    sets it, so the tests stayed green.
    """
    payload = _owned_payload(ME)
    del payload["healthy"]
    answer = _client(monkeypatch, _ok(payload)).acquire(ENTITY, PHASE, ME)
    assert answer.verdict == UNKNOWN
    assert answer.may_mutate is False


def test_unrecognised_state_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """It was the only verdict reaching a caller with an empty reason, which
    is the one case where the caller most needs to know what happened."""
    payload = _owned_payload(OTHER)
    payload["state"] = "quarantined"
    answer = _client(monkeypatch, _ok(payload)).get(ENTITY, PHASE, asking=ME)
    assert answer.verdict == UNKNOWN
    assert "quarantined" in answer.reason


def test_a_200_with_an_unparseable_body_is_not_a_successful_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway or proxy answering for the API serves HTML with a 200.

    That parses to an empty mapping, exactly like a genuine 204, so collapsing
    the two would report a page-not-found as a completed release. `body_empty`
    is what separates them.
    """
    handler = lambda req: _FakeResponse(b"<html>502 Bad Gateway</html>", status=200)  # noqa: E731
    answer = _client(monkeypatch, handler).release(ENTITY, PHASE, ME, epoch=1, reason="done")
    assert answer.verdict == UNKNOWN
    assert answer.wrote is False
