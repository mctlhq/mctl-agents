#!/usr/bin/env python3
"""Publish and promote every agent manifest for one release.

Why this exists: the agent registry is what DevLoopWorkflow resolves an
agent's image from, and nothing ever refreshed it. Publishing was a manual
sequence of MCP calls, so the pins drifted — `shepherd` sat on 1.25.0 from
2026-08-07 while the repo released 1.33.0, and once #230 taught the
in-loop shepherd tick to pin the released image, that stale row became the
image those ticks would actually run.

Run it for a released tag:

    MCTL_TOKEN=... python tools/publish_agent_release.py 1.33.0

`--dry-run` prints what it would publish without writing anything, and
`--agent NAME` limits it to one manifest.

## prompt_hash

The registry stores a prompt_hash per version, but nothing in this repo
ever computed one: every historical row carries the same constant, copied
forward by hand, which makes it useless for detecting prompt drift — the
one thing it is for. This defines the hash instead:

    sha256 over each prompt source, in sorted order by its identifier,
    as "<identifier>\\n<file bytes>" concatenated.

An `inline: path.py:function` source hashes the whole file it names — the
function boundary is not something this can parse reliably, and a change
anywhere in that module is a change to the prompt surface worth noticing.
Sources whose file is missing are a hard error, not a skipped entry: a
silently short hash would compare equal across genuinely different
prompts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_DIR = REPO_ROOT / "agents" / "_manifests"
IMAGE_REPOSITORY = "ghcr.io/mctlhq/mctl-agents"
DEFAULT_API = "https://api.mctl.ai"
ENVIRONMENT = "production"
TIMEOUT_S = 30


class PublishError(RuntimeError):
    pass


def _api_base() -> str:
    base = os.environ.get("MCTL_API_BASE_URL", DEFAULT_API).rstrip("/")
    if not base.startswith("https://"):
        raise PublishError(f"refusing a non-https MCTL_API_BASE_URL: {base}")
    return base


def _token() -> str:
    token = os.environ.get("MCTL_TOKEN", "").strip()
    if not token:
        raise PublishError("MCTL_TOKEN is not set")
    return token


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, str]:
    """One mctl-api call. Non-2xx comes back as a value, not an exception:
    a 409 on publish is an expected no-op, and the caller decides."""
    response = httpx.request(
        method,
        f"{_api_base()}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=TIMEOUT_S,
    )
    return response.status_code, response.text


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed argv from PATH, no shell
        ["git", *args],  # noqa: S607 — git from PATH, as every other tool here does
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _tree_paths(tag: str) -> list[str]:
    return _git("ls-tree", "-r", "--name-only", tag).splitlines()


def _read_at_tag(tag: str, relpath: str) -> bytes | None:
    """File content as of ``tag``, or None if it is not in that tree."""
    result = subprocess.run(  # noqa: S603 — fixed argv from PATH, no shell
        ["git", "show", f"{tag}:{relpath}"],  # noqa: S607 — git from PATH
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one manifest glob with path-aware wildcards.

    NOT fnmatch: its ``*`` happily crosses ``/``, so
    ``agents/[!_]*/CLAUDE.md`` also matches
    ``agents/x/nested/deeper/CLAUDE.md``. Every extra file it swept in
    would land in prompt_hash, making the hash change on edits to files
    the manifest never declared — and drift detection that fires on
    unrelated files is drift detection nobody reads. Shell semantics
    instead: ``*`` and ``?`` stay within one segment, ``**`` spans them,
    and ``[...]`` classes (including ``[!x]`` negation) pass through.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
            continue
        if char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                # An unclosed class is a literal bracket, as in fnmatch.
                out.append(re.escape(char))
            else:
                body = pattern[i + 1 : end]
                if body.startswith(("!", "^")):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end + 1
                continue
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _glob_match(path: str, pattern: str) -> bool:
    compiled = _GLOB_CACHE.get(pattern)
    if compiled is None:
        compiled = _GLOB_CACHE[pattern] = _glob_to_regex(pattern)
    return compiled.match(path) is not None


def prompt_hash(manifest: dict[str, Any], agent: str, tag: str, tree: list[str]) -> str:
    """Hash the manifest's declared prompt surface. See the module docstring.

    Everything is read out of ``tag``'s tree, never the working copy: the
    hash has to describe the released prompt, and publishing one taken
    from a dirty checkout would quietly attest to something that was never
    shipped.
    """
    sources = manifest.get("spec", {}).get("prompt", {}).get("sources", [])
    if not sources:
        raise PublishError(f"{agent}: manifest declares no prompt sources")
    # (identifier, repo-relative path or None) — one entry per real file,
    # so a glob contributes every file it matched at this tag.
    entries: list[tuple[str, str | None]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise PublishError(f"{agent}: malformed prompt source {source!r}")
        if isinstance(source.get("glob"), str):
            pattern = source["glob"]
            matches = sorted(p for p in tree if _glob_match(p, pattern))
            # A glob matching nothing is legitimate — the per-tenant
            # implementer prompts live in directories this repo need not
            # carry. Record the pattern anyway, so the hash changes the
            # moment the first match appears.
            entries.extend((p, p) for p in matches)
            if not matches:
                entries.append((f"{pattern} (no matches)", None))
            continue
        value = source.get("inline") or source.get("file")
        if not isinstance(value, str) or not value:
            raise PublishError(f"{agent}: unsupported prompt source: {source!r}")
        # "path.py:function" for inline sources — hash the whole file.
        entries.append((value, value.split(":", 1)[0]))

    digest = hashlib.sha256()
    for identifier, relpath in sorted(entries, key=lambda e: e[0]):
        digest.update(identifier.encode())
        digest.update(b"\n")
        if relpath is None:
            continue
        content = _read_at_tag(tag, relpath)
        if content is None:
            raise PublishError(f"{agent}: prompt source {identifier} is not in {tag}'s tree")
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def publish(agent: str, version: str, git_sha: str, tree: list[str], *, dry_run: bool) -> bool:
    relpath = f"agents/_manifests/{agent}/agent.yaml"
    raw = _read_at_tag(version, relpath)
    if raw is None:
        raise PublishError(f"{agent}: {relpath} is not in {version}'s tree")
    manifest = yaml.safe_load(raw.decode())
    payload = {
        "version": version,
        # The API takes JSON, not YAML: a raw YAML body is rejected by
        # Postgres as SQLSTATE 22P02 rather than by any validation here.
        "manifest_json": json.dumps(manifest),
        "git_sha": git_sha,
        # No image_digest. The release workflow dispatches the image build
        # to mctl-gitops asynchronously and runs this immediately after, so
        # the image does not exist yet — resolving a digest here could only
        # ever fail, and publishing an empty one pretends otherwise.
        # registry.py falls back to "<repo>:<version>", which is the tag
        # release-deploy pins anyway.
        "image_repository": IMAGE_REPOSITORY,
        "prompt_hash": prompt_hash(manifest, agent, version, tree),
    }
    if dry_run:
        print(f"  would publish {agent}@{version} prompt_hash={payload['prompt_hash']}")
        return True

    status, body = _request("POST", f"/api/v1/agents/{agent}/versions", payload)
    if status == 404:
        # First release for a manifest the registry has never seen. The
        # definition is bookkeeping the version row hangs off, so create
        # it and retry once rather than making a new agent's first publish
        # a manual step — that is precisely the kind of manual step that
        # let these pins go stale.
        owner = manifest.get("metadata", {}).get("owner", "mctl-agents")
        create_status, create_body = _request(
            "POST",
            "/api/v1/agents",
            {"name": agent, "owner": owner, "description": f"{agent} (mctl-agents)"},
        )
        if create_status not in (200, 201, 409):
            print(
                f"  ERROR creating definition for {agent}: HTTP {create_status} {create_body[:300]}",
                file=sys.stderr,
            )
            return False
        print(f"  created registry definition for {agent}")
        status, body = _request("POST", f"/api/v1/agents/{agent}/versions", payload)
    if status == 409:
        # Versions are immutable; re-running for the same tag is a no-op,
        # which is what makes this safe to wire into a release pipeline.
        print(f"  {agent}@{version} already published")
    elif status not in (200, 201):
        print(f"  ERROR publishing {agent}@{version}: HTTP {status} {body[:300]}", file=sys.stderr)
        return False
    else:
        print(f"  published {agent}@{version}")

    status, body = _request(
        "POST",
        f"/api/v1/agents/{agent}/releases",
        {"version": version, "environment": ENVIRONMENT},
    )
    if status not in (200, 201):
        print(f"  ERROR promoting {agent}@{version}: HTTP {status} {body[:300]}", file=sys.stderr)
        return False
    print(f"  promoted {agent}@{version} to {ENVIRONMENT}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="released version, e.g. 1.33.0 (no v prefix)")
    parser.add_argument("--agent", action="append", help="limit to this agent (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.version.startswith("v"):
        print("error: tags in this org carry no v prefix", file=sys.stderr)
        return 2

    try:
        git_sha = _git("rev-list", "-n", "1", args.version)
        tree = _tree_paths(args.version)
    except subprocess.CalledProcessError:
        print(f"error: no such tag: {args.version}", file=sys.stderr)
        return 2
    # The set of agents comes from the tag too, not from the working copy:
    # a manifest added after the release must not be published as part of it.
    manifest_prefix = str(MANIFEST_DIR.relative_to(REPO_ROOT)) + "/"
    in_tag = sorted(
        {p[len(manifest_prefix):].split("/", 1)[0] for p in tree if p.startswith(manifest_prefix)}
    )
    agents = args.agent or in_tag
    unknown = [a for a in agents if a not in in_tag]
    if unknown:
        print(f"error: no manifest in {args.version} for: {', '.join(unknown)}", file=sys.stderr)
        return 2

    print(f"publishing {len(agents)} agent(s) at {args.version} ({git_sha[:8]})")
    # Each agent is independent: one bad manifest must not abandon the
    # rest half-published, which would leave the registry in a state no
    # single re-run reproduces (agy P2).
    failed: list[str] = []
    for agent in agents:
        try:
            if not publish(agent, args.version, git_sha, tree, dry_run=args.dry_run):
                failed.append(agent)
        except (PublishError, httpx.HTTPError) as exc:
            print(f"  ERROR {agent}: {exc}", file=sys.stderr)
            failed.append(agent)
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
