"""Awaiting delegated sub-agents past the ResultMessage (mctl-agents#366).

The bug these pin: a ResultMessage ends one turn, not the run. When the CLI
launches the `implementer` sub-agent asynchronously, a driver that stops reading
at the ResultMessage abandons the child's commit and reports "produced no
follow-up commits".
"""
from __future__ import annotations

import anyio
import pytest
from claude_agent_sdk import (
    ResultMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

from orchestrator import subagent_wait
from orchestrator.subagent_wait import (
    AWAITED_TASK_TYPES,
    LiveTaskLedger,
    OrphanedSubagentError,
    drain_until_settled,
)


def started(task_id: str, task_type: str = "local_agent") -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="apply the codex findings",
        uuid="u",
        session_id="s",
        task_type=task_type,
    )


def notified(task_id: str, status: str) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        output_file="/dev/null",
        summary="done",
        uuid="u",
        session_id="s",
    )


def result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=2, session_id="s", total_cost_usd=0.1,
    )


def updated(task_id: str, status: str) -> TaskUpdatedMessage:
    return TaskUpdatedMessage(
        subtype="task_updated",
        data={},
        task_id=task_id,
        patch={"status": status},
        status=status,
    )


def test_awaited_task_types_match_sdk_deferring_set() -> None:
    """Drift guard. Parity with the SDK's own set is load-bearing, not cosmetic.

    `claude_agent_sdk._internal.query` only keeps stdin open past a result frame
    for tasks whose type is in DEFERRING_TASK_TYPES. If we awaited a type it does
    not track, it would close stdin at the first result, the CLI would exit, our
    stream would end -- and we would be blocked on a child nobody is keeping
    alive. There is an open dependabot PR bumping the SDK, so this must fail
    loudly rather than drift.
    """
    from claude_agent_sdk._internal.query import DEFERRING_TASK_TYPES

    assert AWAITED_TASK_TYPES == DEFERRING_TASK_TYPES


def test_ledger_ignores_untracked_task_types() -> None:
    """Background shells never reliably reach a terminal status — never wait."""
    ledger = LiveTaskLedger()
    ledger.observe(started("t1", task_type="bash"))
    assert ledger.live == set()


def test_ledger_tracks_then_clears_on_task_notification() -> None:
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))
    assert ledger.live == {"t1"}
    ledger.observe(notified("t1", "completed"))
    assert ledger.live == set()
    assert ledger.all_completed is True


def test_ledger_clears_on_task_updated_only() -> None:
    """The documented lifecycle hole: a killed task reports via task_updated
    with the matching notification suppressed (claude_agent_sdk types.py)."""
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))
    ledger.observe(updated("t1", "killed"))
    assert ledger.live == set()
    assert ledger.all_completed is False
    assert "killed" in ledger.describe()


def test_ledger_reads_status_from_patch_when_field_is_none() -> None:
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))
    ledger.observe(
        TaskUpdatedMessage(
            subtype="task_updated", data={}, task_id="t1",
            patch={"status": "completed"}, status=None,
        )
    )
    assert ledger.live == set()


def test_ledger_ignores_non_terminal_and_non_task_messages() -> None:
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))
    ledger.observe(updated("t1", "running"))
    ledger.observe("a plain string from a test fake")
    ledger.observe(object())
    assert ledger.live == {"t1"}


def test_describe_reports_nothing_outstanding_when_clean() -> None:
    assert LiveTaskLedger().describe() == "no outstanding tasks"


def test_drain_returns_at_the_result_frame_not_when_the_child_settles() -> None:
    """The stop condition is the SDK's own, and the difference is load-bearing.

    A task completing WAKES the parent for a follow-up turn. Returning the
    instant `ledger.live` empties abandons whatever the parent does after
    delegating — for the implementer's review-feedback prompt that is steps
    2-4, the commit among them. That is the #366 loss again, moved one actor
    along, so the drain must read on to the result frame that arrives with
    nothing in flight.
    """
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def stream():
        yield updated("t1", "completed")
        yield "parent's follow-up turn does the commit here"
        yield result_message()
        yield "never read"

    seen: list[object] = []
    anyio.run(
        lambda: drain_until_settled(
            stream(), ledger, timeout_s=5, on_message=seen.append,
        )
    )
    assert ledger.live == set()
    assert len(seen) == 3, "drain stopped before the parent's closing frame"


def test_drain_does_not_orphan_when_the_stream_ends_after_the_child_settles() -> None:
    """Widening the stop condition must not turn successes into orphans.

    The stream ending with nothing live means the CLI exited; everything the
    child produced is already in the worktree, so this is a clean finish.
    """
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def exhausts():
        yield updated("t1", "completed")

    anyio.run(
        lambda: drain_until_settled(
            exhausts(), ledger, timeout_s=5, on_message=lambda _m: None,
        )
    )


def test_phase_two_gets_its_own_restarted_grace_and_expiry_is_not_an_error() -> None:
    """The closing frame may never arrive, so waiting for it must not be fatal.

    Phase 2 runs on a RESTARTED deadline, not the remainder of phase 1's — a
    slow child must not eat the parent's grace. And expiry here returns rather
    than raising: nothing is outstanding, whatever the child produced is already
    in the worktree, and `_has_new_commits` is the right adjudicator. If this
    instead ran on the caller's outer bound, `fail_after` would fire with an
    empty ledger, fall through to a plain operation timeout -> exit 44 -> a
    CHARGED attempt, and the child's commit would go in the bin with the tmp
    clone — the exact thing #366 is about.
    """
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def parent_never_closes():
        yield updated("t1", "completed")
        await anyio.sleep(10)
        yield result_message()   # too late

    seen: list[str] = []

    async def run() -> str:
        # Generous outer bound: it must NOT be what stops us.
        with anyio.fail_after(5):
            await drain_until_settled(
                parent_never_closes(), ledger, timeout_s=0.05,
                on_message=lambda _m: None,
            )
        return "returned cleanly"

    assert anyio.run(run) == "returned cleanly"
    assert ledger.live == set()
    assert not seen


def test_phase_two_grace_does_not_run_on_phase_ones_remaining_clock() -> None:
    """A child that nearly exhausts the phase-1 budget must still leave the
    parent a full grace window — otherwise a slow child silently converts into
    a truncated commit window."""
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def slow_child_then_parent():
        await anyio.sleep(0.06)          # most of the phase-1 budget
        yield updated("t1", "completed")
        await anyio.sleep(0.06)          # more than what phase 1 had left
        yield result_message()

    async def run() -> bool:
        with anyio.fail_after(5):
            await drain_until_settled(
                slow_child_then_parent(), ledger, timeout_s=0.1,
                on_message=lambda _m: None,
            )
        return True

    assert anyio.run(run) is True


def test_drain_raises_when_stream_ends_with_live_task() -> None:
    """The CLI exited while the child was still live."""
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def stream():
        yield updated("t2", "completed")

    with pytest.raises(OrphanedSubagentError, match=r"still live: t1"):
        anyio.run(
            lambda: drain_until_settled(
                stream(), ledger, timeout_s=5, on_message=lambda _m: None,
            )
        )


def test_drain_raises_on_sub_deadline() -> None:
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def stream():
        await anyio.sleep(10)
        yield "unreachable"

    with pytest.raises(OrphanedSubagentError, match=r"within 0\.05s"):
        anyio.run(
            lambda: drain_until_settled(
                stream(), ledger, timeout_s=0.05, on_message=lambda _m: None,
            )
        )


def test_drain_defaults_to_printing_each_message(capsys) -> None:
    """The archived Argo log was the only forensic trail this incident left."""
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def stream():
        yield updated("t1", "completed")

    anyio.run(lambda: drain_until_settled(stream(), ledger, timeout_s=5))
    assert "task_updated" in capsys.readouterr().out
    assert subagent_wait.drain_until_settled.__doc__


def test_untracked_task_type_is_logged_not_dropped_silently(capsys) -> None:
    """The filter is the one assumption the whole fix rests on.

    A delegated launch arriving with a new SDK task_type (or None) would leave
    the ledger empty, skip the drain, and reproduce #366 exactly — with every
    test in this module still green, since they all build `local_agent`. Only
    production can reveal that, so the skip has to be greppable in the log.
    """
    ledger = LiveTaskLedger()
    ledger.observe(started("t9", task_type="remote_agent"))
    assert ledger.live == set()
    out = capsys.readouterr().out
    assert "not awaiting task t9" in out and "remote_agent" in out


def test_untracked_task_type_none_is_also_logged(capsys) -> None:
    ledger = LiveTaskLedger()
    ledger.observe(started("t9", task_type=None))
    assert "not awaiting task t9" in capsys.readouterr().out


def test_settle_ignores_tasks_the_ledger_never_adopted() -> None:
    """A background shell ending `failed` must not flip all_completed.

    Notifications arrive for every task the CLI runs, including the types
    AWAITED_TASK_TYPES deliberately excludes. Folding those in would print
    `warn: <id> ended 'failed'` for work the driver never waited on — in exactly
    the log an operator reads after an incident.
    """
    ledger = LiveTaskLedger()
    ledger.observe(started("shell1", task_type="bash"))
    ledger.observe(notified("shell1", "failed"))
    assert ledger.all_completed is True
    assert ledger.describe() == "no outstanding tasks"


def test_drain_returns_early_when_nothing_is_live() -> None:
    """Otherwise it raises the self-contradicting "never reported a terminal
    status ... no outstanding tasks"."""
    async def stream():
        return
        yield  # pragma: no cover — makes this an async generator

    anyio.run(
        lambda: drain_until_settled(
            stream(), LiveTaskLedger(), timeout_s=5, on_message=lambda _m: None,
        )
    )


def test_drain_rejects_a_non_positive_timeout() -> None:
    """Reachable via IMPLEMENTER_DRAIN_TIMEOUT_SECONDS=0, which would make
    move_on_after cancel before the first read and orphan every single run."""
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def stream():
        yield updated("t1", "completed")

    with pytest.raises(ValueError, match=r"must be positive"):
        anyio.run(
            lambda: drain_until_settled(
                stream(), ledger, timeout_s=0, on_message=lambda _m: None,
            )
        )


def test_untracked_type_warns_once_per_type_not_once_per_task(capsys) -> None:
    """Keep the signal, drop the spam.

    Background shells are excluded on purpose and an agent may run dozens; this
    is the same Argo log `_settle` was narrowed to keep readable. A type nobody
    has seen before must still show up.
    """
    ledger = LiveTaskLedger()
    for i in range(5):
        ledger.observe(started(f"sh{i}", task_type="bash"))
    ledger.observe(started("x1", task_type="remote_agent"))
    out = capsys.readouterr().out

    assert out.count("of untracked type 'bash'") == 1
    assert "remote_agent" in out
    assert ledger.live == set()


def test_a_second_delegation_after_the_first_settles_is_also_awaited() -> None:
    """The parent may delegate AGAIN in the turn its first child woke.

    Documentation, not a guard: when the closing frame does arrive, a straight
    phase-1-then-phase-2 pass behaves identically here, so this test passes
    either way. The guard is the sibling below, where the second child never
    settles — that is where abandoning it becomes visible.
    """
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))
    seen: list[object] = []

    async def two_rounds():
        yield updated("t1", "completed")
        yield started("t2")              # parent delegates again
        yield updated("t2", "completed")
        yield "second child's work"
        yield result_message()

    anyio.run(
        lambda: drain_until_settled(
            two_rounds(), ledger, timeout_s=5, on_message=seen.append,
        )
    )
    assert ledger.live == set()
    assert "second child's work" in [m for m in seen if isinstance(m, str)]


def test_a_second_delegation_that_never_settles_is_an_orphan() -> None:
    """And abandoning it silently is exactly what must not happen."""
    ledger = LiveTaskLedger()
    ledger.observe(started("t1"))

    async def relaunch_then_hang():
        yield updated("t1", "completed")
        yield started("t2")
        await anyio.sleep(10)

    with pytest.raises(OrphanedSubagentError, match=r"t2"):
        anyio.run(
            lambda: drain_until_settled(
                relaunch_then_hang(), ledger, timeout_s=0.05,
                on_message=lambda _m: None,
            )
        )
