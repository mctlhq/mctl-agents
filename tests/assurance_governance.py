"""Runtime-assurance regression scenarios for the shipped governance chain
(mctlhq/mctl-agents#731; contracts ADR 022, harness ADR 023).

Each scenario pins one guarantee that already shipped, as a sealed
`AssuranceScenario`, a scripted probe, an in-process fake target and a
server-side detector. They run through `assurance_harness.execute` in the
fixture environment only: no network, no mctl-api, no production.

- `governance-approval-digest`: an approve binds the reviewed content digest
  (mctl-gitops#1979, mctl-api#722).
- `governance-reinvestigate-supersedes`: reinvestigate supersedes a pending
  approval grant (mctlhq/mctl-agents#701).
- `governance-failed-approval`: a failed approval never projects success
  (mctlhq/mctl-agents#702).
- `governance-approval-receipt`: every governed call gets a policy decision,
  and an approval receipt is principal + tool + single use (mctl-api#723, v1).
- `governance-decision-records`: a decision or attempt record that did not
  land is never counted as recorded (mctl-api#701, producer contract).

The first four fakes are reference models of the governed surface: each has
one guard per guarantee, and a named `mutation` switches that guard off, so
the tests can show the scenario goes red when the guarantee regresses. The
decision-record fake drives the real `orchestrator.decision_records`
producer against a scripted sink; its mutation is a producer that reads any
answer as landed.

What is deliberately not claimed: receipts are not bound to exact arguments
(v1 semantics are principal + tool + single use), and `AssuranceRun.approvals`
stays empty (ADR 023), so receipts are checked from the target's answers.

Regenerate the sealed fixtures after a reviewed change with
`uv run --locked python -m tests.assurance_governance`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from orchestrator import assurance_harness as ah
from orchestrator import decision_records as dr
from orchestrator import policy_checkpoint as pc
from orchestrator import runtime_assurance as ra
from orchestrator.context_snapshot import canonical_json
from orchestrator.execution_evidence import VersionPins
from orchestrator.proposal_state import PROPOSAL_DIGEST_FILES, content_digest_of

FIXTURES = Path(__file__).parent / "fixtures" / "assurance" / "governance"
REVISION = "0123456789abcdef0123456789abcdef01234567"
PINS = VersionPins(
    agent="mctl-agents",
    environment="test",
    definition_version="1.88.0",
    definition_content_hash="sha256:" + "c" * 64,
)
#: The principal every fixture call is made as. The transport receives it
#: from the harness; the probe never sees it.
PRINCIPAL = ah.CredentialHandle("vault:assurance/governance-fixture")
PEER_PRINCIPAL = "vault:assurance/another-principal"

#: The fixture tools are mutations the builtin policy gates behind a human
#: approval (`mctl-mcp-default-approval`). Server-owned config allows exactly
#: the granted fixture tools here, so the run reaches the target and every
#: call still carries a real `policy_checkpoint` decision.
FIXTURE_POLICY = pc.Policy(
    "assurance-governance-fixture/v1",
    (pc.Rule("assurance-governance-fixture-tools", pc.MCP_TOOL_CALL, "mcp__mctl__mctl_*", pc.ALLOW,
             requires_grant=True),),
)

REFUSED = "refused"


def _answer(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def _refused(code: str) -> str:
    return _answer(status=REFUSED, code=code)


# ---------------------------------------------------------------------------
# The detector: expected answers to exact requests
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Expectation:
    """The answer the guarantee requires for one exact request. `occurrence`
    selects the n-th executed call with this tool and arguments (0-based);
    None means every such call."""

    label: str
    tool: str
    args: Mapping[str, Any]
    check: Callable[[Any], bool]
    occurrence: int | None = None

    @property
    def args_digest(self) -> str:
        # The digest the harness gate records: of the canonical-JSON copy.
        return pc.args_digest_of(json.loads(canonical_json(dict(self.args))))


@dataclass(frozen=True)
class ExpectedAnswers:
    """`deterministic_check`: every expected request was executed, read in
    full and answered as the guarantee requires. A missing or unread call is
    unknown, never held; an answer that is not JSON is not the required
    answer."""

    expectations: tuple[Expectation, ...]
    check_kind: str = "deterministic_check"
    authority: str = "observed"

    def evaluate(self, calls: Sequence[ah.CallRecord]) -> ah.Detection:
        cited: list[int] = []
        violated: list[int] = []
        incomplete = False
        for exp in self.expectations:
            matching = [c for c in calls if c.executed and c.tool == exp.tool and c.args_digest == exp.args_digest]
            if exp.occurrence is not None:
                matching = matching[exp.occurrence:exp.occurrence + 1]
            if not matching:
                incomplete = True
                continue
            for call in matching:
                cited.append(call.index)
                if call.status != ah.CALL_OK:
                    incomplete = True
                    continue
                try:
                    answer = json.loads(call.output or "")
                except ValueError:
                    violated.append(call.index)
                    continue
                if not exp.check(answer):
                    violated.append(call.index)
        cited_t = tuple(sorted(set(cited)))
        if violated:
            return ah.Detection(ra.VERDICT_VIOLATED, ra.OBS_OBSERVED, tuple(sorted(set(violated))))
        if not cited_t:
            return ah.Detection(ra.VERDICT_UNKNOWN, ra.OBS_NOT_ATTEMPTED)
        if incomplete:
            return ah.Detection(ra.VERDICT_UNKNOWN, ra.OBS_PARTIAL, cited_t)
        return ah.Detection(ra.VERDICT_HELD, ra.OBS_OBSERVED, cited_t)


def _status_is(*statuses: str) -> Callable[[Any], bool]:
    return lambda a: isinstance(a, dict) and a.get("status") in statuses


# ---------------------------------------------------------------------------
# Fake targets
# ---------------------------------------------------------------------------
class FixtureTarget:
    """A `Transport` that dispatches a tool to the method of the same name
    and answers JSON. `mutation` names the one guard switched off."""

    MUTATIONS: frozenset[str] = frozenset()

    def __init__(self, *, mutation: str | None = None) -> None:
        if mutation is not None and mutation not in self.MUTATIONS:
            raise ValueError(f"unknown mutation {mutation!r}")
        self.mutation = mutation
        self.calls: list[tuple[str, dict[str, Any], ah.CredentialHandle | None]] = []

    def call(self, tool: str, args: Mapping[str, Any], *, credential: ah.CredentialHandle | None,
             timeout_seconds: float) -> str:
        self.calls.append((tool, dict(args), credential))
        return getattr(self, tool)(dict(args), credential)


# -- mctl-gitops#1979 / mctl-api#722: an approve binds the reviewed digest ----
PROPOSAL = "proposal-checkout-1"
PROPOSAL_FILES: Mapping[str, bytes | None] = {
    "requirements.md": b"# Requirements\nScale checkout to three replicas.\n",
    "design.md": b"# Design\nRaise replicas in the service values.\n",
    "tasks.md": b"- [ ] update values\n",
    "proposed-content.md": None,
}
REVIEWED_DIGEST = content_digest_of(PROPOSAL_FILES)
STALE_DIGEST = content_digest_of({name: b"an earlier revision\n" for name in PROPOSAL_DIGEST_FILES})


class ApprovalDigestTarget(FixtureTarget):
    """A proposal approval surface. Guard: an approve is accepted only with
    the content digest of the reviewed files (`proposal_state.content_digest_of`)."""

    MUTATIONS = frozenset({"accept_any_digest"})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.state = "proposed"

    def mctl_get_proposal(self, args: dict[str, Any], _cred: Any) -> str:
        return _answer(proposal=PROPOSAL, state=self.state, content_digest=REVIEWED_DIGEST)

    def mctl_approve_proposal(self, args: dict[str, Any], _cred: Any) -> str:
        digest = args.get("content_digest")
        if self.mutation != "accept_any_digest":
            if not isinstance(digest, str) or not digest:
                return _refused("approval-digest-missing")
            if digest != REVIEWED_DIGEST:
                return _refused("approval-digest-mismatch")
        self.state = "accepted"
        return _answer(status="approved", proposal=PROPOSAL, content_digest=REVIEWED_DIGEST)


def probe_approval_digest(ctx: ah.ProbeContext) -> None:
    reviewed = json.loads(ctx.call_tool("mctl_get_proposal", {"proposal": PROPOSAL}).text)["content_digest"]
    ctx.call_tool("mctl_approve_proposal", {"proposal": PROPOSAL})
    ctx.call_tool("mctl_approve_proposal", {"proposal": PROPOSAL, "content_digest": STALE_DIGEST})
    ctx.call_tool("mctl_approve_proposal", {"proposal": PROPOSAL, "content_digest": reviewed})


APPROVAL_DIGEST_CHECK = ExpectedAnswers((
    Expectation("digest-less approve is refused", "mctl_approve_proposal", {"proposal": PROPOSAL},
                _status_is(REFUSED)),
    Expectation("mismatched approve is refused", "mctl_approve_proposal",
                {"proposal": PROPOSAL, "content_digest": STALE_DIGEST}, _status_is(REFUSED)),
    Expectation("the reviewed digest is accepted", "mctl_approve_proposal",
                {"proposal": PROPOSAL, "content_digest": REVIEWED_DIGEST},
                lambda a: _status_is("approved")(a) and a.get("content_digest") == REVIEWED_DIGEST),
))


# -- mctl-agents#701: reinvestigate supersedes a pending grant ---------------
WORK_ITEM = "wi_fixture_701"


class ReinvestigateTarget(FixtureTarget):
    """A grant surface. Guard: starting a reinvestigation supersedes every
    pending grant of the work item, and a superseded grant cannot be
    approved."""

    MUTATIONS = frozenset({"keep_pending_grant"})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.grants: dict[str, dict[str, str]] = {}

    def mctl_request_approval(self, args: dict[str, Any], _cred: Any) -> str:
        grant = f"grant-{len(self.grants) + 1}"
        self.grants[grant] = {"work_item": str(args.get("work_item")), "state": "pending"}
        return _answer(grant=grant, state="pending")

    def mctl_reinvestigate_issue(self, args: dict[str, Any], _cred: Any) -> str:
        superseded = []
        if self.mutation != "keep_pending_grant":
            for grant, g in self.grants.items():
                if g["work_item"] == args.get("work_item") and g["state"] == "pending":
                    g["state"] = "superseded"
                    superseded.append(grant)
        return _answer(status="reinvestigating", superseded=superseded)

    def mctl_approve_grant(self, args: dict[str, Any], _cred: Any) -> str:
        g = self.grants.get(str(args.get("grant")))
        if g is None:
            return _refused("grant-unknown")
        if g["state"] != "pending":
            return _refused(f"grant-{g['state']}")
        g["state"] = "approved"
        return _answer(status="approved", grant=args.get("grant"))

    def mctl_get_grant(self, args: dict[str, Any], _cred: Any) -> str:
        g = self.grants.get(str(args.get("grant")))
        return _answer(grant=args.get("grant"), state=g["state"] if g else "unknown")


def probe_reinvestigate(ctx: ah.ProbeContext) -> None:
    first = json.loads(ctx.call_tool("mctl_request_approval", {"work_item": WORK_ITEM}).text)["grant"]
    ctx.call_tool("mctl_reinvestigate_issue", {"work_item": WORK_ITEM})
    ctx.call_tool("mctl_approve_grant", {"grant": first})
    ctx.call_tool("mctl_get_grant", {"grant": first})
    second = json.loads(ctx.call_tool("mctl_request_approval", {"work_item": WORK_ITEM}).text)["grant"]
    ctx.call_tool("mctl_approve_grant", {"grant": second})


REINVESTIGATE_CHECK = ExpectedAnswers((
    Expectation("approving the pre-reinvestigation grant is refused", "mctl_approve_grant", {"grant": "grant-1"},
                _status_is(REFUSED)),
    Expectation("the pre-reinvestigation grant reads superseded", "mctl_get_grant", {"grant": "grant-1"},
                lambda a: isinstance(a, dict) and a.get("state") == "superseded"),
    Expectation("a grant asked after the reinvestigation is approvable", "mctl_approve_grant",
                {"grant": "grant-2"}, _status_is("approved")),
))


# -- mctl-agents#702: a failed approval never projects success ---------------
WORK_ITEM_OK = "wi_fixture_702_ok"
WORK_ITEM_BROKEN = "wi_fixture_702_broken"
SUCCESS_STATES = frozenset({"approved", "succeeded"})


class FailedApprovalTarget(FixtureTarget):
    """A work-item approval surface whose approval backend fails for one
    item. Guard: the projected state changes only after the backend answered
    success; a failure is reported as failed and leaves the item awaiting."""

    MUTATIONS = frozenset({"project_before_outcome"})
    FAILING = frozenset({WORK_ITEM_BROKEN})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.states = {WORK_ITEM_OK: "awaiting_approval", WORK_ITEM_BROKEN: "awaiting_approval"}

    def mctl_approve_work_item(self, args: dict[str, Any], _cred: Any) -> str:
        item = str(args.get("work_item"))
        if item not in self.states:
            return _refused("work-item-unknown")
        if self.mutation == "project_before_outcome":
            self.states[item] = "approved"
        if item in self.FAILING:
            return _answer(status="failed", code="approval-backend-error", work_item=item)
        self.states[item] = "approved"
        return _answer(status="approved", work_item=item)

    def mctl_get_work_item(self, args: dict[str, Any], _cred: Any) -> str:
        item = str(args.get("work_item"))
        return _answer(work_item=item, state=self.states.get(item, "unknown"))


def probe_failed_approval(ctx: ah.ProbeContext) -> None:
    for item in (WORK_ITEM_BROKEN, WORK_ITEM_OK):
        ctx.call_tool("mctl_approve_work_item", {"work_item": item})
        ctx.call_tool("mctl_get_work_item", {"work_item": item})


FAILED_APPROVAL_CHECK = ExpectedAnswers((
    Expectation("the failing approval is reported as failed", "mctl_approve_work_item",
                {"work_item": WORK_ITEM_BROKEN}, _status_is("failed")),
    Expectation("the failed item never reads as success", "mctl_get_work_item", {"work_item": WORK_ITEM_BROKEN},
                lambda a: isinstance(a, dict) and isinstance(a.get("state"), str)
                and a["state"] not in SUCCESS_STATES),
    Expectation("a successful approval does project", "mctl_get_work_item", {"work_item": WORK_ITEM_OK},
                lambda a: isinstance(a, dict) and a.get("state") == "approved"),
))


# -- mctl-api#723 (v1): a receipt is principal + tool + single use -----------
GOVERNED_TOOL = "mctl_deploy_service"
SERVICE = "checkout"
RECEIPT_OWN = "aar_01fixtureownreceipt"
RECEIPT_PEER = "aar_01fixturepeerreceipt"
RECEIPT_OTHER_TOOL = "aar_01fixtureothertool"


class ApprovalReceiptTarget(FixtureTarget):
    """A governed tool that redeems an approval receipt. Guards: the receipt
    was issued to the calling principal, for this tool, and is spent once.
    Arguments are deliberately not part of the binding (v1)."""

    MUTATIONS = frozenset({"ignore_principal", "ignore_tool", "allow_reuse"})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.receipts = {
            RECEIPT_OWN: {"principal": PRINCIPAL.name, "tool": GOVERNED_TOOL, "spent": False},
            RECEIPT_PEER: {"principal": PEER_PRINCIPAL, "tool": GOVERNED_TOOL, "spent": False},
            RECEIPT_OTHER_TOOL: {"principal": PRINCIPAL.name, "tool": "mctl_restart_service", "spent": False},
        }

    def mctl_deploy_service(self, args: dict[str, Any], cred: ah.CredentialHandle | None) -> str:
        receipt = self.receipts.get(str(args.get("receipt")))
        if receipt is None:
            return _refused("receipt-unknown")
        if self.mutation != "ignore_principal" and (cred is None or receipt["principal"] != cred.name):
            return _refused("receipt-principal-mismatch")
        if self.mutation != "ignore_tool" and receipt["tool"] != GOVERNED_TOOL:
            return _refused("receipt-tool-mismatch")
        if self.mutation != "allow_reuse" and receipt["spent"]:
            return _refused("receipt-consumed")
        receipt["spent"] = True
        return _answer(status="executed", service=args.get("service"))


def probe_approval_receipt(ctx: ah.ProbeContext) -> None:
    for receipt in (RECEIPT_OTHER_TOOL, RECEIPT_PEER, RECEIPT_OWN, RECEIPT_OWN):
        ctx.call_tool(GOVERNED_TOOL, {"service": SERVICE, "receipt": receipt})


def _deploy(receipt: str) -> dict[str, str]:
    return {"service": SERVICE, "receipt": receipt}


APPROVAL_RECEIPT_CHECK = ExpectedAnswers((
    Expectation("a receipt for another tool is refused", GOVERNED_TOOL, _deploy(RECEIPT_OTHER_TOOL),
                _status_is(REFUSED)),
    Expectation("a receipt issued to another principal is refused", GOVERNED_TOOL, _deploy(RECEIPT_PEER),
                _status_is(REFUSED)),
    Expectation("the caller's own receipt for this tool executes once", GOVERNED_TOOL, _deploy(RECEIPT_OWN),
                _status_is("executed"), occurrence=0),
    Expectation("the same receipt is refused the second time", GOVERNED_TOOL, _deploy(RECEIPT_OWN),
                _status_is(REFUSED), occurrence=1),
))


# -- mctl-api#701 (producer): a record that did not land is never recorded ---
#: Per action, the status the sink answers to each write kind before it
#: falls back to 201. 503 persists through every retry (unobservable); 409 is
#: a refusal.
SINK_SCRIPT: Mapping[str, Mapping[str, tuple[int, ...]]] = {
    "landed": {},
    "decision-unobservable": {"decision": (503,) * dr.ATTEMPTS},
    "start-refused": {"started": (409,)},
}
RECORD_EXECUTION_ID = "ex-00000000000a0701"
RECORD_ACTOR = "service:assurance-governance-fixture"
WRITER_TOKEN = "fixture-writer"
#: Each action writes a decision, its `started` and its terminal event.
WRITES_PER_ACTION = 3


class _Sink:
    """mctl-api's two decision routes, answering from `SINK_SCRIPT`. It is
    the ground truth of what landed."""

    def __init__(self) -> None:
        self.script: dict[str, list[int]] = {}
        self.stored = 0
        self.decisions = 0
        self.attempts: dict[str, int] = {}

    def __call__(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        kind = "decision" if url.endswith(dr.DECISIONS_PATH) else body["phase"]
        queued = self.script.get(kind) or []
        status = queued.pop(0) if queued else 201
        request = httpx.Request("POST", url)
        if status not in (200, 201):
            return httpx.Response(status, json={"code": f"fixture_{status}"}, request=request)
        self.stored += 1
        if kind == "decision":
            self.decisions += 1
            return httpx.Response(status, json={"decision": {"id": f"dec_{self.decisions:032x}"}}, request=request)
        if kind == dr.PHASE_STARTED:
            self.attempts[url] = self.attempts.get(url, 0) + 1
            return httpx.Response(status, json={"event": {"attempt_no": self.attempts[url]}}, request=request)
        return httpx.Response(status, json={"event": {"attempt_no": body["attempt_no"]}}, request=request)


class _LandedOnAnyAnswer(dr.DecisionRecorder):
    """The mutation: a producer whose success check passes on any answer, so
    a write that did not land reads as created."""

    def _write(self, path: str, body: dict[str, Any]) -> tuple[str, int, str, Any]:
        result = super()._write(path, body)
        if result[0] in dr.RECORDED:
            return result
        data = {"decision": {"id": "dec_" + "f" * 32}} if path == dr.DECISIONS_PATH else {"event": {"attempt_no": 1}}
        return dr.CREATED, 201, "", data


class DecisionRecordTarget(FixtureTarget):
    """Drives the real `decision_records.DecisionRecorder` (no thread, no
    sleep) against `_Sink`. Each recorded action is one real
    `policy_checkpoint` decision with one attempt."""

    MUTATIONS = frozenset({"count_unlanded_as_recorded"})

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.sink = _Sink()
        cls = _LandedOnAnyAnswer if self.mutation == "count_unlanded_as_recorded" else dr.DecisionRecorder
        self.recorder = cls(token=WRITER_TOKEN, base_url="https://api.fixture.test", post=self.sink,
                            sleep=lambda _s: None, start_thread=False)
        self.intended = 0

    def mctl_record_governed_action(self, args: dict[str, Any], _cred: Any) -> str:
        action = str(args.get("action"))
        if action not in SINK_SCRIPT:
            return _refused("action-unknown")
        self.sink.script = {k: list(v) for k, v in SINK_SCRIPT[action].items()}
        request = pc.ActionRequest(
            action_kind=pc.MCP_TOOL_CALL,
            operation="mcp__mctl__mctl_fixture_action",
            target=f"fixtures/governance/{action}",
            args_digest=pc.args_digest_of({"action": action}),
            execution_id=RECORD_EXECUTION_ID,
            actor=RECORD_ACTOR,
            grants=("mcp__mctl__mctl_fixture_action",),
        )
        decision = pc.decide(request, policy=FIXTURE_POLICY, approvals=pc.NO_APPROVALS)
        self.recorder.record_decision(request, decision)
        attempt = self.recorder.start(decision)
        attempt.succeeded()
        self.recorder.run_pending()
        self.intended += WRITES_PER_ACTION
        return _answer(status="done", action=action)

    def mctl_get_record_audit(self, args: dict[str, Any], _cred: Any) -> str:
        return _answer(producer=self.recorder.stats(), sink_stored=self.sink.stored, intended=self.intended)


def probe_decision_records(ctx: ah.ProbeContext) -> None:
    for action in SINK_SCRIPT:
        ctx.call_tool("mctl_record_governed_action", {"action": action})
    ctx.call_tool("mctl_get_record_audit", {})


def _records_honest(audit: Any) -> bool:
    if not isinstance(audit, dict) or not isinstance(audit.get("producer"), dict):
        return False
    counts = audit["producer"]
    recorded = sum(n for result, n in counts.items() if result in dr.RECORDED)
    not_recorded = sum(n for result, n in counts.items() if result not in dr.RECORDED)
    stored, intended = audit.get("sink_stored"), audit.get("intended")
    return (
        recorded == stored
        and recorded + not_recorded == intended
        and stored < intended  # the fixture must exercise writes that did not land
    )


DECISION_RECORD_CHECK = ExpectedAnswers((
    Expectation("every write counted once, and recorded only when it landed", "mctl_get_record_audit", {},
                _records_honest),
))


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
GUARANTEE = "guarantee-holds"
POLICY_GATE = "policy-gate-hit"


@dataclass(frozen=True)
class GovernanceScenario:
    name: str
    description: str
    statement: str
    tools: tuple[str, ...]
    target: type[FixtureTarget]
    probe: ah.Probe
    check: ExpectedAnswers
    #: Also pin `PolicyDecisionPerCall`: every executed call carries a
    #: permitting policy decision.
    policy_gate: bool = False

    @property
    def fixture_ref(self) -> str:
        return f"fixtures/governance/{self.name.removeprefix('governance-')}"

    @property
    def fixture_path(self) -> Path:
        return FIXTURES / f"{self.name}.json"

    def seal(self) -> ra.AssuranceScenario:
        invariants = [ra.InvariantSpec(GUARANTEE, self.statement, "deterministic_check")]
        if self.policy_gate:
            invariants.append(ra.InvariantSpec(
                POLICY_GATE, "Every call that reached the target carries a permitting policy decision.",
                "policy_decision_expected"))
        return ra.seal_scenario(
            name=self.name,
            version="1.0.0",
            description=self.description,
            target=ra.TargetBoundary(kind="fixture", ref=self.fixture_ref, environment=ra.ENV_FIXTURE),
            invariants=tuple(invariants),
            budgets=ra.Budgets(max_attempts=1, max_tool_calls=16, max_wall_clock_seconds=60, max_followups=0),
            execution_mode=ra.EXEC_DETERMINISTIC,
            mutation_mode=ra.MUTATION_FIXTURE_ONLY,
            permitted_action_kinds=(pc.MCP_TOOL_CALL,),
            permitted_tools=self.tools,
        )

    def load(self) -> ra.AssuranceScenario:
        """The reviewed, sealed fixture (its seal is re-verified on parse)."""
        return ra.AssuranceScenario.from_dict(json.loads(self.fixture_path.read_text()))

    def config(self, target: FixtureTarget, *, clock: ah.Clock | None = None) -> ah.HarnessConfig:
        detectors: dict[str, ah.Detector] = {GUARANTEE: self.check}
        if self.policy_gate:
            detectors[POLICY_GATE] = ah.PolicyDecisionPerCall()
        return ah.HarnessConfig(
            runtime=PINS,
            targets=(ah.TargetBinding(kind="fixture", ref=self.fixture_ref, environment=ra.ENV_FIXTURE,
                                      revision=REVISION, transport=target, credential=PRINCIPAL),),
            detectors=detectors,
            policy=FIXTURE_POLICY,
            clock=clock or ah.ManualClock(),
        )

    def run(self, *, mutation: str | None = None, seed: int = 0) -> tuple[ah.HarnessReport, FixtureTarget]:
        target = self.target(mutation=mutation)
        return ah.execute(self.load(), self.probe, self.config(target), seed=seed), target


SCENARIOS: tuple[GovernanceScenario, ...] = (
    GovernanceScenario(
        name="governance-approval-digest",
        description="An approval decision binds the reviewed content digest (mctl-gitops#1979, mctl-api#722).",
        statement="An approve without the reviewed content digest, or with another digest, is refused; "
                  "the reviewed digest is accepted.",
        tools=("mctl_get_proposal", "mctl_approve_proposal"),
        target=ApprovalDigestTarget,
        probe=probe_approval_digest,
        check=APPROVAL_DIGEST_CHECK,
    ),
    GovernanceScenario(
        name="governance-reinvestigate-supersedes",
        description="Reinvestigate supersedes a pending approval grant (mctlhq/mctl-agents#701).",
        statement="A grant pending when a reinvestigation starts is superseded and cannot be approved; "
                  "a grant asked afterwards can.",
        tools=("mctl_request_approval", "mctl_reinvestigate_issue", "mctl_approve_grant", "mctl_get_grant"),
        target=ReinvestigateTarget,
        probe=probe_reinvestigate,
        check=REINVESTIGATE_CHECK,
    ),
    GovernanceScenario(
        name="governance-failed-approval",
        description="A failed approval never projects success (mctlhq/mctl-agents#702).",
        statement="After an approval fails, the work item never reads as approved or succeeded; "
                  "a successful approval does project.",
        tools=("mctl_approve_work_item", "mctl_get_work_item"),
        target=FailedApprovalTarget,
        probe=probe_failed_approval,
        check=FAILED_APPROVAL_CHECK,
    ),
    GovernanceScenario(
        name="governance-approval-receipt",
        description="Every governed call gets a policy decision; an approval receipt is principal + tool + "
                    "single use (mctl-api#723, v1 semantics; arguments are not bound).",
        statement="A receipt executes the governed tool only for the principal and tool it was issued to, "
                  "and only once.",
        tools=(GOVERNED_TOOL,),
        target=ApprovalReceiptTarget,
        probe=probe_approval_receipt,
        check=APPROVAL_RECEIPT_CHECK,
        policy_gate=True,
    ),
    GovernanceScenario(
        name="governance-decision-records",
        description="A decision or attempt record that did not land is counted as dropped, never as recorded "
                    "(mctl-api#701 producer contract).",
        statement="Every decision and attempt write is counted under exactly one result, and as recorded "
                  "only when the sink stored it.",
        tools=("mctl_record_governed_action", "mctl_get_record_audit"),
        target=DecisionRecordTarget,
        probe=probe_decision_records,
        check=DECISION_RECORD_CHECK,
    ),
)
BY_NAME = {s.name: s for s in SCENARIOS}


def write_fixtures() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for s in SCENARIOS:
        s.fixture_path.write_text(json.dumps(s.seal().to_dict(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    write_fixtures()
