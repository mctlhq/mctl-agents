"""The implementer's scoped mctl-api credential (mctlhq/mctl-agents#717, slice S5).

`run_implementer.main()` sets the run token up under scope `implement`, and
its action-approval client takes its bearer from `run_token.mctl_api_bearer`.
With MCTL_RUN_TOKEN_MODE unset (off) nothing changes.
"""

from __future__ import annotations

import io
import json
import urllib.error

import httpx
import pytest

from orchestrator import action_approvals as aa
from orchestrator import run_implementer, run_token

STATIC = "mctl_static_token"
RUN = "art_run_secret"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    run_token._reset_for_tests()
    monkeypatch.setenv("MCTL_TOKEN", STATIC)
    monkeypatch.delenv(run_token.MODE_ENV, raising=False)
    monkeypatch.delenv(run_implementer.EXECUTION_ID_ENV, raising=False)
    sa = tmp_path / "token"
    sa.write_text("k8s-sa-jwt\n")
    monkeypatch.setenv(run_token.SA_TOKEN_FILE_ENV, str(sa))
    monkeypatch.setattr(run_token.atexit, "register", lambda *a, **k: None)
    yield
    run_token._reset_for_tests()


class _Api:
    """Answers the exchange, the scope probe and the revoke."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url, bearer):
        self.calls.append((url, bearer))
        if url.endswith(run_token.REVOKE_PATH):
            return httpx.Response(204)
        if url.endswith(run_token.EXCHANGE_PATH):
            return httpx.Response(201, json={
                "token": RUN, "token_id": "art_1", "scope": "implement",
                "execution_id": "we_1", "expires_at": "2099-01-01T00:00:00Z",
            })
        if run_token.PROBE_PATH_PREFIX in url:
            return httpx.Response(403, json={"code": run_token.SCOPE_DENIED_CODE})
        raise AssertionError(url)


class _Opener:
    def __init__(self):
        self.auth: list[str | None] = []

    def open(self, req, timeout=None):
        self.auth.append(req.get_header("Authorization"))
        raise urllib.error.HTTPError(
            req.full_url, 404, "nf", {}, io.BytesIO(json.dumps({"code": "approval_not_found"}).encode())
        )


def _client(**kw):
    client = aa.ActionApprovalClient(base_url="https://api.example.test", **kw)
    opener = _Opener()
    client._opener = opener  # type: ignore[assignment]
    return client, opener


# ---- ActionApprovalClient bearer per mode ---------------------------------


def test_mode_off_sends_the_static_token():
    client, opener = _client()
    client.get("aa_1")
    assert opener.auth == [f"Bearer {STATIC}"]


def test_observe_sends_the_static_token(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "observe")
    api = _Api()
    run_token.setup("implement", post=api, get=api)
    client, opener = _client()
    client.get("aa_1")
    assert opener.auth == [f"Bearer {STATIC}"]


def test_enforce_sends_the_run_token(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    client, opener = _client()  # built before setup(): the bearer is per request
    api = _Api()
    run_token.setup("implement", post=api, get=api)
    client.get("aa_1")
    assert opener.auth == [f"Bearer {RUN}"]


def test_enforce_without_a_run_token_sends_nothing(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    client, opener = _client()
    with pytest.raises(aa.ApprovalStoreUnavailable):
        client._request("GET", "/api/v1/action-approvals/aa_1")
    assert client.get("aa_1").status == aa.UNKNOWN
    assert opener.auth == []  # the static token is still set and must not go out


def test_a_mistyped_mode_sends_nothing(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforced")
    client, opener = _client()
    assert client.get("aa_1").status == aa.UNKNOWN
    assert opener.auth == []


def test_unscoped_client_keeps_the_static_token_in_enforce(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    api = _Api()
    run_token.setup("implement", post=api, get=api)
    client, opener = _client(scoped=False)
    client.get("aa_1")
    assert opener.auth == [f"Bearer {STATIC}"]


def test_investigator_commerce_approvals_stay_unscoped(monkeypatch):
    from orchestrator import run_issue_investigator
    from orchestrator.commerce_gateway import CommerceRuntime

    seen = {}
    monkeypatch.setattr(
        CommerceRuntime, "from_env", classmethod(lambda cls, approvals: seen.setdefault("a", approvals))
    )
    run_issue_investigator._commerce_runtime()
    assert seen["a"]._scoped is False


# ---- implementer startup ---------------------------------------------------


def _run_main(monkeypatch, tmp_path, *extra):
    monkeypatch.setattr(run_implementer, "refresh_github_token", lambda: None)
    monkeypatch.setattr("sys.argv", ["run_implementer", "--state-dir", str(tmp_path), *extra])
    run_implementer.main()


def test_main_sets_up_the_run_token_with_scope_implement(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv(run_implementer.EXECUTION_ID_ENV, "we_1")
    monkeypatch.setattr(run_token, "setup", lambda **kw: seen.append(kw))
    _run_main(monkeypatch, tmp_path)
    assert seen == [{"shadow_scope": "implement"}]


def test_main_shadows_the_grant_scope_without_an_execution(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(run_token, "setup", lambda **kw: seen.append(kw))
    _run_main(monkeypatch, tmp_path)
    assert seen == [{"shadow_scope": "implement-grant"}]


def test_main_hands_its_filters_to_the_run_token_setup(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(run_implementer, "_setup_run_token", lambda *a: seen.append(a))
    _run_main(monkeypatch, tmp_path, "--service", "mctl-api", "--slug", "issue-1-x")
    assert seen == [("mctl-api", "issue-1-x")]


def test_main_sets_up_before_find_accepted_proposals(monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(run_token, "setup", lambda **kw: order.append("setup"))
    monkeypatch.setattr(run_implementer, "find_accepted_proposals", lambda *a, **k: order.append("find") or [])
    _run_main(monkeypatch, tmp_path)
    assert order == ["setup", "find"]


def test_dry_run_does_not_set_up(monkeypatch, tmp_path):
    monkeypatch.setattr(run_token, "setup", lambda **kw: pytest.fail("dry-run calls no mctl-api"))
    _run_main(monkeypatch, tmp_path, "--dry-run", "--max-proposals", "0")


def test_mode_off_makes_no_network_call_and_changes_no_state(monkeypatch, tmp_path):
    def no_network(*a, **k):
        raise AssertionError("mode off must not call mctl-api")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    _run_main(monkeypatch, tmp_path)  # no execution_id either: off needs none
    assert run_token.mcp_auth(STATIC) == (STATIC, {})
    assert run_token.mctl_api_bearer() == STATIC


def test_enforce_without_an_execution_id_fails_by_name(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no exchange without an execution"))
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, tmp_path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "needs an execution_id" in err and run_implementer.EXECUTION_ID_ENV in err
    assert "--service and --slug" in err
    assert run_token.mctl_api_bearer() == ""  # and no fallback to MCTL_TOKEN


def test_enforce_with_a_blank_execution_id_fails_too(monkeypatch, tmp_path):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.setenv(run_implementer.EXECUTION_ID_ENV, "  ")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no exchange without an execution"))
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, tmp_path)
    assert exc.value.code == 2


def test_enforce_exchange_failure_stops_the_run(monkeypatch, tmp_path):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.setenv(run_implementer.EXECUTION_ID_ENV, "we_1")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(403, json={"code": "agent_run_token_refused"}))
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, tmp_path)
    assert exc.value.code == 2


def test_enforce_with_an_execution_id_exchanges_and_goes_on(monkeypatch, tmp_path):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.setenv(run_implementer.EXECUTION_ID_ENV, "we_1")
    api = _Api()
    monkeypatch.setattr(httpx, "post", lambda url, headers, timeout: api(url, headers["Authorization"][7:]))
    monkeypatch.setattr(httpx, "get", lambda url, headers, timeout: api(url, headers["Authorization"][7:]))
    _run_main(monkeypatch, tmp_path)
    assert api.calls[0][0].endswith(run_token.EXCHANGE_PATH)
    assert run_token.mctl_api_bearer() == RUN


def test_observe_exchange_failure_is_not_fatal(monkeypatch, tmp_path):
    monkeypatch.setenv(run_token.MODE_ENV, "observe")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(503, json={}))
    _run_main(monkeypatch, tmp_path)  # no execution_id: observe only logs
    assert run_token.mcp_auth(STATIC) == (STATIC, {run_token.SHADOW_HEADER: "implement-grant"})


def test_observe_exchange_failure_shadows_implement_on_an_execution(monkeypatch, tmp_path):
    monkeypatch.setenv(run_token.MODE_ENV, "observe")
    monkeypatch.setenv(run_implementer.EXECUTION_ID_ENV, "we_1")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(503, json={}))
    _run_main(monkeypatch, tmp_path)
    assert run_token.mcp_auth(STATIC) == (STATIC, {run_token.SHADOW_HEADER: "implement"})


@pytest.mark.parametrize(("service", "slug"), [("mctl-api", ""), ("", "issue-1-x"), ("", "")])
def test_enforce_without_an_execution_needs_both_filters(monkeypatch, service, slug, capsys):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no exchange without one proposal"))
    with pytest.raises(SystemExit) as exc:
        run_implementer._setup_run_token(service, slug)
    assert exc.value.code == 2
    assert run_token.mctl_api_bearer() == ""
    assert "--service and --slug" in capsys.readouterr().err


class _PinnedApi(_Api):
    def __call__(self, url, bearer):
        if url.endswith(run_token.EXCHANGE_PATH):
            self.calls.append((url, bearer))
            return httpx.Response(201, json={
                "token": RUN, "token_id": "art_1", "scope": "implement-grant", "execution_id": "",
                "expires_at": "2099-01-01T00:00:00Z", "grant_id": "aar_pinned",
            })
        return super().__call__(url, bearer)


def _pinned_run(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    api = _PinnedApi()
    monkeypatch.setattr(httpx, "post", lambda url, headers, timeout: api(url, headers["Authorization"][7:]))
    monkeypatch.setattr(httpx, "get", lambda url, headers, timeout: api(url, headers["Authorization"][7:]))
    run_implementer._setup_run_token("mctl-api", "issue-1-x")
    return api


def test_enforce_with_one_proposal_exchanges_for_a_pinned_token(monkeypatch):
    api = _pinned_run(monkeypatch)
    assert api.calls[0][0].endswith(run_token.EXCHANGE_PATH)
    assert run_token.mctl_api_bearer() == RUN
    assert run_token.pinned_grant_id() == "aar_pinned"


def test_a_pinned_run_does_not_read_another_grant_as_absent(monkeypatch):
    _pinned_run(monkeypatch)
    asked: list[str] = []

    class _Client:
        def get(self, grant_id):
            asked.append(grant_id)
            return aa.ApprovalAnswer(aa.NOT_FOUND)

    monkeypatch.setattr(run_implementer.action_approvals, "ActionApprovalClient", _Client)

    # The record names an older grant than the token's: never asked, and not
    # the durable refusal a 404 would become.
    verdict = run_implementer.ae.verify_execution_grant(
        {"approval": {"approval_ref": "aar_older"}},
        service="mctl-api", slug="issue-1-x", current_digest="sha256:abc",
        lookup=run_implementer._lookup_grant,
    )
    assert asked == []
    assert verdict.transient and not verdict.ok and not verdict.code
    assert "aar_pinned" in verdict.detail

    # Its own grant is asked for, and the store's 404 for it does mean absent.
    verdict = run_implementer.ae.verify_execution_grant(
        {"approval": {"approval_ref": "aar_pinned"}},
        service="mctl-api", slug="issue-1-x", current_digest="sha256:abc",
        lookup=run_implementer._lookup_grant,
    )
    assert asked == ["aar_pinned"]
    assert verdict.code == run_implementer.ae.GRANT_NOT_FOUND


def test_an_execution_bound_run_has_no_pin(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, "enforce")
    monkeypatch.setenv(run_implementer.EXECUTION_ID_ENV, "we_1")
    api = _Api()
    monkeypatch.setattr(httpx, "post", lambda url, headers, timeout: api(url, headers["Authorization"][7:]))
    monkeypatch.setattr(httpx, "get", lambda url, headers, timeout: api(url, headers["Authorization"][7:]))
    run_implementer._setup_run_token()
    assert run_token.pinned_grant_id() == ""
