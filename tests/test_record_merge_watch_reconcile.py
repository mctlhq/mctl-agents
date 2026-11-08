"""T-7: record_merge_watch_reconcile (mctl-agents#1041, M2b-1c)."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment

from orchestrator.temporal.activities import issue_state
from orchestrator.temporal.activities.issue_state import (
    IssueStateError,
    ReconcileRecordInput,
    ReconcileRecordResult,
    reconcile_comment_body,
    reconcile_marker,
    record_merge_watch_reconcile,
)

API = "https://api.github.com"
LIST_PATH = "/repos/o/r/issues/7/comments"
INP = ReconcileRecordInput(
    repo="o/r", issue_number=7, workflow_id="dev-loop-7", deadline="2026-01-01T00:00:00Z",
    exit_name="grace-polls", last_observed="OPEN https://github.com/o/r/pull/3 head=abc",
    open_request_ids=("req-1", "req-2"),
)
MARKER = reconcile_marker(INP.workflow_id, INP.deadline)


def comment(body=MARKER, login="mctl-agents[bot]", utype="Bot", assoc="CONTRIBUTOR", url="https://x/c/1"):
    return {"body": body, "html_url": url, "user": {"login": login, "type": utype},
            "author_association": assoc}


class Server:
    def __init__(self, pages, *, list_status=200, post_status=201, post_json=None,
                 post_exc=None, list_exc_page=None):
        self.pages = pages  # list of lists, or raw payloads
        self.list_status = list_status
        self.post_status = post_status
        self.post_json = {"html_url": "https://x/c/new"} if post_json is None else post_json
        self.post_exc = post_exc
        self.list_exc_page = list_exc_page
        self.posts: list[dict] = []
        self.requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if request.method == "POST":
            self.posts.append(__import__("json").loads(request.content))
            if self.post_exc:
                raise self.post_exc
            if self.post_status == 201 and not isinstance(self.post_json, str):
                return httpx.Response(201, json=self.post_json)
            if isinstance(self.post_json, str):
                return httpx.Response(self.post_status, text=self.post_json)
            return httpx.Response(self.post_status, json={})
        page = int(request.url.params.get("page", "1"))
        if self.list_exc_page == page:
            raise httpx.ConnectError("boom")
        status = self.list_status
        if status != 200:
            return httpx.Response(status, json={})
        payload = self.pages[page - 1]
        headers = {}
        if page < len(self.pages):
            headers["Link"] = f'<{self.next_base}?per_page=100&page={page + 1}>; rel="next"'
        if isinstance(payload, str):
            return httpx.Response(200, text=payload, headers=headers)
        return httpx.Response(200, json=payload, headers=headers)

    next_base = f"{API}{LIST_PATH}"


def run(monkeypatch, server, inp=INP, token="t"):  # noqa: S107
    monkeypatch.setattr(issue_state, "_resolve_token", lambda: token)
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(server.handler)
        return real(*a, **k)

    monkeypatch.setattr(issue_state.httpx, "AsyncClient", factory)
    return asyncio.run(ActivityEnvironment().run(record_merge_watch_reconcile, inp))


def test_marker_absent_posts_once(monkeypatch):
    s = Server([[comment(body="hello")]])
    res = run(monkeypatch, s)
    assert res == ReconcileRecordResult("recorded", "https://x/c/new")
    assert len(s.posts) == 1
    body = s.posts[0]["body"]
    assert body == reconcile_comment_body(INP)
    for part in (MARKER, "dev-loop-7", "grace-polls", INP.last_observed, "req-1, req-2",
                 "Read the pull request by hand, then re-trigger or close this loop deliberately."):
        assert part in body


def test_empty_fields_render_none():
    body = reconcile_comment_body(ReconcileRecordInput("o/r", 7, "w", "d", "x", ""))
    assert "Last observed PR: none" in body and "Open request ids: none" in body


def test_bot_marker_already_recorded(monkeypatch):
    s = Server([[comment()]])
    assert run(monkeypatch, s) == ReconcileRecordResult("already-recorded", "https://x/c/1")
    assert s.posts == []


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_trusted_association_already_recorded(monkeypatch, assoc):
    s = Server([[comment(login="alice", utype="User", assoc=assoc)]])
    assert run(monkeypatch, s).status == "already-recorded"
    assert s.posts == []


@pytest.mark.parametrize("c", [
    comment(login="eve", utype="User", assoc="NONE"),
    comment(login="eve", utype="User", assoc="CONTRIBUTOR"),
    comment(login="renovate[bot]", utype="Bot", assoc="NONE"),
    comment(login="mctl-agents[bot]", utype="User", assoc="NONE"),
])
def test_third_party_marker_ignored_posts(monkeypatch, c):
    s = Server([[c]])
    assert run(monkeypatch, s).status == "recorded"
    assert len(s.posts) == 1


def test_other_bot_marker_ignored_posts(monkeypatch):
    s = Server([[comment(login="renovate[bot]", assoc="NONE")]])
    assert run(monkeypatch, s).status == "recorded"
    assert len(s.posts) == 1


def test_marker_on_page2_found(monkeypatch):
    s = Server([[comment(body="x")], [comment(url="https://x/c/2")]])
    assert run(monkeypatch, s) == ReconcileRecordResult("already-recorded", "https://x/c/2")
    assert s.posts == []


def test_page2_500_raises_posts_nothing(monkeypatch):
    s = Server([[comment(body="x")], []])
    orig = s.handler

    def handler(request):
        if request.method == "GET" and request.url.params.get("page") == "2":
            return httpx.Response(500)
        return orig(request)

    s.handler = handler
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)
    assert s.posts == []


@pytest.mark.parametrize("page2", [
    {"not": "a list"}, "not json", [{"body": "x"}], [{"user": {}, "body": None}],
    ["str"], [{"body": 5, "user": {}}], [{"body": "x", "user": "u"}],
])
def test_page2_malformed_raises_posts_nothing(monkeypatch, page2):
    s = Server([[comment(body="x")], page2])
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)
    assert s.posts == []


def test_page2_transport_error_raises(monkeypatch):
    s = Server([[comment(body="x")], []], list_exc_page=2)
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)
    assert s.posts == []


def test_next_link_outside_api_raises(monkeypatch):
    s = Server([[comment(body="x")], []])
    s.next_base = "https://evil.example/repos/o/r/issues/7/comments"
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)
    assert s.posts == []


def test_page_bound_raises(monkeypatch):
    monkeypatch.setattr(issue_state, "MAX_COMMENT_PAGES", 2)
    s = Server([[comment(body="x")]] * 3)
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)
    assert s.posts == []


def test_no_token_raises_before_request(monkeypatch):
    s = Server([[]])
    with pytest.raises(IssueStateError):
        run(monkeypatch, s, token=None)
    assert s.requests == 0


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_listing_status_raises(monkeypatch, status):
    s = Server([[]], list_status=status)
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)
    assert s.posts == []


@pytest.mark.parametrize("status", [401, 403, 404, 410, 422, 429, 500])
def test_post_status_raises(monkeypatch, status):
    s = Server([[]], post_status=status)
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)


def test_listing_410_is_gone(monkeypatch):
    s = Server([[]], list_status=410)
    assert run(monkeypatch, s) == ReconcileRecordResult("issue-gone")
    assert s.posts == []


@pytest.mark.parametrize("post_json", [{}, {"html_url": ""}, {"html_url": 5}, "not json", []])
def test_201_without_html_url_raises(monkeypatch, post_json):
    s = Server([[]], post_json=post_json)
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)


def test_post_transport_error_raises(monkeypatch):
    s = Server([[]], post_exc=httpx.ReadTimeout("slow"))
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)


def test_retry_after_lost_answer_finds_own_marker(monkeypatch):
    s = Server([[]], post_exc=httpx.ReadTimeout("slow"))
    posted: list[dict] = []
    orig = s.handler

    def handler(request):
        if request.method == "POST":
            posted.append({"body": __import__("json").loads(request.content)["body"]})
            raise httpx.ReadTimeout("slow")
        s.pages = [[comment(body=p["body"]) for p in posted]]
        return orig(request)

    s.handler = handler
    with pytest.raises(IssueStateError):
        run(monkeypatch, s)
    assert run(monkeypatch, s).status == "already-recorded"
    assert len(posted) == 1


@pytest.mark.parametrize("field,value", [
    ("workflow_id", ""), ("workflow_id", "a b"), ("workflow_id", "a--b"), ("workflow_id", "a\tb"),
    ("deadline", ""), ("deadline", "a b"), ("deadline", "a--b"),
])
def test_invalid_marker_input_raises_before_request(monkeypatch, field, value):
    import dataclasses

    s = Server([[]])
    with pytest.raises(IssueStateError):
        run(monkeypatch, s, inp=dataclasses.replace(INP, **{field: value}))
    assert s.requests == 0


def test_activities_registered_on_control_queue():
    from orchestrator.temporal.constants import TASK_QUEUE
    from orchestrator.temporal.worker import worker_plans

    stub = MagicMock()
    for name in ("list_active_dev_loop_ids", "find_stranded_accepted", "count_swept_prestart_failures"):
        @activity.defn(name=name)
        async def _fn() -> None:
            return None

        setattr(stub, name, _fn)
    control = next(p for p in worker_plans("all", stub) if p.task_queue == TASK_QUEUE)
    assert "record_merge_watch_reconcile" in control.activity_names
    assert "confirm_no_pr" in control.activity_names
