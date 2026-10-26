"""URL normalisation and spike:scout issue lookup (read-only gh api)."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from orchestrator.github_token import refresh_github_token
from orchestrator.proc import CommandFailed, run_capturing

_DROP_PARAMS = {"ref", "fbclid", "gclid"}
_SOURCE_RE = re.compile(r"^Source:\s*(\S+)", re.MULTILINE)


def normalise_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.startswith(("utm_", "mc_")) or k in _DROP_PARAMS)
    ]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def scout_issue_urls(days: int) -> set[str] | None:
    """Normalised Source URLs of mctlhq spike:scout issues; None if the search failed."""
    since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    q = f'org:mctlhq label:"spike:scout" created:>={since}'
    try:
        refresh_github_token()
        proc = run_capturing(
            ["gh", "api", "-X", "GET", "search/issues", "-f", f"q={q}", "-f", "per_page=100", "--paginate",
             "--jq", ".items[] | {body: .body}"],
            timeout=60,
        )
        urls: set[str] = set()
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            body = json.loads(line).get("body") or ""
            m = _SOURCE_RE.search(body)
            if m:
                urls.add(normalise_url(m.group(1)))
        return urls
    except (CommandFailed, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"scout: dedupe search failed: {type(exc).__name__}")
        return None
