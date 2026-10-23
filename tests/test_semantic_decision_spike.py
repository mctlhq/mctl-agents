"""Tests for tools/semantic_decision_spike.py (mctlhq/mctl-agents#556).

Replay-only: no test opens a socket (the autouse fixture makes any attempt
fail), and the live providers run against `httpx.MockTransport`."""
from __future__ import annotations

import itertools
import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import semantic_decision_spike as sd  # noqa: E402

from orchestrator import policy_checkpoint as pc  # noqa: E402

FIXTURE_DIR = ROOT / "tests/fixtures/semantic_decision"
V1 = FIXTURE_DIR / "v1"
FIXTURES = sd.load_fixtures(V1)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a real socket was opened")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _result(status: str = "ok", rec: str = "HIGH_RISK", conf: float = 0.9) -> sd.SemanticDecisionResult:
    return sd.SemanticDecisionResult(
        schema_version=sd.SCHEMA_VERSION, provider="p", model="m", model_version="", endpoint="e",
        signal_status=status, recommendation=rec if status == "ok" else "", confidence=conf)


# T1
@pytest.mark.parametrize("det", sorted(sd.STRICTNESS))
def test_combine_is_monotone_and_never_denies(det: str) -> None:
    thr = sd.DEFAULT_THRESHOLD
    for status, rec, conf in itertools.product(
            sd.SIGNAL_STATUSES, (*sd.RECOMMENDATIONS, ""), (0.0, thr - 1e-6, thr, 1.0)):
        out = sd.combine(det, _result(status, rec, conf), thr)
        assert sd.STRICTNESS[out] >= sd.STRICTNESS[det]
        if det != pc.ALLOW:
            assert out == det
        if det == pc.ALLOW:
            assert out in (pc.ALLOW, pc.REQUIRE_APPROVAL)


def test_combine_over_every_fixture() -> None:
    for fx in FIXTURES:
        det = sd.deterministic_verdict(fx.policy, fx.request)
        for rec in sd.RECOMMENDATIONS:
            out = sd.combine(det, _result("ok", rec, 1.0))
            assert sd.STRICTNESS[out] >= sd.STRICTNESS[det]
            assert out != pc.DENY or det == pc.DENY


def test_only_escalation_path() -> None:
    assert sd.combine(pc.ALLOW, _result("ok", "HIGH_RISK", 0.5), 0.5) == pc.REQUIRE_APPROVAL
    assert sd.combine(pc.ALLOW, _result("ok", "REVIEW", 1.0), 0.5) == pc.ALLOW
    assert sd.combine(pc.ALLOW, _result("ok", "LOW_RISK", 1.0), 0.5) == pc.ALLOW


# T2
@pytest.mark.parametrize("status", ["timeout", "error", "malformed", "low_confidence"])
@pytest.mark.parametrize("det", sorted(sd.STRICTNESS))
def test_failures_return_deterministic_verdict(status: str, det: str) -> None:
    assert sd.combine(det, _result(status, "HIGH_RISK", 1.0)) == det


def test_low_confidence_applied_by_threshold() -> None:
    low = sd.apply_threshold(_result("ok", "HIGH_RISK", 0.2), 0.5)
    assert low.signal_status == "low_confidence"
    assert sd.combine(pc.ALLOW, low) == pc.ALLOW


# T3
def test_fixture_deterministic_verdict_has_not_drifted() -> None:
    for fx in FIXTURES:
        rule, _code, _ = pc.evaluate(sd.POLICIES[fx.policy], fx.request.action_request())
        assert fx.deterministic_verdict == (rule.verdict if rule else pc.DENY), fx.fixture_id


# T4
def test_builder_rejects_credentials_and_long_values() -> None:
    base = dict(json.loads((V1 / "A01.json").read_text())["request"], fixture_id="X", scenario="A")
    sd.build_request(base)
    with pytest.raises(sd.SpikeError):
        sd.build_request({**base, "target": "ghp_" + "a" * 30})
    with pytest.raises(sd.SpikeError):
        sd.build_request({**base, "attrs": {"k": "x" * 257}})
    with pytest.raises(sd.SpikeError):
        sd.build_request({**base, "model_claims": ["Bearer " + "a" * 20]})
    with pytest.raises(sd.SpikeError):
        sd.build_request({**base, "scenario": "Z"})


def test_untrusted_content_only_in_delimited_section() -> None:
    fx = next(f for f in FIXTURES if f.fixture_id == "B10")
    _system, user = sd.build_prompt(fx.request)
    head, _, rest = user.partition(sd.UNTRUSTED_BEGIN)
    body = rest.partition(sd.UNTRUSTED_END)[0]
    assert fx.request.tool_description in body
    assert fx.request.tool_description not in head
    claimed = next(f for f in FIXTURES if f.fixture_id == "B09")
    assert claimed.request.model_claims[0] not in sd.build_prompt(claimed.request)[1].partition(sd.UNTRUSTED_BEGIN)[0]


# T5
@pytest.mark.parametrize("raw", [
    "not json", "[]", "{}", '{"recommendation": "CRITICAL", "categories": [], "confidence": 0.9}',
    '{"recommendation": "LOW_RISK", "categories": ["bogus"], "confidence": 0.9}',
    '{"recommendation": "LOW_RISK", "categories": [], "confidence": 2}',
    '{"recommendation": "LOW_RISK", "categories": [], "confidence": true}',
    '{"recommendation": "LOW_RISK", "categories": [], "confidence": 0.5, "reason_codes": ["x"]}',
    '{"recommendation": "LOW_RISK", "categories": []}',
])
def test_schema_violations_are_malformed(raw: str) -> None:
    assert sd.parse_decision(raw) is None
    prov = sd.ReplayProvider("r", {"A01": {"raw": raw}})
    assert prov.decide(FIXTURES[0].request).signal_status == "malformed"


def test_valid_decision_parses() -> None:
    parsed = sd.parse_decision({"recommendation": "REVIEW", "categories": ["read_only"],
                                "probabilities": {"REVIEW": 0.7, "LOW_RISK": 0.3}})
    assert parsed is not None and parsed["confidence"] == 0.7


# T6
def test_orchestrator_does_not_reference_the_spike() -> None:
    for path in (ROOT / "orchestrator").rglob("*.py"):
        assert "semantic_decision" not in path.read_text(), path
    assert not [n for n in dir(pc) if "semantic" in n.lower()]


# T7
def test_report_matches_golden() -> None:
    records = [json.loads(line) for line in (FIXTURE_DIR / "golden/records.jsonl").read_text().splitlines()]
    assert sd.render_report(records) == (FIXTURE_DIR / "golden/report.md").read_text()
    text = (FIXTURE_DIR / "golden/report.md").read_text()
    assert "## Dangerous false negatives" in text and "## Cost per decisions" in text


def test_report_contested_labels_excluded_from_accuracy() -> None:
    records = [json.loads(line) for line in (FIXTURE_DIR / "golden/records.jsonl").read_text().splitlines()]
    one = [dict(r, label_status="contested") for r in records if r["provider"] == "replay-a"]
    assert sd.provider_metrics(one)["accuracy"] is None


def test_live_metrics_marked_not_measured_for_replay() -> None:
    records = [json.loads(line) for line in (FIXTURE_DIR / "golden/records.jsonl").read_text().splitlines()]
    assert all(not r["live"] for r in records)
    assert "not measured (replay)" in sd.render_report(records)


# T8
def test_replay_pipeline_is_offline_and_deterministic(tmp_path: Path) -> None:
    replay = json.loads((FIXTURE_DIR / "replay-synthetic.json").read_text())
    runs = []
    for _ in range(2):
        providers = [sd.ReplayProvider(n, replay[n]) for n in ("replay-a", "replay-b")]
        runs.append(sd.render_jsonl(sd.run_records(FIXTURES, providers, repeats=5)))
    assert runs[0] == runs[1] == (FIXTURE_DIR / "golden/records.jsonl").read_text()


def test_records_carry_no_prompt_or_completion_keys() -> None:
    replay = json.loads((FIXTURE_DIR / "replay-synthetic.json").read_text())
    recs = sd.run_records(FIXTURES[:3], [sd.ReplayProvider("replay-a", replay["replay-a"])], repeats=1)
    for r in recs:
        assert not [k for k in r if "prompt" in k or "completion" in k or "secret" in k]


def test_live_provider_refused_without_flag() -> None:
    assert sd.main(["run", "--provider", "typesafe-jev"]) == 2


def test_record_raw_never_inside_a_repository(tmp_path: Path) -> None:
    with pytest.raises(sd.SpikeError):
        sd.check_raw_dir(ROOT / "raw")


def _stub(handler: object) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


GOOD = {"recommendation": "HIGH_RISK", "categories": ["destructive"], "confidence": 0.9, "reason_codes": []}
GOOD["categories"] = ["state_changing"]


def test_jev_provider_against_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-not-a-secret")
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "jev-latest"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD)}}],
                                         "usage": {"prompt_tokens": 321}})
    prov = sd.TypesafeJevProvider(_stub(handler))
    assert prov.resolve_model() == "jev-latest"
    res = prov.decide(FIXTURES[0].request)
    assert res.signal_status == "ok" and res.input_tokens == 321 and res.live
    assert "test-not-a-secret" not in json.dumps(sd.result_record(FIXTURES[0], res, "ALLOW", 0, "ALLOW"))


def test_clef_provider_against_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-not-a-secret")
    prov = sd.CloudflareProvider(_stub(lambda r: httpx.Response(
        200, json={"success": True, "result": {"response": json.dumps(GOOD)}})))
    res = prov.decide(FIXTURES[0].request)
    assert res.signal_status == "ok" and res.model == "@cf/cloudflare/clef-flash"
    assert "/accounts/acct/ai/run/@cf/cloudflare/clef-flash" in str(prov._url())


def test_live_failure_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-not-a-secret")
    calls = {"n": 0}

    def timeout(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")
    res = sd.CloudflareProvider(_stub(timeout)).decide(FIXTURES[0].request)
    assert (res.signal_status, res.retries, calls["n"]) == ("timeout", 1, 2)
    res = sd.CloudflareProvider(_stub(lambda r: httpx.Response(500))).decide(FIXTURES[0].request)
    assert (res.signal_status, res.http_status, res.retries) == ("error", 500, 1)
    res = sd.CloudflareProvider(_stub(lambda r: httpx.Response(200, text="<html>"))).decide(FIXTURES[0].request)
    assert res.signal_status == "malformed"
    res = sd.CloudflareProvider(_stub(lambda r: httpx.Response(200, json={"success": True, "result": {
        "response": '{"recommendation": "MAYBE"}'}}))).decide(FIXTURES[0].request)
    assert res.signal_status == "malformed"
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    with pytest.raises(sd.SpikeError):
        sd.CloudflareProvider(_stub(lambda r: httpx.Response(200))).decide(FIXTURES[0].request)


# T9
def test_fixture_corpus_shape() -> None:
    assert len(FIXTURES) >= 40
    for sc in sd.SCENARIOS:
        assert sum(f.scenario == sc for f in FIXTURES) >= 6
    assert sum(1 for f in FIXTURES if f.danger and f.deterministic_verdict == pc.ALLOW) >= 10
    pairs: dict[str, int] = {}
    for f in FIXTURES:
        if f.pair_id:
            pairs[f.pair_id] = pairs.get(f.pair_id, 0) + 1
    assert sum(n == 2 for n in pairs.values()) >= 5
    assert any(f.adversarial for f in FIXTURES)
    for f in FIXTURES:
        assert f.confirmed_by and f.confirmed_by != f.labelled_by, f.fixture_id
        assert f.label_status in ("agreed", "contested", "unconfirmed")


def test_minimal_pairs_differ_in_one_attribute() -> None:
    by_pair: dict[str, list[sd.Fixture]] = {}
    for f in FIXTURES:
        if f.pair_id:
            by_pair.setdefault(f.pair_id, []).append(f)
    for pid, (a, b) in by_pair.items():
        ra, rb = dict(a.data["request"]), dict(b.data["request"])
        diff = {k for k in {**ra, **rb} if ra.get(k) != rb.get(k)} - {"execution_id"}
        flat = 0
        for k in diff:
            if isinstance(ra.get(k), dict) and isinstance(rb.get(k), dict):
                flat += sum(ra[k].get(i) != rb[k].get(i) for i in {*ra[k], *rb[k]})
            elif k == "evidence":
                flat += sum(x != y for x, y in zip(ra[k], rb[k], strict=True))
            else:
                flat += 1
        assert flat == 1, (pid, diff)


def test_fixtures_contain_no_credentials() -> None:
    for path in V1.glob("*.json"):
        assert not sd.contains_credential(path.read_text()), path
