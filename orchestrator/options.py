"""Build ClaudeAgentOptions for service agents and the mentor."""
import functools
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import anyio
import anyio.to_thread
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import HookMatcher

from config.settings import MCTL_MCP_URL
from orchestrator.ci_checks import CI_LOG_MAX_CHECKS
from orchestrator.exec_budget import (
    CommandBudgetLedger,
    command_budget,
    detachment_match,
    is_shell_state_only,
    wrap_bounded,
)
from orchestrator.exec_budget import normalize_shell_command as _normalize_shell_command
from orchestrator.resolver import ExecutionPlan
from orchestrator.usage_ledger import agent_env_without_writer_token

# Paths already warned about by _execution_context_headers(): the audit hook
# re-reads the env on every PreToolUse, so an unreadable context file would
# otherwise print the same warning once per tool call.
_warned_context_paths: set[str] = set()


def _execution_context_headers() -> dict[str, str]:
    """`X-Mctl-Execution-Context` / `X-Mctl-Trace-Id` (mctlhq/mctl-agents#196,
    ADR 011: docs/adr/011-execution-identity-contract.md) — present only when
    the CWFT actually wrote a sealed context to
    `MCTL_EXECUTION_CONTEXT_FILE`, absent (never present-but-empty) otherwise,
    mirroring this module's own convention for MCTL_TOKEN/mctl_mcp_config.
    Every `mcp__mctl__*` call then carries the identity with zero agent
    cooperation; a local run or a test with no context file adds nothing.

    Deferred import: orchestrator.execution_identity is stdlib-only and meant
    to be importable by the long-lived Temporal worker too — nothing here
    requires importing it at options.py's own module scope.
    """
    from orchestrator.execution_identity import (
        MCTL_EXECUTION_CONTEXT_FILE_ENV,
        MCTL_REQUIRE_EXECUTION_CONTEXT_ENV,
        ExecutionIdentityError,
        load_from_environment,
    )

    if not os.environ.get(MCTL_EXECUTION_CONTEXT_FILE_ENV, "").strip():
        if os.environ.get(MCTL_REQUIRE_EXECUTION_CONTEXT_ENV, "").strip():
            # Require mode must fail closed on the MISSING-env case too, not
            # only on a present-but-broken file — an early `return {}` here
            # would silently send headerless calls. Delegate for the
            # canonical ExecutionContextRequiredError raise.
            load_from_environment(executor_type="system")
        return {}
    try:
        # executor_type is only consulted on the local-mint fallback branch,
        # which this call never takes (the file is present) — the value
        # passed here is inert.
        context = load_from_environment(executor_type="system")
    except ExecutionIdentityError as exc:
        # load_from_environment() wraps every read/parse failure of a present
        # file (OSError, JSONDecodeError, UnicodeDecodeError, ...) into
        # ExecutionIdentityError, so this one narrow catch is complete. An
        # ExecutionContextRequiredError (MCTL_REQUIRE_EXECUTION_CONTEXT set)
        # deliberately passes through: require mode fails closed, never
        # degrades to headerless calls.
        path = os.environ.get(MCTL_EXECUTION_CONTEXT_FILE_ENV, "").strip()
        # Keyed on path AND failure text, so a same-path file that starts
        # failing differently (unreadable -> tampered) still warns once.
        warn_key = f"{path}: {exc}"
        if warn_key not in _warned_context_paths:
            _warned_context_paths.add(warn_key)
            print(f"warn: MCTL_EXECUTION_CONTEXT_FILE is set but unreadable ({exc}); omitting identity headers.")
        return {}
    return {"X-Mctl-Execution-Context": context.context_id, "X-Mctl-Trace-Id": context.trace_id}


def mctl_mcp_config(*, always_load: bool = False) -> dict:
    """MCP config for https://api.mctl.ai/mcp.

    Returns an empty dict when MCTL_TOKEN is unset — the agent then runs
    without mcp__mctl__* tools (Read/Write/WebSearch/WebFetch/Bash only).
    Convenient for smoke tests and local dev without mctl access. The
    execution-identity headers are evaluated FIRST, before that early
    return, so MCTL_REQUIRE_EXECUTION_CONTEXT fails closed at options
    construction (ExecutionContextRequiredError) even in a tokenless run.

    always_load: sets the CLI's `alwaysLoad` flag, which blocks first-turn
    dispatch until this server connects (bounded by the CLI's own MCP
    connection timeout) instead of connecting in the background — without
    it, the CLI can dispatch the first turn before the handshake completes,
    silently running with zero mcp__mctl__* tools (see mctl-telegram#316
    for the equivalent stdio-transport bug this mirrors).

    Every builder below now passes always_load=True — the 2026-08-02
    incident-responder outage turned out to be caused by a genuinely dead
    MCTL_TOKEN, not just the race, and checking mctl_list_recent_agent_runs
    afterwards showed the other modes could not prove they weren't also
    silently degrading the same way (service-agent's prompt has its own
    explicit "no mcp__mctl__* tools, skip silently" instruction). Blocking
    dispatch until the connection settles (or the CLI's own timeout) is
    cheap and deterministic either way; see orchestrator/mcp_guard.py for
    the positive-verification layer built on top for callers that also want
    to know whether the connection actually succeeded, and whether that's
    fatal for their mode. shepherd is the one mode with no mctl MCP at all
    (mcp_servers={} below) — it never calls mcp__mctl__* tools, so there is
    nothing to always-load.
    """
    # Build-time fail-closed: raise the require-mode error HERE, at options
    # construction, not only from inside the PreToolUse audit hook — whether
    # an exception raised in a hook propagates is an SDK implementation
    # detail this control must not depend on. Deliberately BEFORE the token
    # early-return, so require mode fails closed even with no MCTL_TOKEN
    # (the hook-time raise below stays as backstop).
    identity_headers = _execution_context_headers()
    token = os.environ.get("MCTL_TOKEN", "").strip()
    if not token:
        print("warn: MCTL_TOKEN is not set — agent will run without mctl MCP tools.")
        return {}
    server_config: dict[str, Any] = {
        "type": "http",
        "url": MCTL_MCP_URL,
        "headers": {"Authorization": f"Bearer {token}", **identity_headers},
    }
    if always_load:
        server_config["alwaysLoad"] = True
    return {"mctl": server_config}


def _mctl_tool_globs() -> list[str]:
    """Allow mcp__mctl__* when MCP is configured; empty otherwise."""
    return ["mcp__mctl__*"] if mctl_mcp_config() else []


# The durable human-clarification primitive (mctlhq/mctl-agents#333, ADR 013).
# A capability entry in `ExecutionPlan.tools`, not an SDK tool name — a model
# cannot "call" it, so it must never leak into `allowed_tools` (the CLI would
# hold a dead allow-list entry). See `plan_grants_human_input` and
# `build_issue_investigator_options_from_plan`'s filtering below, and
# `orchestrator/validate_manifest.py`'s `_CAPABILITY_TOOLS`, which subtracts
# it before the two set-equality checks against options.py's real output.
HUMAN_INPUT_CAPABILITY = "human.request_input"


def plan_grants_human_input(plan: ExecutionPlan) -> bool:
    """Does the resolved plan grant the durable clarification capability?

    True only when `HUMAN_INPUT_CAPABILITY` is literally present in
    `plan.tools` — the resolved `ExecutionPlan`'s authoritative allow-list
    (ADR 007). There is no plan at all in `legacy` resolver mode
    (`build_issue_investigator_options`, the module-constant-driven builder),
    so the capability is always ungranted there; callers must not invent a
    truthy answer for that mode.
    """
    return HUMAN_INPUT_CAPABILITY in plan.tools


def _positive_seconds(name: str, *, default: float) -> float:
    """Read a wall-clock env var, falling back loudly on a non-positive value.

    A floor rather than a raise, and it belongs here rather than at the point of
    use. `anyio.move_on_after(0)` cancels before the first read, so a drain
    deadline of 0 orphans every run that delegates -- but raising instead would
    be worse: the error surfaces only once a sub-agent is actually launched, is
    swallowed by the caller's catch-all into a generic exit 1, and the shepherd
    then classifies it transient, which has NO counter. A typo in one env var
    would re-clone the repo and re-run a paid SDK call every tick forever --
    exactly the pathology MAX_HARNESS_FAILURES exists to prevent. Clamping at
    config time makes a bad value loud and harmless instead of silent and
    unbounded.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"warn: {name}={raw!r} is not a number; using {default:g}s", file=sys.stderr)
        return default
    # `not (value > 0)` rather than `value <= 0`: BOTH comparisons are False for
    # nan, so the naive form lets nan through to anyio.move_on_after(), whose
    # deadline is then nan, whose every `deadline <= now` test is False, so the
    # scope never cancels and the sub-deadline is silently gone. A guard whose
    # entire job is rejecting values that break move_on_after has to cover the
    # one value that breaks it quietly.
    if not (value > 0) or math.isinf(value):
        print(f"warn: {name}={raw!r} is not a positive finite number; using {default:g}s", file=sys.stderr)
        return default
    return value


SERVICE_AGENT_BUDGET_USD = float(os.getenv("SERVICE_AGENT_BUDGET_USD", "5.00"))
MENTOR_BUDGET_USD = float(os.getenv("MENTOR_BUDGET_USD", "2.00"))
# Tier 2 implementer budget — soft cap per single proposal implementation.
# A proposal touching one or two files usually finishes well under this.
# No hard kill: the SDK stops sampling once the cap is exceeded but the
# already-applied edits remain. Caller can raise via env if a proposal needs more.
IMPLEMENTER_BUDGET_USD = float(os.getenv("IMPLEMENTER_BUDGET_USD", "3.00"))
# Per-proposal wall-clock bound. The SDK budget limits spend, not elapsed time;
# without this a stalled stream can consume the whole Argo workflow deadline.
IMPLEMENTER_TIMEOUT_SECONDS = float(
    os.getenv("IMPLEMENTER_TIMEOUT_SECONDS", "900")
)
# Sub-deadline for awaiting a sub-agent the CLI launched asynchronously, nested
# inside IMPLEMENTER_TIMEOUT_SECONDS above (mctl-agents#366). Not needed for
# liveness -- the outer bound already provides that -- but for classification: a
# wedged child that ate the whole remaining budget would surface as a plain
# operation timeout, which the shepherd charges to the proposal's review-attempt
# budget, which is the very bug #366 is about.
#
# NOT a single spend. drain_until_settled loops: it allows this much for the
# child to go quiescent, then this much AGAIN for the parent's closing frame,
# and a second delegation observed while waiting for that frame sends it back
# round on a fresh clock. So the worst case inside the drain is
# 2 x N x this value for N delegations -- capped, because phase 2's grace is
# clamped to what is left of IMPLEMENTER_TIMEOUT_SECONDS and phase 1 raises
# rather than looping forever. Read it as "how long one wait may take", not as
# "how long the drain may take". See orchestrator/subagent_wait.py.
IMPLEMENTER_DRAIN_TIMEOUT_SECONDS = _positive_seconds(
    "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", default=300.0
)
# The same VALUE and the same clamp for the other two drivers that drain
# (mctl-agents#368), kept beside the implementer's so the family is read and
# changed together -- but NOT the same ceiling, and the difference matters.
#
# IMPLEMENTER_DRAIN_TIMEOUT_SECONDS is a sub-deadline nested inside
# IMPLEMENTER_TIMEOUT_SECONDS: `_run_implementer_agent` wraps the whole run in
# `anyio.fail_after`, so the outer bound is what actually caps elapsed time and
# the drain budget only decides how a wedged child is CLASSIFIED.
#
# Neither driver below has an outer `fail_after`. Nothing trips first, and
# `drain_until_settled` restarts this clock on every relaunch, so the
# in-process ceiling is N x 300s for N delegations rather than 300s. That is
# bounded in practice by the Argo step deadline (issue-investigator) and the
# CronWorkflow's own limit (service-agent), which is why it is acceptable
# rather than a leak -- but it is an EXTERNAL bound, so shortening these knobs
# does not shorten the worst case, and phase 2's
# `anyio.current_effective_deadline()` clamp is inert for both (infinite
# remaining budget, nothing to clamp against). Read that way, not as "same as
# the implementer's".
#
# Clamped like the implementer's regardless: a knob that documents itself as
# tunable while silently doing nothing is bad in every mode, even where the
# blast radius is smaller (neither of these goes through the shepherd's
# classification, so a bad value orphans runs rather than looping forever).
#
# Both modes have the precondition the drain rests on -- their builders below
# pass `hooks=_command_audit_hooks()`, and the SDK holds the CLI subprocess
# open past a result frame only when `sdk_mcp_servers or hooks` is truthy.
#
# service-agent: its prompt walks four steps named after the
# `.claude/agents/{researcher,analyst,spec-writer}.md` personas that
# setting_sources=["project"] loads from cwd, so there are real sub-agents to
# delegate to.
SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS = _positive_seconds(
    "SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS", default=300.0
)
# issue-investigator: cwd is a fresh clone of the TARGET repository, so
# setting_sources=["project"] loads whatever `.claude/agents/*.md` that
# repository ships. An orphan there means the downstream pipeline gets no
# proposal -- but by CHOICE, not by physics: the agent may well have written a
# complete triplet into the staging directory before the child was orphaned,
# and `investigate()`'s `finally` discards staging on any error path rather
# than publish work whose provenance it cannot vouch for. See the orphan branch
# in run_issue_investigator.investigate() for why that trade is made there.
ISSUE_INVESTIGATOR_DRAIN_TIMEOUT_SECONDS = _positive_seconds(
    "ISSUE_INVESTIGATOR_DRAIN_TIMEOUT_SECONDS", default=300.0
)
# Bound every synchronous git/gh command as well.  The model-stream timeout
# above cannot interrupt a clone, fetch, or push that has stalled before or
# after the SDK call.
IMPLEMENTER_COMMAND_TIMEOUT_SECONDS = float(
    os.getenv("IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", "300")
)
# ---------------------------------------------------------------------------
# Work-class-derived execution envelope (mctl-agents#423).
#
# #411 made a failing required CI check a first-class review-remediation
# blocker, but the implementer's whole execution still ran inside the ONE
# IMPLEMENTER_TIMEOUT_SECONDS envelope sized for "read a handful of review
# findings and edit a few lines". A CI-remediation bundle is genuinely more
# work (analysing a bounded log excerpt per failing check, on top of the
# fix), and — now that ci_checks.fetch_failure_logs() moves log retrieval
# OUT of the envelope and into the shepherd tick (see orchestrator/ci_checks.py)
# — that extra work is the only thing left for the envelope to fund.
# ---------------------------------------------------------------------------
# Per-check analysis budget added to the base envelope for a CI-remediation or
# mixed run, capped at CI_LOG_MAX_CHECKS checks (see implementer_envelope()).
IMPLEMENTER_CI_ANALYSIS_SECONDS = _positive_seconds(
    "IMPLEMENTER_CI_ANALYSIS_SECONDS", default=120.0
)
# Hard ceiling on the derived envelope, whatever work class or check count
# produced it. Never exceeded — this is the number an operator can point to
# and say "no single implementer run can run longer than this".
IMPLEMENTER_TIMEOUT_CEILING_SECONDS = _positive_seconds(
    "IMPLEMENTER_TIMEOUT_CEILING_SECONDS", default=1800.0
)
# The slice of any envelope that must survive drain/teardown so a run that
# analysed its evidence still has time left to actually mutate code and
# commit. validate_budget_contract() (below) asserts every work class's
# envelope leaves at least this much after 2x the drain sub-budget.
IMPLEMENTER_MUTATION_RESERVE_SECONDS = _positive_seconds(
    "IMPLEMENTER_MUTATION_RESERVE_SECONDS", default=180.0
)
# Bound on the shielded teardown _run_implementer_agent performs when the
# outer envelope expires with a delegated task still live: disconnect the SDK
# client and terminate its CLI child before the process exits, rather than
# abandoning them (mctl-agents#423).
IMPLEMENTER_TEARDOWN_GRACE_SECONDS = _positive_seconds(
    "IMPLEMENTER_TEARDOWN_GRACE_SECONDS", default=15.0
)
# ---------------------------------------------------------------------------
# Per-command execution budget (mctl-agents#430).
#
# #423 bounded CI-log retrieval; live acceptance on mctlhq/mctl-telegram#652
# showed a second hole -- legitimate local verification (`go test -race ...
# &`, then polling the background shell) that is bounded only by the CLI's
# own tool timeout, which backgrounds rather than fails an over-running
# command (ADR-011 "Containment"). These knobs let every implementer-owned
# Bash call derive its own bound from what remains of the run's envelope
# instead. See orchestrator/exec_budget.py for the pure logic.
# ---------------------------------------------------------------------------
# The slice of the remaining envelope held back from every derived command
# bound so `_run_implementer_agent` still has time, after the last command
# returns, to cancel it, drain any delegated children, write the structured
# marker and return before the outer bound fires. Deliberately NOT
# IMPLEMENTER_DRAIN_TIMEOUT_SECONDS (300s), which would leave a 900s
# `review` envelope with too little command budget to be useful -- see the
# proposal's "open questions" for the 120s default's derivation
# (IMPLEMENTER_TEARDOWN_GRACE_SECONDS + a closing turn + the commit).
IMPLEMENTER_TEARDOWN_RESERVE_SECONDS = _positive_seconds(
    "IMPLEMENTER_TEARDOWN_RESERVE_SECONDS", default=120.0
)
# Below this, a derived command bound is not worth admitting -- it would
# expire before the command can do anything useful. `command_budget()`
# returns `None` (deny) rather than a bound under this floor.
IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS = _positive_seconds(
    "IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS", default=20.0
)
# The Claude Code Bash tool's own ceiling for its `timeout` input, in
# milliseconds. A larger value is not honoured, so injecting one would leave
# the tool-input clamp inoperative and put the whole bound on the OS wrapper
# alone (claude P3 on `624a433`).
BASH_TOOL_MAX_TIMEOUT_MS = 600_000

# `timeout --kill-after=<this>s` on every wrapped command: SIGTERM first, then
# SIGKILL after this much longer if the process group ignored it. Keeps the
# "no live child remains" guarantee even against a command that traps SIGTERM.
IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS = _positive_seconds(
    "IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS", default=5.0
)
# Break-glass: "0" (or any falsy-looking value) disables REWRITING commands
# under `timeout` while leaving the tool-input clamp and the detachment
# denials in place. Rollback lever named in the proposal's design, not a
# knob a normal run should ever need to touch.
IMPLEMENTER_BOUND_COMMANDS = os.getenv(
    "IMPLEMENTER_BOUND_COMMANDS", "1"
).strip().lower() not in ("0", "false", "no", "off", "")


def implementer_envelope(work_class: str, n_checks: int = 0) -> float:
    """The outer `anyio.fail_after` bound for one implementer run.

    `work_class` is `"review"` (the pre-#423 default — the base envelope,
    unconditionally), `"ci-remediation"` (CI failures only) or `"mixed"`
    (CI failures plus review findings) — see
    `run_implementer._bundle_work_class`. For the latter two, the envelope
    widens by `IMPLEMENTER_CI_ANALYSIS_SECONDS` per failing check actually
    carried in the bundle, capped at `CI_LOG_MAX_CHECKS` (retrieval itself
    never fetches more than that many logs, so analysing more than that
    many is not a cost this formula needs to fund) and at
    `IMPLEMENTER_TIMEOUT_CEILING_SECONDS` overall.

    Deliberately NOT a blanket timeout increase (see design.md's rejected
    alternative 1): a `"review"` bundle keeps the exact pre-#423 envelope,
    the widening is proportional to a bounded count of bounded evidence, and
    it is capped and logged rather than silently unbounded.

    `IMPLEMENTER_TIMEOUT_CEILING_SECONDS` bounds every work class, `"review"`
    included -- `_review_claim_lease_default()` derives its lease from the
    ceiling alone on the assumption that no class's envelope can exceed it
    (mctl-agents#423 review P2: a `"review"` run used to return
    `IMPLEMENTER_TIMEOUT_SECONDS` uncapped, so an env override raising that
    past the ceiling would silently outlive the lease sized from it).
    """
    if work_class not in ("ci-remediation", "mixed"):
        return min(IMPLEMENTER_TIMEOUT_CEILING_SECONDS, IMPLEMENTER_TIMEOUT_SECONDS)
    n = max(0, min(n_checks, CI_LOG_MAX_CHECKS))
    return min(
        IMPLEMENTER_TIMEOUT_CEILING_SECONDS,
        IMPLEMENTER_TIMEOUT_SECONDS + n * IMPLEMENTER_CI_ANALYSIS_SECONDS,
    )


def validate_budget_contract() -> None:
    """Assert every work class's envelope leaves room for drain + mutation,
    and for at least one command at the floor budget (mctl-agents#430).

    `2 * IMPLEMENTER_DRAIN_TIMEOUT_SECONDS` because `drain_until_settled` can
    restart its own clock once on a second delegation observed mid-drain (see
    the IMPLEMENTER_DRAIN_TIMEOUT_SECONDS comment above); the reserve on top of
    that is what `_run_implementer_agent` needs left over to actually commit.

    Matches `_positive_seconds`'s "loud and harmless" policy rather than
    raising: this runs at import time, so a raise here would make a bad
    combination of env vars a hard startup failure for every mode that
    imports this module, not just the implementer. Instead it logs the
    violation and clamps `IMPLEMENTER_DRAIN_TIMEOUT_SECONDS` down just enough
    for the mutation reserve to survive, for the tightest work class it
    checked — clamping the ceiling or the reserve itself would silently erode
    the two guarantees (a hard cap, and code-mutation time) this contract
    exists to protect.

    The second assertion — `envelope >= IMPLEMENTER_TEARDOWN_RESERVE_SECONDS +
    IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS + IMPLEMENTER_MUTATION_RESERVE_SECONDS`
    — is the same policy applied to the per-command teardown reserve: on
    violation it clamps `IMPLEMENTER_TEARDOWN_RESERVE_SECONDS` down (never the
    ceiling, never the mutation reserve — the two guarantees above), so the
    deadline guard still admits at least one command at the floor budget
    rather than denying every command on a tight envelope.
    """
    global IMPLEMENTER_DRAIN_TIMEOUT_SECONDS, IMPLEMENTER_TEARDOWN_RESERVE_SECONDS
    for work_class in ("review", "ci-remediation", "mixed"):
        envelope = implementer_envelope(work_class, n_checks=CI_LOG_MAX_CHECKS)
        required = 2 * IMPLEMENTER_DRAIN_TIMEOUT_SECONDS + IMPLEMENTER_MUTATION_RESERVE_SECONDS
        if envelope < required:
            clamped_drain = max(0.0, (envelope - IMPLEMENTER_MUTATION_RESERVE_SECONDS) / 2)
            print(
                f"warn: implementer envelope for work_class={work_class!r} "
                f"({envelope:g}s) cannot satisfy 2*drain+mutation-reserve "
                f"({required:g}s); clamping IMPLEMENTER_DRAIN_TIMEOUT_SECONDS "
                f"{IMPLEMENTER_DRAIN_TIMEOUT_SECONDS:g}s -> {clamped_drain:g}s so the "
                f"mutation reserve survives",
                file=sys.stderr,
            )
            IMPLEMENTER_DRAIN_TIMEOUT_SECONDS = clamped_drain

        required_reserve = (
            IMPLEMENTER_TEARDOWN_RESERVE_SECONDS
            + IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS
            + IMPLEMENTER_MUTATION_RESERVE_SECONDS
        )
        if envelope < required_reserve:
            clamped_reserve = max(
                0.0,
                envelope - IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS - IMPLEMENTER_MUTATION_RESERVE_SECONDS,
            )
            print(
                f"warn: implementer envelope for work_class={work_class!r} "
                f"({envelope:g}s) cannot satisfy teardown-reserve+min-command-"
                f"budget+mutation-reserve ({required_reserve:g}s); clamping "
                f"IMPLEMENTER_TEARDOWN_RESERVE_SECONDS "
                f"{IMPLEMENTER_TEARDOWN_RESERVE_SECONDS:g}s -> {clamped_reserve:g}s "
                f"so the minimum command budget and the mutation reserve survive",
                file=sys.stderr,
            )
            if clamped_reserve <= 0.0:
                # Not the same event as an ordinary clamp, and the drain
                # clamp's wording does not describe it (claude P3 on
                # `624a433`). A zero reserve means `command_budget()` hands
                # the WHOLE remaining envelope to one command, leaving the
                # run nothing in which to cancel it, write its marker and
                # return -- so the outer bound, not the guard, becomes what
                # ends the run, which is the failure mode mctl-agents#430
                # exists to remove. Still a clamp rather than a raise, to
                # match this function's standing policy, but it must not be
                # mistaken for a routine adjustment.
                print(
                    f"warn: IMPLEMENTER_TEARDOWN_RESERVE_SECONDS is now 0s for "
                    f"work_class={work_class!r}: the per-command deadline guard "
                    f"can no longer hold anything back for teardown, so a run "
                    f"may be cut off by its outer bound before it can record "
                    f"its outcome. Raise the envelope or lower "
                    f"IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS/"
                    f"IMPLEMENTER_MUTATION_RESERVE_SECONDS.",
                    file=sys.stderr,
                )
            IMPLEMENTER_TEARDOWN_RESERVE_SECONDS = clamped_reserve


# Run at import time, not lazily: a bad combination of env vars must be loud
# the moment this module loads, in whichever process imported it (implementer,
# shepherd, or a test), not only the first time an implementer run happens to
# need the clamped value.
validate_budget_contract()
# Tier 3 shepherd budget — soft cap per shepherd tick.
# Covers the shepherd's own spend only: the sub-agent classification call
# that turns codex findings into the bundle, plus the small amount of
# decision-logic accounting. The implementer subprocess that the shepherd
# forks for follow-ups has its own IMPLEMENTER_BUDGET_USD cap and does
# NOT count against this.
# Default raised to $5.00 to leave headroom for many-finding PRs (a
# typical PR has 0-3 findings, but a noisy review can produce 10+ which
# bloats the bundle prompt). The previous $1.00 default was too tight
# under those conditions and risked tripping max_budget_usd before the
# JSON object was complete.
SHEPHERD_BUDGET_USD = float(os.getenv("SHEPHERD_BUDGET_USD", "5.00"))
# Issue-investigator budget — covers reading a GitHub issue, exploring the
# target repo clone, and writing the requirements/design/tasks triplet.
# Raised 3.00 -> 8.00 to match the deployment: mctl-gitops' CWFT
# (cwft-mctl-agents-investigate.yaml) sets ISSUE_INVESTIGATOR_BUDGET_USD=8.00
# alongside ISSUE_INVESTIGATOR_MODEL=claude-opus-5, and the ExecutionProfile
# issue-investigator-default declares the same. This default is what
# tests/test_manifest.py compares the resolved profile against, so leaving it
# at 3.00 turned that test red on main the moment the profile moved — in
# another repository, with no commit here.
ISSUE_INVESTIGATOR_BUDGET_USD = float(os.getenv("ISSUE_INVESTIGATOR_BUDGET_USD", "8.00"))
# Incident-responder budget — covers listing/getting incidents, fetching logs,
# writing proposal triplets (requirements/design/tasks/.status.yaml), and
# resolving incidents.  Each incident requires ~7 MCP + Write calls; with up
# to 5 incidents per run and claude-sonnet the $2 default was too tight and
# caused consistent budget-exhaustion failures after ~3 minutes.  Raised to
# $5 to match the service-agent and shepherd caps.
INCIDENT_RESPONDER_BUDGET_USD = float(os.getenv("INCIDENT_RESPONDER_BUDGET_USD", "5.00"))


# Some service agents need read access to sibling mctl-* repos (e.g. mctl-docs
# scans their git log). Configurable via env so the same orchestrator works
# locally (paths cloned by user) and in cluster (paths cloned by workflow init).
SIBLING_REPOS_PATH = os.getenv(
    "SIBLING_REPOS_PATH",
    "/Users/dmitriimashkov/PycharmProjects/mctlhq",
)
SERVICES_NEEDING_SIBLING_ACCESS = {"mctl-docs"}
_SIBLING_REPOS = (
    "mctl-api", "mctl-web", "mctl-portal", "mctl-agent",
    "mctl-agents", "mctl-gitops", "mctl-openclaw",
)


async def _audit_pre_tool_use(
    input_data: Any,
    _tool_use_id: str | None,
    _context: Any,
) -> dict[str, Any]:
    """Log Bash invocations. Orchestrator git/gh wrappers already print `$ cmd`;
    this covers the SDK Bash tool the model runs under acceptEdits (SOC F8).

    Reads the execution context id fresh on every call (mctlhq/mctl-agents
    #196, ADR 011) rather than a value captured in a closure at hook-build
    time: `_command_audit_hooks()` is called once per `build_*_options()`
    invocation, and two builders resolving the identical config for the same
    run must be able to compare `==` (tests/test_options.py's declarative-
    vs-legacy equivalence tests) — a closure over a freshly-defined inner
    function breaks that even when its captured value is identical, since two
    distinct function objects are never `==`. `_execution_context_headers()`
    reading `os.environ` again per call is cheap next to a Bash tool call.
    """
    tool_name = ""
    tool_input: dict[str, Any] = {}
    if isinstance(input_data, dict):
        tool_name = str(input_data.get("tool_name") or "")
        raw = input_data.get("tool_input") or {}
        if isinstance(raw, dict):
            tool_input = raw
    execution_context_id = _execution_context_headers().get("X-Mctl-Execution-Context", "")
    context_suffix = f" execution_context={execution_context_id}" if execution_context_id else ""
    if tool_name == "Bash":
        print(f"AUDIT tool=Bash cmd={tool_input.get('command', '')!r}{context_suffix}")
    else:
        print(f"AUDIT tool={tool_name}{context_suffix}")
    return {}


HookEventName = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "Notification",
    "SubagentStart",
    "PermissionRequest",
]


def _command_audit_hooks() -> dict[HookEventName, list[HookMatcher]]:
    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[cast(Any, _audit_pre_tool_use)]),
        ],
    }


# A `gh` global option (`-R owner/repo`, `--repo=owner/repo`, `--hostname
# x`, boolean flags like `-h`) between `gh` and its subcommand. Matched
# against tokens, not enumerated by name, so any current or future gh global
# flag is skipped the same way — the alternative (naming `-R`/`--repo`
# explicitly) is exactly the "exact textual spelling instead of command
# semantics" mistake this pattern exists to avoid (mctl-agents#423
# fix-forward, verified Agy P2 on PR #425's merged head).
#
# The short-flag branch is `-[A-Za-z]\S*` rather than a bare `-[A-Za-z]`: gh
# (Go's pflag) accepts an attached shorthand value with no separator at all
# (`-Rowner/repo`), and `\S*` swallows it as part of the flag token so it is
# never mistaken for the subcommand. The value branch is `=\S*|\s+\S+`
# rather than `[=\s]\S+`: the latter treats `=`/whitespace as a single
# interchangeable character, so `-R  owner/repo` (two spaces — including a
# continuation normalized to more than one space) left `owner/repo`
# unconsumed once the first space was spent on `[=\s]` (verified Agy P2,
# round 2, on this same PR).
_GH_GLOBAL_FLAG = r"(?:-[A-Za-z]\S*|--[A-Za-z][\w-]*)(?:=\S*|\s+\S+)?"
_GH_PREFIX = rf"\bgh\b(?:\s+{_GH_GLOBAL_FLAG})*"

# Bash command shapes that fetch a CI log with no bound of their own
# (mctl-agents#423). Matched against the raw command string a Bash tool call
# would run — case-insensitive, since `gh`/`curl`/`wget` invocations are
# lowercase by convention but a model can capitalise anything. The command is
# normalized (see `_normalize_shell_command`) before matching so a
# backslash-continued multiline invocation cannot slip past `[^&|;\n]*`
# stopping at the embedded newline.
_CI_LOG_DENY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"{_GH_PREFIX}\s+run\s+view\b[^&|;\n]*--log(-failed)?\b",
        rf"{_GH_PREFIX}\s+api\b[^&|;\n]*/logs\b",
        r"\b(curl|wget)\b[^&|;\n]*/logs\b",
    )
)


async def _ci_log_guard_hook(
    input_data: Any,
    _tool_use_id: str | None,
    _context: Any,
) -> dict[str, Any]:
    """Deny unbounded CI-log retrieval on a CI-remediation or mixed run.

    The bundle these runs receive already carries a bounded log excerpt per
    failing check (`ci_checks.fetch_failure_logs()`, fetched in the shepherd
    process, outside this run's own envelope) — see
    `run_implementer._render_ci_failures_section`. Nothing in the prompt can
    reliably stop the agent from fetching the log itself anyway: the CLI's
    own Bash-tool timeout backgrounds a slow command rather than failing it
    (mctlhq/mctl-telegram#652), so a ground rule alone cannot prevent the
    orphaned-subagent failure this proposal exists to fix. Denying the
    command before it starts is the only control that actually holds.

    Returns a deny decision (see `claude_agent_sdk.types.
    PreToolUseHookSpecificOutput`) whose reason points the agent at the
    bundle's own evidence and at the refusal marker as the correct escape
    when that evidence is genuinely insufficient.
    """
    tool_name = ""
    command = ""
    if isinstance(input_data, dict):
        tool_name = str(input_data.get("tool_name") or "")
        raw = input_data.get("tool_input") or {}
        if isinstance(raw, dict):
            command = str(raw.get("command") or "")
    if tool_name != "Bash" or not command:
        return {}
    normalized = _normalize_shell_command(command)
    if any(p.search(normalized) for p in _CI_LOG_DENY_PATTERNS):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Unbounded CI-log retrieval is disabled on this run "
                    "(mctl-agents#423). The bundle already carries a bounded "
                    "log excerpt for each failing check under 'Log excerpt "
                    "(bounded, ...)' — use that evidence; it is what exists. "
                    "If it is genuinely insufficient for a code decision, do "
                    "not retry the fetch: stop and write the refusal marker "
                    "instead, explaining what is missing."
                ),
            }
        }
    return {}


def _ci_log_guard_hooks() -> dict[HookEventName, list[HookMatcher]]:
    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[cast(Any, _ci_log_guard_hook)]),
        ],
    }


def _deadline_guard_hook(
    deadline_monotonic: float,
    ledger: CommandBudgetLedger,
    *,
    timeout_available: bool,
):
    """Factory: bind one run's absolute deadline and ledger, return the hook.

    A FACTORY rather than a free function reading `anyio.current_effective_
    deadline()` — the SDK may dispatch a `PreToolUse` hook outside the
    caller's own cancel scope, where that call returns `inf` and the guard
    would silently become a no-op. Closing over an absolute monotonic
    deadline computed once, next to `anyio.fail_after(envelope_s)`, avoids
    that (mctl-agents#430; see `run_implementer._run_implementer_agent`).

    Per `Bash` tool call:

    1. `run_in_background: true`, or a detached form (`detachment_match`,
       read against the QUOTE-MASKED command so quoted text is data) — deny,
       ledger `denied_background += 1`, reason naming the detachment form.
    2. `command_budget(...) is None` (the remaining envelope, minus the
       teardown reserve, cannot clear the floor) — deny, ledger
       `denied_exhausted += 1`, `exhausted = True`, reason telling the agent
       to stop and record the bounded outcome.
    3. otherwise — allow, with `updatedInput` carrying the derived bound: the
       tool-input `timeout` (milliseconds) is only ever NARROWED (`min`
       against any caller-supplied value — clamping is one-directional), and
       — unless `IMPLEMENTER_BOUND_COMMANDS` is off, `timeout_available` is
       False, or the command is shell-state-only (`is_shell_state_only`) —
       the `command` itself is rewritten under `wrap_bounded()`, at the SAME
       effective bound as the clamp (never the wider envelope one), so
       the bound is enforced at the OS level, not only by the CLI's own tool
       timeout (which backgrounds rather than fails an over-running command,
       ADR-011 "Containment"). Ledger `clamped += 1`.

    Non-`Bash` tools return `{}`, untouched.

    Documented fallback if a future CLI/SDK pair stops honouring
    `updatedInput` on an `allow` decision (see task T8 / the SDK-contract
    pin): DENY the unbounded form instead, with the exact `timeout`-wrapped
    command to re-issue in the reason. That needs no SDK support at all,
    unlike relying on `updatedInput` being applied.
    """

    async def _hook(
        input_data: Any,
        _tool_use_id: str | None,
        _context: Any,
    ) -> dict[str, Any]:
        tool_name = ""
        tool_input: dict[str, Any] = {}
        if isinstance(input_data, dict):
            tool_name = str(input_data.get("tool_name") or "")
            raw = input_data.get("tool_input") or {}
            if isinstance(raw, dict):
                tool_input = raw
        if tool_name != "Bash":
            return {}
        command = str(tool_input.get("command") or "")
        if not command:
            return {}

        if tool_input.get("run_in_background") is True:
            ledger.record_denied_background(command, "`run_in_background: true`")
            return _deny(
                "Backgrounding via `run_in_background: true` is disabled on "
                "this run (mctl-agents#430). Run the command synchronously — "
                "it will be bounded automatically; wait for its result before "
                "ending your turn."
            )
        normalized = _normalize_shell_command(command)
        # Quote-aware: `git commit -m "A & B"` carries `&` as data, not as
        # the async-list operator, and denying it blocked ordinary work
        # (claude P2 on `630ac27`). `detachment_match` also hands back the
        # offending fragment so the denial can quote what tripped it.
        detached = detachment_match(normalized)
        if detached is not None:
            detach_form, fragment = detached
            ledger.record_denied_background(command, detach_form)
            return _deny(
                f"Detached execution is disabled on this run "
                f"(mctl-agents#430). What tripped this: {detach_form} in "
                f"`{fragment}`. Backgrounding, nohup/setsid/disown and "
                "polling loops escape the remaining execution budget and are "
                "blocked outright. Re-run the command synchronously and wait "
                "for its result before ending your turn. If that text was "
                "meant as DATA rather than as a shell operator, quote it "
                "(the guard reads quoted text as data)."
            )

        budget_s = command_budget(
            deadline_monotonic,
            anyio.current_time(),
            ceiling_s=IMPLEMENTER_COMMAND_TIMEOUT_SECONDS,
            reserve_s=IMPLEMENTER_TEARDOWN_RESERVE_SECONDS,
            floor_s=IMPLEMENTER_MIN_COMMAND_BUDGET_SECONDS,
        )
        if budget_s is None:
            ledger.record_denied_exhausted(command)
            return _deny(
                "The remaining execution budget for this run cannot fit "
                "another command (mctl-agents#430). Do not run more commands: "
                "commit what is already proven correct and say so in your "
                "final message, or — if nothing is safe to commit — write the "
                "refusal marker with "
                '`{"refused": true, "verification_budget_exhausted": true, '
                '"reason": "<what you could not verify>"}`.'
            )

        # The EFFECTIVE bound: the envelope-derived budget narrowed against
        # any caller-supplied tool timeout, computed ONCE and used for both
        # the tool-input clamp and the OS-level wrapper. Deriving them
        # separately (the wrapper from `budget_s`, the clamp from the
        # caller's value) leaves the CLI backgrounding the command at the
        # narrower bound while `timeout` holds the process group until the
        # wider one -- the orphaned-background-process defect this guard
        # exists to close, reopened inside the guard itself (agy P2 on
        # `630ac27`). Clamping stays one-directional: `min` never widens.
        # The Bash tool's own ceiling binds the EFFECTIVE budget, not just
        # the number written into `timeout`. Clamping only the tool input
        # would leave the OS bound above the CLI's real timer and put the CLI
        # first again -- the ordering this guard depends on.
        effective_s = min(budget_s, BASH_TOOL_MAX_TIMEOUT_MS / 1000.0)
        existing_timeout_ms = tool_input.get("timeout")
        if (
            isinstance(existing_timeout_ms, int | float)
            and not isinstance(existing_timeout_ms, bool)
            and existing_timeout_ms > 0
        ):
            effective_s = min(effective_s, float(existing_timeout_ms) / 1000.0)

        ledger.record_clamped(command, effective_s)
        updated_input = dict(tool_input)
        # A command built only from shell-state builtins (`cd`, `export`, …)
        # is admitted UNWRAPPED: the Bash tool carries that state across
        # calls, and `bash -c` would discard it, so `cd /repo` would stop
        # applying to the next call (claude P2 on `630ac27`). Such a command
        # cannot run long, so the OS-level bound buys nothing anyway; the
        # tool-input clamp below still applies.
        if (
            IMPLEMENTER_BOUND_COMMANDS
            and timeout_available
            and not is_shell_state_only(normalized)
        ):
            updated_input["command"] = wrap_bounded(
                command, effective_s, kill_after_s=IMPLEMENTER_COMMAND_KILL_GRACE_SECONDS
            )
        updated_input["timeout"] = int(effective_s * 1000)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated_input,
            }
        }

    return _hook


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _deadline_guard_hooks(
    deadline_monotonic: float,
    ledger: CommandBudgetLedger,
    *,
    timeout_available: bool,
) -> dict[HookEventName, list[HookMatcher]]:
    return {
        "PreToolUse": [
            HookMatcher(
                matcher="Bash",
                hooks=[cast(Any, _deadline_guard_hook(
                    deadline_monotonic, ledger, timeout_available=timeout_available,
                ))],
            ),
        ],
    }


@dataclass(frozen=True)
class _PolicyCheckpointHook:
    """PreToolUse hook that puts every MCP tool call through the runtime
    policy checkpoint (mctlhq/mctl-agents#197,
    docs/adr/014-policy-checkpoint.md) — the last point in mctl-agents
    before the call leaves for the MCP server.

    A frozen dataclass rather than a closure so two builders resolving the
    same grants still compare `==` (see `_audit_pre_tool_use`'s docstring).
    Fails closed: any exception inside the hook is a deny, never a pass.

    The checkpoint is synchronous and, with `MCTL_POLICY_APPROVALS=mctl-api`,
    makes up to two blocking HTTP calls to mctl-api. It runs in a worker
    thread so that wait blocks this one tool call, not the SDK's event loop
    (its stdio transport and every other task on it).
    """

    grants: tuple[str, ...]

    async def __call__(self, input_data: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        from orchestrator import policy_checkpoint

        try:
            if not isinstance(input_data, dict):
                return _deny("policy checkpoint: unreadable tool call")
            tool_name = str(input_data.get("tool_name") or "")
            tool_input = input_data.get("tool_input")
            decision = await anyio.to_thread.run_sync(functools.partial(
                policy_checkpoint.checkpoint,
                policy_checkpoint.MCP_TOOL_CALL,
                tool_name,
                tool_name.split("__")[1] if tool_name.count("__") >= 2 else "",
                tool_input if tool_input is not None else {},
                grants=self.grants,
            ))
        except Exception as exc:  # noqa: BLE001 — fail closed
            return _deny(f"policy checkpoint failed ({type(exc).__name__}); the call was not made")
        if decision.permitted:
            return {}
        return _deny(
            f"policy {decision.verdict} ({decision.code}, rule {decision.rule_id or '-'}, "
            f"policy {decision.policy_version}): {decision.reason}"
        )


def _policy_hooks(allowed_tools: list[str]) -> dict[HookEventName, list[HookMatcher]]:
    """Every MCP tool call, whatever the server, meets the checkpoint; the
    builder's own allow-list is the grant set it evaluates against."""
    return {
        "PreToolUse": [
            HookMatcher(matcher="mcp__.*", hooks=[cast(Any, _PolicyCheckpointHook(tuple(allowed_tools)))]),
        ],
    }


def _compose_hooks(
    *hook_maps: dict[HookEventName, list[HookMatcher]],
) -> dict[HookEventName, list[HookMatcher]]:
    """Merge hook maps additively: every matcher from every map is kept, on
    whichever event name it was registered under. Used to add the CI-log
    guard hook to a builder WITHOUT dropping `_command_audit_hooks()` —
    `subagent_wait.drain_until_settled`'s precondition is that `hooks` stays
    truthy on every drainable driver, so replacing rather than composing
    would silently break the #366 drain for every CI-remediation run.
    """
    merged: dict[HookEventName, list[HookMatcher]] = {}
    for hook_map in hook_maps:
        for event, matchers in hook_map.items():
            merged.setdefault(event, []).extend(matchers)
    return merged


def _sibling_add_dirs(service_name: str) -> list[str | Path]:
    """For services that scan sibling repos, expand the workspace to include them."""
    if service_name not in SERVICES_NEEDING_SIBLING_ACCESS:
        return []
    base = Path(SIBLING_REPOS_PATH)
    # Typed str | Path (not just str) because ClaudeAgentOptions.add_dirs is
    # list[str | Path] and list is invariant — a plain list[str] here isn't
    # assignable to that parameter even though every element actually is a str.
    dirs: list[str | Path] = [str(base / r) for r in _SIBLING_REPOS if (base / r).exists()]
    return dirs


def _scrubbed(options: ClaudeAgentOptions) -> ClaudeAgentOptions:
    """Every SDK session's options in this module pass through here.

    Blanks the usage-writer token (mctlhq/.github#50) in the session's env.
    The SDK layers `env` over the inherited environment, so a builder that
    passed none, or copied `os.environ`, would hand the token to the CLI
    child and to everything the model runs through Bash; the token is for
    the usage producer in this process only. A builder cannot opt out:
    tests/test_usage_ledger.py fails on any `ClaudeAgentOptions(...)` here
    that is not the direct argument of this function.
    """
    options.env = agent_env_without_writer_token(options.env or os.environ)
    return options


def build_service_agent_options(service_dir: Path, model: str) -> ClaudeAgentOptions:
    """Options for a service-owner agent."""
    allowed_tools = ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "Bash", *_mctl_tool_globs()]
    return _scrubbed(ClaudeAgentOptions(
        cwd=str(service_dir),                  # CLAUDE.md, .claude/, inbox/, proposals/
        setting_sources=["project"],           # pick up .claude/skills and .claude/agents
        model=model,
        allowed_tools=allowed_tools,
        mcp_servers=mctl_mcp_config(always_load=True),
        permission_mode="acceptEdits",         # non-interactive — meant for cron
        max_budget_usd=SERVICE_AGENT_BUDGET_USD,
        add_dirs=_sibling_add_dirs(service_dir.name),
        hooks=_compose_hooks(_command_audit_hooks(), _policy_hooks(allowed_tools)),
        # Extend (NOT replace) parent env — child needs HOME for the Claude
        # credentials lookup, and PATH for `git`/`gh`/`node`/`npm`, which the
        # Bash tool shells out to. The Claude Code CLI itself doesn't need
        # PATH: claude-agent-sdk bundles its own binary and prefers it over
        # anything on PATH (see Dockerfile).
        env={**os.environ, "SIBLING_REPOS_PATH": SIBLING_REPOS_PATH},
    ))


def build_implementer_agent_options(
    repo_dir: Path,
    model: str,
    proposal_dir: Path | None = None,
    *,
    work_class: str = "review",
    deadline_monotonic: float | None = None,
    budget_ledger: CommandBudgetLedger | None = None,
    timeout_available: bool = True,
) -> ClaudeAgentOptions:
    """Options for the Tier 2 implementer agent.

    Runs with cwd inside the cloned sibling repo (not agents/<svc>/).
    setting_sources=["project"] picks up .claude/agents/implementer.md from
    cwd, so the orchestrator copies implementer.md into
    cloned-repo/.claude/agents/ before launching the agent
    (see run_implementer.py).

    add_dirs=[] — no need to see other sibling repos: the agent only
    works in cwd (the cloned target repo) and reads the spec from
    PROPOSAL_DIR (gitops worktree mounted in the pod, path passed via env).

    GITHUB_TOKEN is forwarded from the parent env — the gh CLI in the
    Python wrapper needs it for clone + pr create, but the SDK agent
    itself does not (the agent commits, never pushes).

    ``work_class`` (mctl-agents#423): ``"ci-remediation"`` or ``"mixed"``
    additionally install `_ci_log_guard_hook()`, composed WITH (never
    replacing) `_command_audit_hooks()` — see that function's docstring for
    why. ``"review"`` (the default — every pre-#423 caller) is unaffected.

    ``deadline_monotonic``/``budget_ledger``/``timeout_available``
    (mctl-agents#430): when BOTH ``deadline_monotonic`` and ``budget_ledger``
    are supplied, the deadline guard (`_deadline_guard_hook`) is composed in
    for EVERY work class — the defect it fixes (an agent-issued command
    outliving the remaining envelope) is generic, not CI-remediation-only.
    Omitting either keeps today's behaviour byte-identical: every existing
    caller and test that does not pass them resolves the exact same
    `ClaudeAgentOptions` as before this parameter existed.
    """
    env = {**os.environ}
    if proposal_dir is not None:
        env["PROPOSAL_DIR"] = str(proposal_dir)
    hooks = _command_audit_hooks()
    if work_class in ("ci-remediation", "mixed"):
        hooks = _compose_hooks(hooks, _ci_log_guard_hooks())
    if deadline_monotonic is not None and budget_ledger is not None:
        hooks = _compose_hooks(
            hooks,
            _deadline_guard_hooks(
                deadline_monotonic, budget_ledger, timeout_available=timeout_available,
            ),
        )
    allowed_tools = ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "Bash", *_mctl_tool_globs()]
    return _scrubbed(ClaudeAgentOptions(
        cwd=str(repo_dir),
        setting_sources=["project"],
        model=model,
        allowed_tools=allowed_tools,
        mcp_servers=mctl_mcp_config(always_load=True),
        permission_mode="acceptEdits",
        max_budget_usd=IMPLEMENTER_BUDGET_USD,
        add_dirs=[],
        env=env,
        hooks=_compose_hooks(hooks, _policy_hooks(allowed_tools)),
    ))


def build_mentor_options(mentor_dir: Path, model: str) -> ClaudeAgentOptions:
    """Options for the mentor. Read-only across agent repos, writes only to digest/."""
    # No hooks, so no policy checkpoint either (#197): any hook makes the
    # mentor drainable (mctl-agents#366/#368), which is its own decision.
    # Its MCP calls stay ungoverned until that is taken — ADR 014.
    return _scrubbed(ClaudeAgentOptions(
        cwd=str(mentor_dir.parent),            # .../agents — so the mentor sees every agent
        setting_sources=["project"],
        model=model,
        # Write/Edit: writes only into _mentor/digest/
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit", *_mctl_tool_globs()],
        mcp_servers=mctl_mcp_config(always_load=True),
        permission_mode="acceptEdits",
        max_budget_usd=MENTOR_BUDGET_USD,
    ))


def build_incident_responder_options(
    agent_dir: Path,
    model: str,
    state_dir: Path | None = None,
) -> ClaudeAgentOptions:
    """Options for the incident responder.

    Runs with cwd=agents/_incident-responder/ so setting_sources=["project"]
    picks up its CLAUDE.md. Needs mctl MCP for listing/resolving incidents and
    Read/Write/Glob for the proposal files.

    Deliberately no Bash. This agent reads incident summaries and service logs,
    which are chosen by whoever can make a service log a line or an alert fire,
    and everything keeping that text from being followed as an instruction is
    another instruction in the same context window. With a shell attached, a
    successful injection is remote code execution on the orchestrator; without
    one, the worst case is a badly written proposal.

    The two things it used the shell for are now supplied instead: the current
    UTC timestamp is interpolated into the prompt, and the proposal slug is
    derived from the incident ID by string manipulation rather than by hashing
    it in a subprocess. See mctlhq/mctl-agents#183.
    """
    env = {**os.environ}
    if state_dir is not None:
        env["INCIDENT_STATE_DIR"] = str(state_dir)
    allowed_tools = ["Read", "Write", "Glob", *_mctl_tool_globs()]
    return _scrubbed(ClaudeAgentOptions(
        cwd=str(agent_dir),
        setting_sources=["project"],
        model=model,
        allowed_tools=allowed_tools,
        mcp_servers=mctl_mcp_config(always_load=True),
        permission_mode="acceptEdits",
        max_budget_usd=INCIDENT_RESPONDER_BUDGET_USD,
        env=env,
        hooks=_compose_hooks(_command_audit_hooks(), _policy_hooks(allowed_tools)),
    ))


def build_issue_investigator_options(
    repo_dir: Path,
    model: str,
    proposal_dir: Path,
) -> ClaudeAgentOptions:
    """Options for the issue-investigator agent.

    ``cwd`` is a fresh, read-only clone of the target sibling repo so the
    agent can ground its proposal in the real code (Glob/Grep/Read/Bash).
    ``setting_sources=["project"]`` picks up that repo's own CLAUDE.md as
    context — useful for conventions, harmless if absent.

    The proposal triplet (requirements/design/tasks.md) is written into
    ``proposal_dir`` — the gitops agents-state worktree, which sits OUTSIDE
    cwd — so it must be granted via ``add_dirs``. The orchestrator creates
    that directory before launching the agent.

    GITHUB_TOKEN is forwarded for any `gh`/`git` the agent might run, though
    the Python wrapper already does the issue read + clone + comment.
    """
    env = {**os.environ, "PROPOSAL_DIR": str(proposal_dir)}
    allowed_tools = ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch", "Bash", *_mctl_tool_globs()]
    return _scrubbed(ClaudeAgentOptions(
        cwd=str(repo_dir),
        setting_sources=["project"],
        model=model,
        allowed_tools=allowed_tools,
        mcp_servers=mctl_mcp_config(always_load=True),
        permission_mode="acceptEdits",
        max_budget_usd=ISSUE_INVESTIGATOR_BUDGET_USD,
        add_dirs=[str(proposal_dir)],
        env=env,
        hooks=_compose_hooks(_command_audit_hooks(), _policy_hooks(allowed_tools)),
    ))


def build_issue_investigator_options_from_plan(
    plan: ExecutionPlan,
    repo_dir: Path,
    proposal_dir: Path,
) -> ClaudeAgentOptions:
    """Options for issue-investigator's `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`
    path (orchestrator/run_issue_investigator.py): built from a resolved
    `orchestrator.resolver.ExecutionPlan` instead of the module-level
    ``ISSUE_INVESTIGATOR_MODEL``/``ISSUE_INVESTIGATOR_BUDGET_USD`` constants
    ``build_issue_investigator_options`` above uses.

    Only the fields the plan actually owns (model, tools, budget) come from
    it — every structural field the plan does NOT declare (cwd, mcp_servers,
    permission_mode, add_dirs, env, hooks) matches
    ``build_issue_investigator_options`` exactly, by construction, so the two
    builders resolve identical `ClaudeAgentOptions` for the same repo/model/
    tools/budget input (the equivalence tests in tests/test_options.py
    assert this directly).

    `mcp__mctl__*` is the one tool whose presence is a conjunction of two
    facts, and BOTH have to hold:

    1. the profile grants it — `plan.tools` is the authoritative allow-list
       under ADR 007, and a future `ExecutionProfile` may narrow it;
    2. mctl MCP is actually configured — `_mctl_tool_globs()` gates the
       legacy builder the same way, so an unset `MCTL_TOKEN` must not leave
       the declarative path holding a dead allow-list entry the legacy path
       omits.

    Treating only (2) as the condition is a privilege escalation, not a
    divergence: a profile that deliberately withholds the mctl tools would
    have them handed back whenever `MCTL_TOKEN` happened to be set. That is
    dormant today only because the single checked-in fixture always lists
    `mcp__mctl__*` — an accident of the fixture, not a property of the
    design (claude P2 on #234, third round; earlier rounds fixed (2) alone).
    """
    env = {**os.environ, "PROPOSAL_DIR": str(proposal_dir)}
    # HUMAN_INPUT_CAPABILITY is filtered out alongside "mcp__mctl__*": both
    # are entries in plan.tools that are not literal SDK tool names, so
    # passing either through to allowed_tools verbatim would leave a dead
    # entry the CLI can never match against a real tool call.
    allowed_tools = [
        t for t in plan.tools if t not in ("mcp__mctl__*", HUMAN_INPUT_CAPABILITY)
    ]
    if "mcp__mctl__*" in plan.tools:
        allowed_tools += _mctl_tool_globs()
    return _scrubbed(ClaudeAgentOptions(
        cwd=str(repo_dir),
        setting_sources=["project"],
        model=plan.model,
        allowed_tools=allowed_tools,
        mcp_servers=mctl_mcp_config(always_load=True),
        permission_mode="acceptEdits",
        max_budget_usd=plan.budget_usd,
        add_dirs=[str(proposal_dir)],
        env=env,
        hooks=_compose_hooks(_command_audit_hooks(), _policy_hooks(allowed_tools)),
    ))


def build_shepherd_options(shepherd_dir: Path, model: str) -> ClaudeAgentOptions:
    """Options for the Tier 3 shepherd sub-agent.

    The sub-agent's only job is to translate a pre-filtered bundle of
    P1/P2 codex findings into a JSON `{p1, p2, summaries}` object the
    Python wrapper hands to the Tier 2 implementer via
    `--review-feedback`. It is read-only — no Write/Edit/Bash.

    ``cwd`` is `agents/_shepherd/` so `setting_sources=["project"]`
    picks up `agents/_shepherd/.claude/agents/shepherd.md` — same
    pattern as every other service agent in this repo (sub-agent prompts
    live under `.claude/agents/` in the working directory).

    No mctl MCP, no sibling repos — the bundle is self-contained text.
    """
    return _scrubbed(ClaudeAgentOptions(
        cwd=str(shepherd_dir),
        setting_sources=["project"],
        model=model,
        allowed_tools=["Read"],
        mcp_servers={},
        permission_mode="acceptEdits",
        max_budget_usd=SHEPHERD_BUDGET_USD,
        env={**os.environ},
    ))
