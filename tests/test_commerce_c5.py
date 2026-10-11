"""C5 (mctlhq/mctl-agents#620): discovery exposure, the advisory approval
hint, tenant reads, the enablement switch, commerce metrics and the offline
proof. T1-T12 of the proposal."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import timedelta
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


# The rows a descriptor without commerce produced before #620, byte for byte.
LEGACY_SEARCH_BYTES = (b'{"capability_id":"mctl://mcp-local/free-provider/echo","consequence":"consequential",'
                       b'"summary":"Echo.","title":"echo"}')
LEGACY_DESCRIBE_BYTES = (b'{"reason_code":"ok","results":{"mctl://mcp-local/free-provider/echo":{"consequence":'
                         b'"consequential","input_schema":{"type":"object"},"reason_code":"ok","summary":"Echo.",'
                         b'"title":"echo"}}}')
LEGACY_KEYS_SEARCH = ["capability_id", "consequence", "summary", "title"]
LEGACY_KEYS_DESCRIBE = ["consequence", "input_schema", "reason_code", "summary", "title"]


def _bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_legacy_rows_are_byte_identical_with_commerce_on_and_off(world):
    on, off = run(world.gateway()), run(world.gateway(commerce=False))
    for g in (on, off):
        row = {r["capability_id"]: r for r in g.search("")["results"]}[FREE_CAP]
        assert sorted(row) == LEGACY_KEYS_SEARCH
        assert sorted(g.describe([FREE_CAP])["results"][FREE_CAP]) == LEGACY_KEYS_DESCRIBE
        assert _bytes(row) == LEGACY_SEARCH_BYTES
        assert _bytes(g.describe([FREE_CAP])) == LEGACY_DESCRIBE_BYTES


def test_discovery_shows_no_price_where_this_execution_cannot_charge(world):
    """Review of #634: discovery and invocation share one availability
    check, so a price is never advertised for a call that is always refused."""
    def legacy_shaped(g):
        row = {r["capability_id"]: r for r in g.search("")["results"]}[PILOT_CAP]
        entry = g.describe([PILOT_CAP])["results"][PILOT_CAP]
        return sorted(row) == LEGACY_KEYS_SEARCH and sorted(entry) == LEGACY_KEYS_DESCRIBE

    assert not legacy_shaped(run(world.gateway()))
    off = run(world.gateway(commerce=False))
    assert legacy_shaped(off)
    assert run(world.gateway(commerce=False)).search("")["results"] == off.search("")["results"]
    # Off: no terms are read, the call goes down the legacy path and the
    # provider ingress fence refuses the unsigned dispatch; the ledger is untouched.
    assert run(_invoke(world, gateway=off))["reason_code"] == "provider-error" and world.ledger.writes() == []
    world.admitted = frozenset({"someone-else"})
    unadmitted = run(world.gateway())
    assert legacy_shaped(unadmitted)
    assert run(_invoke(world, gateway=unadmitted))["reason_code"] == "unsupported-execution"
    world.admitted = frozenset({cp.PILOT_PROVIDER_ID})
    no_checkpoint = run(world.gateway())
    no_checkpoint.checkpoint = SimpleNamespace(grants=())
    assert legacy_shaped(no_checkpoint)


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


def test_describe_approval_expected_is_unknown_when_the_policy_cannot_be_read(world, monkeypatch):
    # Without the policy adapter the capability is not offered at all
    # (test_discovery_shows_no_price_where_this_execution_cannot_charge).
    g = run(world.gateway())

    def broken(*args, **kwargs):
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(pc, "expected_verdict", broken)
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


# mctl-api `commerce.Balance` exactly as GET .../budget sends it.
BUDGET = {"schema_version": 1, "tenant_id": "proof", "payer_account_id": "pa_proof", "currency": "EUR",
          "funds_mode": "simulated", "period": "2026-10", "monthly_limit_minor": 100, "per_request_max_minor": 100,
          "committed_minor": 96, "reserved_minor": 0, "pending_liability_minor": 0, "available_minor": 4,
          "as_of": "2026-10-11T12:00:00Z"}
RECEIPT = {
    "schema_version": 1, "invocation_id": "inv_1", "capability_id": PILOT_CAP, "terms_id": "ctm_1",
    "terms_version": 1, "quote_id": "cq_1", "quote_digest": "sha256:" + "0" * 64, "price_mode": "per_call",
    "price": {"currency": "EUR", "amount_minor": 12}, "funds_mode": "simulated", "phase": "finalized",
    "execution_outcome": "succeeded", "charge_status": "captured", "settlement_status": "settled",
    "approval_id": "", "execution_id": "we_1", "usage_quantity": 1,
    "policy_decision": {"status": "none"},
}


def test_t4_reads_use_the_tenant_scoped_routes_and_return_integer_money():
    wire = _Wire(body=BUDGET)
    answer = wire.get_budget("proof")
    assert answer.ok and wire.paths == ["/api/v1/commerce/tenants/proof/budget"]
    assert answer.body == {"schema_version": 1, "currency": "EUR", "funds_mode": "simulated", "available_minor": 4,
                           "reserved_minor": 0, "committed_minor": 96, "pending_liability_minor": 0}
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
    {"available_minor": 4.0}, {"available_minor": -1}, {"committed_minor": "96"}, {"reserved_minor": True},
    {"pending_liability_minor": None}, {"schema_version": 2}, {"currency": "eur"}, {"currency": "1A2"},
])
def test_t4_a_float_or_malformed_amount_is_rejected(patch):
    answer = _Wire(body={**BUDGET, **patch}).get_budget("t")
    assert answer.status == cm.UNKNOWN
    with pytest.raises(cm.CommerceError):
        cm.BudgetView.from_dict({**BUDGET, **patch})


def test_t4_a_budget_with_unsuffixed_names_is_unreadable_not_zero():
    # The pre-#620 field names are not mctl-api's: reading them must fail
    # closed, never surface as a balance.
    legacy = {k: v for k, v in BUDGET.items() if not k.endswith("_minor")}
    legacy.update({"available": 4, "reserved": 0, "committed": 96, "pending_liability": 0})
    assert _Wire(body=legacy).get_budget("t").status == cm.UNKNOWN


def test_discovery_shows_a_free_summary_without_a_paid_runtime(world):
    """Review of #634: the availability check guards charging, so a free
    summary is shown even where a paid call could not run."""
    world.ledger.publish(PILOT_CAP, world.schema_hash(), price_mode="free", amount=0, version=2)
    world.admitted = frozenset({"someone-else"})
    g = run(world.gateway())
    row = {r["capability_id"]: r for r in g.search("")["results"]}[PILOT_CAP]
    entry = g.describe([PILOT_CAP])["results"][PILOT_CAP]
    assert row["commerce"]["price_mode"] == "free" and entry["commerce"]["price_mode"] == "free"


def test_a_non_object_invocation_list_is_unknown_never_an_exception():
    """agy review of #634: every reader returns UNKNOWN on a body it cannot
    read; `list_invocations` must not raise on a JSON array."""
    for body in ([{"invocation_id": "i"}], "rows", 7):
        assert _Wire(body=body).list_invocations("t").status == cm.UNKNOWN


def test_t4_the_receipt_carries_usage_and_a_whitelisted_policy_reference():
    body = _Wire(body={**RECEIPT, "policy_decision": {
        "status": "recorded", "approval_id": "apr_1", "policy_rule_id": "paid", "policy_version": "v3",
        "note": "approver's free text", "policy_text": "rules..."}}).get_receipt("t", "i").body
    assert body["usage_quantity"] == 1
    assert body["policy_decision"] == {"status": "recorded", "approval_id": "apr_1", "policy_rule_id": "paid",
                                       "policy_version": "v3"}
    # Review of #634: a status this client does not know, or a malformed
    # sub-field, drops the summary; the receipt itself stays readable.
    for bad in ({"status": "approved"}, {}, "recorded", {"status": "none", "approval_id": 7}):
        answer = _Wire(body={**RECEIPT, "policy_decision": bad}).get_receipt("t", "i")
        assert answer.ok and "policy_decision" not in answer.body and answer.body["usage_quantity"] == 1
    assert _Wire(body={**RECEIPT, "usage_quantity": 1.0}).get_receipt("t", "i").status == cm.UNKNOWN


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
    for bad in (-5, 1.5, True):
        with pytest.raises(ValueError):
            cm.price_display("EUR", bad)


# ---------------------------------------------------------------------------
# T5: enablement
# ---------------------------------------------------------------------------

ON = {"MCTL_COMMERCE_ENABLED": "true", "MCTL_COMMERCE_ADMITTED_PROVIDERS": "pilot-provider",
      "MCTL_COMMERCE_FUNDS_MODE": "simulated", "MCTL_COMMERCE_TENANT_ID": "commerce-proof"}


def test_t5_from_env_is_off_by_default_and_fails_closed_when_misconfigured(caplog):
    client = cm.CommerceClient(base_url="https://api.example", token="t")  # noqa: S106
    for value in ("false", "1", "True", "TRUE", " true", "true "):
        assert cg.CommerceRuntime.from_env(env={**ON, "MCTL_COMMERCE_ENABLED": value}, client=client) is None
    with caplog.at_level(logging.WARNING, logger=cg.__name__):
        caplog.clear()
        assert not cg.commerce_enabled({"MCTL_COMMERCE_ENABLED": "True"})
        assert "not exactly 'true'" in caplog.text
        caplog.clear()
        for off in ({}, {"MCTL_COMMERCE_ENABLED": ""}, {"MCTL_COMMERCE_ENABLED": "false"},
                    {"MCTL_COMMERCE_ENABLED": "0"}, {"MCTL_COMMERCE_ENABLED": " Off "}):
            assert not cg.commerce_enabled(off)
        assert caplog.text == ""  # a deliberate off is not a mistake
    for env in ({}, {**ON, "MCTL_COMMERCE_ENABLED": "false"}):
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
    assert o["flag_off"] == "provider-error" and o["switch_on"] == "unsupported-execution" and o["invocations"] == 1


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


# ---------------------------------------------------------------------------
# Review of #634
# ---------------------------------------------------------------------------


def test_an_exception_inside_the_paid_path_is_recorded_as_outcome_unknown(world, recorder, monkeypatch):
    async def boom(self):
        raise RuntimeError("gateway bug")

    monkeypatch.setattr(cg.PaidInvocation, "_run", boom)
    assert run(_invoke(world))["reason_code"] == "outcome-unknown"
    invocations = [a for n, a in recorder.points if n == metrics.INVOCATIONS]
    assert invocations == [{"phase": "", "reason_code": "outcome-unknown", "price_mode": "per_successful_call",
                            "funds_mode": "simulated", "provider_id": cp.PILOT_PROVIDER_ID}]
    assert [n for n, _a in recorder.points].count(metrics.OUTCOME_UNKNOWN) == 1
    assert [n for n, _a in recorder.points].count(metrics.DURATION) == 1


def test_a_cancelled_paid_call_is_counted_by_both_instruments(world, recorder, monkeypatch):
    """Review of #634: `asyncio.CancelledError` is a BaseException; the
    dedicated counter must agree with the `invocations` label."""
    async def cancelled(self):
        raise asyncio.CancelledError

    monkeypatch.setattr(cg.PaidInvocation, "_run", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(_invoke(world))
    names = [n for n, _a in recorder.points]
    assert names.count(metrics.INVOCATIONS) == 1 and names.count(metrics.OUTCOME_UNKNOWN) == 1


def _shared_env(monkeypatch):
    """The live shape offline: every world shares one ledger and one budget,
    and the live order and not-driven list apply."""
    import contextlib
    import tempfile

    from tests import commerce_fakes as fk

    clock = fk.Clock()
    ledger = fk.FakeLedger(clock, price_mode="per_successful_call", amount=proof.PRICE_MINOR,
                           budget=proof.BUDGET_MINOR)
    approvals = fk.FakeApprovals(clock)
    env = proof.Env(name="shared", new_world=contextlib.nullcontext, not_driven=proof.LIVE_NOT_DRIVEN,
                    order=("S6", "S7", "S3", "S4", "S5", "S1", "S2", *(f"S{i}" for i in range(8, 15))))
    monkeypatch.setattr(pc, "current_identity", lambda: pc.ExecutionIdentity(
        execution_id=fk.EXECUTION, trace_id="tr", actor="agent:proof"))
    monkeypatch.setattr(proof.aa, "ttl_s", lambda: 3600)

    @contextlib.contextmanager
    def new_world():
        with tempfile.TemporaryDirectory() as tmp:
            w = fk.World(clock=clock, ledger=ledger, approvals=approvals, tmp=Path(tmp),
                         statuses={f"OPS-{i}": "open" for i in range(1, 60)})
            if not ledger.terms:
                ledger.publish(fk.PILOT_CAP, w.schema_hash())
            proof._mark(w)
            env.worlds.append(w)
            try:
                yield w
            finally:
                proof._close(env, w)

    env.new_world = new_world
    return env, ledger


def test_live_shaped_scenarios_see_only_their_own_invocations_and_budget_deltas(monkeypatch):
    env, _ = _shared_env(monkeypatch)
    results = {r.id: r for r in proof.run_suite(env)}
    assert {sid for sid, r in results.items() if r.status == proof.PASSED} == {"S3", "S4", "S5", "S6", "S7", "S14"}
    assert results["S4"].observed["invocations"] == 1  # despite S7's row on the same tenant
    assert "unspent" in results["S1"].note and "--only S1" in results["S1"].note
    assert "unspent" in results["S2"].note
    assert results["S11"].status == proof.OFFLINE_ONLY and results["S11"].note == proof.OFFLINE_ONLY_SETTLEMENT
    for sid in ("S8", "S9", "S10", "S12", "S13"):
        assert results[sid].status == proof.SKIPPED and results[sid].note == proof.LIVE_NOT_DRIVEN[sid]
    bundle = proof.build_bundle(list(results.values()), backend="live", terms={}, mctl_agents_sha="a")
    assert set(bundle["summary"]["not_driven"]) == {"S1", "S2", "S8", "S9", "S10", "S11", "S12", "S13"}


def test_a_fresh_shared_budget_runs_s1_alone(monkeypatch):
    env, _ = _shared_env(monkeypatch)
    [s1] = proof.run_suite(env, ["S1"])
    assert s1.status == proof.PASSED and s1.observed["committed"] == 96


def test_a_full_page_is_a_failed_read_never_a_complete_one(monkeypatch):
    env, _ = _shared_env(monkeypatch)
    monkeypatch.setattr(proof, "PAGE", 1)
    [s3] = proof.run_suite(env, ["S3"])  # its one row fills the page
    assert s3.status == proof.FAILED and "truncated" in s3.note


def test_s14_is_a_complement_test_so_an_unlisted_phase_is_outstanding(monkeypatch):
    """Review of #634: a phase S14 does not enumerate (mctl-api adds one)
    is outside `TERMINAL`, so it is outstanding, never swept past."""
    env, ledger = _shared_env(monkeypatch)
    [s3] = proof.run_suite(env, ["S3"])
    assert s3.status == proof.PASSED
    stuck = next(iter(ledger.invocations.values()))
    stuck["phase"] = "settling"
    [s14] = proof.run_suite(env, ["S14"])
    assert s14.status == proof.FAILED
    assert [o["phase"] for o in s14.observed["non_terminal"]] == ["settling"]


def test_each_live_world_gets_its_own_execution_id():
    ids = {proof._world_nonce(i) for i in range(3)}
    assert len(ids) == 3 and all(len(n) == 26 and n == n.upper() for n in ids)


def test_s14_alone_reads_every_ledger_and_passes_on_a_clean_tenant():
    """Review of #634: `--only S14` opens its own world, so every read runs;
    a clean tenant passes instead of failing for having nothing to sweep."""
    [s14] = proof.run_suite(proof.offline_env(), ["S14"])
    assert s14.status == proof.PASSED
    assert s14.observed["reads"] == 1 + len(proof.NON_TERMINAL) and s14.observed["invocations_swept"] == 0


def test_s14_after_scenarios_fails_when_their_rows_are_not_in_the_scoped_read(monkeypatch):
    env, ledger = _shared_env(monkeypatch)
    [s3] = proof.run_suite(env, ["S3"])
    assert s3.status == proof.PASSED
    ledger.invocations.clear()  # the scoped read now sees none of S3's rows
    [s14] = proof.run_suite(env, ["S14"])
    assert s14.status == proof.FAILED and s14.observed["missed"] == sorted(s3.receipt_ids) != []


def test_s14_covers_scenarios_that_report_no_receipts():
    """Review of #634: S13 passes no `receipts=`; its world still reports
    its row on close, so a sweep that missed it fails."""
    env = proof.offline_env()
    [s13] = proof.run_suite(env, ["S13"])
    assert s13.status == proof.PASSED and s13.receipt_ids == []
    [world] = env.worlds
    created = {r["invocation_id"] for r in proof._rows(world)}
    assert created and created <= env.created
    world.ledger.invocations.clear()
    [s14] = proof.run_suite(env, ["S14"])
    assert s14.status == proof.FAILED and set(s14.observed["missed"]) == created


def test_the_fake_ledger_honours_since_like_mctl_api(monkeypatch):
    env, ledger = _shared_env(monkeypatch)
    proof.run_suite(env, ["S3"])
    later = cm.hashed_time(ledger.clock() + timedelta(seconds=1))
    assert ledger.list_invocations(ledger.tenant).body["invocations"]
    assert ledger.list_invocations(ledger.tenant, since=later).body["invocations"] == []


def test_a_failed_read_on_close_fails_s14_not_the_scenario(monkeypatch):
    """Review of #634: the close-time bookkeeping read never flips a
    scenario whose checks held; S14 owns the failure."""
    env, _ = _shared_env(monkeypatch)
    real = proof._rows

    def flaky(world):
        if getattr(world, "closing", False):
            raise proof.ProofError("invocations could not be listed: 503")
        return real(world)

    real_close = proof._close

    def close(env_, world):
        world.closing = True
        real_close(env_, world)

    monkeypatch.setattr(proof, "_rows", flaky)
    monkeypatch.setattr(proof, "_close", close)
    s3, s14 = proof.run_suite(env, ["S3", "S14"])
    assert s3.status == proof.PASSED
    assert s14.status == proof.FAILED and "503" in s14.observed["created_unreadable"][0]


def test_a_skipped_world_still_reports_what_it_created(monkeypatch):
    env, _ = _shared_env(monkeypatch)
    with pytest.raises(proof.Skip), env.new_world() as w:
        proof._call(w, "OPS-1")
        raise proof.Skip("after a row was written")
    assert env.created and env.created == {r["invocation_id"] for r in proof._rows(w)}


def test_scoping_excludes_an_earlier_scenario_by_time_not_only_by_preexisting_ids(monkeypatch):
    """Review of #634: offline rows on the older side of a world's
    `proof_since` are excluded by the `since` read itself."""
    env, ledger = _shared_env(monkeypatch)
    proof.run_suite(env, ["S3"])
    ledger.clock.advance(proof.LOOKBACK_S + 1)
    with env.new_world() as later:
        assert later.proof_preexisting == frozenset()  # the read already left S3's rows out
        assert proof._list(later, since=later.proof_since) == []


def test_s14_passes_when_the_scenarios_that_ran_created_no_invocation(monkeypatch):
    """Review of #634: S6 opens a world but creates no row; that is not a
    vacuous sweep."""
    env, _ = _shared_env(monkeypatch)
    s6, s14 = proof.run_suite(env, ["S6", "S14"])
    assert s6.status == proof.PASSED and s6.receipt_ids == []
    assert s14.status == proof.PASSED and s14.observed["created"] == 0


def test_the_cli_refuses_a_live_run_without_a_bundle_and_a_malformed_image_tag():
    with pytest.raises(SystemExit) as no_out:
        proof.main(["--backend", "live"])
    assert no_out.value.code == 2
    with pytest.raises(SystemExit) as bad_tag:
        proof.main(["--image-tag", "mctl-api:v1"])
    assert bad_tag.value.code == 2


def test_the_fake_accept_switch_refuses_new_quotes_like_mctl_api_570(world):
    world.ledger.accepting = False
    answer = world.ledger.mint_quote(world.tenant, capability_id=PILOT_CAP, input_schema_hash=world.schema_hash(),
                                     args_digest=cm.args_digest({"ticket": "OPS-1"}), execution_id="we_1")
    assert (answer.status, answer.http_status, answer.code) == (cm.REFUSED, 422, "unsupported-execution")
