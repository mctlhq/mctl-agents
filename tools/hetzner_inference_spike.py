#!/usr/bin/env python3
"""Benchmark harness for mctlhq/mctl-agents#791: would Hetzner's experimental inference
service (https://experiments.hetzner.com) work behind the current MCTL agent path?

    HETZNER_INFERENCE_BASE_URL=... HETZNER_INFERENCE_API_KEY=... \\
      uv run python tools/hetzner_inference_spike.py snapshot --allow-live --deep
    uv run python tools/hetzner_inference_spike.py bench --allow-live --out-dir docs/spikes/791
    uv run python tools/hetzner_inference_spike.py report --dir docs/spikes/791

Rules (design.md of the proposal):
- Every live call needs `--allow-live`; without it nothing touches the network. Nothing runs in CI.
- One agent loop: sessions are built by the real `orchestrator/options.py` builders and run through
  `claude_agent_sdk`, so `_policy_hooks` stays in force. The only candidate differences are the
  model and `hetzner_session_env`.
- Matching is exact-ID only. A model absent from the catalog is `UNAVAILABLE_AT_RUN_TIME`; a provider
  failure is recorded against its cell and never retried on another model.
- Artifacts reach the repo only after `contains_credential` passes. Raw transcripts go only to
  `--record-raw DIR`, which must be outside any repository.
- Nothing under `orchestrator/` imports this module.
- PRECONDITION: candidate cells keep Bash on the machine running `bench`. The per-cell HOME swap does
  not stop them reading absolute paths (e.g. /home/<op>/.aws/credentials, ~/.config/gh, .env files),
  and their output goes to Hetzner as tool results. Run `bench` only on a throwaway host or dedicated
  user that holds no credentials and cannot read any operator home.
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
import yaml  # noqa: E402

from orchestrator.redaction import contains_credential  # noqa: E402

SPIKE_DIR = _REPO_ROOT / "docs" / "spikes" / "791"
SELECTED_MODELS = (
    "Kimi-K2.7-Code",
    "DeepSeek-V4-Flash-0731",
    "GLM-5.2-NVFP4",
    "Qwen/Qwen3.6-35B-A3B-FP8",
)
AVAILABLE, UNAVAILABLE = "AVAILABLE", "UNAVAILABLE_AT_RUN_TIME"
TASKS = ("investigation", "implementation", "review", "tool-use")
#: Every task resolves through this config/model-policy.yaml task (all four run on the service-agent profile).
POLICY_TASK = "service_agent"
BASELINE_PROVIDER, CANDIDATE_PROVIDER = "anthropic-baseline", "hetzner"
DEFAULT_REPS = 3
HTTP_TIMEOUT_S = 60.0
MAX_429_RETRIES = 2
MAX_ERROR_CHARS = 300
DECISIONS = ("ADOPT_EXPERIMENTAL_PROVIDER", "LIMITED_BENCH_ONLY", "REJECT")

#: Env that must not reach a third-party endpoint (or a model-driven Bash).
_BLANKED_ENV = (
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
    "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_SECONDARY",
    "GITHUB_TOKEN", "GH_TOKEN",
)
_CANDIDATE_ENV_ALLOW = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR"})
#: Recorded in the run manifest: how candidate cells differ from the baseline besides the model.
CANDIDATE_ISOLATION = {"env": "allowlist " + ",".join(sorted(_CANDIDATE_ENV_ALLOW)) + " + ANTHROPIC_* overrides",
                       "home": "per-cell empty temp dir; this only changes what ~ expands to and is NOT isolation: "
                               "candidate Bash can read absolute paths, so run bench ONLY on a throwaway host/user "
                               "with no credentials and no readable operator home",
                       "mcp_servers": "dropped (no live mctl MCP server for candidate cells); mcp__* calls are "
                                      "rejected by the CLI and scored as denied, not as policy bypasses"}
_MODEL_SLOTS = (
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
)
_ISOLATION_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


class SpikeError(ValueError):
    """Invalid or unsafe input to the harness."""


# --------------------------------------------------------------------------
# artifact safety
# --------------------------------------------------------------------------


def check_raw_dir(directory: Path) -> Path:
    """`--record-raw` must never point into a repository."""
    resolved = directory.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists() or parent == _REPO_ROOT:
            raise SpikeError(f"--record-raw {directory} is inside a repository")
    return resolved


def write_artifact(path: Path, text: str) -> None:
    """The only way this tool writes into the repo: refuses credential-shaped content."""
    if contains_credential(text):
        raise SpikeError(f"refusing to write {path}: credential-shaped content")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def render_jsonl(records: Sequence[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: str) -> str:
    return text[:MAX_ERROR_CHARS]


# --------------------------------------------------------------------------
# session env
# --------------------------------------------------------------------------


def hetzner_session_env(env: Mapping[str, str], *, base_url: str, token: str, model: str) -> dict[str, str]:
    """Candidate-session env transform, applied AFTER `orchestrator.options._scrubbed`.

    Points the CLI at `base_url`, pins every model slot it may use to `model` (no hidden Anthropic
    call, no fallback) and blanks every Anthropic credential, the GitHub token and the mctl/usage/
    evidence tokens so none reaches a third party.
    """
    from orchestrator.evidence_producer import agent_env_without_evidence_token
    from orchestrator.options import agent_env_without_mctl_token
    from orchestrator.usage_ledger import agent_env_without_writer_token

    # Allowlist, not blocklist: nothing outside _CANDIDATE_ENV_ALLOW reaches a model-driven Bash.
    kept = {k: v for k, v in env.items() if k in _CANDIDATE_ENV_ALLOW}
    out = agent_env_without_mctl_token(agent_env_without_evidence_token(agent_env_without_writer_token(kept)))
    out.update({name: "" for name in _BLANKED_ENV})
    out["HOME"] = tempfile.mkdtemp(prefix="spike791-cell-home-")
    out.update(_ISOLATION_ENV)
    out.update({slot: model for slot in _MODEL_SLOTS})
    out["ANTHROPIC_BASE_URL"] = base_url
    out["ANTHROPIC_AUTH_TOKEN"] = token
    return out


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

_RATE_HEADERS = re.compile(r"(?i)^(x-ratelimit-.*|retry-after)$")
_TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}


def classify_catalog(catalog_ids: Sequence[str], selected: Sequence[str] = SELECTED_MODELS) -> dict[str, str]:
    """Exact-ID match only: no fuzzy, prefix or substitute matching."""
    ids = set(catalog_ids)
    return {m: AVAILABLE if m in ids else UNAVAILABLE for m in selected}


class HetznerClient:
    """Tiny OpenAI-compatible client for the snapshot. Retries 429 only, counted, max `MAX_429_RETRIES`."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)
        self.retries = 0

    def call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One request; the result is always a plain, screened record (never raises on HTTP errors)."""
        started = time.monotonic()
        for attempt in range(MAX_429_RETRIES + 1):
            try:
                resp = self._client.request(
                    method, f"{self.base_url}{path}", json=body,
                    headers={"Authorization": f"Bearer {self._key}"})
            except httpx.HTTPError as exc:
                return {"status": None, "error": type(exc).__name__, "latency_ms": _ms(started),
                        "retries": self.retries}
            if resp.status_code == 429 and attempt < MAX_429_RETRIES:
                self.retries += 1
                try:
                    time.sleep(min(float(resp.headers.get("retry-after", "1")), 30.0))
                except ValueError:
                    time.sleep(1.0)
                continue
            break
        try:
            parsed: Any = resp.json()
        except ValueError:
            parsed = None
        error = None if resp.is_success else _clip(resp.text)
        if error is not None and contains_credential(error):
            error = "[redacted]"
        return {"status": resp.status_code, "json": parsed, "error": error, "latency_ms": _ms(started),
                "headers": {k.lower(): v for k, v in resp.headers.items() if _RATE_HEADERS.match(k)},
                "retries": self.retries}

    def stream(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """One streamed POST, no retry. Records the SSE framing (event/chunk types, counts, time to first
        event), never the streamed content."""
        started = time.monotonic()
        types: list[str] = []
        ttfe = None
        try:
            with self._client.stream("POST", f"{self.base_url}{path}", json=body,
                                     headers={"Authorization": f"Bearer {self._key}"}) as resp:
                ctype = resp.headers.get("content-type", "")
                if not resp.is_success:
                    resp.read()
                    error = _clip(resp.text)
                    return {"status": resp.status_code, "content_type": ctype, "latency_ms": _ms(started),
                            "error": "[redacted]" if contains_credential(error) else error}
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    ttfe = ttfe if ttfe is not None else _ms(started)
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        types.append("[DONE]")
                        continue
                    try:
                        event = json.loads(payload)
                    except ValueError:
                        types.append("unparsable")
                        continue
                    kind = str(event.get("type") or event.get("object") or "?")
                    delta = (event.get("delta") or {}) if isinstance(event.get("delta"), dict) else {}
                    if delta.get("type"):
                        kind += f":{delta['type']}"
                    choices = event.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        cdelta = choices[0].get("delta") or {}
                        kind += ":" + ",".join(sorted(k for k in cdelta if cdelta.get(k) not in (None, "", [])))
                    types.append(kind)
        except httpx.HTTPError as exc:
            return {"status": None, "error": type(exc).__name__, "latency_ms": _ms(started)}
        counts: dict[str, int] = defaultdict(int)
        for t in types:
            counts[t] += 1
        return {"status": resp.status_code, "content_type": ctype, "error": None, "latency_ms": _ms(started),
                "time_to_first_event_ms": ttfe, "events": len(types), "event_types": dict(sorted(counts.items()))}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _completion_summary(r: Mapping[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = r["json"] if isinstance(r.get("json"), dict) else {}
    choice = (body.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {"status": r.get("status"), "error": r.get("error"), "latency_ms": r.get("latency_ms"),
            "finish_reason": choice.get("finish_reason"), "usage": body.get("usage"),
            "model": body.get("model"), "tool_calls": msg.get("tool_calls") or [],
            "headers": r.get("headers", {})}


_MSG_TOOL = {"name": "get_weather", "description": "Weather for a city.",
             "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
#: Long-context probe: real public repo files, repeated up to about this many characters (~4 chars per token).
LONG_CONTEXT_CHARS = 240_000


def _long_context_text(fixtures_root: Path = SPIKE_DIR / "fixtures") -> str:
    spec = yaml.safe_load((fixtures_root / "investigation" / "fixture.yaml").read_text())
    pad = "".join(f"\n# file: {p}\n" + (_REPO_ROOT / p).read_text() for p in spec["long_context"]["pad_from"])
    return (pad * (LONG_CONTEXT_CHARS // max(len(pad), 1) + 1))[:LONG_CONTEXT_CHARS]


def _message_summary(r: Mapping[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = r["json"] if isinstance(r.get("json"), dict) else {}
    blocks = [b for b in body.get("content") or [] if isinstance(b, dict)]
    return {"status": r.get("status"), "error": r.get("error"), "latency_ms": r.get("latency_ms"),
            "type": body.get("type"), "stop_reason": body.get("stop_reason"), "usage": body.get("usage"),
            "model": body.get("model"), "block_types": [b.get("type") for b in blocks],
            "headers": r.get("headers", {})}


def protocol_probes(client: HetznerClient, model: str) -> dict[str, Any]:
    """Which wire protocols carry what (recorded for #791 and reused by #796): Messages tool_use round
    trip, Messages streaming (text and tool_use), Chat Completions streaming. Content is never recorded."""
    ask = [{"role": "user", "content": "What is the weather in Berlin? Use the tool."}]
    first = client.call("POST", "/v1/messages", {"model": model, "max_tokens": 1024, "tools": [_MSG_TOOL],
                                                 "messages": ask})
    out: dict[str, Any] = {"messages_tool_use": _message_summary(first)}
    content = (first.get("json") or {}).get("content") if isinstance(first.get("json"), dict) else None
    tool_use = next((b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_use"), None)
    if tool_use is not None:
        out["messages_tool_result"] = _message_summary(client.call("POST", "/v1/messages", {
            "model": model, "max_tokens": 1024, "tools": [_MSG_TOOL],
            "messages": [*ask, {"role": "assistant", "content": content},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use.get("id"),
                                                       "content": "12 C, light rain"}]}]}))
    out["messages_stream_text"] = client.stream("/v1/messages", {
        "model": model, "max_tokens": 256, "stream": True,
        "messages": [{"role": "user", "content": "Reply with: ok"}]})
    out["messages_stream_tool_use"] = client.stream("/v1/messages", {
        "model": model, "max_tokens": 1024, "stream": True, "tools": [_MSG_TOOL], "messages": ask})
    out["chat_stream_tool_calls"] = client.stream("/v1/chat/completions", {
        "model": model, "max_tokens": 1024, "stream": True, "tools": [_TOOL], "messages": ask})
    return out


def long_context_probe(client: HetznerClient, model: str) -> dict[str, Any]:
    """One Messages request at a practical MCTL prompt size (~60k tokens of real repo files)."""
    text = _long_context_text()
    question = "\n\nQuestion: which Python function names appear in the first file above? Name two."
    r = client.call("POST", "/v1/messages", {"model": model, "max_tokens": 512,
                                             "messages": [{"role": "user", "content": text + question}]})
    return {"prompt_chars": len(text), **_message_summary(r)}


def take_snapshot(client: HetznerClient, *, base_url: str, selected: Sequence[str] = SELECTED_MODELS,
                  extra: Sequence[str] = (), deep: bool = False) -> dict[str, Any]:
    """Live catalog and capability snapshot. No substitution: an absent model stays unavailable.

    `extra` are models the owner added at run time (exact catalog IDs); they are probed the same way and
    flagged `preregistered: false`. `deep` adds the protocol and long-context probes per available model.
    """
    models = client.call("GET", "/v1/models")
    body = models.get("json")
    ids = [m.get("id") for m in (body or {}).get("data", []) if isinstance(m, dict)] if isinstance(body, dict) else []
    probe_list = [*selected, *(m for m in extra if m not in selected)]
    availability = classify_catalog([i for i in ids if isinstance(i, str)], probe_list)
    per_model: dict[str, Any] = {}
    for model, state in availability.items():
        entry: dict[str, Any] = {"availability": state, "preregistered": model in selected}
        if state == AVAILABLE:
            entry["trivial"] = _completion_summary(client.call("POST", "/v1/chat/completions", {
                "model": model, "max_tokens": 32, "messages": [{"role": "user", "content": "Reply with: ok"}]}))
            tool = _completion_summary(client.call("POST", "/v1/chat/completions", {
                "model": model, "max_tokens": 256, "tools": [_TOOL], "tool_choice": "auto",
                "messages": [{"role": "user", "content": "What is the weather in Berlin? Use the tool."}]}))
            entry["function_call"] = {**tool, "tool_call_returned": bool(tool["tool_calls"])}
            if deep:
                entry["protocols"] = protocol_probes(client, model)
                entry["long_context"] = long_context_probe(client, model)
        per_model[model] = entry
    first = next((m for m, s in availability.items() if s == AVAILABLE), None)
    probe: dict[str, Any] = {"ok": False, "model": first}
    if first:
        r = client.call("POST", "/v1/messages", {"model": first, "max_tokens": 32,
                                                 "messages": [{"role": "user", "content": "Reply with: ok"}]})
        data: dict[str, Any] = r["json"] if isinstance(r.get("json"), dict) else {}
        probe.update(status=r.get("status"), error=r.get("error"), headers=r.get("headers", {}),
                     ok=r.get("status") == 200 and data.get("type") == "message")
    return {"schema": "hetzner-snapshot/v1", "timestamp_utc": _utc_now(), "endpoint": base_url,
            "models_status": models.get("status"), "models_error": models.get("error"),
            "catalog_ids": sorted(i for i in ids if isinstance(i, str)),
            "selected": per_model, "messages_probe": probe, "retries": client.retries}


# --------------------------------------------------------------------------
# fixtures and oracles
# --------------------------------------------------------------------------


def fixture_sha256(workspace: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in workspace.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(path.relative_to(workspace)).encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()


def load_fixture(directory: Path) -> dict[str, Any]:
    spec = yaml.safe_load((directory / "fixture.yaml").read_text())
    spec["dir"] = directory
    spec["workspace"] = directory / "workspace"
    actual = fixture_sha256(spec["workspace"])
    if spec.get("sha256") != actual:
        raise SpikeError(f"fixture {spec.get('id')}: sha256 {spec.get('sha256')} != workspace {actual}")
    return spec


def load_fixtures(root: Path = SPIKE_DIR / "fixtures") -> dict[str, dict[str, Any]]:
    out = {}
    for task in TASKS:
        spec = load_fixture(root / task)
        out[spec["task"]] = spec
    return out


def _finding_lines(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"(?im)^\s*FINDING:\s*(.+)$", text)]


def score_oracle(fixture: Mapping[str, Any], final_text: str, tool_calls: Sequence[Mapping[str, Any]],
                 workspace: Path | None = None) -> dict[str, Any]:
    """Deterministic scoring of provider output. The output is data, never followed."""
    oracle = fixture["oracle"]
    kind = oracle["kind"]
    result: dict[str, Any] = {"oracle_result": "fail"}
    if kind == "facts":
        ok = all(f in final_text for f in oracle["required"]) and all(c in final_text for c in oracle["cite"])
        result["oracle_result"] = "pass" if ok else "fail"
    elif kind == "findings":
        lines = [ln.lower() for ln in _finding_lines(final_text)]
        caught = {fid for fid, kws in oracle["seeded"].items() if any(k.lower() in ln for ln in lines for k in kws)}
        flagged_seeded = lambda ln: any(k.lower() in ln for kws in oracle["seeded"].values() for k in kws)  # noqa: E731
        false_pos = sum(1 for ln in lines if not flagged_seeded(ln))
        missed = len(oracle["seeded"]) - len(caught)
        result.update(findings_caught=len(caught), findings_missed=missed, false_positives=false_pos,
                      oracle_result="pass" if missed == 0 and false_pos == 0 else "fail")
    elif kind == "pytest":
        if workspace is None:
            raise SpikeError("pytest oracle needs the session workspace")
        pristine = fixture["workspace"]
        changed = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*")
                         if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
                         and (not (pristine / p.relative_to(workspace)).exists()
                              or (pristine / p.relative_to(workspace)).read_bytes() != p.read_bytes()))
        in_scope = all(c in oracle["allowed_changes"] for c in changed)
        passed = False
        if in_scope:  # never execute model-written code outside the permitted files
            with tempfile.TemporaryDirectory(prefix="spike791-oracle-home-") as home:
                try:
                    proc = subprocess.run(  # noqa: S603
                        oracle["command"], cwd=workspace, capture_output=True, text=True, timeout=120,
                        env={"PATH": os.environ.get("PATH", ""), "HOME": home, "PYTHONPATH": str(workspace)},
                        check=False)
                    passed = proc.returncode == 0
                except subprocess.TimeoutExpired:
                    passed = False
        result.update(tests_passed=passed, changed_files=changed,
                      oracle_result="pass" if passed and in_scope else "fail")
    elif kind == "answer":
        used = {c["name"] for c in tool_calls}
        bypass = [c for c in tool_calls if c["name"] in oracle["disallowed_tools"] and not c.get("denied")]
        ok = oracle["expect"] in final_text and set(oracle["required_tools"]) <= used and not bypass
        result.update(policy_bypasses=len(bypass), oracle_result="pass" if ok else "fail")
    else:
        raise SpikeError(f"unknown oracle kind {kind!r}")
    return result


# --------------------------------------------------------------------------
# cells
# --------------------------------------------------------------------------


def tool_call_stats(calls: Sequence[Mapping[str, Any]], allowed_tools: Sequence[str]) -> dict[str, int]:
    """valid = allowed and not denied; invalid = known but denied; hallucinated = not in the catalogue."""
    valid = invalid = hallucinated = 0
    for c in calls:
        if not any(fnmatch.fnmatchcase(c["name"], pat) for pat in allowed_tools):
            hallucinated += 1
        elif c.get("denied"):
            invalid += 1
        else:
            valid += 1
    return {"tool_calls_valid": valid, "tool_calls_invalid": invalid, "tool_calls_hallucinated": hallucinated}


def _models_ok(base: Mapping[str, Any], models: set[str]) -> bool:
    """Candidates must use only their model; the baseline (unpinned slots) may add side-request models."""
    if base.get("provider") == BASELINE_PROVIDER:
        return base["model"] in models
    return models == {base["model"]}


def finalize_cell(base: Mapping[str, Any], obs: Mapping[str, Any], fixture: Mapping[str, Any],
                  allowed_tools: Sequence[str], workspace: Path | None) -> dict[str, Any]:
    """One JSONL record: the session observation plus the oracle verdict."""
    rec: dict[str, Any] = {**base, "oracle_result": "fail", "future_cost": "unknown"}
    calls = obs.get("tool_calls", [])
    if base.get("provider") != BASELINE_PROVIDER:
        # candidate cells have no MCP servers: the CLI rejects mcp__* calls before any hook runs
        calls = [{**c, "denied": True} if str(c["name"]).startswith("mcp__") else c for c in calls]
    rec.update(tool_call_stats(calls, allowed_tools))
    names: dict[str, int] = defaultdict(int)
    for c in calls:
        names[str(c["name"])] += 1
    rec["tool_names"] = dict(sorted(names.items()))
    for key in ("turns", "retries", "input_tokens", "output_tokens", "reasoning_tokens", "ttft_ms", "wall_ms",
                "http_errors", "http_429", "timeouts", "finish_reasons", "context_limit_hit", "cost_usd_current",
                "policy_denials"):
        rec[key] = obs.get(key)
    rec["policy_bypasses"] = 0
    status = "ok"
    if obs.get("error") or (obs.get("http_errors") or 0) or (obs.get("timeouts") or 0):
        status = "provider_error"
    models = set(obs.get("usage_models") or [])
    if status == "ok" and not _models_ok(base, models):
        status = "model_mismatch"
    rec["observed_models"] = sorted(models)
    if status == "ok":
        rec.update(score_oracle(fixture, obs.get("final_text", ""), calls, workspace))
    rec["status"] = status
    if status != "ok":
        rec["oracle_result"] = "fail"
        rec["error"] = _clip(str(obs.get("error") or status))
    return rec


#: (task, model, provider, repetition, workspace) -> observation dict
CellRunner = Callable[[str, str, str, int, Path], Awaitable[dict[str, Any]]]


def capture_baseline() -> dict[str, Any]:
    """Everything the baseline is, captured at bench start."""
    from config.model_policy import resolve_model

    sel = resolve_model(POLICY_TASK, log=False)
    policy_version = yaml.safe_load((_REPO_ROOT / "config" / "model-policy.yaml").read_text())["version"]
    manifests: dict[str, Any] = {}
    for path in sorted((_REPO_ROOT / "agents" / "_manifests").glob("*/agent.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        manifests[path.parent.name] = {"version": (data.get("metadata") or {}).get("version", data.get("version")),
                                       "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"policy_task": POLICY_TASK, "profile": sel.profile, "model": sel.model, "source": sel.source,
            "model_policy_version": policy_version, "agent_manifests": manifests}


def _repo_sha() -> str:
    if os.environ.get("SPIKE791_REPO_SHA"):  # a run from a `git archive` export has no .git
        return os.environ["SPIKE791_REPO_SHA"]
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, capture_output=True, text=True, check=False)  # noqa: S607
    return proc.stdout.strip() or "unknown"


def cell_options(
    task: str, model: str, workspace: Path,
    env_transform: Callable[[Mapping[str, str]], dict[str, str]] | None,
) -> Any:
    """Options for one cell from the real builders: the same builder, tools and hooks for baseline and
    candidate. Only `model` and (candidate) the env differ."""
    from orchestrator import options as opt

    scratch = Path(tempfile.mkdtemp(prefix="spike791-proposal-"))
    if task == "implementation":
        built = opt.build_implementer_agent_options(workspace, model, None)
    else:
        built = opt.build_issue_investigator_options(workspace, model, scratch)
        if task in ("review", "tool-use"):
            built.disallowed_tools = [*(built.disallowed_tools or []), "Write", "Edit"]
    if env_transform is not None:
        built.env = env_transform(built.env or {})
        built.mcp_servers = {}
    return built


class _DenialCounter:
    """Wraps PreToolUse hooks to count and remember the calls the policy checkpoint denied."""

    def __init__(self) -> None:
        self.denied: dict[str, int] = defaultdict(int)
        self.denied_ids: set[str] = set()
        self._seen: set[Any] = set()

    def wrap(self, options: Any) -> None:
        for matchers in (options.hooks or {}).values():
            for matcher in matchers:
                matcher.hooks = [self._wrap(h) for h in matcher.hooks]

    def _wrap(self, hook: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[dict[str, Any]]]:
        async def inner(input_data: Any, tool_use_id: Any, context: Any) -> dict[str, Any]:
            out = await hook(input_data, tool_use_id, context)
            if (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny":
                # several hooks may deny the same call: count each tool_use once
                if tool_use_id is None or tool_use_id not in self._seen:
                    self._seen.add(tool_use_id)
                    if tool_use_id is not None:
                        self.denied_ids.add(str(tool_use_id))
                    self.denied[str((input_data or {}).get("tool_name", ""))] += 1
            return out
        return inner


async def sdk_session(options: Any, prompt: str, max_turns: int, max_budget: float) -> dict[str, Any]:
    """Run one session exactly as the run_* entry points do and summarise it."""
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, ToolUseBlock

    options.max_turns = max_turns
    options.max_budget_usd = max_budget
    counter = _DenialCounter()
    counter.wrap(options)
    obs: dict[str, Any] = {"tool_calls": [], "final_text": "", "http_errors": 0, "http_429": 0, "timeouts": 0}
    started = time.monotonic()
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    if obs.get("ttft_ms") is None:
                        obs["ttft_ms"] = _ms(started)
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            obs["tool_calls"].append({"name": block.name, "id": block.id})
                elif isinstance(message, ResultMessage):
                    usage = message.usage or {}
                    obs.update(final_text=message.result or "", turns=message.num_turns,
                               input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                               reasoning_tokens=None, cost_usd_current=message.total_cost_usd,
                               usage_models=list((message.model_usage or {}).keys()),
                               finish_reasons=[message.stop_reason] if message.stop_reason else [])
                    if message.is_error:
                        obs["http_errors"] += 1
                        obs["http_429"] += int(message.api_error_status == 429)
                        obs["error"] = f"api_error_status={message.api_error_status}"
    except Exception as exc:  # noqa: BLE001 — recorded against the cell, never retried elsewhere
        obs["error"] = type(exc).__name__
        obs["timeouts"] += int("Timeout" in type(exc).__name__)
    obs["wall_ms"] = _ms(started)
    for call in obs["tool_calls"]:
        if call.get("id") is not None and str(call["id"]) in counter.denied_ids:
            call["denied"] = True
    obs["policy_denials"] = sum(1 for c in obs["tool_calls"] if c.get("denied"))
    obs["retries"] = 0
    return obs


def make_sdk_runner(fixtures: Mapping[str, Mapping[str, Any]], *, base_url: str, token: str,
                    effort: str | None = None) -> CellRunner:
    """`effort` (candidate cells only) passes `--effort` to the CLI; None keeps the CLI default."""
    async def run(task: str, model: str, provider: str, rep: int, workspace: Path) -> dict[str, Any]:
        fx = fixtures[task]
        transform = None
        if provider == CANDIDATE_PROVIDER:
            def transform(env: Mapping[str, str], _m: str = model) -> dict[str, str]:
                return hetzner_session_env(env, base_url=base_url, token=token, model=_m)
        options = cell_options(task, model, workspace, transform)
        if provider == CANDIDATE_PROVIDER and effort is not None:
            options.effort = effort
        run.last_allowed = list(options.allowed_tools or [])  # type: ignore[attr-defined]
        return await sdk_session(options, fx["prompt"], fx["limits"]["max_turns"], fx["limits"]["budget_usd"])
    run.last_allowed = []  # type: ignore[attr-defined]
    return run


async def run_bench(runner: CellRunner, fixtures: Mapping[str, Mapping[str, Any]], available: Sequence[str],
                    *, reps: int = DEFAULT_REPS, baseline: Mapping[str, Any] | None = None,
                    allowed_tools: Sequence[str] = ("*",), protocol_path: str = "messages",
                    run_id: str | None = None, include_baseline: bool = True,
                    preregistered: Sequence[str] = SELECTED_MODELS,
                    effort: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The matrix: baseline plus each available model, every task, `reps` repetitions. A failed
    cell stays failed: no cell is retried on another model. `include_baseline=False` / `available=[]`
    run one arm alone (the two arms may need different hosts); `merge` joins them."""
    baseline = baseline if baseline is not None else capture_baseline()
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    started_utc = _utc_now()
    repo_sha = _repo_sha()
    subjects = [(BASELINE_PROVIDER, baseline["model"], "n/a")] if include_baseline else []
    subjects += [(CANDIDATE_PROVIDER, m, protocol_path) for m in available]
    records: list[dict[str, Any]] = []
    for task in TASKS:
        fx = fixtures[task]
        for provider, model, path in subjects:
            for rep in range(1, reps + 1):
                with tempfile.TemporaryDirectory(prefix="spike791-") as tmp:
                    ws = Path(tmp) / "workspace"
                    shutil.copytree(fx["workspace"], ws)
                    obs = await runner(task, model, provider, rep, ws)
                    allowed = getattr(runner, "last_allowed", None) or allowed_tools
                    base = {"run_id": run_id, "task": task, "fixture_id": fx["id"], "fixture_sha256": fx["sha256"],
                            "repo_sha": repo_sha, "model_policy_version": baseline["model_policy_version"],
                            "provider": provider, "model": model, "protocol_path": path, "repetition": rep,
                            "preregistered": provider == BASELINE_PROVIDER or model in preregistered,
                            "effort": effort if provider == CANDIDATE_PROVIDER and effort else "cli-default"}
                    records.append(finalize_cell(base, obs, fx, allowed, ws))
    manifest = {"schema": "hetzner-run-manifest/v1", "run_id": run_id, "repo_sha": repo_sha,
                "started_utc": started_utc, "finished_utc": _utc_now(), "baseline": dict(baseline),
                "candidate_isolation": dict(CANDIDATE_ISOLATION),
                "candidate_effort": effort or "cli-default",
                "available_models": list(available), "arms": (["baseline"] if include_baseline else [])
                + (["candidate"] if available else []),
                "repetitions_requested": reps,
                "repetitions_achieved": {f"{r['task']}|{r['model']}": sum(
                    1 for x in records
                    if x["task"] == r["task"] and x["model"] == r["model"] and x["status"] != "provider_error")
                    for r in records},
                "fixtures": {t: {"id": f["id"], "sha256": f["sha256"], "limits": f["limits"]}
                             for t, f in fixtures.items()}}
    return records, manifest


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def _passes(records: Sequence[Mapping[str, Any]], **where: Any) -> int:
    return sum(1 for r in records if r["oracle_result"] == "pass" and all(r.get(k) == v for k, v in where.items()))


def decide(records: Sequence[Mapping[str, Any]], available: Sequence[str], baseline_model: str,
           tool_use_possible: bool = True) -> tuple[str, str]:
    """The pre-registered rule (design.md), applied mechanically."""
    best_tool_use = max((_passes(records, model=m, task="tool-use") for m in available), default=0)
    if not available or not tool_use_possible or best_tool_use < 2:
        return "REJECT", "no selected model completed tool-use in at least 2 repetitions"
    base_impl = _passes(records, model=baseline_model, task="implementation")
    base_tool = _passes(records, model=baseline_model, task="tool-use")
    for m in available:
        mine = [r for r in records if r["model"] == m]
        provider_failed = sum(1 for r in mine if r["status"] == "provider_error")
        if (_passes(records, model=m, task="implementation") >= base_impl - 1
                and _passes(records, model=m, task="tool-use") >= base_tool - 1
                and not any(r.get("policy_bypasses") for r in mine)
                and mine and provider_failed / len(mine) < 0.10):
            return "ADOPT_EXPERIMENTAL_PROVIDER", f"{m} met every adoption criterion"
    return "LIMITED_BENCH_ONLY", "results fall between the REJECT and ADOPT criteria"


_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_BUDGET_STOPS = frozenset({"max_tokens", "length"})


def model_checklist(records: Sequence[Mapping[str, Any]], entry: Mapping[str, Any]) -> dict[str, str]:
    """The model-specific checks of requirements.md: pass, fail or unknown, from records and snapshot."""
    ok = [r for r in records if r["status"] == "ok"]
    lc = entry.get("long_context") or {}
    lc_res = "unknown" if not lc else ("pass" if lc.get("status") == 200 and lc.get("type") == "message" else "fail")
    if not ok:
        tool_wf = "unknown"
    else:
        tool_wf = "pass" if all(not r.get("tool_calls_hallucinated") for r in ok) and any(
            r.get("tool_calls_valid") for r in ok) else "fail"
    stops = [x for r in ok for x in (r.get("finish_reasons") or [])]
    budget = "unknown" if not ok else ("fail" if any(x in _BUDGET_STOPS for x in stops) else "pass")
    reported = "unknown" if not ok else ("pass" if all(r.get("finish_reasons") for r in ok) else "fail")
    # the tool-use fixture's check.py fails on its first run in every fresh workspace and prints RESULT only
    # on a later run, so a passing tool-use oracle implies the model recovered from that scripted tool error
    tool_use = [r for r in ok if r["task"] == "tool-use"]
    recovers = "unknown" if not tool_use else (
        "pass" if any(r["oracle_result"] == "pass" for r in tool_use) else "fail")
    read_only = [r for r in ok if r["task"] != "implementation"]
    complies = "unknown" if not read_only else (
        "fail" if any(_WRITE_TOOLS & set(r.get("tool_names") or {}) for r in read_only) else "pass")
    return {"long context (~60k tokens) accepted": lc_res, "tool-call output well formed": tool_wf,
            "reasoning does not exhaust output budget": budget, "stop reasons reported": reported,
            "recovers after a tool error": recovers, "complies with read-only instruction": complies}


def _fmt_ms(values: Sequence[Any]) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "n/a"
    return f"{int(statistics.median(vals))} ({min(vals)}-{max(vals)})"


def _protocol_rows(entry: Mapping[str, Any]) -> list[str]:
    rows = []
    for name, res in (entry.get("protocols") or {}).items():
        detail = res.get("stop_reason") or ", ".join(f"{k}x{v}" for k, v in (res.get("event_types") or {}).items())
        blocks = ",".join(res.get("block_types") or [])
        rows.append(f"| {name} | {res.get('status')} | {blocks or 'n/a'} | {detail or res.get('error') or 'n/a'} |")
    return rows


def render_report(
    records: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any], manifest: Mapping[str, Any],
    snapshot_end: Mapping[str, Any] | None = None,
) -> str:
    """The data sections of the spike report, generated from the committed artifacts only."""
    base_model = manifest["baseline"]["model"]
    benched = list(manifest["available_models"])
    prereg = [m for m in benched if (snapshot["selected"].get(m) or {}).get("preregistered", m in SELECTED_MODELS)]
    def tool_ok(models: Sequence[str]) -> bool:
        return any(snapshot["selected"].get(m, {}).get("function_call", {}).get("tool_call_returned") for m in models)

    verdict, why = decide(records, benched, base_model, tool_use_possible=tool_ok(benched))
    strict, strict_why = decide(records, prereg, base_model, tool_use_possible=tool_ok(prereg))
    lines = ["## Environment", "",
             f"- run: `{manifest['run_id']}`, repo sha `{manifest['repo_sha']}`",
             f"- bench window (UTC): {manifest['started_utc']} to {manifest.get('finished_utc', 'n/a')}",
             f"- endpoint: `{snapshot['endpoint']}`",
             f"- baseline: `{base_model}` (model policy v{manifest['baseline']['model_policy_version']}, "
             f"task `{manifest['baseline'].get('policy_task')}`, profile `{manifest['baseline'].get('profile')}`)",
             f"- repetitions requested per (task, model): {manifest['repetitions_requested']}"]
    for arm, where in (manifest.get("run_location") or {}).items():
        lines.append(f"- run location, {arm} arm: {where}")
    lines += ["", "## Snapshot", "", f"Start: {snapshot['timestamp_utc']}, `GET /v1/models` HTTP "
              f"{snapshot['models_status']}. Catalog: "
              + ", ".join(f"`{i}`" for i in snapshot["catalog_ids"]) + ".", ""]
    if snapshot_end is not None:
        added = sorted(set(snapshot_end["catalog_ids"]) - set(snapshot["catalog_ids"]))
        gone = sorted(set(snapshot["catalog_ids"]) - set(snapshot_end["catalog_ids"]))
        lines += [f"End: {snapshot_end['timestamp_utc']}, HTTP {snapshot_end['models_status']}. Catalog drift: "
                  + ("none" if not added and not gone else f"added {added}, removed {gone}") + ".", ""]
    lines += ["| model | pre-registered | availability | served as | chat function call | finish | latency ms |",
              "|---|---|---|---|---|---|---|"]
    for m, e in snapshot["selected"].items():
        fc = e.get("function_call", {})
        lines.append(f"| `{m}` | {'yes' if e.get('preregistered', m in SELECTED_MODELS) else 'no (owner-added)'} | "
                     f"{e['availability']} | {('`' + fc['model'] + '`') if fc.get('model') else 'n/a'} | "
                     f"{fc.get('tool_call_returned', 'n/a')} | {fc.get('finish_reason', 'n/a')} | "
                     f"{fc.get('latency_ms', 'n/a')} |")
    probe = snapshot["messages_probe"]
    lines += ["", "## Compatibility", "",
              f"- `POST /v1/messages` probe (`{probe.get('model')}`): HTTP {probe.get('status')}, "
              + ("Anthropic Messages accepted directly; no shim used." if probe.get("ok")
                 else "not accepted; candidate cells ran through the loopback shim."),
              "- protocol path of the candidate cells: "
              + ", ".join(sorted({str(r['protocol_path']) for r in records if r['provider'] == CANDIDATE_PROVIDER}))]
    if manifest.get("shim_unsupported"):
        lines.append(f"- shim_unsupported: {manifest['shim_unsupported']}")
    for m, e in snapshot["selected"].items():
        if e.get("protocols"):
            lines += ["", f"Protocol probes, `{m}`:", "", "| probe | HTTP | blocks | stop reason / SSE events |",
                      "|---|---|---|---|", *_protocol_rows(e)]
    lines += ["", "## Matrix", "", "Oracle passes / cells; median wall-clock seconds over the cells (range).", "",
              "| model | " + " | ".join(TASKS) + " |", "|---|" + "---|" * len(TASKS)]
    for m in [base_model, *benched]:
        cells = []
        for t in TASKS:
            rs = [r for r in records if r["model"] == m and r["task"] == t]
            walls = [round(r["wall_ms"] / 1000) for r in rs if r.get("wall_ms") is not None]
            cells.append(f"{_passes(rs)}/{len(rs)}; {_fmt_ms(walls)}")
        lines.append(f"| `{m}` | " + " | ".join(cells) + " |")
    lines += ["", "Per-cell detail (status, turns, tokens in/out, tool calls valid/invalid/hallucinated, denials):", "",
              "| model | task | rep | status | oracle | turns | tokens in/out | tools v/i/h | denials | finish |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| `{r['model']}` | {r['task']} | {r['repetition']} | {r['status']} | {r['oracle_result']} | "
                     f"{r.get('turns')} | {r.get('input_tokens')}/{r.get('output_tokens')} | "
                     f"{r.get('tool_calls_valid')}/{r.get('tool_calls_invalid')}/{r.get('tool_calls_hallucinated')} | "
                     f"{r.get('policy_denials')} | {','.join(r.get('finish_reasons') or []) or 'n/a'} |")
    lines += ["", "## Model-specific checks", ""]
    checks = {m: model_checklist([r for r in records if r["model"] == m], snapshot["selected"].get(m, {}))
              for m in benched}
    if checks:
        names = list(next(iter(checks.values())))
        lines += ["| check | " + " | ".join(f"`{m}`" for m in benched) + " |", "|---|" + "---|" * len(benched)]
        lines += [f"| {n} | " + " | ".join(checks[m][n] for m in benched) + " |" for n in names]
    lines += ["", "## Reliability", ""]
    for m in [base_model, *benched]:
        rs = [r for r in records if r["model"] == m]
        lines.append(f"- `{m}`: {len(rs)} cells, provider_error {sum(r['status'] == 'provider_error' for r in rs)}, "
                     f"model_mismatch {sum(r['status'] == 'model_mismatch' for r in rs)}, "
                     f"HTTP 429 {sum(r.get('http_429') or 0 for r in rs)}, "
                     f"timeouts {sum(r.get('timeouts') or 0 for r in rs)}, "
                     f"policy bypasses {sum(r.get('policy_bypasses') or 0 for r in rs)}, "
                     f"policy denials {sum(r.get('policy_denials') or 0 for r in rs)}, "
                     f"achieved repetitions per task "
                     + ", ".join(f"{t} {manifest['repetitions_achieved'].get(f'{t}|{m}', 0)}" for t in TASKS))
    lines += ["", "## Rule outcome", "",
              f"- over every benched candidate ({', '.join(f'`{m}`' for m in benched) or 'none'}): "
              f"**{verdict}** ({why})",
              f"- over the pre-registered selection only ({', '.join(f'`{m}`' for m in prereg) or 'none'}): "
              f"**{strict}** ({strict_why})", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _live_env(args: argparse.Namespace) -> tuple[str, str]:
    if not args.allow_live:
        raise SpikeError("live calls need --allow-live")
    base, key = os.environ.get("HETZNER_INFERENCE_BASE_URL", ""), os.environ.get("HETZNER_INFERENCE_API_KEY", "")
    if not base or not key:
        raise SpikeError("HETZNER_INFERENCE_BASE_URL and HETZNER_INFERENCE_API_KEY must be set in the environment")
    return base, key


def _cmd_snapshot(args: argparse.Namespace) -> int:
    base, key = _live_env(args)
    extra = [m for m in (args.models or "").split(",") if m]
    snap = take_snapshot(HetznerClient(base, key), base_url=base, extra=extra, deep=args.deep)
    write_artifact(Path(args.out), json.dumps(snap, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out}")
    return 0


def _upstream_json(res: Mapping[str, Any]) -> dict[str, Any]:
    """Upstream failures are provider errors (shim answers 502), never shim translation gaps (501)."""
    status = res.get("status")
    if not isinstance(status, int) or not 200 <= status < 300:
        raise RuntimeError(f"upstream status {status}")
    return res.get("json") or {}


def _load_shim() -> Any:
    spec = importlib.util.spec_from_file_location("spike791_shim", SPIKE_DIR / "shim.py")
    if spec is None or spec.loader is None:
        raise SpikeError("cannot load docs/spikes/791/shim.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["spike791_shim"] = module
    spec.loader.exec_module(module)
    return module


def bench_models(snap: Mapping[str, Any], requested: Sequence[str]) -> list[str]:
    """The candidate models for a bench: the snapshot's available ones, or an explicit list of exact IDs
    that the snapshot saw AVAILABLE (never a substitute)."""
    available = [m for m, e in snap["selected"].items() if e["availability"] == AVAILABLE]
    if not requested:
        return available
    missing = [m for m in requested if m not in available]
    if missing:
        raise SpikeError(f"not AVAILABLE in the snapshot (no substitution): {', '.join(missing)}")
    return list(requested)


def _cmd_bench(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    if args.effort and out_dir.resolve() == SPIKE_DIR.resolve():
        raise SpikeError("--effort runs are exploratory: pass an --out-dir other than the pre-registered run's")
    snap = json.loads(Path(args.snapshot or out_dir / "snapshot-start.json").read_text())
    requested = [m for m in (args.models or "").split(",") if m]
    if args.arm == "baseline" and requested:
        raise SpikeError("--models has no effect with --arm baseline")
    available = [] if args.arm == "baseline" else bench_models(snap, requested)
    if args.effort and args.arm != "candidate":
        raise SpikeError("--effort changes candidate cells only; use it with --arm candidate")
    if args.arm == "candidate" and not available:
        raise SpikeError("--arm candidate with no AVAILABLE model: nothing to run")
    # the baseline arm talks only to Anthropic, so it needs neither the Hetzner key nor the endpoint
    base, key = _live_env(args) if args.arm != "baseline" else ("", "")
    if args.arm == "baseline" and not args.allow_live:
        raise SpikeError("live calls need --allow-live")
    if args.record_raw:
        check_raw_dir(Path(args.record_raw))
    fixtures = load_fixtures()
    include_baseline = args.arm in ("all", "baseline")
    suffix = "" if args.arm == "all" else f"-{args.arm}"

    async def go(url: str, token: str, protocol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        runner = make_sdk_runner(fixtures, base_url=url, token=token, effort=args.effort)
        return await run_bench(runner, fixtures, available, reps=args.reps, protocol_path=protocol,
                               include_baseline=include_baseline, effort=args.effort)

    if not available or snap["messages_probe"].get("ok"):
        records, manifest = asyncio.run(go(base, key, "messages"))
    else:
        shim = _load_shim()
        client = HetznerClient(base, key)
        with shim.ShimServer(lambda chat: _upstream_json(client.call("POST", "/v1/chat/completions", chat))) as srv:
            records, manifest = asyncio.run(go(srv.url, srv.token, "shim"))
        manifest["shim_unsupported"] = list(srv.unsupported)
    manifest["run_location"] = {args.arm: args.run_location}
    write_artifact(out_dir / f"records{suffix}.jsonl", render_jsonl(records))
    write_artifact(out_dir / f"run-manifest{suffix}.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(records)} records to {out_dir}")
    return 0


_Run = tuple[list[dict[str, Any]], dict[str, Any]]


def _refuse_exploratory(records: Sequence[dict[str, Any]], manifests: Sequence[dict[str, Any]]) -> None:
    """The pre-registered decision only scores runs at the CLI default effort; an `--effort` run is exploratory."""
    efforts = {m.get("candidate_effort", "cli-default") for m in manifests} | {
        r.get("effort", "cli-default") for r in records}
    if efforts != {"cli-default"}:
        raise SpikeError(f"run has a non-default reasoning effort ({sorted(efforts)}); it is exploratory and "
                         "never feeds decide(): use report --exploratory")


def merge_runs(parts: Sequence[_Run]) -> _Run:
    """Join per-arm bench outputs into one record set and manifest. The arms must share the repo SHA, the
    model policy and the fixtures, so the comparison stays like for like."""
    records = [r for recs, _ in parts for r in recs]
    manifests = [m for _, m in parts]
    _refuse_exploratory(records, manifests)
    arms = [a for m in manifests for a in m.get("arms", [])]
    if len(arms) != len(set(arms)):
        raise SpikeError(f"arms repeat across parts ({arms}); refusing to merge")
    cells = [(r.get("task"), r.get("model"), r.get("repetition")) for r in records]
    if len(cells) != len(set(cells)):
        raise SpikeError("a (task, model, repetition) cell repeats across parts; refusing to merge")
    for key in ("repo_sha", "fixtures"):
        if len({json.dumps(m[key], sort_keys=True) for m in manifests}) != 1:
            raise SpikeError(f"arms differ in {key}; refusing to merge")
    if len({m["baseline"]["model_policy_version"] for m in manifests}) != 1:
        raise SpikeError("arms differ in model_policy_version; refusing to merge")
    merged = {**manifests[0], "run_id": "+".join(m["run_id"] for m in manifests),
              "started_utc": min(m["started_utc"] for m in manifests),
              "finished_utc": max(str(m.get("finished_utc") or m["started_utc"]) for m in manifests),
              "arms": [a for m in manifests for a in m.get("arms", [])],
              "available_models": [x for m in manifests for x in m["available_models"]],
              "repetitions_achieved": {k: v for m in manifests for k, v in m["repetitions_achieved"].items()},
              "run_location": {k: v for m in manifests for k, v in (m.get("run_location") or {}).items()},
              "parts": [{"run_id": m["run_id"], "arms": m.get("arms", []), "started_utc": m["started_utc"],
                         "finished_utc": m.get("finished_utc")} for m in manifests]}
    if any("shim_unsupported" in m for m in manifests):
        merged["shim_unsupported"] = [x for m in manifests for x in m.get("shim_unsupported", [])]
    return records, merged


def _read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _cmd_merge(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    parts = [(_read_records(d / f"records-{arm}.jsonl"), json.loads((d / f"run-manifest-{arm}.json").read_text()))
             for arm in args.arms.split(",")]
    records, manifest = merge_runs(parts)
    write_artifact(d / "records.jsonl", render_jsonl(records))
    write_artifact(d / "run-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"merged {len(records)} records into {d}")
    return 0


def render_exploratory(records: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any],
                       snapshot: Mapping[str, Any]) -> str:
    """Tables for a candidate-only exploratory run. It never feeds `decide()`: the token comes only from
    the pre-registered run."""
    models = list(manifest["available_models"])
    lines = [f"- run: `{manifest['run_id']}`, repo sha `{manifest['repo_sha']}`, window (UTC) "
             f"{manifest['started_utc']} to {manifest.get('finished_utc', 'n/a')}",
             f"- candidate reasoning effort: `{manifest.get('candidate_effort', 'cli-default')}`; everything else "
             "as in the main run (same builders, hooks, fixtures, container isolation)",
             f"- catalog at start ({snapshot['timestamp_utc']}): "
             + ", ".join(f"`{i}`" for i in snapshot["catalog_ids"]),
             "", "| model | " + " | ".join(TASKS) + " |", "|---|" + "---|" * len(TASKS)]
    for m in models:
        cells = []
        for t in TASKS:
            rs = [r for r in records if r["model"] == m and r["task"] == t]
            walls = [round(r["wall_ms"] / 1000) for r in rs if r.get("wall_ms") is not None]
            cells.append(f"{_passes(rs)}/{len(rs)}; {_fmt_ms(walls)}")
        lines.append(f"| `{m}` | " + " | ".join(cells) + " |")
    lines += ["", "| model | task | rep | status | oracle | turns | tokens in/out | tools | finish |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        tools = ", ".join(f"{k} {v}" for k, v in (r.get("tool_names") or {}).items()) or "none"
        lines.append(f"| `{r['model']}` | {r['task']} | {r['repetition']} | {r['status']} | {r['oracle_result']} | "
                     f"{r.get('turns')} | {r.get('input_tokens')}/{r.get('output_tokens')} | {tools} | "
                     f"{','.join(r.get('finish_reasons') or []) or 'n/a'} |")
    checks = {m: model_checklist([r for r in records if r["model"] == m], snapshot["selected"].get(m, {}))
              for m in models}
    if checks:
        names = list(next(iter(checks.values())))
        lines += ["", "| check | " + " | ".join(f"`{m}`" for m in models) + " |", "|---|" + "---|" * len(models)]
        lines += [f"| {n} | " + " | ".join(checks[m][n] for m in models) + " |" for n in names]
    lines.append("")
    for m in models:
        rs = [r for r in records if r["model"] == m]
        lines.append(f"- `{m}`: {len(rs)} cells, provider_error {sum(r['status'] == 'provider_error' for r in rs)}, "
                     f"HTTP 429 {sum(r.get('http_429') or 0 for r in rs)}, "
                     f"timeouts {sum(r.get('timeouts') or 0 for r in rs)}, "
                     f"policy bypasses {sum(r.get('policy_bypasses') or 0 for r in rs)}, "
                     f"policy denials {sum(r.get('policy_denials') or 0 for r in rs)}")
    return "\n".join(lines) + "\n"


def _cmd_report(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    if args.exploratory:
        x = Path(args.exploratory)
        text = render_exploratory(_read_records(x / "records-candidate.jsonl"),
                                  json.loads((x / "run-manifest-candidate.json").read_text()),
                                  json.loads((x / "snapshot.json").read_text()))
        if args.out:
            write_artifact(Path(args.out), text)
        else:
            print(text)
        return 0
    end = d / "snapshot-end.json"
    records, manifest = _read_records(d / "records.jsonl"), json.loads((d / "run-manifest.json").read_text())
    _refuse_exploratory(records, [manifest])
    text = render_report(records, json.loads((d / "snapshot-start.json").read_text()), manifest,
                         json.loads(end.read_text()) if end.exists() else None)
    if args.out:
        write_artifact(Path(args.out), text)
    else:
        print(text)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot", help="live catalog and capability snapshot")
    p.add_argument("--allow-live", action="store_true")
    p.add_argument("--out", default=str(SPIKE_DIR / "snapshot-start.json"))
    p.add_argument("--models", default="", help="comma list of owner-added exact catalog IDs to probe as well")
    p.add_argument("--deep", action="store_true", help="add protocol and long-context probes per available model")
    p.set_defaults(func=_cmd_snapshot)
    p = sub.add_parser("bench", help="run the workload matrix")
    p.add_argument("--allow-live", action="store_true")
    p.add_argument("--out-dir", default=str(SPIKE_DIR))
    p.add_argument("--reps", type=int, default=DEFAULT_REPS)
    p.add_argument("--record-raw", default=None, help="directory OUTSIDE any repository")
    p.add_argument("--snapshot", default=None, help="availability source (default OUT_DIR/snapshot-start.json)")
    p.add_argument("--models", default="", help="comma list of exact IDs, each AVAILABLE in the snapshot")
    p.add_argument("--arm", choices=("all", "baseline", "candidate"), default="all",
                   help="run one arm alone (writes records-ARM.jsonl / run-manifest-ARM.json); join with `merge`")
    p.add_argument("--run-location", default="unspecified", help="recorded in the manifest")
    p.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default=None,
                   help="CLI reasoning effort for candidate cells (exploratory runs; default: CLI default)")
    p.set_defaults(func=_cmd_bench)
    p = sub.add_parser("merge", help="join per-arm bench outputs into records.jsonl / run-manifest.json")
    p.add_argument("--dir", default=str(SPIKE_DIR))
    p.add_argument("--arms", default="baseline,candidate")
    p.set_defaults(func=_cmd_merge)
    p = sub.add_parser("report", help="render the report tables from committed records")
    p.add_argument("--dir", default=str(SPIKE_DIR))
    p.add_argument("--out", default=None)
    p.add_argument("--exploratory", default=None,
                   help="render a candidate-only exploratory run dir (records-candidate.jsonl, "
                        "run-manifest-candidate.json, snapshot.json); never used for the decision")
    p.set_defaults(func=_cmd_report)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SpikeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
