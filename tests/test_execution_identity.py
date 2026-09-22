"""Tests for orchestrator/execution_identity.py — mctlhq/mctl-agents#196's
`ExecutionContext` contract (ADR 011:
docs/adr/011-execution-identity-contract.md). T1-T8/T10 below map onto that
proposal's tasks.md "## Tests" section; see execution_identity.py's module
docstring for the fail-closed contract every negative test here asserts.

Naming convention (`# T<n> —` section banners) matches
tests/test_context_snapshot.py.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import execution_identity as ei

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "identity" / "investigator-context.json"

# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _actor(**overrides) -> ei.Actor:
    fields = {"type": "github_user", "id": "octocat", "verification": "control-plane-verified"}
    fields.update(overrides)
    return ei.Actor(**fields)


def _executor(**overrides) -> ei.Executor:
    fields = {
        "type": "issue-investigator",
        "id": "",
        "agent": "issue-investigator",
        "version": "1.0.0",
        "image_ref": "ghcr.io/mctlhq/issue-investigator@sha256:" + "b" * 64,
        "binding": "mctl-agents-investigate-fake:issue-investigator",
    }
    fields.update(overrides)
    return ei.Executor(**fields)


def _scope(**overrides) -> ei.Scope:
    fields = {
        "environment": "production",
        "tenant": "mctlhq",
        "repository": "mctlhq/mctl-agents",
        "target_repository_sha": "c" * 40,
        "service": "mctl-agents",
        "slug": "",
    }
    fields.update(overrides)
    return ei.Scope(**fields)


def _trigger(**overrides) -> ei.Trigger:
    fields = {"type": "github_issue", "ref": "https://github.com/mctlhq/mctl-agents/issues/196"}
    fields.update(overrides)
    return ei.Trigger(**fields)


def _correlation(**overrides) -> ei.Correlation:
    fields = {
        "temporal_workflow_id": "dev-loop-mctlhq-mctl-agents-196",
        "temporal_run_id": "run-1",
        "argo_workflow_name": "mctl-agents-investigate-fake",
        "attempt": 0,
    }
    fields.update(overrides)
    return ei.Correlation(**fields)


def _assertions(**overrides) -> ei.Assertions:
    fields = {
        "asserted_by": "control-plane",
        "asserted_fields": ("actor", "executor.type", "correlation"),
        "declared_fields": ("scope.target_repository_sha",),
    }
    fields.update(overrides)
    return ei.Assertions(**fields)


def _context(**overrides) -> ei.ExecutionContext:
    fields = dict(
        trace_id="a" * 32,
        workflow_type="investigate",
        actor=_actor(),
        executor=_executor(),
        scope=_scope(),
        trigger=_trigger(),
        correlation=_correlation(),
        assertions=_assertions(),
        issued_at="2026-09-19T00:00:00Z",
    )
    fields.update(overrides)
    return ei.seal(**fields)


def _load_fixture_context() -> ei.ExecutionContext:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return ei.ExecutionContext.from_dict(data)


# ---------------------------------------------------------------------------
# T1 — round-trip: from_dict(to_dict(context)) == context; seal() derives
# content_hash/context_id, never random.
# ---------------------------------------------------------------------------
def test_round_trip_investigator_fixture():
    context = _load_fixture_context()
    assert ei.ExecutionContext.from_dict(context.to_dict()) == context


def test_round_trip_minimal_context():
    context = _context()
    assert ei.ExecutionContext.from_dict(context.to_dict()) == context


def test_context_id_is_derived_from_content_hash():
    context = _context()
    assert context.context_id == "ex-" + context.content_hash[7:23]


def test_seal_is_deterministic_across_issued_at():
    a = _context(issued_at="2026-01-01T00:00:00Z")
    b = _context(issued_at="2026-12-31T23:59:59Z")
    assert a.content_hash == b.content_hash
    assert a.context_id == b.context_id


def test_seal_hash_changes_when_a_non_timestamp_field_changes():
    a = _context()
    b = _context(workflow_type="implement")
    assert a.content_hash != b.content_hash
    assert a.context_id != b.context_id


def test_golden_fixture_hash_is_stable():
    context = _load_fixture_context()
    assert ei.recompute_content_hash(context) == context.content_hash
    assert context.context_id == "ex-" + context.content_hash[7:23]


# ---------------------------------------------------------------------------
# T2 — fail-closed validation: unknown api_version/kind, unknown keys, and
# every out-of-vocabulary value raise ExecutionIdentityError.
# ---------------------------------------------------------------------------
def test_from_dict_rejects_unknown_api_version():
    data = _context().to_dict()
    data["api_version"] = "identity.mctl.ai/v2"
    with pytest.raises(ei.ExecutionIdentityError, match="unsupported api_version"):
        ei.ExecutionContext.from_dict(data)


def test_from_dict_rejects_unknown_kind():
    data = _context().to_dict()
    data["kind"] = "SomethingElse"
    with pytest.raises(ei.ExecutionIdentityError, match="kind must be"):
        ei.ExecutionContext.from_dict(data)


def test_from_dict_rejects_unknown_top_level_key():
    data = _context().to_dict()
    data["extra"] = "smuggled"
    with pytest.raises(ei.ExecutionIdentityError, match="unknown key"):
        ei.ExecutionContext.from_dict(data)


def test_from_dict_rejects_unknown_nested_key():
    data = _context().to_dict()
    data["actor"]["extra"] = "smuggled"
    with pytest.raises(ei.ExecutionIdentityError, match="unknown key"):
        ei.ExecutionContext.from_dict(data)


def test_from_dict_rejects_missing_required_field():
    data = _context().to_dict()
    del data["trace_id"]
    with pytest.raises(ei.ExecutionIdentityError):
        ei.ExecutionContext.from_dict(data)


@pytest.mark.parametrize("value", sorted(ei.ACTOR_TYPES))
def test_every_documented_actor_type_is_accepted(value):
    context = _context(actor=_actor(type=value))
    assert context.actor.type == value


def test_validate_rejects_unknown_actor_type():
    with pytest.raises(ei.ExecutionIdentityError, match=r"actor\.type"):
        _context(actor=_actor(type="robot"))


def test_validate_rejects_unknown_actor_verification():
    with pytest.raises(ei.ExecutionIdentityError, match=r"actor\.verification"):
        _context(actor=_actor(verification="trust-me"))


@pytest.mark.parametrize("value", sorted(ei.TRIGGER_TYPES))
def test_every_documented_trigger_type_is_accepted(value):
    context = _context(trigger=_trigger(type=value))
    assert context.trigger.type == value


def test_validate_rejects_unknown_trigger_type():
    with pytest.raises(ei.ExecutionIdentityError, match=r"trigger\.type"):
        _context(trigger=_trigger(type="carrier-pigeon"))


@pytest.mark.parametrize("value", sorted(ei.WORKFLOW_TYPES))
def test_every_documented_workflow_type_is_accepted(value):
    context = _context(workflow_type=value)
    assert context.workflow_type == value


def test_validate_rejects_unknown_workflow_type():
    with pytest.raises(ei.ExecutionIdentityError, match="workflow_type"):
        _context(workflow_type="deploy")


@pytest.mark.parametrize("value", sorted(ei.ENVIRONMENTS))
def test_every_documented_environment_is_accepted(value):
    context = _context(scope=_scope(environment=value))
    assert context.scope.environment == value


def test_validate_rejects_unknown_environment():
    with pytest.raises(ei.ExecutionIdentityError, match=r"scope\.environment"):
        _context(scope=_scope(environment="staging"))


@pytest.mark.parametrize("value", sorted(ei.EXECUTOR_TYPES))
def test_every_documented_executor_type_is_accepted(value):
    context = _context(executor=_executor(type=value))
    assert context.executor.type == value


def test_validate_rejects_unknown_executor_type():
    with pytest.raises(ei.ExecutionIdentityError, match=r"executor\.type"):
        _context(executor=_executor(type="freelancer"))


def test_adr_010_executor_vocabulary_is_a_subset_of_this_schemas():
    """design.md: 'executor.type reuses ADR 010's Executor values verbatim
    so the two contracts never diverge' — every ADR 010 value must still be
    valid here, even though this schema's vocabulary is a superset."""
    adr_010_values = {"shepherd", "pr-steward", "devloop-workflow", "reconciler", "implementer"}
    assert adr_010_values <= ei.EXECUTOR_TYPES


def test_trace_id_must_be_32_lowercase_hex_chars():
    with pytest.raises(ei.ExecutionIdentityError, match="trace_id"):
        _context(trace_id="not-hex")
    with pytest.raises(ei.ExecutionIdentityError, match="trace_id"):
        _context(trace_id="A" * 32)  # uppercase rejected
    with pytest.raises(ei.ExecutionIdentityError, match="trace_id"):
        _context(trace_id="a" * 31)  # wrong length


# ---------------------------------------------------------------------------
# T3 — non-authorization guard: no allow/deny/permit/grant/authorized/role
# token anywhere in the schema.
# ---------------------------------------------------------------------------
_FORBIDDEN_TOKENS = ("allow", "deny", "permit", "grant", "authorized", "role")


def _walk_keys(value):
    if isinstance(value, dict):
        for key, sub in value.items():
            yield key
            yield from _walk_keys(sub)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_serialized_schema_has_no_authorization_field_name():
    context = _load_fixture_context()
    keys = set(_walk_keys(context.to_dict()))
    for key in keys:
        lowered = key.lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in lowered, f"field name {key!r} contains forbidden token {token!r}"


# ---------------------------------------------------------------------------
# T4 — import isolation: the module must be stdlib-only importable, so the
# Temporal worker and the agent sandbox can both load it safely.
# ---------------------------------------------------------------------------
def test_module_import_is_stdlib_only():
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.execution_identity, sys; "
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
    assert not leaked, f"orchestrator.execution_identity pulled in third-party modules: {leaked}"


# ---------------------------------------------------------------------------
# T5 — step chaining: a child must share its parent's trace_id, reference
# parent_context_id, and carry a strictly increasing step_sequence.
# ---------------------------------------------------------------------------
def test_root_context_has_no_parent_context_id():
    context = _context()
    assert context.parent_context_id is None
    context.validate()  # no error with parent=None


def test_child_context_with_matching_trace_id_validates_against_parent():
    parent = _context(step_sequence=0)
    child = _context(
        trace_id=parent.trace_id,
        parent_context_id=parent.context_id,
        step_sequence=1,
        workflow_type="implement",
    )
    child.validate(parent=parent)  # must not raise


def test_child_context_trace_id_mismatch_is_rejected():
    parent = _context(step_sequence=0)
    child = _context(
        trace_id="b" * 32,
        parent_context_id=parent.context_id,
        step_sequence=1,
    )
    with pytest.raises(ei.ExecutionIdentityError, match="trace_id"):
        child.validate(parent=parent)


def test_child_context_parent_id_mismatch_is_rejected():
    parent = _context(step_sequence=0)
    child = _context(
        trace_id=parent.trace_id,
        parent_context_id="ex-0000000000000000",
        step_sequence=1,
    )
    with pytest.raises(ei.ExecutionIdentityError, match="parent_context_id"):
        child.validate(parent=parent)


def test_child_context_non_increasing_step_sequence_is_rejected():
    parent = _context(step_sequence=3)
    child = _context(
        trace_id=parent.trace_id,
        parent_context_id=parent.context_id,
        step_sequence=3,
    )
    with pytest.raises(ei.ExecutionIdentityError, match="step_sequence"):
        child.validate(parent=parent)


# ---------------------------------------------------------------------------
# T7 — degraded modes: no MCTL_EXECUTION_CONTEXT_FILE yields an unverified
# local context; MCTL_REQUIRE_EXECUTION_CONTEXT makes the same case raise.
# ---------------------------------------------------------------------------
def test_mint_local_is_always_unverified_and_local():
    context = ei.mint_local(executor_type="implementer", agent="implementer")
    assert context.actor.verification == "unverified"
    assert context.assertions.asserted_by == "local"
    context.validate()


def test_mint_local_forces_unverified_even_if_a_verified_actor_is_passed():
    context = ei.mint_local(
        executor_type="implementer",
        actor=ei.Actor(type="github_user", id="someone", verification="control-plane-verified"),
    )
    assert context.actor.verification == "unverified"
    assert context.actor.id == "someone"


def test_load_from_environment_degrades_to_local_when_file_is_absent(monkeypatch):
    monkeypatch.delenv(ei.MCTL_EXECUTION_CONTEXT_FILE_ENV, raising=False)
    monkeypatch.delenv(ei.MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, raising=False)
    context = ei.load_from_environment(executor_type="shepherd", agent="shepherd")
    assert context.assertions.asserted_by == "local"
    assert context.executor.type == "shepherd"


def test_load_from_environment_fails_closed_when_required_and_missing(monkeypatch):
    monkeypatch.delenv(ei.MCTL_EXECUTION_CONTEXT_FILE_ENV, raising=False)
    monkeypatch.setenv(ei.MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, "1")
    with pytest.raises(ei.ExecutionContextRequiredError, match="MCTL_REQUIRE_EXECUTION_CONTEXT"):
        ei.load_from_environment(executor_type="shepherd")


def test_required_error_is_not_caught_by_the_drivers_degrade_tuple(monkeypatch, tmp_path):
    """The P1 regression this exists to prevent: every driver degrades with
    `except ExecutionIdentityError` and mints a local identity. The require-
    mode raise must NOT be an instance of that (nor of the old tuple's
    OSError/json.JSONDecodeError), or require mode silently stops failing
    closed."""
    assert not issubclass(ei.ExecutionContextRequiredError, ei.ExecutionIdentityError)
    assert not issubclass(ei.ExecutionContextRequiredError, (ValueError, OSError))


def test_load_from_environment_fails_closed_when_required_and_file_is_broken(monkeypatch, tmp_path):
    """Require mode fails closed on a PRESENT but broken file too — a
    truncated, tampered or unreadable document must not degrade to a
    locally-minted identity when MCTL_REQUIRE_EXECUTION_CONTEXT is set."""
    path = tmp_path / "context.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(ei.MCTL_EXECUTION_CONTEXT_FILE_ENV, str(path))
    monkeypatch.setenv(ei.MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, "1")
    with pytest.raises(ei.ExecutionContextRequiredError):
        ei.load_from_environment(executor_type="shepherd")


def test_load_from_environment_fails_closed_when_required_and_file_is_tampered(monkeypatch, tmp_path):
    context = _load_fixture_context()
    tampered = context.to_dict()
    tampered["workflow_type"] = "investigate" if tampered["workflow_type"] != "investigate" else "implement"
    path = tmp_path / "context.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    monkeypatch.setenv(ei.MCTL_EXECUTION_CONTEXT_FILE_ENV, str(path))
    monkeypatch.setenv(ei.MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, "1")
    with pytest.raises(ei.ExecutionContextRequiredError, match="did not yield one"):
        ei.load_from_environment(executor_type="shepherd")


def test_load_from_environment_wraps_undecodable_bytes_as_identity_error(monkeypatch, tmp_path):
    """P2 regression: UnicodeDecodeError is a ValueError that was in none of
    the drivers' old catch tuples, so a file of binary garbage crashed the
    run instead of degrading. load_from_environment() now wraps it, so the
    single `except ExecutionIdentityError` catch covers it."""
    path = tmp_path / "context.json"
    path.write_bytes(b"\xff\xfe\x00garbage")
    monkeypatch.setenv(ei.MCTL_EXECUTION_CONTEXT_FILE_ENV, str(path))
    monkeypatch.delenv(ei.MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, raising=False)
    with pytest.raises(ei.ExecutionIdentityError, match="unreadable or unparseable"):
        ei.load_from_environment(executor_type="shepherd")


def test_load_from_environment_wraps_oserror_as_identity_error(monkeypatch, tmp_path):
    path = tmp_path / "does-not-exist.json"
    monkeypatch.setenv(ei.MCTL_EXECUTION_CONTEXT_FILE_ENV, str(path))
    monkeypatch.delenv(ei.MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, raising=False)
    with pytest.raises(ei.ExecutionIdentityError, match="unreadable or unparseable"):
        ei.load_from_environment(executor_type="shepherd")


def test_load_from_environment_reads_the_sealed_file_when_present(monkeypatch, tmp_path):
    context = _load_fixture_context()
    path = tmp_path / "context.json"
    path.write_text(json.dumps(context.to_dict()), encoding="utf-8")
    monkeypatch.setenv(ei.MCTL_EXECUTION_CONTEXT_FILE_ENV, str(path))
    loaded = ei.load_from_environment(executor_type="issue-investigator")
    assert loaded == context


# ---------------------------------------------------------------------------
# T8 — tamper evidence: mutating a field and recomputing the hash no longer
# matches context_id; validate() still passes shape checks (tamper detection
# is a caller-side hash comparison, not a validate() raise).
# ---------------------------------------------------------------------------
def test_tampering_with_a_field_breaks_the_recomputed_hash():
    context = _context()
    tampered = ei.ExecutionContext(
        **{**context.__dict__, "workflow_type": "implement"}
    )
    assert ei.recompute_content_hash(tampered) != tampered.content_hash


def test_tampered_context_context_id_no_longer_matches_recomputed_hash():
    context = _context()
    tampered_dict = context.to_dict()
    tampered_dict["scope"]["repository"] = "mctlhq/somewhere-else"
    tampered = ei.ExecutionContext.from_dict(
        {**tampered_dict, "content_hash": context.content_hash, "context_id": context.context_id}
    )
    assert ei.recompute_content_hash(tampered) != tampered.content_hash
    assert tampered.context_id != "ex-" + ei.recompute_content_hash(tampered)[7:23]


# ---------------------------------------------------------------------------
# T10 — projection: to_execution_correlation() produces a valid
# context_snapshot.ExecutionCorrelation populated from the context.
# ---------------------------------------------------------------------------
def test_to_execution_correlation_projects_identity_owned_fields():
    from orchestrator.context_snapshot import ExecutionCorrelation

    context = _load_fixture_context()
    correlation = context.to_execution_correlation(
        definition_version="3",
        definition_content_hash="sha256:" + "1a" * 32,
        profile_version="5",
        profile_content_hash="sha256:" + "2b" * 32,
        release_revision=12,
    )
    assert isinstance(correlation, ExecutionCorrelation)
    assert correlation.agent == context.executor.agent
    assert correlation.environment == context.scope.environment
    assert correlation.temporal_workflow_id == context.correlation.temporal_workflow_id
    assert correlation.temporal_run_id == context.correlation.temporal_run_id
    assert correlation.argo_workflow_name == context.correlation.argo_workflow_name
    assert correlation.target_repository_sha == context.scope.target_repository_sha
    assert correlation.definition_version == "3"
    assert correlation.definition_content_hash == "sha256:" + "1a" * 32
    assert correlation.profile_version == "5"
    assert correlation.profile_content_hash == "sha256:" + "2b" * 32
    assert correlation.release_revision == 12


def test_to_execution_correlation_result_passes_context_snapshot_validation():
    """The projected block must be usable inside a real, sealed
    ContextSnapshot — proving it satisfies ADR 009's own validate(), not
    just this module's shape."""
    from orchestrator import context_snapshot as cs

    context = _load_fixture_context()
    correlation = context.to_execution_correlation(
        definition_version="3",
        definition_content_hash="sha256:" + "1a" * 32,
        profile_version="5",
        profile_content_hash="sha256:" + "2b" * 32,
        release_revision=12,
    )
    snapshot = cs.seal(
        execution=correlation,
        strategy=cs.ContextStrategy(name="deterministic-fixed-order", version="1.0.0"),
        budget=cs.ContextBudget(
            max_sources=5, max_bytes=60000, max_bytes_per_source=50000, used_sources=0, used_bytes=0
        ),
        retention=cs.RetentionPolicy(class_="execution-record", expires_after_days=90),
        created_at="2026-09-19T00:00:00Z",
    )
    snapshot.validate()


# ---------------------------------------------------------------------------
# to_log_dict — never a payload, always the identifiers/hashes/versions.
# ---------------------------------------------------------------------------
def test_to_log_dict_matches_to_dict_because_no_field_is_a_payload():
    context = _load_fixture_context()
    assert context.to_log_dict() == context.to_dict()


def test_golden_fixture_has_no_free_text_payload_field():
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert set(data) <= ei._CONTEXT_KEYS  # the fixture must only ever declare documented keys
