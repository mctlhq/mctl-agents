"""The review-worker producer (mctlhq/mctl-agents#986, ADR 025 "Producer").

A fake mctl-api (`httpx.MockTransport`, recording every request) stands in for
the service. No network. The fake models the two server rules the producer
must respect: one non-terminal execution per work item (`409
execution_active`), and a worker submit only under an `art_` bearer.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator import evidence_producer as ep
from orchestrator import execution_provenance as prov
from orchestrator import policy_checkpoint as pc
from orchestrator import review_receipts as rr
from orchestrator import run_review_worker as rrw
from orchestrator.options import REVIEW_WORKER_TOOLS, build_review_worker_options
from orchestrator.review_worker import contract as c
from orchestrator.review_worker import session, submitter
from orchestrator.review_worker.api import ReviewApi, reviewer_engine_run

SERVICE_BEARER = "Bearer svc-credential-xyz"
AUTHORING_EXEC = "we_" + "a" * 32
AUTHORING_ITEM = "wi_authoring"
AUTHORING_TENANT = "tenant-1"
REVIEW_WORKFLOW = "review-wf-1"
TOKEN = "art_" + "S3cretTokenValue" * 3
RECEIPT_ID = "rc_" + "1" * 16
RECEIPT_HASH = "sha256:" + "2" * 64
GITOPS_COMMIT = "b" * 40
TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})


def _attempt(ref: str) -> str:
    return prov.attempt_id("argo", ref)


AUTHORING_PROVENANCE = {
    "attempt_id": _attempt("authoring-wf"),
    "lineage_root": prov.lineage_root(AUTHORING_EXEC),
    "attached_direct_service": True,
}


class FakeApi:
    """A recording fake of the mctl-api routes the producer calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, Any]] = []
        self.overrides: list[tuple[str, re.Pattern[str], Callable[[httpx.Request, Any], httpx.Response]]] = []
        self.busy_items = {AUTHORING_ITEM}  # holds a non-terminal execution (a DevLoop parked at approval)
        self.items: dict[str, dict[str, str]] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.pool: list[dict[str, Any]] = []
        self.minted_tokens: list[str] = []
        self.revoked: list[str] = []
        self.phases: list[tuple[str, str]] = []  # (execution id, phase) per accepted attach

    def final_phases(self) -> dict[str, str]:
        return {eid: ex["phase"] for eid, ex in self.executions.items()}

    def on(self, method: str, pattern: str, fn: Callable[[httpx.Request, Any], httpx.Response]) -> None:
        self.overrides.insert(0, (method, re.compile(pattern), fn))

    def respond(self, method: str, pattern: str, status: int, body: Any = None) -> None:
        self.on(method, pattern, lambda _r, _b: httpx.Response(status, json=body if body is not None else {}))

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _a, _b in self.calls if method is None or m == method]

    def count(self, fragment: str) -> int:
        return sum(1 for _m, p, _a, _b in self.calls if fragment in p)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        body = json.loads(raw) if raw else None
        auth = request.headers.get("authorization", "")
        kind = "service" if auth == SERVICE_BEARER else "art" if auth.startswith("Bearer art_") else "other"
        path = request.url.path
        self.calls.append((request.method, path, kind, body))
        for method, pattern, fn in self.overrides:
            if method == request.method and pattern.search(path):
                return fn(request, body)
        return self._default(request, path, body, kind)

    def _default(self, request: httpx.Request, path: str, body: Any, kind: str) -> httpx.Response:
        method = request.method
        if method == "GET" and (m := re.fullmatch(r"/api/v1/executions/([^/]+)/provenance", path)):
            execution_id = m.group(1)
            if execution_id == AUTHORING_EXEC:
                return httpx.Response(200, json={"provenance": AUTHORING_PROVENANCE})
            ex = self.executions.get(execution_id)
            if ex is None:
                return httpx.Response(404, json={"code": "execution_provenance_absent"})
            return httpx.Response(200, json={"provenance": {
                "attempt_id": _attempt(ex["engine_ref"]),
                "lineage_root": prov.lineage_root(execution_id),
                "attached_direct_service": kind == "service",
            }})
        if method == "POST" and path == "/api/v1/review-requests":
            return httpx.Response(201, json={"request_id": body["request_id"]})
        if method == "GET" and (m := re.fullmatch(r"/api/v1/work-items/([^/]+)", path)):
            if m.group(1) == AUTHORING_ITEM:
                return httpx.Response(200, json={"work_item": {"id": AUTHORING_ITEM, "tenant": AUTHORING_TENANT}})
            return httpx.Response(404, json={"code": "work_item_not_found"})
        if method == "POST" and path == "/api/v1/work-items":
            item_id = "wi_review_" + body["external_key"].split("_")[-1]
            self.items[item_id] = body
            return httpx.Response(201, json={"schema_version": "workitem/v1", "work_item": {
                "id": item_id, "tenant": body["tenant"], "external_key": body["external_key"], "state": "active"}})
        if method == "POST" and (m := re.fullmatch(r"/api/v1/work-items/([^/]+)/executions", path)):
            # mctl-api: an execution is keyed on (work_item_id, engine,
            # engine_ref); re-attaching it moves its phase (200), and an item
            # holds one non-terminal execution at a time.
            item_id = m.group(1)
            key = (item_id, body["engine"], body["engine_ref"])
            existing = next((eid for eid, ex in self.executions.items()
                             if (ex["work_item_id"], ex["engine"], ex["engine_ref"]) == key), None)
            if existing is not None:
                ex = self.executions[existing]
                if ex["phase"] in TERMINAL_PHASES and body["phase"] != ex["phase"]:
                    return httpx.Response(409, json={"code": "execution_terminal"})
                ex["phase"] = body["phase"]
                self.phases.append((existing, body["phase"]))
                if body["phase"] in TERMINAL_PHASES:
                    self.busy_items.discard(item_id)
                return httpx.Response(200, json={"schema_version": "workitem/v1", "execution": {
                    "id": existing, "work_item_id": item_id, "engine": body["engine"],
                    "engine_ref": body["engine_ref"], "phase": body["phase"], "attempt": 1}})
            if item_id in self.busy_items:
                return httpx.Response(409, json={"code": "execution_active"})
            execution_id = "we_" + format(len(self.executions) + 1, "x").rjust(32, "0")
            self.executions[execution_id] = {**body, "work_item_id": item_id}
            self.phases.append((execution_id, body["phase"]))
            if body["phase"] not in TERMINAL_PHASES:
                self.busy_items.add(item_id)
            return httpx.Response(201, json={"schema_version": "workitem/v1", "execution": {
                "id": execution_id, "work_item_id": item_id, "engine": body["engine"],
                "engine_ref": body["engine_ref"], "phase": body["phase"], "attempt": 1}})
        if method == "POST" and path == "/api/v1/agent-run-tokens":
            self.minted_tokens.append(TOKEN)
            return httpx.Response(201, json={
                "token": TOKEN, "token_id": "tok_1", "scope": body["scope"],
                "execution_id": body["execution_id"], "expires_at": "2030-01-01T00:00:00Z"})
        if method == "POST" and path == "/api/v1/review-receipts":
            if kind != "art":
                return httpx.Response(403, json={"code": "review_run_token_required"})
            return httpx.Response(201, json={
                "receipt_id": RECEIPT_ID, "request_id": body["request_id"], "mode": "worker",
                "assurance": "independent", "tenant": AUTHORING_TENANT,
                "receipt": {"content_hash": RECEIPT_HASH}})
        if method == "POST" and path == "/api/v1/agent-run-tokens/self/revoke":
            self.revoked.append(request.headers.get("authorization", ""))
            return httpx.Response(204)
        if method == "GET" and path == "/api/v1/review-receipts":
            return httpx.Response(200, json={"receipts": self.pool})
        return httpx.Response(500, json={"code": "fake_unrouted"})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    monkeypatch.setenv("WORKFLOW_NAME", REVIEW_WORKFLOW)
    monkeypatch.delenv("MCTL_ENGINE_REF", raising=False)
    monkeypatch.delenv("MCTL_ENGINE", raising=False)
    return FakeApi()


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    # The production default pauses before the one retry; tests do not wait.
    monkeypatch.setattr(submitter, "RETRY_DELAY_SECONDS", 0.0)


@pytest.fixture
def sealed(monkeypatch: pytest.MonkeyPatch) -> list[ep.RunEvidence]:
    captured: list[ep.RunEvidence] = []
    monkeypatch.setattr(ep, "produce", lambda evidence, **_kw: captured.append(evidence) or ep.CREATED)
    return captured


def make_api(fake: FakeApi) -> ReviewApi:
    return ReviewApi(
        base_url="https://api.example.test", transport=fake.transport(),
        service_headers=lambda: {"Authorization": SERVICE_BEARER},
    )


def envelope_text(evidence: ep.RunEvidence) -> str:
    return ep.envelope_bytes(ep.build(evidence, amendment_2=False)).decode()


def artifacts_of(evidence: ep.RunEvidence) -> list[dict[str, str]]:
    return json.loads(envelope_text(evidence)).get("artifacts") or []


def proposal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "proposal"
    d.mkdir()
    for name in ("requirements.md", "design.md", "tasks.md"):
        (d / name).write_text(f"# {name}\nbody\n")
    return d


def spike_dir(tmp_path: Path) -> Path:
    d = tmp_path / "spike"
    (d / "measurements").mkdir(parents=True)
    (d / "report.md").write_text("# report\n")
    (d / "measurements" / "m.csv").write_text("a,b\n1,2\n")
    return d


PASS_VERDICT = {
    "outcome": "PASS",
    "criteria": [{"id": cid, "status": "met"} for cid in rr.RUBRICS[rr.RUBRIC_PROPOSAL]],
    "findings": "private findings text SECRET-FINDINGS",
}
SPIKE_PASS_VERDICT = {**PASS_VERDICT, "criteria": [{"id": cid, "status": "met"} for cid in rr.RUBRICS[rr.RUBRIC_SPIKE]]}


class Runner:
    def __init__(self, verdict: Any = PASS_VERDICT) -> None:
        self.verdict = verdict
        self.prompts: list[str] = []
        self.options: list[Any] = []

    async def __call__(self, options: Any, prompt: str) -> str:
        self.options.append(options)
        self.prompts.append(prompt)
        return self.verdict if isinstance(self.verdict, str) else json.dumps(self.verdict)


def proposal_args(tmp_path: Path, **over: Any) -> rrw.RunArgs:
    base: dict[str, Any] = {
        "subject_kind": rr.SUBJECT_PROPOSAL, "authoring_execution_id": AUTHORING_EXEC,
        "authoring_work_item_id": AUTHORING_ITEM, "author_actors": ("author-bot",),
        "service": "mctl-agents", "slug": "issue-1-x", "gitops_commit": GITOPS_COMMIT,
        "proposal_dir": proposal_dir(tmp_path), "artifact_dir": tmp_path / "artifacts",
    }
    return rrw.RunArgs(**{**base, **over})


def spike_args(tmp_path: Path, **over: Any) -> rrw.RunArgs:
    base: dict[str, Any] = {
        "subject_kind": rr.SUBJECT_SPIKE_REPORT, "authoring_execution_id": AUTHORING_EXEC,
        "authoring_work_item_id": AUTHORING_ITEM, "author_actors": ("author-bot",),
        "repository": "mctlhq/mctl-agents", "report_root": "docs/spikes/1", "report_path": "report.md",
        "source_sha": "c" * 40, "base_sha": "d" * 40, "head_sha": "e" * 40,
        "spike_dir": spike_dir(tmp_path), "artifact_dir": tmp_path / "artifacts",
    }
    return rrw.RunArgs(**{**base, **over})


def run(fake: FakeApi, args: rrw.RunArgs, runner: Runner | None = None) -> c.Outcome:
    return rrw.run(args, api=make_api(fake), runner=runner or Runner())


def sequence(fake: FakeApi) -> list[tuple[str, str, str]]:
    return [(m, p, a) for m, p, a, _b in fake.calls]


EXPECTED_ORDER = [
    ("GET", "provenance", "service"),
    ("POST", "/review-requests", "service"),
    ("GET", "/work-items/wi_authoring", "service"),
    ("POST", "/work-items", "service"),
    ("POST", "/executions", "service"),
    ("GET", "provenance", "service"),
    ("POST", "/agent-run-tokens", "service"),
    ("POST", "/review-receipts", "art"),
    ("POST", "/self/revoke", "art"),
    ("POST", "/executions", "service"),  # the reviewer execution's terminal phase
]


def assert_order(fake: FakeApi) -> None:
    got = sequence(fake)
    assert len(got) == len(EXPECTED_ORDER), got
    for (method, path, auth), (want_method, fragment, want_auth) in zip(got, EXPECTED_ORDER, strict=True):
        assert method == want_method and fragment in path and auth == want_auth, got


# ---------------------------------------------------------------------------
# T1, T2: happy paths
# ---------------------------------------------------------------------------
def test_t1_happy_path_proposal(fake: FakeApi, sealed: list[ep.RunEvidence], tmp_path: Path) -> None:
    runner = Runner()
    outcome = run(fake, proposal_args(tmp_path), runner)
    assert outcome.stored and outcome.receipt_id == RECEIPT_ID and outcome.content_hash == RECEIPT_HASH
    assert_order(fake)
    submit = next(b for m, p, a, b in fake.calls if p == "/api/v1/review-receipts")
    assert list(submit) == list(c.WORKER_BODY_KEYS)
    assert fake.revoked == [f"Bearer {TOKEN}"]
    mint = next(b for _m, p, _a, b in fake.calls if p == "/api/v1/agent-run-tokens")
    assert mint["agent"] == "review-worker" and mint["scope"] == "review" and mint["permissions"] == []
    assert mint["ttl_seconds"] == 900 and mint["execution_id"].startswith("we_")
    (evidence,) = sealed
    assert {"name": RECEIPT_ID, "kind": "review-receipt", "content_hash": RECEIPT_HASH} in artifacts_of(evidence)
    assert evidence.execution_id == mint["execution_id"]
    assert evidence.work_item_id.startswith("wi_review_")
    assert evidence.outcome is not None and evidence.outcome[0] == "succeeded"
    assert len(runner.prompts) == 1


def test_t2_happy_path_spike(fake: FakeApi, sealed: list[ep.RunEvidence], tmp_path: Path) -> None:
    outcome = run(fake, spike_args(tmp_path), Runner(SPIKE_PASS_VERDICT))
    assert outcome.stored
    assert_order(fake)
    opened = next(b for _m, p, _a, b in fake.calls if p == "/api/v1/review-requests")
    assert opened["rubric"] == rr.RUBRIC_SPIKE and opened["mode"] == "worker"
    assert opened["subject"]["kind"] == rr.SUBJECT_SPIKE_REPORT
    assert {a["kind"] for a in artifacts_of(sealed[0])} == {"review-receipt", "review-findings"}


def test_request_carries_server_facts_not_caller_strings(fake: FakeApi, tmp_path: Path) -> None:
    run(fake, proposal_args(tmp_path))
    opened = next(b for _m, p, _a, b in fake.calls if p == "/api/v1/review-requests")
    assert opened["authoring_attempt_ids"] == [AUTHORING_PROVENANCE["attempt_id"]]
    assert opened["authoring_lineage_root"] == AUTHORING_PROVENANCE["lineage_root"]
    assert opened["authoring_execution_id"] == AUTHORING_EXEC
    assert "created_at" not in opened
    assert len(opened["nonce"]) >= 43  # token_urlsafe(32)
    attach = next(b for _m, p, _a, b in fake.calls if p.endswith("/executions") and _m == "POST")
    assert attach["engine_ref"] == f"{REVIEW_WORKFLOW}#review"
    created = next(b for _m, p, _a, b in fake.calls if p == "/api/v1/work-items" and _m == "POST")
    assert created["tenant"] == AUTHORING_TENANT and created["external_key"] == f"review:{opened['request_id']}"
    assert created["idempotency_key"].endswith(opened["request_id"])


# ---------------------------------------------------------------------------
# T3: disabled route
# ---------------------------------------------------------------------------
def test_t3_route_disabled(fake: FakeApi, sealed: list[ep.RunEvidence], tmp_path: Path) -> None:
    fake.respond("POST", "^/api/v1/review-receipts$", 501, {"code": "review_worker_ingest_disabled"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_DISABLED and outcome.code == "review_worker_ingest_disabled"
    assert not outcome.stored and rrw.EXIT_BY_STATE[outcome.state] != 0
    assert fake.count("/review-receipts") == 1  # no retry
    assert fake.revoked  # the token is revoked however it ends
    assert all(a["kind"] != "review-receipt" for a in artifacts_of(sealed[0]))
    assert sealed[0].outcome == ("refused", "review_worker_ingest_disabled")


# ---------------------------------------------------------------------------
# T4, T9: classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(c.KNOWN_REFUSALS))
def test_t4_each_typed_refusal(code: str) -> None:
    status = c.KNOWN_REFUSALS[code] or 409
    outcome = c.classify(status, {"code": code, "error": "free text"}, request_id="rr_" + "0" * 16)
    assert outcome.state == c.STATE_REFUSED and outcome.code == code and not outcome.stored


def test_t4_unknown_4xx_code_is_refused_not_success() -> None:
    outcome = c.classify(418, {"code": "something_new"}, request_id="rr_" + "0" * 16)
    assert outcome.state == c.STATE_REFUSED and outcome.code == "something_new"
    assert c.classify(422, {}, request_id="rr_" + "0" * 16).state == c.STATE_REFUSED


def test_t4_refusal_through_the_run(fake: FakeApi, tmp_path: Path) -> None:
    fake.respond("POST", "^/api/v1/review-receipts$", 409, {"code": "review_reviewer_not_independent"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_REFUSED and outcome.code == "review_reviewer_not_independent"
    assert fake.count("/review-receipts") == 1


RID = "rr_" + "0" * 16


def _good_201(**over: Any) -> dict[str, Any]:
    body = {"receipt_id": RECEIPT_ID, "request_id": RID, "mode": "worker", "assurance": "independent",
            "tenant": "t", "receipt": {"content_hash": RECEIPT_HASH}}
    return {**body, **over}


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (503, {"code": "review_store_unavailable"}),
        (503, {}),
        (500, {}),
        (500, {"code": "review_nonce_consumed"}),  # a 5xx is never a definite refusal
        (502, "not json"),
        (0, {}),
        (401, {"code": "unauthorized"}),
        (408, {}),
        (429, {}),
        (400, {"code": "review_provenance_unavailable"}),
        (200, {}),
        (201, _good_201(assurance="advisory")),
        (201, _good_201(request_id="rr_" + "9" * 16)),
        (201, _good_201(mode="manual")),
        (201, _good_201(receipt_id="x")),
        (201, _good_201(receipt={})),
        (201, {}),
        (201, "not json"),
    ],
)
def test_t5_unobserved_or_unverified_is_never_stored(status: int, payload: Any) -> None:
    outcome = c.classify(status, payload, request_id=RID)
    assert outcome.state == c.STATE_UNOBSERVED and not outcome.stored


def test_t5_only_a_verified_201_is_stored() -> None:
    assert c.classify(201, _good_201(), request_id=RID).stored


def test_t9_classify_table() -> None:
    for status in (500, 501, 502, 503, 504):
        assert c.classify(status, {}, request_id=RID).state == c.STATE_UNOBSERVED
    assert c.classify(501, {"code": "review_worker_ingest_disabled"}, request_id=RID).state == c.STATE_DISABLED
    assert c.classify(200, _good_201(), request_id=RID).state == c.STATE_UNOBSERVED  # only 201 stores
    assert c.classify(404, {"code": "review_request_not_found"}, request_id=RID).state == c.STATE_REFUSED
    with pytest.raises(ValueError):
        c.Outcome(c.STATE_STORED)  # a stored outcome without a receipt cannot exist


def test_t5_retry_then_unobserved_stays_unobserved(fake: FakeApi, tmp_path: Path) -> None:
    fake.respond("POST", "^/api/v1/review-receipts$", 503, {"code": "review_store_unavailable"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_UNOBSERVED and outcome.code == "review_store_unavailable"
    assert fake.count("/review-receipts") == 2  # exactly one retry, same nonce
    bodies = [b for _m, p, _a, b in fake.calls if p == "/api/v1/review-receipts"]
    assert bodies[0] == bodies[1]


def _consumed_after_unobserved(fake: FakeApi) -> None:
    answers = iter([httpx.Response(503, json={"code": "review_store_unavailable"}),
                    httpx.Response(409, json={"code": "review_nonce_consumed"})])
    fake.on("POST", "^/api/v1/review-receipts$", lambda _r, _b: next(answers))


def _request_id(fake: FakeApi) -> str:
    return next(b for _m, p, _a, b in fake.calls if p == "/api/v1/review-requests")["request_id"]


def _stored_document(request_id: str, **over: Any) -> dict[str, Any]:
    """A stored receipt document as mctl-api keeps it (`ReviewReceipt.to_dict`
    keys, server fields filled): the element of `GET /review-receipts`."""
    doc = {
        "api_version": rr.API_VERSION, "kind": rr.RECEIPT_KIND, "request_id": request_id,
        "subject": {"kind": rr.SUBJECT_PROPOSAL}, "rubric": rr.RUBRIC_PROPOSAL, "outcome": "PASS",
        "criteria": [], "findings_ref": {"name": "f", "kind": "review-findings", "content_hash": RECEIPT_HASH},
        "mode": "worker", "assurance": "independent", "reviewer_actor": "agent:review-worker",
        "reviewer_execution_id": "we_" + "1" * 32, "attempt_id": "a", "lineage_root": "l", "nonce": "n",
        "evidence_ref": "", "receipt_id": RECEIPT_ID, "content_hash": RECEIPT_HASH,
        "created_at": "2026-11-03T00:00:00Z",
    }
    return {**doc, **over}


def _pool_answer(entries: list[dict[str, Any]]) -> httpx.Response:
    # mctl-api ListReviewReceipts: the complete pool, every element the flat document.
    return httpx.Response(200, json={"receipts": entries, "count": len(entries), "complete": True})


def test_t5_consumed_nonce_reconciled_from_the_real_pool_shape(fake: FakeApi, tmp_path: Path) -> None:
    _consumed_after_unobserved(fake)
    fake.on("GET", "^/api/v1/review-receipts$", lambda _r, _b: _pool_answer([
        _stored_document("rr_" + "9" * 16, receipt_id="rc_" + "9" * 16),  # someone else's, first
        _stored_document(_request_id(fake)),
    ]))
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.stored and outcome.receipt_id == RECEIPT_ID and outcome.content_hash == RECEIPT_HASH


def test_t5_pool_entry_in_the_submit_envelope_shape_is_read_from_the_right_level(
    fake: FakeApi, tmp_path: Path,
) -> None:
    """agy P2 on #990: an entry shaped like the 201 submit answer (envelope
    fields beside a nested `receipt` that holds only `content_hash`) used to
    be read entirely from the nested level, lost `mode`, `assurance` and
    `request_id`, and ended `unobserved` despite a stored receipt."""
    _consumed_after_unobserved(fake)
    fake.on("GET", "^/api/v1/review-receipts$", lambda _r, _b: _pool_answer([{
        "receipt_id": RECEIPT_ID, "request_id": _request_id(fake), "mode": "worker", "assurance": "independent",
        "tenant": AUTHORING_TENANT, "receipt": {"content_hash": RECEIPT_HASH}}]))
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.stored and outcome.content_hash == RECEIPT_HASH


@pytest.mark.parametrize("over", [{"assurance": "advisory"}, {"mode": "manual"}, {"content_hash": "x"}])
def test_t5_pool_entry_that_is_not_our_independent_receipt_is_not_stored(
    fake: FakeApi, tmp_path: Path, over: dict[str, Any],
) -> None:
    _consumed_after_unobserved(fake)
    fake.on("GET", "^/api/v1/review-receipts$",
            lambda _r, _b: _pool_answer([_stored_document(_request_id(fake), **over)]))
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_UNOBSERVED and outcome.code == c.CODE_RECEIPT_UNOBSERVED


@pytest.mark.parametrize("pool", [[], [{"receipt_id": RECEIPT_ID, "request_id": "rr_other", "mode": "worker",
                                        "assurance": "independent", "content_hash": RECEIPT_HASH}]])
def test_t5_consumed_nonce_without_our_receipt_is_unobserved(fake: FakeApi, tmp_path: Path, pool: list[Any]) -> None:
    _consumed_after_unobserved(fake)
    fake.pool = pool
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_UNOBSERVED and not outcome.stored


def test_t5_consumed_nonce_with_unreadable_pool_is_unobserved(fake: FakeApi, tmp_path: Path) -> None:
    _consumed_after_unobserved(fake)
    fake.respond("GET", "^/api/v1/review-receipts$", 503, {"code": "review_store_unavailable"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_UNOBSERVED and not outcome.stored


def test_t5_a_first_attempt_consumed_nonce_is_a_refusal_not_reconciled(fake: FakeApi, tmp_path: Path) -> None:
    fake.respond("POST", "^/api/v1/review-receipts$", 409, {"code": "review_nonce_consumed"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_REFUSED and outcome.code == "review_nonce_consumed"
    assert fake.count("/review-receipts") == 1


def test_t5_transport_timeout_is_unobserved(fake: FakeApi, tmp_path: Path) -> None:
    def boom(request: httpx.Request, _body: Any) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    fake.on("POST", "^/api/v1/review-receipts$", boom)
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_UNOBSERVED and outcome.code == "transport_error"


# ---------------------------------------------------------------------------
# T6: no execution, no token
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (503, {}),  # UNKNOWN
        (422, {"code": "invalid_phase"}),  # REFUSED
        (200, "existing"),  # EXISTING: an earlier review reused this engine run
        (409, {"code": "execution_active"}),
    ],
)
def test_t6_no_attached_execution_means_no_mint(fake: FakeApi, tmp_path: Path, status: int, body: Any) -> None:
    def attach(request: httpx.Request, req_body: Any) -> httpx.Response:
        if body == "existing":
            item = request.url.path.split("/")[4]
            return httpx.Response(200, json={"schema_version": "workitem/v1", "execution": {
                "id": "we_" + "f" * 32, "work_item_id": item, "engine": req_body["engine"],
                "engine_ref": req_body["engine_ref"], "phase": req_body["phase"], "attempt": 1}})
        return httpx.Response(status, json=body)

    fake.on("POST", r"/executions$", attach)
    outcome = run(fake, proposal_args(tmp_path))
    assert not outcome.stored
    assert fake.count("agent-run-tokens") == 0 and fake.count("review-receipts") == 0
    if body == "existing":
        assert outcome.code == c.CODE_NOT_INDEPENDENT


@pytest.mark.parametrize("bad", ["", "wi_x", "we_", "ex-0123456789abcdef"])
def test_t6_mint_and_submit_raises_before_any_request(fake: FakeApi, bad: str) -> None:
    request = rr.seal_request(
        subject=rr.proposal_subject(service="s", slug="x", gitops_commit=GITOPS_COMMIT, files=_files()),
        mode="worker", author_actors=[], nonce="n" * 43, authoring_execution_id=AUTHORING_EXEC,
    )
    draft = rr.seal_receipt(request=request, outcome="UNOBSERVABLE", criteria=[])
    with pytest.raises(ValueError):
        submitter.mint_and_submit(make_api(fake), bad, request, draft)
    assert fake.calls == []


def _files() -> dict[str, bytes | None]:
    return {"requirements.md": b"r", "design.md": b"d", "tasks.md": b"t", "proposed-content.md": None}


# ---------------------------------------------------------------------------
# T7: independence
# ---------------------------------------------------------------------------
def _sealed_request() -> rr.ReviewRequest:
    return rr.seal_request(
        subject=rr.proposal_subject(service="s", slug="x", gitops_commit=GITOPS_COMMIT, files=_files()),
        mode="worker", author_actors=[], nonce="n" * 43, authoring_execution_id=AUTHORING_EXEC,
        authoring_attempt_ids=[AUTHORING_PROVENANCE["attempt_id"]],
        authoring_lineage_root=AUTHORING_PROVENANCE["lineage_root"],
    )


GOOD = c.Provenance("we_" + "1" * 32, _attempt("review-wf-1"), prov.lineage_root("we_" + "1" * 32), True)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, None),
        ({"execution_id": AUTHORING_EXEC}, c.CODE_NOT_INDEPENDENT),
        ({"attempt_id": AUTHORING_PROVENANCE["attempt_id"]}, c.CODE_NOT_INDEPENDENT),
        ({"lineage_root": AUTHORING_PROVENANCE["lineage_root"]}, c.CODE_NOT_INDEPENDENT),
        ({"attached_direct_service": False}, c.CODE_INELIGIBLE),
        ({"attempt_id": ""}, c.CODE_EXECUTION_UNPROVEN),
        ({"lineage_root": ""}, c.CODE_EXECUTION_UNPROVEN),
    ],
)
def test_t7_check_independent_each_clause(change: dict[str, Any], expected: str | None) -> None:
    reviewer = c.Provenance(**{**GOOD.__dict__, **change})
    assert c.check_independent(_sealed_request(), reviewer) == expected


@pytest.mark.parametrize(
    ("record", "code"),
    [
        ({"attempt_id": AUTHORING_PROVENANCE["attempt_id"]}, c.CODE_NOT_INDEPENDENT),
        ({"lineage_root": AUTHORING_PROVENANCE["lineage_root"]}, c.CODE_NOT_INDEPENDENT),
        ({"attached_direct_service": False}, c.CODE_INELIGIBLE),
    ],
)
def test_t7_a_non_independent_attach_stops_before_the_mint(fake: FakeApi, tmp_path: Path, record: dict[str, Any],
                                                          code: str) -> None:
    def provenance(request: httpx.Request, _body: Any) -> httpx.Response:
        eid = request.url.path.split("/")[4]
        if eid == AUTHORING_EXEC:
            return httpx.Response(200, json={"provenance": AUTHORING_PROVENANCE})
        return httpx.Response(200, json={"provenance": {**GOOD.__dict__, **record}})

    fake.on("GET", r"/provenance$", provenance)
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_REFUSED and outcome.code == code
    assert fake.count("agent-run-tokens") == 0 and fake.count("review-receipts") == 0


def test_t7_reviewer_execution_equal_to_authoring_stops(fake: FakeApi, tmp_path: Path) -> None:
    def attach(request: httpx.Request, body: Any) -> httpx.Response:
        return httpx.Response(201, json={"schema_version": "workitem/v1", "execution": {
            "id": AUTHORING_EXEC, "work_item_id": request.url.path.split("/")[4], "engine": body["engine"],
            "engine_ref": body["engine_ref"], "phase": body["phase"], "attempt": 1}})

    fake.on("POST", r"/executions$", attach)
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.code == c.CODE_NOT_INDEPENDENT
    assert fake.count("agent-run-tokens") == 0


# ---------------------------------------------------------------------------
# T8: capability bound
# ---------------------------------------------------------------------------
def test_t8_reviewer_capability_is_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MCTL_TOKEN"):
        monkeypatch.setenv(name, "leak-" + name)
    options = build_review_worker_options(tmp_path, "model-x")
    assert options.allowed_tools == ["Read", "Grep", "Glob"] == list(REVIEW_WORKER_TOOLS)
    assert options.mcp_servers == {}
    env = options.env or {}
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MCTL_TOKEN"):
        assert not env.get(name)
    assert "leak-" not in repr(env)


GITHUB_OPERATIONS = {
    pc.GITHUB_GIT_PUSH: "push:new-branch",
    pc.GITHUB_PR_CREATE: "create",
    pc.GITHUB_PR_MERGE: "merge",
    pc.GITHUB_PR_COMMENT: "comment:review-trigger",
    pc.GITHUB_ISSUE_COMMENT: "comment",
    pc.GITHUB_ISSUE_LABEL: "remove",
    pc.GITHUB_RUN_RERUN: "rerun:failed",
}


@pytest.mark.parametrize(("kind", "operation"), sorted(GITHUB_OPERATIONS.items()))
def test_t8_policy_denies_every_github_action(kind: str, operation: str) -> None:
    decision = pc.checkpoint(kind, operation, "t", {}, policy=pc.REVIEW_WORKER_POLICY)
    assert not decision.permitted and decision.verdict == pc.DENY
    # The builtin policy would have allowed it: the named policy is what refuses.
    assert pc.REVIEW_WORKER_POLICY.rules and all(r.verdict == pc.DENY for r in pc.REVIEW_WORKER_POLICY.rules)


def test_t8_policy_has_no_mctl_or_allow_rule_and_denies_mcp() -> None:
    assert all(r.verdict == pc.DENY and r.action_kind.startswith("github.") for r in pc.REVIEW_WORKER_POLICY.rules)
    decision = pc.checkpoint(pc.MCP_TOOL_CALL, "mcp__mctl__mctl_get_review_gate", "mctl", {},
                             grants=("mcp__mctl__*",), policy=pc.REVIEW_WORKER_POLICY)
    assert not decision.permitted


def test_t8_no_gating_bot_login_in_the_review_worker_path() -> None:
    from orchestrator.run_shepherd import GATING_BOTS

    root = Path(rrw.__file__).parent
    sources = [root / "run_review_worker.py", *sorted((root / "review_worker").glob("*.py"))]
    text = "\n".join(p.read_text() for p in sources).lower()
    options_text = repr(build_review_worker_options(Path("/tmp"), "m")).lower()
    for login in GATING_BOTS:
        assert login.lower() not in text and login.lower() not in options_text
    github_writers = ("github_token", "refresh_github_token", "run_shepherd", "gh pr", "gh api")
    assert not [w for w in github_writers if w in text]


# ---------------------------------------------------------------------------
# T10: secret hygiene
# ---------------------------------------------------------------------------
def test_t10_token_and_nonce_stay_out_of_every_surface(
    fake: FakeApi, sealed: list[ep.RunEvidence], tmp_path: Path, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    runner = Runner()
    outcome = run(fake, proposal_args(tmp_path), runner)
    assert outcome.stored
    nonce = next(b for _m, p, _a, b in fake.calls if p == "/api/v1/review-requests")["nonce"]
    captured = capsys.readouterr()
    surfaces = [captured.out, captured.err, caplog.text, envelope_text(sealed[0]), repr(runner.options[0]),
                "\n".join(runner.prompts), repr(runner.options[0].env), repr(outcome)]
    for surface in surfaces:
        assert TOKEN not in surface and nonce not in surface
        assert "SECRET-FINDINGS" not in surface  # findings text never leaves the private artifact
    submit = next(b for _m, p, _a, b in fake.calls if p == "/api/v1/review-receipts")
    for forbidden in ("reviewer_actor", "reviewer_execution_id", "attempt_id", "lineage_root", "assurance",
                      "mode", "tenant"):
        assert forbidden not in submit
    assert submit["evidence_ref"] == ""
    findings_file = tmp_path / "artifacts" / session.FINDINGS_NAME
    assert "SECRET-FINDINGS" in findings_file.read_text()
    assert oct(findings_file.stat().st_mode)[-3:] == "600"


def test_t10_errors_carry_status_and_code_only(fake: FakeApi, tmp_path: Path) -> None:
    fake.respond("POST", "^/api/v1/agent-run-tokens$", 400, {"code": "agent_unknown", "error": TOKEN + " leak"})
    outcome = run(fake, proposal_args(tmp_path))
    assert TOKEN not in repr(outcome)
    with pytest.raises(c.Stop) as info:
        raise c.Stop(outcome)
    assert TOKEN not in str(info.value)


def test_t13_unregistered_agent_is_refused(fake: FakeApi, tmp_path: Path) -> None:
    fake.respond("POST", "^/api/v1/agent-run-tokens$", 400, {"code": "agent_unknown"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_REFUSED and outcome.code == "agent_unknown"
    assert fake.count("review-receipts") == 0 and not fake.revoked


def test_mint_that_does_not_describe_this_execution_is_not_used(fake: FakeApi, tmp_path: Path) -> None:
    fake.on("POST", "^/api/v1/agent-run-tokens$", lambda _r, b: httpx.Response(201, json={
        "token": TOKEN, "token_id": "t", "scope": "review", "execution_id": "we_" + "0" * 32}))
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_REFUSED and outcome.code == c.CODE_TOKEN_UNVERIFIED
    assert fake.count("review-receipts") == 0
    assert fake.revoked  # the unusable token is still revoked


# ---------------------------------------------------------------------------
# T11: authoring resolution
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("execution_id", ["", "ex-0123456789abcdef", "wi_x", "we_not-hex"])
def test_t11_unresolvable_authoring_id(fake: FakeApi, tmp_path: Path, execution_id: str) -> None:
    outcome = run(fake, proposal_args(tmp_path, authoring_execution_id=execution_id))
    assert outcome.code == c.CODE_AUTHORING_UNRESOLVED and outcome.state == c.STATE_REFUSED
    assert fake.calls == []


@pytest.mark.parametrize("status", [404, 503])
def test_t11_provenance_read_failure(fake: FakeApi, tmp_path: Path, status: int) -> None:
    fake.respond("GET", r"/provenance$", status, {"code": "execution_provenance_absent"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.code == c.CODE_AUTHORING_UNRESOLVED
    assert fake.paths("POST") == []  # no request, attach or mint


def test_t11_unreadable_provenance_body(fake: FakeApi, tmp_path: Path) -> None:
    fake.respond("GET", r"/provenance$", 200, {"provenance": {"attempt_id": "nope"}})
    assert run(fake, proposal_args(tmp_path)).code == c.CODE_AUTHORING_UNRESOLVED


# ---------------------------------------------------------------------------
# T13: reviewer work item
# ---------------------------------------------------------------------------
def test_t13_reviewer_gets_its_own_item_while_the_authoring_item_is_busy(fake: FakeApi, tmp_path: Path) -> None:
    assert AUTHORING_ITEM in fake.busy_items
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.stored
    attached_to = [p for m, p, _a, _b in fake.calls if m == "POST" and p.endswith("/executions")]
    assert attached_to and all(AUTHORING_ITEM not in p for p in attached_to)


def test_t13_mutation_attaching_to_the_authoring_item_is_refused(fake: FakeApi) -> None:
    with pytest.raises(c.Stop) as info:
        make_api(fake).attach_reviewer(AUTHORING_ITEM, reviewer_engine_run(REVIEW_WORKFLOW))
    assert info.value.outcome.code == "execution_active"


@pytest.mark.parametrize(
    ("status", "body", "state"),
    [
        (409, {"code": "external_key_in_use"}, c.STATE_REFUSED),
        (503, {"code": "work_items_unavailable"}, c.STATE_UNOBSERVED),
        (500, {}, c.STATE_UNOBSERVED),
        (0, {}, c.STATE_UNOBSERVED),
    ],
)
def test_t13_work_item_create_failure_stops_before_attach(fake: FakeApi, tmp_path: Path, status: int, body: Any,
                                                          state: str) -> None:
    if status == 0:
        def boom(request: httpx.Request, _b: Any) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        fake.on("POST", r"^/api/v1/work-items$", boom)
    else:
        fake.respond("POST", r"^/api/v1/work-items$", status, body)
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == state and not outcome.stored
    assert not [p for p in fake.paths("POST") if p.endswith("/executions")]
    assert fake.count("agent-run-tokens") == 0


def test_t13_a_replayed_create_is_this_requests_item(fake: FakeApi, tmp_path: Path) -> None:
    def replay(_r: httpx.Request, body: Any) -> httpx.Response:
        return httpx.Response(200, json={"schema_version": "workitem/v1", "work_item": {
            "id": "wi_replayed", "tenant": body["tenant"], "external_key": body["external_key"], "state": "active"}})

    fake.on("POST", r"^/api/v1/work-items$", replay)
    assert run(fake, proposal_args(tmp_path)).stored
    assert any(p == "/api/v1/work-items/wi_replayed/executions" for _m, p, _a, _b in fake.calls)


# ---------------------------------------------------------------------------
# Session: digest, strict verdict, Tier A guard
# ---------------------------------------------------------------------------
def test_session_digest_mismatch_stops_before_the_model(fake: FakeApi, tmp_path: Path) -> None:
    args = proposal_args(tmp_path)
    subject, material = rrw._subject_and_material(args)
    request = rr.seal_request(subject=subject, mode="worker", author_actors=[], nonce="n" * 43,
                              authoring_execution_id=AUTHORING_EXEC)
    drifted = session.ProposalMaterial({**material.files, "design.md": b"changed"})  # type: ignore[union-attr]
    runner = Runner()
    with pytest.raises(session.SessionError) as info:
        asyncio.run(session.run_review(request, drifted, model="m", agent_dir=tmp_path, work_dir=tmp_path,
                                       artifact_dir=tmp_path / "a", runner=runner))
    assert info.value.code == c.CODE_SUBJECT_MISMATCH and runner.prompts == []


@pytest.mark.parametrize(
    "verdict",
    [
        "not json",
        {"outcome": "PASS", "criteria": [], "findings": "x", "extra": 1},
        {"outcome": "PASS", "criteria": [{"id": "scope_bounded"}], "findings": "x"},
        {"outcome": "PASS", "criteria": "x", "findings": "x"},
        {"outcome": "PASS", "criteria": [], "findings": 3},
    ],
)
def test_session_malformed_verdict_does_not_submit(fake: FakeApi, tmp_path: Path, verdict: Any) -> None:
    outcome = run(fake, proposal_args(tmp_path), Runner(verdict))
    assert outcome.state == c.STATE_REFUSED and outcome.code == c.CODE_VERDICT_MALFORMED
    assert fake.count("agent-run-tokens") == 0 and fake.count("review-receipts") == 0


@pytest.mark.parametrize(
    ("verdict", "code"),
    [
        ({"outcome": "PASS", "criteria": [{"id": "scope_bounded", "status": "met"}], "findings": "x"},
         rr.CODE_PASS_WITH_UNOBSERVED),
        ({"outcome": "PASS", "findings": "x",
          "criteria": [{"id": cid, "status": "unobserved"} for cid in rr.RUBRICS[rr.RUBRIC_PROPOSAL]]},
         rr.CODE_PASS_WITH_UNOBSERVED),
        ({"outcome": "NEEDS_HUMAN", "criteria": [{"id": "hypothesis_stated", "status": "met"}], "findings": "x"},
         rr.CODE_MALFORMED),
    ],
)
def test_session_tier_a_refusal_does_not_submit(fake: FakeApi, tmp_path: Path, verdict: Any, code: str) -> None:
    outcome = run(fake, proposal_args(tmp_path), Runner(verdict))
    assert outcome.state == c.STATE_REFUSED and outcome.code == code
    assert fake.count("agent-run-tokens") == 0 and fake.count("review-receipts") == 0


def test_a_stopped_run_still_seals_evidence_with_the_outcome(fake: FakeApi, sealed: list[ep.RunEvidence],
                                                             tmp_path: Path) -> None:
    fake.respond("GET", r"/provenance$", 503, {})
    run(fake, proposal_args(tmp_path))
    (evidence,) = sealed
    assert evidence.outcome is not None and evidence.outcome[0] == "refused"
    assert all(a["kind"] != "review-receipt" for a in artifacts_of(evidence))


def test_note_artifact_ref_validates_the_hash() -> None:
    evidence = ep.RunEvidence(ep.STAGE_REVIEWER)
    evidence.note_artifact_ref("rc_x", "review-receipt", "not-a-hash")
    assert not evidence.artifacts and evidence.gaps
    evidence.note_artifact_ref("rc_x", "review-receipt", RECEIPT_HASH)
    assert [(a.name, a.kind, a.content_hash) for a in evidence.artifacts] == [("rc_x", "review-receipt", RECEIPT_HASH)]
    assert ep.STAGE_REVIEWER in ep.STAGES


def test_cli_help_wires_argparse() -> None:
    with pytest.raises(SystemExit) as info:
        rrw._parser().parse_args(["--help"])
    assert info.value.code == 0


# ---------------------------------------------------------------------------
# T12: the manifest against a fixture copy of the companion profile
# ---------------------------------------------------------------------------
COMPANION_PROFILE = """\
apiVersion: agents.mctl.ai/v1alpha2
kind: ExecutionProfile
metadata:
  name: independent-review
  owner: platform
spec:
  version: "1.0.0"
  modelPolicyRef:
    task: service_agent
    compatibility: ">=1.0.0 <2.0.0"
  skills: []
  tools:
    - Read
    - Grep
    - Glob
  policyRef: independent-review
  permissions:
    repository:
      read: true
      branchCreate: false
      commit: false
      pullRequestCreate: false
      merge: false
    kubernetes: none
    network: approved-providers-only
    mutationScopes: []
  budgetUsd: 2.00
  runtime:
    entrypoint: orchestrator.run_review_worker:main
    optionsBuilder: orchestrator.options:build_review_worker_options
    sandbox:
      backend: argo
      clusterWorkflowTemplate: mctl-agents-run
      approved: true
"""


def test_t12_manifest_validates_and_matches_the_companion_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator import validate_manifest as vm
    from orchestrator.manifest import load

    manifest = load(vm.REPO_ROOT / "agents" / "_manifests" / "review-worker" / "agent.yaml")
    assert manifest.options_builder == "orchestrator.options:build_review_worker_options"
    assert set(manifest.tool_allow) == set(REVIEW_WORKER_TOOLS)  # the tools diff is empty
    assert vm._check_tool_policy_and_budget_match_options_py(manifest) == []
    assert vm._check_entrypoints_resolve(manifest) == []
    assert vm._check_prompt_sources(manifest) == []

    profiles = tmp_path / "execution-profiles" / "independent-review"
    profiles.mkdir(parents=True)
    (profiles / "profile.yaml").write_text(COMPANION_PROFILE)
    monkeypatch.setattr(vm, "GITOPS_CATALOG_PROFILES_DIR", profiles.parent)
    assert vm.check_catalog_profiles_match_builders({"review-worker": manifest}) == []


# ---------------------------------------------------------------------------
# The reviewer execution always ends in a terminal phase (claude P2 on #990)
# ---------------------------------------------------------------------------
def _reviewer_phases(fake: FakeApi) -> dict[str, str]:
    return {eid: phase for eid, phase in fake.final_phases().items()}


def test_terminal_phase_succeeded_when_stored(fake: FakeApi, tmp_path: Path) -> None:
    assert run(fake, proposal_args(tmp_path)).stored
    (eid,) = fake.executions
    assert _reviewer_phases(fake) == {eid: "Succeeded"}
    assert [ph for e, ph in fake.phases if e == eid] == ["Running", "Succeeded"]
    item = fake.executions[eid]["work_item_id"]
    assert item not in fake.busy_items  # the item holds no non-terminal execution any more


@pytest.mark.parametrize(
    ("arrange", "state"),
    [
        (lambda f: f.respond("POST", "^/api/v1/review-receipts$", 409, {"code": "review_reviewer_not_independent"}),
         c.STATE_REFUSED),
        (lambda f: f.respond("POST", "^/api/v1/review-receipts$", 501, {"code": "review_worker_ingest_disabled"}),
         c.STATE_DISABLED),
        (lambda f: f.respond("POST", "^/api/v1/review-receipts$", 503, {"code": "review_store_unavailable"}),
         c.STATE_UNOBSERVED),
        (lambda f: f.respond("POST", "^/api/v1/agent-run-tokens$", 400, {"code": "agent_unknown"}),
         c.STATE_REFUSED),
    ],
)
def test_terminal_phase_failed_for_every_end_without_a_receipt(
    fake: FakeApi, tmp_path: Path, arrange: Callable[[FakeApi], None], state: str,
) -> None:
    arrange(fake)
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == state
    assert list(_reviewer_phases(fake).values()) == ["Failed"]


def test_terminal_phase_failed_when_a_stop_ends_the_run_after_the_attach(fake: FakeApi, tmp_path: Path) -> None:
    # The session refuses (malformed verdict): no mint, but the execution still ends.
    outcome = run(fake, proposal_args(tmp_path), Runner("not json"))
    assert outcome.code == c.CODE_VERDICT_MALFORMED
    assert list(_reviewer_phases(fake).values()) == ["Failed"]


def test_terminal_phase_failed_when_the_reviewer_provenance_is_unreadable(fake: FakeApi, tmp_path: Path) -> None:
    def provenance(request: httpx.Request, _body: Any) -> httpx.Response:
        if request.url.path.split("/")[4] == AUTHORING_EXEC:
            return httpx.Response(200, json={"provenance": AUTHORING_PROVENANCE})
        return httpx.Response(503, json={"code": "execution_provenance_unavailable"})

    fake.on("GET", r"/provenance$", provenance)
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.state == c.STATE_UNOBSERVED
    assert list(_reviewer_phases(fake).values()) == ["Failed"]


def test_terminal_phase_failed_on_an_unexpected_exception(fake: FakeApi, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> c.Outcome:
        raise RuntimeError("bug")

    monkeypatch.setattr(submitter, "mint_and_submit", boom)
    with pytest.raises(RuntimeError):
        run(fake, proposal_args(tmp_path))
    assert list(_reviewer_phases(fake).values()) == ["Failed"]


def test_terminal_phase_failed_even_when_the_engine_will_retry(fake: FakeApi, tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike the investigator, a non-final attempt still ends its execution:
    the retry opens a new request and attaches to a different item."""
    monkeypatch.setenv("MCTL_ENGINE_FINAL_ATTEMPT", "false")
    run(fake, proposal_args(tmp_path), Runner("not json"))
    assert list(_reviewer_phases(fake).values()) == ["Failed"]


def test_an_argo_retry_of_the_same_workflow_attaches_a_new_execution(fake: FakeApi, tmp_path: Path) -> None:
    """claude P2 on #990 feared a retry would meet its first attempt's
    execution (EXECUTION_EXISTING -> refused). Executions are keyed per work
    item and each run opens its own `review:<request_id>` item, so the retry
    attaches anew and can store; the first attempt's execution ended Failed."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = run(fake, proposal_args(tmp_path / "a"), Runner("not json"))
    assert first.code == c.CODE_VERDICT_MALFORMED
    second = run(fake, proposal_args(tmp_path / "b"))
    assert second.stored
    assert sorted(_reviewer_phases(fake).values()) == ["Failed", "Succeeded"]
    items = {ex["work_item_id"] for ex in fake.executions.values()}
    assert len(items) == 2
    assert {ex["engine_ref"] for ex in fake.executions.values()} == {f"{REVIEW_WORKFLOW}#review"}


def test_no_terminal_attach_for_an_execution_this_run_did_not_attach(fake: FakeApi, tmp_path: Path) -> None:
    fake.respond("POST", r"/executions$", 409, {"code": "execution_active"})
    outcome = run(fake, proposal_args(tmp_path))
    assert outcome.code == "execution_active"
    assert len([p for m, p, _a, _b in fake.calls if m == "POST" and p.endswith("/executions")]) == 1


def test_a_refused_terminal_attach_never_changes_the_outcome(fake: FakeApi, tmp_path: Path,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    calls = {"n": 0}
    real = fake._default

    def attach(request: httpx.Request, body: Any) -> httpx.Response:
        calls["n"] += 1
        if body["phase"] != "Running":
            return httpx.Response(503, json={"code": "work_items_unavailable"})
        return real(request, request.url.path, body, "service")

    fake.on("POST", r"/executions$", attach)
    assert run(fake, proposal_args(tmp_path)).stored
    assert calls["n"] == 2
    assert "could not advance the reviewer execution" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Session: a workable read-only reviewer (agy P2 on #990)
# ---------------------------------------------------------------------------
def test_session_runs_in_the_subject_copy_with_its_instructions(fake: FakeApi, tmp_path: Path) -> None:
    runner = Runner()
    assert run(fake, proposal_args(tmp_path), runner).stored
    options, prompt = runner.options[0], runner.prompts[0]
    cwd = Path(options.cwd)
    assert cwd.name == session.SUBJECT_DIRNAME
    assert options.setting_sources == []  # nothing from the untrusted subject is loaded as config
    assert options.tools == list(REVIEW_WORKER_TOOLS) == options.allowed_tools  # no Task, no sub-agent
    assert options.system_prompt and "independent reviewer" in options.system_prompt
    assert not options.system_prompt.startswith("---")  # frontmatter stripped
    assert "sub-agent" not in prompt and "current working directory" in prompt
    for name in ("requirements.md", "design.md", "tasks.md"):
        assert f"- {name}" in prompt


def test_reviewer_instructions_missing_stops_before_the_model(fake: FakeApi, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rrw, "REVIEW_WORKER_DIR", tmp_path / "nowhere")
    runner = Runner()
    outcome = run(fake, proposal_args(tmp_path), runner)
    assert outcome.code == session.CODE_INSTRUCTIONS_MISSING and runner.prompts == []
    assert fake.count("agent-run-tokens") == 0


def test_a_failing_sdk_session_is_a_typed_refusal(fake: FakeApi, tmp_path: Path) -> None:
    class Crashing(Runner):
        async def __call__(self, options: Any, prompt: str) -> str:
            raise ConnectionError("cli died")

    outcome = run(fake, proposal_args(tmp_path), Crashing())
    assert outcome.state == c.STATE_REFUSED and outcome.code == session.CODE_SESSION_FAILED
    assert fake.count("agent-run-tokens") == 0
    assert list(_reviewer_phases(fake).values()) == ["Failed"]


def test_sdk_runner_returns_the_result_and_traces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import claude_agent_sdk as sdk

    from orchestrator import tracing

    seen: list[str] = []

    class Trace:
        def observe(self, message: Any) -> None:
            seen.append(type(message).__name__)

    class Ctx:
        def __enter__(self) -> Trace:
            return Trace()

        def __exit__(self, *_exc: Any) -> None:
            return None

    agents: list[str] = []
    monkeypatch.setattr(tracing, "agent_run", lambda name, model: agents.append(name) or Ctx())
    assistant = sdk.AssistantMessage(content=[sdk.TextBlock(text="thinking")], model="m")
    result = sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                               session_id="s", result=json.dumps(PASS_VERDICT))

    async def query(*, prompt: str, options: Any) -> Any:
        assert prompt == "p" and options.model == "m"
        for message in (assistant, result):
            yield message

    monkeypatch.setattr(sdk, "query", query)
    options = build_review_worker_options(tmp_path, "m", subject_dir=tmp_path)
    assert json.loads(asyncio.run(rrw.sdk_runner(options, "p"))) == PASS_VERDICT
    assert agents == ["review-worker"] and seen == ["AssistantMessage", "ResultMessage"]

    async def text_only(*, prompt: str, options: Any) -> Any:
        yield assistant

    monkeypatch.setattr(sdk, "query", text_only)
    assert asyncio.run(rrw.sdk_runner(options, "p")) == "thinking"


def test_run_uses_the_sdk_runner_by_default(fake: FakeApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_sdk(options: Any, prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(PASS_VERDICT)

    monkeypatch.setattr(rrw, "sdk_runner", fake_sdk)
    assert rrw.run(proposal_args(tmp_path), api=make_api(fake)).stored
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Verdict parsing: one surrounding fence, nothing looser (claude P3 on #990)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```", "  ```json\n{}\n```\n"])
def test_parse_verdict_accepts_one_surrounding_fence(fence: str) -> None:
    outcome, criteria, findings = session.parse_verdict(fence.replace("{}", json.dumps(PASS_VERDICT)))
    assert outcome == "PASS" and findings == PASS_VERDICT["findings"] and len(criteria) == len(PASS_VERDICT["criteria"])


@pytest.mark.parametrize(
    "text",
    [
        "Here is my verdict:\n```json\n{}\n```",
        "```json\n{}\n```\nThanks.",
        "```json\n```json\n{}\n```\n```",
        "```json {} ```",
        "```json\n{\"outcome\": \"PASS\", \"criteria\": [], \"findings\": \"x\", \"extra\": 1}\n```",
    ],
)
def test_parse_verdict_rejects_anything_but_one_clean_fence(text: str) -> None:
    with pytest.raises(session.SessionError) as info:
        session.parse_verdict(text.replace("{}", json.dumps(PASS_VERDICT)))
    assert info.value.code == c.CODE_VERDICT_MALFORMED


def test_a_fenced_verdict_is_stored_through_the_run(fake: FakeApi, tmp_path: Path) -> None:
    assert run(fake, proposal_args(tmp_path), Runner(f"```json\n{json.dumps(PASS_VERDICT)}\n```")).stored


# ---------------------------------------------------------------------------
# Retry pause (claude P3 / agy P3 on #990)
# ---------------------------------------------------------------------------
def test_the_retry_pauses_by_default() -> None:
    import inspect
    import time

    assert inspect.signature(submitter.mint_and_submit).parameters["sleep"].default is time.sleep


def test_the_one_retry_pauses_once_before_it_fires(fake: FakeApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(submitter, "RETRY_DELAY_SECONDS", 2.0)
    fake.respond("POST", "^/api/v1/review-receipts$", 503, {"code": "review_store_unavailable"})
    request = _sealed_request()
    draft = rr.seal_receipt(request=request, outcome="UNOBSERVABLE", criteria=[])
    pauses: list[float] = []
    outcome = submitter.mint_and_submit(make_api(fake), "we_" + "1" * 32, request, draft, sleep=pauses.append)
    assert outcome.state == c.STATE_UNOBSERVED
    assert pauses == [2.0] and fake.count("/review-receipts") == 2


# ---------------------------------------------------------------------------
# Spike subject paths stay inside the report root (agy P3 on #990)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("report_path", ["/etc/passwd", "../outside.md", "measurements/../../outside.md", ""])
def test_a_report_path_outside_the_root_is_a_typed_refusal(fake: FakeApi, tmp_path: Path, report_path: str) -> None:
    (tmp_path / "outside.md").write_text("x")
    outcome = run(fake, spike_args(tmp_path, report_path=report_path), Runner(SPIKE_PASS_VERDICT))
    assert outcome.state == c.STATE_REFUSED and outcome.code == rr.CODE_BAD_SUBJECT
    assert fake.paths("POST") == []  # nothing was opened


def test_a_symlinked_measurements_dir_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "spike"
    root.mkdir()
    (root / "report.md").write_text("# r\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("s")
    (root / "measurements").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(session.SessionError) as info:
        session.read_spike_dir(root, "report.md")
    assert info.value.code == c.CODE_SUBJECT_MISMATCH
