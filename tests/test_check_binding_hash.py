"""Tests for tools/check_binding_hash.py — the release binding gate
(mctlhq/mctl-agents#565).

The gate is only worth having if it is a detector in both directions: green
when agent.yaml matches the binding, red on a one-byte edit, and red — never
green — whenever the binding cannot be observed. Every test here mocks the
fetch with an `httpx.MockTransport`, so the suite stays deterministic and
offline; the live comparison runs only as the `binding hash` CI job and the
release workflow's `binding gate` job.
"""
from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml

from orchestrator import resolver

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOL = _REPO_ROOT / "tools" / "check_binding_hash.py"
_spec = importlib.util.spec_from_file_location("check_binding_hash", _TOOL)
assert _spec and _spec.loader
check_binding_hash = importlib.util.module_from_spec(_spec)
sys.modules["check_binding_hash"] = check_binding_hash
_spec.loader.exec_module(check_binding_hash)

_AGENT_YAML = _REPO_ROOT / "agents" / "_manifests" / "issue-investigator" / "agent.yaml"


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """Retries are exercised, their sleeps are not."""
    monkeypatch.setattr(check_binding_hash, "RETRY_BACKOFF_S", 0.0)
    # A 429's Retry-After bypasses RETRY_BACKOFF_S, so stub sleep itself.
    monkeypatch.setattr(check_binding_hash.time, "sleep", lambda seconds: None)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _binding_doc(content_hash: object = None, **overrides: object) -> dict:
    """A binding shaped like mctl-gitops' releases/shadow/issue-investigator.yaml,
    pinned to the real agent.yaml unless told otherwise."""
    doc: dict = {
        "apiVersion": "agents.mctl.ai/v1alpha2",
        "kind": "ReleaseBindingIntent",
        "metadata": {"agent": "issue-investigator", "environment": "shadow"},
        "spec": {
            "sourceManifest": {
                "repo": "mctlhq/mctl-agents",
                "path": "agents/_manifests/issue-investigator/agent.yaml",
                "contentHash": _sha256(_AGENT_YAML.read_bytes()) if content_hash is None else content_hash,
            },
            "bindingSource": "compatibility-fixture",
            "promotable": False,
            "registryLifecycle": {"definition": "published", "profile": "published"},
            "definition": {"name": "issue-investigator", "version": "1", "profileCompatibility": ">=1.0.0 <2.0.0"},
            "profile": {"name": "issue-investigator-default", "version": "1.5.1"},
            "bindingRevision": 13,
        },
    }
    doc.update(overrides)
    return doc


_PROFILE_DOC = {
    "apiVersion": "agents.mctl.ai/v1alpha2",
    "kind": "ExecutionProfile",
    "metadata": {"name": "issue-investigator-default"},
    "spec": {"version": "1.5.1"},
}


def _serving(
    body: bytes, status: int = 200, *, profile: bytes | None = None, profile_status: int = 200
) -> httpx.MockTransport:
    """Serve `body` for the binding and a profile (by default the one the
    binding pins, at the version it pins) for the execution-profile read."""
    profile_body = _yaml(_PROFILE_DOC) if profile is None else profile

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile.yaml"):
            return httpx.Response(profile_status, content=profile_body)
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handler)


def _raising(exc_factory: Callable[[httpx.Request], Exception]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_factory(request)

    return httpx.MockTransport(handler)


def _yaml(doc: dict) -> bytes:
    return yaml.safe_dump(doc, sort_keys=False).encode()


# ---------------------------------------------------------------------------
# Green on a converged input
# ---------------------------------------------------------------------------
def test_matching_binding_passes(capsys):
    assert check_binding_hash.check(_serving(_yaml(_binding_doc()))) == check_binding_hash.EXIT_MATCH
    out = capsys.readouterr().out
    assert out.startswith("ok: ")
    assert "bindingRevision 13" in out


def test_the_local_hash_is_the_one_the_resolver_compares():
    """The gate must not compute its own idea of the definition hash: it is
    only meaningful if it is the value `execute()` compares at run time."""
    assert resolver.load_definition("issue-investigator").content_hash == _sha256(_AGENT_YAML.read_bytes())


def test_the_fetch_reads_gitops_main_through_the_contents_api():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_yaml(_binding_doc()))

    check_binding_hash.fetch_binding(httpx.MockTransport(handler))
    (request,) = seen
    assert request.url.host == "api.github.com"
    assert request.url.path == (
        "/repos/mctlhq/mctl-gitops/contents/platform-gitops/agent-platform/releases/shadow/issue-investigator.yaml"
    )
    assert request.url.params["ref"] == "main"
    assert request.headers["accept"] == "application/vnd.github.raw"


# ---------------------------------------------------------------------------
# Red on a deliberate mutation
# ---------------------------------------------------------------------------
def test_a_one_byte_edit_of_agent_yaml_fails(tmp_path, monkeypatch, capsys):
    """The binding still pins the committed agent.yaml; the definition the
    resolver reads has one extra byte. That is exactly the release that
    would have shipped green and failed every investigation."""
    pinned = _sha256(_AGENT_YAML.read_bytes())
    edited_dir = tmp_path / "issue-investigator"
    edited_dir.mkdir()
    shutil.copy(_AGENT_YAML, edited_dir / "agent.yaml")
    with (edited_dir / "agent.yaml").open("ab") as fh:
        fh.write(b"\n")
    edited = _sha256((edited_dir / "agent.yaml").read_bytes())
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", tmp_path)

    rc = check_binding_hash.check(_serving(_yaml(_binding_doc(content_hash=pinned))))

    assert rc == check_binding_hash.EXIT_MISMATCH
    err = capsys.readouterr().err
    assert "agent.yaml" in err
    assert pinned in err
    assert edited in err
    assert "Re-pin spec.sourceManifest.contentHash in mctl-gitops" in err
    assert check_binding_hash.RUNBOOK in err


def test_a_binding_pinned_to_other_bytes_fails(capsys):
    other = "sha256:" + "0" * 64
    assert check_binding_hash.check(_serving(_yaml(_binding_doc(content_hash=other)))) == (
        check_binding_hash.EXIT_MISMATCH
    )
    assert other in capsys.readouterr().err


def _spec_with(**changes: dict) -> dict:
    spec = _binding_doc()["spec"]
    return {**spec, **{key: {**spec[key], **value} for key, value in changes.items()}}


# The hash matches in every one of these; what drifted is a field execute()
# also cross-checks against agent.yaml. A re-pin that updated only the hash
# lands exactly here, and every run would fail on it.
_MIRROR_DRIFT = {
    "definition.name": ({"definition": {"name": "issue-investigator-old"}}, "definition.name"),
    "profileCompatibility mirror": (
        {"definition": {"profileCompatibility": ">=1.0.0 <3.0.0"}},
        "mirror drift",
    ),
    "profile.name": ({"profile": {"name": "issue-investigator-other"}}, "profile.name"),
    "profile.version outside the range": ({"profile": {"version": "2.0.0"}}, "compatibility mismatch"),
}
# The catalog profile each case is served with: the out-of-range case moves
# the catalog too, so that only the compatibility check can catch it.
_DRIFT_PROFILE_VERSION = {"profile.version outside the range": "2.0.0"}


@pytest.mark.parametrize("case", list(_MIRROR_DRIFT))
def test_a_matching_hash_with_drifted_mirrored_fields_fails(case, capsys):
    changes, needle = _MIRROR_DRIFT[case]
    version = _DRIFT_PROFILE_VERSION.get(case, "1.5.1")
    profile = _yaml({**_PROFILE_DOC, "spec": {"version": version}})
    rc = check_binding_hash.check(_serving(_yaml(_binding_doc(spec=_spec_with(**changes))), profile=profile))

    assert rc == check_binding_hash.EXIT_MISMATCH
    err = capsys.readouterr().err
    assert "matches the shadow binding's contentHash" in err
    assert needle in err
    assert check_binding_hash.RUNBOOK in err


def test_a_binding_stale_against_the_catalog_profile_fails(capsys):
    """Inside the compatibility range, but not the version the catalog
    profile declares: execute() fails it on "ambiguous version"."""
    stale = _yaml({**_PROFILE_DOC, "spec": {"version": "1.6.0"}})
    rc = check_binding_hash.check(_serving(_yaml(_binding_doc()), profile=stale))

    assert rc == check_binding_hash.EXIT_MISMATCH
    err = capsys.readouterr().err
    assert "ambiguous version" in err
    assert "1.6.0" in err


def test_the_profile_is_read_from_the_same_gitops_ref():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_yaml(_PROFILE_DOC))

    check_binding_hash.fetch_profile("issue-investigator-default", httpx.MockTransport(handler))
    (request,) = seen
    assert request.url.path == (
        "/repos/mctlhq/mctl-gitops/contents/platform-gitops/agent-platform/"
        "execution-profiles/issue-investigator-default/profile.yaml"
    )
    assert request.url.params["ref"] == "main"


_UNOBSERVABLE_PROFILE = {
    "404": {"profile_status": 404},
    "500": {"profile_status": 500},
    "malformed yaml": {"profile": b"spec: [unclosed"},
    "not a profile": {"profile": _yaml({**_PROFILE_DOC, "kind": "Something"})},
    "another profile": {"profile": _yaml({**_PROFILE_DOC, "metadata": {"name": "other"}})},
    "no spec.version": {"profile": _yaml({**_PROFILE_DOC, "spec": {}})},
    "unsupported apiVersion": {"profile": _yaml({**_PROFILE_DOC, "apiVersion": "agents.mctl.ai/v9"})},
    # Pinned by the binding too, so only the version's validity is at fault.
    "unparseable spec.version": {"profile": _yaml({**_PROFILE_DOC, "spec": {"version": "1.5.1-rc1"}})},
}


@pytest.mark.parametrize("case", list(_UNOBSERVABLE_PROFILE))
def test_an_unobservable_profile_fails_closed(case, capsys):
    binding = _binding_doc()
    if case == "unparseable spec.version":
        binding = _binding_doc(spec=_spec_with(profile={"version": "1.5.1-rc1"}))
    rc = check_binding_hash.check(_serving(_yaml(binding), **_UNOBSERVABLE_PROFILE[case]))

    assert rc == check_binding_hash.EXIT_UNOBSERVED
    captured = capsys.readouterr()
    assert "ok:" not in captured.out
    assert "FAILED CLOSED" in captured.err


@pytest.mark.parametrize(
    "binding",
    [
        _binding_doc(content_hash="sha256:" + "0" * 64),
        _binding_doc(spec=_spec_with(definition={"name": "issue-investigator-old"})),
    ],
    ids=["hash mismatch", "mirrored-field mismatch"],
)
def test_a_mismatch_is_reported_before_the_profile_is_read(binding):
    """A mismatch fixable from agent.yaml or the binding must not be masked
    by an outage on the profile read: exit 1 and exit 2 send the operator to
    different procedures."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile.yaml"):
            raise AssertionError("the profile must not be read before a mismatch is reported")
        return httpx.Response(200, content=_yaml(binding))

    assert check_binding_hash.check(httpx.MockTransport(handler)) == check_binding_hash.EXIT_MISMATCH


def _seen_request(monkeypatch, **env: str) -> httpx.Request:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_yaml(_binding_doc()))

    check_binding_hash.fetch_binding(httpx.MockTransport(handler))
    (request,) = seen
    return request


def test_github_token_is_sent_as_a_bearer_header(monkeypatch):
    request = _seen_request(monkeypatch, **{"GITHUB_TOKEN": "t-github", "GH_TOKEN": "t-gh"})
    assert request.headers["authorization"] == "Bearer t-github"


def test_gh_token_is_the_fallback(monkeypatch):
    request = _seen_request(monkeypatch, **{"GH_TOKEN": "t-gh"})
    assert request.headers["authorization"] == "Bearer t-gh"


@pytest.mark.parametrize("env", [{}, {"GITHUB_TOKEN": ""}], ids=["unset", "empty"])
def test_no_token_sends_no_authorization(monkeypatch, env):
    request = _seen_request(monkeypatch, **env)
    assert "authorization" not in request.headers


# ---------------------------------------------------------------------------
# Could not observe is never observed-equal
# ---------------------------------------------------------------------------
_UNOBSERVABLE = {
    "connect error": _raising(lambda req: httpx.ConnectError("connection refused", request=req)),
    "timeout": _raising(lambda req: httpx.ReadTimeout("timed out", request=req)),
    # A non-200 carrying a body that WOULD match: the status alone must
    # fail it, not the parser tripping over an error page.
    "404": _serving(_yaml(_binding_doc()), status=404),
    "403 rate limit": _serving(_yaml(_binding_doc()), status=403),
    "500": _serving(_yaml(_binding_doc()), status=500),
    "empty 200": _serving(b""),
    "malformed yaml": _serving(b"spec: [unclosed\n  - : :"),
    "not utf-8": _serving(b"\xff\xfe\x00binding"),
    "root is a list": _serving(b"- a\n- b\n"),
    "missing sourceManifest": _serving(
        _yaml({**_binding_doc(), "spec": {k: v for k, v in _binding_doc()["spec"].items() if k != "sourceManifest"}})
    ),
    "missing contentHash": _serving(
        _yaml(_binding_doc(spec={
            **_binding_doc()["spec"],
            "sourceManifest": {"repo": "mctlhq/mctl-agents", "path": "agents/_manifests/issue-investigator/agent.yaml"},
        }))
    ),
    "contentHash not sha256": _serving(_yaml(_binding_doc(content_hash="md5:abc"))),
    "contentHash not a string": _serving(_yaml(_binding_doc(content_hash=12345))),
    "binding for another agent": _serving(
        _yaml(_binding_doc(metadata={"agent": "implementer", "environment": "shadow"}))
    ),
    "binding for another environment": _serving(
        _yaml(_binding_doc(metadata={"agent": "issue-investigator", "environment": "production"}))
    ),
}


@pytest.mark.parametrize("transport", list(_UNOBSERVABLE.values()), ids=list(_UNOBSERVABLE))
def test_an_unobservable_binding_fails_closed(transport, capsys):
    rc = check_binding_hash.check(transport)

    assert rc == check_binding_hash.EXIT_UNOBSERVED
    captured = capsys.readouterr()
    assert "ok:" not in captured.out
    assert "FAILED CLOSED" in captured.err
    assert "unknown is not a match" in captured.err


def test_unobservable_and_mismatch_are_distinct_exit_codes():
    """A caller has to be able to tell "re-pin" from "retry the read"."""
    assert len({
        check_binding_hash.EXIT_MATCH,
        check_binding_hash.EXIT_MISMATCH,
        check_binding_hash.EXIT_UNOBSERVED,
    }) == 3
    assert check_binding_hash.EXIT_MATCH == 0


def test_an_unresolvable_local_definition_fails(tmp_path, monkeypatch, capsys):
    """If agent.yaml itself does not load, the resolver would fail at run
    time whatever the binding says — not a pass, and the binding is not
    even fetched."""
    broken = tmp_path / "issue-investigator"
    broken.mkdir()
    (broken / "agent.yaml").write_text("apiVersion: agents.mctl.ai/v1alpha1\nkind: Agent\n")
    monkeypatch.setattr(resolver, "DEFINITIONS_DIR", tmp_path)

    def must_not_fetch(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the binding must not be fetched for an unresolvable definition")

    assert check_binding_hash.check(httpx.MockTransport(must_not_fetch)) == check_binding_hash.EXIT_MISMATCH
    assert "does not resolve locally" in capsys.readouterr().err


def test_the_runbook_the_message_points_at_exists():
    runbook = _REPO_ROOT / check_binding_hash.RUNBOOK
    assert runbook.is_file()
    assert "contentHash" in runbook.read_text()


# ---------------------------------------------------------------------------
# Wiring: the gate has to sit in front of everything a release does
# ---------------------------------------------------------------------------
def _workflow(name: str) -> dict:
    return yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / name).read_text())


def test_release_please_cannot_start_before_the_binding_gate():
    """The tag, the image build + CWFT bump (release-deploy dispatch) and
    the registry promotion all happen inside the release-please job, so
    `needs` on that job is what puts the gate in front of all three."""
    jobs = _workflow("release-please.yml")["jobs"]
    assert jobs["release-please"]["needs"] in ("binding-gate", ["binding-gate"])
    gate_steps = " ".join(str(step.get("run", "")) for step in jobs["binding-gate"]["steps"])
    assert "tools/check_binding_hash.py" in gate_steps
    release_steps = [step.get("name", "") for step in jobs["release-please"]["steps"]]
    assert "Dispatch centralized release-deploy" in release_steps
    assert "Refresh agent registry" in release_steps


def test_the_pr_validation_runs_the_live_comparison():
    job = _workflow("pr-validation.yml")["jobs"]["binding-hash"]
    assert any("tools/check_binding_hash.py" in str(step.get("run", "")) for step in job["steps"])


def test_the_release_gate_takes_only_manifests_from_the_release_commit():
    """The script, the resolver and the lockfile must come from the run's
    head: a pending release commit that predates the gate has no script, and
    checking out its whole tree would fail on a missing file forever."""
    steps = _workflow("release-please.yml")["jobs"]["binding-gate"]["steps"]
    run = " ".join(str(step.get("run", "")) for step in steps)
    assert 'git checkout --quiet "$sha" -- agents/_manifests' in run
    assert "--detach" not in run
    assert "select(. != null)" in run
    assert 'git cat-file -e "${sha}:agents/_manifests/issue-investigator/agent.yaml"' in run
