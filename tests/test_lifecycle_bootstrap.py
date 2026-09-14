"""Importing the in-flight pull requests into the ownership store.

The two properties worth testing hardest are the ones that make this safe to
run against production: it writes NOTHING when it could not read, and a second
run writes nothing at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.lifecycle import bootstrap
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

    # And the writer is never reached: main() returns before apply_report.
    bootstrap.apply_report(report, client)
    assert client.acquires == []


# --- the ladder --------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "legacy", "want_decision", "want_owner_type"),
    [
        (OWNED_BY_OTHER, LEGACY_FREE, bootstrap.DECISION_ALREADY_OWNED, "shepherd"),
        (UNOWNED, LEGACY_OWNED, bootstrap.DECISION_DEVLOOP, "devloop-workflow"),
        # No live DevLoop: AMBIGUOUS, not a shepherd row. Importing one turns
        # an agreeing entity into store-forbids-old-permits for the length of
        # its liveness bound, and nothing heartbeats it.
        (UNOWNED, LEGACY_FREE, bootstrap.DECISION_AMBIGUOUS, ""),
        # The probe could not answer. NOT "no DevLoop": falling through to the
        # shepherd rung would hand it a pull request another machine may be
        # pushing to -- the exact condition the store exists to prevent.
        (UNOWNED, LEGACY_UNKNOWN, bootstrap.DECISION_AMBIGUOUS, ""),
    ],
)
def test_the_import_ladder(verdict, legacy, want_decision, want_owner_type) -> None:
    held = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id="shepherd:mctl-web"),
        state="active",
        healthy=True,
        held=True,
    )
    answer = OwnershipAnswer(verdict=verdict, ownership=held if verdict == OWNED_BY_OTHER else None)
    plan = bootstrap.plan_for(_ref(), "mctlhq/mctl-web#42", answer, legacy, repo="mctlhq/mctl-web")
    assert plan.decision == want_decision
    assert plan.owner_type == want_owner_type


def test_a_skipped_service_is_not_imported_either(monkeypatch) -> None:
    """pr-steward has no lifecycle writer, same as the shepherd.

    The reason names the owner that WOULD own it, so an operator reading the
    report can see what the entity is waiting on rather than just that it was
    skipped.
    """
    from orchestrator import run_shepherd

    monkeypatch.setattr(run_shepherd, "_service_mode", lambda service, **kw: run_shepherd.SKIP)
    plan = bootstrap.plan_for(
        _ref(service="mctl-claude-remote"),
        "mctlhq/mctl-claude-remote#1",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_FREE,
        repo="mctlhq/mctl-claude-remote",
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.owner_id == ""
    assert "pr-steward" in plan.reason
    assert "no lifecycle writer" in plan.reason


def test_the_devloop_rung_names_the_workflow_the_probe_confirmed() -> None:
    """Recording any other owner would name a holder that is not the one
    actually pushing to the branch."""
    plan = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/mctl-web",
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
    )
    second = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/mctl-web",
    )
    assert first.owner_id == second.owner_id
    assert first.owner_id == run_shepherd.devloop_workflow_id("mctl-web", "issue-7-a-thing")


def test_a_second_run_writes_nothing() -> None:
    """The first leg: read first, and an entity the store already holds is
    reported already-owned and never acquired."""
    held = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id="shepherd:mctl-web"),
        state="active",
        healthy=True,
        held=True,
    )
    client = _Client(
        {"mctlhq/mctl-web#42": OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=held)}
    )
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_FREE))
    bootstrap.apply_report(report, client)

    assert [p.decision for p in report.planned] == [bootstrap.DECISION_ALREADY_OWNED]
    assert client.acquires == []


def test_a_first_run_writes_the_planned_rows() -> None:
    client = _Client()
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_OWNED))
    bootstrap.apply_report(report, client)

    assert [a[0] for a in client.acquires] == ["mctlhq/mctl-web#42"]
    assert client.acquires[0][1:3] == ("devloop-workflow", "dev-loop-mctlhq-mctl-web-7")
    assert report.written == ["mctlhq/mctl-web#42"]
    # The provenance goes with the row: "why does this actor own it" is
    # answerable from the record instead of by re-deriving the environment.
    assert client.acquires[0][3]["proposal_ref"] == "mctl-web/issue-7-a-thing"
    assert client.acquires[0][3]["policy_ref"].startswith("service-mode:mctl-web=")


def test_a_refused_acquire_is_reported_not_raised() -> None:
    client = _Client(acquire_answer=OwnershipAnswer(verdict=OWNED_BY_OTHER, reason="409"))
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_OWNED))
    bootstrap.apply_report(report, client)
    assert report.written == []
    assert report.failed[0]["entity_id"] == "mctlhq/mctl-web#42"


# --- the report --------------------------------------------------------


def test_ambiguous_is_reported_and_left_unowned() -> None:
    """ADR-010 §4 asks for a `conflicted` state; types.py defines four states
    and says there deliberately is no such thing -- it would be a DERIVED
    condition needing something to sweep and write it. An unowned entity is one
    the old mechanism still drives, i.e. the status quo, not a new risk."""
    client = _Client()
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_UNKNOWN))
    assert report.planned == []
    assert report.ambiguous[0].decision == bootstrap.DECISION_AMBIGUOUS
    bootstrap.apply_report(report, client)
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


def _writes_allowed(monkeypatch) -> None:
    """--apply refuses below `observe`; every test that writes needs this.

    Set through the environment rather than by patching rollout, so the tests
    exercise the same read the deployment does.
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


def test_a_skipped_service_is_discovered_at_all(tmp_path, monkeypatch, capsys) -> None:
    """The pr-steward rung fires iff the service is SKIP — which is exactly the
    set _discover_refs drops when reconcile is False. Without the fix_only
    override the rung, and the pr-steward arm of owner_for, are dead in
    production and counts["pr-steward"] reads 0 for a reason the report does
    not show."""
    from orchestrator import run_shepherd

    root = _state_dir(tmp_path, "mctl-claude-remote", "issue-7-a-thing")
    # Honours force_fix_only, as the real _service_mode does: that override is
    # exactly what lets discovery see a skipped service, and a fake that
    # ignored it would assert against a function this code does not call.
    monkeypatch.setattr(
        run_shepherd,
        "_service_mode",
        lambda s, force_fix_only=False: run_shepherd.FIX_ONLY if force_fix_only else run_shepherd.SKIP,
    )
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root)])
    report = _report_of(capsys.readouterr().out)
    assert report["counts"]["devloop-workflow"] == 1


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


def test_apply_is_required_to_write(tmp_path, monkeypatch, capsys) -> None:
    from orchestrator import run_shepherd

    _writes_allowed(monkeypatch)

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    client = _Client()
    _install_client(monkeypatch, client)

    bootstrap.main(["--state-dir", str(root)])
    assert client.acquires == [], "a run without --apply wrote to the store"
    capsys.readouterr()

    bootstrap.main(["--state-dir", str(root), "--apply"])
    assert [a[0] for a in client.acquires] == ["mctlhq/mctl-web#42"]


def test_an_unknown_read_aborts_with_a_nonzero_exit(tmp_path, monkeypatch, capsys) -> None:
    from orchestrator import run_shepherd

    _writes_allowed(monkeypatch)

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    client = _Client({"mctlhq/mctl-web#42": OwnershipAnswer(verdict=UNKNOWN, reason="down")})
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 1
    report = _report_of(capsys.readouterr().out)
    assert report["aborted"]
    assert client.acquires == []


def test_the_devloop_row_carries_the_workflow_id(tmp_path, monkeypatch, capsys) -> None:
    """The workflow's own acquire sets temporal_workflow_id; a row written here
    naming the same owner should carry the same field."""
    from orchestrator import run_shepherd

    _writes_allowed(monkeypatch)

    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    client = _Client()
    _install_client(monkeypatch, client)

    bootstrap.main(["--state-dir", str(root), "--apply"])
    assert client.acquires[0][3]["temporal_workflow_id"] == "dev-loop-mctlhq-mctl-web-7"


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

    monkeypatch.setattr("orchestrator.run_shepherd.DEV_LOOP_LIVENESS_BUDGET_S", 0.5)
    refs = [_ref(slug="issue-7-fast", number=1), _ref(slug="issue-8-slow", number=2)]
    try:
        answers = bootstrap.probe_all(refs, probe)
    finally:
        release.set()
    assert answers.get(0) == LEGACY_FREE
    # Absent, read as LEGACY_UNKNOWN through the caller's default.
    assert 1 not in answers


def test_apply_refuses_below_observe(tmp_path, monkeypatch, capsys) -> None:
    """The one switch every other writer in this package honours.

    At `off` the DevLoopWorkflow's ownership activity short-circuits, so a row
    written here for a devloop-workflow owner is never heartbeated, progressed
    or released by the workflow it names: it sits `active` until its liveness
    bound expires, held by an owner that does not know it holds anything.
    """
    from orchestrator import run_shepherd

    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    client = _Client()
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 2
    assert client.acquires == []
    captured = capsys.readouterr()
    assert "refused --apply" in captured.err
    # The refusal emits a report too: the marker exists so a consumer can
    # always find one, and an Argo step reading it as an output parameter must
    # not get nothing on the one path where the mode IS the answer.
    assert bootstrap.REPORT_MARKER in captured.out
    assert "'off'" in captured.out


def test_a_dry_run_is_allowed_below_observe(tmp_path, monkeypatch, capsys) -> None:
    """The refusal is about WRITING. A dry run is how an operator decides
    whether to flip in the first place."""
    from orchestrator import run_shepherd

    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    assert bootstrap.main(["--state-dir", str(root)]) == 0
    assert _report_of(capsys.readouterr().out)["counts"]["devloop-workflow"] == 1


def test_the_scope_can_be_narrowed(tmp_path, monkeypatch, capsys) -> None:
    """apply_report skips an entity the store already holds, so a row written
    wrongly cannot be corrected by re-running. A first --apply must not have to
    be fleet-wide."""
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


def test_a_refused_acquire_reaches_the_exit_code(tmp_path, monkeypatch, capsys) -> None:
    """The Argo step has to fail. Without this, a refactor that moved
    apply_report after the print, or dropped the `failed` check, passes the
    whole suite."""
    from orchestrator import run_shepherd

    _writes_allowed(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(
        monkeypatch, _Client(acquire_answer=OwnershipAnswer(verdict=OWNED_BY_OTHER, reason="409"))
    )
    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 1


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


def test_the_report_carries_what_apply_writes() -> None:
    """The dry run is read "against the store and live Temporal", which needs
    policy_ref and temporal_workflow_id -- three of the six fields
    apply_report writes were absent from it."""
    report = bootstrap.build_report([_ref()], _Client(), _probe(LEGACY_OWNED))
    row = report.as_dict()["planned"][0]
    assert row["proposal_ref"] == "mctl-web/issue-7-a-thing"
    assert row["policy_ref"].startswith("service-mode:")
    assert row["temporal_workflow_id"] == "dev-loop-mctlhq-mctl-web-7"


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


def test_the_report_records_the_attested_mode(tmp_path, monkeypatch, capsys) -> None:
    """The gate reads THIS process's environment, not the worker's. It is an
    operator attestation, not a verification, so what was claimed goes on the
    record next to what it licensed."""
    from orchestrator import run_shepherd

    _writes_allowed(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root), "--apply"])
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
    from orchestrator.lifecycle import shadow

    # What the import fixes.
    driven = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_OWNED,
        repo="mctlhq/mctl-web",
    )
    assert driven.decision == bootstrap.DECISION_DEVLOOP
    before = shadow.classify(OwnershipAnswer(verdict=UNOWNED), shadow.LEGACY_OWNED)
    assert before.divergence_class == shadow.DIVERGE_STORE_PERMITS
    assert before.dangerous is True

    # What it would have broken.
    undriven = bootstrap.plan_for(
        _ref(), "mctlhq/mctl-web#42", OwnershipAnswer(verdict=UNOWNED), LEGACY_FREE,
        repo="mctlhq/mctl-web",
    )
    assert undriven.decision == bootstrap.DECISION_AMBIGUOUS
    assert shadow.classify(
        OwnershipAnswer(verdict=UNOWNED), shadow.LEGACY_FREE
    ).divergence_class == shadow.DIVERGE_AGREE


def test_an_apply_at_enforce_is_refused(tmp_path, monkeypatch, capsys) -> None:
    """records_writes() is at_least(OBSERVE), so it is also true at enforce and
    only — where the shepherd is already CONSUMING the store and a bulk import
    races a live reader. A pre-soak migration has no business running after the
    soak."""
    from orchestrator import run_shepherd

    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_OWNED)
    client = _Client()
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 2
    assert client.acquires == []
    assert "expected 'observe'" in capsys.readouterr().err


def test_an_undetermined_entity_fails_the_run(tmp_path, monkeypatch, capsys) -> None:
    """An entity the run could not get an ANSWER about is the failure.

    Not "nothing was written" — since only live-DevLoop entities are imported,
    writing nothing is the ordinary state of the fleet.
    """
    from orchestrator import run_shepherd

    _writes_allowed(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_UNKNOWN)
    _install_client(monkeypatch, _Client())

    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 1
    assert "could not be determined" in capsys.readouterr().err


def test_an_apply_with_nothing_to_write_succeeds(tmp_path, monkeypatch, capsys) -> None:
    """The correction the shepherd/pr-steward removal forces.

    Only live-DevLoop entities are imported, so "no live DevLoop anywhere right
    now" is the ordinary state of the fleet: every plan is ambiguous, nothing
    is written, and that is the right outcome. The earlier criterion
    (`not report.planned` -> exit 1) condemned exactly this case.
    """
    from orchestrator import run_shepherd

    _writes_allowed(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    client = _Client()
    _install_client(monkeypatch, client)

    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 0
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

    _writes_allowed(monkeypatch)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)

    class _NoToken:
        def get_many(self, kind, phase, ids, asking=None):
            return {i: OwnershipAnswer(verdict=UNKNOWN, reason="MCTL_TOKEN is not set") for i in ids}

        def acquire(self, *a, **kw):
            raise AssertionError("acquired despite an aborted read")

    _install_client(monkeypatch, _NoToken())
    assert bootstrap.main(["--state-dir", str(root), "--apply"]) == 1
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

    _writes_allowed(monkeypatch)
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

    bootstrap.main(["--state-dir", str(root), "--apply"])
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
    )
    assert plan.decision == bootstrap.DECISION_AMBIGUOUS
    assert plan.owner_id == ""
    assert "mctlhq/mctl-web" in plan.reason
