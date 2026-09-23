"""Tests for orchestrator/action_approvals.py and its wiring into the policy
checkpoint (mctlhq/mctl-agents#197/#198, mctl-api#366).

The fake mctl-api below implements the store's contract independently of the
module under test (its own intent encoding, idempotent create, 409 on intent
drift, lazy expiry, one compare-and-set consume), so the lookup is exercised
against the behaviour it relies on rather than against itself.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from orchestrator import action_approvals as aa
from orchestrator import policy_checkpoint as pc

DEPLOY = "mcp__mctl__mctl_deploy_service"
GRANTS = ("mcp__mctl__*",)
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
GO_VECTOR = "sha256:28c2d880c05722c10c09828b57160cdeccef1375fbf5a89982513c877e8a0df2"


# ---------------------------------------------------------------------------
# A fake mctl-api
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _go_intent_hash(body: dict[str, Any]) -> str:
    """mctl-api's encoding, written out independently of the module."""
    out = b"mctl-action-intent/v1\n"
    for name in ("execution_id", "action_kind", "target", "args_digest", "policy_rule_id", "policy_version",
                 "artifact_hash", "work_item_id"):
        raw = str(body.get(name, "")).encode()
        out += b"%s:%d:%s\n" % (name.encode(), len(raw), raw)
    return "sha256:" + hashlib.sha256(out).hexdigest()


class _Resp(io.BytesIO):
    def __init__(self, payload: Any, status: int) -> None:
        super().__init__(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        self._status = status

    def getcode(self) -> int:
        return self._status

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeMctlApi:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.records: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.spent = 0
        #: route -> callable(req) raising or returning a response, to inject faults.
        self.override: dict[str, Any] = {}
        self.lazy_expiry = True
        self.log: list[str] | None = None

    # -- helpers --------------------------------------------------------------

    def view(self, rec: dict[str, Any]) -> dict[str, Any]:
        out = dict(rec)
        expires = datetime.fromisoformat(rec["expires_at"])
        if self.lazy_expiry and rec["state"] in ("pending", "approved") and expires <= self.clock():
            out["state"] = "expired"
        return out

    def decide(self, approval_id: str, decision: str) -> None:
        rec = self.records[approval_id]
        assert self.view(rec)["state"] == "pending"
        rec["state"] = "approved" if decision == "approve" else "denied"
        rec["decided_by"] = "github:root"

    def only(self) -> dict[str, Any]:
        assert len(self.records) == 1, self.records
        return next(iter(self.records.values()))

    @staticmethod
    def _answer(status: int, payload: dict[str, Any]) -> Any:
        if status >= 300:
            raise urllib.error.HTTPError("https://api.example.test", status, "err", {},  # type: ignore[arg-type]
                                         io.BytesIO(json.dumps(payload).encode()))
        return _Resp(payload, status)

    def _ok(self, status: int, rec: dict[str, Any]) -> Any:
        return self._answer(status, {"schema_version": "actionapproval/v1", "approval": self.view(rec)})

    @staticmethod
    def _err(status: int, code: str) -> Any:
        return FakeMctlApi._answer(status, {"error": code, "code": code})

    # -- the opener -------------------------------------------------------------

    def open(self, req: urllib.request.Request, timeout: Any = None) -> Any:
        path = urlparse(req.full_url).path
        method = req.get_method()
        self.calls.append((method, path))
        parts = path.removeprefix("/api/v1/action-approvals").strip("/").split("/")
        route = "create" if parts == [""] else ("consume" if parts[-1] == "consume" else "get")
        if self.log is not None:
            self.log.append(route)
        if route in self.override:
            return self.override[route](req)
        body = json.loads(req.data) if req.data else {}
        if route == "create":
            return self._create(body)
        rec = self.records.get(parts[0])
        if rec is None:
            return self._err(404, "approval_not_found")
        if route == "get":
            return self._ok(200, rec)
        return self._consume(rec, body["intent_hash"])

    def _create(self, body: dict[str, Any]) -> Any:
        for required in ("execution_id", "action_kind", "target", "args_digest", "policy_rule_id", "policy_version",
                         "idempotency_key", "expires_at"):
            if not body.get(required):
                return self._err(400, "invalid_request")
        digest = _go_intent_hash(body)
        if body.get("intent_hash") and body["intent_hash"] != digest:
            return self._err(400, "intent_hash_mismatch")
        key = body["idempotency_key"]
        if key in self.by_key:
            rec = self.records[self.by_key[key]]
            if rec["intent_hash"] != digest:
                return self._err(409, "approval_idempotency_conflict")
            return self._ok(200, rec)
        expires = datetime.fromisoformat(body["expires_at"])
        if not expires > self.clock() or expires - self.clock() > timedelta(days=7):
            return self._err(400, "invalid_request")
        rid = f"aar_{len(self.records):032x}"
        self.records[rid] = {
            "id": rid, **{k: body.get(k, "") for k in ("execution_id", "action_kind", "target", "args_digest",
                                                          "policy_rule_id", "policy_version", "artifact_hash")},
            "idempotency_key": key, "requested_by": "service:mctl-agents", "intent_hash": digest,
            "state": "pending", "expires_at": expires.isoformat(), "created_at": self.clock().isoformat(),
            "schema_version": "actionapproval/v1",
        }
        self.by_key[key] = rid
        return self._ok(201, self.records[rid])

    def _consume(self, rec: dict[str, Any], presented: str) -> Any:
        state = self.view(rec)["state"]
        if state == "approved" and rec["intent_hash"] == presented:
            rec["state"] = "consumed"
            self.spent += 1
            return self._ok(200, rec)
        if state == "approved":
            return self._err(409, "approval_intent_mismatch")
        code = {"pending": "approval_not_approved"}.get(state, f"approval_{state}")
        return self._err(409, code)

    def consume_calls(self) -> int:
        return sum(1 for m, p in self.calls if m == "POST" and p.endswith("/consume"))


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> FakeMctlApi:
    monkeypatch.setattr(pc, "current_identity",
                        lambda: pc.ExecutionIdentity(execution_id="ctx-1", trace_id="tr-1", actor="human:alice"))
    return FakeMctlApi(clock)


def _lookup(api: FakeMctlApi, clock: _Clock, **client_kw: Any) -> aa.MctlApiApprovals:
    kw = {"base_url": "https://api.example.test", "token": "test-token", **client_kw}
    client = aa.ActionApprovalClient(**kw)
    client._opener = api  # type: ignore[assignment]
    return aa.MctlApiApprovals(client, now=clock)


def _deploy(lookup: aa.MctlApiApprovals, args: dict[str, Any], **kw: Any) -> pc.Decision:
    return pc.checkpoint(pc.MCP_TOOL_CALL, DEPLOY, "mctl", args, grants=GRANTS, approvals=lookup, **kw)


ARGS = {"service": "web", "tag": "1.2.0"}


def _approved(api: FakeMctlApi, lookup: aa.MctlApiApprovals, args: dict[str, Any] = ARGS) -> str:
    pending = _deploy(lookup, args)
    assert pending.code == pc.CODE_APPROVAL_PENDING
    api.decide(pending.approval_ref, "approve")
    return pending.approval_ref


# ---------------------------------------------------------------------------
# The intent
# ---------------------------------------------------------------------------


def test_intent_hash_reproduces_the_go_vector():
    vector = aa.ActionIntent(execution_id="e", action_kind="k", target="t", args_digest="a",
                             policy_rule_id="r", policy_version="v")
    assert aa.intent_hash(vector) == GO_VECTOR


def test_intent_hash_counts_utf8_bytes_and_covers_every_field():
    base = aa.ActionIntent("exec-1", "github.merge_pr", "mctlhq/mctl-api#1", "sha256:args", "merge-needs-human",
                           "v3", "sha256:diff", "wi_1")
    h = aa.intent_hash(base)
    assert h == _go_intent_hash(base.__dict__)
    utf8 = aa.ActionIntent("é", "k", "t", "a", "r", "v")
    assert aa.intent_hash(utf8) == _go_intent_hash(utf8.__dict__)  # "é" is 2 bytes, not 1 character
    for name in base.__dict__:
        assert aa.intent_hash(aa.ActionIntent(**{**base.__dict__, name: base.__dict__[name] + "x"})) != h, name
    shifted = aa.ActionIntent(**{**base.__dict__, "action_kind": base.action_kind + "m", "target": base.target[1:]})
    assert aa.intent_hash(shifted) != h


def test_idempotency_key_is_deterministic_per_intent():
    a = aa.ActionIntent("e", "k", "t", "a", "r", "v")
    b = aa.ActionIntent("e", "k", "t", "a2", "r", "v")
    assert aa.idempotency_key(a) == aa.idempotency_key(aa.ActionIntent("e", "k", "t", "a", "r", "v"))
    assert aa.idempotency_key(a) != aa.idempotency_key(b)
    assert aa.idempotency_key(a, 1) != aa.idempotency_key(a)
    assert len(aa.idempotency_key(a)) <= 512


def test_the_intent_binds_everything_the_action_digest_did():
    req = pc.ActionRequest(pc.MCP_TOOL_CALL, DEPLOY, "mctl", "sha256:x", execution_id="ctx-1", actor="human:alice")
    intent = aa.intent_for(req, rule_id="r", policy_version="p")
    assert (intent.execution_id, intent.action_kind, intent.artifact_hash) == (
        "ctx-1", f"{pc.MCP_TOOL_CALL}:{DEPLOY}", req.action_digest())
    other_actor = pc.ActionRequest(**{**req.__dict__, "actor": "human:mallory"})
    assert aa.intent_hash(aa.intent_for(other_actor, rule_id="r", policy_version="p")) != aa.intent_hash(intent)


# ---------------------------------------------------------------------------
# The lookup, through the checkpoint
# ---------------------------------------------------------------------------


def test_pending_is_not_permitted_and_names_a_reused_request(api, clock):
    lookup = _lookup(api, clock)
    first = _deploy(lookup, ARGS)
    again = _deploy(lookup, ARGS)
    assert (first.verdict, first.code, first.permitted, first.awaiting_approval) == (
        pc.REQUIRE_APPROVAL, pc.CODE_APPROVAL_PENDING, False, True)
    assert first.approval_ref.startswith("aar_") and again.approval_ref == first.approval_ref
    assert len(api.records) == 1 and api.consume_calls() == 0
    rec = api.only()
    assert rec["idempotency_key"].startswith("mctl-agents/intent/")
    assert rec["action_kind"] == f"{pc.MCP_TOOL_CALL}:{DEPLOY}" and rec["policy_rule_id"] == "mctl-mcp-default-approval"


def test_approved_and_matching_is_permitted_and_consumed_exactly_once(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    api.log = []
    effects: list[str] = []

    def side_effect() -> str:
        api.log.append("effect")
        effects.append("deployed")
        return "ok"

    request = pc.request_for(pc.MCP_TOOL_CALL, DEPLOY, "mctl", ARGS, grants=GRANTS)
    assert isinstance(request, pc.ActionRequest)
    assert pc.enforce(request, side_effect, approvals=lookup) == "ok"
    assert api.log == ["create", "consume", "effect"]  # consumed immediately before the effect
    with pytest.raises(pc.PolicyRefused) as replay:
        pc.enforce(request, side_effect, approvals=lookup)
    assert effects == ["deployed"] and api.spent == 1
    assert replay.value.decision.code == pc.CODE_APPROVAL_CONSUMED
    assert api.records[ref]["state"] == "consumed"


def test_the_permitted_decision_names_the_spent_receipt(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    d = _deploy(lookup, ARGS)
    assert (d.verdict, d.code, d.permitted, d.approval_ref) == (pc.REQUIRE_APPROVAL, pc.CODE_APPROVED, True, ref)


def test_changed_args_against_the_approved_receipt_is_a_mismatch_and_consumes_nothing(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    changed = {**ARGS, "tag": "9.9.9"}
    d = _deploy(lookup, changed, approval_ref=ref)
    assert (d.verdict, d.code, d.permitted, d.approval_ref) == (
        pc.REQUIRE_APPROVAL, pc.CODE_APPROVAL_INTENT_MISMATCH, False, ref)
    assert api.consume_calls() == 0 and api.records[ref]["state"] == "approved"
    # Without a named receipt the changed action is simply a new, pending request.
    d = _deploy(lookup, changed)
    assert d.code == pc.CODE_APPROVAL_PENDING and d.approval_ref != ref
    assert api.consume_calls() == 0
    # The approved action itself is still spendable exactly once.
    assert _deploy(lookup, ARGS, approval_ref=ref).permitted


def test_a_store_record_bound_to_another_intent_is_a_mismatch(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    other = {**api.records[ref], "intent_hash": "sha256:" + "0" * 64}
    # The store answers, approved, a record bound to another intent.
    api.override["create"] = api.override["get"] = lambda req: api._ok(200, other)
    for d in (_deploy(lookup, ARGS), _deploy(lookup, ARGS, approval_ref=ref)):
        assert (d.code, d.permitted) == (pc.CODE_APPROVAL_INTENT_MISMATCH, False)
    assert api.consume_calls() == 0


def test_consumed_fails_closed(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    api.records[ref]["state"] = "consumed"
    for d in (_deploy(lookup, ARGS), _deploy(lookup, ARGS, approval_ref=ref)):
        assert (d.verdict, d.code, d.permitted) == (pc.REQUIRE_APPROVAL, pc.CODE_APPROVAL_CONSUMED, False)
    assert api.consume_calls() == 0


def test_denied_fails_closed(api, clock):
    lookup = _lookup(api, clock)
    ref = _deploy(lookup, ARGS).approval_ref
    api.decide(ref, "deny")
    d = _deploy(lookup, ARGS)
    assert (d.code, d.permitted, d.approval_ref) == (pc.CODE_APPROVAL_DENIED, False, ref)
    assert api.consume_calls() == 0


def test_expired_fails_closed(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    clock.now += timedelta(days=2)
    d = _deploy(lookup, ARGS)
    assert (d.code, d.permitted, d.approval_ref) == (pc.CODE_APPROVAL_EXPIRED, False, ref)
    assert api.consume_calls() == 0


def test_an_approved_answer_past_its_expiry_is_expired_locally(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    api.lazy_expiry = False  # the store still says approved
    clock.now += timedelta(days=2)
    d = _deploy(lookup, ARGS, approval_ref=ref)
    assert (d.code, d.permitted) == (pc.CODE_APPROVAL_EXPIRED, False)
    assert api.consume_calls() == 0


def _raise_transport(req: Any) -> Any:
    raise urllib.error.URLError("connection refused")


@pytest.mark.parametrize("consume", [
    lambda req: FakeMctlApi._err(409, "approval_consumed"),  # lost a race to another consumer
    lambda req: FakeMctlApi._err(409, "approval_intent_mismatch"),
    lambda req: FakeMctlApi._err(503, "work_items_unavailable"),
    lambda req: FakeMctlApi._err(429, "rate_limited"),
    _raise_transport,
    lambda req: _Resp(b"not json", 200),
    lambda req: _Resp({"schema_version": "actionapproval/v1", "approval": {"id": "aar_x"}}, 200),
    lambda req: _Resp({"schema_version": "actionapproval/v1", "approval": {
        "id": "aar_x", "state": [], "intent_hash": "sha256:x", "expires_at": "2099-01-01T00:00:00+00:00"}}, 200),
    lambda req: FakeMctlApi._err(401, "unauthorized"),
], ids=["consumed", "mismatch", "503", "429", "transport", "malformed", "incomplete", "state-wrong-type", "401"])
def test_a_refused_or_uncertain_consume_does_not_run_the_side_effect(api, clock, consume):
    lookup = _lookup(api, clock)
    _approved(api, lookup)
    api.override["consume"] = consume
    effects: list[str] = []
    request = pc.request_for(pc.MCP_TOOL_CALL, DEPLOY, "mctl", ARGS, grants=GRANTS)
    with pytest.raises(pc.PolicyRefused) as refused:
        pc.enforce(request, lambda: effects.append("deployed"), approvals=lookup)
    assert effects == [] and api.consume_calls() == 1
    assert not refused.value.decision.permitted


def test_a_consume_answer_for_another_state_is_not_a_spend(api, clock):
    lookup = _lookup(api, clock)
    ref = _approved(api, lookup)
    api.override["consume"] = lambda req: api._ok(200, api.records[ref])  # 200 but still "approved"
    d = _deploy(lookup, ARGS)
    assert (d.code, d.permitted) == (pc.CODE_APPROVAL_LOOKUP_ERROR, False)


@pytest.mark.parametrize("create", [
    lambda req: FakeMctlApi._err(503, "work_items_unavailable"),
    lambda req: FakeMctlApi._err(500, "internal"),
    lambda req: FakeMctlApi._err(429, "rate_limited"),
    _raise_transport,
    lambda req: _Resp(b"", 200),
], ids=["503", "500", "429", "transport", "empty"])
def test_an_unknown_store_fails_closed(api, clock, create):
    lookup = _lookup(api, clock)
    api.override["create"] = create
    d = _deploy(lookup, ARGS)
    assert (d.verdict, d.code, d.permitted, d.undecided) == (pc.DENY, pc.CODE_APPROVAL_LOOKUP_ERROR, False, True)


@pytest.mark.parametrize("client_kw", [{"token": ""}, {"base_url": "http://api.example.test"}], ids=["token", "http"])
def test_an_unusable_client_fails_closed_without_sending(api, clock, client_kw):
    d = _deploy(_lookup(api, clock, **client_kw), ARGS)
    assert (d.code, d.permitted) == (pc.CODE_APPROVAL_LOOKUP_ERROR, False)
    assert api.calls == []


def test_a_refused_request_is_typed_and_fails_closed(api, clock):
    lookup = _lookup(api, clock)
    api.override["create"] = lambda req: FakeMctlApi._err(400, "intent_hash_mismatch")
    d = _deploy(lookup, ARGS)
    assert (d.verdict, d.code, d.permitted) == (pc.REQUIRE_APPROVAL, pc.CODE_APPROVAL_REFUSED, False)
    d = _deploy(lookup, ARGS, approval_ref="aar_missing")
    assert (d.code, d.permitted) == (pc.CODE_APPROVAL_REFUSED, False)


def test_without_an_execution_identity_no_request_is_made(api, clock, monkeypatch):
    monkeypatch.setattr(pc, "current_identity", lambda: pc.ExecutionIdentity())
    d = _deploy(_lookup(api, clock), ARGS)
    assert (d.code, d.permitted) == (pc.CODE_APPROVAL_REFUSED, False)
    assert api.calls == []


@pytest.mark.parametrize("state", [[], {}, 1, None, "approved "])
def test_a_wrong_typed_state_is_malformed_not_an_exception(state):
    payload = {"schema_version": "actionapproval/v1", "approval": {
        "id": "aar_x", "state": state, "intent_hash": "sha256:x", "expires_at": "2099-01-01T00:00:00+00:00"}}
    assert aa._record_of(payload) is None


def test_a_rotated_token_is_undecided_not_a_policy_answer(api, clock):
    lookup = _lookup(api, clock)
    api.override["create"] = lambda req: FakeMctlApi._err(401, "unauthorized")
    d = _deploy(lookup, ARGS)
    assert (d.verdict, d.code, d.permitted, d.undecided) == (pc.DENY, pc.CODE_APPROVAL_LOOKUP_ERROR, False, True)


def test_client_classifies_typed_codes():
    cases = {
        (409, "approval_not_approved"): aa.PENDING,
        (409, "approval_denied"): aa.DENIED,
        (409, "approval_expired"): aa.EXPIRED,
        (409, "approval_intent_mismatch"): aa.MISMATCH,
        (409, "approval_consumed"): aa.CONSUMED,
        (404, "approval_not_found"): aa.NOT_FOUND,
        (409, "approval_idempotency_conflict"): aa.REFUSED,
        (403, "approval_requester_forbidden"): aa.REFUSED,
        (403, "approval_reader_forbidden"): aa.REFUSED,
        (401, ""): aa.UNKNOWN,  # a rotated MCTL_TOKEN is a fault, not an answer
        (401, "unauthorized"): aa.UNKNOWN,
        (403, ""): aa.UNKNOWN,  # a principal without the scope, likewise
        (403, "forbidden"): aa.UNKNOWN,
        (400, "invalid_request"): aa.REFUSED,
        (503, "work_items_unavailable"): aa.UNKNOWN,
        (502, ""): aa.UNKNOWN,
        (429, ""): aa.UNKNOWN,
        (302, ""): aa.UNKNOWN,
    }
    for (status, code), want in cases.items():
        assert aa._refusal(status, {"code": code} if code else {}).status == want, (status, code)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_default_config_is_no_approvals_and_makes_no_http(monkeypatch):
    monkeypatch.delenv(pc.APPROVALS_ENV, raising=False)
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    monkeypatch.setattr(pc, "current_identity",
                        lambda: pc.ExecutionIdentity(execution_id="ctx-1", actor="human:alice"))
    sent: list[str] = []

    def _no_http(self: Any, req: Any, *a: Any, **kw: Any) -> Any:
        sent.append(getattr(req, "full_url", str(req)))
        raise AssertionError("no HTTP by default")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", _no_http)
    assert pc.configured_approvals() is pc.NO_APPROVALS
    d = pc.checkpoint(pc.MCP_TOOL_CALL, DEPLOY, "mctl", ARGS, grants=GRANTS)
    assert (d.verdict, d.code, d.permitted) == (pc.REQUIRE_APPROVAL, pc.CODE_APPROVAL_REQUIRED, False)
    assert sent == []


@pytest.mark.parametrize("ttl,want", [
    (None, aa.DEFAULT_TTL_S), ("", aa.DEFAULT_TTL_S), ("nonsense", aa.DEFAULT_TTL_S), ("1.5", aa.DEFAULT_TTL_S),
    ("1", aa.MIN_TTL_S), ("-5", aa.MIN_TTL_S), ("3600", 3600),
    (str(7 * 24 * 3600), aa.MAX_TTL_S), ("999999999", aa.MAX_TTL_S),
])
def test_the_ttl_env_var_is_parsed_and_clamped(monkeypatch, ttl, want):
    if ttl is None:
        monkeypatch.delenv(aa.TTL_ENV, raising=False)
    else:
        monkeypatch.setenv(aa.TTL_ENV, ttl)
    assert aa._ttl_s() == want


def test_an_over_cap_ttl_still_opens_a_request(api, clock, monkeypatch):
    """mctl-api refuses a window over 7 days with a 400; the clamp keeps an
    over-large TTL from turning every gated action into approval_refused."""
    monkeypatch.setenv(aa.TTL_ENV, str(30 * 24 * 3600))
    d = _deploy(_lookup(api, clock), ARGS)
    assert (d.code, d.permitted) == (pc.CODE_APPROVAL_PENDING, False)
    assert api.only()["state"] == "pending"


def test_the_env_var_selects_the_store(monkeypatch):
    monkeypatch.setenv(pc.APPROVALS_ENV, "none")
    assert pc.configured_approvals() is pc.NO_APPROVALS
    monkeypatch.setenv(pc.APPROVALS_ENV, "mctl-api")
    assert isinstance(pc.configured_approvals(), aa.MctlApiApprovals)
    monkeypatch.setenv(pc.APPROVALS_ENV, "mctl-apii")
    monkeypatch.setattr(pc, "current_identity", lambda: pc.ExecutionIdentity(execution_id="ctx-1"))
    d = pc.checkpoint(pc.MCP_TOOL_CALL, DEPLOY, "mctl", ARGS, grants=GRANTS)
    assert (d.verdict, d.code, d.permitted) == (pc.DENY, pc.CODE_APPROVAL_LOOKUP_ERROR, False)


def test_a_granted_outcome_without_a_receipt_is_refused():
    class _Sloppy:
        def redeem(self, request, *, rule_id, policy_version, approval_ref=""):
            return pc.ApprovalOutcome(pc.APPROVAL_GRANTED)

    d = pc.checkpoint(pc.MCP_TOOL_CALL, DEPLOY, "mctl", ARGS, grants=GRANTS, approvals=_Sloppy())
    assert (d.code, d.permitted) == (pc.CODE_APPROVAL_LOOKUP_ERROR, False)


def test_module_import_is_stdlib_only():
    result = subprocess.run(
        [sys.executable, "-c", "import orchestrator.action_approvals, sys; print(chr(10).join(sorted(sys.modules)))"],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    third_party = ("claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio")
    leaked = sorted(n for n in result.stdout.split("\n") if n.split(".")[0] in third_party)
    assert not leaked, leaked
