"""Tests for orchestrator/work_context/client.py (mctlhq/mctl-agents#267).

T4 in the proposal's tasks.md: monkeypatched urllib, in the style of
tests/test_lifecycle_client.py: 200 -> WORK_ITEM_FOUND, 404 -> WORK_ITEM_ABSENT,
409 -> WORK_ITEM_CONFLICT, transport error / non-https base / missing
MCTL_TOKEN -> WORK_ITEM_UNKNOWN; a 3xx is surfaced, never followed.
"""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from orchestrator.work_context import client as work_context_client
from orchestrator.work_context.contract import (
    WORK_ITEM_ABSENT,
    WORK_ITEM_CONFLICT,
    WORK_ITEM_FOUND,
    WORK_ITEM_UNKNOWN,
    ExecutionRef,
)


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200) -> None:
        super().__init__(data)
        self._status = status

    def getcode(self) -> int:
        return self._status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> work_context_client.WorkItemClient:
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    c = work_context_client.WorkItemClient()

    class _Opener:
        def open(self, req: Any, timeout: int | None = None) -> Any:
            return handler(req)

    c._opener = _Opener()
    return c


def _ok(payload: dict[str, Any]) -> Any:
    return lambda req: _FakeResponse(json.dumps(payload).encode())


def _http_error(status: int, payload: dict[str, Any]) -> Any:
    def _raise(req: Any) -> Any:
        raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(json.dumps(payload).encode()))

    return _raise


WORK_ITEM_PAYLOAD = {
    "work_item_id": "wi-1",
    "revision": "1",
    "state": "open",
    "issue_url": "https://github.com/mctlhq/mctl-agents/issues/267",
    "service": "mctl-agents",
    "slug": "issue-267-x",
    "executions": [],
}


def test_get_200_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _client(monkeypatch, _ok(WORK_ITEM_PAYLOAD)).get("wi-1")
    assert answer.verdict == WORK_ITEM_FOUND
    assert answer.item is not None
    assert answer.item.work_item_id == "wi-1"


def test_get_404_with_error_envelope_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _client(monkeypatch, _http_error(404, {"error": "no such work item"})).get("wi-1")
    assert answer.verdict == WORK_ITEM_ABSENT


def test_get_404_without_error_envelope_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 with no envelope did not come from mctl-api — an ingress rule
    or a wrong base path. Answering ABSENT would report a routing problem as
    a clean "no such work item"."""
    answer = _client(monkeypatch, _http_error(404, {})).get("wi-1")
    assert answer.verdict == WORK_ITEM_UNKNOWN


def test_get_409_is_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _client(monkeypatch, _http_error(409, {"error": "conflict", **WORK_ITEM_PAYLOAD})).get("wi-1")
    assert answer.verdict == WORK_ITEM_CONFLICT


def test_unreachable_store_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(req: Any) -> Any:
        raise OSError("connection refused")

    answer = _client(monkeypatch, _boom).get("wi-1")
    assert answer.verdict == WORK_ITEM_UNKNOWN
    assert answer.verdict != WORK_ITEM_ABSENT


def test_missing_token_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCTL_TOKEN", raising=False)
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    answer = work_context_client.WorkItemClient().get("wi-1")
    assert answer.verdict == WORK_ITEM_UNKNOWN


def test_non_https_base_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "http://api.example.test")
    answer = work_context_client.WorkItemClient().get("wi-1")
    assert answer.verdict == WORK_ITEM_UNKNOWN


def test_a_3xx_is_surfaced_never_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-redirect opener must see the 3xx as an HTTPError, not a
    silently-followed redirect to somewhere else."""
    answer = _client(monkeypatch, _http_error(302, {})).get("wi-1")
    assert answer.verdict == WORK_ITEM_UNKNOWN


def test_list_executions_200_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _client(monkeypatch, _ok(WORK_ITEM_PAYLOAD)).list_executions("wi-1")
    assert answer.verdict == WORK_ITEM_FOUND


def test_record_execution_posts_and_reads_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _handler(req: Any) -> Any:
        captured["body"] = json.loads(req.data)
        captured["url"] = req.full_url
        return _FakeResponse(json.dumps(WORK_ITEM_PAYLOAD).encode())

    execution = ExecutionRef(execution_id="e2", sequence=2)
    answer = _client(monkeypatch, _handler).record_execution("wi-1", execution)
    assert answer.verdict == WORK_ITEM_FOUND
    assert captured["body"]["execution_id"] == "e2"
    assert captured["url"].endswith("/api/v1/work-items/wi-1/executions")
