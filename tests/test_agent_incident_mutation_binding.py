"""Incident mutations are bound to the agent that declares them.

The incident responder is the one agent built to resolve and acknowledge
incidents on its own (execution profile scope `incident-resolve`). Every
other agent that holds the mctl server reads untrusted text too, so for each
real builder this drives its own PreToolUse checkpoint hook, or for the
mentor, which has none, its tool lists, and checks that only the responder
can make the call.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator import options
from orchestrator import policy_checkpoint as pc

MUTATIONS = pc.AGENT_MUTATION_TOOLS


def _checkpoint_hook(built) -> options._PolicyCheckpointHook:
    hooks = [h for m in (built.hooks or {}).get("PreToolUse", []) for h in m.hooks
             if isinstance(h, options._PolicyCheckpointHook)]
    assert len(hooks) == 1
    return hooks[0]


def _denied(hook, tool: str) -> bool:
    answer = asyncio.run(hook({"tool_name": tool, "tool_input": {"id": "inc-12345678"}}, "tu-1", None))
    return answer.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.fixture
def built(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    service_dir = tmp_path / "mctl-api"
    service_dir.mkdir()
    responder_dir = tmp_path / "_incident-responder"
    responder_dir.mkdir()
    return {
        "incident-responder": options.build_incident_responder_options(
            agent_dir=responder_dir, model="m", state_dir=tmp_path),
        "service-agent": options.build_service_agent_options(service_dir, model="m"),
        "implementer": options.build_implementer_agent_options(service_dir, model="m"),
        "issue-investigator": options.build_issue_investigator_options(
            service_dir, model="m", proposal_dir=tmp_path / "proposals" / "issue-1"),
    }


def test_only_the_incident_responder_may_resolve_or_acknowledge(built):
    for name, opts in built.items():
        hook = _checkpoint_hook(opts)
        for tool in MUTATIONS:
            assert _denied(hook, tool) is (name != "incident-responder"), (name, tool)
        # The same hook still allows a read for every one of them, so the
        # refusals above are about the mutation, not a broken hook.
        assert not _denied(hook, "mcp__mctl__mctl_get_incident"), name


def test_only_the_incident_responder_grants_the_mutations_by_name(built):
    for name, opts in built.items():
        named = set(MUTATIONS) & set(_checkpoint_hook(opts).grants)
        assert named == (set(MUTATIONS) if name == "incident-responder" else set()), name
        # The SDK allow-list is the profile's, unchanged: the explicit grant
        # lives on the checkpoint only (tests/test_manifest.py pins the list).
        assert not set(MUTATIONS) & set(opts.allowed_tools), name


def test_the_mentor_has_no_checkpoint_so_the_mutations_are_disallowed(tmp_path, monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    mentor_dir = tmp_path / "agents" / "_mentor"
    mentor_dir.mkdir(parents=True)
    built = options.build_mentor_options(mentor_dir, model="m")
    assert not (built.hooks or {}).get("PreToolUse")
    assert set(MUTATIONS) <= set(built.disallowed_tools)
    assert not set(MUTATIONS) & set(built.allowed_tools)


def test_without_mctl_mcp_nothing_is_granted_or_disallowed(tmp_path, monkeypatch):
    monkeypatch.delenv("MCTL_TOKEN", raising=False)
    responder_dir = tmp_path / "_incident-responder"
    responder_dir.mkdir()
    responder = options.build_incident_responder_options(agent_dir=responder_dir, model="m", state_dir=tmp_path)
    assert not set(MUTATIONS) & set(_checkpoint_hook(responder).grants)
