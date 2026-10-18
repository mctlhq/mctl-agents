"""Tests for orchestrator/runtime_assurance.py (mctlhq/mctl-agents#694, ADR 022)."""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
from pathlib import Path

import pytest

from orchestrator import execution_evidence as ee
from orchestrator import runtime_assurance as ra
from orchestrator.execution_evidence import ApprovalRef, ExecutionJoin, PolicyDecisionRef, VersionPins

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "assurance"

CLASS_BY_FIXTURE = {
    "scenario-fixture.json": ra.AssuranceScenario,
    "scenario-shadow.json": ra.AssuranceScenario,
    "allowlist.json": ra.AssuranceTargetAllowlist,
    "run.json": ra.AssuranceRun,
    "replay-run.json": ra.AssuranceRun,
    "result-violated.json": ra.AssuranceResult,
    "replay-result.json": ra.AssuranceResult,
    "finding-candidate.json": ra.AssuranceFinding,
    "finding-confirmed.json": ra.AssuranceFinding,
    "finding-rejected.json": ra.AssuranceFinding,
}
H = "sha256:" + "d" * 64


def load(name):
    return json.loads((FIXTURES / name).read_text())


def parse(name):
    return CLASS_BY_FIXTURE[name].from_dict(load(name))


def reseal(record):
    """Re-seal from the record's own fields (ids and hash recomputed)."""
    seal = {
        ra.AssuranceScenario: ra.seal_scenario,
        ra.AssuranceTargetAllowlist: ra.seal_allowlist,
        ra.AssuranceRun: ra.seal_run,
        ra.AssuranceResult: ra.seal_result,
        ra.AssuranceFinding: ra.seal_finding,
    }[type(record)]
    skip = {"scenario_id", "allowlist_id", "run_id", "result_id", "finding_id", "content_hash"}
    if isinstance(record, ra.AssuranceResult):
        skip -= {"run_id"}
    return seal(**{f.name: getattr(record, f.name) for f in dataclasses.fields(record) if f.name not in skip})


@pytest.fixture
def shadow():
    return parse("scenario-shadow.json")


@pytest.fixture
def allowlist():
    return parse("allowlist.json")


@pytest.fixture
def run():
    return parse("run.json")


@pytest.fixture
def result():
    return parse("result-violated.json")


# T1 -------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(CLASS_BY_FIXTURE))
def test_golden_fixtures_round_trip_and_keep_hashes(name):
    record = parse(name)
    assert record.to_dict() == load(name)
    assert reseal(record) == record
    assert ra.recompute_content_hash(record) == load(name)["content_hash"]


def test_hashed_fields_change_the_id_and_timestamps_do_not(run):
    other = dataclasses.replace(run, attempt=2, run_id="", content_hash="")
    assert (
        ra.seal_run(
            **{
                f.name: getattr(other, f.name)
                for f in dataclasses.fields(other)
                if f.name not in ("run_id", "content_hash")
            }
        ).run_id
        != run.run_id
    )
    shifted = dataclasses.replace(run, started_at="2027-01-01T00:00:00Z", ended_at=None)
    assert ra.recompute_content_hash(shifted) == run.content_hash
    assert dataclasses.replace(shifted).run_id == run.run_id


# T2 -------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(CLASS_BY_FIXTURE))
def test_envelope_and_unknown_keys_are_refused(name):
    cls = CLASS_BY_FIXTURE[name]
    for key, value in (
        ("api_version", "assurance.mctl.ai/v2"),
        ("kind", "AssuranceRun" if cls is not ra.AssuranceRun else "AssuranceResult"),
    ):
        doc = load(name)
        doc[key] = value
        with pytest.raises(ra.RuntimeAssuranceError):
            cls.from_dict(doc)
    doc = load(name)
    doc["surprise"] = 1
    with pytest.raises(ra.RuntimeAssuranceError):
        cls.from_dict(doc)


def test_unknown_key_at_nested_levels_and_wrong_types(run):
    doc = load("run.json")
    doc["binding"]["target"]["extra"] = "x"
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceRun.from_dict(doc)
    doc = load("run.json")
    doc["observations"][0]["extra"] = "x"
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceRun.from_dict(doc)
    doc = load("run.json")
    doc["attempt"] = "1"
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceRun.from_dict(doc)
    doc = load("scenario-shadow.json")
    doc["description"] = "x" * (ra.MAX_DESCRIPTION_CHARS + 1)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceScenario.from_dict(doc)
    doc = load("scenario-shadow.json")
    doc["followup_templates"][0]["parameters"]["variant"]["extra"] = 1
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceScenario.from_dict(doc)


@pytest.mark.parametrize("name", sorted(CLASS_BY_FIXTURE))
def test_tampered_hash_or_id_is_refused(name):
    cls = CLASS_BY_FIXTURE[name]
    id_field = next(
        k
        for k in load(name)
        if k.endswith("_id")
        and k in ("scenario_id", "allowlist_id", "run_id", "result_id", "finding_id")
        and not (name.startswith("result") and k == "run_id")
    )
    doc = load(name)
    doc["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(ra.RuntimeAssuranceError):
        cls.from_dict(doc)
    doc = load(name)
    doc[id_field] = doc[id_field][:4] + ("0" * 16 if doc[id_field][4:] != "0" * 16 else "1" * 16)
    with pytest.raises(ra.RuntimeAssuranceError):
        cls.from_dict(doc)
    doc = load(name)
    del doc["content_hash"]
    with pytest.raises(ra.RuntimeAssuranceError):
        cls.from_dict(doc)


# T3 -------------------------------------------------------------------------
def _scenario_kwargs(name="scenario-fixture.json"):
    sc = parse(name)
    return {
        f.name: getattr(sc, f.name) for f in dataclasses.fields(sc) if f.name not in ("scenario_id", "content_hash")
    }


@pytest.mark.parametrize("ref", ["*", "owner/*", "owner/na?me", "owner/[ab]", "", " x", "a b"])
def test_wildcard_or_glob_target_refs_are_refused(ref):
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.TargetBoundary("repository", ref, "fixture")


@pytest.mark.parametrize("env", ["shadow", "production"])
def test_shadow_and_production_require_an_allowlist_entry(env):
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.TargetBoundary("repository", "mctlhq/x", env)


def test_fixture_target_forbids_allowlist_entry(shadow):
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.TargetBoundary("fixture", "f", "fixture", shadow.target.allowlist_entry)


def test_production_cannot_mutate(shadow):
    kwargs = _scenario_kwargs("scenario-shadow.json")
    kwargs["target"] = ra.TargetBoundary(
        "repository", "mctlhq/mctl-agents", "production", shadow.target.allowlist_entry
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="production"):
        ra.seal_scenario(**kwargs)
    kwargs["mutation_mode"] = "none"
    kwargs["permitted_action_kinds"] = ("mcp.tool.call",)
    assert ra.seal_scenario(**kwargs).mutation_mode == "none"


def test_mutating_action_kind_requires_a_mutation_mode():
    kwargs = _scenario_kwargs()
    kwargs["permitted_action_kinds"] = ("github.git.push",)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**kwargs)
    kwargs["permitted_action_kinds"] = ("not.a.kind",)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**kwargs)


@pytest.mark.parametrize(
    "axis,value",
    [
        ("max_attempts", 0),
        ("max_attempts", 11),
        ("max_tool_calls", 0),
        ("max_tool_calls", 257),
        ("max_wall_clock_seconds", 0),
        ("max_wall_clock_seconds", 3601),
        ("max_followups", -1),
        ("max_followups", 17),
        ("max_attempts", None),
        ("max_attempts", "unlimited"),
        ("max_attempts", True),
    ],
)
def test_budget_axes_are_bounded(axis, value):
    base = {"max_attempts": 1, "max_tool_calls": 1, "max_wall_clock_seconds": 1, "max_followups": 0}
    base[axis] = value
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.Budgets(**base)


def test_missing_budget_axis_is_refused():
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.Budgets.from_dict({"max_attempts": 1, "max_tool_calls": 1, "max_wall_clock_seconds": 1})


def test_deterministic_scenario_allows_no_followups(shadow):
    kwargs = _scenario_kwargs("scenario-shadow.json")
    kwargs["execution_mode"] = "deterministic"
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**kwargs)
    kwargs["followup_templates"] = ()
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**kwargs)  # max_followups still 4


def test_collection_caps():
    kwargs = _scenario_kwargs()
    many = tuple(ra.InputSpec(f"i{n}", "literal", value="v") for n in range(ra.MAX_INPUTS + 1))
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**{**kwargs, "inputs": many})
    invs = tuple(ra.InvariantSpec(f"v{n}", "s", "evidence_present") for n in range(ra.MAX_INVARIANTS + 1))
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**{**kwargs, "invariants": invs})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**{**kwargs, "invariants": ()})
    tools = tuple(f"tool{n}" for n in range(ra.MAX_TOOLS + 1))
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**{**kwargs, "permitted_tools": tools})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**{**kwargs, "permitted_tools": ("tool*",)})


def test_credentials_in_authoritative_strings_are_refused():
    kwargs = _scenario_kwargs()
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_scenario(**{**kwargs, "description": "use ghp_" + "a" * 30})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.InputSpec("tok", "literal", value="sk-" + "a" * 30)


def test_catalog_version_collision(shadow):
    ra.check_scenario_catalog([shadow, shadow, parse("scenario-fixture.json")])
    other = ra.seal_scenario(**{**_scenario_kwargs("scenario-shadow.json"), "description": "different"})
    with pytest.raises(ra.RuntimeAssuranceError, match="collision"):
        ra.check_scenario_catalog([shadow, other])


# T4 -------------------------------------------------------------------------
def _binding_variant(binding, **runtime):
    return dataclasses.replace(binding, runtime=dataclasses.replace(binding.runtime, **runtime))


@pytest.mark.parametrize("field", ["agent", "environment", "definition_version", "definition_content_hash"])
def test_blank_runtime_pins_are_refused(run, field):
    with pytest.raises(ra.RuntimeAssuranceError):
        _binding_variant(run.binding, **{field: ""})


def test_profile_pins_come_together(run):
    with pytest.raises(ra.RuntimeAssuranceError):
        _binding_variant(run.binding, profile_version="")
    with pytest.raises(ra.RuntimeAssuranceError):
        _binding_variant(run.binding, profile_name="", profile_version="3")
    _binding_variant(run.binding, profile_name="", profile_version="")


@pytest.mark.parametrize("revision", ["", "  ", "abc def", "x" * 129, "-bad"])
def test_bad_target_revision_is_refused(revision):
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.TargetRevision("repository", "mctlhq/x", revision)


def test_git_sha_and_version_token_revisions_are_accepted():
    ra.TargetRevision("repository", "mctlhq/x", "a" * 40)
    ra.TargetRevision("mctl_service", "admins/x", "1.84.0")


def test_version_incomplete_results_and_findings_are_refused(result):
    with pytest.raises(ra.RuntimeAssuranceError):
        _binding_variant(result.binding, definition_content_hash="")
    finding = parse("finding-candidate.json")
    with pytest.raises(ra.RuntimeAssuranceError):
        _binding_variant(finding.binding, environment="")


def test_missing_scenario_content_hash_is_refused(run):
    doc = load("run.json")
    del doc["binding"]["scenario"]["content_hash"]
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceRun.from_dict(doc)


def test_binding_runtime_is_execution_evidence_version_pins(run):
    assert type(run.binding.runtime) is VersionPins


# T5 -------------------------------------------------------------------------
def test_allowlist_exact_match_succeeds(shadow, allowlist):
    ra.check_target_allowlisted(shadow, allowlist)


def test_allowlist_mismatches_fail(shadow, allowlist):
    bumped = ra.seal_allowlist(name=allowlist.name, version="2", entries=allowlist.entries)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.check_target_allowlisted(shadow, bumped)
    entries = (dataclasses.replace(allowlist.entries[0], environments=("production",)), allowlist.entries[1])
    changed = ra.seal_allowlist(name=allowlist.name, version=allowlist.version, entries=entries)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.check_target_allowlisted(shadow, changed)  # id differs
    entry = shadow.target.allowlist_entry
    forged = dataclasses.replace(entry, allowlist_id=changed.allowlist_id)
    forged_sc = ra.seal_scenario(
        **{
            **_scenario_kwargs("scenario-shadow.json"),
            "target": dataclasses.replace(shadow.target, allowlist_entry=forged),
        }
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="hash"):
        ra.check_target_allowlisted(forged_sc, changed)
    other_ref = ra.seal_scenario(
        **{**_scenario_kwargs("scenario-shadow.json"), "target": dataclasses.replace(shadow.target, ref="mctlhq/other")}
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="ref"):
        ra.check_target_allowlisted(other_ref, allowlist)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.check_target_allowlisted(parse("scenario-fixture.json"), allowlist)


def test_allowlist_environment_not_listed(shadow, allowlist):
    prod_target = dataclasses.replace(shadow.target, environment="production")
    sc = ra.seal_scenario(
        **{
            **_scenario_kwargs("scenario-shadow.json"),
            "target": prod_target,
            "mutation_mode": "none",
            "permitted_action_kinds": ("mcp.tool.call",),
        }
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="environment"):
        ra.check_target_allowlisted(sc, allowlist)


# T6 -------------------------------------------------------------------------
def proposal(**params):
    return ra.FollowupProposal("vary-body", tuple(params.items()))


def test_valid_followup_is_accepted(shadow):
    assert ra.validate_followup(shadow, proposal(variant="long", repeat=3)).template_id == "vary-body"


@pytest.mark.parametrize(
    "prop",
    [
        ra.FollowupProposal("nope", ()),
        ra.FollowupProposal("vary-body", (("variant", "long"), ("repeat", 3), ("extra", 1))),
        ra.FollowupProposal("vary-body", (("variant", "long"), ("repeat", 99))),
        ra.FollowupProposal("vary-body", (("variant", "huge"), ("repeat", 2))),
        ra.FollowupProposal("vary-body", (("variant", "long"),)),
        ra.FollowupProposal("vary-body", (("variant", "long"), ("repeat", "3"))),
    ],
)
def test_invalid_followups_are_refused(shadow, prop):
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.validate_followup(shadow, prop)


@pytest.mark.parametrize("key", ["target", "tenant", "credential", "tool", "environment", "action_kind"])
def test_followup_proposal_cannot_express_scope(key):
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.FollowupProposal.from_dict({"template_id": "vary-body", "parameters": {}, key: "x"})


# T7 -------------------------------------------------------------------------
def _run_kwargs(run):
    return {f.name: getattr(run, f.name) for f in dataclasses.fields(run) if f.name not in ("run_id", "content_hash")}


def test_valid_run_checks_against_scenario(run, shadow):
    ra.check_run_against_scenario(run, shadow)


def test_run_against_scenario_refusals(run, shadow):
    k = _run_kwargs(run)
    with pytest.raises(ra.RuntimeAssuranceError, match="budget"):
        ra.check_run_against_scenario(ra.seal_run(**{**k, "granted_budgets": ra.Budgets(3, 129, 600, 2)}), shadow)
    with pytest.raises(ra.RuntimeAssuranceError, match="mutation_mode"):
        ra.check_run_against_scenario(ra.seal_run(**{**k, "mutation_mode": "none"}), shadow)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(**{**k, "attempt": 3})  # granted max_attempts is 2
    other = dataclasses.replace(run.binding, scenario=dataclasses.replace(run.binding.scenario, content_hash=H))
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.check_run_against_scenario(ra.seal_run(**{**k, "binding": other}), shadow)


def test_run_requires_runtime_execution_id(run):
    k = _run_kwargs(run)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(**{**k, "execution": ExecutionJoin(execution_id="we_abc")})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(**{**k, "execution": ExecutionJoin(runtime_execution_id="we_0123456789abcdef")})


def test_run_relations(run, shadow):
    k = _run_kwargs(run)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(**{**k, "relation": "replay"})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(**{**k, "parent_run_id": run.run_id})
    follow = ra.seal_run(
        **{**k, "relation": "followup", "parent_run_id": run.run_id, "followup": proposal(variant="long", repeat=1)}
    )
    ra.check_run_against_scenario(follow, shadow)
    bad = ra.seal_run(
        **{**k, "relation": "followup", "parent_run_id": run.run_id, "followup": proposal(variant="long", repeat=50)}
    )
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.check_run_against_scenario(bad, shadow)


def test_run_policy_and_approval_refs_are_validated(run):
    k = _run_kwargs(run)
    decision = PolicyDecisionRef(
        action_digest="sha256:" + "1" * 64, verdict="ALLOW", code="allowed", policy_version="v1"
    )
    approval = ApprovalRef(approval_id="aar_1", intent_hash=H, state="approved")
    sealed = ra.seal_run(**{**k, "policy_decisions": (decision,), "approvals": (approval,)})
    assert ra.AssuranceRun.from_dict(sealed.to_dict()) == sealed
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(**{**k, "policy_decisions": (dataclasses.replace(decision, verdict="MAYBE"),)})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(**{**k, "approvals": (dataclasses.replace(approval, state="whatever"),)})


# T8 -------------------------------------------------------------------------
def _result_kwargs(result):
    return {
        f.name: getattr(result, f.name)
        for f in dataclasses.fields(result)
        if f.name not in ("result_id", "content_hash")
    }


def _run_with_obs(run, obs):
    return ra.seal_run(**{**_run_kwargs(run), "observations": obs})


def _obs(oid, inv, status, **kw):
    return ra.ObservationRef(oid, (inv,), status, "observed", **kw)


@pytest.mark.parametrize("status", ["partial", "observation_failed", "not_attempted"])
def test_held_cannot_rest_on_a_non_observed_observation(run, result, shadow, status):
    obs = (
        _obs("obs-1", "no-secret-in-comment", "observed", evidence_id="ev-0123456789abcdef"),
        _obs("obs-2", "policy-gate-hit", status),
    )
    weak_run = _run_with_obs(run, obs)
    res = ra.seal_result(**{**_result_kwargs(result), "run_id": weak_run.run_id})
    with pytest.raises(ra.RuntimeAssuranceError, match="held"):
        ra.check_result_against_run(res, weak_run, shadow)
    unknown = ra.seal_result(
        **{
            **_result_kwargs(result),
            "run_id": weak_run.run_id,
            "evidence_ids": ("ev-0123456789abcdef",),
            "invariant_verdicts": (
                result.invariant_verdicts[0],
                ra.InvariantVerdict("policy-gate-hit", "unknown", ("obs-2",)),
            ),
        }
    )
    ra.check_result_against_run(unknown, weak_run, shadow)


def test_valid_result_checks_against_run(run, result, shadow):
    ra.check_result_against_run(result, run, shadow)


def test_result_outcome_consistency(result):
    k = _result_kwargs(result)
    unknown = ra.InvariantVerdict("policy-gate-hit", "unknown")
    held = ra.InvariantVerdict("no-secret-in-comment", "held", ("obs-1",))
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_result(**{**k, "outcome": "invariant_held", "invariant_verdicts": (held, unknown)})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_result(**{**k, "outcome": "invariant_violated", "invariant_verdicts": (held, unknown)})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_result(**{**k, "outcome": "refused"})  # still carries a violated verdict
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.InvariantVerdict("x", "held")  # no observations


def test_violated_needs_observed_support_and_full_coverage(run, result, shadow):
    weak = _run_with_obs(
        run,
        (
            _obs("obs-1", "no-secret-in-comment", "partial"),
            _obs("obs-2", "policy-gate-hit", "observed", evidence_id="ev-0123456789abcdef"),
        ),
    )
    res = ra.seal_result(**{**_result_kwargs(result), "run_id": weak.run_id})
    with pytest.raises(ra.RuntimeAssuranceError, match="observed"):
        ra.check_result_against_run(res, weak, shadow)
    missing = ra.seal_result(
        **{**_result_kwargs(result), "outcome": "inconclusive", "invariant_verdicts": (result.invariant_verdicts[1],)}
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="cover"):
        ra.check_result_against_run(missing, run, shadow)


def test_observed_requires_an_evidence_or_artifact_reference():
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.ObservationRef("o", ("i",), "observed", "observed")
    ra.ObservationRef("o", ("i",), "partial", "observed")
    ra.ObservationRef(
        "o", ("i",), "observed", "derived", artifact=ee.ArtifactRef(name="trace.json", kind="trace", content_hash=H)
    )
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.ObservationRef(
            "o",
            ("i",),
            "observed",
            "observed",
            artifact=ee.ArtifactRef(name="trace.json", kind="trace", content_hash="x"),
        )


def test_consumed_budget_rule(run, result, shadow):
    over = ra.BudgetUsage(1, 500, 95, 0)
    res = ra.seal_result(**{**_result_kwargs(result), "consumed": over})
    with pytest.raises(ra.RuntimeAssuranceError, match="budget_exhausted"):
        ra.check_result_against_run(res, run, shadow)
    ok = ra.seal_result(
        **{
            **_result_kwargs(result),
            "consumed": over,
            "outcome": "budget_exhausted",
            "invariant_verdicts": (
                ra.InvariantVerdict("no-secret-in-comment", "unknown"),
                ra.InvariantVerdict("policy-gate-hit", "unknown"),
            ),
        }
    )
    ra.check_result_against_run(ok, run, shadow)


def test_result_must_match_run_and_binding(run, result, shadow):
    other = ra.seal_result(**{**_result_kwargs(result), "run_id": "arn-" + "0" * 16})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.check_result_against_run(other, run, shadow)


# T9 -------------------------------------------------------------------------
def test_candidate_to_confirmed_with_matching_replay():
    cand = parse("finding-candidate.json")
    conf = parse("finding-confirmed.json")
    assert conf.state == "confirmed" and conf.supersedes == cand.finding_id
    assert conf.validation.authority in ("observed", "derived")
    again = ra.transition_finding(
        cand, "confirmed", replay_run=parse("replay-run.json"), replay_result=parse("replay-result.json")
    )
    assert again == conf


def _replay_kwargs():
    replay, rres = parse("replay-run.json"), parse("replay-result.json")
    return replay, rres


def test_confirmation_refusals(run, result):
    cand = parse("finding-candidate.json")
    replay, rres = _replay_kwargs()
    rk = _run_kwargs(replay)
    with pytest.raises(ra.RuntimeAssuranceError):  # same run as origin
        ra.transition_finding(cand, "confirmed", replay_run=run, replay_result=result)
    with pytest.raises(ra.RuntimeAssuranceError):  # no replay at all
        ra.transition_finding(cand, "confirmed")
    other_binding = dataclasses.replace(
        replay.binding, target=dataclasses.replace(replay.binding.target, revision="b" * 40)
    )
    bad_run = ra.seal_run(**{**rk, "binding": other_binding})
    bad_res = ra.seal_result(**{**_result_kwargs(rres), "binding": other_binding, "run_id": bad_run.run_id})
    with pytest.raises(ra.RuntimeAssuranceError, match="bound"):
        ra.transition_finding(cand, "confirmed", replay_run=bad_run, replay_result=bad_res)
    held = ra.seal_result(
        **{
            **_result_kwargs(rres),
            "outcome": "invariant_held",
            "invariant_verdicts": (
                ra.InvariantVerdict("no-secret-in-comment", "held", ("obs-1",)),
                ra.InvariantVerdict("policy-gate-hit", "held", ("obs-2",)),
            ),
        }
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="violate"):
        ra.transition_finding(cand, "confirmed", replay_run=replay, replay_result=held)
    other_inv = ra.seal_result(
        **{
            **_result_kwargs(rres),
            "invariant_verdicts": (
                ra.InvariantVerdict("no-secret-in-comment", "held", ("obs-1",)),
                ra.InvariantVerdict("policy-gate-hit", "violated", ("obs-2",)),
            ),
        }
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="same invariant"):
        ra.transition_finding(cand, "confirmed", replay_run=replay, replay_result=other_inv)
    not_replay = ra.seal_run(**{**rk, "relation": "followup", "followup": proposal(variant="long", repeat=1)})
    with pytest.raises(ra.RuntimeAssuranceError, match="replay"):
        ra.transition_finding(cand, "confirmed", replay_run=not_replay, replay_result=rres)
    asserted = tuple(dataclasses.replace(o, authority="asserted") for o in replay.observations)
    weak_run = ra.seal_run(**{**rk, "observations": asserted})
    weak_res = ra.seal_result(**{**_result_kwargs(rres), "run_id": weak_run.run_id})
    with pytest.raises(ra.RuntimeAssuranceError, match="asserted"):
        ra.transition_finding(cand, "confirmed", replay_run=weak_run, replay_result=weak_res)


def test_rejection_needs_a_disposition_and_states_are_terminal():
    cand = parse("finding-candidate.json")
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.transition_finding(cand, "rejected")
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.transition_finding(cand, "rejected", disposition="whatever")
    rejected = parse("finding-rejected.json")
    assert rejected.disposition == "false_positive" and rejected.supersedes == cand.finding_id
    for terminal in (rejected, parse("finding-confirmed.json")):
        for target in ("confirmed", "rejected", "candidate"):
            with pytest.raises(ra.RuntimeAssuranceError, match="terminal"):
                ra.transition_finding(terminal, target, disposition="duplicate")
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.transition_finding(cand, "candidate")


def test_confirmed_cannot_be_built_or_parsed_without_validation():
    doc = load("finding-confirmed.json")
    k = {
        f.name: getattr(parse("finding-confirmed.json"), f.name)
        for f in dataclasses.fields(ra.AssuranceFinding)
        if f.name not in ("finding_id", "content_hash")
    }
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_finding(**{**k, "validation": None})
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_finding(**{**k, "validation": dataclasses.replace(k["validation"], replay_run_id=k["origin_run_id"])})
    del doc["validation"]
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceFinding.from_dict(doc)
    cand = load("finding-candidate.json")
    cand["state"] = "confirmed"
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceFinding.from_dict(cand)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.FindingValidation("arn-" + "1" * 16, "ars-" + "1" * 16, "asserted")


def test_candidate_forbids_validation_and_disposition():
    k = {
        f.name: getattr(parse("finding-candidate.json"), f.name)
        for f in dataclasses.fields(ra.AssuranceFinding)
        if f.name not in ("finding_id", "content_hash")
    }
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_finding(**{**k, "disposition": "duplicate"})


# T10 ------------------------------------------------------------------------
def test_model_text_is_redacted_and_bounded():
    k = {
        f.name: getattr(parse("finding-candidate.json"), f.name)
        for f in dataclasses.fields(ra.AssuranceFinding)
        if f.name not in ("finding_id", "content_hash")
    }
    leaked = ra.seal_finding(**{**k, "summary": "found token ghp_" + "a" * 30 + " in output"})
    assert leaked.summary == "[redacted]"
    long = ra.seal_finding(**{**k, "summary": "word " * 1000})
    assert 0 < len(long.summary) <= ra.MAX_SUMMARY_CHARS
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.AssuranceFinding(**{**k, "summary": "ghp_" + "a" * 30})
    note = ra.transition_finding(
        parse("finding-candidate.json"), "rejected", disposition="duplicate", disposition_note="sk-" + "a" * 30
    )
    assert note.disposition_note == "[redacted]"


def test_authoritative_credentials_are_refused_in_reused_blocks(run):
    k = _run_kwargs(run)
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.seal_run(
            **{**k, "execution": ExecutionJoin(runtime_execution_id="ex-0123456789abcdef", trace_id="ghp_" + "a" * 30)}
        )


# T11 ------------------------------------------------------------------------
ALLOWED_INTRA = {
    "orchestrator.context_snapshot",
    "orchestrator.redaction",
    "orchestrator.execution_evidence",
    "orchestrator.policy_checkpoint",
    "orchestrator",
}
STDLIB = {"re", "dataclasses", "collections.abc", "typing", "__future__"}


def test_module_imports_are_stdlib_plus_allowed_orchestrator_modules():
    tree = ast.parse((REPO_ROOT / "orchestrator" / "runtime_assurance.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module)
    assert modules <= STDLIB | ALLOWED_INTRA, sorted(modules - STDLIB - ALLOWED_INTRA)


#: Field names that merely name scope or budget data, not a decision. Pinned
#: exactly: any other field containing an authority word fails the scan.
SCOPE_FIELD_EXEMPTIONS = {
    "allowlist_entry",
    "allowlist_id",
    "permitted_action_kinds",
    "permitted_tools",
    "granted_budgets",
}
FORBIDDEN_TOKENS = ("allow", "deny", "permit", "grant", "authorized")


def test_no_dataclass_field_expresses_authority():
    offenders = set()
    for obj in vars(ra).values():
        if dataclasses.is_dataclass(obj) and isinstance(obj, type) and obj.__module__ == ra.__name__:
            for f in dataclasses.fields(obj):
                if any(t in f.name.lower() for t in FORBIDDEN_TOKENS):
                    offenders.add(f.name)
    assert offenders == SCOPE_FIELD_EXEMPTIONS


# T12 ------------------------------------------------------------------------
def test_reused_blocks_are_the_execution_evidence_classes(run):
    assert type(run.execution) is ee.ExecutionJoin
    assert type(run.binding.runtime) is ee.VersionPins
    fields_by_name = {f.name: f for f in dataclasses.fields(ra.AssuranceRun)}
    assert "ExecutionJoin" in str(fields_by_name["execution"].type)
    assert ra.AUTHORITIES is ee.AUTHORITIES
    assert ra.MAX_TOOL_CALLS_CAP == ee.MAX_TOOL_CALLS
    assert ra._GIT_SHA_RE.pattern == ee._GIT_SHA_PATTERN.pattern
    assert ra.MUTATING_ACTION_KINDS == ra._pc.ACTION_KINDS - {"mcp.tool.call"}


def test_deep_copy_and_unknown_fixture_isolation():
    doc = load("run.json")
    clone = copy.deepcopy(doc)
    ra.AssuranceRun.from_dict(doc)
    assert doc == clone


# Review follow-ups -----------------------------------------------------------
def test_read_only_scenario_refuses_mutating_mcp_tools():
    kwargs = _scenario_kwargs()
    kwargs["mutation_mode"] = "none"
    kwargs["permitted_action_kinds"] = ("mcp.tool.call",)
    for tool in ("resolve_incident", "mctl_acknowledge_incident", "mctl_deploy_service", "unknown_tool"):
        with pytest.raises(ra.RuntimeAssuranceError, match="read-only"):
            ra.seal_scenario(**{**kwargs, "permitted_tools": ("mctl_get_service_config", tool)})
    ok = ra.seal_scenario(**{**kwargs, "permitted_tools": ("mctl_get_service_config", "whoami", "list_apps")})
    assert ok.permitted_tools


def test_result_evidence_must_come_from_run_observations(run, result, shadow):
    ra.check_result_against_run(result, run, shadow)
    bad = ra.seal_result(**{**_result_kwargs(result), "evidence_ids": (*result.evidence_ids, "ev-ffffffffffffffff")})
    with pytest.raises(ra.RuntimeAssuranceError, match="evidence"):
        ra.check_result_against_run(bad, run, shadow)


def test_check_finding_confirmation_matches_transition():
    cand = parse("finding-candidate.json")
    replay, rres = _replay_kwargs()
    assert ra.check_finding_confirmation(cand, replay, rres) in ("observed", "derived")
    with pytest.raises(ra.RuntimeAssuranceError):
        ra.check_finding_confirmation(cand, None, None)


def test_check_finding_against_result(result, shadow):
    cand = parse("finding-candidate.json")
    ra.check_finding_against_result(cand, result, shadow)
    k = {
        f.name: getattr(cand, f.name) for f in dataclasses.fields(cand) if f.name not in ("finding_id", "content_hash")
    }
    for change in (
        {"invariant_id": "not-an-invariant"},
        {"origin_result_id": "ars-" + "1" * 16},
        {"evidence_ids": ("ev-ffffffffffffffff",)},
    ):
        with pytest.raises(ra.RuntimeAssuranceError):
            ra.check_finding_against_result(ra.seal_finding(**{**k, **change}), result, shadow)


def test_confirmed_validation_result_must_differ_from_origin_result():
    k = {
        f.name: getattr(parse("finding-confirmed.json"), f.name)
        for f in dataclasses.fields(ra.AssuranceFinding)
        if f.name not in ("finding_id", "content_hash")
    }
    bad = dataclasses.replace(k["validation"], replay_result_id=k["origin_result_id"])
    with pytest.raises(ra.RuntimeAssuranceError, match="result"):
        ra.seal_finding(**{**k, "validation": bad})


def test_confirmation_refuses_evidence_about_another_invariant():
    # Owner review on 988dae0: the violated verdict cited `obs-2`, an
    # observation about `policy-gate-hit`, and still confirmed a
    # `no-secret-in-comment` finding that `check_result_against_run` rejects.
    cand = parse("finding-candidate.json")
    replay, rres = _replay_kwargs()
    assert "no-secret-in-comment" not in {o.observation_id: o for o in replay.observations}["obs-2"].invariant_ids
    wrong = ra.seal_result(
        **{
            **_result_kwargs(rres),
            "invariant_verdicts": (
                ra.InvariantVerdict("no-secret-in-comment", "violated", ("obs-2",)),
                ra.InvariantVerdict("policy-gate-hit", "held", ("obs-2",)),
            ),
        }
    )
    for check in (
        lambda: ra.check_finding_confirmation(cand, replay, wrong),
        lambda: ra.transition_finding(cand, "confirmed", replay_run=replay, replay_result=wrong),
    ):
        with pytest.raises(ra.RuntimeAssuranceError, match="not about the invariant"):
            check()
    unknown = ra.seal_result(
        **{
            **_result_kwargs(rres),
            "invariant_verdicts": (
                ra.InvariantVerdict("no-secret-in-comment", "violated", ("obs-1", "obs-404")),
                rres.invariant_verdicts[1],
            ),
        }
    )
    with pytest.raises(ra.RuntimeAssuranceError, match="not about the invariant"):
        ra.check_finding_confirmation(cand, replay, unknown)


def test_confirmation_refuses_replay_evidence_the_run_did_not_observe():
    cand = parse("finding-candidate.json")
    replay, rres = _replay_kwargs()
    forged = ra.seal_result(**{**_result_kwargs(rres), "evidence_ids": (*rres.evidence_ids, "ev-ffffffffffffffff")})
    with pytest.raises(ra.RuntimeAssuranceError, match="evidence"):
        ra.check_finding_confirmation(cand, replay, forged)
