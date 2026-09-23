"""Tests for orchestrator/work_context/client.py (mctlhq/mctl-agents#267).

T4 in the proposal's tasks.md: monkeypatched urllib, in the style of
tests/test_lifecycle_client.py, answering with real mctl-api `workitem/v1`
responses (#452): 200 view + executions ledger -> WORK_ITEM_FOUND, the
work-items 404 -> WORK_ITEM_ABSENT,
409 -> WORK_ITEM_CONFLICT, transport error / non-https base / missing
MCTL_TOKEN -> WORK_ITEM_UNKNOWN; a 3xx is surfaced, never followed.
"""
from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from orchestrator.work_context import client as work_context_client
from orchestrator.work_context.contract import (
    WORK_ITEM_ABSENT,
    WORK_ITEM_CONFLICT,
    WORK_ITEM_FOUND,
    WORK_ITEM_UNKNOWN,
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


# Real mctl-api responses (see tests/test_work_context_contract.py).
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "workitem"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


VIEW = _fixture("get-active-resumed.json")
WID = VIEW["work_item"]["id"]


def _routes(view: Any, executions: Any) -> Any:
    """Answer the view and the executions route; each may be a payload or a
    handler of its own (to raise an HTTP error)."""
    seen: list[str] = []

    def _handle(req: Any) -> Any:
        seen.append(req.full_url)
        target = executions if req.full_url.endswith("/executions") else view
        return target(req) if callable(target) else _FakeResponse(json.dumps(target).encode())

    _handle.seen = seen  # type: ignore[attr-defined]
    return _handle


def test_get_reads_the_view_and_the_whole_execution_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _routes(VIEW, _fixture("executions-two.json"))
    answer = _client(monkeypatch, handler).get(WID)
    assert answer.verdict == WORK_ITEM_FOUND, answer.reason
    assert answer.item is not None
    assert answer.item.work_item_id == WID and answer.item.state_version == 3
    assert [e.sequence for e in answer.item.executions] == [1, 2]
    assert handler.seen == [
        f"https://api.example.test/api/v1/work-items/{WID}",
        f"https://api.example.test/api/v1/work-items/{WID}/executions",
    ]


def test_get_item_with_no_executions_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _fixture("get-active-no-executions.json")
    answer = _client(monkeypatch, _routes(view, _fixture("executions-empty.json"))).get(view["work_item"]["id"])
    assert answer.verdict == WORK_ITEM_FOUND and answer.item.executions == ()


def test_a_failed_executions_read_is_unknown_never_an_understated_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(req: Any) -> Any:
        raise OSError("connection reset")

    wrong_schema = {"schema_version": "workitem/v2", "executions": []}
    # Also with a view that names no latest execution, so nothing but the
    # failed read itself can refuse the answer.
    for view in (VIEW, _fixture("get-active-no-executions.json")):
        wid = view["work_item"]["id"]
        for executions in (_http_error(503, {"error": "store down"}), _boom, wrong_schema):
            answer = _client(monkeypatch, _routes(view, executions)).get(wid)
            assert answer.verdict == WORK_ITEM_UNKNOWN and answer.item is None, answer
            assert answer.reason.startswith("executions:")
            # Accepted only when the store answered: a 2xx it could not use.
            assert answer.accepted is (executions is wrong_schema)

    def _empty(req: Any) -> Any:
        return _FakeResponse(b"")

    answer = _client(monkeypatch, _routes(VIEW, _empty)).get(WID)
    assert answer.verdict == WORK_ITEM_UNKNOWN and answer.accepted is False


def test_a_ledger_missing_the_views_latest_execution_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _fixture("executions-two.json")
    stale["executions"] = stale["executions"][:1]
    answer = _client(monkeypatch, _routes(VIEW, stale)).get(WID)
    assert answer.verdict == WORK_ITEM_UNKNOWN and "latest" in answer.reason


def test_a_ledger_newer_than_the_view_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """An execution attached between the two reads overstates the ledger."""
    answer = _client(monkeypatch, _routes(_fixture("get-waiting.json"), _fixture("executions-two.json"))).get(WID)
    assert answer.verdict == WORK_ITEM_UNKNOWN and "read again" in answer.reason
    view = _fixture("get-active-no-executions.json")
    answer = _client(monkeypatch, _routes(view, _fixture("executions-two.json"))).get(WID)
    assert answer.verdict == WORK_ITEM_UNKNOWN


def test_an_answer_about_another_work_item_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _routes(VIEW, _fixture("executions-two.json"))
    answer = _client(monkeypatch, handler).get("wi_other")
    assert answer.verdict == WORK_ITEM_UNKNOWN and "the store answered" in answer.reason
    assert len(handler.seen) == 1  # refused before reading anyone's executions


def test_a_terminal_item_is_found_with_its_state(monkeypatch: pytest.MonkeyPatch) -> None:
    view = _fixture("get-completed.json")
    answer = _client(monkeypatch, _routes(view, _fixture("executions-two.json"))).get(WID)
    assert answer.verdict == WORK_ITEM_FOUND and answer.item.state == "completed"


def test_get_real_404_is_absent_and_reads_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _routes(_http_error(404, _fixture("get-not-found.json")), _fixture("executions-two.json"))
    answer = _client(monkeypatch, handler).get(WID)
    assert answer.verdict == WORK_ITEM_ABSENT
    assert len(handler.seen) == 1


def test_get_404_without_error_envelope_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 with no envelope did not come from mctl-api — an ingress rule
    or a wrong base path. Answering ABSENT would report a routing problem as
    a clean "no such work item"."""
    answer = _client(monkeypatch, _http_error(404, {})).get("wi-1")
    assert answer.verdict == WORK_ITEM_UNKNOWN


def test_get_409_is_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _client(monkeypatch, _http_error(409, {"error": "conflict", "code": "state_version_conflict"})).get("wi-1")
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




# -- attach_execution (mctlhq/mctl-agents#455) ------------------------------


def _attached(status: int, **overrides: Any) -> dict[str, Any]:
    execution = {"id": "we_1", "work_item_id": WID, "engine": "argo", "engine_ref": "wf-1", "attempt": 2,
                 "phase": "Running", **overrides}
    return {"schema_version": "workitem/v1", "execution": execution}


def test_attach_posts_the_engine_run_and_reads_back_the_store_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.work_context import executions as ex

    seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(req: Any) -> Any:
        seen.append((req.get_method(), req.full_url, json.loads(req.data)))
        return _FakeResponse(json.dumps(_attached(201)).encode(), status=201)

    answer = _client(monkeypatch, handler).attach_execution(WID, ex.EngineRun("argo", "wf-1"), ex.PHASE_RUNNING)
    assert (answer.verdict, answer.execution_id, answer.attempt) == (ex.EXECUTION_ATTACHED, "we_1", 2)
    assert seen == [(
        "POST", f"https://api.example.test/api/v1/work-items/{WID}/executions",
        {"engine": "argo", "engine_ref": "wf-1", "phase": "Running"},
    )]


@pytest.mark.parametrize(("status", "payload", "verdict"), [
    (200, _attached(200), "execution-existing"),
    (409, {"code": "execution_active", "error": "busy"}, "execution-active"),
    (409, {"code": "invalid_transition", "error": "already ended Failed"}, "execution-refused"),
    (409, {"code": "idempotency_key_reused", "error": "x"}, "execution-refused"),
    (404, {"code": "work_item_not_found", "error": "x"}, "execution-refused"),
    (403, {"code": "tenant_forbidden", "error": "x"}, "execution-unknown"),
    (503, {"error": "down"}, "execution-unknown"),
    # A 2xx counts only when it describes exactly what was sent.
    (201, _attached(201, id="sha-local"), "execution-unknown"),
    (201, _attached(201, engine_ref="wf-other"), "execution-unknown"),
    (201, _attached(201, work_item_id="wi_other"), "execution-unknown"),
    (201, _attached(201, phase="Pending"), "execution-unknown"),
    (201, _attached(201, attempt=0), "execution-unknown"),
    (201, _attached(201, attempt=True), "execution-unknown"),
    (201, {"schema_version": "workitem/v2", "execution": _attached(201)["execution"]}, "execution-unknown"),
])
def test_attach_answers_are_classified(status: int, payload: dict[str, Any], verdict: str) -> None:
    from orchestrator.work_context import executions as ex

    answer = ex.answer_from_attach(status, payload, work_item_id=WID, run=ex.EngineRun("argo", "wf-1"),
                                   phase=ex.PHASE_RUNNING)
    assert answer.verdict == verdict, answer.reason
    assert answer.usable == (verdict in ("execution-attached", "execution-existing"))


def test_attach_unreachable_store_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.work_context import executions as ex

    def _down(req: Any) -> Any:
        raise OSError("connection refused")

    answer = _client(monkeypatch, _down).attach_execution(WID, ex.EngineRun("argo", "wf-1"), ex.PHASE_RUNNING)
    assert answer.verdict == ex.EXECUTION_UNKNOWN


def test_attach_goes_through_the_policy_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import policy_checkpoint as pc
    from orchestrator.work_context import executions as ex

    sent: list[Any] = []

    def handler(req: Any) -> Any:
        sent.append(req)
        return _FakeResponse(json.dumps(_attached(201)).encode(), status=201)

    client = _client(monkeypatch, handler)
    run = ex.EngineRun("argo", "wf-1")
    assert client.attach_execution(WID, run, ex.PHASE_RUNNING).usable
    assert len(sent) == 1

    deny = pc.Policy(version="deny-all", rules=())
    monkeypatch.setattr(pc, "BUILTIN_POLICY", deny)
    monkeypatch.setattr(pc.checkpoint, "__kwdefaults__", {**pc.checkpoint.__kwdefaults__, "policy": deny})
    answer = client.attach_execution(WID, run, ex.PHASE_SUCCEEDED)
    assert answer.verdict == ex.EXECUTION_REFUSED and "policy DENY" in answer.reason
    assert len(sent) == 1  # nothing sent


def test_the_attach_rule_allows_only_the_attach() -> None:
    from orchestrator import policy_checkpoint as pc
    from orchestrator.work_context import executions as ex

    decision = pc.checkpoint(pc.MCTL_WORK_ITEM_WRITE, ex.ATTACH_EXECUTION_OPERATION, WID, {"a": 1})
    assert (decision.code, decision.rule_id) == (pc.CODE_ALLOWED, "mctl-attach-own-execution")
    for operation in ("resume", "transition:complete", "attach:work-item"):
        assert pc.checkpoint(pc.MCTL_WORK_ITEM_WRITE, operation, WID, {"a": 1}).code == pc.CODE_NO_RULE


def test_the_engine_run_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.work_context import executions as ex

    for var in (ex.ENGINE_ENV_VAR, ex.ENGINE_REF_ENV_VAR, ex.WORKFLOW_NAME_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    run, why = ex.engine_ref_from_env()
    assert run is None and "neither MCTL_ENGINE_REF nor WORKFLOW_NAME" in why

    monkeypatch.setenv(ex.WORKFLOW_NAME_ENV_VAR, " mctl-agents-investigate-x ")
    assert ex.engine_ref_from_env()[0] == ex.EngineRun("argo", "mctl-agents-investigate-x", ex.WORKFLOW_NAME_ENV_VAR)
    monkeypatch.setenv(ex.ENGINE_REF_ENV_VAR, "dev-loop-1")
    monkeypatch.setenv(ex.ENGINE_ENV_VAR, "temporal")
    assert ex.engine_ref_from_env()[0] == ex.EngineRun("temporal", "dev-loop-1", ex.ENGINE_REF_ENV_VAR)

    monkeypatch.setenv(ex.ENGINE_ENV_VAR, "jenkins")
    assert ex.engine_ref_from_env()[0] is None
    monkeypatch.setenv(ex.ENGINE_ENV_VAR, "argo")
    monkeypatch.setenv(ex.ENGINE_REF_ENV_VAR, "x" * (ex.MAX_ENGINE_REF_BYTES + 1))
    assert ex.engine_ref_from_env()[0] is None
