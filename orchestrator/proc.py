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
import threading

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


def describe_output(stdout, stderr) -> str:
    """Render captured streams for an error message, bounded and never empty.

    Both streams, never one or the other. Which one holds the reason is not
    knowable here: git writes progress to stderr and can put the fatal line on
    stdout, so preferring stderr would print the noise and drop the cause — the
    exact failure this module exists to prevent. stderr goes last because that
    is where the reason usually is, and a truncated log is read from the end.

    Public because the timeout path needs it too: `subprocess.TimeoutExpired`
    also carries partial output, and discarding it leaves an operator with a
    hung command and no clue what it was doing.
    """
    parts = []
    out, err = _tail(stdout), _tail(stderr)
    if out:
        parts.append(f"stdout: {out}")
    if err:
        parts.append(f"stderr: {err}")
    if not parts:
        return "(no output captured)"
    return " ".join(parts)


class CommandFailed(subprocess.CalledProcessError):
    """A `CalledProcessError` whose message carries the captured output.

    Subclasses rather than replaces `CalledProcessError` so existing
    `except subprocess.CalledProcessError` handlers keep working, and
    `.returncode` / `.stdout` / `.stderr` stay where callers expect them.
    """

    def __str__(self) -> str:
        return f"{super().__str__()} {describe_output(self.stdout, self.stderr)}"


def run_capturing(
    cmd: list[str],
    *,
    cwd=None,
    check: bool = True,
    timeout: float | None = None,
    max_output_bytes: int | None = None,
) -> subprocess.CompletedProcess:
    """`subprocess.run` with captured text output, raising `CommandFailed`.

    Identical to `subprocess.run(cmd, cwd=cwd, check=check, text=True,
    capture_output=True, timeout=timeout)` except that a non-zero exit with
    `check=True` raises `CommandFailed`, so the reason survives into whatever
    logs the exception.

    `max_output_bytes`, when given, caps how much of stdout this process
    ever reads into memory: the read stops at that many bytes instead of
    buffering the child's entire output first (what plain
    `capture_output=True` does) and trimming it down only afterward — the
    gap an unbounded CI log fetch can walk right through (mctl-agents#423
    review P2). Hitting the cap kills the child rather than draining it to
    completion, but is NOT treated as a command failure: the caller gets
    back the (truncated) bytes it already read with `returncode=0`, same as
    a `check=True` success. A `timeout` breach still raises
    `subprocess.TimeoutExpired`, same as the plain path above.
    """
    if max_output_bytes is None:
        proc = subprocess.run(  # noqa: S603 — cmd is the caller's list[str], never shell=True
            cmd,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    else:
        proc = _run_capturing_bounded(cmd, cwd=cwd, timeout=timeout, max_output_bytes=max_output_bytes)
    if check and proc.returncode != 0:
        raise CommandFailed(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)
    return proc


def _run_capturing_bounded(
    cmd: list[str], *, cwd, timeout: float | None, max_output_bytes: int
) -> subprocess.CompletedProcess:
    """The `max_output_bytes` path of `run_capturing` — reads stdout in
    capped chunks instead of `communicate()`-ing the whole thing.

    A `threading.Timer` mirrors what `subprocess.run(timeout=...)` does
    internally: it kills the child (and marks `timed_out`) if `timeout`
    elapses, which also unblocks a `.read()` call that was stuck waiting on
    a hung child. That is the only job the timer has; the byte cap is
    enforced directly by the read loop below.
    """
    popen = subprocess.Popen(  # noqa: S603 — cmd is the caller's list[str], never shell=True
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    # Always present: both were just opened as PIPE above.
    assert popen.stdout is not None
    assert popen.stderr is not None

    timed_out = threading.Event()
    timer: threading.Timer | None = None
    timeout_used = 0.0
    if timeout is not None:
        timeout_used = timeout

        def _on_timeout() -> None:
            timed_out.set()
            popen.kill()

        timer = threading.Timer(timeout, _on_timeout)
        timer.start()

    try:
        chunks: list[bytes] = []
        total = 0
        while total < max_output_bytes:
            chunk = popen.stdout.read(min(65536, max_output_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        capped = total >= max_output_bytes
        if capped:
            # Stop the child from producing (and us from buffering) any
            # more than we will ever keep.
            popen.kill()
        stdout_bytes = b"".join(chunks)
        stderr_bytes = popen.stderr.read()
        popen.wait()
    finally:
        if timer is not None:
            timer.cancel()
        popen.stdout.close()
        popen.stderr.close()

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout_used, output=stdout_bytes)

    returncode = 0 if capped else popen.returncode
    return subprocess.CompletedProcess(
        cmd,
        returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )
