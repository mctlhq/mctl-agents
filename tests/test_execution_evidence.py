"""Tests for orchestrator/execution_evidence.py — mctlhq/mctl-agents#199's
`ExecutionEvidence` contract (ADR 015:
docs/adr/015-execution-evidence-contract.md). T1-T8 below map onto that
proposal's tasks.md "## Tests" section.

Naming convention (`# T<n> —` section banners) matches
tests/test_execution_identity.py / tests/test_context_snapshot.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import execution_evidence as ee

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _actor(**overrides) -> ee.Actor:
    fields = {"type": "github_user", "id": "octocat", "verification": "control-plane-verified"}
    fields.update(overrides)
    return ee.Actor(**fields)


def _executor(**overrides) -> ee.Executor:
    fields = {
        "type": "implementer", "id": "", "agent": "implementer", "version": "1.0.0", "image_ref": "", "binding": "",
    }
    fields.update(overrides)
    return ee.Executor(**fields)


def _identity(**overrides) -> ee.IdentityBlock:
    fields = {
        "actor": _actor(),
        "executor": _executor(),
        "environment": "production",
        "tenant": "mctlhq",
        "repository": "mctlhq/mctl-agents",
        "target_repository_sha": "a" * 40,
        "context_trust": "control-plane",
    }
    fields.update(overrides)
    return ee.IdentityBlock(**fields)


def _execution(**overrides) -> ee.ExecutionBlock:
    fields = {
        "trace_id": "b" * 32,
        "context_id": "ex-deadbeefdeadbeef",
        "workflow_type": "implement",
        "temporal_workflow_id": "dev-loop-mctlhq-mctl-agents-199",
        "attempt": 1,
        "started_at": "2026-09-23T00:00:00Z",
        "completed_at": "2026-09-23T00:05:00Z",
        "duration_ms": 300000,
    }
    fields.update(overrides)
    return ee.ExecutionBlock(**fields)


def _retention(**overrides) -> ee.Retention:
    fields = {"class_": "gitops", "expires_after_days": 3650}
    fields.update(overrides)
    return ee.Retention(**fields)


def _outcome(**overrides) -> ee.Outcome:
    fields = {"status": "SUCCESS", "code": "merged"}
    fields.update(overrides)
    return ee.Outcome(**fields)


def _completeness(**overrides) -> ee.Completeness:
    fields = {"status": "COMPLETE", "gaps": ()}
    fields.update(overrides)
    return ee.Completeness(**fields)


def _action(**overrides) -> ee.ActionRecord:
    fields = {
        "sequence": 0,
        "action_kind": "github.pull_request.create",
        "operation": "create",
        "args_digest": "sha256:" + "1" * 64,
        "action_digest": "sha256:" + "2" * 64,
        "policy_version": "mctl-agents/policy/v1",
        "rule_id": "github-pr-create",
        "decision": "ALLOW",
        "code": "allowed",
        "permitted": True,
        "undecided": False,
        "mutation": True,
        "approval_ref": "",
        "at": "2026-09-23T00:01:00Z",
    }
    fields.update(overrides)
    return ee.ActionRecord(**fields)


def _artifact(**overrides) -> ee.ArtifactRecord:
    fields = {
        "name": "pull_request", "kind": "pull_request", "content_hash": "sha256:" + "3" * 64, "byte_count": 0,
        "locator": "mctlhq/mctl-agents#123",
    }
    fields.update(overrides)
    return ee.ArtifactRecord(**fields)


def _seal_minimal(**overrides) -> ee.ExecutionEvidence:
    fields = {
        "execution": _execution(),
        "identity": _identity(),
        "completeness": _completeness(),
        "outcome": _outcome(),
        "retention": _retention(),
        "created_at": "2026-09-23T00:05:01Z",
    }
    fields.update(overrides)
    return ee.seal(**fields)


# ---------------------------------------------------------------------------
# T1 — deterministic seal/hash/id
# ---------------------------------------------------------------------------


def test_seal_is_deterministic_across_created_at():
    record_a = _seal_minimal(created_at="2026-09-23T00:05:01Z")
    record_b = _seal_minimal(created_at="2099-01-01T00:00:00Z")
    assert record_a.content_hash == record_b.content_hash
    assert record_a.evidence_id == record_b.evidence_id


def test_evidence_id_is_derived_from_content_hash():
    record = _seal_minimal()
    assert record.evidence_id == "ev-" + record.content_hash[7:23]


def test_recompute_content_hash_agrees_with_seal():
    record = _seal_minimal()
    assert ee.recompute_content_hash(record) == record.content_hash


def test_seal_hash_changes_when_a_non_timestamp_field_changes():
    record_a = _seal_minimal()
    record_b = _seal_minimal(outcome=_outcome(status="FAILURE", code="review-stuck"))
    assert record_a.content_hash != record_b.content_hash


def test_is_trustworthy_true_for_a_freshly_sealed_record():
    assert ee.is_trustworthy(_seal_minimal())


def test_is_trustworthy_false_when_content_hash_is_tampered():
    import dataclasses

    record = _seal_minimal()
    tampered = dataclasses.replace(record, content_hash="sha256:" + "0" * 64)
    assert not ee.is_trustworthy(tampered)


def test_is_trustworthy_false_when_a_field_changed_but_hash_did_not():
    import dataclasses

    record = _seal_minimal()
    tampered = dataclasses.replace(record, outcome=_outcome(status="FAILURE", code="tampered"))
    assert not ee.is_trustworthy(tampered)


# ---------------------------------------------------------------------------
# T2 — from_dict rejects unknown keys / unsupported api_version
# ---------------------------------------------------------------------------


def test_from_dict_round_trips_a_sealed_record():
    record = _seal_minimal(actions=(_action(),), artifacts=(_artifact(),))
    restored = ee.ExecutionEvidence.from_dict(record.to_dict())
    assert restored == record


def test_from_dict_rejects_unsupported_api_version():
    data = _seal_minimal().to_dict()
    data["api_version"] = "evidence.mctl.ai/v2"
    with pytest.raises(ee.ExecutionEvidenceError):
        ee.ExecutionEvidence.from_dict(data)


def test_from_dict_rejects_unknown_top_level_key():
    data = _seal_minimal().to_dict()
    data["extra_payload_field"] = "should never be accepted"
    with pytest.raises(ee.ExecutionEvidenceError):
        ee.ExecutionEvidence.from_dict(data)


def test_from_dict_rejects_unknown_nested_key():
    data = _seal_minimal().to_dict()
    data["identity"]["actor"]["role"] = "admin"
    with pytest.raises(ee.ExecutionEvidenceError):
        ee.ExecutionEvidence.from_dict(data)


def test_from_dict_rejects_mismatched_kind():
    data = _seal_minimal().to_dict()
    data["kind"] = "SomethingElse"
    with pytest.raises(ee.ExecutionEvidenceError):
        ee.ExecutionEvidence.from_dict(data)


def test_validate_rejects_both_target_ref_and_target_digest():
    action = _action(target_ref="mctlhq/mctl-agents", target_digest="sha256:" + "4" * 64)
    with pytest.raises(ee.ExecutionEvidenceError):
        _seal_minimal(actions=(action,))


def test_validate_rejects_unknown_outcome_status():
    with pytest.raises(ee.ExecutionEvidenceError):
        _seal_minimal(outcome=ee.Outcome(status="MAYBE"))


def test_validate_rejects_completeness_status_gaps_mismatch():
    with pytest.raises(ee.ExecutionEvidenceError):
        _seal_minimal(completeness=ee.Completeness(status="COMPLETE", gaps=(ee.Gap(code=ee.GAP_IDENTITY_UNAVAILABLE),)))


# ---------------------------------------------------------------------------
# T3 — redaction: no secret substring anywhere in the sealed dict
# ---------------------------------------------------------------------------

_GHP_TOKEN = "ghp_" + "a" * 40
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PYVQ6MyVFTFI"
_OVERSIZED_STRING = "x" * 4096
_LONG_ARGV = "--body " + ("y" * 512)


def _walk_strings(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_walk_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_walk_strings(v))
    return out


def test_redaction_drops_credential_and_oversized_strings_from_recorder_offers():
    recorder = ee.EvidenceRecorder()
    recorder.offer_decision(
        action_kind="github.pull_request.create",
        operation="create",
        target=_GHP_TOKEN,
        args_digest="sha256:" + "1" * 64,
        action_digest="sha256:" + "2" * 64,
        policy_version="mctl-agents/policy/v1",
        rule_id="github-pr-create",
        decision="ALLOW",
        code="allowed",
        permitted=True,
        approval_ref=_JWT,
    )
    recorder.offer_artifact(
        name=_OVERSIZED_STRING, kind="pull_request", content_hash="sha256:" + "3" * 64, locator=_LONG_ARGV,
    )
    recorder.offer_approval(receipt_id="aar_1", approver=_GHP_TOKEN, state=_JWT)
    record = recorder.finish(
        execution=_execution(), identity=_identity(), retention=_retention(), outcome=_outcome(),
        created_at="2026-09-23T00:05:01Z",
    )
    assert record is not None
    serialized = record.to_dict()
    for secret in (_GHP_TOKEN, _JWT, _OVERSIZED_STRING, _LONG_ARGV):
        for value in _walk_strings(serialized):
            assert secret not in value, f"secret {secret[:12]!r}... leaked into field value {value[:40]!r}..."


def test_target_outside_allowlist_is_reduced_to_a_digest():
    ref, digest = ee._split_target("https://example.com/some/other/path")
    assert ref is None
    assert digest is not None and digest.startswith("sha256:")


@pytest.mark.parametrize("target", [
    "https://github.com/mctlhq/mctl-agents/pull/123",
    "https://github.com/mctlhq/mctl-agents/issues/45",
    "mctlhq/mctl-agents",
    "mctlhq/mctl-agents:feat/agents-issue-199",
    "aar_abc123",
    "we_abc123",
    "ex-abc123",
    "cs-abc123",
    "execute:mctl-agents-investigate",
])
def test_target_inside_allowlist_is_kept_as_a_ref(target):
    ref, digest = ee._split_target(target)
    assert ref == target
    assert digest is None


def test_credential_shaped_target_is_never_kept_as_a_ref_even_if_it_matched():
    # A credential-shaped string that happens to fit the owner/repo shape's
    # character class is defensively still rejected as a ref: _split_target
    # rechecks the credential pattern regardless of allowlist shape match.
    ref, digest = ee._split_target("ghp_" + "a" * 40)
    assert ref is None
    assert digest is not None


# ---------------------------------------------------------------------------
# T4 — completeness: a mutation artifact with no matching decision
# ---------------------------------------------------------------------------


def test_mutation_artifact_without_permitted_action_is_incomplete():
    completeness = ee.check_completeness(actions=(), artifacts=(_artifact(),), identity=_identity())
    assert completeness.status == "INCOMPLETE"
    assert any(g.code == ee.GAP_MUTATION_WITHOUT_DECISION for g in completeness.gaps)


def test_policy_compliance_fails_when_completeness_is_incomplete():
    completeness = ee.check_completeness(actions=(), artifacts=(_artifact(),), identity=_identity())
    policy_eval, _ = ee.built_in_evaluations(actions=(), completeness=completeness)
    assert policy_eval.name == ee.EVAL_POLICY_COMPLIANCE
    assert policy_eval.result == "FAIL"


def test_mutation_artifact_with_matching_permitted_action_is_complete():
    completeness = ee.check_completeness(actions=(_action(),), artifacts=(_artifact(),), identity=_identity())
    assert completeness.status == "COMPLETE"
    policy_eval, completeness_eval = ee.built_in_evaluations(actions=(_action(),), completeness=completeness)
    assert policy_eval.result == "PASS"
    assert completeness_eval.result == "PASS"


def test_unpermitted_mutation_action_fails_policy_compliance_even_if_complete():
    denied_mutation = _action(code="denied_by_rule", decision="DENY", permitted=False, mutation=True)
    completeness = ee.check_completeness(actions=(denied_mutation,), artifacts=(), identity=_identity())
    policy_eval, _ = ee.built_in_evaluations(actions=(denied_mutation,), completeness=completeness)
    assert policy_eval.result == "FAIL"


# ---------------------------------------------------------------------------
# T5 — approvals: resolved receipt links; unresolved yields a gap and still seals
# ---------------------------------------------------------------------------


def test_resolved_approval_produces_no_gap():
    approved_action = _action(code="approved", decision="REQUIRE_APPROVAL", approval_ref="aar_1", mutation=True)
    approval = ee.ApprovalRecordRef(
        receipt_id="aar_1", intent_hash="sha256:" + "5" * 64, state="approved", approver="human:octocat",
        requested_at="2026-09-23T00:00:00Z", decided_at="2026-09-23T00:00:30Z", consumed_at="2026-09-23T00:01:00Z",
    )
    completeness = ee.check_completeness(
        actions=(approved_action,), approvals=(approval,), identity=_identity(),
    )
    assert completeness.status == "COMPLETE"
    record = _seal_minimal(actions=(approved_action,), approvals=(approval,), completeness=completeness)
    assert record.approvals[0].receipt_id == "aar_1"
    assert record.approvals[0].consumed_at == "2026-09-23T00:01:00Z"


def test_unresolvable_approval_yields_gap_and_still_seals():
    approved_action = _action(code="approved", decision="REQUIRE_APPROVAL", approval_ref="aar_missing", mutation=True)
    completeness = ee.check_completeness(actions=(approved_action,), approvals=(), identity=_identity())
    assert completeness.status == "INCOMPLETE"
    assert any(g.code == ee.GAP_APPROVAL_UNRESOLVED for g in completeness.gaps)
    # Must still seal — an incomplete record is still a valid, sealed document.
    record = _seal_minimal(actions=(approved_action,), completeness=completeness)
    assert record.evidence_id.startswith("ev-")


def test_approved_but_not_consumed_yields_decision_without_outcome_gap():
    approved_action = _action(code="approved", decision="REQUIRE_APPROVAL", approval_ref="aar_2", mutation=True)
    approval = ee.ApprovalRecordRef(receipt_id="aar_2", state="approved", consumed_at="")
    completeness = ee.check_completeness(actions=(approved_action,), approvals=(approval,), identity=_identity())
    assert any(g.code == ee.GAP_DECISION_WITHOUT_OUTCOME for g in completeness.gaps)


# ---------------------------------------------------------------------------
# T6 — undecided codes never force REFUSED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(ee.UNDECIDED_CODES))
def test_undecided_action_is_recorded_as_undecided(code):
    recorder = ee.EvidenceRecorder()
    recorder.offer_decision(
        action_kind="mcp.tool.call", operation="mcp__mctl__do_something", code=code, decision="DENY",
        permitted=False, undecided=True,
    )
    record = recorder.finish(
        execution=_execution(), identity=_identity(), retention=_retention(),
        outcome=_outcome(status="UNDECIDED", code=code), created_at="2026-09-23T00:05:01Z",
    )
    assert record is not None
    assert record.actions[0].undecided is True
    assert record.outcome.status != "REFUSED"


def test_an_undecided_action_alone_does_not_imply_refused_outcome():
    # The recorder never sets outcome itself (the caller does), but this
    # test pins the contract: nothing in this module ever derives REFUSED
    # from an undecided action.
    for code in sorted(ee.UNDECIDED_CODES):
        action = _action(code=code, decision="DENY", permitted=False, undecided=True, mutation=False)
        record = _seal_minimal(actions=(action,), outcome=_outcome(status="SUCCESS", code="ok"))
        assert record.outcome.status == "SUCCESS"


# ---------------------------------------------------------------------------
# T7 — ungoverned_transport is a standing gap; COMPLETE is unreachable
# ---------------------------------------------------------------------------


def test_ungoverned_transport_standing_gap_makes_complete_unreachable():
    standing = (ee.standing_gap(ee.GAP_UNGOVERNED_TRANSPORT),)
    completeness = ee.check_completeness(
        actions=(_action(),), artifacts=(), identity=_identity(), standing_gaps=standing,
    )
    assert completeness.status == "INCOMPLETE"
    assert any(g.code == ee.GAP_UNGOVERNED_TRANSPORT for g in completeness.gaps)


# ---------------------------------------------------------------------------
# T8 — failure isolation: every EvidenceRecorder method is total
# ---------------------------------------------------------------------------


def test_offer_decision_never_raises_on_garbage_input():
    recorder = ee.EvidenceRecorder()
    recorder.offer_decision(action_kind=object(), operation=None, target=12345, permitted="not-a-bool")  # type: ignore[arg-type]


def test_offer_artifact_never_raises_on_garbage_input():
    recorder = ee.EvidenceRecorder()
    recorder.offer_artifact(name=object(), kind=None, byte_count="not-an-int")  # type: ignore[arg-type]


def test_offer_model_turn_never_raises_on_garbage_input():
    recorder = ee.EvidenceRecorder()
    recorder.offer_model_turn(provider=None, model=object(), input_tokens="lots")  # type: ignore[arg-type]


def test_offer_approval_never_raises_on_garbage_input():
    recorder = ee.EvidenceRecorder()
    recorder.offer_approval(receipt_id=None, state=object())  # type: ignore[arg-type]


def test_finish_never_raises_and_returns_none_on_broken_inputs():
    recorder = ee.EvidenceRecorder()
    recorder.offer_decision(action_kind="github.pull_request.create", operation="create", permitted=True)
    result = recorder.finish(
        execution="not-an-execution-block",  # type: ignore[arg-type]
        identity="not-an-identity-block",  # type: ignore[arg-type]
        retention="not-a-retention",  # type: ignore[arg-type]
        outcome="not-an-outcome",  # type: ignore[arg-type]
        created_at="2026-09-23T00:05:01Z",
    )
    assert result is None


def test_unset_recorder_offers_are_pure_no_ops():
    assert ee.get_recorder() is None
    # No sink installed: this must not raise, and there is nothing to assert
    # about state because there is no recorder to hold it.
    recorder = ee.get_recorder()
    if recorder is not None:  # pragma: no cover - defensive, sink is unset
        recorder.offer_decision(action_kind="x", operation="y")


def test_set_and_get_recorder_round_trips():
    recorder = ee.EvidenceRecorder()
    try:
        ee.set_recorder(recorder)
        assert ee.get_recorder() is recorder
    finally:
        ee.set_recorder(None)
    assert ee.get_recorder() is None


# ---------------------------------------------------------------------------
# Import isolation: stdlib-only, so the worker and the agent sandbox can
# both load this module without pulling in claude_agent_sdk / temporalio /
# httpx / the OTel SDK (matches tests/test_execution_identity.py's pattern).
# ---------------------------------------------------------------------------


def test_module_import_is_stdlib_only():
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.execution_evidence, sys; "
            "print(chr(10).join(sorted(sys.modules)))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr[-2000:]}"
    loaded = set(result.stdout.split("\n"))
    third_party_prefixes = (
        "claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio", "mcp",
        "opentelemetry",
    )
    leaked = sorted(
        name for name in loaded
        if any(name == prefix or name.startswith(prefix + ".") for prefix in third_party_prefixes)
    )
    assert not leaked, f"orchestrator.execution_evidence pulled in third-party modules: {leaked}"


def test_module_does_not_import_policy_checkpoint_or_execution_identity_or_tracing():
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.execution_evidence, sys; "
            "print(chr(10).join(sorted(sys.modules)))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr[-2000:]}"
    loaded = set(result.stdout.split("\n"))
    forbidden = (
        "orchestrator.policy_checkpoint", "orchestrator.execution_identity", "orchestrator.tracing",
        "orchestrator.tracing_sdk",
    )
    leaked = sorted(name for name in loaded if name in forbidden)
    assert not leaked, f"orchestrator.execution_evidence pulled in {leaked} at import time"
