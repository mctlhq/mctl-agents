"""Runtime-assurance regression scenarios for the governance chain
(mctlhq/mctl-agents#731). Scenarios, fakes and detectors live in
`tests/assurance_governance.py`; the sealed scenarios in
`tests/fixtures/assurance/governance/`.

Every scenario is proven both ways: `invariant_held` on the correct
behaviour, and `invariant_violated` when the guard it pins is switched off
in its fake target (or, for decision records, in the producer).
"""

from __future__ import annotations

import json

import httpx
import pytest

from orchestrator import assurance_harness as ah
from orchestrator import decision_records as dr
from orchestrator import policy_checkpoint as pc
from orchestrator import runtime_assurance as ra
from tests import assurance_governance as gov

MUTANTS = [(s.name, m) for s in gov.SCENARIOS for m in sorted(s.target.MUTATIONS)]


@pytest.fixture(autouse=True)
def _no_global_records(monkeypatch):
    # The decision-record fake owns its recorder; the process-wide one stays off.
    monkeypatch.delenv(dr.ENABLE_ENV, raising=False)
    dr._install_for_tests(None)
    yield
    dr._install_for_tests(None)


def assert_contract(report, sc):
    ra.check_run_against_scenario(report.run, sc)
    ra.check_result_against_run(report.result, report.run, sc)
    assert ra.AssuranceRun.from_dict(json.loads(json.dumps(report.run.to_dict()))) == report.run
    assert ra.AssuranceResult.from_dict(json.loads(json.dumps(report.result.to_dict()))) == report.result


def verdicts(report):
    return {v.invariant_id: v.verdict for v in report.result.invariant_verdicts}


# ---------------------------------------------------------------------------
# The sealed fixtures are the reviewed scenarios
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(gov.BY_NAME))
def test_sealed_fixture_is_the_reviewed_scenario(name):
    s = gov.BY_NAME[name]
    loaded = s.load()
    assert loaded == s.seal(), "fixture drifted: regenerate with `python -m tests.assurance_governance`"
    assert loaded.target.environment == ra.ENV_FIXTURE
    assert loaded.mutation_mode == ra.MUTATION_FIXTURE_ONLY
    assert loaded.permitted_action_kinds == (pc.MCP_TOOL_CALL,)


def test_fixture_directory_holds_exactly_the_catalog():
    assert sorted(p.stem for p in gov.FIXTURES.glob("*.json")) == sorted(gov.BY_NAME)
    ra.check_scenario_catalog([s.load() for s in gov.SCENARIOS])


def test_every_guard_has_a_mutant():
    for s in gov.SCENARIOS:
        assert s.target.MUTATIONS, f"{s.name} has no mutation proving it can go red"


# ---------------------------------------------------------------------------
# Green: each guarantee holds on the correct behaviour
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(gov.BY_NAME))
def test_guarantee_holds_on_the_correct_target(name):
    s = gov.BY_NAME[name]
    report, target = s.run()
    assert (report.failure_class, report.stop_reason, report.outcome) == (
        ah.FAILURE_NONE, ah.STOP_COMPLETED, ra.OUTCOME_HELD), report.detail
    assert set(verdicts(report).values()) == {ra.VERDICT_HELD}
    assert_contract(report, s.load())
    # Every call reached the target governed, as the principal the harness owns.
    assert len(report.run.policy_decisions) == len(target.calls) == report.result.consumed.tool_calls
    assert all(d.code == pc.CODE_ALLOWED for d in report.run.policy_decisions)
    assert all(cred == gov.PRINCIPAL for _, _, cred in target.calls)


@pytest.mark.parametrize("name", sorted(gov.BY_NAME))
def test_same_inputs_reproduce_the_same_records(name):
    s = gov.BY_NAME[name]
    first, _ = s.run(seed=11)
    second, _ = s.run(seed=11)
    assert first.authoritative() == second.authoritative()


# ---------------------------------------------------------------------------
# Red: each guarantee fails when its guard is switched off
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "mutation"), MUTANTS)
def test_guarantee_is_violated_when_its_guard_regresses(name, mutation):
    s = gov.BY_NAME[name]
    report, _ = s.run(mutation=mutation)
    assert (report.failure_class, report.stop_reason, report.outcome) == (
        ah.FAILURE_INVARIANT, ah.STOP_COMPLETED, ra.OUTCOME_VIOLATED), report.detail
    assert verdicts(report)[gov.GUARANTEE] == ra.VERDICT_VIOLATED
    assert_contract(report, s.load())


def test_an_unknown_mutation_is_refused():
    with pytest.raises(ValueError, match="unknown mutation"):
        gov.ApprovalDigestTarget(mutation="no_such_guard")


# ---------------------------------------------------------------------------
# The detector never reads silence as a held guarantee
# ---------------------------------------------------------------------------
def test_a_probe_that_skips_the_guarded_call_is_not_held():
    s = gov.BY_NAME["governance-approval-digest"]

    def only_reads(ctx):
        ctx.call_tool("mctl_get_proposal", {"proposal": gov.PROPOSAL})

    report = ah.execute(s.load(), only_reads, s.config(gov.ApprovalDigestTarget()), seed=0)
    assert report.outcome == "inconclusive"
    assert verdicts(report)[gov.GUARANTEE] == ra.VERDICT_UNKNOWN
    assert report.run.observations[0].status == ra.OBS_NOT_ATTEMPTED


def test_a_partial_transcript_is_unknown_not_held():
    s = gov.BY_NAME["governance-approval-receipt"]

    def first_two(ctx):
        ctx.call_tool(gov.GOVERNED_TOOL, gov._deploy(gov.RECEIPT_OTHER_TOOL))
        ctx.call_tool(gov.GOVERNED_TOOL, gov._deploy(gov.RECEIPT_PEER))

    report = ah.execute(s.load(), first_two, s.config(gov.ApprovalReceiptTarget()), seed=0)
    assert report.outcome == "inconclusive"
    assert verdicts(report)[gov.GUARANTEE] == ra.VERDICT_UNKNOWN
    assert report.run.observations[0].status == ra.OBS_PARTIAL


def test_a_failed_read_of_a_guarded_answer_is_unknown():
    s = gov.BY_NAME["governance-failed-approval"]

    class Flaky(gov.FailedApprovalTarget):
        def mctl_get_work_item(self, args, cred):
            if args.get("work_item") == gov.WORK_ITEM_BROKEN:
                raise ConnectionError("fixture read failed")
            return super().mctl_get_work_item(args, cred)

    report = ah.execute(s.load(), s.probe, s.config(Flaky()), seed=0)
    assert report.failure_class == ah.FAILURE_TARGET
    assert verdicts(report)[gov.GUARANTEE] == ra.VERDICT_UNKNOWN
    assert report.run.observations[0].status == ra.OBS_PARTIAL  # the other expected calls were read


def test_no_expected_answer_read_is_observation_failed():
    s = gov.BY_NAME["governance-decision-records"]

    class Unreadable(gov.DecisionRecordTarget):
        def mctl_get_record_audit(self, args, cred):
            raise ConnectionError("fixture read failed")

    report = ah.execute(s.load(), s.probe, s.config(Unreadable()), seed=0)
    assert verdicts(report)[gov.GUARANTEE] == ra.VERDICT_UNKNOWN
    assert report.run.observations[0].status == ra.OBS_FAILED


def test_an_answer_that_is_not_json_is_not_the_required_answer():
    s = gov.BY_NAME["governance-approval-digest"]

    class Garbled(gov.ApprovalDigestTarget):
        def mctl_approve_proposal(self, args, cred):
            return "ok" if "content_digest" not in args else super().mctl_approve_proposal(args, cred)

    report = ah.execute(s.load(), s.probe, s.config(Garbled()), seed=0)
    assert verdicts(report)[gov.GUARANTEE] == ra.VERDICT_VIOLATED


# ---------------------------------------------------------------------------
# Scenario specifics
# ---------------------------------------------------------------------------
def test_receipt_scenario_pins_the_policy_gate_and_not_argument_binding():
    s = gov.BY_NAME["governance-approval-receipt"]
    report, _ = s.run()
    assert verdicts(report) == {gov.GUARANTEE: ra.VERDICT_HELD, gov.POLICY_GATE: ra.VERDICT_HELD}
    # v1 semantics: the binding is principal + tool + single use. No
    # expectation varies the arguments of an otherwise valid receipt.
    own = [e for e in gov.APPROVAL_RECEIPT_CHECK.expectations if e.args.get("receipt") == gov.RECEIPT_OWN]
    assert {json.dumps(dict(e.args), sort_keys=True) for e in own} == {
        json.dumps(gov._deploy(gov.RECEIPT_OWN), sort_keys=True)}


def test_receipt_principal_comes_from_the_harness_not_the_probe():
    # The principal is the credential the harness binds to the target. A
    # probe that tries to carry one in its arguments is stopped before policy
    # or transport, so it cannot borrow another principal's receipt.
    s = gov.BY_NAME["governance-approval-receipt"]
    target = gov.ApprovalReceiptTarget()

    def names_a_credential(ctx):
        ctx.call_tool(gov.GOVERNED_TOOL, {**gov._deploy(gov.RECEIPT_PEER), "authorization": "peer"})

    report = ah.execute(s.load(), names_a_credential, s.config(target), seed=0)
    assert report.stop_reason == ah.STOP_RESERVED_ARGUMENT
    assert target.calls == []


def test_decision_record_scenario_drives_the_real_producer():
    s = gov.BY_NAME["governance-decision-records"]
    report, target = s.run()
    assert report.outcome == ra.OUTCOME_HELD
    assert target.recorder.stats() == {
        dr.CREATED: 4,  # landed: decision, started, succeeded; start-refused: its decision
        dr.UNOBSERVABLE: 1,
        dr.DROPPED_DECISION_UNRECORDED: 2,
        dr.REFUSED: 1,
        dr.DROPPED_ATTEMPT_UNOPENED: 1,
    }
    assert target.sink.stored == 4
    assert target.intended == gov.WRITES_PER_ACTION * len(gov.SINK_SCRIPT)
    # Fixture adequacy, not part of the guarantee: some writes must not land.
    assert target.sink.stored < target.intended


def test_a_stored_write_the_producer_cannot_address_keeps_the_guarantee():
    # The producer counts a 201 whose decision id it cannot read as
    # unobservable (decision_records: stored, but its attempts cannot be
    # addressed). Recorded implies stored, not the converse, so this stays held.
    class UnreadableId(gov._Sink):
        def __call__(self, url, body, headers):
            resp = super().__call__(url, body, headers)
            if url.endswith(dr.DECISIONS_PATH) and resp.status_code in (200, 201) and self.decisions == 1:
                return httpx.Response(201, json={"decision": {"id": "not-an-id"}}, request=resp.request)
            return resp

    s = gov.BY_NAME["governance-decision-records"]
    target = gov.DecisionRecordTarget(sink=UnreadableId())
    report = ah.execute(s.load(), s.probe, s.config(target), seed=0)
    assert report.outcome == ra.OUTCOME_HELD, report.detail
    recorded = sum(n for k, n in target.recorder.stats().items() if k in dr.RECORDED)
    assert recorded < target.sink.stored
    # One stored write with an unreadable id, one persistent 503.
    assert target.recorder.stats()[dr.UNOBSERVABLE] == 2
    assert target.sink.stored == 2


def test_decision_record_mutant_claims_writes_that_did_not_land():
    s = gov.BY_NAME["governance-decision-records"]
    _, target = s.run(mutation="count_unlanded_as_recorded")
    recorded = sum(n for k, n in target.recorder.stats().items() if k in dr.RECORDED)
    assert recorded > target.sink.stored
