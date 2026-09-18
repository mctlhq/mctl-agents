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


def test_a_409_whose_record_names_me_is_my_own_claim_not_a_rival() -> None:
    """A restarted pod re-derives the SAME attempt id (`_resolve_attempt_id` is
    deterministic for exactly that reason, ADR-010 §8), so a conflict against
    its own orphaned claim names itself. Read as HELD_BY_OTHER it would stand
    down behind itself for the whole lease."""
    payload = {"error": "held", "code": "claim-held", "claim": _claim(ME)}
    answer = claim_answer_from(409, payload, ME)
    assert answer.verdict == CLAIM_HELD_BY_ME
    assert answer.may_execute is True
    assert answer.claim is not None
    assert answer.claim.executor == ME


def test_a_409_whose_record_names_another_executor_stays_held_by_other() -> None:
    payload = {"error": "held", "code": "claim-held", "claim": _claim(OTHER)}
    answer = claim_answer_from(409, payload, ME)
    assert answer.verdict == CLAIM_HELD_BY_OTHER
    assert answer.may_execute is False
    assert answer.claim is not None
    assert answer.claim.executor == OTHER


@pytest.mark.parametrize("state", ["released", "expired", "fenced"])
def test_a_free_state_on_a_409_is_never_read_as_unclaimed(state: str) -> None:
    """A free state on a CONFLICT is a contradiction. Resolving it toward
    "nobody holds it" is the one direction that licenses a second executor, so
    a foreign record stays HELD_BY_OTHER whatever state it carries."""
    payload = {"error": "held", "code": "claim-held", "claim": _claim(OTHER, state=state)}
    answer = claim_answer_from(409, payload, ME)
    assert answer.verdict == CLAIM_HELD_BY_OTHER
    assert answer.may_execute is False


@pytest.mark.parametrize("state", ["released", "expired"])
def test_a_free_state_naming_me_on_a_409_does_not_license_execution(state: str) -> None:
    """The same contradiction with our own name on it: `claim_verdict_for`
    answers UNCLAIMED for a free state, which is not HELD_BY_ME, so the
    promotion above must not fire and the answer falls through to the
    fail-closed arm."""
    payload = {"error": "held", "code": "claim-held", "claim": _claim(ME, state=state)}
    answer = claim_answer_from(409, payload, ME)
    assert answer.verdict == CLAIM_HELD_BY_OTHER
    assert answer.may_execute is False


def test_a_fence_naming_me_is_still_a_fence() -> None:
    """Fencing outranks ownership: the epoch moved, so even our own record is
    not a licence to keep going."""
    payload = {"error": "owner epoch moved", "code": "fenced", "claim": _claim(ME, state="fenced")}
    answer = claim_answer_from(409, payload, ME)
    assert answer.verdict == CLAIM_FENCED
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


def test_env_example_never_pins_a_computed_claim_lease() -> None:
    """An active line in `.env.example` propagates into every environment
    copied from it and pins a lease nobody chose. `_claim_lease_seconds` now
    refuses a value SHORTER than the computed one, so the worst case is no
    longer a claim expiring mid-run (agy P2 on `0af3b38`, claude P3 on
    `b362b5e`) — but a pinned longer value is still a number to keep in step
    by hand. The example file may document the variables; it must not set
    them."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    active = [
        line
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("LIFECYCLE_CLAIM_LEASE_SECONDS_")
    ]
    assert active == [], active


def test_a_retaken_409_is_logged_renewed_not_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store REFUSED this acquire; the client read the record as ours. The
    event must say what the store did — `acquired` asserts a grant that never
    happened, and the retake is the line worth seeing, since reaching it means
    a pod died holding a claim (claude P3 on `6794aad`)."""
    lines: list[str] = []
    monkeypatch.setattr(claim_module, "_emit", lambda *a, **kw: lines.append(a[0]))
    c = _client(
        monkeypatch,
        lambda req: _FakeResponse(
            json.dumps({"error": "held", "code": "claim-held", "claim": _claim(ME)}).encode(),
            status=409,
        ),
    )
    answer = c.acquire(ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", lease_seconds=60)
    assert answer.verdict == CLAIM_HELD_BY_ME
    assert answer.retaken is True
    assert lines == [claim_module.EVENT_RENEWED], lines


def test_a_granted_acquire_is_not_marked_retaken(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    monkeypatch.setattr(claim_module, "_emit", lambda *a, **kw: lines.append(a[0]))
    c = _client(monkeypatch, lambda req: _FakeResponse(json.dumps(_claim(ME)).encode()))
    answer = c.acquire(ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", lease_seconds=60)
    assert answer.retaken is False
    assert lines == [claim_module.EVENT_ACQUIRED], lines


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
    """`renew`'s only production call is the retake in `_acquire_claim`, which
    never varies the body — the review lease is sized to outlive its run
    instead of being heartbeated — so its wire shape is held in place by this
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


# --- a 2xx renew with no record (claude P2 on `0af3b38`) --------------------


@pytest.mark.parametrize(
    "payload,body_empty",
    [({}, True), ({"status": "renewed"}, False), ({"ok": True}, False)],
)
def test_a_renew_the_store_performed_is_held_by_me_even_with_no_record(
    payload: dict[str, Any], body_empty: bool
) -> None:
    """`/renew` is addressed BY CLAIM ID by the actor already holding it, so a
    2xx answers the only question asked and the record resolves nothing the
    caller did not send. Reading it as UNKNOWN refuses the attempt whose lease
    the store just extended — and under the ownership break-glass it does so on
    every restart until the orphan lease expires."""
    answer = claim_answer_from(
        200 if payload else 204,
        payload,
        ME,
        path="/api/v1/lifecycle/claims/renew",
        body_empty=body_empty,
    )
    assert answer.verdict == CLAIM_HELD_BY_ME
    assert answer.claim is None
    assert answer.accepted is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"error": "claim expired"},
        {"code": "claim-held"},
        {"claim": {"claim_id": "c1"}},
        {"data": {"claim_id": "c1", "state": "active"}},
        {"claim_id": "c1"},
    ],
)
def test_a_2xx_renew_describing_a_claim_we_cannot_read_is_never_a_hold(
    payload: dict[str, Any],
) -> None:
    """The branch admits an acknowledgement, never a record this image failed
    to read. A body that did not parse (`{}` with `body_empty` False — an HTML
    error page served with a 200), a 200 error envelope, and a record shaped
    for an mctl-api this image is behind on all leave `claim_record_of`
    answering None, and reading THAT as a live hold is the same unpinned-shape
    assumption as the defect this route's tolerance fixes, pointed the other
    way (claude P2 on `b362b5e`)."""
    answer = claim_answer_from(
        200, payload, ME, path="/api/v1/lifecycle/claims/renew", body_empty=False
    )
    assert answer.verdict == CLAIM_UNKNOWN


def test_only_renew_reads_a_recordless_2xx_as_a_hold() -> None:
    """On `acquire` the record is the only thing naming the winner, so a
    body-less 2xx there is a protocol anomaly, not a grant. The path list is
    closed on this side deliberately."""
    for route in ("acquire", "check", "record", "release"):
        answer = claim_answer_from(
            204, {}, ME, path=f"/api/v1/lifecycle/claims/{route}", body_empty=True
        )
        assert answer.verdict == CLAIM_UNKNOWN, route


# --- the op field on the event line (claude P3 on `0af3b38`) ---------------


def test_a_retake_and_its_renew_are_two_distinguishable_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retake maps a refused `acquire` onto `renewed` and then renews for
    real. Without the call that produced it, the two lines are byte-identical
    and the event the remap exists to surface cannot be counted."""
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    claim_module._emit(
        claim_module.EVENT_RENEWED, ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", "c1",
        op="acquire",
    )
    claim_module._emit(
        claim_module.EVENT_RENEWED, ENTITY, PHASE, 0, "sha-a", ME, "attempt-1", "c1",
        op="renew",
    )
    first, second = out.getvalue().splitlines()
    assert first.endswith("op=acquire")
    assert second.endswith("op=renew")
    assert first != second


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", 1800),
        ("900", 1800),
        ("1799", 1800),
        ("1800", 1800),
        ("3600", 3600),
        ("not-a-number", 1800),
    ],
)
def test_a_claim_lease_override_can_only_lengthen(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """The hazard the commented-out `.env.example` lines documented, closed in
    code: a lease shorter than the run it guards expires under its own attempt
    and comes back from the push-site check as CLAIM_UNCLAIMED, standing a
    valid attempt down. An operator may hold a claim LONGER — that only costs
    a slower takeover after a crash — so a larger value is honoured and a
    smaller one is refused with a log line (claude P3 on `b362b5e`)."""
    from datetime import timedelta

    from orchestrator.run_implementer import _claim_lease_seconds

    monkeypatch.setenv("LIFECYCLE_CLAIM_LEASE_SECONDS_REVIEW", raw)
    assert (
        _claim_lease_seconds(
            "LIFECYCLE_CLAIM_LEASE_SECONDS_REVIEW", timedelta(seconds=1800)
        )
        == expected
    )
