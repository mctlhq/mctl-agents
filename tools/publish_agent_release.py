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

## Promotion requires a matching binding (mctlhq/mctl-agents#470)

Publishing and promoting are two different acts. Publishing records an
immutable version row; nothing resolves it until it is promoted. Promotion
to `production` is what activates an agent, and it used to follow every
publish unconditionally — so a merged agent.yaml self-activated.

Promotion is now gated per agent on its mctl-gitops release binding
(`check_binding_hash.evaluate_promotion`): the binding's pinned content hash
must equal the agent.yaml in this tag, and every field the resolver
cross-checks must agree. No binding, a stale one, or one that cannot be read
refuses that agent's promotion; the others go ahead. The version is still
published, so that once the binding is re-pinned the same release can be
promoted by hand (`mctl_promote_agent`) without re-publishing. The gate is
evaluated BEFORE any registry write, so an outage reading mctl-gitops never
leaves a half-done agent behind.

Exit status: 1 if any agent failed, or was refused because its binding is
stale, mismatched or unreadable — those name a binding someone meant to
match. A refusal for an absent binding (HTTP 404, the contents API's
documented absence signal) is a warning, not a failure, ONLY for the agents
listed in UNBOUND_AGENTS: it is the expected state of an agent nobody has
bound yet, and a release that is red every time is one nobody reads. A 404
for any other agent means its binding disappeared, and fails the step. Every agent's outcome is printed, and written to
$GITHUB_STEP_SUMMARY when it is set.

## prompt_hash

The registry stores a prompt_hash per version, but nothing in this repo
ever computed one: every historical row carries the same constant, copied
forward by hand, which makes it useless for detecting prompt drift — the
one thing it is for. This defines the hash instead:

    sha256 over each prompt source, in sorted order by its identifier,
    as length-prefixed "<len>\\n<identifier><len>\\n<file bytes>" pairs.

The length prefixes are what make the encoding unambiguous: plain
concatenation lets text move between one source's content and the next
source's identifier without changing the digest, so two different prompt
surfaces could hash equal and the drift would go unreported.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# Run as a plain script from the release job, and loaded by path in tests:
# either way tools/ is not a package, so make its sibling importable.
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import check_binding_hash  # noqa: E402

MANIFEST_DIR = REPO_ROOT / "agents" / "_manifests"
IMAGE_REPOSITORY = "ghcr.io/mctlhq/mctl-agents"
DEFAULT_API = "https://api.mctl.ai"
ENVIRONMENT = "production"
TIMEOUT_S = 30


class PublishError(RuntimeError):
    pass


# Agents that have no release binding in the mctl-gitops catalog yet. For
# these, and only these, a 404 is the expected state: their promotion is
# refused with a warning and the step stays green. For every other agent a
# 404 means a binding that existed was deleted or renamed, and that fails the
# step. Shrink this set in the same change that adds a binding (claude P2 on
# #574) — an agent left here after it is bound only loses the loud failure
# for a later deletion, never its gate.
UNBOUND_AGENTS = frozenset({"incident-responder", "mentor", "service-agent"})

PROMOTED = "promoted"
REFUSED = "refused"
FAILED = "failed"


@dataclass(frozen=True)
class Outcome:
    """What happened to one agent. `state` is PROMOTED, REFUSED or FAILED;
    `verdict` is the promotion gate's, when it got that far."""

    agent: str
    state: str
    detail: str
    verdict: check_binding_hash.PromotionVerdict | None = None

    @property
    def fails_release(self) -> bool:
        if self.state == FAILED:
            return True
        if self.state == REFUSED:
            # Only an absent binding for a known-unbound agent is a quiet
            # refusal; see the docstring and UNBOUND_AGENTS.
            if self.verdict is None or self.verdict.status != check_binding_hash.VERDICT_MISSING:
                return True
            return self.agent not in UNBOUND_AGENTS
        return False


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
    """Every file path in ``tag``'s tree.

    ``-z`` because git's default output C-quotes any path containing a
    space or a non-ASCII byte (``"agents/x/my prompt.md"``), quotes and
    all. Those literal quotes would then travel into the tree set and into
    `git show <tag>:<path>`, which cannot resolve them — so a prompt file
    with a space in its name would be reported as missing from a tree it
    is plainly in (agy P2). NUL-terminated output is never quoted.
    """
    raw = _git("ls-tree", "-r", "-z", "--name-only", tag)
    return [p for p in raw.split("\0") if p]


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
        # Length-prefixed, not newline-separated. Concatenating
        # "<identifier>\n<content>" with no bound between one entry's
        # content and the next entry's identifier makes the encoding
        # ambiguous: moving text out of one declared file and into
        # another's identifier can reproduce the exact same digest across
        # genuinely different prompt surfaces (agy P2). Prefixing each
        # part with its length makes the parse unique.
        identifier_bytes = identifier.encode()
        digest.update(f"{len(identifier_bytes)}\n".encode())
        digest.update(identifier_bytes)
        if relpath is None:
            digest.update(b"-\n")
            continue
        if relpath not in tree:
            # `git show <tag>:<dir>` prints a DIRECTORY LISTING and exits
            # 0, so a manifest naming a directory would hash its file
            # names while every edit inside it went unnoticed — drift
            # detection reporting "unchanged" for changed prompts (agy
            # P2). `tree` comes from `git ls-tree -r` without -t, so it
            # contains blobs only: membership is the blob check.
            raise PublishError(
                f"{agent}: prompt source {identifier} is not a file in {tag}'s tree"
            )
        content = _read_at_tag(tag, relpath)
        if content is None:
            raise PublishError(f"{agent}: prompt source {identifier} is not in {tag}'s tree")
        digest.update(f"{len(content)}\n".encode())
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def publish(
    agent: str,
    version: str,
    git_sha: str,
    tree: list[str],
    *,
    dry_run: bool,
    gitops_transport: httpx.BaseTransport | None = None,
) -> Outcome:
    relpath = f"agents/_manifests/{agent}/agent.yaml"
    raw = _read_at_tag(version, relpath)
    if raw is None:
        raise PublishError(f"{agent}: {relpath} is not in {version}'s tree")
    # Decided before any registry write, from the exact bytes being
    # released. evaluate_promotion turns every unreadable or invalid input
    # into a refusal; anything it still raises propagates to main(), which
    # records the agent as FAILED — never as promoted.
    verdict = check_binding_hash.evaluate_promotion(agent, raw, gitops_transport)
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
        if verdict.promote:
            print(f"  would promote {agent}@{version} to {ENVIRONMENT}: {verdict.reason}")
            return Outcome(agent, PROMOTED, f"(dry run) {verdict.reason}", verdict)
        print(f"  would REFUSE promoting {agent}@{version} ({verdict.status}): {verdict.reason}")
        return Outcome(agent, REFUSED, f"(dry run) {verdict.status}: {verdict.reason}", verdict)

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
            detail = f"creating definition: HTTP {create_status} {create_body[:300]}"
            print(f"  ERROR {agent}: {detail}", file=sys.stderr)
            return Outcome(agent, FAILED, detail, verdict)
        print(f"  created registry definition for {agent}")
        status, body = _request("POST", f"/api/v1/agents/{agent}/versions", payload)
    if status == 409:
        # Versions are immutable; re-running for the same tag is a no-op,
        # which is what makes this safe to wire into a release pipeline.
        print(f"  {agent}@{version} already published")
    elif status not in (200, 201):
        detail = f"publishing {version}: HTTP {status} {body[:300]}"
        print(f"  ERROR {agent}: {detail}", file=sys.stderr)
        return Outcome(agent, FAILED, detail, verdict)
    else:
        print(f"  published {agent}@{version}")

    if not verdict.promote:
        # The security boundary of #470: a version with no matching binding
        # stays published and inactive. Production keeps resolving whatever
        # was promoted before.
        print(
            f"  REFUSED promoting {agent}@{version} to {ENVIRONMENT} ({verdict.status}): {verdict.reason}",
            file=sys.stderr,
        )
        return Outcome(agent, REFUSED, f"{verdict.status}: {verdict.reason}", verdict)

    # No 409 allowance here, deliberately, unlike /versions above: promotion
    # is idempotent server-side — PromoteRelease returns 200 for a version
    # already released to this environment, and mctl-api's only 409 on this
    # route is ErrNoRollbackTarget, which needs rollback=true (a flag this
    # script never sends). A 409 here would therefore be a genuine surprise
    # and should fail loudly rather than be reported as success.
    status, body = _request(
        "POST",
        f"/api/v1/agents/{agent}/releases",
        {"version": version, "environment": ENVIRONMENT},
    )
    if status not in (200, 201):
        detail = f"promoting {version}: HTTP {status} {body[:300]}"
        print(f"  ERROR {agent}: {detail}", file=sys.stderr)
        return Outcome(agent, FAILED, detail, verdict)
    print(f"  promoted {agent}@{version} to {ENVIRONMENT}")
    return Outcome(agent, PROMOTED, verdict.reason, verdict)


def _report(version: str, outcomes: list[Outcome]) -> None:
    """One line per agent on stdout, an annotation per refusal or failure,
    and a table in the job summary: a refused promotion must be visible
    without reading the whole log."""
    in_actions = bool(os.environ.get("GITHUB_ACTIONS"))
    print(f"promotion summary for {version}:")
    for outcome in outcomes:
        print(f"  {outcome.agent}: {outcome.state} — {outcome.detail}")
        if in_actions and outcome.state != PROMOTED:
            level = "error" if outcome.fails_release else "warning"
            # Annotations are one line.
            text = f"{outcome.agent} {outcome.state}: {outcome.detail}".replace("\n", " ")
            print(f"::{level}::{text}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    lines = [
        f"### Agent registry: {version} → {ENVIRONMENT}",
        "",
        "| agent | outcome | detail |",
        "|---|---|---|",
    ]
    for outcome in outcomes:
        detail = outcome.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{outcome.agent}` | {outcome.state} | {detail} |")
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


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
    # Anchored on agent.yaml, not on "anything under _manifests/": a
    # README or OWNERS dropped in that directory would otherwise be read
    # as an agent name and fail the release looking for its manifest
    # (agy P3).
    in_tag = sorted(
        p[len(manifest_prefix):].split("/", 1)[0]
        for p in tree
        if p.startswith(manifest_prefix) and p.endswith("/agent.yaml")
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
    outcomes: list[Outcome] = []
    for agent in agents:
        try:
            outcomes.append(publish(agent, args.version, git_sha, tree, dry_run=args.dry_run))
        except Exception as exc:  # noqa: BLE001 — isolation is the point
            # Deliberately every exception, not just PublishError/HTTPError:
            # a malformed manifest reaches this loop as yaml.YAMLError, or
            # TypeError from json.dumps on a datetime, or AttributeError on
            # an empty document — and each of those aborted the whole run,
            # which is exactly the half-published registry this isolation
            # exists to prevent (agy P2). The run still fails below.
            print(f"  ERROR {agent}: {exc!r}", file=sys.stderr)
            outcomes.append(Outcome(agent, FAILED, repr(exc)))
    _report(args.version, outcomes)
    failed = [o.agent for o in outcomes if o.fails_release]
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
