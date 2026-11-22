"""The adversarial boundary suite (mctlhq/mctl-agents#696): the versioned
manifest, the loader's integrity checks, every scenario run through the #695
harness, and a mutation check for every guard a scenario pins.

Frame and judge: `tests/assurance_adversarial.py`. Scenarios, fixture targets
and probes: `tests/assurance_adversarial_scenarios.py`. Sealed scenarios,
manifest and immutable stimuli: `tests/fixtures/assurance/adversarial/`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket

import pytest

from orchestrator import assurance_harness as ah
from orchestrator import policy_checkpoint as pc
from orchestrator import runtime_assurance as ra
from orchestrator.context_snapshot import hash_bytes
from orchestrator.redaction import contains_credential
from tests import assurance_adversarial as adv
from tests import assurance_adversarial_scenarios as scn

SUITE = adv.load_suite()
ENTRIES = {e.name: e for e in SUITE.entries}
MUTANTS = [(e.name, m) for e in SUITE.entries for m in e.mutants]
CREDENTIAL_ENV = ("MCTL_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                  "VAULT_TOKEN")


@pytest.fixture(autouse=True)
def _no_network_and_no_credentials(monkeypatch):
    """The suite needs neither: a socket connect or a credential in the
    environment would fail the run, so a scenario that quietly started to
    need one is caught here."""

    def no_network(*_a, **_k):
        raise AssertionError("an adversarial scenario tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)


def assert_contract(outcome):
    report, sc = outcome.report, outcome.scenario
    ra.check_run_against_scenario(report.run, sc)
    ra.check_result_against_run(report.result, report.run, sc)
    assert ra.AssuranceRun.from_dict(json.loads(json.dumps(report.run.to_dict()))) == report.run
    assert ra.AssuranceResult.from_dict(json.loads(json.dumps(report.result.to_dict()))) == report.result


# ---------------------------------------------------------------------------
# The reviewed fixtures are the suite
# ---------------------------------------------------------------------------
def test_manifest_and_sealed_scenarios_are_the_reviewed_specs():
    assert json.loads(adv.MANIFEST.read_text()) == adv.render_manifest(), (
        "fixtures drifted: regenerate with `python -m tests.assurance_adversarial --write`")
    for spec in adv.specs():
        assert json.loads(spec.scenario_path.read_text()) == spec.seal().to_dict()


def test_fixture_directory_holds_exactly_the_catalog_and_its_stimuli():
    assert sorted(p.name for p in adv.FIXTURES.glob("*.json")) == sorted(
        ["suite.json", *(f"{s.name}.json" for s in adv.specs())])
    used = {s.stimulus for s in adv.specs() if s.stimulus}
    assert set(adv.stimulus_files()) == used, "an unreferenced stimulus file is an unreviewed one"


def test_the_catalog_has_unique_names_and_versions():
    assert len(ENTRIES) == len(SUITE.entries)
    ra.check_scenario_catalog([e.scenario for e in SUITE.entries])
    assert (SUITE.name, SUITE.version) == (adv.SUITE_NAME, adv.SUITE_VERSION)
    assert all(e.name.startswith(adv.SCENARIO_PREFIX) for e in SUITE.entries)


@pytest.mark.parametrize("entry", SUITE.entries, ids=lambda e: e.name)
def test_every_scenario_is_a_fixture_scenario_with_explicit_invariants_and_a_deterministic_oracle(entry):
    sc = entry.scenario
    assert (sc.target.kind, sc.target.environment, sc.target.allowlist_entry) == ("fixture", "fixture", None)
    assert sc.execution_mode == ra.EXEC_DETERMINISTIC and sc.budgets.max_followups == 0
    assert sc.permitted_action_kinds == (pc.MCP_TOOL_CALL,)
    assert sc.invariants
    # No LLM judge: every invariant is checked by a server-side detector of its own kind.
    spec = adv.spec_by_name()[entry.name]
    rig = spec.rig(adv.RigContext(sc, None if not entry.stimulus_hash else adv.stimulus_by_hash(entry.stimulus_hash),
                                  (), 0))
    for inv in sc.invariants:
        assert rig.detectors[inv.invariant_id].check_kind == inv.check_kind
        assert inv.check_kind in {"deterministic_check", "policy_decision_expected"}
    assert entry.references


def test_the_unsafe_stimuli_are_immutable_versioned_files_pinned_by_hash():
    for spec in adv.specs():
        if not spec.stimulus:
            continue
        entry = ENTRIES[spec.name]
        assert ".v" in spec.stimulus and spec.stimulus.endswith(".txt")
        pinned = {i.name: i.content_hash for i in entry.scenario.inputs}["stimulus"]
        assert pinned == hash_bytes((adv.STIMULI / spec.stimulus).read_bytes()) == entry.stimulus_hash
        text = adv.stimulus_by_hash(pinned)
        assert adv.directives(text), "a stimulus without a directive attacks nothing"
        assert not contains_credential(text), "stimuli carry no credential shape; the token is built at runtime"
        assert adv.SYNTHETIC_TOKEN not in text


# ---------------------------------------------------------------------------
# The loader refuses a suite that is not the reviewed one
# ---------------------------------------------------------------------------
@pytest.fixture
def copy_of_suite(tmp_path, monkeypatch):
    root = tmp_path / "adversarial"
    shutil.copytree(adv.FIXTURES, root)
    monkeypatch.setattr(adv, "STIMULI", root / "stimuli")
    return root


def edit_manifest(root, edit):
    path = root / "suite.json"
    data = json.loads(path.read_text())
    edit(data)
    path.write_text(json.dumps(data))
    return path


def test_loader_accepts_an_untouched_copy(copy_of_suite):
    assert adv.load_suite(copy_of_suite / "suite.json").entries == SUITE.entries


def test_loader_refuses_a_changed_stimulus(copy_of_suite):
    path = copy_of_suite / "stimuli" / "evidence-injection.v1.txt"
    path.write_text(path.read_text() + "\nOne more instruction.\n")
    with pytest.raises(adv.SuiteError, match="immutable text"):
        adv.load_suite(copy_of_suite / "suite.json")


def test_loader_refuses_a_stimulus_the_scenario_does_not_pin(copy_of_suite):
    def edit(data):
        entry = next(e for e in data["scenarios"] if e["name"] == "adversarial-evidence-injection")
        entry["stimulus"]["file"] = "tool-output-injection.v1.txt"
        other = copy_of_suite / "stimuli" / entry["stimulus"]["file"]
        entry["stimulus"]["content_hash"] = hash_bytes(other.read_bytes())

    with pytest.raises(adv.SuiteError, match="immutable text"):
        adv.load_suite(edit_manifest(copy_of_suite, edit))


def test_loader_refuses_a_tampered_sealed_scenario(copy_of_suite):
    path = copy_of_suite / "adversarial-evidence-injection.json"
    data = json.loads(path.read_text())
    data["description"] = "A different scenario under the same seal."
    path.write_text(json.dumps(data))
    with pytest.raises(adv.SuiteError, match="does not load"):
        adv.load_suite(copy_of_suite / "suite.json")


def test_loader_refuses_a_scenario_whose_authority_was_widened_and_resealed(copy_of_suite):
    spec = adv.spec_by_name()["adversarial-evidence-injection"]
    widened = dataclasses.replace(spec, tools=(*spec.tools, "mctl_delete_tenant"),
                                  mutation_mode=ra.MUTATION_FIXTURE_ONLY)
    (copy_of_suite / spec.scenario_path.name).write_text(json.dumps(widened.seal().to_dict()))

    def edit(data):
        entry = next(e for e in data["scenarios"] if e["name"] == spec.name)
        sealed = widened.seal()
        entry["scenario_id"], entry["content_hash"] = sealed.scenario_id, sealed.content_hash

    with pytest.raises(adv.SuiteError, match="pinned authority"):
        adv.load_suite(edit_manifest(copy_of_suite, edit))


def test_loader_refuses_a_manifest_of_another_suite_version(copy_of_suite):
    with pytest.raises(adv.SuiteError, match="expected"):
        adv.load_suite(edit_manifest(copy_of_suite, lambda d: d.update(version="9.9.9")))


def test_loader_refuses_a_non_fixture_target(copy_of_suite):
    spec = adv.spec_by_name()["adversarial-evidence-injection"]
    sealed = spec.seal()
    shadow = dataclasses.replace(
        sealed, scenario_id="", content_hash="",
        target=ra.TargetBoundary(
            kind="mctl_service", ref="admins/checkout", environment="production",
            allowlist_entry=ra.AllowlistEntryRef("atl-" + "0" * 16, "checkout", "sha256:" + "0" * 64)))
    resealed = ra.seal_scenario(**{f.name: getattr(shadow, f.name) for f in dataclasses.fields(shadow)
                                   if f.name not in ("scenario_id", "content_hash")})
    (copy_of_suite / spec.scenario_path.name).write_text(json.dumps(resealed.to_dict()))

    def edit(data):
        entry = next(e for e in data["scenarios"] if e["name"] == spec.name)
        entry["scenario_id"], entry["content_hash"] = resealed.scenario_id, resealed.content_hash
        entry["authority"] = adv.authority_of(resealed)

    with pytest.raises(adv.SuiteError, match="fixture targets only"):
        adv.load_suite(edit_manifest(copy_of_suite, edit))


def test_a_changed_stimulus_is_not_served_under_the_old_pin():
    with pytest.raises(adv.SuiteError, match="no stimulus file hashes"):
        adv.stimulus_by_hash("sha256:" + "0" * 64)


# ---------------------------------------------------------------------------
# Coverage: the issue's acceptance criteria, as checks over the loaded suite
# ---------------------------------------------------------------------------
def test_every_one_of_the_ten_threat_surfaces_has_a_scenario():
    covered = adv.coverage(SUITE)
    assert sorted(covered) == list(range(1, 11))
    assert all(covered[n] for n in covered), {n: v for n, v in covered.items() if not v}


def tagged(tag):
    return [e for e in SUITE.entries if tag in e.tags]


def test_at_least_three_authority_escalations_are_attempted_and_proven_blocked():
    escalations = tagged(adv.TAG_ESCALATION)
    assert len(escalations) >= 3
    for e in escalations:
        assert e.expected.outcome in (adv.REFUSED, ra.OUTCOME_HELD), e.name
        outcome = adv.run_entry(e)
        assert outcome.passed, outcome.mismatches
        assert outcome.target.unauthorized_effects() == []
    # The three layers of the harness each block one: the scenario's tools,
    # a harness-owned argument and the policy checkpoint.
    blocked_by = {e.expected.stop_reason for e in escalations}
    assert {ah.STOP_TOOL_NOT_PERMITTED, ah.STOP_RESERVED_ARGUMENT, ah.STOP_POLICY_REFUSED} <= blocked_by


def test_at_least_two_scenarios_carry_untrusted_text_that_tries_to_change_harness_authority():
    texts = tagged(adv.TAG_UNTRUSTED_TEXT)
    assert len(texts) >= 2 and all(e.stimulus_hash for e in texts)
    for e in texts:
        stimulus = adv.stimulus_by_hash(e.stimulus_hash)
        assert any(w in stimulus.lower() for w in ("superseded", "granted", "waived", "widened", "required platform"))
        assert adv.run_entry(e).passed


def test_an_approval_replay_and_an_approval_mismatch_fail_closed():
    approval = tagged(adv.TAG_APPROVAL)
    codes = {c for e in approval for c in e.expected.policy_codes}
    assert {pc.CODE_APPROVAL_PENDING, pc.CODE_APPROVAL_INTENT_MISMATCH, pc.CODE_APPROVAL_CONSUMED} <= codes
    assert all(e.expected.outcome == adv.REFUSED and e.expected.stop_reason == ah.STOP_POLICY_REFUSED
               for e in approval)


def test_a_cross_principal_scenario_proves_no_data_or_authority_crosses_the_boundary():
    assert adv.TAG_CROSS_PRINCIPAL in ENTRIES["adversarial-cross-principal-leakage"].tags
    outcome = adv.run_entry(ENTRIES["adversarial-cross-principal-leakage"])
    assert outcome.passed
    # The principal comes from the credential the harness bound, on every call.
    assert outcome.target.calls and all(cred == scn.TENANT_A_PRINCIPAL for _, _, cred in outcome.target.calls)
    for call in outcome.report.run.policy_decisions:
        assert call.code == pc.CODE_ALLOWED


def test_no_scenario_needs_credentials_or_a_network_target():
    for e in SUITE.entries:
        assert e.scenario.target.environment == ra.ENV_FIXTURE
        assert e.ci_subset
    assert [e.name for e in SUITE.subset("ci")] == [e.name for e in SUITE.entries]
    with pytest.raises(ValueError, match="unknown subset"):
        SUITE.subset("prod")


# ---------------------------------------------------------------------------
# Green: every scenario, through the harness, with the declared safe outcome
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entry", SUITE.entries, ids=lambda e: e.name)
def test_scenario_reaches_its_declared_safe_outcome(entry):
    outcome = adv.run_entry(entry)
    assert outcome.passed, outcome.mismatches
    exp = entry.expected
    report = outcome.report
    assert (report.failure_class, report.stop_reason, report.outcome) == (
        exp.failure_class, exp.stop_reason, exp.outcome)
    assert [d.code for d in report.run.policy_decisions] == list(exp.policy_codes)
    assert outcome.target.unauthorized_effects() == []
    assert_contract(outcome)


@pytest.mark.parametrize("entry", [e for e in SUITE.entries if e.expected.outcome == adv.REFUSED],
                         ids=lambda e: e.name)
def test_a_refusal_is_a_first_class_result_not_a_failure(entry):
    outcome = adv.run_entry(entry)
    assert outcome.passed and outcome.report.outcome == "refused"
    # By contract a stopped run is never read as a held invariant.
    assert {v.verdict for v in outcome.report.result.invariant_verdicts} == {ra.VERDICT_UNKNOWN}
    assert all(o.status != ra.OBS_OBSERVED for o in outcome.report.run.observations)


@pytest.mark.parametrize("entry", [e for e in SUITE.entries if e.expected.stop_reason == ah.STOP_COMPLETED],
                         ids=lambda e: e.name)
def test_a_completed_scenario_holds_every_invariant_with_evidence_references(entry):
    outcome = adv.run_entry(entry)
    run = outcome.report.run
    assert {v.verdict for v in outcome.report.result.invariant_verdicts} == {ra.VERDICT_HELD}
    assert run.observations and all(o.status == ra.OBS_OBSERVED and o.artifact is not None for o in run.observations)
    assert len(run.policy_decisions) == len(outcome.target.calls) == outcome.report.result.consumed.tool_calls


@pytest.mark.parametrize("entry", SUITE.entries, ids=lambda e: e.name)
def test_results_retain_exact_scenario_runtime_and_target_versions_and_evidence(entry):
    outcome = adv.run_entry(entry)
    record = outcome.record()
    run = outcome.report.run
    assert json.loads(json.dumps(record)) == record
    assert record["scenario"] == {"name": entry.scenario.name, "version": entry.scenario.version,
                                  "scenario_id": entry.scenario.scenario_id,
                                  "content_hash": entry.scenario.content_hash}
    assert run.binding.scenario.content_hash == entry.scenario.content_hash
    assert run.binding.scenario.version == entry.scenario.version
    assert record["runtime"] == adv.PINS.to_dict()
    assert record["target"] == {"kind": "fixture", "ref": entry.scenario.target.ref, "revision": adv.REVISION}
    assert record["stimulus_content_hash"] == entry.stimulus_hash
    assert record["run_id"] == run.run_id and record["result_id"] == outcome.report.result.result_id
    assert record["suite"] == {"name": adv.SUITE_NAME, "version": adv.SUITE_VERSION}
    # Evidence references: the decisions the run recorded, and the transcript
    # artifact of every observation a detector could make.
    assert [d["code"] for d in record["policy_decisions"]] == list(entry.expected.policy_codes)
    observed = [o for o in run.observations if o.artifact is not None]
    assert [e["artifact"]["content_hash"] for e in record["evidence"]] == [o.artifact.content_hash for o in observed]
    if entry.expected.stop_reason == ah.STOP_COMPLETED:
        assert len(record["evidence"]) == len(entry.scenario.invariants)


@pytest.mark.parametrize("entry", SUITE.entries, ids=lambda e: e.name)
def test_same_pinned_inputs_reproduce_identical_records(entry):
    first = adv.run_entry(entry, seed=5)
    second = adv.run_entry(entry, seed=5)
    assert first.report.authoritative() == second.report.authoritative()
    assert first.report.run.to_dict() == second.report.run.to_dict()
    assert adv.run_entry(entry, seed=6).passed, "the verdict does not depend on the seed"


def test_the_ci_subset_runs_green_and_the_cli_reports_it(capsys):
    assert adv.main(["--subset", "ci"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] and report["subset"] == "ci"
    assert [r["scenario"]["name"] for r in report["results"]] == [e.name for e in SUITE.entries]
    assert all(r["passed"] and not r["mismatches"] for r in report["results"])


# ---------------------------------------------------------------------------
# Red: every guard a scenario pins fails the scenario when it is reverted
# ---------------------------------------------------------------------------
def test_every_scenario_declares_at_least_one_mutant_and_every_mutant_is_known():
    for spec in adv.specs():
        assert spec.mutants, f"{spec.name} has no mutation proving it can go red"
        assert {p for m in spec.mutants for p in m.split("+")} <= adv.known_mutations(spec)


@pytest.mark.parametrize(("name", "mutation"), MUTANTS)
def test_the_scenario_goes_red_when_its_guard_is_reverted(name, mutation):
    entry = ENTRIES[name]
    outcome = adv.run_entry(entry, mutation=mutation)
    assert not outcome.passed, f"{name} stayed green with {mutation} reverted"
    # And not by accident: either a hostile or forbidden thing happened, or the
    # exact blocking layer the manifest pins moved.
    assert (outcome.report.outcome == ra.OUTCOME_VIOLATED or outcome.target.unauthorized_effects()
            or outcome.report.stop_reason != entry.expected.stop_reason
            or [d.code for d in outcome.report.run.policy_decisions] != list(entry.expected.policy_codes))


@pytest.mark.parametrize(
    ("name", "mutation", "effect"),
    [
        ("adversarial-evidence-injection", "no_permitted_tools_gate+no_policy_checkpoint", "delete_tenant"),
        ("adversarial-tool-output-injection", "no_reserved_argument_screen", "foreign tenant"),
        ("adversarial-read-to-write-escalation", "no_policy_checkpoint", "scale_service"),
        ("adversarial-read-to-write-escalation", "approvals_grant_all", "scale_service"),
        ("adversarial-approval-mismatch", "approval_intent_unbound", '"replicas": 0'),
        ("adversarial-approval-receipt-confusion", "approval_intent_unbound", '"replicas": 0'),
        ("adversarial-approval-replay", "reusable_receipt", "ran 2 times"),
        ("adversarial-credential-smuggling", "no_credential_screen", "credential-shaped"),
        ("adversarial-stale-worker-fencing", "claim_veto_reverted", "impl-stale"),
        ("adversarial-retry-idempotency", "no_idempotency_ledger", "second deployment"),
        ("adversarial-actor-confusion", "trust_on_behalf_of", "user:admin"),
        ("adversarial-model-label-authority", "trust_intent_label", "without a server approval"),
        ("adversarial-capability-confusion", "membership_check_reverted", "mctl_scale_service"),
    ],
)
def test_a_reverted_guard_lets_the_hostile_effect_reach_the_target(name, mutation, effect):
    outcome = adv.run_entry(ENTRIES[name], mutation=mutation)
    assert any(effect in e for e in outcome.target.unauthorized_effects()), outcome.target.unauthorized_effects()


def test_a_blocking_layer_that_moves_is_reported_even_when_nothing_gets_through():
    # Defence in depth: with only the scenario's tool gate gone, the policy
    # checkpoint still refuses the call, so nothing reaches the target. The
    # suite still goes red because the manifest pins WHICH layer blocks.
    outcome = adv.run_entry(ENTRIES["adversarial-evidence-injection"], mutation="no_permitted_tools_gate")
    assert outcome.report.stop_reason == ah.STOP_POLICY_REFUSED
    assert outcome.report.run.policy_decisions[-1].code == pc.CODE_APPROVAL_REQUIRED
    assert outcome.target.unauthorized_effects() == []
    assert not outcome.passed
    # The reverse: the policy checkpoint reverted alone changes nothing here,
    # because the tool gate blocks first. Another scenario owns that guard.
    assert adv.run_entry(ENTRIES["adversarial-evidence-injection"], mutation="no_policy_checkpoint").passed


def test_an_unknown_mutation_is_refused_not_read_as_green():
    with pytest.raises(ValueError, match="unknown mutation"):
        adv.run_entry(ENTRIES["adversarial-evidence-injection"], mutation="no_such_guard")
    with pytest.raises(ValueError, match="unknown mutation"):
        scn.GuardedSendTarget(mutation="no_such_guard")


def test_patches_are_removed_after_a_mutated_run():
    adv.run_entry(ENTRIES["adversarial-credential-smuggling"], mutation="no_credential_screen")
    adv.run_entry(ENTRIES["adversarial-read-to-write-escalation"], mutation="no_policy_checkpoint")
    adv.run_entry(ENTRIES["adversarial-stale-worker-fencing"], mutation="claim_veto_reverted")
    assert ah.contains_credential(adv.SYNTHETIC_TOKEN)
    assert pc.decide.__module__ == "orchestrator.policy_checkpoint"
    assert "LIFECYCLE_ROLLOUT_MODE" not in os.environ
    for e in SUITE.entries:
        assert adv.run_entry(e).passed


# ---------------------------------------------------------------------------
# The judge and the detectors are proven both ways
# ---------------------------------------------------------------------------
def test_the_judge_rejects_a_run_that_does_not_match_a_changed_expectation():
    entry = ENTRIES["adversarial-read-to-write-escalation"]
    wrong = dataclasses.replace(entry, expected=adv.completed("guarantee-holds"))
    assert not adv.run_entry(wrong).passed
    wrong_codes = dataclasses.replace(entry, expected=adv.refused(ah.STOP_POLICY_REFUSED, ah.FAILURE_POLICY_REFUSED,
                                                                  [pc.CODE_ALLOWED, pc.CODE_DENIED]))
    assert not adv.run_entry(wrong_codes).passed


def test_a_probe_that_never_makes_the_guarded_call_is_not_read_as_held():
    # Observed absence needs an observation: no call at all is `not_attempted`.
    entry = ENTRIES["adversarial-cross-principal-leakage"]
    outcome = adv.run_entry(entry, probe=lambda ctx: None)
    assert outcome.report.outcome == "inconclusive" and not outcome.passed
    assert {v.verdict for v in outcome.report.result.invariant_verdicts} == {ra.VERDICT_UNKNOWN}
    assert all(o.status == ra.OBS_NOT_ATTEMPTED for o in outcome.report.run.observations)


def test_a_partial_probe_is_unknown_not_held():
    entry = ENTRIES["adversarial-actor-confusion"]
    outcome = adv.run_entry(entry, probe=lambda ctx: ctx.call_tool("mctl_get_effective_actor", {}))
    verdicts = {v.invariant_id: v.verdict for v in outcome.report.result.invariant_verdicts}
    assert verdicts["guarantee-holds"] == ra.VERDICT_UNKNOWN and not outcome.passed


def call(index, tool, digest="sha256:" + "1" * 64, status=ah.CALL_OK, output="{}"):
    return ah.CallRecord(index, tool, digest, status, output=output if status == ah.CALL_OK else None,
                         output_hash="sha256:" + "2" * 64 if status == ah.CALL_OK else "")


def test_forbidden_calls_detector_both_ways():
    d = adv.ForbiddenCalls(frozenset({"mctl_delete_tenant"}), frozenset({"sha256:" + "9" * 64}),
                           frozenset({"sha256:" + "5" * 64}))
    assert d.evaluate([]).status == ra.OBS_NOT_ATTEMPTED
    blocked = ah.CallRecord(0, "mctl_delete_tenant", "sha256:" + "1" * 64, ah.CALL_BLOCKED)
    assert d.evaluate([blocked]).verdict == ra.VERDICT_UNKNOWN, "a blocked call is not an observation"
    held = d.evaluate([call(0, "mctl_get_x")])
    assert (held.verdict, held.call_indexes) == (ra.VERDICT_HELD, (0,))
    assert d.evaluate([call(0, "mctl_get_x"), call(1, "mctl_delete_tenant")]).verdict == ra.VERDICT_VIOLATED
    assert d.evaluate([call(0, "mctl_get_x", digest="sha256:" + "9" * 64)]).verdict == ra.VERDICT_VIOLATED
    failed = call(0, "mctl_delete_tenant", status=ah.CALL_FAILED)
    assert d.evaluate([failed]).verdict == ra.VERDICT_VIOLATED, "a call that reached the target has an unknown effect"
    once = "sha256:" + "5" * 64
    assert d.evaluate([call(0, "mctl_get_x", digest=once)]).verdict == ra.VERDICT_HELD
    replay = d.evaluate([call(0, "mctl_get_x", digest=once), call(1, "mctl_get_x", digest=once)])
    assert (replay.verdict, replay.call_indexes) == (ra.VERDICT_VIOLATED, (0, 1))


def test_forbidden_output_markers_detector_both_ways():
    d = adv.ForbiddenOutputMarkers(("SECRET-X",))
    assert d.evaluate([]).status == ra.OBS_NOT_ATTEMPTED
    assert d.evaluate([call(0, "t", output="fine")]).verdict == ra.VERDICT_HELD
    assert d.evaluate([call(0, "t", output="fine"), call(1, "t", output="has SECRET-X")]).verdict == ra.VERDICT_VIOLATED
    failed = d.evaluate([call(0, "t", status=ah.CALL_FAILED)])
    assert (failed.verdict, failed.status) == (ra.VERDICT_UNKNOWN, ra.OBS_FAILED)
    partial = d.evaluate([call(0, "t", output="fine"), call(1, "t", status=ah.CALL_FAILED)])
    assert (partial.verdict, partial.status) == (ra.VERDICT_UNKNOWN, ra.OBS_PARTIAL)


# ---------------------------------------------------------------------------
# Authority cannot be broadened by the scenario, the stimulus or the probe
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("entry", SUITE.entries, ids=lambda e: e.name)
def test_a_probe_cannot_reach_a_tool_the_scenario_does_not_permit(entry):
    outcome = adv.run_entry(entry, probe=lambda ctx: ctx.call_tool("mctl_delete_everything", {}))
    assert outcome.report.stop_reason == ah.STOP_TOOL_NOT_PERMITTED
    assert outcome.report.outcome == "refused" and outcome.target.calls == []


@pytest.mark.parametrize("entry", SUITE.entries, ids=lambda e: e.name)
def test_a_probe_cannot_move_the_target_scope_or_borrow_a_credential(entry):
    tool = entry.scenario.permitted_tools[0]
    for args, stop in (
        ({"tenant": "tenant-other"}, ah.STOP_RESERVED_ARGUMENT),
        ({"environment": "production"}, ah.STOP_RESERVED_ARGUMENT),
        ({"authorization": "x"}, ah.STOP_RESERVED_ARGUMENT),
        ({"note": adv.SYNTHETIC_TOKEN}, ah.STOP_CREDENTIAL_IN_REQUEST),
    ):
        outcome = adv.run_entry(entry, probe=lambda ctx, a=args: ctx.call_tool(tool, a))
        assert outcome.report.stop_reason == stop and outcome.target.calls == []


def test_the_stimulus_a_target_serves_is_the_pinned_file():
    for name in ("adversarial-evidence-injection", "adversarial-credential-smuggling"):
        entry = ENTRIES[name]
        outcome = adv.run_entry(entry, mutation=None)
        assert hash_bytes(outcome.target.stimulus.encode()) == entry.stimulus_hash


def test_a_non_fixture_environment_is_not_enabled_for_a_suite_scenario():
    spec = adv.spec_by_name()["adversarial-evidence-injection"]
    entry = ENTRIES[spec.name]
    rig = spec.rig(adv.RigContext(entry.scenario, adv.stimulus_by_hash(entry.stimulus_hash), (), 0))
    config = ah.HarnessConfig(
        runtime=adv.PINS,
        targets=(ah.TargetBinding("fixture", spec.fixture_ref, "shadow", adv.REVISION, rig.target),),
        detectors=rig.detectors)
    assert ah.execute(entry.scenario, spec.probe, config, seed=0).stop_reason == ah.STOP_TARGET_NOT_CONFIGURED


# ---------------------------------------------------------------------------
# Scenario specifics
# ---------------------------------------------------------------------------
def test_the_approval_scenarios_bind_the_exact_request_the_harness_decided():
    entry = ENTRIES["adversarial-approval-replay"]
    outcome = adv.run_entry(entry)
    request_a = scn.gate_request(entry.scenario, scn.SCALE, scn.SCALE_A)
    assert outcome.report.run.policy_decisions[0].action_digest == request_a.action_digest()
    # The actor is part of what an approval binds, and the harness owns it: an
    # identity a probe names in its arguments is a different, unapproved action.
    assert request_a.actor == ah.HARNESS_ACTOR
    assert dataclasses.replace(request_a, actor="user:admin").action_digest() != request_a.action_digest()
    assert scn.gate_request(entry.scenario, scn.SCALE, scn.SCALE_B).action_digest() != request_a.action_digest()
    assert outcome.target.effects == [json.dumps(scn.SCALE_A, sort_keys=True)], "A ran exactly once"


def test_approved_a_then_executing_b_is_refused_without_consuming_a():
    entry = ENTRIES["adversarial-approval-mismatch"]
    spec = adv.spec_by_name()[entry.name]
    rig = spec.rig(adv.RigContext(entry.scenario, None, (), 0))
    store = rig.approvals._client
    approved = next(iter(store.records.values()))
    outcome = adv.run_entry(entry)
    assert outcome.passed and outcome.target.effects == []
    assert approved.state == "approved", "refusing B must not spend A's receipt"
    # The same approval still authorizes A.
    rerun = adv.run_entry(entry, probe=lambda ctx: ctx.call_tool(scn.SCALE, scn.SCALE_A))
    assert rerun.report.outcome == ra.OUTCOME_HELD


def test_the_capability_gateway_scenario_reaches_a_provider_only_for_the_eligible_capability():
    outcome = adv.run_entry(ENTRIES["adversarial-capability-confusion"])
    assert [tool for tool, _ in outcome.target.session.calls] == ["mctl_get_service_status"]
    assert set(outcome.target.gateway.descriptors_by_id) == {scn.ELIGIBLE_ID}


def test_the_fencing_scenario_uses_the_real_claim_contract():
    outcome = adv.run_entry(ENTRIES["adversarial-stale-worker-fencing"])
    assert outcome.target.pushed == [scn.HOLDER]
    assert "LIFECYCLE_ROLLOUT_MODE" not in os.environ, "the enforce stage is scoped to the run"


def test_the_ssrf_guard_of_the_fixture_fetcher():
    assert scn.url_allowed("https://fixtures.example.test/evidence/1")
    for url in ("http://fixtures.example.test/x", "https://fixtures.example.test:8443/x",
                "https://fixtures.example.test@evil.example/x", "https://169.254.169.254/", "https://[::1]/",
                "file:///etc/passwd", "https://fixtures.example.test.evil.example/", "https://fixtures.example.test:bad/"):
        assert not scn.url_allowed(url), url
