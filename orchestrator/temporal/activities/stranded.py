"""Activity: find `accepted` proposals no live DevLoopWorkflow owns.

mctl-agents#412. The approve flip (`proposed -> accepted`) is a side effect
of DevLoopWorkflow's own implement step, never a trigger: any OTHER approval
path — the standalone `mctl_trigger_approve` / `mctl-agents-approve`, or the
incident responder writing `status: accepted` directly — produces an
`accepted` proposal with no owner and nothing that promotes it further.

A proposal is "stranded" when it is `accepted` AND none of the following
hold, checked in this order (design.md §1):

  1. it carries a `pr:` — that is `detect_orphans`' and the shepherd's case;
  2. its `attempt` lease has not expired — an implementer run holds it;
  3. it is unrunnable as written (`unrunnable_reason` / a `blocked` marker) —
     a submit here could only refuse (#349);
  4. it carries no execution authorization — `execution_authorization` finds
     no non-anonymous `approval.approved_by`. An absent `control` block only
     means "approval was never required" (a write-time default), not "someone
     authorized this to run", so it is never read as consent. Provenance of
     the WRITER is deliberately not part of this: a writer allowlist was tried
     and rejected by the 2026-09-19 product decision on mctl-agents#412, and
     `ProposalStateRef` no longer carries `updated_by` at all (see the block
     comment below);
  5. its `updated_at` is inside the stranding grace period — a DevLoopWorkflow
     may be between its approve flip and its own implement submit;
  6. its derived DevLoop workflow id is in the caller's active set — a live
     loop already owns it.

Fail-CLOSED on an unknown active set: unlike `detect_orphans` (whose
projection write is harmless without it), the active set here IS the safety
argument against double-running the implementer, so the caller must not call
this activity at all when it cannot be trusted — see
`implement_sweep.ImplementSweepWorkflow`, which skips the whole tick rather
than pass through an empty list that would otherwise read as "no active
loops anywhere".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from temporalio import activity

from orchestrator.temporal.activities.gitops_state import ProposalStateRef, list_proposal_refs
from orchestrator.temporal.activities.orphans import expected_dev_loop_id

# The sweep submits execution on records nobody is watching, so it fails
# closed on authorization: a proposal is swept only when something explicitly
# authorized executing it (`proposal_state.execution_authorization`).
#
# A writer allowlist (`updated_by: _incident-responder` ⇒ trusted) was tried
# here and rejected by the 2026-09-19 product decision on #412. Provenance of
# who CREATED an auto-accepted record is not human authorization to execute
# it — an agent's own auto-accept cannot authorize the agent's own execution,
# and an allowlist keyed on it is a loop, not a gate. Missing `control` /
# `approval` metadata must never read as "approval not required" on this
# path, whatever it means at write time.
#
# The 69 legacy `incident-*` proposals that have sat in `accepted` since
# August are exactly this shape. They are diagnostic artifacts, not approved
# work: they are quarantined from execution here and reported for human
# triage, with their requirements/design/tasks preserved and untouched.


@dataclass(frozen=True)
class StrandedProposal:
    service: str
    slug: str
    updated_at: str
    reason: str


@dataclass(frozen=True)
class StrandedScanResult:
    total_accepted: int
    stranded: list[StrandedProposal]
    # (service/slug, why it was skipped) for every accepted proposal that is
    # NOT stranded — one entry per filter above that matched.
    skipped: list[tuple[str, str]]
    # The subset of `skipped` that carries no execution authorization at all:
    # quarantined from execution and awaiting human triage, NOT a clean skip.
    # Reported separately because a count that a human can see is the whole
    # difference between "quarantined" and "silently dropped every 15 minutes
    # forever" (mctl-agents#412 product decision, point 3).
    unauthorized: list[tuple[str, str]] = field(default_factory=list)


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse, `Z`-suffixed or not; None on anything else.

    Mirrors run_shepherd._attempt_is_fresh's own parse: an unparseable
    timestamp is treated as absent rather than raised, so one hand-edited
    `.status.yaml` cannot take the whole scan down.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # A naive result (no offset in `value`) would raise on comparison
    # against the aware `now` both call sites use — assume UTC rather than
    # let one offset-less `.status.yaml` timestamp take the scan down.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _scan(
    refs: list[ProposalStateRef],
    active_workflow_ids: set[str],
    grace_minutes: int,
    now: datetime,
) -> StrandedScanResult:
    accepted = [r for r in refs if r.status == "accepted"]
    stranded: list[StrandedProposal] = []
    skipped: list[tuple[str, str]] = []
    unauthorized: list[tuple[str, str]] = []

    for ref in accepted:
        key = f"{ref.service}/{ref.slug}"

        if ref.pr_url:
            skipped.append((key, "carries a pr: url; left to detect_orphans and the shepherd"))
            continue

        expires_at = _parse_iso(ref.attempt_expires_at)
        if expires_at is not None and expires_at > now:
            skipped.append((key, "unexpired attempt lease; an implementer run holds it"))
            continue

        if ref.unrunnable:
            skipped.append((key, "unrunnable as written (mctl-agents#349, approval-missing)"))
            continue
        if ref.blocked:
            skipped.append((key, "a blocked marker is present"))
            continue

        # Deliberately AFTER `unrunnable`/`blocked`: those two are already
        # durable, human-visible markers, so re-reporting them here as
        # "unauthorized" would bury the records that carry no marker at all —
        # which are the ones this census exists to surface.
        if ref.execution_authorization is None:
            # The reason comes from the record, not from this call site: a
            # never-approved record and one a human approved through a path
            # that dropped the approver identity both land here, and an
            # operator triaging the quarantine acts on them differently.
            reason = ref.unauthorized_reason or "no explicit execution authorization"
            unauthorized.append((key, reason))
            skipped.append((key, f"no explicit execution authorization ({reason})"))
            continue

        updated_at = _parse_iso(ref.updated_at)
        if updated_at is not None and now - updated_at < timedelta(minutes=grace_minutes):
            skipped.append(
                (key, f"updated within the {grace_minutes}-minute stranding grace period")
            )
            continue

        expected_id = expected_dev_loop_id(ref.slug, None, ref.service)
        if expected_id and expected_id in active_workflow_ids:
            skipped.append((key, f"owned by the live DevLoopWorkflow {expected_id}"))
            continue

        stranded.append(
            StrandedProposal(
                service=ref.service,
                slug=ref.slug,
                updated_at=ref.updated_at or "",
                reason="accepted, no PR, no live DevLoopWorkflow",
            )
        )

    return StrandedScanResult(
        total_accepted=len(accepted),
        stranded=stranded,
        skipped=skipped,
        unauthorized=unauthorized,
    )


@activity.defn
async def find_stranded_accepted(
    active_workflow_ids: list[str], grace_minutes: int
) -> StrandedScanResult:
    """`accepted` proposals with no PR, no fresh attempt and no owning loop.

    Reads gitops over the GitHub API (`list_proposal_refs`, no clone — the
    worker has none by design), the same read `detect_orphans` performs.
    Raises on a listing failure rather than returning an empty result: the
    caller (`ImplementSweepWorkflow`) treats that as "unknown", not "clean",
    the same distinction the visibility query's own failure gets one call
    earlier.
    """
    refs = await list_proposal_refs()
    result = _scan(refs, set(active_workflow_ids), grace_minutes, datetime.now(UTC))
    activity.logger.info(
        "implement-sweep: %d accepted proposal(s), %d stranded, %d skipped, "
        "%d quarantined unauthorized",
        result.total_accepted,
        len(result.stranded),
        len(result.skipped),
        len(result.unauthorized),
    )
    for key, reason in result.unauthorized:
        activity.logger.warning(
            "UNAUTHORIZED %s: %s — quarantined from execution, needs human triage",
            key,
            reason,
        )
    return result
