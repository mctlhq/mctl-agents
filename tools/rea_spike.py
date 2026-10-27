#!/usr/bin/env python3
"""Measurement harness for mctlhq/mctl-agents#796: does REA (Reverse Engineer
Anything) improve DevLoop investigations of source-owned fixtures, and do
current Claude models finish such workflows?

    uv run python tools/rea_spike.py --help
    uv run python tools/rea_spike.py run --allow-live --fixture native-codec --arm rea-cli ...
    uv run python tools/rea_spike.py report RECORDS.jsonl

Scope. Operator-run only, on fixtures MCTL built from its own source under
`docs/spikes/796/fixtures/`. Nothing in `orchestrator/` imports this module,
and no commit or CI run uses the live subcommands: they exit non-zero without
`--allow-live`.

`run` prints one JSON record (kind, fixture, arm, model, wall_clock_s, turns,
tool_calls, usage, final, outcome "ok"|"failed", failure_class, and on failure
a bounded `failure` {sha256, reason}); exit 0 when ok, 1 when failed.

Refusals are measurements. A stage is attempted exactly once; on a refusal or
failure the probe records the earliest failing stage and stops. There is no
retry, rephrase or decomposition path in this module.

Like `tools/capability_bench.py`, every function that does I/O is a thin
wrapper around a pure function (`classify`, `score`, `report_markdown`,
`run_probe`) that the unit tests exercise from recorded data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

SPIKE_DIR = _REPO_ROOT / "docs" / "spikes" / "796"

ARMS = ("baseline", "rea-mcp", "rea-cli")
STAGES = ("1-discovery", "2-static", "3-trace", "4-synthesis", "5-implement")

MODEL_POLICY_REFUSAL = "MODEL_POLICY_REFUSAL"
CLIENT_TOOL_ADMISSION = "CLIENT_TOOL_ADMISSION"
MCP_SCHEMA_OR_CONTEXT_FAILURE = "MCP_SCHEMA_OR_CONTEXT_FAILURE"
REA_TOOL_FAILURE = "REA_TOOL_FAILURE"
PROVIDER_RUNTIME_FAILURE = "PROVIDER_RUNTIME_FAILURE"
UNKNOWN = "UNKNOWN"
FAILURE_CLASSES = (
    MODEL_POLICY_REFUSAL,
    CLIENT_TOOL_ADMISSION,
    MCP_SCHEMA_OR_CONTEXT_FAILURE,
    REA_TOOL_FAILURE,
    PROVIDER_RUNTIME_FAILURE,
    UNKNOWN,
)

#: Open models named by the issue owner; their rows are `not-run` unless an
#: existing #791 endpoint is supplied through REA_SPIKE_ALT_BASE_URL.
MATRIX_MODELS = ("kimi-k2.7-code", "deepseek-v4-flash", "glm-5.2", "qwen-3.6")

#: Raw REA output is never persisted beyond this many bytes (plus a sha256).
MAX_SUMMARY_BYTES = 4096

UNOBSERVED = "unobserved"

#: Claude Code injects this notice when its safety classifier withholds part
#: of a response. The session may go on afterwards, so it is not by itself a
#: failed stage, but it must never disappear into an "ok" record.
CLASSIFIER_NOTICE = "stopped by a safety classifier"

SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bounded_summary(text: str, limit: int = MAX_SUMMARY_BYTES) -> dict[str, Any]:
    """Keep at most `limit` bytes of `text` plus the digest of the whole."""
    raw = text.encode("utf-8")
    return {
        "summary": raw[:limit].decode("utf-8", errors="ignore"),
        "bytes": len(raw),
        "truncated": len(raw) > limit,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def classify(signals: Mapping[str, Any]) -> str:
    """Map the observed signals of ONE failed stage to exactly one class.

    Order matters: the most specific, most mechanical cause wins, so a model
    refusal is only reported when nothing else explains the stop.
    """
    if signals.get("tools_registered") is False:
        return CLIENT_TOOL_ADMISSION
    if signals.get("schema_error") or signals.get("context_overflow"):
        return MCP_SCHEMA_OR_CONTEXT_FAILURE
    if signals.get("tool_error") or signals.get("rea_exit_code") not in (None, 0):
        return REA_TOOL_FAILURE
    if signals.get("transport_error"):
        return PROVIDER_RUNTIME_FAILURE
    if signals.get("declined"):
        return MODEL_POLICY_REFUSAL
    return UNKNOWN


def failure_text_record(text: str, store_text: bool) -> dict[str, Any]:
    """Verbatim text only when the operator opted in; otherwise a normalized
    reason (whitespace-collapsed, 200 chars) plus the sha256 of the text."""
    rec: dict[str, Any] = {"sha256": sha256_text(text), "reason": " ".join(text.split())[:200]}
    if store_text:
        rec["text"] = text
    return rec


def npm_provenance_commit(attestations: Mapping[str, Any], integrity: str, repo: str) -> str:
    """Source commit an npm package was built from, read from its registry
    SLSA provenance attestation.

    The statement must name the tarball whose `dist.integrity` is `integrity`
    and a `git+<repo>@...` dependency with a full commit SHA. Anything missing,
    malformed or pointing elsewhere raises ValueError: an unreadable binding is
    never treated as a match. Signature verification is `npm audit signatures`.
    """
    import base64

    algo, _, b64 = integrity.partition("-")
    if algo != "sha512" or not b64:
        raise ValueError(f"unsupported integrity {integrity!r}")
    want = base64.b64decode(b64).hex()
    commits: set[str] = set()
    for att in attestations.get("attestations") or []:
        if att.get("predicateType") != SLSA_PROVENANCE_V1:
            continue
        statement = json.loads(base64.b64decode(att["bundle"]["dsseEnvelope"]["payload"]))
        if not any((s.get("digest") or {}).get("sha512") == want for s in statement.get("subject") or []):
            raise ValueError("provenance subject does not match dist.integrity")
        for dep in (statement.get("predicate") or {}).get("buildDefinition", {}).get("resolvedDependencies") or []:
            commit = (dep.get("digest") or {}).get("gitCommit", "")
            if str(dep.get("uri", "")).startswith(f"git+{repo}@") and re.fullmatch(r"[0-9a-f]{40}", commit):
                commits.add(commit)
    if len(commits) != 1:
        raise ValueError(f"expected one source commit for {repo}, found {sorted(commits)}")
    return commits.pop()


StageRunner = Callable[[str], Mapping[str, Any]]


def run_probe(run_stage: StageRunner, stages: Sequence[str] = STAGES, store_text: bool = False) -> dict[str, Any]:
    """Run each stage ONCE, in order. The first stage that does not report
    `ok` is classified and the probe stops: later stages are `not-run`, and
    the failed stage is never attempted again."""
    results: list[dict[str, Any]] = []
    failed: dict[str, Any] | None = None
    for stage in stages:
        if failed is not None:
            results.append({"stage": stage, "outcome": "not-run", "reason": f"earlier stage {failed['stage']} failed"})
            continue
        sig = dict(run_stage(stage))
        interruptions = sig.get("classifier_interruptions", UNOBSERVED)
        if sig.get("ok"):
            results.append({"stage": stage, "outcome": "ok", "classifier_interruptions": interruptions})
            continue
        failed = {
            "stage": stage,
            "outcome": "failed",
            "class": classify(sig),
            "classifier_interruptions": interruptions,
            "text": failure_text_record(str(sig.get("text", "")), store_text),
        }
        results.append(failed)
    return {
        "stages": results,
        "earliest_failing_stage": failed["stage"] if failed else None,
        "failure_class": failed["class"] if failed else None,
    }


def load_oracle(path: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(path.read_text())
    return {str(f["id"]): f for f in data["facts"]}


def score(record: Mapping[str, Any], oracle: Mapping[str, Any], grading: Mapping[str, Any]) -> dict[str, Any]:
    """Metrics for one run from the human-filled grading entry. Anything not
    observed stays the string `unobserved`, never zero."""
    claims = grading.get("claims", [])
    hits = {
        str(c["label"]).split(":", 1)[1]
        for c in claims
        if str(c.get("label", "")).startswith("oracle:") and str(c["label"]).split(":", 1)[1] in oracle
    }
    unsupported = sum(1 for c in claims if c.get("label") == "unsupported")
    cited = sum(1 for c in claims if c.get("cites_rea") is True)
    usage = record.get("usage") or {}
    return {
        "oracle_facts_recovered": f"{len(hits)}/{len(oracle)}",
        "unsupported_claims": unsupported,
        "evidence_grounded_claims": cited if record.get("arm") != "baseline" else 0,
        "turns": record.get("turns", UNOBSERVED),
        "tool_calls": record.get("tool_calls", UNOBSERVED),
        "input_tokens": usage.get("input_tokens", UNOBSERVED),
        "output_tokens": usage.get("output_tokens", UNOBSERVED),
        "wall_clock_s": record.get("wall_clock_s", UNOBSERVED),
        "plan_actionable": grading.get("actionable", UNOBSERVED),
    }


def _row(cells: Sequence[Any]) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def metrics_table(rows: Sequence[Mapping[str, Any]]) -> str:
    cols = (
        "fixture", "arm", "oracle_facts_recovered", "unsupported_claims", "evidence_grounded_claims",
        "turns", "tool_calls", "input_tokens", "output_tokens", "wall_clock_s", "plan_actionable",
    )
    out = [_row(cols), _row(["---"] * len(cols))]
    out += [_row([r.get(c, UNOBSERVED) for c in cols]) for r in rows]
    return "\n".join(out)


def model_matrix(records: Sequence[Mapping[str, Any]], models: Sequence[str] = MATRIX_MODELS,
                 reason: str = "no #791 endpoint supplied") -> str:
    """One row per model and stage. A model with no records renders
    `not-run (<reason>)`, never a zero or a pass."""
    out = [_row(("model", *STAGES)), _row(["---"] * (len(STAGES) + 1))]
    seen = {str(r["model"]) for r in records if r.get("kind") == "probe" and r.get("model")}
    for model in [*sorted(seen), *[m for m in models if m not in seen]]:
        probes = [r for r in records if r.get("kind") == "probe" and r.get("model") == model]
        if not probes:
            out.append(_row([model, *[f"not-run ({reason})"] * len(STAGES)]))
            continue
        by_stage = {s["stage"]: s for s in probes[-1]["probe"]["stages"]}
        cells = []
        for stage in STAGES:
            s = by_stage.get(stage)
            if s is None:
                cells.append(UNOBSERVED)
            elif s["outcome"] == "failed":
                cells.append(f"failed: {s['class']}")
            elif s["outcome"] == "not-run":
                cells.append(f"not-run ({s['reason']})")
            else:
                cells.append("ok")
        out.append(_row([model, *cells]))
    return "\n".join(out)


def report_markdown(records: Sequence[Mapping[str, Any]], scored: Sequence[Mapping[str, Any]] = ()) -> str:
    return "\n".join([
        "## Metrics table", "", metrics_table(scored), "",
        "## Cross-model matrix", "", model_matrix(records), "",
    ])


def schema_bytes_report(tools: Sequence[Any]) -> dict[str, Any]:
    """Same counting rule as `tools/capability_bench.py schema-bytes`: the
    canonical-JSON size of each tool's input schema, summed."""
    from orchestrator.context_snapshot import canonical_json

    total = sum(len(canonical_json(dict(getattr(t, "inputSchema", None) or {}))) for t in tools)
    return {"tool_count": len(tools), "schema_bytes": total}


# ---------------------------------------------------------------------------
# Live wrappers (operator only)
# ---------------------------------------------------------------------------


def _require_live(args: argparse.Namespace) -> bool:
    if not getattr(args, "allow_live", False):
        print("refusing to run: live subcommands need --allow-live (operator-run only)", file=sys.stderr)
        return False
    return True


def _read_active_model() -> str:
    model = os.environ.get("ISSUE_INVESTIGATOR_MODEL", "")
    if not model:
        raise SystemExit("set ISSUE_INVESTIGATOR_MODEL to the value read from the investigate CWFT")
    return model


def _is_rea_tool_use(block: Any) -> bool:
    """True for an MCP REA tool call, or a Bash call that invokes the REA CLI."""
    name = block.name
    if name.startswith("mcp__rea__"):
        return True
    if name == "Bash":
        command = (getattr(block, "input", None) or {}).get("command", "")
        return isinstance(command, str) and re.search(r"(^|[\s;&|/])rea(\s|$)", command) is not None
    return False


async def _collect(stream: Any, sig: dict[str, Any]) -> dict[str, Any]:
    """Fold an SDK message stream into the signals `classify` reads. Duck-typed
    so a stubbed stream can drive it in unit tests."""
    text: list[str] = []
    rea_use_ids: set[str] = set()
    tool_calls = 0
    try:
        async for msg in stream:
            if hasattr(msg, "is_error") and hasattr(msg, "num_turns"):  # ResultMessage
                sig["usage"] = getattr(msg, "usage", None)
                sig["turns"] = msg.num_turns
                if getattr(msg, "stop_reason", None) == "refusal":
                    sig["ok"] = False
                    sig["declined"] = True
                if msg.is_error:
                    sig["ok"] = False
                    sig["transport_error"] = True
                continue
            if getattr(msg, "stop_reason", None) == "refusal":
                sig["ok"] = False
                sig["declined"] = True
            content = getattr(msg, "content", None)
            if not isinstance(content, list):
                continue
            for block in content:
                use_id = getattr(block, "id", None)
                if isinstance(use_id, str) and isinstance(getattr(block, "name", None), str):  # ToolUseBlock
                    tool_calls += 1
                    if _is_rea_tool_use(block):
                        rea_use_ids.add(use_id)
                if getattr(block, "is_error", None) is True:  # failed tool result
                    if getattr(block, "tool_use_id", None) in rea_use_ids:
                        sig["ok"] = False
                        sig["tool_error"] = True
                    else:  # Bash/Read/Glob misses are normal; count, do not fail the stage
                        sig["other_tool_errors"] = int(sig.get("other_tool_errors", 0)) + 1
                if isinstance(getattr(block, "text", None), str):
                    text.append(block.text)
    except Exception as exc:  # noqa: BLE001 - recorded, never retried
        sig["ok"] = False
        sig["transport_error"] = True
        text.append(repr(exc))
    else:  # a broken stream leaves the counts unobserved, not short
        sig["tool_calls"] = tool_calls
        sig["classifier_interruptions"] = sum(t.count(CLASSIFIER_NOTICE) for t in text)
    sig["text"] = "\n".join(text)
    return sig


def _live_stage(prompt: str, model: str, fixture_dir: Path, arm: str) -> dict[str, Any]:  # pragma: no cover
    """One single-attempt Claude session. Returns the signals `classify` reads."""
    import anyio
    from claude_agent_sdk import ClaudeAgentOptions, query

    mcp_cmd = os.environ.get("REA_SPIKE_MCP_CMD", "")
    parts = mcp_cmd.split()
    servers: Any = {"rea": {"command": parts[0], "args": parts[1:]}} if arm == "rea-mcp" and parts else {}
    tools_registered = not (arm == "rea-mcp" and not servers)
    if not tools_registered:
        # Fail fast: running without the REA tools would be recorded as a baseline success.
        return {"ok": False, "tools_registered": False, "text": "rea-mcp arm without REA_SPIKE_MCP_CMD"}
    options = ClaudeAgentOptions(model=model, cwd=str(fixture_dir), mcp_servers=servers,
                                 allowed_tools=["Read", "Glob", "Grep", "Bash", *(["mcp__rea__*"] if servers else [])])

    async def _go() -> dict[str, Any]:
        return await _collect(query(prompt=prompt, options=options),
                              {"ok": True, "tools_registered": True, "text": ""})

    return anyio.run(_go)


RUNTIME_ARTIFACTS = {"native-codec": "codec", "packaged-app": "build/app.jar"}


def stage_runtime_fixture(fixture: str, dest_root: Path) -> Path:
    """Copy only the built target and task.md into dest_root/<fixture>.

    The agent's cwd must not expose sources or oracle.yaml (the answer key).
    """
    import shutil

    src = SPIKE_DIR / "fixtures" / fixture
    out = dest_root / fixture
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src / "task.md", out / "task.md")
    artifact = RUNTIME_ARTIFACTS[fixture]
    shutil.copy2(src / artifact, out / Path(artifact).name)
    return out


def _fixture_dir(args: argparse.Namespace) -> Path:
    root = getattr(args, "fixture_root", "")
    return Path(root) / args.fixture if root else SPIKE_DIR / "fixtures" / args.fixture


def _cmd_stage_fixture(args: argparse.Namespace) -> int:
    fixtures = [args.fixture] if getattr(args, "fixture", "") else list(RUNTIME_ARTIFACTS)
    rc = 0
    for fx in fixtures:
        try:
            stage_runtime_fixture(fx, Path(args.dest))
        except (OSError, KeyError) as exc:
            print(f"stage-fixtures: {fx}: {exc}", file=sys.stderr)
            rc = 1
    return rc


def _cmd_probe(args: argparse.Namespace) -> int:
    if not _require_live(args):
        return 2
    model = args.model or _read_active_model()
    fixture_dir = _fixture_dir(args)

    prior: list[tuple[str, str]] = []

    def runner(stage: str) -> Mapping[str, Any]:
        # Each stage is a fresh session; carry earlier stage output forward as context.
        prompt = (SPIKE_DIR / "stages" / f"{stage}.md").read_text() + "\n\n" + (fixture_dir / "task.md").read_text()
        if prior:
            prompt += "\n\n## Output of earlier stages\n\n" + "\n\n".join(f"### {n}\n\n{t}" for n, t in prior)
        sig = _live_stage(prompt, model, fixture_dir, args.arm)
        if sig.get("ok"):
            prior.append((stage, str(sig.get("text", ""))))
        return sig

    result = run_probe(runner, store_text=args.store_text)
    print(json.dumps({"kind": "probe", "fixture": args.fixture, "arm": args.arm, "model": model, "probe": result}))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if not _require_live(args):
        return 2
    import time

    model = args.model or _read_active_model()
    fixture_dir = _fixture_dir(args)
    started = time.monotonic()
    sig = _live_stage((fixture_dir / "task.md").read_text(), model, fixture_dir, args.arm)
    rec = {
        "kind": "run", "fixture": args.fixture, "arm": args.arm, "model": model,
        "wall_clock_s": round(time.monotonic() - started, 1),
        "turns": sig.get("turns", UNOBSERVED), "tool_calls": sig.get("tool_calls", UNOBSERVED),
        "classifier_interruptions": sig.get("classifier_interruptions", UNOBSERVED),
        "usage": sig.get("usage") or {},
        "final": bounded_summary(str(sig.get("text", ""))),
        "outcome": "ok" if sig.get("ok") else "failed",
        "failure_class": None if sig.get("ok") else classify(sig),
    }
    if not sig.get("ok"):
        rec["failure"] = failure_text_record(str(sig.get("text", "")), store_text=False)
    print(json.dumps(rec))
    return 0 if sig.get("ok") else 1


def _cmd_schema_bytes(args: argparse.Namespace) -> int:
    if not _require_live(args):
        return 2
    import anyio

    cmd = os.environ.get("REA_SPIKE_MCP_CMD", "").split()
    if not cmd:
        raise SystemExit("set REA_SPIKE_MCP_CMD to the REA MCP server command")

    async def _list() -> list[Any]:
        import mcp
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async with stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:])) as (r, w):
            async with mcp.ClientSession(r, w) as session:
                await session.initialize()
                return list((await session.list_tools()).tools)

    print(json.dumps(schema_bytes_report(anyio.run(_list))))
    return 0


def _score_rows(records_path: str, grading_path: str) -> list[dict[str, Any]]:
    grading = yaml.safe_load(Path(grading_path).read_text())
    rows: list[dict[str, Any]] = []
    for line in Path(records_path).read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("kind") != "run":
            continue
        g = next((e for e in grading["entries"] if e["fixture"] == rec["fixture"] and e["arm"] == rec["arm"]), None)
        if g is None:
            continue
        oracle = load_oracle(SPIKE_DIR / "fixtures" / rec["fixture"] / "oracle.yaml")
        rows.append({"fixture": rec["fixture"], "arm": rec["arm"], **score(rec, oracle, g)})
    return rows


def _cmd_score(args: argparse.Namespace) -> int:
    print(metrics_table(_score_rows(args.records, args.grading)))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    records = [json.loads(ln) for ln in Path(args.records).read_text().splitlines() if ln.strip()]
    rows: list[dict[str, Any]] = []
    if args.grading:
        rows = _score_rows(args.records, args.grading)
    print(report_markdown(records, scored=rows))
    return 0


def _cmd_npm_provenance(args: argparse.Namespace) -> int:
    try:
        commit = npm_provenance_commit(json.loads(Path(args.attestations).read_text()), args.integrity, args.repo)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"npm-provenance: {exc}", file=sys.stderr)
        return 1
    print(commit)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    sub = p.add_subparsers(dest="cmd", required=True)

    def live(name: str, fn: Callable[[argparse.Namespace], int], fixture: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name)
        sp.add_argument("--allow-live", action="store_true", help="required: this subcommand calls a model or REA")
        if fixture:
            sp.add_argument("--fixture", required=True)
            sp.add_argument("--arm", choices=ARMS, default="baseline")
            sp.add_argument("--model", default="")
            sp.add_argument("--fixture-root", default="", help="staged runtime-only fixtures (cwd for the agent)")
        sp.set_defaults(func=fn)
        return sp

    live("run", _cmd_run)
    live("probe-stages", _cmd_probe).add_argument("--store-text", action="store_true")
    live("schema-bytes", _cmd_schema_bytes, fixture=False)
    st = sub.add_parser("stage-fixtures")
    st.add_argument("dest")
    st.add_argument("--fixture", default="")
    st.set_defaults(func=_cmd_stage_fixture)
    sc = sub.add_parser("score")
    sc.add_argument("records")
    sc.add_argument("grading")
    sc.set_defaults(func=_cmd_score)
    np_ = sub.add_parser("npm-provenance", help="print the source commit from a saved npm attestations response")
    np_.add_argument("attestations")
    np_.add_argument("--integrity", required=True)
    np_.add_argument("--repo", required=True)
    np_.set_defaults(func=_cmd_npm_provenance)
    rp = sub.add_parser("report")
    rp.add_argument("records")
    rp.add_argument("--grading", default="")
    rp.set_defaults(func=_cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
