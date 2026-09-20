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
#
# Every pattern below runs against the QUOTE-MASKED command (`mask_quoted`),
# never the raw string: `git commit -m "A & B"` and `echo 'nohup'` carry these
# characters as DATA, and denying them was a false positive that blocked
# ordinary work (claude P2 on `630ac27`).
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


def mask_quoted(command: str) -> str:
    """Replace every quoted character with ``x``, preserving length.

    Quote characters, backslashes and everything outside quotes are kept
    verbatim, so offsets into the mask are offsets into the input and a
    pattern that matches the mask can be reported against the original text.
    A backslash-escaped character outside quotes is masked too (`\\&` is a
    literal ampersand, not the async-list operator). An unterminated quote
    masks to end of string -- the shell would not run such a command anyway,
    and masking the tail is the conservative direction: it can only turn a
    would-be denial into an allow, never invent one.

    Exists because every detachment pattern here, and every command-word scan
    below, must read the command's SHELL STRUCTURE rather than its data.
    """
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            out.append("x")
            escaped = False
            continue
        if quote is None:
            if ch == "\\":
                out.append(ch)
                escaped = True
            elif ch in ("'", '"'):
                quote = ch
                out.append(ch)
            else:
                out.append(ch)
            continue
        # Inside quotes. Single quotes have no escapes at all.
        if ch == quote:
            quote = None
            out.append(ch)
        elif quote == '"' and ch == "\\":
            out.append("x")
            escaped = True
        else:
            out.append("x")
    return "".join(out)


# Shell builtins whose whole point is to mutate the SHELL's own state. The
# Bash tool carries that state (notably the working directory) across calls
# within one session, and `wrap_bounded`'s `bash -c` subshell silently
# discards it -- so `cd /repo` in one call would no longer apply to the next
# (claude P2 on `630ac27`). A command built only from these is left unwrapped:
# it cannot run long, so the OS-level bound buys nothing, while wrapping it
# costs a behaviour change the agent has no way to see.
SHELL_STATE_BUILTINS = frozenset({
    ".", "alias", "cd", "export", "popd", "pushd", "set", "shopt",
    "source", "umask", "unalias", "unset",
})

_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|;|\||\n")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")


def detachment_match(command: str) -> tuple[str, str] | None:
    """``(label, fragment)`` for the first detachment form found, else None.

    ``fragment`` is the offending text with a little surrounding context,
    so a denial can quote what actually tripped it instead of leaving the
    agent to guess which part of a long command was the problem.
    """
    normalized = normalize_shell_command(command)
    masked = mask_quoted(normalized)
    for label, pattern in DETACH_PATTERNS:
        found = pattern.search(masked)
        if found is None:
            continue
        start = max(0, found.start() - 20)
        end = min(len(normalized), found.end() + 20)
        fragment = normalized[start:end].strip()
        if start > 0:
            fragment = "…" + fragment
        if end < len(normalized):
            fragment = fragment + "…"
        return label, fragment
    return None


def is_shell_state_only(command: str) -> bool:
    """True when EVERY command word is a `SHELL_STATE_BUILTINS` member.

    `cd /repo`, `cd a && cd b`, `export FOO=1` -- yes. `cd /repo && go test
    ./...` -- no: it also runs something that can take arbitrarily long, and
    bounding that matters more than preserving the `cd` (which, in that
    shape, the agent wrote as a prefix to THIS command anyway). Callers use
    this to decide whether `wrap_bounded` may rewrite the command at all.

    A command this cannot parse (unbalanced quotes) returns False -- the
    conservative direction, since False only means "bound it as usual".
    """
    normalized = normalize_shell_command(command)
    masked = mask_quoted(normalized)
    bounds: list[tuple[int, int]] = []
    start = 0
    for separator in _SEGMENT_SPLIT_RE.finditer(masked):
        bounds.append((start, separator.start()))
        start = separator.end()
    bounds.append((start, len(masked)))

    saw_a_word = False
    for lo, hi in bounds:
        segment = normalized[lo:hi].strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        index = 0
        while index < len(words) and _ASSIGNMENT_RE.match(words[index]):
            index += 1
        if index >= len(words):
            # A bare `FOO=1` assignment: shell state too, by the same logic.
            saw_a_word = True
            continue
        if words[index] not in SHELL_STATE_BUILTINS:
            return False
        saw_a_word = True
    return saw_a_word


def is_detached(command: str) -> str | None:
    """Return the matched detachment form, or ``None``.

    ``command`` is normalized first (see `normalize_shell_command`) so a
    line-continued `&`/`nohup`/`setsid`/`disown` cannot hide past a pattern
    anchored on adjacent tokens, then quote-masked (see `mask_quoted`) so the
    same characters appearing as DATA are not mistaken for shell structure.
    """
    found = detachment_match(command)
    return None if found is None else found[0]


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

    ``budget_s`` is the EFFECTIVE bound, not the envelope-derived one: the
    caller must already have narrowed it against any caller-supplied tool
    timeout. Passing the wider envelope bound while the CLI's own tool
    timeout is narrower reinstates the very defect this module exists to
    close -- the CLI backgrounds the command at ITS timeout (ADR-011
    "Containment") while `timeout` sits on the process group until the wider
    bound, so the process escapes for the difference (agy P2 on `630ac27`).

    Both bounds are rounded up to whole seconds (`timeout`'s own resolution)
    and floored at 1s so a sub-second budget still renders a valid,
    non-zero invocation rather than `timeout 0s ...`, which fires
    immediately.
    """
    budget = max(1, round(budget_s))
    kill_after = max(1, round(kill_after_s))
    return f"timeout --kill-after={kill_after}s {budget}s bash -c {shlex.quote(command)}"
