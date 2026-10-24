"""Spike-issue classification (mctl-agents#763).

Manifest fixtures in tests/fixtures/spike_manifests/ are verbatim copies of
mctlhq/.github roadmap/spikes/*.yaml at commit
51694fdac70615ecccecd7f51ef46fe95d179759.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
import yaml

from orchestrator import run_implementer, spike_issue
from orchestrator.run_implementer import ProposalRef, _issue_reference_line

FIX = Path(__file__).parent / "fixtures" / "spike_manifests"
NAMES = ["security-finding-lifecycle.yaml", "duet-sessions.yaml"]
CLOSING = re.compile(r"(?i)\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+\S*#\d+")


def _file_payload(text: str) -> dict:
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def _gh(title="plain work", manifests=None, *, issue_exc=None, list_result=None, calls=None):
    files = {n: (FIX / n).read_text() for n in NAMES} if manifests is None else manifests

    def gh(args):
        path = args[0]
        if calls is not None:
            calls.append(path)
        if "/issues/" in path:
            if issue_exc:
                raise issue_exc
            return {"title": title}
        if path.endswith("roadmap/spikes"):
            if list_result is not None:
                if isinstance(list_result, Exception):
                    raise list_result
                return list_result
            return [{"name": n, "type": "file"} for n in files]
        name = path.rsplit("/", 1)[1]
        v = files[name]
        if isinstance(v, Exception):
            raise v
        return _file_payload(v)

    return gh


def test_fixtures_are_real_and_unmodified():
    doc = yaml.safe_load((FIX / NAMES[0]).read_text())
    assert doc["spec"]["github"]["issue"] == {"repository": "mctlhq/.github", "number": 209}


@pytest.mark.parametrize("title", ["spike(security): x", "spike: y", "  SPIKE(x): z"])
def test_spike_title_skips_manifest_lookup(title):
    calls: list[str] = []
    r = spike_issue.classify_issue("mctlhq/.github", 209, gh_api_json=_gh(title, calls=calls))
    assert r.outcome == "spike-title"
    assert not any("roadmap" in c for c in calls)


@pytest.mark.parametrize("title", ["spikes in latency", "fix: spike handling"])
def test_non_spike_titles_not_title_matched(title):
    r = spike_issue.classify_issue("mctlhq/mctl-agents", 763, gh_api_json=_gh(title))
    assert r.outcome == "not-spike"


@pytest.mark.parametrize("repo", ["mctlhq/.github", "MCTLHQ/.GITHUB"])
def test_manifest_binding_matches_case_insensitively(repo):
    r = spike_issue.classify_issue(repo, 209, gh_api_json=_gh())
    assert r.outcome == "spike-manifest"


def test_t11_real_manifests_do_not_bind_other_issue():
    r = spike_issue.classify_issue("mctlhq/mctl-agents", 763, gh_api_json=_gh())
    assert r.outcome == "not-spike"
    assert not r.is_spike_or_unknown


def test_issue_read_failure():
    r = spike_issue.classify_issue("a/b", 1, gh_api_json=_gh(issue_exc=RuntimeError("boom")))
    assert r.outcome == "observation-error" and "step=issue-read" in (r.detail or "")


@pytest.mark.parametrize("lst", [RuntimeError("404"), {"message": "Not Found"}])
def test_listing_failure(lst):
    r = spike_issue.classify_issue("a/b", 1, gh_api_json=_gh(list_result=lst))
    assert r.outcome == "observation-error" and "step=spike-list" in (r.detail or "")


@pytest.mark.parametrize(
    "bad",
    [
        "mctlhq/.github#209",
        209,
        {"repo": "mctlhq/.github", "number": 209},
        {"repository": "mctlhq/.github", "number": True},
        {"repository": "mctlhq/.github", "number": 0},
        None,
    ],
)
def test_non_canonical_binding_is_observation_error(bad):
    spec = {"github": {}} if bad is None else {"github": {"issue": bad}}
    text = yaml.safe_dump({"spec": spec})
    r = spike_issue.classify_issue("mctlhq/.github", 209, gh_api_json=_gh(manifests={"x.yaml": text}))
    assert r.outcome == "observation-error"
    assert "step=spike-manifest:x.yaml" in (r.detail or "")


def test_manifest_fetch_or_yaml_failure():
    for m in ({"x.yaml": RuntimeError("500")}, {"x.yaml": "a: [unterminated"}):
        r = spike_issue.classify_issue("a/b", 1, gh_api_json=_gh(manifests=m))
        assert r.outcome == "observation-error"


# --- run_implementer wiring -------------------------------------------------
def _ref(tmp_path, source):
    d = tmp_path / "p"
    d.mkdir()
    payload = {"status": "accepted"}
    if source is not None:
        payload["source"] = source
    (d / ".status.yaml").write_text(yaml.safe_dump(payload))
    return ProposalRef(service="mctl-agents", slug="p", proposal_dir=d, status="accepted")


SRC = {"type": "github_issue", "repo": "mctlhq/.github", "issue": 209}


def test_spike_title_gives_refs_and_no_closing_keyword(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_implementer, "_gh_api_json", _gh("spike(security): x"))
    _, body = run_implementer._pr_title_and_body(_ref(tmp_path, SRC))
    assert body.endswith("Refs mctlhq/.github#209")
    assert not CLOSING.search(body)
    assert "spike-classification: issue=mctlhq/.github#209 outcome=spike-title keyword=Refs" in capsys.readouterr().out


def test_not_spike_gives_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(run_implementer, "_gh_api_json", _gh())
    src = {"type": "github_issue", "repo": "mctlhq/mctl-agents", "issue": 763}
    assert _issue_reference_line(_ref(tmp_path, src)) == "\n\nCloses mctlhq/mctl-agents#763"


def test_observation_error_gives_refs_and_logs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_implementer, "_gh_api_json", _gh(issue_exc=RuntimeError("x")))
    assert _issue_reference_line(_ref(tmp_path, SRC)) == "\n\nRefs mctlhq/.github#209"
    out = capsys.readouterr().out
    assert "outcome=observation-error" in out and "step=issue-read" in out


def test_no_source_emits_nothing(tmp_path):
    assert _issue_reference_line(_ref(tmp_path, None)) == ""
