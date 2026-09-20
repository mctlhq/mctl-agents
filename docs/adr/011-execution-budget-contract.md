# ADR 011 — Execution-budget contract for CI-remediation work

> **Status:** proposed
> **Date:** 2026-09-20
> **Issue:** mctlhq/mctl-agents#423, mctlhq/mctl-agents#430
> **Supersedes:** nothing. It records the boundary this proposal draws
> between four related-but-distinct invariants (#411, #418, #423, #430) so
> future changes to any one of them do not accidentally reopen another.

## Context

mctl-agents#411 made a failing required CI check a first-class
review-remediation blocker: `ci_checks.read_required_checks()` turns the
per-context nodes on a PR head into `CheckBlocker` records, and the Tier 3
shepherd hands the actionable ones to the Tier 2 implementer alongside any
codex review findings. That ingestion is correct, but nothing revalidated the
envelope the resulting work has to fit into. `run_implementer._run_implementer_agent()`
wrapped the whole SDK run — model turns, delegated sub-agents, and (until this
proposal) whatever CI-log retrieval the agent decided to do itself — in one
`anyio.fail_after(IMPLEMENTER_TIMEOUT_SECONDS)` (900s by default), sized for
the pre-#411 workload of "read a handful of review findings and edit a few
lines".

On `mctlhq/mctl-telegram#652` the bundle handed the agent a check name, a
conclusion, a run URL and a 1500-char annotation-derived excerpt — no actual
CI log. The implementer did what the prompt implicitly invited: it fetched the
log for `test-cross-platform (macos-latest)` itself. One step alone
contributed ~64.6 KB, the fetch ran past the Claude Code CLI's own ~120s Bash
tool timeout (which backgrounds the command rather than failing it), and the
outer 900s bound expired with the delegated child still live. The run exited
`EXIT_ORPHANED_SUBAGENT` (46) — correctly classified as blameless — but with
`MAX_HARNESS_FAILURES = 3` the PR still converged to `review-stuck` with no
code mutation ever attempted.

This ADR records the three distinct invariants now in play, and the coverage
table for the specific fix (mctl-agents#423) that keeps them from being
confused with one another.

## The three invariants

1. **#411 — ingestion correctness.** A failing required check on the current
   head must be classified `actionable` or `infrastructure`, surfaced through
   `Blockers`/`decide()`, and rendered into the follow-up bundle
   deterministically (never through the summariser SDK, so it cannot be
   model-rewritten). This says nothing about HOW LONG any of that is allowed
   to take.

2. **#418 — admission and lock-waiting must not consume the execution
   budget.** Acquiring an `ExecutionClaim` (ADR-010 phase 2) or waiting on a
   lock happens OUTSIDE `_run_implementer_agent`'s `anyio.fail_after`, before
   the SDK run is ever entered. A slow claim store must delay the START of a
   run, not eat into the wall-clock the run itself gets once it starts.

3. **#423 — work newly added INSIDE the execution must fit, or explicitly
   negotiate, the envelope.** #411 added real work (reading and acting on CI
   evidence) to the thing #418 already protects the boundary of. Nothing
   forced that new work to declare its own cost, so it silently borrowed from
   the review-findings budget until the budget ran out. This proposal is the
   negotiation: move the unbounded part (log retrieval) OUTSIDE the envelope
   entirely (mirroring #418's shape — pay the cost before the SDK run starts,
   not during it), and let the bounded part (analysing a fixed number of
   fixed-size excerpts) widen the envelope by a declared, capped, logged
   amount.

4. **#430 (this ADR) — work the agent chooses to do INSIDE the execution
   must derive its bound FROM the envelope, not carry an independent one.**
   #423 closed the specific hole of unbounded CI-log retrieval, but live
   acceptance on mctlhq/mctl-telegram#652 showed a second, distinct one:
   legitimate local verification (`go test -race ./... > /tmp/test-race.log
   2>&1 &`, then polling that background shell) that #423's containment does
   not touch at all. Nothing inside a running implementer knew how much of
   its envelope was left, so an agent-issued Bash command was bounded only
   by the Claude Code CLI's own tool timeout — which backgrounds a slow
   command rather than failing it (see "Containment" below) — and an
   explicit `cmd &` escapes task accounting entirely
   (`subagent_wait.AWAITED_TASK_TYPES` deliberately excludes background
   shells). The outer envelope then expired with the delegated child still
   live, and the run exited `EXIT_ORPHANED_SUBAGENT` — correctly classified,
   but as the NORMAL result of a slow test suite rather than the safety net
   it is meant to be. #430 makes every agent-issued Bash command derive its
   own bound from the REMAINING envelope minus a teardown reserve, denies
   detached launches that would escape that bound, and turns the exhausted
   case into a structured, blameless outcome (`EXIT_VERIFICATION_BUDGET_
   EXHAUSTED`) instead.

Confusing any two of these has a specific failure shape: folding #423 into
#411 would mean "ingestion" quietly grows an unbounded retrieval step;
folding #423 into #418 would mean claim/lock waiting time trades against
code-mutation time, which is the opposite of what #418 established; folding
#430 into #423 would mean the per-command bound only applies to CI-remediation
runs, when the defect it fixes (a command outliving the remaining envelope)
is generic over the `Bash` tool and every work class.

## Coverage table

| Operation | Bounded by | Inside the implementer envelope? |
|---|---|---|
| Required-check discovery, annotations | `SHEPHERD_CI_LOG_TIMEOUT_SECONDS` per call | no |
| CI-log retrieval (`ci_checks.fetch_failure_logs`) | per-fetch timeout (`SHEPHERD_CI_LOG_TIMEOUT_SECONDS`) + per-bundle time budget (`SHEPHERD_CI_LOG_BUDGET_SECONDS`) + check count cap (`CI_LOG_MAX_CHECKS`) + byte caps (`CI_LOG_MAX_CHARS`, `CI_LOG_TOTAL_MAX_CHARS`) | no — runs in the shepherd process, before the implementer subprocess forks |
| Admission / claim acquisition | ADR-010's own boundary (#418) | no |
| Clone, fetch, push | `IMPLEMENTER_COMMAND_TIMEOUT_SECONDS` per command | no |
| Model turns, delegated sub-agents, drain | `implementer_envelope(work_class, n_checks)` | yes |
| Awaiting one delegated child | `IMPLEMENTER_DRAIN_TIMEOUT_SECONDS` | yes (nested) |
| Agent-issued Bash command | `min(IMPLEMENTER_COMMAND_TIMEOUT_SECONDS, remaining - IMPLEMENTER_TEARDOWN_RESERVE_SECONDS)`, OS-enforced | yes (nested) |
| Teardown after expiry | `IMPLEMENTER_TEARDOWN_GRACE_SECONDS` (shielded) | no (after) |

No row is left unbounded: every operation that can consume wall-clock time
between admission and teardown is either OUTSIDE the envelope with its own
independent bound, or INSIDE it with a bound derived from what remains.

## The envelope formula

```python
def implementer_envelope(work_class: str, n_checks: int = 0) -> float:
    if work_class not in ("ci-remediation", "mixed"):
        return IMPLEMENTER_TIMEOUT_SECONDS
    n = max(0, min(n_checks, CI_LOG_MAX_CHECKS))
    return min(
        IMPLEMENTER_TIMEOUT_CEILING_SECONDS,
        IMPLEMENTER_TIMEOUT_SECONDS + n * IMPLEMENTER_CI_ANALYSIS_SECONDS,
    )
```

`work_class` (`"review"` | `"ci-remediation"` | `"mixed"`) is derived from the
bundle by `run_implementer._bundle_work_class`. The review-only class keeps
`IMPLEMENTER_TIMEOUT_SECONDS` exactly, unconditionally — this is deliberately
not a global timeout increase (see the proposal's design.md for the
alternatives considered and rejected). The widening for the other two classes
is proportional to a bounded count of bounded evidence and capped at
`IMPLEMENTER_TIMEOUT_CEILING_SECONDS`.

`validate_budget_contract()` (`orchestrator/options.py`, run at import time)
asserts `envelope >= 2 * IMPLEMENTER_DRAIN_TIMEOUT_SECONDS +
IMPLEMENTER_MUTATION_RESERVE_SECONDS` for every work class; on violation it
logs loudly and clamps `IMPLEMENTER_DRAIN_TIMEOUT_SECONDS` down just enough
for the mutation reserve to survive, rather than raising — the same
"loud and harmless, never silent and unbounded" policy `_positive_seconds`
already documents for every other timeout knob in this module.

`run_implementer._review_claim_lease_default()` derives its bound from
`IMPLEMENTER_TIMEOUT_CEILING_SECONDS + 2 * IMPLEMENTER_COMMAND_TIMEOUT_SECONDS`
— the ceiling, not `IMPLEMENTER_TIMEOUT_SECONDS` alone, because the claim is
acquired before the bundle's work class is even known, so the lease has to
outlive the WIDEST envelope any class could select, unconditionally.

## Containment

`options._ci_log_guard_hook()` is a `PreToolUse` hook, composed with (never
replacing) `_command_audit_hooks()`, installed for the `ci-remediation` and
`mixed` work classes. It denies `gh run view ... --log`/`--log-failed`,
`gh api .../logs`, and `curl`/`wget` of a `.../logs` URL, with a deny reason
pointing at the bundle's already-retrieved excerpt. This is the enforcement
the prompt alone cannot provide: the Claude Code CLI backgrounds a Bash
command that exceeds its own tool timeout rather than failing it, so a ground
rule cannot stop the #652 pattern from recurring — only denying the command
before it starts can.

`options._deadline_guard_hook()` (mctl-agents#430) is the generic form of the
same lesson, installed for EVERY work class when the caller supplies both a
`deadline_monotonic` and a `CommandBudgetLedger`
(`orchestrator/exec_budget.py`). Every `Bash` tool call takes exactly one of
three decisions, before the command starts:

1. **Deny a detached launch** — a trailing async-list `&`, `nohup`, `setsid`,
   `disown`, or `run_in_background: true` — with a reason that both names the
   form and quotes the fragment that tripped it.
2. **Deny outright** once `command_budget()` —
   `min(IMPLEMENTER_COMMAND_TIMEOUT_SECONDS, remaining_envelope -
   IMPLEMENTER_TEARDOWN_RESERVE_SECONDS)` — cannot clear
   `IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS`.
3. **Allow, bounded.** The EFFECTIVE bound is applied to the tool-input
   `timeout` (narrowed only — clamping is one-directional) and, by rewriting
   the command under `timeout --kill-after=<k>s <budget>s bash -c <quoted
   command>` (`exec_budget.wrap_bounded()`), at the OS level too. The rewrite
   is skipped — leaving the tool-input clamp alone in force — for the
   `IMPLEMENTER_BOUND_COMMANDS=0` break-glass, a missing `timeout` binary, or
   a shell-state-only command.

Three rules keep that rewrite from being worse than the defect it closes:

- **One effective bound, not two — and the OS timer strictly first.** The
  wrapper and the tool-input clamp use the SAME number: the envelope-derived
  budget narrowed against any caller-supplied tool timeout. Deriving them
  separately lets the CLI background the command at the narrower bound while
  `timeout` holds the process group until the wider one, reopening the orphan
  window for the difference. The rendered `timeout` bound is then
  `floor(effective)` whole seconds, shaved by one more when that floor lands
  exactly on `effective`, so GNU `timeout` fires deterministically BEFORE the
  CLI's timer rather than merely tying with it — rounding to nearest put a
  20.6s budget at 21s, i.e. the wrong side of that ordering. The
  `--kill-after` grace rounds UP and is deliberately NOT also subtracted from
  the bound: requiring `bound + grace <= effective` is unsatisfiable once the
  budget is at or below the grace, and the guarantee that matters is that
  SIGTERM lands before the CLI backgrounds, not that the pathological SIGKILL
  does too. Below one second the whole-second grid cannot express the
  ordering at all — flooring a 0.2s budget to 1s puts the bound back above
  the tool timeout — so a fractional duration is rendered there instead,
  which GNU `timeout` accepts. The Bash tool's own `timeout` ceiling (`BASH_TOOL_MAX_TIMEOUT_MS`,
  600000 ms) binds that effective bound as well: a larger value is silently
  ignored by the tool, so clamping only the injected number would leave the
  OS bound above the CLI's real timer and put the CLI first again.
- **Shell structure, not text — and quoted is not the same as inert.**
  Detachment patterns and command-word scans read the QUOTE-MASKED command
  (`exec_budget.mask_quoted()`), so `git commit -m "A & B"` and `echo 'nohup'`
  carry those characters as data and are admitted. A backslash-escaped
  operator outside quotes is masked too. But quoted text the shell will
  EXECUTE is scanned recursively at its own level: a `bash -c` payload and
  the contents of `$(...)`/backticks, to `MAX_PAYLOAD_DEPTH`. A `-c` flag
  belongs to its own SEGMENT, never to the command line: judged line-wide,
  `cd /repo/bash && git -c user.name="A & B" commit` read `git -c` as a shell
  payload because some earlier token happened to spell a shell. `bash -c "cmd
  &"` backgrounds inside the inner shell, and GNU `timeout` exits with its
  DIRECT child, so the grandchild survives — the #652 shape reached through a
  quoted payload rather than a bare one.
- **Shell state survives, but only where it cannot cost time.** A command
  built only from `SHELL_STATE_BUILTINS` (`cd`, `export`, `set`, …) is
  admitted UNWRAPPED, because the Bash tool carries that state — notably the
  working directory — across calls and `bash -c` would discard it. The
  exemption is justified entirely by those commands being instantaneous, so
  it is withheld from anything that can run long: `source`/`.` execute an
  arbitrary script and are NOT in the set, and any command carrying a
  substitution (`export FOO=$(slow)`) is refused the exemption however it is
  spelled. A compound that also runs real work (`cd /repo && go test ./...`)
  is wrapped for the same reason. The accepted cost: a sourced virtualenv no
  longer survives to the next call, so an agent invokes the interpreter by
  path rather than activating it. GNU `timeout` signals the
command's whole PROCESS GROUP, so this is OS-level enforcement, not only the
CLI's own Bash-tool timeout that backgrounds rather than fails an
over-running command. Every decision (clamped, denied-background,
denied-exhausted, the bound applied, a truncated command label) is recorded
on the orchestrator-owned `CommandBudgetLedger` — never in model prose — and,
when a run ends with no commit and the ledger shows the budget exhausted,
`run_implementer` exits `EXIT_VERIFICATION_BUDGET_EXHAUSTED` (51) and writes
the ledger summary as JSON to `--refusal-out`.

When the outer envelope expires with a delegated task still live,
`_run_implementer_agent`'s `TimeoutError` handler performs a shielded,
bounded teardown (`anyio.CancelScope(shield=True)` +
`anyio.move_on_after(IMPLEMENTER_TEARDOWN_GRACE_SECONDS)`) before re-raising
`ImplementerOrphanedSubagent`: it disconnects the SDK client and terminates
its CLI child if the transport exposes it, rather than abandoning them.
Without the shield, that disconnect would await inside the very scope
`fail_after` just cancelled, and the first checkpoint inside it would raise
immediately — skipping the teardown and letting the child outlive the
process. `EXIT_ORPHANED_SUBAGENT` remains this safety net — the per-command
deadline guard is what keeps a slow, legitimate verification step from being
the thing that reaches it in the normal case.

## Blamelessness

`EXIT_CI_EVIDENCE_INSUFFICIENT` (50) lets a run end deliberately and
boundedly when the bounded log excerpt cannot support a code decision.
`EXIT_VERIFICATION_BUDGET_EXHAUSTED` (51, mctl-agents#430) is the same shape
for local verification: the run stayed inside its own envelope the whole
time, and a structured, orchestrator-derived ledger — never model prose —
recorded the per-command budget running out before a commit or a merits
decision was reached, either because the deadline guard denied one more
command outright or because the agent itself recorded the same fact via the
refusal marker's `verification_budget_exhausted` flag. Both join
`EXIT_ORPHANED_SUBAGENT` (46) in `run_shepherd._followup_code_sets()`'s
harness set: blameless (never charges `review_attempts`), still bounded by
`MAX_HARNESS_FAILURES`. Blamelessness is a property of BOTH drivers, not just
the review path: an implement run that ends with no commit and an exhausted
ledger hands the proposal back to `accepted` (`_hand_back_if_still_ours`,
under the same compare-and-swap the claim-vanished arm uses) instead of
writing `needs-triage` — but only `IMPLEMENT_MAX_BUDGET_HANDBACKS` (3) times
in a row, tallied in `.status.yaml`'s `budget_handbacks` and cleared by any
run that gets through. The cap counts like `MAX_HARNESS_FAILURES`, which it
mirrors down to the comparison: the Nth consecutive occurrence is the one
that stops the loop, so N-1 hand-backs actually happen. The hand-back writes
`.status.yaml` BEFORE releasing the claim, the order every other terminal arm
uses — the reverse frees mutual exclusion while the file still names a live
attempt. The implement driver has no `review_attempts` or
`harness_failures` budget of its own, which is why its sibling
orphaned-subagent arm stays terminal; an unconditional hand-back would
therefore trade a wrong terminal state for an unbounded PAID retry loop. At
the cap the run is recorded terminally under `verification-budget-exhausted`,
never `no-commits`, so the proposal's history still says what happened. `needs-triage` is terminal by contract — a retry
needs an operator-reviewed gitops change moving the proposal back to
`accepted` — so charging a busy runner there parks a sound proposal at a
human gate for a fact about the runner. A plain no-commit run stays terminal:
that one IS deterministic, and re-running it buys the same result.

The ledger also OUTRANKS the refusal marker when the two disagree by
omission. An agent that stops because it ran out of budget but writes a
marker without `verification_budget_exhausted` would otherwise fall through
to `EXIT_DELIBERATE_NO_OP` (47) — a decision on the merits, charged to
`review_attempts` — so a missing optional boolean in model-authored JSON
would charge the proposal for a fact about the runner. The orchestrator-owned
ledger is the evidence of record precisely so that model prose cannot decide
blame; the agent's own reason still rides along with the exit-51 result. An
ordinary refusal, with no exhausted ledger behind it, stays exit 47 and stays
charged. A run that DOES produce a commit still pushes and
exits `EXIT_OK` even if some verification was cut short along the way — the
commit is the outcome; the ledger's clamp/deny counts are printed to the Argo
log either way, so the truncation stays visible without gating success on it.
