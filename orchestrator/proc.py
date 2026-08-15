"""Subprocess helpers that keep stderr visible in the raised error.

`subprocess.run(..., check=True, capture_output=True)` raises
`CalledProcessError`, whose `str()` is only

    Command '[...]' returned non-zero exit status 1.

The captured stderr lives on the exception but never reaches the message, so a
runner that lets the exception propagate logs the exit status and nothing else.
That is how a `401 Bad credentials` from `gh` stayed invisible in Temporal
workflow history for 9+ hours on 2026-08-15 — the cause was only found by
running the command by hand inside the pod.
"""
from __future__ import annotations

import subprocess

# How much of each stream to keep in the message. Enough for a `gh` or `git`
# error (which is a line or two), short of pasting a whole build log into every
# workflow history entry.
_MAX_STREAM_CHARS = 2000


def _tail(stream: str | bytes | None, limit: int = _MAX_STREAM_CHARS) -> str:
    """Last `limit` characters of a captured stream, or "" if there is none."""
    if not stream:
        return ""
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", errors="replace")
    stream = stream.strip()
    if len(stream) <= limit:
        return stream
    return "...(truncated)... " + stream[-limit:]


class CommandFailed(subprocess.CalledProcessError):
    """A `CalledProcessError` whose message carries the captured output.

    Subclasses rather than replaces `CalledProcessError` so existing
    `except subprocess.CalledProcessError` handlers keep working, and
    `.returncode` / `.stdout` / `.stderr` stay where callers expect them.
    """

    def __str__(self) -> str:
        # Both streams, never one or the other. Which one holds the reason is
        # not knowable here: git writes progress to stderr and can put the
        # fatal line on stdout, so preferring stderr would print the noise and
        # drop the cause — the exact failure this class exists to prevent.
        # stderr goes last because that is where the reason usually is, and a
        # truncated log is read from the end.
        parts = [super().__str__()]
        stdout, stderr = _tail(self.stdout), _tail(self.stderr)
        if stdout:
            parts.append(f"stdout: {stdout}")
        if stderr:
            parts.append(f"stderr: {stderr}")
        if len(parts) == 1:
            parts.append("(no output captured)")
        return " ".join(parts)


def run_capturing(
    cmd: list[str],
    *,
    cwd=None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """`subprocess.run` with captured text output, raising `CommandFailed`.

    Identical to `subprocess.run(cmd, cwd=cwd, check=check, text=True,
    capture_output=True, timeout=timeout)` except that a non-zero exit with
    `check=True` raises `CommandFailed`, so the reason survives into whatever
    logs the exception.
    """
    proc = subprocess.run(  # noqa: S603 — cmd is the caller's list[str], never shell=True
        cmd,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise CommandFailed(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)
    return proc
