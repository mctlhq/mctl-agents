"""orchestrator.run_token: the investigate pod's scoped MCP credential
(mctlhq/mctl-agents#737)."""

from __future__ import annotations

import httpx
import pytest

from orchestrator import options, run_token

STATIC = "mctl_static_token"
RUN = "art_run_secret"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    run_token._reset_for_tests()
    monkeypatch.setenv("MCTL_TOKEN", STATIC)
    monkeypatch.delenv(run_token.MODE_ENV, raising=False)
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    sa = tmp_path / "token"
    sa.write_text("k8s-sa-jwt\n")
    monkeypatch.setenv(run_token.SA_TOKEN_FILE_ENV, str(sa))
    monkeypatch.setattr(run_token.atexit, "register", lambda *a, **k: None)
    yield
    run_token._reset_for_tests()


class FakeAPI:
    def __init__(self, exchange: httpx.Response | Exception | None = None):
        self.exchange = exchange or httpx.Response(
            201,
            json={
                "token": RUN,
                "token_id": "art_1",
                "scope": "investigate",
                "execution_id": "we_1",
                "expires_at": "2026-10-22T12:00:00Z",
            },
        )
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, bearer: str) -> httpx.Response:
        self.calls.append((url, bearer))
        if url.endswith(run_token.REVOKE_PATH):
            return httpx.Response(204)
        if isinstance(self.exchange, Exception):
            raise self.exchange
        return self.exchange


def _mcp_headers() -> dict[str, str]:
    return options.mctl_mcp_config()["mctl"]["headers"]


def test_off_is_unchanged():
    api = FakeAPI()
    run_token.setup("investigate", post=api)
    assert api.calls == []
    headers = _mcp_headers()
    assert headers["Authorization"] == f"Bearer {STATIC}"
    assert run_token.SHADOW_HEADER not in headers


def test_unknown_mode_is_an_error_not_off(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforced")
    with pytest.raises(run_token.RunTokenError):
        run_token.setup("investigate", post=FakeAPI())


def test_enforce_connects_with_the_run_token(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    api = FakeAPI()
    run_token.setup("investigate", post=api)
    # Only the Kubernetes token is sent; mctl-api decides the rest.
    assert api.calls == [("https://api.mctl.ai" + run_token.EXCHANGE_PATH, "k8s-sa-jwt")]
    headers = _mcp_headers()
    assert headers["Authorization"] == f"Bearer {RUN}"
    assert STATIC not in str(options.mctl_mcp_config())
    assert run_token.SHADOW_HEADER not in headers


def test_enforce_without_a_static_token_still_connects(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.delenv("MCTL_TOKEN")
    run_token.setup("investigate", post=FakeAPI())
    assert _mcp_headers()["Authorization"] == f"Bearer {RUN}"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(403, json={"code": "agent_run_exchange_refused"}),
        httpx.Response(503, json={"code": "agent_run_exchange_unavailable"}),
        httpx.Response(201, json={"token": "not-a-run-token"}),
        httpx.ConnectError("refused"),
    ],
)
def test_enforce_fails_closed(monkeypatch, answer):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    with pytest.raises(run_token.RunTokenError):
        run_token.setup("investigate", post=FakeAPI(answer))


def test_enforce_without_a_projected_token_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.setenv(run_token.SA_TOKEN_FILE_ENV, str(tmp_path / "missing"))
    with pytest.raises(run_token.RunTokenError):
        run_token.setup("investigate", post=FakeAPI())


def test_observe_proves_the_exchange_then_shadows_on_the_static_token(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "observe")
    api = FakeAPI()
    run_token.setup("investigate", post=api)
    # The exchanged token is revoked at once, with itself.
    assert api.calls[-1] == ("https://api.mctl.ai" + run_token.REVOKE_PATH, RUN)
    headers = _mcp_headers()
    assert headers["Authorization"] == f"Bearer {STATIC}"
    assert headers[run_token.SHADOW_HEADER] == "investigate"


def test_observe_never_fails_the_run(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "observe")
    run_token.setup("investigate", post=FakeAPI(httpx.Response(503, json={})))
    headers = _mcp_headers()
    assert headers["Authorization"] == f"Bearer {STATIC}"
    assert headers[run_token.SHADOW_HEADER] == "investigate"


def test_revoke_never_raises():
    def boom(url, bearer):
        raise httpx.ConnectError("down")

    run_token.revoke(run_token.RunToken(RUN, "art_1", "investigate", "we_1", ""), post=boom)


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(201, content=b"not json"),
        httpx.Response(201, json=["art_x"]),
        httpx.Response(201, json={"token": None}),
        httpx.Response(502, json=["bad gateway"]),
        httpx.Response(503, json="unavailable"),
        httpx.Response(403, json=None),
        httpx.Response(500, content=b"<html>oops</html>"),
    ],
)
def test_a_malformed_success_body_is_a_failed_exchange(monkeypatch, answer):
    monkeypatch.setenv(run_token.MODE_ENV, "observe")
    run_token.setup("investigate", post=FakeAPI(answer))  # observe: never raises
    assert _mcp_headers()[run_token.SHADOW_HEADER] == "investigate"
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    with pytest.raises(run_token.RunTokenError):
        run_token.setup("investigate", post=FakeAPI(answer))


def test_a_null_scope_falls_back_to_the_named_one(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "observe")
    answer = httpx.Response(201, json={"token": RUN, "token_id": "art_1", "scope": None})
    run_token.setup("investigate", post=FakeAPI(answer))
    assert _mcp_headers()[run_token.SHADOW_HEADER] == "investigate"


def test_enforce_without_setup_fails_closed(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    with pytest.raises(run_token.RunTokenError, match=r"call run_token.setup\(\) first"):
        options.mctl_mcp_config()
