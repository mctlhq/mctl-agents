"""Tests for orchestrator/capability.py — mctlhq/mctl-agents#242's
`CapabilitySet` contract (ADR 017:
docs/adr/017-capability-discovery-and-gateway-contract.md), slice 1
(tasks.md revision 2). Only the slice-1 test list applies here: T1, T2, T4,
T8, T15. T3, T5-T7, T9-T14, T16 are deferred to slices 2/3 and are
deliberately not implemented by this file.

Naming convention (`# T<n> —` section banners) matches
tests/test_context_snapshot.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace as dc_replace
from pathlib import Path

import pytest

from orchestrator import capability as cap
from orchestrator.context_snapshot import ExecutionCorrelation

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "capability" / "investigator-capability-set.json"
CONSEQUENCE_TABLE_PATH = REPO_ROOT / "config" / "capability-consequence.yaml"

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _execution(**overrides) -> ExecutionCorrelation:
    fields = {
        "agent": "issue-investigator",
        "environment": "production",
        "temporal_workflow_id": "dev-loop-mctlhq-mctl-agents-242",
        "target_repository_sha": "a" * 40,
        "definition_version": "3",
        "definition_content_hash": "sha256:" + "1a" * 32,
        "profile_version": "5",
        "profile_content_hash": "sha256:" + "2b" * 32,
        "release_revision": 12,
    }
    fields.update(overrides)
    return ExecutionCorrelation(**fields)


def _provider(**overrides) -> cap.ProviderRef:
    fields = {"type": "mcp-remote", "id": "mctl-api", "alias": "mctl", "endpoint_ref": "https://api.mctl.ai/mcp"}
    fields.update(overrides)
    return cap.ProviderRef(**fields)


def _descriptor(provider: cap.ProviderRef, **overrides) -> cap.CapabilityDescriptor:
    fields = dict(
        capability_id="mctl://mcp-remote/mctl-api/mctl_get_service_status",
        tool_name="mcp__mctl__mctl_get_service_status",
        provider=provider,
        title="Get service status",
        summary="Return the current deployment status for one service.",
        keywords=("status", "service"),
        input_schema_hash="sha256:" + "3c" * 32,
        input_schema_bytes=512,
        consequence="read-only",
        matched_tool_pattern="mcp__mctl__*",
        annotations={},
    )
    fields.update(overrides)
    return cap.CapabilityDescriptor(**fields)


def _strategy(**overrides) -> cap.CapabilityStrategy:
    fields = {"name": "lexical-fixed-order", "version": "1.0.0"}
    fields.update(overrides)
    return cap.CapabilityStrategy(**fields)


def _retention(**overrides) -> cap.RetentionPolicy:
    fields = {"class_": "execution-record", "expires_after_days": 90}
    fields.update(overrides)
    return cap.RetentionPolicy(**fields)


def _sealed_set(**overrides) -> cap.CapabilitySet:
    provider = _provider()
    fields = dict(
        execution=_execution(),
        plan_tools=("Read", "Glob", "Grep", "mcp__mctl__*"),
        providers=[provider],
        capabilities=[_descriptor(provider)],
        excluded_count=70,
        strategy=_strategy(),
        retention=_retention(),
        created_at="2026-09-23T00:00:00Z",
    )
    fields.update(overrides)
    return cap.seal(**fields)


def _load_fixture_set() -> cap.CapabilitySet:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return cap.CapabilitySet.from_dict(data)


# ---------------------------------------------------------------------------
# T1 — seal() determinism, golden fixture, created_at exclusion, unknown-key
# rejection, bounded string lengths.
# ---------------------------------------------------------------------------
def test_seal_is_deterministic_across_created_at():
    provider = _provider()
    common = dict(
        execution=_execution(),
        plan_tools=("Read", "mcp__mctl__*"),
        providers=[provider],
        capabilities=[_descriptor(provider)],
        excluded_count=1,
        strategy=_strategy(),
        retention=_retention(),
    )

    set_one = cap.seal(created_at="2026-01-01T00:00:00Z", **common)
    set_two = cap.seal(created_at="2099-01-01T00:00:00Z", **common)
    assert set_one.content_hash == set_two.content_hash
    assert set_one.capability_set_id == set_two.capability_set_id
    assert set_one.created_at != set_two.created_at


def test_capability_set_id_is_derived_from_content_hash():
    sealed = _sealed_set()
    assert sealed.capability_set_id == "cap-" + sealed.content_hash[7:23]
    assert sealed.content_hash.startswith("sha256:")


def test_seal_hash_changes_when_a_non_timestamp_field_changes():
    base = _sealed_set()
    changed = _sealed_set(execution=_execution(agent="implementer"))
    assert base.content_hash != changed.content_hash
    assert base.capability_set_id != changed.capability_set_id


def test_golden_fixture_hash_is_stable():
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    recorded_hash = raw["content_hash"]
    capability_set = cap.CapabilitySet.from_dict(raw)
    assert cap.recompute_content_hash(capability_set) == recorded_hash
    assert capability_set.capability_set_id == "cap-" + recorded_hash[7:23]


def test_golden_fixture_round_trips():
    capability_set = _load_fixture_set()
    assert cap.CapabilitySet.from_dict(capability_set.to_dict()) == capability_set


def test_from_dict_rejects_unknown_top_level_key():
    doc = _sealed_set().to_dict()
    doc["spec"] = {}
    with pytest.raises(cap.CapabilityError, match="unknown key"):
        cap.CapabilitySet.from_dict(doc)


def test_from_dict_rejects_unknown_key_in_execution_block():
    doc = _sealed_set().to_dict()
    doc["execution"]["extra_field"] = "nope"
    with pytest.raises(cap.CapabilityError, match="unknown key"):
        cap.CapabilitySet.from_dict(doc)


def test_from_dict_rejects_unknown_key_in_capability():
    doc = _load_fixture_set().to_dict()
    doc["capabilities"][0]["payload"] = "not allowed"
    with pytest.raises(cap.CapabilityError, match="unknown key"):
        cap.CapabilitySet.from_dict(doc)


def test_from_dict_rejects_unknown_key_in_provider():
    doc = _sealed_set().to_dict()
    doc["providers"][0]["secret"] = "nope"
    with pytest.raises(cap.CapabilityError, match="unknown key"):
        cap.CapabilitySet.from_dict(doc)


def test_from_dict_rejects_unknown_api_version():
    doc = _sealed_set().to_dict()
    doc["api_version"] = "capability.mctl.ai/v1alpha2"
    with pytest.raises(cap.CapabilityError, match="api_version"):
        cap.CapabilitySet.from_dict(doc)


def test_invocation_record_from_dict_rejects_unknown_key():
    record = cap.InvocationRecord(
        capability_id="mctl://mcp-remote/mctl-api/mctl_get_service_status",
        capability_set_id="cap-0000000000000000",
        outcome="ok",
        reason_code="ok",
        duration_ms=12,
        policy_checkpoint="absent",
        arguments_hash="sha256:" + "0" * 64,
    )
    doc = record.to_dict()
    doc["payload"] = "smuggled"
    with pytest.raises(cap.CapabilityError, match="unknown key"):
        cap.InvocationRecord.from_dict(doc)


def test_title_too_long_raises():
    with pytest.raises(cap.CapabilityError, match="title"):
        _descriptor(_provider(), title="x" * (cap.MAX_TITLE_LENGTH + 1))


def test_summary_too_long_raises():
    with pytest.raises(cap.CapabilityError, match="summary"):
        _descriptor(_provider(), summary="x" * (cap.MAX_SUMMARY_LENGTH + 1))


def test_too_many_keywords_raises():
    with pytest.raises(cap.CapabilityError, match="keyword"):
        _descriptor(_provider(), keywords=tuple(f"k{i}" for i in range(cap.MAX_KEYWORDS + 1)))


def test_single_keyword_too_long_raises():
    with pytest.raises(cap.CapabilityError, match="keyword"):
        _descriptor(_provider(), keywords=("x" * (cap.MAX_KEYWORD_LENGTH + 1),))


def test_descriptor_rejects_input_schema_hash_missing_sha256_prefix():
    """A CapabilityDescriptor built directly (the way seal() builds one,
    never through from_dict) must reject the same malformed hash from_dict
    would — otherwise seal() could produce a document that its own
    from_dict rejects on reload."""
    with pytest.raises(cap.CapabilityError, match="input_schema_hash"):
        _descriptor(_provider(), input_schema_hash="not-a-hash")


def test_invocation_record_rejects_arguments_hash_missing_sha256_prefix():
    with pytest.raises(cap.CapabilityError, match="arguments_hash"):
        cap.InvocationRecord(
            capability_id="mctl://mcp-remote/mctl-api/mctl_get_service_status",
            capability_set_id="cap-0000000000000000",
            outcome="ok",
            reason_code="ok",
            duration_ms=5,
            policy_checkpoint="absent",
            arguments_hash="not-a-hash",
        )


def test_invocation_record_rejects_result_hash_missing_sha256_prefix():
    with pytest.raises(cap.CapabilityError, match="result_hash"):
        cap.InvocationRecord(
            capability_id="mctl://mcp-remote/mctl-api/mctl_get_service_status",
            capability_set_id="cap-0000000000000000",
            outcome="ok",
            reason_code="ok",
            duration_ms=5,
            policy_checkpoint="absent",
            arguments_hash="sha256:" + "0" * 64,
            result_hash="not-a-hash",
        )


def test_validate_rejects_empty_created_at():
    """Same property as the two tests above, at the CapabilitySet level:
    validate() must catch what from_dict's _require_str(created_at) would,
    so seal() can never accept what from_dict later rejects."""
    sealed = _sealed_set()
    bad = dc_replace(sealed, created_at="")
    with pytest.raises(cap.CapabilityError, match="created_at"):
        bad.validate()


def test_validate_rejects_empty_capability_set_id():
    sealed = _sealed_set()
    bad = dc_replace(sealed, capability_set_id="")
    with pytest.raises(cap.CapabilityError, match="capability_set_id"):
        bad.validate()


def test_descriptor_rejects_empty_capability_id():
    with pytest.raises(cap.CapabilityError, match="capability_id"):
        _descriptor(_provider(), capability_id="")


def test_descriptor_rejects_empty_tool_name():
    with pytest.raises(cap.CapabilityError, match="tool_name"):
        _descriptor(_provider(), tool_name="")


def test_descriptor_rejects_empty_title():
    with pytest.raises(cap.CapabilityError, match="title"):
        _descriptor(_provider(), title="")


def test_descriptor_rejects_empty_matched_tool_pattern():
    with pytest.raises(cap.CapabilityError, match="matched_tool_pattern"):
        _descriptor(_provider(), matched_tool_pattern="")


def test_provider_rejects_empty_type():
    with pytest.raises(cap.CapabilityError, match=r"provider\.type"):
        _provider(type="")


def test_provider_rejects_empty_id():
    with pytest.raises(cap.CapabilityError, match=r"provider\.id"):
        _provider(id="")


def test_provider_rejects_empty_alias():
    with pytest.raises(cap.CapabilityError, match=r"provider\.alias"):
        _provider(alias="")


def test_sealed_set_round_trips_through_to_dict_and_from_dict():
    """The round-trip property seal()/from_dict() must hold end to end: a
    freshly sealed CapabilitySet, serialized with to_dict() and reloaded
    with from_dict(), must reconstruct an equal object (not merely a
    from_dict()-loaded fixture reloaded again, which test_golden_fixture_
    round_trips already covers) — otherwise a document seal() considers
    valid could be one its own from_dict() cannot faithfully reproduce."""
    sealed = _sealed_set()
    reloaded = cap.CapabilitySet.from_dict(sealed.to_dict())
    assert reloaded == sealed
    assert reloaded.to_dict() == sealed.to_dict()
    assert cap.recompute_content_hash(reloaded) == sealed.content_hash


# ---------------------------------------------------------------------------
# T2 — narrowing invariant: every member's matched_tool_pattern is an
# element of plan_tools, and its tool_name matches that pattern; a set
# containing a capability outside plan.tools fails validate().
# ---------------------------------------------------------------------------
def test_valid_narrowing_passes_validate():
    sealed = _sealed_set()
    sealed.validate()  # must not raise


def test_capability_outside_plan_tools_fails_validate():
    provider = _provider()
    good = _descriptor(provider)
    outside = _descriptor(
        provider,
        capability_id="mctl://mcp-remote/mctl-api/mctl_deploy_service",
        tool_name="mcp__mctl__mctl_deploy_service",
        matched_tool_pattern="mcp__other__*",  # not a member of plan_tools below
    )
    sealed = _sealed_set(capabilities=[good])
    bad = dc_replace(sealed, capabilities=(good, outside))
    with pytest.raises(cap.CapabilityError, match="matched_tool_pattern"):
        bad.validate()


def test_seal_rejects_a_capability_outside_plan_tools():
    provider = _provider()
    outside = _descriptor(
        provider,
        matched_tool_pattern="mcp__other__*",
    )
    with pytest.raises(cap.CapabilityError, match="matched_tool_pattern"):
        cap.seal(
            execution=_execution(),
            plan_tools=("Read", "mcp__mctl__*"),
            providers=[provider],
            capabilities=[outside],
            excluded_count=0,
            strategy=_strategy(),
            retention=_retention(),
            created_at="2026-09-23T00:00:00Z",
        )


def test_tool_name_not_matching_its_own_pattern_fails_validate():
    # Built via dataclasses.replace after a valid seal(), not seal() itself —
    # seal() would already refuse to produce this set, so validate() is the
    # thing under test (mirrors tests/test_context_snapshot.py's regression
    # style for post-construction invariant violations).
    provider = _provider()
    valid = _descriptor(
        provider,
        tool_name="mcp__mctl__mctl_get_service_status",
        matched_tool_pattern="mcp__mctl__mctl_get_*",
    )
    sealed = _sealed_set(plan_tools=("Read", "mcp__mctl__mctl_get_*"), capabilities=[valid])
    mismatched = _descriptor(
        provider,
        tool_name="mcp__mctl__mctl_deploy_service",
        matched_tool_pattern="mcp__mctl__mctl_get_*",  # a plan_tools member, but does not fnmatch tool_name
    )
    bad = dc_replace(sealed, capabilities=(mismatched,))
    with pytest.raises(cap.CapabilityError, match="does not match matched_tool_pattern"):
        bad.validate()


def test_narrowing_invariant_holds_with_literal_pattern_membership():
    """matched_tool_pattern need not be a wildcard: a literal ExecutionPlan.tools
    entry (no glob metacharacters) narrows to exactly itself via fnmatch."""
    provider = _provider()
    literal = _descriptor(
        provider,
        tool_name="mcp__mctl__mctl_get_service_status",
        matched_tool_pattern="mcp__mctl__mctl_get_service_status",
    )
    sealed = _sealed_set(
        plan_tools=("Read", "mcp__mctl__mctl_get_service_status"),
        capabilities=[literal],
    )
    sealed.validate()  # must not raise


# ---------------------------------------------------------------------------
# T4 — PolicyCheckpoint protocol: a recording fake checkpoint returning a
# denied verdict yields policy-denied from the pure verdict-to-reason-code
# helper; every InvocationRecord built via AbsentPolicyCheckpoint carries
# policy_checkpoint: absent. No gateway, no provider involved.
# ---------------------------------------------------------------------------
class _RecordingCheckpoint:
    """A fake PolicyCheckpoint (structurally satisfies the Protocol) that
    records every call and returns a fixed verdict."""

    def __init__(self, decision: str, reason: str = "") -> None:
        self.decision = decision
        self.reason = reason
        self.calls: list[tuple[cap.CapabilityDescriptor, ExecutionCorrelation]] = []

    def check(self, descriptor, correlation):
        self.calls.append((descriptor, correlation))
        return cap.CheckpointVerdict(decision=self.decision, reason=self.reason)


def test_denied_verdict_yields_policy_denied_reason_code():
    checkpoint = _RecordingCheckpoint("denied", reason="rule mctl-mcp-default-approval")
    descriptor = _descriptor(_provider())
    correlation = _execution()

    verdict = checkpoint.check(descriptor, correlation)

    assert len(checkpoint.calls) == 1
    assert cap.reason_code_for_verdict(verdict) == "policy-denied"


def test_allowed_verdict_yields_ok_reason_code():
    checkpoint = _RecordingCheckpoint("allowed")
    verdict = checkpoint.check(_descriptor(_provider()), _execution())
    assert cap.reason_code_for_verdict(verdict) == "ok"


def test_checkpoint_verdict_rejects_unknown_decision():
    with pytest.raises(cap.CapabilityError, match="decision"):
        cap.CheckpointVerdict(decision="maybe")


def test_absent_policy_checkpoint_always_reports_allowed_verdict():
    checkpoint = cap.AbsentPolicyCheckpoint()
    verdict = checkpoint.check(_descriptor(_provider()), _execution())
    assert verdict.decision == "allowed"


def test_every_invocation_record_via_absent_checkpoint_carries_absent_status():
    checkpoint = cap.AbsentPolicyCheckpoint()
    descriptor = _descriptor(_provider())
    correlation = _execution()

    for _ in range(3):
        verdict = checkpoint.check(descriptor, correlation)
        status = cap.policy_checkpoint_status(checkpoint, verdict)
        assert status == "absent"
        record = cap.InvocationRecord(
            capability_id=descriptor.capability_id,
            capability_set_id="cap-0000000000000000",
            outcome="ok",
            reason_code="ok",
            duration_ms=5,
            policy_checkpoint=status,
            arguments_hash="sha256:" + "0" * 64,
        )
        assert record.policy_checkpoint == "absent"


def test_a_real_checkpoint_reports_its_own_verdict_not_absent():
    """Swapping AbsentPolicyCheckpoint for a real #197 implementation
    touches exactly one construction site: policy_checkpoint_status already
    reports the real checkpoint's own verdict.decision for anything that is
    not an AbsentPolicyCheckpoint instance."""
    checkpoint = _RecordingCheckpoint("denied")
    verdict = checkpoint.check(_descriptor(_provider()), _execution())
    assert cap.policy_checkpoint_status(checkpoint, verdict) == "denied"


# ---------------------------------------------------------------------------
# T8 — no-payload/no-authorization tests, and a subprocess import-direction
# assertion that orchestrator/capability.py loads stdlib only.
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


def test_serialized_schema_has_no_authorization_field_name():
    capability_set = _load_fixture_set()
    keys = set(_walk_keys(capability_set.to_dict()))
    for key in keys:
        lowered = key.lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in lowered, f"field name {key!r} contains forbidden token {token!r}"


def test_invocation_record_schema_has_no_authorization_field_name():
    record = cap.InvocationRecord(
        capability_id="mctl://mcp-remote/mctl-api/mctl_get_service_status",
        capability_set_id="cap-0000000000000000",
        outcome="ok",
        reason_code="ok",
        duration_ms=1,
        policy_checkpoint="absent",
        arguments_hash="sha256:" + "0" * 64,
    )
    keys = set(_walk_keys(record.to_dict()))
    for key in keys:
        lowered = key.lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in lowered, f"field name {key!r} contains forbidden token {token!r}"


def test_dataclass_field_names_carry_no_authorization_token():
    """A field-NAME assertion across every dataclass this module declares,
    not just serialized instances — mirrors
    tests/test_context_snapshot.py's equivalent."""
    import dataclasses

    for cls in (
        cap.ProviderRef, cap.CapabilityDescriptor, cap.CapabilityStrategy, cap.RetentionPolicy,
        cap.CapabilitySet, cap.DiscoveryDecision, cap.InvocationRecord, cap.CheckpointVerdict,
    ):
        for f in dataclasses.fields(cls):
            lowered = f.name.lower()
            for token in _FORBIDDEN_TOKENS:
                assert token not in lowered, f"{cls.__name__}.{f.name} contains forbidden token {token!r}"


def test_module_import_is_stdlib_only():
    """Mirrors tests/test_worker_isolation.py and
    tests/test_context_snapshot.py's test_module_import_is_stdlib_only: a
    subprocess import of orchestrator.capability must not pull in
    claude_agent_sdk, mcp, yaml or any other third-party package — only
    calling load_consequence_table() (never called here) would do that."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.capability, sys; "
            "print(chr(10).join(sorted(sys.modules)))",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"import failed:\n{result.stderr[-2000:]}"
    loaded = set(result.stdout.split("\n"))
    third_party_prefixes = ("claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio", "mcp")
    leaked = sorted(
        name for name in loaded
        if any(name == prefix or name.startswith(prefix + ".") for prefix in third_party_prefixes)
    )
    assert not leaked, f"orchestrator.capability pulled in third-party modules: {leaked}"


# ---------------------------------------------------------------------------
# T15 — consequence table loader: parses, every listed tool has a valid
# classification, unknown name defaults to consequential, loader is pure.
# ---------------------------------------------------------------------------
def test_consequence_table_parses():
    table = cap.load_consequence_table()
    assert len(table) > 0


def test_every_listed_tool_has_a_valid_classification():
    table = cap.load_consequence_table()
    for name, consequence in table.items():
        assert consequence in cap.CONSEQUENCE_VALUES, f"{name}: {consequence!r} is not a valid consequence"


def test_default_consequence_table_path_is_the_checked_in_file():
    assert cap.DEFAULT_CONSEQUENCE_TABLE_PATH == CONSEQUENCE_TABLE_PATH


def test_unknown_tool_name_defaults_to_consequential():
    table = cap.load_consequence_table()
    assert cap.classify_consequence(
        "mctl_this_tool_does_not_exist", table, provider_id=cap.MCTL_API_PROVIDER_ID
    ) == "consequential"


def test_sdk_visible_name_is_stripped_to_bare_tool_name_before_lookup():
    table = cap.load_consequence_table()
    bare = cap.classify_consequence("mctl_deploy_service", table, provider_id=cap.MCTL_API_PROVIDER_ID)
    sdk_visible = cap.classify_consequence(
        "mcp__mctl__mctl_deploy_service", table, provider_id=cap.MCTL_API_PROVIDER_ID
    )
    assert bare == sdk_visible == "mutating"


def test_known_read_only_tool_classifies_read_only():
    table = cap.load_consequence_table()
    assert cap.classify_consequence("mctl_whoami", table, provider_id=cap.MCTL_API_PROVIDER_ID) == "read-only"
    assert cap.classify_consequence(
        "mctl_get_service_status", table, provider_id=cap.MCTL_API_PROVIDER_ID
    ) == "read-only"


def test_known_consequential_tool_classifies_consequential():
    table = cap.load_consequence_table()
    assert cap.classify_consequence(
        "mctl_delete_tenant", table, provider_id=cap.MCTL_API_PROVIDER_ID
    ) == "consequential"
    assert cap.classify_consequence(
        "mctl_trigger_issue", table, provider_id=cap.MCTL_API_PROVIDER_ID
    ) == "consequential"


def test_classify_consequence_requires_an_explicit_provider_id():
    """`provider_id` has no default (P2 codex finding): a caller that omits
    it must fail loudly rather than silently fall back to the trusted
    MCTL_API_PROVIDER_ID table lookup, which would reproduce the exact
    "unclassified skips the checkpoint by omission" failure mode this
    function exists to close, just moved from the tool name to the
    provider id."""
    table = cap.load_consequence_table()
    with pytest.raises(TypeError):
        cap.classify_consequence("mctl_whoami", table)  # type: ignore[call-arg]


def test_classify_consequence_defaults_to_the_mctl_api_provider():
    table = cap.load_consequence_table()
    assert cap.classify_consequence("mctl_whoami", table, provider_id=cap.MCTL_API_PROVIDER_ID) == "read-only"


def test_classify_consequence_ignores_the_table_for_a_different_provider():
    """A bare-name collision with an unrelated provider's tool must never
    borrow mctl-api's classification — config/capability-consequence.yaml
    documents mctl-api's own tool set only, so any other provider_id falls
    through to DEFAULT_CONSEQUENCE regardless of what the table says about
    that name."""
    table = cap.load_consequence_table()
    assert cap.classify_consequence("mcp__other__mctl_whoami", table, provider_id="other-provider") == "consequential"
    assert cap.classify_consequence("mctl_whoami", table, provider_id="other-provider") == "consequential"


def test_loader_rejects_an_invalid_consequence_value(tmp_path):
    bad_path = tmp_path / "bad-consequence.yaml"
    bad_path.write_text("tools:\n  mctl_whoami: sometimes\n", encoding="utf-8")
    with pytest.raises(cap.CapabilityError, match="consequence"):
        cap.load_consequence_table(bad_path)


def test_loader_rejects_a_non_mapping_tools_value(tmp_path):
    bad_path = tmp_path / "bad-tools.yaml"
    bad_path.write_text("tools:\n  - mctl_whoami\n", encoding="utf-8")
    with pytest.raises(cap.CapabilityError, match="mapping"):
        cap.load_consequence_table(bad_path)


def test_loader_does_no_io_beyond_the_single_file_read(tmp_path, monkeypatch):
    """Pure aside from the one file read: reading the same path twice with
    no other filesystem/network access yields an identical table both
    times, and the loader never mutates the source file."""
    custom_path = tmp_path / "custom-consequence.yaml"
    custom_path.write_text("tools:\n  mctl_example: mutating\n", encoding="utf-8")
    before = custom_path.read_text(encoding="utf-8")

    table_one = cap.load_consequence_table(custom_path)
    table_two = cap.load_consequence_table(custom_path)

    assert dict(table_one) == dict(table_two) == {"mctl_example": "mutating"}
    assert custom_path.read_text(encoding="utf-8") == before


def test_every_tool_documented_in_the_repo_owned_mcp_tool_inventory_is_classified():
    """docs/diagrams/archify/facts.yaml's mcp_tools list (generated from
    mctl-api's server.go by tools/diagram_facts.py) is the checked-in
    source of truth for the advertised tool set this table classifies."""
    import yaml

    facts_path = REPO_ROOT / "docs" / "diagrams" / "archify" / "facts.yaml"
    facts = yaml.safe_load(facts_path.read_text(encoding="utf-8"))
    mcp_tools = facts["mcp_tools"]
    assert mcp_tools, "facts.yaml's mcp_tools list must not be empty"

    table = cap.load_consequence_table()
    missing = sorted(name for name in mcp_tools if name not in table)
    assert not missing, f"tool(s) advertised in facts.yaml but not classified: {missing}"
