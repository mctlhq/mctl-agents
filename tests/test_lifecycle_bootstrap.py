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
        (UNOWNED, LEGACY_FREE, bootstrap.DECISION_SHEPHERD, "shepherd"),
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


def test_a_skipped_service_belongs_to_pr_steward(monkeypatch) -> None:
    from orchestrator import run_shepherd

    monkeypatch.setattr(run_shepherd, "_service_mode", lambda service: run_shepherd.SKIP)
    plan = bootstrap.plan_for(
        _ref(service="mctl-claude-remote"),
        "mctlhq/mctl-claude-remote#1",
        OwnershipAnswer(verdict=UNOWNED),
        LEGACY_FREE,
        repo="mctlhq/mctl-claude-remote",
    )
    assert plan.decision == bootstrap.DECISION_PR_STEWARD
    assert plan.owner_id == "pr-steward:mctlhq/mctl-claude-remote"


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
    """The second leg of idempotence. A pod name or a timestamp here and every
    re-run is a fresh owner colliding with the last one's row."""
    first = bootstrap.owner_for(
        bootstrap.DECISION_SHEPHERD, service="mctl-web", repo="mctlhq/mctl-web"
    )
    second = bootstrap.owner_for(
        bootstrap.DECISION_SHEPHERD, service="mctl-web", repo="mctlhq/mctl-web"
    )
    assert first == second == Owner(type="shepherd", id="shepherd:mctl-web")


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
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_FREE))
    bootstrap.apply_report(report, client)

    assert [a[0] for a in client.acquires] == ["mctlhq/mctl-web#42"]
    assert client.acquires[0][1:3] == ("shepherd", "shepherd:mctl-web")
    assert report.written == ["mctlhq/mctl-web#42"]
    # The provenance goes with the row: "why does this actor own it" is
    # answerable from the record instead of by re-deriving the environment.
    assert client.acquires[0][3]["proposal_ref"] == "mctl-web/issue-7-a-thing"
    assert client.acquires[0][3]["policy_ref"].startswith("service-mode:mctl-web=")


def test_a_refused_acquire_is_reported_not_raised() -> None:
    client = _Client(acquire_answer=OwnershipAnswer(verdict=OWNED_BY_OTHER, reason="409"))
    report = bootstrap.build_report([_ref()], client, _probe(LEGACY_FREE))
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
        [_ref(slug="issue-7-a"), _ref(slug="issue-8-b")], client, _probe(LEGACY_FREE)
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
    """The JSON report, which main() prints after any discovery notices.

    Extracted rather than assumed to be the whole of stdout: _discover_refs
    prints skip notices, and an operator reads this from an Argo log where it
    is mixed with everything else anyway.
    """
    return json.loads(out[out.index("{"):])


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
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    _install_client(monkeypatch, _Client())

    bootstrap.main(["--state-dir", str(root)])
    report = _report_of(capsys.readouterr().out)
    assert report["counts"]["pr-steward"] == 1
    assert report["planned"][0]["owner_id"] == "pr-steward:mctlhq/mctl-claude-remote"


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
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
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
    """A ref the probe's budget did not answer must not become a decision."""
    import threading

    release = threading.Event()

    def slow(service, slug):
        if slug == "issue-7-fast":
            return LEGACY_FREE
        release.wait(timeout=5)
        return LEGACY_OWNED

    monkeypatch.setattr("orchestrator.run_shepherd.DEV_LOOP_LIVENESS_BUDGET_S", 0.2)
    refs = [_ref(slug="issue-7-fast", number=1), _ref(slug="issue-8-slow", number=2)]
    try:
        answers = bootstrap.probe_all(refs, slow)
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
    assert "refusing --apply" in capsys.readouterr().err


def test_a_dry_run_is_allowed_below_observe(tmp_path, monkeypatch, capsys) -> None:
    """The refusal is about WRITING. A dry run is how an operator decides
    whether to flip in the first place."""
    from orchestrator import run_shepherd

    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    root = _state_dir(tmp_path, "mctl-web", "issue-7-a-thing")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
    _install_client(monkeypatch, _Client())

    assert bootstrap.main(["--state-dir", str(root)]) == 0
    assert _report_of(capsys.readouterr().out)["counts"]["shepherd"] == 1


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
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
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
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda s, sl: LEGACY_FREE)
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
