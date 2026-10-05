"""Tests for tools/check_agent_bindings.py: the pre-tag binding check over
every agent manifest (mctlhq/mctl-agents#582).

A detector in both directions, for every agent rather than one: green when
all bindings pin the manifests on disk, red on a one-byte edit of any of
them, red when a manifest has no binding at all, and a different red when a
binding or profile could not be read. mctl-gitops is an
`httpx.MockTransport` throughout, and the manifests are a copy in `tmp_path`,
so the suite is offline and never edits the repository's own files. The live
comparison runs as the `binding hash` CI job and the release `binding gate`.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from orchestrator import resolver

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOL = _REPO_ROOT / "tools" / "check_agent_bindings.py"
_spec = importlib.util.spec_from_file_location("check_agent_bindings", _TOOL)
assert _spec and _spec.loader
tool = importlib.util.module_from_spec(_spec)
sys.modules["check_agent_bindings"] = tool
_spec.loader.exec_module(tool)
# The copies the tool itself holds: other test modules load their own.
gate = tool.check_binding_hash
release = tool.publish_agent_release

_REAL_MANIFESTS = _REPO_ROOT / "agents" / "_manifests"
_AGENTS = sorted(p.parent.name for p in _REAL_MANIFESTS.glob("*/agent.yaml"))
_V2 = "issue-investigator"
_PROFILES = {agent: (f"{agent}-default", "1.0.0") for agent in _AGENTS}
_PROFILES[_V2] = ("issue-investigator-default", "1.5.1")
# Not a manifest in this repository: the stand-in for an agent deliberately
# shipped without a binding, since no real agent is in that state today.
_UNBOUND = "unbound-fixture"
_NEW = "added-at-the-release-commit"

Responder = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _offline_and_quiet(monkeypatch):
    monkeypatch.setattr(gate, "RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(gate.time, "sleep", lambda seconds: None)
    for name in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def manifests(tmp_path, monkeypatch) -> Path:
    """A copy of the six real manifests that the tool and the resolver both
    read, standing in for the working tree at the commit being checked."""
    root = tmp_path / "_manifests"
    shutil.copytree(_REAL_MANIFESTS, root)
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", root)
    return root


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _binding(agent: str, raw: bytes, *, profile: tuple[str, str] | None = None) -> bytes:
    profile_name, profile_version = profile or _PROFILES[agent]
    doc: dict[str, Any] = {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ReleaseBindingIntent",
        "metadata": {"agent": agent, "environment": "shadow"},
        "spec": {
            "sourceManifest": {
                "repo": "mctlhq/mctl-agents",
                "path": f"agents/_manifests/{agent}/agent.yaml",
                "contentHash": _sha256(raw),
            },
            "bindingSource": "compatibility-fixture",
            "promotable": False,
            "registryLifecycle": {"definition": "published", "profile": "published"},
            "definition": {"name": agent, "version": "1", "profileCompatibility": ">=1.0.0 <2.0.0"},
            "profile": {"name": profile_name, "version": profile_version},
            "bindingRevision": 3,
        },
    }
    return yaml.safe_dump(doc, sort_keys=False).encode()


def _profile(name: str, version: str) -> bytes:
    return yaml.safe_dump({
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ExecutionProfile",
        "metadata": {"name": name},
        "spec": {"version": version},
    }).encode()


def _pinned() -> dict[str, bytes | Responder]:
    """Bindings that pin the committed manifests: what mctl-gitops holds
    when everything is converged."""
    return {agent: _binding(agent, (_REAL_MANIFESTS / agent / "agent.yaml").read_bytes()) for agent in _AGENTS}


def _gitops(
    bindings: dict[str, bytes | Responder], profiles: dict[str, bytes | Responder] | None = None
) -> httpx.MockTransport:
    """Serve bindings by agent and profiles by name; anything else is a 404,
    which is what GitHub's contents API answers for an absent path."""
    served: dict[str, bytes | Responder] = {name: _profile(name, version) for name, version in _PROFILES.values()}
    served.update(profiles or {})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.url.params["ref"] == "main"
        if path.endswith("/profile.yaml"):
            entry = served.get(path.rsplit("/", 2)[-2])
        else:
            entry = bindings.get(path.rsplit("/", 1)[-1].removesuffix(".yaml"))
        if entry is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(entry):
            return entry(request)
        return httpx.Response(200, content=entry)

    return httpx.MockTransport(handler)


def _status(out: str) -> dict[str, str]:
    """`label -> status` for every reported line."""
    rows: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("  ") and " — " in line and ": " in line:
            label, _, rest = line.strip().partition(": ")
            rows[label] = rest.split(" — ", 1)[0].split(" (", 1)[0]
    return rows


def _one_byte_edit(manifests: Path, agent: str) -> None:
    with (manifests / agent / "agent.yaml").open("ab") as fh:
        fh.write(b"\n")


def _add_manifest(manifests: Path, name: str) -> bytes:
    shepherd = (_REAL_MANIFESTS / "shepherd" / "agent.yaml").read_bytes()
    raw = shepherd.replace(b"name: shepherd", f"name: {name}".encode())
    assert raw != shepherd
    (manifests / name).mkdir()
    (manifests / name / "agent.yaml").write_bytes(raw)
    return raw


# ---------------------------------------------------------------------------
# Green on a converged input
# ---------------------------------------------------------------------------
def test_six_manifests_are_what_this_suite_covers():
    assert len(_AGENTS) == 6
    assert _V2 in _AGENTS


def test_all_matching_bindings_pass(manifests, capsys):
    assert tool.check_all(_gitops(_pinned())) == gate.EXIT_MATCH

    rows = _status(capsys.readouterr().out)
    assert rows == {tool.RESOLVER_CHECK_LABEL: "match", **dict.fromkeys(_AGENTS, "match")}


def test_every_manifest_is_asked_the_releases_own_question(manifests, monkeypatch):
    """Not a reimplementation: the verdict is evaluate_promotion's, on the
    bytes on disk, which is what the release reads out of the tag."""
    asked: list[tuple[str, bytes]] = []
    real = gate.evaluate_promotion

    def spy(agent, raw, transport=None):
        asked.append((agent, raw))
        return real(agent, raw, transport)

    monkeypatch.setattr(gate, "evaluate_promotion", spy)
    tool.check_all(_gitops(_pinned()))

    assert asked == [(agent, (manifests / agent / "agent.yaml").read_bytes()) for agent in _AGENTS]


# ---------------------------------------------------------------------------
# Red on a deliberate mutation, for every agent
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("agent", _AGENTS)
def test_a_one_byte_edit_of_any_manifest_is_a_mismatch(agent, manifests, capsys):
    _one_byte_edit(manifests, agent)

    assert tool.check_all(_gitops(_pinned())) == gate.EXIT_MISMATCH

    out = capsys.readouterr().out
    rows = _status(out)
    # All six are reported, and only the edited one is refused.
    assert {a: rows[a] for a in _AGENTS} == {a: ("mismatch" if a == agent else "match") for a in _AGENTS}
    assert "stale binding" in out
    assert gate.RUNBOOK in out


def test_a_manifest_with_no_binding_is_missing(manifests, capsys):
    bindings = _pinned()
    del bindings["mentor"]

    assert tool.check_all(_gitops(bindings)) == gate.EXIT_MISMATCH

    out = capsys.readouterr().out
    rows = _status(out)
    assert rows["mentor"] == "missing"
    assert [a for a in _AGENTS if rows[a] != "match"] == ["mentor"]
    assert "no release binding for mentor" in out


def test_a_manifest_only_the_checked_commit_has_is_evaluated(manifests, capsys):
    """The release gate replaces agents/_manifests with the release
    commit's. A directory that commit adds, and that nobody bound, has to be
    found here and not in "Refresh agent registry"."""
    _add_manifest(manifests, _NEW)

    assert tool.check_all(_gitops(_pinned())) == gate.EXIT_MISMATCH

    rows = _status(capsys.readouterr().out)
    assert rows[_NEW] == "missing"
    assert all(rows[a] == "match" for a in _AGENTS)


# ---------------------------------------------------------------------------
# Could not observe is neither a match, nor a mismatch, nor missing
# ---------------------------------------------------------------------------
def _five_hundred(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, content=b"upstream unavailable")


def _unobservable_cases() -> dict[str, tuple[dict[str, bytes | Responder], dict[str, bytes | Responder]]]:
    """(binding overrides, profile overrides), each for `service-agent`,
    which sorts after four agents whose reads have already succeeded."""
    profile_name = _PROFILES["service-agent"][0]
    return {
        "binding 5xx": ({"service-agent": _five_hundred}, {}),
        "binding 403 rate limit": ({"service-agent": lambda r: httpx.Response(403, content=b"rate limit")}, {}),
        "binding malformed body": ({"service-agent": b"spec: [unclosed\n  - : :"}, {}),
        "binding empty 200": ({"service-agent": b""}, {}),
        "binding transport error": (
            {"service-agent": lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r))},
            {},
        ),
        # The binding read succeeds and the profile read after it fails.
        "profile 5xx after the binding was read": ({}, {profile_name: _five_hundred}),
        "profile malformed body": ({}, {profile_name: b"spec: [unclosed"}),
    }


@pytest.mark.parametrize("case", list(_unobservable_cases()))
def test_an_unreadable_binding_or_profile_is_unobservable(case, manifests, capsys):
    binding_overrides, profile_overrides = _unobservable_cases()[case]

    rc = tool.check_all(_gitops({**_pinned(), **binding_overrides}, profile_overrides))

    assert rc == gate.EXIT_UNOBSERVED
    rows = _status(capsys.readouterr().out)
    assert rows["service-agent"] == "unobservable"
    # The failure hides nobody: agents before and after it are still reported.
    assert [a for a in _AGENTS if rows[a] != "match"] == ["service-agent"]
    assert rows["shepherd"] == "match"


def test_a_missing_profile_is_unobservable_not_a_missing_binding(manifests, capsys):
    """404 means "no binding" only when it is the binding that is absent. A
    profile the binding names and the catalog lacks is a broken catalog."""
    gone = {_PROFILES["mentor"][0]: lambda r: httpx.Response(404, json={"message": "Not Found"})}

    assert tool.check_all(_gitops(_pinned(), gone)) == gate.EXIT_UNOBSERVED
    assert _status(capsys.readouterr().out)["mentor"] == "unobservable"


def test_a_mismatch_is_not_masked_by_another_agents_failed_read(manifests, capsys):
    """check()'s convention, kept across agents: an observed mismatch is
    reported as one (exit 1, re-pin), and the agent that could not be read
    keeps its own status rather than borrowing either neighbour's."""
    _one_byte_edit(manifests, "implementer")

    rc = tool.check_all(_gitops({**_pinned(), "shepherd": _five_hundred}))

    assert rc == gate.EXIT_MISMATCH
    rows = _status(capsys.readouterr().out)
    assert rows["implementer"] == "mismatch"
    assert rows["shepherd"] == "unobservable"
    assert all(rows[a] == "match" for a in _AGENTS if a not in ("implementer", "shepherd"))


def test_an_evaluation_that_raises_is_unobservable_and_hides_nobody(manifests, monkeypatch, capsys):
    real = gate.evaluate_promotion

    def flaky(agent, raw, transport=None):
        if agent == "incident-responder":
            raise RuntimeError("boom")
        return real(agent, raw, transport)

    monkeypatch.setattr(gate, "evaluate_promotion", flaky)

    assert tool.check_all(_gitops(_pinned())) == gate.EXIT_UNOBSERVED
    rows = _status(capsys.readouterr().out)
    assert rows["incident-responder"] == "unobservable"
    assert [a for a in _AGENTS if rows[a] != "match"] == ["incident-responder"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through file modes")
def test_an_unreadable_local_manifest_is_unobservable(manifests, capsys):
    path = manifests / "mentor" / "agent.yaml"
    path.chmod(0)
    try:
        rc = tool.check_all(_gitops(_pinned()))
    finally:
        path.chmod(0o644)

    assert rc == gate.EXIT_UNOBSERVED
    assert _status(capsys.readouterr().out)["mentor"] == "unobservable"


def test_no_manifest_at_all_is_not_a_pass(tmp_path, monkeypatch, capsys):
    """An empty listing is a check that looked in the wrong place. The
    single-agent check is stubbed green so that only the listing decides."""
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(gate, "check", lambda transport=None: gate.EXIT_MATCH)

    assert tool.check_all(_gitops({})) == gate.EXIT_UNOBSERVED
    out = capsys.readouterr().out
    assert "no */agent.yaml under" in out
    assert "0 manifest(s)" in out


# ---------------------------------------------------------------------------
# UNBOUND_AGENTS, exactly as the release reads it
# ---------------------------------------------------------------------------
def test_a_missing_binding_for_a_listed_unbound_agent_is_quiet(manifests, monkeypatch, capsys):
    _add_manifest(manifests, _UNBOUND)
    monkeypatch.setattr(release, "UNBOUND_AGENTS", frozenset({_UNBOUND}))

    assert tool.check_all(_gitops(_pinned())) == gate.EXIT_MATCH

    out = capsys.readouterr().out
    assert _status(out)[_UNBOUND] == "missing"
    assert f"  {_UNBOUND}: missing (listed in UNBOUND_AGENTS: not a failure)" in out


def test_listing_an_agent_as_unbound_quiets_only_its_missing_binding(manifests, monkeypatch, capsys):
    """A binding that exists and is stale names something someone meant to
    match, listed or not."""
    raw = _add_manifest(manifests, _UNBOUND)
    monkeypatch.setattr(release, "UNBOUND_AGENTS", frozenset({_UNBOUND}))
    stale = _binding(_UNBOUND, raw + b"\n", profile=_PROFILES["shepherd"])

    assert tool.check_all(_gitops({**_pinned(), _UNBOUND: stale})) == gate.EXIT_MISMATCH
    assert _status(capsys.readouterr().out)[_UNBOUND] == "mismatch"


def test_an_unreadable_binding_for_a_listed_unbound_agent_is_not_quiet(manifests, monkeypatch):
    _add_manifest(manifests, _UNBOUND)
    monkeypatch.setattr(release, "UNBOUND_AGENTS", frozenset({_UNBOUND}))

    assert tool.check_all(_gitops({**_pinned(), _UNBOUND: _five_hundred})) == gate.EXIT_UNOBSERVED


# ---------------------------------------------------------------------------
# issue-investigator keeps its stricter check
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("rc", [1, 2], ids=["mismatch", "unobservable"])
def test_the_resolver_check_still_decides_for_issue_investigator(rc, manifests, monkeypatch, capsys):
    """evaluate_promotion passing for every agent does not excuse check():
    it runs with the same transport, and its exit code is kept as it is."""
    seen: list[object] = []

    def strict(transport=None):
        seen.append(transport)
        return rc

    monkeypatch.setattr(gate, "check", strict)
    transport = _gitops(_pinned())

    assert tool.check_all(transport) == rc
    assert seen == [transport]
    rows = _status(capsys.readouterr().out)
    assert rows[tool.RESOLVER_CHECK_LABEL] == {1: "mismatch", 2: "unobservable"}[rc]
    assert all(rows[a] == "match" for a in _AGENTS)


def test_a_deleted_issue_investigator_manifest_fails_through_the_resolver_check(manifests, capsys):
    """Dropping the directory removes it from the listing, so only check()
    is left to notice, as it did before this tool existed."""
    shutil.rmtree(manifests / _V2)

    assert tool.check_all(_gitops(_pinned())) == gate.EXIT_MISMATCH
    captured = capsys.readouterr()
    assert _status(captured.out)[tool.RESOLVER_CHECK_LABEL] == "mismatch"
    assert "does not resolve locally" in captured.err


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_the_job_summary_lists_every_agent(manifests, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _one_byte_edit(manifests, "shepherd")

    tool.check_all(_gitops(_pinned()), label="release commit abc123")

    text = summary.read_text()
    assert "### Agent bindings (release commit abc123) against mctlhq/mctl-gitops@main" in text
    assert "Exit 1:" in text
    for agent in _AGENTS:
        assert f"| `{agent}` | {'mismatch | yes' if agent == 'shepherd' else 'match | no'} |" in text


def test_failures_are_annotated_in_actions(manifests, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _add_manifest(manifests, _UNBOUND)
    monkeypatch.setattr(release, "UNBOUND_AGENTS", frozenset({_UNBOUND}))
    _one_byte_edit(manifests, "mentor")

    tool.check_all(_gitops(_pinned()))

    out = capsys.readouterr().out
    assert "::error::mentor mismatch: " in out
    assert f"::warning::{_UNBOUND} missing: " in out
    assert "::error::shepherd" not in out


def test_the_manifests_option_points_the_resolver_at_a_copy(manifests, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    seen: list[Path] = []
    monkeypatch.setattr(tool, "check_all", lambda label="": seen.append(resolver.DEFINITIONS_DIR) or 0)

    assert tool.main(["--manifests", str(other)]) == 0
    assert seen == [other.resolve()]
