"""Bounded adaptive follow-up probe generation for runtime assurance
(mctlhq/mctl-agents#697; harness ADR 023, contracts ADR 022, suite note
docs/assurance-adversarial-suite.md).

A *generator* may look at what the first run observed and propose the next
probe. It is advice, never authority. Every power stays with the deterministic
scenario and harness envelope:

- The only shape a generator can author is a `ProbeCandidate`: a template id
  and parameters (a `ra.FollowupProposal` underneath), a reason, evidence
  references and the invariant under test. It has no field for a target,
  tenant, principal, credential, tool list, mutation mode or budget, and a
  candidate that names one is rejected, not trimmed.
- The scenario declares what may vary: its sealed `followup_templates` (an
  enum, an integer range or a declared fixture ref per parameter). A tool
  choice is an enum parameter named `tool` whose values must already be in the
  scenario's `permitted_tools`. `validate_candidate` checks the candidate
  against the schema and that contract BEFORE anything runs.
- A validated candidate does not carry the call itself: a server-side
  `FollowupBuilder` registered per template turns the validated parameters
  into a probe. The probe still runs through `assurance_harness.execute`, so
  the gate, the policy checkpoint, the credential screen and the budgets of
  the harness apply to it exactly as to a fixed scenario.
- Untrusted target output reaches the generator only as data in an immutable
  `GenerationRequest`; nothing in it is read as an instruction by this module.
- A generator that errors, times out, returns something malformed, names an
  authority field or tries to leave its limits stops adaptation. Nothing falls
  back to a wider path, and the parent result stays the authoritative one.
- Depth, generation attempts, follow-up runs, tool calls and wall time are all
  bounded, and the bounds can only be narrowed from the scenario's budgets
  (and the harness `budget_ceiling`), never raised.
- A refused, policy-denied or otherwise incomplete run ends adaptation. An
  observation that was not completed is not evidence of absence, and a denial
  is never an invitation to try a weaker route.
- Every generated probe is recorded in a sealed `GeneratedProbe`: the parent
  scenario and run, the exact generator and model version, the candidate and
  the source observations it was generated from.

Without a generator (`generator=None`, the default) `execute_adaptive` is one
call to `assurance_harness.execute`, byte for byte. This module is imported by
nothing in `orchestrator/` and a real model-backed generator is not wired on
anywhere: one may only be supplied by the caller, behind `ProbeGenerator`.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol

from orchestrator import assurance_harness as ah
from orchestrator import runtime_assurance as ra
from orchestrator.context_snapshot import canonical_json, hash_bytes
from orchestrator.redaction import contains_credential

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------
#: The template parameter that selects a tool. Its value must be a tool the
#: sealed scenario already permits.
TOOL_PARAMETER = "tool"

REJECT_MALFORMED = "malformed"
REJECT_SCHEMA = "schema_invalid"
REJECT_UNKNOWN_FIELD = "unknown_field"
REJECT_AUTHORITY_FIELD = "authority_field"
REJECT_RESERVED_PARAMETER = "reserved_parameter"
REJECT_CREDENTIAL = "credential_value"
REJECT_TOOL = "tool_not_permitted"
REJECT_BOUNDS = "out_of_bounds"
REJECT_INVARIANT = "unknown_invariant"
REJECT_EVIDENCE_MISSING = "evidence_missing"
REJECT_EVIDENCE_UNKNOWN = "evidence_unknown"
REJECT_DUPLICATE = "duplicate_candidate"
REJECT_NO_BUILDER = "no_builder"
REJECT_CODES = frozenset({
    REJECT_MALFORMED, REJECT_SCHEMA, REJECT_UNKNOWN_FIELD, REJECT_AUTHORITY_FIELD, REJECT_RESERVED_PARAMETER,
    REJECT_CREDENTIAL, REJECT_TOOL, REJECT_BOUNDS, REJECT_INVARIANT, REJECT_EVIDENCE_MISSING,
    REJECT_EVIDENCE_UNKNOWN, REJECT_DUPLICATE, REJECT_NO_BUILDER,
})
#: A candidate that reaches for authority means the generator is not to be
#: asked again: adaptation stops. These are never "skipped and retried".
VIOLATION_CODES = frozenset({REJECT_AUTHORITY_FIELD, REJECT_RESERVED_PARAMETER, REJECT_CREDENTIAL, REJECT_TOOL})
#: A candidate that is not even a candidate (wrong shape, unknown field).
MALFORMED_CODES = frozenset({REJECT_MALFORMED, REJECT_SCHEMA, REJECT_UNKNOWN_FIELD})

ADAPT_DISABLED = "adaptation_disabled"
ADAPT_NOT_PERMITTED = "adaptation_not_permitted"
ADAPT_PARENT_INCOMPLETE = "parent_not_completed"
ADAPT_FOLLOWUP_INCOMPLETE = "followup_not_completed"
ADAPT_DECLINED = "generator_declined"
ADAPT_GENERATOR_ERROR = "generator_error"
ADAPT_GENERATOR_TIMEOUT = "generator_timeout"
ADAPT_GENERATOR_MALFORMED = "generator_malformed"
ADAPT_GENERATOR_VIOLATION = "generator_violation"
ADAPT_BUILDER_ERROR = "builder_error"
ADAPT_DEPTH = "depth_limit"
ADAPT_FOLLOWUPS = "followup_limit"
ADAPT_CANDIDATES = "candidate_limit"
ADAPT_TOOL_BUDGET = "tool_budget_exhausted"
ADAPT_TIME_BUDGET = "time_budget_exhausted"
ADAPT_STOPS = frozenset({
    ADAPT_DISABLED, ADAPT_NOT_PERMITTED, ADAPT_PARENT_INCOMPLETE, ADAPT_FOLLOWUP_INCOMPLETE, ADAPT_DECLINED,
    ADAPT_GENERATOR_ERROR, ADAPT_GENERATOR_TIMEOUT, ADAPT_GENERATOR_MALFORMED, ADAPT_GENERATOR_VIOLATION,
    ADAPT_BUILDER_ERROR, ADAPT_DEPTH, ADAPT_FOLLOWUPS, ADAPT_CANDIDATES, ADAPT_TOOL_BUDGET, ADAPT_TIME_BUDGET,
})

STEP_EXECUTED, STEP_REJECTED, STEP_STOPPED = "executed", "rejected", "stopped"

#: Hard ceilings. `AdaptiveLimits` can only go below them, and the scenario's
#: budgets then narrow them further.
ABSOLUTE_MAX_DEPTH = 4
ABSOLUTE_MAX_CANDIDATES = 16
ABSOLUTE_GENERATOR_TIMEOUT_SECONDS = 30.0
MAX_REASON_CHARS = 300
MAX_EVIDENCE_REFS = 8
MAX_OBSERVATION_CHARS = 4096
MAX_OBSERVATIONS_SHOWN = 32

CANDIDATE_FIELDS = frozenset({"template_id", "parameters", "reason", "evidence", "expected_invariant"})
#: Keys that name something only the scenario or the harness may decide. A
#: candidate carrying one is an attempt to widen authority, whatever its value.
_AUTHORITY_KEY = re.compile(
    r"(?i)target|(?:^|_)ref(?:$|_)|environment|tenant|principal|actor|credential|token|secret|auth|tool|capabilit"
    r"|mutat|budget|(?:^|_)max|attempt|timeout|time|cost|retry|action_kind|permit|grant|approv|polic|scope|depth"
    r"|limit|allowlist|resource|on_behalf|delegat"
)
_EVIDENCE_REF = re.compile(r"arn-[0-9a-f]{16}/\d{1,6}/sha256:[0-9a-f]{64}")


def _authority_key(key: str) -> bool:
    return bool(_AUTHORITY_KEY.search(key.replace("-", "_")) or ah.is_reserved_argument_key(key))


class CandidateRejected(ValueError):
    """A candidate did not pass validation. `code` is one of `REJECT_CODES`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# What a generator sees and what it may author
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeneratorIdentity:
    """The exact generator and model that produced a probe. A deterministic
    stub says `model_version="none"`. Required and non-empty: a probe without
    a known author is not generated."""

    generator_id: str
    version: str
    model_version: str

    def __post_init__(self) -> None:
        for name in ("generator_id", "version", "model_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"generator identity: {name} must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        return {"generator_id": self.generator_id, "version": self.version, "model_version": self.model_version}


@dataclass(frozen=True)
class Observation:
    """One call of a completed run, as data. `output` is untrusted target
    text (truncated; `output_hash` covers the whole text). It is never an
    instruction to the harness or to this module."""

    run_id: str
    index: int
    tool: str
    status: str
    output_hash: str
    #: Digest of the exact arguments the call carried, so a generator can tell
    #: which probes already ran without being handed the arguments.
    args_digest: str = ""
    output: str = ""
    truncated: bool = False

    @property
    def ref(self) -> str:
        """The stable evidence reference a candidate cites."""
        return f"{self.run_id}/{self.index}/{self.output_hash}"

    def summary(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "run_id": self.run_id, "index": self.index, "tool": self.tool,
            "status": self.status, "output_hash": self.output_hash,
            "args_digest": self.args_digest,
        }


@dataclass(frozen=True)
class GenerationRequest:
    """Everything a generator is given, immutable. It holds no target
    binding, credential, policy or budget handle: only what the sealed
    scenario declares public, the observations so far and what is left."""

    scenario_name: str
    scenario_version: str
    scenario_hash: str
    permitted_tools: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    templates: tuple[ra.FollowupTemplate, ...]
    #: 1-based depth of the follow-up being asked for.
    depth: int
    remaining_followups: int
    remaining_tool_calls: int
    remaining_wall_clock_seconds: int
    observations: tuple[Observation, ...]
    #: Canonical keys of candidates already run, so a generator can avoid them.
    tried: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProbeCandidate:
    """A validated candidate. Build one with `parse_candidate`."""

    template_id: str
    parameters: tuple[tuple[str, str | int], ...]
    reason: str
    evidence: tuple[str, ...]
    expected_invariant: str

    @property
    def proposal(self) -> ra.FollowupProposal:
        return ra.FollowupProposal(self.template_id, self.parameters)

    @property
    def key(self) -> str:
        """Identity of what would run, independent of the stated reason."""
        return hash_bytes(canonical_json({"template_id": self.template_id, "parameters": dict(self.parameters)}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "parameters": dict(self.parameters),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "expected_invariant": self.expected_invariant,
        }


class ProbeGenerator(Protocol):
    """The interface a generator implements. `propose` returns a candidate
    (a mapping or a `ProbeCandidate`) or `None` for "nothing more to test".
    A deterministic stub and a model-backed generator are the same thing to
    this module; neither can do more than return a candidate."""

    identity: GeneratorIdentity

    def propose(self, request: GenerationRequest) -> Any: ...


@dataclass(frozen=True)
class DeterministicStubGenerator:
    """A generator that is a pure function of the request. No model, no
    network, no clock: the same request gives the same candidate."""

    identity: GeneratorIdentity
    rule: Callable[[GenerationRequest], Any]

    def propose(self, request: GenerationRequest) -> Any:
        return self.rule(request)


#: A server-side builder, registered per template id, from validated
#: parameters to the probe that runs through the harness.
FollowupBuilder = Callable[[Mapping[str, str | int]], ah.Probe]


@dataclass(frozen=True)
class AdaptiveLimits:
    """How far adaptation may go. Each value can only be at or below an
    absolute ceiling; tool calls and wall time are not configurable here at
    all: they are one pool, the scenario's own budget (narrowed by the
    harness `budget_ceiling`), shared by the parent and every follow-up."""

    max_depth: int = 2
    max_candidates: int = 4
    max_followups: int = 2
    generator_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("max_depth", ABSOLUTE_MAX_DEPTH),
            ("max_candidates", ABSOLUTE_MAX_CANDIDATES),
            ("max_followups", ra.MAX_FOLLOWUPS_CAP),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= ceiling:
                raise ValueError(f"adaptive limits: {name} must be an integer within 0..{ceiling}")
        timeout = self.generator_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not 0 < timeout <= ABSOLUTE_GENERATOR_TIMEOUT_SECONDS
            or math.isnan(timeout)
        ):
            raise ValueError(
                f"adaptive limits: generator_timeout_seconds must be within 0..{ABSOLUTE_GENERATOR_TIMEOUT_SECONDS}"
            )


# ---------------------------------------------------------------------------
# Admission of a scenario for adaptation
# ---------------------------------------------------------------------------
def check_adaptive_scenario(scenario: ra.AssuranceScenario, followups: Mapping[str, FollowupBuilder]) -> str:
    """Why this scenario may not be adapted, or an empty string. Adaptation
    needs a scenario that declared it (`bounded_adaptive`, a follow-up budget,
    templates), a builder for every template, and templates whose tool choices
    are tools the scenario already permits and whose parameters name no
    authority."""
    if scenario.execution_mode != ra.EXEC_ADAPTIVE:
        return "the scenario is not bounded_adaptive"
    if scenario.budgets.max_followups < 1 or not scenario.followup_templates:
        return "the scenario declares no follow-up budget or template"
    for template in scenario.followup_templates:
        if template.template_id not in followups:
            return f"template {template.template_id!r} has no registered builder"
        for param in template.parameters:
            if _authority_key(param.name) and param.name != TOOL_PARAMETER:
                return f"template {template.template_id!r} declares the authority-shaped parameter {param.name!r}"
            if param.name == TOOL_PARAMETER:
                if param.kind != "enum" or not set(param.values) <= set(scenario.permitted_tools):
                    return f"template {template.template_id!r} offers a tool the scenario does not permit"
    return ""


# ---------------------------------------------------------------------------
# Validation of a candidate, before anything runs
# ---------------------------------------------------------------------------
def _check_shape(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, ProbeCandidate):
        raw = raw.to_dict()
    if not isinstance(raw, Mapping):
        raise CandidateRejected(REJECT_MALFORMED, f"expected an object, got {type(raw).__name__}")
    return raw


def _check_fields(raw: Mapping[str, Any]) -> None:
    extra = sorted(str(k) for k in raw if k not in CANDIDATE_FIELDS)
    authority = [k for k in extra if _authority_key(k)]
    if authority:
        raise CandidateRejected(REJECT_AUTHORITY_FIELD, f"a candidate cannot carry authority field(s) {authority!r}")
    if extra:
        raise CandidateRejected(REJECT_UNKNOWN_FIELD, f"unknown field(s) {extra!r}")
    missing = sorted(CANDIDATE_FIELDS - set(raw))
    if missing:
        raise CandidateRejected(REJECT_SCHEMA, f"missing field(s) {missing!r}")


def _check_types(raw: Mapping[str, Any]) -> tuple[str, dict[str, Any], str, tuple[str, ...], str]:
    template_id, params = raw["template_id"], raw["parameters"]
    reason, evidence, invariant = raw["reason"], raw["evidence"], raw["expected_invariant"]
    if not isinstance(template_id, str) or not isinstance(invariant, str) or not isinstance(reason, str):
        raise CandidateRejected(REJECT_SCHEMA, "template_id, reason and expected_invariant must be strings")
    if not isinstance(params, Mapping) or not all(isinstance(k, str) for k in params):
        raise CandidateRejected(REJECT_SCHEMA, "parameters must be an object with string names")
    if len(reason) > MAX_REASON_CHARS:
        raise CandidateRejected(REJECT_SCHEMA, f"reason exceeds {MAX_REASON_CHARS} characters")
    if (
        not isinstance(evidence, list | tuple)
        or len(evidence) > MAX_EVIDENCE_REFS
        or not all(isinstance(e, str) for e in evidence)
    ):
        raise CandidateRejected(REJECT_SCHEMA, f"evidence must be at most {MAX_EVIDENCE_REFS} strings")
    return template_id, dict(params), reason, tuple(evidence), invariant


def _check_no_authority_in_parameters(params: Mapping[str, Any], reason: str) -> None:
    reserved = sorted(k for k in params if k != TOOL_PARAMETER and _authority_key(k))
    if reserved:
        raise CandidateRejected(REJECT_RESERVED_PARAMETER, f"parameter name(s) {reserved!r} name an authority")
    strings = [reason, *(v for v in params.values() if isinstance(v, str))]
    if any(contains_credential(s) for s in strings):
        raise CandidateRejected(REJECT_CREDENTIAL, "a candidate carries a credential-shaped value")


def _check_tool_in_envelope(params: Mapping[str, Any], scenario: ra.AssuranceScenario) -> None:
    if TOOL_PARAMETER in params:
        tool = params[TOOL_PARAMETER]
        if not isinstance(tool, str) or tool not in scenario.permitted_tools:
            raise CandidateRejected(REJECT_TOOL, f"tool {tool!r} is not permitted by the scenario")


def _check_bounds(scenario: ra.AssuranceScenario, template_id: str, params: Mapping[str, Any]) -> None:
    try:
        proposal = ra.FollowupProposal(template_id, tuple(params.items()))
        ra.validate_followup(scenario, proposal)
    except ra.RuntimeAssuranceError as exc:
        raise CandidateRejected(REJECT_BOUNDS, str(exc)) from None


def _check_invariant(scenario: ra.AssuranceScenario, invariant: str) -> None:
    if invariant not in {i.invariant_id for i in scenario.invariants}:
        raise CandidateRejected(REJECT_INVARIANT, f"{invariant!r} is not an invariant of the scenario")


def _check_evidence(evidence: Sequence[str], known: frozenset[str]) -> None:
    if not evidence:
        raise CandidateRejected(REJECT_EVIDENCE_MISSING, "a follow-up must cite at least one observation")
    unknown = [e for e in evidence if e not in known]
    if unknown:
        raise CandidateRejected(REJECT_EVIDENCE_UNKNOWN, "evidence cites an observation the run did not make")


def _check_not_repeated(key: str, tried: frozenset[str]) -> None:
    if key in tried:
        raise CandidateRejected(REJECT_DUPLICATE, "this probe already ran")


def _check_builder(template_id: str, followups: Mapping[str, FollowupBuilder]) -> None:
    if template_id not in followups:
        raise CandidateRejected(REJECT_NO_BUILDER, f"template {template_id!r} has no registered builder")


def validate_candidate(
    raw: Any,
    scenario: ra.AssuranceScenario,
    *,
    followups: Mapping[str, FollowupBuilder],
    known_evidence: frozenset[str],
    tried: frozenset[str] = frozenset(),
) -> ProbeCandidate:
    """Schema and contract validation of one generator output, in a fixed
    order, raising `CandidateRejected` with a closed code. Returns a candidate
    only when every check passed; nothing has run by then and nothing here
    runs it."""
    shaped = _check_shape(raw)
    _check_fields(shaped)
    template_id, params, reason, evidence, invariant = _check_types(shaped)
    _check_no_authority_in_parameters(params, reason)
    _check_tool_in_envelope(params, scenario)
    _check_bounds(scenario, template_id, params)
    _check_invariant(scenario, invariant)
    _check_evidence(evidence, known_evidence)
    _check_builder(template_id, followups)
    try:
        candidate = ProbeCandidate(template_id, tuple(sorted(params.items())), reason, evidence, invariant)
    except ra.RuntimeAssuranceError as exc:  # pragma: no cover - _check_bounds already built the proposal
        raise CandidateRejected(REJECT_SCHEMA, str(exc)) from None
    _check_not_repeated(candidate.key, tried)
    return candidate


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeneratedProbe:
    """Provenance of one generated probe that ran. Sealed with a content hash
    over every field, so it cannot be edited without showing. The contract's
    `AssuranceRun` carries the parent run and the follow-up proposal; this
    adds the generator, the candidate and the source observations."""

    scenario: Mapping[str, str]
    parent_run_id: str
    parent_result_id: str
    depth: int
    generator: GeneratorIdentity
    candidate: Mapping[str, Any]
    source_observations: tuple[Mapping[str, Any], ...]
    observations_digest: str
    run_id: str
    result_id: str
    record_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = {
            "scenario": dict(self.scenario),
            "parent_run_id": self.parent_run_id,
            "parent_result_id": self.parent_result_id,
            "depth": self.depth,
            "generator": self.generator.to_dict(),
            "candidate": dict(self.candidate),
            "source_observations": [dict(o) for o in self.source_observations],
            "observations_digest": self.observations_digest,
            "run_id": self.run_id,
            "result_id": self.result_id,
        }
        out["record_hash"] = self.record_hash
        return out

    def verify(self) -> bool:
        body = {k: v for k, v in self.to_dict().items() if k != "record_hash"}
        return self.record_hash == hash_bytes(canonical_json(body))


def _seal_probe(**fields_: Any) -> GeneratedProbe:
    draft = GeneratedProbe(**fields_)
    body = {k: v for k, v in draft.to_dict().items() if k != "record_hash"}
    return replace(draft, record_hash=hash_bytes(canonical_json(body)))


@dataclass(frozen=True)
class AdaptationStep:
    """One generation attempt: a probe that ran, a candidate that was
    rejected (with its closed code), or the stop."""

    kind: str
    code: str
    detail: str
    candidate: Mapping[str, Any] | None = None
    probe: GeneratedProbe | None = None
    report: ah.HarnessReport | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "code": self.code,
            "detail": self.detail,
            "candidate": dict(self.candidate) if self.candidate is not None else None,
            "probe": self.probe.to_dict() if self.probe else None,
            "report": self.report.to_dict() if self.report else None,
        }


@dataclass(frozen=True)
class AdaptiveReport:
    """The parent report, untouched, and what adaptation did after it."""

    parent: ah.HarnessReport
    steps: tuple[AdaptationStep, ...]
    stop_reason: str
    generator: GeneratorIdentity | None = None

    @property
    def followups(self) -> tuple[ah.HarnessReport, ...]:
        return tuple(s.report for s in self.steps if s.kind == STEP_EXECUTED and s.report is not None)

    @property
    def generated(self) -> tuple[GeneratedProbe, ...]:
        return tuple(s.probe for s in self.steps if s.probe is not None)

    @property
    def rejected(self) -> tuple[AdaptationStep, ...]:
        return tuple(s for s in self.steps if s.kind == STEP_REJECTED)

    def authoritative(self) -> dict[str, Any]:
        """What a re-run of the same pinned inputs and the same generator
        output must reproduce."""
        return {
            "parent": self.parent.authoritative(),
            "followups": [r.authoritative() for r in self.followups],
            "steps": [(s.kind, s.code) for s in self.steps],
            "stop_reason": self.stop_reason,
            "generator": self.generator.to_dict() if self.generator else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent": self.parent.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "stop_reason": self.stop_reason,
            "generator": self.generator.to_dict() if self.generator else None,
        }


# ---------------------------------------------------------------------------
# Running a generator under a timeout
# ---------------------------------------------------------------------------
_OK, _ERROR, _TIMEOUT = "ok", "error", "timeout"


def _ask(generator: ProbeGenerator, request: GenerationRequest, timeout: float) -> tuple[str, Any]:
    """Call the generator on a daemon thread with a real-time watchdog. A
    generator that raises or does not return in time yields no candidate; a
    result that arrives late is never read."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = generator.propose(request)
        except BaseException as exc:  # noqa: BLE001 - any generator failure, SystemExit included, stops adaptation
            box["error"] = type(exc).__name__

    worker = threading.Thread(target=work, name="assurance-probe-generator", daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    if worker.is_alive():
        return _TIMEOUT, None
    if "error" in box:
        return _ERROR, box["error"]
    return _OK, box.get("value")


OBSERVATION_WITHHELD = "[output withheld: credential shape]"


def _observations_of(report: ah.HarnessReport) -> tuple[Observation, ...]:
    if report.run is None:
        return ()
    run_id = report.run.run_id
    out = []
    for call in report.calls:
        if not call.executed:
            continue
        text = call.output or ""
        # The generator never sees a credential (module contract): a target
        # output with a credential shape is replaced whole, screened before the
        # truncation so a cut cannot hide one. output_hash still covers the
        # original bytes, so the record stays checkable.
        if contains_credential(text):
            text = OBSERVATION_WITHHELD
        out.append(Observation(
            run_id=run_id, index=call.index, tool=call.tool, status=call.status, output_hash=call.output_hash,
            args_digest=call.args_digest, output=text[:MAX_OBSERVATION_CHARS],
            truncated=len(text) > MAX_OBSERVATION_CHARS,
        ))
    return tuple(out)


def _digest_of(observations: Sequence[Observation]) -> str:
    return hash_bytes(canonical_json([o.summary() for o in observations]))


# ---------------------------------------------------------------------------
# The adaptive loop
# ---------------------------------------------------------------------------
def execute_adaptive(
    scenario: Any,
    probe: ah.Probe,
    config: ah.HarnessConfig,
    *,
    seed: int,
    attempt: int = 1,
    generator: ProbeGenerator | None = None,
    followups: Mapping[str, FollowupBuilder] | None = None,
    limits: AdaptiveLimits | None = None,
) -> AdaptiveReport:
    """Run `scenario` with `probe`, then, if a generator is supplied and
    everything stayed inside the envelope, up to the declared number of
    generated follow-up probes. Never raises.

    With `generator=None` this is a single `assurance_harness.execute`: the
    parent report is what a fixed run produces, byte for byte."""
    clock = config.clock
    started = clock.monotonic()
    parent = ah.execute(scenario, probe, config, seed=seed, attempt=attempt)
    if generator is None:
        return AdaptiveReport(parent, (), ADAPT_DISABLED)
    try:
        return _adapt(parent, scenario, config, started, seed, generator, followups or {}, limits or AdaptiveLimits())
    except Exception as exc:  # noqa: BLE001 - the adaptive layer reports its failure and stops; it never widens
        step = AdaptationStep(STEP_STOPPED, ADAPT_GENERATOR_ERROR, f"adaptive layer error: {type(exc).__name__}")
        return AdaptiveReport(parent, (step,), ADAPT_GENERATOR_ERROR, None)


def _stop(parent: ah.HarnessReport, steps: list[AdaptationStep], reason: str, detail: str,
          identity: GeneratorIdentity | None) -> AdaptiveReport:
    steps.append(AdaptationStep(STEP_STOPPED, reason, ra.redact_text(detail, max_chars=300)))
    return AdaptiveReport(parent, tuple(steps), reason, identity)


def _adapt(
    parent: ah.HarnessReport,
    scenario_in: Any,
    config: ah.HarnessConfig,
    started: float,
    seed: int,
    generator: ProbeGenerator,
    followups: Mapping[str, FollowupBuilder],
    limits: AdaptiveLimits,
) -> AdaptiveReport:
    steps: list[AdaptationStep] = []
    identity = getattr(generator, "identity", None)
    if not isinstance(identity, GeneratorIdentity):
        return _stop(parent, steps, ADAPT_NOT_PERMITTED, "the generator has no exact identity and version", None)
    # Only a completed run is evidence. A refused, policy-denied, over-budget or
    # failed run is not "observed absent", and a denial is never an invitation
    # to probe again through another route.
    if parent.stop_reason != ah.STOP_COMPLETED or parent.run is None or parent.result is None:
        return _stop(parent, steps, ADAPT_PARENT_INCOMPLETE, f"the parent run ended {parent.stop_reason}", identity)
    scenario = (
        scenario_in if isinstance(scenario_in, ra.AssuranceScenario) else ra.AssuranceScenario.from_dict(scenario_in)
    )
    refusal = check_adaptive_scenario(scenario, followups)
    if refusal:
        return _stop(parent, steps, ADAPT_NOT_PERMITTED, refusal, identity)

    granted = ah._narrow(scenario.budgets, config.budget_ceiling)
    max_followups = min(limits.max_followups, granted.max_followups)
    max_depth = min(limits.max_depth, max_followups)
    used_tools = parent.result.consumed.tool_calls
    previous = parent
    shown: list[Observation] = list(_observations_of(parent))
    tried: set[str] = set()
    ran = 0
    asked = 0

    while True:
        if ran >= max_followups:
            return _stop(parent, steps, ADAPT_FOLLOWUPS, f"{ran} follow-up run(s) reached the limit", identity)
        if ran + 1 > max_depth:
            return _stop(parent, steps, ADAPT_DEPTH, f"depth {ran + 1} exceeds the limit {max_depth}", identity)
        if asked >= limits.max_candidates:
            return _stop(parent, steps, ADAPT_CANDIDATES, f"{asked} generation attempt(s) reached the limit", identity)
        tools_left = granted.max_tool_calls - used_tools
        if tools_left < 1:
            return _stop(parent, steps, ADAPT_TOOL_BUDGET, "no tool calls left in the scenario budget", identity)
        wall_left = math.floor(granted.max_wall_clock_seconds - (config.clock.monotonic() - started))
        if wall_left < 1:
            return _stop(parent, steps, ADAPT_TIME_BUDGET, "no wall clock left in the scenario budget", identity)

        visible = tuple(shown[-MAX_OBSERVATIONS_SHOWN:])
        request = GenerationRequest(
            scenario_name=scenario.name,
            scenario_version=scenario.version,
            scenario_hash=scenario.content_hash,
            permitted_tools=scenario.permitted_tools,
            invariant_ids=tuple(i.invariant_id for i in scenario.invariants),
            templates=scenario.followup_templates,
            depth=ran + 1,
            remaining_followups=max_followups - ran,
            remaining_tool_calls=tools_left,
            remaining_wall_clock_seconds=wall_left,
            observations=visible,
            tried=tuple(sorted(tried)),
        )
        asked += 1
        status, raw = _ask(generator, request, limits.generator_timeout_seconds)
        if status == _TIMEOUT:
            return _stop(parent, steps, ADAPT_GENERATOR_TIMEOUT, "the generator did not answer in time", identity)
        if status == _ERROR:
            return _stop(parent, steps, ADAPT_GENERATOR_ERROR, f"the generator raised {raw}", identity)
        if raw is None:
            return _stop(parent, steps, ADAPT_DECLINED, "the generator proposed nothing", identity)

        try:
            candidate = validate_candidate(
                raw, scenario, followups=followups, known_evidence=frozenset(o.ref for o in visible),
                tried=frozenset(tried),
            )
        except CandidateRejected as exc:
            steps.append(AdaptationStep(STEP_REJECTED, exc.code, ra.redact_text(exc.detail, max_chars=300),
                                        candidate=_describe(raw)))
            if exc.code in VIOLATION_CODES:
                return _stop(parent, steps, ADAPT_GENERATOR_VIOLATION, f"candidate rejected: {exc.code}", identity)
            if exc.code in MALFORMED_CODES:
                return _stop(parent, steps, ADAPT_GENERATOR_MALFORMED, f"candidate rejected: {exc.code}", identity)
            continue  # a candidate that is merely out of bounds or repeated is skipped and counted

        # The generator may have taken its time: the budget is re-read before the run.
        wall_left = math.floor(granted.max_wall_clock_seconds - (config.clock.monotonic() - started))
        if wall_left < 1:
            return _stop(parent, steps, ADAPT_TIME_BUDGET, "the generator used the remaining wall clock", identity)
        try:
            child_probe = followups[candidate.template_id](dict(candidate.parameters))
        except Exception as exc:  # noqa: BLE001 - a broken server-side builder stops adaptation
            return _stop(parent, steps, ADAPT_BUILDER_ERROR, f"builder raised {type(exc).__name__}", identity)

        tried.add(candidate.key)
        ceiling = ra.Budgets(
            max_attempts=granted.max_attempts,
            max_tool_calls=tools_left,
            max_wall_clock_seconds=wall_left,
            max_followups=granted.max_followups,
        )
        child_config = replace(config, budget_ceiling=ah._narrow(ceiling, config.budget_ceiling))
        if previous.run is None:  # unreachable: only a run that sealed is ever `previous`
            return _stop(parent, steps, ADAPT_FOLLOWUP_INCOMPLETE, "the previous run has no record", identity)
        lineage = ah.FollowupLineage(previous.run.run_id, candidate.proposal, ran + 1)
        child = ah.execute(scenario, child_probe, child_config, seed=seed, attempt=1, lineage=lineage)
        ran += 1
        if child.run is None or child.result is None:
            steps.append(AdaptationStep(STEP_STOPPED, ADAPT_FOLLOWUP_INCOMPLETE, child.detail,
                                        candidate=candidate.to_dict(), report=child))
            return AdaptiveReport(parent, tuple(steps), ADAPT_FOLLOWUP_INCOMPLETE, identity)
        used_tools += child.result.consumed.tool_calls
        record = _seal_probe(
            scenario={"name": scenario.name, "version": scenario.version, "scenario_id": scenario.scenario_id,
                      "content_hash": scenario.content_hash},
            parent_run_id=previous.run.run_id,
            parent_result_id=previous.result.result_id if previous.result else "",
            depth=ran,
            generator=identity,
            candidate=candidate.to_dict(),
            source_observations=tuple(o.summary() for o in visible),
            observations_digest=_digest_of(visible),
            run_id=child.run.run_id,
            result_id=child.result.result_id,
        )
        steps.append(AdaptationStep(STEP_EXECUTED, "executed", "the follow-up probe ran",
                                    candidate=candidate.to_dict(), probe=record, report=child))
        if child.stop_reason != ah.STOP_COMPLETED:
            return AdaptiveReport(parent, tuple(steps), ADAPT_FOLLOWUP_INCOMPLETE, identity)
        previous = child
        shown.extend(_observations_of(child))


def _describe(raw: Any) -> dict[str, Any] | None:
    """A bounded, redacted account of a rejected output, for the record. It is
    descriptive data only, never read back as input."""
    if isinstance(raw, ProbeCandidate):
        raw = raw.to_dict()
    if not isinstance(raw, Mapping):
        return {"type": type(raw).__name__}
    try:
        text = ra.redact_text(canonical_json(dict(raw)).decode(), max_chars=400)
    except Exception:  # noqa: BLE001 - an undescribable output is recorded as such
        return {"type": "mapping", "undescribable": True}
    return {"type": "mapping", "text": text}
