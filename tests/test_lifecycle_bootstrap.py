"""Importing the in-flight pull requests into the ownership store.

The two properties worth testing hardest are the ones that make this safe to
run against production: it writes NOTHING when it could not read, and a second
run writes nothing at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.lifecycle import bootstrap, shadow
from orchestrator.lifecycle.contract import (
    OWNED_BY_OTHER,
    UNKNOWN,
    UNOWNED,
    EntityRef,
    Owner,
    Ownership,
    OwnershipAnswer,
)
from orchestrator.run_shepherd import LEGACY_FREE, LEGACY_OWNED, LEGACY_UNKNOWN, ProposalRef


def _ref(service="mctl-web", slug="issue-7-a-thing", number=42) -> ProposalRef:
    return ProposalRef(
        service=service,
        slug=slug,
        proposal_dir=Path("/tmp/x"),
        status="implemented",
        pr_url=f"https://github.com/mctlhq/{service}/pull/{number}",
    )


class _Client:
    """Stands in for OwnershipClient: scripted reads, recorded writes."""

    def __init__(self, answers=None, acquire_answer=None):
        self._answers = answers or {}
        self._acquire = acquire_answer or OwnershipAnswer(verdict=OWNED_BY_OTHER, accepted=True)
        self.acquires: list[tuple] = []

    def get_many(self, kind, phase, ids, asking=None):
        return {i: self._answers.get(i, OwnershipAnswer(verdict=UNOWNED)) for i in ids}

    def acquire(self, entity, phase, owner, **kw):
        self.acquires.append((entity.id, owner.type, owner.id, kw))
        return self._acquire


def _probe(answer):
    return lambda service, slug: answer


# --- the read-first abort ----------------------------------------------


def test_any_unknown_aborts_the_whole_run_with_nothing_written() -> None:
    """An id that read UNKNOWN is one whose owner cannot be seen.

    Writing an owner over a row you could not read is how a bootstrap takes an
    entity from whoever actually holds it. Aborting costs a re-run; the
    alternative costs an entity. It aborts the WHOLE run, not the one id:
    a store that failed on one id was failing, and the rest of the batch is
    not more trustworthy for having answered.
    """
    client = _Client({"mctlhq/mctl-web#42": OwnershipAnswer(verdict=UNKNOWN, reason="down")})
    report = bootstrap.build_report(
        [_ref(), _ref(slug="issue-8-b", number=43)], client, _probe(LEGACY_FREE)
    )
    assert report.aborted
    assert "mctlhq/mctl-web#42" in report.aborted
    assert report.planned == []

    # Nothing reached the store either way: this build has no writer at all,
    # which is what `_Client.acquires` staying empty across this whole file
    # asserts. The abort's own guarantee -- that a writer added later is never
    # reached on this path -- is pinned where that writer lands.
    assert client.acquires == []


# --- the ladder --------------------------------------------------------


@pytest.mark.parametrize(
    (
        "verdict",
        "held",
        "legacy",
        "want_decision",
        "want_owner_type",
        "want_undetermined",
        "want_retryable",
    ),
    [
        (
            OWNED_BY_OTHER,
            True,
            LEGACY_FREE,
            bootstrap.DECISION_ALREADY_OWNED,
            "shepherd",
            False,
            False,
        ),
        (
            UNOWNED,
            False,
            LEGACY_OWNED,
            bootstrap.DECISION_DEVLOOP,
            "devloop-workflow",
            False,
            False,
        ),
        # No live DevLoop: AMBIGUOUS, not a shepherd row. Importing one turns
        # an agreeing entity into store-forbids-old-permits for the length of
        # its liveness bound, and nothing heartbeats it. NOT undetermined --
        # this is the ordinary outcome and a run made of it is a success.
        (UNOWNED, False, LEGACY_FREE, bootstrap.DECISION_AMBIGUOUS, "", False, False),
        # The probe could not answer. NOT "no DevLoop": falling through to the
        # shepherd rung would hand it a pull request another machine may be
        # pushing to -- the exact condition the store exists to prevent.
        #
        # The ONLY retryable rung: the probe is the one producer of
        # `undetermined` a re-run can plausibly answer differently.
        (UNOWNED, False, LEGACY_UNKNOWN, bootstrap.DECISION_AMBIGUOUS, "", True, True),
    ],
)
def test_the_import_ladder(
    verdict, held, legacy, want_decision, want_owner_type, want_undetermined, want_retryable
) -> None:
    # `held` is now an INPUT to the ladder, evaluated once by build_report and
    # passed in, so it belongs in the table beside the verdict rather than
    # being implied by the record. The two are independent: a holding verdict
    # with held False is a record past its liveness bound, and that row lives
    # in its own tests below.
    record = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id="shepherd:mctl-web"),
        state="active",
        healthy=True,
        held=held,
    )
    answer = OwnershipAnswer(
        verdict=verdict, ownership=record if verdict == OWNED_BY_OTHER else None
    )
    plan = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", answer, legacy, repo="mctlhq/mctl-web", held=held
    )
    assert plan.decision == want_decision
    assert plan.owner_type == want_owner_type
    # The ladder table is the one place all rungs sit together, so it is where
    # the three-way distinction belongs: two rungs report AMBIGUOUS and mean
    # opposite things, and main() branches the exit code on exactly this.
    assert plan.undetermined is want_undetermined
    # And the SECOND field main() reads off a plan. Pinned in the same table
    # for the same reason: `undetermined` says the run is red, `retryable` says
    # what to do about it, and they are set on different rungs. Left unpinned,
    # marking a checkout problem retryable would tell an operator to re-run it
    # forever with this suite still green.
    assert plan.retryable is want_retryable


def _record(*, held, state="active", healthy=True, owner_id="shepherd:mctl-web") -> Ownership:
    return Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id=owner_id),
        state=state,
        healthy=healthy,
        held=held,
    )


def _holding(record: Ownership) -> OwnershipAnswer:
    """What the store answers for a record in a HOLDING state.

    OWNED_BY_OTHER whatever `held` says -- which is the whole point. `state` is
    `active`, and `verdict_for` classifies on the state set, not on liveness.
    """
    return OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=record)


def test_an_already_owned_line_carries_the_records_provenance() -> None:
    """Not this run's.

    `base` is built with POLICY_REF and a proposal_ref derived from the ref
    being imported -- what a row this run WROTE would say. On an already-owned
    line those describe a row written by somebody else, at some other time,
    under some other policy, and the operator reading the dry run line by line
    cannot tell the two apart because every other field on the line is real.
    """
    record = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id="shepherd:mctl-web"),
        state="active",
        healthy=True,
        held=True,
        policy_ref="service-mode:mctl-web=full",
        proposal_ref="mctl-web/issue-3-something-older",
        temporal_workflow_id="dev-loop-mctlhq-mctl-web-3",
    )

    plan = bootstrap.plan_for(
        _ref(),
        "mctlhq/mctl-web#42",
        _holding(record),
        LEGACY_FREE,
        repo="mctlhq/mctl-web",
        held=True,
    )
    assert plan.decision == bootstrap.DECISION_ALREADY_OWNED
    assert plan.policy_ref == "service-mode:mctl-web=full"
    assert plan.proposal_ref == "mctl-web/issue-3-something-older"
    assert plan.temporal_workflow_id == "dev-loop-mctlhq-mctl-web-3"
    assert plan.policy_ref != bootstrap.POLICY_REF


def test_an_already_owned_line_with_no_record_says_nothing_rather_than_guessing() -> None:
    """An absent value is readable as absent; a plausible wrong one is not."""
    plan = bootstrap.plan_for(
        _ref(),
        "mctlhq/mctl-web#42",
        OwnershipAnswer(verdict=OWNED_BY_OTHER),
        LEGACY_FREE,
        repo="mctlhq/mctl-web",
        held=True,
    )
    assert plan.decision == bootstrap.DECISION_ALREADY_OWNED
    assert plan.policy_ref == ""
    assert plan.proposal_ref == ""
    assert plan.temporal_workflow_id == ""


def test_a_dead_record_does_not_stop_the_ladder() -> None:
    """The one shape rung 1 has to get right, and the one the import exists for.

    `verdict_for` answers OWNED_BY_OTHER for ANY record in a holding state,
    healthy or not -- correctly for its own question, "may I act". The shadow
    compare asks a different one: `derived.held`, computed from the takeover
    predicate, which is False for a record left `active` past its liveness
    bound.

    So with a live DevLoop the soak reports `store-permits-old-forbids`, the
    one DANGEROUS class, while a verdict-keyed rung 1 called the same entity
    already-owned and returned. Rung 1 is terminal, so no re-run would revisit
    it: the import would report a clean run over exactly the entities it was
    built to fix.
    """
    plan = bootstrap.plan_for(
        _ref(),
        "mctlhq/mctl-web#42",
        _holding(_record(held=False, healthy=False)),
        LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_DEVLOOP
    assert plan.owner_type == "devloop-workflow"
    assert plan.owner_id == "dev-loop-mctlhq-mctl-web-7"
    assert plan.undetermined is False


def test_a_live_record_still_stops_the_ladder() -> None:
    """The other half, or the fix above would be "never trust the store".

    A record that genuinely holds the entity is left alone even with a live
    DevLoop reported -- the store is authoritative about who holds it, and
    writing over a live owner is how a bootstrap takes an entity from whoever
    actually has it.
    """
    plan = bootstrap.plan_for(
        _ref(),
        "mctlhq/mctl-web#42",
        _holding(_record(held=True)),
        LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=True,
    )
    assert plan.decision == bootstrap.DECISION_ALREADY_OWNED
    assert plan.owner_id == "shepherd:mctl-web"


def test_a_dead_record_with_no_devloop_is_ordinary_ambiguity() -> None:
    """Falling through is not the same as being importable. With nothing
    driving the pull request there is still no owner type with a writer, so
    this is rung 4: ambiguous, NOT undetermined, and a run made of it is a
    success."""
    plan = bootstrap.plan_for(
        _ref(),
        "mctlhq/mctl-web#42",
        _holding(_record(held=False, healthy=False)),
        LEGACY_FREE,
        repo="mctlhq/mctl-web",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.owner_id == ""
    assert plan.undetermined is False


def test_a_record_without_derived_held_is_undetermined_not_already_owned() -> None:
    """An absence, never a third reading of `held`.

    mctl-api predating the `derived` block answers a record with no `held`, and
    `shadow.held` returns None. Classifying that as already-owned is the branch
    that leaves the dangerous class in place silently; classifying it as a dead
    record would write over an owner nobody read. It is undetermined, and
    PERMANENT -- a re-run against the same mctl-api answers the same.

    Loud on purpose: against a stale API every entity takes this branch and the
    whole run goes red, which is the signal, rather than a quiet
    misclassification of the entire fleet.
    """
    plan = bootstrap.plan_for(
        _ref(),
        "mctlhq/mctl-web#42",
        _holding(_record(held=None)),
        LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=None,
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.undetermined is True
    assert plan.retryable is False
    assert "derived.held" in plan.reason


def test_held_ness_is_asked_once_and_decides_both(monkeypatch) -> None:
    """The probe set and rung 1 must come from ONE evaluation.

    This is where the first attempt at the dead-record fix went wrong. Rung 1
    was moved onto `shadow.held` while `build_report` kept selecting the probe
    set on the VERDICT, so a dead `active` row -- OWNED_BY_OTHER -- was never
    probed, reached the ladder with LEGACY_UNKNOWN, fell through rung 1 exactly
    as intended and stopped at rung 2: undetermined, `retryable` claiming a
    probe that was never attempted, and rung 3 unreachable for the one class
    the tool exists to remove. Two readings of one question, one function
    further out than the defect being fixed.

    So the test drives it from `build_report` rather than `plan_for`, and moves
    `shadow.held` under a record that says held=True. If anything re-derives
    held-ness from the record or the verdict, the entity is not probed and the
    decision is already-owned.
    """
    from orchestrator import run_shepherd

    monkeypatch.setattr(shadow, "held", lambda answer: False)
    probed = []

    def probe(service, slug):
        probed.append((service, slug))
        return LEGACY_OWNED

    client = _Client({"mctlhq/mctl-web#42": _holding(_record(held=True))})
    report = bootstrap.build_report([_ref()], client, probe)

    assert probed == [("mctl-web", "issue-7-a-thing")], "a held=False entity was not probed"
    assert [p.decision for p in report.planned] == [bootstrap.DECISION_DEVLOOP]
    assert report.planned[0].owner_id == run_shepherd.devloop_workflow_id(
        "mctl-web", "issue-7-a-thing"
    )


def test_a_free_record_without_derived_held_is_still_probed() -> None:
    """The branch the previous filter dropped.

    `shadow.held` returns `ownership.held` whenever a record is present --
    including for UNOWNED, which `answer_from` gives a released or terminal
    row. So a FREE-state record whose `derived.held` is absent answers UNOWNED
    with held None.

    Keyed on `held is False` alone, that entity was excluded from the probe
    set; and rung 1 declines to answer for it, because its guard also requires
    a holding verdict. It therefore reached rung 2 as undetermined with
    `retryable=True` for a probe that was never attempted -- the same defect
    the dead-`active`-row fix closed, one branch over.

    It must be PROBED, and then decided on the real answer: nothing holds a
    released row, so a live DevLoop is importable.
    """
    probed = []

    def probe(service, slug):
        probed.append(slug)
        return LEGACY_OWNED

    released = _record(held=None, state="released", owner_id="")
    client = _Client(
        {"mctlhq/mctl-web#42": OwnershipAnswer(verdict=UNOWNED, ownership=released)}
    )
    report = bootstrap.build_report([_ref()], client, probe)

    assert probed == ["issue-7-a-thing"], "a free record was dropped from the probe set"
    assert [p.decision for p in report.planned] == [bootstrap.DECISION_DEVLOOP]
    assert report.ambiguous == []


def test_a_free_record_with_no_devloop_is_not_undetermined() -> None:
    """The other half: probed, answered, and NOT red.

    Without the probe this entity was undetermined and retryable, which makes
    the run fail and tells the operator to re-run it. With it, the answer is
    the ordinary rung 4: nothing drives this pull request.
    """
    released = _record(held=None, state="terminal", owner_id="")
    client = _Client(
        {"mctlhq/mctl-web#42": OwnershipAnswer(verdict=UNOWNED, ownership=released)}
    )
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_FREE))

    assert report.planned == []
    assert report.ambiguous[0].undetermined is False
    assert report.ambiguous[0].retryable is False


def test_a_record_the_server_cannot_report_held_for_is_not_probed(monkeypatch) -> None:
    """`is False`, not `is not True`, and the difference is the budget.

    Rung 1 answers undetermined for a None `held` whatever the probe says, so
    probing it spends the budget on an entity already decided. Against an
    mctl-api predating the `derived` block that is EVERY entity: a run that
    should fail fast would burn PROBE_BUDGET_S first and then fail anyway.
    """
    probed = []

    def probe(service, slug):
        probed.append(slug)
        return LEGACY_OWNED

    client = _Client({"mctlhq/mctl-web#42": _holding(_record(held=None))})
    report = bootstrap.build_report([_ref()], client, probe)

    assert probed == [], "an undeterminable record was probed anyway"
    assert report.planned == []
    assert report.ambiguous[0].undetermined is True
    assert report.ambiguous[0].retryable is False


def test_a_skipped_service_is_not_imported_either(monkeypatch) -> None:
    """pr-steward has no lifecycle writer, same as the shepherd.

    The reason says what the entity is WAITING ON rather than just that it was
    skipped -- but it names both candidate owners rather than resolving which,
    and that is deliberate. Resolving it means `policy.default_owner_for`,
    which reads THIS pod's SHEPHERD_SKIP_SERVICES: unset in the bootstrap pod,
    the sentence read `shepherd` for a repository the shepherd skips. Neither
    owner type has a writer, so the distinction changes no decision and is not
    worth stating from the wrong environment.
    """
    from orchestrator import run_shepherd

    monkeypatch.setattr(run_shepherd, "_service_mode", lambda service, **kw: run_shepherd.SKIP)
    plan = bootstrap.plan_for(
        _ref(service="mctl-claude-remote"),
        "mctlhq/mctl-claude-remote#1",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_FREE,
        repo="mctlhq/mctl-claude-remote",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.owner_id == ""
    assert "pr-steward" in plan.reason and "shepherd" in plan.reason
    assert "no lifecycle writer" in plan.reason
    # Not undetermined, and so not retryable: nothing drives this entity and
    # nothing will until its owner type has a writer. A re-run says the same.
    assert plan.undetermined is False
    assert plan.retryable is False


def test_the_devloop_rung_names_the_workflow_the_probe_confirmed() -> None:
    """Recording any other owner would name a holder that is not the one
    actually pushing to the branch."""
    plan = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=False,
    )
    assert plan.owner_id == "dev-loop-mctlhq-mctl-web-7"


# --- idempotence -------------------------------------------------------


def test_owner_ids_are_deterministic() -> None:
    """The second leg of idempotence. A pod name or a timestamp in the owner id
    and every re-run is a fresh owner colliding with the last one's row."""
    from orchestrator import run_shepherd

    first = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=False,
    )
    second = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=False,
    )
    assert first.owner_id == second.owner_id
    assert first.owner_id == run_shepherd.devloop_workflow_id("mctl-web", "issue-7-a-thing")


def test_ambiguous_is_reported_and_left_unowned() -> None:
    """ADR-010 §4 asks for a `conflicted` state; types.py defines four states
    and says there deliberately is no such thing -- it would be a DERIVED
    condition needing something to sweep and write it. An unowned entity is one
    the old mechanism still drives, i.e. the status quo, not a new risk."""
    client = _Client()
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_UNKNOWN))
    assert report.planned == []
    assert report.ambiguous[0].decision == bootstrap.DECISION_AMBIGUOUS
    assert client.acquires == []


def test_two_proposals_on_one_pr_do_not_race_the_decision() -> None:
    client = _Client()
    report = bootstrap.build_report(
        [_ref(slug="issue-7-a"), _ref(slug="issue-8-b")], client, _probe(LEGACY_OWNED)
    )
    assert len(report.planned) == 1
    assert len(report.ambiguous) == 1
    assert "a second proposal maps to" in report.ambiguous[0].reason


def test_an_unreadable_pr_url_is_ambiguous_not_a_crash() -> None:
    ref = _ref()
    ref.pr_url = "not-a-url"
    report = bootstrap.build_report([ref], _Client(), _probe(LEGACY_FREE))
    assert report.planned == []
    assert report.ambiguous[0].decision == bootstrap.DECISION_AMBIGUOUS


def test_the_report_counts_every_decision_including_zeros() -> None:
    """counts.total is the measured decision rate the soak's sample target is
    re-derived from, and a decision that only appears when non-zero cannot be
    told from one that never happened."""
    report = bootstrap.build_report([_ref()], _Client(), _probe(LEGACY_FREE))
    counts = report.as_dict()["counts"]
    for decision in bootstrap.DECISIONS:
        assert decision in counts
    assert counts["total"] == 1


# --- through main(), including the discovery layer ---------------------
#
# Every test above calls build_report/plan_for with hand-built refs, so the
# discovery layer went untested — and two of the three defects the review found
# lived exactly there. These drive main().


def _state_dir(tmp_path: Path, service: str, slug: str, pr: int = 42) -> Path:
    root = tmp_path / "agents-state"
    d = root / service / "proposals" / slug
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(
        f"status: implemented\npr: https://github.com/mctlhq/{service}/pull/{pr}\n"
    )
    return root


def _report_of(out: str) -> dict:
    """The JSON report, found by its marker.

    Not "the first brace" and not "the last object": _discover_refs prints a
    skip notice for every service the shepherd does not own, and a probe thread
    that outlived the budget can print AFTER the report. The marker is the only
    reliable handle, which is why it exists.
    """
    line = next(ln for ln in out.splitlines() if ln.startswith(bootstrap.REPORT_MARKER))
    return json.loads(line[len(bootstrap.REPORT_MARKER):])


def _install_client(monkeypatch, client) -> None:
    monkeypatch.setattr(bootstrap, "OwnershipClient", lambda *a, **kw: client)


def _observe_env(monkeypatch) -> None:
    """The rollout mode this run records. Nothing in this build depends on it.

    There is no writer here, so the mode gates nothing: it is a load-bearing
    process input the report's reader cannot otherwise see, recorded for the
    same reason `skip_services` is. Set through the ENVIRONMENT rather than by
    patching rollout, so the tests exercise the same read the deployment does
    -- which is the read the write path's gate will use.
    """
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")


def test_a_dry_run_does_not_touch_the_gitops_checkout(tmp_path, monkeypatch, capsys) -> None:
    """_discover_refs rewrites .status.yaml when it finds a PR by branch —
    flipping in-progress to implemented — and this tool writes ownership rows,
    never gitops files. A dry run that edited the checkout before the store was
    even read would contradict the `aborted` path's "nothing was written"."""
    from orchestrator import run_shepherd

    root = tmp_path / "agents-state"
    d = root / "mctl-web" / "proposals" / "issue-7-a-thing"
    d.mkdir(parents=True)
    status = d / ".status.yaml"
    status.write_text("status: in-progress\n")  # no `pr:` key -> branch lookup
    before = status.read_text()

    monkeypatch.setattr(
        run_shepherd, "_find_pr_url_by_branch",
        lambda service, slug: "https://github.com/mctlhq/mctl-web/pull/42",
    )
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root)])
    assert status.read_text() == before, "the dry run rewrote .status.yaml"


def test_a_skipped_service_is_still_discovered(tmp_path, monkeypatch, capsys) -> None:
    """SHEPHERD_SKIP_SERVICES must not decide what this imports.

    A DevLoopWorkflow is started per ISSUE and knows nothing about the skip
    set, so a skipped service can have a live DevLoop driving one of its pull
    requests -- exactly the entity this import exists to record. Dropping it at
    discovery leaves the store with no owner for an entity a DevLoop drives:
    store-permits-old-forbids, left in place by a run that exits 0 with a clean
    report.

    fix_only=True is the lever that short-circuits _service_mode before the
    skip set is read, which is what makes the import independent of THIS pod's
    environment.
    """
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = _state_dir(tmp_path, "mctl-claude-remote", "issue-7-a-thing")
    monkeypatch.setattr(
        run_shepherd,
        "_service_mode",
        lambda svc, force_fix_only=False: (
            run_shepherd.FIX_ONLY if force_fix_only else run_shepherd.SKIP
        ),
    )
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    client = _Client()
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root)]) == 0
    report = _report_of(capsys.readouterr().out)
    assert report["counts"]["devloop-workflow"] == 1
    # PLANNED, which is what this build produces. The entity reaching the
    # report at all is the regression this pins: dropped at discovery it
    # appears nowhere and the run still exits 0 with a clean report.
    assert [r["entity_id"] for r in report["planned"]] == ["mctlhq/mctl-claude-remote#42"]


def test_the_report_records_the_discovery_inputs(tmp_path, monkeypatch, capsys) -> None:
    """The skip set is load-bearing input read from this pod's environment, and
    the report's reader cannot see that environment -- the same shape of
    problem as the rollout mode, and it gets the same treatment."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "SHEPHERD_SKIP_SERVICES", frozenset({"a-svc", "b-svc"}))
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root)])
    report = _report_of(capsys.readouterr().out)
    assert report["skip_services"] == "a-svc,b-svc"
    assert report["discovery_ignored_skip_set"] is True


def test_the_counts_include_the_ambiguous_ones(tmp_path, monkeypatch, capsys) -> None:
    """`total` is the measured decision rate the soak's sample target is
    re-derived from. Counting only `planned` left counts["ambiguous"]
    structurally 0 beside a populated list, and excluded those entities from
    the rate entirely."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_UNKNOWN)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root)])
    report = _report_of(capsys.readouterr().out)
    assert report["counts"]["ambiguous"] == 1
    assert report["counts"]["total"] == 1
    assert len(report["ambiguous"]) == 1


def test_apply_is_refused_and_writes_nothing(tmp_path, monkeypatch, capsys) -> None:
    """--apply is accepted by the parser and refused by the program.

    Accepted because the WorkflowTemplate on mctl-gitops main already passes it
    when dry_run=false: a removed flag would fail there as argparse's
    "unrecognized arguments", exit 2 with NO report, which an Argo log cannot
    tell from a deliberate refusal. So the refusal is exit 2 WITH a report
    behind the marker -- and it writes nothing. The entity here is one a real
    apply would write, so a build that quietly wrote it would pass a weaker
    version of this test.
    """
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    client = _Client()
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root)]) == 0
    assert client.acquires == [], "a plan-only build wrote to the store"
    capsys.readouterr()

    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 2
    report = _report_of(capsys.readouterr().out)
    assert "refused --apply" in report["aborted"]
    assert "plans and reports only" in report["aborted"]
    assert client.acquires == [], "--apply wrote in a build with no write path"


def test_an_unknown_read_aborts_with_a_nonzero_exit(tmp_path, monkeypatch, capsys) -> None:
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    client = _Client({"mctlhq/mctl-web#42": OwnershipAnswer(verdict=UNKNOWN, reason="down")})
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root)]) == 1
    report = _report_of(capsys.readouterr().out)
    assert report["aborted"]
    assert client.acquires == []


def test_the_devloop_row_carries_the_workflow_id(tmp_path, monkeypatch, capsys) -> None:
    """The workflow's own acquire sets temporal_workflow_id; a row written here
    naming the same owner should carry the same field."""
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    client = _Client()
    _install_client(monkeypatch, client)

    bootstrap.main(["--state-dir", str(root)])
    report = _report_of(capsys.readouterr().out)
    assert report["planned"][0]["temporal_workflow_id"] == "dev-loop-mctlhq-mctl-web-7"


def test_an_unanswered_probe_becomes_ambiguous_not_a_decision(monkeypatch) -> None:
    """A ref the probe's budget did not answer must not become a decision.

    The fast probe signals an Event the slow one waits on, so the ordering is
    pinned by synchronisation rather than by a 200ms wall clock -- on a loaded
    runner thread-pool startup can lose that race, and the failure would read
    as a regression in probe_all rather than as scheduling noise.
    """
    import threading

    fast_done = threading.Event()
    release = threading.Event()

    def probe(service, slug):
        if slug == "issue-7-fast":
            fast_done.set()
            return LEGACY_FREE
        # Not scheduled until the fast one has answered, and then held past
        # the budget.
        fast_done.wait(timeout=5)
        release.wait(timeout=5)
        return LEGACY_OWNED

    monkeypatch.setattr(bootstrap, "PROBE_BUDGET_S", 0.5)
    refs = [_ref(slug="issue-7-fast", number=1), _ref(slug="issue-8-slow", number=2)]
    try:
        answers = bootstrap.probe_all(refs, probe)
    finally:
        release.set()
    assert answers.get(0) == LEGACY_FREE
    # Absent, read as LEGACY_UNKNOWN through the caller's default.
    assert 1 not in answers


def test_a_report_run_is_allowed_below_observe(tmp_path, monkeypatch, capsys) -> None:
    """A report is how an operator decides whether to flip in the first place,
    so it must run with the variable unset. Pinned here rather than left to
    follow from "there is no gate": the gate lands with the writer, and this is
    the case it must not catch."""
    from orchestrator import run_shepherd

    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    assert bootstrap.main(["--state-dir", str(root)]) == 0
    assert _report_of(capsys.readouterr().out)["counts"]["devloop-workflow"] == 1


def test_an_unknown_service_is_rejected_at_the_argument(tmp_path, capsys) -> None:
    """Not discovered-as-nothing.

    An unknown --service matches no proposal directory, so without this the run
    discovers nothing and exits 1 saying "discovered no proposals at all" --
    whose message sends the operator to check the volume mount for what is a
    typo in their own argument. --service is used precisely on the scoped first
    apply, which is the worst moment for that.
    """
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    assert bootstrap.main(["--state-dir", str(root), "--service", "mctl-wbe"]) == 2
    # WITH a report. exit 2 and nothing on stdout is the one outcome an Argo
    # step reading the report as an output parameter cannot interpret, and the
    # --apply refusal below pays for a report specifically to avoid it.
    report = _report_of(capsys.readouterr().out)
    assert "no service state dir 'mctl-wbe'" in report["aborted"]
    assert "found: mctl-web" in report["aborted"]


def test_a_service_with_no_proposals_dir_is_not_an_argument_error(tmp_path, capsys) -> None:
    """An ordinary state, not a typo.

    A real service that never had a proposal, or had its last one archived,
    has a state dir and no `proposals/`. Refusing it as an argument error would
    exit 2 with no report for something nobody mistyped; it falls through to
    "discovered no proposals at all", which exits 1 WITH one.
    """
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    (root / "mctl-docs").mkdir()

    assert bootstrap.main(["--state-dir", str(root), "--service", "mctl-docs"]) == 1
    captured = capsys.readouterr()
    assert _report_of(captured.out)["aborted"] == ""
    assert "discovered no proposals at all" in captured.err


@pytest.mark.skipif(os.geteuid() == 0, reason="uid 0 bypasses directory read permission")
def test_a_mount_lost_during_discovery_reports(tmp_path, capsys) -> None:
    """The readability check is a snapshot; discovery is the walk.

    `discover_services` proves the state dir listable at one instant, and
    `_discover_refs` then descends into every `proposals/` and reads a
    `.status.yaml` from each. A `proposals/` the pod cannot list, or a mount
    that goes away between the two, raises from in there — and unguarded that
    left `main()` as a traceback: no report, no marker, which is the outcome
    this module's whole exit design is against.
    """
    root = tmp_path / "agents-state"
    proposals = root / "mctl-web" / "proposals"
    proposals.mkdir(parents=True)
    proposals.chmod(0o111)  # passes discover_services, fails the walk
    try:
        assert bootstrap.main(["--state-dir", str(root)]) == 1
        report = _report_of(capsys.readouterr().out)
        assert "discovery failed" in report["aborted"]
    finally:
        proposals.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="uid 0 bypasses directory read permission")
@pytest.mark.parametrize("extra", [[], ["--service", "mctl-web"]])
def test_a_run_that_proceeds_reports_a_fault_at_any_depth(tmp_path, capsys, extra) -> None:
    """The invariant, stated as what it actually is.

    A run that gets past the refusals reports an infrastructure fault wherever
    in the walk it happens — including one directory down, where
    `discover_services` admits a service on a `stat` of its `proposals/` and
    only the walk's own guard can see. That guard is what closes the depth gap,
    and it is why the earlier root-only test could not.

    NOT "one mount, one answer whatever the flags". That claim was broader than
    the code and broader than it should be: `--apply` in this build is refused
    unconditionally, and its refusal is a complete answer to what was asked —
    the flag does nothing here. The mount is reported by the re-run the refusal
    asks for, and putting the refusal behind discovery to avoid that cost the
    production invocation a full fleet walk, with a `gh pr list` per PR-less
    proposal, before printing one sentence.
    """
    root = tmp_path / "agents-state"
    proposals = root / "mctl-web" / "proposals"
    proposals.mkdir(parents=True)
    proposals.chmod(0o111)
    try:
        assert bootstrap.main(["--state-dir", str(root), *extra]) == 1
        report = _report_of(capsys.readouterr().out)
        assert "discovery failed" in report["aborted"], extra
    finally:
        proposals.chmod(0o755)


@pytest.mark.skipif(os.geteuid() == 0, reason="uid 0 bypasses directory read permission")
def test_a_refused_run_does_not_learn_about_the_walks_faults(tmp_path, capsys) -> None:
    """The OTHER half of the ordering rule, asserted rather than only written.

    An unreadable `proposals/` is visible to nothing cheaper than the walk, and
    the walk runs after the refusal. So `--apply` answers "this flag does
    nothing in this build" and says NOTHING about the mount — deliberately,
    because the run did nothing and that is a complete answer to what was
    asked. The re-run the refusal asks for is what finds it, which the
    fleet-wide parameter of the test above pins.

    Written down here because the prose said it and no test did: the three
    flag shapes that once compared the refusal against an infrastructure fault
    were dropped when the rule changed, leaving the ordering asserted only in a
    docstring — and asserted in the OPPOSITE direction by the missing-state-dir
    test, which is a cheap fault and so genuinely does outrank the refusal.
    Both are true; they are different rungs of the same rule.
    """
    root = tmp_path / "agents-state"
    proposals = root / "mctl-web" / "proposals"
    proposals.mkdir(parents=True)
    proposals.chmod(0o111)
    try:
        assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 2
        aborted = _report_of(capsys.readouterr().out)["aborted"]
        assert "refused --apply" in aborted
        assert "discovery failed" not in aborted
        assert "cannot read" not in aborted
    finally:
        proposals.chmod(0o755)


def test_the_refusal_does_not_wait_for_the_walk(tmp_path, monkeypatch, capsys) -> None:
    """`--apply` answers without reading the checkout.

    The refusal is deterministic — there is no writer in this build, so nothing
    discovery learns can change it — while discovery shells `gh pr list` per
    PR-less proposal, serially and unbudgeted. Behind it, the production
    invocation walked the whole fleet before printing one sentence.
    """
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")

    def _explode(*a, **kw):
        raise AssertionError("discovery ran before the refusal")

    monkeypatch.setattr(run_shepherd, "_discover_refs", _explode)
    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 2
    assert "refused --apply" in _report_of(capsys.readouterr().out)["aborted"]


@pytest.mark.skipif(os.geteuid() == 0, reason="uid 0 bypasses directory read permission")
def test_an_unreadable_state_dir_reports_on_a_fleet_wide_run(tmp_path, capsys) -> None:
    """The shape an operator is most likely to run, and the one the check
    originally skipped.

    `is_dir()` stats and `iterdir()` lists, so a `--x` directory passes the
    guard and raises `PermissionError` out of the walk. Handled only inside the
    `--service` block, a fleet-wide run — which passes no service — never
    reached it, and the exception left `main()` as a traceback: no report, no
    marker, on the invocation the WorkflowTemplate actually makes.
    """
    root = tmp_path / "agents-state"
    (root / "mctl-web" / "proposals").mkdir(parents=True)
    root.chmod(0o111)  # traversable, not listable
    try:
        assert bootstrap.main(["--state-dir", str(root)]) == 1
        report = _report_of(capsys.readouterr().out)
        # THE PROPERTY -- a report exists and says the run could not read the
        # checkout -- not which arm wrote it. Asserting the exact wording
        # pinned the message rather than the invariant, and the invariant is
        # what must survive a guard moving.
        assert report["aborted"], "a fleet-wide run over an unreadable mount carried no report"
        assert str(root) in report["aborted"]
    finally:
        root.chmod(0o755)


def test_a_missing_state_dir_reports_rather_than_exiting_bare(tmp_path, capsys) -> None:
    """An infrastructure fault, answered with a REPORT.

    `--apply --service mctl-web` in a pod whose gitops volume failed to mount
    is not an argument error, and `parser.error` gave it exit 2 with nothing on
    stdout — byte-identical, to an Argo step reading the report as an output
    parameter, to the deliberate `--apply` refusal.

    Exit 1 with a report is not much better if the report is missing, which is
    where the first fix left it: the mount that went away MID-run was reported
    and the mount that never arrived exited through `_discover_refs`'s bare
    `SystemExit`. To the consumer this whole area is written for, those two
    fail identically. The earlier version of this test asserted only the exit
    code, so it pinned the absence of the report rather than catching it.
    """
    argv = ["--state-dir", str(tmp_path / "absent"), "--service", "mctl-web"]
    assert bootstrap.main(argv) == 1
    captured = capsys.readouterr()
    report = _report_of(captured.out)
    assert "state dir not found" in report["aborted"]
    assert "ABORTED" in captured.err

    # AND under --apply. A MISSING state dir is a cheap fault — a stat — so it
    # is answered before the refusal and wins: exit 1, not exit 2. That is rung
    # 2 of the ordering rule in bootstrap.py, and it is not in tension with
    # `test_a_refused_run_does_not_learn_about_the_walks_faults`, which pins
    # rung 3: a fault only the WALK can see is reported after the refusal, so a
    # refused run never reaches it.
    assert bootstrap.main([*argv, "--apply"]) == 1
    assert "state dir not found" in _report_of(capsys.readouterr().out)["aborted"]


def test_the_filter_and_the_discovery_share_one_definition(tmp_path, monkeypatch, capsys) -> None:
    """A name the filter admits is a name discovery accepts, and vice versa.

    Two answers to "is this a service" is how a --service the loop never
    accepts reaches "discovered no proposals at all", which reads as a
    mis-mounted volume rather than as an argument. And how a directory that is
    not a service -- editor droppings, a stray archive -- reaches an id
    builder, which is where the ValueError in `workflow_id_for` came from.

    Asserted by MOVING the shared function: patching it moves both, so a second
    definition on either side fails here while every value-level test stays
    green.
    """
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    (root / "_scratch").mkdir()
    (root / "notes.txt").write_text("not a service")
    (root / "half-a-clone").mkdir()  # no proposals/ -- structurally not a service

    assert run_shepherd.discover_services(root) == frozenset({"mctl-web"})

    # Moving the shared function moves DISCOVERY, which is the load-bearing
    # half: the same run over the same checkout now finds nothing. A second
    # definition on either side fails here while every value-level test stays
    # green.
    _install_client(monkeypatch, _Client())
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    assert bootstrap.main(["--state-dir", str(root), "--service", "mctl-web"]) == 0

    monkeypatch.setattr(run_shepherd, "discover_services", lambda d: frozenset())
    assert bootstrap.main(["--state-dir", str(root), "--service", "mctl-web"]) == 1


def test_a_directory_that_is_not_a_service_is_not_one(tmp_path) -> None:
    """Structural, not "everything that is not underscore-prefixed".

    Replacing the SERVICES check with "any directory" re-opened the class that
    produced the `workflow_id_for` ValueError: a checkout holds things that are
    not services, and a name that is not a valid repository name must not reach
    an id builder in the first place.
    """
    from orchestrator import run_shepherd

    root = tmp_path / "agents-state"
    (root / "mctl-web" / "proposals").mkdir(parents=True)
    (root / "not a repo name").mkdir()
    (root / "_archive" / "proposals").mkdir(parents=True)

    assert run_shepherd.discover_services(root) == frozenset({"mctl-web"})
    assert run_shepherd.discover_services(tmp_path / "absent") == frozenset()


def test_a_service_outside_SERVICES_is_still_addressable(tmp_path, monkeypatch, capsys) -> None:
    """The checkout is the authority, not `config.settings.SERVICES`.

    Discovery walks every directory under the state dir that does not begin
    with `_`, so a repository with proposals but no SERVICES entry --
    mctl-claude-remote, whose pull requests another lifecycle drives -- is
    discoverable. Validating the flag against SERVICES would make it the one
    thing an operator cannot scope to.
    """
    from config.settings import SERVICES
    from orchestrator import run_shepherd

    assert "mctl-claude-remote" not in SERVICES, "the premise of this test"

    root = _state_dir(tmp_path, "mctl-claude-remote", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    _install_client(monkeypatch, _Client())

    assert bootstrap.main(["--state-dir", str(root), "--service", "mctl-claude-remote"]) == 0
    assert _report_of(capsys.readouterr().out)["counts"]["total"] == 1


def test_the_scope_can_be_narrowed(tmp_path, monkeypatch, capsys) -> None:
    """An apply skips an entity the store already holds, so a row written
    wrongly cannot be corrected by re-running. A first apply must not have to
    be fleet-wide -- and neither must the report an operator reviews first."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    (root / "other-svc" / "proposals" / "issue-9-b").mkdir(parents=True)
    (root / "other-svc" / "proposals" / "issue-9-b" / ".status.yaml").write_text(
        "status: implemented\npr: https://github.com/mctlhq/other-svc/pull/9\n"
    )
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root), "--service", "mctl-web"])
    report = _report_of(capsys.readouterr().out)
    assert [p["entity_id"] for p in report["planned"]] == ["mctlhq/mctl-web#42"]


def test_an_already_owned_entity_is_not_probed(tmp_path, monkeypatch, capsys) -> None:
    """The budget is shared wall-clock. Probing entities whose answer rung 1
    discards starves the undecided ones, which then fall to UNKNOWN and are
    reported ambiguous -- left UNOWNED, the very class this tool removes."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    probed: list[str] = []

    def _probe(service, slug):
        probed.append(slug)
        return LEGACY_FREE

    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", _probe)
    held = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id="shepherd:mctl-web"),
        state="active",
        healthy=True,
        held=True,
    )
    _install_client(
        monkeypatch,
        _Client({"mctlhq/mctl-web#42": OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=held)}),
    )

    bootstrap.main(["--state-dir", str(root)])
    assert probed == [], "probed an entity the store already holds"


def test_a_raising_probe_produces_a_report_not_a_traceback() -> None:
    """`probe` is a parameter, so its totality is the caller's guarantee and
    not probe_all's. A raise must become one unanswered ref, never no report."""

    def _explodes(service, slug):
        raise RuntimeError("anything at all")

    assert bootstrap.probe_all([_ref()], _explodes) == {}


def test_an_aborted_report_carries_no_counts() -> None:
    """`total` is the number the soak's sample target is re-derived from, and
    build_report returns before the plan loop on the abort path -- so any count
    there is a partial scan of a run that decided nothing."""
    client = _Client({"mctlhq/mctl-web#42": OwnershipAnswer(verdict=UNKNOWN, reason="down")})
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_FREE))
    assert report.as_dict()["counts"] is None


def test_the_report_carries_the_provenance_a_write_would_need() -> None:
    """The report is read "against the store and live Temporal", which needs
    policy_ref and temporal_workflow_id -- three of the six fields a write
    carries were absent from it."""
    report = bootstrap.build_report([_ref()], _Client(), _probe(LEGACY_OWNED))
    row = report.as_dict()["planned"][0]
    assert row["proposal_ref"] == "mctl-web/issue-7-a-thing"
    assert row["policy_ref"] == "lifecycle-bootstrap:devloop-live"
    assert row["temporal_workflow_id"] == "dev-loop-mctlhq-mctl-web-7"


def test_the_policy_ref_does_not_encode_this_pods_environment(monkeypatch) -> None:
    """policy_ref names the policy that decided the ROW, not the importer.

    It used to be `policy.policy_ref_for(service)`, i.e.
    `service-mode:{service}={mode}` off run_shepherd._service_mode, which reads
    the SHEPHERD_SKIP_SERVICES / SHEPHERD_FIX_ONLY_SERVICES of whatever pod ran
    the import. Unset in the bootstrap pod -- the default the WorkflowTemplate
    was written against -- a row for mctl-claude-remote recorded
    `service-mode:mctl-claude-remote=full` while the shepherd resolves that
    service to `skip`, and `full` is the mode meaning discover, fix AND merge.
    Durable, and on the one field policy.py's docstring says exists so an
    operator need not reconstruct the deciding process's environment.

    The test MOVES the environment under an otherwise identical run. Asserting
    the value once would hold just as well for the old code on a pod where the
    two happened to agree; asserting it is the same across three service modes
    is what the old code could not do.
    """
    from orchestrator import run_shepherd

    refs = []
    for mode in (run_shepherd.FULL, run_shepherd.SKIP, run_shepherd.FIX_ONLY):
        monkeypatch.setattr(
            run_shepherd, "_service_mode", lambda svc, force_fix_only=False, m=mode: m
        )
        report = bootstrap.build_report(
            [_ref(service="mctl-claude-remote")], _Client(), _probe(LEGACY_OWNED)
        )
        refs.append(report.as_dict()["planned"][0]["policy_ref"])
    assert refs == ["lifecycle-bootstrap:devloop-live"] * 3


def test_the_workflow_id_is_adapted_not_re_derived(monkeypatch) -> None:
    """`dev-loop-{owner}-{repo}-{issue}` has ONE definition, and it is the one
    Temporal starts executions with.

    `issue_ref.workflow_id_for` is what `temporal.start` names the execution
    with. `devloop_workflow_id` translates a proposal into the issue URL that
    function takes; it does not re-derive the id. Transcribed here, a future
    scheme or validation change would let the liveness probe -- and this
    bootstrap, which writes the value DURABLY into temporal_workflow_id -- name
    a workflow Temporal never started.

    Moving `workflow_id_for` has to move the id. Re-deriving it from an
    f-string would leave the value correct today and this test failing, which
    is the point: the formats agreeing is not the property worth pinning.
    """
    from orchestrator import run_shepherd

    monkeypatch.setattr(run_shepherd, "workflow_id_for", lambda url: f"moved::{url}")
    assert run_shepherd.devloop_workflow_id("mctl-web", "issue-7-a-thing") == (
        "moved::https://github.com/mctlhq/mctl-web/issues/7"
    )


def test_the_devloop_org_has_one_definition() -> None:
    """The check and the thing it checks must be the same string.

    `issue_ref` owns the org: its URL regex is built from `ISSUE_URL_ORG`, and
    `run_shepherd` re-exports rather than re-spells it. The bootstrap refuses a
    pull request whose repository is not `{org}/{service}` before writing a row
    naming that workflow, so a second spelling would be a check able to
    disagree with the thing it checks -- and it would fail quietly, refusing
    every entity as "in another repository", a red run whose reason names the
    wrong cause.

    Identity, not equality: two equal literals is exactly the state this is
    against.
    """
    from orchestrator import run_shepherd
    from orchestrator.temporal import issue_ref

    assert run_shepherd.DEVLOOP_WORKFLOW_ORG is issue_ref.ISSUE_URL_ORG

    # And the guard resolves through it rather than through a literal.
    accepted = bootstrap.plan_for(
        _ref(service="mctl-web"),
        f"{issue_ref.ISSUE_URL_ORG}/mctl-web#42",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_OWNED,
        repo=f"{issue_ref.ISSUE_URL_ORG}/mctl-web",
        held=False,
    )
    assert accepted.decision == bootstrap.DECISION_DEVLOOP
    assert accepted.owner_id == issue_ref.workflow_id_for(
        f"https://github.com/{issue_ref.ISSUE_URL_ORG}/mctl-web/issues/7"
    )

    refused = bootstrap.plan_for(
        _ref(service="mctl-web"),
        "otherorg/mctl-web#42",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_OWNED,
        repo="otherorg/mctl-web",
        held=False,
    )
    assert refused.decision == bootstrap.DECISION_AMBIGUOUS
    assert refused.undetermined is True


def test_the_workflow_id_has_one_definition() -> None:
    """The bootstrap writes this value DURABLY into temporal_workflow_id, and
    its guarantee -- "the id recorded is the one the probe just confirmed
    alive" -- holds only while the probe and this agree."""
    from orchestrator import run_shepherd

    report = bootstrap.build_report([_ref()], _Client(), _probe(LEGACY_OWNED))
    assert report.planned[0].owner_id == run_shepherd.devloop_workflow_id(
        "mctl-web", "issue-7-a-thing"
    )


def test_the_report_is_findable_among_other_output(tmp_path, monkeypatch, capsys) -> None:
    """stdout is not ours alone. _discover_refs prints a skip notice per
    unowned service, and a probe thread past its budget can print after the
    report -- so neither the first brace nor the last object finds it."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    (root / "skipped-svc" / "proposals").mkdir(parents=True)
    monkeypatch.setattr(
        run_shepherd,
        "_service_mode",
        lambda svc, force_fix_only=False: (
            run_shepherd.SKIP if svc == "skipped-svc" and not force_fix_only else run_shepherd.FULL
        ),
    )
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root)])
    out = capsys.readouterr().out
    assert out.count(bootstrap.REPORT_MARKER) == 1
    assert _report_of(out)["counts"]["total"] == 1


def test_the_report_records_the_rollout_mode(tmp_path, monkeypatch, capsys) -> None:
    """The mode is read from THIS process's environment while the hazard it
    speaks to belongs to the Temporal worker -- an operator attestation, not a
    verification. Recorded before anything depends on it, so the write path's
    gate has a field to point at rather than one to add."""
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root)])
    assert _report_of(capsys.readouterr().out)["rollout_mode"] == "observe"


def test_the_abort_path_still_summarises(tmp_path, monkeypatch, capsys) -> None:
    """The one outcome the summary exists for was the one it skipped."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    _install_client(
        monkeypatch,
        _Client({"mctlhq/mctl-web#42": OwnershipAnswer(verdict=UNKNOWN, reason="down")}),
    )
    assert bootstrap.main(["--state-dir", str(root)]) == 1
    assert "ABORTED" in capsys.readouterr().err


def test_a_pr_outside_the_org_gets_no_devloop_row() -> None:
    """devloop_workflow_id hardcodes the org, which is fail-open on the PROBE
    -- a wrong owner just 404s into "not owned". On the write path the id
    becomes owner_id and temporal_workflow_id, so a wrong owner is a durable
    row naming a workflow that does not exist."""
    ref = _ref()
    ref.pr_url = "https://github.com/someone/a-fork/pull/42"
    plan = bootstrap.plan_for(
        ref, "someone/a-fork#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="someone/a-fork",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.owner_id == ""
    assert "someone/a-fork" in plan.reason


def test_slug_narrows_across_services(tmp_path, monkeypatch, capsys) -> None:
    """--slug without --service narrows across every service, matching
    _discover_refs. Worth pinning: read as a per-service filter it looks like a
    no-op when used alone."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    (root / "other-svc" / "proposals" / "issue-7-a-thing").mkdir(parents=True)
    (root / "other-svc" / "proposals" / "issue-7-a-thing" / ".status.yaml").write_text(
        "status: implemented\npr: https://github.com/mctlhq/other-svc/pull/9\n"
    )
    (root / "other-svc" / "proposals" / "issue-8-b").mkdir(parents=True)
    (root / "other-svc" / "proposals" / "issue-8-b" / ".status.yaml").write_text(
        "status: implemented\npr: https://github.com/mctlhq/other-svc/pull/8\n"
    )
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root), "--slug", "issue-7-a-thing"])
    planned = {p["entity_id"] for p in _report_of(capsys.readouterr().out)["planned"]}
    assert planned == {"mctlhq/mctl-web#42", "mctlhq/other-svc#9"}


def test_only_devloop_rows_are_imported() -> None:
    """The measurement, walked end to end.

    shadow.classify has one owner-type arm and it is devloop-workflow. An
    entity the old mechanism does not drive reads `agree` with no row; import a
    shepherd row and the same entity reads store-forbids-old-permits until the
    row dies at its liveness bound and reads `agree` again — restored by the
    row rotting, not by anything being right.

    So the import is only worth making where it removes the DANGEROUS class,
    which is where a live DevLoop drives an entity the store has no live owner
    for.
    """
    # What the import fixes.
    driven = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=False,
    )
    assert driven.decision == bootstrap.DECISION_DEVLOOP
    before = shadow.classify(OwnershipAnswer(verdict=UNOWNED), shadow.LEGACY_OWNED)
    assert before.divergence_class == shadow.DIVERGE_STORE_PERMITS
    assert before.dangerous is True

    # What it would have broken.
    undriven = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_FREE,
        repo="mctlhq/mctl-web",
        held=False,
    )
    assert undriven.decision == bootstrap.DECISION_AMBIGUOUS
    assert shadow.classify(
        OwnershipAnswer(verdict=UNOWNED), shadow.LEGACY_FREE
    ).divergence_class == shadow.DIVERGE_AGREE


def test_an_undetermined_entity_fails_the_run(tmp_path, monkeypatch, capsys) -> None:
    """An entity the run could not get an ANSWER about is the failure.

    Not "nothing was written" — since only live-DevLoop entities are imported,
    writing nothing is the ordinary state of the fleet.
    """
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_UNKNOWN)
    _install_client(monkeypatch, _Client())

    assert bootstrap.main(["--state-dir", str(root)]) == 1
    assert "could not be determined" in capsys.readouterr().err


def test_a_run_with_nothing_to_write_succeeds(tmp_path, monkeypatch, capsys) -> None:
    """The correction the shepherd/pr-steward removal forces.

    Only live-DevLoop entities are imported, so "no live DevLoop anywhere right
    now" is the ordinary state of the fleet: every plan is ambiguous, nothing
    is written, and that is the right outcome. The earlier criterion
    (`not report.planned` -> exit 1) condemned exactly this case.
    """
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    client = _Client()
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root)]) == 0
    assert client.acquires == []
    report = _report_of(capsys.readouterr().out)
    assert report["counts"]["ambiguous"] == 1
    # The two ambiguity causes are distinguishable on the wire, which is what
    # makes the exit code above defensible.
    assert report["ambiguous"][0]["undetermined"] is False


def test_a_missing_token_aborts_rather_than_looking_empty(tmp_path, monkeypatch, capsys) -> None:
    """The scenario an earlier comment here claimed was silent.

    It is not: the store read uses the same token, OwnershipClient raises
    without one, every id reads UNKNOWN, and the run aborts with nothing
    written. The loud path was always the loud path.
    """
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)

    class _NoToken:
        def get_many(self, kind, phase, ids, asking=None):
            return {i: OwnershipAnswer(verdict=UNKNOWN, reason="MCTL_TOKEN is not set") for i in ids}

        def acquire(self, *a, **kw):
            raise AssertionError("acquired despite an aborted read")

    _install_client(monkeypatch, _NoToken())
    assert bootstrap.main(["--state-dir", str(root)]) == 1
    err = capsys.readouterr().err
    assert "ABORTED" in err


def test_discovering_no_proposals_is_not_success(tmp_path, monkeypatch, capsys) -> None:
    """A checkout mounted one level off reads exactly like a clean import."""
    root = tmp_path / "agents-state"
    root.mkdir()
    _install_client(monkeypatch, _Client())
    assert bootstrap.main(["--state-dir", str(root)]) == 1
    assert "discovered no proposals" in capsys.readouterr().err


def test_the_summary_separates_already_owned_from_work(tmp_path, monkeypatch, capsys) -> None:
    """The idempotent second run printed "312 planned, 0 written", which in a
    log tail is indistinguishable from 312 rows the store refused."""
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    held = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="devloop-workflow", id="dev-loop-mctlhq-mctl-web-7"),
        state="active",
        healthy=True,
        held=True,
    )
    _install_client(
        monkeypatch,
        _Client({"mctlhq/mctl-web#42": OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=held)}),
    )

    bootstrap.main(["--state-dir", str(root)])
    err = capsys.readouterr().err
    assert "0 to write, 1 already owned" in err


def test_a_pr_url_that_is_not_a_pull_request_is_rejected() -> None:
    """_parse_pr_url walks four segments from the right and validates neither
    the host nor the route, so `.../issues/42` parses cleanly into the entity
    id of pull request 42 — a different thing entirely.

    On the read side that was fail-open: the shepherd 404s and moves on. Here
    it would acquire a durable row against a real but unrelated entity.
    """
    for bad in (
        "https://github.com/mctlhq/mctl-web/issues/42",
        "https://example.invalid/a/b/c/42",
        "https://github.com/mctlhq/mctl-web/pull/notanumber",
    ):
        ref = _ref()
        ref.pr_url = bad
        report = bootstrap.build_report([ref], _Client(), _probe(LEGACY_OWNED))
        assert report.planned == [], bad
        assert report.ambiguous[0].undetermined is True
        assert report.ambiguous[0].retryable is False
        assert "pull request" in report.ambiguous[0].reason

    for good in (
        "https://github.com/mctlhq/mctl-web/pull/42",
        "https://api.github.com/repos/mctlhq/mctl-web/pulls/42",
    ):
        ref = _ref()
        ref.pr_url = good
        report = bootstrap.build_report([ref], _Client(), _probe(LEGACY_OWNED))
        assert [p.entity_id for p in report.planned] == ["mctlhq/mctl-web#42"], good


def test_the_whole_repo_must_match_the_service() -> None:
    """devloop_workflow_id builds `dev-loop-mctlhq-{service}-{N}` from the
    proposal's directory, which is the DevLoop's id only while the service name
    and the repo name are the same string. Checking the org alone let a
    proposal under agents-state/mctl-web whose `pr:` points at
    mctlhq/something-else be imported as an owner naming another repository's
    workflow.
    """
    plan = bootstrap.plan_for(
        _ref(service="mctl-web"),
        "mctlhq/something-else#42",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_OWNED,
        repo="mctlhq/something-else",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.owner_id == ""
    assert "mctlhq/mctl-web" in plan.reason
    # UNDETERMINED, and this is the load-bearing half: a live DevLoop is
    # confirmed driving this entity and we decline to name it, so the store is
    # left with no live owner while a DevLoop drives the PR -- the dangerous
    # class, left in place by the run meant to remove it.
    assert plan.undetermined is True
    assert plan.retryable is False


@pytest.mark.parametrize(
    "pr_url",
    [
        "https://github.com/mctlhq/mctl-web/pull/42",
        # Miscased, which is the input that used to be argued about. GitHub
        # treats owner and repo case-insensitively; the ownership store does
        # not, since the id is an opaque string. Every component therefore has
        # to build the SAME one, and the answer this repo has settled on is
        # "the `pr:` field verbatim" -- `PRState.repo` is `pr_match.group(1)`,
        # never re-read from the API's full_name (pr_state.py), DevLoop's
        # acquire is `state.repo or ""` (dev_loop.py), and `entity_id_for_pr`
        # is verbatim too.
        "https://github.com/MCTLHQ/MCTL-Web/pull/42",
        "https://api.github.com/repos/mctlhq/mctl-web/pulls/42",
    ],
)
def test_this_module_and_the_shadow_compare_build_one_id(pr_url) -> None:
    """The property canonicalising the id would have broken.

    A row written under an id the shadow compare does not construct is
    invisible to `compare_proposal_refs`: it reads no record and classifies
    store-permits-old-forbids, dangerous — the class the module docstring opens
    by naming as the thing to remove. And rung 1 would miss a row
    DevLoopWorkflow already holds and plan a duplicate. Rung 1 is terminal, so
    no re-run revisits either.

    So the id is the `pr:` field verbatim, on both sides, and this pins the two
    together rather than each against a literal.
    """
    from orchestrator.lifecycle import shadow as shadow_mod
    from orchestrator.run_shepherd import _parse_pr_url

    ref = _ref()
    ref = ProposalRef(
        service=ref.service, slug=ref.slug, proposal_dir=ref.proposal_dir,
        status=ref.status, pr_url=pr_url,
    )
    report = bootstrap.build_report([ref], _Client(), _probe(LEGACY_FREE))

    owner, repo, number = _parse_pr_url(pr_url)
    assert report.ambiguous[0].entity_id == shadow_mod.entity_id_for_pr(owner, repo, int(number))


def test_the_repo_check_is_case_insensitive() -> None:
    """GitHub treats owner and repository names case-insensitively, so a `pr:`
    spelled mctlhq/MCTL-Web names the same repository and must not be refused
    over it."""
    ref = _ref()
    ref.pr_url = "https://github.com/mctlhq/MCTL-Web/pull/42"
    plan = bootstrap.plan_for(
        ref, "mctlhq/MCTL-Web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/MCTL-Web",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_DEVLOOP
    assert plan.owner_id == "dev-loop-mctlhq-mctl-web-7"


def test_a_confirmed_devloop_we_cannot_name_fails_the_run(tmp_path, monkeypatch, capsys) -> None:
    """Both of rung 3's refusals leave the DANGEROUS class in place.

    LEGACY_OWNED means a live DevLoop is confirmed. Declining to record an
    owner for it leaves the store with no live owner while a DevLoop drives the
    pull request -- `store-permits-old-forbids`, which is the one thing this
    import exists to remove. A run that hit it must be red, not quietly
    ambiguous like "nobody drives this".
    """
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = tmp_path / "agents-state"
    d = root / "mctl-web" / "proposals" / "issue-7-a-thing"
    d.mkdir(parents=True)
    # The PR is in another repository than the proposal's DevLoop id is built for.
    (d / ".status.yaml").write_text(
        "status: implemented\npr: https://github.com/mctlhq/something-else/pull/42\n"
    )
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    client = _Client()
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root)]) == 1
    assert client.acquires == []
    assert "could not be determined" in capsys.readouterr().err


def test_an_unrepresentable_service_through_the_real_probe(tmp_path, monkeypatch) -> None:
    """What an operator actually gets, not what the ladder would say in
    isolation.

    In production `build_report` is called with `probe=_dev_loop_owns_answer`,
    and that function answers LEGACY_UNKNOWN for exactly the inputs that make
    `devloop_workflow_id` raise -- by design, since an exception there would
    take the whole sweep tick down. So the entity never reaches rung 3's
    permanent refusal: it stops at rung 2, and rung 2 is where the permanence
    has to be named.

    Asserted through the REAL probe rather than a double, because that
    interaction is the finding: a test calling `plan_for` with LEGACY_OWNED in
    hand exercises the one input `build_report` cannot produce for this shape.
    """
    from orchestrator import run_shepherd

    monkeypatch.setenv("MCTL_TOKEN", "t")
    # `_ref` already builds the matching pr_url from the service, so nothing
    # needs overriding -- and `ProposalRef` is not frozen, so the
    # `object.__setattr__` that used to be here implied a frozenness it does
    # not have while doing nothing.
    ref = _ref(service="not a repo name")

    report = bootstrap.build_report([ref], _Client(), run_shepherd._dev_loop_owns_answer)

    # A report, not an abort: one bad directory must not take the run with it.
    assert report.aborted == ""
    assert report.planned == []
    plan = report.ambiguous[0]
    assert plan.undetermined is True
    # The reason names the CAUSE, not the messenger: the probe answered fine,
    # it answered UNKNOWN because there was no id to ask about.
    assert "is not a usable repository name" in plan.reason
    assert "Rename the directory" in plan.reason
    # PERMANENT. Retryable here sends an operator to re-run a directory name
    # the same checkout reproduces exactly, and `main()` counts it under
    # "retryable" in the summary that tells them which to do.
    assert plan.retryable is False


def test_an_unrepresentable_service_is_undetermined_not_a_crash() -> None:
    """`devloop_workflow_id` validates now, and the unvalidated half of the URL
    it builds is the SERVICE, not the slug.

    `_discover_refs` takes the service from any directory under the state dir
    that does not begin with `_`, never checks it against SERVICES, and this
    module passes fix_only=True so every directory is discovered whatever its
    mode resolves to. A directory name outside `parse_issue_url`'s
    `[A-Za-z0-9_.-]+` therefore raises -- and `build_report`'s plan loop has no
    handler, so unhandled it would abort the whole report rather than fail for
    its own entity.

    Undetermined and PERMANENT: a live DevLoop is confirmed and we are
    declining to name it, and a re-run over the same checkout reproduces it.
    """
    plan = bootstrap.plan_for(
        _ref(service="not a repo name"),
        "mctlhq/not a repo name#42",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_OWNED,
        repo="mctlhq/not a repo name",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.undetermined is True
    assert plan.retryable is False
    assert plan.owner_id == ""
    assert "workflow id cannot be built" in plan.reason


def test_a_slug_with_no_workflow_id_under_a_live_devloop_is_undetermined() -> None:
    plan = bootstrap.plan_for(
        _ref(slug="incident-9-oom"),
        "mctlhq/mctl-web#42",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_OWNED,
        repo="mctlhq/mctl-web",
        held=False,
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.undetermined is True
    assert plan.retryable is False
    assert plan.owner_id == ""


def test_a_duplicate_mapping_is_undetermined() -> None:
    """Two proposals on one pull request: the run cannot tell which is
    authoritative, so whatever the winner decided is a coin toss the report
    must not present as settled."""
    report = bootstrap.build_report(
        [_ref(slug="issue-7-a"), _ref(slug="issue-8-b")], _Client(), _probe(LEGACY_OWNED)
    )
    dupes = [p for p in report.ambiguous if "a second proposal maps to" in p.reason]
    assert len(dupes) == 1
    assert dupes[0].undetermined is True
    assert dupes[0].retryable is False


def test_the_summary_separates_transient_from_permanent(tmp_path, monkeypatch, capsys) -> None:
    """The two need opposite responses: a probe that could not answer is worth
    re-running, a malformed `pr:` is a data problem the same run reproduces."""
    from orchestrator import run_shepherd

    _observe_env(monkeypatch)
    root = tmp_path / "agents-state"
    for slug, pr in (
        ("issue-7-a-thing", "https://github.com/mctlhq/mctl-web/pull/42"),
        ("issue-8-b-thing", "https://github.com/mctlhq/mctl-web/issues/43"),
    ):
        d = root / "mctl-web" / "proposals" / slug
        d.mkdir(parents=True)
        (d / ".status.yaml").write_text(f"status: implemented\npr: {pr}\n")

    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_UNKNOWN)
    _install_client(monkeypatch, _Client())

    assert bootstrap.main(["--state-dir", str(root)]) == 1
    err = capsys.readouterr().err
    assert "1 retryable, 1 needing a fix in the checkout" in err


def test_the_probe_budget_is_this_modules_own() -> None:
    """Not the sweep's.

    DEV_LOOP_LIVENESS_BUDGET_S is 60s because the sweep runs every five
    minutes and an unanswered ref is FREE there — the shepherd fails open for
    that proposal and asks again next tick. Here an unanswered ref is
    undetermined, which makes the run red, so inheriting a bound sized for "we
    will ask again shortly" would make budget exhaustion the expected outcome
    on a large fleet: the report comes back full of undetermined entities, the
    run exits 1, and the re-run races the same clock.
    """
    from orchestrator import run_shepherd

    assert bootstrap.PROBE_BUDGET_S > run_shepherd.DEV_LOOP_LIVENESS_BUDGET_S


def test_the_probe_concurrency_is_this_modules_own_too() -> None:
    """A budget raised over an inherited worker count moves nothing.

    Throughput is workers x budget / timeout. At the sweep's 8 workers a
    degraded mctl-api answering at the 10s probe timeout capped this pass at
    240 refs whatever PROBE_BUDGET_S said, and everything past that read
    LEGACY_UNKNOWN -- undetermined, so red. The PAIR has to move, which is why
    both constants live here and this asserts the arithmetic, not the number.
    """
    from orchestrator import run_shepherd

    assert bootstrap.PROBE_WORKERS > run_shepherd.DEV_LOOP_LIVENESS_WORKERS
    degraded_ceiling = (
        bootstrap.PROBE_WORKERS * bootstrap.PROBE_BUDGET_S / run_shepherd.DEV_LOOP_LIVENESS_TIMEOUT_S
    )
    assert degraded_ceiling >= 400
