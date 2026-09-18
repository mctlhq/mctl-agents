"""ExecutionClaim contract and client (ADR-010 phase 2, #352).

Same governing rule as the ownership contract's own test file: CLAIM_UNKNOWN
must never license execution, and a claim answer must never be re-classified
by a second implementation once contract.claim_answer_from has spoken.
"""
from __future__ import annotations

import io
import json
from typing import Any

import pytest

from orchestrator.lifecycle import claim as claim_module
from orchestrator.lifecycle.claim import ClaimClient, blocks_mutation
from orchestrator.lifecycle.contract import (
    CLAIM_FENCED,
    CLAIM_HELD_BY_ME,
    CLAIM_HELD_BY_OTHER,
    CLAIM_UNCLAIMED,
    CLAIM_UNKNOWN,
    UNKNOWN,
    ClaimAnswer,
    EntityRef,
    Executor,
    answer_from,
    claim_answer_from,
    idempotency_key_for,
)

ENTITY = EntityRef.for_pull_request("mctlhq/mctl-web", 42, "sha-a")
PHASE = "review-remediation"
ME = Executor(type="shepherd", id="attempt-1")
OTHER = Executor(type="pr-steward", id="attempt-2")


def _claim(executor: Executor, *, state: str = "active", claim_id: str = "c1") -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "entity": {"kind": ENTITY.kind, "id": ENTITY.id, "version": "sha-a"},
        "phase": PHASE,
        "owner_epoch": 3,
        "entity_version": "sha-a",
        "executor": {"type": executor.type, "id": executor.id},
        "attempt": executor.id,
        "state": state,
    }


# --- contract.claim_answer_from -----------------------------------------


def test_a_2xx_naming_me_is_held_by_me() -> None:
    answer = claim_answer_from(200, _claim(ME), ME)
    assert answer.verdict == CLAIM_HELD_BY_ME
    assert answer.may_execute is True


def test_a_2xx_naming_someone_else_is_held_by_other_and_names_the_winner() -> None:
    """T1: the loser's answer names the winning executor."""
    answer = claim_answer_from(200, _claim(OTHER), ME)
    assert answer.verdict == CLAIM_HELD_BY_OTHER
    assert answer.may_execute is False
    assert answer.claim is not None
    assert answer.claim.executor == OTHER


def test_free_claim_states_are_unclaimed_not_unknown() -> None:
    for state in ("released", "expired", "fenced"):
        answer = claim_answer_from(200, _claim(ME, state=state), ME)
        assert answer.verdict == CLAIM_UNCLAIMED, state
        assert answer.may_execute is False


def test_unrecognised_state_is_unknown_not_guessed() -> None:
    answer = claim_answer_from(200, _claim(ME, state="quarantined"), ME)
    assert answer.verdict == CLAIM_UNKNOWN
    assert "quarantined" in answer.reason


def test_a_409_with_fenced_code_is_fenced() -> None:
    """T2/T3: fencing is a distinct verdict from held-by-other."""
    payload = {"error": "owner epoch moved", "code": "fenced", **{"claim": _claim(OTHER, state="fenced")}}
    answer = claim_answer_from(409, payload, ME)
    assert answer.verdict == CLAIM_FENCED
    assert answer.may_execute is False


def test_a_409_without_a_recognised_code_is_held_by_other_not_fenced() -> None:
    """The open question in requirements.md: an unrecognised 409 code is the
    fail-closed direction — it neither licenses execution nor charges an
    attempt, and is NOT a fence (which ends the attempt outright)."""
    answer = claim_answer_from(409, {"error": "conflict"}, ME)
    assert answer.verdict == CLAIM_HELD_BY_OTHER
    assert answer.may_execute is False


@pytest.mark.parametrize(
    "status,payload",
    [
        (500, {"error": "internal"}),
        (503, {"error": "store down"}),
        (412, {"error": "stale"}),
        (404, {"error": "no such route"}),
        (200, {"unexpected": "envelope"}),
        (200, {}),
    ],
)
def test_unknown_never_licenses_execution(status: int, payload: dict[str, Any]) -> None:
    """T9. Every failure the transport can produce must answer CLAIM_UNKNOWN
    and never CLAIM_HELD_BY_ME."""
    answer = claim_answer_from(status, payload, ME)
    assert answer.verdict == CLAIM_UNKNOWN, (status, payload)
    assert answer.may_execute is False


def test_claim_and_ownership_classifiers_agree_on_shared_status_codes() -> None:
    """T14. Both classifiers live in contract.py so they cannot drift — pin
    the codes that mean the same thing on both sides regardless of body."""
    for status in (500, 503, 412):
        own = answer_from(status, {"error": "x"}, None)
        cla = claim_answer_from(status, {"error": "x"}, None)
        assert own.verdict == UNKNOWN
        assert cla.verdict == CLAIM_UNKNOWN
    # A 404 on every claim route (there is no claim "read") agrees with a
    # 404 on an ownership WRITE — never treated as "no such record".
    own_write = answer_from(404, {"error": "not found"}, None, is_read=False, path="/acquire")
    cla_write = claim_answer_from(404, {"error": "not found"}, None, path="/acquire")
    assert own_write.verdict == UNKNOWN
    assert cla_write.verdict == CLAIM_UNKNOWN


# --- idempotency_key_for --------------------------------------------------


def test_idempotency_key_is_deterministic() -> None:
    """T6. Identical input always produces identical output — a Temporal
    replay, an activity retry and a pod restart must all land on the same
    key with no clock and no random source."""
    args = ("pull-request", "mctlhq/mctl-web#42", "review-remediation", 3, "attempt-1", "sha-a", "push")
    assert idempotency_key_for(*args) == idempotency_key_for(*args)


def test_idempotency_key_changes_with_any_field() -> None:
    base = idempotency_key_for("pull-request", "id", "phase", 1, "attempt", "v1", "push")
    assert base != idempotency_key_for("pull-request", "id", "phase", 2, "attempt", "v1", "push")
    assert base != idempotency_key_for("pull-request", "id", "phase", 1, "attempt", "v2", "push")
    assert base != idempotency_key_for("pull-request", "id", "phase", 1, "other", "v1", "push")


# --- claim_verdict_for state closure --------------------------------------


def test_a_claim_state_in_neither_closed_set_is_unknown() -> None:
    from orchestrator.lifecycle.contract import ExecutionClaim, claim_verdict_for

    claim = ExecutionClaim(state="paused", executor=ME)
    assert claim_verdict_for(claim, ME) == CLAIM_UNKNOWN


# --- ClaimClient -----------------------------------------------------------


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200) -> None:
        super().__init__(data)
        self._status = status

    def getcode(self) -> int:
        return self._status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> ClaimClient:
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    c = ClaimClient()

    class _Opener:
        def open(self, req: Any, timeout: int | None = None) -> Any:
            return handler(req)

    c._opener = _Opener()
    return c


def test_off_mode_makes_no_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """T10. At `off` no claim HTTP call is made at all."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "off")
    monkeypatch.setenv("MCTL_TOKEN", "test-token")

    def _boom(req: Any) -> Any:
        raise AssertionError("no HTTP call should be made at rollout=off")

    c = ClaimClient()

    class _Opener:
        def open(self, req: Any, timeout: int | None = None) -> Any:
            return _boom(req)

    c._opener = _Opener()
    answer = c.acquire(ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", lease_seconds=60)
    assert answer.verdict == CLAIM_UNKNOWN
    assert answer.may_execute is False


def test_acquire_sends_lease_seconds_never_lease_until(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    def _handler(req: Any) -> Any:
        sent.append(json.loads(req.data.decode()))
        return _FakeResponse(json.dumps(_claim(ME)).encode())

    c = _client(monkeypatch, _handler)
    c.acquire(ENTITY, PHASE, 3, "sha-a", ME, "attempt-1", lease_seconds=7800)
    assert sent[0]["lease_seconds"] == 7800
    assert "lease_until" not in sent[0]


def test_acquire_refuses_an_empty_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, lambda req: _FakeResponse(b"{}"))
    with pytest.raises(ValueError, match="attempt"):
        c.acquire(ENTITY, PHASE, 0, "sha-a", ME, "", lease_seconds=60)


def test_two_acquires_produce_exactly_one_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    """T1, against a stubbed store: the loser's answer names the winner and
    has may_execute False."""

    def _handler_for(winner: Executor) -> Any:
        def _handle(req: Any) -> Any:
            payload = json.loads(req.data.decode())
            asking = Executor(type=payload["executor_type"], id=payload["executor_id"])
            if asking == winner:
                return _FakeResponse(json.dumps(_claim(winner)).encode())
            return _FakeResponse(
                json.dumps({"error": "held", "code": "claim-held", **{"claim": _claim(winner)}}).encode(),
                status=409,
            )

        return _handle

    handler = _handler_for(ME)
    c1 = _client(monkeypatch, handler)
    c2 = _client(monkeypatch, handler)
    a1 = c1.acquire(ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", lease_seconds=60)
    a2 = c2.acquire(ENTITY, PHASE, 0, "sha-a", OTHER, "attempt-2", lease_seconds=60)
    assert a1.verdict == CLAIM_HELD_BY_ME
    assert a2.verdict == CLAIM_HELD_BY_OTHER
    assert a2.claim is not None and a2.claim.executor == ME


def test_events_emitted_for_the_closed_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """T13. Each decision emits a line carrying entity, phase, epoch, executor
    and attempt identifiers."""
    lines: list[str] = []
    monkeypatch.setattr(claim_module, "_emit", lambda *a, **kw: lines.append(a[0]))

    c = _client(monkeypatch, lambda req: _FakeResponse(json.dumps(_claim(ME)).encode()))
    c.acquire(ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", lease_seconds=60)
    assert claim_module.EVENT_ACQUIRED in lines

    c2 = _client(
        monkeypatch,
        lambda req: _FakeResponse(
            json.dumps({"error": "held", "code": "claim-held", "claim": _claim(OTHER)}).encode(), status=409
        ),
    )
    c2.acquire(ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", lease_seconds=60)
    assert claim_module.EVENT_REJECTED in lines

    c3 = _client(
        monkeypatch,
        lambda req: _FakeResponse(
            json.dumps({"error": "fenced", "code": "fenced", "claim": _claim(OTHER, state="fenced")}).encode(),
            status=409,
        ),
    )
    c3.check("c1", ENTITY, PHASE, 0, "sha-a", ME, "attempt-1")
    assert claim_module.EVENT_FENCED in lines

    c4 = _client(monkeypatch, lambda req: _FakeResponse(b"", status=204))
    c4.release("c1", ENTITY, PHASE, 0, ME, "attempt-1")
    assert claim_module.EVENT_RELEASED in lines


def test_emission_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.print", lambda *a, **kw: (_ for _ in ()).throw(OSError("broken pipe")))
    # Must not raise even though the underlying print explodes.
    claim_module._emit("acquired", ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", "c1")


# --- blocks_mutation --------------------------------------------------------


def test_blocks_mutation_respects_rollout_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    fenced = ClaimAnswer(verdict=CLAIM_FENCED)
    unknown = ClaimAnswer(verdict=CLAIM_UNKNOWN)
    held_by_me = ClaimAnswer(verdict=CLAIM_HELD_BY_ME)

    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    assert blocks_mutation(fenced) is False, "observe composes no safety"
    assert blocks_mutation(unknown) is False

    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    assert blocks_mutation(fenced) is True
    assert blocks_mutation(held_by_me) is False

    monkeypatch.delenv("LIFECYCLE_OWNERSHIP_REQUIRED", raising=False)
    assert blocks_mutation(unknown) is True
    monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", "false")
    assert blocks_mutation(unknown) is False, "break-glass exempts UNKNOWN only"
    assert blocks_mutation(fenced) is True, "the break-glass never exempts a genuine fence"


@pytest.mark.parametrize("payload", ["502 Bad Gateway", ["error"], None, 7])
def test_a_non_dict_error_body_still_answers_unknown(payload) -> None:
    """A gateway in front of mctl-api can answer a valid JSON scalar or list.

    `_claim_error_of` called `.get()` on whatever `json.loads` returned, so a
    proxy's `"502 Bad Gateway"` raised AttributeError from inside the very
    branch that exists to answer CLAIM_UNKNOWN — a fail-closed verdict turned
    into a crash (agy P3 on `31232dc`).
    """
    answer = claim_answer_from(502, payload, None)
    assert answer.verdict == CLAIM_UNKNOWN
    assert answer.may_execute is False


@pytest.mark.parametrize("payload", ["conflict", [], None])
def test_a_non_dict_409_body_still_fails_closed(payload) -> None:
    answer = claim_answer_from(409, payload, None)
    assert answer.verdict == CLAIM_HELD_BY_OTHER
    assert answer.may_execute is False


def test_a_release_that_never_reached_the_store_is_not_logged_as_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one direction this log must never be wrong in: asserting a freed
    hold that may still be held. The transport-failure path answers
    CLAIM_UNKNOWN, and `release` used to map to `released` unconditionally
    (agy P3 on `c29195c`)."""
    lines: list[str] = []
    monkeypatch.setattr(claim_module, "_emit", lambda *a, **kw: lines.append(a[0]))

    def _boom(req: Any) -> Any:
        raise OSError("connection refused")

    c = _client(monkeypatch, _boom)
    answer = c.release("c1", ENTITY, PHASE, 0, ME, "attempt-1")
    assert answer.verdict == CLAIM_UNKNOWN
    assert claim_module.EVENT_RELEASED not in lines
    assert claim_module.EVENT_REJECTED in lines


@pytest.mark.parametrize(
    "body",
    [b"", b'{"status": "released"}', b'{"ok": true}'],
    ids=["204-empty", "status-body", "ack-body"],
)
def test_any_2xx_release_is_logged_as_released_whatever_the_body(
    monkeypatch: pytest.MonkeyPatch, body: bytes,
) -> None:
    """The mirror of the test above, and the other direction of the same lie.
    A 2xx release freed the claim regardless of what came back in the body:
    a body-less 204 answers CLAIM_UNKNOWN with `accepted` True, a plain
    `{"status": "released"}` answers it with `accepted` FALSE, and neither is
    a rejection. The HTTP status is the discriminator (claude P3 on
    `af661d7`)."""
    lines: list[str] = []
    monkeypatch.setattr(claim_module, "_emit", lambda *a, **kw: lines.append(a[0]))

    status = 204 if body == b"" else 200
    c = _client(monkeypatch, lambda req: _FakeResponse(body, status=status))
    c.release("c1", ENTITY, PHASE, 0, ME, "attempt-1")
    assert claim_module.EVENT_RELEASED in lines
    assert claim_module.EVENT_REJECTED not in lines


def test_a_refused_release_is_not_logged_as_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store that answered, and answered no. Distinct from both the 2xx
    above and the transport failure below it."""
    lines: list[str] = []
    monkeypatch.setattr(claim_module, "_emit", lambda *a, **kw: lines.append(a[0]))

    c = _client(monkeypatch, lambda req: _FakeResponse(b'{"code": "claim-held"}', status=409))
    c.release("c1", ENTITY, PHASE, 0, ME, "attempt-1")
    assert claim_module.EVENT_RELEASED not in lines
    assert claim_module.EVENT_REJECTED in lines


def test_renew_sends_lease_seconds_and_parses_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`renew` has no production callers yet — the review lease is sized to
    outlive its run instead — so its wire shape is only held in place by this
    test (agy P3 on `c29195c`)."""
    sent: list[dict[str, Any]] = []
    paths: list[str] = []

    def _handler(req: Any) -> Any:
        paths.append(req.full_url)
        sent.append(json.loads(req.data.decode()))
        return _FakeResponse(json.dumps(_claim(ME)).encode())

    c = _client(monkeypatch, _handler)
    answer = c.renew("c1", ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", lease_seconds=900)
    assert answer.verdict == CLAIM_HELD_BY_ME
    assert sent[0]["lease_seconds"] == 900
    assert sent[0]["executor_id"] == ME.id
    assert "lease_until" not in sent[0]
    assert paths[0].endswith("/renew")


def test_record_sends_the_idempotency_key_and_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    paths: list[str] = []

    def _handler(req: Any) -> Any:
        paths.append(req.full_url)
        sent.append(json.loads(req.data.decode()))
        return _FakeResponse(json.dumps(_claim(ME)).encode())

    c = _client(monkeypatch, _handler)
    answer = c.record(
        "c1", ENTITY, PHASE, 0, "sha-a", ME, "attempt-1",
        idempotency_key="key-1", action="push", outcome="ok",
    )
    assert answer.verdict == CLAIM_HELD_BY_ME
    assert sent[0]["idempotency_key"] == "key-1"
    assert sent[0]["action"] == "push"
    assert sent[0]["outcome"] == "ok"
    assert paths[0].endswith("/record")
