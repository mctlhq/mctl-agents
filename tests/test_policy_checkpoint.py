"""Tests for orchestrator/policy_checkpoint.py and its two governed paths
(mctlhq/mctl-agents#197): the directive poller's GitHub comment and mctl-api
operation submit, and the PreToolUse hook every MCP tool call meets."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from orchestrator import options, run_issue_directive_poller
from orchestrator import policy_checkpoint as pc
from orchestrator.directives import Directive
from orchestrator.temporal.activities.gitops_state import ProposalStateRef

ISSUE = "https://github.com/mctlhq/mctl-web/issues/9"
DEPLOY = "mcp__mctl__mctl_deploy_service"
READ = "mcp__mctl__mctl_get_service_status"
GRANTS = ("Read", "mcp__mctl__*")

DENY_ALL = pc.Policy(version="test/deny-all", rules=(
    pc.Rule("deny-everything-comment", pc.GITHUB_ISSUE_COMMENT, "*", pc.DENY),
    pc.Rule("deny-everything-op", pc.MCTL_OPERATION_EXECUTE, "*", pc.DENY),
))


def _records(capsys) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith(pc.DECISION_PREFIX)]


def _mcp(tool: str, args: dict, *, grants=GRANTS, **kw) -> pc.Decision:
    return pc.checkpoint(pc.MCP_TOOL_CALL, tool, "mctl", args, grants=grants, **kw)


class _Approvals:
    def __init__(self, approved: dict[str, str]) -> None:
        self.approved = approved
        self.asked: list[tuple[str, str]] = []

    def find(self, action_digest: str, policy_version: str) -> str | None:
        self.asked.append((action_digest, policy_version))
        return self.approved.get(action_digest)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_all_three_outcomes_are_explicit(capsys):
    assert pc.VERDICTS == {"ALLOW", "DENY", "REQUIRE_APPROVAL"}
    allow = _mcp(READ, {"service": "x"})
    need = _mcp(DEPLOY, {"service": "x"})
    deny = _mcp(READ, {"service": "x"}, grants=("Read",))
    assert (allow.verdict, allow.code, allow.permitted) == (pc.ALLOW, pc.CODE_ALLOWED, True)
    assert (need.verdict, need.code, need.permitted) == (pc.REQUIRE_APPROVAL, pc.CODE_APPROVAL_REQUIRED, False)
    assert (deny.verdict, deny.code, deny.permitted) == (pc.DENY, pc.CODE_GRANT_MISSING, False)
    assert [r["decision"] for r in _records(capsys)] == ["ALLOW", "REQUIRE_APPROVAL", "DENY"]


def test_the_governed_paths_of_today_are_allowed_by_the_builtin_policy():
    assert pc.checkpoint(pc.GITHUB_ISSUE_COMMENT, "comment", ISSUE, {"body": "hi"}).permitted
    assert pc.checkpoint(pc.MCTL_OPERATION_EXECUTE, "execute:mctl-agents-investigate", "mctl-agents-investigate",
                         {"issue_url": ISSUE}).permitted


def test_anything_no_rule_covers_is_denied():
    for kind, op in (
        (pc.MCTL_OPERATION_EXECUTE, "execute:mctl-deploy"),
        (pc.GITHUB_ISSUE_COMMENT, "edit"),
        ("github.pr.merge", "merge"),
        (pc.MCP_TOOL_CALL, "mcp__other__tool"),
    ):
        d = pc.checkpoint(kind, op, "t", {}, grants=("*",))
        assert (d.verdict, d.code) == (pc.DENY, pc.CODE_NO_RULE), (kind, op)


def test_every_mctl_tool_that_is_not_a_known_read_or_agent_mutation_needs_an_approval():
    for tool in ("mctl_deploy_service", "mctl_rollback_service", "mctl_delete_tenant", "mctl_retire_service",
                 "mctl_promote_agent", "mctl_scale_service", "mctl_grant_repo_access", "mctl_trigger_approve",
                 "mctl_approve_dev_loop", "mctl_deploy_openclaw",
                 # verb-final and unknown names are gated too, never allowed
                 "mctl_trigger_deploy", "mctl_trigger_rollback", "mctl_delete", "mctl_brand_new_tool",
                 "mctl_set_budget_limit", "deploy_service",
                 # starting a paid run is a trigger like the others
                 "mctl_trigger_issue", "trigger_issue"):
        assert _mcp(f"mcp__mctl__{tool}", {}).code == pc.CODE_APPROVAL_REQUIRED, tool
    for tool in ("mctl_list_services", "mctl_get_dev_loop", "mctl_whoami", "mctl_get_service_status",
                 "get_service_status", "mctl_incident_summary", "mctl_resolve_incident",
                 "mctl_acknowledge_incident"):
        assert _mcp(f"mcp__mctl__{tool}", {}).code == pc.CODE_ALLOWED, tool



def test_the_rule_table_classifies_the_recorded_mctl_tool_inventory():
    """Cross-check against the inventory this repo records for mctl-api's
    tools: each one is either allowed or gated, and only reads plus the two
    named agent mutations are allowed."""
    import yaml

    facts = yaml.safe_load((Path(__file__).parents[1] / "docs/diagrams/archify/facts.yaml").read_text(encoding="utf-8"))
    tools = facts["mcp_tools"]
    assert len(tools) == facts["mcp_tool_count"]
    allowed = set()
    for tool in tools:
        code = _mcp(f"mcp__mctl__{tool}", {}).code
        assert code in (pc.CODE_ALLOWED, pc.CODE_APPROVAL_REQUIRED), tool
        if code == pc.CODE_ALLOWED:
            allowed.add(tool)
    reads = {t for t in tools if t.removeprefix("mctl_").startswith(("get_", "list_", "read_", "search_", "describe_"))
             or t.removeprefix("mctl_") in ("whoami", "incident_summary", "resolve_agent")}
    assert allowed == reads | {"mctl_resolve_incident", "mctl_acknowledge_incident"}

def test_an_approval_binds_to_the_exact_action_only():
    request = pc.ActionRequest(pc.MCP_TOOL_CALL, DEPLOY, "mctl", pc.args_digest_of({"service": "x", "tag": "1.2.0"}),
                               execution_id="ctx-1", actor="human:alice", grants=GRANTS)
    approvals = _Approvals({request.action_digest(): "approval-7"})
    approved = pc.decide(request, approvals=approvals)
    assert (approved.verdict, approved.code, approved.permitted, approved.approval_ref) == (
        pc.REQUIRE_APPROVAL, pc.CODE_APPROVED, True, "approval-7")
    assert approvals.asked == [(request.action_digest(), pc.BUILTIN_POLICY.version)]

    # A changed argument, execution, actor or target is a different action.
    for changed in (
        pc.ActionRequest(**{**request.__dict__, "args_digest": pc.args_digest_of({"service": "x", "tag": "9.9.9"})}),
        pc.ActionRequest(**{**request.__dict__, "execution_id": "ctx-2"}),
        pc.ActionRequest(**{**request.__dict__, "actor": "human:mallory"}),
        pc.ActionRequest(**{**request.__dict__, "target": "other"}),
    ):
        d = pc.decide(changed, approvals=approvals)
        assert not d.permitted and d.code == pc.CODE_APPROVAL_REQUIRED and d.approval_ref == ""


def test_the_default_store_never_approves():
    assert pc.NO_APPROVALS.find("sha256:x", "v") is None
    assert not _mcp(DEPLOY, {"service": "x"}).permitted


def test_every_failure_fails_closed():
    broken = pc.Policy(version="broken", rules=(pc.Rule("bad", pc.MCP_TOOL_CALL, "*", "MAYBE"),))
    d = _mcp(READ, {}, policy=broken)
    assert (d.verdict, d.code, d.permitted) == (pc.DENY, pc.CODE_EVALUATOR_ERROR, False)

    class _Down:
        def find(self, action_digest: str, policy_version: str) -> str | None:
            raise OSError("store unreachable")

    d = _mcp(DEPLOY, {}, approvals=_Down())
    assert (d.verdict, d.code, d.permitted) == (pc.DENY, pc.CODE_APPROVAL_LOOKUP_ERROR, False)

    d = pc.decide(pc.ActionRequest(pc.MCP_TOOL_CALL, READ, "mctl", "", grants=GRANTS))
    assert (d.verdict, d.code) == (pc.DENY, pc.CODE_INVALID_REQUEST)

    d = pc.checkpoint(pc.MCP_TOOL_CALL, READ, "mctl", {"not": {"json"}}, grants=GRANTS)
    assert (d.verdict, d.code, d.permitted) == (pc.DENY, pc.CODE_INVALID_REQUEST, False)


def test_require_mode_without_an_execution_context_is_denied(monkeypatch):
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setenv("MCTL_REQUIRE_EXECUTION_CONTEXT", "1")
    d = _mcp(READ, {})
    assert (d.verdict, d.code, d.permitted) == (pc.DENY, pc.CODE_IDENTITY_UNAVAILABLE, False)


def test_require_mode_with_a_broken_context_file_is_denied(monkeypatch, tmp_path):
    broken = tmp_path / "ctx.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("MCTL_EXECUTION_CONTEXT_FILE", str(broken))
    monkeypatch.setenv("MCTL_REQUIRE_EXECUTION_CONTEXT", "1")
    d = _mcp(READ, {})
    assert (d.verdict, d.code, d.permitted) == (pc.DENY, pc.CODE_IDENTITY_UNAVAILABLE, False)
    # Outside require mode the same broken file degrades to no identity.
    monkeypatch.delenv("MCTL_REQUIRE_EXECUTION_CONTEXT")
    assert _mcp(READ, {}).permitted


def test_early_refusals_carry_the_policy_that_was_asked(monkeypatch):
    other = pc.Policy(version="test/other", rules=())
    assert pc.checkpoint(pc.MCP_TOOL_CALL, READ, "mctl", {"x": {1}}, policy=other).policy_version == "test/other"
    monkeypatch.delenv("MCTL_EXECUTION_CONTEXT_FILE", raising=False)
    monkeypatch.setenv("MCTL_REQUIRE_EXECUTION_CONTEXT", "1")
    assert pc.checkpoint(pc.MCP_TOOL_CALL, READ, "mctl", {}, policy=other).policy_version == "test/other"


def test_the_execution_identity_is_bound_into_the_action(monkeypatch, capsys):
    monkeypatch.setattr(pc, "current_identity",
                        lambda: pc.ExecutionIdentity(execution_id="ctx-9", trace_id="tr-9", actor="human:alice"))
    d = _mcp(DEPLOY, {"service": "x"})
    rec = _records(capsys)[-1]
    assert (rec["execution_id"], rec["trace_id"], rec["actor"]) == ("ctx-9", "tr-9", "human:alice")
    expected = pc.ActionRequest(pc.MCP_TOOL_CALL, DEPLOY, "mctl", pc.args_digest_of({"service": "x"}),
                                execution_id="ctx-9", actor="human:alice").action_digest()
    assert d.action_digest == expected == rec["action_digest"]


def test_the_record_carries_the_decision_and_never_the_arguments(capsys):
    secret = "ghp_notARealTokenButLooksLikeOne123456"
    pc.checkpoint(pc.GITHUB_ISSUE_COMMENT, "comment", ISSUE, {"body": secret}, metadata={"issue_url": ISSUE})
    out = capsys.readouterr().out
    assert secret not in out
    rec = json.loads(out.strip().split(" ", 1)[1])
    for key in ("execution_id", "action_kind", "operation", "target", "action_digest", "args_digest",
                "policy_version", "rule_id", "decision", "code", "reason", "approval_ref"):
        assert key in rec, key
    assert (rec["rule_id"], rec["policy_version"], rec["metadata"]) == (
        "github-issue-comment", pc.BUILTIN_POLICY.version, {"issue_url": ISSUE})


def test_enforce_runs_the_side_effect_only_when_permitted():
    calls: list[str] = []
    allow = pc.ActionRequest(pc.MCP_TOOL_CALL, READ, "mctl", pc.args_digest_of({}), grants=GRANTS)
    assert pc.enforce(allow, lambda: calls.append("read") or "ok") == "ok"
    need = pc.ActionRequest(pc.MCP_TOOL_CALL, DEPLOY, "mctl", pc.args_digest_of({}), grants=GRANTS)
    with pytest.raises(pc.PolicyRefused) as refused:
        pc.enforce(need, lambda: calls.append("deploy"))
    assert calls == ["read"]
    assert refused.value.decision.code == pc.CODE_APPROVAL_REQUIRED


def test_module_import_is_stdlib_only():
    result = subprocess.run(
        [sys.executable, "-c", "import orchestrator.policy_checkpoint, sys; print(chr(10).join(sorted(sys.modules)))"],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = set(result.stdout.split("\n"))
    third_party = ("claude_agent_sdk", "temporalio", "httpx", "yaml", "anyio")
    leaked = sorted(n for n in loaded if n.split(".")[0] in third_party)
    assert not leaked, leaked


# ---------------------------------------------------------------------------
# Path 1: every MCP tool call (PreToolUse hook)
# ---------------------------------------------------------------------------


def _hook_answer(hook, tool: str, args) -> dict:
    return asyncio.run(hook({"tool_name": tool, "tool_input": args}, "tu-1", None))


def _is_deny(answer: dict) -> bool:
    return answer.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_the_mcp_hook_gates_every_tool_call():
    hook = options._PolicyCheckpointHook(GRANTS)
    assert _hook_answer(hook, READ, {"service": "x"}) == {}
    deploy = _hook_answer(hook, DEPLOY, {"service": "x"})
    assert _is_deny(deploy) and "REQUIRE_APPROVAL" in deploy["hookSpecificOutput"]["permissionDecisionReason"]
    assert _is_deny(_hook_answer(options._PolicyCheckpointHook(("Read",)), READ, {}))
    assert _is_deny(_hook_answer(hook, "mcp__other__anything", {}))
    assert _is_deny(asyncio.run(hook("not-a-dict", "tu-1", None)))


def test_the_mcp_hook_fails_closed_when_the_checkpoint_breaks(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("evaluator crashed")

    monkeypatch.setattr(pc, "checkpoint", _boom)
    answer = _hook_answer(options._PolicyCheckpointHook(GRANTS), READ, {})
    assert _is_deny(answer) and "not made" in answer["hookSpecificOutput"]["permissionDecisionReason"]


def test_every_builder_with_mcp_installs_the_hook_on_every_mcp_call(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "t")
    built = [
        options.build_service_agent_options(tmp_path, "m"),
        options.build_implementer_agent_options(tmp_path, "m"),
        options.build_incident_responder_options(tmp_path, "m"),
        options.build_issue_investigator_options(tmp_path, "m", tmp_path),
    ]
    built.append(_plan_options(tmp_path))
    for opts in built:
        matchers = [m for m in (opts.hooks or {}).get("PreToolUse", []) if m.matcher == "mcp__.*"]
        hooks = [h for m in matchers for h in m.hooks if isinstance(h, options._PolicyCheckpointHook)]
        assert len(hooks) == 1, opts
        assert hooks[0].grants == tuple(opts.allowed_tools)


# ---------------------------------------------------------------------------
# Path 2: the directive poller's GitHub comment and mctl-api submit
# ---------------------------------------------------------------------------


def _deny_everything(monkeypatch):
    real = pc.checkpoint
    monkeypatch.setattr(pc, "checkpoint", lambda *a, **k: real(*a, **{**k, "policy": DENY_ALL}))


def test_a_refused_comment_never_reaches_gh(monkeypatch):
    ran: list = []
    monkeypatch.setattr(run_issue_directive_poller, "_run", lambda cmd: ran.append(cmd))
    run_issue_directive_poller._post_reply(ISSUE, "hello")
    assert ran and ran[0][:3] == ["gh", "issue", "comment"]

    ran.clear()
    _deny_everything(monkeypatch)
    with pytest.raises(pc.PolicyRefused) as refused:
        run_issue_directive_poller._post_reply(ISSUE, "hello")
    assert ran == []
    assert refused.value.decision.rule_id == "deny-everything-comment"


def test_a_refused_submit_sends_no_request(monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "t")
    sent: list = []
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(
            200, json={"workflow": {"workflowName": "wf-1"}}))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    submit = run_issue_directive_poller.submit_investigate
    assert asyncio.run(submit(ISSUE, "issue-9-fix", "octocat")) == "wf-1" and len(sent) == 1

    _deny_everything(monkeypatch)
    with pytest.raises(pc.PolicyRefused):
        asyncio.run(submit(ISSUE, "issue-9-fix", "octocat"))
    assert len(sent) == 1


def test_a_refused_dispatch_is_answered_once_and_not_retried(monkeypatch):
    ref = ProposalStateRef(service="mctl-web", slug="issue-9-fix", status="proposed", pr_url=None)

    async def _refused(issue_url, slug, requested_by):
        raise pc.PolicyRefused(pc.Decision(pc.DENY, pc.CODE_DENIED, "rule x", "v1", "x", "sha256:d"))

    replies: list[str] = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _refused)
    monkeypatch.setattr(run_issue_directive_poller, "_post_reply", lambda url, body: replies.append(body))
    directive = Directive(comment_id="c1", author="octocat", created_at="2026-09-23T10:00:00Z",
                          verb="reinvestigate", authorized=True)
    outcome = asyncio.run(run_issue_directive_poller._handle_directive(
        directive, issue_url=ISSUE, ref=ref, all_refs=[ref], dry_run=False))
    assert outcome == "policy-refused"
    assert len(replies) == 1
    assert "`DENY`" in replies[0] and "mctl-directive-ack: c1" in replies[0]
    assert "mctl-directive-fail" not in replies[0]


@pytest.mark.parametrize("code", sorted(pc.UNDECIDED_CODES))
def test_an_undecided_checkpoint_is_retried_not_acked(monkeypatch, code):
    ref = ProposalStateRef(service="mctl-web", slug="issue-9-fix", status="proposed", pr_url=None)

    async def _undecided(issue_url, slug, requested_by):
        raise pc.PolicyRefused(pc.Decision(pc.DENY, code, "could not decide", "v1", "", "sha256:d"))

    replies: list[str] = []
    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _undecided)
    monkeypatch.setattr(run_issue_directive_poller, "_post_reply", lambda url, body: replies.append(body))
    directive = Directive(comment_id="c1", author="octocat", created_at="2026-09-23T10:00:00Z",
                          verb="reinvestigate", authorized=True)
    outcome = asyncio.run(run_issue_directive_poller._handle_directive(
        directive, issue_url=ISSUE, ref=ref, all_refs=[ref], dry_run=False))
    assert outcome == "dispatch-failed"
    assert len(replies) == 1
    assert "mctl-directive-fail: c1" in replies[0] and "mctl-directive-ack" not in replies[0]



def _plan_options(tmp_path):
    """The declarative investigator builder, from the resolved real plan."""
    from orchestrator import resolver

    plan = resolver.execute("issue-investigator", resolver.Task(target_repository_sha="d" * 40))
    return options.build_issue_investigator_options_from_plan(plan, tmp_path, tmp_path)


def test_a_refused_ack_after_a_real_dispatch_is_loud_and_not_silent(monkeypatch, capsys):
    """The ack is the only record that a dispatch happened: a policy refusal
    there must take the same loud resubmit-warning path as a gh failure."""
    ref = ProposalStateRef(service="mctl-web", slug="issue-9-fix", status="proposed", pr_url=None)

    async def _submitted(issue_url, slug, requested_by):
        return "wf-42"

    def _refuse(url, body):
        raise pc.PolicyRefused(pc.Decision(pc.DENY, pc.CODE_IDENTITY_UNAVAILABLE, "no ctx", "v1", "", "sha256:d"))

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _submitted)
    monkeypatch.setattr(run_issue_directive_poller, "_post_reply", _refuse)
    directive = Directive(comment_id="c1", author="octocat", created_at="2026-09-23T10:00:00Z",
                          verb="reinvestigate", authorized=True)
    with pytest.raises(pc.PolicyRefused):
        asyncio.run(run_issue_directive_poller._handle_directive(
            directive, issue_url=ISSUE, ref=ref, all_refs=[ref], dry_run=False))
    out = capsys.readouterr().out
    assert "dispatched successfully" in out and "wf-42" in out and "WILL be resubmitted" in out


def test_the_hook_matcher_catches_every_mcp_tool_and_nothing_else():
    """Claude Code matches `matcher` as a regex against the tool name."""
    import re

    (matcher,) = options._policy_hooks(["mcp__mctl__*"])["PreToolUse"]
    for name in (DEPLOY, READ, "mcp__other__x", "mcp__mctl__get_service_status"):
        assert re.fullmatch(matcher.matcher, name), name
    for name in ("Bash", "Read", "Write", "WebFetch"):
        assert not re.fullmatch(matcher.matcher, name), name


@pytest.mark.parametrize("submit_error", ["ambiguous", "failed"])
def test_a_refused_marker_or_ambiguous_ack_raises_the_refusal_not_an_attribute_error(monkeypatch, capsys, submit_error):
    """Every ack/marker handler that catches PolicyRefused must survive it:
    PolicyRefused has no `stderr`."""
    ref = ProposalStateRef(service="mctl-web", slug="issue-9-fix", status="proposed", pr_url=None)

    async def _submit(issue_url, slug, requested_by):
        if submit_error == "ambiguous":
            raise run_issue_directive_poller.DispatchOutcomeAmbiguous("reply lost")
        raise RuntimeError("mctl-api down")

    def _refuse(url, body):
        raise pc.PolicyRefused(pc.Decision(pc.DENY, pc.CODE_IDENTITY_UNAVAILABLE, "no ctx", "v1", "", "sha256:d"))

    monkeypatch.setattr(run_issue_directive_poller, "submit_investigate", _submit)
    monkeypatch.setattr(run_issue_directive_poller, "_post_reply", _refuse)
    monkeypatch.setattr(run_issue_directive_poller.time, "sleep", lambda _s: None)
    directive = Directive(comment_id="c1", author="octocat", created_at="2026-09-23T10:00:00Z",
                          verb="reinvestigate", authorized=True)
    with pytest.raises(pc.PolicyRefused):
        asyncio.run(run_issue_directive_poller._handle_directive(
            directive, issue_url=ISSUE, ref=ref, all_refs=[ref], dry_run=False, prior_failures=0))
    assert "FAIL:" in capsys.readouterr().out
