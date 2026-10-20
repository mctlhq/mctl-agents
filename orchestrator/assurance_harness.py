"""Deterministic bounded probe harness for runtime assurance scenarios
(mctlhq/mctl-agents#695, ADR 023: docs/adr/023-runtime-assurance-probe-harness.md).

Executes one `AssuranceScenario` (#694, `orchestrator.runtime_assurance`)
against a server-configured fixture or sandbox target and emits the canonical
`AssuranceRun` / `AssuranceResult` records. The contracts are consumed
unchanged.

Ownership split. A *probe* is the adaptive part (scripted today, model-driven
later): it may only choose which permitted tool to call next and with which
arguments, through `ProbeContext.call_tool`. Everything else is the harness's,
enforced outside the probe and independent of its cooperation:

- the target, its revision, transport and credential come from
  `HarnessConfig.targets`, keyed by the scenario's exact (kind, ref,
  environment); a non-fixture target must also pass
  `check_target_allowlisted` against the server-owned allowlist, and its
  environment must be in `HarnessConfig.enabled_environments` (fixture only by
  default). All of this is checked before the target is touched.
- the credential is passed by the harness to the transport. The probe never
  sees it, and a request whose arguments name a credential or scope key, or
  carry a credential-shaped value, stops the run.
- a tool outside `permitted_tools` stops the run; a non-read tool stops it
  unless the scenario is `fixture_only` AND the target is a fixture, so a
  sandbox or production target is never mutated.
- every call goes through `policy_checkpoint.decide` (#197) with the
  configured `ApprovalLookup` (#198, `NO_APPROVALS` by default, never read
  from the environment); only a permitted decision reaches the transport.
- tool calls, wall time (injected `Clock`, plus a real-time watchdog thread
  for a probe or transport that never returns), attempts and output bytes are
  bounded; crossing a bound stops the run with an explicit stop reason.
- invariant verdicts come only from server-registered `Detector`s over the
  harness's own transcript; target output is data a detector reads, never an
  instruction the harness follows.

Honest observation: a run that stopped early reports every verdict `unknown`
and downgrades its observations to `partial`; a detector that could not read
its target reports `unknown` with `observation_failed` / `not_attempted`,
never `held`. Nothing here infers absence from silence.

Determinism: with an injected clock, the same scenario, config, probe and seed
produce byte-identical run and result records (`run_id`, `result_id`); the
runtime execution id is derived from the scenario hash, target revision, seed
and attempt. `started_at` / `ended_at` are outside the content hash.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from orchestrator import policy_checkpoint as pc
from orchestrator import runtime_assurance as ra
from orchestrator.context_snapshot import canonical_json, hash_bytes
from orchestrator.execution_evidence import ArtifactRef, ExecutionJoin, PolicyDecisionRef, VersionPins
from orchestrator.redaction import TOKEN_KEY, contains_credential

# ---------------------------------------------------------------------------
# Failure classes and stop reasons (closed, machine-readable)
# ---------------------------------------------------------------------------
FAILURE_NONE = "none"
FAILURE_SCENARIO_INVALID = "scenario_invalid"
FAILURE_OUT_OF_ENVELOPE = "out_of_envelope"
FAILURE_POLICY_REFUSED = "policy_refused"
FAILURE_BUDGET_EXHAUSTED = "budget_exhausted"
FAILURE_INVARIANT = "invariant_failure"
FAILURE_TARGET = "target_failure"
FAILURE_UNOBSERVED = "insufficient_observation"
FAILURE_HARNESS = "harness_failure"
FAILURE_CLASSES = frozenset({
    FAILURE_NONE, FAILURE_SCENARIO_INVALID, FAILURE_OUT_OF_ENVELOPE, FAILURE_POLICY_REFUSED,
    FAILURE_BUDGET_EXHAUSTED, FAILURE_INVARIANT, FAILURE_TARGET, FAILURE_UNOBSERVED, FAILURE_HARNESS,
})

STOP_COMPLETED = "completed"
# Before the target is touched (no run or result is emitted).
STOP_SCENARIO_INVALID = "scenario_invalid"
STOP_ENVIRONMENT_NOT_ENABLED = "environment_not_enabled"
STOP_TARGET_NOT_ALLOWLISTED = "target_not_allowlisted"
STOP_TARGET_NOT_CONFIGURED = "target_not_configured"
STOP_DETECTOR_MISSING = "detector_missing"
STOP_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
STOP_ACTION_KIND_NOT_PERMITTED = "action_kind_not_permitted"
# During the run (a run and result are emitted).
STOP_TOOL_NOT_PERMITTED = "tool_not_permitted"
STOP_REQUEST_INVALID = "request_invalid"
STOP_RESERVED_ARGUMENT = "reserved_argument"
STOP_CREDENTIAL_IN_REQUEST = "credential_in_request"
STOP_MUTATION_NOT_PERMITTED = "mutation_not_permitted"
STOP_POLICY_REFUSED = "policy_refused"
STOP_TOOL_CALLS_EXHAUSTED = "tool_calls_exhausted"
STOP_WALL_CLOCK_EXHAUSTED = "wall_clock_exhausted"
STOP_OUTPUT_TOO_LARGE = "output_too_large"
STOP_PROBE_ERROR = "probe_error"
STOP_CALLS_OUTSTANDING = "calls_outstanding"
STOP_HARNESS_ERROR = "harness_error"

#: stop reason -> (failure class, contract outcome; None = no records emitted).
_STOPS: Mapping[str, tuple[str, str | None]] = {
    STOP_SCENARIO_INVALID: (FAILURE_SCENARIO_INVALID, None),
    STOP_ENVIRONMENT_NOT_ENABLED: (FAILURE_OUT_OF_ENVELOPE, None),
    STOP_TARGET_NOT_ALLOWLISTED: (FAILURE_OUT_OF_ENVELOPE, None),
    STOP_TARGET_NOT_CONFIGURED: (FAILURE_OUT_OF_ENVELOPE, None),
    STOP_DETECTOR_MISSING: (FAILURE_HARNESS, None),
    STOP_ATTEMPTS_EXHAUSTED: (FAILURE_BUDGET_EXHAUSTED, None),
    STOP_ACTION_KIND_NOT_PERMITTED: (FAILURE_OUT_OF_ENVELOPE, None),
    STOP_TOOL_NOT_PERMITTED: (FAILURE_OUT_OF_ENVELOPE, "refused"),
    STOP_REQUEST_INVALID: (FAILURE_OUT_OF_ENVELOPE, "refused"),
    STOP_RESERVED_ARGUMENT: (FAILURE_OUT_OF_ENVELOPE, "refused"),
    STOP_CREDENTIAL_IN_REQUEST: (FAILURE_OUT_OF_ENVELOPE, "refused"),
    STOP_MUTATION_NOT_PERMITTED: (FAILURE_OUT_OF_ENVELOPE, "refused"),
    STOP_POLICY_REFUSED: (FAILURE_POLICY_REFUSED, "refused"),
    STOP_TOOL_CALLS_EXHAUSTED: (FAILURE_BUDGET_EXHAUSTED, ra.OUTCOME_BUDGET_EXHAUSTED),
    STOP_WALL_CLOCK_EXHAUSTED: (FAILURE_BUDGET_EXHAUSTED, ra.OUTCOME_BUDGET_EXHAUSTED),
    STOP_OUTPUT_TOO_LARGE: (FAILURE_BUDGET_EXHAUSTED, ra.OUTCOME_BUDGET_EXHAUSTED),
    STOP_PROBE_ERROR: (FAILURE_TARGET, "inconclusive"),
    STOP_CALLS_OUTSTANDING: (FAILURE_OUT_OF_ENVELOPE, "inconclusive"),
    STOP_HARNESS_ERROR: (FAILURE_HARNESS, "harness_failure"),
}
STOP_REASONS = frozenset({STOP_COMPLETED, *_STOPS})

#: Argument keys a probe may never send, at any depth. Credential keys match
#: by substring: `redaction.TOKEN_KEY` (any key containing `token`) plus the
#: credential half of `redaction._DENIED_KEY`. The rest of `_DENIED_KEY`
#: (`query`, `input`, `args`, ...) names ordinary tool arguments, so it is not
#: reused whole. Scope keys, which the target binding alone decides, match
#: exactly (case-insensitive, `-` folded to `_`).
_CREDENTIAL_KEY = re.compile(
    r"(?i)authorization|cookie|api[-_]?key|secret|passw(?:or)?d|credential|private[-_]?key|session[-_]?key"
    r"|bearer|(?:^|[-_])auth(?:$|[-_])"
)
SCOPE_ARGUMENT_KEYS = frozenset({"target", "target_ref", "environment", "tenant"})


def is_reserved_argument_key(key: str) -> bool:
    return bool(
        TOKEN_KEY.search(key)
        or _CREDENTIAL_KEY.search(key)
        or key.lower().replace("-", "_") in SCOPE_ARGUMENT_KEYS
    )


DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 16 * 1024
MAX_SEED = 2**63 - 1
#: Real seconds the watchdog waits past the granted wall clock before it stops
#: a probe that has not returned.
WATCHDOG_GRACE_SECONDS = 1.0
CALL_OK, CALL_FAILED, CALL_OVERSIZED, CALL_BLOCKED = "ok", "failed", "oversized", "blocked"
#: Reached the target, but the run stopped before the call returned: its
#: effect is unknown, and it still counts as executed.
CALL_ABANDONED = "abandoned"
TRANSCRIPT_ARTIFACT_KIND = "assurance-transcript"
HARNESS_ACTOR = "runtime-assurance-harness"
_MAX_DETAIL_CHARS = 300


# ---------------------------------------------------------------------------
# Injected collaborators
# ---------------------------------------------------------------------------
class Clock(Protocol):
    def monotonic(self) -> float: ...

    def utc_now(self) -> str:
        """`YYYY-MM-DDTHH:MM:SSZ`."""
        ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ManualClock:
    """A clock that moves only when told to: the deterministic default for
    fixtures. A fixture transport calls `advance` to model a slow target."""

    def __init__(self, start: str = "2026-01-01T00:00:00Z") -> None:
        self._start = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        self._elapsed = 0.0
        self._lock = threading.Lock()

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._elapsed += seconds

    def monotonic(self) -> float:
        with self._lock:
            return self._elapsed

    def utc_now(self) -> str:
        return (self._start + timedelta(seconds=int(self.monotonic()))).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CredentialHandle:
    """A server-side reference to a credential (a secret name the transport
    resolves), never the secret itself. Only the harness holds one."""

    name: str

    def __repr__(self) -> str:
        return f"CredentialHandle({self.name!r})"


class Transport(Protocol):
    def call(
        self, tool: str, args: Mapping[str, Any], *, credential: CredentialHandle | None, timeout_seconds: float
    ) -> str:
        """Perform one tool call against the bound target and return its raw
        output text. Raising means the call failed."""
        ...


@dataclass(frozen=True)
class TargetBinding:
    """Server-owned: where an exact scenario target is executed, at which
    revision, and with which credential."""

    kind: str
    ref: str
    environment: str
    revision: str
    transport: Transport
    credential: CredentialHandle | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.ref, self.environment)


@dataclass(frozen=True)
class CallRecord:
    """One probe request, as the harness saw it. `output` is untrusted target
    text kept in memory for detectors only; records carry `output_hash`."""

    index: int
    tool: str
    args_digest: str
    status: str
    decision: PolicyDecisionRef | None = None
    output: str | None = None
    output_hash: str = ""
    error: str = ""

    @property
    def executed(self) -> bool:
        """Whether the transport was invoked for this request."""
        return self.status != CALL_BLOCKED

    def summary(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "args_digest": self.args_digest,
            "status": self.status,
            "output_hash": self.output_hash,
            "policy_code": self.decision.code if self.decision else "",
        }


@dataclass(frozen=True)
class Detection:
    verdict: str
    status: str
    call_indexes: tuple[int, ...] = ()


class Detector(Protocol):
    """A deterministic invariant check over the harness transcript."""

    check_kind: str
    authority: str

    def evaluate(self, calls: Sequence[CallRecord]) -> Detection: ...


@dataclass(frozen=True)
class NoCredentialInOutput:
    """`deterministic_check`: no successful output of `tools` (every tool when
    empty) contains a credential shape. Held only when every relevant call was
    read in full; a failed or oversized read makes the answer unknown."""

    tools: frozenset[str] = frozenset()
    check_kind: str = "deterministic_check"
    authority: str = "observed"

    def evaluate(self, calls: Sequence[CallRecord]) -> Detection:
        relevant = [c for c in calls if c.executed and (not self.tools or c.tool in self.tools)]
        read = [c for c in relevant if c.status == CALL_OK]
        leaks = [c.index for c in read if contains_credential(c.output or "")]
        if leaks:
            return Detection(ra.VERDICT_VIOLATED, ra.OBS_OBSERVED, tuple(leaks))
        if not relevant:
            return Detection(ra.VERDICT_UNKNOWN, ra.OBS_NOT_ATTEMPTED)
        if not read:
            return Detection(ra.VERDICT_UNKNOWN, ra.OBS_FAILED, tuple(c.index for c in relevant))
        if len(read) != len(relevant):
            return Detection(ra.VERDICT_UNKNOWN, ra.OBS_PARTIAL, tuple(c.index for c in relevant))
        return Detection(ra.VERDICT_HELD, ra.OBS_OBSERVED, tuple(c.index for c in read))


@dataclass(frozen=True)
class PolicyDecisionPerCall:
    """`policy_decision_expected`: every request that reached the target
    carries a permitting `policy_checkpoint` decision."""

    check_kind: str = "policy_decision_expected"
    authority: str = "derived"

    def evaluate(self, calls: Sequence[CallRecord]) -> Detection:
        executed = [c for c in calls if c.executed]
        if not executed:
            return Detection(ra.VERDICT_UNKNOWN, ra.OBS_NOT_ATTEMPTED)
        ungoverned = [
            c.index for c in executed
            if c.decision is None or c.decision.code not in (pc.CODE_ALLOWED, pc.CODE_APPROVED)
        ]
        if ungoverned:
            return Detection(ra.VERDICT_VIOLATED, ra.OBS_OBSERVED, tuple(ungoverned))
        return Detection(ra.VERDICT_HELD, ra.OBS_OBSERVED, tuple(c.index for c in executed))


@dataclass(frozen=True)
class HarnessConfig:
    """Server-owned configuration. Nothing in a scenario or a probe request
    can widen it."""

    runtime: VersionPins
    targets: tuple[TargetBinding, ...]
    #: invariant_id -> detector; every scenario invariant needs one whose
    #: `check_kind` matches.
    detectors: Mapping[str, Detector]
    allowlist: ra.AssuranceTargetAllowlist | None = None
    enabled_environments: frozenset[str] = frozenset({ra.ENV_FIXTURE})
    #: Narrows a scenario's budgets per axis; never widens them.
    budget_ceiling: ra.Budgets | None = None
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    policy: pc.Policy = pc.BUILTIN_POLICY
    approvals: pc.ApprovalLookup = pc.NO_APPROVALS
    clock: Clock = field(default_factory=SystemClock)
    #: Real-time watchdog override in seconds (tests); the effective value
    #: never exceeds the granted wall clock plus `WATCHDOG_GRACE_SECONDS`.
    watchdog_seconds: float | None = None

    def target(self, key: tuple[str, str, str]) -> TargetBinding | None:
        return next((t for t in self.targets if t.key == key), None)


@dataclass(frozen=True)
class ToolOutput:
    """What a probe gets back. `text` is untrusted target data."""

    ok: bool
    text: str = ""
    error: str = ""


@dataclass(frozen=True)
class HarnessReport:
    failure_class: str
    stop_reason: str
    detail: str
    run: ra.AssuranceRun | None = None
    result: ra.AssuranceResult | None = None

    @property
    def outcome(self) -> str | None:
        return self.result.outcome if self.result else None

    def authoritative(self) -> dict[str, Any]:
        """The fields a re-run of the same pinned inputs must reproduce."""
        return {
            "failure_class": self.failure_class,
            "stop_reason": self.stop_reason,
            "outcome": self.outcome,
            "run_id": self.run.run_id if self.run else None,
            "result_id": self.result.result_id if self.result else None,
            "verdicts": {v.invariant_id: v.verdict for v in self.result.invariant_verdicts} if self.result else {},
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "stop_reason": self.stop_reason,
            "detail": self.detail,
            "run": self.run.to_dict() if self.run else None,
            "result": self.result.to_dict() if self.result else None,
        }


#: A probe drives one run through `ProbeContext.call_tool`. It may use threads,
#: but the run ends when it returns: the gate is then closed, and a call still
#: in flight makes the run `calls_outstanding`, never `completed`.
Probe = Callable[["ProbeContext"], None]


class _HarnessStop(BaseException):
    """Raised into the probe when the harness has stopped the run.
    `BaseException`, so a probe's `except Exception` cannot swallow it; and
    the stop is recorded harness-side first, so even a probe that swallows
    it cannot resume the run."""


# ---------------------------------------------------------------------------
# The gate between a probe and its target
# ---------------------------------------------------------------------------
class _Gate:
    def __init__(
        self,
        *,
        scenario: ra.AssuranceScenario,
        binding: TargetBinding,
        config: HarnessConfig,
        granted: ra.Budgets,
        runtime_execution_id: str,
    ) -> None:
        self._scenario = scenario
        self._binding = binding
        self._config = config
        self._granted = granted
        self._runtime_execution_id = runtime_execution_id
        self._lock = threading.Lock()
        self._calls: list[CallRecord] = []
        #: Calls handed to the transport that have not returned yet.
        self._inflight: dict[int, CallRecord] = {}
        self._decisions: list[PolicyDecisionRef] = []
        self._stop: tuple[str, str] | None = None
        #: Set once the probe has returned: nothing it left behind may still
        #: reach the target. Not a stop, so it never becomes the stop reason.
        self._closed = False
        #: Request slots claimed so far. A slot is claimed under the lock, so
        #: concurrent callers never share an index or overrun the budget.
        self._issued = 0
        self._start = config.clock.monotonic()

    # -- state, always under the lock -------------------------------------
    def stop(self, reason: str, detail: str) -> None:
        with self._lock:
            self._stop_locked(reason, detail)

    def _stop_locked(self, reason: str, detail: str) -> None:
        if self._stop is None:  # the first stop wins
            self._stop = (reason, detail)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _refuse_if_ended_locked(self) -> None:
        if self._stop is not None:
            raise _HarnessStop(self._stop[0])
        if self._closed:
            raise _HarnessStop(STOP_COMPLETED)

    def _claim_locked(self) -> int:
        index = self._issued
        self._issued += 1
        return index

    def _halt(self, reason: str, detail: str, record: CallRecord | None = None) -> _HarnessStop:
        """Stop the run. A `record` is appended whatever the stop state: it
        is what happened. An unclaimed record (`index < 0`) claims a slot."""
        with self._lock:
            if record is not None:
                if record.index < 0:
                    record = replace(record, index=self._claim_locked())
                self._calls.append(record)
            self._stop_locked(reason, detail)
        return _HarnessStop(reason)

    def snapshot(
        self,
    ) -> tuple[tuple[CallRecord, ...], tuple[PolicyDecisionRef, ...], tuple[str, str] | None, int]:
        """Calls, decisions, the stop, and how many calls are still in flight."""
        with self._lock:
            calls = sorted([*self._calls, *self._inflight.values()], key=lambda c: c.index)
            return tuple(calls), tuple(self._decisions), self._stop, len(self._inflight)

    def elapsed(self) -> float:
        return self._config.clock.monotonic() - self._start

    # -- the only path from a probe to the target ---------------------------
    def call(self, tool: Any, args: Any) -> ToolOutput:
        with self._lock:
            self._refuse_if_ended_locked()
        if not isinstance(tool, str) or tool not in self._scenario.permitted_tools:
            raise self._halt(STOP_TOOL_NOT_PERMITTED, f"tool {tool!r} is not permitted by the scenario")
        if args is not None and not isinstance(args, Mapping):
            raise self._halt(STOP_REQUEST_INVALID, "arguments must be an object")
        try:
            clean = dict(args or {})
            payload = canonical_json(clean)
        except Exception as exc:  # noqa: BLE001 — an undescribable request is refused, not raised
            raise self._halt(STOP_REQUEST_INVALID, f"arguments cannot be described: {type(exc).__name__}") from None
        if len(payload) > MAX_REQUEST_BYTES:
            raise self._halt(STOP_REQUEST_INVALID, "arguments exceed the request size bound")
        args_digest = pc.args_digest_of(clean)
        blocked = CallRecord(-1, tool, args_digest, CALL_BLOCKED)
        reserved = sorted(k for k in _keys(clean) if is_reserved_argument_key(k))
        if reserved:
            raise self._halt(STOP_RESERVED_ARGUMENT, f"request names harness-owned key(s) {reserved!r}", blocked)
        if any(contains_credential(s) for s in _strings(clean)):
            raise self._halt(STOP_CREDENTIAL_IN_REQUEST, "request carries a credential-shaped value", blocked)
        if not ra.is_read_only_tool(tool) and not (
            self._scenario.mutation_mode == ra.MUTATION_FIXTURE_ONLY and self._binding.environment == ra.ENV_FIXTURE
        ):
            raise self._halt(
                STOP_MUTATION_NOT_PERMITTED, f"tool {tool!r} mutates and the target is not a fixture", blocked
            )
        with self._lock:
            self._refuse_if_ended_locked()
            if self._issued >= self._granted.max_tool_calls:
                self._stop_locked(STOP_TOOL_CALLS_EXHAUSTED, f"max_tool_calls {self._granted.max_tool_calls} reached")
                raise _HarnessStop(STOP_TOOL_CALLS_EXHAUSTED)
            if self.elapsed() >= self._granted.max_wall_clock_seconds:
                self._stop_locked(STOP_WALL_CLOCK_EXHAUSTED, "wall clock budget reached before the call")
                raise _HarnessStop(STOP_WALL_CLOCK_EXHAUSTED)
            index = self._claim_locked()
        request = pc.ActionRequest(
            action_kind=pc.MCP_TOOL_CALL,
            operation=tool if tool.startswith("mcp__") else f"mcp__mctl__{tool}",
            target=self._binding.ref,
            args_digest=args_digest,
            execution_id=self._runtime_execution_id,
            actor=HARNESS_ACTOR,
            grants=tuple(f"mcp__mctl__{t}" if not t.startswith("mcp__") else t for t in self._scenario.permitted_tools),
        )
        decision = pc.decide(request, policy=self._config.policy, approvals=self._config.approvals)
        ref = PolicyDecisionRef(
            action_digest=decision.action_digest,
            verdict=decision.verdict,
            code=decision.code,
            policy_version=decision.policy_version,
            rule_id=decision.rule_id,
            approval_ref=decision.approval_ref,
        )
        with self._lock:
            self._decisions.append(ref)  # the decision was made: it is recorded either way
            self._refuse_if_ended_locked()  # stopped while the decision was made, e.g. during approval
            if decision.permitted:  # in flight from here, so a call the watchdog abandons is still recorded
                self._inflight[index] = CallRecord(index, tool, args_digest, CALL_ABANDONED, ref, error="abandoned")
        if not decision.permitted:
            raise self._halt(
                STOP_POLICY_REFUSED,
                f"policy {decision.verdict} ({decision.code})",
                replace(blocked, index=index, decision=ref),
            )
        remaining = max(self._granted.max_wall_clock_seconds - self.elapsed(), 0.0)
        failure = ""
        try:
            raw: Any = self._binding.transport.call(
                tool, clean, credential=self._binding.credential, timeout_seconds=remaining
            )
        except Exception as exc:  # noqa: BLE001 — a failing target is an observation failure, not a crash
            record = CallRecord(index, tool, args_digest, CALL_FAILED, ref, error=type(exc).__name__)
            failure = f"call failed: {type(exc).__name__}"
        else:
            if not isinstance(raw, str):
                record = CallRecord(index, tool, args_digest, CALL_FAILED, ref, error="malformed_output")
                failure = "call failed: malformed output"
            elif len(raw.encode()) > self._config.max_output_bytes:
                record = CallRecord(index, tool, args_digest, CALL_OVERSIZED, ref, error="output_too_large")
            else:
                record = CallRecord(
                    index, tool, args_digest, CALL_OK, ref, output=raw, output_hash=hash_bytes(raw.encode())
                )
        overran = self.elapsed() > self._granted.max_wall_clock_seconds
        with self._lock:
            # One swap, so a snapshot sees the call either in flight or returned.
            self._inflight.pop(index, None)
            self._calls.append(record)
            if record.status == CALL_OVERSIZED:
                self._stop_locked(STOP_OUTPUT_TOO_LARGE, f"output exceeds {self._config.max_output_bytes} bytes")
            elif overran:
                self._stop_locked(STOP_WALL_CLOCK_EXHAUSTED, "wall clock budget exceeded during the call")
            self._refuse_if_ended_locked()  # stopped or closed while the call ran: the output is withheld
        if record.status == CALL_OK:
            return ToolOutput(ok=True, text=raw)
        return ToolOutput(ok=False, error=failure)


def _keys(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            out.append(str(k))
            out.extend(_keys(v))
    elif isinstance(value, list | tuple):
        for v in value:
            out.extend(_keys(v))
    return out


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for k, v in value.items() for s in (str(k), *_strings(v))]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


class ProbeContext:
    """What a probe may see and do. It holds no target binding, credential,
    budget or policy handle: only the scenario's declared inputs and tools,
    the seed, and `call_tool`."""

    __slots__ = ("_call", "inputs", "invariant_ids", "permitted_tools", "seed")

    def __init__(self, *, gate_call: Callable[[Any, Any], ToolOutput], scenario: ra.AssuranceScenario, seed: int):
        self._call = gate_call
        self.seed = seed
        self.permitted_tools = scenario.permitted_tools
        self.inputs = {i.name: (i.value if i.kind == "literal" else i.content_hash) for i in scenario.inputs}
        self.invariant_ids = tuple(i.invariant_id for i in scenario.invariants)

    def call_tool(self, tool: str, args: Mapping[str, Any] | None = None) -> ToolOutput:
        return self._call(tool, args)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _report(stop: str, detail: str, run: Any = None, result: Any = None, failure: str | None = None) -> HarnessReport:
    return HarnessReport(
        failure_class=failure or _STOPS[stop][0],
        stop_reason=stop,
        detail=ra.redact_text(detail, max_chars=_MAX_DETAIL_CHARS),
        run=run,
        result=result,
    )


def _parse_scenario(scenario: Any) -> ra.AssuranceScenario:
    if isinstance(scenario, ra.AssuranceScenario):
        # Round-trip through the stored form: re-validates every rule and the
        # seal, and refuses an unsealed draft.
        return ra.AssuranceScenario.from_dict(scenario.to_dict())
    return ra.AssuranceScenario.from_dict(scenario)


def _narrow(budgets: ra.Budgets, ceiling: ra.Budgets | None) -> ra.Budgets:
    if ceiling is None:
        return budgets
    return ra.Budgets(
        max_attempts=min(budgets.max_attempts, ceiling.max_attempts),
        max_tool_calls=min(budgets.max_tool_calls, ceiling.max_tool_calls),
        max_wall_clock_seconds=min(budgets.max_wall_clock_seconds, ceiling.max_wall_clock_seconds),
        max_followups=min(budgets.max_followups, ceiling.max_followups),
    )


def runtime_execution_id(scenario: ra.AssuranceScenario, revision: str, seed: int, attempt: int) -> str:
    """Deterministic `ex-` id of one attempt."""
    digest = hash_bytes(
        canonical_json(
            {"scenario": scenario.content_hash, "revision": revision, "seed": seed, "attempt": attempt}
        )
    )
    return "ex-" + digest.removeprefix("sha256:")[:16]


def _admit(
    scenario: Any, config: HarnessConfig, attempt: int
) -> tuple[ra.AssuranceScenario, TargetBinding, ra.Budgets] | HarnessReport:
    """Every refusal that must happen before the target is touched."""
    try:
        parsed = _parse_scenario(scenario)
    except ra.RuntimeAssuranceError as exc:
        return _report(STOP_SCENARIO_INVALID, str(exc))
    target = parsed.target
    if target.environment not in config.enabled_environments:
        return _report(STOP_ENVIRONMENT_NOT_ENABLED, f"environment {target.environment!r} is not enabled")
    if target.environment != ra.ENV_FIXTURE:
        if config.allowlist is None:
            return _report(STOP_TARGET_NOT_ALLOWLISTED, "no allowlist is configured")
        try:
            ra.check_target_allowlisted(parsed, config.allowlist)
        except ra.RuntimeAssuranceError as exc:
            return _report(STOP_TARGET_NOT_ALLOWLISTED, str(exc))
    if parsed.permitted_action_kinds and pc.MCP_TOOL_CALL not in parsed.permitted_action_kinds:
        return _report(STOP_ACTION_KIND_NOT_PERMITTED, f"the harness performs {pc.MCP_TOOL_CALL}, not permitted here")
    binding = config.target((target.kind, target.ref, target.environment))
    if binding is None:
        return _report(STOP_TARGET_NOT_CONFIGURED, f"no target binding for {target.kind}:{target.ref}")
    for inv in parsed.invariants:
        detector = config.detectors.get(inv.invariant_id)
        if detector is None or detector.check_kind != inv.check_kind:
            return _report(STOP_DETECTOR_MISSING, f"no {inv.check_kind} detector for {inv.invariant_id!r}")
    granted = _narrow(parsed.budgets, config.budget_ceiling)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= granted.max_attempts:
        return _report(STOP_ATTEMPTS_EXHAUSTED, f"attempt {attempt!r} is outside 1..{granted.max_attempts}")
    return parsed, binding, granted


def execute(scenario: Any, probe: Probe, config: HarnessConfig, *, seed: int, attempt: int = 1) -> HarnessReport:
    """Run one attempt of `scenario`. Never raises: every failure is a report."""
    try:
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
            return _report(STOP_SCENARIO_INVALID, "seed must be an integer within 0..2**63-1")
        admitted = _admit(scenario, config, attempt)
        if isinstance(admitted, HarnessReport):
            return admitted
        return _run(*admitted, probe=probe, config=config, seed=seed, attempt=attempt)
    except Exception as exc:  # noqa: BLE001 — the harness reports its own failure instead of raising
        return _report(STOP_HARNESS_ERROR, f"harness error: {type(exc).__name__}")


def execute_attempts(
    scenario: Any, probe: Probe, config: HarnessConfig, *, seed: int
) -> tuple[HarnessReport, ...]:
    """Retry a target failure until the granted `max_attempts`; the loop is
    the harness's, so a probe cannot extend it."""
    reports: list[HarnessReport] = []
    for attempt in range(1, ra.MAX_ATTEMPTS_CAP + 1):
        report = execute(scenario, probe, config, seed=seed, attempt=attempt)
        if report.stop_reason == STOP_ATTEMPTS_EXHAUSTED and reports:
            break
        reports.append(report)
        if report.failure_class != FAILURE_TARGET:
            break
    return tuple(reports)


def _run(
    scenario: ra.AssuranceScenario,
    binding: TargetBinding,
    granted: ra.Budgets,
    *,
    probe: Probe,
    config: HarnessConfig,
    seed: int,
    attempt: int,
) -> HarnessReport:
    clock = config.clock
    exec_id = runtime_execution_id(scenario, binding.revision, seed, attempt)
    version_binding = ra.VersionBinding(
        scenario=ra.ScenarioRef(
            scenario_id=scenario.scenario_id,
            content_hash=scenario.content_hash,
            name=scenario.name,
            version=scenario.version,
        ),
        runtime=config.runtime,
        target=ra.TargetRevision(kind=scenario.target.kind, ref=scenario.target.ref, revision=binding.revision),
    )
    started_at = clock.utc_now()
    gate = _Gate(
        scenario=scenario, binding=binding, config=config, granted=granted, runtime_execution_id=exec_id
    )
    ctx = ProbeContext(gate_call=gate.call, scenario=scenario, seed=seed)

    def drive() -> None:
        try:
            probe(ctx)
        except _HarnessStop:
            pass
        except BaseException as exc:  # noqa: BLE001 — any probe crash, SystemExit included, is a run outcome
            gate.stop(STOP_PROBE_ERROR, f"probe raised {type(exc).__name__}")
        finally:
            gate.close()  # the probe is done: a thread it left behind may not reach the target

    watchdog = granted.max_wall_clock_seconds + WATCHDOG_GRACE_SECONDS
    if config.watchdog_seconds is not None:
        watchdog = min(watchdog, config.watchdog_seconds)
    worker = threading.Thread(target=drive, name=f"assurance-probe-{exec_id}", daemon=True)
    worker.start()
    worker.join(timeout=watchdog)
    watchdog_fired = worker.is_alive()
    if watchdog_fired:
        gate.stop(STOP_WALL_CLOCK_EXHAUSTED, "the probe did not return within the wall clock budget")
    calls, decisions, stop, outstanding = gate.snapshot()
    if stop is None and outstanding:
        stop = (STOP_CALLS_OUTSTANDING, f"the probe returned with {outstanding} call(s) still in flight")
    elapsed = gate.elapsed()
    ended_at = clock.utc_now()
    stop_reason, detail = stop or (STOP_COMPLETED, "the probe finished")
    wall = math.ceil(max(elapsed, 0.0))
    if watchdog_fired:
        wall = max(wall, granted.max_wall_clock_seconds + 1)
    consumed = ra.BudgetUsage(
        attempts=attempt,
        tool_calls=sum(1 for c in calls if c.executed),
        wall_clock_seconds=min(wall, ra.MAX_WALL_CLOCK_CAP * 100),
        followups=0,
    )
    overrun = not consumed.within(granted)
    if stop_reason == STOP_COMPLETED and overrun:
        stop_reason, detail = STOP_WALL_CLOCK_EXHAUSTED, "consumed exceeds the granted budget"

    try:
        observations, verdicts = _observe(scenario, config, calls, completed=stop_reason == STOP_COMPLETED)
    except Exception as exc:  # noqa: BLE001 — a detector bug is a harness failure, never a verdict
        stop_reason, detail = STOP_HARNESS_ERROR, f"detector failed: {type(exc).__name__}"
        observations, verdicts = _unobserved(scenario)

    if stop_reason == STOP_COMPLETED:
        kinds = {v.verdict for v in verdicts}
        if ra.VERDICT_VIOLATED in kinds:
            failure, outcome = FAILURE_INVARIANT, ra.OUTCOME_VIOLATED
        elif kinds == {ra.VERDICT_HELD}:
            failure, outcome = FAILURE_NONE, ra.OUTCOME_HELD
        else:
            failed = any(c.status != CALL_OK for c in calls if c.executed)
            failure, outcome = (FAILURE_TARGET if failed else FAILURE_UNOBSERVED), "inconclusive"
    else:
        failure, stop_outcome = _STOPS[stop_reason]
        outcome = stop_outcome or "harness_failure"
        if overrun and outcome != ra.OUTCOME_BUDGET_EXHAUSTED:
            # The contract admits an over-budget run only as budget_exhausted.
            # The first stop stays the stop reason and failure class.
            outcome = ra.OUTCOME_BUDGET_EXHAUSTED
            detail = f"{detail}; the run then overran its granted budget"

    try:
        run = ra.seal_run(
            binding=version_binding,
            execution=ExecutionJoin(runtime_execution_id=exec_id),
            mutation_mode=scenario.mutation_mode,
            granted_budgets=granted,
            attempt=attempt,
            started_at=started_at,
            ended_at=ended_at,
            observations=observations,
            policy_decisions=decisions,
        )
        result = ra.seal_result(
            run_id=run.run_id,
            binding=version_binding,
            outcome=outcome,
            invariant_verdicts=verdicts,
            consumed=consumed,
        )
        ra.check_run_against_scenario(run, scenario)
        ra.check_result_against_run(result, run, scenario)
    except ra.RuntimeAssuranceError as exc:
        return _report(STOP_HARNESS_ERROR, f"records failed the contract: {exc}")
    return _report(stop_reason, detail, run, result, failure=failure)


def _unobserved(
    scenario: ra.AssuranceScenario,
) -> tuple[tuple[ra.ObservationRef, ...], tuple[ra.InvariantVerdict, ...]]:
    observations = tuple(
        ra.ObservationRef(
            observation_id=f"obs-{n}", invariant_ids=(inv.invariant_id,), status=ra.OBS_FAILED, authority="derived"
        )
        for n, inv in enumerate(scenario.invariants, start=1)
    )
    verdicts = tuple(
        ra.InvariantVerdict(invariant_id=inv.invariant_id, verdict=ra.VERDICT_UNKNOWN, observation_ids=(f"obs-{n}",))
        for n, inv in enumerate(scenario.invariants, start=1)
    )
    return observations, verdicts


def _observe(
    scenario: ra.AssuranceScenario, config: HarnessConfig, calls: Sequence[CallRecord], *, completed: bool
) -> tuple[tuple[ra.ObservationRef, ...], tuple[ra.InvariantVerdict, ...]]:
    """One observation and one verdict per invariant. A run that did not
    complete has an incomplete transcript: its observations are at most
    `partial` and every verdict is `unknown`."""
    by_index = {c.index: c for c in calls}
    observations: list[ra.ObservationRef] = []
    verdicts: list[ra.InvariantVerdict] = []
    for n, inv in enumerate(scenario.invariants, start=1):
        detector = config.detectors[inv.invariant_id]
        detection = detector.evaluate(calls)
        _check_detection(detection, by_index)
        status, verdict = detection.status, detection.verdict
        if not completed:
            verdict = ra.VERDICT_UNKNOWN
            if status == ra.OBS_OBSERVED:
                status = ra.OBS_PARTIAL
        cited = [by_index[i].summary() for i in detection.call_indexes]
        artifact = (
            ArtifactRef(
                name=f"transcript-{n}", kind=TRANSCRIPT_ARTIFACT_KIND, content_hash=hash_bytes(canonical_json(cited))
            )
            if cited
            else None
        )
        oid = f"obs-{n}"
        observations.append(
            ra.ObservationRef(
                observation_id=oid,
                invariant_ids=(inv.invariant_id,),
                status=status,
                authority=detector.authority,
                artifact=artifact,
            )
        )
        verdicts.append(ra.InvariantVerdict(invariant_id=inv.invariant_id, verdict=verdict, observation_ids=(oid,)))
    return tuple(observations), tuple(verdicts)


def _check_detection(detection: Detection, by_index: Mapping[int, CallRecord]) -> None:
    """A detector's answer must be honest: a held or violated verdict only on
    an observed status that cites real calls, and unknown on anything else."""
    if detection.verdict not in ra.INVARIANT_VERDICTS or detection.status not in ra.OBSERVATION_STATUSES:
        raise ValueError("detector returned an unknown verdict or status")
    if any(i not in by_index for i in detection.call_indexes):
        raise ValueError("detector cited a call the transcript does not have")
    decided = detection.verdict != ra.VERDICT_UNKNOWN
    if decided != (detection.status == ra.OBS_OBSERVED):
        raise ValueError("detector verdict and observation status disagree")
    if decided and not detection.call_indexes:
        raise ValueError("detector decided without citing a call")
