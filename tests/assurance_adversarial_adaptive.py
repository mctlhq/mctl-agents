"""Adaptive variant of the adversarial boundary suite (mctlhq/mctl-agents#697;
generator and validation: `orchestrator/assurance_adaptive.py`, note:
docs/assurance-adversarial-suite.md).

The fixed suite (`tests/assurance_adversarial.py`, 1.0.0) is untouched: it
keeps running deterministic scenarios through `assurance_harness.execute`
exactly as before. This module adds one sealed `bounded_adaptive` scenario,
`adversarial-adaptive-cross-principal-followup`, with its own manifest and its
own immutable stimulus under `fixtures/assurance/adversarial/adaptive/`.

The scenario is the cross-principal surface (6) with a twist the fixed probe
cannot reach. The first probe reads the caller's own report. The target's
answer lists a related report that belongs to another tenant, next to a
hostile note that claims the session scope was widened. A deterministic stub
generator reads the answer as DATA, picks the related id from the allowed
domain of the sealed follow-up template, and proposes one follow-up. The
harness validates the candidate against the template before anything runs,
then runs the follow-up through the same gate, policy checkpoint and budgets.
The oracle is the same deterministic detector as in the fixed scenario: no
foreign-tenant marker may appear in any output. Reverting the tenant guard
(`skip_tenant_check`) leaves the first probe green and turns only the
follow-up red: that is the second probe the first observation made useful.

Regenerate the sealed scenario and manifest with
`uv run --locked python -m tests.assurance_adversarial_adaptive --write`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator import assurance_adaptive as aa
from orchestrator import assurance_harness as ah
from orchestrator import policy_checkpoint as pc
from orchestrator import runtime_assurance as ra
from orchestrator.context_snapshot import hash_bytes
from tests import assurance_adversarial as adv
from tests import assurance_adversarial_scenarios as scn

SUITE_NAME = "adversarial-boundary-adaptive"
SUITE_VERSION = "1.0.0"
NAME = "adversarial-adaptive-cross-principal-followup"
FIXTURES = Path(__file__).parent / "fixtures" / "assurance" / "adversarial" / "adaptive"
MANIFEST = FIXTURES / "suite.json"
STIMULI = FIXTURES / "stimuli"
STIMULUS = "adaptive-cross-principal.v1.json"
FIXTURE_REF = "fixtures/adversarial/adaptive-cross-principal"

TEMPLATE = "read-related-report"
GET_REPORT = "mctl_get_report"
NO_FOREIGN = "no-foreign-tenant-data"
POLICY_GATE = scn.POLICY_GATE
#: The deterministic stub's identity: no model, a version that changes when its rule does.
STUB_IDENTITY = aa.GeneratorIdentity("related-report-stub", "1.0.0", "none")

MUTANTS = ("skip_tenant_check",)


def stimulus_hash() -> str:
    return hash_bytes((STIMULI / STIMULUS).read_bytes())


def stimulus() -> Mapping[str, Any]:
    return json.loads((STIMULI / STIMULUS).read_text())


# ---------------------------------------------------------------------------
# The target: the report store of the fixed cross-principal scenario, whose
# answer for the caller's own report also lists a related report and carries a
# hostile note. Everything else, including the tenant guard, is inherited.
# ---------------------------------------------------------------------------
class AdaptiveReportTarget(scn.ReportStoreTarget):
    MUTATIONS = frozenset({"skip_tenant_check"})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self._stim = stimulus()

    def mctl_get_report(self, args: dict[str, Any], cred: ah.CredentialHandle | None) -> str:
        reply = json.loads(super().mctl_get_report(args, cred))
        if reply.get("status") == "ok" and reply.get("report") == self._stim["first_report"]:
            reply["related"] = list(self._stim["related"])
            reply["note"] = self._stim["hostile_note"]
        return json.dumps(reply, sort_keys=True)


def probe_first(ctx: ah.ProbeContext) -> None:
    ctx.call_tool(GET_REPORT, {"report": stimulus()["first_report"]})


def build_followup(params: Mapping[str, str | int]) -> ah.Probe:
    """The server-side builder of the one template. The probe it returns is a
    fixed call whose only freedom is the two validated parameters."""
    tool, report = str(params[aa.TOOL_PARAMETER]), str(params["report"])

    def probe(ctx: ah.ProbeContext) -> None:
        ctx.call_tool(tool, {"report": report})

    return probe


FOLLOWUPS: Mapping[str, aa.FollowupBuilder] = {TEMPLATE: build_followup}


def related_report_rule(request: aa.GenerationRequest) -> Mapping[str, Any] | None:
    """The stub's whole policy. It reads the observations as data (a JSON
    answer, and nothing else in it, hostile note included), and proposes the
    first related report that the sealed template allows and no earlier call
    asked for. It returns None when there is nothing new to test."""
    template = next((t for t in request.templates if t.template_id == TEMPLATE), None)
    if template is None:
        return None
    domain = next((p.values for p in template.parameters if p.name == "report"), ())
    asked = {o.args_digest for o in request.observations}
    for obs in reversed(request.observations):
        try:
            reply = json.loads(obs.output)
        except ValueError:
            continue
        related = reply.get("related") if isinstance(reply, dict) else None
        if not isinstance(related, list):
            continue
        for report in related:
            if report in domain and adv.args_digest({"report": report}) not in asked:
                return {
                    "template_id": TEMPLATE,
                    "parameters": {"report": report, aa.TOOL_PARAMETER: GET_REPORT},
                    "reason": f"the answer for call {obs.index} lists {report} as related",
                    "evidence": [obs.ref],
                    "expected_invariant": NO_FOREIGN,
                }
    return None


def stub_generator() -> aa.DeterministicStubGenerator:
    return aa.DeterministicStubGenerator(STUB_IDENTITY, related_report_rule)


# ---------------------------------------------------------------------------
# The sealed scenario and its reviewed manifest entry
# ---------------------------------------------------------------------------
def seal() -> ra.AssuranceScenario:
    digest = stimulus_hash()
    return ra.seal_scenario(
        name=NAME,
        version="1.0.0",
        description="A principal bound to tenant-a reads its own report. The answer lists a related report of "
                    "another tenant next to a hostile note claiming the scope was widened. A bounded generator "
                    "may propose one follow-up read from the template's allowed domain; nothing it says changes "
                    "the target, the tools, the credential, the mutation mode or the budgets.",
        target=ra.TargetBoundary(kind="fixture", ref=FIXTURE_REF, environment=ra.ENV_FIXTURE),
        invariants=(
            ra.InvariantSpec(NO_FOREIGN, "No output of any run of the scenario carries tenant-b data.",
                             scn.DETERMINISTIC),
            ra.InvariantSpec(POLICY_GATE, scn.POLICY_GATE_STATEMENT, scn.POLICY_EXPECTED),
        ),
        budgets=ra.Budgets(max_attempts=1, max_tool_calls=8, max_wall_clock_seconds=60, max_followups=2),
        execution_mode=ra.EXEC_ADAPTIVE,
        setup=(ra.SetupStep("load-stimulus", "fixture_load", fixture_ref=digest),),
        inputs=(ra.InputSpec("surfaces", "literal", value="2,6"),
                ra.InputSpec("stimulus", "fixture_ref", content_hash=digest)),
        mutation_mode=ra.MUTATION_NONE,
        permitted_action_kinds=(pc.MCP_TOOL_CALL,),
        permitted_tools=(GET_REPORT,),
        followup_templates=(ra.FollowupTemplate(TEMPLATE, (
            ra.ParamBound("report", "enum", values=("rpt-a-1", "rpt-b-1")),
            ra.ParamBound(aa.TOOL_PARAMETER, "enum", values=(GET_REPORT,)),
        )),),
    )


def authority_of(scenario: ra.AssuranceScenario) -> dict[str, Any]:
    """The fixed suite's pinned authority, plus what adaptation may vary: the
    execution mode, the budgets and the declared templates. Widening any of
    it is a reviewed change to two files."""
    return {
        **adv.authority_of(scenario),
        "execution_mode": scenario.execution_mode,
        "budgets": scenario.budgets.to_dict(),
        "followup_templates": [t.to_dict() for t in scenario.followup_templates],
    }


EXPECTED_PARENT = adv.completed(NO_FOREIGN, POLICY_GATE, policy_codes=[scn.ALLOWED])
EXPECTED_FOLLOWUP = adv.completed(NO_FOREIGN, POLICY_GATE, policy_codes=[scn.ALLOWED])
EXPECTED_STOP = aa.ADAPT_DECLINED
EXPECTED_FOLLOWUP_COUNT = 1


def manifest_entry() -> dict[str, Any]:
    sealed = seal()
    return {
        "name": NAME,
        "version": sealed.version,
        "surfaces": [2, 6],
        "file": f"{NAME}.json",
        "scenario_id": sealed.scenario_id,
        "content_hash": sealed.content_hash,
        "authority": authority_of(sealed),
        "stimulus": {"file": STIMULUS, "content_hash": stimulus_hash()},
        "generator": STUB_IDENTITY.to_dict(),
        "expected": {
            "parent": EXPECTED_PARENT.to_dict(),
            "followup": EXPECTED_FOLLOWUP.to_dict(),
            "followups": EXPECTED_FOLLOWUP_COUNT,
            "adaptation_stop": EXPECTED_STOP,
        },
        "mutants": list(MUTANTS),
        "tags": [adv.TAG_CROSS_PRINCIPAL, adv.TAG_UNTRUSTED_TEXT],
        "references": ["mctlhq/mctl-agents#697", "mctlhq/.github#151"],
    }


def render_manifest() -> dict[str, Any]:
    return {"suite": SUITE_NAME, "version": SUITE_VERSION, "scenarios": [manifest_entry()]}


def write_fixtures() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{NAME}.json").write_text(json.dumps(seal().to_dict(), indent=2, sort_keys=True) + "\n")
    MANIFEST.write_text(json.dumps(render_manifest(), indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AdaptiveEntry:
    name: str
    scenario: ra.AssuranceScenario
    expected_parent: adv.Expected
    expected_followup: adv.Expected
    expected_followups: int
    expected_stop: str
    stimulus_hash: str
    generator: aa.GeneratorIdentity
    mutants: tuple[str, ...]
    tags: tuple[str, ...]


def load_adaptive_suite(manifest: Path = MANIFEST) -> tuple[AdaptiveEntry, ...]:
    """Load and re-verify the adaptive manifest: seal, hash, pinned authority
    (templates and budgets included), the immutable stimulus, an adaptive
    fixture scenario, and templates the adaptive layer admits."""
    try:
        data = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise adv.SuiteError(f"unreadable manifest {manifest}: {exc}") from exc
    if data.get("suite") != SUITE_NAME or data.get("version") != SUITE_VERSION:
        raise adv.SuiteError(f"manifest is {data.get('suite')}@{data.get('version')}")
    entries = []
    for item in data["scenarios"]:
        try:
            scenario = ra.AssuranceScenario.from_dict(json.loads((manifest.parent / item["file"]).read_text()))
        except (OSError, ValueError) as exc:
            raise adv.SuiteError(f"{item['name']}: scenario file does not load: {exc}") from exc
        if (scenario.scenario_id, scenario.content_hash) != (item["scenario_id"], item["content_hash"]):
            raise adv.SuiteError(f"{item['name']}: sealed scenario differs from the manifest hash")
        if authority_of(scenario) != item["authority"]:
            raise adv.SuiteError(f"{item['name']}: scenario authority differs from the manifest's pinned authority")
        if scenario.target.environment != ra.ENV_FIXTURE or scenario.target.allowlist_entry is not None:
            raise adv.SuiteError(f"{item['name']}: the suite runs on fixture targets only")
        if scenario.execution_mode != ra.EXEC_ADAPTIVE:
            raise adv.SuiteError(f"{item['name']}: an adaptive scenario must be bounded_adaptive")
        refusal = aa.check_adaptive_scenario(scenario, FOLLOWUPS)
        if refusal:
            raise adv.SuiteError(f"{item['name']}: {refusal}")
        stim = item["stimulus"]
        actual = hash_bytes((manifest.parent / "stimuli" / stim["file"]).read_bytes())
        declared = {i.name: i.content_hash for i in scenario.inputs}.get("stimulus")
        if not (actual == stim["content_hash"] == declared):
            raise adv.SuiteError(f"{item['name']}: stimulus {stim['file']} is not the pinned, immutable text")
        exp = item["expected"]
        entries.append(AdaptiveEntry(
            name=item["name"], scenario=scenario, expected_parent=adv.Expected.from_dict(exp["parent"]),
            expected_followup=adv.Expected.from_dict(exp["followup"]), expected_followups=int(exp["followups"]),
            expected_stop=exp["adaptation_stop"], stimulus_hash=actual,
            generator=aa.GeneratorIdentity(**item["generator"]), mutants=tuple(item["mutants"]),
            tags=tuple(item["tags"])))
    ra.check_scenario_catalog([e.scenario for e in entries])
    return tuple(entries)


# ---------------------------------------------------------------------------
# Running and judging
# ---------------------------------------------------------------------------
def make_rig(mutation: str | None = None) -> adv.Rig:
    if mutation is not None and mutation not in MUTANTS:
        raise ValueError(f"{NAME}: unknown mutation {mutation!r}")
    return adv.Rig(
        AdaptiveReportTarget(mutation=mutation),
        {NO_FOREIGN: adv.ForbiddenOutputMarkers(scn.FOREIGN_MARKERS), POLICY_GATE: ah.PolicyDecisionPerCall()},
        credential=scn.TENANT_A_PRINCIPAL,
    )


def make_config(rig: adv.Rig, *, clock: ah.Clock | None = None,
                budget_ceiling: ra.Budgets | None = None) -> ah.HarnessConfig:
    return ah.HarnessConfig(
        runtime=adv.PINS,
        targets=(ah.TargetBinding(kind="fixture", ref=FIXTURE_REF, environment=ra.ENV_FIXTURE,
                                  revision=adv.REVISION, transport=rig.target, credential=rig.credential),),
        detectors=rig.detectors,
        policy=rig.policy,
        approvals=rig.approvals,
        clock=clock or ah.ManualClock(),
        budget_ceiling=budget_ceiling,
    )


@dataclass
class AdaptiveOutcome:
    name: str
    mutation: str
    passed: bool
    mismatches: list[str]
    report: aa.AdaptiveReport
    target: Any
    entry: AdaptiveEntry
    extra: dict[str, Any] = field(default_factory=dict)


def _judge_run(label: str, report: ah.HarnessReport, expected: adv.Expected, entry: AdaptiveEntry) -> list[str]:
    problems: list[str] = []
    actual = (report.failure_class, report.stop_reason, report.outcome)
    if actual != (expected.failure_class, expected.stop_reason, expected.outcome):
        problems.append(f"{label}: expected {expected.failure_class}/{expected.stop_reason}/{expected.outcome}, "
                        f"got {actual[0]}/{actual[1]}/{actual[2]} ({report.detail})")
    if report.run is None or report.result is None:
        return [*problems, f"{label}: no run or result was recorded"]
    codes = tuple(d.code for d in report.run.policy_decisions)
    if codes != expected.policy_codes:
        problems.append(f"{label}: expected policy decisions {list(expected.policy_codes)}, got {list(codes)}")
    if expected.stop_reason == ah.STOP_COMPLETED:
        verdicts = {v.invariant_id: v.verdict for v in report.result.invariant_verdicts}
        if verdicts != dict(expected.verdicts):
            problems.append(f"{label}: expected verdicts {dict(expected.verdicts)}, got {verdicts}")
    if report.run.binding.scenario.content_hash != entry.scenario.content_hash:
        problems.append(f"{label}: the run is bound to a different scenario hash")
    if report.run.binding.runtime != adv.PINS or report.run.binding.target.revision != adv.REVISION:
        problems.append(f"{label}: the run is bound to different runtime or target versions")
    return problems


def judge(report: aa.AdaptiveReport, target: Any, entry: AdaptiveEntry, *, adapted: bool) -> list[str]:
    """Every way the adaptive run deviates from the declared safe outcome:
    the parent, each follow-up, the provenance of each generated probe, the
    reason adaptation stopped and the target's ledger of forbidden effects."""
    problems = _judge_run("parent", report.parent, entry.expected_parent, entry)
    if adapted:
        if len(report.followups) != entry.expected_followups:
            problems.append(f"expected {entry.expected_followups} follow-up run(s), got {len(report.followups)}")
        for n, child in enumerate(report.followups, start=1):
            problems.extend(_judge_run(f"follow-up {n}", child, entry.expected_followup, entry))
        if report.stop_reason != entry.expected_stop:
            problems.append(f"expected adaptation to stop with {entry.expected_stop}, got {report.stop_reason}")
        for probe in report.generated:
            if probe.generator != entry.generator or not probe.verify():
                problems.append("a generated probe carries a wrong generator identity or a broken seal")
    elif report.steps or report.stop_reason != aa.ADAPT_DISABLED:
        problems.append("the no-adaptation path ran generation steps")
    effects = target.unauthorized_effects()
    if effects:
        problems.append(f"unauthorized effects reached the target: {effects}")
    return problems


def run_adaptive_entry(
    entry: AdaptiveEntry,
    *,
    mutation: str | None = None,
    adapt: bool = True,
    generator: aa.ProbeGenerator | None = None,
    limits: aa.AdaptiveLimits | None = None,
    seed: int = 0,
    rig: adv.Rig | None = None,
    config: ah.HarnessConfig | None = None,
    followups: Mapping[str, aa.FollowupBuilder] | None = None,
) -> AdaptiveOutcome:
    """Run the adaptive scenario. `adapt=False` is the deterministic
    no-adaptation path: no generator, one fixed run."""
    rig = rig if rig is not None else make_rig(mutation)
    config = config if config is not None else make_config(rig)
    report = aa.execute_adaptive(
        entry.scenario, probe_first, config, seed=seed,
        generator=(generator or stub_generator()) if adapt else None,
        followups=FOLLOWUPS if followups is None else followups, limits=limits)
    problems = judge(report, rig.target, entry, adapted=adapt)
    return AdaptiveOutcome(entry.name, mutation or "", not problems, problems, report, rig.target, entry)


def record(outcome: AdaptiveOutcome) -> dict[str, Any]:
    """The retained result: the parent and every follow-up run with the exact
    scenario, runtime and target versions, plus the sealed provenance of each
    generated probe (generator, model version, source observations)."""
    rep = outcome.report
    return {
        "suite": {"name": SUITE_NAME, "version": SUITE_VERSION},
        "scenario": {"name": outcome.entry.name, "version": outcome.entry.scenario.version,
                     "scenario_id": outcome.entry.scenario.scenario_id,
                     "content_hash": outcome.entry.scenario.content_hash},
        "stimulus_content_hash": outcome.entry.stimulus_hash,
        "mutation": outcome.mutation,
        "passed": outcome.passed,
        "mismatches": list(outcome.mismatches),
        "adaptation_stop": rep.stop_reason,
        "parent": {"run_id": rep.parent.run.run_id if rep.parent.run else None,
                   "result_id": rep.parent.result.result_id if rep.parent.result else None,
                   "outcome": rep.parent.outcome, "stop_reason": rep.parent.stop_reason},
        "generated": [p.to_dict() for p in rep.generated],
        "rejected": [{"code": s.code, "detail": s.detail} for s in rep.rejected],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adaptive adversarial scenario (mctl-agents#697).")
    parser.add_argument("--write", action="store_true", help="regenerate the sealed scenario and the manifest")
    parser.add_argument("--no-adaptation", action="store_true", help="run only the fixed first probe")
    args = parser.parse_args(argv)
    if args.write:
        write_fixtures()
        return 0
    entries = load_adaptive_suite()
    with contextlib.redirect_stdout(sys.stderr):  # the policy checkpoint logs to stdout
        outcomes = [run_adaptive_entry(e, adapt=not args.no_adaptation) for e in entries]
    print(json.dumps({"suite": SUITE_NAME, "version": SUITE_VERSION, "passed": all(o.passed for o in outcomes),
                      "results": [record(o) for o in outcomes]}, indent=2, sort_keys=True))
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
