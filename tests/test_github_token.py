"""Unit tests for orchestrator.github_token.refresh_github_token."""
from __future__ import annotations

from orchestrator.github_token import refresh_github_token


def test_noop_when_github_token_file_unset(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "seed-token")
    refresh_github_token()
    assert __import__("os").environ["GITHUB_TOKEN"] == "seed-token"


def test_reads_fresh_token_from_file(tmp_path, monkeypatch):
    token_file = tmp_path / "github-token"
    token_file.write_text("ghs_freshtoken\n")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("GITHUB_TOKEN", "stale-token")
    refresh_github_token()
    assert __import__("os").environ["GITHUB_TOKEN"] == "ghs_freshtoken"


def test_keeps_previous_token_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("GITHUB_TOKEN", "still-here")
    refresh_github_token()
    assert __import__("os").environ["GITHUB_TOKEN"] == "still-here"


def test_keeps_previous_token_when_file_empty(tmp_path, monkeypatch):
    token_file = tmp_path / "github-token"
    token_file.write_text("")
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("GITHUB_TOKEN", "still-here")
    refresh_github_token()
    assert __import__("os").environ["GITHUB_TOKEN"] == "still-here"
