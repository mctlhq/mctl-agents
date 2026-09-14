"""Import the pull requests already in flight into the ownership store.

The soak compares two mechanisms. Before it starts, the store has to hold a
row for every entity the old mechanism already knows about — otherwise every
comparison reads `store-permits-old-forbids`, the one DANGEROUS class, and the
measurement is swamped by its own migration.

Why this lives in mctl-agents and not behind an mctl-api endpoint: the import
rules need `.status.yaml` from the gitops checkout, `_service_mode` /
`SHEPHERD_SKIP_SERVICES`, and the `pr_url` fallback. None of those exist in
mctl-api, and moving them there would put a second copy of the shepherd's
policy in a repository that cannot see the state it applies to.

Run as a one-shot Argo Workflow. ``--dry-run`` is the default posture: the
report is the deliverable, and an operator reads it against the store and live
Temporal before anything is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.lifecycle import policy
from orchestrator.lifecycle.client import OwnershipClient
from orchestrator.lifecycle.contract import (
    KIND_PULL_REQUEST,
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    OWNER_DEVLOOP_WORKFLOW,
    OWNER_PR_STEWARD,
    OWNER_SHEPHERD,
    PHASE_REVIEW_REMEDIATION,
    UNKNOWN,
    UNOWNED,
    EntityRef,
    Owner,
    OwnershipAnswer,
)

#: The report's decision vocabulary. Closed, like every other vocabulary in
#: this package: an operator reviewing a dry run must be able to enumerate what
#: they might see.
DECISION_ALREADY_OWNED = "already-owned"
DECISION_DEVLOOP = "devloop-workflow"
DECISION_PR_STEWARD = "pr-steward"
DECISION_SHEPHERD = "shepherd"
DECISION_AMBIGUOUS = "ambiguous"

DECISIONS = (
    DECISION_ALREADY_OWNED,
    DECISION_DEVLOOP,
    DECISION_PR_STEWARD,
    DECISION_SHEPHERD,
    DECISION_AMBIGUOUS,
)


@dataclass
class Plan:
    """One entity's import decision, before anything is written."""

    entity_id: str = ""
    service: str = ""
    slug: str = ""
    decision: str = DECISION_AMBIGUOUS
    owner_type: str = ""
    owner_id: str = ""
    reason: str = ""
    proposal_ref: str = ""
    policy_ref: str = ""
    temporal_workflow_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "service": self.service,
            "slug": self.slug,
            "decision": self.decision,
            "owner_type": self.owner_type,
            "owner_id": self.owner_id,
            "reason": self.reason,
        }


@dataclass
class Report:
    planned: list[Plan] = field(default_factory=list)
    #: Entities left UNOWNED because no rule in the ladder applied.
    #:
    #: ADR-010 §4 asks bootstrap to write these as a `conflicted` state.
    #: types.py defines four states and says there is deliberately no
    #: `conflicted`: it would be a DERIVED condition needing something to sweep
    #: and write it, which is the scheduler this design refuses to become. So
    #: the ambiguity is reported to the operator and the row is left alone --
    #: an unowned entity is one the old mechanism still drives, which is the
    #: status quo rather than a new risk.
    ambiguous: list[Plan] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    aborted: str = ""

    def as_dict(self) -> dict[str, Any]:
        # BOTH lists. Ambiguous plans land in `ambiguous` and never in
        # `planned`, so counting only the latter left counts["ambiguous"]
        # structurally 0 next to a populated list — and `total`, which the
        # soak's sample target is re-derived from, excluded them entirely.
        counts = dict.fromkeys(DECISIONS, 0)
        for plan in [*self.planned, *self.ambiguous]:
            counts[plan.decision] = counts.get(plan.decision, 0) + 1
        return {
            "aborted": self.aborted,
            # `total` is the measured decision rate the soak's sample target is
            # re-derived from: the ADR's floor of 200 comparisons is a floor on
            # the wrong axis, since the shepherd re-evaluates the same ref
            # roughly 96 times a day.
            "counts": {**counts, "total": len(self.planned) + len(self.ambiguous)},
            "planned": [p.as_dict() for p in self.planned],
            "ambiguous": [p.as_dict() for p in self.ambiguous],
            "written": self.written,
            "failed": self.failed,
        }


def owner_for(decision: str, *, service: str, repo: str) -> Owner:
    """The owner a decision names.

    Covers the two POLICY-derived owners only. The devloop-workflow owner is
    built in `plan_for` instead, because its id comes from the slug rather than
    from policy — the one thing this function cannot answer from `service` and
    `repo` alone.

    Ids are DETERMINISTIC -- `shepherd:{service}`, never a pod name or a
    timestamp. Idempotence rests on two legs and this is the second one: the
    read-first pass skips entities that already have a row, and a deterministic
    id means a re-run that races that pass writes the same owner rather than
    colliding with itself and answering 409 for every entity.
    """
    if decision == DECISION_PR_STEWARD:
        return Owner(type=OWNER_PR_STEWARD, id=f"pr-steward:{repo}")
    if decision == DECISION_SHEPHERD:
        return Owner(type=OWNER_SHEPHERD, id=f"shepherd:{service}")
    raise ValueError(f"decision {decision!r} names no policy-derived owner")


def plan_for(
    ref,
    entity_id: str,
    answer: OwnershipAnswer,
    legacy_answer: str,
    *,
    repo: str,
) -> Plan:
    """The import ladder for one entity.

    Order matters and each rung is a different question:

      1. the store already holds it -- nothing to do, and this is what makes a
         re-run write nothing;
      2. a live DevLoopWorkflow drives it -- the workflow is the owner, and
         recording anything else would name a holder that is not the one
         actually pushing;
      3. the service is skipped by the shepherd -- its PRs belong to another
         lifecycle entirely (pr-steward today);
      4. a proposal directory and a pull request -- the shepherd;
      5. anything else is AMBIGUOUS and is left unowned.

    `legacy_answer` is the tri-state probe, not the bool: a probe that failed
    must not be read as "no DevLoop is driving this", or rung 2 falls through
    to rung 4 and the bootstrap hands the shepherd a pull request another
    machine is pushing to -- manufacturing the exact condition the store exists
    to prevent.
    """
    from orchestrator.run_shepherd import LEGACY_OWNED, LEGACY_UNKNOWN

    base = Plan(
        entity_id=entity_id,
        service=ref.service,
        slug=ref.slug,
        proposal_ref=f"{ref.service}/{ref.slug}",
        policy_ref=policy.policy_ref_for(ref.service),
    )

    if answer.verdict in (OWNED_BY_OTHER, OWNED_BY_ME):
        base.decision = DECISION_ALREADY_OWNED
        owner = answer.ownership.owner if answer.ownership else Owner()
        base.owner_type, base.owner_id = owner.type, owner.id
        base.reason = "the store already holds this entity phase"
        return base

    if legacy_answer == LEGACY_UNKNOWN:
        base.decision = DECISION_AMBIGUOUS
        base.reason = "the DevLoop liveness probe could not answer; owner undetermined"
        return base

    if legacy_answer == LEGACY_OWNED:
        workflow_id = _devloop_workflow_id(ref)
        if not workflow_id:
            # Unreachable from the probe — a slug with no issue-<N>- prefix
            # answers LEGACY_FREE — but an empty owner id must never become a
            # row. dev_loop.py guards on `bool(result.owner_id)` precisely
            # because a record owned by nobody withholds the entity from
            # everyone and names no one to ask.
            base.decision = DECISION_AMBIGUOUS
            base.reason = (
                f"a live DevLoop is reported for {ref.slug!r}, whose slug yields no workflow id"
            )
            return base
        base.decision = DECISION_DEVLOOP
        base.owner_type = OWNER_DEVLOOP_WORKFLOW
        # The workflow id is the one the DevLoop writes for itself, so a row
        # written here names the same owner the workflow would have named —
        # and carries it in temporal_workflow_id too, as the workflow's own
        # acquire does.
        base.owner_id = workflow_id
        base.temporal_workflow_id = workflow_id
        base.reason = "a live DevLoopWorkflow is driving this pull request"
        return base

    if policy.default_owner_for(ref.service) == OWNER_PR_STEWARD:
        base.decision = DECISION_PR_STEWARD
        owner = owner_for(DECISION_PR_STEWARD, service=ref.service, repo=repo)
        base.owner_type, base.owner_id = owner.type, owner.id
        base.reason = f"{ref.service} is skipped by the shepherd; its PRs belong to pr-steward"
        return base

    if ref.proposal_dir is not None and ref.pr_url:
        base.decision = DECISION_SHEPHERD
        owner = owner_for(DECISION_SHEPHERD, service=ref.service, repo=repo)
        base.owner_type, base.owner_id = owner.type, owner.id
        base.reason = "a proposal with an open pull request and no live DevLoop"
        return base

    base.decision = DECISION_AMBIGUOUS
    base.reason = "no rule in the ladder applied"
    return base


def _devloop_workflow_id(ref) -> str:
    """The workflow id a DevLoop would use for this proposal.

    Derived the way start.py derives it, from the slug -- the same rule
    run_shepherd's liveness probe applies, so the id recorded here is the one
    the probe just confirmed alive.
    """
    import re

    m = re.match(r"issue-(\d+)-", ref.slug)
    if not m:
        # Unreachable from the ladder: a slug without the prefix answers
        # LEGACY_FREE from the probe and never reaches the DevLoop rung.
        return ""
    return f"dev-loop-mctlhq-{ref.service}-{m.group(1)}"


def probe_all(refs, probe) -> dict[int, str]:
    """The DevLoop liveness answer for every ref, concurrently and bounded.

    Serially this is one HTTP call per proposal at the probe's own 10s timeout,
    and the whole set runs BETWEEN the store read and the decisions made from
    it. That gap is where a bootstrap goes wrong: the longer it is, the more
    likely the store moved under a decision already taken. The same pool and
    the same wall-clock budget the sweep uses keep it independent of how many
    proposals are open.

    A ref the budget did not answer is LEGACY_UNKNOWN — read as the dict's
    default rather than written — so it reaches the ladder as ambiguous and is
    left unowned. An unanswered probe must not become a decision.
    """
    from orchestrator.run_shepherd import (
        DEV_LOOP_LIVENESS_BUDGET_S,
        DEV_LOOP_LIVENESS_WORKERS,
    )

    answers: dict[int, str] = {}
    if not refs:
        return answers
    pool = ThreadPoolExecutor(max_workers=min(DEV_LOOP_LIVENESS_WORKERS, len(refs)))
    try:
        futures = {
            pool.submit(probe, ref.service, ref.slug): i for i, ref in enumerate(refs)
        }
        try:
            for future in as_completed(futures, timeout=DEV_LOOP_LIVENESS_BUDGET_S):
                answers[futures[future]] = future.result()
        except FuturesTimeoutError:
            unanswered = sum(1 for f in futures if not f.done())
            print(
                f"warn: dev-loop probe hit its {DEV_LOOP_LIVENESS_BUDGET_S}s budget "
                f"with {unanswered} proposal(s) unchecked — they will be reported ambiguous"
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return answers


def build_report(refs, client: OwnershipClient, probe) -> Report:
    """Read the store, then decide. Never writes.

    READ FIRST, and any UNKNOWN aborts the whole run with nothing written. An
    id that read UNKNOWN is one whose owner cannot be seen, and writing an
    owner over a row you could not read is how a bootstrap takes an entity from
    whoever actually holds it. Aborting costs a re-run; the alternative costs
    an entity.
    """
    from orchestrator.run_shepherd import LEGACY_UNKNOWN, _parse_pr_url

    report = Report()
    by_entity: dict[str, tuple[Any, str]] = {}

    for ref in refs:
        try:
            owner_name, repo_name, number = _parse_pr_url(ref.pr_url or "")
        except Exception as exc:  # noqa: BLE001 — an unreadable URL is not a crash
            report.ambiguous.append(
                Plan(
                    service=ref.service,
                    slug=ref.slug,
                    decision=DECISION_AMBIGUOUS,
                    reason=f"no pull request entity id: {exc}",
                )
            )
            continue
        entity_id = EntityRef.for_pull_request(f"{owner_name}/{repo_name}", int(number)).id
        # Two proposals can point at one pull request. First wins and the
        # second is reported rather than silently overwriting the decision --
        # which would make the import depend on directory ordering.
        if entity_id in by_entity:
            report.ambiguous.append(
                Plan(
                    entity_id=entity_id,
                    service=ref.service,
                    slug=ref.slug,
                    decision=DECISION_AMBIGUOUS,
                    reason=f"a second proposal maps to {entity_id}",
                )
            )
            continue
        by_entity[entity_id] = (ref, f"{owner_name}/{repo_name}")

    if not by_entity:
        return report

    answers = client.get_many(
        KIND_PULL_REQUEST, PHASE_REVIEW_REMEDIATION, sorted(by_entity), asking=None
    )
    unknown = sorted(
        entity_id
        for entity_id in by_entity
        if (answers.get(entity_id) or OwnershipAnswer()).verdict == UNKNOWN
    )
    if unknown:
        report.aborted = (
            f"{len(unknown)} entit(ies) read UNKNOWN from the store "
            f"(first: {unknown[0]}); nothing was written"
        )
        return report

    # Probed AFTER the store read and all at once, so the read-then-decide gap
    # is one budget rather than one timeout per proposal.
    ordered = sorted(by_entity)
    probe_refs = [by_entity[entity_id][0] for entity_id in ordered]
    legacy = probe_all(probe_refs, probe)

    for i, entity_id in enumerate(ordered):
        ref, repo = by_entity[entity_id]
        answer = answers.get(entity_id) or OwnershipAnswer(verdict=UNOWNED)
        plan = plan_for(ref, entity_id, answer, legacy.get(i, LEGACY_UNKNOWN), repo=repo)
        if plan.decision == DECISION_AMBIGUOUS:
            report.ambiguous.append(plan)
        else:
            report.planned.append(plan)
    return report


def apply_report(report: Report, client: OwnershipClient) -> Report:
    """Write the rows the report plans. Only the writable decisions."""
    for plan in report.planned:
        if plan.decision in (DECISION_ALREADY_OWNED, DECISION_AMBIGUOUS):
            continue
        answer = client.acquire(
            EntityRef(kind=KIND_PULL_REQUEST, id=plan.entity_id),
            PHASE_REVIEW_REMEDIATION,
            Owner(type=plan.owner_type, id=plan.owner_id),
            proposal_ref=plan.proposal_ref,
            policy_ref=plan.policy_ref,
            temporal_workflow_id=plan.temporal_workflow_id,
        )
        if answer.wrote:
            report.written.append(plan.entity_id)
        else:
            report.failed.append(
                {"entity_id": plan.entity_id, "verdict": answer.verdict, "reason": answer.reason}
            )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the planned rows. Without it nothing is written: a dry run is the default posture.",
    )
    args = parser.parse_args(argv)

    from orchestrator.run_shepherd import _dev_loop_owns_answer, _discover_refs

    # dry_run=True ALWAYS, including under --apply. _discover_refs rewrites
    # .status.yaml when it finds a PR by branch — flipping in-progress to
    # implemented — and this tool writes ownership rows, never gitops files. A
    # "dry run" that edited the checkout before the store was even read would
    # contradict every other guarantee here, the `aborted` path's "nothing was
    # written" included.
    #
    # fix_only=True is how the SKIPPED services get discovered at all. Without
    # it _discover_refs drops every service in SHEPHERD_SKIP_SERVICES, which is
    # exactly the set the pr-steward rung exists for — the rung, and the
    # pr-steward arm of owner_for, would be dead code and counts["pr-steward"]
    # would read 0 for a reason the report does not show. The override affects
    # DISCOVERY only: policy.default_owner_for re-reads the real service mode,
    # so a skipped service still answers pr-steward in the ladder.
    refs = _discover_refs(args.state_dir, dry_run=True, fix_only=True)
    client = OwnershipClient()
    report = build_report(refs, client, _dev_loop_owns_answer)

    if report.aborted:
        print(json.dumps(report.as_dict(), indent=2))
        return 1
    if args.apply:
        apply_report(report, client)
    print(json.dumps(report.as_dict(), indent=2))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
