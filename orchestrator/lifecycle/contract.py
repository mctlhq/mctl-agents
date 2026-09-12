"""Typed lifecycle-ownership contract.

Frozen dataclasses co-located with their consumer, matching
`orchestrator/resolver.py` — this repo has no pydantic and no `schemas/`
package. Every field is defaulted so a value recorded before a field existed
still deserializes out of Temporal history.
"""
from __future__ import annotations

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
        )


# The three answers to "is this entity owned?", and the reason this type
# exists at all.
#
# `_dev_loop_owns` returns a bool, and every failure — a missing token, a 404,
# a network error, a budget timeout — collapses into False, which the caller
# reads as "not owned" and therefore "safe to act". So the system cannot tell
# "nobody owns this" from "I could not find out", and acts identically on both.
#
# Three values make that impossible to express.
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

    ``verdict`` is one of the four constants above. ``UNKNOWN`` is the whole
    point: it is what an unreachable or unconfigured store returns, and it is
    NOT ``UNOWNED``. A caller that treats them the same has reintroduced the
    defect this contract exists to remove.
    """

    verdict: str = UNKNOWN
    ownership: Ownership | None = None
    reason: str = ""

    @property
    def wrote(self) -> bool:
        """Whether a mutating call is known to have succeeded.

        OWNED_BY_ME covers the usual case, where the server returned the record
        it wrote. WROTE_NO_RECORD covers a 2xx with no body.
        """
        return self.verdict in (OWNED_BY_ME, WROTE_NO_RECORD)

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
