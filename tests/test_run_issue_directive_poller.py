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

import httpx
import pytest

from orchestrator import run_issue_directive_poller
from orchestrator.directives import Directive
from orchestrator.run_issue_directive_poller import (
    DirectiveScanResult,
    DispatchOutcomeAmbiguous,
    _handle_directive,
    scan,
    submit_investigate,
)
from orchestrator.temporal.activities.gitops_state import ProposalStateRef


def _mock_async_client(monkeypatch, handler):
    """Force every httpx.AsyncClient(...) constructed by submit_investigate
    to route through a MockTransport, mirroring
    tests/test_temporal_activities.py's identically-named helper."""
    real_client_cls = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class FakeGitHub:
    """Stands in for `gh` (via `_run`): a per-issue comment list that
    `gh issue view` reads and `gh issue comment` appends to — so a reply
    posted by one scan() call is visible to the next, the same as real
    GitHub."""

    def __init__(self) -> None:
        self.comments: dict[str, list[dict]] = {}
        self.edit_calls: list[list[str]] = []
        self._next_id = 1
        self.login = "mctl-agents[bot]"

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
        if cmd[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=self.login + "\n", stderr="")
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


@pytest.fixture(autouse=True)
def _reset_bot_identity_cache():
    run_issue_directive_poller._bot_identity_checked = False
    yield
    run_issue_directive_poller._bot_identity_checked = False


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


def test_log_dispatch_error_fires_on_attempts_1_and_2_not_only_give_up(monkeypatch, capsys):
    """claude P3 on #421: `_log_dispatch_error` was added so an operator can
    tell a 401 from a 404 from a 503 well before the retry budget runs out
    -- this pins that it actually fires on the FIRST failed attempt, not
    only the terminal give-up one."""
    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))

    async def failing_submit(*_args, **_kwargs):
        raise RuntimeError("mctl-api unreachable")

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", failing_submit)

    _run_scan(max_directives=10)

    out = capsys.readouterr().out
    assert "dispatch attempt 1/" in out, "the first failed attempt must be logged, not only give-up"
    assert "mctl-api unreachable" in out


def test_log_dispatch_error_includes_status_and_body_for_an_http_status_error(capsys):
    """claude P3 on #421: `_log_dispatch_error`'s `httpx.HTTPStatusError`
    branch (status code + response body) was unreachable from any existing
    test, since every dispatch-failure test raises a bare `RuntimeError`."""
    request = httpx.Request("POST", "https://mctl-api.internal/api/v1/operations/x/execute")
    response = httpx.Response(503, request=request, text="upstream unavailable")
    error = httpx.HTTPStatusError("503 Server Error", request=request, response=response)

    run_issue_directive_poller._log_dispatch_error("test context", error)

    out = capsys.readouterr().out
    assert "status=503" in out
    assert "upstream unavailable" in out


def test_dispatch_failure_gives_up_after_max_attempts_and_acks(monkeypatch):
    """The give-up bound (MAX_DISPATCH_ATTEMPTS): once a comment id's
    dispatch has failed that many times in a row, the scan must stop
    retrying it — but must do so visibly (a reply naming the give-up,
    carrying the ack trailer) rather than either spamming forever or
    silently losing the directive (codex review on #417)."""
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

    for _ in range(run_issue_directive_poller.MAX_DISPATCH_ATTEMPTS):
        _run_scan(max_directives=10)

    assert attempts["n"] == run_issue_directive_poller.MAX_DISPATCH_ATTEMPTS
    bot_comments = [c for c in gh.comments[issue_url] if c["author"]["login"] == "mctl-agents[bot]"]
    give_up_comments = [c for c in bot_comments if "mctl-directive-ack" in c["body"]]
    assert give_up_comments, "the give-up reply must carry the ack trailer, not vanish silently"
    assert "giving up" in give_up_comments[-1]["body"].lower()

    # A further tick must not resubmit: the comment is now acked, so the
    # directive is not retried forever, but the give-up reply above is the
    # durable, visible record that it happened (not a silent drop).
    result = _run_scan(max_directives=10)
    assert attempts["n"] == run_issue_directive_poller.MAX_DISPATCH_ATTEMPTS, (
        "the give-up bound must stop further retries once hit"
    )
    assert result.dispatched == 0


def test_transient_marker_post_failure_does_not_escalate_to_give_up(monkeypatch):
    """A one-off blip posting the retry-marker reply (the write that records
    a dispatch-failure attempt) must not by itself escalate straight to the
    permanent give-up path on the very first dispatch attempt — it must be
    absorbed by the bounded in-process retries in `_post_reply_with_retries`
    (codex review on #417)."""
    monkeypatch.setattr(run_issue_directive_poller.time, "sleep", lambda _s: None)

    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    comment_calls = {"n": 0}

    def flaky_run(cmd):
        if cmd[:3] == ["gh", "issue", "comment"]:
            comment_calls["n"] += 1
            if comment_calls["n"] == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="transient blip")
        return gh.run(cmd)

    monkeypatch.setattr(run_issue_directive_poller, "_run", flaky_run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))

    attempts = {"n": 0}

    async def failing_submit(*_args, **_kwargs):
        attempts["n"] += 1
        raise RuntimeError("mctl-api unreachable")

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", failing_submit)

    result = _run_scan(max_directives=10)

    assert attempts["n"] == 1
    assert result.dispatched == 0
    assert comment_calls["n"] == 2, "the first (failed) attempt must be retried in-process"
    bot_comments = [c for c in gh.comments[issue_url] if c["author"]["login"] == "mctl-agents[bot]"]
    assert bot_comments, "the retry-marker reply must have been posted after the retry"
    assert all("mctl-directive-ack" not in c["body"] for c in bot_comments), (
        "a transient marker-post blip must not escalate to the give-up (acked) reply"
    )

    result = _run_scan(max_directives=10)
    assert attempts["n"] == 2, "the directive must still be retried on the next tick"


def test_persistent_marker_post_failure_still_escalates_to_give_up(monkeypatch):
    """If posting the retry-marker reply fails on every one of
    `MARKER_POST_ATTEMPTS` in-process retries (a truly broken write path —
    dead token, outage), the escalation to the give-up path must still
    happen, same as before this change, just after the extra retries
    (codex review on #417)."""
    monkeypatch.setattr(run_issue_directive_poller.time, "sleep", lambda _s: None)

    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    comment_calls = {"n": 0}

    def flaky_run(cmd):
        if cmd[:3] == ["gh", "issue", "comment"]:
            comment_calls["n"] += 1
            if comment_calls["n"] <= run_issue_directive_poller.MARKER_POST_ATTEMPTS:
                raise subprocess.CalledProcessError(1, cmd, stderr="persistent outage")
        return gh.run(cmd)

    monkeypatch.setattr(run_issue_directive_poller, "_run", flaky_run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))

    attempts = {"n": 0}

    async def failing_submit(*_args, **_kwargs):
        attempts["n"] += 1
        raise RuntimeError("mctl-api unreachable")

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", failing_submit)

    result = _run_scan(max_directives=10)

    assert attempts["n"] == 1
    assert result.dispatched == 0
    assert comment_calls["n"] == run_issue_directive_poller.MARKER_POST_ATTEMPTS + 1, (
        "the marker post must be retried MARKER_POST_ATTEMPTS times before the give-up "
        "reply (which succeeds) is posted"
    )
    bot_comments = [c for c in gh.comments[issue_url] if c["author"]["login"] == "mctl-agents[bot]"]
    give_up_comments = [c for c in bot_comments if "mctl-directive-ack" in c["body"]]
    assert give_up_comments, "persistent marker-post failure must still escalate to give-up"
    assert "giving up" in give_up_comments[-1]["body"].lower()


def test_bot_identity_mismatch_raises_and_does_not_dispatch(monkeypatch):
    """If the actually-authenticated `gh` login has drifted from
    `directives.BOT_LOGINS`, the scan must fail loudly (raise) rather than
    silently redispatching every pending directive forever (codex review
    on #417)."""
    gh = FakeGitHub()
    gh.login = "mctl-app"
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))

    submits: list = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submits))

    with pytest.raises(RuntimeError, match="BOT_LOGINS mismatch"):
        _run_scan(max_directives=10)

    assert submits == []


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


def test_max_directives_round_robins_across_issues_not_by_issue_order(monkeypatch):
    """Distinguishes the round-robin-across-issues cap from a naive
    prefix-slice over a flat list ordered by issue (codex review on #417):
    `test_max_directives_caps_dispatch_and_reports_deferred` above gives
    every issue exactly one pending directive, so a flat prefix slice and a
    round-robin produce identical output — it cannot tell them apart. Here
    issue A has three pending directives (a comment burst) and issue B has
    one; a prefix slice over issue order would drain all of A's queue
    before B gets a turn, deferring B's only directive entirely. The actual
    round-robin dispatches to both issues instead.
    """
    gh = FakeGitHub()
    ref_a = _ref(slug="issue-1-a")
    ref_b = _ref(slug="issue-2-b")
    url_a = run_issue_directive_poller.issue_url_for(ref_a.service, ref_a.slug)
    url_b = run_issue_directive_poller.issue_url_for(ref_b.service, ref_b.slug)
    gh.add_comment(url_a)
    gh.add_comment(url_a)
    gh.add_comment(url_a)
    gh.add_comment(url_b)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref_a, ref_b]))
    submitted: list = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submitted))

    result = _run_scan(max_directives=2)
    assert result.dispatched == 2
    assert result.deferred == 2
    dispatched_issue_urls = {issue_url for issue_url, _slug, _requested_by in submitted}
    assert dispatched_issue_urls == {url_a, url_b}


def test_ambiguous_same_issue_proposals_are_scanned_and_replied_once(monkeypatch):
    """Two proposal directories that both resolve to the same GitHub issue
    (the ambiguous case `_handle_directive` detects via `matches`) must be
    treated as ONE pending bucket, not two: `pending_by_issue` used to be
    keyed by `(ref.service, ref.slug)`, so `read_issue_comments` ran once
    per proposal sharing the issue and the same unacked comment was queued
    under both keys — producing a duplicate `_reply_ambiguous` reply and
    burning two `--max-directives` slots for what is a single dispatch
    decision (agy P2 / claude P3 on #421)."""
    gh = FakeGitHub()
    ref_a = _ref(slug="issue-9-fix")
    ref_b = _ref(slug="issue-9-fix-renamed")
    issue_url = run_issue_directive_poller.issue_url_for(ref_a.service, ref_a.slug)
    assert issue_url == run_issue_directive_poller.issue_url_for(ref_b.service, ref_b.slug)
    gh.add_comment(issue_url)

    read_calls: list[str] = []
    real_run = gh.run

    def counting_run(cmd):
        if cmd[:3] == ["gh", "issue", "view"]:
            read_calls.append(cmd[-1])
        return real_run(cmd)

    monkeypatch.setattr(run_issue_directive_poller, "_run", counting_run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref_a, ref_b]))
    submitted: list = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(submitted))

    result = _run_scan(max_directives=10)

    assert read_calls == [issue_url], "read_issue_comments must run once per issue, not once per proposal"
    assert result.replied == 1, "the same directive comment must be actioned once, not once per proposal"
    assert result.dispatched == 0
    assert len(gh.comments[issue_url]) == 2, "seed comment + exactly one ambiguous reply"


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


def test_a_malformed_gh_payload_does_not_stop_the_scan(monkeypatch):
    """A `gh issue view` reply that is not valid JSON (a bad/unexpected `gh`
    payload) must be a per-issue failure, not a tick-ending crash — mirrors
    test_a_gh_failure_on_one_issue_does_not_stop_the_scan but for a
    JSONDecodeError instead of a CalledProcessError (codex review on
    #417)."""
    gh = FakeGitHub()
    bad_ref = _ref(slug="issue-1-bad")
    good_ref = _ref(slug="issue-2-good")
    bad_url = run_issue_directive_poller.issue_url_for(bad_ref.service, bad_ref.slug)
    good_url = run_issue_directive_poller.issue_url_for(good_ref.service, good_ref.slug)
    gh.add_comment(good_url)

    def flaky_run(cmd):
        if cmd[:3] == ["gh", "issue", "view"] and cmd[-1] == bad_url:
            return subprocess.CompletedProcess(cmd, 0, stdout="not valid json", stderr="")
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


# ---------------------------------------------------------------------------
# submit_investigate — direct tests of the real function (claude/agy P2 on
# #421: every existing test above monkeypatches submit_investigate away, so
# nothing exercised the real HTTP/parsing logic or the three-way outcome
# classification it is responsible for).
# ---------------------------------------------------------------------------
def _submit(monkeypatch, handler):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    _mock_async_client(monkeypatch, handler)
    return asyncio.run(submit_investigate("https://github.com/mctlhq/mctl-web/issues/9", "issue-9-fix", "octocat"))


def test_submit_investigate_on_a_definite_rejection_raises_http_status_error_not_ambiguous(monkeypatch):
    """A clean 4xx/5xx before any workflow could have started — mctl-api
    affirmatively rejected the request, so the caller's normal retry is
    safe. Must NOT be classified as DispatchOutcomeAmbiguous."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "operation not found"})

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        _submit(monkeypatch, handler)
    assert not isinstance(exc_info.value, DispatchOutcomeAmbiguous)


def test_submit_investigate_on_a_clean_success_returns_the_workflow_name(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        body = json.loads(request.content)
        assert body == {"issue_url": "https://github.com/mctlhq/mctl-web/issues/9"}
        return httpx.Response(200, json={"workflow": {"workflowName": "wf-abc123"}})

    assert _submit(monkeypatch, handler) == "wf-abc123"


def test_submit_investigate_on_a_2xx_malformed_body_raises_ambiguous_not_dispatch_failed(monkeypatch):
    """mctl-api returned 2xx (accepted the request — a real Argo workflow
    may already be running) but the body this code walks to find the
    workflow name doesn't have the expected shape. This must be classified
    as ambiguous, never as a plain retry-safe failure (claude P2 on #421)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(DispatchOutcomeAmbiguous):
        _submit(monkeypatch, handler)


def test_submit_investigate_on_a_read_timeout_raises_ambiguous(monkeypatch):
    """A timeout during the request cannot distinguish 'mctl-api never
    received it' from 'mctl-api received and is processing it' — must be
    classified as ambiguous, same as the malformed-2xx-body case."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(DispatchOutcomeAmbiguous):
        _submit(monkeypatch, handler)


def test_submit_investigate_on_a_remote_protocol_error_raises_ambiguous(monkeypatch):
    """claude P2 on #421 (carried from the previous round): a dropped
    connection mid-response — the everyday shape of an mctl-api pod restart
    or ingress drop — happens at or after the request reaches the server,
    same as a ReadTimeout, so it must be classified as ambiguous too, not
    fall through to a plain retry-safe failure."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response", request=request)

    with pytest.raises(DispatchOutcomeAmbiguous):
        _submit(monkeypatch, handler)


def test_submit_investigate_on_a_read_error_raises_ambiguous(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    with pytest.raises(DispatchOutcomeAmbiguous):
        _submit(monkeypatch, handler)


def test_submit_investigate_on_a_connect_error_is_not_ambiguous(monkeypatch):
    """The connection never reached mctl-api at all — a plain retry is
    genuinely safe here, unlike every other transport error above. Must
    propagate unchanged, never wrapped as DispatchOutcomeAmbiguous."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.ConnectError) as exc_info:
        _submit(monkeypatch, handler)
    assert not isinstance(exc_info.value, DispatchOutcomeAmbiguous)


def test_submit_investigate_on_a_connect_timeout_is_not_ambiguous(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    with pytest.raises(httpx.ConnectTimeout) as exc_info:
        _submit(monkeypatch, handler)
    assert not isinstance(exc_info.value, DispatchOutcomeAmbiguous)


# ---------------------------------------------------------------------------
# Ambiguous dispatch outcome — the poller-level behavior around
# DispatchOutcomeAmbiguous: ack immediately (no resubmit budget), never
# resubmit automatically on a later tick.
# ---------------------------------------------------------------------------
def test_ambiguous_dispatch_outcome_acks_immediately_and_is_not_resubmitted(monkeypatch):
    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))

    attempts = {"n": 0}

    async def ambiguous_submit(*_args, **_kwargs):
        attempts["n"] += 1
        raise DispatchOutcomeAmbiguous("mctl-api accepted it but the body was unparseable")

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", ambiguous_submit)

    result = _run_scan(max_directives=10)
    assert attempts["n"] == 1
    assert result.dispatched == 0
    assert result.failed == 1

    bot_comments = [c for c in gh.comments[issue_url] if c["author"]["login"] == "mctl-agents[bot]"]
    assert bot_comments, "the ambiguous outcome must still reply"
    assert any("mctl-directive-ack" in c["body"] for c in bot_comments), (
        "an ambiguous outcome must ack immediately, on the very first occurrence — "
        "there is no safe retry budget to spend here"
    )
    assert any("unknown" in c["body"].lower() for c in bot_comments)

    # A further tick must NOT resubmit: mctl-api may already have started
    # the workflow, so a blind retry could duplicate a real, paid Argo run.
    result = _run_scan(max_directives=10)
    assert attempts["n"] == 1, "an acked ambiguous outcome must never be resubmitted"
    assert result.dispatched == 0


def test_ambiguous_dispatch_outcome_is_distinct_from_plain_dispatch_failure(monkeypatch):
    """A plain (non-ambiguous) failure must still go through the normal
    retry-then-give-up path unchanged — this pins that the new
    DispatchOutcomeAmbiguous branch does not swallow ordinary failures."""
    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    monkeypatch.setattr(run_issue_directive_poller, "_run", gh.run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))

    async def failing_submit(*_args, **_kwargs):
        raise RuntimeError("mctl-api unreachable")

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", failing_submit)

    result = _run_scan(max_directives=10)
    bot_comments = [c for c in gh.comments[issue_url] if c["author"]["login"] == "mctl-agents[bot]"]
    assert all("mctl-directive-ack" not in c["body"] for c in bot_comments), (
        "a plain (non-ambiguous) failure must NOT ack immediately — it still gets "
        "a bounded number of retries first"
    )
    assert result.failed == 1


# ---------------------------------------------------------------------------
# Dispatch-ack write resilience (agy P2 on #421): a successful submit whose
# ack-post transiently fails must not resurface as "never dispatched".
# ---------------------------------------------------------------------------
def test_a_transient_ack_post_blip_after_successful_dispatch_is_absorbed(monkeypatch):
    monkeypatch.setattr(run_issue_directive_poller.time, "sleep", lambda _s: None)

    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    comment_calls = {"n": 0}

    def flaky_run(cmd):
        if cmd[:3] == ["gh", "issue", "comment"]:
            comment_calls["n"] += 1
            if comment_calls["n"] == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="transient blip")
        return gh.run(cmd)

    monkeypatch.setattr(run_issue_directive_poller, "_run", flaky_run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit([]))

    result = _run_scan(max_directives=10)

    assert result.dispatched == 1
    assert comment_calls["n"] == 2, "the first (failed) ack write must be retried in-process"
    bot_comments = [c for c in gh.comments[issue_url] if c["author"]["login"] == "mctl-agents[bot]"]
    assert any("mctl-directive-ack" in c["body"] for c in bot_comments)

    # Already acked — a further tick must not resubmit.
    calls = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit(calls))
    _run_scan(max_directives=10)
    assert calls == [], "an already-acked dispatch must never be resubmitted"


def test_a_persistent_ack_post_failure_after_successful_dispatch_is_reported_not_silent(monkeypatch):
    """If posting the dispatch-ack fails on every MARKER_POST_ATTEMPTS
    in-process retry (a truly broken write path), the failure must
    propagate as a reported, counted failure — never silently swallowed —
    even though the underlying Argo workflow already started."""
    monkeypatch.setattr(run_issue_directive_poller.time, "sleep", lambda _s: None)

    gh = FakeGitHub()
    ref = _ref()
    issue_url = run_issue_directive_poller.issue_url_for(ref.service, ref.slug)
    gh.add_comment(issue_url)

    comment_calls = {"n": 0}

    def flaky_run(cmd):
        if cmd[:3] == ["gh", "issue", "comment"]:
            comment_calls["n"] += 1
            raise subprocess.CalledProcessError(1, cmd, stderr="persistent outage")
        return gh.run(cmd)

    monkeypatch.setattr(run_issue_directive_poller, "_run", flaky_run)
    monkeypatch.setattr(run_issue_directive_poller, "list_proposal_refs", _refs_stub([ref]))
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _recording_submit([]))

    result = _run_scan(max_directives=10)

    assert result.dispatched == 0
    assert result.failed == 1
    assert comment_calls["n"] == run_issue_directive_poller.MARKER_POST_ATTEMPTS
