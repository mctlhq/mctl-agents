"""T-17: confirm_no_pr (mctl-agents#1041, M2b-1c)."""
from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from orchestrator.temporal.activities import pr_state
from orchestrator.temporal.activities.pr_state import NoPrProof, confirm_no_pr
from orchestrator.temporal.activities.proposals import ProposalListingError

STATUS_PATH = "/repos/mctlhq/mctl-gitops/contents/platform-gitops/agents-state/svc/proposals/slug/.status.yaml"
PULLS_PATH = "/repos/mctlhq/svc/pulls"
BRANCH_PATH = "/repos/mctlhq/svc/branches/feat/agents-slug"


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def status_ok(text="status: proposed\n"):
    return httpx.Response(200, json={"encoding": "base64", "content": b64(text)})


def run(monkeypatch, routes, service="svc", slug="slug", token="t"):  # noqa: S107
    """routes: path -> Response | Exception | callable(request)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        route = routes[request.url.path]
        if callable(route):
            route = route(request)
        if isinstance(route, Exception):
            raise route
        return route

    monkeypatch.setattr(pr_state, "_resolve_token", lambda: token)
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(pr_state.httpx, "AsyncClient", factory)
    try:
        proof = asyncio.run(ActivityEnvironment().run(confirm_no_pr, service, slug))
    except ProposalListingError:
        run.seen = seen  # type: ignore[attr-defined]
        raise
    run.seen = seen  # type: ignore[attr-defined]
    # T-17h: confirmed is only ever True on the full happy path.
    assert proof.confirmed == (proof.reason == "no-link-no-pull")
    return proof


def pulls_pages(page1, page2=None, link=None):
    def route(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=page2 if page2 is not None else [])
        headers = {}
        if page2 is not None:
            headers["Link"] = f'<{link or "https://api.github.com" + PULLS_PATH + "?page=2"}>; rel="next"'
        return httpx.Response(200, json=page1, headers=headers)

    return route


def happy(**over):
    routes = {
        STATUS_PATH: status_ok(),
        PULLS_PATH: pulls_pages([], []),
        BRANCH_PATH: httpx.Response(404),
    }
    routes.update(over)
    return routes


def test_happy_path_confirmed(monkeypatch):
    assert run(monkeypatch, happy()) == NoPrProof(True, "no-link-no-pull")
    assert run.seen == [STATUS_PATH, PULLS_PATH, PULLS_PATH, BRANCH_PATH]  # type: ignore[attr-defined]


def test_status_file_404_not_confirmed(monkeypatch):
    p = run(monkeypatch, {STATUS_PATH: httpx.Response(404)})
    assert p == NoPrProof(False, "status-file-missing")


@pytest.mark.parametrize("resp", [
    httpx.Response(500),
    httpx.Response(403),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"encoding": "none", "content": b64("status: x")}),
    httpx.Response(200, json={"content": b64("status: x")}),
    httpx.Response(200, json={"encoding": "base64"}),
    httpx.Response(200, json={"encoding": "base64", "content": 5}),
    httpx.Response(200, json={"encoding": "base64", "content": "!!!not base64!!!"}),
    httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"\xff\xfe\xfa").decode()}),
    httpx.ReadTimeout("slow"),
])
def test_status_file_failures_raise(monkeypatch, resp):
    with pytest.raises(ProposalListingError):
        run(monkeypatch, {STATUS_PATH: resp})


@pytest.mark.parametrize("encoding", ["utf-8", "none", "", None])
def test_non_base64_encoding_raises(monkeypatch, encoding):
    # The content is VALID base64 holding a good status line, so only the
    # explicit encoding guard can reject it; the strict base64 decoder would
    # accept this payload (a failure from the decoder is not proof of the guard).
    body = {"encoding": encoding, "content": b64("status: proposed\n")}
    with pytest.raises(ProposalListingError):
        run(monkeypatch, {STATUS_PATH: httpx.Response(200, json=body)})


@pytest.mark.parametrize("text", ["", "  \n\t\n"])
def test_status_file_empty(monkeypatch, text):
    assert run(monkeypatch, {STATUS_PATH: status_ok(text)}) == NoPrProof(False, "status-file-empty")


def test_no_status_line_not_confirmed(monkeypatch):
    p = run(monkeypatch, {STATUS_PATH: status_ok("service: svc\n")})
    assert p == NoPrProof(False, "status-line-missing")


@pytest.mark.parametrize("line", [
    "pr:", "pr: https://github.com/mctlhq/svc/pull/3", "pr: null", "  pr: x", '"pr": x', "'pr': x", "pr : x",
], ids=["blank", "url", "null", "indented", "quoted_double", "quoted_single", "space"])
def test_pr_line_blocks(monkeypatch, line):
    p = run(monkeypatch, {STATUS_PATH: status_ok(f"status: proposed\n{line}\n")})
    assert p == NoPrProof(False, "pr-line-present")
    assert run.seen == [STATUS_PATH]  # type: ignore[attr-defined]


@pytest.mark.parametrize("page1,page2", [
    ([{"head": {"ref": "feat/agents-slug"}}], None),
    ([{"head": {"ref": "unrelated"}}], None),
    ([], [{"head": {"ref": "x"}}]),
])
def test_pull_listed_not_confirmed(monkeypatch, page1, page2):
    p = run(monkeypatch, happy(**{PULLS_PATH: pulls_pages(page1, page2)}))
    assert p == NoPrProof(False, "pull-request-listed")


def test_pulls_page2_500_raises(monkeypatch):
    def route(request):
        if request.url.params.get("page") == "2":
            return httpx.Response(500)
        return httpx.Response(200, json=[], headers={
            "Link": f'<https://api.github.com{PULLS_PATH}?page=2>; rel="next"'})

    with pytest.raises(ProposalListingError):
        run(monkeypatch, happy(**{PULLS_PATH: route}))


@pytest.mark.parametrize("resp", [
    httpx.Response(500), httpx.Response(404), httpx.Response(200, json={}),
    httpx.Response(200, text="nope"), httpx.ReadTimeout("slow"),
])
def test_pulls_first_page_failures_raise(monkeypatch, resp):
    with pytest.raises(ProposalListingError):
        run(monkeypatch, happy(**{PULLS_PATH: resp}))


def test_pulls_page2_non_list_raises(monkeypatch):
    with pytest.raises(ProposalListingError):
        run(monkeypatch, happy(**{PULLS_PATH: pulls_pages([], {"x": 1})}))


def test_pulls_next_outside_api_raises(monkeypatch):
    with pytest.raises(ProposalListingError):
        run(monkeypatch, happy(**{PULLS_PATH: pulls_pages([], [], link="https://evil.example/p?page=2")}))


def test_pulls_page_bound_raises(monkeypatch):
    monkeypatch.setattr(pr_state, "MAX_PULL_PAGES", 2)

    def route(request):
        return httpx.Response(200, json=[], headers={
            "Link": f'<https://api.github.com{PULLS_PATH}?page=9>; rel="next"'})

    with pytest.raises(ProposalListingError):
        run(monkeypatch, happy(**{PULLS_PATH: route}))


def test_branch_200_not_confirmed(monkeypatch):
    p = run(monkeypatch, happy(**{BRANCH_PATH: httpx.Response(200, json={})}))
    assert p == NoPrProof(False, "branch-exists")


@pytest.mark.parametrize("resp", [httpx.Response(500), httpx.Response(403), httpx.ReadTimeout("slow")])
def test_branch_failures_raise(monkeypatch, resp):
    with pytest.raises(ProposalListingError):
        run(monkeypatch, happy(**{BRANCH_PATH: resp}))


def test_no_token_raises_before_request(monkeypatch):
    with pytest.raises(ProposalListingError):
        run(monkeypatch, {}, token=None)
    assert run.seen == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("service,slug", [("a/b", "slug"), ("svc", "../x"), ("svc", "a/b"), ("", "slug"), ("svc", "")])
def test_invalid_names_raise_before_request(monkeypatch, service, slug):
    with pytest.raises(ProposalListingError):
        run(monkeypatch, {}, service=service, slug=slug)
    assert run.seen == []  # type: ignore[attr-defined]
