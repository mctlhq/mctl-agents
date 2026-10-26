"""Read-only spike backlog: open spike issues plus SpikeDefinitions in mctlhq/.github."""
from __future__ import annotations

import base64
import json
import subprocess
from typing import Any

import yaml

from orchestrator.github_token import refresh_github_token
from orchestrator.proc import CommandFailed, run_capturing
from orchestrator.spike_issue import SPIKE_TITLE_RE, SPIKES_DIR, SPIKES_REPO

MAX_ENTRIES = 60
MAX_CHARS = 8000


def _gh_json(args: list[str]) -> Any:
    proc = run_capturing(["gh", "api", "-X", "GET", *args], timeout=60)
    return json.loads(proc.stdout)


def _spike_definition(name: str, raw: str) -> dict[str, str] | None:
    try:
        doc = yaml.safe_load(raw) or {}
        spec = doc.get("spec") or {}
        meta = doc.get("metadata") or {}
        return {
            "ref": f"{SPIKES_REPO}:{name}",
            "title": str(meta.get("name") or spec.get("title") or name),
            "question": str(spec.get("question") or "")[:300],
            "lifecycle": str((doc.get("status") or {}).get("lifecycle") or spec.get("lifecycle") or ""),
        }
    except (yaml.YAMLError, AttributeError):
        return None


def collect_backlog() -> list[dict[str, str]] | None:
    """Return the capped backlog, or None when GitHub could not be read."""
    try:
        refresh_github_token()
        data = _gh_json(["search/issues", "-f", "q=org:mctlhq is:issue is:open in:title spike", "-f", "per_page=100"])
        entries: list[dict[str, str]] = []
        for issue in data.get("items", []):
            title = str(issue.get("title", ""))
            if not SPIKE_TITLE_RE.match(title):
                continue
            repo = str(issue.get("repository_url", "")).rsplit("/", 1)[-1]
            entries.append({
                "ref": f"{repo}#{issue.get('number')}",
                "title": title[:200],
                "question": "",
                "lifecycle": "open",
            })
        listing = _gh_json([f"repos/{SPIKES_REPO}/contents/{SPIKES_DIR}"])
        for f in listing if isinstance(listing, list) else []:
            if not str(f.get("name", "")).endswith((".yaml", ".yml")):
                continue
            body = _gh_json([f"repos/{SPIKES_REPO}/contents/{SPIKES_DIR}/{f['name']}"])
            raw = base64.b64decode(body.get("content", "")).decode("utf-8", "replace")
            entry = _spike_definition(str(f["name"]), raw)
            if entry:
                entries.append(entry)
    except (CommandFailed, OSError, ValueError, KeyError, AttributeError, subprocess.TimeoutExpired) as exc:
        print(f"scout: backlog unavailable: {type(exc).__name__}")
        return None
    out: list[dict[str, str]] = []
    size = 0
    for e in entries[:MAX_ENTRIES]:
        size += len(json.dumps(e))
        if size > MAX_CHARS:
            break
        out.append(e)
    return out
