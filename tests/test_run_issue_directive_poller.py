"""Unit tests for orchestrator.run_issue_directive_poller (mctl-agents#417).

`gh` (via `_run`), `list_proposal_refs` and `submit_investigate` are mocked
here — Argo/mctl-api and the real GitHub API have their own coverage
elsewhere. The acceptance test (`test_two_distinct_comments_...`) is the
issue's stated acceptance criterion: two comments produce two runs, the
same comment observed on later ticks produces zero more, and the label
path is never touched.
"""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from orchestrator import run_issue_directive_poller
from orchestrator.directives import Directive
from orchestrator.run_issue_directive_poller import DirectiveScanResult, _handle_directive, scan
from orchestrator.temporal.activities.gitops_state import ProposalStateRef


class FakeGitHub:
    """Stands in for `gh` (via `_run`): a per-issue comment list that
    `gh issue view` reads and `gh issue comment` appends to — so a reply
    posted by one scan() call is visible to the next, the same as real
    GitHub."""

    def __init__(self) -> None:
        self.comments: dict[str, list[dict]] = {}
        self.edit_calls: list[list[str]] = []
        self._next_id = 1

    def add_comment(
        self,
        issue_url: str,
        *,
        body: str = "@MCTL reinvestigate",
        author: str = "octocat",
        association: str = "OWNER",
    ) -> str:
        cid = f"c{self._next_id}"
        self._next_id += 1
        self.comments.setdefault(issue_url, []).append({
            "id": cid,
            "author": {"login": author},
            "createdAt": "2026-09-19T10:00:00Z",
            "body": body,
            "authorAssociation": association,
        })
        return cid

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        if cmd[:3] == ["gh", "issue", "view"]:
            issue_url = cmd[-1]
            payload = {
                "number": 1, "url": issue_url, "state": "OPEN",
                "comments": self.comments.get(issue_url, []),
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        if cmd[:3] == ["gh", "issue", "comment"]:
            issue_url = cmd[3]
            body = cmd[cmd.index("--body") + 1]
            self.comments.setdefault(issue_url, []).append({
                "id": f"ack{self._next_id}",
                "author": {"login": "mctl-agents[bot]"},
                "createdAt": "2026-09-19T10:05:00Z",
                "body": body,
                "authorAssociation": "NONE",
            })
            self._next_id += 1
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["gh", "issue", "edit"]:
            self.edit_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected _run call: {cmd}")


def _ref(service="mctl-web", slug="issue-9-fix", status="proposed") -> ProposalStateRef:
    return ProposalStateRef(service=service, slug=slug, status=status, pr_url=None)


def _refs_stub(refs: list[ProposalStateRef]):
    async def _inner():
        return list(refs)
    return _inner


def _recording_submit(calls: list):
    async def _inner(issue_url, slug, requested_by):
        calls.append((issue_url, slug, requested_by))
        return f"wf-{len(calls)}"
    return _inner


def _run_scan(**kwargs) -> DirectiveScanResult:
    return asyncio.run(scan(**kwargs))


# ---------------------------------------------------------------------------
# T3 — the acceptance test
# ---------------------------------------------------------------------------
def test_two_distinct_comments_produce_two_runs_and_then_go_silent(monkeypatch):
    """The issue's stated acceptance criterion: the same directive posted
    twice as two distinct comment ids produces exactly two submits and two
    replies. Re-running scan() over the resulting comment list (now
    carrying both acks) produces zero further submits. And the label path
    is never touched — no `gh issue edit` call is ever made.
    """
    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)
    gh.add_comment(issue_url)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))
    submits: list = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submits))

    result = _run_scan(max_directives=10)
    assert result.dispatched == 2
    assert result.replied == 2
    assert result.failed == 0
    assert len(submits) == 2

    for _ in range(3):
        result = _run_scan(max_directives=10)
        assert result.dispatched == 0
        assert result.replied == 0

    assert len(submits) == 2, "a later tick re-dispatched an already-acked comment"
    assert gh.edit_calls == [], "the comment path must never touch a label"


def test_disabled_via_env_short_circuits(monkeypatch):
    monkeypatch.setenv("MCTL_DIRECTIVE_SCAN_ENABLED", "false")

    async def _boom():
        raise AssertionError("list_proposal_refs must not be called when disabled")

    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _boom)
    result = _run_scan()
    assert result == DirectiveScanResult()


# ---------------------------------------------------------------------------
# T4 — the decision table, one test per non-dispatch branch
# ---------------------------------------------------------------------------
def _directive(**overrides) -> Directive:
    defaults = dict(comment_id="c1", author="octocat", created_at="2026-09-19T10:00:00Z",
                     verb="reinvestigate", authorized=True)
    defaults.update(overrides)
    return Directive(**defaults)


def _handle(directive, ref, all_refs, monkeypatch, dry_run=False):
    replies: list[str] = []
    monkeypatch.setattr(run_issue_directive_poller, "_post_reply", lambda url, body: replies.append(body))
    outcome = asyncio.run(
        _handle_directive(
            directive,
            issue_url="https://github.com/mctlhq/mctl-web/issues/9",
            ref=ref, all_refs=all_refs, dry_run=dry_run,
        )
    )
    return outcome, replies


def test_unauthorized_author_replies_and_does_not_dispatch(monkeypatch):
    ref = _ref()
    outcome, replies = _handle(_directive(authorized=False), ref, [ref], monkeypatch)
    assert outcome == "unauthorized"
    assert len(replies) == 1
    assert "not accepted" in replies[0]
    assert "mctl-directive-ack: c1" in replies[0]


def test_unrecognised_verb_replies_and_does_not_dispatch(monkeypatch):
    ref = _ref()
    outcome, replies = _handle(_directive(verb=None), ref, [ref], monkeypatch)
    assert outcome == "unrecognised"
    assert len(replies) == 1
    assert "not recognised" in replies[0]
    assert "mctl-directive-ack: c1" in replies[0]


def test_no_proposal_directory_replies_and_points_at_the_label(monkeypatch):
    ref = _ref()
    # all_refs deliberately does not include `ref` itself — simulating the
    # defensive branch for an issue number that resolves to no proposal at all.
    outcome, replies = _handle(_directive(), ref, [], monkeypatch)
    assert outcome == "no-proposal"
    assert "agents:intake" in replies[0]
    assert "mctl-directive-ack: c1" in replies[0]


def test_ambiguous_proposal_dirs_are_named_in_the_reply(monkeypatch):
    ref = _ref(slug="issue-9-fix")
    other = _ref(slug="issue-9-fix-renamed")
    outcome, replies = _handle(_directive(), ref, [ref, other], monkeypatch)
    assert outcome == "ambiguous"
    assert "issue-9-fix" in replies[0] and "issue-9-fix-renamed" in replies[0]
    assert "mctl-directive-ack: c1" in replies[0]


def test_non_overwritable_status_is_named_in_the_reply(monkeypatch):
    ref = _ref(status="accepted")
    outcome, replies = _handle(_directive(), ref, [ref], monkeypatch)
    assert outcome == "not-overwritable"
    assert "accepted" in replies[0]
    assert "mctl-directive-ack: c1" in replies[0]


def test_dry_run_posts_no_reply_and_submits_nothing(monkeypatch):
    submitted = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submitted))
    ref = _ref()
    outcome, replies = _handle(_directive(), ref, [ref], monkeypatch, dry_run=True)
    assert outcome == "dry-run"
    assert replies == []
    assert submitted == []


# ---------------------------------------------------------------------------
# T5 — submit failure
# ---------------------------------------------------------------------------
def test_dispatch_failure_leaves_no_ack_and_retries_next_tick(monkeypatch):
    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))

    attempts = {"n": 0}

    async def failing_submit(*_args, **_kwargs):
        attempts["n"] += 1
        raise RuntimeError("mctl-api unreachable")

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", failing_submit)

    result = _run_scan(max_directives=10)
    assert result.dispatched == 0
    assert result.failed == 1
    assert attempts["n"] == 1
    assert all(
        "mctl-directive-ack" not in c["body"]
        for c in gh.comments[issue_url]
        if c["author"]["login"] == "mctl-agents[bot]"
    )

    result = _run_scan(max_directives=10)
    assert attempts["n"] == 2, "a failed dispatch must be retried, not permanently acked"
    assert result.failed == 1


# ---------------------------------------------------------------------------
# T6 — dry-run / cap / per-issue tolerance at the scan() level
# ---------------------------------------------------------------------------
def test_scan_dry_run_reports_without_posting_or_submitting(monkeypatch):
    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))
    submitted: list = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submitted))

    result = _run_scan(dry_run=True, max_directives=10)
    assert submitted == []
    assert result.dispatched == 0
    assert result.replied == 0
    # Only the seed comment is there — no reply appended.
    assert len(gh.comments[issue_url]) == 1


def test_max_directives_caps_dispatch_and_reports_deferred(monkeypatch):
    gh = FakeGitHub()
    refs = [_ref(slug=f"issue-{n}-x") for n in (1, 2, 3)]
    for ref in refs:
        gh.add_comment(run_issue_directive_poller.issue_url_for(ref.service, ref.slug))

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub(refs))
    submitted: list = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submitted))

    result = _run_scan(max_directives=2)
    assert result.dispatched == 2
    assert result.deferred == 1
    assert len(submitted) == 2


def test_a_gh_failure_on_one_issue_does_not_stop_the_scan(monkeypatch):
    gh = FakeGitHub()
    bad_ref = _ref(slug="issue-1-bad")
    good_ref = _ref(slug="issue-2-good")
    bad_url = run_issue_directive_poller.issue_url_for(bad_ref.service, bad_ref.slug)
    good_url = run_issue_directive_poller.issue_url_for(good_ref.service, good_ref.slug)
    gh.add_comment(good_url)

    def flaky_run(cmd):
        if cmd[:3] == ["gh", "issue", "view"] and cmd[-1] == bad_url:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        return gh.run(cmd)

    monkeypatch.setattr(run_issue_directive_poller, "_run", flaky_run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([bad_ref, good_ref]))
    submitted: list = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submitted))

    result = _run_scan(max_directives=10)
    assert result.failed == 1
    assert result.dispatched == 1


# ---------------------------------------------------------------------------
# issue_url_for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "service,slug,expected",
    [
        ("mctl-web", "issue-9-fix-thing", "https://github.com/mctlhq/mctl-web/issues/9"),
        ("mctl-agents", "issue-417-fix-devloop", "https://github.com/mctlhq/mctl-agents/issues/417"),
    ],
)
def test_issue_url_for(service, slug, expected):
    assert run_issue_directive_poller.issue_url_for(service, slug) == expected


def test_issue_url_for_a_non_issue_slug_is_none():
    assert run_issue_directive_poller.issue_url_for("mctl-web", "adopted-pr-12") is None
