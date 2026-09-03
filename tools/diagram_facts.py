"""Detect drift between docs/diagrams/archify/*.json and the code they describe.

The diagrams state numbers and names — timeouts of the dev loop, the status
vocabulary of .status.yaml, per-agent budgets, the CWFT list, the size of the
MCP tool surface. Every one of those is a constant somewhere in mctl-agents,
mctl-gitops or mctl-api. This script re-reads them from source and compares
with docs/diagrams/archify/facts.yaml, the values the diagrams were authored
against. A mismatch is a *drift report*, not a failure: the refresh workflow
hands the report to an agent that updates the diagrams and opens a PR.

Deliberately regex-based and dependency-free (PyYAML only, which the
orchestrator already needs): the refresh job must not have to `uv sync` the
whole agent SDK just to read a handful of constants, and importing
dev_loop.py would pull temporalio into a job that never runs a workflow.

Usage:
    python3 tools/diagram_facts.py --gitops ../mctl-gitops/platform-gitops --api ../mctl-api \
        [--releases releases.json] [--update] [--report drift.md] [--json drift.json]

Exit codes: 0 no drift, 3 drift found (so a workflow `if:` can branch on it),
1 on a real error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FACTS_PATH = REPO_ROOT / "docs" / "diagrams" / "archify" / "facts.yaml"

# Constants the diagrams quote. Left: fact key, right: regex over the file's
# text with one capture group. timedelta(...) values are normalised to a
# human string ("30 min", "14 d") by _timedelta_text so facts.yaml stays
# readable and matches what a diagram label says.
_DEV_LOOP_CONSTANTS = {
    "sdk_step_timeout": r"^SDK_STEP_TIMEOUT = timedelta\((.+)\)",
    "approve_step_timeout": r"^APPROVE_STEP_TIMEOUT = timedelta\((.+)\)",
    "merge_poll_interval": r"^MERGE_POLL_INTERVAL = timedelta\((.+)\)",
    "merge_watch_deadline": r"^MERGE_WATCH_DEADLINE = timedelta\((.+)\)",
    "shepherd_tick_every_polls": r"^SHEPHERD_TICK_EVERY_POLLS = (\d+)",
    "shepherd_ticks_max": r"^SHEPHERD_TICKS_MAX = (\d+)",
    "release_lookup_deadline": r"^RELEASE_LOOKUP_DEADLINE = timedelta\((.+)\)",
    "deploy_verify_deadline": r"^DEPLOY_VERIFY_DEADLINE = timedelta\((.+)\)",
    "incident_watch_window": r"^INCIDENT_WATCH_WINDOW = timedelta\((.+)\)",
    "sdk_step_retry_attempts": r"^SDK_STEP_RETRY_POLICY = RetryPolicy\(maximum_attempts=(\d+)\)",
}

_SHEPHERD_CONSTANTS = {
    "max_review_attempts": r"^MAX_REVIEW_ATTEMPTS = (\d+)",
    "merge_settle_min_default": r'os\.environ\.get\("SHEPHERD_MERGE_SETTLE_MIN", "(\d+)"\)',
}

_BUDGET_RE = re.compile(r'^([A-Z_]+)_BUDGET_USD = float\(os\.getenv\("[A-Z_]+", "([\d.]+)"\)\)', re.M)
_STATUS_SET_RE = re.compile(r"^(RECONCILE_INPUT_STATUSES|SHEPHERD_INPUT_STATUSES) = \{([^}]*)\}", re.M | re.S)
_SCHEDULE_RE = re.compile(
    r"every=timedelta\(([^)]+)\)\)\],\n(?:.*\n){0,6}?.*_ensure_schedule\(client, \w+, \w+, \"(\w+)\""
)


def _timedelta_text(args: str) -> str:
    """'minutes=30' -> '30 min'; 'days=14' -> '14 d'; 'hours=2' -> '2 h'."""
    unit_names = {"seconds": "s", "minutes": "min", "hours": "h", "days": "d"}
    parts = []
    for piece in args.split(","):
        unit, _, value = piece.strip().partition("=")
        parts.append(f"{value} {unit_names.get(unit, unit)}")
    return " ".join(parts)


def _grep(text: str, patterns: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.M)
        if not m:
            out[key] = "<not found>"
            continue
        raw = m.group(1)
        out[key] = _timedelta_text(raw) if "timedelta" in pattern else raw
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def facts_from_code(gitops: Path | None, api: Path | None) -> dict[str, Any]:
    facts: dict[str, Any] = {}

    dev_loop = _read(REPO_ROOT / "orchestrator" / "temporal" / "workflows" / "dev_loop.py")
    facts["dev_loop"] = _grep(dev_loop, _DEV_LOOP_CONSTANTS)
    facts["dev_loop"]["patched_markers"] = sorted(set(re.findall(r'workflow\.patched\("([^"]+)"\)', dev_loop)))

    shepherd = _read(REPO_ROOT / "orchestrator" / "run_shepherd.py")
    facts["shepherd"] = _grep(shepherd, _SHEPHERD_CONSTANTS)
    for name, body in _STATUS_SET_RE.findall(shepherd):
        facts["shepherd"][name.lower()] = sorted(re.findall(r'"([a-z-]+)"', body))
    facts["shepherd"]["gating_bots"] = sorted(set(re.findall(r'^[A-Z_]*BOT = "([^"]+)"', shepherd, re.M)))

    options = _read(REPO_ROOT / "orchestrator" / "options.py")
    facts["budgets_usd"] = {agent.lower(): value for agent, value in _BUDGET_RE.findall(options)}

    inventory = yaml.safe_load(_read(REPO_ROOT / "docs" / "agent-inventory.yaml"))
    facts["agents"] = sorted(a["name"] for a in inventory.get("agents", []))
    facts["agent_risk"] = {a["name"]: a.get("riskLevel", "") for a in inventory.get("agents", [])}

    worker = _read(REPO_ROOT / "orchestrator" / "temporal" / "worker.py")
    facts["schedules"] = {wf: _timedelta_text(every) for every, wf in _SCHEDULE_RE.findall(worker)}
    workflows_dir = REPO_ROOT / "orchestrator" / "temporal" / "workflows"
    facts["workflows"] = sorted(
        m for f in workflows_dir.glob("*.py") for m in re.findall(r"^class (\w+Workflow)\b", _read(f), re.M)
    )

    manifests_dir = REPO_ROOT / "agents" / "_manifests"
    facts["manifest_api_versions"] = {
        p.parent.name: yaml.safe_load(_read(p)).get("apiVersion", "")
        for p in sorted(manifests_dir.glob("*/agent.yaml"))
    }

    if gitops is not None:
        cwft_dir = gitops / "argo-workflows" / "cluster-templates"
        facts["cwfts"] = sorted(
            p.name.removeprefix("cwft-").removesuffix(".yaml") for p in cwft_dir.glob("cwft-mctl-agents-*.yaml")
        )
        crons: dict[str, str] = {}
        for p in sorted(cwft_dir.glob("cronworkflow-*.yaml")) + sorted(cwft_dir.glob("cwft-mctl-agents-daily.yaml")):
            m = re.search(r'^\s*schedule:\s*"?([^"\n]+)"?', _read(p), re.M)
            if m:
                crons[p.name] = m.group(1).strip()
        facts["cronworkflows"] = crons
        catalog = gitops / "platform-skills" / "catalog"
        facts["platform_skills"] = sorted(p.name for p in catalog.iterdir() if (p / "SKILL.md").exists())
        policy = gitops / "agent-platform" / "policy.yaml"
        if policy.exists():
            pol = yaml.safe_load(_read(policy))
            spec = pol.get("spec", pol)
            facts["policy_ceilings"] = {k: spec.get(k) for k in ("maxBudgetUsd", "maxTimeoutSeconds") if k in spec}

    if api is not None:
        server = _read(api / "internal" / "mcp" / "server.go")
        facts["mcp_tool_count"] = len(re.findall(r"srv\.AddTool\(", server))
        facts["mcp_tools"] = sorted(set(re.findall(r'"(mctl_[a-z_]+)"', server)))
        registry = _read(api / "internal" / "operations" / "registry.go")
        facts["operations"] = sorted(set(re.findall(r'^\s*Name:\s*"([a-z-]+)",\s*$', registry, re.M)))
        types_go = _read(api / "internal" / "agentregistry" / "types.go")
        facts["registry_environments"] = sorted(re.findall(r'Environment\w+\s*=\s*"([a-z]+)"', types_go))

    return facts


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    return {prefix.rstrip("."): d}


def diff_facts(recorded: dict[str, Any], current: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    a, b = _flatten(recorded), _flatten(current)
    drift = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            drift.append((key, a.get(key), b.get(key)))
    return drift


def new_releases(releases_path: Path, seen: dict[str, str]) -> list[dict[str, str]]:
    """releases.json: [{repo, tag, published_at, body}] as fetched by the workflow.

    A release counts as new when its tag differs from the recorded watermark
    for that repo. Tags in this org carry no v prefix, but compare as strings
    anyway — a moved watermark is the only thing that matters here.
    """
    if not releases_path.exists():
        return []
    entries = json.loads(releases_path.read_text(encoding="utf-8"))
    latest: dict[str, dict[str, str]] = {}
    for e in entries:
        if e["repo"] not in latest or e["published_at"] > latest[e["repo"]]["published_at"]:
            latest[e["repo"]] = e
    return [e for repo, e in sorted(latest.items()) if seen.get(repo) != e["tag"]]


def render_report(drift: list[tuple[str, Any, Any]], releases: list[dict[str, str]]) -> str:
    lines = ["# Diagram drift report", ""]
    if drift:
        lines += ["## Facts that changed since the diagrams were authored", ""]
        lines += ["| fact | diagrams say | code says |", "|---|---|---|"]
        lines += [f"| `{k}` | `{a}` | `{b}` |" for k, a, b in drift]
        lines.append("")
    if releases:
        lines += ["## Releases not yet reflected", ""]
        for r in releases:
            lines.append(f"### {r['repo']} {r['tag']} ({r['published_at'][:10]})")
            lines.append("")
            lines.append((r.get("body") or "").strip()[:4000])
            lines.append("")
    if not drift and not releases:
        lines.append("No drift. Nothing to do.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gitops", type=Path, help="path to mctl-gitops/platform-gitops")
    ap.add_argument("--api", type=Path, help="path to an mctl-api checkout")
    ap.add_argument("--releases", type=Path, default=Path("releases.json"))
    ap.add_argument("--update", action="store_true", help="rewrite facts.yaml with the current values")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--json", type=Path)
    ns = ap.parse_args()

    current = facts_from_code(ns.gitops, ns.api)
    recorded_doc: dict[str, Any] = yaml.safe_load(_read(FACTS_PATH)) if FACTS_PATH.exists() else {}
    recorded = dict(recorded_doc)
    seen_releases: dict[str, str] = recorded.pop("releases_seen", {}) or {}
    # Sections not extractable in this run (no --gitops / --api) are not drift.
    recorded = {k: v for k, v in recorded.items() if k in current}

    drift = diff_facts(recorded, current)
    releases = new_releases(ns.releases, seen_releases)

    report = render_report(drift, releases)
    if ns.report:
        ns.report.write_text(report, encoding="utf-8")
    if ns.json:
        payload = {"drift": [{"fact": k, "recorded": a, "current": b} for k, a, b in drift], "releases": releases}
        ns.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(report)

    if ns.update:
        updated = dict(current)
        for r in releases:
            seen_releases[r["repo"]] = r["tag"]
        updated["releases_seen"] = dict(sorted(seen_releases.items()))
        FACTS_PATH.write_text(
            "# Values the archify diagrams were authored against. Regenerated by\n"
            "# tools/diagram_facts.py --update; a diff here is the drift report.\n"
            + yaml.safe_dump(updated, sort_keys=True, allow_unicode=True),
            encoding="utf-8",
        )
        print(f"wrote {FACTS_PATH.relative_to(REPO_ROOT)}")

    return 3 if (drift or releases) else 0


if __name__ == "__main__":
    sys.exit(main())
