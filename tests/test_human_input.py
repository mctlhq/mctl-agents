"""Tests for `orchestrator/human_input.py` (mctlhq/mctl-agents#333, ADR 011).

Mirrors `tests/test_context_snapshot.py`'s structure and T-numbering where
the acceptance criteria line up, since the two modules are deliberately
modelled on each other.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import orchestrator.human_input as hi
from orchestrator.context_snapshot import ExecutionCorrelation

REPO_ROOT = Path(__file__).resolve().parent.parent

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
NOW_ISO = NOW.isoformat()
EXPIRES_ISO = (NOW + timedelta(hours=24)).isoformat()


def _execution(**overrides) -> ExecutionCorrelation:
    fields = dict(
        agent="issue-investigator",
        environment="shadow",
        temporal_workflow_id="dev-loop-mctlhq-mctl-web-10",
        target_repository_sha="a" * 40,
        definition_version="1.0.0",
        definition_content_hash="sha256:" + "1" * 64,
        profile_version="1.2.0",
        profile_content_hash="sha256:" + "2" * 64,
        release_revision=1,
    )
    fields.update(overrides)
    return ExecutionCorrelation(**fields)


def _free_text_spec(**overrides) -> hi.ResponseSpec:
    fields = dict(type="free_text")
    fields.update(overrides)
    return hi.ResponseSpec(**fields)


def _requested_from(**overrides) -> hi.RequestedFrom:
    fields = dict(audience="work_item_owner", actor_refs=("github:alice",))
    fields.update(overrides)
    return hi.RequestedFrom(**fields)


def _seal(**overrides) -> hi.HumanInputRequest:
    fields = dict(
        work_item_id="mctl-web-10",
        execution=_execution(),
        question="Use library A or library B?",
        reason="the issue names both without saying which",
        response=_free_text_spec(),
        requested_from=_requested_from(),
        created_at=NOW_ISO,
        expires_at=EXPIRES_ISO,
    )
    fields.update(overrides)
    return hi.seal_request(**fields)


# ---------------------------------------------------------------------------
# T1 — request creation and typed validation
# ---------------------------------------------------------------------------
def test_single_choice_without_options_is_rejected():
    with pytest.raises(hi.HumanInputError, match="options"):
        _seal(response=hi.ResponseSpec(type="single_choice"))


def test_single_choice_value_outside_options_is_rejected():
    request = _seal(response=hi.ResponseSpec(type="single_choice", options=("A", "B")))
    response = hi.HumanInputResponse(
        api_version=hi.API_VERSION, kind=hi.RESPONSE_KIND, request_id=request.request_id,
        request_hash=request.request_hash, respondent=hi.Respondent("github", "alice"),
        surface="telegram", value="C", received_at=NOW_ISO,
    )
    with pytest.raises(hi.HumanInputError, match="single_choice"):
        hi.validate_response(request, response, now=NOW)


def test_multi_choice_cardinality_rejects_empty_list():
    request = _seal(response=hi.ResponseSpec(type="multi_choice", options=("A", "B")))
    response = hi.HumanInputResponse(
        api_version=hi.API_VERSION, kind=hi.RESPONSE_KIND, request_id=request.request_id,
        request_hash=request.request_hash, respondent=hi.Respondent("github", "alice"),
        surface="telegram", value=[], received_at=NOW_ISO,
    )
    with pytest.raises(hi.HumanInputError, match="multi_choice"):
        hi.validate_response(request, response, now=NOW)


def test_multi_choice_accepts_a_subset_of_options():
    request = _seal(response=hi.ResponseSpec(type="multi_choice", options=("A", "B", "C")))
    response = hi.HumanInputResponse(
        api_version=hi.API_VERSION, kind=hi.RESPONSE_KIND, request_id=request.request_id,
        request_hash=request.request_hash, respondent=hi.Respondent("github", "alice"),
        surface="telegram", value=["A", "C"], received_at=NOW_ISO,
    )
    hi.validate_response(request, response, now=NOW)  # does not raise


def test_empty_free_text_is_rejected():
    request = _seal(response=_free_text_spec())
    response = hi.HumanInputResponse(
        api_version=hi.API_VERSION, kind=hi.RESPONSE_KIND, request_id=request.request_id,
        request_hash=request.request_hash, respondent=hi.Respondent("github", "alice"),
        surface="telegram", value="   ", received_at=NOW_ISO,
    )
    with pytest.raises(hi.HumanInputError, match="free_text"):
        hi.validate_response(request, response, now=NOW)


def test_context_ref_without_allowed_prefix_is_rejected():
    with pytest.raises(hi.HumanInputError, match="context_refs"):
        _seal(context_refs=("https://example.com/evil",))


@pytest.mark.parametrize("prefix", hi.CONTEXT_REF_PREFIXES)
def test_context_ref_with_an_allowed_prefix_is_accepted(prefix):
    request = _seal(context_refs=(f"{prefix}something",))
    assert request.context_refs == (f"{prefix}something",)


def test_expires_at_at_or_before_created_at_is_rejected():
    with pytest.raises(hi.HumanInputError, match="expires_at"):
        _seal(expires_at=NOW_ISO)


def test_expires_at_beyond_max_ttl_is_rejected():
    too_far = (NOW + timedelta(seconds=hi.MAX_REQUEST_TTL_SECONDS + 1)).isoformat()
    with pytest.raises(hi.HumanInputError, match="MAX_REQUEST_TTL_SECONDS"):
        _seal(expires_at=too_far)


def test_expires_at_at_the_ttl_boundary_is_accepted():
    at_boundary = (NOW + timedelta(seconds=hi.MAX_REQUEST_TTL_SECONDS)).isoformat()
    request = _seal(expires_at=at_boundary)
    assert request.expires_at == at_boundary


# ---------------------------------------------------------------------------
# T2 — identity determinism
# ---------------------------------------------------------------------------
def test_sealing_identical_inputs_at_different_created_at_yields_the_same_identity():
    a = _seal(created_at=NOW_ISO)
    b = _seal(created_at=(NOW + timedelta(minutes=5)).isoformat())
    assert a.request_id == b.request_id
    assert a.request_hash == b.request_hash
    assert a.created_at != b.created_at


def test_changing_a_hashed_field_changes_the_identity():
    a = _seal()
    b = _seal(question="Use library A or library C?")
    assert a.request_id != b.request_id
    assert a.request_hash != b.request_hash


def test_changing_round_changes_request_identity_but_not_question_hash():
    a = _seal(round=1)
    b = _seal(round=2)
    assert a.request_id != b.request_id
    assert a.question_hash == b.question_hash


# ---------------------------------------------------------------------------
# T3 — fail-loud versioning
# ---------------------------------------------------------------------------
def test_unknown_api_version_is_rejected():
    document = _seal().to_dict()
    document["api_version"] = "humaninput.mctl.ai/v9"
    with pytest.raises(hi.HumanInputError, match="api_version"):
        hi.HumanInputRequest.from_dict(document)


def test_unknown_kind_is_rejected():
    document = _seal().to_dict()
    document["kind"] = "SomethingElse"
    with pytest.raises(hi.HumanInputError, match="kind"):
        hi.HumanInputRequest.from_dict(document)


def test_unknown_key_in_request_is_rejected():
    document = _seal().to_dict()
    document["extra_field"] = "smuggled"
    with pytest.raises(hi.HumanInputError, match="unknown key"):
        hi.HumanInputRequest.from_dict(document)


def test_unknown_key_in_response_is_rejected():
    request = _seal()
    document = {
        "api_version": hi.API_VERSION, "kind": hi.RESPONSE_KIND, "request_id": request.request_id,
        "request_hash": request.request_hash,
        "respondent": {"actor_type": "github", "actor_id": "alice"},
        "surface": "telegram", "value": "A", "received_at": NOW_ISO,
        "transcript": "smuggled",
    }
    with pytest.raises(hi.HumanInputError, match="unknown key"):
        hi.HumanInputResponse.from_dict(document)


def test_request_round_trips_through_to_dict_from_dict():
    request = _seal()
    assert hi.HumanInputRequest.from_dict(request.to_dict()) == request


# ---------------------------------------------------------------------------
# T3b — the content address is enforced on the read path
# ---------------------------------------------------------------------------
def test_tampered_question_is_rejected_on_read():
    document = _seal().to_dict()
    document["question"] = "A subtly different question?"
    with pytest.raises(hi.HumanInputError, match="hash"):
        hi.HumanInputRequest.from_dict(document)


def test_carried_request_hash_not_matching_content_is_rejected():
    document = _seal().to_dict()
    document["request_hash"] = "sha256:" + "0" * 64
    with pytest.raises(hi.HumanInputError, match="request_hash"):
        hi.HumanInputRequest.from_dict(document)


def test_carried_question_hash_not_matching_content_is_rejected():
    document = _seal().to_dict()
    document["question_hash"] = "sha256:" + "0" * 64
    with pytest.raises(hi.HumanInputError, match="question_hash"):
        hi.HumanInputRequest.from_dict(document)


def test_request_id_not_derived_from_hash_is_rejected():
    document = _seal().to_dict()
    document["request_id"] = "hir-0123456789abcdef"
    with pytest.raises(hi.HumanInputError, match="request_id"):
        hi.HumanInputRequest.from_dict(document)


def test_unparseable_expires_at_is_rejected_on_read():
    document = _seal().to_dict()
    document["expires_at"] = "tomorrow"
    with pytest.raises(hi.HumanInputError, match="expires_at"):
        hi.HumanInputRequest.from_dict(document)


def test_expires_before_created_is_rejected_on_read():
    document = _seal().to_dict()
    document["created_at"], document["expires_at"] = document["expires_at"], document["created_at"]
    with pytest.raises(hi.HumanInputError, match="expires_at"):
        hi.HumanInputRequest.from_dict(document)


# ---------------------------------------------------------------------------
# T4 — response rejection matrix (each rejects, and the positive case accepts)
# ---------------------------------------------------------------------------
def _valid_response(request: hi.HumanInputRequest, **overrides) -> hi.HumanInputResponse:
    fields = dict(
        api_version=hi.API_VERSION, kind=hi.RESPONSE_KIND, request_id=request.request_id,
        request_hash=request.request_hash, respondent=hi.Respondent("github", "alice"),
        surface="telegram", value="use A", received_at=NOW_ISO,
    )
    fields.update(overrides)
    return hi.HumanInputResponse(**fields)


def test_positive_case_is_accepted():
    request = _seal()
    hi.validate_response(request, _valid_response(request), now=NOW)  # does not raise


def test_wrong_request_id_is_rejected():
    request = _seal()
    response = _valid_response(request, request_id="hir-doesnotexist0000")
    with pytest.raises(hi.HumanInputError, match="request_id"):
        hi.validate_response(request, response, now=NOW)


def test_mismatched_request_hash_is_rejected():
    request = _seal()
    response = _valid_response(request, request_hash="sha256:" + "0" * 64)
    with pytest.raises(hi.HumanInputError, match="request_hash"):
        hi.validate_response(request, response, now=NOW)


def test_expired_request_is_rejected():
    request = _seal()
    response = _valid_response(request)
    past_expiry = datetime.fromisoformat(request.expires_at) + timedelta(seconds=1)
    with pytest.raises(hi.HumanInputError, match="expired"):
        hi.validate_response(request, response, now=past_expiry)


def test_respondent_outside_audience_is_rejected():
    request = _seal()
    response = _valid_response(request, respondent=hi.Respondent("github", "mallory"))
    with pytest.raises(hi.HumanInputError, match="requested_from audience"):
        hi.validate_response(request, response, now=NOW)


def test_empty_actor_refs_is_rejected_at_validation():
    """An empty allow-list seals a request nobody can ever answer — it would
    burn its full TTL and time out (claude P2 on #450)."""
    with pytest.raises(hi.HumanInputError, match="actor_refs"):
        _requested_from(actor_refs=()).validate()
    with pytest.raises(hi.HumanInputError, match="actor_refs"):
        _seal(requested_from=hi.RequestedFrom(audience="work_item_owner", actor_refs=()))


# ---------------------------------------------------------------------------
# T6 — question_hash dedupe
# ---------------------------------------------------------------------------
def test_question_hash_is_insensitive_to_whitespace_and_case():
    a = hi.question_hash_for("Use library A or library B?", _free_text_spec())
    b = hi.question_hash_for("  use   library a or library b?  ", _free_text_spec())
    assert a == b


def test_question_hash_changes_with_response_spec():
    a = hi.question_hash_for("Pick one", hi.ResponseSpec(type="single_choice", options=("A", "B")))
    b = hi.question_hash_for("Pick one", hi.ResponseSpec(type="single_choice", options=("A", "C")))
    assert a != b


# ---------------------------------------------------------------------------
# T14 — safe telemetry: ids/hashes only, never question/reason/answer text
# ---------------------------------------------------------------------------
def test_request_log_dict_never_contains_question_or_reason_text():
    request = _seal(question="a very specific secret-shaped question", reason="a very specific reason")
    log = hi.request_log_dict(request)
    assert "question" not in log
    assert "reason" not in log
    serialized = str(log)
    assert "secret-shaped" not in serialized


def test_response_log_dict_never_contains_the_answer_value():
    request = _seal()
    response = _valid_response(request, value="a very specific secret answer")
    log = hi.response_log_dict(response)
    assert "value" not in log
    assert "secret answer" not in str(log)


# ---------------------------------------------------------------------------
# T20 — worker isolation: stdlib-only, no claude_agent_sdk / third-party import
# ---------------------------------------------------------------------------
def test_module_import_is_stdlib_only():
    """Mirrors tests/test_context_snapshot.py's T5 and
    tests/test_worker_isolation.py: a subprocess import of
    orchestrator.human_input must not pull in claude_agent_sdk or any
    third-party package, so both the long-lived Temporal worker and the
    short-lived agent sandbox can import it safely."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.human_input, sys; "
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
    assert not leaked, f"orchestrator.human_input pulled in third-party modules: {leaked}"
