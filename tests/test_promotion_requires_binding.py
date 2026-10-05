"""Production promotion requires a matching gitops release binding
(mctlhq/mctl-agents#470).

`tools/publish_agent_release.py` used to promote every agent.yaml in a tag to
`production` right after publishing it, so a merged manifest activated
itself. These tests pin the gate in both directions: a binding that pins the
released bytes promotes exactly as before, and every other state — no
binding, a stale one, one that cannot be read — refuses that agent's
promotion without touching the others.

mctl-api is faked at `_request`; mctl-gitops is an `httpx.MockTransport`, so
the suite stays offline.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOL = _REPO_ROOT / "tools" / "publish_agent_release.py"
_spec = importlib.util.spec_from_file_location("publish_agent_release", _TOOL)
assert _spec and _spec.loader
publish_agent_release = importlib.util.module_from_spec(_spec)
sys.modules["publish_agent_release"] = publish_agent_release
_spec.loader.exec_module(publish_agent_release)
gate = publish_agent_release.check_binding_hash

_VERSION = "9.9.9"
_MANIFESTS = _REPO_ROOT / "agents" / "_manifests"
# One v1alpha2 AgentDefinition (full resolver cross-checks) and one v1alpha1
# manifest (hash, name and profile pin only).
_V2 = "issue-investigator"
_V1 = "shepherd"
# Every agent with a binding in the mctl-gitops catalog: all six shipped
# agents since mctl-gitops#1683. Kept here, not in the tool: it only exists to
# force a decision at PR time, when a manifest is added or removed.
_BOUND_AGENTS = frozenset(
    {"implementer", "incident-responder", "issue-investigator", "mentor", "service-agent", "shepherd"}
)
# The profile each fake binding pins. Only issue-investigator's has to agree
# with its agent.yaml; a v1alpha1 manifest names no profile of its own.
_PROFILES = {agent: (f"{agent}-default", "1.0.0") for agent in _BOUND_AGENTS}
_PROFILES[_V2] = ("issue-investigator-default", "1.5.1")
# Not a manifest in this repository: the stand-in for an agent deliberately
# shipped without a binding, since no real agent is in that state today.
_UNBOUND = "unbound-fixture"


def _raw(agent: str) -> bytes:
    return (_MANIFESTS / agent / "agent.yaml").read_bytes()


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _binding(agent: str, *, content_hash: str | None = None, **spec_overrides: Any) -> bytes:
    profile_name, profile_version = _PROFILES[agent]
    spec: dict[str, Any] = {
        "sourceManifest": {
            "repo": "mctlhq/mctl-agents",
            "path": f"agents/_manifests/{agent}/agent.yaml",
            "contentHash": _sha256(_raw(agent)) if content_hash is None else content_hash,
        },
        "bindingSource": "compatibility-fixture",
        "promotable": False,
        "registryLifecycle": {"definition": "published", "profile": "published"},
        "definition": {"name": agent, "version": "1", "profileCompatibility": ">=1.0.0 <2.0.0"},
        "profile": {"name": profile_name, "version": profile_version},
        "bindingRevision": 3,
    }
    spec.update(spec_overrides)
    doc = {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ReleaseBindingIntent",
        "metadata": {"agent": agent, "environment": "shadow"},
        "spec": spec,
    }
    return yaml.safe_dump(doc, sort_keys=False).encode()


def _profile(name: str, version: str) -> bytes:
    return yaml.safe_dump({
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ExecutionProfile",
        "metadata": {"name": name},
        "spec": {"version": version},
    }).encode()


Responder = Callable[[httpx.Request], httpx.Response]


def _gitops(bindings: dict[str, bytes | Responder], profiles: dict[str, bytes | Responder] | None = None):
    """Serve bindings by agent and profiles by name; anything else is a 404,
    which is what GitHub's contents API answers for a path that is absent."""
    if profiles is None:
        profiles = {name: _profile(name, version) for name, version in _PROFILES.values()}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.url.params["ref"] == "main"
        if path.endswith("/profile.yaml"):
            entry = profiles.get(path.rsplit("/", 2)[-2])
        else:
            entry = bindings.get(path.rsplit("/", 1)[-1].removesuffix(".yaml"))
        if entry is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if callable(entry):
            return entry(request)
        return httpx.Response(200, content=entry)

    return httpx.MockTransport(handler)


class _Registry:
    """A fake mctl-api that records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
        self.calls.append((method, path, payload))
        return 201, "{}"

    def published(self) -> list[str]:
        return [p.split("/")[4] for _, p, _ in self.calls if p.endswith("/versions")]

    def promoted(self) -> list[str]:
        return [p.split("/")[4] for _, p, _ in self.calls if p.endswith("/releases")]


@pytest.fixture
def registry(monkeypatch) -> _Registry:
    fake = _Registry()
    monkeypatch.setattr(publish_agent_release, "_request", fake)
    monkeypatch.setattr(
        publish_agent_release, "_read_at_tag", lambda tag, rel: (_REPO_ROOT / rel).read_bytes()
    )
    # The prompt surface is not what is under test here.
    monkeypatch.setattr(publish_agent_release, "prompt_hash", lambda *a: "sha256:" + "a" * 64)
    for name in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return fake


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Retries are exercised, their sleeps are not: a 429's Retry-After
    bypasses RETRY_BACKOFF_S, so sleep itself is stubbed too."""
    monkeypatch.setattr(gate, "RETRY_BACKOFF_S", 0.0)
    monkeypatch.setattr(gate.time, "sleep", lambda seconds: None)


@pytest.fixture
def unbound_agent(registry, monkeypatch) -> str:
    """List a synthetic agent in UNBOUND_AGENTS and give it a manifest in the
    tag. The real set is empty, so the quiet refusal it exists for is
    exercised with a name no shipped agent has."""
    raw = _raw(_V1).replace(b"name: shepherd", f"name: {_UNBOUND}".encode())
    assert raw != _raw(_V1)
    relpath = f"agents/_manifests/{_UNBOUND}/agent.yaml"
    monkeypatch.setattr(
        publish_agent_release,
        "_read_at_tag",
        lambda tag, rel: raw if rel == relpath else (_REPO_ROOT / rel).read_bytes(),
    )
    monkeypatch.setattr(publish_agent_release, "UNBOUND_AGENTS", frozenset({_UNBOUND}))
    return _UNBOUND


def _publish(agent: str, transport: httpx.BaseTransport, *, dry_run: bool = False):
    return publish_agent_release.publish(
        agent, _VERSION, "cafe" * 10, [], dry_run=dry_run, gitops_transport=transport
    )


# ---------------------------------------------------------------------------
# Green on a converged input: promotion as before
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("agent", sorted(_BOUND_AGENTS))
def test_a_matching_binding_is_promoted(agent, registry):
    outcome = _publish(agent, _gitops({agent: _binding(agent)}))

    assert outcome.state == publish_agent_release.PROMOTED
    assert not outcome.fails_release
    assert registry.published() == [agent]
    assert registry.promoted() == [agent]
    (release,) = [payload for _, path, payload in registry.calls if path.endswith("/releases")]
    assert release == {"version": _VERSION, "environment": "production"}


def test_the_gate_hashes_the_released_bytes_as_the_resolver_does():
    from orchestrator import resolver

    assert resolver.definition_content_hash(_raw(_V2)) == resolver.load_definition(_V2).content_hash


# ---------------------------------------------------------------------------
# Red: every refusal leaves production alone
# ---------------------------------------------------------------------------
def _refused(outcome, registry, agent: str, status: str) -> None:
    assert outcome.state == publish_agent_release.REFUSED
    assert outcome.verdict is not None
    assert outcome.verdict.status == status
    assert registry.promoted() == []
    # Publishing an immutable, inactive version still happens, so the
    # release can be promoted by hand once the binding is re-pinned.
    assert registry.published() == [agent]


def test_no_binding_for_a_listed_unbound_agent_is_a_quiet_refusal(registry, unbound_agent):
    outcome = _publish(unbound_agent, _gitops({}))
    _refused(outcome, registry, unbound_agent, gate.VERDICT_MISSING)
    # The log says what the refusal costs, not only where the binding is not.
    assert "is not released to production by this tag" in outcome.detail
    assert "the CWFT's default image if it was never promoted" in outcome.detail
    assert f"no release binding for {unbound_agent}" in outcome.detail
    assert f"releases/shadow/{unbound_agent}.yaml" in outcome.detail
    # Expected state for an agent listed as unbound: refused, not a red run.
    assert not outcome.fails_release


def test_listing_an_agent_as_unbound_quiets_only_its_missing_binding(registry, unbound_agent):
    """The allowance is for an absent binding. A binding that exists and is
    stale names something someone meant to match, listed or not."""
    stale = _binding(_V1, content_hash="sha256:" + "0" * 64, definition={
        "name": unbound_agent, "version": "1", "profileCompatibility": ">=1.0.0 <2.0.0",
    })
    stale = stale.replace(b"agent: shepherd", f"agent: {unbound_agent}".encode())
    outcome = _publish(unbound_agent, _gitops({unbound_agent: stale}))
    _refused(outcome, registry, unbound_agent, gate.VERDICT_MISMATCH)
    assert outcome.fails_release


def test_no_shipped_agent_is_listed_as_unbound():
    """Every shipped agent has a binding since mctl-gitops#1683, so the real
    set is empty and a missing binding is loud for all of them. Listing an
    agent again is a decision: change this test in the same commit."""
    assert publish_agent_release.UNBOUND_AGENTS == frozenset()


def test_every_manifest_is_classified_as_bound():
    """A new manifest with no binding would first fail the next release in a
    step that cannot be re-run. Either add its gitops binding (and the name to
    _BOUND_AGENTS) or list it in UNBOUND_AGENTS; a name in UNBOUND_AGENTS that
    is no manifest directory is a typo or a rename, and voids the allowance."""
    real = {p.parent.name for p in _MANIFESTS.glob("*/agent.yaml")}
    assert real == _BOUND_AGENTS | publish_agent_release.UNBOUND_AGENTS
    assert not (_BOUND_AGENTS & publish_agent_release.UNBOUND_AGENTS)
    assert real == _BOUND_AGENTS


def test_every_refusal_points_at_the_runbook(registry):
    outcome = _publish(_V1, _gitops({}))
    assert gate.RUNBOOK in outcome.detail


def test_an_injected_transport_survives_the_retries(registry):
    """Closing a client closes its transport: an injected transport must
    stay open across the retries and across the binding and profile reads."""
    closed: list[bool] = []
    responder, seen = _flaky([lambda r: httpx.Response(502)] * 2, _binding(_V2))
    inner = _gitops({_V2: responder})

    class Tracking(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            assert not closed, "request sent through a closed transport"
            return inner.handle_request(request)

        def close(self) -> None:
            closed.append(True)

    outcome = _publish(_V2, Tracking())
    assert outcome.state == publish_agent_release.PROMOTED
    assert len(seen) == gate.FETCH_ATTEMPTS


@pytest.mark.parametrize("agent", sorted(_BOUND_AGENTS))
def test_a_missing_binding_fails_the_release_for_every_shipped_agent(agent, registry):
    """claude P2 on #574: a 404 is quiet only for an agent listed as unbound,
    and none is. If a binding disappears, production must not silently stay
    behind on a green run."""
    outcome = _publish(agent, _gitops({}))
    _refused(outcome, registry, agent, gate.VERDICT_MISSING)
    assert f"no release binding for {agent}" in outcome.detail
    assert outcome.fails_release


@pytest.mark.parametrize("agent", [_V2, _V1])
def test_a_stale_binding_refuses_promotion(agent, registry):
    stale = "sha256:" + "0" * 64
    outcome = _publish(agent, _gitops({agent: _binding(agent, content_hash=stale)}))
    _refused(outcome, registry, agent, gate.VERDICT_MISMATCH)
    assert stale in outcome.detail
    assert _sha256(_raw(agent)) in outcome.detail
    assert outcome.fails_release


def _definition(name: str, compatibility: str = ">=1.0.0 <2.0.0") -> dict:
    return {"definition": {"name": name, "version": "1", "profileCompatibility": compatibility}}


_MISMATCHED = {
    "definition.name": (_V2, _definition("other")),
    "v1alpha1 definition.name": (_V1, _definition("other")),
    "compatibility mirror": (_V2, _definition(_V2, ">=1.0.0 <3.0.0")),
    "profile.name": (_V2, {"profile": {"name": "shepherd-default", "version": "1.0.0"}}),
    "profile.version stale vs catalog": (_V1, {"profile": {"name": "shepherd-default", "version": "1.1.0"}}),
    # Mirror and catalog both agree with the binding; only the definition's
    # own range rejects the profile version, so only check_profile_compatibility
    # can refuse it.
    "profile version outside the definition's range": (
        _V2, {"profile": {"name": "issue-investigator-default", "version": "2.5.0"}},
    ),
}
_CATALOG_OVERRIDES = {"profile version outside the definition's range": {"issue-investigator-default": "2.5.0"}}


@pytest.mark.parametrize("case", list(_MISMATCHED))
def test_a_matching_hash_with_a_mismatched_version_or_profile_refuses(case, registry):
    agent, overrides = _MISMATCHED[case]
    catalog = {name: version for name, version in _PROFILES.values()}
    catalog.update(_CATALOG_OVERRIDES.get(case, {}))
    profiles = {name: _profile(name, version) for name, version in catalog.items()}
    outcome = _publish(agent, _gitops({agent: _binding(agent, **overrides)}, profiles))
    _refused(outcome, registry, agent, gate.VERDICT_MISMATCH)
    assert outcome.fails_release
    if case in _CATALOG_OVERRIDES:
        assert "compatibility mismatch" in outcome.detail


def _raise(exc: type[httpx.HTTPError]) -> Responder:
    def responder(request: httpx.Request) -> httpx.Response:
        raise exc("boom", request=request)

    return responder


_UNOBSERVABLE: dict[str, tuple[dict, dict | None]] = {
    "malformed yaml": ({_V2: b"spec: [unclosed\n  - : :"}, None),
    "empty 200": ({_V2: b""}, None),
    "not a binding": ({_V2: b"apiVersion: v1\nkind: ConfigMap\n"}, None),
    "binding for another agent": ({_V2: _binding(_V1)}, None),
    "500": ({_V2: lambda r: httpx.Response(500, text="oops")}, None),
    "403 rate limit": ({_V2: lambda r: httpx.Response(403, text="rate limited")}, None),
    "connect error": ({_V2: _raise(httpx.ConnectError)}, None),
    "timeout": ({_V2: _raise(httpx.ReadTimeout)}, None),
    "profile missing": ({_V2: _binding(_V2)}, {}),
    "profile 500": ({_V2: _binding(_V2)}, {"issue-investigator-default": lambda r: httpx.Response(500)}),
    "profile malformed": ({_V2: _binding(_V2)}, {"issue-investigator-default": b"spec: [unclosed"}),
}


@pytest.mark.parametrize("case", list(_UNOBSERVABLE))
def test_an_unreadable_binding_source_refuses_and_is_not_skipped(case, registry):
    bindings, profiles = _UNOBSERVABLE[case]
    outcome = _publish(_V2, _gitops(bindings, profiles))
    _refused(outcome, registry, _V2, gate.VERDICT_UNOBSERVED)
    assert "unknown is not a match" in outcome.detail
    # Unknown never passes quietly: it fails the release step.
    assert outcome.fails_release


def _flaky(failures: list[Responder], then: bytes) -> tuple[Responder, list[int]]:
    seen: list[int] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) <= len(failures):
            return failures[len(seen) - 1](request)
        return httpx.Response(200, content=then)

    return responder, seen


@pytest.mark.parametrize(
    "failure",
    [
        lambda r: httpx.Response(502),
        lambda r: httpx.Response(429, headers={"Retry-After": "30"}),
        _raise(httpx.ConnectError),
        _raise(httpx.ReadTimeout),
    ],
    ids=["502", "429 secondary rate limit", "connect error", "timeout"],
)
def test_a_transient_failure_is_retried_before_refusing(failure, registry):
    responder, seen = _flaky([failure, failure], _binding(_V2))
    outcome = _publish(_V2, _gitops({_V2: responder}))
    assert outcome.state == publish_agent_release.PROMOTED
    assert len(seen) == gate.FETCH_ATTEMPTS


def test_a_persistent_failure_still_refuses_after_the_retries(registry):
    responder, seen = _flaky([lambda r: httpx.Response(503)] * 5, _binding(_V2))
    outcome = _publish(_V2, _gitops({_V2: responder}))
    _refused(outcome, registry, _V2, gate.VERDICT_UNOBSERVED)
    assert len(seen) == gate.FETCH_ATTEMPTS


@pytest.mark.parametrize("status", [404, 403])
def test_answers_are_not_retried(status, registry):
    responder, seen = _flaky([lambda r: httpx.Response(status)] * 5, _binding(_V2))
    _publish(_V2, _gitops({_V2: responder}))
    assert len(seen) == 1


def test_an_unknown_api_version_takes_the_strict_path_and_refuses(registry, monkeypatch):
    raw = _raw(_V1).replace(b"agents.mctl.ai/v1alpha1", b"agents.mctl.ai/v1alpha3")
    monkeypatch.setattr(publish_agent_release, "_read_at_tag", lambda tag, rel: raw)
    outcome = _publish(_V1, _gitops({_V1: _binding(_V1, content_hash=_sha256(raw))}))
    _refused(outcome, registry, _V1, gate.VERDICT_MISMATCH)
    assert "the released agent.yaml does not resolve" in outcome.detail
    assert "v1alpha3" in outcome.detail


def test_an_unresolvable_released_definition_is_named_as_such(registry, monkeypatch):
    raw = _raw(_V2).replace(b"kind: AgentDefinition", b"kind: Something")
    monkeypatch.setattr(publish_agent_release, "_read_at_tag", lambda tag, rel: raw)
    outcome = _publish(_V2, _gitops({_V2: _binding(_V2, content_hash=_sha256(raw))}))
    _refused(outcome, registry, _V2, gate.VERDICT_MISMATCH)
    assert "the released agent.yaml does not resolve" in outcome.detail
    assert "binding disagrees" not in outcome.detail


def test_the_gate_is_decided_before_any_registry_write(registry, monkeypatch):
    """A gate that blows up must leave no half-done agent: nothing published,
    nothing promoted, and the agent recorded as failed by main()."""

    def explode(*a, **k):
        raise RuntimeError("gate crashed")

    monkeypatch.setattr(gate, "evaluate_promotion", explode)
    with pytest.raises(RuntimeError):
        _publish(_V2, _gitops({_V2: _binding(_V2)}))
    assert registry.calls == []


def test_dry_run_reports_the_verdict_and_writes_nothing(registry, capsys):
    outcome = _publish(_V1, _gitops({}), dry_run=True)
    assert outcome.state == publish_agent_release.REFUSED
    assert registry.calls == []
    assert "would REFUSE promoting shepherd" in capsys.readouterr().out
    assert "is not released to production by this tag" in outcome.detail


@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7.0), ("60", 60.0), ("Wed, 21 Oct 2026 07:28:00 GMT", 2.0), (None, 2.0)],
    ids=["seconds", "at the limit", "http-date", "absent"],
)
def test_a_429_waits_for_retry_after(header, expected, registry, monkeypatch):
    monkeypatch.setattr(gate, "RETRY_BACKOFF_S", 2.0)
    slept: list[float] = []
    monkeypatch.setattr(gate.time, "sleep", slept.append)
    headers = {"Retry-After": header} if header is not None else {}
    responder, _ = _flaky([lambda r: httpx.Response(429, headers=headers)], _binding(_V2))
    outcome = _publish(_V2, _gitops({_V2: responder}))
    assert outcome.state == publish_agent_release.PROMOTED
    assert slept == [expected]


def test_a_retry_after_beyond_the_cap_refuses_at_once(registry, monkeypatch):
    """Retrying before the window ends can extend the block, and the verdict
    would be the same: refuse now, without sleeping or re-sending."""
    slept: list[float] = []
    monkeypatch.setattr(gate.time, "sleep", slept.append)
    over_cap = [lambda r: httpx.Response(429, headers={"Retry-After": "600"})] * 5
    responder, seen = _flaky(over_cap, _binding(_V2))
    outcome = _publish(_V2, _gitops({_V2: responder}))
    _refused(outcome, registry, _V2, gate.VERDICT_UNOBSERVED)
    assert slept == []
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# A mixed release: only the bound agents are promoted
# ---------------------------------------------------------------------------
def _run_main(monkeypatch, transport: httpx.BaseTransport, agents: list[str]) -> int:
    real_publish = publish_agent_release.publish
    monkeypatch.setattr(publish_agent_release, "_git", lambda *a: "cafe1234cafe")
    monkeypatch.setattr(
        publish_agent_release, "_tree_paths", lambda tag: [f"agents/_manifests/{a}/agent.yaml" for a in agents]
    )
    monkeypatch.setattr(
        publish_agent_release,
        "publish",
        lambda *a, **k: real_publish(*a, **k, gitops_transport=transport),
    )
    monkeypatch.setattr(sys, "argv", ["publish_agent_release.py", _VERSION])
    return publish_agent_release.main()


def test_a_mixed_release_promotes_only_the_matching_agents(
    registry, unbound_agent, monkeypatch, tmp_path, capsys
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    transport = _gitops({
        _V2: _binding(_V2),  # matches
        _V1: _binding(_V1, content_hash="sha256:" + "1" * 64),  # stale
        # mentor and the listed-unbound agent: no binding at all
    })
    agents = ["issue-investigator", "mentor", "shepherd", unbound_agent]

    code = _run_main(monkeypatch, transport, agents)

    assert registry.promoted() == ["issue-investigator"]
    assert registry.published() == agents
    # A stale binding and a shipped agent's absent one both block; only the
    # listed agent's absent binding is a warning.
    assert code == 1
    captured = capsys.readouterr()
    assert "failed: mentor, shepherd" in captured.err
    assert "issue-investigator: promoted" in captured.out
    assert "mentor: refused — missing" in captured.out
    assert "shepherd: refused — mismatch" in captured.out
    assert f"{unbound_agent}: refused — missing" in captured.out
    assert "::error::mentor refused" in captured.out
    assert "::error::shepherd refused" in captured.out
    assert f"::warning::{unbound_agent} refused" in captured.out
    table = summary.read_text()
    assert "| `issue-investigator` | promoted |" in table
    assert "| `mentor` | refused |" in table
    assert "| `shepherd` | refused |" in table
    assert f"| `{unbound_agent}` | refused |" in table


def test_only_a_listed_agents_absent_binding_keeps_the_release_green(registry, unbound_agent, monkeypatch):
    code = _run_main(monkeypatch, _gitops({_V2: _binding(_V2)}), ["issue-investigator", unbound_agent])
    assert code == 0
    assert registry.promoted() == ["issue-investigator"]


def test_one_missing_binding_fails_the_release_but_not_the_other_agents(registry, monkeypatch, capsys):
    """With nothing listed as unbound, one shipped agent's absent binding is a
    red step. The five that match are still published and promoted first."""
    agents = sorted(_BOUND_AGENTS)
    bound = [a for a in agents if a != "mentor"]
    code = _run_main(monkeypatch, _gitops({a: _binding(a) for a in bound}), agents)
    assert code == 1
    assert registry.published() == agents
    assert registry.promoted() == bound
    assert "failed: mentor\n" in capsys.readouterr().err


def test_a_release_with_every_binding_matching_promotes_all_six(registry, monkeypatch):
    agents = sorted(_BOUND_AGENTS)
    code = _run_main(monkeypatch, _gitops({a: _binding(a) for a in agents}), agents)
    assert code == 0
    assert registry.promoted() == agents


def test_a_crashing_gate_fails_the_agent_without_stopping_the_others(registry, monkeypatch, capsys):
    real = gate.evaluate_promotion

    def flaky(agent, raw, transport=None):
        if agent == "mentor":
            raise RuntimeError("gate crashed")
        return real(agent, raw, transport)

    monkeypatch.setattr(gate, "evaluate_promotion", flaky)
    code = _run_main(monkeypatch, _gitops({_V2: _binding(_V2)}), ["issue-investigator", "mentor"])
    assert code == 1
    assert registry.promoted() == ["issue-investigator"]
    assert "mentor" not in registry.published()


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def test_the_release_job_runs_the_gated_tool_with_the_locked_environment():
    """The gate imports orchestrator.resolver, which a bare
    `pip install httpx pyyaml` cannot satisfy."""
    workflow = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "release-please.yml").read_text())
    steps = workflow["jobs"]["release-please"]["steps"]
    (refresh,) = [s for s in steps if s.get("name") == "Refresh agent registry"]
    assert "uv run --locked python tools/publish_agent_release.py" in refresh["run"]
    assert "pip install" not in refresh["run"]
    assert refresh["env"]["GITHUB_TOKEN"] == "${{ github.token }}"
