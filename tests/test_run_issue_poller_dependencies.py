"""Dependency gate of the issue-poller (mctl-agents#290)."""
from __future__ import annotations

import asyncio
import json
import subprocess
from types import SimpleNamespace

from orchestrator import run_issue_poller as rip
from orchestrator.run_issue_investigator import IssueRef
from orchestrator.run_issue_poller import dependency_block_reason, parse_dependencies


def _ref(number: int = 1, repo: str = "mctl-telegram") -> IssueRef:
    return IssueRef("mctlhq", repo, number, f"https://github.com/mctlhq/{repo}/issues/{number}")


def _done(out: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=0, stdout=out, stderr="")


def _gh(monkeypatch, *, labels=(), body="", states=None, view_error=False):
    states = states or {}

    def fake(cmd, **kw):
        if cmd[:3] == ["gh", "issue", "view"]:
            if view_error:
                raise subprocess.CalledProcessError(1, cmd, stderr="boom")
            return _done(json.dumps({"labels": [{"name": n} for n in labels], "body": body}))
        key = cmd[2].removeprefix("repos/").replace("/issues/", "#")
        if key not in states:
            raise subprocess.CalledProcessError(1, cmd, stderr="404")
        return _done(json.dumps({"state": states[key]}))

    monkeypatch.setattr(rip, "_run", fake)


def test_parse_forms_and_comment_stripping() -> None:
    body = (
        "Depends on #481, mctlhq/mctl-api#7 and https://github.com/mctlhq/mctl-gitops/issues/9\n"
        "blocked by #481\n"
        "<!-- Depends on #N -->\n"
    )
    refs, bad = parse_dependencies(body, "mctlhq", "mctl-telegram")
    assert refs == [
        ("mctlhq", "mctl-telegram", 481),
        ("mctlhq", "mctl-api", 7),
        ("mctlhq", "mctl-gitops", 9),
    ]
    assert bad == []


def test_unparseable_declaration_is_reported() -> None:
    refs, bad = parse_dependencies("Depends on the auth rework", "mctlhq", "x")
    assert refs == [] and len(bad) == 1


def test_label_blocks_without_parsing(monkeypatch) -> None:
    _gh(monkeypatch, labels=["agents:intake", "agents:blocked"], body="no deps")
    assert "agents:blocked" in dependency_block_reason(_ref())


def test_open_dependency_blocks_closed_does_not(monkeypatch) -> None:
    _gh(monkeypatch, body="Depends on #2", states={"mctlhq/mctl-telegram#2": "open"})
    assert "open" in dependency_block_reason(_ref())
    _gh(monkeypatch, body="Depends on #2", states={"mctlhq/mctl-telegram#2": "closed"})
    assert dependency_block_reason(_ref()) is None


def test_no_declaration_is_free(monkeypatch) -> None:
    _gh(monkeypatch, body="Just a feature request.")
    assert dependency_block_reason(_ref()) is None


def test_unreadable_dependency_blocks(monkeypatch) -> None:
    _gh(monkeypatch, body="Depends on mctlhq/other#5", states={})
    assert "could not be read" in dependency_block_reason(_ref())


def test_unreadable_issue_blocks(monkeypatch) -> None:
    _gh(monkeypatch, view_error=True)
    assert "could not read" in dependency_block_reason(_ref())


def test_unparseable_declaration_blocks(monkeypatch) -> None:
    _gh(monkeypatch, body="Blocked by upstream work")
    assert "does not name" in dependency_block_reason(_ref())


def test_poll_skips_blocked_keeps_label_and_frees_cap(monkeypatch, capsys) -> None:
    blocked, free = _ref(1), _ref(2)
    monkeypatch.setattr(rip, "search_labeled_issues", lambda label: [blocked, free])
    monkeypatch.setattr(
        rip, "dependency_block_reason",
        lambda r: "depends on x#1 which is open" if r.number == 1 else None,
    )
    started, removed = [], []

    async def start(url, client=None):
        started.append(url)
        return SimpleNamespace(id="wf", result_run_id="r")

    async def connect():
        return SimpleNamespace()

    monkeypatch.setattr(rip, "start_dev_loop_workflow", start)
    monkeypatch.setattr(rip, "connect", connect)
    monkeypatch.setattr(rip, "remove_label", lambda url, label: removed.append(url))
    # cap of 1: the blocked head of the queue must not consume it
    result = asyncio.run(rip.poll(max_issues=1))
    assert started == [free.url] and removed == [free.url]
    assert result.failures == 0
    assert "BLOCKED:" in capsys.readouterr().out


import pytest


@pytest.mark.parametrize("body", [
    "Blocked by:\n- #12\n- #13",
    "## Depends on\n- #12\n- #13\n\n## Other\n#99",
    "Depends on:\n\n#12 and #13\n",
    "**Blocked by**\n* mctlhq/mctl-telegram#12\n* #13",
])
def test_multiline_declaration_forms_yield_refs(body) -> None:
    refs, bad = parse_dependencies(body, "mctlhq", "mctl-telegram")
    assert bad == []
    assert ("mctlhq", "mctl-telegram", 12) in refs and ("mctlhq", "mctl-telegram", 13) in refs
    assert ("mctlhq", "mctl-telegram", 99) not in refs


def test_multiline_closed_deps_release_the_issue(monkeypatch) -> None:
    _gh(monkeypatch, body="Blocked by:\n- #12\n- #13",
        states={"mctlhq/mctl-telegram#12": "closed", "mctlhq/mctl-telegram#13": "closed"})
    assert dependency_block_reason(_ref()) is None


def test_prose_phrases_do_not_block_or_misparse() -> None:
    body = "The pod is blocked by NetworkPolicy.\nNot blocked by #3 anymore.\nbut depends on nothing"
    assert parse_dependencies(body, "mctlhq", "x") == ([], [])


def test_heading_with_nothing_beneath_is_unparseable() -> None:
    refs, bad = parse_dependencies("## Depends on\n\n## Notes\n#5", "mctlhq", "x")
    assert refs == [] and len(bad) == 1
