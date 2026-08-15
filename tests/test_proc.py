"""The point of orchestrator.proc is that a failure says *why* it failed."""
from __future__ import annotations

import subprocess

import pytest

from orchestrator.proc import CommandFailed, run_capturing


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
