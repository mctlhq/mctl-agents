"""The point of orchestrator.proc is that a failure says *why* it failed."""
from __future__ import annotations

import subprocess

import pytest

from orchestrator.proc import CommandFailed, describe_output, run_capturing


def test_success_returns_completed_process():
    proc = run_capturing(["python", "-c", "print('hi')"])
    assert proc.returncode == 0
    assert proc.stdout.strip() == "hi"


def test_failure_message_carries_stderr():
    with pytest.raises(CommandFailed) as excinfo:
        run_capturing(["python", "-c", "import sys; sys.stderr.write('401 Bad credentials'); sys.exit(1)"])
    message = str(excinfo.value)
    assert "401 Bad credentials" in message, message
    # The plain CalledProcessError text must still be there — it carries the
    # command and exit status.
    assert "exit status 1" in message


def test_failure_is_a_called_process_error():
    """Existing `except subprocess.CalledProcessError` handlers must keep working."""
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        run_capturing(["python", "-c", "import sys; sys.exit(3)"])
    assert excinfo.value.returncode == 3


def test_keeps_both_streams_when_both_have_output():
    """git writes progress to stderr and can put the fatal line on stdout.

    Preferring one stream would print the noise and drop the reason.
    """
    script = (
        "import sys; "
        "sys.stderr.write('remote: Enumerating objects'); "
        "sys.stdout.write('fatal: the real reason'); "
        "sys.exit(1)"
    )
    with pytest.raises(CommandFailed) as excinfo:
        run_capturing(["python", "-c", script])
    message = str(excinfo.value)
    assert "fatal: the real reason" in message, message
    assert "remote: Enumerating objects" in message, message


def test_falls_back_to_stdout_when_stderr_is_empty():
    """git reports some failures on stdout; an empty stderr must not hide them."""
    with pytest.raises(CommandFailed) as excinfo:
        run_capturing(["python", "-c", "print('nothing to commit'); raise SystemExit(1)"])
    assert "nothing to commit" in str(excinfo.value)


def test_says_so_when_there_is_no_output_at_all():
    with pytest.raises(CommandFailed) as excinfo:
        run_capturing(["python", "-c", "raise SystemExit(1)"])
    assert "no output captured" in str(excinfo.value)


def test_long_stderr_is_truncated_but_keeps_the_tail():
    """The tail is where the actual error is; the head is usually progress noise."""
    script = (
        "import sys; sys.stderr.write('x' * 5000 + 'FINAL ERROR'); sys.exit(1)"
    )
    with pytest.raises(CommandFailed) as excinfo:
        run_capturing(["python", "-c", script])
    message = str(excinfo.value)
    assert "FINAL ERROR" in message
    assert "truncated" in message
    assert len(message) < 3000


def test_check_false_returns_instead_of_raising():
    proc = run_capturing(["python", "-c", "import sys; sys.exit(2)"], check=False)
    assert proc.returncode == 2


def test_timeout_still_raises_timeout_expired():
    """run_implementer catches TimeoutExpired by type — don't swallow it."""
    with pytest.raises(subprocess.TimeoutExpired):
        run_capturing(["python", "-c", "import time; time.sleep(5)"], timeout=0.3)


def test_describe_output_renders_both_streams():
    assert describe_output("out", "err") == "stdout: out stderr: err"


def test_describe_output_says_so_when_empty():
    assert describe_output("", None) == "(no output captured)"


def test_describe_output_accepts_bytes():
    """TimeoutExpired hands back bytes when the child was not started in text mode."""
    assert describe_output(b"out", b"err") == "stdout: out stderr: err"


# ---------------------------------------------------------------------------
# `max_output_bytes` (`_run_capturing_bounded`) — mctl-agents#423 review P2:
# this path had no test coverage at all.
# ---------------------------------------------------------------------------
def test_bounded_path_returns_full_output_under_the_cap():
    proc = run_capturing(
        ["python", "-c", "import sys; sys.stdout.write('hi'); sys.stderr.write('warn')"],
        max_output_bytes=1000,
    )
    assert proc.returncode == 0
    assert proc.stdout == "hi"
    assert proc.stderr == "warn"


def test_bounded_path_still_raises_on_a_real_failure_within_the_cap():
    with pytest.raises(CommandFailed) as excinfo:
        run_capturing(
            ["python", "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"],
            max_output_bytes=1000,
        )
    assert "boom" in str(excinfo.value)


def test_bounded_path_truncates_stdout_and_does_not_treat_the_cap_as_a_failure():
    """Hitting the cap kills the child rather than draining it to completion,
    but is NOT a command failure: `returncode=0` even though the child, left
    to run, would have exited non-zero."""
    proc = run_capturing(
        [
            "python", "-c",
            "import sys; sys.stdout.write('x' * 100000); sys.stdout.flush(); sys.exit(3)",
        ],
        max_output_bytes=100,
    )
    assert proc.returncode == 0
    assert len(proc.stdout) == 100


def test_bounded_path_timeout_still_raises_timeout_expired():
    with pytest.raises(subprocess.TimeoutExpired):
        run_capturing(
            ["python", "-c", "import time; time.sleep(5)"],
            timeout=0.3,
            max_output_bytes=1000,
        )


def test_bounded_path_drains_stderr_concurrently_so_a_full_pipe_does_not_deadlock():
    """A child that fills the stderr pipe before ever writing to stdout used
    to deadlock the bounded path: the stdout read loop blocked waiting for
    output while the child blocked writing stderr into a full, undrained
    pipe (mctl-agents#423 review P2). Draining stderr on its own thread lets
    the child make progress regardless. The 300000-byte stderr write is well
    past the OS pipe buffer (typically 64KB), so the old implementation
    would reliably hang here until the `timeout` below killed it and this
    assertion failed with a `TimeoutExpired`; the fix completes almost
    immediately with both streams intact.
    """
    script = (
        "import sys; "
        "sys.stderr.write('e' * 300000); sys.stderr.flush(); "
        "sys.stdout.write('o' * 300000); sys.stdout.flush()"
    )
    proc = run_capturing(
        ["python", "-c", script],
        timeout=15,
        max_output_bytes=1_000_000,
    )
    assert proc.returncode == 0
    assert len(proc.stdout) == 300000
    assert len(proc.stderr) == 300000
