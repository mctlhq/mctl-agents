"""Unit tests for orchestrator.auth's detect_auth().

No coverage existed for this module before. It matters more than usual right
now: the Dockerfile used to install a global `claude` CLI, which made
`shutil.which("claude")` — detect_auth()'s third fallback path — always
succeed inside the image. That install was removed because claude-agent-sdk
bundles its own CLI binary and never used the global one anyway (see the
Dockerfile), but removing it also means `shutil.which("claude")` now returns
None in the image, and a container started with neither credential env var
set hits the RuntimeError instead of the silent cli_session fallback. These
tests pin that behavior down explicitly, in both directions, rather than
leaving it to be discovered at runtime.

Every test monkeypatches both credential env vars and `shutil.which`
directly rather than relying on ambient .env / PATH state, so the suite
behaves the same regardless of what a contributor's local environment has.
"""
from __future__ import annotations

import os

import pytest

from orchestrator import auth


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_oauth_token_selected_when_set(monkeypatch):
    _clear_creds(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")

    mode = auth.detect_auth()

    assert mode.name == "oauth"
    assert mode.env_var == "CLAUDE_CODE_OAUTH_TOKEN"


def test_api_key_selected_when_oauth_absent(monkeypatch):
    _clear_creds(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-fake")

    mode = auth.detect_auth()

    assert mode.name == "api_key"
    assert mode.env_var == "ANTHROPIC_API_KEY"


def test_oauth_wins_when_both_are_set(monkeypatch):
    """Matches the module docstring: "OAuth wins when both are set."."""
    _clear_creds(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-fake")

    mode = auth.detect_auth()

    assert mode.name == "oauth"


def test_malformed_oauth_token_still_selected_but_warns(monkeypatch, capsys):
    """A prefix mismatch is a warning, not a hard failure — likely a pasted
    API key in the wrong variable, but detect_auth() doesn't second-guess
    the user's env, it just flags it."""
    _clear_creds(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "not-an-oauth-token")

    mode = auth.detect_auth()

    assert mode.name == "oauth"
    assert "does not look like an OAuth token" in capsys.readouterr().out


def test_cli_session_fallback_when_claude_on_path(monkeypatch):
    """Local-dev path: no credential env vars, but a `claude` binary is
    somewhere on PATH (developer's own install, or — historically — the
    image's now-removed global npm install)."""
    _clear_creds(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude")

    mode = auth.detect_auth()

    assert mode.name == "cli_session"


def test_raises_when_no_creds_and_no_cli_on_path(monkeypatch):
    """The production/cron/Docker path this repository actually runs in:
    the image carries no global `claude` binary (claude-agent-sdk bundles
    its own and never used the global one — see the Dockerfile), so with
    no credential env var set, detect_auth() must fail loudly rather than
    silently proceed unauthenticated."""
    _clear_creds(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: None)

    with pytest.raises(RuntimeError, match="Auth not found"):
        auth.detect_auth()


def test_ensure_auth_for_sdk_drops_empty_api_key_under_oauth(monkeypatch):
    """The CLI gives ANTHROPIC_API_KEY precedence over OAuth even when it's
    an empty string, so ensure_auth_for_sdk() must pop it rather than leave
    a blank value that would silently outrank a real OAuth token."""
    _clear_creds(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    mode = auth.ensure_auth_for_sdk()

    assert mode.name == "oauth"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_ensure_auth_for_sdk_keeps_nonempty_api_key_under_oauth(monkeypatch):
    """A genuinely set ANTHROPIC_API_KEY is left alone even when OAuth also
    wins mode selection — only the empty/garbage case gets cleaned up."""
    _clear_creds(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-real")

    mode = auth.ensure_auth_for_sdk()

    assert mode.name == "oauth"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-api03-real"
