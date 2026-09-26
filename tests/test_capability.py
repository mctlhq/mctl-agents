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


_STRATEGY_SHAPES = {
    "with-ranker": ({"ranker_name": "bm25", "ranker_version": "0.1.0"}, None),
    "ranker-none": ({"ranker_name": None, "ranker_version": None}, None),
    "ranker-name-only": ({"ranker_name": "bm25"}, None),
    "empty-name": ({"name": ""}, r"strategy\.name"),
    "empty-version": ({"version": ""}, r"strategy\.version"),
    "empty-ranker-name": ({"ranker_name": ""}, r"strategy\.ranker_name"),
    "empty-ranker-version": ({"ranker_version": ""}, r"strategy\.ranker_version"),
    "non-str-ranker-name": ({"ranker_name": 1}, r"strategy\.ranker_name"),
}


@pytest.mark.parametrize("overrides,rejected", _STRATEGY_SHAPES.values(), ids=_STRATEGY_SHAPES.keys())
def test_strategy_is_sealable_exactly_when_it_round_trips(overrides, rejected):
    """The round-trip property, per strategy shape: an accepted strategy
    survives strategy -> seal -> to_dict -> JSON -> from_dict unchanged, and
    a rejected one fails at construction, before anything is sealed, with
    the same error from_dict gives for that document."""
    if rejected is None:
        sealed = _sealed_set(strategy=_strategy(**overrides))
        reloaded = cap.CapabilitySet.from_dict(json.loads(json.dumps(sealed.to_dict())))
        assert reloaded == sealed
        assert reloaded.strategy == sealed.strategy
        assert cap.recompute_content_hash(reloaded) == sealed.content_hash
        return
    with pytest.raises(cap.CapabilityError, match=rejected):
        _strategy(**overrides)
    document = _sealed_set().to_dict()
    document["strategy"].update(overrides)
    with pytest.raises(cap.CapabilityError, match=rejected):
        cap.CapabilitySet.from_dict(document)


@pytest.mark.parametrize(
    "overrides,rejected",
    [
        ({"excluded_count": True}, "excluded_count"),
        ({"plan_tools": ("Read", 1, "mcp__mctl__*")}, r"plan_tools"),
        (
            {"retention": lambda: cap.RetentionPolicy(class_="execution-record", expires_after_days=True)},
            "expires_after_days",
        ),
        ({"capabilities": lambda: [_descriptor(_provider(), input_schema_bytes=True)]}, "input_schema_bytes"),
        ({"capabilities": lambda: [_descriptor(_provider(), summary=1)]}, "summary"),
        ({"providers": lambda: [_provider(endpoint_ref=1)]}, "endpoint_ref"),
        ({"providers": lambda: [_provider(id=1)]}, r"provider\.id"),
        ({"created_at": 1}, "created_at"),
    ],
    ids=[
        "bool-excluded-count", "non-str-plan-tool", "bool-expiry", "bool-schema-bytes", "non-str-summary",
        "non-str-endpoint-ref", "non-str-provider-id", "non-str-created-at",
    ],
)
def test_seal_refuses_what_from_dict_would_refuse(overrides, rejected):
    """Construction or seal() refuses it, always as CapabilityError (never a
    bare TypeError), so no sealed document can fail its own from_dict.

    Every override that needs a bad OBJECT (rather than a bad primitive) is
    a zero-argument callable, invoked only inside `pytest.raises` below —
    never as a bare parametrize literal. `CapabilityDescriptor`/
    `ProviderRef`/`RetentionPolicy` now raise `CapabilityError` straight
    from their own `__post_init__` for exactly these bad shapes (R1-R3,
    review follow-ups from mctl-agents#485's slice-1 review); constructing
    one eagerly, as a parametrize literal evaluated at collection time,
    would fail test COLLECTION instead of the test itself — the sentinel-
    indirection workaround this replaces.
    """
    with pytest.raises(cap.CapabilityError, match=rejected):
        resolved = {k: (v() if callable(v) else v) for k, v in overrides.items()}
        _sealed_set(**resolved)


# ---------------------------------------------------------------------------
# R1 — CapabilitySet.validate() internal consistency (review follow-ups from
# #485, mctl-agents#242 design.md "Contract hardening"): tool_name derives
# from capability_id + provider.alias, every capability's provider is a
# member of the set's providers, and the collision check (two capabilities
# resolving to one tool_name, or two providers claiming one alias).
# ---------------------------------------------------------------------------
def test_validate_rejects_tool_name_inconsistent_with_capability_id_and_alias():
    provider = _provider()
    bad = _descriptor(provider, tool_name="mcp__mctl__mctl_wrong_tool")
    with pytest.raises(cap.CapabilityError, match="does not match the name derived"):
        cap.seal(
            execution=_execution(), plan_tools=("Read", "mcp__mctl__*"), providers=[provider],
            capabilities=[bad], excluded_count=0, strategy=_strategy(), retention=_retention(),
            created_at="2026-09-23T00:00:00Z",
        )


def test_validate_rejects_capability_id_mismatched_with_its_provider():
    provider = _provider()
    bad = _descriptor(provider, capability_id="mctl://mcp-remote/some-other-provider/mctl_get_service_status")
    with pytest.raises(cap.CapabilityError, match="does not match its provider"):
        cap.seal(
            execution=_execution(), plan_tools=("Read", "mcp__mctl__*"), providers=[provider],
            capabilities=[bad], excluded_count=0, strategy=_strategy(), retention=_retention(),
            created_at="2026-09-23T00:00:00Z",
        )


def test_validate_rejects_a_malformed_capability_id():
    provider = _provider()
    bad = _descriptor(provider, capability_id="mctl://mcp-remote/mctl-api")  # missing the <tool> segment
    with pytest.raises(cap.CapabilityError, match="must have the shape"):
        cap.seal(
            execution=_execution(), plan_tools=("Read", "mcp__mctl__*"), providers=[provider],
            capabilities=[bad], excluded_count=0, strategy=_strategy(), retention=_retention(),
            created_at="2026-09-23T00:00:00Z",
        )


def test_validate_rejects_a_capability_whose_provider_is_not_a_set_member():
    member = _provider()
    stray = _provider(id="stray-provider", alias="stray")
    bad = _descriptor(
        stray,
        capability_id="mctl://mcp-remote/stray-provider/mctl_get_service_status",
        tool_name="mcp__stray__mctl_get_service_status",
        matched_tool_pattern="mcp__stray__*",
    )
    with pytest.raises(cap.CapabilityError, match="is not a member of this set's providers"):
        cap.seal(
            execution=_execution(), plan_tools=("Read", "mcp__stray__*"), providers=[member],
            capabilities=[bad], excluded_count=0, strategy=_strategy(), retention=_retention(),
            created_at="2026-09-23T00:00:00Z",
        )


def test_validate_rejects_two_capabilities_resolving_to_the_same_tool_name():
    provider_a = cap.ProviderRef(type="sdk-builtin", id="sdk-a", alias="sdk-a")
    provider_b = cap.ProviderRef(type="sdk-builtin", id="sdk-b", alias="sdk-b")
    capability_a = _descriptor(
        provider_a, capability_id="mctl://sdk-builtin/sdk-a/toolX", tool_name="toolX", matched_tool_pattern="toolX",
    )
    capability_b = _descriptor(
        provider_b, capability_id="mctl://sdk-builtin/sdk-b/toolX", tool_name="toolX", matched_tool_pattern="toolX",
    )
    with pytest.raises(cap.CapabilityCollisionError, match="resolve to tool_name"):
        cap.seal(
            execution=_execution(), plan_tools=("toolX",), providers=[provider_a, provider_b],
            capabilities=[capability_a, capability_b], excluded_count=0, strategy=_strategy(),
            retention=_retention(), created_at="2026-09-23T00:00:00Z",
        )


def test_validate_rejects_two_providers_claiming_the_same_alias():
    provider_a = cap.ProviderRef(type="mcp-remote", id="provider-a", alias="dup")
    provider_b = cap.ProviderRef(type="mcp-remote", id="provider-b", alias="dup")
    with pytest.raises(cap.CapabilityCollisionError, match="claim alias"):
        cap.seal(
            execution=_execution(), plan_tools=("Read",), providers=[provider_a, provider_b],
            capabilities=[], excluded_count=0, strategy=_strategy(), retention=_retention(),
            created_at="2026-09-23T00:00:00Z",
        )


def test_capability_collision_error_is_a_capability_error_with_a_fixed_reason_code():
    assert issubclass(cap.CapabilityCollisionError, cap.CapabilityError)
    assert cap.CapabilityCollisionError("boom").reason_code == "collision"
    assert "collision" in cap.REASON_CODES


# ---------------------------------------------------------------------------
# R2 — numeric bounds: negative rank/input_schema_bytes/expires_after_days
# are rejected; _optional_float rejects nan/inf.
# ---------------------------------------------------------------------------
def test_discovery_decision_rejects_negative_rank():
    with pytest.raises(cap.CapabilityError, match="rank"):
        cap.DiscoveryDecision(capability_id="cap-1", rank=-1, reason_code="ok", included=True)


def test_descriptor_rejects_negative_input_schema_bytes():
    with pytest.raises(cap.CapabilityError, match="input_schema_bytes"):
        _descriptor(_provider(), input_schema_bytes=-1)


def test_retention_policy_rejects_negative_expires_after_days():
    with pytest.raises(cap.CapabilityError, match="expires_after_days"):
        cap.RetentionPolicy(class_="execution-record", expires_after_days=-1)


def test_discovery_decision_score_rejects_nan():
    with pytest.raises(cap.CapabilityError, match="score"):
        cap.DiscoveryDecision(capability_id="cap-1", rank=0, reason_code="ok", included=True, score=float("nan"))


def test_discovery_decision_score_rejects_infinity():
    with pytest.raises(cap.CapabilityError, match="score"):
        cap.DiscoveryDecision(capability_id="cap-1", rank=0, reason_code="ok", included=True, score=float("inf"))


# ---------------------------------------------------------------------------
# R3 — error-type consistency: wrong-typed keywords/annotations raise
# CapabilityError (never TypeError); _require_str(allow_empty=True) no
# longer says "non-empty"; RetentionPolicy.__post_init__ enforces what
# from_dict enforces; a YAML syntax error is wrapped in CapabilityError.
# ---------------------------------------------------------------------------
def test_descriptor_rejects_a_non_iterable_keywords_value():
    with pytest.raises(cap.CapabilityError, match="keywords"):
        _descriptor(_provider(), keywords=42)


def test_descriptor_rejects_a_non_mapping_annotations_value():
    with pytest.raises(cap.CapabilityError, match="annotations"):
        _descriptor(_provider(), annotations=["not", "a", "mapping"])


def test_require_str_allow_empty_message_omits_non_empty_wording():
    with pytest.raises(cap.CapabilityError) as exc_info:
        cap.ProviderRef(type="mcp-remote", id="p", alias="a", endpoint_ref=123)
    message = str(exc_info.value)
    assert "must be a string" in message
    assert "non-empty" not in message


def test_retention_policy_direct_construction_rejects_what_from_dict_would():
    """RetentionPolicy.__post_init__ enforces the same rules from_dict does,
    so a direct construction (the way seal()'s caller builds one) can never
    produce a value its own from_dict would reject on reload."""
    with pytest.raises(cap.CapabilityError, match=r"retention\.class"):
        cap.RetentionPolicy(class_="", expires_after_days=1)
    with pytest.raises(cap.CapabilityError, match="expires_after_days"):
        cap.RetentionPolicy(class_="execution-record", expires_after_days="90")  # type: ignore[arg-type]


def test_loader_wraps_a_yaml_syntax_error_in_capability_error(tmp_path):
    bad_path = tmp_path / "syntax-error.yaml"
    bad_path.write_text("tools:\n  mctl_whoami: [unterminated\n", encoding="utf-8")
    with pytest.raises(cap.CapabilityError, match="invalid YAML"):
        cap.load_consequence_table(bad_path)


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


def test_classify_consequence_uses_the_table_for_the_mctl_api_provider():
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


def test_loader_rejects_an_unhashable_key(tmp_path):
    bad_path = tmp_path / "complex.yaml"
    bad_path.write_text("tools:\n  ? [a, b]\n  : read-only\n", encoding="utf-8")
    with pytest.raises(cap.CapabilityError, match="unhashable key"):
        cap.load_consequence_table(bad_path)


def test_invocation_record_refuses_a_bool_duration():
    with pytest.raises(cap.CapabilityError, match="duration_ms"):
        cap.InvocationRecord(
            capability_id="cap-1", capability_set_id="cap-2", outcome="ok", reason_code="ok",
            duration_ms=True, policy_checkpoint="absent", arguments_hash="sha256:" + "0" * 64,
        )


def test_loader_rejects_a_duplicated_tool(tmp_path):
    bad_path = tmp_path / "dup.yaml"
    bad_path.write_text("tools:\n  mctl_whoami: read-only\n  mctl_whoami: consequential\n", encoding="utf-8")
    with pytest.raises(cap.CapabilityError, match="duplicate key 'mctl_whoami'"):
        cap.load_consequence_table(bad_path)


def test_discovery_decision_round_trips_and_normalizes_score():
    for score, expected in [(None, None), (3, 3.0), (0.25, 0.25)]:
        decision = cap.DiscoveryDecision(
            capability_id="cap-1", rank=1, reason_code="ok", included=True, score=score
        )
        assert decision.score == expected
        assert type(decision.score) is (float if expected is not None else type(None))
        assert cap.DiscoveryDecision.from_dict(json.loads(json.dumps(decision.to_dict()))) == decision


@pytest.mark.parametrize(
    "overrides,rejected",
    [
        ({"score": True}, "score"),
        ({"score": "0.5"}, "score"),
        ({"reason_code": "because"}, "reason_code"),
        ({"rank": "1"}, "rank"),
        ({"included": 1}, "included"),
        ({"surplus": 1}, "surplus"),
    ],
    ids=["bool-score", "str-score", "unknown-reason", "str-rank", "int-included", "unknown-key"],
)
def test_discovery_decision_from_dict_rejects(overrides, rejected):
    document = {"capability_id": "cap-1", "rank": 1, "score": None, "reason_code": "ok", "included": True}
    document.update(overrides)
    with pytest.raises(cap.CapabilityError, match=rejected):
        cap.DiscoveryDecision.from_dict(document)


def test_to_log_dict_carries_ids_hashes_and_counts_only():
    sealed = _sealed_set(strategy=_strategy(ranker_name="bm25", ranker_version="0.1.0"))
    log = sealed.to_log_dict()
    assert log == {
        "capability_set_id": sealed.capability_set_id,
        "content_hash": sealed.content_hash,
        "strategy_name": "lexical-fixed-order",
        "strategy_version": "1.0.0",
        "ranker_name": "bm25",
        "ranker_version": "0.1.0",
        "provider_count": 1,
        "capability_count": 1,
        "excluded_count": 70,
    }
    rendered = json.dumps(log)
    for capability in sealed.capabilities:
        for leaked in (capability.tool_name, capability.title, capability.summary, capability.provider.alias):
            if leaked:
                assert leaked not in rendered
