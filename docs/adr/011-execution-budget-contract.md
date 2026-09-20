# ADR 011 — Execution-budget contract for CI-remediation work

> **Status:** proposed
> **Date:** 2026-09-20
> **Issue:** mctlhq/mctl-agents#423
> **Supersedes:** nothing. It records the boundary this proposal draws
> between three related-but-distinct invariants (#411, #418, #423) so future
> changes to any one of them do not accidentally reopen another.

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

3. **#423 (this ADR) — work newly added INSIDE the execution must fit, or
   explicitly negotiate, the envelope.** #411 added real work (reading and
   acting on CI evidence) to the thing #418 already protects the boundary of.
   Nothing forced that new work to declare its own cost, so it silently
   borrowed from the review-findings budget until the budget ran out. This
   proposal is the negotiation: move the unbounded part (log retrieval)
   OUTSIDE the envelope entirely (mirroring #418's shape — pay the cost before
   the SDK run starts, not during it), and let the bounded part (analysing a
   fixed number of fixed-size excerpts) widen the envelope by a declared,
   capped, logged amount.

Confusing any two of these has a specific failure shape: folding #423 into
#411 would mean "ingestion" quietly grows an unbounded retrieval step;
folding #423 into #418 would mean claim/lock waiting time trades against
code-mutation time, which is the opposite of what #418 established.

## Coverage table

| Operation | Bounded by | Inside the implementer envelope? |
|---|---|---|
| Required-check discovery, annotations | `SHEPHERD_CI_LOG_TIMEOUT_SECONDS` per call | no |
| CI-log retrieval (`ci_checks.fetch_failure_logs`) | per-fetch timeout (`SHEPHERD_CI_LOG_TIMEOUT_SECONDS`) + per-bundle time budget (`SHEPHERD_CI_LOG_BUDGET_SECONDS`) + check count cap (`CI_LOG_MAX_CHECKS`) + byte caps (`CI_LOG_MAX_CHARS`, `CI_LOG_TOTAL_MAX_CHARS`) | no — runs in the shepherd process, before the implementer subprocess forks |
| Admission / claim acquisition | ADR-010's own boundary (#418) | no |
| Clone, fetch, push | `IMPLEMENTER_COMMAND_TIMEOUT_SECONDS` per command | no |
| Model turns, delegated sub-agents, drain | `implementer_envelope(work_class, n_checks)` | yes |
| Awaiting one delegated child | `IMPLEMENTER_DRAIN_TIMEOUT_SECONDS` | yes (nested) |
| Teardown after expiry | `IMPLEMENTER_TEARDOWN_GRACE_SECONDS` (shielded) | no (after) |

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

When the outer envelope expires with a delegated task still live,
`_run_implementer_agent`'s `TimeoutError` handler performs a shielded,
bounded teardown (`anyio.CancelScope(shield=True)` +
`anyio.move_on_after(IMPLEMENTER_TEARDOWN_GRACE_SECONDS)`) before re-raising
`ImplementerOrphanedSubagent`: it disconnects the SDK client and terminates
its CLI child if the transport exposes it, rather than abandoning them.
Without the shield, that disconnect would await inside the very scope
`fail_after` just cancelled, and the first checkpoint inside it would raise
immediately — skipping the teardown and letting the child outlive the
process.

## Blamelessness

`EXIT_CI_EVIDENCE_INSUFFICIENT` (50) lets a run end deliberately and
boundedly when the bounded log excerpt cannot support a code decision. It
joins `EXIT_ORPHANED_SUBAGENT` (46) in `run_shepherd._followup_code_sets()`'s
harness set: blameless (never charges `review_attempts`), still bounded by
`MAX_HARNESS_FAILURES`.
