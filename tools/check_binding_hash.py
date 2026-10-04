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
`check_binding_against_definition`, `check_binding_profile_version` and
`check_profile_compatibility`, the last two against the execution profile
read from the same mctl-gitops ref. A binding
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

mctlhq/mctl-agents#470 generalises the same comparison to every agent:
`evaluate_promotion(agent, raw_agent_yaml)` is what
tools/publish_agent_release.py asks before it promotes an agent to
production. It reuses every piece above — the fetch, the resolver's parser,
the resolver's hash and cross-checks — and returns a verdict instead of an
exit code, so one refused agent does not decide the others.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
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
RELEASES_PATH = f"platform-gitops/agent-platform/releases/{ENVIRONMENT}"
BINDING_PATH = f"{RELEASES_PATH}/{AGENT}.yaml"
# The contents API rather than raw.githubusercontent.com: the raw CDN caches
# for up to five minutes, so right after a re-pin merges it would keep
# serving the old hash and fail a release that is in fact correct.
CONTENTS_API = f"https://api.github.com/repos/{GITOPS_REPO}/contents"
BINDING_URL = f"{CONTENTS_API}/{BINDING_PATH}"
PROFILES_PATH = "platform-gitops/agent-platform/execution-profiles"
RUNBOOK = "docs/runbooks/agent-yaml-binding-repin.md"
TIMEOUT_S = 20.0
# Transport errors and 5xx are retried: the release reads two files per
# agent, and one GitHub blip must not cost a manual promotion — a workflow
# re-run cannot redo it, because release-please reports release_created only
# once. 429 (GitHub's secondary rate limit, which it documents as
# retryable) is retried too. 404 and 403 (the primary rate limit) are
# answers, not blips, and are not retried.
FETCH_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
# A 429's Retry-After (normally 60s) is honoured up to this cap, so one hint
# cannot stall the release step. A window longer than the cap still refuses
# as unobservable: wait it out and promote by hand, do not re-pin.
RETRY_AFTER_CAP_S = 60.0

EXIT_MATCH = 0
EXIT_MISMATCH = 1
EXIT_UNOBSERVED = 2


class BindingUnobservable(RuntimeError):
    """The binding could not be read or validated. Not a mismatch and never
    a match: the gate fails on it either way, with its own exit code."""


class BindingMissing(BindingUnobservable):
    """GitHub answered 404: there is no binding at that path on the ref.

    A subclass, so every caller that only knows `BindingUnobservable` keeps
    failing closed on it exactly as before. Only the promotion gate tells
    the two apart, and only to word its refusal — both refuse."""


def binding_path(agent: str) -> str:
    return f"{RELEASES_PATH}/{agent}.yaml"


def fetch_binding(transport: httpx.BaseTransport | None = None, *, agent: str = AGENT) -> bytes:
    """Return the raw binding bytes for `agent` from mctl-gitops `main`.

    Anything but a 200 is `BindingUnobservable`. A 404 included (as its
    `BindingMissing` subclass): the binding being absent would make the
    resolver fail closed at run time too ("missing release"), so it is not a
    state a release may ship into.
    """
    return _fetch(f"{CONTENTS_API}/{binding_path(agent)}", transport)


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
    response: httpx.Response | None = None
    # One client for every attempt. Closing a client closes its transport,
    # so an injected transport belongs to the caller and is never closed
    # here: the binding read and the profile read share it, and a client per
    # attempt would hand it back closed to the next one (claude P3 on #574).
    client = httpx.Client(transport=transport, timeout=TIMEOUT_S, follow_redirects=True)
    try:
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                response = client.get(url, params={"ref": GITOPS_REF}, headers=headers)
            except httpx.TransportError as exc:
                if attempt == FETCH_ATTEMPTS:
                    raise BindingUnobservable(
                        f"could not fetch {url} after {attempt} attempts: {exc!r}"
                    ) from exc
            except httpx.HTTPError as exc:
                raise BindingUnobservable(f"could not fetch {url}: {exc!r}") from exc
            else:
                retryable = response.status_code >= 500 or response.status_code == 429
                if not retryable or attempt == FETCH_ATTEMPTS:
                    break
                if response.status_code == 429:
                    time.sleep(_retry_after(response, default=RETRY_BACKOFF_S * attempt))
                    continue
            time.sleep(RETRY_BACKOFF_S * attempt)
    finally:
        if transport is None:
            client.close()
    if response is None:  # unreachable: every attempt without one raised
        raise BindingUnobservable(f"could not fetch {url}: no response")
    # 404 is the contents API's documented "no such path on this ref". Every
    # other non-200 (403/429 rate limits, 5xx) says nothing about the binding.
    if response.status_code == 404:
        raise BindingMissing(f"no file at {url} on {GITOPS_REF} (HTTP 404)")
    if response.status_code != 200:
        raise BindingUnobservable(
            f"could not fetch {url}: HTTP {response.status_code} {response.text[:200]!r}"
        )
    # An empty 200 needs no case of its own: it parses to no mapping, which
    # `parse_binding` and `parse_profile_version` already refuse.
    return response.content


def _retry_after(response: httpx.Response, *, default: float) -> float:
    """Seconds a 429 asks us to wait, capped at RETRY_AFTER_CAP_S. The
    header may also be an HTTP-date; anything unparseable uses `default`."""
    try:
        wait = float(response.headers.get("Retry-After", default))
    except ValueError:
        wait = default
    return max(0.0, min(wait, RETRY_AFTER_CAP_S))


def parse_binding(raw: bytes, *, agent: str = AGENT) -> resolver.ReleaseBinding:
    """A fetched binding, validated by the resolver's own parser. Any
    `ResolverError` — malformed YAML, a missing or non-`sha256:` contentHash,
    a wrong agent or environment — becomes `BindingUnobservable`."""
    label = Path(f"{GITOPS_REPO}@{GITOPS_REF}") / binding_path(agent)
    try:
        document = resolver.parse_yaml_mapping(raw, path=label)
        binding = resolver.parse_release_binding(document, path=label, agent=agent, environment=ENVIRONMENT)
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
    # apiVersion and kind are contracts between the two repositories, as
    # load_profile enforces them; the rest of the profile's shape is not.
    if document.get("apiVersion") != resolver.SUPPORTED_PROFILE_API_VERSION:
        raise BindingUnobservable(f"{label}: unsupported profile apiVersion {document.get('apiVersion')!r}")
    if document.get("kind") != "ExecutionProfile" or not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise BindingUnobservable(f"{label}: not an ExecutionProfile with metadata and spec")
    if metadata.get("name") != name:
        raise BindingUnobservable(f"{label}: metadata.name {metadata.get('name')!r} is not {name!r}")
    version = spec.get("version")
    if not isinstance(version, str) or not version:
        raise BindingUnobservable(f"{label}: spec.version is required")
    # An unparseable version is an invalid profile, not a mismatch with
    # agent.yaml: without this it would surface from the compatibility check
    # as exit 1, naming agent.yaml for a fault that lives in mctl-gitops.
    try:
        resolver._parse_version(version, path=label, field_path="spec.version")
    except resolver.ResolverError as exc:
        raise BindingUnobservable(str(exc)) from exc
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


# ---------------------------------------------------------------------------
# Production promotion gate (mctlhq/mctl-agents#470)
# ---------------------------------------------------------------------------
VERDICT_MATCH = "match"
VERDICT_MISSING = "missing"
VERDICT_MISMATCH = "mismatch"
VERDICT_UNOBSERVED = "unobservable"
# The manifest format the non-resolver agents still use. It has no
# executionProfileRef, so only the hash, the name and the profile pin apply.
LEGACY_DEFINITION_API_VERSION = "agents.mctl.ai/v1alpha1"


@dataclass(frozen=True)
class PromotionVerdict:
    """Whether one agent's released agent.yaml may be promoted to
    production. Only `VERDICT_MATCH` promotes; every other status is a
    refusal, and `reason` says which and why."""

    agent: str
    status: str
    reason: str

    @property
    def promote(self) -> bool:
        return self.status == VERDICT_MATCH


def evaluate_promotion(
    agent: str, raw_definition: bytes, transport: httpx.BaseTransport | None = None
) -> PromotionVerdict:
    """Compare the agent.yaml bytes being released with `agent`'s binding on
    mctl-gitops `main`, using the same checks as `check()`.

    - the binding's spec.sourceManifest.contentHash must equal
      `resolver.definition_content_hash(raw_definition)`;
    - a v1alpha2 AgentDefinition must also pass the resolver's mirrored-field
      checks and its profile-compatibility range, exactly as `check()` runs
      them for issue-investigator;
    - a v1alpha1 manifest has no executionProfileRef to mirror, so it gets
      the hash, the definition name, and the profile pin;
    - the profile the binding pins (name and version — the binding schema
      requires both) must exist in the catalog at that spec.version.

    A binding or profile that cannot be read or validated is
    `VERDICT_UNOBSERVED`, never a match. A 404 on the binding is
    `VERDICT_MISSING`. Both refuse.
    """
    local_hash = resolver.definition_content_hash(raw_definition)

    def refuse(status: str, reason: str) -> PromotionVerdict:
        # Every refusal names the runbook: it is the one place that lists the
        # remedies (re-pin, add the binding, or list the agent as unbound).
        return PromotionVerdict(agent, status, f"{reason}; see {RUNBOOK}")

    try:
        binding = parse_binding(fetch_binding(transport, agent=agent), agent=agent)
    except BindingMissing:
        return refuse(
            VERDICT_MISSING,
            f"no release binding for {agent} at {GITOPS_REPO}@{GITOPS_REF}:{binding_path(agent)} "
            f"(releases/{ENVIRONMENT}/ is the only binding catalog, the one the resolver reads; "
            "there is no releases/production/)",
        )
    except BindingUnobservable as exc:
        return refuse(VERDICT_UNOBSERVED, f"binding could not be read, and unknown is not a match: {exc}")

    revision = binding.release_revision
    if binding.definition_content_hash != local_hash:
        return refuse(
            VERDICT_MISMATCH,
            f"stale binding: bindingRevision {revision} pins {binding.definition_content_hash}, "
            f"the released agent.yaml is {local_hash}",
        )

    # First: does the released agent.yaml resolve at all? A fault here lives
    # in this repository, not in the binding, and the reason says so.
    definition_path = resolver.DEFINITIONS_DIR / agent / "agent.yaml"
    definition: resolver.AgentDefinition | None = None
    try:
        document = resolver.parse_yaml_mapping(raw_definition, path=definition_path)
        api_version = document.get("apiVersion")
        if api_version == resolver.SUPPORTED_DEFINITION_API_VERSION:
            definition = resolver.parse_definition(raw_definition, path=definition_path)
        elif api_version != LEGACY_DEFINITION_API_VERSION:
            # Strict by default: a new or mistyped apiVersion must not fall
            # into the lenient v1alpha1 path below.
            raise resolver.ResolverError(f"{definition_path}: unsupported apiVersion {api_version!r}")
    except resolver.ResolverError as exc:
        return refuse(VERDICT_MISMATCH, f"the released agent.yaml does not resolve: {exc}")

    try:
        if definition is not None:
            resolver.check_binding_against_definition(binding, definition)
        else:
            metadata = document.get("metadata")
            name = metadata.get("name") if isinstance(metadata, dict) else None
            if binding.definition_name != name:
                raise resolver.ResolverError(
                    f"release binding definition.name {binding.definition_name!r} does not match "
                    f"agent.yaml metadata.name {name!r}"
                )
    except resolver.ResolverError as exc:
        return refuse(VERDICT_MISMATCH, f"binding disagrees with the released agent.yaml: {exc}")

    try:
        profile_version = parse_profile_version(fetch_profile(binding.profile_name, transport), binding.profile_name)
    except BindingUnobservable as exc:
        return refuse(
            VERDICT_UNOBSERVED,
            f"execution profile {binding.profile_name!r} could not be read, and unknown is not a match: {exc}",
        )
    try:
        resolver.check_binding_profile_version(binding, profile_version)
        if definition is not None:
            resolver.check_profile_compatibility(definition, profile_version)
    except resolver.ResolverError as exc:
        return refuse(VERDICT_MISMATCH, f"binding disagrees with the catalog profile: {exc}")

    return PromotionVerdict(
        agent,
        VERDICT_MATCH,
        f"matches {GITOPS_REPO}@{GITOPS_REF} bindingRevision {revision} ({local_hash}, "
        f"profile {binding.profile_name}@{profile_version})",
    )


if __name__ == "__main__":
    sys.exit(check())
