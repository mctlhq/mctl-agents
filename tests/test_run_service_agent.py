"""Wiring-level tests for run_service_agent()'s mctl MCP connectivity guard.

Regression coverage for a Claude-review finding on PR #84: the modes newly
wired onto ensure_mctl_connected(fatal=False) (this one, run_mentor.py,
run_issue_investigator.py, run_implementer.py) had no test exercising the
mcp_configured=True branch — every existing test ran with MCTL_TOKEN unset,
so ensure_mctl_connected was never actually invoked at the integration
level, only in isolation via test_mcp_guard.py. These use the real
run_service_agent() with build_service_agent_options() stubbed, matching
the pattern already established in tests/test_run_incident_responder.py.
"""
from __future__ import annotations

import types

import anyio
import pytest
from claude_agent_sdk import TaskUpdatedMessage

from orchestrator import run_service_agent as rsa
from tests.conftest import (
    fake_mcp_client_factory,
    result_message,
    task_notification_message,
    task_started_message,
    task_updated_message,
)


def _stub_build_options(monkeypatch, *, mcp_servers):
    monkeypatch.setattr(
        rsa, "build_service_agent_options",
        lambda *args, **kwargs: types.SimpleNamespace(mcp_servers=mcp_servers),
    )


def test_connected_mcp_dispatches_query_without_warning(monkeypatch, capsys):
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    monkeypatch.setattr(
        rsa, "ClaudeSDKClient",
        fake_mcp_client_factory(
            statuses=[{"mcpServers": [{"name": "mctl", "status": "connected"}]}]
        ),
    )
    anyio.run(rsa.run_service_agent, "mctl-agent")
    assert "warn:" not in capsys.readouterr().err


def test_failed_mcp_warns_but_still_dispatches_query(monkeypatch, capsys):
    """fatal=False: a broken mctl connection must not stop the agent from
    running — service-agent still does useful work without mctl tools."""
    _stub_build_options(monkeypatch, mcp_servers={"mctl": {}})
    factory = fake_mcp_client_factory(
        statuses=[{"mcpServers": [{"name": "mctl", "status": "failed", "error": "boom"}]}]
    )
    monkeypatch.setattr(rsa, "ClaudeSDKClient", factory)
    anyio.run(rsa.run_service_agent, "mctl-agent")  # must not raise
    assert "boom" in capsys.readouterr().err


def test_mcp_not_configured_skips_status_check_entirely(monkeypatch):
    """MCTL_TOKEN unset -> mcp_servers={} -> get_mcp_status() must never be
    called (FakeMcpClient raises AssertionError if it is)."""
    _stub_build_options(monkeypatch, mcp_servers={})
    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory())
    anyio.run(rsa.run_service_agent, "mctl-agent")  # must not raise


# ---------------------------------------------------------------------------
# Awaiting an async-launched sub-agent (mctl-agents#366)
#
# `ResultMessage` ends one TURN, not the RUN. `receive_response()` returns at
# the first one by contract, so the pre-#366 driver abandoned any sub-agent the
# CLI had launched asynchronously (`isAsync: True`, `status: "async_launched"`).
# This driver is exposed in practice, not just in principle: its prompt walks
# four steps named after the `.claude/agents/{researcher,analyst,spec-writer}.md`
# personas that `setting_sources=["project"]` loads from the agent's own cwd.
#
# Draining is sound here only because `build_service_agent_options` passes
# `hooks=_command_audit_hooks()` — the SDK keeps stdin (and so the CLI
# subprocess) open past the result frame while tasks are in flight, but ONLY
# when `sdk_mcp_servers or hooks` is truthy. That precondition is pinned once
# for every builder in tests/test_options.py, not re-asserted here.
# ---------------------------------------------------------------------------
def test_service_agent_waits_for_async_launched_subagent(monkeypatch):
    """The headline regression: fails without the drain.

    `consumed` is the real assertion — returning normally is not enough, the
    driver must have read *past* the ResultMessage to see the child settle.
    """
    _stub_build_options(monkeypatch, mcp_servers={})
    consumed: list[object] = []

    async def messages():
        for message in (task_started_message(), result_message(), task_updated_message()):
            consumed.append(message)
            yield message

    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory(messages=messages))
    monkeypatch.setattr(rsa, "SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS", 5)

    anyio.run(rsa.run_service_agent, "mctl-agent")

    assert any(isinstance(m, TaskUpdatedMessage) for m in consumed), (
        "driver stopped at the ResultMessage and abandoned the live sub-agent"
    )


def test_service_agent_returns_immediately_when_no_tasks_are_live(monkeypatch):
    """Runs without a delegated child must not pay for the drain."""
    _stub_build_options(monkeypatch, mcp_servers={})
    consumed: list[object] = []

    async def messages():
        for message in ("chatter", result_message(), task_updated_message()):
            consumed.append(message)
            yield message

    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory(messages=messages))

    anyio.run(rsa.run_service_agent, "mctl-agent")

    assert not any(isinstance(m, TaskUpdatedMessage) for m in consumed)


def test_service_agent_raises_orphaned_when_task_never_settles(monkeypatch):
    _stub_build_options(monkeypatch, mcp_servers={})

    async def messages():
        yield task_started_message()
        yield result_message()
        await anyio.sleep(10)

    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory(messages=messages))
    monkeypatch.setattr(rsa, "SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(rsa.ServiceAgentOrphanedSubagent, match=r"orphaned sub-agent:"):
        anyio.run(rsa.run_service_agent, "mctl-agent")


def test_service_agent_raises_orphaned_when_stream_ends_with_live_task(monkeypatch):
    """The CLI exited while the child was still live."""
    _stub_build_options(monkeypatch, mcp_servers={})

    async def messages():
        yield task_started_message()
        yield result_message()

    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory(messages=messages))
    monkeypatch.setattr(rsa, "SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS", 5)

    with pytest.raises(rsa.ServiceAgentOrphanedSubagent):
        anyio.run(rsa.run_service_agent, "mctl-agent")


def test_service_agent_does_not_orphan_on_failed_terminal_status(monkeypatch, capsys):
    """A failed child is quiescent, not orphaned — warn, do not raise.

    Whatever it wrote into inbox/ or proposals/ is on disk and the mentor will
    read it; nothing is still mutating the agent directory.
    """
    _stub_build_options(monkeypatch, mcp_servers={})

    async def messages():
        yield task_started_message()
        yield result_message()
        yield task_notification_message(status="failed", summary="boom")

    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory(messages=messages))
    monkeypatch.setattr(rsa, "SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS", 5)

    anyio.run(rsa.run_service_agent, "mctl-agent")

    out = capsys.readouterr().out
    assert "warn:" in out and "failed" in out


def test_orphan_does_not_tear_down_the_sibling_agents(monkeypatch, capsys):
    """run_all's per-service guard must keep catching it.

    ServiceAgentOrphanedSubagent subclasses RuntimeError, so `_safe_run_service`
    logs and drops it — one orphaned agent must not cancel the other agents
    sharing its task group, which is the failure mode that guard exists for.
    """
    from orchestrator import run_all

    async def _boom(service):
        raise rsa.ServiceAgentOrphanedSubagent("orphaned sub-agent: t1 still live")

    monkeypatch.setattr(run_all, "run_service_agent", _boom)
    anyio.run(run_all._safe_run_service, "mctl-agent")  # must not raise

    assert "ServiceAgentOrphanedSubagent" in capsys.readouterr().err


def test_service_agent_awaits_a_second_delegation(monkeypatch):
    """The parent may delegate AGAIN in the turn its first child woke.

    #367's helper loops for exactly this: a straight phase-1-then-phase-2 pass
    saw the relaunch only as "the ledger is non-empty again" and abandoned it,
    reproducing #366 on the second delegation. This driver's prompt makes that
    the expected shape rather than an edge case — steps 1-3 (researcher,
    analyst, spec-writer) are three named personas in sequence, so a run that
    delegates twice is the normal path, not a pathological one.

    Pinned here at driver level because the helper's own test cannot see the
    driver's break-then-drain wiring: it is possible to consume the relaunch in
    the turn loop and never enter the drain at all.
    """
    consumed: list[object] = []

    _stub_build_options(monkeypatch, mcp_servers={})

    async def messages():
        for message in (
            task_started_message("researcher"),
            result_message(),
            task_updated_message("researcher"),
            # The turn the first child woke delegates again.
            task_started_message("spec-writer"),
            task_updated_message("spec-writer"),
            result_message(),
        ):
            consumed.append(message)
            yield message

    monkeypatch.setattr(rsa, "ClaudeSDKClient", fake_mcp_client_factory(messages=messages))
    monkeypatch.setattr(rsa, "SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS", 5)

    anyio.run(rsa.run_service_agent, "mctl-agent")

    assert any(
        isinstance(m, TaskUpdatedMessage) and m.task_id == "spec-writer"
        for m in consumed
    ), "driver abandoned the SECOND delegation"


# ---------------------------------------------------------------------------
# The unguarded entrypoints (claude P2, repeat, on #368).
#
# `run_all._safe_run_service` only covers the RUN_MODE=full task group.
# `_single_service` — reached by the declared `mctl-agents-single-service`
# operator operation — and `main()`'s `python -m orchestrator.run_service_agent
# <svc>` both called the driver bare under anyio.run. On those paths raising
# turned a silent exit-0 partial into a non-zero exit, so the workflow's
# commit-and-push step never ran and the parent's ALREADY-WRITTEN inbox entry
# and proposals were dropped: strictly more loss than before #366, which is the
# inversion of what #366 is for.
# ---------------------------------------------------------------------------
def test_an_orphan_does_not_propagate_out_of_the_tolerating_wrapper(monkeypatch, capsys):
    """The parent's work outlives the orphan: no raise, so the commit step runs."""
    async def _orphan(service):
        raise rsa.ServiceAgentOrphanedSubagent("orphaned sub-agent: t1 still live")

    monkeypatch.setattr(rsa, "run_service_agent", _orphan)

    anyio.run(rsa.run_service_agent_tolerating_orphans, "mctl-agent")  # must not raise

    err = capsys.readouterr().err
    assert "warn: service-agent mctl-agent" in err
    assert "t1 still live" in err


def test_single_service_mode_does_not_propagate_an_orphan(monkeypatch, capsys):
    """`RUN_MODE=single-service` is real operator traffic, not theory.

    Patches the driver rather than the wrapper, so this exercises the actual
    `_single_service` -> wrapper -> driver wiring; patching the wrapper would
    pass even if `_single_service` still called the bare driver.

    `run_all` binds `run_service_agent` by name at import, so that binding is
    ALSO stubbed — with a tripwire rather than the orphan. Without it, a
    regression to the bare call reaches the real driver and a real
    ClaudeSDKClient: the test then hangs on a subprocess instead of failing,
    which is how a guard like this rots into a test nobody trusts. (Found the
    hard way — the first version of this test did exactly that.)
    """
    from orchestrator import run_all

    async def _orphan(service):
        raise rsa.ServiceAgentOrphanedSubagent("orphaned sub-agent: t1 still live")

    async def _bare_call_tripwire(service):
        raise AssertionError(
            "_single_service called the bare driver: an orphan there exits "
            "non-zero and the commit step never runs (claude P2 on #368)"
        )

    monkeypatch.setattr(rsa, "run_service_agent", _orphan)
    monkeypatch.setattr(run_all, "run_service_agent", _bare_call_tripwire)

    anyio.run(run_all._single_service, "mctl-agent")  # must not raise

    assert "warn: service-agent mctl-agent" in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("budget exhausted"), ValueError("boom")],
)
def test_the_wrapper_is_orphan_only_and_still_fails_loudly_otherwise(monkeypatch, exc):
    """Deliberately NOT `except Exception`.

    Every other failure may mean zero real work happened, and swallowing those
    would report a false green to Argo for a run that produced nothing — the
    bug `_safe_run_incident_responder`'s McpNotConnectedError branch exists to
    prevent. Only the orphan is guaranteed to leave something on disk worth
    committing.
    """
    async def _boom(service):
        raise exc

    monkeypatch.setattr(rsa, "run_service_agent", _boom)

    with pytest.raises(type(exc)):
        anyio.run(rsa.run_service_agent_tolerating_orphans, "mctl-agent")


def test_the_module_entrypoint_goes_through_the_guard():
    """`python -m orchestrator.run_service_agent <svc>` is what this module's
    own docstring documents, so it must not be the one path left bare."""
    import inspect

    source = inspect.getsource(rsa.main)
    assert "run_service_agent_tolerating_orphans" in source
    assert "anyio.run(run_service_agent," not in source
