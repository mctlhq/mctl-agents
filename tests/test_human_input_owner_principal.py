"""mctl-agents#668: a sealed human-input request names the work item's
owner by principal as well as by GitHub login, so an owner with no GitHub
identity (a ZITADEL sign-in) can answer. The principal comes only from the
resolved work item (mctl-api#661), never from the issue author's login."""

from __future__ import annotations

import json

import pytest

from orchestrator import human_input as hi
from orchestrator import run_issue_investigator as rii
from orchestrator.work_context.contract import WorkItem
from tests import human_input_harness as harness_mod
from tests.test_human_input_producer import NOW, _answer, _correlation, _draft, _seal
from tests.test_work_context_execution_identity import (
    URL,
    WID,
    store,  # noqa: F401 — pytest fixture
)
from tests.test_work_context_execution_identity import ex as executions

PRN = "prn_01JZED00000000000000000000"

MALFORMED = [
    "",
    "prn_",
    "prn_01jzed00000000000000000000",  # not canonical (lower case)
    "prn_01JZED0000000000000000000",  # 25 characters
    "prn_01JZED00000000000000000000\n",
    "prn_01JZED0000000000000000000O",  # O is not Crockford base32
    "github:alice",
    PRN[4:],  # a bare ULID
]


def _item(**over):
    return {"id": WID, "state": "active", "state_version": 1, **over}


class TestWorkItemOwnerPrincipal:
    def test_a_canonical_principal_is_read(self):
        assert WorkItem.from_payload(_item(owner_principal_id=PRN)).owner_principal_id == PRN

    def test_absent_reads_as_none(self):
        assert WorkItem.from_payload(_item()).owner_principal_id == ""

    @pytest.mark.parametrize("value", [*MALFORMED, None, 7, ["prn"], {"id": PRN}])
    def test_anything_else_reads_as_none_and_keeps_the_record(self, value):
        item = WorkItem.from_payload(_item(owner_principal_id=value))
        assert item is not None and item.work_item_id == WID
        assert item.owner_principal_id == ""

    def test_the_owner_principal_is_read_as_recorded(self):
        # mctl-api#701: the actor of a store-bound policy identity.
        item = WorkItem.from_payload(_item(owner_principal="service:mctl-agent"))
        assert item.owner_principal == "service:mctl-agent"
        assert WorkItem.from_payload(_item()).owner_principal == ""
        assert WorkItem.from_payload(_item(owner_principal=7)).owner_principal == ""


class TestSealedActorRefs:
    def test_a_principal_is_named_after_the_login(self, tmp_path):
        _draft(tmp_path)
        request, rejection = _seal(tmp_path, owner_principal_id=PRN)
        assert rejection == ""
        assert request.requested_from.actor_refs == ("github:alice", f"principal:{PRN}")
        on_disk = hi.HumanInputRequest.from_dict(json.loads((tmp_path / "human-input" / "request.json").read_text()))
        assert on_disk == request

    @pytest.mark.parametrize("value", MALFORMED)
    def test_no_valid_principal_names_only_the_login(self, tmp_path, value):
        _draft(tmp_path)
        request, rejection = _seal(tmp_path, owner_principal_id=value)
        assert rejection == ""
        assert request.requested_from.actor_refs == ("github:alice",)

    def test_the_principal_respondent_mctl_api_signals_is_accepted(self, tmp_path):
        """mctl-api signals `{actor_type: principal, actor_id: prn_...}`;
        the validator rebuilds it as the sealed ref, unchanged."""
        _draft(tmp_path)
        request, _ = _seal(tmp_path, owner_principal_id=PRN)

        def response(actor_type, actor_id):
            return hi.HumanInputResponse.from_dict({
                "api_version": request.api_version, "kind": "HumanInputResponse",
                "request_id": request.request_id, "request_hash": request.request_hash,
                "respondent": {"actor_type": actor_type, "actor_id": actor_id},
                "surface": "portal", "value": "yes", "received_at": NOW.isoformat().replace("+00:00", "Z"),
            })

        hi.validate_response(request, response("principal", PRN), now=NOW)
        hi.validate_response(request, response("github", "alice"), now=NOW)
        for actor_type, actor_id in (("principal", "prn_01JQTHER000000000000000000"), ("prn", PRN[4:])):
            with pytest.raises(hi.HumanInputError):
                hi.validate_response(request, response(actor_type, actor_id), now=NOW)


def _investigate_with_draft(tmp_path, monkeypatch, engine_run=True, **investigate_kwargs):
    if engine_run:
        monkeypatch.setenv(executions.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-hi")
    rii.gh_issue_view(URL).author = "alice"

    def agent(repo_dir, prompt, proposal_dir):
        harness_mod.write_triplet(proposal_dir)
        _draft(proposal_dir)
        return _correlation()

    monkeypatch.setattr(rii, "_run_agent", agent)
    investigate_kwargs.setdefault("work_item_id", WID)
    result = rii.investigate(URL, state_dir=tmp_path, temporal_workflow_id="dev-loop-1",
                             temporal_run_id="run-1", **investigate_kwargs)
    assert result.error is None, result.error
    return json.loads((result.proposal_dir / "human-input" / "request.json").read_text())


def test_investigate_names_the_resolved_owners_principal(tmp_path, monkeypatch, store):  # noqa: F811
    store.owner_principal_id = PRN
    request = _investigate_with_draft(tmp_path, monkeypatch)
    assert request["requested_from"]["actor_refs"] == ["github:alice", f"principal:{PRN}"]


def test_investigate_without_an_owner_principal_names_the_login_only(tmp_path, monkeypatch, store):  # noqa: F811
    request = _investigate_with_draft(tmp_path, monkeypatch)
    assert request["requested_from"]["actor_refs"] == ["github:alice"]


def test_a_work_item_about_another_issue_names_no_principal(tmp_path, monkeypatch, store):  # noqa: F811
    """At `observe` a mismatched work item is dropped, and so is its owner:
    the run is not that work item's, whoever owns it."""
    store.owner_principal_id = PRN
    store.external_key = "https://github.com/mctlhq/mctl-api/issues/1"
    request = _investigate_with_draft(tmp_path, monkeypatch)
    assert request["requested_from"]["actor_refs"] == ["github:alice"]


def test_a_run_without_work_context_names_no_principal(tmp_path, monkeypatch, store):  # noqa: F811
    """The work item matches the issue, but with no engine run there is no
    execution identity, so `observe` proceeds without work context: the
    principal ref follows the work context, not the bare lookup."""
    store.owner_principal_id = PRN
    request = _investigate_with_draft(tmp_path, monkeypatch, engine_run=False)
    assert request["requested_from"]["actor_refs"] == ["github:alice"]


# mctl-agents#684: a continuation runs without the work item (dev_loop.py
# drops the sealed execution identity), so the DevLoopWorkflow carries the
# owner principal round 1 named as `--work-item-owner-principal-id`.
OTHER_PRN = "prn_01JQTHER000000000000000000"


def _continuation(**over):
    return {"work_item_id": None, "human_input_responses": json.dumps([_answer()]), **over}


def test_a_continuation_names_the_carried_owner_principal(tmp_path, monkeypatch, store):  # noqa: F811
    request = _investigate_with_draft(
        tmp_path, monkeypatch, **_continuation(work_item_owner_principal_id=PRN),
    )
    assert request["round"] == 2
    assert request["requested_from"]["actor_refs"] == ["github:alice", f"principal:{PRN}"]


def test_a_continuation_without_a_carried_principal_names_the_login_only(tmp_path, monkeypatch, store):  # noqa: F811
    request = _investigate_with_draft(tmp_path, monkeypatch, **_continuation())
    assert request["requested_from"]["actor_refs"] == ["github:alice"]


def test_a_carried_principal_outside_a_continuation_is_ignored(tmp_path, monkeypatch, store):  # noqa: F811
    """Only a continuation takes it: a first round without work context
    still names no principal, whatever the caller sends."""
    request = _investigate_with_draft(
        tmp_path, monkeypatch, work_item_id=None, work_item_owner_principal_id=PRN,
    )
    assert request["requested_from"]["actor_refs"] == ["github:alice"]


def test_the_resolved_owner_wins_over_a_carried_one(tmp_path, monkeypatch, store):  # noqa: F811
    store.owner_principal_id = PRN
    request = _investigate_with_draft(
        tmp_path, monkeypatch,
        human_input_responses=json.dumps([_answer()]), work_item_owner_principal_id=OTHER_PRN,
    )
    assert request["requested_from"]["actor_refs"] == ["github:alice", f"principal:{PRN}"]


@pytest.mark.parametrize("value", [v for v in MALFORMED if v])
def test_a_malformed_carried_principal_ends_the_run_before_any_work(tmp_path, value):
    with pytest.raises(SystemExit, match="work-item-owner-principal-id"):
        rii.investigate(URL, state_dir=tmp_path, human_input_responses=json.dumps([_answer()]),
                        work_item_owner_principal_id=value)


def test_the_cli_forwards_the_carried_principal(monkeypatch):
    seen = {}

    def fake_investigate(**kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(rii, "investigate", fake_investigate)
    monkeypatch.setattr(rii, "_work_context_from_args", lambda args: None)
    monkeypatch.setattr("sys.argv", [
        "run_issue_investigator", "--issue-url", URL, "--dry-run",
        "--work-item-owner-principal-id", PRN,
    ])
    with pytest.raises(SystemExit):
        rii.main()
    assert seen["work_item_owner_principal_id"] == PRN
