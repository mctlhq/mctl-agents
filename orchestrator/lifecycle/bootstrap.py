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

Run as a one-shot Argo Workflow. A dry run is the default posture: the report
is the deliverable, and an operator reads it against the store and live
Temporal before anything is written.

KNOWN LIMITS, written down rather than left to be rediscovered:

- **The rollout gate is an attestation, not a verification.** ``--apply``
  refuses below ``observe``, but the mode it reads is THIS process's, while the
  hazard is the Temporal worker's. Setting the variable here with the worker
  still off passes the gate. The mode is echoed into the report so what was
  claimed sits next to what it licensed.
- **Only DevLoop rows are imported.** The shepherd and pr-steward rungs were
  here and are gone: ``shadow.classify`` has one owner-type arm, so importing
  those rows turns an *agreeing* entity into ``store-forbids-old-permits``
  until the row dies at its liveness bound — and nothing in this repo
  heartbeats, progresses or releases them, because there are exactly two
  ownership writers and neither is theirs. They come back when their owners
  have real lifecycle writers.
- **A terminal pull request can be imported as active.** Discovery reads
  ``.status.yaml``, so a PR merged or closed out of band whose status still
  says ``implemented`` becomes an active row. It withholds the entity from
  nobody who wants it and ages out at its liveness bound, but it inflates
  ``store-forbids-old-permits`` during the soak. The reconcile pass is what
  corrects the status; run it first if the counts matter.

``EntityRef`` is sent with no ``version``, unlike DevLoopWorkflow's own
acquire, which carries ``head_sha``. Deliberate: version is NOT part of the
ownership key (see contract.py) — it records which version the owner last
observed, and this process observed none.
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

from orchestrator.lifecycle import policy, rollout
from orchestrator.lifecycle.client import OwnershipClient
from orchestrator.lifecycle.contract import (
    KIND_PULL_REQUEST,
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    OWNER_DEVLOOP_WORKFLOW,
    PHASE_REVIEW_REMEDIATION,
    UNKNOWN,
    EntityRef,
    Owner,
    OwnershipAnswer,
)

#: The report's decision vocabulary. Closed, like every other vocabulary in
#: this package: an operator reviewing a dry run must be able to enumerate what
#: they might see.
DECISION_ALREADY_OWNED = "already-owned"
DECISION_DEVLOOP = "devloop-workflow"
DECISION_AMBIGUOUS = "ambiguous"

#: The org devloop_workflow_id builds ids under. Checked before a bootstrap
#: writes one, because that function's hardcoded org is fail-open on a read and
#: durable on a write.
DEVLOOP_WORKFLOW_OWNER = "mctlhq"

#: Prefix on the report line. stdout carries other things -- _discover_refs
#: prints a skip notice per unowned service, and a probe thread past its budget
#: can print after the report -- so finding it must be a grep rather than a
#: guess about brace positions.
REPORT_MARKER = "lifecycle-bootstrap-report:"

DECISIONS = (
    DECISION_ALREADY_OWNED,
    DECISION_DEVLOOP,
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
            # The three provenance fields apply_report actually writes. The
            # dry run is the deliverable and an operator reads it "against the
            # store and live Temporal" — which needs policy_ref, the provenance
            # policy.py exists to record, and temporal_workflow_id, the field
            # that makes the Temporal half of that check possible at all.
            "proposal_ref": self.proposal_ref,
            "policy_ref": self.policy_ref,
            "temporal_workflow_id": self.temporal_workflow_id,
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
    #: The rollout mode this run attested to. On the record because the gate
    #: reads THIS process's environment and not the worker's.
    rollout_mode: str = ""

    def as_dict(self) -> dict[str, Any]:
        # Computed only where it is used: on the abort path there is nothing
        # to count, and a partial count is worse than none.
        # BOTH lists. Ambiguous plans land in `ambiguous` and never in
        # `planned`, so counting only the latter left counts["ambiguous"]
        # structurally 0 next to a populated list — and `total`, which the
        # soak's sample target is re-derived from, excluded them entirely.
        if self.aborted:
            # No counts at all on the abort path. build_report returns before
            # the plan loop, so `ambiguous` holds only the pre-read entries and
            # `total` would be a PARTIAL scan of a run that decided nothing —
            # and `total` is the one number the soak's sample target is
            # re-derived from. An operator scraping it out of an Argo log has
            # no reason to check `aborted` first, so it must not be there.
            return {
                "aborted": self.aborted,
                "rollout_mode": self.rollout_mode,
                "counts": None,
                "planned": [],
                "ambiguous": [p.as_dict() for p in self.ambiguous],
                "written": [],
                "failed": [],
            }
        counts = dict.fromkeys(DECISIONS, 0)
        for plan in [*self.planned, *self.ambiguous]:
            counts[plan.decision] = counts.get(plan.decision, 0) + 1
        return {
            "aborted": self.aborted,
            # The attestation, on the record next to what it licensed.
            "rollout_mode": self.rollout_mode,
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
        # run_shepherd.devloop_workflow_id, NOT a second transcription of the
        # rule: this value is written durably into temporal_workflow_id, and
        # "the id recorded is the one the probe just confirmed alive" holds
        # only while the two agree.
        from orchestrator.run_shepherd import devloop_workflow_id

        # `pr_org`, not `owner`: three other branches in this function bind
        # `owner` to an Owner, and one name holding two types works only while
        # the branches stay exclusive.
        pr_org = repo.split("/", 1)[0]
        if pr_org != DEVLOOP_WORKFLOW_OWNER:
            # devloop_workflow_id hardcodes the org, which is fail-open on the
            # probe -- a wrong owner just 404s into "not owned". On THIS path
            # the id becomes owner_id and temporal_workflow_id, so a wrong
            # owner is a durable row naming a workflow that does not exist.
            # The owner is in hand here, so check it rather than inherit a
            # justification that holds only for the read.
            base.decision = DECISION_AMBIGUOUS
            base.reason = (
                f"{repo} is not under {DEVLOOP_WORKFLOW_OWNER}; "
                "the DevLoop workflow id would name another org's workflow"
            )
            return base
        workflow_id = devloop_workflow_id(ref.service, ref.slug)
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

    # NO shepherd or pr-steward rung. Both were here and both are gone, for two
    # reasons that compound.
    #
    # They make the measurement WORSE. shadow.classify has one owner-type arm,
    # and it is devloop-workflow. An entity the old mechanism does not drive
    # reads `agree` today (no row, held False, legacy FREE). Import a shepherd
    # row and the same entity reads `store-forbids-old-permits` — until the row
    # dies at its liveness bound and it reads `agree` again, restored by the
    # row rotting rather than by anything being right. Zero reduction in the
    # dangerous class, manufactured volume in the conservative one.
    #
    # And nothing maintains them. This repo has exactly two ownership writers:
    # DevLoopWorkflow's activity, and this function. Nothing acquires,
    # progresses, heartbeats, releases or terminates a `shepherd:{service}` or
    # `pr-steward:{repo}` row — run_shepherd only ever READS the store, through
    # the shadow compare. Flipping the worker to observe makes the devloop rows
    # live; it does nothing for these. They would be durable rows for owners
    # with no lifecycle, and a re-run cannot correct them because already-owned
    # is skipped.
    #
    # The dangerous class comes from entities a live DevLoop drives and the
    # store has no live owner for. That is the rung above, and it is the whole
    # job of a pre-soak import. These two come back when their owners have real
    # writers — #57 phase 2, not this.
    base.decision = DECISION_AMBIGUOUS
    base.reason = (
        f"no live DevLoop drives this; {policy.default_owner_for(ref.service)} would own it, "
        "and that owner type has no lifecycle writer yet"
    )
    return base


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
                try:
                    answers[futures[future]] = future.result()
                except Exception as exc:  # noqa: BLE001 — see the docstring
                    # `probe` is a PARAMETER, so its totality is the caller's
                    # guarantee and not this function's. Letting a raise out
                    # would take down build_report and main() with it, and the
                    # operator would get a traceback instead of a report —
                    # while the docstring above promises an unanswered ref
                    # becomes LEGACY_UNKNOWN. Absent from the dict is exactly
                    # that, through the caller's default.
                    print(f"warn: dev-loop probe raised: {exc}")
        except FuturesTimeoutError:
            unanswered = sum(1 for f in futures if not f.done())
            print(
                f"warn: dev-loop probe hit its {DEV_LOOP_LIVENESS_BUDGET_S}s budget "
                f"with {unanswered} proposal(s) unchecked — they will be reported ambiguous"
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return answers


def _answers_in_order(answers: dict[int, str], count: int) -> list[str]:
    """probe_all's index-keyed answers as a dense list.

    Absent means the budget did not answer that ref, which is LEGACY_UNKNOWN —
    read through this default rather than written anywhere, so the
    budget-expired and never-started cases cannot drift apart.
    """
    from orchestrator.run_shepherd import LEGACY_UNKNOWN

    return [answers.get(i, LEGACY_UNKNOWN) for i in range(count)]


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

    # Probed AFTER the store read, all at once, and ONLY for the entities whose
    # decision depends on the answer.
    #
    # An entity the store already holds returns at rung 1 before the legacy
    # answer is read, so probing it buys nothing — and the budget is shared
    # wall-clock, not per-ref. On the idempotence path, where every entity is
    # already-owned, the whole 60s would go to answers nobody reads; against a
    # slow mctl-api those probes crowd out the undecided entities, which then
    # fall to UNKNOWN and are reported ambiguous. Ambiguous means left UNOWNED,
    # which is precisely the store-permits-old-forbids class this tool exists
    # to remove.
    ordered = sorted(by_entity)
    undecided = [
        entity_id
        for entity_id in ordered
        if answers[entity_id].verdict not in (OWNED_BY_OTHER, OWNED_BY_ME)
    ]
    legacy_by_entity = dict(
        zip(
            undecided,
            _answers_in_order(
                probe_all([by_entity[e][0] for e in undecided], probe), len(undecided)
            ),
            strict=True,
        )
    )

    for entity_id in ordered:
        ref, repo = by_entity[entity_id]
        # Indexed, not `.get(...) or UNOWNED`: an id missing from `answers`
        # has already aborted the run above, so a default here would never
        # fire while reading as a deliberate "treat a missing read as unowned"
        # — the exact rule the abort exists to forbid.
        answer = answers[entity_id]
        plan = plan_for(
            ref, entity_id, answer, legacy_by_entity.get(entity_id, LEGACY_UNKNOWN), repo=repo
        )
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


def _print_report(report: Report) -> None:
    """The report on stdout behind its marker, and a summary on stderr.

    ONE LINE, prefixed: see REPORT_MARKER. The summary goes out on EVERY path
    including the abort, which is the one outcome it exists for -- an Argo log
    tail should answer "did this work" without parsing anything.
    """
    print(f"{REPORT_MARKER} {json.dumps(report.as_dict())}", flush=True)
    if report.aborted:
        print(f"lifecycle-bootstrap: ABORTED: {report.aborted}", file=sys.stderr)
        return
    # `already-owned` counted apart from the work. Both live in `planned` and
    # apply_report skips the first, so the idempotent second run -- the one
    # this tool sells as writing nothing -- printed "312 planned, 0 written",
    # indistinguishable in a log tail from 312 rows the store refused.
    already = sum(1 for p in report.planned if p.decision == DECISION_ALREADY_OWNED)
    print(
        f"lifecycle-bootstrap: {len(report.planned) - already} to write, "
        f"{already} already owned, {len(report.ambiguous)} ambiguous, "
        f"{len(report.written)} written, {len(report.failed)} failed",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the planned rows. Without it nothing is written: a dry run is the default posture.",
    )
    # Scoping exists so a first --apply does not have to be fleet-wide. It
    # matters more here than on the shepherd: apply_report skips an entity the
    # store already holds, so a row written wrongly cannot be corrected by
    # re-running -- a mistake at fleet scale is a mistake to undo by hand.
    parser.add_argument("--service", default=None, help="limit to one service")
    parser.add_argument("--slug", default=None, help="limit to one proposal slug")
    args = parser.parse_args(argv)

    if args.apply and rollout.mode() != rollout.OBSERVE:
        # The one switch every other writer in this package honours. At `off`
        # the DevLoopWorkflow's own ownership activity short-circuits, so a row
        # this tool writes for a devloop-workflow owner is never heartbeated,
        # progressed or released by the workflow it names: it sits `active`
        # until its liveness bound expires, held by an owner that does not know
        # it holds anything.
        #
        # So the order is: flip the WORKER to observe (the writer goes live),
        # then bootstrap, then flip the SHEPHERD (the comparison starts against
        # a populated store). Writing first and flipping after fills the store
        # with rows nobody refreshes.
        #
        # EXACTLY observe, not records_writes(), which is at_least(OBSERVE) and
        # so also true at enforce and only. That is a SECOND hazard: at enforce
        # the shepherd is already CONSUMING the store, so a bulk import races a
        # live reader and bypasses the staged order above. A pre-soak migration
        # has no business running after the soak.
        #
        # THE LIMIT OF THIS GATE, stated because it is not obvious: the mode is
        # read from THIS process's environment, and the orphan hazard belongs
        # to the TEMPORAL WORKER, which this process cannot see. It is an
        # operator ATTESTATION, not a verification -- the WorkflowTemplate
        # makes it an explicit parameter for that reason, and the mode is
        # echoed into the report so the claim sits beside what it licensed.
        # A report on this path too. The marker exists so a consumer can always
        # find the report by grepping for it, and an Argo step reading it as an
        # output parameter gets nothing if this is the one exit that prints
        # none — a different failure from "the report says it refused". The
        # mode IS the answer here, which is what rollout_mode is for.
        _print_report(
            Report(
                aborted=(
                    f"refused --apply: {rollout.ENV_VAR}={rollout.mode()!r}, expected "
                    f"{rollout.OBSERVE!r}. Below it the owners these rows name are not "
                    "heartbeating; above it the shepherd is already reading the store "
                    "and a bulk import races it."
                ),
                rollout_mode=rollout.mode(),
            )
        )
        return 2

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
    refs = _discover_refs(
        args.state_dir,
        service_filter=args.service,
        slug_filter=args.slug,
        dry_run=True,
        fix_only=True,
    )
    client = OwnershipClient()
    report = build_report(refs, client, _dev_loop_owns_answer)
    report.rollout_mode = rollout.mode()

    if not report.aborted and args.apply:
        apply_report(report, client)

    _print_report(report)
    if report.aborted or report.failed:
        return 1
    # A run that classified nothing, or an --apply that wrote nothing, must not
    # look like a successful import.
    #
    # The likeliest way to import nothing is SILENT: with MCTL_TOKEN unset the
    # probe answers LEGACY_UNKNOWN for every ref and prints nothing at all
    # ("never asked"), so every plan is ambiguous, `planned` is empty, `failed`
    # is empty, and the Argo step goes green having done nothing. A checkout
    # mounted one level off reads the same.
    if not report.planned and not report.ambiguous:
        print("lifecycle-bootstrap: discovered no proposals at all", file=sys.stderr)
        return 1
    if args.apply and not report.planned:
        print(
            f"lifecycle-bootstrap: --apply had nothing to write; "
            f"{len(report.ambiguous)} entit(ies) were ambiguous",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
