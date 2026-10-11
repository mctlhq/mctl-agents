"""C5 (mctlhq/mctl-agents#620): discovery exposure, the advisory approval
hint, tenant reads, the enablement switch, commerce metrics and the offline
proof. T1-T12 of the proposal."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import commerce as cm
from orchestrator import commerce_gateway as cg
from orchestrator import commerce_metrics as metrics
from orchestrator import commerce_provider as cp
from orchestrator import policy_checkpoint as pc
from tests.commerce_fakes import (
    ALLOW_PAID,
    APPROVE_PAID,
    PILOT_CAP,
    World,
    _invoke,
    make_world,
    run,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import commerce_proof as proof

FREE_CAP = "mctl://mcp-local/free-provider/echo"
BANNED = ("allow", "deny", "permit", "grant", "authoriz")


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    return make_world(tmp_path, monkeypatch)


def _keys(value):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _keys(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


# ---------------------------------------------------------------------------
# T1, T2: discovery
# ---------------------------------------------------------------------------


def test_t1_search_and_describe_show_the_price_only_for_paid_descriptors(world):
    g = run(world.gateway())
    rows = {r["capability_id"]: r for r in g.search("")["results"]}
    assert set(rows[FREE_CAP]) == {"capability_id", "title", "summary", "consequence"}  # as before, no null key
    paid = rows[PILOT_CAP]["commerce"]
    assert paid["price_display"] == "EUR 0.12"
    assert paid["price"] == {"currency": "EUR", "amount_minor": 12}
    assert type(paid["price"]["amount_minor"]) is int
    assert paid["quote_required"] is True and paid["price_mode"] == "per_successful_call"
    assert paid["funds_mode"] == "simulated" and paid["billable_on"] == "confirmed_success"
    assert {"terms_id", "terms_version", "meter"} <= set(paid)

    described = g.describe([FREE_CAP, PILOT_CAP])["results"]
    assert set(described[FREE_CAP]) == {"reason_code", "title", "summary", "consequence", "input_schema"}
    full = described[PILOT_CAP]
    assert full["commerce"]["price_display"] == "EUR 0.12" and full["consequence"]
    assert full["approval_expected"] in {"required", "not_required", "unknown"}
    # ADR 017: no discovery field reads like a grant.
    keys = set(_keys(g.search(""))) | set(_keys(g.describe([PILOT_CAP])))
    assert not [k for k in keys if any(b in k.lower() for b in BANNED)]


def test_t2_an_id_outside_the_sealed_set_shows_no_price_and_no_approval(world):
    g = run(world.gateway())
    other = "mctl://mcp-local/pilot-provider/other"
    assert g.describe([other])["results"] == {other: {"reason_code": "not-found"}}


@pytest.mark.parametrize(("policy", "expected"), [(APPROVE_PAID, "required"), (ALLOW_PAID, "not_required")])
def test_describe_approval_expected_follows_the_configured_policy(world, policy, expected):
    world.policy = policy
    g = run(world.gateway())
    assert g.describe([PILOT_CAP])["results"][PILOT_CAP]["approval_expected"] == expected
    # The hint reads no arguments and does not touch the ledger or approvals.
    assert world.ledger.writes() == [] and world.approvals.by_id == {}


def test_describe_approval_expected_is_unknown_without_the_policy_adapter(world):
    g = run(world.gateway())
    g.checkpoint = SimpleNamespace(grants=())
    assert g.describe([PILOT_CAP])["results"][PILOT_CAP]["approval_expected"] == "unknown"


def test_the_tool_descriptions_say_the_hints_are_advisory(world):
    g = run(world.gateway())
    text = " ".join(t.description for t in g.sdk_tools())
    assert text.count("capability_invoke re-checks") == 2


# ---------------------------------------------------------------------------
# T3: expected_verdict
# ---------------------------------------------------------------------------


def test_t3_expected_verdict():
    kind, op = pc.MCP_TOOL_CALL, "mcp__pilot__lookup_status"
    assert pc.expected_verdict(APPROVE_PAID, kind, op, ("mcp__pilot__*",)) == "required"
    assert pc.expected_verdict(ALLOW_PAID, kind, op, ("mcp__pilot__*",)) == "not_required"
    assert pc.expected_verdict(pc.Policy("none", ()), kind, op, ("mcp__pilot__*",)) == "unknown"
    assert pc.expected_verdict(APPROVE_PAID, kind, op, ()) == "unknown"  # grant missing
    bad = pc.Policy("bad", (pc.Rule("r", kind, "*", "NOPE"),))
    assert pc.expected_verdict(bad, kind, op, ()) == "unknown"


# ---------------------------------------------------------------------------
# T4: tenant reads
# ---------------------------------------------------------------------------


class _Wire(cm.CommerceClient):
    def __init__(self, status=200, body=None, error=False):
        super().__init__(base_url="https://api.example", token="t")  # noqa: S106
        self.reply = (status, body if body is not None else {})
        self.error = error
        self.paths: list[str] = []

    def _request(self, method, path, payload):
        self.paths.append(path)
        if self.error:
            raise cm.CommerceUnavailable("down")
        return self.reply


BUDGET = {"schema_version": 1, "currency": "EUR", "funds_mode": "simulated", "available": 4, "reserved": 0,
          "committed": 96, "pending_liability": 0}
RECEIPT = {
    "schema_version": 1, "invocation_id": "inv_1", "capability_id": PILOT_CAP, "terms_id": "ctm_1",
    "terms_version": 1, "quote_id": "cq_1", "quote_digest": "sha256:" + "0" * 64, "price_mode": "per_call",
    "price": {"currency": "EUR", "amount_minor": 12}, "funds_mode": "simulated", "phase": "finalized",
    "execution_outcome": "succeeded", "charge_status": "captured", "settlement_status": "settled",
    "approval_id": "", "execution_id": "we_1",
}


def test_t4_reads_use_the_tenant_scoped_routes_and_return_integer_money():
    wire = _Wire(body=BUDGET)
    answer = wire.get_budget("proof")
    assert answer.ok and answer.body["committed"] == 96 and wire.paths == ["/api/v1/commerce/tenants/proof/budget"]
    wire = _Wire(body=RECEIPT)
    assert wire.get_receipt("proof", "inv_1").body["price"] == {"currency": "EUR", "amount_minor": 12}
    assert wire.paths == ["/api/v1/commerce/tenants/proof/invocations/inv_1/receipt"]
    wire = _Wire(body={"invocations": [RECEIPT]})
    assert wire.list_invocations("proof", phase="finalized", limit=5).body["invocations"][0]["phase"] == "finalized"
    assert "phase=finalized" in wire.paths[0] and "limit=5" in wire.paths[0]


def test_t4_a_foreign_tenant_and_a_missing_id_answer_identically():
    foreign = _Wire(404, {"code": "not-found", "message": "invocation belongs to acme"}).get_receipt("b", "inv_a")
    missing = _Wire(404, {"code": "not-found", "message": "no such invocation"}).get_receipt("b", "inv_x")
    plain = _Wire(404, {}).get_receipt("b", "inv_y")
    assert foreign == missing == plain
    assert foreign.status == cm.REFUSED and foreign.code == "not-found"
    assert _Wire(404, {"message": "x"}).get_budget("b") == _Wire(404, {}).get_budget("b")


def test_t4_unreachable_is_unknown_never_zero_or_empty():
    for call in (lambda c: c.get_budget("t"), lambda c: c.get_receipt("t", "i"), lambda c: c.list_invocations("t")):
        for wire in (_Wire(error=True), _Wire(503, {}), _Wire(401, {})):
            answer = call(wire)
            assert answer.status == cm.UNKNOWN and answer.body == {}


@pytest.mark.parametrize("patch", [
    {"available": 4.0}, {"available": -1}, {"committed": "96"}, {"reserved": True}, {"schema_version": 2},
    {"currency": "eur"},
])
def test_t4_a_float_or_malformed_amount_is_rejected(patch):
    answer = _Wire(body={**BUDGET, **patch}).get_budget("t")
    assert answer.status == cm.UNKNOWN
    with pytest.raises(cm.CommerceError):
        cm.BudgetView.from_dict({**BUDGET, **patch})


def test_t4_a_float_in_a_receipt_is_rejected_and_nothing_secret_is_surfaced():
    bad = {**RECEIPT, "price": {"currency": "EUR", "amount_minor": 0.12}}
    assert _Wire(body=bad).get_receipt("t", "i").status == cm.UNKNOWN
    assert _Wire(body={"invocations": [bad]}).list_invocations("t").status == cm.UNKNOWN
    leaky = {**RECEIPT, "arguments": {"ticket": "OPS-1"}, "result": "secret", "payment_proof": "p",
             "external_ref": {"rail": "x"}}
    body = _Wire(body=leaky).get_receipt("t", "i").body
    assert not {"arguments", "result", "payment_proof", "external_ref"}.intersection(body)
    assert "secret" not in repr(body)
    assert _Wire(body={"invocations": "nope"}).list_invocations("t").status == cm.UNKNOWN


def test_price_display_is_display_only_and_exact():
    assert cm.price_display("EUR", 12) == "EUR 0.12"
    assert cm.price_display("EUR", 100) == "EUR 1.00" and cm.price_display("EUR", 5) == "EUR 0.05"
    assert cm.price_display("JPY", 12) == "JPY 12"
    assert cm.price_display("XYZ", 12) == "XYZ 12 minor"


# ---------------------------------------------------------------------------
# T5: enablement
# ---------------------------------------------------------------------------

ON = {"MCTL_COMMERCE_ENABLED": "true", "MCTL_COMMERCE_ADMITTED_PROVIDERS": "pilot-provider",
      "MCTL_COMMERCE_FUNDS_MODE": "simulated", "MCTL_COMMERCE_TENANT_ID": "commerce-proof"}


def test_t5_from_env_is_off_by_default_and_fails_closed_when_misconfigured():
    client = cm.CommerceClient(base_url="https://api.example", token="t")  # noqa: S106
    for env in ({}, {**ON, "MCTL_COMMERCE_ENABLED": "false"}, {**ON, "MCTL_COMMERCE_ENABLED": "1"}):
        assert cg.CommerceRuntime.from_env(env=env, client=client) is None
    runtime = cg.CommerceRuntime.from_env(env=ON, client=client)
    assert runtime is not None and runtime.tenant_id == "commerce-proof"
    assert runtime.admitted_providers == frozenset({"pilot-provider"})
    for patch in ({"MCTL_COMMERCE_FUNDS_MODE": "live"}, {"MCTL_COMMERCE_FUNDS_MODE": ""},
                  {"MCTL_COMMERCE_ADMITTED_PROVIDERS": ""}, {"MCTL_COMMERCE_ADMITTED_PROVIDERS": " , "},
                  {"MCTL_COMMERCE_TENANT_ID": ""}):
        with pytest.raises(cg.CommerceConfigError):
            cg.CommerceRuntime.from_env(env={**ON, **patch}, client=client)
    named = cg.CommerceRuntime.from_env("t2", env={**ON, "MCTL_COMMERCE_TENANT_ID": ""}, client=client)
    assert named is not None and named.tenant_id == "t2"


def test_t5_the_investigator_builds_no_runtime_unless_enabled(monkeypatch):
    from orchestrator import run_issue_investigator as rii

    monkeypatch.delenv("MCTL_COMMERCE_ENABLED", raising=False)
    assert rii._commerce_runtime() is None
    monkeypatch.setenv("MCTL_COMMERCE_ENABLED", "true")
    monkeypatch.setenv("MCTL_COMMERCE_FUNDS_MODE", "live")
    with pytest.raises(cg.CommerceConfigError):
        rii._commerce_runtime()


# ---------------------------------------------------------------------------
# T6: metrics
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.points: list[tuple[str, dict]] = []

    def add(self, name, value, attributes):
        self.points.append((name, dict(attributes)))

    def record(self, name, value, attributes):
        self.points.append((name, dict(attributes)))


@pytest.fixture
def recorder():
    rec = Recorder()
    metrics.set_backend(rec)
    yield rec
    metrics.set_backend(None)


def test_t6_attributes_are_closed_and_unknown_providers_collapse():
    attrs = metrics.attributes_for(
        admitted_providers={"pilot-provider"}, phase="finalized", reason_code="ok", price_mode="per_call",
        funds_mode="simulated", provider_id="someone-else")
    assert set(attrs) == metrics.ALLOWED_ATTRIBUTES and attrs["provider_id"] == "other"
    junk = metrics.attributes_for(admitted_providers=set(), phase="inv_01ABC", reason_code="tenant-x",
                                  price_mode="EUR 0.12", funds_mode="x", provider_id="pilot-provider")
    assert junk == {"phase": "other", "reason_code": "other", "price_mode": "other", "funds_mode": "other",
                    "provider_id": "other"}


def test_t6_a_paid_call_emits_only_bounded_attributes(world, recorder):
    assert run(_invoke(world))["reason_code"] == "ok"
    names = {n for n, _a in recorder.points}
    assert {metrics.INVOCATIONS, metrics.DURATION} <= names
    for _name, attrs in recorder.points:
        assert set(attrs) <= metrics.ALLOWED_ATTRIBUTES
        assert not [k for k in attrs if k.endswith("_id") and k != "provider_id"]
        assert not [v for v in attrs.values() if "12" in v or "inv_" in v or "acme" in v]
    assert any(a.get("provider_id") == cp.PILOT_PROVIDER_ID for _n, a in recorder.points)


def test_t6_metrics_are_a_noop_without_telemetry_and_never_raise(world):
    metrics.set_backend(None)
    assert run(_invoke(world))["reason_code"] == "ok"

    class Broken:
        def add(self, *args):
            raise RuntimeError("exporter down")

        record = add

    metrics.set_backend(Broken())
    try:
        assert run(_invoke(world, key="again"))["reason_code"] == "ok"
    finally:
        metrics.set_backend(None)


# ---------------------------------------------------------------------------
# T7-T12 through the proof scenarios (offline backend)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def suite():
    results = proof.run_suite(proof.offline_env())
    return {r.id: r for r in results}


def test_every_offline_scenario_passes_or_is_marked_offline_only(suite):
    assert list(suite) == list(proof.ORDER)
    failed = {sid: r.note or [c for c in r.checks if not c["passed"]] for sid, r in suite.items()
              if r.status == proof.FAILED}
    assert failed == {}
    assert {sid for sid, r in suite.items() if r.status == proof.OFFLINE_ONLY} == {"S11"}


def test_t7_budget_of_100_with_12_concurrent_calls_captures_at_most_8(suite):
    s1, s2 = suite["S1"], suite["S2"]
    assert s1.observed["captured"] <= 8 and s1.observed["committed"] <= 96
    assert s1.observed["budget_exhausted"] == 12 - s1.observed["captured"]
    assert s2.observed["ninth"] == "budget-exhausted" and s2.observed["committed"] == 96


def test_t8_idempotency(suite):
    assert suite["S4"].observed == {"same_invocation": True, "invocations": 1, "captures": 1, "executions": 1}
    assert suite["S5"].observed["conflict"] == "idempotency-conflict" and suite["S5"].observed["original_unchanged"]


def test_t9_telemetry_off_equals_telemetry_on():
    env = proof.offline_env()

    def observed(backend):
        metrics.set_backend(backend)
        try:
            return {r.id: r.observed for r in proof.run_suite(env) if r.id not in {"S12", "S14"}}
        finally:
            metrics.set_backend(None)

    assert observed(Recorder()) == observed(None)
    assert proof.run_suite(env, ["S12"])[0].status == proof.PASSED


def test_t10_settlement_unknown_stays_pending_then_settles_without_redispatch(suite):
    s11 = suite["S11"]
    assert s11.observed["settlement_before"] == "pending" and s11.observed["settlement_after"] == "settled"
    assert s11.observed["executions"] == 1 and s11.observed["dispatches"] == 1
    assert s11.note == proof.OFFLINE_ONLY_SETTLEMENT and s11.status == proof.OFFLINE_ONLY


def test_t11_disable_refuses_new_calls_and_the_pending_one_reconciles(suite):
    o = suite["S13"].observed
    assert o["phase_before"] == "outcome_unknown" and o["phase_after"] == "finalized"
    assert o["flag_off"] != "ok" and o["switch_on"] == "unsupported-execution" and o["invocations"] == 1


def test_t12_direct_provider_calls_without_authorization_execute_nothing(suite):
    assert suite["S6"].observed == {"refusals": {"none": "refused", "forged": "refused"}, "executions": 0}


def test_s14_leaves_no_unexplained_liability(suite):
    assert suite["S14"].observed["non_terminal"] == [] and suite["S14"].observed["invocations_swept"] > 0


def test_the_bundle_pins_versions_and_carries_no_arguments_or_secrets(suite):
    bundle = proof.build_bundle(list(suite.values()), backend="offline",
                                terms={"terms_id": "ctm_1", "terms_version": 1}, mctl_agents_sha="a" * 40,
                                mctl_api_sha="b" * 40, image_tags={"mctl-agents": "v1"})
    assert bundle["provider_contract"] == {
        "dispatch": "mctl-commerce-dispatch/v1", "quote": "mctl-commerce-quote/v1", "terms": "mctl-commerce-terms/v1"}
    assert bundle["mctl_agents_sha"] == "a" * 40 and bundle["terms"] == {"terms_id": "ctm_1", "terms_version": 1}
    assert bundle["summary"]["ok"] and [s["id"] for s in bundle["scenarios"]] == list(proof.ORDER)
    text = proof.render_markdown(bundle)
    assert "S11" in text and "token" not in text.lower()


def test_the_live_backend_refuses_the_admins_tenant(monkeypatch):
    monkeypatch.setenv("MCTL_COMMERCE_TENANT_ID", "admins")
    with pytest.raises(proof.ProofError):
        proof.live_env()
    monkeypatch.delenv("MCTL_COMMERCE_TENANT_ID")
    with pytest.raises(proof.ProofError):
        proof.live_env()
