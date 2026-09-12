"""Await background sub-agents before a ClaudeSDKClient run is declared over.

Built for mctl-agents#366. On 2026-09-12 the implementer's review-feedback
follow-up for portfolio#69 read the finding, derived the correct one-line fix,
launched the staged `implementer` sub-agent -- and ended its own turn 25.8s
later, before that sub-agent had run. The orchestrator's `git log` check saw an
unchanged HEAD, reported "implementer produced no follow-up commits", exited 42,
and the shepherd charged the proposal a review attempt for work the platform
itself had thrown away.

The cause is a turn/run confusion. `ClaudeSDKClient.receive_response()` returns
at the first `ResultMessage` by contract, but a `ResultMessage` ends one *turn*,
not the *run*: when the CLI launches a sub-agent asynchronously
(`isAsync: True`, `status: "async_launched"`) the child keeps running past it.
Prompt text cannot fix this -- run_implementer's prompt has forbidden background
deferral since well before the incident -- because asynchronous launch is the
CLI's choice, not a parameter the model picks.

Draining past the ResultMessage is viable because the SDK already keeps the
subprocess alive for exactly this case. `claude_agent_sdk._internal.query`:

  - `_read_messages` does NOT fire `_first_result_event` on a result frame while
    `_inflight_tasks` is non-empty; it logs "keeping stdin open" instead, because
    "each task completion wakes the parent for a follow-up turn, so a later
    result frame arrives with no tasks in flight and closes stdin then".
  - That hold only applies when `sdk_mcp_servers or hooks` is truthy.
    `build_implementer_agent_options` passes `hooks=_command_audit_hooks()`, so
    the implementer qualifies.

    Read `sdk_mcp_servers` carefully before relying on it: `_internal/client.py`
    lifts an entry into it ONLY when its config says `type: "sdk"`, and this
    repo's `mctl_mcp_config()` emits `type: "http"`. So a non-empty
    `mcp_servers` does NOT satisfy the precondition -- `hooks` is the whole of
    it for every driver here. Strip the audit hooks from a driver and its
    delegated children stop being awaitable, silently.

Hence AWAITED_TASK_TYPES below must stay identical to the SDK's own
DEFERRING_TASK_TYPES: awaiting a task type the SDK does not track would mean the
SDK closes stdin at the first result, the CLI exits, our stream ends -- and we
would be waiting on a child nobody is keeping alive. tests/test_subagent_wait.py
pins the two sets together so an SDK bump cannot silently break the assumption.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)

# Mirror of claude_agent_sdk._internal.query.DEFERRING_TASK_TYPES. Deliberately
# a literal copy rather than an import of a private name: the drift guard in
# tests/test_subagent_wait.py is the contract, and a private import would turn
# an SDK refactor into an ImportError at agent runtime instead of a red test.
#
# Background *shells* are excluded on purpose, by the SDK and therefore by us:
# they need not ever reach a terminal status, so awaiting one would hang.
AWAITED_TASK_TYPES = frozenset({"local_agent", "local_workflow"})


class OrphanedSubagentError(RuntimeError):
    """A run ended while a delegated sub-agent was still live.

    Raised when the child neither reported a terminal status before the drain
    deadline nor was still reachable on the stream. Callers map this to a
    harness-failure exit code: the agent never got its attempt, so the work is
    lost through no fault of the proposal being implemented.
    """


class LiveTaskLedger:
    """Tracks which delegated sub-agent tasks are still running.

    Fed every message off the stream, including plain strings from test fakes,
    so `observe` ignores anything that is not one of the three typed lifecycle
    messages. Terminal status is honoured from BOTH vocabularies:
    `task_notification` reports `stopped` for a killed task while `task_updated`
    reports the raw `killed`, and the SDK documents that a terminal state can
    arrive as a `task_updated` patch with no accompanying notification at all.
    """

    def __init__(self) -> None:
        self._live: set[str] = set()
        self._settled: dict[str, str] = {}

    @property
    def live(self) -> set[str]:
        """Task ids started and not yet seen to reach a terminal status."""
        return set(self._live)

    @property
    def all_completed(self) -> bool:
        """True iff every task that settled did so as ``completed``.

        A `failed`/`stopped`/`killed` child is NOT an orphan -- it is quiescent,
        so nothing is still mutating the worktree and the caller's own
        post-conditions (for the implementer, `_has_new_commits`) are the right
        adjudicator. This flag exists only so the caller can say so in the log.
        """
        return all(status == "completed" for status in self._settled.values())

    def describe(self) -> str:
        parts = []
        if self._live:
            parts.append(f"{len(self._live)} task(s) still live: {', '.join(sorted(self._live))}")
        non_completed = sorted(
            f"{task_id} ended {status!r}"
            for task_id, status in self._settled.items()
            if status != "completed"
        )
        parts.extend(non_completed)
        return "; ".join(parts) if parts else "no outstanding tasks"

    def observe(self, message: Any) -> None:
        """Fold one stream message into the ledger. Never raises."""
        if isinstance(message, TaskStartedMessage):
            # task_type is optional on the wire; only delegated agent work is
            # kept alive by the SDK past the result frame, so only that is safe
            # to wait for.
            if message.task_type in AWAITED_TASK_TYPES:
                self._live.add(message.task_id)
            else:
                # Say so out loud. This filter is the single assumption the
                # whole fix rests on: a delegated launch arriving with a new
                # SDK task_type (or None) would leave the ledger empty, skip
                # the drain, and reproduce mctl-agents#366 exactly -- with
                # every test still green, since they all construct
                # task_type="local_agent". The Argo log was the only forensic
                # trail the original incident left, so make the skip greppable
                # rather than silent.
                print(
                    f"warn: not awaiting task {message.task_id} of untracked "
                    f"type {message.task_type!r}"
                )
            return
        if isinstance(message, TaskNotificationMessage):
            self._settle(message.task_id, message.status)
            return
        if isinstance(message, TaskUpdatedMessage):
            # `.status` is already derived from `.patch["status"]` by the SDK
            # parser, but read the patch as a fallback: that parser is
            # deliberately defensive about patches of an unexpected shape.
            status = message.status
            if status is None and isinstance(message.patch, dict):
                status = message.patch.get("status")
            self._settle(message.task_id, status)

    def _settle(self, task_id: str, status: str | None) -> None:
        # Only tasks this ledger actually adopted. Notifications arrive for
        # every task the CLI runs, including the background shells
        # AWAITED_TASK_TYPES deliberately excludes -- folding those in would
        # flip `all_completed` and print `warn: <id> ended 'failed'` for work
        # the driver never waited on, in exactly the log an operator reads
        # after an incident.
        if task_id not in self._live:
            return
        if status not in TERMINAL_TASK_STATUSES:
            return
        self._live.discard(task_id)
        self._settled[task_id] = status


async def drain_until_settled(
    stream: AsyncIterator[Any],
    ledger: LiveTaskLedger,
    *,
    timeout_s: float,
    on_message: Callable[[Any], None] = print,
) -> None:
    """Read ``stream`` until ``ledger`` has no live tasks left.

    Takes an already-open iterator rather than the client on purpose: the
    caller's main loop and this drain must share ONE generator. Every
    `client.receive_messages()` call returns a fresh generator over the same
    underlying memory stream, so two of them would split the messages between
    them and leave the first suspended and unclosed.

    ``timeout_s`` is a sub-deadline nested inside the caller's own wall-clock
    bound. It is not needed for liveness -- the outer bound already guarantees
    that -- but for classification: letting a wedged child consume the caller's
    whole remaining budget would surface as a plain operation timeout, which the
    shepherd charges to the proposal, reintroducing mctl-agents#366 by another
    route.

    Raises ``OrphanedSubagentError`` on the deadline OR on stream exhaustion
    with a task still live (the CLI exited early).
    """
    if not ledger.live:
        # Nothing to wait for. Guarded here as well as at the call site so the
        # helper states its own precondition -- otherwise it falls through and
        # raises the self-contradicting "never reported a terminal status ...
        # no outstanding tasks".
        return
    if timeout_s <= 0:
        raise ValueError(
            f"drain timeout must be positive, got {timeout_s!r}: a non-positive "
            f"deadline cancels before the first read and orphans every run"
        )
    with anyio.move_on_after(timeout_s):
        async for message in stream:
            on_message(message)
            ledger.observe(message)
            if not ledger.live:
                return
    raise OrphanedSubagentError(
        f"sub-agent task(s) never reported a terminal status within "
        f"{timeout_s:g}s: {ledger.describe()}"
    )
