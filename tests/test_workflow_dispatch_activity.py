"""dispatch_and_observe / report_dispatch_failure over a MockTransport (#559)."""
from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest

from orchestrator.temporal.activities import workflow_dispatch as act

_REAL = httpx.AsyncClient
NB = "2026-10-04T09:01:00Z"
INP = act.DispatchInput(repo="mctlhq/portfolio", workflow_file="weekly-refresh.yml", ref="main", not_before=NB)
REP = act.FailureReport(
    repo="mctlhq/portfolio",
    workflow_file="weekly-refresh.yml",
    workflow_id="wf-1",
    error_type="RunNotObserved",
    message="boom",
)
TITLE = "Scheduled dispatch failed: weekly-refresh.yml"


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
    monkeypatch.setattr(act, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(act, "OBSERVE_TIMEOUT", 1.0)
    monkeypatch.setattr(act, "_prior_dispatch", lambda: False)
    monkeypatch.setattr(act.activity, "heartbeat", lambda *a, **k: None)


def _drive(monkeypatch, handler, fn, arg):
    calls: list[httpx.Request] = []

    def _h(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    def _factory(**kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(_h)
        return _REAL(**kw)

    monkeypatch.setattr(act.httpx, "AsyncClient", _factory)
    try:
        return anyio.run(fn, arg), calls
    except BaseException as exc:
        exc.calls = calls  # type: ignore[attr-defined]
        raise


def _runs(*ids: int) -> httpx.Response:
    return httpx.Response(200, json={"workflow_runs": [{"id": i, "html_url": f"u/{i}"} for i in ids]})


def _posts(calls):
    return [c for c in calls if c.method == "POST"]


def _dispatch(monkeypatch, handler):
    return _drive(monkeypatch, handler, act.dispatch_and_observe, INP)


def test_dispatch_422_is_non_retryable(monkeypatch):
    def h(r):
        return _runs() if r.method == "GET" else httpx.Response(422, text="no")

    with pytest.raises(act.DispatchRejected) as ei:
        _dispatch(monkeypatch, h)
    assert ei.value.non_retryable


@pytest.mark.parametrize("status", [500, 429])
def test_dispatch_5xx_429_retryable(monkeypatch, status):
    def h(r):
        return _runs() if r.method == "GET" else httpx.Response(status)

    with pytest.raises(act.DispatchFailed) as ei:
        _dispatch(monkeypatch, h)
    assert not ei.value.non_retryable


def test_dispatch_transport_error_retryable(monkeypatch):
    def h(r):
        if r.method == "GET":
            return _runs()
        raise httpx.ConnectError("down")

    with pytest.raises(act.DispatchFailed):
        _dispatch(monkeypatch, h)


@pytest.mark.parametrize(
    "resp",
    [httpx.Response(500), httpx.Response(200, content=b"not json"), httpx.Response(200, json={"x": 1})],
)
def test_precheck_unreadable_listing_raises_and_never_posts(monkeypatch, resp):
    with pytest.raises(act.RunsListingUnreadable) as ei:
        _dispatch(monkeypatch, lambda r: resp)
    assert _posts(ei.value.calls) == []


def test_observe_unreadable_listing_raises(monkeypatch):
    state = {"gets": 0}

    def h(r):
        if r.method == "POST":
            return httpx.Response(204)
        state["gets"] += 1
        return _runs() if state["gets"] == 1 else httpx.Response(500)

    with pytest.raises(act.RunsListingUnreadable):
        _dispatch(monkeypatch, h)


def test_no_run_observed_is_non_retryable(monkeypatch):
    def h(r):
        return _runs() if r.method == "GET" else httpx.Response(204)

    with pytest.raises(act.RunNotObserved) as ei:
        _dispatch(monkeypatch, h)
    assert ei.value.non_retryable


def test_run_on_second_poll(monkeypatch):
    state = {"gets": 0}

    def h(r):
        if r.method == "POST":
            return httpx.Response(204)
        state["gets"] += 1
        return _runs() if state["gets"] < 3 else _runs(7)

    res, calls = _dispatch(monkeypatch, h)
    assert res == act.DispatchResult(dispatched=True, run_id=7, html_url="u/7")
    assert len(_posts(calls)) == 1


def test_precheck_hit_does_not_post(monkeypatch):
    res, calls = _dispatch(monkeypatch, lambda r: _runs(5))
    assert res.dispatched is False and res.run_id == 5
    assert _posts(calls) == []


def test_empty_token_makes_no_request(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(act.NoGitHubToken) as ei:
        _dispatch(monkeypatch, lambda r: _runs())
    assert ei.value.calls == []
    assert not ei.value.non_retryable  # a token-file rotation gap heals on retry


def test_retry_after_accepted_dispatch_observes_without_posting(monkeypatch):
    # An earlier attempt had its dispatch accepted (heartbeat details), then
    # failed while observing. The retry must not POST a second dispatch.
    monkeypatch.setattr(act, "_prior_dispatch", lambda: True)
    state = {"gets": 0}

    def h(r):
        state["gets"] += 1
        return _runs() if state["gets"] < 2 else _runs(9)

    res, calls = _dispatch(monkeypatch, h)
    assert res == act.DispatchResult(dispatched=True, run_id=9, html_url="u/9")
    assert _posts(calls) == []


def test_accepted_dispatch_is_recorded_before_observing(monkeypatch):
    beats: list[tuple] = []
    monkeypatch.setattr(act.activity, "heartbeat", lambda *a: beats.append(a))
    state = {"gets": 0}

    def h(r):
        if r.method == "POST":
            assert act.DISPATCHED_MARK not in [b[0] for b in beats if b]
            return httpx.Response(204)
        state["gets"] += 1
        if state["gets"] == 1:
            return _runs()
        assert (act.DISPATCHED_MARK,) in beats  # recorded before the first observe call
        return httpx.Response(500)

    with pytest.raises(act.RunsListingUnreadable):
        _dispatch(monkeypatch, h)


def test_request_filters(monkeypatch):
    _, calls = _dispatch(monkeypatch, lambda r: _runs(5))
    q = calls[0].url.params
    assert q["event"] == "workflow_dispatch"
    assert q["branch"] == "main"
    assert q["created"] == ">=2026-10-04T09:00:00Z"  # not_before - SKEW_SLACK
    assert calls[0].headers["authorization"] == "Bearer tok"


# --- report_dispatch_failure -------------------------------------------------


def _report(monkeypatch, handler):
    return _drive(monkeypatch, handler, act.report_dispatch_failure, REP)


def _gh(*, label=200, search=None, search_resp=None, create=201, comment=201):
    def h(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.endswith("/labels/scheduled-dispatch-failed"):
            return httpx.Response(label)
        if path.endswith("/labels"):
            return httpx.Response(201)
        if path.endswith("/comments"):
            return httpx.Response(comment)
        if r.method == "GET":
            return search_resp or httpx.Response(200, json=search or [])
        return httpx.Response(create, json={"number": 9, "html_url": "new"})

    return h


def test_creates_issue_when_none_open(monkeypatch):
    res, calls = _report(monkeypatch, _gh())
    assert res.created and res.issue_number == 9
    (post,) = _posts(calls)
    body = json.loads(post.content)
    assert body["title"] == TITLE and body["labels"] == ["scheduled-dispatch-failed"]
    assert "wf-1" in body["body"] and "RunNotObserved" in body["body"]


def test_comments_when_open_issue_exists(monkeypatch):
    res, calls = _report(monkeypatch, _gh(search=[{"number": 3, "title": TITLE, "html_url": "x"}]))
    assert not res.created and res.issue_number == 3
    (post,) = _posts(calls)
    assert post.url.path.endswith("/issues/3/comments")


def test_other_titles_do_not_match(monkeypatch):
    res, _ = _report(monkeypatch, _gh(search=[{"number": 3, "title": "other"}]))
    assert res.created


@pytest.mark.parametrize(
    "resp",
    [httpx.Response(500), httpx.Response(200, content=b"nope"), httpx.Response(200, json={"a": 1})],
)
def test_unreadable_search_never_reads_as_no_issue(monkeypatch, resp):
    with pytest.raises(act.AlertIssueSearchUnreadable) as ei:
        _report(monkeypatch, _gh(search_resp=resp))
    assert _posts(ei.value.calls) == []
    assert not ei.value.non_retryable


def test_search_transport_error_retryable(monkeypatch):
    def h(r):
        if r.method == "GET" and r.url.path.endswith("/issues"):
            raise httpx.ConnectError("x")
        return httpx.Response(200)

    with pytest.raises(act.AlertReportFailed):
        _report(monkeypatch, h)


def test_empty_token_for_report(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(act.NoGitHubToken) as ei:
        _report(monkeypatch, _gh())
    assert ei.value.calls == []


def test_label_404_creates_label_first(monkeypatch):
    _, calls = _report(monkeypatch, _gh(label=404))
    paths = [c.url.path for c in _posts(calls)]
    assert paths[0].endswith("/labels") and paths[1].endswith("/issues")


def test_label_200_no_label_post(monkeypatch):
    _, calls = _report(monkeypatch, _gh())
    assert not any(c.url.path.endswith("/labels") for c in _posts(calls))


def test_label_500_raises_without_issue_post(monkeypatch):
    with pytest.raises(act.AlertReportFailed) as ei:
        _report(monkeypatch, _gh(label=500))
    assert _posts(ei.value.calls) == []


@pytest.mark.parametrize(
    "kw,issues",
    [
        ({"label": 422}, None),
        ({"search_resp": httpx.Response(403)}, None),
        ({"create": 422}, None),
        ({"comment": 422}, [{"number": 3, "title": TITLE}]),
    ],
)
def test_other_4xx_non_retryable(monkeypatch, kw, issues):
    if issues:
        kw["search"] = issues
    with pytest.raises(act.AlertReportRejected) as ei:
        _report(monkeypatch, _gh(**kw))
    assert ei.value.non_retryable


@pytest.mark.parametrize(
    "kw,issues",
    [
        ({"create": 503}, None),
        ({"create": 429}, None),
        ({"comment": 500}, [{"number": 3, "title": TITLE}]),
        ({"label": 429}, None),
    ],
)
def test_5xx_429_retryable(monkeypatch, kw, issues):
    if issues:
        kw["search"] = issues
    with pytest.raises(act.AlertReportFailed) as ei:
        _report(monkeypatch, _gh(**kw))
    assert not ei.value.non_retryable


@pytest.mark.parametrize("bad", [{"repo": "a/../b"}, {"workflow_file": "x.txt"}, {"repo": "a/b?c"}])
def test_invalid_target_makes_no_request_and_no_token_lookup(monkeypatch, bad):
    def _no_token():
        raise AssertionError("token resolved")

    monkeypatch.setattr(act, "_resolve_token", _no_token)
    for fn, arg in (
        (act.dispatch_and_observe, act.DispatchInput(**{**INP.__dict__, **bad})),
        (act.report_dispatch_failure, act.FailureReport(**{**REP.__dict__, **bad})),
    ):
        with pytest.raises(act.InvalidDispatchTarget) as ei:
            _drive(monkeypatch, lambda r: httpx.Response(500), fn, arg)
        assert ei.value.non_retryable
        assert ei.value.calls == []


def _issue(n: int, title: str) -> dict:
    return {"number": n, "title": title, "html_url": f"u/{n}"}


def _search_handler(pages: list[Any]):
    """pages[i] is a list of issues, or an int status; page i+1 is linked unless last."""

    def h(r: httpx.Request) -> httpx.Response:
        path = r.url.path
        if path.endswith(f"/labels/{act.ALERT_LABEL}"):
            return httpx.Response(200, json={})
        if r.method == "GET" and path.endswith("/issues"):
            page = int(r.url.params.get("page", "1"))
            body = pages[page - 1]
            if isinstance(body, int):
                return httpx.Response(body)
            headers = {}
            if page < len(pages):
                headers["Link"] = f'<https://api.github.com/repos/mctlhq/portfolio/issues?page={page + 1}>; rel="next"'
            return httpx.Response(200, json=body, headers=headers)
        if r.method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={})
        if r.method == "POST" and path.endswith("/issues"):
            return httpx.Response(201, json={"number": 99, "html_url": "new"})
        return httpx.Response(404)

    return h


def test_alert_match_on_page_two_is_commented_not_duplicated(monkeypatch):
    res, calls = _report(monkeypatch, _search_handler([[_issue(1, "other")], [_issue(7, TITLE)]]))
    assert res.issue_number == 7 and res.created is False
    assert [c.url.path for c in _posts(calls)] == ["/repos/mctlhq/portfolio/issues/7/comments"]


def test_no_match_across_complete_listing_creates(monkeypatch):
    res, _calls = _report(monkeypatch, _search_handler([[_issue(1, "a")], [_issue(2, "b")]]))
    assert res.created is True


def test_page_cap_reached_is_unreadable_and_creates_nothing(monkeypatch):
    pages = [[_issue(i, "other")] for i in range(1, act.ALERT_SEARCH_MAX_PAGES + 2)]
    with pytest.raises(act.AlertIssueSearchUnreadable) as ei:
        _report(monkeypatch, _search_handler(pages))
    assert _posts(ei.value.calls) == []


def test_foreign_host_next_link_is_not_followed(monkeypatch):
    def h(r):
        if r.url.path.endswith("/labels/" + act.ALERT_LABEL):
            return httpx.Response(200, json={})
        return httpx.Response(200, json=[], headers={"Link": '<https://evil.example/x>; rel="next"'})

    with pytest.raises(act.AlertIssueSearchUnreadable) as ei:
        _report(monkeypatch, h)
    assert all(c.url.host == "api.github.com" for c in ei.value.calls)


def test_server_error_on_page_two_is_unreadable(monkeypatch):
    with pytest.raises(act.AlertIssueSearchUnreadable) as ei:
        _report(monkeypatch, _search_handler([[_issue(1, "a")], 500]))
    assert _posts(ei.value.calls) == []
