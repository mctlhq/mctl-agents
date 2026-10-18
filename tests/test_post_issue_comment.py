"""Unit tests for the post_issue_comment activity (mctl-agents#701)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from orchestrator.temporal.activities import issue_state
from orchestrator.temporal.activities.issue_state import IssueStateError, post_issue_comment


def _patch(monkeypatch, *, token="t", status=201, exc=None):
    monkeypatch.setattr(issue_state, "_resolve_token", lambda: token)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            if exc is not None:
                raise exc
            return httpx.Response(status, text="body", request=httpx.Request("POST", url))

    monkeypatch.setattr(issue_state.httpx, "AsyncClient", _Client)


def test_no_token_is_quiet_false(monkeypatch):
    _patch(monkeypatch, token=None)
    assert asyncio.run(post_issue_comment("o/r", 1, "x")) is False


def test_created_returns_true(monkeypatch):
    _patch(monkeypatch, status=201)
    assert asyncio.run(post_issue_comment("o/r", 1, "x")) is True


@pytest.mark.parametrize("status", [403, 404, 410, 422])
def test_4xx_is_quiet_false(monkeypatch, status):
    _patch(monkeypatch, status=status)
    assert asyncio.run(post_issue_comment("o/r", 1, "x")) is False


@pytest.mark.parametrize("status", [429, 500, 502])
def test_retryable_status_raises(monkeypatch, status):
    _patch(monkeypatch, status=status)
    with pytest.raises(IssueStateError):
        asyncio.run(post_issue_comment("o/r", 1, "x"))


def test_transport_error_raises(monkeypatch):
    _patch(monkeypatch, exc=httpx.ConnectError("boom"))
    with pytest.raises(IssueStateError):
        asyncio.run(post_issue_comment("o/r", 1, "x"))
