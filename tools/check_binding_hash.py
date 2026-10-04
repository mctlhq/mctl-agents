#!/usr/bin/env python3
"""Fail when agent.yaml has drifted from the gitops release binding's pin.

mctlhq/mctl-agents#565. Since mctl-gitops#1585 the investigate CWFT runs
`ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`, so every investigation
recomputes the sha256 of `agents/_manifests/issue-investigator/agent.yaml`
inside the image and compares it with `spec.sourceManifest.contentHash` in
mctl-gitops' `releases/shadow/issue-investigator.yaml`. A mismatch raises
`ResolverError` before the model runs. Nothing checked that pair before a
release, so a release that changed agent.yaml would have shipped and been
promoted green, and then failed every investigation in production.

This runs the same comparison ahead of time, against the binding on
mctl-gitops `main` — the one the CWFT clones at run time:

    uv run --locked python tools/check_binding_hash.py

Both halves come from the resolver itself rather than being reimplemented:
the local hash is `resolver.load_definition(...).content_hash`, the value
`execute()` compares, the fetched binding goes through
`resolver.parse_yaml_mapping` and `resolver.parse_release_binding`, and the
remaining binding-vs-definition checks are `execute()`'s own
`check_binding_against_definition` and `check_profile_compatibility`. A binding
this accepts is one the resolver would accept, and the two cannot hash or
parse differently.

Exit codes keep "could not observe" apart from "observed a mismatch":

    0  the pins match
    1  the pins differ (contentHash or a field execute() cross-checks), or
       the local agent.yaml is itself unresolvable
    2  the binding, or the execution profile it names, could not be read or
       validated (network error, non-200, malformed YAML, missing or invalid
       contentHash or spec.version). Never a match.

The re-pin procedure is docs/runbooks/agent-yaml-binding-repin.md.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
# tools/ is not a package, and the release job runs this as a plain script.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator import resolver  # noqa: E402

AGENT = "issue-investigator"
ENVIRONMENT = resolver.DEFAULT_ENVIRONMENT
GITOPS_REPO = "mctlhq/mctl-gitops"
GITOPS_REF = "main"
BINDING_PATH = f"platform-gitops/agent-platform/releases/{ENVIRONMENT}/{AGENT}.yaml"
# The contents API rather than raw.githubusercontent.com: the raw CDN caches
# for up to five minutes, so right after a re-pin merges it would keep
# serving the old hash and fail a release that is in fact correct.
CONTENTS_API = f"https://api.github.com/repos/{GITOPS_REPO}/contents"
BINDING_URL = f"{CONTENTS_API}/{BINDING_PATH}"
PROFILES_PATH = "platform-gitops/agent-platform/execution-profiles"
RUNBOOK = "docs/runbooks/agent-yaml-binding-repin.md"
TIMEOUT_S = 20.0

EXIT_MATCH = 0
EXIT_MISMATCH = 1
EXIT_UNOBSERVED = 2


class BindingUnobservable(RuntimeError):
    """The binding could not be read or validated. Not a mismatch and never
    a match: the gate fails on it either way, with its own exit code."""


def fetch_binding(transport: httpx.BaseTransport | None = None) -> bytes:
    """Return the raw binding bytes from mctl-gitops `main`.

    Anything but a 200 is `BindingUnobservable`. A 404 included:
    the binding being absent would make the resolver fail closed at run time
    too ("missing release"), so it is not a state a release may ship into.
    """
    return _fetch(BINDING_URL, transport)


def fetch_profile(name: str, transport: httpx.BaseTransport | None = None) -> bytes:
    """Return the raw `ExecutionProfile` bytes for `name` from mctl-gitops
    `main`, with the same failure semantics as `fetch_binding`."""
    return _fetch(f"{CONTENTS_API}/{PROFILES_PATH}/{name}/profile.yaml", transport)


def _fetch(url: str, transport: httpx.BaseTransport | None) -> bytes:
    headers = {"Accept": "application/vnd.github.raw", "X-GitHub-Api-Version": "2022-11-28"}
    # Optional: the repository is public. In Actions the token lifts the
    # 60-requests-per-hour anonymous limit that shared runner IPs can hit.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT_S, follow_redirects=True) as client:
            response = client.get(url, params={"ref": GITOPS_REF}, headers=headers)
    except httpx.HTTPError as exc:
        raise BindingUnobservable(f"could not fetch {url}: {exc!r}") from exc
    if response.status_code != 200:
        raise BindingUnobservable(
            f"could not fetch {url}: HTTP {response.status_code} {response.text[:200]!r}"
        )
    # An empty 200 needs no case of its own: it parses to no mapping, which
    # `parse_binding` and `parse_profile_version` already refuse.
    return response.content


def parse_binding(raw: bytes) -> resolver.ReleaseBinding:
    """A fetched binding, validated by the resolver's own parser. Any
    `ResolverError` — malformed YAML, a missing or non-`sha256:` contentHash,
    a wrong agent or environment — becomes `BindingUnobservable`."""
    label = Path(f"{GITOPS_REPO}@{GITOPS_REF}") / BINDING_PATH
    try:
        document = resolver.parse_yaml_mapping(raw, path=label)
        binding = resolver.parse_release_binding(document, path=label, agent=AGENT, environment=ENVIRONMENT)
    except resolver.ResolverError as exc:
        raise BindingUnobservable(str(exc)) from exc
    return binding


def parse_profile_version(raw: bytes, name: str) -> str:
    """The declared spec.version of a fetched `ExecutionProfile`. Only the
    fields this gate needs are checked, as `load_profile` checks them; the
    rest of the profile is mctl-gitops' to validate. Anything unreadable is
    `BindingUnobservable`."""
    label = Path(f"{GITOPS_REPO}@{GITOPS_REF}") / PROFILES_PATH / name / "profile.yaml"
    try:
        document = resolver.parse_yaml_mapping(raw, path=label)
    except resolver.ResolverError as exc:
        raise BindingUnobservable(str(exc)) from exc
    metadata = document.get("metadata")
    spec = document.get("spec")
    if document.get("kind") != "ExecutionProfile" or not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise BindingUnobservable(f"{label}: not an ExecutionProfile with metadata and spec")
    if metadata.get("name") != name:
        raise BindingUnobservable(f"{label}: metadata.name {metadata.get('name')!r} is not {name!r}")
    version = spec.get("version")
    if not isinstance(version, str) or not version:
        raise BindingUnobservable(f"{label}: spec.version is required")
    return version


def _error(message: str) -> None:
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") else "error: "
    # Annotations are one line; keep the detail in the plain lines after it.
    first, _, rest = message.partition("\n")
    print(prefix + first, file=sys.stderr)
    if rest:
        print(rest, file=sys.stderr)


def check(transport: httpx.BaseTransport | None = None) -> int:
    try:
        definition = resolver.load_definition(AGENT)
    except resolver.ResolverError as exc:
        _error(f"agent.yaml for {AGENT} does not resolve locally: {exc}")
        return EXIT_MISMATCH
    local_hash = definition.content_hash
    definition_path = resolver.DEFINITIONS_DIR / AGENT / "agent.yaml"
    relpath = (
        definition_path.relative_to(resolver.REPO_ROOT)
        if definition_path.is_relative_to(resolver.REPO_ROOT)
        else definition_path
    )

    try:
        binding = parse_binding(fetch_binding(transport))
    except BindingUnobservable as exc:
        _error(
            f"binding check FAILED CLOSED: the {ENVIRONMENT} binding for {AGENT} could not be read, "
            "so whether it matches is unknown, and unknown is not a match.\n"
            f"  {exc}\n"
            f"  local {relpath}: {local_hash}"
        )
        return EXIT_UNOBSERVED

    pinned_hash, revision = binding.definition_content_hash, binding.release_revision
    if pinned_hash != local_hash:
        _error(
            f"{relpath} does not match the mctl-gitops {ENVIRONMENT} binding: every declarative "
            "investigation would fail with ResolverError.\n"
            f"  {relpath} sha256:     {local_hash}\n"
            f"  {GITOPS_REPO}@{GITOPS_REF}:{BINDING_PATH}\n"
            f"    spec.sourceManifest.contentHash (bindingRevision {revision}): {pinned_hash}\n"
            f"Re-pin spec.sourceManifest.contentHash in mctl-gitops; see {RUNBOOK} for the order of steps."
        )
        return EXIT_MISMATCH

    # The hash is necessary, not sufficient: execute() also requires the
    # binding's definition.name, profile.name and profileCompatibility mirror
    # to agree with agent.yaml, the binding's profile.version to be the
    # catalog profile's spec.version, and that version to satisfy agent.yaml's
    # range. A re-pin that updated only the hash would pass the comparison
    # above and still fail every run, so the gate runs the resolver's own
    # checks too, against the profile on the same gitops ref.
    try:
        resolver.check_binding_against_definition(binding, definition)
    except resolver.ResolverError as exc:
        return _mirrored_mismatch(relpath, exc)
    try:
        profile_version = parse_profile_version(fetch_profile(binding.profile_name, transport), binding.profile_name)
    except BindingUnobservable as exc:
        _error(
            f"binding check FAILED CLOSED: the execution profile {binding.profile_name!r} could not be read, "
            "so whether the binding matches it is unknown, and unknown is not a match.\n"
            f"  {exc}"
        )
        return EXIT_UNOBSERVED
    try:
        resolver.check_binding_profile_version(binding, profile_version)
        resolver.check_profile_compatibility(definition, profile_version)
    except resolver.ResolverError as exc:
        return _mirrored_mismatch(relpath, exc)

    print(f"ok: {relpath} matches {GITOPS_REPO}@{GITOPS_REF} bindingRevision {revision} ({local_hash})")
    return EXIT_MATCH


def _mirrored_mismatch(relpath: Path, exc: resolver.ResolverError) -> int:
    _error(
        f"{relpath} matches the {ENVIRONMENT} binding's contentHash, but the binding disagrees with it "
        "elsewhere: every declarative investigation would fail with ResolverError.\n"
        f"  {exc}\n"
        f"Re-pin the mirrored fields in mctl-gitops too; see {RUNBOOK}."
    )
    return EXIT_MISMATCH


if __name__ == "__main__":
    sys.exit(check())
