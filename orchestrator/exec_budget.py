"""Bound an implementer-owned Bash command to what remains of its envelope.

Built for mctl-agents#430. #423 moved unbounded CI-log retrieval outside the
implementer's execution envelope and denied the unbounded form inside it. Live
acceptance on mctlhq/mctl-telegram#652 showed a second, distinct hole: the
implementer never attempted a forbidden CI-log self-fetch, it did legitimate
local reproduction instead -- `go test -race ./... > /tmp/test-race.log 2>&1
&`, then polled that background shell across repeated Bash calls. Nothing
inside a running implementer knew how much of its envelope was left, so an
agent-issued command was bounded only by the Claude Code CLI's own tool
timeout (which backgrounds a slow command rather than failing it, per ADR-011
"Containment"), and an explicit `cmd &` escapes task accounting entirely
(`subagent_wait.AWAITED_TASK_TYPES` deliberately excludes background shells).
The outer envelope eventually expired with the delegated sub-agent still
live, and the run exited `EXIT_ORPHANED_SUBAGENT` -- blamelessly, but as the
NORMAL result of a slow test suite rather than as the safety net it is meant
to be.

This module holds the pure logic: no `claude_agent_sdk` import at any scope,
so it is importable by the Temporal worker (which deliberately does not load
the agent SDK -- see tests/test_worker_isolation.py) and unit-testable without
it. `orchestrator/options.py` (which already imports the SDK) wires this into
a `PreToolUse` hook; `orchestrator/run_implementer.py` supplies the deadline
and reads the ledger back out.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

# A command label kept in the ledger is bounded, not the full command text --
# the same discipline `run_implementer.MAX_REFUSAL_REASON_CHARS` imposes on
# model-authored text. This ledger is orchestrator-owned, not model prose, but
# an agent controls the command string that lands in `last_command`, so it
# gets the same treatment.
MAX_LEDGER_COMMAND_CHARS = 200


def _truncate_command(command: str) -> str:
    command = " ".join(command.split())
    if len(command) <= MAX_LEDGER_COMMAND_CHARS:
        return command
    return command[:MAX_LEDGER_COMMAND_CHARS] + "…"


@dataclass
class CommandBudgetLedger:
    """Per-run record of every deadline-guard decision.

    Orchestrator-owned and orchestrator-populated -- never fed from model
    text -- so it is safe to serialise verbatim to `--refusal-out` (see
    `run_implementer`'s `EXIT_VERIFICATION_BUDGET_EXHAUSTED` handling).
    """

    clamped: int = 0
    denied_background: int = 0
    denied_exhausted: int = 0
    last_bound_s: float | None = None
    last_command: str = ""
    exhausted: bool = False
    # Internal-only: kept off `as_dict()`/`describe()`'s stable shape but
    # useful for callers that want to see every denial form observed.
    _denial_forms: list[str] = field(default_factory=list, repr=False)

    def record_clamped(self, command: str, bound_s: float) -> None:
        self.clamped += 1
        self.last_bound_s = bound_s
        self.last_command = _truncate_command(command)

    def record_denied_background(self, command: str, form: str) -> None:
        self.denied_background += 1
        self.last_command = _truncate_command(command)
        self._denial_forms.append(form)

    def record_denied_exhausted(self, command: str) -> None:
        self.denied_exhausted += 1
        self.exhausted = True
        self.last_command = _truncate_command(command)

    def describe(self) -> str:
        parts = [
            f"clamped={self.clamped}",
            f"denied_background={self.denied_background}",
            f"denied_exhausted={self.denied_exhausted}",
        ]
        if self.last_bound_s is not None:
            parts.append(f"last_bound_s={self.last_bound_s:g}")
        if self.last_command:
            parts.append(f"last_command={self.last_command!r}")
        if self.exhausted:
            parts.append("exhausted=true")
        return "; ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        """Machine-readable projection written to `--refusal-out` (no model
        prose beyond the truncated command label already bounded above)."""
        return {
            "clamped": self.clamped,
            "denied_background": self.denied_background,
            "denied_exhausted": self.denied_exhausted,
            "last_bound_s": self.last_bound_s,
            "last_command": self.last_command,
            "exhausted": self.exhausted,
            "verification_budget_exhausted": self.exhausted,
            "reason": self.describe(),
        }


def command_budget(
    deadline_monotonic: float,
    now: float,
    *,
    ceiling_s: float,
    reserve_s: float,
    floor_s: float,
) -> float | None:
    """The wall-clock bound for one command, or ``None`` meaning "deny".

    ``min(ceiling_s, remaining - reserve_s)`` -- never wider than
    ``ceiling_s`` (the existing ``IMPLEMENTER_COMMAND_TIMEOUT_SECONDS``,
    reused rather than a new knob), and never wider than what is actually
    left of the envelope once the teardown reserve is set aside. Returns
    ``None`` when even that narrowed bound cannot clear ``floor_s`` --
    admitting a command that provably cannot finish is worse than denying it
    up front.
    """
    remaining = deadline_monotonic - now
    budget = min(ceiling_s, remaining - reserve_s)
    if budget < floor_s:
        return None
    return budget


# A `&` used as the shell's async-list control operator: not part of `&&`
# (a logical AND), not part of `>&`/`<&` (a redirect merge, e.g. `2>&1`), and
# either terminating the command or immediately followed by whitespace (so it
# also catches `cmd & disown`, mid-command). The production shape from
# mctl-agents#430 -- `go test -race ./... > /tmp/test-race.log 2>&1 &` -- is a
# plain trailing token; `2>&1` earlier in the same string is excluded by the
# `(?<![&<>])` lookbehind, since its `&` is preceded by `>`.
_TRAILING_BACKGROUND_RE = re.compile(r"(?<![&<>])&(?!&)(?=\s|$)")
_NOHUP_RE = re.compile(r"\bnohup\b", re.IGNORECASE)
_SETSID_RE = re.compile(r"\bsetsid\b", re.IGNORECASE)
_DISOWN_RE = re.compile(r"\bdisown\b", re.IGNORECASE)

# Ordered (label, pattern) pairs -- `is_detached` returns the first label that
# matches, so the order only affects which name is reported, not whether a
# command is denied.
DETACH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("trailing background (`&`)", _TRAILING_BACKGROUND_RE),
    ("`nohup`", _NOHUP_RE),
    ("`setsid`", _SETSID_RE),
    ("`disown`", _DISOWN_RE),
)


def normalize_shell_command(command: str) -> str:
    """Collapse backslash-newline line continuations the way the shell does.

    Moved here from `orchestrator/options.py` (mctl-agents#423's CI-log guard
    originally owned this) and re-exported from there under its old name so
    that guard's exact matching behaviour is unchanged. See its docstring
    there for the two-pass rationale (mid-token continuations vanish with no
    replacement; whitespace-adjacent continuations collapse to one space) --
    both this module's detachment patterns and options.py's CI-log deny
    patterns rely on a multi-line command being joined into one line before
    they run, or a trailing `\\` before a newline could hide a `&`/`nohup`
    past whichever pattern stops at the literal `\\n`.
    """
    command = re.sub(r"(?<=\S)\\[ \t]*\r?\n(?=\S)", "", command)
    return re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", command)


def is_detached(command: str) -> str | None:
    """Return the matched detachment form, or ``None``.

    ``command`` is normalized first (see `normalize_shell_command`) so a
    line-continued `&`/`nohup`/`setsid`/`disown` cannot hide past a pattern
    anchored on adjacent tokens.
    """
    normalized = normalize_shell_command(command)
    for label, pattern in DETACH_PATTERNS:
        if pattern.search(normalized):
            return label
    return None


def wrap_bounded(command: str, budget_s: float, *, kill_after_s: float) -> str:
    """Render ``command`` under a GNU ``timeout`` wrapper.

    ``timeout --kill-after=<k>s <budget>s bash -c <quoted command>``. GNU
    `timeout` puts the child in its own process group and signals the GROUP
    on expiry, so a test harness that forks workers dies with it;
    `--kill-after` guarantees SIGKILL if SIGTERM is ignored -- the "no live
    child remains" guarantee this proposal exists to provide.

    The whole original command is `shlex.quote`-d as a single argument to
    `bash -c`, which preserves heredocs, pipelines, `&&` chains, multi-line
    scripts and `cd` semantics exactly -- the alternative (parsing and
    rewriting the command) is the "command rewriting breaks a legitimate
    invocation" risk the design explicitly rejects.

    Both bounds are rounded up to whole seconds (`timeout`'s own resolution)
    and floored at 1s so a sub-second budget still renders a valid,
    non-zero invocation rather than `timeout 0s ...`, which fires
    immediately.
    """
    budget = max(1, round(budget_s))
    kill_after = max(1, round(kill_after_s))
    return f"timeout --kill-after={kill_after}s {budget}s bash -c {shlex.quote(command)}"
