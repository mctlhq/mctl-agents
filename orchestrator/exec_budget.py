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

import math
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

# Below one second, `wrap_bounded` renders a FRACTIONAL bound rather than
# flooring to 1s -- flooring there would put the OS bound above the CLI's own
# tool timeout and invert the ordering the whole guard rests on. The fraction
# keeps the bound strictly under the budget; the floor keeps it a duration
# GNU `timeout` will accept rather than an immediate expiry.
SUB_SECOND_BOUND_FRACTION = 0.8
MIN_BOUND_SECONDS = 0.001


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
# `)` closes a subshell, so `( worker & )` backgrounds exactly as a trailing
# `&` does -- and after arithmetic stopped being blanked wholesale, that is
# the spelling `$((worker &) )` reaches the scanner with.
_TRAILING_BACKGROUND_RE = re.compile(r"(?<![&<>])&(?!&)(?=[\s)]|$)")
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


def _mask(command: str, *, mask_double: bool) -> str:
    """The shared quote scanner. See `mask_quoted` for the contract."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in command:
        if escaped:
            out.append("x" if (quote != '"' or mask_double) else ch)
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
        if ch == quote:
            quote = None
            out.append(ch)
        elif quote == '"' and ch == "\\":
            out.append("x" if mask_double else ch)
            escaped = True
        elif quote == '"' and not mask_double:
            out.append(ch)
        else:
            out.append("x")
    return "".join(out)


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

    Masking is the right reading for OPERATORS only. Quoted text can still be
    a shell program in its own right -- `bash -c "cmd &"`, `"$(cmd &)"` -- so
    `detachment_match` additionally recurses into those payloads rather than
    treating "quoted" as "inert" (claude P2 on `624a433`).
    """
    return _mask(command, mask_double=True)


# Shell builtins whose whole point is to mutate the SHELL's own state. The
# Bash tool carries that state (notably the working directory) across calls
# within one session, and `wrap_bounded`'s `bash -c` subshell silently
# discards it -- so `cd /repo` in one call would no longer apply to the next
# (claude P2 on `630ac27`). A command built only from these is left unwrapped:
# it cannot run long, so the OS-level bound buys nothing, while wrapping it
# costs a behaviour change the agent has no way to see.
# Deliberately EXCLUDES `source`/`.`: those execute an arbitrary script, so
# they can run for arbitrarily long, and an unwrapped long command is the
# exact hole this module closes. The cost is real and accepted -- a sourced
# virtualenv no longer survives to the next Bash call, so an agent must
# invoke the interpreter by path (`.venv/bin/python`) instead of activating
# it (claude P3 on `624a433`).
SHELL_STATE_BUILTINS = frozenset({
    "alias", "cd", "export", "popd", "pushd", "set", "shopt",
    "umask", "unalias", "unset",
})

_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|;|\||\n")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")


# Command words whose `-c` argument is a shell PROGRAM, not data. A `&` in
# there backgrounds inside the inner shell, and GNU `timeout` exits with its
# DIRECT child, so the grandchild survives -- the mctl-telegram#652 shape
# reached through a quoted payload (claude P2 on `624a433`).
SHELL_COMMAND_WORDS = frozenset({"ash", "bash", "busybox", "dash", "ksh", "sh", "zsh"})

# How far to follow nested payloads (`bash -c "bash -c '...'"`). Three is well
# past anything legitimate; the cap only stops a pathological input from
# costing unbounded work.
MAX_PAYLOAD_DEPTH = 3

_SUBSTITUTION_OPEN_RE = re.compile(r"\$\(")
_ARITHMETIC_OPEN_RE = re.compile(r"\$\(\(")
# A shell's `-c` need not be a lone token: `bash -lc`, `sh -ec`, `bash -cl`
# all take the next word as the command to run. Only applied once the
# segment's command word is already known to be a shell, so this cannot
# reopen the `git -c user.name=...` false positive that per-segment
# scanning fixed (claude P2 on `4449024`).
_SHELL_C_FLAG_RE = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
# Shell options that consume the NEXT word. Stopping at the first word that
# does not start with `-` would otherwise end the scan on their ARGUMENT:
# `bash -o pipefail -c "go test ./... &"` never reaches its `-c` (claude P2
# on `68f3a05`, confirmed against bash 5). A cluster ending in `o`/`O`
# (`-euo pipefail`) consumes one too.
_OPTIONS_TAKING_A_WORD = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
# Commands that change HOW a command runs and then exec it, leaving the shell
# that follows them in charge of the payload. `env` alone was skipped as a
# bare word, so `env -i bash -c "go test ./... &"` stopped on `-i` and the
# payload was never scanned -- the same envelope escape #430 exists to close,
# reached through a wrapper instead of a flag spelling (claude/agy P3 on
# `24fe327`). Each wrapper's own options are stepped over, including the ones
# that consume the next word.
_WRAPPER_OPTIONS_TAKING_A_WORD: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "sudo": frozenset({
        "-u", "--user", "-g", "--group", "-p", "--prompt", "-C", "--close-from",
        "-D", "--chdir", "-R", "--chroot", "-T", "--command-timeout",
        "-U", "--other-user", "-r", "--role", "-t", "--type", "-h", "--host",
    }),
}


def _arithmetic_spans(visible: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every `$((...))` arithmetic expansion.

    Arithmetic runs no command of its own: its `&`, `|` and `;` are
    operators on numbers, so `$((a & b))` is a bitwise AND and not a
    detachment (claude P3 on `4449024`). Read against an already
    quote-masked view. A command substitution NESTED inside arithmetic
    still executes, and `_command_substitutions` keeps scanning through
    this span so `$(( $(cmd &) ))` is still caught.
    """
    spans: list[tuple[int, int]] = []
    position = 0
    while True:
        opened = _ARITHMETIC_OPEN_RE.search(visible, position)
        if opened is None:
            return spans
        # Count from the INNER paren and require the char that closes it to
        # be immediately followed by `)`. That is what tells arithmetic apart
        # from a command substitution whose first word is a subshell: bash
        # runs both commands in `echo $((echo x) && (echo y))`, so the span
        # `$((a) && (b &))` is NOT arithmetic and its `&` is a real
        # background operator (claude P3 on `8465c6e`, confirmed against
        # bash 5). `$((cmd &) )` fails the same test and stays a payload.
        depth = 1
        index = opened.end()
        while index < len(visible) and depth:
            if visible[index] == "(":
                depth += 1
            elif visible[index] == ")":
                depth -= 1
            index += 1
        if depth or index >= len(visible) or visible[index] != ")":
            position = opened.end()
            continue
        spans.append((opened.start(), index + 1))
        position = index + 1


def _blank_arithmetic(visible: str) -> str:
    """`visible` with every arithmetic expansion blanked, length preserved."""
    spans = _arithmetic_spans(visible)
    if not spans:
        return visible
    chars = list(visible)
    for start, end in spans:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _command_substitutions(command: str) -> list[str]:
    """Contents of every `$(...)` and backtick pair that the shell would run.

    Single-quoted regions are masked (no substitution happens there); DOUBLE
    quotes are left transparent, because `"$(cmd &)"` does substitute.
    """
    visible = _mask(command, mask_double=False)
    arithmetic_starts = {start for start, _ in _arithmetic_spans(visible)}
    found: list[str] = []

    position = 0
    while True:
        opened = _SUBSTITUTION_OPEN_RE.search(visible, position)
        if opened is None:
            break
        if opened.start() in arithmetic_starts:
            # Not a command substitution. Advance INTO it rather than past
            # it, so a `$(cmd)` nested in the arithmetic is still found.
            position = opened.end()
            continue
        depth = 1
        index = opened.end()
        while index < len(visible) and depth:
            if visible[index] == "(":
                depth += 1
            elif visible[index] == ")":
                depth -= 1
            index += 1
        if depth == 0:
            found.append(command[opened.end():index - 1])
        position = opened.end()

    backticks = [i for i, ch in enumerate(visible) if ch == "`"]
    for start, end in zip(backticks[::2], backticks[1::2], strict=False):
        found.append(command[start + 1:end])
    return found


def _segments(command: str) -> list[str]:
    """Split on unquoted `&&`, `||`, `;`, `|` and newlines.

    Shared by `_shell_c_payloads` and `is_shell_state_only` so both judge a
    COMMAND, not a whole command line: `shlex.split` does not partition on
    separators, so scanning the line as one token list lets a token from an
    unrelated segment decide another segment's meaning.
    """
    masked = mask_quoted(command)
    bounds: list[tuple[int, int]] = []
    start = 0
    for separator in _SEGMENT_SPLIT_RE.finditer(masked):
        bounds.append((start, separator.start()))
        start = separator.end()
    bounds.append((start, len(masked)))
    return [command[lo:hi].strip() for lo, hi in bounds]


def _attached_c_payload(word: str, *, has_next_word: bool) -> str | None:
    """The payload of a `-c` whose value is ATTACHED, else None.

    `bash -c'cmd &'` survives `shlex.split` as one token, `-ccmd &`. The
    cluster is scanned letter by letter; the first `c` owns whatever is
    left after it.

    A remainder that is itself pure alphabetic is ambiguous with a flag
    cluster (`-cl` is `-c -l`, not `-c l`), so it is read as a cluster
    whenever a following word exists to be the payload -- and as an
    attached value when there is none, since then it is the only candidate.
    """
    for offset, letter in enumerate(word[1:], start=1):
        if not letter.isalpha():
            return None
        if letter == "c":
            remainder = word[offset + 1:]
            if not remainder:
                return None
            if remainder.isalpha() and has_next_word:
                return None
            return remainder
    return None


def _shell_c_payloads(command: str) -> list[str]:
    """The argument of a `-c` flag passed to a SHELL, if any.

    Judged per segment, and only when the segment's own command word is a
    shell. Scanning the whole line instead let `cd /repo/bash && git -c
    user.name="A & B" commit` read `git -c` as a shell payload -- and since
    `shlex.split` unquotes, the extracted "payload" was `user.name=A & B`,
    whose now-bare `&` tripped the detachment deny (agy P2 on `166133b`).
    """
    found: list[str] = []
    for segment in _segments(command):
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        index = 0
        # Skip leading `VAR=value` assignments and exec wrappers, which change
        # who runs but not what the command word means.
        while index < len(words):
            word = words[index]
            if _ASSIGNMENT_RE.match(word):
                index += 1
                continue
            takes_a_word = _WRAPPER_OPTIONS_TAKING_A_WORD.get(word.split("/")[-1])
            if takes_a_word is None:
                break
            index += 1
            while (
                index < len(words)
                and words[index].startswith("-")
                and words[index] != "-"
            ):
                index += 2 if words[index] in takes_a_word else 1
        if index >= len(words):
            continue
        if words[index].split("/")[-1] not in SHELL_COMMAND_WORDS:
            continue
        position = index + 1
        # `busybox sh -c ...`: the applet name follows the multi-call binary.
        if (
            words[index].split("/")[-1] == "busybox"
            and position < len(words)
            and words[position].split("/")[-1] in SHELL_COMMAND_WORDS
        ):
            position += 1
        while position < len(words):
            word = words[position]
            if word in _OPTIONS_TAKING_A_WORD:
                position += 2
                continue
            if (
                len(word) > 1
                and word[0] in "-+"
                and word[1:].isalpha()
                and word[-1] in "oO"
            ):
                position += 2
                continue
            if not word.startswith("-") or word == "-":
                # The shell's first OPERAND. Everything after it is the
                # script's own argv, where `-ec` is just a string:
                # `bash deploy.sh -ec "restart A & B"` runs no payload
                # (claude P3 on `8465c6e`).
                break
            if word in ("--", "-s"):
                break
            attached = _attached_c_payload(
                word, has_next_word=position + 1 < len(words)
            )
            if attached is not None:
                # `bash -c'cmd &'` lexes as the single token `-ccmd &`.
                found.append(attached)
                break
            if _SHELL_C_FLAG_RE.match(word) and position + 1 < len(words):
                found.append(words[position + 1])
                break
            position += 1
    return found


def detachment_match(command: str, _depth: int = 0) -> tuple[str, str] | None:
    """``(label, fragment)`` for the first detachment form found, else None.

    ``fragment`` is the offending text with a little surrounding context,
    so a denial can quote what actually tripped it instead of leaving the
    agent to guess which part of a long command was the problem.

    Operators are read against the quote-masked command, so quoted text is
    data. But quoted text that the shell will EXECUTE -- a `bash -c` payload,
    a command substitution -- is scanned recursively at its own level, so
    `bash -c "cmd &"` and `"$(cmd &)"` are caught rather than admitted by the
    very masking that fixed the false positives (claude P2 on `624a433`).
    """
    normalized = normalize_shell_command(command)
    masked = _blank_arithmetic(mask_quoted(normalized))
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

    if _depth >= MAX_PAYLOAD_DEPTH:
        return None
    for payload in _command_substitutions(normalized) + _shell_c_payloads(normalized):
        if not payload:
            continue
        nested = detachment_match(payload, _depth + 1)
        if nested is not None:
            label, fragment = nested
            return label, f"{fragment} (inside an executed payload)"
    return None


def is_shell_state_only(command: str) -> bool:
    """True when EVERY command word is a `SHELL_STATE_BUILTINS` member.

    `cd /repo`, `cd a && cd b`, `export FOO=1` -- yes. `cd /repo && go test
    ./...` -- no: it also runs something that can take arbitrarily long, and
    bounding that matters more than preserving the `cd` (which, in that
    shape, the agent wrote as a prefix to THIS command anyway). Callers use
    this to decide whether `wrap_bounded` may rewrite the command at all.

    A command carrying a command substitution is never exempt, however it is
    spelled: `export FOO=$(slow)` is a builtin by command word and an
    unbounded subprocess in fact (claude P3 on `624a433`).

    A command this cannot parse (unbalanced quotes) returns False -- the
    conservative direction, since False only means "bound it as usual".
    """
    normalized = normalize_shell_command(command)
    if _command_substitutions(normalized):
        return False

    saw_a_word = False
    for segment in _segments(normalized):
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

    The rendered bound is ``floor(budget_s)`` whole seconds, shaved by one
    more second when that floor lands exactly on ``budget_s``, and floored at
    1s. Two reasons, both about ORDERING rather than precision:

    - It must never EXCEED ``budget_s``. Rounding to nearest (`round`) does
      exceed it for e.g. 20.6s -> 21s, which puts the CLI's own tool timeout
      (set from the same 20.6s) FIRST -- and the CLI backgrounds rather than
      fails, so the command escapes for the difference. That is the defect
      this module exists to close, reintroduced by a rounding mode (agy P2 on
      `624a433`).
    - It must not merely TIE with it either. At an exact integer budget both
      timers fire in the same instant and which one wins is unspecified; one
      second of headroom makes GNU `timeout` deterministically first, and one
      second out of a budget whose floor is `IMPLEMENTER_MIN_COMMAND_BUDGET_
      SECONDS` (20s by default) is a rounding error, not a real loss.

    Below one second the whole-second form cannot express the ordering at
    all -- flooring to 1s puts the bound back ABOVE a 0.2s tool timeout and
    inverts exactly what this function is for (claude P3 on `aa60779`) -- so
    a fractional bound is rendered instead. GNU `timeout` takes a floating
    point duration, and `SUB_SECOND_BOUND_FRACTION` of the budget keeps the
    bound strictly under it without the integer grid.

    The `--kill-after` grace is rounded UP, never down: it is the backstop
    for a command that ignores SIGTERM, and shortening it weakens exactly the
    guarantee it exists to give. It is deliberately NOT subtracted from the
    bound as well -- requiring `bound + grace <= budget_s` is unsatisfiable
    whenever the budget is at or below the grace, and the meaningful
    guarantee is that SIGTERM lands before the CLI's timer, not that the
    pathological SIGKILL does too.
    """
    # Compared in MILLISECONDS, because that is the unit the CLI's own tool
    # timeout is set in: at 20.0004s the floor renders `20s` while the CLI
    # gets `int(20.0004 * 1000)` = 20000 ms, and the two bounds tie instead
    # of the OS one landing first (claude P3 on `68f3a05`).
    budget_ms = int(budget_s * 1000)
    whole = math.floor(budget_s)
    if whole * 1000 >= budget_ms:
        whole -= 1
    if whole >= 1:
        rendered = f"{whole:d}"
    else:
        fractional = max(MIN_BOUND_SECONDS, budget_s * SUB_SECOND_BOUND_FRACTION)
        rendered = f"{fractional:.3f}".rstrip("0").rstrip(".")
    kill_after = max(1, math.ceil(kill_after_s))
    return (
        f"timeout --kill-after={kill_after}s {rendered}s "
        f"bash -c {shlex.quote(command)}"
    )
