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
import re
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import action_approvals as aa
from orchestrator import execution_evidence as ee
from orchestrator import execution_identity as ei
from orchestrator import policy_checkpoint as pc
from orchestrator import redaction as red
from orchestrator import tracing_sdk as ts
from orchestrator import usage_ledger as ul
from orchestrator.context_snapshot import ContextSnapshotError, EvidenceRef
from orchestrator.human_input import CONTEXT_REF_PREFIXES
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context import snapshots as wcs

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "evidence"
FIXTURE_PATH = EVIDENCE_FIXTURE_DIR / "investigator-evidence.json"
IMPLEMENTER_FIXTURE_PATH = EVIDENCE_FIXTURE_DIR / "implementer-evidence.json"
SHEPHERD_FIXTURE_PATH = EVIDENCE_FIXTURE_DIR / "shepherd-evidence.json"
# ADR 018 Amendment 2 (mctlhq/mctl-agents#199): subject-bound vectors.
SHEPHERD_PR_FIXTURE_PATH = EVIDENCE_FIXTURE_DIR / "shepherd-pr-evidence.json"
SHEPHERD_PR_SUPERSEDING_FIXTURE_PATH = EVIDENCE_FIXTURE_DIR / "shepherd-pr-superseding-evidence.json"

# The three pre-Amendment-2 vectors. Their files and these literals are
# byte-identical to what main carried before the amendment: the new blocks
# are absent from them, so they must hash exactly as before.
LEGACY_FIXTURE_PATHS = (FIXTURE_PATH, IMPLEMENTER_FIXTURE_PATH, SHEPHERD_FIXTURE_PATH)

# (fixture path, literal content_hash, literal evidence_id, expected primary_execution_ref kind)
GOLDEN_FIXTURES = (
    (
        FIXTURE_PATH,
        "sha256:624602c79c9fe0cf74954f51d838c01c72434071279c52b767dcc43d228dd656",
        "ev-624602c79c9fe0cf",
        "work",
    ),
    (
        IMPLEMENTER_FIXTURE_PATH,
        "sha256:28b12deda5b9a546aa12f004fca51071e956ad4c1ffcad3308e46b0fa2daa564",
        "ev-28b12deda5b9a546",
        "runtime",
    ),
    (
        SHEPHERD_FIXTURE_PATH,
        "sha256:13362a728651f5ffaa2b899b238545c6aaa77c7dac903cc43c39634700603dd7",
        "ev-13362a728651f5ff",
        "work",
    ),
    (
        SHEPHERD_PR_FIXTURE_PATH,
        "sha256:df6d015f8ddf64ac6f1955649883708402c7185618a09b1c07b7276fa32c7cc8",
        "ev-df6d015f8ddf64ac",
        "work",
    ),
    (
        SHEPHERD_PR_SUPERSEDING_FIXTURE_PATH,
        "sha256:5a7500c45dd74b8b0d46388115813a3b9576237c5523679d0f254e0994c4b282",
        "ev-5a7500c45dd74b8b",
        "work",
    ),
)

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


_SHA1 = "1" * 40
_SHA2 = "2" * 40


def _subject(**overrides) -> ee.SubjectRef:
    fields = dict(kind="pull_request", repository="mctlhq/mctl-agents", ref="524", revision=_SHA1)
    fields.update(overrides)
    return ee.SubjectRef(**fields)


def _provenance(**overrides) -> ee.Provenance:
    fields = dict(authority="observed", observed_at="2026-10-04T10:00:00Z", supersedes="")
    fields.update(overrides)
    return ee.Provenance(**fields)


def _versions(**overrides) -> ee.VersionPins:
    fields = dict(
        agent="pr-shepherd", environment="shadow", definition_version="1",
        definition_content_hash="sha256:" + "d1" * 32, profile_name="pr-shepherd-default",
        profile_version="3", profile_content_hash="sha256:" + "f3" * 32, release_revision=7,
    )
    fields.update(overrides)
    return ee.VersionPins(**fields)


def _tool_call(**overrides) -> ee.ToolCallRef:
    fields = dict(
        kind="github.pull_request.merge", name="merge_pull_request",
        action_digest="sha256:" + "5e" * 32, status="succeeded",
    )
    fields.update(overrides)
    return ee.ToolCallRef(**fields)


def _seal(**overrides) -> ee.ExecutionEvidence:
    fields = dict(
        execution=_execution(),
        outcome=_outcome(),
        created_at="2026-09-11T00:00:40Z",
        policy_decisions=[_policy_decision()],
    )
    fields.update(overrides)
    return ee.seal(**fields)


def _load_fixture_evidence(path: Path = FIXTURE_PATH) -> ee.ExecutionEvidence:
    data = json.loads(path.read_text(encoding="utf-8"))
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


@pytest.mark.parametrize(
    "content_hash",
    [
        "sha256:ghp_" + "a" * 40,  # a credential riding in the hash field
        "sha256:" + "x" * 100_000,  # an unbounded blob
        "sha256:" + "A" * 64,  # right length, wrong alphabet
        "sha256:" + "a" * 63,  # one short
    ],
)
def test_from_dict_rejects_a_forged_self_consistent_hash_and_id(content_hash):
    """A forged pair whose id is derived from the forged hash is internally
    consistent, so only a shape check on both fields rejects it -- before it
    reaches to_dict() or the to_log_dict() telemetry export."""
    doc = _seal().to_dict()
    doc["content_hash"] = content_hash
    doc["evidence_id"] = ee.EVIDENCE_ID_PREFIX + content_hash[7:23]
    with pytest.raises(ee.ExecutionEvidenceError, match="content_hash"):
        ee.ExecutionEvidence.from_dict(doc)


# ---------------------------------------------------------------------------
# T3 — golden fixture
#
# Extended by mctlhq/mctl-agents#539 (T15 in tasks.md) into a parametrized
# loader over all three golden vectors -- a we_-only envelope (unchanged),
# an ex--only envelope and a both-identities envelope -- each asserting its
# literal content_hash, its literal evidence_id, to_dict() round-trip
# equality, recompute_content_hash() agreement and its expected
# primary_execution_ref kind. The investigator fixture's two literals stay
# byte-identical to what T3 asserted before this amendment.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,content_hash,evidence_id,primary_kind", GOLDEN_FIXTURES)
def test_golden_fixture_round_trips_and_hash_and_id_match_literals(path, content_hash, evidence_id, primary_kind):
    raw = json.loads(path.read_text(encoding="utf-8"))
    evidence = ee.ExecutionEvidence.from_dict(raw)
    assert evidence.to_dict() == raw
    assert evidence.content_hash == content_hash
    assert evidence.evidence_id == evidence_id
    assert ee.recompute_content_hash(evidence) == evidence.content_hash
    assert evidence.execution.primary_execution_ref[0] == primary_kind


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
    # ADR 018 Amendment 2 blocks: each credential goes into the one leaf of
    # that block that may legitimately be blank after redaction.
    if block == "versions":
        return _seal(versions=_versions(environment=_CREDENTIAL))
    if block == "subject":
        return _seal(
            subject=_subject(kind="work_item", ref="wi-1", repository=_CREDENTIAL, revision=""),
            provenance=_provenance(),
        )
    if block == "tool_calls":
        return _seal(tool_calls=[_tool_call(name=_CREDENTIAL)])
    if block == "provenance":
        return _seal(provenance=_provenance(supersedes=_CREDENTIAL))
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


@pytest.mark.parametrize("block", sorted(ee.BLOCK_NAMES))
def test_from_dict_round_trips_an_envelope_that_was_redacted_at_seal_time(block):
    # The legitimate counterpart to
    # test_from_dict_rejects_a_credential_planted_directly_in_the_raw_mapping
    # below: a document produced by seal() already carries the
    # _REDACTED_LEAF ("") in place of the credential _safe() dropped, plus
    # the redacted_out Gap recording it. from_dict's hard-fail read-path
    # check must not mistake that already-safe leaf for a fresh credential
    # hit and reject a document seal() actually produced.
    sealed = _seal_with_credential_in(block)
    reloaded = ee.ExecutionEvidence.from_dict(sealed.to_dict())
    assert reloaded.to_dict() == sealed.to_dict()
    assert reloaded.content_hash == sealed.content_hash
    matching = [g for g in reloaded.gaps if g.block == block and g.code == "redacted_out"]
    assert len(matching) == 1, reloaded.gaps


def test_from_dict_rejects_a_credential_planted_directly_in_the_raw_mapping():
    # from_dict is a read path too (e.g. a document handed back from
    # storage): a credential planted straight into the raw mapping, bypassing
    # seal(), must be rejected outright, not silently redacted. A silent
    # rewrite here would leave content_hash/evidence_id (taken from the raw,
    # unredacted mapping) certifying content the returned envelope no longer
    # carries.
    doc = _seal().to_dict()
    doc["execution"]["trace_id"] = _CREDENTIAL
    with pytest.raises(ee.ExecutionEvidenceError, match="requires redaction"):
        ee.ExecutionEvidence.from_dict(doc)


def test_from_dict_rejects_an_over_cap_leaf_planted_directly_in_the_raw_mapping():
    # Same read-path parity, for the generic per-leaf length cap rather than
    # the credential shape: an over-cap blob must be rejected, not silently
    # dropped and re-emitted.
    doc = _seal().to_dict()
    oversized = "x" * (ee.MAX_LEAF_CHARS + 1)
    doc["execution"]["trace_id"] = oversized
    with pytest.raises(ee.ExecutionEvidenceError, match="requires redaction"):
        ee.ExecutionEvidence.from_dict(doc)


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


@pytest.mark.parametrize("path", tuple(entry[0] for entry in GOLDEN_FIXTURES))
def test_no_authorization_field_name_anywhere_in_the_schema(path):
    evidence = _load_fixture_evidence(path)
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
    # mctlhq/mctl-agents#539: RUNTIME_EXECUTION_ID_PREFIX must agree with
    # execution_identity.CONTEXT_ID_PREFIX, the constant seal() derives
    # ExecutionContext.context_id from -- the same drift-guard pattern as
    # every prefix above, now that #539 extracted a named constant there.
    assert ee.RUNTIME_EXECUTION_ID_PREFIX == ei.CONTEXT_ID_PREFIX == "ex-"


def test_runtime_execution_id_pattern_matches_a_real_sealed_context_id():
    # mctlhq/mctl-agents#539 P2 follow-up: the T11 check above only pins the
    # "ex-" prefix, never the 16-hex *body* shape _RUNTIME_EXECUTION_ID_PATTERN
    # asserts. Seal a real ExecutionContext through execution_identity.seal()
    # (CONTEXT_ID_PREFIX + content_hash[7:23]) and confirm it fully matches the
    # pattern, so a change to that slice (a different length, a different
    # derivation) fails this test instead of silently making
    # _check_execution_join reject every legitimate runtime_execution_id.
    from tests.test_execution_identity import _context

    context = _context()
    assert ee._RUNTIME_EXECUTION_ID_PATTERN.fullmatch(context.context_id)


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
        "evidence_id", "content_hash", "completeness", "outcome_code", "primary_execution_kind",
        "policy_decision_count", "snapshot_ref_count", "approval_count",
        "artifact_count", "gap_count", "tool_call_count", "subject_kind", "authority",
    }
    assert set(log) == expected_keys
    assert log["primary_execution_kind"] in ee.EXECUTION_REF_KINDS | {""}
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


# ---------------------------------------------------------------------------
# T15 — execution join: two typed identities (mctlhq/mctl-agents#539, ADR 018
# Amendment 1). See also T3's and T11's extensions above.
# ---------------------------------------------------------------------------
_RUNTIME_ID = "ex-0123456789abcdef"


def test_execution_id_rejects_a_runtime_context_id():
    with pytest.raises(ee.ExecutionEvidenceError, match="runtime_execution_id"):
        _seal(execution=_execution(execution_id=_RUNTIME_ID))


def test_runtime_execution_id_rejects_a_work_execution_id():
    with pytest.raises(ee.ExecutionEvidenceError, match="execution_id"):
        _seal(execution=_execution(execution_id="", runtime_execution_id="we_01J8ZQK7N3XG9F6C2R4D8T1M5W"))


@pytest.mark.parametrize(
    "bad_runtime_id",
    (
        "ex-",
        "ex-XYZ",
        "ex-0123456789abcde",  # one short
        "ex-0123456789ABCDEF",  # wrong alphabet (uppercase)
        "ex-" + "a" * 17,  # one long
    ),
)
def test_runtime_execution_id_rejects_a_malformed_shape(bad_runtime_id):
    with pytest.raises(ee.ExecutionEvidenceError, match="runtime_execution_id"):
        _seal(execution=_execution(execution_id="", runtime_execution_id=bad_runtime_id))


def test_execution_id_still_rejects_a_foreign_prefix():
    with pytest.raises(ee.ExecutionEvidenceError, match="execution_id"):
        _seal(execution=_execution(execution_id="cs_abc"))


def test_from_dict_rejects_an_unknown_execution_key():
    doc = _seal().to_dict()
    doc["execution"]["runtime_execution"] = "ex-0123456789abcdef"
    with pytest.raises(ee.ExecutionEvidenceError, match="unknown key"):
        ee.ExecutionEvidence.from_dict(doc)


def test_runtime_only_envelope_seals_complete():
    ev = ee.seal(
        execution=ee.ExecutionJoin(runtime_execution_id=_RUNTIME_ID),
        outcome=_outcome(),
        created_at="2026-09-29T00:00:00Z",
        policy_decisions=[_policy_decision()],
    )
    assert ev.completeness == ee.COMPLETE
    assert ev.gaps == ()
    assert ev.execution.primary_execution_ref == ("runtime", _RUNTIME_ID)


def test_seal_raises_when_both_identities_are_blank_and_ungapped():
    with pytest.raises(ee.ExecutionEvidenceError, match="execution"):
        ee.seal(
            execution=ee.ExecutionJoin(),
            outcome=_outcome(),
            created_at="2026-09-29T00:00:00Z",
            policy_decisions=[_policy_decision()],
        )


def test_seal_succeeds_when_that_absence_carries_a_required_gap():
    ev = ee.seal(
        execution=ee.ExecutionJoin(),
        outcome=_outcome(),
        created_at="2026-09-29T00:00:00Z",
        policy_decisions=[_policy_decision()],
        gaps=[ee.Gap(block="execution", code="not_produced", required=True)],
    )
    assert ev.completeness == ee.INCOMPLETE
    assert ev.execution.primary_execution_ref == ("", "")


def test_blank_runtime_identity_is_hash_neutral():
    with_explicit_blank = _seal(execution=_execution(runtime_execution_id=""))
    without_the_field = _seal(execution=_execution())
    assert with_explicit_blank.content_hash == without_the_field.content_hash
    assert with_explicit_blank.evidence_id == without_the_field.evidence_id
    # And against a hand-built payload whose execution block omits the key
    # entirely -- the T2 idiom at test_execution_evidence.py:155.
    manual_payload = {
        "api_version": ee.API_VERSION,
        "kind": ee.KIND,
        "execution": {
            "execution_id": without_the_field.execution.execution_id,
            "work_item_id": without_the_field.execution.work_item_id,
            "trace_id": without_the_field.execution.trace_id,
        },
        "outcome": without_the_field.outcome.to_dict(),
        "policy_decisions": [p.to_dict() for p in without_the_field.policy_decisions],
    }
    assert ee.hash_bytes(ee.canonical_json(manual_payload)) == without_the_field.content_hash


def test_adding_a_runtime_identity_changes_the_hash():
    without = _seal(execution=_execution())
    with_runtime = _seal(execution=_execution(runtime_execution_id=_RUNTIME_ID))
    assert without.content_hash != with_runtime.content_hash
    assert without.evidence_id != with_runtime.evidence_id


@pytest.mark.parametrize(
    "execution",
    (
        _execution(),  # we_ only
        ee.ExecutionJoin(runtime_execution_id=_RUNTIME_ID),  # ex- only
        _execution(runtime_execution_id=_RUNTIME_ID),  # both
    ),
    ids=("work-only", "runtime-only", "both"),
)
def test_recompute_content_hash_agrees_for_all_three_join_shapes(execution):
    ev = _seal(execution=execution)
    assert ee.recompute_content_hash(ev) == ev.content_hash


def test_redaction_round_trip_covers_runtime_execution_id():
    # Extends T5's per-block redaction parametrization: the credential must
    # be planted in runtime_execution_id specifically, not just trace_id.
    evidence = _seal(execution=_execution(runtime_execution_id=_CREDENTIAL))
    assert _CREDENTIAL not in json.dumps(evidence.to_dict())
    assert evidence.execution.runtime_execution_id == ""
    matching = [g for g in evidence.gaps if g.block == "execution" and g.code == "redacted_out"]
    assert len(matching) == 1, evidence.gaps
    assert ee.recompute_content_hash(evidence) == evidence.content_hash


@pytest.mark.parametrize(
    "execution_id,runtime_execution_id,expected",
    (
        ("we_test0000000000000000000000", "", ("work", "we_test0000000000000000000000")),
        ("we_test0000000000000000000000", _RUNTIME_ID, ("work", "we_test0000000000000000000000")),
        ("", _RUNTIME_ID, ("runtime", _RUNTIME_ID)),
        ("", "", ("", "")),
    ),
)
def test_primary_execution_ref_precedence(execution_id, runtime_execution_id, expected):
    join = ee.ExecutionJoin(execution_id=execution_id, runtime_execution_id=runtime_execution_id)
    assert join.primary_execution_ref == expected
    assert join.primary_execution_ref[0] in ee.EXECUTION_REF_KINDS | {""}


def test_primary_execution_ref_is_not_an_init_parameter():
    with pytest.raises(TypeError):
        ee.ExecutionJoin(execution_id="we_x", primary_execution_ref=("work", "we_x"))


def test_primary_execution_ref_is_rejected_as_a_from_dict_key():
    doc = _seal().to_dict()
    doc["execution"]["primary_execution_ref"] = ["work", "we_x"]
    with pytest.raises(ee.ExecutionEvidenceError, match="unknown key"):
        ee.ExecutionEvidence.from_dict(doc)


# ---------------------------------------------------------------------------
# T16 — ADR 018 Amendment 2 (mctlhq/mctl-agents#199): versions, subject,
# tool calls, provenance (authority, observed_at, supersedes), the
# observation_failed gap and resolve_current. Every test here was
# mutation-verified: reverting the rule it names makes it fail.
# ---------------------------------------------------------------------------
_AMENDMENT_2_KEYS = {"versions", "subject", "tool_calls", "provenance"}


@pytest.mark.parametrize("path", LEGACY_FIXTURE_PATHS)
def test_legacy_fixtures_carry_no_amendment_2_key_and_reseal_to_their_literal(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert not (_AMENDMENT_2_KEYS & set(raw))
    evidence = ee.ExecutionEvidence.from_dict(raw)
    assert not (_AMENDMENT_2_KEYS & set(evidence.to_dict()))
    # Re-sealing the same blocks through the amended seal() reproduces the
    # identity committed before the amendment.
    resealed = ee.seal(
        execution=evidence.execution,
        outcome=evidence.outcome,
        created_at="2030-01-01T00:00:00Z",
        policy_decisions=evidence.policy_decisions,
        snapshot_refs=evidence.snapshot_refs,
        execution_request=evidence.execution_request,
        usage=evidence.usage,
        approvals=evidence.approvals,
        artifacts=evidence.artifacts,
        gaps=evidence.gaps,
        requirements=ee.Requirements(policy_decisions=False),
    )
    assert resealed.content_hash == evidence.content_hash
    assert resealed.evidence_id == evidence.evidence_id


def test_absent_amendment_2_blocks_are_hash_neutral():
    baseline = _seal()
    explicit = _seal(versions=None, subject=None, tool_calls=(), provenance=None)
    assert explicit.content_hash == baseline.content_hash
    manual_payload = {
        "api_version": ee.API_VERSION,
        "kind": ee.KIND,
        "execution": baseline.execution.to_dict(),
        "outcome": baseline.outcome.to_dict(),
        "policy_decisions": [p.to_dict() for p in baseline.policy_decisions],
    }
    assert ee.hash_bytes(ee.canonical_json(manual_payload)) == baseline.content_hash
    assert not (_AMENDMENT_2_KEYS & set(baseline.to_dict()))


def test_from_dict_treats_explicit_null_and_empty_new_blocks_as_absent():
    doc = _seal().to_dict()
    doc.update({"versions": None, "subject": None, "tool_calls": [], "provenance": None})
    reloaded = ee.ExecutionEvidence.from_dict(doc)
    assert ee.recompute_content_hash(reloaded) == reloaded.content_hash


@pytest.mark.parametrize(
    "block",
    ("versions", "subject", "tool_calls", "provenance"),
)
def test_each_amendment_2_block_changes_the_hash_when_present(block):
    baseline = _seal()
    if block == "subject":
        with_block = _seal(subject=_subject(), provenance=_provenance())
        assert with_block.content_hash != _seal(provenance=_provenance()).content_hash
    else:
        value = {"versions": _versions(), "tool_calls": [_tool_call()], "provenance": _provenance()}[block]
        with_block = _seal(**{block: value})
    assert with_block.content_hash != baseline.content_hash
    assert ee.recompute_content_hash(with_block) == with_block.content_hash
    reloaded = ee.ExecutionEvidence.from_dict(with_block.to_dict())
    assert reloaded == with_block


# -- subject binding --------------------------------------------------------


def test_pr_at_sha1_and_pr_at_sha2_never_share_an_identity():
    at_sha1 = _seal(subject=_subject(revision=_SHA1), provenance=_provenance())
    at_sha2 = _seal(subject=_subject(revision=_SHA2), provenance=_provenance())
    assert at_sha1.content_hash != at_sha2.content_hash
    assert at_sha1.evidence_id != at_sha2.evidence_id
    assert at_sha1.to_dict()["subject"]["revision"] == _SHA1
    assert ee.recompute_content_hash(at_sha1) == at_sha1.content_hash


@pytest.mark.parametrize(
    "field,value",
    (("repository", "mctlhq/mctl-api"), ("ref", "525"), ("kind", "branch")),
)
def test_every_subject_key_field_participates_in_the_hash(field, value):
    base = _seal(subject=_subject(), provenance=_provenance())
    overrides = {field: value}
    if field == "kind":
        overrides["ref"] = "main"
    changed = _seal(subject=_subject(**overrides), provenance=_provenance())
    assert changed.content_hash != base.content_hash


@pytest.mark.parametrize("kind", sorted(ee.SHA_BOUND_SUBJECT_KINDS))
def test_a_sha_bound_subject_without_its_revision_is_rejected(kind):
    ref = "524" if kind == "pull_request" else "main"
    with pytest.raises(ee.ExecutionEvidenceError, match=r"subject.revision is required"):
        _seal(subject=_subject(kind=kind, ref=ref, revision=""), provenance=_provenance())


@pytest.mark.parametrize("revision", ("abc123", "1" * 39, "A" * 40, "1" * 41, "sha256:" + "1" * 64))
def test_a_pr_subject_revision_must_be_a_full_lowercase_git_sha(revision):
    with pytest.raises(ee.ExecutionEvidenceError, match=r"subject.revision"):
        _seal(subject=_subject(revision=revision), provenance=_provenance())


def test_a_64_hex_sha256_repository_object_id_is_accepted():
    _seal(subject=_subject(revision="a" * 64), provenance=_provenance())  # must not raise


@pytest.mark.parametrize(
    "overrides,match",
    (
        ({"kind": "deployment"}, "subject.kind"),
        ({"kind": ""}, "subject.kind"),
        ({"ref": "abc"}, "subject.ref"),
        ({"ref": ""}, "subject.ref"),
        ({"repository": ""}, "subject.repository"),
        ({"repository": "mctl-agents"}, "subject.repository"),
        ({"repository": "a/b/c"}, "subject.repository"),
        ({"kind": "branch", "ref": "feat/../x"}, "path fragment"),
        ({"kind": "branch", "ref": "/etc/passwd"}, "subject.ref"),
        ({"kind": "branch", "ref": "a b"}, "subject.ref"),
    ),
)
def test_subject_shape_is_enforced(overrides, match):
    with pytest.raises(ee.ExecutionEvidenceError, match=match):
        _seal(subject=_subject(**overrides), provenance=_provenance())


def test_issue_and_work_item_subjects_may_omit_the_revision():
    _seal(subject=_subject(kind="issue", ref="199", revision=""), provenance=_provenance())
    _seal(subject=_subject(kind="work_item", ref="wi-1", repository="", revision=""), provenance=_provenance())


def test_subject_bound_evidence_requires_provenance():
    with pytest.raises(ee.ExecutionEvidenceError, match="provenance"):
        _seal(subject=_subject())


def test_from_dict_rejects_an_unknown_subject_key():
    doc = _seal(subject=_subject(), provenance=_provenance()).to_dict()
    doc["subject"]["head_sha"] = _SHA1
    with pytest.raises(ee.ExecutionEvidenceError, match="unknown key"):
        ee.ExecutionEvidence.from_dict(doc)


# -- provenance: authority, observed_at, supersedes -------------------------


def test_authority_precedence_is_observed_then_derived_then_asserted():
    assert ee.AUTHORITIES == ("observed", "derived", "asserted")
    assert ee.AUTHORITY_RANK["observed"] > ee.AUTHORITY_RANK["derived"] > ee.AUTHORITY_RANK["asserted"]


@pytest.mark.parametrize("authority", ee.AUTHORITIES)
def test_every_authority_in_the_vocabulary_is_accepted(authority):
    _seal(provenance=_provenance(authority=authority))  # must not raise


@pytest.mark.parametrize("authority", ("", "authoritative", "OBSERVED", "model", "verified"))
def test_authority_outside_the_vocabulary_is_rejected(authority):
    with pytest.raises(ee.ExecutionEvidenceError, match=r"provenance.authority"):
        _seal(provenance=_provenance(authority=authority))


def test_authority_participates_in_the_hash():
    assert (
        _seal(provenance=_provenance(authority="observed")).content_hash
        != _seal(provenance=_provenance(authority="asserted")).content_hash
    )


@pytest.mark.parametrize("observed_at", ("", "yesterday", "2026-10-04 10:00:00Z", "2026-10-04T10:00:00+00:00"))
def test_observed_at_is_required_and_shape_checked(observed_at):
    with pytest.raises(ee.ExecutionEvidenceError, match="observed_at"):
        _seal(provenance=_provenance(observed_at=observed_at))


def test_observed_at_participates_in_the_hash_but_created_at_does_not():
    a = _seal(provenance=_provenance(observed_at="2026-10-04T10:00:00Z"), created_at="2026-10-04T11:00:00Z")
    b = _seal(provenance=_provenance(observed_at="2026-10-04T10:00:01Z"), created_at="2026-10-04T11:00:00Z")
    c = _seal(provenance=_provenance(observed_at="2026-10-04T10:00:00Z"), created_at="2026-10-04T12:00:00Z")
    assert a.content_hash != b.content_hash
    assert a.content_hash == c.content_hash


@pytest.mark.parametrize(
    "supersedes",
    (
        "ev-XYZ",
        "ev-" + "a" * 15,
        "ev-" + "A" * 16,
        "ev-" + "a" * 17,
        "cs-" + "a" * 16,
        "ex-" + "a" * 16,
        "aar_" + "a" * 16,
        "a" * 19,
    ),
)
def test_supersedes_must_reference_an_evidence_id(supersedes):
    with pytest.raises(ee.ExecutionEvidenceError, match="supersedes"):
        _seal(provenance=_provenance(supersedes=supersedes))


def test_supersedes_accepts_a_real_evidence_id_and_changes_the_hash():
    earlier = _seal(provenance=_provenance())
    later = _seal(provenance=_provenance(supersedes=earlier.evidence_id))
    assert later.provenance is not None and later.provenance.supersedes == earlier.evidence_id
    assert later.content_hash != earlier.content_hash


def test_validate_rejects_an_envelope_that_supersedes_itself():
    sealed = _seal(provenance=_provenance())
    forged = ee.ExecutionEvidence(
        **{**sealed.__dict__, "provenance": _provenance(supersedes=sealed.evidence_id)}
    )
    with pytest.raises(ee.ExecutionEvidenceError, match="must not name the envelope itself"):
        forged.validate()


# -- tool calls --------------------------------------------------------------


def test_tool_call_kinds_are_the_policy_checkpoint_action_kinds():
    assert ee.TOOL_CALL_KINDS is pc.ACTION_KINDS


def test_tool_call_kinds_cover_every_governed_policy_action_kind():
    action_kind_shape = r"^[a-z_]+(\.[a-z_]+)+$"
    declared = {
        value for name, value in vars(pc).items()
        if name.isupper() and isinstance(value, str) and re.fullmatch(action_kind_shape, value)
    }
    assert declared, "policy_checkpoint declares no action kinds?"
    assert declared == set(ee.TOOL_CALL_KINDS)


@pytest.mark.parametrize(
    "overrides,match",
    (
        ({"kind": "shell.exec"}, "tool_call.kind"),
        ({"kind": ""}, "tool_call.kind"),
        ({"action_digest": ""}, "tool_call.action_digest"),
        ({"action_digest": "5e" * 32}, "tool_call.action_digest"),
        ({"action_digest": "sha256:" + "5E" * 32}, "tool_call.action_digest"),
        ({"status": "ok"}, "tool_call.status"),
        ({"status": ""}, "tool_call.status"),
        ({"name": "rm -rf /"}, "tool_call.name"),
        ({"name": "x" * 129}, "tool_call.name"),
    ),
)
def test_tool_call_shape_is_enforced(overrides, match):
    with pytest.raises(ee.ExecutionEvidenceError, match=match):
        _seal(tool_calls=[_tool_call(**overrides)])


def test_tool_call_digest_and_status_participate_in_the_hash():
    base = _seal(tool_calls=[_tool_call()])
    assert base.content_hash != _seal(tool_calls=[_tool_call(action_digest="sha256:" + "6f" * 32)]).content_hash
    assert base.content_hash != _seal(tool_calls=[_tool_call(status="unknown")]).content_hash


def test_tool_calls_are_count_bounded():
    with pytest.raises(ee.ExecutionEvidenceError, match="tool_calls holds"):
        _seal(tool_calls=[_tool_call()] * (ee.MAX_TOOL_CALLS + 1))


# -- versions ----------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,match",
    (
        ({"definition_content_hash": ""}, "definition_content_hash is required"),
        ({"agent": ""}, "versions.agent is required"),
        ({"definition_content_hash": "sha256:short"}, "definition_content_hash"),
        ({"profile_content_hash": "d1" * 32}, "profile_content_hash"),
        ({"release_revision": -1}, "release_revision"),
        ({"agent": "Has Spaces"}, "versions.agent"),
        ({"definition_version": "1 2"}, "versions.definition_version"),
    ),
)
def test_versions_shape_is_enforced(overrides, match):
    with pytest.raises(ee.ExecutionEvidenceError, match=match):
        _seal(versions=_versions(**overrides))


@pytest.mark.parametrize(
    "field,value",
    (
        ("definition_content_hash", "sha256:" + "d2" * 32),
        ("profile_version", "4"),
        ("profile_content_hash", "sha256:" + "f4" * 32),
        ("release_revision", 8),
    ),
)
def test_every_version_pin_participates_in_the_hash(field, value):
    assert _seal(versions=_versions()).content_hash != _seal(versions=_versions(**{field: value})).content_hash


def test_versions_release_revision_round_trips_as_null():
    sealed = _seal(versions=_versions(release_revision=None))
    assert sealed.to_dict()["versions"]["release_revision"] is None
    assert ee.ExecutionEvidence.from_dict(sealed.to_dict()) == sealed


# -- unknown is not absence -------------------------------------------------


def test_observation_failed_gap_must_be_required():
    with pytest.raises(ee.ExecutionEvidenceError, match="must be required"):
        _seal(gaps=[ee.Gap(block="tool_calls", code="observation_failed", required=False)])


def test_observation_failed_gap_makes_the_envelope_incomplete():
    ev = _seal(gaps=[ee.Gap(block="subject", code="observation_failed", required=True)])
    assert ev.completeness == ee.INCOMPLETE


def test_a_caller_required_new_block_must_be_present_or_gapped():
    with pytest.raises(ee.ExecutionEvidenceError, match="subject"):
        _seal(requirements=ee.Requirements(subject=True))
    ev = _seal(
        requirements=ee.Requirements(subject=True),
        gaps=[ee.Gap(block="subject", code="observation_failed", required=True)],
    )
    assert ev.completeness == ee.INCOMPLETE


# -- resolve_current ---------------------------------------------------------


def _pr_evidence(revision=_SHA1, **provenance) -> ee.ExecutionEvidence:
    return _seal(subject=_subject(revision=revision), provenance=_provenance(**provenance))


def _resolve(candidates, revision=_SHA1):
    return ee.resolve_current(
        candidates, kind="pull_request", repository="mctlhq/mctl-agents", ref="524", revision=revision,
    )


def test_resolve_current_without_a_live_revision_is_unknown_not_none():
    result = _resolve([_pr_evidence()], revision="")
    assert result.state == "unknown_revision"
    assert result.evidence is None


def test_resolve_current_never_returns_sha1_evidence_for_sha2():
    at_sha1 = _pr_evidence(revision=_SHA1)
    assert _resolve([at_sha1], revision=_SHA1).evidence == at_sha1
    result = _resolve([at_sha1], revision=_SHA2)
    assert result.state == "no_evidence"
    assert result.evidence is None


def test_resolve_current_drops_a_superseded_envelope():
    earlier = _pr_evidence(observed_at="2026-10-04T10:00:00Z")
    # The correction is observed EARLIER than what it supersedes, so only
    # the explicit supersedes link (not recency) can make it current.
    correction = _pr_evidence(observed_at="2026-10-04T09:00:00Z", supersedes=earlier.evidence_id)
    result = _resolve([earlier, correction])
    assert result.state == "current"
    assert result.evidence == correction


def test_resolve_current_ignores_supersession_from_another_revision():
    target = _pr_evidence(revision=_SHA1)
    other_revision = _pr_evidence(revision=_SHA2, supersedes=target.evidence_id)
    assert _resolve([target, other_revision]).evidence == target


def test_resolve_current_never_lets_a_weaker_authority_supersede_a_stronger_one():
    observed = _pr_evidence(authority="observed", observed_at="2026-10-04T10:00:00Z")
    assertion = _pr_evidence(
        authority="asserted", observed_at="2026-10-04T11:00:00Z", supersedes=observed.evidence_id,
    )
    assert _resolve([observed, assertion]).evidence == observed
    # Equal authority may supersede.
    correction = _pr_evidence(observed_at="2026-10-04T09:00:00Z", supersedes=observed.evidence_id)
    assert _resolve([observed, correction]).evidence == correction


def test_resolve_current_ranks_authority_above_recency():
    observed = _pr_evidence(authority="observed", observed_at="2026-10-04T10:00:00Z")
    newer_assertion = _pr_evidence(authority="asserted", observed_at="2026-10-04T11:00:00Z")
    newer_derivation = _pr_evidence(authority="derived", observed_at="2026-10-04T12:00:00Z")
    assert _resolve([newer_assertion, observed, newer_derivation]).evidence == observed


def test_resolve_current_prefers_the_later_observation_at_equal_authority():
    older = _pr_evidence(observed_at="2026-10-04T10:00:00Z")
    newer = _pr_evidence(observed_at="2026-10-04T10:00:00.5Z")
    assert _resolve([newer, older]).evidence == newer
    assert _resolve([older, newer]).evidence == newer


def test_resolve_current_reports_a_tie_as_ambiguous():
    a = _seal(subject=_subject(), provenance=_provenance(), outcome=_outcome(code="succeeded"))
    b = _seal(subject=_subject(), provenance=_provenance(), outcome=_outcome(code="failed"))
    result = _resolve([a, b])
    assert result.state == "ambiguous"
    assert result.evidence is None
    # The same envelope listed twice is not a tie.
    assert _resolve([a, a]).evidence == a


def test_resolve_current_over_the_golden_supersession_pair():
    first = _load_fixture_evidence(SHEPHERD_PR_FIXTURE_PATH)
    second = _load_fixture_evidence(SHEPHERD_PR_SUPERSEDING_FIXTURE_PATH)
    assert second.provenance is not None and second.provenance.supersedes == first.evidence_id
    assert first.subject is not None and first.subject == second.subject
    assert second.completeness == ee.INCOMPLETE  # observation_failed on versions
    result = ee.resolve_current(
        [first, second], kind="pull_request", repository="mctlhq/mctl-agents", ref="524",
        revision=first.subject.revision,
    )
    assert result.state == "current"
    assert result.evidence == second
    assert result.state in ee.CURRENT_STATES


# -- review round 1 (PR #575) -----------------------------------------------


@pytest.mark.parametrize(
    "block,overrides",
    (
        (
            "subject",
            {"subject": _subject(kind="branch", ref="feat/rotate-" + _CREDENTIAL), "provenance": _provenance()},
        ),
        ("subject", {"subject": _subject(repository="mctlhq/" + _CREDENTIAL), "provenance": _provenance()}),
        ("versions", {"versions": _versions(agent="sk-" + "a" * 20)}),
    ),
    ids=("subject.ref", "subject.repository", "versions.agent"),
)
def test_a_redacted_free_form_required_leaf_seals_incomplete_instead_of_losing_the_envelope(block, overrides):
    evidence = _seal(**overrides)
    dumped = json.dumps(evidence.to_dict())
    assert _CREDENTIAL not in dumped and "sk-" + "a" * 20 not in dumped
    gaps = [g for g in evidence.gaps if g.block == block and g.code == "redacted_out"]
    assert len(gaps) == 1 and gaps[0].required
    assert evidence.completeness == ee.INCOMPLETE
    assert ee.recompute_content_hash(evidence) == evidence.content_hash
    assert ee.ExecutionEvidence.from_dict(evidence.to_dict()) == evidence


@pytest.mark.parametrize(
    "overrides,match",
    (
        ({"subject": _subject(revision=_CREDENTIAL), "provenance": _provenance()}, r"subject\.revision"),
        ({"tool_calls": [_tool_call(action_digest=_CREDENTIAL)]}, r"tool_call\.action_digest"),
        ({"tool_calls": [_tool_call(kind=_CREDENTIAL)]}, r"tool_call\.kind"),
        ({"provenance": _provenance(observed_at=_CREDENTIAL)}, r"provenance\.observed_at"),
        ({"provenance": _provenance(authority=_CREDENTIAL)}, r"provenance\.authority"),
        ({"versions": _versions(definition_content_hash=_CREDENTIAL)}, r"versions\.definition_content_hash"),
    ),
    ids=("subject.revision", "tool_call.action_digest", "tool_call.kind", "provenance.observed_at",
         "provenance.authority", "versions.definition_content_hash"),
)
def test_a_redacted_vocabulary_or_shape_leaf_is_never_excused(overrides, match):
    # No legitimate value of these leaves can trip _safe(): a credential
    # there is a producer bug and must raise, not seal INCOMPLETE.
    with pytest.raises(ee.ExecutionEvidenceError, match=match):
        _seal(**overrides)


def test_a_sibling_redaction_never_excuses_a_never_supplied_leaf():
    # ref is redacted (excusable) but revision was simply forgotten: seal()'s
    # pre-redaction presence check must still reject it.
    with pytest.raises(ee.ExecutionEvidenceError, match=r"subject\.revision is required"):
        _seal(subject=_subject(ref="524", repository="mctlhq/" + _CREDENTIAL, revision=""), provenance=_provenance())
    # Two excusable free-form leaves: one redacted, one never supplied.
    with pytest.raises(ee.ExecutionEvidenceError, match=r"subject\.repository is required"):
        _seal(
            subject=_subject(kind="branch", ref="feat/x-" + _CREDENTIAL, repository=""), provenance=_provenance()
        )
    with pytest.raises(ee.ExecutionEvidenceError, match=r"versions\.agent is required"):
        _seal(versions=_versions(environment=_CREDENTIAL, agent=""))
    with pytest.raises(ee.ExecutionEvidenceError, match="definition_content_hash is required"):
        _seal(versions=_versions(environment=_CREDENTIAL, definition_content_hash=""))
    with pytest.raises(ee.ExecutionEvidenceError, match=r"tool_call\.status is required"):
        _seal(tool_calls=[_tool_call(name=_CREDENTIAL), _tool_call(status="")])


def test_from_dict_excuses_a_blank_free_form_leaf_only_under_a_declared_redaction_gap():
    # The excusal is declarative (ADR 018 Amendment 2): from_dict trusts a
    # required redacted_out gap as written, for the free-form leaves only.
    sealed = _seal(subject=_subject(kind="branch", ref="feat/x-" + _CREDENTIAL), provenance=_provenance())
    doc = sealed.to_dict()
    assert doc["subject"]["ref"] == ""
    assert ee.ExecutionEvidence.from_dict(doc) == sealed
    # Without the gap, the same blank ref is rejected.
    doc_without_gap = {**doc, "gaps": []}
    with pytest.raises(ee.ExecutionEvidenceError, match=r"subject\.ref is required"):
        ee.ExecutionEvidence.from_dict(doc_without_gap)
    # A declared gap never excuses a blank SHA on a SHA-bound subject.
    forged = json.loads(json.dumps(doc))
    forged["subject"]["ref"] = "feat/x"
    forged["subject"]["revision"] = ""
    with pytest.raises(ee.ExecutionEvidenceError, match=r"subject\.revision is required"):
        ee.ExecutionEvidence.from_dict(forged)


def test_a_blank_required_leaf_with_only_a_non_required_redaction_gap_is_rejected():
    sealed = _seal(subject=_subject(kind="branch", ref="feat/x-" + _CREDENTIAL), provenance=_provenance())
    doc = sealed.to_dict()
    doc["gaps"] = [{**g, "required": False} for g in doc["gaps"]]
    with pytest.raises(ee.ExecutionEvidenceError, match="must be required"):
        ee.ExecutionEvidence.from_dict(doc)


@pytest.mark.parametrize(
    "kind,repository,ref,revision",
    (
        ("pull_request", "mctlhq/mctl-agents", "524", "4f2c9e1"),
        ("pull_request", "mctlhq/mctl-agents", "524", "A" * 40),
        ("pr", "mctlhq/mctl-agents", "524", _SHA1),
        ("PullRequest", "mctlhq/mctl-agents", "524", ""),
        ("issue", "mctlhq/mctl-agents", "199", "has space"),
        ("pull_request", "mctl-agents", "524", _SHA1),
        ("pull_request", "mctlhq/mctl-agents", "#524", _SHA1),
        ("pull_request", "mctlhq/mctl-agents", 524, _SHA1),
    ),
)
def test_resolve_current_rejects_a_malformed_argument_instead_of_answering_no_evidence(
    kind, repository, ref, revision
):
    with pytest.raises(ee.ExecutionEvidenceError):
        ee.resolve_current([_pr_evidence()], kind=kind, repository=repository, ref=ref, revision=revision)


@pytest.mark.parametrize("repository", ("mctlhq/..", "mctlhq/.", "mctlhq/...", "mctlhq/a..b"))
def test_subject_repository_rejects_path_fragments(repository):
    with pytest.raises(ee.ExecutionEvidenceError, match=r"subject\.repository"):
        _seal(subject=_subject(repository=repository), provenance=_provenance())


def test_subject_repository_accepts_a_dot_prefixed_real_name():
    _seal(subject=_subject(repository="mctlhq/.github"), provenance=_provenance())  # must not raise


def test_release_revision_is_bounded_to_a_signed_64_bit_integer():
    _seal(versions=_versions(release_revision=2**63 - 1))  # must not raise
    with pytest.raises(ee.ExecutionEvidenceError, match="release_revision"):
        _seal(versions=_versions(release_revision=2**63))


def test_to_log_dict_reports_the_amendment_2_codes():
    log = _seal(subject=_subject(), provenance=_provenance(), tool_calls=[_tool_call()]).to_log_dict()
    assert log["tool_call_count"] == 1
    assert log["subject_kind"] == "pull_request"
    assert log["authority"] == "observed"
    assert _seal().to_log_dict()["subject_kind"] == ""


def _issue_evidence(revision="", **provenance) -> ee.ExecutionEvidence:
    return _seal(subject=_subject(kind="issue", ref="199", revision=revision), provenance=_provenance(**provenance))


def _resolve_issue(candidates, revision=""):
    return ee.resolve_current(
        candidates, kind="issue", repository="mctlhq/mctl-agents", ref="199", revision=revision,
    )


def test_resolve_current_answers_current_for_an_unversioned_issue_subject():
    older = _issue_evidence(observed_at="2026-10-04T10:00:00Z")
    newer = _issue_evidence(observed_at="2026-10-04T11:00:00Z")
    result = _resolve_issue([older, newer])
    assert result.state == "current"
    assert result.evidence == newer


def test_resolve_current_pools_issue_evidence_by_its_version_token():
    unversioned = _issue_evidence()
    versioned = _issue_evidence(revision="2026-10-04T10:00:00Z")
    assert _resolve_issue([unversioned, versioned]).evidence == unversioned
    assert _resolve_issue([unversioned, versioned], revision="2026-10-04T10:00:00Z").evidence == versioned
    assert _resolve_issue([unversioned], revision="2026-10-04T10:00:00Z").state == "no_evidence"


def test_a_blank_revision_is_unknown_only_for_sha_bound_kinds():
    assert _resolve([_pr_evidence()], revision="").state == "unknown_revision"
    assert _resolve_issue([_issue_evidence()]).state == "current"
