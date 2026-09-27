"""Tests for orchestrator/execution_evidence.py — mctlhq/mctl-agents#520's
`ExecutionEvidence` contract (ADR 018:
docs/adr/018-execution-evidence-envelope-contract.md). T1-T14 below map onto
that proposal's tasks.md "## Tests" section; see execution_evidence.py's
module docstring for the fail-closed contract every negative test here
asserts.

Naming convention (`# T<n> —` section banners) matches
tests/test_context_snapshot.py.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import action_approvals as aa
from orchestrator import execution_evidence as ee
from orchestrator import policy_checkpoint as pc
from orchestrator import redaction as red
from orchestrator import tracing_sdk as ts
from orchestrator import usage_ledger as ul
from orchestrator.context_snapshot import ContextSnapshotError, EvidenceRef
from orchestrator.human_input import CONTEXT_REF_PREFIXES
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context import snapshots as wcs

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "evidence" / "investigator-evidence.json"

_SOURCE = inspect.getsource(ee)

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _execution(**overrides) -> ee.ExecutionJoin:
    fields = dict(execution_id="we_test0000000000000000000000", work_item_id="wi-1", trace_id="tr-1")
    fields.update(overrides)
    return ee.ExecutionJoin(**fields)


def _outcome(**overrides) -> ee.Outcome:
    fields = dict(code="succeeded", reason_code="all-good")
    fields.update(overrides)
    return ee.Outcome(**fields)


def _policy_decision(**overrides) -> ee.PolicyDecisionRef:
    fields = dict(
        action_digest="sha256:" + "5e" * 32,
        verdict="ALLOW",
        code="allowed",
        policy_version="mctl-agents/policy/v1",
        rule_id="r1",
        approval_ref="",
    )
    fields.update(overrides)
    return ee.PolicyDecisionRef(**fields)


def _snapshot_ref(**overrides) -> ee.SnapshotRef:
    fields = dict(snapshot_id="cs-" + "0" * 16, content_hash="sha256:" + "1a" * 32)
    fields.update(overrides)
    return ee.SnapshotRef(**fields)


def _execution_request(**overrides) -> ee.ExecutionRequestRef:
    fields = dict(request_id="xr_test000000000000000000000", kind="start", state="fulfilled")
    fields.update(overrides)
    return ee.ExecutionRequestRef(**fields)


def _usage(**overrides) -> ee.UsageRef:
    fields = dict(session_id="sess-1", model_key="claude-sonnet-5", result_uuid=None, devloop_stage="investigator")
    fields.update(overrides)
    return ee.UsageRef(**fields)


def _approval(**overrides) -> ee.ApprovalRef:
    fields = dict(approval_id="aar_test00000000000000000000", intent_hash="sha256:" + "2b" * 32, state="approved")
    fields.update(overrides)
    return ee.ApprovalRef(**fields)


def _artifact(**overrides) -> ee.ArtifactRef:
    fields = dict(name="artifact.txt", kind="doc", content_hash="sha256:" + "3c" * 32)
    fields.update(overrides)
    return ee.ArtifactRef(**fields)


def _seal(**overrides) -> ee.ExecutionEvidence:
    fields = dict(
        execution=_execution(),
        outcome=_outcome(),
        created_at="2026-09-11T00:00:40Z",
        policy_decisions=[_policy_decision()],
    )
    fields.update(overrides)
    return ee.seal(**fields)


def _load_fixture_evidence() -> ee.ExecutionEvidence:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return ee.ExecutionEvidence.from_dict(data)


# ---------------------------------------------------------------------------
# T1 — seal determinism and identity
# ---------------------------------------------------------------------------
def test_seal_is_deterministic_across_created_at():
    common = dict(execution=_execution(), outcome=_outcome(), policy_decisions=[_policy_decision()])
    ev_one = ee.seal(created_at="2026-01-01T00:00:00Z", **common)
    ev_two = ee.seal(created_at="2099-01-01T00:00:00Z", **common)
    assert ev_one.content_hash == ev_two.content_hash
    assert ev_one.evidence_id == ev_two.evidence_id
    assert ev_one.created_at != ev_two.created_at


def test_seal_hash_changes_when_a_reference_changes():
    base = _seal()
    changed = _seal(execution=_execution(work_item_id="a-different-work-item"))
    assert base.content_hash != changed.content_hash
    assert base.evidence_id != changed.evidence_id


def test_evidence_id_is_derived_from_content_hash():
    ev = _seal()
    assert ev.content_hash.startswith("sha256:")
    assert ev.evidence_id == ee.EVIDENCE_ID_PREFIX + ev.content_hash[7:23]


def test_recompute_content_hash_agrees_with_seal_without_mutating():
    ev = _seal()
    before = ev.to_dict()
    assert ee.recompute_content_hash(ev) == ev.content_hash
    assert ev.to_dict() == before


def test_validate_rejects_an_evidence_id_that_does_not_derive_from_content_hash():
    doc = _seal().to_dict()
    doc["evidence_id"] = ee.EVIDENCE_ID_PREFIX + "0" * 16
    with pytest.raises(ee.ExecutionEvidenceError, match="does not match"):
        ee.ExecutionEvidence.from_dict(doc)


# ---------------------------------------------------------------------------
# T2 — optional-block hash stability
# ---------------------------------------------------------------------------
def test_optional_blocks_absent_hash_the_same_as_a_payload_omitting_the_key():
    sealed = _seal()  # no snapshot_refs/execution_request/usage/approvals/artifacts
    manual_payload = {
        "api_version": ee.API_VERSION,
        "kind": ee.KIND,
        "execution": sealed.execution.to_dict(),
        "outcome": sealed.outcome.to_dict(),
        "policy_decisions": [p.to_dict() for p in sealed.policy_decisions],
    }
    assert ee.hash_bytes(ee.canonical_json(manual_payload)) == sealed.content_hash


def test_adding_an_optional_block_changes_the_hash():
    without = _seal()
    with_snapshot = _seal(snapshot_refs=[_snapshot_ref()])
    assert without.content_hash != with_snapshot.content_hash


# ---------------------------------------------------------------------------
# created_at: excluded from content_hash by design (identical evidence_id
# for two different created_at values, above), but still bounded and
# format-checked like every other schema field.
# ---------------------------------------------------------------------------
def test_seal_rejects_a_malformed_created_at():
    with pytest.raises(ee.ExecutionEvidenceError, match="created_at"):
        _seal(created_at="not-a-timestamp")


def test_seal_rejects_an_overlong_created_at():
    with pytest.raises(ee.ExecutionEvidenceError, match="created_at"):
        _seal(created_at="2026-01-01T00:00:00." + "9" * 40 + "Z")


def test_from_dict_rejects_a_malformed_created_at():
    doc = _seal().to_dict()
    doc["created_at"] = "yesterday"
    with pytest.raises(ee.ExecutionEvidenceError, match="created_at"):
        ee.ExecutionEvidence.from_dict(doc)


# ---------------------------------------------------------------------------
# T3 — golden fixture
# ---------------------------------------------------------------------------
def test_golden_fixture_round_trips_and_hash_and_id_match_literals():
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    evidence = ee.ExecutionEvidence.from_dict(raw)
    assert evidence.to_dict() == raw
    assert evidence.content_hash == "sha256:624602c79c9fe0cf74954f51d838c01c72434071279c52b767dcc43d228dd656"
    assert evidence.evidence_id == "ev-624602c79c9fe0cf"
    assert ee.recompute_content_hash(evidence) == evidence.content_hash


# ---------------------------------------------------------------------------
# T4 — undecided derives from the canonical set
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(pc.UNDECIDED_CODES))
def test_undecided_derives_from_the_canonical_undecided_codes(code):
    ref = _policy_decision(code=code)
    assert ref.undecided is True


def test_a_decided_code_is_not_undecided():
    ref = _policy_decision(code="allowed")
    assert ref.undecided is False


def test_no_undecided_code_literal_appears_in_module_source():
    for code in pc.UNDECIDED_CODES:
        assert code not in _SOURCE, f"{code!r} must not be hardcoded; import UNDECIDED_CODES instead"


# ---------------------------------------------------------------------------
# T5 — redaction covers every block
# ---------------------------------------------------------------------------
_CREDENTIAL = "ghp_" + "a" * 40


def _seal_with_credential_in(block: str) -> ee.ExecutionEvidence:
    if block == "execution":
        return _seal(execution=_execution(trace_id=_CREDENTIAL))
    if block == "outcome":
        return _seal(outcome=_outcome(reason_code=_CREDENTIAL))
    if block == "policy_decisions":
        return _seal(policy_decisions=[_policy_decision(rule_id=_CREDENTIAL)])
    if block == "snapshot_refs":
        return _seal(snapshot_refs=[_snapshot_ref(snapshot_id=_CREDENTIAL)])
    if block == "execution_request":
        return _seal(execution_request=_execution_request(request_id=_CREDENTIAL))
    if block == "usage":
        return _seal(usage=_usage(model_key=_CREDENTIAL))
    if block == "approvals":
        return _seal(approvals=[_approval(approval_id=_CREDENTIAL)])
    if block == "artifacts":
        return _seal(artifacts=[_artifact(name=_CREDENTIAL)])
    raise AssertionError(f"no builder for block {block!r}")


@pytest.mark.parametrize("block", sorted(ee.BLOCK_NAMES))
def test_redaction_covers_every_block(block):
    evidence = _seal_with_credential_in(block)
    canonical_bytes = ee.canonical_json(evidence.to_dict())
    assert _CREDENTIAL.encode() not in canonical_bytes
    assert b"REDACTED" not in canonical_bytes
    assert b"***" not in canonical_bytes
    matching = [g for g in evidence.gaps if g.block == block and g.code == "redacted_out"]
    assert len(matching) == 1, evidence.gaps


@pytest.mark.parametrize(
    "credential",
    (
        "ghp_" + "a" * 36,
        "github_pat_" + "b" * 30,
        "sk-" + "c" * 30,
        "hvs." + "d" * 30,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Bearer " + "e" * 20,
        "https://user:pass@example.com/path",
    ),
)
def test_redaction_drops_every_known_credential_shape(credential):
    evidence = _seal(execution=_execution(trace_id=credential))
    assert credential not in json.dumps(evidence.to_dict())
    assert evidence.execution.trace_id == ""


@pytest.mark.parametrize("block", sorted(ee.BLOCK_NAMES))
def test_recompute_content_hash_agrees_after_a_leaf_is_redacted(block):
    # A redacted envelope must still be reproducible: recompute_content_hash
    # has to hash the same leaf representation _safe() wrote at seal() time,
    # not whatever a fresh to_dict() of the reconstructed dataclass defaults
    # an absent field to.
    evidence = _seal_with_credential_in(block)
    assert evidence.completeness in (ee.COMPLETE, ee.INCOMPLETE)  # sealed at all
    assert ee.recompute_content_hash(evidence) == evidence.content_hash


# ---------------------------------------------------------------------------
# T6 — completeness is derived and iff
# ---------------------------------------------------------------------------
def test_completeness_complete_with_no_required_gap():
    assert _seal().completeness == ee.COMPLETE


def test_completeness_incomplete_with_one_required_gap():
    ev = _seal(gaps=[ee.Gap(block="usage", code="not_produced", required=True)])
    assert ev.completeness == ee.INCOMPLETE


def test_completeness_stays_complete_with_only_a_non_required_gap():
    ev = _seal(gaps=[ee.Gap(block="usage", code="not_applicable", required=False)])
    assert ev.completeness == ee.COMPLETE


def test_completeness_is_not_an_init_parameter():
    with pytest.raises(TypeError):
        ee.ExecutionEvidence(
            api_version=ee.API_VERSION,
            kind=ee.KIND,
            evidence_id="ev-" + "0" * 16,
            content_hash="sha256:" + "0" * 64,
            created_at="2026-01-01T00:00:00Z",
            execution=_execution(),
            outcome=_outcome(),
            completeness="COMPLETE",
        )


def test_completeness_is_rejected_as_a_from_dict_key():
    doc = _seal().to_dict()
    doc["completeness"] = "COMPLETE"
    with pytest.raises(ee.ExecutionEvidenceError, match="unknown key"):
        ee.ExecutionEvidence.from_dict(doc)


# ---------------------------------------------------------------------------
# T7 — required-block enforcement
# ---------------------------------------------------------------------------
def test_seal_raises_when_the_default_required_block_is_absent_and_ungapped():
    with pytest.raises(ee.ExecutionEvidenceError, match="policy_decisions"):
        ee.seal(execution=_execution(), outcome=_outcome(), created_at="2026-01-01T00:00:00Z")


def test_seal_succeeds_when_the_same_absence_carries_a_gap():
    ev = ee.seal(
        execution=_execution(),
        outcome=_outcome(),
        created_at="2026-01-01T00:00:00Z",
        gaps=[ee.Gap(block="policy_decisions", code="not_produced", required=True)],
    )
    assert ev.completeness == ee.INCOMPLETE


def test_seal_raises_when_the_same_absence_carries_only_a_non_required_gap():
    # A Gap naming the block is not enough on its own: the Gap must itself
    # be required=True, or a caller could seal a COMPLETE envelope that is
    # silently missing a block Requirements marks required.
    with pytest.raises(ee.ExecutionEvidenceError, match="policy_decisions"):
        ee.seal(
            execution=_execution(),
            outcome=_outcome(),
            created_at="2026-01-01T00:00:00Z",
            gaps=[ee.Gap(block="policy_decisions", code="not_produced", required=False)],
        )


def test_seal_raises_for_a_caller_declared_required_block():
    requirements = ee.Requirements(usage=True)
    with pytest.raises(ee.ExecutionEvidenceError, match="usage"):
        _seal(requirements=requirements)


def test_seal_succeeds_for_a_caller_declared_required_block_with_a_gap():
    requirements = ee.Requirements(usage=True)
    ev = _seal(requirements=requirements, gaps=[ee.Gap(block="usage", code="store_unavailable", required=True)])
    assert ev.completeness == ee.INCOMPLETE


# ---------------------------------------------------------------------------
# T8 — no paths, no I/O
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden",
    ("import pathlib", "import os", "open(", "import httpx", "import urllib", "import subprocess"),
)
def test_module_source_has_no_forbidden_import_or_io(forbidden):
    assert forbidden not in _SOURCE


@pytest.mark.parametrize("bad_name", ("a/b", "a\\b", "..", "a/../b", "~secret", "/etc/passwd"))
def test_artifact_name_rejects_path_fragments(bad_name):
    with pytest.raises(ee.ExecutionEvidenceError):
        ee._check_artifact_name(bad_name)


def test_artifact_name_accepts_a_bounded_plain_name():
    ee._check_artifact_name("requirements.md")  # must not raise


# ---------------------------------------------------------------------------
# T9 — stdlib-only import (mirrors tests/test_policy_checkpoint.py:233-242)
# ---------------------------------------------------------------------------
def test_module_import_is_stdlib_only():
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.execution_evidence, sys; print(chr(10).join(sorted(sys.modules)))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = set(result.stdout.split("\n"))
    third_party_prefixes = ("claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio", "mcp", "opentelemetry")
    leaked = sorted(
        name for name in loaded
        if any(name == prefix or name.startswith(prefix + ".") for prefix in third_party_prefixes)
    )
    assert not leaked, f"orchestrator.execution_evidence pulled in third-party modules: {leaked}"


# ---------------------------------------------------------------------------
# T10 — no-authorization invariant (mirrors tests/test_context_snapshot.py:281-304)
# ---------------------------------------------------------------------------
_FORBIDDEN_TOKENS = ("allow", "deny", "permit", "grant", "authorized")


def _walk_keys(value):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _walk_keys(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_no_authorization_field_name_anywhere_in_the_schema():
    evidence = _load_fixture_evidence()
    keys = set(_walk_keys(evidence.to_dict()))
    for key in keys:
        lowered = key.lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in lowered, f"field name {key!r} contains forbidden token {token!r}"


# ---------------------------------------------------------------------------
# T11 — prefix agreement with the owning modules
# ---------------------------------------------------------------------------
def test_prefixes_agree_with_their_owning_modules():
    assert ee.EXECUTION_ID_PREFIX == wcs.EXECUTION_ID_PREFIX == "we_"
    assert ee.SNAPSHOT_ID_PREFIX == wcs.SNAPSHOT_ID_PREFIX == "cs_"
    assert ee.REQUEST_ID_PREFIX == xr.REQUEST_ID_PREFIX == "xr_"
    assert ee.APPROVAL_ID_PREFIX == aa.ID_PREFIX == "aar_"


def test_execution_request_vocabularies_agree_with_their_owning_module():
    assert ee.EXECUTION_REQUEST_KINDS == xr.KINDS
    assert ee.EXECUTION_REQUEST_STATES == xr.STATES


def test_approval_states_agree_with_their_owning_module():
    assert ee.APPROVAL_STATES == {aa.PENDING, aa.APPROVED, aa.DENIED, aa.EXPIRED, aa.CONSUMED}


def test_usage_devloop_stages_agree_with_their_owning_module():
    assert ee.USAGE_DEVLOOP_STAGES == ul.DEVLOOP_STAGES


# ---------------------------------------------------------------------------
# T12 — no second hashing convention
# ---------------------------------------------------------------------------
def test_module_defines_no_second_hashing_convention():
    assert "hashlib." not in _SOURCE
    assert "json.dumps(" not in _SOURCE


# ---------------------------------------------------------------------------
# T13 — seam compatibility
# ---------------------------------------------------------------------------
def test_evidence_ref_is_accepted_unchanged_by_context_snapshot_evidence_ref():
    evidence = _seal()
    ref = ee.evidence_ref(evidence, "proposal")
    assert set(ref) == {"evidence_id", "kind"}
    accepted = EvidenceRef.from_dict(ref)
    assert accepted.evidence_id == evidence.evidence_id
    assert accepted.kind == "proposal"


def test_evidence_ref_with_a_third_key_is_rejected_by_evidence_ref_from_dict():
    ref = ee.evidence_ref(_seal(), "proposal")
    ref["extra"] = "nope"
    with pytest.raises(ContextSnapshotError, match="unknown key"):
        EvidenceRef.from_dict(ref)


def test_evidence_id_is_a_valid_human_input_evidence_context_ref():
    evidence = _seal()
    ref = f"evidence:{evidence.evidence_id}"
    assert ref.startswith(CONTEXT_REF_PREFIXES)


def test_to_log_dict_carries_no_unbounded_text():
    evidence = _seal()
    log = evidence.to_log_dict()
    expected_keys = {
        "evidence_id", "content_hash", "completeness", "outcome_code",
        "policy_decision_count", "snapshot_ref_count", "approval_count",
        "artifact_count", "gap_count",
    }
    assert set(log) == expected_keys
    for key, value in log.items():
        if isinstance(value, str):
            assert len(value) <= 128, f"{key} looks unbounded: {value!r}"
        else:
            assert isinstance(value, int), f"{key} must be an int count, got {type(value).__name__}"


# ---------------------------------------------------------------------------
# T14 — tracing_sdk regression (task 1's extraction)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "token",
    (
        "ghp_" + "a" * 36,
        "github_pat_" + "b" * 30,
        "sk-" + "c" * 30,
        "hvs." + "d" * 30,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Bearer " + "e" * 20,
        "https://user:pass@example.com/path",
        "a perfectly ordinary, non-secret string",
    ),
)
def test_redaction_contains_credential_matches_the_old_tracing_sdk_inline_pattern(token):
    old_verdict = bool(ts._CREDENTIAL_VALUE.search(token))
    new_verdict = red.contains_credential(token)
    assert new_verdict == old_verdict
