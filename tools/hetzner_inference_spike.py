#!/usr/bin/env python3
"""Benchmark harness for mctlhq/mctl-agents#791: would Hetzner's experimental inference
service (https://experiments.hetzner.com) work behind the current MCTL agent path?

    HETZNER_INFERENCE_BASE_URL=... HETZNER_INFERENCE_API_KEY=... \\
      uv run python tools/hetzner_inference_spike.py snapshot --allow-live --out docs/spikes/791/snapshot.json
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
_CANDIDATE_ENV_ALLOW = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"})
#: Recorded in the run manifest: how candidate cells differ from the baseline besides the model.
CANDIDATE_ISOLATION = {"env": "allowlist " + ",".join(sorted(_CANDIDATE_ENV_ALLOW)) + " + ANTHROPIC_* overrides",
                       "mcp_servers": "dropped (no live mctl MCP server for candidate cells)"}
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


def take_snapshot(client: HetznerClient, *, base_url: str, selected: Sequence[str] = SELECTED_MODELS) -> dict[str, Any]:
    """Live catalog and capability snapshot. No substitution: an absent model stays unavailable."""
    models = client.call("GET", "/v1/models")
    body = models.get("json")
    ids = [m.get("id") for m in (body or {}).get("data", []) if isinstance(m, dict)] if isinstance(body, dict) else []
    availability = classify_catalog([i for i in ids if isinstance(i, str)], selected)
    per_model: dict[str, Any] = {}
    for model, state in availability.items():
        entry: dict[str, Any] = {"availability": state}
        if state == AVAILABLE:
            entry["trivial"] = _completion_summary(client.call("POST", "/v1/chat/completions", {
                "model": model, "max_tokens": 32, "messages": [{"role": "user", "content": "Reply with: ok"}]}))
            tool = _completion_summary(client.call("POST", "/v1/chat/completions", {
                "model": model, "max_tokens": 256, "tools": [_TOOL], "tool_choice": "auto",
                "messages": [{"role": "user", "content": "What is the weather in Berlin? Use the tool."}]}))
            entry["function_call"] = {**tool, "tool_call_returned": bool(tool["tool_calls"])}
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
                proc = subprocess.run(  # noqa: S603
                    oracle["command"], cwd=workspace, capture_output=True, text=True, timeout=120,
                    env={"PATH": os.environ.get("PATH", ""), "HOME": home, "PYTHONPATH": str(workspace)},
                    check=False)
            passed = proc.returncode == 0
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
    rec.update(tool_call_stats(calls, allowed_tools))
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
                            obs["tool_calls"].append({"name": block.name})
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
        if counter.denied.get(call["name"]):
            counter.denied[call["name"]] -= 1
            call["denied"] = True
    obs["policy_denials"] = sum(1 for c in obs["tool_calls"] if c.get("denied"))
    obs["retries"] = 0
    return obs


def make_sdk_runner(fixtures: Mapping[str, Mapping[str, Any]], *, base_url: str, token: str) -> CellRunner:
    async def run(task: str, model: str, provider: str, rep: int, workspace: Path) -> dict[str, Any]:
        fx = fixtures[task]
        transform = None
        if provider == CANDIDATE_PROVIDER:
            def transform(env: Mapping[str, str], _m: str = model) -> dict[str, str]:
                return hetzner_session_env(env, base_url=base_url, token=token, model=_m)
        options = cell_options(task, model, workspace, transform)
        run.last_allowed = list(options.allowed_tools or [])  # type: ignore[attr-defined]
        return await sdk_session(options, fx["prompt"], fx["limits"]["max_turns"], fx["limits"]["budget_usd"])
    run.last_allowed = []  # type: ignore[attr-defined]
    return run


async def run_bench(runner: CellRunner, fixtures: Mapping[str, Mapping[str, Any]], available: Sequence[str],
                    *, reps: int = DEFAULT_REPS, baseline: Mapping[str, Any] | None = None,
                    allowed_tools: Sequence[str] = ("*",), protocol_path: str = "messages",
                    run_id: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The matrix: baseline plus each available model, every task, `reps` repetitions. A failed
    cell stays failed: no cell is retried on another model."""
    baseline = baseline if baseline is not None else capture_baseline()
    run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    repo_sha = _repo_sha()
    subjects = [(BASELINE_PROVIDER, baseline["model"], "n/a")]
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
                            "provider": provider, "model": model, "protocol_path": path, "repetition": rep}
                    records.append(finalize_cell(base, obs, fx, allowed, ws))
    manifest = {"schema": "hetzner-run-manifest/v1", "run_id": run_id, "repo_sha": repo_sha,
                "started_utc": _utc_now(), "baseline": dict(baseline), "candidate_isolation": dict(CANDIDATE_ISOLATION), "available_models": list(available),
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


def render_report(
    records: Sequence[Mapping[str, Any]], snapshot: Mapping[str, Any], manifest: Mapping[str, Any],
) -> str:
    base_model = manifest["baseline"]["model"]
    available = [m for m, e in snapshot["selected"].items() if e["availability"] == AVAILABLE]
    tool_ok = any(e.get("function_call", {}).get("tool_call_returned") for e in snapshot["selected"].values())
    verdict, why = decide(records, available, base_model, tool_use_possible=tool_ok)
    lines = ["# Spike #791: Hetzner experimental inference", "",
             "## Environment", "", f"- run: {manifest['run_id']}, repo sha {manifest['repo_sha']}",
             f"- endpoint: {snapshot['endpoint']}, snapshot {snapshot['timestamp_utc']}",
             f"- baseline: {base_model} (model policy v{manifest['baseline']['model_policy_version']})", "",
             "## Snapshot", "", "| model | availability | function call | finish | latency ms |",
             "|---|---|---|---|---|"]
    for m, e in snapshot["selected"].items():
        fc = e.get("function_call", {})
        lines.append(f"| {m} | {e['availability']} | {fc.get('tool_call_returned', 'n/a')} | "
                     f"{fc.get('finish_reason', 'n/a')} | {fc.get('latency_ms', 'n/a')} |")
    probe = snapshot["messages_probe"]
    lines += ["", "## Compatibility", "",
              "- `POST /v1/messages` probe: "
              + ("ok (direct)" if probe.get("ok") else "failed (loopback shim needed)"), "",
              "## Matrix (oracle passes / repetitions; median wall ms)", "",
              "| model | " + " | ".join(TASKS) + " |", "|---|" + "---|" * len(TASKS)]
    for m in [base_model, *available]:
        cells = []
        for t in TASKS:
            rs = [r for r in records if r["model"] == m and r["task"] == t]
            walls = [r["wall_ms"] for r in rs if r.get("wall_ms") is not None]
            med = int(statistics.median(walls)) if walls else "n/a"
            cells.append(f"{_passes(rs)}/{len(rs)}; {med}")
        lines.append(f"| {m} | " + " | ".join(cells) + " |")
    lines += ["", "## Reliability", ""]
    for m in available:
        rs = [r for r in records if r["model"] == m]
        lines.append(f"- {m}: provider_error {sum(r['status'] == 'provider_error' for r in rs)}, "
                     f"model_mismatch {sum(r['status'] == 'model_mismatch' for r in rs)}, "
                     f"429s {sum(r.get('http_429') or 0 for r in rs)}, "
                     f"policy bypasses {sum(r.get('policy_bypasses') or 0 for r in rs)}")
    lines += ["", "## Decision", "", f"Proposed verdict: {verdict}", "",
              f"Rule outcome: {why}. The owner records the verdict.", ""]
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
    snap = take_snapshot(HetznerClient(base, key), base_url=base)
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


def _cmd_bench(args: argparse.Namespace) -> int:
    base, key = _live_env(args)
    out_dir = Path(args.out_dir)
    if args.record_raw:
        check_raw_dir(Path(args.record_raw))
    snap = json.loads((out_dir / "snapshot.json").read_text())
    available = [m for m, e in snap["selected"].items() if e["availability"] == AVAILABLE]
    fixtures = load_fixtures()

    async def go(url: str, token: str, protocol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        runner = make_sdk_runner(fixtures, base_url=url, token=token)
        return await run_bench(runner, fixtures, available, reps=args.reps, protocol_path=protocol)

    if snap["messages_probe"].get("ok"):
        records, manifest = asyncio.run(go(base, key, "messages"))
    else:
        shim = _load_shim()
        client = HetznerClient(base, key)
        with shim.ShimServer(lambda chat: _upstream_json(client.call("POST", "/v1/chat/completions", chat))) as srv:
            records, manifest = asyncio.run(go(srv.url, srv.token, "shim"))
        manifest["shim_unsupported"] = list(srv.unsupported)
    write_artifact(out_dir / "records.jsonl", render_jsonl(records))
    write_artifact(out_dir / "run-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(records)} records to {out_dir}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    d = Path(args.dir)
    records = [json.loads(line) for line in (d / "records.jsonl").read_text().splitlines() if line.strip()]
    text = render_report(records, json.loads((d / "snapshot.json").read_text()),
                         json.loads((d / "run-manifest.json").read_text()))
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
    p.add_argument("--out", default=str(SPIKE_DIR / "snapshot.json"))
    p.set_defaults(func=_cmd_snapshot)
    p = sub.add_parser("bench", help="run the workload matrix")
    p.add_argument("--allow-live", action="store_true")
    p.add_argument("--out-dir", default=str(SPIKE_DIR))
    p.add_argument("--reps", type=int, default=DEFAULT_REPS)
    p.add_argument("--record-raw", default=None, help="directory OUTSIDE any repository")
    p.set_defaults(func=_cmd_bench)
    p = sub.add_parser("report", help="render the report tables from committed records")
    p.add_argument("--dir", default=str(SPIKE_DIR))
    p.add_argument("--out", default=None)
    p.set_defaults(func=_cmd_report)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SpikeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
