"""Typed lifecycle-ownership contract.

Frozen dataclasses co-located with their consumer, matching
`orchestrator/resolver.py` — this repo has no pydantic and no `schemas/`
package. Every field is defaulted so a value recorded before a field existed
still deserializes out of Temporal history.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

# Entity kinds.
KIND_DEVLOOP_PROPOSAL = "devloop-proposal"
KIND_PULL_REQUEST = "pull-request"

# Phases, typed per entity kind.
PHASE_IMPLEMENT = "implement"
PHASE_REVIEW_REMEDIATION = "review-remediation"

# Ownership states. There is deliberately no "stale" and no "conflicted":
# both are DERIVED on read, and storing them would need something to sweep and
# write them, which is the scheduler this must not become.
STATE_ACTIVE = "active"
STATE_HANDING_OFF = "handing-off"
STATE_RELEASED = "released"
STATE_TERMINAL = "terminal"

# Owner types. These name actors, never permissions: ownership grants neither
# push nor merge authority (ADR-010 §6).
OWNER_DEVLOOP_WORKFLOW = "devloop-workflow"
OWNER_SHEPHERD = "shepherd"
OWNER_PR_STEWARD = "pr-steward"
OWNER_RECONCILER = "reconciler"
OWNER_HUMAN_CODEOWNER = "human-codeowner"
# The Tier 2 implementer CLI process (run_implementer.py). Added for
# ExecutionClaim executors (phase 2, #352): it is the first lifecycle caller
# that is neither the DevLoop workflow nor a Tier 3 process.
OWNER_IMPLEMENTER = "implementer"


@dataclass(frozen=True)
class EntityRef:
    """The thing whose lifecycle is being advanced.

    ``version`` is NOT part of the ownership key. It is recorded so a reader
    can see which version the owner last observed, and becomes a precondition
    on an execution claim (phase 2, #352) — never on ownership itself.
    Ownership must survive a review-fix push, because a new head on the same
    PR is the normal case rather than a handoff.
    """

    kind: str = ""
    id: str = ""
    version: str = ""

    @staticmethod
    def for_pull_request(repo: str, number: int, head_sha: str = "") -> EntityRef:
        return EntityRef(kind=KIND_PULL_REQUEST, id=f"{repo}#{number}", version=head_sha)

    @staticmethod
    def for_proposal(service: str, slug: str, version: str = "") -> EntityRef:
        return EntityRef(kind=KIND_DEVLOOP_PROPOSAL, id=f"{service}/{slug}", version=version)


@dataclass(frozen=True)
class Owner:
    type: str = ""
    id: str = ""


@dataclass(frozen=True)
class Ownership:
    """One durable record: actor X holds responsibility for (entity, phase).

    ``dead``, ``stuck`` and ``healthy`` are computed by mctl-api and carried
    here rather than recomputed locally, so every consumer gets the same answer
    instead of each re-implementing the bounds and drifting.
    """

    entity: EntityRef = field(default_factory=EntityRef)
    phase: str = ""
    owner: Owner = field(default_factory=Owner)
    epoch: int = 0
    state: str = ""
    proposal_ref: str = ""
    policy_ref: str = ""
    handoff_to: Owner | None = None
    handoff_from: Owner | None = None
    last_seen_at: str = ""
    last_progress_at: str = ""
    progress_evidence: str = ""
    temporal_workflow_id: str = ""
    dead: bool = False
    stuck: bool = False
    healthy: bool = False

    #: ``derived.held`` from mctl-api: does this record withhold the entity
    #: from other actors? ``None`` means the server did not send it.
    #:
    #: Three-valued on purpose, and NOT defaulted to False. `held` is the
    #: takeover predicate, and mctl-api computes it from the state and the
    #: liveness bound rather than from the status string, because a
    #: handing-off row past its bound reports the more specific
    #: `handoff-stalled` while being dead and recoverable. Re-deriving that
    #: here would be the second implementation of one predicate in two
    #: languages, and mctl-api's own derive.go records that the first consumer
    #: to re-derive it got it wrong — in the direction that turns the one
    #: dangerous divergence class into agreement.
    #:
    #: So an absent field is carried as absent. A default of False would say
    #: "nobody holds this", which is the single answer that licenses action.
    held: bool | None = None

    #: ``derived.status`` — the closed vocabulary healthy | stuck | dead |
    #: handing-off | handoff-stalled | released | terminal | unknown. Carried
    #: for reporting only; nothing decides on it.
    derived_status: str = ""

    @staticmethod
    def from_payload(data: dict[str, Any]) -> Ownership | None:
        """Build from an mctl-api response, or return None if it is not one.

        Two rules, and both exist because this module's whole contract is that
        uncertainty is a VALUE and never an exception.

        Unknown keys are IGNORED: mctl-api may add a field before this repo's
        image is rebuilt, and a client that hard-failed on that would turn a
        routine API deploy into an ownership outage — which now fails mutations
        closed.

        A MALFORMED payload returns None rather than raising or, worse,
        parsing into an all-empty record. An empty record would carry
        ``owner=Owner("","")`` and ``healthy=False``, which reads downstream as
        a confident "somebody else owns this" — a wrong answer stated with the
        same confidence as a right one. None becomes UNKNOWN at the call site.
        """
        if not isinstance(data, dict):
            return None

        def _mapping(raw: Any) -> dict[str, Any]:
            return raw if isinstance(raw, dict) else {}

        def _owner(raw: Any) -> Owner | None:
            if not isinstance(raw, dict):
                return None
            return Owner(type=str(raw.get("type") or ""), id=str(raw.get("id") or ""))

        def _int(raw: Any) -> int:
            try:
                return int(raw or 0)
            except (TypeError, ValueError):
                return 0

        ent = _mapping(data.get("entity"))
        own = _mapping(data.get("owner"))
        # A record with no phase, owner type or state is not a record. Every
        # real response carries all three, and this is the cheapest way to tell
        # an ownership payload from an error envelope that happened to be 200.
        #
        # `state` is required as hard as the other two on purpose: the verdict
        # is derived from it, and a payload that omitted it would be classified
        # as an unrecognised state — correct, but it would look like a server
        # change rather than a malformed body.
        if not data.get("phase") or not own.get("type") or not data.get("state"):
            return None
        if "healthy" not in data:
            # Required as hard as `state`, and for the same reason: the verdict
            # derives from it. A write response that omitted the computed
            # `healthy` would default it to False, and the acquirer would answer
            # OWNED_BY_OTHER for the record it had just created — may_mutate
            # False for the owner, blocks_others True for everyone else, and an
            # empty reason. Tests would stay green, because every fixture sets
            # it.
            return None

        return Ownership(
            entity=EntityRef(
                kind=str(ent.get("kind") or ""),
                id=str(ent.get("id") or ""),
                version=str(ent.get("version") or ""),
            ),
            phase=str(data.get("phase") or ""),
            owner=Owner(type=str(own.get("type") or ""), id=str(own.get("id") or "")),
            epoch=_int(data.get("epoch")),
            state=str(data.get("state") or ""),
            proposal_ref=str(data.get("proposal_ref") or ""),
            policy_ref=str(data.get("policy_ref") or ""),
            handoff_to=_owner(data.get("handoff_to")),
            handoff_from=_owner(data.get("handoff_from")),
            last_seen_at=str(data.get("last_seen_at") or ""),
            last_progress_at=str(data.get("last_progress_at") or ""),
            progress_evidence=str(data.get("progress_evidence") or ""),
            temporal_workflow_id=str(data.get("temporal_workflow_id") or ""),
            dead=bool(data.get("dead")),
            stuck=bool(data.get("stuck")),
            healthy=bool(data.get("healthy")),
            # NOT required the way `state` and `healthy` are: an mctl-api that
            # predates the derived block must still parse. It answers held=None
            # and the shadow compare reports store-unknown for that record,
            # which is loud and correct — rather than quietly classifying
            # against a predicate nobody computed.
            held=_derived_held(data.get("derived")),
            derived_status=str(_mapping(data.get("derived")).get("status") or ""),
        )


def _derived_held(raw: Any) -> bool | None:
    """``derived.held``, or None when the server did not send it.

    A non-bool value is also None: a string "false" coerced with bool() is
    True, and that coercion in this particular field would report an
    unowned entity as held — or, one release later, the reverse.
    """
    if not isinstance(raw, dict):
        return None
    held = raw.get("held")
    return held if isinstance(held, bool) else None


# The five answers to "is this entity owned?", and the reason this type
# exists at all.
#
# `_dev_loop_owns` returns a bool, and every failure — a missing token, a 404,
# a network error, a budget timeout — collapses into False, which the caller
# reads as "not owned" and therefore "safe to act". So the system cannot tell
# "nobody owns this" from "I could not find out", and acts identically on both.
#
# A closed vocabulary makes that impossible to express.
OWNED_BY_OTHER = "owned-by-other"
OWNED_BY_ME = "owned-by-me"
UNOWNED = "unowned"
UNKNOWN = "unknown"
# A mutating call that succeeded and returned no record. Distinct from UNKNOWN,
# which means the call may not have happened at all: a caller gating local state
# on a successful release must be able to tell the two apart.
WROTE_NO_RECORD = "wrote-no-record"


@dataclass(frozen=True)
class OwnershipAnswer:
    """The result of asking who owns an entity phase.

    ``verdict`` is one of the five constants above. ``UNKNOWN`` is the whole
    point: it is what an unreachable or unconfigured store returns, and it is
    NOT ``UNOWNED``. A caller that treats them the same has reintroduced the
    defect this contract exists to remove.
    """

    verdict: str = UNKNOWN
    ownership: Ownership | None = None
    reason: str = ""

    # Set by answer_from when a MUTATING call got a 2xx. It is deliberately not
    # derived from the verdict: a successful release() or terminal() returns the
    # record it just wrote, whose state is released/terminal, which verdict_for
    # correctly maps to UNOWNED — a verdict in neither arm of any "did it work"
    # test built from verdicts alone. A caller gating local state on that could
    # never record a release the server had accepted, which is the
    # `result is not None` defect arriving from the opposite direction.
    accepted: bool = False

    @property
    def wrote(self) -> bool:
        """Whether a mutating call is known to have succeeded."""
        return self.accepted

    @property
    def may_mutate(self) -> bool:
        """Whether the asking actor may perform a mutating step.

        UNKNOWN is False. An unreachable store must not license a second actor
        to push or merge — that is the one behaviour inversion this contract
        makes deliberately, and it is the reason the verdict is not a bool.
        """
        return self.verdict == OWNED_BY_ME

    @property
    def blocks_others(self) -> bool:
        """Whether another actor must stand down.

        UNKNOWN is True, for the same reason may_mutate is False: uncertainty
        resolves toward "do not act", never toward "act".
        """
        return self.verdict in (OWNED_BY_OTHER, OWNED_BY_ME, UNKNOWN)


# --- classification ----------------------------------------------------
#
# These rules live in the CONTRACT module, not in a transport, because there
# are two transports — a synchronous urllib client for CLI processes and an
# async httpx activity for Temporal — and neither may carry its own copy. The
# activity itself lands with mctlhq/mctl-agents#362; this module is written for
# both transports from the start precisely so the second one cannot arrive
# carrying its own answer.
#
# They already drifted once, in the direction that matters: the activity's copy
# classified an unrecognised state as FREE after the client's had been fixed to
# fail closed, and accepted only an exact 200 where the client accepted the 2xx
# range. Two implementations of one safety decision is how that decision
# becomes a coin flip.


# The routes that RELINQUISH an entity — the only two whose body-less 2xx may
# answer WROTE_NO_RECORD, because that verdict is `blocks_others` False and so
# is literally the statement that nobody holds the entity now.
#
# CLOSED, and closed on this side deliberately. The first version listed the
# CLAIMING routes and treated everything else as relinquishing, which left the
# path vocabulary open on the FAIL-OPEN side: a named route this image does not
# recognise — mctl-api adds one, or a base path gains a prefix — answered
# "nobody holds it". Every other vocabulary in this module is closed on both
# sides for exactly that reason (HOLDING_STATES and FREE_STATES are both
# explicit, and a state in neither is UNKNOWN), and this one was the exception.
#
# It was also wrong about two routes that already existed. `/progress` leaves
# the record active and owned by the caller, and `/handoff/start` writes
# handing-off, a HOLDING state that test_handing_off_record_still_blocks pins
# as blocking — so the same row answered `blocks_others` True when read back
# and False when the write that produced it returned 204.
#
# Keyed on the request path rather than a parameter because both transports
# already pass `path` and neither can forget to: a flag would be a third thing
# the sync client and the Temporal activity have to agree about, which is
# exactly the drift this module exists to stop.
RELINQUISHING_PATH_SUFFIXES = ("/release", "/terminal")


def _relinquishes_ownership(path: str) -> bool:
    return path.endswith(RELINQUISHING_PATH_SUFFIXES)


def _error_of(status: int, payload: dict[str, Any]) -> str:
    return str(payload.get("error") or f"HTTP {status}")


def record_of(payload: dict[str, Any]) -> Ownership | None:
    """The record in a response body, top level or nested under ``ownership``.

    ONE unwrapper, because the two shapes are both real and both in use: the
    single-record routes answer with the record at the top level, and the
    conflict envelope and the batch route nest it. Having recognition and
    parsing disagree about which shapes count produced the worst answer in the
    module's vocabulary — ``accepted=True`` with ``verdict=UNKNOWN`` and no
    record attached, i.e. "I recognise this body" followed immediately by "it
    contains nothing" — and on a read the same body was UNKNOWN for every
    entity in it.
    """
    if not isinstance(payload, dict):
        # The same guard from_payload carries, and for the same reason: this
        # module's contract is that uncertainty is a VALUE, never an exception.
        # `_get_chunk` passes each per-id value through screened only for None,
        # so a batch answering {"ownership": {"mctlhq/a#1": "active"}} reaches
        # here as a string — and .get would raise AttributeError and kill the
        # whole sweep tick. mypy cannot see it: that value is typed Any.
        return None
    own = Ownership.from_payload(payload)
    if own is not None:
        return own
    nested = payload.get("ownership")
    if isinstance(nested, dict):
        return Ownership.from_payload(nested)
    return None


def _looks_like_our_answer(payload: dict[str, Any]) -> bool:
    """Whether this body came from the lifecycle API rather than something in
    front of it.

    An ownership record, or the conflict envelope that nests one. Anything else
    — an error object, a health page rendered as JSON, a proxy's own schema —
    is a 2xx we did not ask for.
    """
    return record_of(payload) is not None


# The two states in which the record still holds the entity, and the two in
# which it has let go. Both lists are CLOSED, and a state in neither is
# deliberately not classified — see verdict_for.
HOLDING_STATES = frozenset({STATE_ACTIVE, STATE_HANDING_OFF})
FREE_STATES = frozenset({STATE_RELEASED, STATE_TERMINAL})


def verdict_for(own: Ownership, asking: Owner | None) -> str:
    """Turn a record into an answer.

    ``state`` is load-bearing, and this function has had it wrong twice in
    opposite directions.

    First it ignored state entirely. A released or terminal row still names an
    owner, so that answered OWNED_BY_OTHER for an entity explicitly handed
    back — and since UNKNOWN and OWNED_BY_OTHER both set ``blocks_others``, the
    next actor stood down forever on a PR nobody owned.

    The fix classified anything *outside* the holding set as free, which fails
    the other way: a holding state added server-side and unknown to this image
    — this client is deployed in a container image that lags mctl-api by a
    release — would read as UNOWNED, and a second actor would act alongside the
    true owner. That is worse than the bug it replaced: the first mistake made
    the system too timid, this one makes it act.

    So both sets are closed and a state in neither is UNKNOWN. Uncertainty
    resolves toward "do not act", which is the same rule the verdict itself
    encodes — an unrecognised state is exactly as much of an unknown as an
    unreachable store.
    """
    if own.state in FREE_STATES:
        return UNOWNED
    if own.state not in HOLDING_STATES:
        return UNKNOWN
    if asking is not None and own.owner == asking and own.healthy:
        return OWNED_BY_ME
    return OWNED_BY_OTHER


def answer_from(
    status: int,
    payload: dict[str, Any],
    asking: Owner | None,
    *,
    is_read: bool = False,
    path: str = "",
    body_empty: bool = False,
) -> OwnershipAnswer:
    """Turn one HTTP response into an answer. The only implementation.

    ``body_empty`` separates a genuine no-content success from a body that
    failed to parse. Both reach here as an empty mapping, and collapsing them
    reads an HTML error page served with a 200 — a gateway answering for the
    API — as a successful write.
    """
    # `accepted` requires mctl-api's OWN answer, not merely a 2xx.
    #
    # Deriving it from the status alone meant a sidecar answering
    # `200 {"error": "rate limited"}`, or anything else returning valid JSON
    # that is not a record, told a caller its release had succeeded. The module
    # already applies this rule to 404s — believe the status only when the
    # envelope backs it — and a 2xx deserves the same, because the 2xx is the
    # one a caller acts on.
    # `accepted` and `verdict` answer DIFFERENT questions, and on a body-less
    # 2xx to a claiming route they deliberately disagree: `wrote` is True
    # because mctl-api took the write, `verdict` is UNKNOWN because the record
    # that would name us as owner never arrived. Callers gating a MUTATION must
    # read `may_mutate`, never `wrote` — `wrote` says the request landed, not
    # that the caller may act on it.
    accepted = False
    if not is_read and 200 <= status < 300:
        accepted = body_empty or _looks_like_our_answer(payload)
    if 200 <= status < 300:
        # A 200 whose body is not an ownership record is a surprise, not an
        # answer. Parsing it into an all-empty record would produce a confident
        # OWNED_BY_OTHER with no reason — a wrong answer stated as firmly as a
        # right one.
        own = record_of(payload)
        if own is None:
            if not is_read and body_empty and _relinquishes_ownership(path):
                # A genuinely body-less 2xx on a RELINQUISHING write means it
                # SUCCEEDED and told us nothing more. Reporting UNKNOWN would
                # make it indistinguishable from a 503, and a caller gating its
                # local state on the result could never record a successful
                # release.
                #
                # An empty PARSE is a different thing and must not reach here:
                # a 200 carrying an HTML error page from a gateway is not a
                # successful write, and body_empty is what separates them.
                #
                # And it must not reach here for any write that LEAVES the
                # entity held. This verdict is the only one outside the three
                # "somebody holds it" answers, so blocks_others is False —
                # correct for a release or a terminal, which is precisely the
                # statement that nobody holds the entity now, and fail-open for
                # every other route: acquire and handoff/complete leave the
                # CALLER holding it, progress leaves the record active, and
                # handoff/start writes handing-off, a HOLDING state. A
                # body-less 2xx on any of those is a protocol anomaly —
                # mctl-api answers them with the record — so it is UNKNOWN, and
                # so is an unrecognised or empty path, because the list above is
                # closed and this is its fail-CLOSED side.
                return OwnershipAnswer(
                    verdict=WROTE_NO_RECORD,
                    reason=f"{status} with no body",
                    accepted=accepted,
                )
            # A 2xx the server accepted whose body we cannot read is still an
            # accepted write; we simply learned nothing about ownership.
            return OwnershipAnswer(
                verdict=UNKNOWN,
                reason=f"no ownership record in a {status} response",
                accepted=accepted,
            )
        verdict = verdict_for(own, asking)
        reason = "" if verdict != UNKNOWN else f"unrecognised ownership state {own.state!r}"
        return OwnershipAnswer(
            verdict=verdict, ownership=own, reason=reason, accepted=accepted
        )
    if status == 404 and is_read:
        if not isinstance(payload.get("error"), str) or not payload["error"]:
            # A 404 without mctl-api's own error envelope did not come from
            # mctl-api: an ingress rule that stopped matching, a wrong base
            # path, or a proxy's HTML page. Answering UNOWNED would free EVERY
            # entity asked — this is the module's only fail-open branch.
            #
            # Gating on non-empty JSON was one step short, because every
            # JSON-speaking intermediary answers exactly that: an ALB returns
            # {"message": ...}, Envoy a {"code", "message"} pair, a misrouted
            # apiserver a Status object. All parse non-empty. `error` is the
            # key _error_of already treats as the envelope, so it is the one
            # this must agree with.
            #
            # It agrees with the SERVER, not just with this file: mctl-api's
            # lifecycle 404 goes through `writeError`
            # (internal/api/handlers_read.go), which writes exactly
            # {"error": message}. That matters more than the false-positive
            # side — some intermediaries (Kong, OAuth-shaped proxies) do use
            # `error`, and being wrong that way costs one UNOWNED read that
            # should have been UNKNOWN. Being wrong the OTHER way, if mctl-api
            # stopped sending the key, would stall every sweep closed forever.
            # Changing that response shape is therefore a change to this gate.
            return OwnershipAnswer(
                verdict=UNKNOWN, reason=f"404 with no error envelope from {path or 'a read'}"
            )
        return OwnershipAnswer(verdict=UNOWNED, reason="no record")
    if status == 404:
        # On a WRITE a 404 is not "no such row". POST /acquire has no
        # not-found semantics, so a 404 there is a missing route, a wrong base
        # path, or an ingress answering for something else — and answering
        # UNOWNED would set blocks_others False for EVERY entity asked. That is
        # fail-open, in the one place in this module that can produce it.
        return OwnershipAnswer(verdict=UNKNOWN, reason=f"404 from {path or 'a write'}")
    if status == 409:
        own = record_of(payload)
        if own is None:
            return OwnershipAnswer(verdict=OWNED_BY_OTHER, reason=_error_of(status, payload))
        # A 409 is a REFUSED write, and a refused write may inform but must
        # never grant. Routing it straight through verdict_for did both:
        #
        #   - a 409 on the heartbeat acquire of a row this loop is already
        #     handing off answered OWNED_BY_ME (handing-off is a HOLDING
        #     state), so the loop kept pushing after the server said no — and
        #     a stale-epoch 409 did the same, while _owner_epoch still held
        #     the value the server had just rejected;
        #   - a 409 carrying a released or terminal record answered UNOWNED,
        #     which is blocks_others False: the one verdict that licenses
        #     action, produced by a write that was declined. That is the same
        #     fail-open the 404-on-a-write branch nine lines above exists to
        #     stop.
        #
        # So only the standing-down half survives. OWNED_BY_OTHER passes
        # through, because a 409 naming somebody else is the server answering
        # the question directly. Everything else — our own record, a free
        # record, an unrecognised state — becomes UNKNOWN: may_mutate False,
        # blocks_others True, and the record still attached so the caller can
        # see what the server saw.
        #
        # This does not make the loop give up work it owns, which was the
        # earlier finding here: UNKNOWN is "I could not establish ownership on
        # this attempt", not "somebody else has it". The next tick asks again.
        observed = verdict_for(own, asking)
        if observed == OWNED_BY_OTHER:
            return OwnershipAnswer(
                verdict=OWNED_BY_OTHER, ownership=own, reason=_error_of(status, payload)
            )
        return OwnershipAnswer(
            verdict=UNKNOWN,
            ownership=own,
            reason=(
                f"{_error_of(status, payload)} (refused write; "
                f"record reads {observed}, which a 409 may not grant)"
            ),
        )
    # 412, 503, 5xx, 401/403 — every one of these means "I could not establish
    # ownership", which is UNKNOWN and never UNOWNED. A 412 in particular means
    # the record moved underneath the caller, which is the strongest possible
    # reason not to act on a stale belief.
    return OwnershipAnswer(verdict=UNKNOWN, reason=_error_of(status, payload))


#: Ids per batch read, for every client that issues one. The server caps a
#: batch at 500 and the URL carries one `id=` each. It lives here rather than
#: in either caller because a difference between two callers' chunk sizes
#: would be a difference in what a batch means, not a tuning choice.
BATCH_CHUNK_SIZE = 100


def batch_answers_from(
    status: int,
    payload: dict[str, Any],
    ids: list[str],
    asking: Owner | None = None,
    *,
    path: str = "",
    reason: str = "",
) -> dict[str, OwnershipAnswer]:
    """Turn one `/ownership/batch` response into an answer PER ID.

    Here rather than in a transport for the reason `answer_from` is: there are
    two callers — the synchronous client used by CLI processes and the async
    reconcile activity — and the envelope rules below are safety decisions,
    not parsing details. Every id asked for appears in the result; an id that
    could not be answered is UNKNOWN, never absent, so a caller iterating the
    result cannot read a store outage as a clean sweep.
    """
    if not (200 <= status < 300):
        why = reason or _error_of(status, payload)
        return {i: OwnershipAnswer(verdict=UNKNOWN, reason=why) for i in ids}
    raw_found = payload.get("ownership")
    if not isinstance(raw_found, dict):
        # A 200 without the envelope this endpoint documents is a surprise.
        # Reading it as "no records" would report every id UNOWNED, which is
        # the one wrong answer that licenses action.
        return {
            i: OwnershipAnswer(verdict=UNKNOWN, reason="unrecognised batch payload")
            for i in ids
        }
    out: dict[str, OwnershipAnswer] = {}
    for i in ids:
        raw = raw_found.get(i)
        if raw is None:
            out[i] = OwnershipAnswer(verdict=UNOWNED)
            continue
        out[i] = answer_from(200, raw, asking, is_read=True, path=path)
    return out


# ---------------------------------------------------------------------------
# ExecutionClaim (ADR-010 phase 2, #352).
#
# Ownership above answers "who is responsible for reaching a terminal state on
# (entity, phase)" — a durable fact. A claim answers a narrower, shorter-lived
# question: "may THIS attempt perform a mutating step RIGHT NOW". Holding a
# claim grants nothing about merge or push authority on its own (#344); it is
# mutual exclusion plus fencing plus idempotency, nothing else.
#
# The classifier lives here, next to `answer_from`, for the same reason: two
# transports (a sync urllib client for CLI processes, an async httpx activity
# for Temporal) must not each carry their own copy of "what does this HTTP
# response mean". That already happened once to the ownership classifier.
# ---------------------------------------------------------------------------

CLAIM_HELD_BY_ME = "claim-held-by-me"
CLAIM_HELD_BY_OTHER = "claim-held-by-other"
CLAIM_FENCED = "claim-fenced"
CLAIM_UNCLAIMED = "claim-unclaimed"
CLAIM_UNKNOWN = "claim-unknown"

# Claim states, distinct from the ownership STATE_* vocabulary above even
# where the words coincide: a claim is a short lease, not a durable row, and
# "active" here never implies ownership health.
CLAIM_STATE_ACTIVE = "active"
CLAIM_STATE_RELEASED = "released"
CLAIM_STATE_EXPIRED = "expired"
CLAIM_STATE_FENCED = "fenced"

# Both closed, mirroring HOLDING_STATES / FREE_STATES: a claim state in
# neither set is CLAIM_UNKNOWN, never guessed toward either side.
HOLDING_CLAIM_STATES = frozenset({CLAIM_STATE_ACTIVE})
FREE_CLAIM_STATES = frozenset({CLAIM_STATE_RELEASED, CLAIM_STATE_EXPIRED, CLAIM_STATE_FENCED})


@dataclass(frozen=True)
class Executor:
    """The concrete worker attempt asking for (or holding) a claim.

    Distinct from ``Owner``: an ``Owner`` names who is durably RESPONSIBLE,
    an ``Executor`` names who is, right now, DOING. A delegated claim (a
    shepherd fixing a steward-owned PR) has an executor of type "shepherd"
    while the owning row still names the steward — holding the claim moves
    no ownership and grants no merge authority (#344).
    """

    type: str = ""  # shepherd | pr-steward | devloop-workflow | reconciler | implementer
    id: str = ""


@dataclass(frozen=True)
class ExecutionClaim:
    """One short-lived mutual-exclusion lease on (entity, phase, epoch, version).

    Every field defaulted, matching ``Ownership``, so a claim recorded before a
    field existed still deserializes out of Temporal history.
    """

    claim_id: str = ""
    entity: EntityRef = field(default_factory=EntityRef)
    phase: str = ""
    #: FENCE. Must match the ownership row's current epoch, or the claim is
    #: CLAIM_FENCED (a handoff moved on without this attempt).
    owner_epoch: int = 0
    #: FENCE. The head SHA or content hash this attempt is pinned to. A new
    #: PR head or a changed `.status.yaml` content hash fences it.
    entity_version: str = ""
    executor: Executor = field(default_factory=Executor)
    attempt: str = ""
    #: Server clock. Informational to the client — never computed locally.
    lease_until: str = ""
    idempotency_key: str = ""
    outcome: str = ""
    state: str = ""

    @staticmethod
    def from_payload(data: dict[str, Any]) -> ExecutionClaim | None:
        """Build from an mctl-api response, or None if it is not one.

        Same two rules as ``Ownership.from_payload``: unknown keys are
        ignored (a routine mctl-api deploy must not become a claims outage),
        and a malformed body returns None rather than an all-empty record
        that would read downstream as a confident answer.
        """
        if not isinstance(data, dict):
            return None

        def _mapping(raw: Any) -> dict[str, Any]:
            return raw if isinstance(raw, dict) else {}

        def _int(raw: Any) -> int:
            try:
                return int(raw or 0)
            except (TypeError, ValueError):
                return 0

        ent = _mapping(data.get("entity"))
        exe = _mapping(data.get("executor"))
        # A record with no claim_id, phase, executor type or state is not a
        # record — every real response carries all four, and this is the
        # cheapest way to tell a claim payload from an error envelope that
        # happened to be 200.
        if (
            not data.get("claim_id")
            or not data.get("phase")
            or not exe.get("type")
            or not data.get("state")
        ):
            return None

        return ExecutionClaim(
            claim_id=str(data.get("claim_id") or ""),
            entity=EntityRef(
                kind=str(ent.get("kind") or ""),
                id=str(ent.get("id") or ""),
                version=str(ent.get("version") or ""),
            ),
            phase=str(data.get("phase") or ""),
            owner_epoch=_int(data.get("owner_epoch")),
            entity_version=str(data.get("entity_version") or ""),
            executor=Executor(type=str(exe.get("type") or ""), id=str(exe.get("id") or "")),
            attempt=str(data.get("attempt") or ""),
            lease_until=str(data.get("lease_until") or ""),
            idempotency_key=str(data.get("idempotency_key") or ""),
            outcome=str(data.get("outcome") or ""),
            state=str(data.get("state") or ""),
        )


@dataclass(frozen=True)
class ClaimAnswer:
    """The result of asking for (or checking) an execution claim."""

    verdict: str = CLAIM_UNKNOWN
    claim: ExecutionClaim | None = None
    reason: str = ""
    accepted: bool = False
    # True only for the one answer the store REFUSED and the client still read
    # as ours: a 409 whose record names the asking attempt (see the 409 arm in
    # `claim_answer_from`). The caller must not treat that like a granted
    # acquire — no lease was applied, so the adopted claim carries the dead
    # predecessor's remaining `lease_until` and has to be renewed before the
    # run leans on it (claude P2 on `6794aad`). The log needs it too: `acquired`
    # would assert a grant that never happened.
    retaken: bool = False

    @property
    def may_execute(self) -> bool:
        """Whether the asking executor may perform the mutating step.

        True only for CLAIM_HELD_BY_ME. CLAIM_UNKNOWN is False for the same
        reason OwnershipAnswer.may_mutate is False on UNKNOWN: uncertainty
        must never license a second executor to push or merge.
        """
        return self.verdict == CLAIM_HELD_BY_ME


def claim_record_of(payload: dict[str, Any]) -> ExecutionClaim | None:
    """The claim in a response body, top level or nested under ``claim``."""
    if not isinstance(payload, dict):
        return None
    claim = ExecutionClaim.from_payload(payload)
    if claim is not None:
        return claim
    nested = payload.get("claim")
    if isinstance(nested, dict):
        return ExecutionClaim.from_payload(nested)
    return None


def claim_verdict_for(claim: ExecutionClaim, asking: Executor | None) -> str:
    """Turn a claim record into an answer, mirroring ``verdict_for``.

    Both state sets are closed and a state in neither is CLAIM_UNKNOWN — a
    claim state added server-side and unrecognised by this image must not be
    guessed toward either "I hold it" or "it is free".
    """
    if claim.state in FREE_CLAIM_STATES:
        return CLAIM_UNCLAIMED
    if claim.state not in HOLDING_CLAIM_STATES:
        return CLAIM_UNKNOWN
    if asking is not None and claim.executor == asking:
        return CLAIM_HELD_BY_ME
    return CLAIM_HELD_BY_OTHER


def _claim_error_of(status: int, payload: Any) -> str:
    """The error prose for a non-2xx claim response.

    `payload` is typed loosely on purpose: it comes from
    `json.loads(exc.read() or b"{}")` on an error body, and a gateway or proxy
    in front of mctl-api can answer a valid JSON scalar, list or `null`
    instead of an envelope. Calling `.get()` on one of those raises
    AttributeError from inside the very branch that exists to answer
    CLAIM_UNKNOWN — turning a fail-closed verdict into a crash (agy P3 on
    `31232dc`). Every other reader in this function already isinstance-guards;
    this one did not.
    """
    err = payload.get("error") if isinstance(payload, dict) else None
    return str(err or f"HTTP {status}")


def _looks_like_claim_answer(payload: dict[str, Any]) -> bool:
    return claim_record_of(payload) is not None


# The one claim route whose 2xx is meaningful WITHOUT a record. Closed, and
# closed on this side deliberately, exactly like RELINQUISHING_PATH_SUFFIXES
# above: a named route this image does not recognise must fall through to
# CLAIM_UNKNOWN, never into the branch that keeps a hold alive.
#
# A renew is addressed BY CLAIM ID by the actor that already holds it, so a
# 2xx answers only one question — "your lease is extended" — and the record it
# normally carries resolves nothing the caller did not already send. That is
# what separates it from `acquire`, where the record is the only thing naming
# the winner and a body-less 2xx is a protocol anomaly. Reading a 2xx renew as
# CLAIM_UNKNOWN is therefore not fail-closed but simply wrong: it refuses the
# attempt whose lease the store just extended, and under
# `LIFECYCLE_OWNERSHIP_REQUIRED` it does so on every restart until the lease
# runs out — the stall the retake exists to end (claude P2 on `0af3b38`).
RENEWING_PATH_SUFFIXES = ("/renew",)


def _confirms_existing_claim(path: str) -> bool:
    return path.endswith(RENEWING_PATH_SUFFIXES)


# The distinction `body_empty` alone cannot make. A 200 `{"error": "claim
# expired"}`, or a record from an mctl-api deploy this image is behind on —
# a renamed field, a level of nesting added — both leave `claim_record_of`
# answering None, and reading THAT as a live hold is the same unpinned-shape
# assumption as the defect this branch fixes, pointed the other way
# (claude P2 on `b362b5e`).
#
# The ONLY keys a bare renew acknowledgement may carry, and the only values
# they may hold. Both are closed ALLOW-lists, the direction every other set in
# this module points: a miss falls to CLAIM_UNKNOWN, never to the verdict.
# A deny-list of claim-shaped names was the first shape of this guard and it
# leaked in the obvious way — it inspected no value, so the exact negation of
# an acknowledgement (`{"ok": false}`, `{"status": "expired"}`, `{"renewed":
# false}`, `{"reason": "lease already expired"}`) read as a held claim, and
# `reason` is a key this repo's own `_claim_payload` sends (claude P2 on
# `f4d0dec`).
ACKNOWLEDGING_KEYS = frozenset({"ok", "renewed", "result", "status", "success"})
AFFIRMATIVE_VALUES = frozenset(
    {"accepted", "active", "held", "ok", "renewed", "success", "updated"}
)


def _acknowledges_without_describing(payload: dict[str, Any]) -> bool:
    """Whether a body says "renewed" and nothing else at all.

    `{"status": "renewed"}` and `{"ok": true}` qualify. A key outside
    `ACKNOWLEDGING_KEYS` does not — that includes every claim-shaped name, and
    also every name nobody has thought of, which is the point of reading the
    set this way round. A value outside `AFFIRMATIVE_VALUES` does not either:
    an acknowledgement that says NO is a refusal, and a refusal must never
    reach the most confident verdict in the vocabulary. Nesting is refused by
    both rules at once — a `dict` is no affirmative value — which is how a
    record arriving one level deeper than this image looks (`{"data":
    {"claim": ...}}`) stays CLAIM_UNKNOWN.
    """
    if not isinstance(payload, dict) or not payload:
        return False
    for key, value in payload.items():
        if key not in ACKNOWLEDGING_KEYS:
            return False
        if value is True:
            continue
        if isinstance(value, str) and value.strip().lower() in AFFIRMATIVE_VALUES:
            continue
        return False
    return True


def claim_answer_from(
    status: int,
    payload: dict[str, Any],
    asking: Executor | None,
    *,
    path: str = "",
    body_empty: bool = False,
) -> ClaimAnswer:
    """Turn one HTTP response into a claim answer. The only implementation.

    Deliberately simpler than ``answer_from``: every claim route is a
    mutation (there is no claim "read" the way ownership has ``GET``), so
    there is no ``is_read`` branch and no WROTE_NO_RECORD-equivalent verdict
    — a body-less 2xx here is a protocol anomaly the same way it is for every
    ownership route except release/terminal.
    """
    if 200 <= status < 300:
        accepted = body_empty or _looks_like_claim_answer(payload)
        claim = claim_record_of(payload)
        if claim is None:
            if _confirms_existing_claim(path) and (
                body_empty or _acknowledges_without_describing(payload)
            ):
                # The store performed the renew and said nothing more: a 204,
                # a `{"status": "renewed"}`, an `{"ok": true}`. The claim is
                # held by the asker — that is what a 2xx on this route means —
                # and no record is attached, because none arrived.
                #
                # The guard admits only a genuine no-content answer or a body
                # that describes NO claim at all. A body that failed to parse
                # (an HTML error page served with a 200) reaches here as an
                # empty mapping with `body_empty` False and is neither; so is a
                # 200 error envelope, and so is a record whose shape this image
                # is behind on — all three keep answering CLAIM_UNKNOWN, which
                # is where an unrecognised claim belongs, exactly as
                # `claim_verdict_for` fails an unrecognised STATE closed.
                return ClaimAnswer(
                    verdict=CLAIM_HELD_BY_ME,
                    reason=f"{status} renew with no claim record",
                    accepted=True,
                )
            return ClaimAnswer(
                verdict=CLAIM_UNKNOWN,
                reason=f"no claim record in a {status} response",
                accepted=accepted,
            )
        verdict = claim_verdict_for(claim, asking)
        reason = "" if verdict != CLAIM_UNKNOWN else f"unrecognised claim state {claim.state!r}"
        return ClaimAnswer(verdict=verdict, claim=claim, reason=reason, accepted=accepted)
    if status == 404:
        # POST .../acquire (and every other claim route) has no not-found
        # semantics. A 404 is a missing route or a wrong base path, never "no
        # such claim" — answering CLAIM_UNCLAIMED would be fail-open exactly
        # like the equivalent ownership branch.
        return ClaimAnswer(verdict=CLAIM_UNKNOWN, reason=f"404 from {path or 'a write'}")
    if status == 409:
        # ADR-010 §6's open question: mctl-api answers a fence and a
        # held-by-other conflict with the same status code, distinguished by
        # an envelope `code` field. Anything else on a 409 is UNKNOWN, not
        # guessed toward either side.
        code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
        claim = claim_record_of(payload)
        if code == "fenced":
            return ClaimAnswer(verdict=CLAIM_FENCED, claim=claim, reason=_claim_error_of(status, payload))
        if claim is not None and claim_verdict_for(claim, asking) == CLAIM_HELD_BY_ME:
            # The record names the ASKING attempt: this is our own claim, not
            # a rival's, and whether mctl-api encodes a same-executor
            # re-acquire as 200 or 409 is the §6 open question two lines up.
            # The client does not have to bet on it — the record and the
            # comparison are both already here, and `claim_verdict_for` is the
            # one function allowed to make it.
            #
            # The case is not exotic: `_resolve_attempt_id` is deterministic
            # precisely so a restarted pod re-derives the SAME identity and can
            # re-take the claim its killed predecessor never released (§8).
            # Read as HELD_BY_OTHER it was told its own orphan claim belongs to
            # somebody else, stood down writing nothing, and left the proposal
            # stuck `in-progress` behind itself for the full lease — with the
            # operator told a competing executor holds it, naming us (claude P2
            # on `8ac2080`).
            return ClaimAnswer(
                verdict=CLAIM_HELD_BY_ME,
                claim=claim,
                reason=_claim_error_of(status, payload),
                retaken=True,
            )
        if code == "claim-held" or claim is not None:
            # Any other record on a 409 is somebody else's. Deliberately NOT
            # `claim_verdict_for`'s full answer: a free state on a CONFLICT is
            # a contradiction the client must not resolve toward "nobody holds
            # it", which is the one direction that licenses a second executor.
            return ClaimAnswer(
                verdict=CLAIM_HELD_BY_OTHER, claim=claim, reason=_claim_error_of(status, payload)
            )
        # A 409 with neither a recognised code nor a claim record is the
        # fail-closed direction: it neither licenses execution nor charges an
        # attempt (requirements.md, "Wire status for a fence").
        return ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER, reason=_claim_error_of(status, payload))
    # 412, 503, 5xx, 401/403, timeouts surfaced by the transport as a non-2xx
    # — every one of these means "I could not establish the claim", which is
    # CLAIM_UNKNOWN and never CLAIM_UNCLAIMED.
    return ClaimAnswer(verdict=CLAIM_UNKNOWN, reason=_claim_error_of(status, payload))


def idempotency_key_for(
    kind: str, entity_id: str, phase: str, owner_epoch: int, attempt: str, version: str, action: str
) -> str:
    """The idempotency key ADR-010 §8 specifies: a pure hash, no clock, no
    random source.

    Identical input always produces identical output — the property a
    Temporal activity retry, a Temporal replay and an Argo pod restart all
    depend on to dedupe against the same recorded outcome.
    """
    raw = f"{kind}|{entity_id}|{phase}|{owner_epoch}|{attempt}|{version}|{action}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
