"""The investigator names the DevLoop that submitted it (mctlhq/mctl-agents#461
gap 2 and 3, #451).

gitops#1345 declares three optional investigate CWFT parameters and forwards
each as a flag only when non-empty: `--temporal-workflow-id`,
`--temporal-run-id`, `--execution-request-id`. A loop started by the
execution-request dispatcher is `dev-loop-xr_<id>`, which the investigator
cannot derive from the issue URL, so without the passed id it would name the
issue-keyed `dev-loop-mctlhq-<repo>-<n>`: in the approve instructions it posts
(signalling a loop that does not exist) and in the correlation it seals (which
the loop compares against its own id before accepting a clarification).

The loop side, end to end, is in tests/test_execution_request_dispatch.py.
"""

from __future__ import annotations

import pytest

from orchestrator import context_assembly as ca
from orchestrator import run_issue_investigator
from orchestrator.context_snapshot import MAX_WORK_CONTEXT_ID_LENGTH
from orchestrator.temporal.issue_ref import loop_workflow_id, workflow_id_for
from tests.test_run_issue_investigator import _args, _investigate_harness

URL = "https://github.com/mctlhq/mctl-telegram/issues/123"
ISSUE_KEYED = "dev-loop-mctlhq-mctl-telegram-123"
LOOP = "dev-loop-xr_461c0000"
RUN = "0199a6b2-7c1d-7e3f-9a4b-5c6d7e8f9a0b"
XR = "xr_461c0000"


def test_the_passed_loop_id_wins_and_absent_it_is_derived():
    assert loop_workflow_id(URL, LOOP) == LOOP
    assert loop_workflow_id(URL) == ISSUE_KEYED == workflow_id_for(URL)
    assert loop_workflow_id(URL, None) == ISSUE_KEYED
    assert loop_workflow_id(URL, "") == ISSUE_KEYED


# -- the CLI ------------------------------------------------------------------


def _main(monkeypatch, *extra: str) -> dict:
    """Run `main()` with `extra` flags; return what it handed `investigate`."""
    seen: dict = {}

    def fake_investigate(issue_url, **kwargs):
        seen.update(kwargs, issue_url=issue_url)
        return run_issue_investigator.InvestigateResult(
            "mctl-telegram", "issue-123-x", kwargs["state_dir"], skipped_reason="dry-run"
        )

    monkeypatch.setattr(run_issue_investigator, "investigate", fake_investigate)
    monkeypatch.setattr("sys.argv", ["run_issue_investigator", "--issue-url", URL, "--dry-run", *extra])
    run_issue_investigator.main()
    return seen


def test_the_cli_forwards_the_three_flags(monkeypatch):
    seen = _main(
        monkeypatch,
        "--temporal-workflow-id", LOOP, "--temporal-run-id", RUN, "--execution-request-id", XR,
    )
    assert seen["temporal_workflow_id"] == LOOP
    assert seen["temporal_run_id"] == RUN
    assert seen["execution_request_id"] == XR


def test_without_the_flags_nothing_changes(monkeypatch):
    seen = _main(monkeypatch)
    assert seen["temporal_workflow_id"] is None
    assert seen["temporal_run_id"] is None
    assert seen["execution_request_id"] is None


@pytest.mark.parametrize("flag", ["temporal_workflow_id", "temporal_run_id", "execution_request_id"])
def test_each_flag_is_length_bounded_like_the_other_id_flags(flag):
    extra = {"temporal_workflow_id": LOOP} if flag == "temporal_run_id" else {}
    cli = "--" + flag.replace("_", "-")
    with pytest.raises(SystemExit, match=cli):
        run_issue_investigator._work_context_from_args(
            _args(**extra, **{flag: "x" * (MAX_WORK_CONTEXT_ID_LENGTH + 1)})
        )
    run_issue_investigator._work_context_from_args(_args(**extra, **{flag: "x" * MAX_WORK_CONTEXT_ID_LENGTH}))


@pytest.mark.parametrize("flag", ["temporal_workflow_id", "temporal_run_id", "execution_request_id"])
@pytest.mark.parametrize("bad", ["", "dev loop", "x`y", "a/b", "a\nb", "[x](y)"])
def test_each_flag_must_be_a_plain_token(flag, bad):
    """They are rendered into a public issue comment, inside backticks and a
    URL path."""
    extra = {"temporal_workflow_id": LOOP} if flag == "temporal_run_id" else {}
    with pytest.raises(SystemExit, match="--" + flag.replace("_", "-")):
        run_issue_investigator._work_context_from_args(_args(**extra, **{flag: bad}))


def test_real_loop_and_request_ids_are_accepted():
    run_issue_investigator._work_context_from_args(
        _args(temporal_workflow_id=LOOP, temporal_run_id=RUN, execution_request_id=XR)
    )
    run_issue_investigator._work_context_from_args(_args(temporal_workflow_id=ISSUE_KEYED))


def test_a_run_id_without_its_workflow_id_is_refused():
    """Sealed beside the derived issue-keyed id, it would name a run of a
    different workflow."""
    with pytest.raises(SystemExit, match="--temporal-run-id requires --temporal-workflow-id"):
        run_issue_investigator._work_context_from_args(_args(temporal_run_id=RUN))


# -- the two places the loop id is used ---------------------------------------


def _comment_body(monkeypatch, **kwargs) -> str:
    captured: list[list[str]] = []
    monkeypatch.setattr(run_issue_investigator, "_run", lambda cmd, **kw: captured.append(cmd))
    run_issue_investigator.post_proposal_comment(URL, "mctl-telegram", "issue-123-fix-foo", **kwargs)
    assert len(captured) == 1
    return captured[0][captured[0].index("--body") + 1]


def test_the_approve_instructions_name_the_passed_loop(monkeypatch):
    body = _comment_body(monkeypatch, temporal_workflow_id=LOOP)
    assert f"POST /api/v1/agents/dev-loop/{LOOP}/approve" in body
    assert f"cli approve {LOOP} --approver" in body
    assert ISSUE_KEYED not in body


def test_the_approve_instructions_still_derive_without_a_passed_loop(monkeypatch):
    body = _comment_body(monkeypatch)
    assert f"POST /api/v1/agents/dev-loop/{ISSUE_KEYED}/approve" in body


def _correlation(**kwargs):
    return ca.build_execution_correlation(
        resolver_mode="legacy",
        issue_url=URL,
        target_repository_sha="a" * 40,
        legacy_model="m",
        legacy_allowed_tools=("Read",),
        legacy_budget_usd=1.0,
        **kwargs,
    )


def test_the_sealed_correlation_names_the_passed_loop_and_run():
    execution = _correlation(temporal_workflow_id=LOOP, temporal_run_id=RUN)
    assert execution.temporal_workflow_id == LOOP
    assert execution.temporal_run_id == RUN


def test_the_sealed_correlation_still_derives_without_a_passed_loop():
    execution = _correlation()
    assert execution.temporal_workflow_id == ISSUE_KEYED
    assert execution.temporal_run_id is None


def test_the_declarative_correlation_names_the_passed_loop_too():
    import types

    plan = types.SimpleNamespace(
        definition_version="1.0.0",
        definition_content_hash="sha256:" + "1" * 64,
        profile_version="1.0.0",
        profile_content_hash="sha256:" + "2" * 64,
        release_revision=3,
    )
    execution = ca.build_execution_correlation(
        resolver_mode="declarative", issue_url=URL, target_repository_sha="a" * 40, plan=plan,
        temporal_workflow_id=LOOP, temporal_run_id=RUN,
    )
    assert (execution.temporal_workflow_id, execution.temporal_run_id) == (LOOP, RUN)


# -- through investigate() ----------------------------------------------------


def _write_triplet(repo_dir, prompt, proposal_dir):
    for name in ("requirements.md", "design.md", "tasks.md"):
        (proposal_dir / name).write_text(f"x {name}")


@pytest.mark.parametrize("passed", [True, False])
def test_investigate_threads_the_loop_into_the_snapshot_and_the_comment(tmp_path, monkeypatch, capsys, passed):
    monkeypatch.setenv("ISSUE_INVESTIGATOR_CONTEXT_MODE", "shadow")
    monkeypatch.setattr(run_issue_investigator, "_target_repository_sha", lambda repo_dir: "a" * 40)
    captured: dict = {}
    real_entry = ca.assemble_investigator_context

    def capturing_entry(**kwargs):
        result = real_entry(**kwargs)
        captured["execution"] = result.snapshot.execution
        return result

    monkeypatch.setattr(run_issue_investigator.context_assembly, "assemble_investigator_context", capturing_entry)
    issue = _investigate_harness(tmp_path, monkeypatch, number=123, agent=_write_triplet)
    monkeypatch.setattr(
        run_issue_investigator, "post_proposal_comment",
        lambda url, service, slug, **kw: captured.update(comment=kw),
    )
    flags = {"temporal_workflow_id": LOOP, "temporal_run_id": RUN, "execution_request_id": XR} if passed else {}

    result = run_issue_investigator.investigate(issue.ref.url, state_dir=tmp_path, **flags)

    assert result.error is None
    expected = (LOOP, RUN) if passed else (ISSUE_KEYED, None)
    assert (captured["execution"].temporal_workflow_id, captured["execution"].temporal_run_id) == expected
    assert captured["comment"] == {"temporal_workflow_id": LOOP if passed else None}
    out = capsys.readouterr().out
    if passed:
        line = f"info: loop correlation temporal_workflow_id={LOOP} temporal_run_id={RUN} execution_request_id={XR}"
        assert line in out
    else:
        assert "info: loop correlation" not in out
