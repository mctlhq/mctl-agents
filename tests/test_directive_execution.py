"""Unit tests for orchestrator.directive_execution (mctl-agents#758)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

from orchestrator.directive_execution import (
    ExecutionLookupUnavailable,
    NoExecution,
    resolve_loop_execution,
)

URL = "https://github.com/mctlhq/mctl-web/issues/9"


class _Handle:
    def __init__(self, describe=None, query=None):
        self._describe, self._query = describe, query
        self.calls: list[str] = []

    async def describe(self, **_kw):
        self.calls.append("describe")
        if isinstance(self._describe, Exception):
            raise self._describe
        return self._describe

    async def query(self, name, **_kw):
        self.calls.append(f"query:{name}")
        if isinstance(self._query, Exception):
            raise self._query
        return self._query


class _Client:
    def __init__(self, handle):
        self.handle, self.ids = handle, []

    def get_workflow_handle(self, wid):
        self.ids.append(wid)
        return self.handle


def _running():
    return SimpleNamespace(status=SimpleNamespace(name="RUNNING"), run_id="run-1")


def _resolve(handle):
    return asyncio.run(resolve_loop_execution(URL, client=_Client(handle)))


def test_running_canonical_loop_returns_identity():
    h = _Handle(_running(), {"work_item_id": "wi_x", "execution_id": "we_y"})
    got = _resolve(h)
    assert (got.work_item_id, got.execution_id, got.run_id) == ("wi_x", "we_y", "run-1")
    assert got.workflow_id == "dev-loop-mctlhq-mctl-web-9"
    assert h.calls == ["describe", "query:work_context"]


def test_not_found_is_no_live_loop():
    with pytest.raises(NoExecution) as e:
        _resolve(_Handle(RPCError("nf", RPCStatusCode.NOT_FOUND, b"")))
    assert e.value.reason == "no-live-loop"


def test_completed_loop_is_no_live_loop():
    h = _Handle(SimpleNamespace(status=SimpleNamespace(name="COMPLETED"), run_id="r"))
    with pytest.raises(NoExecution) as e:
        _resolve(h)
    assert e.value.reason == "no-live-loop"


def test_seed_hash_execution_is_not_canonical():
    with pytest.raises(NoExecution) as e:
        _resolve(_Handle(_running(), {"work_item_id": "wi_x", "execution_id": "ab12cd"}))
    assert e.value.reason == "loop-not-canonical"


def test_empty_work_item():
    with pytest.raises(NoExecution) as e:
        _resolve(_Handle(_running(), {"work_item_id": "", "execution_id": "we_y"}))
    assert e.value.reason == "no-work-item"


def test_unknown_query_is_not_canonical():
    h = _Handle(_running(), WorkflowQueryFailedError("unknown queryType work_context"))
    with pytest.raises(NoExecution) as e:
        _resolve(h)
    assert e.value.reason == "loop-not-canonical"


def test_describe_rpc_error_is_unavailable():
    with pytest.raises(ExecutionLookupUnavailable):
        _resolve(_Handle(RPCError("down", RPCStatusCode.UNAVAILABLE, b"")))


def test_query_failure_is_unavailable():
    with pytest.raises(ExecutionLookupUnavailable):
        _resolve(_Handle(_running(), asyncio.TimeoutError()))
    with pytest.raises(ExecutionLookupUnavailable):
        _resolve(_Handle(_running(), WorkflowQueryFailedError("worker busy")))
