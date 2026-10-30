# ruff: noqa: E501, S106
"""Offline tests for tools/hetzner_inference_spike.py and docs/spikes/791/shim.py
(mctlhq/mctl-agents#791). No network beyond loopback; no SDK session is started."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import anyio
import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import hetzner_inference_spike as hs  # noqa: E402

_spec = importlib.util.spec_from_file_location("spike791_shim_test", ROOT / "docs" / "spikes" / "791" / "shim.py")
assert _spec is not None and _spec.loader is not None
shim = importlib.util.module_from_spec(_spec)
sys.modules["spike791_shim_test"] = shim
_spec.loader.exec_module(shim)


def _client(handler) -> hs.HetznerClient:
    return hs.HetznerClient("https://example.test", "k", httpx.Client(transport=httpx.MockTransport(handler)))


def _fake_hetzner(catalog: list[str], messages_ok: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": i} for i in catalog]})
        if path == "/v1/messages":
            return httpx.Response(200, json={"type": "message"}) if messages_ok else httpx.Response(404, text="nope")
        body = json.loads(request.content)
        msg = {"role": "assistant", "content": "ok"}
        if body.get("tools"):
            msg = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\":\"Berlin\"}"}}]}
        return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                                         "choices": [{"finish_reason": "stop", "message": msg}]})
    return handler


# T1
def test_snapshot_missing_model_is_unavailable_and_not_substituted():
    catalog = ["Kimi-K2.7-Code", "GLM-5.2-NVFP4", "Qwen/Qwen3.6-35B-A3B-FP8", "DeepSeek-V4-Flash"]  # near miss
    snap = hs.take_snapshot(_client(_fake_hetzner(catalog)), base_url="https://example.test")
    assert snap["selected"]["DeepSeek-V4-Flash-0731"] == {"availability": hs.UNAVAILABLE, "preregistered": True}
    assert snap["selected"]["Kimi-K2.7-Code"]["function_call"]["tool_call_returned"] is True
    assert snap["messages_probe"]["ok"] is False
    assert "timestamp_utc" in snap and not hs.contains_credential(json.dumps(snap))


# T2
def test_hetzner_session_env_blanks_every_credential():
    env = {k: "secret-value" for k in (
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_SECONDARY", "GITHUB_TOKEN", "MCTL_TOKEN")}
    from orchestrator.evidence_producer import TOKEN_ENV as EVIDENCE_TOKEN_ENV
    from orchestrator.usage_ledger import TOKEN_ENV as WRITER_TOKEN_ENV
    env[EVIDENCE_TOKEN_ENV] = env[WRITER_TOKEN_ENV] = "secret-value"
    out = hs.hetzner_session_env(env, base_url="http://127.0.0.1:1", token="tok", model="M")
    for name in env:
        assert out[name] == "", name
    assert out["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1" and out["ANTHROPIC_AUTH_TOKEN"] == "tok"
    assert all(out[s] == "M" for s in hs._MODEL_SLOTS)


# T3
def test_candidate_options_use_the_real_builder_and_policy_denies_deploy(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "t")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
    ws = tmp_path / "ws"
    ws.mkdir()
    base = hs.cell_options("investigation", "base-model", ws, None)
    cand = hs.cell_options("investigation", "M", ws, lambda e: hs.hetzner_session_env(e, base_url="u", token="t", model="M"))
    assert base.allowed_tools == cand.allowed_tools
    assert base.hooks == cand.hooks or [type(h).__name__ for m in base.hooks["PreToolUse"] for h in m.hooks] == [
        type(h).__name__ for m in cand.hooks["PreToolUse"] for h in m.hooks]
    assert cand.env["CLAUDE_CODE_OAUTH_TOKEN"] == "" and base.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert cand.model == "M"

    counter = hs._DenialCounter()
    counter.wrap(cand)
    call = {"tool_name": "mcp__mctl__mctl_deploy_service", "tool_input": {"service": "x"}}
    outs = [anyio.run(h, call, "id1", None) for m in cand.hooks["PreToolUse"] for h in m.hooks]
    assert any((o.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny" for o in outs)
    assert counter.denied["mcp__mctl__mctl_deploy_service"] == 1


# T4
def test_shim_codec_round_trip():
    req = {"model": "M", "max_tokens": 10, "system": [{"type": "text", "text": "sys"}], "stream": True,
           "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
           "messages": [
               {"role": "user", "content": "hi"},
               {"role": "assistant", "content": [{"type": "text", "text": "x"},
                                                 {"type": "tool_use", "id": "a", "name": "t", "input": {"q": 1}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "res", "is_error": True}]}]}
    chat, _ = shim.messages_to_chat(req)
    assert [m["role"] for m in chat["messages"]] == ["system", "user", "assistant", "tool"]
    assert chat["messages"][2]["tool_calls"][0]["function"]["arguments"] == "{\"q\": 1}"
    assert chat["messages"][3]["content"].startswith("[tool error]")
    assert chat["tools"][0]["function"]["name"] == "t"
    msg = shim.chat_to_message({"choices": [{"finish_reason": "tool_calls", "message": {
        "content": "go", "tool_calls": [{"id": "b", "function": {"name": "t", "arguments": "{\"z\": 2}"}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7}}, "M")
    assert msg["stop_reason"] == "tool_use" and msg["content"][1] == {"type": "tool_use", "id": "b", "name": "t", "input": {"z": 2}}
    sse = shim.message_to_sse(msg)
    events = re.findall(r"^event: (\w+)$", sse, flags=re.M)
    assert events[0] == "message_start" and events[-1] == "message_stop" and "input_json_delta" in sse
    with pytest.raises(shim.ShimUnsupported):
        shim.messages_to_chat({"model": "M", "messages": [{"role": "user", "content": [{"type": "image"}]}]})
    with pytest.raises(shim.ShimUnsupported):
        shim.messages_to_chat({"model": "M", "messages": [], "mystery": 1})


def test_shim_server_loopback_and_token():
    with pytest.raises(ValueError):
        shim.ShimServer(lambda c: {}, host="0.0.0.0")  # noqa: S104
    reply = {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}], "usage": {}}
    with shim.ShimServer(lambda c: reply) as srv:
        body = {"model": "M", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}
        assert httpx.post(f"{srv.url}/v1/messages", json=body).status_code == 401
        assert httpx.post(f"{srv.url}/v1/messages", json=body, headers={"x-api-key": "wrong"}).status_code == 401
        ok = httpx.post(f"{srv.url}/v1/messages", json=body, headers={"x-api-key": srv.token})
        assert ok.status_code == 200 and ok.json()["content"][0]["text"] == "hello"
        bad = httpx.post(f"{srv.url}/v1/messages", json={**body, "weird": 1}, headers={"x-api-key": srv.token})
        assert bad.status_code == 501 and srv.unsupported


# T5
def test_artifact_safety(tmp_path):
    with pytest.raises(hs.SpikeError):
        hs.write_artifact(tmp_path / "x.json", "key sk-ant-api03-" + "a" * 40)
    assert not (tmp_path / "x.json").exists()
    with pytest.raises(hs.SpikeError):
        hs.check_raw_dir(ROOT / "docs" / "raw")


def test_no_live_call_without_allow_live(monkeypatch):
    monkeypatch.setenv("HETZNER_INFERENCE_BASE_URL", "https://example.test")
    monkeypatch.setenv("HETZNER_INFERENCE_API_KEY", "k")
    assert hs.main(["snapshot"]) == 2


# T6 / bench dry run
def test_bench_dry_run_and_model_mismatch(tmp_path):
    fixtures = hs.load_fixtures()
    good = {"tool_calls": [], "final_text": "", "turns": 1, "wall_ms": 5, "http_errors": 0, "timeouts": 0}

    async def runner(task, model, provider, rep, ws):
        text = {"investigation": "CLAUDE_BALANCED_MODEL claude-sonnet-5-5 config/model_policy.py config/model-policy.yaml",
                "implementation": "", "review": "", "tool-use": "RESULT=42"}[task]
        calls = [{"name": n} for n in ("Glob", "Read", "Bash")] if task == "tool-use" else []
        # the cell for "Qwen/Qwen3.6-35B-A3B-FP8" reports a different serving model
        served = "other" if model.startswith("Qwen") else model
        if task == "implementation":
            (ws / "calc" / "__init__.py").write_text("def mean(v):\n    return sum(v) / len(v) if v else 0.0\n")
        return {**good, "final_text": text, "tool_calls": calls, "usage_models": [served]}

    base = {"model": "claude-test", "model_policy_version": 1}
    records, manifest = anyio.run(lambda: hs.run_bench(
        runner, fixtures, ["Kimi-K2.7-Code", "Qwen/Qwen3.6-35B-A3B-FP8"], reps=1, baseline=base, allowed_tools=["*"]))
    assert len(records) == 4 * 3
    qwen = [r for r in records if r["model"].startswith("Qwen")]
    assert all(r["status"] == "model_mismatch" and r["oracle_result"] == "fail" for r in qwen)
    kimi = {r["task"]: r for r in records if r["model"] == "Kimi-K2.7-Code"}
    assert kimi["tool-use"]["oracle_result"] == "pass" and kimi["implementation"]["tests_passed"] is True
    assert kimi["review"]["findings_missed"] == 4
    assert manifest["baseline"]["model"] == "claude-test" and set(manifest["fixtures"]) == set(hs.TASKS)
    hs.write_artifact(tmp_path / "records.jsonl", hs.render_jsonl(records))
    snapshot = hs.take_snapshot(_client(_fake_hetzner(["Kimi-K2.7-Code"])), base_url="https://example.test")
    text = hs.render_report(records, snapshot, manifest, snapshot)
    assert "## Rule outcome" in text and "Catalog drift: none" in text


# T7
def test_orchestrator_does_not_import_the_spike():
    for path in (ROOT / "orchestrator").rglob("*.py"):
        text = path.read_text()
        assert "hetzner_inference_spike" not in text and "spikes.791" not in text and "spikes/791" not in text, path


# T8
def test_oracles_score_known_good_and_bad():
    fx = hs.load_fixtures()
    inv = fx["investigation"]
    assert hs.score_oracle(inv, "CLAUDE_BALANCED_MODEL claude-sonnet-5-5 config/model_policy.py config/model-policy.yaml", [])["oracle_result"] == "pass"
    assert hs.score_oracle(inv, "I do not know", [])["oracle_result"] == "fail"
    rev = fx["review"]
    good = ("FINDING: page offset skips the first item :: off-by-one\nFINDING: password logged :: leak\n"
            "FINDING: deploy has no approval :: policy\nFINDING: empty list division by zero :: crash")
    scored = hs.score_oracle(rev, good, [])
    assert scored["oracle_result"] == "pass" and scored["findings_caught"] == 4
    bad = hs.score_oracle(rev, "FINDING: label uses string concatenation :: style", [])
    assert bad["oracle_result"] == "fail" and bad["false_positives"] == 1 and bad["findings_missed"] == 4
    tool = fx["tool-use"]
    calls = [{"name": n} for n in ("Glob", "Read", "Bash")]
    assert hs.score_oracle(tool, "RESULT=42", calls)["oracle_result"] == "pass"
    assert hs.score_oracle(tool, "RESULT=42", [*calls, {"name": "mcp__mctl__mctl_deploy_service"}])["policy_bypasses"] == 1
    assert hs.score_oracle(tool, "RESULT=41", calls)["oracle_result"] == "fail"


def test_decision_rule():
    def rec(model, task, ok):
        return {"model": model, "task": task, "oracle_result": "pass" if ok else "fail", "status": "ok", "policy_bypasses": 0}
    base = [rec("b", t, True) for t in ("implementation", "tool-use") for _ in range(3)]
    cand = [rec("m", t, True) for t in ("implementation", "tool-use") for _ in range(3)]
    assert hs.decide(base + cand, ["m"], "b")[0] == "ADOPT_EXPERIMENTAL_PROVIDER"
    weak = [rec("m", "tool-use", False)] * 3
    assert hs.decide(base + weak, ["m"], "b")[0] == "REJECT"
    mixed = [rec("m", "tool-use", True)] * 2 + [rec("m", "implementation", False)] * 3
    assert hs.decide(base + mixed, ["m"], "b")[0] == "LIMITED_BENCH_ONLY"


def test_baseline_with_extra_side_model_is_not_a_mismatch():
    base = {"provider": hs.BASELINE_PROVIDER, "model": "m"}
    assert hs._models_ok(base, {"m", "small"})
    assert not hs._models_ok(base, {"small"})
    assert not hs._models_ok({"provider": hs.CANDIDATE_PROVIDER, "model": "m"}, {"m", "small"})


def test_upstream_failure_is_not_a_shim_gap():
    import pytest
    with pytest.raises(RuntimeError):
        hs._upstream_json({"status": 429, "json": {"error": "x"}})
    assert hs._upstream_json({"status": 200, "json": {"choices": []}}) == {"choices": []}


def test_candidate_env_is_allowlisted():
    out = hs.hetzner_session_env({"PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "s", "HCLOUD_TOKEN": "h"},
                                 base_url="u", token="t", model="M")
    assert "AWS_SECRET_ACCESS_KEY" not in out and "HCLOUD_TOKEN" not in out and out["PATH"] == "/bin"


def test_candidate_mcp_call_is_rejected_not_a_bypass_and_home_is_isolated():
    fx = hs.load_fixtures()
    base = {"provider": hs.CANDIDATE_PROVIDER, "model": "M"}
    obs = {"tool_calls": [{"name": n} for n in ("Glob", "Read", "Bash", "mcp__mctl__mctl_deploy_service")],
           "final_text": "RESULT=42", "usage_models": ["M"]}
    rec = hs.finalize_cell(base, obs, fx["tool-use"], ["Glob", "Read", "Bash", "mcp__mctl__*"], None)
    assert rec["policy_bypasses"] == 0 and rec["tool_calls_invalid"] == 1
    out = hs.hetzner_session_env({"PATH": "/bin", "HOME": "/home/operator"}, base_url="u", token="t", model="M")
    assert out["HOME"] != "/home/operator"


def test_owner_added_model_is_probed_and_flagged_and_bench_never_substitutes():
    snap = hs.take_snapshot(_client(_fake_hetzner(["Qwen/Qwen3.6-35B-A3B-FP8", "Qwen3.8-27B"])),
                            base_url="https://example.test", extra=["Qwen3.8-27B", "Absent-1"])
    assert snap["selected"]["Qwen3.8-27B"]["preregistered"] is False
    assert snap["selected"]["Qwen3.8-27B"]["function_call"]["tool_call_returned"] is True
    assert snap["selected"]["Absent-1"]["availability"] == hs.UNAVAILABLE
    assert hs.bench_models(snap, []) == ["Qwen/Qwen3.6-35B-A3B-FP8", "Qwen3.8-27B"]
    assert hs.bench_models(snap, ["Qwen3.8-27B"]) == ["Qwen3.8-27B"]
    with pytest.raises(hs.SpikeError):
        hs.bench_models(snap, ["Absent-1"])


def test_deep_probes_record_protocol_framing_not_content():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "M"}]})
        body = json.loads(request.content)
        if body.get("stream"):
            sse = (b'data: {"type":"message_start"}\n\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"secret-ish"}}\n\n'
                   b"data: [DONE]\n\n")
            return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        if request.url.path == "/v1/messages":
            last = body["messages"][-1]["content"]
            if body.get("tools") and isinstance(last, str):
                return httpx.Response(200, json={"type": "message", "stop_reason": "tool_use", "model": "M",
                                                 "content": [{"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}}]})
            return httpx.Response(200, json={"type": "message", "stop_reason": "end_turn", "model": "M",
                                             "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 5}})
        return _fake_hetzner(["M"])(request)

    snap = hs.take_snapshot(_client(handler), base_url="https://example.test", selected=("M",), deep=True)
    probes = snap["selected"]["M"]["protocols"]
    assert probes["messages_tool_use"]["stop_reason"] == "tool_use"
    assert probes["messages_tool_result"]["stop_reason"] == "end_turn"
    assert probes["messages_stream_text"]["event_types"] == {"message_start": 1, "content_block_delta:text_delta": 1, "[DONE]": 1}
    assert "secret-ish" not in json.dumps(snap)
    assert snap["selected"]["M"]["long_context"]["status"] == 200
    assert snap["selected"]["M"]["long_context"]["prompt_chars"] == hs.LONG_CONTEXT_CHARS


def test_merge_joins_arms_and_refuses_mismatched_fixtures():
    fx = {"t": {"id": "x", "sha256": "s", "limits": {}}}
    m1 = {"run_id": "a", "repo_sha": "r", "fixtures": fx, "baseline": {"model_policy_version": 1}, "started_utc": "1",
          "finished_utc": "2", "arms": ["baseline"], "available_models": [], "repetitions_achieved": {"t|b": 3},
          "run_location": {"baseline": "host"}}
    m2 = {**m1, "run_id": "c", "arms": ["candidate"], "available_models": ["M"], "repetitions_achieved": {"t|M": 3},
          "run_location": {"candidate": "container"}, "started_utc": "3", "finished_utc": "4"}
    records, merged = hs.merge_runs([([{"model": "b", "task": "t", "repetition": 1}], m1),
                                     ([{"model": "M", "task": "t", "repetition": 1}], m2)])
    assert len(records) == 2 and merged["arms"] == ["baseline", "candidate"] and merged["available_models"] == ["M"]
    assert merged["repetitions_achieved"] == {"t|b": 3, "t|M": 3} and merged["finished_utc"] == "4"
    with pytest.raises(hs.SpikeError):
        hs.merge_runs([([], m1), ([], {**m2, "fixtures": {"t": {"id": "y", "sha256": "z", "limits": {}}}})])
    with pytest.raises(hs.SpikeError):
        hs.merge_runs([([], m2), ([], m2)])


def test_checklist_flags_write_in_read_only_task_and_budget_stop():
    rec = {"model": "M", "task": "review", "status": "ok", "oracle_result": "fail", "tool_calls_valid": 1,
           "tool_calls_hallucinated": 0, "finish_reasons": ["max_tokens"], "tool_names": {"Edit": 1}}
    checks = hs.model_checklist([rec], {"long_context": {"status": 400, "type": None}})
    assert checks["complies with read-only instruction"] == "fail"
    assert checks["reasoning does not exhaust output budget"] == "fail"
    assert checks["long context (~60k tokens) accepted"] == "fail"
    assert checks["recovers after a tool error"] == "unknown"


def test_bench_arm_misuse_is_refused(tmp_path):
    snap = {"selected": {"M": {"availability": hs.UNAVAILABLE}}, "messages_probe": {"ok": True}}
    (tmp_path / "snapshot-start.json").write_text(json.dumps(snap))
    common = ["bench", "--allow-live", "--out-dir", str(tmp_path)]
    assert hs.main([*common, "--arm", "baseline", "--models", "M"]) == 2
    assert hs.main([*common, "--arm", "candidate"]) == 2


def test_effort_is_candidate_only_and_recorded(tmp_path):
    snap = {"selected": {"M": {"availability": hs.AVAILABLE}}, "messages_probe": {"ok": True}}
    (tmp_path / "snapshot-start.json").write_text(json.dumps(snap))
    common = ["bench", "--allow-live", "--out-dir", str(tmp_path), "--effort", "medium"]
    assert hs.main([*common, "--arm", "baseline"]) == 2
    assert hs.main([*common, "--arm", "all"]) == 2
    fixtures = hs.load_fixtures()

    async def runner(task, model, provider, rep, ws):
        return {"tool_calls": [], "final_text": "", "usage_models": [model], "http_errors": 0, "timeouts": 0}

    base = {"model": "claude-test", "model_policy_version": 1}
    records, manifest = anyio.run(lambda: hs.run_bench(runner, fixtures, ["M"], reps=1, baseline=base,
                                                       include_baseline=False, effort="medium"))
    assert {r["effort"] for r in records} == {"medium"} and manifest["candidate_effort"] == "medium"
    text = hs.render_exploratory(records, manifest, {"timestamp_utc": "t", "catalog_ids": ["M"], "selected": {}})
    assert "`medium`" in text and "| `M` |" in text
    assert hs.main(["bench", "--allow-live", "--arm", "candidate", "--effort", "medium"]) == 2
    base_manifest = {**manifest, "run_id": "b", "arms": ["baseline"], "candidate_effort": "cli-default"}
    with pytest.raises(hs.SpikeError, match="exploratory"):
        hs.merge_runs([([], base_manifest), (records, manifest)])
    with pytest.raises(hs.SpikeError, match="exploratory"):
        hs.merge_runs([([], base_manifest), (records, {**manifest, "candidate_effort": "cli-default"})])


def _policy_bench_setup(tmp_path, monkeypatch, *, available=True, base_url="https://inference.hetzner.com/api"):
    snap = {"selected": {"Qwen/Qwen3.8-27B": {"availability": hs.AVAILABLE if available else hs.UNAVAILABLE}},
            "messages_probe": {"ok": True}}
    (tmp_path / "snapshot-start.json").write_text(json.dumps(snap))
    monkeypatch.setenv("HETZNER_INFERENCE_BASE_URL", base_url)
    monkeypatch.setenv("HETZNER_INFERENCE_API_KEY", "k")
    policy = str(ROOT / "docs" / "spikes" / "825" / "model-policy-qwen38.yaml")
    return ["bench", "--allow-live", "--out-dir", str(tmp_path), "--arm", "candidate",
            "--model-policy", policy, "--policy-task", "bench_candidate"]


def test_committed_825_bench_policy_resolves_to_qwen38_medium_hetzner():
    from config.model_policy import ModelPolicy

    sel = ModelPolicy.load(ROOT / "docs" / "spikes" / "825" / "model-policy-qwen38.yaml").resolve(
        "bench_candidate", log=False)
    assert (sel.model, sel.effort) == ("Qwen/Qwen3.8-27B", "medium")
    assert sel.provider is not None and sel.provider.name == "hetzner"


def test_policy_bench_refusals_happen_before_any_live_call(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("live call attempted")

    monkeypatch.setattr(hs, "make_sdk_runner", boom)
    monkeypatch.setattr(hs, "HetznerClient", boom)
    args = _policy_bench_setup(tmp_path, monkeypatch)

    def swap(flag, value):
        out = list(args)
        out[out.index(flag) + 1] = value
        return out

    assert hs.main([*args, "--effort", "medium"]) == 2
    assert hs.main(swap("--arm", "all")) == 2
    assert hs.main(args[:-2]) == 2  # --policy-task missing
    assert hs.main([*args, "--models", "other"]) == 2
    assert hs.main(swap("--out-dir", str(hs.SPIKE_DIR))) == 2
    monkeypatch.setenv("HETZNER_INFERENCE_BASE_URL", "https://elsewhere.example")
    assert hs.main(args) == 2
    args = _policy_bench_setup(tmp_path, monkeypatch, available=False)
    assert hs.main(args) == 2
    # a policy task that resolves to no provider
    args = _policy_bench_setup(tmp_path, monkeypatch)
    assert hs.main(swap("--policy-task", "service_agent")) == 2


def test_policy_bench_records_effort_source_and_stays_exploratory(tmp_path, monkeypatch):
    args = _policy_bench_setup(tmp_path, monkeypatch)
    ns = hs.argparse.Namespace(model_policy=args[args.index("--model-policy") + 1], policy_task="bench_candidate",
                               effort=None, arm="candidate")
    snap = json.loads((tmp_path / "snapshot-start.json").read_text())
    model, effort, info = hs._policy_candidate(ns, snap)
    assert (model, effort) == ("Qwen/Qwen3.8-27B", "medium") and len(info["model_policy_sha256"]) == 64
    fixtures = hs.load_fixtures()

    async def runner(task, m, provider, rep, ws):
        return {"tool_calls": [], "final_text": "", "usage_models": [m], "http_errors": 0, "timeouts": 0}

    base = {"model": "claude-test", "model_policy_version": 1}
    records, manifest = anyio.run(lambda: hs.run_bench(
        runner, fixtures, [model], reps=1, baseline=base, include_baseline=False, effort=effort,
        effort_source="policy", policy_info=info))
    assert {(r["effort"], r["effort_source"]) for r in records} == {("medium", "policy")}
    assert manifest["candidate_effort_source"] == "policy" and manifest["model_policy_sha256"]
    with pytest.raises(hs.SpikeError, match="exploratory"):
        hs._refuse_exploratory(records, [manifest])


def test_count_http_400():
    recs = [{"error": "api_error_status=400"}, {"error": "api_error_status=429"}, {"error": None}, {}]
    assert hs.count_http_400(recs) == 1
