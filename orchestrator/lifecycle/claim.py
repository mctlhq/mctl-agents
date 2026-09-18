"""HTTP client for the mctl-api execution-claim surface (ADR-010 phase 2).

``ClaimClient`` is the synchronous urllib transport for ``run_shepherd`` and
``run_implementer`` — ordinary CLI processes inside an Argo pod, the same
constraint ``OwnershipClient`` is built for. It reuses that module's https
pin, no-redirect opener and unavailable-to-unknown conversion rather than
carrying a second copy: a claim call that could silently talk to the wrong
host, or follow a redirect off the pinned scheme, is the same defect in a
different module.

The Temporal side does NOT use this module, for the same reason it does not
use ``client.py``: an activity is async and this client is not, and workflow
code must never call either one directly (ADR-010 §9). The activity
counterpart is ``orchestrator.temporal.activities.lifecycle.execution_claim``.

A denial is data, not an exception — ``acquire`` returning "someone else
holds this" is the system working, so every call returns a ``ClaimAnswer``
rather than raising.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from orchestrator.lifecycle import rollout
from orchestrator.lifecycle.client import (
    DEFAULT_TIMEOUT_S,
    OwnershipUnavailable,
    _HTTPResult,
    _no_redirect_opener,
    api_base,
)
from orchestrator.lifecycle.contract import (
    CLAIM_FENCED,
    CLAIM_HELD_BY_ME,
    CLAIM_STATE_EXPIRED,
    CLAIM_UNKNOWN,
    ClaimAnswer,
    EntityRef,
    Executor,
    claim_answer_from,
)

# Structured-log prefix, in the style of ``shadow.LOG_PREFIX`` — greppable in
# an unattended cron/Argo Workflow log without a JSON parser.
LOG_PREFIX = "lifecycle-claim:"

# The closed event vocabulary (requirements.md, "Observability and
# placement"). A caller emitting a string outside this set is a bug in this
# module, not a variation the operator needs to learn.
EVENT_ACQUIRED = "acquired"
EVENT_REJECTED = "rejected"
EVENT_RENEWED = "renewed"
EVENT_RELEASED = "released"
EVENT_EXPIRED = "expired"
EVENT_FENCED = "fenced"


def _emit(
    event: str,
    entity: EntityRef,
    phase: str,
    owner_epoch: int,
    entity_version: str,
    executor: Executor,
    attempt: str,
    claim_id: str,
) -> None:
    """Emit one structured claim event line. Never raises.

    A logging call must not be the reason a claim decision is lost: this
    function runs after the HTTP round trip already completed, so an
    exception here (a broken stdout, an encoding surprise in an entity id)
    must not turn a successful claim decision into an unhandled crash.
    """
    try:
        print(
            f"{LOG_PREFIX} {event} entity={entity.kind}:{entity.id} phase={phase} "
            f"epoch={owner_epoch} version={entity_version} "
            f"executor={executor.type}/{executor.id} attempt={attempt} claim={claim_id}",
            flush=True,
        )
    except Exception:  # noqa: BLE001, S110 — emission must never raise, and the
        # failure mode here (a broken stdout) is exactly what a log call would
        # hit too, so there is nothing safe left to log to.
        pass


def blocks_mutation(answer: ClaimAnswer) -> bool:
    """Whether this claim answer must stop a mutation right now.

    The single decision every push/merge fencing check-in must share, so it
    is not re-derived per call site. Mirrors the ownership contract's split
    between "someone else definitely holds it" (always blocks once the
    rollout stage lets the new answer veto) and "I could not tell"
    (``CLAIM_UNKNOWN``, gated behind the ``LIFECYCLE_OWNERSHIP_REQUIRED``
    break-glass) — never treated the same, and never blocking below
    ``enforce``, where ``observe`` logs a fence but composes no safety.
    """
    if answer.may_execute:
        return False
    if not rollout.new_answer_may_veto():
        return False
    if answer.verdict == CLAIM_UNKNOWN:
        return rollout.blocks_on_unknown()
    # CLAIM_FENCED, CLAIM_HELD_BY_OTHER, CLAIM_UNCLAIMED-when-a-hold-was-
    # expected: all definite answers, not uncertainty, so the break-glass
    # (which exists only for "an unreachable store must not license a second
    # actor") does not apply to them.
    return True


class ClaimClient:
    """Synchronous execution-claim client. Safe in Argo pods and CLI entry points."""

    def __init__(
        self, base_url: str | None = None, token: str | None = None, timeout: int = DEFAULT_TIMEOUT_S
    ) -> None:
        self._base = (base_url or api_base()).rstrip("/")
        self._token = token if token is not None else os.environ.get("MCTL_TOKEN", "").strip()
        self._timeout = timeout
        self._opener = _no_redirect_opener()

    # -- transport ------------------------------------------------------
    #
    # Deliberately mirrors OwnershipClient._request rather than importing it:
    # the two clients are the SAME transport shape (https pin, no-redirect
    # opener, JSON in/out, HTTPError-to-_HTTPResult), but binding one client's
    # bound method onto another's `self` would tie their lifecycles together
    # for no benefit — each already carries its own opener and token.

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> _HTTPResult:
        if not self._token:
            raise OwnershipUnavailable("MCTL_TOKEN is not set")
        url = f"{self._base}{path}"
        if not url.startswith("https://"):
            raise OwnershipUnavailable(f"refusing non-https MCTL_API_BASE_URL: {self._base}")
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
            raise OwnershipUnavailable(str(exc)) from exc
        try:
            return _HTTPResult(
                status=status,
                payload=json.loads(body or b"{}"),
                status_path=path,
                body_empty=not body,
            )
        except ValueError as exc:
            raise OwnershipUnavailable(f"malformed response: {exc}") from exc

    # -- writes -----------------------------------------------------------

    def acquire(
        self,
        entity: EntityRef,
        phase: str,
        owner_epoch: int,
        entity_version: str,
        executor: Executor,
        attempt: str,
        *,
        lease_seconds: int,
        proposal_ref: str = "",
    ) -> ClaimAnswer:
        """Claim (entity, phase) for this attempt, or learn who holds it.

        Sends ``lease_seconds``, never ``lease_until`` — the lease is the
        SERVER's clock; a worker with a skewed local clock must not be able
        to extend its own lease by computing an expiry itself.
        """
        if not attempt:
            # The same local-failure-is-legible rule OwnershipClient.progress
            # applies to empty evidence: an empty attempt id would otherwise
            # be filtered out of the body by `_write` and the server would
            # answer 400 with a message this caller never sees.
            raise ValueError("acquire requires a non-empty attempt id")
        return self._write(
            "acquire",
            entity,
            phase,
            owner_epoch,
            entity_version,
            executor,
            attempt,
            op_name="acquire",
            lease_seconds=lease_seconds,
            proposal_ref=proposal_ref,
        )

    def renew(
        self,
        claim_id: str,
        entity: EntityRef,
        phase: str,
        owner_epoch: int,
        entity_version: str,
        executor: Executor,
        attempt: str,
        *,
        lease_seconds: int,
    ) -> ClaimAnswer:
        """Extend the lease on a claim this attempt already holds."""
        if not attempt:
            raise ValueError("renew requires a non-empty attempt id")
        return self._write(
            "renew",
            entity,
            phase,
            owner_epoch,
            entity_version,
            executor,
            attempt,
            op_name="renew",
            claim_id=claim_id,
            lease_seconds=lease_seconds,
        )

    def check(
        self,
        claim_id: str,
        entity: EntityRef,
        phase: str,
        owner_epoch: int,
        entity_version: str,
        executor: Executor,
        attempt: str,
    ) -> ClaimAnswer:
        """The precondition check immediately before a mutation.

        This is the EARLY filter, not the authoritative one — the
        authoritative CAS is the target system's own precondition
        (``--force-with-lease``, ``--match-head-commit``). A caller must
        never skip the authoritative check because this one passed.
        """
        if not attempt:
            raise ValueError("check requires a non-empty attempt id")
        return self._write(
            "check",
            entity,
            phase,
            owner_epoch,
            entity_version,
            executor,
            attempt,
            op_name="check",
            claim_id=claim_id,
        )

    def record(
        self,
        claim_id: str,
        entity: EntityRef,
        phase: str,
        owner_epoch: int,
        entity_version: str,
        executor: Executor,
        attempt: str,
        *,
        idempotency_key: str,
        action: str,
        outcome: str,
    ) -> ClaimAnswer:
        """Record a mutation's outcome under an idempotency key.

        A retry (Temporal activity retry, Temporal replay, Argo pod restart)
        that re-derives the same ``idempotency_key`` gets the previously
        recorded outcome back rather than repeating the effective mutation.
        """
        if not attempt:
            raise ValueError("record requires a non-empty attempt id")
        return self._write(
            "record",
            entity,
            phase,
            owner_epoch,
            entity_version,
            executor,
            attempt,
            op_name="record",
            claim_id=claim_id,
            idempotency_key=idempotency_key,
            action=action,
            outcome=outcome,
        )

    def release(
        self,
        claim_id: str,
        entity: EntityRef,
        phase: str,
        owner_epoch: int,
        executor: Executor,
        attempt: str,
        *,
        reason: str = "",
    ) -> ClaimAnswer:
        """Let go of a claim this attempt holds."""
        if not attempt:
            raise ValueError("release requires a non-empty attempt id")
        return self._write(
            "release",
            entity,
            phase,
            owner_epoch,
            "",
            executor,
            attempt,
            op_name="release",
            claim_id=claim_id,
            reason=reason,
        )

    # -- shared write path ------------------------------------------------

    def _write(
        self,
        route: str,
        entity: EntityRef,
        phase: str,
        owner_epoch: int,
        entity_version: str,
        executor: Executor,
        attempt: str,
        *,
        op_name: str,
        **extra: Any,
    ) -> ClaimAnswer:
        if not rollout.records_writes():
            # Break-glass OFF (ADR-010 §12): no claim HTTP call at all. Below
            # `observe` the new answer is never consulted, so making the call
            # anyway would only cost a round trip for a result nobody reads.
            answer = ClaimAnswer(
                verdict=CLAIM_UNKNOWN,
                reason=f"rollout mode {rollout.mode()}: claims not consulted",
            )
            self._emit_for(
                op_name, answer, entity, phase, owner_epoch, entity_version, executor, attempt,
                status=None,
            )
            return answer
        payload: dict[str, Any] = {
            "kind": entity.kind,
            "id": entity.id,
            "phase": phase,
            "owner_epoch": owner_epoch,
            "entity_version": entity_version,
            "executor_type": executor.type,
            "executor_id": executor.id,
            "attempt": attempt,
        }
        payload.update({k: v for k, v in extra.items() if v not in ("", None)})
        path = f"/api/v1/lifecycle/claims/{route}"
        try:
            res = self._request("POST", path, payload)
        except OwnershipUnavailable as exc:
            answer = ClaimAnswer(verdict=CLAIM_UNKNOWN, reason=str(exc))
            self._emit_for(
                op_name, answer, entity, phase, owner_epoch, entity_version, executor, attempt,
                status=None,
            )
            return answer
        answer = claim_answer_from(
            res.status, res.payload, executor, path=res.status_path, body_empty=res.body_empty
        )
        self._emit_for(
            op_name, answer, entity, phase, owner_epoch, entity_version, executor, attempt,
            status=res.status,
        )
        return answer

    def _emit_for(
        self,
        op_name: str,
        answer: ClaimAnswer,
        entity: EntityRef,
        phase: str,
        owner_epoch: int,
        entity_version: str,
        executor: Executor,
        attempt: str,
        *,
        status: int | None = None,
    ) -> None:
        """Map one call's outcome onto the closed event vocabulary and log it.

        ``status`` is the HTTP status the store actually answered with, or
        None when no request was made at all (transport failure, or the
        rollout mode that skips the call). It is the only input that separates
        "the store answered" from "we never got there", and the release event
        below is the one mapping that needs to know the difference.
        """
        claim_id = answer.claim.claim_id if answer.claim else ""
        if answer.verdict == CLAIM_FENCED:
            event = EVENT_FENCED
        elif answer.claim is not None and answer.claim.state == CLAIM_STATE_EXPIRED:
            event = EVENT_EXPIRED
        elif op_name == "acquire":
            if answer.retaken:
                # The store REFUSED this acquire (409) and the client read the
                # record as ours anyway. `acquired` would assert a grant that
                # never happened — the same "say what the store did, not what
                # the call asked for" rule the release arm below was corrected
                # to twice. `renewed` is the honest one: the claim already
                # existed, and it is the event worth seeing, because reaching
                # it means a pod died holding a claim (claude P3 on `6794aad`).
                event = EVENT_RENEWED
            else:
                event = EVENT_ACQUIRED if answer.verdict == CLAIM_HELD_BY_ME else EVENT_REJECTED
        elif op_name == "renew":
            event = EVENT_RENEWED if answer.verdict == CLAIM_HELD_BY_ME else EVENT_REJECTED
        elif op_name == "release":
            # A release that never reached the store is not a release, and
            # logging `released` for it asserts a freed hold that may still be
            # held — the one direction this log must never be wrong in (agy P3
            # on `c29195c`). The discriminator is the HTTP status, not
            # `answer.accepted` and not the verdict: a body-less 204 answers
            # CLAIM_UNKNOWN with `accepted` True, but so does a plain
            # `{"status": "released"}` body with `accepted` FALSE — that is a
            # release the store performed, and logging it `rejected` is the
            # same lie in the opposite direction (claude P3 on `af661d7`).
            # Within this arm: every 2xx is a release, and everything else —
            # 409, 404, 5xx, or no request at all — is not. It IS only within
            # this arm: the `fenced` and `expired` checks above run first and
            # answer their own events, so a 2xx release whose body reports a
            # fenced or expired record never reaches here (claude P3 on
            # `0808376`). That ordering is deliberate — those two say what
            # happened to the claim, which outranks what this call asked for.
            event = EVENT_RELEASED if status is not None and 200 <= status < 300 else EVENT_REJECTED
        elif op_name in ("check", "record") and answer.verdict != CLAIM_HELD_BY_ME:
            event = EVENT_REJECTED
        else:
            # A successful check()/record() is a pass-through, not a
            # decision — nothing changed, so nothing is logged.
            return
        _emit(event, entity, phase, owner_epoch, entity_version, executor, attempt, claim_id)
