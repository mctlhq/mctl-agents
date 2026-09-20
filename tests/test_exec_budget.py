"""Unit tests for orchestrator.exec_budget (mctl-agents#430).

Pure-logic module -- no `claude_agent_sdk` import at any scope, so these
tests exercise it directly rather than through a hook. See
tests/test_worker_isolation.py for the module-import-graph guard that keeps
it that way.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator import exec_budget

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_module_does_not_import_the_agent_sdk() -> None:
    """Importable by the Temporal worker, and unit-testable without the SDK.

    A fresh subprocess, not an in-process `sys.modules` check: pytest has
    already imported half the codebase (including the SDK, via other test
    modules) by the time this runs in the same session, so `sys.modules`
    here would prove nothing — see tests/test_worker_isolation.py, whose
    module docstring explains the same thing about its own check.
    """
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import orchestrator.exec_budget, sys; "
            "assert 'claude_agent_sdk' not in sys.modules",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]


# ---------------------------------------------------------------------------
# command_budget — T1
# ---------------------------------------------------------------------------
def test_command_budget_early_in_the_envelope_clamps_to_the_ceiling() -> None:
    # 1000s remaining, reserve 120 -> 880 available, but the ceiling (300)
    # is narrower.
    assert exec_budget.command_budget(
        1000.0, 0.0, ceiling_s=300.0, reserve_s=120.0, floor_s=20.0,
    ) == 300.0


def test_command_budget_mid_envelope_clamps_to_remaining_minus_reserve() -> None:
    # 1000s deadline, 700s elapsed -> 300s remaining, minus 120s reserve = 180s,
    # narrower than the 300s ceiling.
    assert exec_budget.command_budget(
        1000.0, 700.0, ceiling_s=300.0, reserve_s=120.0, floor_s=20.0,
    ) == 180.0


def test_command_budget_below_the_floor_denies() -> None:
    # 1000s deadline, 995s elapsed -> 5s remaining, minus 120s reserve is
    # negative, well under the 20s floor.
    assert exec_budget.command_budget(
        1000.0, 995.0, ceiling_s=300.0, reserve_s=120.0, floor_s=20.0,
    ) is None


def test_command_budget_exactly_at_the_floor_is_admitted() -> None:
    # remaining - reserve == floor exactly -> admitted, not denied.
    assert exec_budget.command_budget(
        1000.0, 860.0, ceiling_s=300.0, reserve_s=120.0, floor_s=20.0,
    ) == 20.0


def test_command_budget_never_widens_above_the_ceiling() -> None:
    """Clamping is one-directional (EARS: 'never widen a command's bound')."""
    huge_remaining = exec_budget.command_budget(
        1_000_000.0, 0.0, ceiling_s=300.0, reserve_s=120.0, floor_s=20.0,
    )
    assert huge_remaining == 300.0


# ---------------------------------------------------------------------------
# is_detached — T2
# ---------------------------------------------------------------------------
def test_is_detached_matches_the_production_shape_from_mctl_telegram_652() -> None:
    assert exec_budget.is_detached(
        "go test -race ./... > /tmp/test-race.log 2>&1 &"
    ) is not None


def test_is_detached_matches_nohup_setsid_disown() -> None:
    assert exec_budget.is_detached("nohup ./run.sh") is not None
    assert exec_budget.is_detached("setsid ./run.sh") is not None
    assert exec_budget.is_detached("./run.sh & disown") is not None


def test_is_detached_matches_a_backslash_continued_trailing_ampersand() -> None:
    assert exec_budget.is_detached("go test ./... \\\n  &") is not None


def test_is_detached_does_not_match_logical_and() -> None:
    assert exec_budget.is_detached("a && b") is None


def test_is_detached_does_not_match_redirect_merges() -> None:
    assert exec_budget.is_detached("cmd 2>&1") is None
    assert exec_budget.is_detached("cmd 1>&2") is None
    assert exec_budget.is_detached("go test -race ./... > /tmp/test-race.log 2>&1") is None


def test_is_detached_does_not_match_a_quoted_ampersand() -> None:
    assert exec_budget.is_detached("grep '&' file") is None


def test_is_detached_returns_none_for_an_ordinary_command() -> None:
    assert exec_budget.is_detached("pytest -q") is None
    assert exec_budget.is_detached("git status") is None


# ---------------------------------------------------------------------------
# wrap_bounded — T3
# ---------------------------------------------------------------------------
def test_wrap_bounded_always_carries_kill_after() -> None:
    # 29s, not 30s: an exact-integer budget is shaved by one second so GNU
    # `timeout` fires deterministically BEFORE the CLI's own tool timeout,
    # which is set from the same budget and backgrounds rather than fails
    # (agy P2 on `624a433`).
    wrapped = exec_budget.wrap_bounded("pytest -q", 30.0, kill_after_s=5.0)
    assert wrapped.startswith("timeout --kill-after=5s 29s bash -c ")


def test_wrap_bounded_round_trips_a_heredoc() -> None:
    command = "cat <<'EOF'\nhello\nEOF"
    wrapped = exec_budget.wrap_bounded(command, 10.0, kill_after_s=5.0)
    # The whole original command must be a single shlex-quoted argument to
    # `bash -c`, preserving the heredoc verbatim.
    import shlex
    tokens = shlex.split(wrapped)
    assert tokens[-1] == command


def test_wrap_bounded_round_trips_a_pipeline_and_and_chain() -> None:
    command = "echo hi | grep h && echo done"
    wrapped = exec_budget.wrap_bounded(command, 10.0, kill_after_s=5.0)
    import shlex
    tokens = shlex.split(wrapped)
    assert tokens[-1] == command


def test_wrap_bounded_round_trips_a_multiline_script() -> None:
    command = "set -e\ncd /tmp\nls -la"
    wrapped = exec_budget.wrap_bounded(command, 10.0, kill_after_s=5.0)
    import shlex
    tokens = shlex.split(wrapped)
    assert tokens[-1] == command


def test_wrap_bounded_renders_sub_second_budgets_fractionally() -> None:
    """Flooring to 1s here would put the OS bound ABOVE a 0.2s tool timeout
    and invert the ordering the guard rests on (claude P3 on `aa60779`). GNU
    `timeout` takes a floating point duration, so the bound stays under the
    budget instead."""
    wrapped = exec_budget.wrap_bounded("echo hi", 0.2, kill_after_s=0.1)
    assert wrapped.startswith("timeout --kill-after=1s 0.16s bash -c ")


# ---------------------------------------------------------------------------
# CommandBudgetLedger
# ---------------------------------------------------------------------------
def test_ledger_records_clamped_commands() -> None:
    ledger = exec_budget.CommandBudgetLedger()
    ledger.record_clamped("pytest -q", 42.0)
    assert ledger.clamped == 1
    assert ledger.last_bound_s == 42.0
    assert ledger.last_command == "pytest -q"
    assert ledger.exhausted is False


def test_ledger_records_denied_background() -> None:
    ledger = exec_budget.CommandBudgetLedger()
    ledger.record_denied_background("cmd &", "trailing background (`&`)")
    assert ledger.denied_background == 1
    assert ledger.exhausted is False


def test_ledger_records_denied_exhausted_and_sets_exhausted() -> None:
    ledger = exec_budget.CommandBudgetLedger()
    ledger.record_denied_exhausted("go test ./...")
    assert ledger.denied_exhausted == 1
    assert ledger.exhausted is True


def test_ledger_as_dict_is_machine_readable_and_bounded() -> None:
    ledger = exec_budget.CommandBudgetLedger()
    ledger.record_denied_exhausted("x" * 10_000)
    payload = ledger.as_dict()
    assert payload["exhausted"] is True
    assert payload["verification_budget_exhausted"] is True
    assert payload["denied_exhausted"] == 1
    assert len(payload["last_command"]) <= exec_budget.MAX_LEDGER_COMMAND_CHARS + 1
    assert isinstance(payload["reason"], str)


def test_ledger_describe_is_a_short_summary_line() -> None:
    ledger = exec_budget.CommandBudgetLedger()
    assert "clamped=0" in ledger.describe()
    assert "exhausted=true" not in ledger.describe()
    ledger.record_denied_exhausted("cmd")
    assert "exhausted=true" in ledger.describe()


# ---------------------------------------------------------------------------
# Quote awareness (claude P2 on `630ac27`): these characters are DATA inside
# quotes, and denying an ordinary command for carrying them was a false
# positive that blocked real work.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "A & B"',
        "git commit -m 'fix & polish'",
        'echo "nohup is a word"',
        "grep -r 'disown' .",
        'python -c "print(1) # setsid"',
        r"git commit -m A\ \&\ B",
    ],
)
def test_is_detached_treats_quoted_operators_as_data(command):
    assert exec_budget.is_detached(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "go test -race ./... > /tmp/test-race.log 2>&1 &",
        'git commit -m "A & B" && sleep 100 &',
        "nohup go test ./...",
        "setsid make check",
        "go test ./... & disown",
    ],
)
def test_is_detached_still_catches_real_detachment(command):
    assert exec_budget.is_detached(command) is not None


def test_detachment_match_reports_the_offending_fragment():
    found = exec_budget.detachment_match(
        "go test -race ./... > /tmp/test-race.log 2>&1 &"
    )
    assert found is not None
    label, fragment = found
    assert "&" in label
    # The fragment must quote the actual text, not just name the form.
    assert "test-race.log" in fragment


def test_mask_quoted_preserves_length_and_structure():
    command = 'git commit -m "A & B" && echo ok'
    masked = exec_budget.mask_quoted(command)
    assert len(masked) == len(command)
    # The `&&` outside quotes survives; the `&` inside quotes does not.
    assert "&&" in masked
    assert masked.count("&") == 2


def test_mask_quoted_masks_an_unterminated_quote_to_end_of_string():
    masked = exec_budget.mask_quoted('echo "oops & more')
    assert masked.count("&") == 0


# ---------------------------------------------------------------------------
# Shell-state-only commands (claude P2 on `630ac27`): `bash -c` would discard
# the working directory the Bash tool carries across calls.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    ["cd /repo", "cd /repo && cd src", "export FOO=1", "FOO=1", "umask 022"],
)
def test_is_shell_state_only_accepts_pure_state_commands(command):
    assert exec_budget.is_shell_state_only(command) is True


@pytest.mark.parametrize(
    "command",
    ["cd /repo && go test ./...", "pytest -q", "cd /repo; make check", ""],
)
def test_is_shell_state_only_rejects_anything_that_also_runs_work(command):
    assert exec_budget.is_shell_state_only(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # `source`/`.` execute an arbitrary script: state-mutating AND
        # potentially long, so the exemption would reopen the hole.
        "source .venv/bin/activate",
        ". ./env.sh",
        # A builtin by command word, an unbounded subprocess in fact.
        "export FOO=$(slow)",
        "export FOO=`slow`",
        "cd $(find / -name x)",
    ],
)
def test_is_shell_state_only_refuses_state_commands_that_can_run_long(command):
    """claude P3 on `624a433`: the exemption exists because these commands
    cannot run long. One that can must not get it."""
    assert exec_budget.is_shell_state_only(command) is False


def test_is_shell_state_only_is_conservative_on_unparseable_input():
    assert exec_budget.is_shell_state_only('cd "/repo') is False


# ---------------------------------------------------------------------------
# Ordering between the OS bound and the CLI's own tool timeout (agy P2 on
# `624a433`): GNU `timeout` must fire STRICTLY FIRST, or the CLI backgrounds
# a still-live command and the orphan window reopens.
# ---------------------------------------------------------------------------
def _rendered_bound(budget_s: float, kill_after_s: float = 5.0) -> float:
    rendered = exec_budget.wrap_bounded(budget_s=budget_s, command="x", kill_after_s=kill_after_s)
    found = re.search(r"--kill-after=\d+s ([\d.]+)s bash -c ", rendered)
    assert found is not None, rendered
    return float(found.group(1))


@pytest.mark.parametrize(
    "budget_s,want_bound",
    [
        (300.0, 299.0),  # exact integer: shaved by one so it cannot tie
        (20.6, 20.0),    # fractional: floored, never rounded UP past the budget
        (20.4, 20.0),
        (5.0, 4.0),
        (2.0, 1.0),
        (1.2, 1.0),
        (1.0, 0.8),      # below the integer grid: rendered fractionally
        (0.2, 0.16),
    ],
)
def test_wrap_bounded_never_exceeds_the_budget(budget_s, want_bound):
    bound = _rendered_bound(budget_s)
    assert bound == pytest.approx(want_bound)
    # The invariant the ordering rests on, stated directly.
    assert bound < budget_s


def test_the_rendered_bound_stays_under_the_budget_across_the_range():
    """One property, checked over the whole range rather than row by row."""
    for budget_s in (0.05, 0.2, 0.9, 1.0, 1.5, 2.0, 19.9, 20.0, 119.7, 300.0, 600.0):
        assert _rendered_bound(budget_s) < budget_s, budget_s


def test_wrap_bounded_rounds_the_kill_grace_up_not_down():
    """Shortening the SIGKILL backstop weakens the one guarantee it gives."""
    rendered = exec_budget.wrap_bounded(budget_s=300.0, command="x", kill_after_s=4.2)
    assert "--kill-after=5s" in rendered


# ---------------------------------------------------------------------------
# Quoted text that the shell EXECUTES (claude P2 on `624a433`): masking is the
# right reading for operators, but a `bash -c` payload or a command
# substitution is a shell program, and GNU `timeout` exits with its DIRECT
# child -- so a backgrounded grandchild in there is the #652 shape again.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        'bash -c "cmd &"',
        "bash -c 'go test ./... &'",
        "sh -c 'nohup ./run.sh'",
        "/bin/bash -c 'setsid ./run.sh'",
        'echo "$(cmd &)"',
        "X=`slow &` echo hi",
        'bash -c "bash -c \'x &\'"',
    ],
)
def test_detachment_match_follows_executed_payloads(command):
    found = exec_budget.detachment_match(command)
    assert found is not None, command
    assert "inside an executed payload" in found[1]


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "A & B"',
        "bash -c 'echo \"A & B\"'",
        'echo "$(date)"',
        "grep -r 'disown' .",
        'python -c "print(1) # setsid"',
    ],
)
def test_executed_payload_recursion_does_not_reintroduce_false_positives(command):
    assert exec_budget.detachment_match(command) is None


def test_payload_recursion_is_depth_bounded():
    """A pathological nest must cost bounded work, not unbounded."""
    nested = "cmd &"
    for _ in range(exec_budget.MAX_PAYLOAD_DEPTH + 3):
        nested = f"bash -c {nested!r}"
    # Either it is caught within the cap or it is not; what must not happen is
    # recursion without a limit.
    exec_budget.detachment_match(nested)
    assert exec_budget.MAX_PAYLOAD_DEPTH >= 1
