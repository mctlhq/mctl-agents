"""Usage records name the issue, PR and execution that spent them
(mctlhq/mctl-agents#499).

Two halves. `usage_ledger` builds the fields in the shapes mctl-api accepts
and carries a runner's scope into the recorder `tracing.agent_run` builds
inside `anyio.run`. And each runner opens that scope around its SDK session
with what it already holds: this file drives the real call sites with the
SDK session replaced by a probe that reads the scope at the moment the
recorder would be built.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest
import yaml

from orchestrator import run_implementer, run_issue_investigator, run_shepherd, usage_ledger
from orchestrator.run_issue_investigator import investigate
from tests.test_run_issue_investigator import _investigate_harness
from tests.test_run_shepherd import CodexReview, make_finding, make_pr, make_ref
from tests.test_usage_ledger import OPUS, TOKEN, FakeApi, _recorder, _result, _usage, ledger  # noqa: F401
from tests.test_work_context_execution_identity import (  # noqa: F401 — fixtures
    URL,
    WID,
    _write_triplet,
    sealed,
    store,
)
from tests.test_work_context_execution_identity import (
    ex as work_context_executions,
)

ISSUE_SOURCE = {"type": "github_issue", "repo": "mctlhq/mctl-web", "issue": 12}


def _scope() -> dict[str, Any]:
    return dict(usage_ledger._SCOPED.get({}))


# ---------------------------------------------------------------------------
# The fields
# ---------------------------------------------------------------------------


def test_the_pr_repository_is_the_target_and_its_own_issue_is_kept():
    assert usage_ledger.work_correlation(
        execution_id="ex-0123456789abcdef", source=ISSUE_SOURCE, pr_repo="mctlhq/mctl-web", pr_number=42,
    ) == {
        "target_repo": "mctlhq/mctl-web",
        "pr_number": 42,
        "issue_number": 12,
        "execution_id": "ex-0123456789abcdef",
    }


def test_an_issue_from_another_repository_is_left_out_not_misattributed():
    """Both numbers are read against `target_repo`: mctl-web#12 recorded
    beside mctl-api's PR would be found by a query for mctl-api#12."""
    fields = usage_ledger.work_correlation(source=ISSUE_SOURCE, pr_repo="mctlhq/mctl-api", pr_number=42)
    assert fields == {"target_repo": "mctlhq/mctl-api", "pr_number": 42}


def test_without_a_pr_the_issue_repository_is_the_target():
    fields = usage_ledger.work_correlation(source=ISSUE_SOURCE, repo="mctlhq/other")
    assert fields == {"target_repo": "mctlhq/mctl-web", "issue_number": 12}


def test_without_a_pr_or_an_issue_the_run_repository_is_the_target():
    assert usage_ledger.work_correlation(source={"type": "incident"}, repo="mctlhq/mctl-web") == {
        "target_repo": "mctlhq/mctl-web"
    }
    assert usage_ledger.work_correlation() == {}


def test_a_typeless_source_block_is_the_legacy_github_issue_shape():
    legacy = {"repo": "mctlhq/mctl-web", "issue": 12}
    assert usage_ledger.work_correlation(source=legacy) == {"target_repo": "mctlhq/mctl-web", "issue_number": 12}
    assert usage_ledger.work_correlation(source={**legacy, "type": "incident"}, repo="mctlhq/x") == {
        "target_repo": "mctlhq/x"
    }


def test_explicit_issue_fields_win_over_the_source_block():
    fields = usage_ledger.work_correlation(source=ISSUE_SOURCE, issue_repo="mctlhq/mctl-web", issue_number="7")
    assert fields == {"target_repo": "mctlhq/mctl-web", "issue_number": 7}


@pytest.mark.parametrize("number", [0, -3, "x", "", None, True, 1.5, "\u00b2", "\u0663"])
def test_an_unusable_number_is_dropped_with_its_repository(number):
    assert usage_ledger.work_correlation(issue_repo="mctlhq/mctl-web", issue_number=number) == {}
    assert usage_ledger.work_correlation(pr_repo="mctlhq/mctl-web", pr_number=number) == {}


# ---------------------------------------------------------------------------
# What the recorder sends
# ---------------------------------------------------------------------------


def test_a_record_carries_the_correlation():
    api = FakeApi()
    _recorder(api, target_repo="mctlhq/mctl-web", issue_number=12, pr_number="42",
               execution_id="we_77e7b93a-0000-4000-8000-000000000000").observe(_result("u1", {OPUS: _usage(1, 2)}))
    (record,) = api.records
    assert (record["target_repo"], record["issue_number"], record["pr_number"], record["execution_id"]) == (
        "mctlhq/mctl-web", 12, 42, "we_77e7b93a-0000-4000-8000-000000000000",
    )


@pytest.mark.parametrize(
    ("correlation", "sent"),
    [
        # A malformed repository takes its numbers with it: they mean nothing alone.
        ({"target_repo": "mctl-web", "issue_number": 12, "pr_number": 42}, {}),
        ({"target_repo": "https://github.com/mctlhq/mctl-web", "pr_number": 42}, {}),
        ({"issue_number": 12}, {}),
        ({"pr_number": 42}, {}),
        ({"target_repo": "mctlhq/mctl-web", "issue_number": 0}, {"target_repo": "mctlhq/mctl-web"}),
        ({"target_repo": "mctlhq/mctl-web", "pr_number": "4x"}, {"target_repo": "mctlhq/mctl-web"}),
        ({"execution_id": "we_ bad id"}, {}),
        ({"execution_id": "-leading-dash"}, {}),
        ({"execution_id": "x" * 129}, {}),
        ({"execution_id": "ex-0123456789abcdef"}, {"execution_id": "ex-0123456789abcdef"}),
    ],
)
def test_a_value_the_server_would_reject_is_not_sent(correlation, sent, caplog):
    """The ingest is all-or-nothing, so one malformed field would cost every
    record in its batch. The rest of the record still goes."""
    api = FakeApi()
    _recorder(api, **correlation).observe(_result("u1", {OPUS: _usage(1, 2)}))
    (record,) = api.records
    keys = ("target_repo", "issue_number", "pr_number", "execution_id")
    assert {k: record[k] for k in keys if k in record} == sent
    if sent != correlation:
        assert "usage ledger: not sending" in caplog.text


# ---------------------------------------------------------------------------
# The scope
# ---------------------------------------------------------------------------


def test_the_scope_reaches_a_recorder_built_inside_anyio_run(monkeypatch):
    """The path every runner takes: `correlate` around `anyio.run`, the
    recorder built by `tracing.agent_run` in the task it starts."""
    env = {usage_ledger.TOKEN_ENV: TOKEN, "WORKFLOW_NAME": "mctl-agents-implement-x"}

    async def build() -> usage_ledger.UsageRecorder:
        return usage_ledger.UsageRecorder.from_env("implementer", env)

    with usage_ledger.correlate({"target_repo": "mctlhq/mctl-web", "pr_number": 42}):
        inside = anyio.run(build)
    after = anyio.run(build)

    (record,) = inside.records_for(_result("u1", {OPUS: _usage(1, 2)}))
    assert (record["target_repo"], record["pr_number"], record["argo_workflow_name"]) == (
        "mctlhq/mctl-web", 42, "mctl-agents-implement-x",
    )
    # The next piece of work in the same process starts clean.
    (record,) = after.records_for(_result("u2", {OPUS: _usage(1, 2)}))
    assert "target_repo" not in record and "pr_number" not in record


def test_nested_scopes_add_to_each_other_and_unwind():
    with usage_ledger.correlate({"execution_id": "ex-0123456789abcdef", "target_repo": "mctlhq/a"}):
        with usage_ledger.correlate({"target_repo": "mctlhq/b", "pr_number": 3, "issue_number": None}):
            assert _scope() == {"execution_id": "ex-0123456789abcdef", "target_repo": "mctlhq/b", "pr_number": 3}
        assert _scope() == {"execution_id": "ex-0123456789abcdef", "target_repo": "mctlhq/a"}
    assert _scope() == {}


def test_explicit_correlation_wins_over_the_scope_and_the_scope_over_the_environment():
    env = {usage_ledger.TOKEN_ENV: TOKEN, "WORKFLOW_WORK_ITEM_ID": "wi_env"}
    with usage_ledger.correlate({"work_item_id": "wi_scope", "target_repo": "mctlhq/a"}):
        scoped = usage_ledger.UsageRecorder.from_env("implementer", env)
        explicit = usage_ledger.UsageRecorder.from_env("implementer", env, target_repo="mctlhq/b")
    assert scoped._correlation["work_item_id"] == "wi_scope"
    assert explicit._correlation["target_repo"] == "mctlhq/b"


def test_the_scope_reaches_a_real_driver_session(ledger, tmp_path, monkeypatch):  # noqa: F811
    from tests.test_tracing_agents import _run_implementer_agent

    with usage_ledger.correlate({"target_repo": "mctlhq/mctl-web", "pr_number": 42}):
        _run_implementer_agent(tmp_path, monkeypatch, [_result("u1", {OPUS: _usage(10, 4)})])
    assert usage_ledger.flush(5)
    (record,) = ledger.records
    assert (record["target_repo"], record["pr_number"]) == ("mctlhq/mctl-web", 42)


# ---------------------------------------------------------------------------
# The investigator
# ---------------------------------------------------------------------------


def _probe_investigator(seen: list[dict]):
    def agent(repo_dir, prompt, proposal_dir):
        seen.append(_scope())
        _write_triplet(repo_dir, prompt, proposal_dir)

    return agent


def test_the_investigator_names_its_issue_and_its_store_execution(tmp_path, monkeypatch, store, sealed):  # noqa: F811
    seen: list[dict] = []
    monkeypatch.setattr(run_issue_investigator, "_run_agent", _probe_investigator(seen))
    monkeypatch.setenv(work_context_executions.WORKFLOW_NAME_ENV_VAR, "mctl-agents-investigate-usage")

    assert investigate(URL, state_dir=tmp_path, work_item_id=WID).error is None

    mine = store.by_ref("mctl-agents-investigate-usage")
    assert seen == [{"target_repo": "mctlhq/mctl-telegram", "issue_number": 455, "execution_id": mine["id"]}]
    assert mine["id"].startswith("we_")
    assert _scope() == {}


def test_without_a_store_execution_the_investigator_uses_its_execution_context(tmp_path, monkeypatch):
    seen: list[dict] = []
    _investigate_harness(tmp_path, monkeypatch, agent=_probe_investigator(seen))
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)

    assert investigate("https://github.com/mctlhq/mctl-telegram/issues/7", state_dir=tmp_path).error is None

    (scope,) = seen
    assert (scope["target_repo"], scope["issue_number"]) == ("mctlhq/mctl-telegram", 7)
    assert scope["execution_id"].startswith("ex-")


def test_an_unverified_execution_id_argument_is_not_the_identity(tmp_path, monkeypatch):
    """`--execution-id` that the store never confirmed is correlation for the
    log only (the CLI's own help): the ledger gets the ExecutionContext."""
    seen: list[dict] = []
    _investigate_harness(tmp_path, monkeypatch, agent=_probe_investigator(seen))
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)

    investigate(
        "https://github.com/mctlhq/mctl-telegram/issues/7", state_dir=tmp_path,
        execution_id="we_99999999-9999-4999-8999-999999999999",
    )

    (scope,) = seen
    assert scope["execution_id"].startswith("ex-")


# ---------------------------------------------------------------------------
# The implementer
# ---------------------------------------------------------------------------


def _write_status(proposal_dir: Path, **fields: Any) -> None:
    proposal_dir.mkdir(parents=True, exist_ok=True)
    (proposal_dir / ".status.yaml").write_text(yaml.safe_dump({"status": "accepted", **fields}), encoding="utf-8")


def _probe_anyio_run(seen: list[dict]):
    def run(func, *args, **kwargs):
        seen.append(_scope())
        return None

    return run


def test_the_implementer_names_the_source_issue_and_its_execution_context(tmp_path, monkeypatch):
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="issue-12", proposal_dir=tmp_path / "proposal", status="accepted",
        approval_ok=True,
    )
    _write_status(ref.proposal_dir, source=ISSUE_SOURCE)
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(
        run_implementer, "_preflight_existing_result", lambda _ref: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a: tmp_path / "clone")
    monkeypatch.setattr(run_implementer, "_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        run_implementer, "read_source_issue",
        lambda *_a, **_kw: run_implementer.SourceIssueVerdict(known=True, failure=None, issue_ref="mctlhq/mctl-web#12"),
    )
    logged: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **_k: logged.append(" ".join(map(str, a))))
    seen: list[dict] = []
    monkeypatch.setattr(run_implementer.anyio, "run", _probe_anyio_run(seen))

    run_implementer.implement_one(ref)

    (scope,) = seen
    assert (scope["target_repo"], scope["issue_number"]) == ("mctlhq/mctl-web", 12)
    assert "pr_number" not in scope
    # The same identity the run logged, not a second mint.
    (identity,) = [line for line in logged if line.startswith("[identity] execution_context=")]
    assert f'"context_id": "{scope["execution_id"]}"' in identity
    assert _scope() == {}


def test_a_review_fix_names_its_pr(tmp_path, monkeypatch):
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="issue-12", proposal_dir=tmp_path / "proposal", status="implemented",
    )
    _write_status(ref.proposal_dir, source=ISSUE_SOURCE, pr="https://github.com/mctlhq/mctl-web/pull/42")
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a: "old")
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_push_followup", lambda *_a, **_kw: None)
    seen: list[dict] = []
    monkeypatch.setattr(run_implementer.anyio, "run", _probe_anyio_run(seen))

    run_implementer.review_feedback_one(ref, {"p1": True, "p2": False, "summaries": []})

    (scope,) = seen
    assert scope == {
        "target_repo": "mctlhq/mctl-web", "pr_number": 42, "issue_number": 12, "devloop_stage": "shepherd",
    }


def test_a_review_fix_names_the_shepherd_as_the_spending_stage():
    """The scope a review-feedback run opens (above) is what a real recorder
    built inside it would carry: `agent` still names the binary that spent
    the tokens, `devloop_stage` the phase that ordered the spend."""
    api = FakeApi()
    _recorder(api, "implementer", devloop_stage="shepherd").observe(_result("u1", {OPUS: _usage(1, 2)}))
    (record,) = api.records
    assert (record["agent"], record["devloop_stage"]) == ("implementer", "shepherd")


def test_an_implementer_proposal_without_a_source_issue_names_its_service_repository(tmp_path):
    """An incident- or adoption-sourced proposal has no issue; the service
    repository it works in is still worth filtering by."""
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="incident-1", proposal_dir=tmp_path / "proposal", status="accepted",
    )
    _write_status(ref.proposal_dir, source={"type": "incident", "id": "inc-1"})
    assert run_implementer._usage_correlation(ref, execution_id="ex-0123456789abcdef") == {
        "target_repo": "mctlhq/mctl-web", "execution_id": "ex-0123456789abcdef",
    }


def test_a_review_fix_names_a_control_plane_execution_but_never_a_local_mint(tmp_path, monkeypatch):
    """A local mint is random and `review_feedback_one` logs it nowhere; the
    sealed context is the one the shepherd tick that forked it read too."""
    import json

    from tests.test_execution_identity import _assertions, _context

    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT", raising=False)
    sealed_file = tmp_path / "context.json"
    context = _context()
    sealed_file.write_text(json.dumps(context.to_dict()), encoding="utf-8")
    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", str(sealed_file))
    assert run_implementer._review_execution_id() == context.context_id

    local = _context(assertions=_assertions(asserted_by="local"))
    sealed_file.write_text(json.dumps(local.to_dict()), encoding="utf-8")
    assert run_implementer._review_execution_id() == ""

    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE")
    assert run_implementer._review_execution_id() == ""


def test_a_review_fix_without_a_readable_identity_still_runs(tmp_path, monkeypatch):
    """Require mode with no context file fails `implement_one` closed, but a
    review-fix never loaded an identity before #499 and must not start
    failing over bookkeeping."""
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setenv("MCTL_REQUIRE_EXECUTION_CONTEXT", "1")
    assert run_implementer._review_execution_id() == ""
    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", str(tmp_path / "missing.json"))
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT")
    assert run_implementer._review_execution_id() == ""


# ---------------------------------------------------------------------------
# The shepherd
# ---------------------------------------------------------------------------


def test_the_shepherd_names_the_pr_its_issue_and_its_tick(tmp_path):
    ref = make_ref(tmp_path, service="mctl-web")
    status = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    ref.status_path.write_text(yaml.safe_dump({**status, "source": ISSUE_SOURCE}), encoding="utf-8")
    review = CodexReview(has_responded=True, findings=[make_finding(severity="P1", commit_id=make_pr().head_sha)])
    seen: list[dict] = []

    def fake_apply_followup(*_a, **_kw):
        seen.append(_scope())
        return {"p1": True, "p2": False, "summaries": ["fix it"]}

    with patch.object(run_shepherd, "find_pr_for_proposal", return_value=make_pr()), \
         patch.object(run_shepherd, "read_codex_review", return_value=review), \
         patch.object(run_shepherd, "read_copilot_review", return_value=run_shepherd.CopilotReview(False, 0)), \
         patch.object(run_shepherd, "apply_followup", side_effect=fake_apply_followup), \
         patch.object(run_shepherd, "trigger_review"):
        result = run_shepherd.process_one(ref, skip_subprocess=True, execution_id="ex-0123456789abcdef")

    assert result.decision == "address-review"
    assert seen == [{
        "target_repo": "mctlhq/mctl-web", "pr_number": 42, "issue_number": 12,
        "execution_id": "ex-0123456789abcdef",
    }]
    assert _scope() == {}


def test_the_shepherd_tick_passes_its_execution_context_to_each_proposal(tmp_path, monkeypatch):
    state_dir = tmp_path / "agents-state"
    state_dir.mkdir()
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setattr("sys.argv", ["run_shepherd", "--state-dir", str(state_dir), "--slug", "issue-12"])
    ref = run_shepherd.ProposalRef(service="mctl-web", slug="issue-12", proposal_dir=tmp_path, status="implemented")
    logged: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **_k: logged.append(" ".join(map(str, a))))
    calls: list[dict] = []

    def fake_process_one(ref, **kwargs):
        calls.append(kwargs)
        return run_shepherd.ShepherdResult(ref=ref, decision="wait")

    with patch.object(run_shepherd, "_discover_refs", return_value=[ref]), \
         patch("orchestrator.auth.ensure_auth_for_sdk"), \
         patch.object(run_shepherd, "process_one", side_effect=fake_process_one):
        try:
            run_shepherd.main()
        except SystemExit as exc:
            assert not exc.code

    (kwargs,) = calls
    (identity,) = [line for line in logged if line.startswith("[identity] execution_context=")]
    assert kwargs["execution_id"].startswith("ex-")
    assert f'"context_id": "{kwargs["execution_id"]}"' in identity


def test_process_one_still_accepts_callers_that_pass_no_execution_id():
    """Every existing caller and test calls `process_one(ref, ...)` without it."""
    import inspect

    assert inspect.signature(run_shepherd.process_one).parameters["execution_id"].default == ""

