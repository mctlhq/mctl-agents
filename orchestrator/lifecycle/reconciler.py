"""What a reconcile sweep should DO about one ownership record.

ADR-010 phase 3 (mctlhq/mctl-agents#353). Ownership rows are written by the
actors that hold them, and every writer can die between two writes. This
module answers the one question that follows: given what the store says about
an entity phase, and what GitHub says about the entity itself, is there
anything to repair — and if so, the least action that repairs it.

**Pure.** No network, no clock, no environment. The decision is a function of
an observation, so it can be exercised for every condition #353 lists without
a store, and so the same table is auditable from the evidence it records. The
I/O lives in ``orchestrator/temporal/activities/lifecycle_reconcile.py``.

Three rules shape the table, and they are the reason it refuses more often
than it acts:

1. **The client never asserts that an owner is dead.** ``dead`` is computed by
   mctl-api against its own clock and carried on the record; the recovery
   route re-derives it server-side under the row lock and answers
   ``ErrOwnerAlive`` if the owner is still within its bound (ADR-010 §5). A
   delayed heartbeat therefore cannot cost a healthy owner its entity, even if
   this sweep reads a stale row and asks.
2. **Unknown is not unowned.** An unreachable store, an unrecognised state, a
   record this image cannot parse — all of them leave the entity alone. The
   sweep that cannot see is the sweep that must not write.
3. **Losing PROGRESS never moves ownership.** A stuck owner is escalated to a
   human and keeps the entity, because handing a stuck entity to another
   machine produces a second stuck machine and an epoch bump (ADR-010 §4).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from orchestrator.lifecycle.contract import (
    CLAIM_UNKNOWN,
    FREE_STATES,
    HOLDING_STATES,
    OWNED_BY_OTHER,
    STATE_ACTIVE,
    STATE_HANDING_OFF,
    UNKNOWN,
    UNOWNED,
    Owner,
)

# --- actions -----------------------------------------------------------
#
# A closed vocabulary, for the same reason the verdicts are: a caller
# switching on this must not have a default arm that silently absorbs an
# action added later, and an operator reading an evidence line must be able to
# enumerate what they might see.

#: The record is what it should be. Nothing is written.
ACTION_NONE = "none"
#: Something is wrong with the record, but nothing may be done about it from
#: here — the store did not answer, or the answer is one this image cannot
#: classify. Distinct from `none`: `none` means healthy, `skip` means blind.
ACTION_SKIP = "skip"
#: A handing-off row whose named target never arrived: complete it FOR that
#: target, which is the deterministic outcome the handoff already recorded.
ACTION_COMPLETE_HANDOFF = "complete-handoff"
#: The owner is dead per the store. Ask the server to re-derive that and hand
#: the row over at epoch+1, which fences every claim pinned to the old epoch.
#: The reconciler does not KEEP what it recovers — see `then_release`.
ACTION_RECOVER = "recover"
#: A conflict a machine must not resolve. Reported with evidence; no write.
ACTION_ESCALATE = "escalate"

ACTIONS = frozenset(
    {
        ACTION_NONE,
        ACTION_SKIP,
        ACTION_COMPLETE_HANDOFF,
        ACTION_RECOVER,
        ACTION_ESCALATE,
    }
)

#: Actions that write. Everything else is a report.
#:
#: Closing a terminal row is NOT an action of its own. It only ever follows a
#: recovery — an alive owner closes its own row — so it is a flag on that
#: decision (`then_finalise`) rather than a second entry here that no branch
#: could return.
MUTATING_ACTIONS = frozenset({ACTION_COMPLETE_HANDOFF, ACTION_RECOVER})


@dataclass(frozen=True)
class Observation:
    """Everything the sweep learned about one (entity, phase).

    Flat and fully defaulted, like the activity wire types: this crosses an
    activity boundary inside the result, and a field added later must not stop
    an older result from deserializing.

    The ownership half is a projection of ``OwnershipAnswer``/``Ownership``
    rather than a second parse of the payload — the classification of an HTTP
    response has exactly one implementation (``contract.answer_from``) and this
    module is not allowed to become a second.
    """

    kind: str = ""
    entity_id: str = ""
    phase: str = ""
    proposal_ref: str = ""

    # -- the store's answer
    verdict: str = UNKNOWN
    state: str = ""
    owner: Owner = field(default_factory=Owner)
    epoch: int = 0
    dead: bool = False
    stuck: bool = False
    healthy: bool = False
    derived_status: str = ""
    handoff_to: Owner | None = None
    record_version: str = ""
    temporal_workflow_id: str = ""

    # -- the entity itself, read from GitHub in the same sweep
    #: The canonical head this sweep observed. Recorded as evidence, never
    #: used as a takeover precondition: ownership survives a review-fix push
    #: by design (ADR-010 §4), so a new head is the normal case rather than a
    #: reason to move the row.
    head: str = ""
    #: The PR merged or closed, or the proposal reached a terminal status.
    entity_terminal: bool = False
    entity_terminal_reason: str = ""
    #: A DevLoopWorkflow for this entity is running right now, per the same
    #: Temporal visibility query the orphan sweep uses. Empty means none.
    #: May hold several comma-joined ids when more than one loop runs for the
    #: entity (mctlhq/mctl-agents#474), so read it only for evidence prose and
    #: a `!=` against the record's id, never as one loop to act on.
    live_workflow_id: str = ""
    #: Whether the entity still has work to do — an open PR on an actionable
    #: proposal. False for anything the sweep should not adopt an owner for.
    needs_owner: bool = False

    # -- the execution claim, when the claims API answers at all
    claim_verdict: str = CLAIM_UNKNOWN
    claim_state: str = ""
    claim_owner_epoch: int = 0
    claim_entity_version: str = ""


@dataclass(frozen=True)
class Decision:
    """One action, the rule that produced it, and why.

    ``reason`` is a stable slug rather than prose: it is what a metric or an
    alert groups on, and #353 requires every decision to be reconstructable
    afterwards. ``evidence`` is the human half, and is what goes into the
    ``evidence`` field of a recovery — the route refuses an empty one.
    """

    action: str = ACTION_SKIP
    reason: str = ""
    evidence: str = ""
    #: New owner for `recover` and `complete-handoff`. Empty otherwise.
    to_owner: Owner | None = None
    #: The epoch the write must assert as its precondition. A recovery of a
    #: row this sweep did not read at this epoch must fail (412), not succeed
    #: against whatever the row happens to hold now.
    expected_epoch: int = 0
    #: After a successful recovery, close the row immediately: the entity is
    #: already terminal and the recovered owner has nothing left to do. One
    #: decision rather than two sweeps, because between them the row would
    #: name an owner that is not working on anything.
    then_finalise: bool = False
    #: After a successful recovery of an entity that still has work, RELEASE
    #: it — the reconciler is not an actor that can advance a PR, and a row
    #: naming it would refuse the next real owner's `acquire` until it went
    #: dead in turn. Recovery here is for the EPOCH BUMP (which fences every
    #: claim pinned to the dead generation) and for leaving the entity in a
    #: state the next actor can take cleanly, which is `released`.
    then_release: bool = False

    @property
    def mutates(self) -> bool:
        return self.action in MUTATING_ACTIONS


def classify(obs: Observation, reconciler: Owner) -> Decision:
    """The decision table. Order is load-bearing; see the guards themselves.

    ``reconciler`` is this sweep's own identity — the owner a recovery asks to
    be granted at epoch+1. It is never the FINAL owner: the reconciler cannot
    advance a PR, so it recovers only to bump the epoch (fencing the dead
    generation's claims) and then releases or closes the row, leaving the
    entity in a state the next real actor can take cleanly.
    """
    where = f"{obs.kind} {obs.entity_id} phase={obs.phase}"

    # 1. Blind. No mutation may be derived from an answer that is not one.
    if obs.verdict == UNKNOWN:
        return Decision(
            action=ACTION_SKIP,
            reason="store-unknown",
            evidence=f"{where}: ownership store did not answer ({obs.derived_status or 'unknown'})",
        )
    if obs.verdict not in (UNOWNED, OWNED_BY_OTHER) or (
        obs.state and obs.state not in HOLDING_STATES and obs.state not in FREE_STATES
    ):
        # A verdict or state this image does not recognise. The sweep runs in
        # an image that lags mctl-api by a release, so this is a routine
        # possibility rather than a corruption — and the safe reading of a
        # state nobody here can name is that somebody else understands it.
        return Decision(
            action=ACTION_SKIP,
            reason="unrecognised-record",
            evidence=f"{where}: verdict={obs.verdict} state={obs.state!r} — not classifiable here",
        )

    # 2. Nobody holds it.
    if obs.verdict == UNOWNED:
        if obs.entity_terminal:
            return Decision(
                action=ACTION_NONE,
                reason="terminal-unowned",
                evidence=f"{where}: {obs.entity_terminal_reason or 'terminal'} and unowned — settled",
            )
        if not obs.needs_owner:
            # Unowned with nobody working on it: a queued proposal before its
            # implementer starts, a phase already past, or an entity whose
            # missing worker `detect_orphans` reports in the same tick. None
            # of the three is an ownership anomaly.
            return Decision(
                action=ACTION_NONE,
                reason="no-work",
                evidence=f"{where}: unowned, and nothing is working on the entity",
            )
        # Zero owner on live work — reported, never adopted.
        #
        # Adopting would be actively harmful, and the reason is in the store's
        # own semantics: `acquire` refuses an entity phase that already has an
        # ACTIVE row, regardless of who wrote it. A row this sweep planted
        # naming `reconciler` would therefore REFUSE the next DevLoop's
        # acquire — the legitimate owner — until it aged past its liveness
        # bound. The remedy for a zero-owner PR is an actor that can advance
        # it, which the orphan sweep in this same workflow already surfaces,
        # and proposal-less adoption discovery is explicitly #334's (ADR-010
        # pilot case 4). #353's requirement is that the condition converges to
        # one owner OR a visible conflict; this is the second arm, stated out
        # loud rather than by planting a row nobody can use.
        return Decision(
            action=ACTION_ESCALATE,
            reason="zero-owner-live-worker",
            evidence=(
                f"{where}: no ownership record on live work; head={obs.head or 'unknown'}, "
                f"{obs.live_workflow_id or 'a worker'} is running against a record nobody wrote"
            ),
        )

    # 3. Somebody holds it. Dead first: it is the only takeover licence, and
    #    it outranks every other repair because the others all assume an actor
    #    that can still perform them.
    if obs.dead:
        if obs.state == STATE_HANDING_OFF and obs.handoff_to is not None:
            # Both halves are broken: the outgoing owner is gone and the
            # incoming one never arrived. Recovery wins over completion here
            # because completing would hand the row to an actor that has
            # already demonstrated it is not coming — the server records the
            # abandoned target in the recovered event.
            return _recover(
                obs,
                where,
                reconciler,
                reason="handoff-stalled",
                detail=(
                    f"handing-off to {_owner_str(obs.handoff_to)} since before the liveness "
                    f"bound, and {_owner_str(obs.owner)} is dead"
                ),
            )
        return _recover(
            obs,
            where,
            reconciler,
            reason="dead-owner",
            detail=f"{_owner_str(obs.owner)} is dead per the store ({obs.derived_status or 'dead'})",
        )

    # 4. The holder is alive. Nothing below this line may move ownership.
    if obs.state == STATE_HANDING_OFF:
        if obs.handoff_to is None:
            return Decision(
                action=ACTION_ESCALATE,
                reason="handoff-no-target",
                evidence=f"{where}: state is handing-off with no target owner recorded",
            )
        return Decision(
            action=ACTION_COMPLETE_HANDOFF,
            reason="handoff-incomplete",
            evidence=(
                f"{where}: handing-off from {_owner_str(obs.owner)} to "
                f"{_owner_str(obs.handoff_to)}; completing on the target's behalf at "
                f"epoch {obs.epoch}"
            ),
            to_owner=obs.handoff_to,
            expected_epoch=obs.epoch,
        )

    if obs.entity_terminal:
        # The owner is alive and its entity is finished: closing the row is
        # ITS job, and it has one liveness bound to do it. Taking that away
        # here would race a live actor for a write neither of us needs to
        # win.
        return Decision(
            action=ACTION_NONE,
            reason="terminal-live-owner",
            evidence=(
                f"{where}: {obs.entity_terminal_reason or 'terminal'} while "
                f"{_owner_str(obs.owner)} is alive — leaving the close to the owner"
            ),
        )

    conflict = _claim_conflict(obs)
    if conflict:
        # Not fenced from here. The claim's fences are the owner epoch and the
        # entity version, and both are the SERVER's to enforce (ADR-010 §6);
        # a client that expired another actor's claim would be asserting
        # exactly the thing rule 1 forbids. The next recovery bumps the epoch
        # and the claim is fenced by construction.
        return Decision(
            action=ACTION_ESCALATE,
            reason="stale-claim",
            evidence=f"{where}: {conflict}",
        )

    if obs.stuck:
        return Decision(
            action=ACTION_ESCALATE,
            reason="stuck-owner",
            evidence=(
                f"{where}: {_owner_str(obs.owner)} is alive but has recorded no progress "
                f"within its bound — a human decides, ownership does not move"
            ),
        )

    if obs.live_workflow_id and obs.temporal_workflow_id and (
        obs.live_workflow_id != obs.temporal_workflow_id
    ):
        # Two actors on one entity. The database's partial unique index makes
        # a second ACTIVE ROW impossible, so this is the shape the condition
        # actually takes: one row, and a second worker running against it.
        return Decision(
            action=ACTION_ESCALATE,
            reason="conflicting-owner",
            evidence=(
                f"{where}: record names {obs.temporal_workflow_id} but "
                f"{obs.live_workflow_id} is also running"
            ),
        )

    return Decision(
        action=ACTION_NONE,
        reason="healthy",
        evidence=f"{where}: {_owner_str(obs.owner)} holds it and is healthy",
    )


def _recover(
    obs: Observation, where: str, reconciler: Owner, *, reason: str, detail: str
) -> Decision:
    """A takeover REQUEST — the server still decides whether it is granted."""
    return Decision(
        action=ACTION_RECOVER,
        reason=reason,
        evidence=(
            f"{where}: {detail}; head={obs.head or 'unknown'}, record version="
            f"{obs.record_version or 'unset'}, epoch={obs.epoch} — asking the server to "
            f"re-derive liveness and hand over at epoch+1"
        ),
        to_owner=reconciler,
        expected_epoch=obs.epoch,
        # A dead owner on a finished entity has nothing to hand on. Recover
        # and close in one decision, or the row would spend a sweep interval
        # naming an owner with no work.
        then_finalise=obs.entity_terminal,
        then_release=not obs.entity_terminal,
    )


def _claim_conflict(obs: Observation) -> str:
    """Why this entity's execution claim contradicts its ownership row, if it does.

    Answers "" when the claims API did not speak. Today that is every sweep:
    the `/api/v1/lifecycle/claims/*` routes ship with mctl-api's half of phase
    2 and do not exist yet, so every claim read is CLAIM_UNKNOWN — which is
    the no-mutation direction, and the reason this returns a REPORT rather
    than a fence.
    """
    if obs.claim_verdict == CLAIM_UNKNOWN or not obs.claim_state:
        return ""
    if obs.state != STATE_ACTIVE:
        return ""
    if obs.claim_owner_epoch and obs.claim_owner_epoch != obs.epoch:
        return (
            f"an execution claim is held at owner epoch {obs.claim_owner_epoch} while the "
            f"ownership row is at {obs.epoch}"
        )
    if obs.head and obs.claim_entity_version and obs.claim_entity_version != obs.head:
        return (
            f"an execution claim is pinned to {obs.claim_entity_version} while the head is "
            f"{obs.head}"
        )
    return ""


def _owner_str(owner: Owner | None) -> str:
    if owner is None or not owner.type:
        return "nobody"
    return f"{owner.type}:{owner.id}" if owner.id else owner.type
