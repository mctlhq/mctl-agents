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
  refuses outside ``observe``, but the mode it reads is THIS process's, while
  the hazard is the Temporal worker's. Setting the variable here with the
  worker still off passes the gate. The mode is echoed into the report so what
  was claimed sits next to what it licensed.
- **The probe cannot see the second patch marker.** ENFORCED, not described:
  ``may_apply`` refuses every write while ``OWNERSHIP_CAPABILITY_KNOWN`` is
  False, and the only way past it is an explicit, scoped
  ``--assume-tracked-ownership REASON`` whose justification goes on the report.
  ``LEGACY_OWNED`` means ``shepherd_in_loop is
  True``, i.e. ``workflow.patched("shepherd-in-loop")`` plus the pinned-image
  check. But ``dev_loop.py`` gates every ownership call on ``shepherd_in_loop
  AND workflow.patched("lifecycle-ownership")`` — a SECOND, independent marker.
  An execution recorded between the two replays the second as False forever:
  it answers ``shepherd_in_loop=True`` to the probe, rung 3 plans a
  ``devloop-workflow`` row, and ``_ownership()`` is never called for it. No
  acquire, no heartbeat, no release — the orphan the rollout gate exists to
  prevent, reached through the probe instead of the mode.

  It is NOT the gap the bullet above describes. That one is operator-fixable:
  flip the worker and it closes. A patch marker is per-execution history and
  permanent for the life of that execution, and the cohort is largest exactly
  when a pre-soak migration runs, against watches that stay Running up to
  ``MERGE_WATCH_DEADLINE``.

  The probe cannot distinguish it today: ``/api/v1/agents/dev-loop/{id}``
  returns ``status``, ``shepherd_in_loop`` and ``shepherd_in_loop_known``, and
  neither the ``lifecycle_claim`` query (``dev_loop.py``, the reader that
  answers this directly) nor the execution's start time is exposed. So the fix
  is an mctl-api field, and until it lands the writes are REFUSED rather than
  discouraged. Scope is not what licenses one: narrowing a run does not make an
  unheartbeated row safe, it makes fewer of them.

  The dependency order, so it is not rediscovered: this module's planner
  (mctlhq/mctl-agents#384), then the entity-id contract across all three
  builders (#386), then the API capability (mctlhq/mctl-api#322), and only then
  a fleet-wide apply.
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
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.lifecycle import rollout, shadow
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


#: Prefix on the report line. stdout carries other things -- _discover_refs
#: prints a skip notice per unowned service, and a probe thread past its budget
#: can print after the report -- so finding it must be a grep rather than a
#: guess about brace positions.
REPORT_MARKER = "lifecycle-bootstrap-report:"

#: What goes on a row's ``policy_ref``: the policy that decided THIS row.
#:
#: NOT ``policy.policy_ref_for``, which answers `service-mode:{service}={mode}`
#: off ``run_shepherd._service_mode`` and so encodes the SHEPHERD_SKIP_SERVICES
#: / SHEPHERD_FIX_ONLY_SERVICES of whatever pod happened to run the import. The
#: docstring on that function says the field exists so an operator need not
#: reconstruct the environment the deciding process ran in — recording an
#: unset-by-default env var from a one-shot migration pod does exactly that,
#: and durably: a row for `mctl-claude-remote` would claim
#: `service-mode:mctl-claude-remote=full` while the shepherd resolves it to
#: `skip`, and `full` is the mode that means discover, fix AND merge.
#:
#: The service mode did not decide this row. The ladder did, on one measured
#: fact — a DevLoopWorkflow confirmed RUNNING for the proposal — and that fact
#: is the same whatever any pod's environment says. So the field names the
#: ladder, which is true from every pod.
#:
#: The service mode is not lost: the pod's skip set is recorded once on the
#: report (``Report.skip_services``), which is where a process-wide input
#: belongs, rather than stamped onto every row as if it were per-entity
#: provenance.
POLICY_REF = "lifecycle-bootstrap:devloop-live"

#: Wall-clock budget for the whole liveness probe pass.
#:
#: NOT the sweep's DEV_LOOP_LIVENESS_BUDGET_S, and the difference is the point.
#: That budget is 60s because the sweep runs every five minutes and an
#: unanswered ref is FREE there -- the shepherd fails open for that proposal
#: and asks again on the next tick. Here an unanswered ref is LEGACY_UNKNOWN,
#: which is undetermined, which makes the run red. Inheriting a bound sized for
#: "we will ask again shortly" into a one-shot migration turns budget
#: exhaustion into the expected outcome on a large fleet: --apply writes what
#: it could, exits 1, and the re-run races the same clock.
#:
#: This runs ONCE, so it gets a bound sized for finishing rather than for
#: protecting a cadence.
#:
#: It is a bound on the PROBE PASS, not on the run, and the difference matters
#: when sizing the WorkflowTemplate's activeDeadlineSeconds (900s today). That
#: deadline has to cover the clone, DISCOVERY, the store read and then this.
#: Discovery is the unbounded one: `_discover_refs` shells out to `gh pr list`
#: per proposal, serially, with no budget of its own -- on a fleet-wide run
#: that is the largest term in the sum and nothing here constrains it. So
#: raising this constant is not free: the deadline has to move with it, and
#: the headroom left over is what discovery gets.
PROBE_BUDGET_S = 300

#: Concurrency for the same pass, sized from the same arithmetic.
#:
#: Moving the budget off the sweep's and leaving the concurrency on it half
#: solved the problem: throughput is workers x budget / timeout, so at the
#: sweep's 8 workers a degraded mctl-api answering at the 10s probe timeout
#: caps this pass at 8*300/10 = 240 refs however long the budget is.
#: Everything past that reads LEGACY_UNKNOWN, which is undetermined, which is
#: red — and restoring fix_only put the skipped services back in that
#: denominator.
#:
#: 16 puts the degraded ceiling at 16*300/10 = 480 refs, and far higher in the
#: ordinary case. Not raised further because the far side is a single mctl-api
#: and this is a read-only probe against it, not a reason to double the
#: sweep's steady-state pressure on a one-shot run.
#:
#: DEV_LOOP_LIVENESS_WORKERS is deliberately not reused, for the same reason
#: DEV_LOOP_LIVENESS_BUDGET_S is not: that number was chosen for a five-minute
#: cadence where an unanswered ref is free.
PROBE_WORKERS = 16

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
    #: AMBIGUOUS for lack of an ANSWER rather than because of one.
    #:
    #: `main()` branches the exit code on this, so the full list of producers
    #: belongs here:
    #:
    #:   - rung 2 -- the probe could not speak, or could not be ASKED. The
    #:     first is TRANSIENT and a re-run may fix it; the second (a service
    #:     directory that is not a usable repository name, so no workflow id
    #:     exists to ask about) is PERMANENT, which is why `retryable` is set
    #:     from `id_error` there rather than unconditionally;
    #:   - rung 3's two refusals -- a live DevLoop is CONFIRMED and we cannot
    #:     name it, because the pull request is in another repository or the
    #:     slug yields no workflow id. These leave the dangerous class in place
    #:     and are PERMANENT until `.status.yaml` is corrected;
    #:   - an unreadable or non-pull-request `pr:` (PERMANENT, same reason);
    #:   - two proposals mapping to one pull request, where the run cannot tell
    #:     which is authoritative.
    #:
    #: Rung 4 is the one ambiguity that is NOT undetermined: no live DevLoop
    #: drives the entity and the owner that would has no writer yet. That is
    #: the ordinary outcome for most of the fleet, and a run made entirely of
    #: it wrote nothing because there was nothing to write.
    undetermined: bool = False
    #: Whether a re-run could plausibly answer differently.
    #:
    #: A FIELD, not a substring of `reason`. Deriving it from the prose is what
    #: `undetermined` itself was introduced to stop doing, and the prose is not
    #: even a reliable source: the one refusal an operator cannot fix in
    #: `.status.yaml` is a slug that yields no workflow id, where the fix is
    #: renaming the proposal directory.
    #:
    #: Rung 2 is retryable WHEN the probe was actually asked and could not
    #: answer. It is not when the probe could not be asked at all -- a service
    #: directory that is not a usable repository name yields no workflow id, so
    #: `_dev_loop_owns_answer` answers UNKNOWN without an HTTP call and the
    #: next run over the same checkout does the same. That case arrives at rung
    #: 2 too, which is why the field is set from `id_error` there rather than
    #: unconditionally.
    #:
    #: Every other producer is a data problem in the checkout that the same run
    #: reproduces exactly.
    retryable: bool = False
    #: Whether the entity id is the one every other component will build.
    #:
    #: A conjunct of `may_apply`, not a report ornament. Rung 3 compares the
    #: pull request's repository with `casefold`, and `build_report` records
    #: the `pr:` spelling verbatim -- as `PRState.repo` and
    #: `shadow.entity_id_for_pr` also do, which is why they agree today. But
    #: "agree today" is an accident, not a contract (mctlhq/mctl-agents#386),
    #: and the entity this makes durable is precisely the one whose id was
    #: hand-typed in a casing nothing else will reproduce.
    #:
    #: EXACT, not case-insensitive: the case-insensitive comparison is what
    #: lets the entity through the ladder, and this is what stops a write
    #: turning that tolerance into a row.
    entity_id_exact: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "service": self.service,
            "slug": self.slug,
            "decision": self.decision,
            "owner_type": self.owner_type,
            "owner_id": self.owner_id,
            "reason": self.reason,
            "undetermined": self.undetermined,
            "retryable": self.retryable,
            "entity_id_exact": self.entity_id_exact,
            # The three provenance fields apply_report writes. The dry run is
            # the deliverable and an operator reads it "against the store and
            # live Temporal" — which needs policy_ref, the provenance
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
    #: Writable plans `may_apply` refused, with the clause that refused them.
    #:
    #: Apart from `failed`, which is the store declining a write this tool
    #: attempted. These were never attempted, and the distinction is the one an
    #: operator acts on: a refusal here is a gate in this repository, fixed by
    #: landing the issue it names, while a failure is the store's answer about
    #: that entity.
    blocked: list[dict[str, str]] = field(default_factory=list)
    #: The operator's justification for overriding `may_apply`'s capability
    #: clause, verbatim. Empty when none was given.
    #:
    #: On the record because an override is a claim — "I checked these in
    #: Temporal by hand" — and a claim that licenses durable writes belongs
    #: next to what it licensed, for the same reason `rollout_mode` does.
    ownership_override: str = ""
    aborted: str = ""
    #: The rollout mode this run attested to. On the record because the gate
    #: reads THIS process's environment and not the worker's.
    rollout_mode: str = ""
    #: The skip set this pod saw, and whether discovery was made independent of
    #: it. Recorded for the same reason as rollout_mode: both are load-bearing
    #: inputs read from an environment the report's reader cannot see, and a
    #: skip set wider than the shepherd's would silently drop entities a live
    #: DevLoop drives.
    skip_services: str = ""
    discovery_ignored_skip_set: bool = False
    #: Whether this run was asked to WRITE. The posture, not the outcome.
    #:
    #: The load-bearing process input this half adds, and it belongs beside
    #: rollout_mode and skip_services for the identical reason: a reader of the
    #: report cannot otherwise see it. Without it a dry run and an apply that
    #: wrote nothing are byte-identical artifacts -- same `0 written, 0
    #: failed`, same empty lists, same exit 0 -- so a regression in the
    #: WorkflowTemplate's dry_run -> --apply plumbing is silent, and the
    #: reviewed dry run the merge order depends on carries no field saying
    #: which posture produced it.
    applied: bool = False

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
                "skip_services": self.skip_services,
                "discovery_ignored_skip_set": self.discovery_ignored_skip_set,
                "applied": self.applied,
                "counts": None,
                # THE FIELD, not a literal. Empty today because build_report
                # returns before the plan loop, but a literal is a field that
                # cannot contradict the code, which is the one thing a report
                # is for.
                "planned": [p.as_dict() for p in self.planned],
                # NOT empty, and never was: build_report appends the pre-read
                # entries -- an unreadable `pr:`, a duplicate mapping -- before
                # it can abort, which is what the comment above this branch
                # says about `total`.
                "ambiguous": [p.as_dict() for p in self.ambiguous],
                # Empty today -- `main` gates the write on `not
                # report.aborted` -- but if a future path ever writes before
                # aborting, the report must be able to say so rather than be
                # structurally incapable of it.
                "written": self.written,
                "failed": self.failed,
                "blocked": self.blocked,
                "ownership_override": self.ownership_override,
            }
        counts = dict.fromkeys(DECISIONS, 0)
        for plan in [*self.planned, *self.ambiguous]:
            counts[plan.decision] = counts.get(plan.decision, 0) + 1
        return {
            "aborted": self.aborted,
            # The attestation, on the record next to what it licensed.
            "rollout_mode": self.rollout_mode,
            "skip_services": self.skip_services,
            "discovery_ignored_skip_set": self.discovery_ignored_skip_set,
            "applied": self.applied,
            # `total` is the measured decision rate the soak's sample target is
            # re-derived from: the ADR's floor of 200 comparisons is a floor on
            # the wrong axis, since the shepherd re-evaluates the same ref
            # roughly 96 times a day.
            "counts": {**counts, "total": len(self.planned) + len(self.ambiguous)},
            "planned": [p.as_dict() for p in self.planned],
            "ambiguous": [p.as_dict() for p in self.ambiguous],
            "written": self.written,
            "failed": self.failed,
            "blocked": self.blocked,
            "ownership_override": self.ownership_override,
        }


def _rung_one_answers(answer: OwnershipAnswer, held: bool | None) -> bool:
    """Whether rung 1 returns for this entity, so the probe buys nothing.

    THE predicate, called by `plan_for` to take rung 1 and by `build_report` to
    choose the probe set. Two expressions that agree today is what this has
    gone wrong as, twice: first the probe set keyed on the verdict while rung 1
    keyed on `held`, so a dead `active` row was never probed; then it keyed on
    `held` alone while rung 1 keyed on the verdict AND `held`, so a record in a
    FREE state whose `derived.held` is absent -- UNOWNED with held None, which
    `shadow.held` returns for a released or terminal row like any other -- was
    dropped from the probe set by a rung that then declined to answer for it.

    Both times the entity landed on rung 2: undetermined, and `retryable`
    asserting that a re-run might answer differently about a probe that was
    never attempted.

    `held is not False`, so the budget saving survives: rung 1 answers for a
    held record (already-owned) AND for a holding verdict whose held-ness the
    server did not report (undetermined, permanent). Neither reads the legacy
    answer, so neither is worth an HTTP call -- which matters most against an
    mctl-api predating the `derived` block, where that is every entity and the
    run should fail fast rather than burn PROBE_BUDGET_S first.
    """
    return answer.verdict in (OWNED_BY_OTHER, OWNED_BY_ME) and held is not False


def plan_for(
    ref,
    entity_id: str,
    answer: OwnershipAnswer,
    legacy_answer: str,
    *,
    repo: str,
    held: bool | None,
) -> Plan:
    """The import ladder for one entity. Four rungs, in this order:

      1. the store HOLDS it -- ALREADY_OWNED. Nothing to do, and this is what
         makes a re-run write nothing. "Holds", from the server's
         ``derived.held``, NOT from the verdict: see below;
      2. the probe could not answer -- AMBIGUOUS, `undetermined`. An absence,
         not an answer;
      3. a live DevLoopWorkflow drives it -- DEVLOOP. The workflow is the
         owner, and recording anything else would name a holder that is not
         the one actually pushing;
      4. anything else -- AMBIGUOUS, and NOT undetermined. This is a real
         answer: no live DevLoop drives the entity, and the owner that would
         is one with no lifecycle writer yet. Left unowned on purpose.

    Rung 3 has two refusals of its own -- a pull request outside the repo the
    proposal's DevLoop id is built for, and a slug that yields no id. Both are
    AMBIGUOUS and both are UNDETERMINED, because a live DevLoop is confirmed
    and we are declining to name it: the store then holds no live owner for an
    entity a DevLoop drives, which is the dangerous class this import exists
    to remove.

    Rungs 2 and 4 both produce AMBIGUOUS and mean opposite things, which is
    why `Plan.undetermined` separates them: rung 4 is the ordinary outcome for
    most of the fleet and a run made entirely of it is a success, while rung 2
    and rung 3's refusals are the run failing to do its job on that entity.

    `legacy_answer` is the tri-state probe, not the bool. A probe that failed
    must not be read as "no DevLoop is driving this": rung 2 exists precisely
    so it cannot fall through to rung 4 and be counted as a measured absence.

    RUNG 1 TAKES BOTH THE VERDICT AND ``held``; see ``_rung_one_answers``,
    which is the predicate and is shared with the probe set so the two cannot
    drift. ``held`` is what makes it more than the verdict: ``verdict_for``
    answers OWNED_BY_OTHER for any record in a HOLDING state, healthy or not --
    correctly, for its own purpose, which is "may I act" -- while
    ``shadow.held`` reads the server's ``derived.held``, computed from the
    TAKEOVER PREDICATE. Those disagree on exactly the shape this tool exists
    for: a record left `active` past its liveness bound. The soak sees
    held=False and with a live DevLoop reports ``store-permits-old-forbids``,
    the one dangerous class, while a verdict-only rung 1 called the same entity
    already-owned and returned. Rung 1 is terminal, so no re-run would ever
    revisit it: the import would report a clean run over the very entities it
    was built to fix.

    So a record that holds nothing does not stop the ladder. It falls through,
    and a live DevLoop is recorded as the owner -- which is a TAKEOVER of a
    dead row, and legal for exactly the reason the row reads held=False.

    ``held`` unavailable is an ABSENCE, not a third reading. The server may
    predate the ``derived`` block, in which case every entity under a holding
    verdict answers None and the whole run goes red rather than quietly
    classifying on a field that is not there.
    """
    from orchestrator.run_shepherd import (
        DEVLOOP_WORKFLOW_ORG,
        LEGACY_OWNED,
        LEGACY_UNKNOWN,
        devloop_workflow_id,
    )

    # ONE evaluation, ABOVE the rungs, because two of them need it and they
    # need it for opposite purposes: rung 3 writes the id, and rung 2 decides
    # whether its ambiguity is retryable. Computed inside rung 3 instead, the
    # permanent classification was unreachable through the production probe --
    # `_dev_loop_owns_answer` converts the same ValueError to LEGACY_UNKNOWN,
    # so the entity stopped at rung 2 and was reported RETRYABLE, telling an
    # operator to re-run a directory name the same checkout reproduces exactly.
    # That is the mislabel the branch was written to prevent, reached by the
    # only path production can take.
    #
    # `id_error` is the third state, kept apart from `""`: an empty id means
    # the slug never had a DevLoop (structural, an answer), while an error
    # means the entity cannot be represented at all (a data problem in the
    # checkout). Collapsing them would make a bad directory look like a
    # pre-Temporal proposal.
    try:
        workflow_id, id_error = devloop_workflow_id(ref.service, ref.slug), ""
    except ValueError as exc:
        workflow_id, id_error = "", str(exc)

    base = Plan(
        entity_id=entity_id,
        service=ref.service,
        slug=ref.slug,
        proposal_ref=f"{ref.service}/{ref.slug}",
        policy_ref=POLICY_REF,
    )

    if _rung_one_answers(answer, held):
        # `held` is PASSED IN, evaluated once by build_report, rather than
        # computed here. Not tidiness: the first version of this fix keyed rung
        # 1 on `shadow.held` and left build_report selecting the probe set on
        # the VERDICT, so a dead row fell through rung 1 exactly as intended
        # and arrived with LEGACY_UNKNOWN because it had never been probed --
        # rung 3 unreachable for the dangerous class, and `retryable` claiming
        # a probe that was never attempted. Two readings of one question is the
        # same defect the fix was for, one function further out. One evaluation
        # per entity, at the point that also decides who gets probed.
        if held is None:
            # A held verdict whose held-ness cannot be read: either no record
            # came back with it, or the server predates `derived.held`. Both
            # are "the owner cannot be seen", which is the condition the
            # read-first abort exists for -- so it is undetermined and
            # PERMANENT, since a re-run against the same mctl-api answers the
            # same. Never silently already-owned: that is the branch that
            # leaves the dangerous class in place.
            base.decision = DECISION_AMBIGUOUS
            base.undetermined = True
            base.reason = (
                "the store holds a record whose derived.held it did not report; "
                "owner undetermined (mctl-api predates the derived block?)"
            )
            return base
        if held:
            base.decision = DECISION_ALREADY_OWNED
            record = answer.ownership
            owner = record.owner if record else Owner()
            base.owner_type, base.owner_id = owner.type, owner.id
            # THE RECORD'S provenance, not this run's. `base` carries
            # POLICY_REF and a proposal_ref built from the ref being imported,
            # which are what a row this run WROTE would say. On an
            # already-owned line they would describe a row written by somebody
            # else, at some other time, under some other policy -- and the
            # operator reading the dry run line by line has no way to tell the
            # two apart, because every other field on the line is real.
            #
            # Empty when the record did not come back. An absent value is
            # readable as absent; a plausible wrong one is not.
            base.policy_ref = record.policy_ref if record else ""
            base.proposal_ref = record.proposal_ref if record else ""
            base.temporal_workflow_id = record.temporal_workflow_id if record else ""
            base.reason = "the store already holds this entity phase"
            return base
        # held=False under a holding verdict: a record past its liveness bound.
        # It withholds the entity from nobody, so it must not stop the ladder.
        # Fall through -- if a live DevLoop drives the pull request, rung 3
        # records it and the acquire is a takeover the dead row licenses.

    if legacy_answer == LEGACY_UNKNOWN:
        base.decision = DECISION_AMBIGUOUS
        base.undetermined = True
        # RETRYABLE only if a re-run could plausibly answer differently, and an
        # unrepresentable service never will: `_dev_loop_owns_answer` answers
        # UNKNOWN for it precisely because it could not build the id, and the
        # next run over the same checkout builds the same nothing. This is the
        # ONLY path production takes for that input, so it is where the
        # permanence has to be named.
        base.retryable = not id_error
        # The REASON names the cause, not the messenger. The probe answered
        # perfectly well; it answered UNKNOWN *because* the id could not be
        # built, so a sentence whose subject is the probe makes the one detail
        # that separates this from an ordinary timeout into a suffix on a claim
        # that is not true of it. And the URL in `id_error` is one this process
        # synthesised from a directory name, not anything in `.status.yaml`, so
        # it is quoted as what it is rather than left to read as a recorded
        # value the operator should go looking for.
        if id_error:
            base.reason = (
                f"this proposal's service directory {ref.service!r} is not a usable "
                "repository name, so no DevLoop workflow id exists for it; the probe "
                "could not be asked. Rename the directory"
            )
        else:
            base.reason = "the DevLoop liveness probe could not answer; owner undetermined"
        return base

    if legacy_answer == LEGACY_OWNED:
        # run_shepherd.devloop_workflow_id, NOT a second transcription of the
        # rule: this value is written durably into temporal_workflow_id, and
        # "the id recorded is the one the probe just confirmed alive" holds
        # only while the two agree.
        # DEVLOOP_WORKFLOW_ORG comes from there too, rather than being spelled
        # again here. `devloop_workflow_id` builds ids under it and this branch
        # checks the pull request against it; two copies is a check that can
        # disagree with the thing it checks, and it would fail quietly -- every
        # entity refused as "in another repository", a red run whose reason
        # names the wrong cause.
        #
        # The WHOLE repo, not just the org, and CASE-INSENSITIVELY. GitHub
        # treats owner and repository names case-insensitively, so a `pr:`
        # spelled mctlhq/MCTL-Web in .status.yaml names the same repository
        # and must not be refused over it.
        #
        # devloop_workflow_id builds `dev-loop-mctlhq-{service}-{N}` from the
        # proposal's service directory, which is the DevLoop's id only while
        # the service name and the repo name are the same string. Checking the
        # org alone let a proposal under agents-state/mctl-web whose `pr:`
        # points at mctlhq/something-else be imported as an owner naming
        # another repository's workflow.
        expected_repo = f"{DEVLOOP_WORKFLOW_ORG}/{ref.service}"
        if repo.casefold() != expected_repo.casefold():
            # UNDETERMINED, and this is the important half. A live DevLoop is
            # confirmed driving this entity — that is what LEGACY_OWNED means —
            # and we are declining to record an owner for it. The store is then
            # left with no live owner while a DevLoop drives the pull request,
            # which is `store-permits-old-forbids`: the DANGEROUS class, and
            # precisely the condition this import exists to remove.
            #
            # So it is not the benign ambiguity of "nobody drives this". It is
            # the run failing to do its one job on this entity, and it makes
            # the run red.
            base.decision = DECISION_AMBIGUOUS
            base.undetermined = True
            base.reason = (
                f"a live DevLoop drives this, but the pull request is in {repo} "
                f"while this proposal's DevLoop id is built for {expected_repo}; "
                "importing it would name another repository's workflow"
            )
            return base
        if id_error:
            # The service directory is not a representable repository name.
            # `_discover_refs` takes it from any directory under the state dir
            # that does not begin with `_` and never checks it against
            # SERVICES, and this module passes fix_only=True so every directory
            # is discovered whatever its mode resolves to.
            #
            # UNDETERMINED and PERMANENT, the same as the two refusals around
            # it and for the same reason: a live DevLoop is CONFIRMED for this
            # entity and we are declining to name it, so the store keeps no
            # live owner for a pull request a DevLoop drives. A re-run over the
            # same checkout reproduces it exactly -- the fix is renaming the
            # directory.
            #
            # Reached only with an INJECTED probe. In production the probe is
            # `_dev_loop_owns_answer`, which answers LEGACY_UNKNOWN for exactly
            # this input, so the entity stops at rung 2 above -- where the same
            # `id_error` makes the ambiguity permanent rather than retryable.
            # Kept because a test double, or a future probe that answers from
            # somewhere other than that function, can still reach here, and
            # because an exception out of the ladder would abort the whole
            # report rather than fail for its own entity.
            base.decision = DECISION_AMBIGUOUS
            base.undetermined = True
            base.reason = (
                f"a live DevLoop drives this, but its workflow id cannot be built: {id_error}"
            )
            return base
        if not workflow_id:
            # Unreachable from the probe — a slug with no issue-<N>- prefix
            # answers LEGACY_FREE — but an empty owner id must never become a
            # row. dev_loop.py guards on `bool(result.owner_id)` precisely
            # because a record owned by nobody withholds the entity from
            # everyone and names no one to ask.
            # UNDETERMINED, same reasoning as the repo branch above: a live
            # DevLoop is confirmed and we cannot name it, so the store keeps no
            # live owner for an entity a DevLoop is driving — the dangerous
            # class, left in place by the run that was supposed to remove it.
            base.decision = DECISION_AMBIGUOUS
            base.undetermined = True
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
    # Which of the two it would be is NOT named, and that is the same
    # correction POLICY_REF makes. `policy.default_owner_for` resolves it
    # through `run_shepherd._service_mode`, i.e. through THIS pod's
    # SHEPHERD_SKIP_SERVICES — unset here by default — so the sentence read
    # `shepherd` for a repository the shepherd actually skips. Prose in a
    # report an operator reviews line by line is not a place for a claim that
    # is wrong from the wrong pod, and neither owner type has a lifecycle
    # writer, so the distinction changes no decision.
    base.reason = (
        "no live DevLoop drives this; the owner that would take it "
        "(shepherd or pr-steward, by service mode) has no lifecycle writer yet"
    )
    return base


def probe_all(refs, probe) -> dict[int, str]:
    """The DevLoop liveness answer for every ref, concurrently and bounded.

    Serially this is one HTTP call per proposal at the probe's own 10s timeout,
    and the whole set runs BETWEEN the store read and the decisions made from
    it. That gap is where a bootstrap goes wrong: the longer it is, the more
    likely the store moved under a decision already taken. PROBE_BUDGET_S and
    PROBE_WORKERS bound it — this module's OWN budget and its own concurrency,
    neither inherited from the sweep, because an unanswered ref costs nothing
    there and makes the run red here. Both, not just the budget: the ceiling
    is workers x budget / timeout, so a budget raised over an inherited worker
    count moves nothing.

    A ref the budget did not answer is LEGACY_UNKNOWN — read as the dict's
    default rather than written — so it reaches the ladder as ambiguous, is
    left unowned, and makes the run RED. An unanswered probe must not become a
    decision, and here it must not be mistaken for one either: sizing the
    budget is an operator concern before a first --apply on a large fleet.
    """
    answers: dict[int, str] = {}
    if not refs:
        return answers
    pool = ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(refs)))
    try:
        futures = {
            pool.submit(probe, ref.service, ref.slug): i for i, ref in enumerate(refs)
        }
        try:
            for future in as_completed(futures, timeout=PROBE_BUDGET_S):
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
                f"warn: dev-loop probe hit its {PROBE_BUDGET_S}s budget "
                f"with {unanswered} proposal(s) unchecked — they will be reported ambiguous"
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return answers


#: A GitHub pull request URL, web or API form. _parse_pr_url walks four
#: segments from the right and validates neither the host nor the route, so
#: `.../issues/42` and `https://example.invalid/a/b/c/42` both parse cleanly
#: into a pull-request entity id. On the READ side that was fail-open — the
#: shepherd 404s and moves on. Here it would acquire a durable row against a
#: real but unrelated entity, quietly in both directions.
_PULL_REQUEST_URL = re.compile(
    r"^https://(github\.com/[^/]+/[^/]+/pull"
    r"|api\.github\.com/repos/[^/]+/[^/]+/pulls)/[0-9]+/?$"
)


def _require_pull_request_url(pr_url: str) -> None:
    """Raise unless `pr_url` names a GitHub pull request."""
    if not _PULL_REQUEST_URL.match(pr_url.strip()):
        raise ValueError(f"not a GitHub pull request URL: {pr_url!r}")


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
    from orchestrator.run_shepherd import DEVLOOP_WORKFLOW_ORG, LEGACY_UNKNOWN, _parse_pr_url

    report = Report()
    by_entity: dict[str, tuple[Any, str]] = {}

    for ref in refs:
        try:
            _require_pull_request_url(ref.pr_url or "")
            owner_name, repo_name, number = _parse_pr_url(ref.pr_url or "")
        except Exception as exc:  # noqa: BLE001 — an unreadable URL is not a crash
            report.ambiguous.append(
                Plan(
                    service=ref.service,
                    slug=ref.slug,
                    decision=DECISION_AMBIGUOUS,
                    # Undetermined: the entity could not be identified, which
                    # is an absence of an answer rather than one.
                    undetermined=True,
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
                    # UNDETERMINED: the run cannot tell which proposal is
                    # authoritative for this pull request, so whatever the
                    # winner decided is a coin toss the report should not
                    # present as settled. Two proposals on one PR is a data
                    # problem, and a bootstrap is the wrong place to guess past
                    # one.
                    undetermined=True,
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
    # An entity rung 1 answers for returns before the legacy answer is read, so
    # probing it buys nothing — and the budget is shared wall-clock, not
    # per-ref. On the idempotence path, where every entity is already-owned,
    # the whole PROBE_BUDGET_S would go to answers nobody reads; against a slow
    # mctl-api those probes crowd out the undecided entities, which then fall
    # to UNKNOWN and are reported ambiguous. Ambiguous means left UNOWNED,
    # which is precisely the store-permits-old-forbids class this tool exists
    # to remove.
    ordered = sorted(by_entity)
    # `held` evaluated ONCE per entity, and the probe set taken from THE SAME
    # PREDICATE rung 1 takes -- not from a second expression that agrees with
    # it. See `_rung_one_answers` for the two ways the second expression has
    # already been wrong.
    held_by_entity = {entity_id: shadow.held(answers[entity_id]) for entity_id in ordered}
    undecided = [
        entity_id
        for entity_id in ordered
        if not _rung_one_answers(answers[entity_id], held_by_entity[entity_id])
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
            ref,
            entity_id,
            answer,
            legacy_by_entity.get(entity_id, LEGACY_UNKNOWN),
            repo=repo,
            held=held_by_entity[entity_id],
        )
        # Set here, not in `plan_for`: this is where the parsed spelling and
        # the service directory are both in scope, and the comparison the
        # ladder makes is deliberately case-INSENSITIVE. `may_apply` needs the
        # exact answer, because the tolerance that admits the entity is what a
        # write would turn into a durable id nothing else reproduces.
        plan.entity_id_exact = repo == f"{DEVLOOP_WORKFLOW_ORG}/{ref.service}"
        if plan.decision == DECISION_AMBIGUOUS:
            report.ambiguous.append(plan)
        else:
            report.planned.append(plan)
    return report


#: Whether mctl-api can say that a DevLoop execution tracks ownership.
#:
#: FALSE until mctlhq/mctl-api#322 lands. `/api/v1/agents/dev-loop/{id}`
#: reports `shepherd_in_loop`, which is `workflow.patched("shepherd-in-loop")`
#: plus the pinned-image check -- but every ownership call is gated on
#: `shepherd_in_loop AND workflow.patched("lifecycle-ownership")`, a second,
#: independent marker. An execution recorded between the two answers the probe
#: True and replays the second as False forever: it never acquires, heartbeats
#: or releases the row a write would create.
#:
#: A CONSTANT rather than a probe result, because there is nothing to probe:
#: the field does not exist. When it does, this becomes a per-entity answer and
#: the pair must repeat the `shepherd_in_loop`/`shepherd_in_loop_known`
#: contract -- ABSENT is not FALSE, and unknown must BLOCK.
OWNERSHIP_CAPABILITY_KNOWN = False


def may_apply(plan: Plan, *, override: str = "") -> str:
    """Whether this entity may be written. "" means yes; otherwise the reason.

    THE predicate, and a conjunction — every clause must hold, and an UNKNOWN
    clause blocks rather than passing or being retried:

      1. the plan is a devloop-workflow import (the only writable decision);
      2. the entity id is the one every other component will build;
      3. mctl-api can report whether the execution tracks ownership, and
         reports that it does.

    IN THAT ORDER, which is the order they are evaluated, because the order
    decides which reason a blocked entity carries. A miscased id reports #386
    rather than #322 — the right answer, since the override cannot clear it and
    an operator sent to wait for #322 would wait forever.

    (3) is one constant today, `OWNERSHIP_CAPABILITY_KNOWN`, because the field
    does not exist yet — so this refuses every write until mctlhq/mctl-api#322
    lands, which is the honest reading of "unknown blocks".

    A PREDICATE, not a comment. The limit was written into KNOWN LIMITS as a
    blocker and enforced nowhere, and the first attempt to enforce it made a
    NARROWER SCOPE the thing that licensed the write — which is a bypass of the
    same gate rather than a gate: scoping a run does not make an unheartbeated
    row safe, it just makes fewer of them.

    `override` is the operator escape hatch, and it is explicit: a non-empty
    justification, recorded on the report, asserting that these entities were
    checked in Temporal by hand. It does not silently follow from `--service`.
    """
    if plan.decision != DECISION_DEVLOOP:
        return f"not a writable decision ({plan.decision})"
    if not plan.entity_id_exact:
        return (
            "the pull request's repository is spelled differently from the proposal's "
            "service directory, so the id written here is not the one DevLoopWorkflow "
            "and the shadow compare build (mctlhq/mctl-agents#386)"
        )
    if not OWNERSHIP_CAPABILITY_KNOWN and not override:
        return (
            "mctl-api cannot report whether this DevLoop execution tracks ownership "
            "(mctlhq/mctl-api#322); the probe reads shepherd_in_loop, which is True for "
            "executions recorded between the shepherd-in-loop and lifecycle-ownership "
            "patch markers -- they answer it and never acquire, heartbeat or release"
        )
    return ""


def apply_report(report: Report, client: OwnershipClient, *, override: str = "") -> Report:
    """Write the rows the report plans. Only the writable decisions.

    Idempotent on two legs, and both are load-bearing. `build_report`'s
    read-first pass marks an entity the store HOLDS ALREADY_OWNED and this loop
    skips it; and the owner ids are deterministic -- POLICY_REF and the
    DevLoop's own workflow id, never a pod name -- so a re-run of the same
    fleet asks for the same rows rather than a second set of them. Still true
    after a takeover: a dead row this run replaced reads held=True on the next
    one, so the second run skips what the first wrote.

    SOME OF THESE ARE TAKEOVERS, not first writes. Rung 1 keys on
    `derived.held`, so an entity whose record is `active` past its liveness
    bound falls through the ladder and arrives here with a DevLoop owner. That
    is the point -- it is the dangerous class -- and the same predicate that
    let it through is the one mctl-api evaluates on the acquire. If the store
    disagrees and refuses, the refusal lands in `failed` and the run goes red
    rather than being retried or reasoned around here: a store that declines a
    takeover is the authority on that entity.

    Total on the ENTITY, not on the run: a refused acquire is recorded in
    `failed` and the loop continues. A store that declines one row has told you
    something about that row; the rest of the batch still has to be attempted,
    and `main` carries the refusal out in the exit code.

    SERIAL, and unbudgeted, which is a deliberate pair. One HTTP call per
    writable row, in the report's order, because these are writes against a
    store the shepherd may already be reading and a one-shot migration is not
    the place to multiply pressure on it -- PROBE_WORKERS is justified by the
    probe being read-only, and that justification does not carry here. The cost
    is that this is a third unbounded term inside the WorkflowTemplate's
    activeDeadlineSeconds, after discovery and alongside the probe pass. It is
    bounded in practice by `counts.devloop-workflow` in the dry run, which is
    the number an operator reads BEFORE applying -- which is what makes the
    reviewed dry run a precondition rather than a courtesy.
    """
    for plan in report.planned:
        # ONE predicate, and it is a conjunction where unknown BLOCKS. The
        # decision check that used to live here is its first clause, so a
        # decision added to DECISIONS later is still not written by default.
        #
        # A blocked entity is RECORDED, not skipped silently: "312 planned, 0
        # written" with no reason is the artifact an operator cannot act on,
        # and `blocked` is the field that says which gate refused and why.
        refusal = may_apply(plan, override=override)
        if refusal:
            if plan.decision == DECISION_DEVLOOP:
                # `proposal_ref` too. The field comment calls this list an
                # action item for a human, and clause 2's reason names "the
                # proposal's service directory" without saying WHICH -- so the
                # list said a `.status.yaml` needs editing and not which one.
                # It is already computed, and passed to `acquire` six lines
                # down.
                report.blocked.append(
                    {
                        "entity_id": plan.entity_id,
                        "proposal_ref": plan.proposal_ref,
                        "reason": refusal,
                    }
                )
            continue
        answer = client.acquire(
            EntityRef(kind=KIND_PULL_REQUEST, id=plan.entity_id),
            PHASE_REVIEW_REMEDIATION,
            Owner(type=plan.owner_type, id=plan.owner_id),
            proposal_ref=plan.proposal_ref,
            policy_ref=plan.policy_ref,
            temporal_workflow_id=plan.temporal_workflow_id,
        )
        # `may_mutate`, NOT `wrote`. contract.py states the rule and the reason:
        # on a body-less 2xx to a claiming route the two deliberately disagree
        # -- `wrote` is True because mctl-api took the write, `verdict` is
        # UNKNOWN because the record that would name us as owner never arrived.
        #
        # `written` is read by an operator as "the store now holds a row naming
        # this owner", and gating it on `wrote` would put an unseen row in that
        # list. This tool aborts a whole run over one id whose owner it could
        # not see; recording an unconfirmed write as a success is the same
        # claim from the other side.
        #
        # The direction of the mistake matters too. A write that landed but
        # could not be confirmed goes to `failed`, the run exits 1, and a
        # re-run reads the row as held and reports it already-owned -- a red
        # run over a correct store. The opposite error reports a green run over
        # a store nobody verified.
        if answer.may_mutate:
            report.written.append(plan.entity_id)
        else:
            # The OWNER too, when the server attached a record. contract.py
            # attaches it so "the caller can see what the server saw", and
            # `reason` is empty for a 2xx naming another owner -- so a refusal
            # could reach the report with nothing in it that explains the
            # refusal, which is the one thing an operator reads it for.
            record = answer.ownership
            owner = record.owner if record else Owner()
            report.failed.append(
                {
                    "entity_id": plan.entity_id,
                    "verdict": answer.verdict,
                    "reason": answer.reason,
                    "held_by_type": owner.type,
                    "held_by_id": owner.id,
                    "held_state": record.state if record else "",
                }
            )
    return report


def _posture(report: Report) -> str:
    """What the run was ASKED to do, in the tense the field is defined in.

    `Report.applied` is "the posture, not the outcome", so the label must not
    be past tense. `[applied]` read as a claim about what happened, and on the
    one path where posture and outcome are guaranteed to disagree it printed
    `lifecycle-bootstrap [applied]: ABORTED: refused --apply` for a run
    asserted to have written nothing.
    """
    return "apply" if report.applied else "dry run"


def _print_report(report: Report) -> None:
    """The report on stdout behind its marker, and a summary on stderr.

    ONE LINE, prefixed: see REPORT_MARKER. The summary goes out on EVERY path
    including the abort, which is the one outcome it exists for -- an Argo log
    tail should answer "did this work" without parsing anything.
    """
    print(f"{REPORT_MARKER} {json.dumps(report.as_dict())}", flush=True)
    if report.aborted:
        print(
            f"lifecycle-bootstrap [{_posture(report)}]: ABORTED: {report.aborted}",
            file=sys.stderr,
        )
        return
    # `already-owned` counted apart from the work. Both live in `planned` and
    # apply_report skips the first, so the idempotent second run -- the one
    # this tool sells as writing nothing -- printed "312 planned, 0 written",
    # indistinguishable in a log tail from 312 rows the store refused.
    #
    # TWO numbers, because they answer two questions and they are not the same
    # number any more.
    #
    # `importable` is the sizing number: how many entities this fleet has that
    # the ladder would import. It is what `apply_report`'s docstring calls the
    # bound on an apply, "which is what makes the reviewed dry run a
    # precondition rather than a courtesy", so it must not shrink to whatever
    # today's gates happen to allow.
    #
    # `writable` is the promise: how many a write would actually attempt. It
    # asks `may_apply`, because that is what `apply_report` asks. Counting
    # `decision == DECISION_DEVLOOP` was true when that was the whole
    # predicate; `may_apply` then grew `entity_id_exact` and the capability
    # clause, the predicate moved and the count did not follow, and a dry run
    # over 312 live DevLoops printed "312 to write" for an apply that writes
    # zero and blocks 312. The dry run cannot say so anywhere else: `blocked`
    # is populated only by `apply_report`, which runs only under --apply.
    #
    # `ownership_override` off the REPORT, not from an argument: this function
    # takes only a report, and the field exists precisely so a reader can see
    # what the run claimed.
    already = sum(1 for p in report.planned if p.decision == DECISION_ALREADY_OWNED)
    importable = sum(1 for p in report.planned if p.decision == DECISION_DEVLOOP)
    writable = sum(
        1 for p in report.planned if not may_apply(p, override=report.ownership_override)
    )
    # The posture on this channel too. It is the one the docstring says exists
    # to answer "did this work" from a log tail without parsing anything, and
    # without it a dry run and an apply that wrote nothing read identically
    # there -- which is the exact case `applied` was added to the JSON for.
    print(
        f"lifecycle-bootstrap [{_posture(report)}]: {importable} importable, "
        f"{writable} writable now, {already} already owned, {len(report.ambiguous)} ambiguous, "
        f"{len(report.written)} written, {len(report.blocked)} blocked, "
        f"{len(report.failed)} failed",
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
    # Scoping exists so a first --apply does not have to be fleet-wide, and so
    # a dry run can be reviewed one service at a time. It matters more here
    # than on the shepherd: apply_report skips an entity the store already
    # holds, so a row written wrongly cannot be corrected by re-running -- a
    # mistake at fleet scale is a mistake to undo by hand.
    parser.add_argument("--service", default=None, help="limit to one service")
    parser.add_argument("--slug", default=None, help="limit to one proposal slug")
    # The override, and it is a JUSTIFICATION rather than a bare flag: it takes
    # the operator's reason, which goes on the report verbatim. A claim that
    # licenses durable writes belongs next to what it licensed.
    #
    # It exists because `may_apply`'s capability clause refuses every write
    # until mctlhq/mctl-api#322 lands, and an emergency path that is real must
    # be explicit rather than something a narrower scope quietly confers.
    parser.add_argument(
        "--assume-tracked-ownership",
        default="",
        metavar="REASON",
        help=(
            "override the mctl-api#322 capability block. Requires --service: "
            "the flag asserts these entities were checked in Temporal by hand, "
            "and --slug alone matches a slug in every service. "
            "The reason is recorded on the report."
        ),
    )
    args = parser.parse_args(argv)

    if not args.state_dir.is_dir():
        # The infrastructure fault the block below names FIRST, and the one an
        # unmounted Argo volume actually produces. It used to fall through to
        # `_discover_refs`'s bare `SystemExit("State dir not found: ...")` --
        # stderr only, nothing behind REPORT_MARKER -- while the mount that
        # went away MID-run, added in the same commit, printed a report. To the
        # consumer this whole area is written for ("an Argo step reads the
        # report as an output parameter") those two failed identically.
        #
        # So the invariant that commit claimed -- every red exit from this
        # module carries the report -- is true now rather than nearly true.
        _print_report(Report(aborted=f"state dir not found: {args.state_dir}"))
        return 1

    if args.service is not None:
        # Validated against `run_shepherd.discover_services`, which is what
        # `_discover_refs` iterates. ONE definition of "this checkout contains
        # this service", so this filter and that loop cannot disagree about a
        # name. (The shepherd's own CLI validates `--service` against
        # `config.settings.SERVICES` and still does — a different question, for
        # a sweep over a fixed fleet, and not this one.)
        #
        # Not SERVICES here: a repository whose pull requests another lifecycle
        # drives has proposals in this checkout and no SERVICES entry
        # (`mctl-claude-remote`), and SERVICES would make it the one service an
        # operator cannot scope a run to.
        #
        # What this buys is a NAME an operator mistyped, nothing more. It is
        # not what keeps a bad directory name out of an id builder -- discovery
        # walks unfiltered and `devloop_workflow_id`'s own callers contain that
        # -- and claiming otherwise would be a guard justified by a hazard it
        # does not remove.
        #
        # THE EFFECTIVE VALIDATOR IS `named_is_dir`, not `discover_services`,
        # and that is deliberate rather than an oversight. `present` is a
        # SUBSET of the directories under the state dir, so `service in
        # present` implies `named_is_dir` and the condition below reduces to
        # `not named_is_dir`. `discover_services` survives here as the source
        # of the `; found: ...` listing -- the half that does the diagnostic
        # work -- while the admit/refuse decision is deliberately wider,
        # because of the second outcome below.
        #
        # TWO outcomes this check separates, and a third it must not touch:
        #
        #   - the name is not a directory at all -> a typo, refused below;
        #   - the name IS a directory with no `proposals/` -> an ordinary
        #     state, a service that never had a proposal or had its last one
        #     archived. Refusing it would be exit 2 for something nobody
        #     mistyped, so it falls through to "discovered no proposals at
        #     all", which exits 1 WITH a report;
        #   - the state dir is missing or unreadable -> an infrastructure
        #     fault, not an argument error. Both are answered ABOVE this block
        #     and above the --apply refusal, so one broken mount reads the same
        #     way whichever flags the run carried; the walk's own guard below
        #     catches what happens after this instant.
        #
        # The distinction is which red an operator can act on, and it is why
        # this refusal prints a report rather than calling `parser.error`:
        # argparse exits 2 with nothing on stdout, which an Argo step reading
        # the report as an output parameter cannot tell from the deliberate
        # `--apply` refusal below -- whose entire design note is that exit 2
        # with no report is the one outcome a log cannot interpret.
        # Read HERE, where the `; found: ...` listing it supplies is used.
        # This is an ARGUMENT check -- it answers "did you name something
        # this checkout has" -- so it belongs with the argument, and its own
        # OSError arm covers a state dir that cannot be listed at the root.
        # Every deeper read is the walk's, and the walk's guard is what
        # answers for it.
        from orchestrator.run_shepherd import discover_services

        try:
            present = discover_services(args.state_dir)
        except OSError as exc:
            _print_report(
                Report(aborted=f"cannot read the state dir {args.state_dir}: {exc}")
            )
            return 1
        named_is_dir = (args.state_dir / args.service).is_dir()
        if args.service not in present and not named_is_dir:
            _print_report(
                Report(
                    aborted=(
                        f"no service state dir {args.service!r} under {args.state_dir}"
                        + (f"; found: {', '.join(sorted(present))}" if present else "")
                    )
                )
            )
            return 2

    from orchestrator import run_shepherd
    from orchestrator.run_shepherd import _dev_loop_owns_answer, _discover_refs

    # dry_run=True ALWAYS, including under --apply. _discover_refs rewrites
    # .status.yaml when it finds a PR by branch — flipping in-progress to
    # implemented — and this tool writes ownership rows, never gitops files. A
    # "dry run" that edited the checkout before the store was even read would
    # contradict every other guarantee here, the `aborted` path's "nothing was
    # written" included.
    #
    # fix_only=True, and it is NOT about fix-only mode. It is the lever that
    # short-circuits _service_mode before SHEPHERD_SKIP_SERVICES is read, which
    # is what makes this import INDEPENDENT of the bootstrap pod's environment.
    #
    # It was briefly removed, on the premise that a skipped service can only
    # land in rung 4 and so only pads counts.total. That premise is false: a
    # DevLoopWorkflow is started per ISSUE and knows nothing about
    # SHEPHERD_SKIP_SERVICES, so a skipped service can perfectly well have a
    # live DevLoop driving one of its pull requests — exactly the entity this
    # import exists to record.
    #
    # Removing it failed quietly in both directions. Unset here (the default,
    # and the WorkflowTemplate was written against a caller that did not read
    # this variable) nothing resolves to SKIP and the removal is a production
    # no-op with no signal that it did not take effect. Set wider than the
    # shepherd's, a service the sweep DOES compare is dropped at discovery: its
    # live-DevLoop entities are never probed, never planned, never reported —
    # and the store keeps no owner for an entity a DevLoop drives, which is
    # store-permits-old-forbids left in place by a run that exits 0 with a
    # clean report.
    #
    # The skip set is recorded in the report either way. It is load-bearing
    # input read from this process's environment, the same shape of problem as
    # the rollout mode, and it gets the same treatment.
    #
    # ONE name for the flag and the field that reports it. Written twice --
    # `True` in the call and `True` again on the report -- the field could only
    # ever say `true`, including on a build where the call had changed and the
    # claim had become false. A report field that cannot contradict the code it
    # describes documents an intention rather than a run.
    discovery_ignores_skip_set = True
    # GUARDED, and this is where the invariant lives for EVERY caller: a
    # `--service` run, a fleet-wide one, and every depth of the walk. Anything
    # earlier is a snapshot, and a snapshot cannot answer for the read that
    # follows it.
    #
    # What raises out of the walk is the two listings —
    # `sorted(state_dir.iterdir())` and `sorted(proposals_dir.iterdir())`. The
    # per-file `.status.yaml` read does NOT: `_discover_refs` wraps that in its
    # own `except Exception` and warns-and-skips, deliberately, so one
    # malformed file anywhere in the fleet does not abort the run. Saying
    # otherwise here would invite a later reader to delete that narrower
    # handler as redundant with this one, which is the behaviour its `noqa`
    # comment exists to prevent.
    #
    # `SystemExit` alongside `OSError`, because that is how `_discover_refs`
    # reports a missing state dir: a message rather than a report.
    #
    # The domain is wider than the walk and the message says so — this also
    # catches `gh` missing from PATH (a `FileNotFoundError`, hence an
    # `OSError`) from the `pr:`-less branch lookup, and attributing that to the
    # state dir alone would mislead on the one field an Argo step reads as its
    # output parameter.
    try:
        refs = _discover_refs(
            args.state_dir,
            service_filter=args.service,
            slug_filter=args.slug,
            dry_run=True,
            fix_only=discovery_ignores_skip_set,
        )
    except (OSError, SystemExit) as exc:
        _print_report(
            Report(
                aborted=(
                    f"discovery failed while walking {args.state_dir} "
                    f"(or running a tool it needs): {exc}"
                )
            )
        )
        return 1
    # POLICY REFUSALS COME AFTER DISCOVERY, and that ordering is the point.
    #
    # "There is no readable checkout" outranks "this build does not write":
    # the second is about what this run may do, the first about whether there
    # is anything to do it to, and an operator sent away with the policy
    # answer learns about the broken volume only on a second run.
    #
    # It was in front, with a cheap readability probe hoisted above it to keep
    # the ordering true. That held for a mount broken AT THE ROOT and not one
    # directory down: `discover_services` admits a service on a `stat` of its
    # `proposals/`, so a `proposals/` at 0o111 passed the probe, hit the
    # refusal, and answered `--apply` and a fleet-wide run differently for one
    # permission bit. A guard that covers a depth is not a guard that covers
    # the class.
    #
    # Discovery is where the whole class is answered, so the refusals moved
    # behind it rather than the guard moving forward again. Discovery writes
    # nothing -- `dry_run=True` unconditionally -- so nothing is risked by
    # doing it first; what it costs is that a wrong mode is reported after the
    # walk instead of before it.

    # Read ONCE. `rollout.mode()` maps an unrecognised value to OFF and warns
    # as a side effect, so evaluating it four times in the refusal both printed
    # the warning four times -- onto the report's own stdout channel -- and
    # produced a message naming `'off'` for an operator who had actually typed
    # `obserev`. The one exit whose entire job is to be diagnostic must not
    # rename the mistake it is diagnosing.
    configured = rollout.mode()
    # From `rollout`, not `os.environ`: the switch table in that module says
    # LIFECYCLE_ROLLOUT_MODE is "read in this module and nowhere else", and a
    # second read site here would also normalise differently -- `mode()` strips
    # and lowercases, so ` Observe ` reads as valid there and as a typo in a
    # message that re-read the variable itself. That difference is the exact
    # thing naming the raw value exists to surface.
    raw_mode = rollout.raw_mode()
    if args.apply and args.assume_tracked_ownership and not args.service:
        # UNDER --apply only. The override licenses nothing on a dry run --
        # nothing is written for it to license -- so refusing one that carries
        # it costs the operator the very report they need and gives back no
        # safety. An earlier version was deliberately independent of --apply as
        # "the conservative direction"; conservative about a posture that
        # writes nothing is just a report withheld, and this module's stronger
        # invariant is that a dry run always produces one.
        #
        # The override requires `--service`, and this is the only place scope
        # carries weight. It is not a gate of its own: narrowing a run does not
        # make an unheartbeated row safe, it just makes fewer of them, and an
        # earlier version of this refusal let a scope alone license the write --
        # a bypass of `may_apply`'s capability clause wearing the shape of a
        # gate.
        #
        # What it is for HERE is that the override's claim is "I checked these
        # entities in Temporal by hand", and that claim is only checkable, and
        # only honest, about a set the operator can enumerate.
        #
        # `--service`, NOT "--service or --slug". `_discover_refs` applies
        # `slug_filter` INSIDE the per-service walk, so `--slug issue-7-x`
        # alone matches that slug in every service -- which
        # `test_slug_narrows_across_services` pins as deliberate behaviour.
        # Accepting it as the named set made two tests in one file assert
        # opposite things about the same flag. `--slug` still narrows further;
        # it just does not bound the set on its own.
        _print_report(
            Report(
                aborted=(
                    "refused --assume-tracked-ownership without --service: the "
                    "override asserts these entities were checked in Temporal by hand, "
                    "which is a claim about a named set. --slug alone is not one: "
                    "_discover_refs applies it inside the per-service walk, so it "
                    "matches that slug in every service. Add --service (--slug may "
                    "narrow further)."
                ),
                rollout_mode=configured,
                applied=args.apply,
                ownership_override=args.assume_tracked_ownership,
            )
        )
        return 2
    if args.apply and configured != rollout.OBSERVE:
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
        #
        # A report on this path too. The marker exists so a consumer can always
        # find the report by grepping for it, and an Argo step reading it as an
        # output parameter gets nothing if this is the one exit that prints
        # none — a different failure from "the report says it refused". The
        # mode IS the answer here, which is what rollout_mode is for.
        _print_report(
            Report(
                aborted=(
                    f"refused --apply: {rollout.ENV_VAR} is "
                    f"{'unset' if raw_mode is None else repr(raw_mode)} and reads as "
                    f"{configured!r}, expected {rollout.OBSERVE!r}. Below it the owners "
                    "these rows name are not heartbeating; above it the shepherd is "
                    "already reading the store and a bulk import races it."
                ),
                rollout_mode=configured,
                applied=args.apply,
                # On this path too. The field's whole rationale is that a claim
                # licensing durable writes sits next to what it licensed, and
                # an operator who passed one and was refused for a different
                # reason still made the claim.
                ownership_override=args.assume_tracked_ownership,
            )
        )
        return 2

    client = OwnershipClient()
    report = build_report(refs, client, _dev_loop_owns_answer)
    report.rollout_mode = configured
    report.skip_services = ",".join(sorted(run_shepherd.SHEPHERD_SKIP_SERVICES))
    report.discovery_ignored_skip_set = discovery_ignores_skip_set
    report.applied = args.apply

    report.ownership_override = args.assume_tracked_ownership
    if not report.aborted and args.apply:
        apply_report(report, client, override=args.assume_tracked_ownership)

    _print_report(report)
    # `blocked` counts, and it is the shape a production apply has TODAY.
    #
    # With OWNERSHIP_CAPABILITY_KNOWN False and no override, every writable row
    # is blocked -- and the WorkflowTemplate reaches that with one parameter,
    # since dry_run=false already yields --apply. Exiting 0 with `written: []`
    # is, in the coarsest channel an Argo step branches on, indistinguishable
    # from a successful apply against a fleet with nothing to import -- which
    # is the ORDINARY state of the fleet by this module's own account. That is
    # the same collapse `applied` was added to the JSON to prevent, one level
    # out.
    #
    # The mixed case is worse and survives the override:
    # `--assume-tracked-ownership` answers the capability clause and not the id
    # clause, so a batch where three of forty rows fail `entity_id_exact`
    # writes thirty-seven, records three, and exits 0 -- a partial apply that
    # silently skipped entities.
    #
    # No dry-run false positive to weigh against it: `apply_report` is the only
    # writer of `blocked`, and it runs only under --apply.
    if report.aborted or report.failed or report.blocked:
        return 1
    # Discovering NOTHING is not a successful import: a checkout mounted one
    # level off, or a state dir with no proposals in an actionable status,
    # reads exactly like a clean run.
    if not report.planned and not report.ambiguous:
        print("lifecycle-bootstrap: discovered no proposals at all", file=sys.stderr)
        return 1

    # Writing nothing IS a success, and this is the correction the shepherd and
    # pr-steward removal forces. Since only live-DevLoop entities are imported,
    # "no live DevLoop anywhere right now" is the ordinary state of the fleet:
    # every plan is ambiguous, nothing is written, and that is the right
    # outcome rather than a failure.
    #
    # What must fail is an entity the run could not get an ANSWER about.
    #
    # An earlier version failed on `not report.planned` and justified it with
    # "MCTL_TOKEN unset makes the probe answer UNKNOWN silently". That was
    # wrong twice: the criterion condemned the ordinary case, and the scenario
    # cannot happen -- the store read uses the same token, OwnershipClient
    # raises OwnershipUnavailable without one, every id reads UNKNOWN, and the
    # run ABORTS above with nothing written. The loud path was always the loud
    # path.
    undetermined = [p for p in report.ambiguous if p.undetermined]
    if undetermined:
        # Retryable and permanent causes counted apart, because they need
        # opposite responses: a probe that could not answer clears on a re-run,
        # while a malformed `pr:`, a pull request in the wrong repository, a
        # slug that yields no workflow id or two proposals on one PR are data
        # problems the same run reproduces exactly.
        #
        # Read off the FIELD. An earlier version grepped a substring of the
        # human-readable reason, which is the thing `undetermined` exists to
        # replace -- and it mislabelled the slug case as "fix .status.yaml",
        # where the fix is renaming the proposal directory.
        retryable = sum(1 for p in undetermined if p.retryable)
        permanent = len(undetermined) - retryable
        detail = f"{retryable} retryable, {permanent} needing a fix in the checkout"
        print(
            f"lifecycle-bootstrap: {len(undetermined)} entit(ies) could not be "
            f"determined ({detail}; first: "
            f"{undetermined[0].entity_id or undetermined[0].slug})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
