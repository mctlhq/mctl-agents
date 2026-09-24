"""Tests for orchestrator/approval_ticket.py and its `.status.yaml` block in
orchestrator/proposal_state.py (mctlhq/mctl-agents#198).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import policy_checkpoint as pc
from orchestrator import proposal_state
from orchestrator.action_approvals import ApprovalRecord
from orchestrator.approval_ticket import from_json, ticket_from

REQUEST = pc.ActionRequest(
    action_kind=pc.GITHUB_PR_MERGE,
    operation="merge",
    target="https://github.com/mctlhq/mctl-web/pull/42",
    args_digest="sha256:" + "a" * 64,
    execution_id="ex-1",
    trace_id="tr-1",
    actor="human:alice",
)
DECISION = pc.Decision(
    verdict=pc.REQUIRE_APPROVAL,
    code=pc.CODE_APPROVAL_PENDING,
    reason="rule github-pr-merge: needs an approval bound to sha256:deadbeef",
    policy_version="mctl-agents/policy/v1",
    rule_id="github-pr-merge",
    action_digest="sha256:" + "b" * 64,
    approval_ref="aar_1",
)
RECORD = ApprovalRecord(
    id="aar_1", state="pending", intent_hash="sha256:" + "c" * 64, expires_at="2026-09-24T00:00:00Z",
)


def test_ticket_from_carries_no_raw_arguments():
    ticket = ticket_from(DECISION, REQUEST, RECORD, artifact_ref="deadsha")
    payload = ticket.to_json()
    assert payload["approval_ref"] == "aar_1"
    assert payload["intent_hash"] == RECORD.intent_hash
    assert payload["expires_at"] == RECORD.expires_at
    assert payload["action_kind"] == pc.GITHUB_PR_MERGE
    assert payload["operation"] == "merge"
    assert payload["target"] == REQUEST.target
    assert payload["policy_rule_id"] == "github-pr-merge"
    assert payload["trace_id"] == "tr-1"
    assert payload["execution_id"] == "ex-1"
    assert payload["actor"] == "human:alice"
    assert payload["artifact_ref"] == "deadsha"
    # No raw arguments anywhere in the serialised shape.
    assert "args" not in payload and "arguments" not in payload and "payload" not in payload


def test_ticket_from_requires_an_awaiting_approval_decision():
    allowed = pc.Decision(pc.ALLOW, pc.CODE_ALLOWED, "ok", "v1", "r", "sha256:" + "0" * 64)
    with pytest.raises(ValueError):
        ticket_from(allowed, REQUEST, RECORD)


def test_ticket_from_requires_the_record_to_match_the_decision_ref():
    other = ApprovalRecord(id="aar_2", state="pending", intent_hash=RECORD.intent_hash, expires_at=RECORD.expires_at)
    with pytest.raises(ValueError):
        ticket_from(DECISION, REQUEST, other)


def test_to_json_round_trips_through_from_json():
    ticket = ticket_from(DECISION, REQUEST, RECORD, artifact_ref="deadsha")
    restored = from_json(ticket.to_json())
    assert restored == ticket


@pytest.mark.parametrize(
    "bad",
    [None, {}, "nope", 42, {"schema_version": "other/v1"}, {"schema_version": "mctl-agents/approval-ticket/v1"}],
)
def test_from_json_tolerates_absent_foreign_and_malformed(bad):
    assert from_json(bad) is None


def test_module_import_is_stdlib_only():
    result = subprocess.run(
        [sys.executable, "-c", "import orchestrator.approval_ticket, sys; print(chr(10).join(sorted(sys.modules)))"],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    third_party = ("claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio")
    leaked = sorted(n for n in result.stdout.split("\n") if n.split(".")[0] in third_party)
    assert not leaked, leaked


# ---------------------------------------------------------------------------
# The `.status.yaml` `approval` block (orchestrator/proposal_state.py)
# ---------------------------------------------------------------------------


def test_read_approval_is_none_zero_zero_when_absent():
    assert proposal_state.read_approval({}) == (None, 0, 0)
    assert proposal_state.read_approval({"approval": "not-a-mapping"}) == (None, 0, 0)


def test_approval_payload_round_trips_the_ticket_and_counters():
    ticket = ticket_from(DECISION, REQUEST, RECORD, artifact_ref="deadsha")
    payload = proposal_state.approval_payload(ticket, denials=2, attempt=1)
    restored_ticket, denials, attempt = proposal_state.read_approval({"approval": payload})
    assert restored_ticket == ticket
    assert (denials, attempt) == (2, 1)


def test_approval_payload_can_clear_the_ticket_while_keeping_counters():
    payload = proposal_state.approval_payload(None, denials=1, attempt=3)
    ticket, denials, attempt = proposal_state.read_approval({"approval": payload})
    assert ticket is None
    assert (denials, attempt) == (1, 3)


def test_read_approval_ignores_negative_or_non_integer_counters():
    ticket, denials, attempt = proposal_state.read_approval(
        {"approval": {"ticket": None, "denials": -1, "attempt": "nope"}}
    )
    assert ticket is None and denials == 0 and attempt == 0
