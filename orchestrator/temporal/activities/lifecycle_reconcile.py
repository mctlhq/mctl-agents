"""Activity: reconcile orphaned, stale and conflicting ownership records.

ADR-010 phase 3 (mctlhq/mctl-agents#353). Every ownership row is written by
the actor that holds it, and every actor can die between two writes. This
sweep is what notices — and it runs inside the reconcile workflow that already
exists, on the same 15-minute tick, rather than as a scheduler of its own
(#353 explicitly rules out a second always-on scheduler).

**Decisions are not made here.** The table lives in
`orchestrator/lifecycle/reconciler.py` as a pure function of an observation,
so every condition can be exercised without a store and so the evidence a
decision records is derivable from the same inputs an operator would read.
This module gathers the observations, applies whatever the table returned, and
reports.

Two things it deliberately does NOT do:

* **It never asserts that an owner is dead.** `dead` arrives on the record,
  computed by mctl-api, and the recovery route re-derives it server-side under
  the row lock — answering 409 `ErrOwnerAlive` if the owner is still within
  its bound. So a sweep reading a stale row cannot cost a healthy owner its
  entity, which is the acceptance criterion that a delayed heartbeat must not
  be enough to replace an owner.
* **It never fences an execution claim.** The fences are the owner epoch and
  the entity version, and both are the server's to enforce (ADR-010 §6). A
  stale claim is reported, and the epoch bump a recovery performs fences it by
  construction. mctl-api has no `/api/v1/lifecycle/claims/*` routes yet, so
  every claim read answers CLAIM_UNKNOWN today — the no-mutation direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import httpx
from temporalio import activity

from orchestrator.lifecycle import reconciler, rollout
from orchestrator.lifecycle.contract import (
    KIND_DEVLOOP_PROPOSAL,
    KIND_PULL_REQUEST,
    OWNER_DEVLOOP_WORKFLOW,
    OWNER_RECONCILER,
    PHASE_IMPLEMENT,
    PHASE_REVIEW_REMEDIATION,
    EntityRef,
    Owner,
    OwnershipAnswer,
    batch_answers_from,
)
from orchestrator.temporal.activities.gitops_state import (
    ProposalStateRef,
    PRSnapshot,
    fetch_pr_snapshots,
    list_proposal_refs,
)
from orchestrator.temporal.activities.lifecycle import (
    OwnershipRequest,
    OwnershipResult,
    perform_ownership_op,
)
from orchestrator.temporal.activities.orphans import (
    ACTIONABLE_STATUSES,
    _expected_workflow_id,
)
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

READ_TIMEOUT_SECONDS = 20.0

#: Statuses that mean the proposal's own lifecycle is over. An ownership row
#: still holding one of these is the "entity terminal while ownership active"
#: condition #353 lists.
TERMINAL_STATUSES = frozenset({"merged", "rejected", "review-stuck"})

#: Ids per batch read. The server caps a batch at 500 and the URL carries one
#: `id=` each; the sync client uses 100 for the same reason and this must not
#: drift from it, so the number is imported rather than repeated... it is not
#: importable without dragging urllib into an async module, so it is asserted
#: in tests instead (tests/test_lifecycle_reconcile.py).
BATCH_CHUNK_SIZE = 100


@dataclass(frozen=True)
class ReconcileFinding:
    """One entity, what the table decided, and what came of it.

    #353's evidence requirement in one record: entity, phase, version/head,
    old and new owner epoch, claim state, reason and outcome. Flat and fully
    defaulted, because this crosses an activity boundary into workflow history.
    """

    kind: str = ""
    entity_id: str = ""
    phase: str = ""
    action: str = ""
    reason: str = ""
    evidence: str = ""
    outcome: str = ""
    owner_type: str = ""
    owner_id: str = ""
    epoch_before: int = 0
    epoch_after: int = 0
    head: str = ""
    claim_state: str = ""


@dataclass(frozen=True)
class LifecycleReconcileResult:
    examined: int = 0
    findings: list[ReconcileFinding] = field(default_factory=list)
    #: Writes that the server ACCEPTED. Not the number of decisions to write:
    #: a refused recovery (the owner turned out to be alive) is a finding with
    #: an outcome, and counting it here would report a takeover that did not
    #: happen.
    applied: int = 0
    escalations: int = 0
    #: Set when the sweep did not run at all. Same contract as
    #: OrphanDetectionResult.skipped_reason: a skipped tick must not read like
    #: a clean one.
    skipped_reason: str = ""


#: Outcomes a finding can carry. Closed vocabulary, same rule as the actions.
OUTCOME_REPORTED = "reported"
OUTCOME_OBSERVED = "observed"
OUTCOME_APPLIED = "applied"
OUTCOME_REFUSED = "refused"


@activity.defn
async def reconcile_lifecycle_ownership(
    active_workflow_ids: list[str] | None = None,
) -> LifecycleReconcileResult:
    """Examine every tracked entity's ownership record and repair what it can.

    Fails SOFT in one direction only: anything it cannot establish becomes a
    skipped sweep or an UNKNOWN verdict, and neither writes. It does raise on
    a GitHub listing failure, because that is what Temporal's retry policy is
    for and because a sweep that silently examined nothing is the shape of
    #270.
    """
    if not rollout.computes_new_answer():
        mode = rollout.mode()
        activity.logger.info(
            "lifecycle reconcile skipped: %s=%s", rollout.ENV_VAR, mode
        )
        return LifecycleReconcileResult(skipped_reason=f"rollout mode {mode}")

    try:
        headers = auth_headers()
    except Exception as exc:  # noqa: BLE001 — auth_headers raises on a missing token
        activity.logger.warning("lifecycle reconcile has no usable credentials: %s", exc)
        return LifecycleReconcileResult(skipped_reason=f"auth: {exc}")

    refs = await list_proposal_refs()
    snapshots = await fetch_pr_snapshots(refs)
    active = set(active_workflow_ids or [])

    observations = _observe(refs, snapshots, active)
    if not observations:
        return LifecycleReconcileResult(examined=0)

    await _fill_ownership(observations, headers)

    # The reconciler's identity on any row it recovers. The workflow id, not a
    # constant: a recovered row must name the sweep that took it, or the
    # `recovered` event answers "who" with a word rather than with something an
    # operator can look up in Temporal.
    info = activity.info()
    me = Owner(type=OWNER_RECONCILER, id=info.workflow_id or "reconcile")

    findings: list[ReconcileFinding] = []
    applied = 0
    escalations = 0
    for obs in observations:
        decision = reconciler.classify(obs, me)
        finding = await _apply(obs, decision, me)
        findings.append(finding)
        if finding.outcome == OUTCOME_APPLIED:
            applied += 1
        if decision.action == reconciler.ACTION_ESCALATE:
            escalations += 1
        if decision.action != reconciler.ACTION_NONE:
            # One line per non-trivial decision, in the shape the orphan sweep
            # already logs: this is what an operator greps, and what a promtail
            # rule can count without parsing a result payload out of history.
            activity.logger.info(
                "LIFECYCLE-RECONCILE kind=%s id=%s phase=%s action=%s reason=%s "
                "outcome=%s epoch=%d->%d evidence=%s",
                finding.kind,
                finding.entity_id,
                finding.phase,
                finding.action,
                finding.reason,
                finding.outcome,
                finding.epoch_before,
                finding.epoch_after,
                finding.evidence,
            )

    return LifecycleReconcileResult(
        examined=len(observations),
        findings=findings,
        applied=applied,
        escalations=escalations,
    )


def _observe(
    refs: list[ProposalStateRef],
    snapshots: dict[tuple[str, str], PRSnapshot],
    active: set[str],
) -> list[reconciler.Observation]:
    """One observation per (entity, phase) this sweep can say anything about.

    Both kinds, because both have writers that can die: a DevLoopWorkflow
    holds `pull-request/review-remediation` and the implementer holds
    `devloop-proposal/implement`. The ownership half is filled in afterwards
    by a batched read — building the list first is what makes that batching
    possible.
    """
    out: list[reconciler.Observation] = []
    for ref in refs:
        pr = snapshots.get((ref.service, ref.slug))
        actionable = ref.status in ACTIONABLE_STATUSES
        proposal_ref = f"{ref.service}/{ref.slug}"

        out.append(
            reconciler.Observation(
                kind=KIND_DEVLOOP_PROPOSAL,
                entity_id=EntityRef.for_proposal(ref.service, ref.slug).id,
                phase=PHASE_IMPLEMENT,
                proposal_ref=proposal_ref,
                entity_terminal=ref.status in TERMINAL_STATUSES,
                entity_terminal_reason=f"proposal status {ref.status}",
                # The implement phase ends when a PR exists: the PR, not the
                # proposal, is what anybody works on afterwards.
                needs_owner=actionable and pr is None,
                head=pr.head_sha if pr else "",
                live_workflow_id=_live_id(ref, pr, active),
            )
        )

        if pr is None:
            continue
        terminal_reason = ""
        if pr.merged:
            terminal_reason = "PR merged"
        elif pr.closed_unmerged:
            terminal_reason = "PR closed unmerged"
        out.append(
            reconciler.Observation(
                kind=KIND_PULL_REQUEST,
                entity_id=EntityRef.for_pull_request(pr.repo, pr.number).id,
                phase=PHASE_REVIEW_REMEDIATION,
                proposal_ref=proposal_ref,
                entity_terminal=bool(terminal_reason),
                entity_terminal_reason=terminal_reason,
                needs_owner=actionable and not terminal_reason,
                head=pr.head_sha,
                live_workflow_id=_live_id(ref, pr, active),
            )
        )
    return out


def _live_id(ref: ProposalStateRef, pr: PRSnapshot | None, active: set[str]) -> str:
    """The DevLoopWorkflow running against this proposal, or "".

    Through `_expected_workflow_id`, the orphan sweep's own reconstruction,
    rather than a second one: that function already carries the correction
    that made the ids match at all (#151) and the owner-derivation fix on top
    of it (#212), and a private copy here would be the third answer to a
    question that has been wrong twice.
    """
    expected = _expected_workflow_id(ref.slug, pr.repo if pr else None, ref.service)
    return expected if expected and expected in active else ""


async def _fill_ownership(
    observations: list[reconciler.Observation], headers: dict[str, str]
) -> None:
    """Batch-read the store and fold the answers into the observations.

    In place, by replacing entries of the list: `Observation` is frozen, which
    is what stops a decision from mutating what it was derived from.
    """
    by_key: dict[tuple[str, str], list[int]] = {}
    for index, obs in enumerate(observations):
        by_key.setdefault((obs.kind, obs.phase), []).append(index)

    async with httpx.AsyncClient(
        base_url=MCTL_API_BASE_URL, timeout=READ_TIMEOUT_SECONDS
    ) as client:
        for (kind, phase), indexes in by_key.items():
            ids = [observations[i].entity_id for i in indexes]
            answers = await _read_batch(client, headers, kind, phase, ids)
            for i in indexes:
                obs = observations[i]
                observations[i] = _with_answer(obs, answers[obs.entity_id])


async def _read_batch(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    kind: str,
    phase: str,
    ids: list[str],
) -> dict[str, OwnershipAnswer]:
    """Every id answered, chunked. A failure answers UNKNOWN, never absent."""
    out: dict[str, OwnershipAnswer] = {}
    for start in range(0, len(ids), BATCH_CHUNK_SIZE):
        chunk = ids[start : start + BATCH_CHUNK_SIZE]
        params: list[tuple[str, str]] = [("kind", kind), ("phase", phase)]
        params += [("id", i) for i in chunk]
        path = "/api/v1/lifecycle/ownership/batch"
        try:
            resp = await client.get(path, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001 — httpx raises a wide family
            activity.logger.warning("lifecycle reconcile batch read failed: %s", exc)
            out.update(
                {i: OwnershipAnswer(verdict="unknown", reason=str(exc)) for i in chunk}
            )
            continue
        body: dict[str, Any] = {}
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:  # noqa: BLE001 — a non-JSON body is still a response
            body = {}
        # `asking=None`: the reconciler asks as nobody. A record naming this
        # sweep's own id would otherwise answer OWNED_BY_ME and fall out of the
        # "somebody holds it" arm of the table, which is the one place the
        # sweep must not treat itself specially.
        out.update(batch_answers_from(resp.status_code, body, chunk, None, path=path))
    return out


def _with_answer(
    obs: reconciler.Observation, answer: OwnershipAnswer
) -> reconciler.Observation:
    own = answer.ownership
    return reconciler.Observation(
        kind=obs.kind,
        entity_id=obs.entity_id,
        phase=obs.phase,
        proposal_ref=obs.proposal_ref,
        verdict=answer.verdict,
        state=own.state if own else "",
        owner=own.owner if own else Owner(),
        epoch=own.epoch if own else 0,
        dead=own.dead if own else False,
        stuck=own.stuck if own else False,
        healthy=own.healthy if own else False,
        derived_status=own.derived_status if own else answer.reason,
        handoff_to=own.handoff_to if own else None,
        record_version=own.entity.version if own else "",
        temporal_workflow_id=own.temporal_workflow_id if own else "",
        head=obs.head,
        entity_terminal=obs.entity_terminal,
        entity_terminal_reason=obs.entity_terminal_reason,
        live_workflow_id=obs.live_workflow_id,
        needs_owner=obs.needs_owner,
    )


async def _apply(
    obs: reconciler.Observation, decision: reconciler.Decision, me: Owner
) -> ReconcileFinding:
    """Carry out one decision, or record why it was not carried out."""
    finding = ReconcileFinding(
        kind=obs.kind,
        entity_id=obs.entity_id,
        phase=obs.phase,
        action=decision.action,
        reason=decision.reason,
        evidence=decision.evidence,
        outcome=OUTCOME_REPORTED,
        owner_type=obs.owner.type,
        owner_id=obs.owner.id,
        epoch_before=obs.epoch,
        epoch_after=obs.epoch,
        head=obs.head,
        claim_state=obs.claim_state,
    )
    if not decision.mutates:
        return finding
    if not rollout.new_answer_may_veto():
        # Below `enforce` the new answer does not decide anything (ADR-010
        # §12), and a recovery is the most consequential write in this
        # contract: it moves an entity off its owner. So at `observe` the
        # decision is recorded and not performed — which is exactly what
        # `observe` is for, and what makes the first enforce tick something an
        # operator has already read the shape of.
        return replace(finding, outcome=OUTCOME_OBSERVED)

    if decision.action == reconciler.ACTION_COMPLETE_HANDOFF:
        target = decision.to_owner or Owner()
        result = await _op(
            "handoff-complete", obs, target, epoch=decision.expected_epoch,
            reason=decision.reason,
        )
        if not result.accepted:
            return replace(finding, outcome=f"{OUTCOME_REFUSED}: {result.reason}")
        return replace(finding, outcome=OUTCOME_APPLIED, epoch_after=result.epoch)

    # ACTION_RECOVER. Everything below runs at the epoch the SERVER granted,
    # never at the one this sweep asked with: between the two writes the row
    # is ours, and asserting a stale generation would fail the follow-up with
    # a 412 and leave the row named after an actor that does no work.
    result = await _op(
        "recover", obs, me, epoch=decision.expected_epoch, evidence=decision.evidence
    )
    if not result.accepted:
        # The common refusal is 409 ErrOwnerAlive: the server re-derived
        # liveness and the owner is not dead after all. Recorded, not retried
        # — the next tick reads the row again.
        return replace(finding, outcome=f"{OUTCOME_REFUSED}: {result.reason}")
    finding = replace(finding, outcome=OUTCOME_APPLIED, epoch_after=result.epoch)

    if decision.then_finalise:
        done = await _op(
            "terminal", obs, me, epoch=result.epoch,
            reason=f"reconciler: {obs.entity_terminal_reason or 'entity terminal'}",
        )
        return replace(
            finding,
            evidence=f"{finding.evidence}; recovered and closed"
            if done.accepted
            else f"{finding.evidence}; recovered, but close was refused: {done.reason}",
        )
    if decision.then_release:
        done = await _op(
            "release", obs, me, epoch=result.epoch,
            reason="reconciler: recovered from a dead owner; free for the next actor",
        )
        return replace(
            finding,
            evidence=f"{finding.evidence}; recovered and released"
            if done.accepted
            else f"{finding.evidence}; recovered, but release was refused: {done.reason}",
        )
    return finding


async def _op(
    op: str,
    obs: reconciler.Observation,
    owner: Owner,
    *,
    epoch: int = 0,
    evidence: str = "",
    reason: str = "",
) -> OwnershipResult:
    return await perform_ownership_op(
        OwnershipRequest(
            # Both routes that MOVE a row to a new actor — recover and
            # handoff/complete — write `temporal_workflow_id` unconditionally
            # (mctl-api's ownerOptions says so in as many words), so a caller
            # that sends nothing BLANKS it and leaves a live row explaining
            # nobody. For a devloop-workflow owner the id is the workflow id by
            # construction (dev_loop.py acquires with `owner_id=info.workflow_id`
            # and `temporal_workflow_id=info.workflow_id`), and for this sweep it
            # is the reconcile execution. A shepherd owner has no workflow at
            # all, and blank is then the honest answer.
            temporal_workflow_id=(
                owner.id
                if owner.type in (OWNER_RECONCILER, OWNER_DEVLOOP_WORKFLOW)
                else ""
            ),
            op=op,
            kind=obs.kind,
            entity_id=obs.entity_id,
            phase=obs.phase,
            # The head this sweep OBSERVED, not the one on the record: the
            # field is informational on an ownership row (ADR-010 §4), and
            # writing back what the row already said would erase the one piece
            # of evidence a reader could use to see how far behind the dead
            # owner was.
            version=obs.head,
            owner_type=owner.type,
            owner_id=owner.id,
            epoch=epoch,
            evidence=evidence,
            reason=reason,
            proposal_ref=obs.proposal_ref,
        )
    )


__all__ = [
    "LifecycleReconcileResult",
    "ReconcileFinding",
    "reconcile_lifecycle_ownership",
]
