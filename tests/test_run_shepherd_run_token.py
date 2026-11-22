"""The shepherd sweep's scoped mctl-api credential (mctlhq/mctl-gitops#1980).

The liveness read takes its bearer from `run_token.mctl_api_bearer`. In enforce
mode it never uses the static token, and a sweep that cannot get a run token
stops rather than treat every proposal as free to drive.
"""

from __future__ import annotations

import io
import json

import pytest

from orchestrator import pr_adoption, run_shepherd, run_token
from orchestrator.lifecycle.shadow import LEGACY_OWNED, LEGACY_UNKNOWN

STATIC = "mctl_static_token"
RUN = "art_run_secret"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    run_token._reset_for_tests()
    monkeypatch.setenv("MCTL_TOKEN", STATIC)
    monkeypatch.delenv(run_token.MODE_ENV, raising=False)
    yield
    run_token._reset_for_tests()


def _capture_bearer(monkeypatch) -> list[str]:
    seen: list[str] = []

    class _Opener:
        def open(self, request, timeout=None):
            seen.append(request.get_header("Authorization"))
            body = {"status": "Running", "shepherd_in_loop": True, "shepherd_in_loop_known": True}
            return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(run_shepherd, "_no_redirect_opener", lambda: _Opener())
    return seen


def _enforce_with_run_token(monkeypatch) -> None:
    monkeypatch.setenv(run_token.MODE_ENV, run_token.MODE_ENFORCE)
    run_token._state.mode = run_token.MODE_ENFORCE
    run_token._state.token = run_token.RunToken(RUN, "art_1", "shepherd", "", "")


def test_liveness_read_uses_the_static_token_when_off(monkeypatch):
    seen = _capture_bearer(monkeypatch)
    assert run_shepherd._dev_loop_owns_answer("svc", "issue-1-x") == LEGACY_OWNED
    assert seen == [f"Bearer {STATIC}"]


def test_liveness_read_uses_the_run_token_under_enforce(monkeypatch):
    seen = _capture_bearer(monkeypatch)
    _enforce_with_run_token(monkeypatch)
    assert run_shepherd._dev_loop_owns_answer("svc", "issue-1-x") == LEGACY_OWNED
    assert seen == [f"Bearer {RUN}"]


def test_enforce_without_a_run_token_is_unknown_and_never_static(monkeypatch):
    seen = _capture_bearer(monkeypatch)
    monkeypatch.setenv(run_token.MODE_ENV, run_token.MODE_ENFORCE)
    assert run_shepherd._dev_loop_owns_answer("svc", "issue-1-x") == LEGACY_UNKNOWN
    assert seen == []


def test_refused_exchange_under_enforce_stops_the_sweep(monkeypatch, capsys):
    def refuse(**_):
        raise run_token.RunTokenError("run token exchange refused: HTTP 403")

    monkeypatch.setattr(run_token, "setup", refuse)
    with pytest.raises(SystemExit) as exc:
        run_shepherd._setup_run_token()
    assert exc.value.code == 2
    assert "exchange refused" in capsys.readouterr().err


def test_setup_names_the_shepherd_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(run_token, "setup", lambda **kw: calls.append(kw))
    run_shepherd._setup_run_token()
    assert calls == [{"shadow_scope": "shepherd"}]


def test_implementer_child_does_not_inherit_the_mode(monkeypatch):
    monkeypatch.setenv(run_token.MODE_ENV, run_token.MODE_ENFORCE)
    monkeypatch.setenv("SOME_OTHER", "kept")
    env = run_shepherd._implementer_child_env()
    assert run_token.MODE_ENV not in env
    assert env["SOME_OTHER"] == "kept"


def test_adoption_does_not_warn_of_a_missing_token_when_it_has_a_run_token(monkeypatch):
    """Enforce with MCTL_TOKEN gone from the pod: the run token is the credential."""
    _enforce_with_run_token(monkeypatch)
    monkeypatch.delenv("MCTL_TOKEN")
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *_: run_shepherd.LEGACY_FREE)
    budget = pr_adoption._DevLoopProbeBudget()
    assert pr_adoption._devloop_free("svc", "pr-1", [7], probe_budget=budget) is True
    assert budget.warned_no_token is False
