"""The implementer executes only on a verified mctl-api approval grant.

`.status.yaml` is a claim about an approval; the decision is the DevLoop's
`aar_` grant in mctl-api (`orchestrator.approval_evidence`). These tests
assert on the REFUSALS: a record that looks approved in every field the file
can carry, but whose grant is missing, foreign, undecided by a human, bound
to other content or not spent, must not reach the model or `gh pr create`.
The accepted cases are guards against the gate being too strict.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator import action_approvals as aa
from orchestrator import approval_evidence as ae
from orchestrator import policy_checkpoint, run_implementer
from orchestrator.proposal_state import load_status, proposal_content_digest

# Taken at import time, before conftest's autouse stub replaces them.
REAL_VERIFY = run_implementer._verify_grant
REAL_EXECUTE = run_implementer._execute_grant

SERVICE = "mctl-web"
SLUG = "issue-7-example"
TARGET = f"{SERVICE}/{SLUG}"
GRANT = "aar_0123456789abcdef"


def _proposal(
    tmp_path: Path, *, approval: dict | None = None, control: dict | None = None,
) -> run_implementer.ProposalRef:
    d = tmp_path / SERVICE / "proposals" / SLUG
    d.mkdir(parents=True)
    (d / "requirements.md").write_text("# requirements\n", encoding="utf-8")
    (d / "design.md").write_text("# design\n", encoding="utf-8")
    (d / "tasks.md").write_text("- [ ] one\n", encoding="utf-8")
    digest = proposal_content_digest(d)
    status: dict = {"status": "accepted"}
    if control is not None:
        status["control"] = control
    if approval is not None:
        status["approval"] = {"content_digest": digest, **approval}
    (d / ".status.yaml").write_text(yaml.safe_dump(status), encoding="utf-8")
    return run_implementer.ProposalRef(
        service=SERVICE, slug=SLUG, proposal_dir=d, status="accepted", approval_ok=True,
    )


def _record(**over) -> aa.ApprovalRecord:
    fields = {
        "id": GRANT,
        "state": "consumed",
        "intent_hash": "sha256:" + "1" * 64,
        "expires_at": "2026-10-20T00:00:00Z",
        "decided_by": "github:mashkovd",
        "action_kind": ae.APPROVE_ACTION_KIND,
        "target": TARGET,
        "artifact_hash": "",
    }
    fields.update(over)
    return aa.ApprovalRecord(**fields)


def _answer(rec: aa.ApprovalRecord) -> aa.ApprovalAnswer:
    return aa.ApprovalAnswer(aa._STATES[rec.state], record=rec)


class _Stops(Exception):
    """Raised where the run would go on to GitHub or the model."""


@pytest.fixture
def gate(monkeypatch):
    """The real gate over a fake store; preflight and the model are tripwires."""
    calls: dict[str, list] = {"lookup": [], "execute": [], "preflight": [], "model": []}
    store: dict[str, aa.ApprovalAnswer] = {}

    def lookup(grant_id):
        calls["lookup"].append(grant_id)
        return store.get(grant_id, aa.ApprovalAnswer(aa.NOT_FOUND, code="approval_not_found"))

    def preflight(ref, **_kw):
        calls["preflight"].append(ref.slug)
        return gate.existing

    def model(*_a, **_kw):
        calls["model"].append(True)
        raise _Stops("model")

    monkeypatch.setattr(run_implementer, "_verify_grant", REAL_VERIFY)
    monkeypatch.setattr(run_implementer, "_lookup_grant", lookup)
    monkeypatch.setattr(run_implementer, "_preflight_existing_result", preflight)
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", model)
    monkeypatch.setattr(run_implementer, "read_source_issue",
                        lambda *_a, **_kw: run_implementer.SourceIssueVerdict(linked=False, known=True, failure=None))
    monkeypatch.setattr(run_implementer, "_skip_while_rate_limited", lambda _ref: None)
    gate.calls = calls
    gate.store = store
    gate.existing = run_implementer.ExistingResult(action="none")
    return gate


def _grant_for(ref: run_implementer.ProposalRef, **over) -> aa.ApprovalAnswer:
    over.setdefault("artifact_hash", proposal_content_digest(ref.proposal_dir))
    return _answer(_record(**over))


def _run(ref):
    try:
        return run_implementer._implement_one(ref)
    except (_Stops, SystemExit):  # the auth tripwire surfaces as SystemExit
        return None


APPROVED = {"approved_by": "mashkovd", "approved_at": "2026-10-19T08:00:00Z", "approval_ref": GRANT}


# --- refusals through _implement_one ----------------------------------------

@pytest.mark.parametrize("control", [None, {"requires_human_approval": True}], ids=["no-control", "requires"])
def test_a_record_without_a_grant_ref_never_runs(tmp_path, gate, control) -> None:
    ref = _proposal(tmp_path, control=control,
                    approval={"approved_by": "mashkovd", "approved_at": "2026-10-19T08:00:00Z"})
    result = _run(ref)
    assert result is not None and result.blocked == ae.GRANT_MISSING
    assert gate.calls["preflight"] == [] and gate.calls["model"] == []
    assert load_status(ref.status_path)["blocked"]["code"] == ae.GRANT_MISSING


def test_a_record_with_no_approval_block_never_runs(tmp_path, gate) -> None:
    ref = _proposal(tmp_path)
    result = _run(ref)
    assert result is not None and result.blocked == ae.GRANT_MISSING
    assert gate.calls["preflight"] == [] and gate.calls["model"] == []


def test_a_grant_ref_the_store_does_not_hold_never_runs(tmp_path, gate) -> None:
    ref = _proposal(tmp_path, control={"requires_human_approval": True}, approval=APPROVED)
    result = _run(ref)
    assert gate.calls["lookup"] == [GRANT]
    assert result is not None and result.blocked == ae.GRANT_NOT_FOUND
    assert gate.calls["preflight"] == [] and gate.calls["model"] == []


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"action_kind": "github-pr-create:create"}, ae.GRANT_WRONG_KIND),
        ({"decided_by": ""}, ae.GRANT_NOT_HUMAN),
        ({"decided_by": "service:mctl-agents"}, ae.GRANT_NOT_HUMAN),
        ({"decided_by": "github:"}, ae.GRANT_NOT_HUMAN),
        ({"target": "mctl-web/issue-8-other"}, ae.GRANT_WRONG_TARGET),
        ({"target": "mctl-api/" + SLUG}, ae.GRANT_WRONG_TARGET),
        ({"artifact_hash": "sha256:" + "2" * 64}, ae.GRANT_CONTENT_MISMATCH),
        ({"artifact_hash": ""}, ae.GRANT_CONTENT_MISMATCH),
        ({"state": "approved"}, ae.GRANT_NOT_SPENT),
        ({"state": "pending", "decided_by": ""}, ae.GRANT_NOT_HUMAN),
        ({"state": "denied"}, ae.GRANT_NOT_SPENT),
    ],
    ids=["kind", "no-decider", "service-decider", "empty-login", "other-slug", "other-service",
         "other-content", "no-content", "unspent", "pending", "denied"],
)
def test_a_grant_that_does_not_bind_this_execution_never_runs(tmp_path, gate, over, code) -> None:
    ref = _proposal(tmp_path, control={"requires_human_approval": True}, approval=APPROVED)
    gate.store[GRANT] = _grant_for(ref, **over)
    result = _run(ref)
    assert result is not None and result.blocked == code, result
    assert gate.calls["preflight"] == [] and gate.calls["model"] == []


def test_content_edited_after_the_decision_never_runs(tmp_path, gate) -> None:
    ref = _proposal(tmp_path, control={"requires_human_approval": True}, approval=APPROVED)
    gate.store[GRANT] = _grant_for(ref)
    # Rewrite the files and the recorded digest together: the file then agrees
    # with itself, and only the grant still names what the human decided.
    (ref.proposal_dir / "tasks.md").write_text("- [ ] something else\n", encoding="utf-8")
    status = load_status(ref.status_path)
    status["approval"]["content_digest"] = proposal_content_digest(ref.proposal_dir)
    ref.status_path.write_text(yaml.safe_dump(status), encoding="utf-8")
    result = _run(ref)
    assert result is not None and result.blocked == ae.GRANT_CONTENT_MISMATCH
    assert gate.calls["preflight"] == [] and gate.calls["model"] == []


def test_an_unreadable_store_defers_without_a_marker(tmp_path, gate) -> None:
    ref = _proposal(tmp_path, control={"requires_human_approval": True}, approval=APPROVED)
    gate.store[GRANT] = aa.ApprovalAnswer(aa.UNKNOWN, reason="HTTP 503")
    result = _run(ref)
    assert result is not None and result.blocked is None
    assert "deferring" in (result.skipped_reason or "")
    assert result.counts_toward_limit is False
    assert "blocked" not in load_status(ref.status_path)
    assert gate.calls["preflight"] == [] and gate.calls["model"] == []


def test_an_executed_grant_with_nothing_to_finish_never_runs_the_model(tmp_path, gate) -> None:
    ref = _proposal(tmp_path, control={"requires_human_approval": True}, approval=APPROVED)
    gate.store[GRANT] = _grant_for(ref, state="executed")
    result = _run(ref)
    assert gate.calls["preflight"] == [SLUG]
    assert result is not None and result.blocked == ae.GRANT_ALREADY_EXECUTED
    assert gate.calls["model"] == []


# --- guards against a gate that is too strict --------------------------------

@pytest.mark.parametrize("control", [None, {"requires_human_approval": True}], ids=["no-control", "requires"])
def test_a_spent_human_grant_for_this_content_proceeds(tmp_path, gate, control) -> None:
    ref = _proposal(tmp_path, control=control, approval=APPROVED)
    gate.store[GRANT] = _grant_for(ref)
    assert _run(ref) is None  # reached the model tripwire
    assert gate.calls["preflight"] == [SLUG] and gate.calls["model"] == [True]
    assert ref.grant_id == GRANT and ref.grant_allows_new_run is True
    assert ref.grant_digest == proposal_content_digest(ref.proposal_dir)


def test_an_executed_grant_may_finish_an_open_pr(tmp_path, gate) -> None:
    ref = _proposal(tmp_path, control={"requires_human_approval": True}, approval=APPROVED)
    gate.store[GRANT] = _grant_for(ref, state="executed")
    gate.existing = run_implementer.ExistingResult(action="open", pr_url="https://github.com/mctlhq/mctl-web/pull/9")
    result = _run(ref)
    assert result is not None and result.pr_url == "https://github.com/mctlhq/mctl-web/pull/9"
    assert load_status(ref.status_path)["status"] == "implemented"
    assert gate.calls["model"] == []


# --- the execution mark before gh pr create ----------------------------------

@pytest.fixture
def pr_create(monkeypatch):
    sent: dict[str, list] = {"execute": [], "gh": []}

    def fake_run(cmd, **_kw):
        sent["gh"].append(cmd)
        return type("P", (), {"stdout": "https://github.com/mctlhq/mctl-web/pull/9\n", "returncode": 0})()

    monkeypatch.setattr(run_implementer, "_run", fake_run)
    monkeypatch.setattr(run_implementer, "_pr_title_and_body", lambda _ref: ("t", "b"))
    monkeypatch.setattr(policy_checkpoint, "require", lambda _d: None)
    monkeypatch.setattr(policy_checkpoint, "checkpoint", lambda *_a, **_kw: None)
    return sent


def _verified_ref() -> run_implementer.ProposalRef:
    return run_implementer.ProposalRef(
        service=SERVICE, slug=SLUG, proposal_dir=Path("/nonexistent"), status="accepted",
        approval_ok=True, grant_id=GRANT, grant_digest="sha256:" + "3" * 64,
    )


@pytest.mark.parametrize(
    "answer",
    [
        aa.ApprovalAnswer(aa.UNKNOWN, reason="timeout"),
        aa.ApprovalAnswer(aa.NOT_CONSUMED, code="approval_not_consumed"),
        aa.ApprovalAnswer(aa.MISMATCH, code="approval_target_mismatch"),
        aa.ApprovalAnswer(aa.DENIED, code="approval_denied"),
        aa.ApprovalAnswer(aa.NOT_FOUND, code="approval_not_found"),
    ],
    ids=["unknown", "not-consumed", "target-mismatch", "denied", "not-found"],
)
def test_no_pr_is_opened_unless_the_grant_is_marked(monkeypatch, pr_create, answer) -> None:
    def execute(grant_id, target, artifact_hash):
        pr_create["execute"].append((grant_id, target, artifact_hash))
        return answer

    monkeypatch.setattr(run_implementer, "_execute_grant", execute)
    with pytest.raises(policy_checkpoint.PolicyRefused) as exc:
        run_implementer._open_pr_for_branch(_verified_ref(), "feat/agents-" + SLUG)
    assert exc.value.decision.undecided  # handed back, never recorded as this proposal's failure
    assert pr_create["execute"] == [(GRANT, TARGET, "sha256:" + "3" * 64)]
    assert pr_create["gh"] == []


def test_no_pr_is_opened_without_a_verified_grant(monkeypatch, pr_create) -> None:
    monkeypatch.setattr(run_implementer, "_execute_grant", REAL_EXECUTE)
    ref = run_implementer.ProposalRef(service=SERVICE, slug=SLUG, proposal_dir=Path("/x"), status="accepted")
    with pytest.raises(policy_checkpoint.PolicyRefused):
        run_implementer._open_pr_for_branch(ref, "feat/agents-" + SLUG)
    assert pr_create["gh"] == []


@pytest.mark.parametrize("status", [aa.EXECUTED_NOW, aa.EXECUTED], ids=["marked-now", "already-marked"])
def test_a_marked_grant_opens_the_pr(monkeypatch, pr_create, status) -> None:
    monkeypatch.setattr(run_implementer, "_execute_grant", lambda *_a: aa.ApprovalAnswer(status))
    url = run_implementer._open_pr_for_branch(_verified_ref(), "feat/agents-" + SLUG)
    assert url == "https://github.com/mctlhq/mctl-web/pull/9"
    assert pr_create["gh"] and pr_create["gh"][0][:3] == ["gh", "pr", "create"]


# --- the verifier's own edges ------------------------------------------------

def test_a_store_answering_for_another_id_is_not_an_answer() -> None:
    rec = _record(id="aar_somethingelse", artifact_hash="sha256:" + "4" * 64)
    verdict = ae.verify_execution_grant(
        {"approval": {"approval_ref": GRANT}}, service=SERVICE, slug=SLUG,
        current_digest="sha256:" + "4" * 64, lookup=lambda _id: _answer(rec),
    )
    assert verdict.transient and not verdict.ok


@pytest.mark.parametrize("ref", ["aar_", "aar_x y", "nope", "aar_" + "x" * 125])
def test_a_malformed_grant_ref_is_refused_without_a_lookup(ref) -> None:
    def lookup(_id):
        raise AssertionError("looked up a malformed ref")

    verdict = ae.verify_execution_grant(
        {"approval": {"approval_ref": ref}}, service=SERVICE, slug=SLUG,
        current_digest="sha256:" + "4" * 64, lookup=lookup,
    )
    assert verdict.code == ae.GRANT_INVALID


# --- the client's execute route ----------------------------------------------

def _client(monkeypatch, status, body):
    client = aa.ActionApprovalClient(base_url="https://api.example", token="t")  # noqa: S106 — a fake bearer for a stubbed transport
    sent = []

    def request(method, path, payload=None):
        sent.append((method, path, payload))
        return status, body

    monkeypatch.setattr(client, "_request", request)
    return client, sent


def _body(**over):
    approval = {
        "id": GRANT, "state": "executed", "intent_hash": "sha256:" + "1" * 64,
        "expires_at": "2026-10-20T00:00:00Z", "decided_by": "github:mashkovd",
        "action_kind": ae.APPROVE_ACTION_KIND, "target": TARGET, "artifact_hash": "sha256:" + "5" * 64,
    }
    approval.update(over)
    return {"schema_version": aa.SCHEMA_VERSION, "approval": approval}


def test_execute_marks_exactly_this_grant(monkeypatch) -> None:
    client, sent = _client(monkeypatch, 200, _body())
    answer = client.execute(GRANT, TARGET, "sha256:" + "5" * 64)
    assert answer.status == aa.EXECUTED_NOW
    assert sent == [("POST", f"/api/v1/action-approvals/{GRANT}/execute",
                     {"target": TARGET, "artifact_hash": "sha256:" + "5" * 64})]
    assert answer.record.target == TARGET and answer.record.action_kind == ae.APPROVE_ACTION_KIND


@pytest.mark.parametrize(
    "over",
    [{"state": "consumed"}, {"target": "mctl-web/other"}, {"artifact_hash": "sha256:" + "6" * 64},
     {"id": "aar_other"}],
    ids=["not-executed", "other-target", "other-content", "other-id"],
)
def test_an_execute_answer_for_something_else_is_unknown(monkeypatch, over) -> None:
    client, _ = _client(monkeypatch, 200, _body(**over))
    assert client.execute(GRANT, TARGET, "sha256:" + "5" * 64).status == aa.UNKNOWN


@pytest.mark.parametrize(
    ("status", "code", "want"),
    [
        (409, "approval_executed", aa.EXECUTED),
        (409, "approval_not_consumed", aa.NOT_CONSUMED),
        (409, "approval_target_mismatch", aa.MISMATCH),
        (404, "approval_not_found", aa.NOT_FOUND),
        (503, "", aa.UNKNOWN),
    ],
)
def test_execute_refusals_keep_their_meaning(monkeypatch, status, code, want) -> None:
    client, _ = _client(monkeypatch, status, {"code": code})
    assert client.execute(GRANT, TARGET, "sha256:" + "5" * 64).status == want


def test_an_executed_grant_is_read_back_as_executed(monkeypatch) -> None:
    client, _ = _client(monkeypatch, 200, _body())
    answer = client.get(GRANT)
    assert answer.status == aa.EXECUTED and answer.record.artifact_hash == "sha256:" + "5" * 64
