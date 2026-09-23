"""Tier 2 implementer — turns an `accepted` proposal into a real PR.

Pipeline per proposal:
    1. Find proposals/<slug>/.status.yaml with status: accepted
    2. Query GitHub for the canonical PR and deterministic branch. Adopt an
       existing result, or fail closed when GitHub cannot be queried.
    3. Mark .status.yaml as `in-progress` with an expiring attempt lease.
    4. `gh repo clone mctlhq/<service>` to /tmp/impl-<service>-<slug>-<ts>/
    5. Create branch `feat/agents-<slug>` in the cloned repo.
    6. Run the implementer Claude sub-agent with cwd=cloned-repo and
       PROPOSAL_DIR pointing at the gitops proposal directory. The agent
       reads requirements.md / design.md / tasks.md, edits files, and
       commits — but does NOT push (orchestrator handles push + PR open).
    7. `git push -u origin <branch>` from the cloned repo.
    8. `gh pr create` with title/body referencing the proposal.
    9. Update .status.yaml → `implemented`, write the PR URL.

Review-feedback mode (`--review-feedback <path>`):
    Used by the Tier 3 shepherd to address codex P1/P2 findings on an
    existing PR. The bundle JSON has shape
    `{"p1": bool, "p2": bool, "summaries": [{"file": "...",
    "line": 123, "severity": "P1", "body": "..."}, ...]}` written by
    `orchestrator.run_shepherd.apply_followup()`.

    Behaviour differences from the new-branch path:
    - The branch `feat/agents-<slug>` MUST already exist on origin; the
      implementer fetches it and checks it out (no `-b`).
    - The codex findings bundle is appended to the sub-agent prompt as
      additional context.
    - After the agent commits, the implementer pushes to the existing
      branch (no `-u`) and does NOT open a new PR — the existing PR
      auto-updates because head ref is unchanged.
    - `.status.yaml` does NOT flip to `implemented` on success. The
      shepherd already wrote `review-fixing` before the subprocess call;
      this mode leaves the status as-is so the next shepherd tick can
      re-evaluate codex against the new commit. The transition to
      `implemented` (and eventually `merged`) belongs to the shepherd.

Auth:
    Uses GITHUB_TOKEN from env (Sub-plan B introduces this in the
    mctl-agents-secrets ExternalSecret as `github-token` Vault key, surfaced
    via `dataFrom: extract`). The PAT must have `repo` write scope on
    `mctlhq/*`. When sub-plan B has not yet landed, GITHUB_TOKEN is empty
    and `gh` CLI falls back to the device-flow login — the script will fail
    fast in that case (set --dry-run to verify spec parsing without auth).

Idempotency:
    GitHub is checked before any model call.  An existing PR or remote result
    branch is adopted instead of re-running the implementer. Failed attempts
    move to `needs-triage`; an operator must explicitly move that proposal
    back to `accepted` before it can run again.

    A THIRD outcome sits beside "succeeded" and "failed/skipped": an
    `accepted` proposal whose `control.requires_human_approval` is set but
    carries no verified `approval.approved_by` can never run as written --
    `accepted` alone is not authorisation (gitops#986), and no supported
    path records an approver on an already-accepted proposal
    (mctl-agents#349). That proposal is classified `blocked` (stable code
    `approval-missing`), never `skipped`: it never counts toward the run's
    `--max-proposals` budget, and (outside `--dry-run`) the implementer
    writes a durable top-level `blocked: {code, since, message, remedy}`
    block to its `.status.yaml`, leaving `status: accepted` unchanged. The
    write is idempotent -- unchanged `code`/`message`/`remedy` leave the
    file byte-identical -- so a permanently blocked proposal produces
    exactly one gitops commit, not one per tick. The block is cleared as
    soon as the proposal clears the approval gate on a later run. A batch
    whose only proposals are blocked exits `EXIT_BLOCKED_ONLY` (45) instead
    of `0`, so the condition is visible on the workflow itself; a batch that
    also produced a PR still exits `0` so that write is not put at risk.
    `--dry-run` reports a blocked proposal in its summary but never writes
    the marker and never changes the exit code.

    A fourth gate sits between the GitHub preflight and the model call: when
    the preflight finds no existing branch or PR at all, the implementer
    also reads the proposal's source GitHub issue (`source:` in
    `.status.yaml`, written by the investigator) and refuses -- writing
    `needs-triage` with `failure.code` `source-resolved` or
    `source-not-planned` at `failure.stage: admission` -- if that issue is
    already closed, so subscription quota is never spent on work that is
    already done or abandoned (mctl-agents#410). A proposal with no
    `source` block, or whose issue is still open, is unaffected; an
    unreadable GitHub leaves it `accepted` and untouched.

Usage:
    python -m orchestrator.run_implementer
    python -m orchestrator.run_implementer --service mctl-web
    python -m orchestrator.run_implementer --service mctl-web --slug wrangler-cve-0933
    python -m orchestrator.run_implementer --slug wrangler-cve-0933
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import anyio
from claude_agent_sdk import ClaudeSDKClient, ResultMessage

from config.settings import (
    AGENTS_DIR,
    SERVICE_AGENT_MODEL,
    SERVICES,
)
from orchestrator import tracing
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.exec_budget import CommandBudgetLedger
from orchestrator.execution_identity import ExecutionIdentityError, load_from_environment, mint_local
from orchestrator.github_token import refresh_github_token
from orchestrator.lifecycle import rollout
from orchestrator.lifecycle.claim import ClaimClient, blocks_mutation
from orchestrator.lifecycle.contract import (
    CLAIM_FENCED,
    CLAIM_HELD_BY_OTHER,
    CLAIM_STATE_EXPIRED,
    CLAIM_STATE_RELEASED,
    CLAIM_UNCLAIMED,
    CLAIM_UNKNOWN,
    OWNER_IMPLEMENTER,
    PHASE_IMPLEMENT,
    PHASE_REVIEW_REMEDIATION,
    EntityRef,
    Executor,
)
from orchestrator.manifest import MANIFESTS_DIR
from orchestrator.manifest import load as load_agent_manifest
from orchestrator.mcp_guard import ensure_mctl_connected
from orchestrator.options import (
    IMPLEMENTER_COMMAND_TIMEOUT_SECONDS,
    IMPLEMENTER_DRAIN_TIMEOUT_SECONDS,
    IMPLEMENTER_TEARDOWN_GRACE_SECONDS,
    IMPLEMENTER_TIMEOUT_CEILING_SECONDS,
    IMPLEMENTER_TIMEOUT_SECONDS,
    build_implementer_agent_options,
    implementer_envelope,
)
from orchestrator.proc import describe_output, run_capturing
from orchestrator.proposal_state import (
    BLOCKED_APPROVAL_MISSING,
    human_approval_satisfied,
    load_status,
    now_iso,
    update_status_file,
)
from orchestrator.service_skills import ServiceSkillBundle, ServiceSkillError, neutralize_service_skill_tags
from orchestrator.service_skills import is_enabled as is_service_skills_enabled
from orchestrator.service_skills import pin_sha as service_skill_pin_sha
from orchestrator.service_skills import resolve_bundle as resolve_service_skill_bundle
from orchestrator.source_issue import SourceIssueVerdict, read_source_issue
from orchestrator.subagent_wait import (
    LiveTaskLedger,
    OrphanedSubagentError,
    drain_until_settled,
)
from orchestrator.temporal.issue_ref import workflow_id_for

# ---------------------------------------------------------------------------
# State directory resolution.
# In the cluster, the orchestrator container has /workdir/mctl-gitops/
# mounted (see entrypoint.sh + cwft-mctl-agents-implement.yaml). Locally, the
# user can override via STATE_DIR env or pass --state-dir.
# ---------------------------------------------------------------------------
DEFAULT_STATE_DIR = Path(
    os.getenv(
        "STATE_DIR",
        "/workdir/mctl-gitops/platform-gitops/agents-state",
    )
)


# ---------------------------------------------------------------------------
# Sentinel exit codes for review-feedback mode. These let the Tier 3 shepherd
# tell three kinds of failure apart: deterministic *content* failures, transient
# subprocess / auth / network plumbing failures, and *harness* failures where our
# own orchestration lost the agent's work. Only the first should consume one of
# the MAX_REVIEW_ATTEMPTS budget slots and eventually flip the proposal to
# `review-stuck`; the other two are retried without charging the proposal.
# Without a sentinel, the shepherd treats every non-zero exit as transient and
# retries forever — see codex P1 on PR #12.
#
# The mapping is intentionally narrow: any error path that is NOT one of
# these explicit codes falls back to plain `sys.exit(1)` which the shepherd
# classifies as transient. New deterministic-failure paths must opt in by
# returning an `ImplementResult.error` whose prefix matches the table in
# `_review_feedback_exit_code()`.
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_GENERIC_FAILURE = 1
EXIT_NO_FOLLOWUP_COMMITS = 42
EXIT_BRANCH_MISSING_ON_ORIGIN = 43
EXIT_OPERATION_TIMEOUT = 44
# Batch-mode only (see `main()`); never returned from --review-feedback mode,
# whose exit codes come from `_review_feedback_exit_code()` below.
#
# See mctlhq/mctl-gitops#1206 (filed as the required follow-up):
# cwft-mctl-agents-approve.yaml should record approval.approved_by on an
# already-accepted proposal, and cwft-mctl-agents-implement.yaml should not
# treat EXIT_BLOCKED_ONLY as a retryable/quota failure -- today neither the
# `implement-fallback` `when` gate nor `assert-attempt` looks at an exit
# code, both compare Argo step status strings, so this exit makes
# `implement` `Failed`, triggers a pointless account-2 retry, and blames the
# Claude usage limit in `assert-attempt`'s stderr for an unapproved
# proposal (mctl-agents#349). Tracked in mctl-gitops, not mctl-agents, since
# both fixes are CWFT-side.
EXIT_BLOCKED_ONLY = 45
# Harness failure, NOT a content failure: the CLI launched the implementer
# sub-agent asynchronously and the run ended before that child reported a
# terminal status, so the work it was doing is lost (mctl-agents#366). Unlike
# 42/43/44 this says nothing about the proposal or the findings -- re-running is
# the correct response and the shepherd must NOT charge a review attempt for it.
EXIT_ORPHANED_SUBAGENT = 46
# Deliberate no-op, NOT a failure: the agent read the findings, decided the
# right move was to change nothing, and said so in a machine-readable marker
# file (see REFUSAL_MARKER_FILENAME). Covers both "the finding is invalid or
# already addressed" and "an explicit operator decision recorded on the PR
# forbids this change" (mctl-agents#360). Re-running is pointless *for these
# findings*, but the proposal did nothing wrong, so the shepherd must NOT
# charge a review attempt: on portfolio#56 two correct refusals burned 2 of 5
# attempts and forced a GitOps reset (mctl-gitops#1205).
EXIT_DELIBERATE_NO_OP = 47
# Fenced, NOT a failure of the proposal or the findings: an ExecutionClaim
# check immediately before a push answered CLAIM_FENCED — the owner epoch
# moved (a handoff completed) or the entity's pinned version changed (a new
# PR head) since this attempt's claim was pinned. Re-running against the
# world this attempt observed cannot succeed; the next tick must take a new
# claim against the CURRENT world instead (ADR-010 phase 2, #352). Shares the
# non-charging behaviour of 46/47: the executor did nothing wrong.
EXIT_FENCED = 48
# A claim check REFUSED this attempt: another executor holds the entity, or
# the claim store could not be reached while `LIFECYCLE_OWNERSHIP_REQUIRED` is
# on. Distinct from 48 — nothing moved under this attempt, it simply may not
# act right now — but it shares 48's shepherd handling (`FollowupKind =
# "fenced"`, non-charging, its own log line) because the operator question is
# the same one: a claim stood this attempt down, deliberately. Left as
# EXIT_GENERIC_FAILURE it reached the operator as "follow-up subprocess failed
# transiently", the exact legibility gap EXIT_FENCED was added to close, and
# it repeats every tick for the life of a leaked lease (claude P3 on
# `31232dc`).
EXIT_CLAIM_REFUSED = 49
# The agent ran, but the bounded CI-log evidence the bundle carried could not
# support a code decision, and it said so via the refusal marker (below),
# distinguished from a plain refusal by `"insufficient_evidence": true`
# (mctl-agents#423). Distinct from EXIT_DELIBERATE_NO_OP (47): a 47 means "the
# findings are addressed, or an operator forbade the change" -- a considered
# NO on the merits. A 50 means "I cannot tell, on what I was handed" -- the
# agent never reached a merits decision at all, so charging it to `refusals`
# (bounded by MAX_REFUSALS) would misrepresent a platform-supplied-evidence
# gap as the agent repeatedly declining to act. It joins the shepherd's
# harness set instead: blameless, and bounded by the same
# MAX_HARNESS_FAILURES an orphaned sub-agent is (see
# run_shepherd._followup_code_sets).
EXIT_CI_EVIDENCE_INSUFFICIENT = 50
# The run ended with the per-command execution budget exhausted: every
# agent-issued Bash command is bounded to what remains of the run's envelope
# (mctl-agents#430, `orchestrator/exec_budget.py`'s deadline guard), and this
# run ran out of budget for another command before it could commit or refuse
# on the merits. Distinct from EXIT_ORPHANED_SUBAGENT (46): 46 means our own
# handoff lost a live child; 51 means the run stayed inside its envelope the
# whole time and the STRUCTURED ledger (never model prose) says so -- the
# same "no live agent-launched child process survives" guarantee, reached
# deliberately instead of by a hard cancellation. Joins the shepherd's
# harness set: blameless, bounded by the same MAX_HARNESS_FAILURES.
EXIT_VERIFICATION_BUDGET_EXHAUSTED = 51

# Machine-readable refusal marker, written by the agent in the root of the
# cloned target repo. A file is deliberately chosen over scraping the final
# message: the prompt already asks the agent to "STOP without committing and
# explain why", but prose is unstable across model versions, and a regex over
# it is one wording away from either missing a real refusal or — far worse —
# reading a crash narration as one. Writing a named JSON file with an exact
# shape is an act the agent cannot perform by accident while doing something
# else.
REFUSAL_MARKER_FILENAME = ".implementer-refusal.json"
# Prefix on `ImplementResult.error` that `_review_feedback_exit_code()` maps to
# EXIT_DELIBERATE_NO_OP. Same string-prefix style as the other sentinels.
REFUSAL_ERROR_PREFIX = "deliberate no-op:"
# Prefix mapped to EXIT_FENCED. Same style, raised by ImplementerFenced.
FENCED_ERROR_PREFIX = "fenced:"
# Prefix mapped to EXIT_CLAIM_REFUSED, raised by ImplementerClaimRefused.
CLAIM_REFUSED_ERROR_PREFIX = "claim-refused:"
# Prefix mapped to EXIT_CI_EVIDENCE_INSUFFICIENT. Same style, used when the
# refusal marker carries `"insufficient_evidence": true` (mctl-agents#423).
CI_EVIDENCE_INSUFFICIENT_ERROR_PREFIX = "ci-evidence-insufficient:"
# Prefix mapped to EXIT_VERIFICATION_BUDGET_EXHAUSTED (mctl-agents#430). Used
# both when the ORCHESTRATOR-derived ledger reports `exhausted` with no
# commit and no other refusal, and when the agent's own marker carries
# `"verification_budget_exhausted": true`.
VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX = "verification-budget-exhausted:"
# The reason travels into a `.status.yaml` note and a summary line; cap it so a
# verbose model cannot turn the durable projection into a transcript.
MAX_REFUSAL_REASON_CHARS = 600
# Refuse to READ a marker larger than this, rather than reading it and then
# rejecting it. The file sits in a cloned target repo's worktree and is written
# by an LLM holding a Bash tool: a redirect into the wrong path, a loop that
# appends, or a stray `tee` is enough to make it enormous, with no malice
# required. `Path.read_text()` would pull all of it into the orchestrator, and
# an implementer OOM is the least diagnosable failure this platform has --
# memory pressure there is a live issue, and an OOMKill does not appear in the
# container's `last_terminated_reason`. Generous next to a legitimate marker,
# whose `reason` is itself capped at MAX_REFUSAL_REASON_CHARS, so nothing
# honest is anywhere near it.
MAX_REFUSAL_MARKER_BYTES = 64 * 1024


@dataclass(frozen=True)
class RefusalMarker:
    """A validated `.implementer-refusal.json` (see `_read_refusal_marker`).

    `insufficient_evidence` (mctl-agents#423) distinguishes a considered "no"
    on the merits (the ordinary refusal, exit 47 — findings addressed, or an
    operator decision forbids the change) from "the bounded CI-log evidence
    I was handed cannot support a code decision" (exit 50 — the agent never
    reached a merits decision at all). Both share the same marker shape and
    the same validation; only the mapped exit code, and therefore the
    shepherd's charging behaviour, differs.

    `verification_budget_exhausted` (mctl-agents#430) is the third form: the
    agent decided, on its own, that the remaining per-command execution
    budget could not fit another verification step and recorded that
    deliberately rather than being cut off by the deadline guard's own
    denial. Maps to the same EXIT_VERIFICATION_BUDGET_EXHAUSTED the
    ORCHESTRATOR-derived ledger produces when it observes the same fact.
    """

    reason: str
    insufficient_evidence: bool = False
    verification_budget_exhausted: bool = False


def _read_refusal_marker(repo_dir: Path) -> RefusalMarker | None:
    """Return the refusal marker iff this run produced a valid one.

    Every check below exists to make "the agent refused" something that cannot
    be produced by accident:

    - the file must be small enough to read at all
      (``MAX_REFUSAL_MARKER_BYTES``). Enforced by a BOUNDED READ — one byte
      past the cap, then a length test — not by ``stat()`` followed by
      ``read_text()``. The marker lives in the agent's own workspace and the
      agent holds a Bash tool, so a size established by an earlier syscall is
      not a fact about the read that follows it: a background appender (a stray
      loop, no malice needed) passes the check and then makes the read grow
      without bound. A bounded read cannot be raced, needs no ``stat()``, and
      the one extra byte distinguishes "exactly at the cap" from "over it"
      without a second syscall. Over-cap is refused outright rather than
      truncated: a truncated marker would fail the JSON check below and reach
      the charged path for an accidental reason, hiding an operational problem
      behind a correct outcome;
    - the file must parse as a JSON object with ``refused`` exactly ``True``
      and a non-empty string ``reason`` — a stray file, a truncated write or a
      progress note does not qualify;
    - the file must be UNTRACKED, *provably* so. A marker committed into a
      target repo would otherwise make every future follow-up on that repo look
      like a refusal and permanently exempt it from the attempt cap, so a
      ``git ls-files`` invocation that fails to answer the question counts as a
      no, not a yes.

    Returns ``None`` (and logs why) for anything that does not qualify, so the
    caller falls back to the ordinary "no follow-up commits" failure.

    The handlers below are deliberately BROAD rather than a tuple of the
    exception types we happened to think of. Enumerating them is the same class
    of bug as a bound written ``value <= 0`` that misses ``nan``: deeply nested
    JSON raises ``RecursionError`` (a ``RuntimeError``, so ``OSError |
    ValueError`` misses it) from a payload that fits inside
    ``MAX_REFUSAL_MARKER_BYTES`` — and an escape from here does not fail safe.
    It surfaces as a bare ``Exception`` with no prefix
    ``_review_feedback_exit_code`` recognises, exits 1, and the shepherd
    classifies 1 as *transient* — the one arm with no counter at all, so it
    retries a paid run every tick forever. Catching broadly is safe here
    precisely because the fallback is the conservative, charged path: every
    failure means "not a refusal", which costs one bounded attempt.
    ``BaseException`` (KeyboardInterrupt, SystemExit) is deliberately not
    caught.
    """
    path = repo_dir / REFUSAL_MARKER_FILENAME
    if not path.is_file():
        return None
    # Bounded read, binary: `read(n)` on a text handle counts CHARACTERS, which
    # would let a multi-byte payload pull up to 4n bytes past a cap named in
    # bytes. Reading one byte past the cap makes the over-cap case detectable
    # from the length alone, and nothing is decoded unless the whole file
    # arrived under the cap, so a partial UTF-8 sequence is never in play.
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_REFUSAL_MARKER_BYTES + 1)
    except Exception as e:  # noqa: BLE001 — see the docstring: broad on purpose
        print(
            f"warn: cannot read {REFUSAL_MARKER_FILENAME} "
            f"({type(e).__name__}: {e}); ignoring"
        )
        return None
    if len(raw) > MAX_REFUSAL_MARKER_BYTES:
        print(
            f"warn: {REFUSAL_MARKER_FILENAME} is over the "
            f"{MAX_REFUSAL_MARKER_BYTES}-byte cap (the read stopped there); "
            f"refusing it. A marker this size is not a considered no-op — "
            f"treating the run as a plain no-commit failure"
        )
        return None
    try:
        tracked = _run(
            ["git", "ls-files", "--error-unmatch", REFUSAL_MARKER_FILENAME],
            cwd=repo_dir,
            check=False,
        )
    except Exception as e:  # noqa: BLE001 — see the docstring: broad on purpose
        # The last step in this function that could still throw past the
        # caller. Deliberately swallows ImplementerOperationTimeout too: a slow
        # `git ls-files` would otherwise reclassify the whole run as a timeout
        # (44) when the honest statement is narrower — we could not establish
        # whether the marker is tracked, so it is not a refusal (42). Both
        # charge an attempt; only one of them is true.
        print(
            f"warn: could not check whether {REFUSAL_MARKER_FILENAME} is "
            f"tracked ({type(e).__name__}: {e}); ignoring the marker"
        )
        return None
    if tracked.returncode == 0:
        print(
            f"warn: {REFUSAL_MARKER_FILENAME} is tracked in {repo_dir.name}; "
            f"ignoring it — only a marker written during this run counts"
        )
        return None
    if tracked.returncode != 1:
        # 0 is tracked, 1 is "no such path in the index". Anything else (a
        # corrupt or absent index, an unexpected 128) means we could not
        # establish the fact — and every other check in this function treats
        # "could not establish" as "not a refusal". Erring the other way here
        # would honour a marker precisely when the repository state is unknown.
        #
        # A missing `git` binary is NOT one of these: `subprocess.run` raises
        # FileNotFoundError rather than returning, `check=False` or not, so it
        # is the handler above — not this branch — that absorbs it.
        print(
            f"warn: could not determine whether {REFUSAL_MARKER_FILENAME} is "
            f"tracked (git ls-files exited {tracked.returncode}); ignoring"
        )
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — see the docstring: broad on purpose
        # RecursionError (nested JSON), UnicodeDecodeError (a binary file
        # written to this path), MemoryError, and whatever the next one turns
        # out to be all mean the same thing: we could not read a refusal here.
        print(
            f"warn: {REFUSAL_MARKER_FILENAME} is not readable JSON "
            f"({type(e).__name__}: {e}); ignoring"
        )
        return None
    if not isinstance(data, dict) or data.get("refused") is not True:
        print(f"warn: {REFUSAL_MARKER_FILENAME} has no `refused: true`; ignoring")
        return None
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        print(f"warn: {REFUSAL_MARKER_FILENAME} carries no reason; ignoring")
        return None
    return RefusalMarker(
        reason=" ".join(reason.split())[:MAX_REFUSAL_REASON_CHARS],
        insufficient_evidence=data.get("insufficient_evidence") is True,
        verification_budget_exhausted=data.get("verification_budget_exhausted") is True,
    )


def _write_refusal_out(path: Path, reason: str) -> None:
    """Hand the refusal reason to the caller (the shepherd) as JSON.

    Best-effort by design: the exit code alone already carries the decision
    that matters (do not charge an attempt). Losing the prose must never turn
    a correct refusal into a failed run, so an unwritable path is a warning.
    """
    try:
        path.write_text(
            json.dumps({"refused": True, "reason": reason}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 — advisory write; see below
        # Broad for the same reason as `_read_refusal_marker`, and with a
        # sharper consequence: this runs immediately before `sys.exit(47)`, so
        # an escape would replace a correctly-classified refusal with an
        # uncaught traceback and exit 1 — the counter-less transient arm. The
        # reason is advisory; the exit code is what matters.
        print(
            f"warn: could not write refusal reason to {path} "
            f"({type(e).__name__}: {e}); the exit code still carries the decision",
            file=sys.stderr,
        )


def _write_verification_budget_exhausted_out(
    path: Path, reason: str, ledger: CommandBudgetLedger | None,
) -> None:
    """Hand the ORCHESTRATOR-derived ledger summary to the shepherd as JSON
    (mctl-agents#430) — the structured evidence `EXIT_VERIFICATION_BUDGET_
    EXHAUSTED` exists to provide, not model prose. `reason` overrides the
    ledger's own `describe()` text: for the marker-derived path it is the
    agent's own (capped) explanation, which is more useful to an operator
    than the orchestrator's clamp/deny counters alone; those counters still
    ride along from `ledger.as_dict()` when a ledger is available.

    Best-effort, same as `_write_refusal_out`: the exit code alone already
    carries the decision that matters.
    """
    payload: dict[str, Any] = ledger.as_dict() if ledger is not None else {}
    payload["reason"] = reason
    payload["refused"] = True
    payload["verification_budget_exhausted"] = True
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — advisory write; see _write_refusal_out
        print(
            f"warn: could not write refusal reason to {path} "
            f"({type(e).__name__}: {e}); the exit code still carries the decision",
            file=sys.stderr,
        )


def _review_feedback_exit_code(error: str) -> int:
    """Map an ``ImplementResult.error`` string to a sentinel exit code.

    Deterministic content failures get their own non-1 codes so the Tier 3
    shepherd can distinguish them from transient plumbing errors:

      - 42: agent ran but committed nothing — re-running the SDK with the
        same findings will deterministically reproduce the same outcome.
      - 43: PR branch was deleted on origin between ticks — re-running
        cannot resurrect it; a human must look at the PR.
      - 44: a model stream or shell command exceeded its explicit wall-clock
        bound. Re-running forever cannot make forward progress, so this
        consumes the shepherd's bounded review-attempt budget.

    Two codes are deliberately NOT deterministic:

      - 47: the agent deliberately changed nothing and recorded why in the
        refusal marker (mctl-agents#360) — either the findings were already
        addressed or an explicit operator decision on the PR forbade the
        change. Re-running these same findings is pointless, but the proposal
        is not at fault, so the shepherd records the reason and waits instead
        of spending one of its bounded attempts.

      - 46: the CLI launched the sub-agent asynchronously and the run ended
        before it reported a terminal status (mctl-agents#366). The agent never
        got its attempt, so charging the proposal for it would let a PR exhaust
        MAX_REVIEW_ATTEMPTS without a single real try at the findings. The
        shepherd classifies this as a harness failure and retries.

      - 48: an ExecutionClaim check right before the push answered
        CLAIM_FENCED (ADR-010 phase 2, #352) — the epoch moved or the pinned
        entity version changed since this attempt's claim was taken. The
        executor aborted before invoking git; the shepherd must not charge a
        review attempt for a race it did not cause.

      - 49: an ExecutionClaim check REFUSED this attempt — another executor
        holds the entity, or the store was unreachable under the ownership
        break-glass. Nothing moved, so it is not a fence; the shepherd
        handles it with the same non-charging arm and its own log line,
        because "a claim stood this attempt down" is what the operator needs
        to read either way.

      - 50: the bounded CI-log evidence in the bundle could not support a
        code decision, and the agent said so via the refusal marker's
        `insufficient_evidence` flag (mctl-agents#423). Distinct from 47: the
        agent never reached a merits decision, so the shepherd treats it as
        blameless the way it treats an orphaned sub-agent (its own harness
        counter, bounded by MAX_HARNESS_FAILURES) rather than charging it to
        `refusals`.

      - 51: the run ended with the per-command execution budget exhausted
        (mctl-agents#430) — every agent-issued Bash command is bounded to
        what remains of the run's envelope, and either the orchestrator's
        own ledger observed that budget run out with no commit produced, or
        the agent recorded the same fact itself via the refusal marker's
        `verification_budget_exhausted` flag. Blameless the same way 46 and
        50 are: the run stayed inside its envelope and said so structurally,
        rather than being cut off by the outer bound.

    Everything else (non-timeout shell failures, SystemExit from missing
    config, unexpected exceptions) is left as ``EXIT_GENERIC_FAILURE`` and
    the shepherd treats it as transient.

    ``EXIT_BLOCKED_ONLY`` (45) is NOT reachable from here: it is a batch-mode
    exit code from `main()`'s new-branch path, and `--review-feedback` mode
    never evaluates the approval gate (it selects `{implemented,
    review-fixing}` proposals, which are already past `accepted`).
    """
    if not error:
        return EXIT_OK
    if error == "implementer produced no follow-up commits":
        return EXIT_NO_FOLLOWUP_COMMITS
    if error.startswith("branch ") and "not found on origin" in error:
        return EXIT_BRANCH_MISSING_ON_ORIGIN
    if error.startswith(REFUSAL_ERROR_PREFIX):
        return EXIT_DELIBERATE_NO_OP
    if error.startswith(FENCED_ERROR_PREFIX):
        return EXIT_FENCED
    if error.startswith(CLAIM_REFUSED_ERROR_PREFIX):
        return EXIT_CLAIM_REFUSED
    if error.startswith(CI_EVIDENCE_INSUFFICIENT_ERROR_PREFIX):
        return EXIT_CI_EVIDENCE_INSUFFICIENT
    if error.startswith(VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX):
        return EXIT_VERIFICATION_BUDGET_EXHAUSTED
    if error.startswith("orphaned sub-agent:"):
        return EXIT_ORPHANED_SUBAGENT
    if error.startswith("operation timed out:"):
        return EXIT_OPERATION_TIMEOUT
    return EXIT_GENERIC_FAILURE


@dataclass
class ProposalRef:
    """Lightweight handle to a proposal on disk."""

    service: str          # e.g. "mctl-web"
    slug: str             # e.g. "wrangler-cve-0933"
    proposal_dir: Path    # state-dir / service / proposals / slug
    status: str           # current value parsed from .status.yaml (or "proposed" if absent)
    # Whether this proposal's own control block is satisfied. Carried here
    # because find_accepted_proposals already has the parsed .status.yaml in
    # hand and used to throw it away, leaving the consumer with nothing but
    # `status` to gate on (gitops#986).
    #
    # Fail-CLOSED default: a ref built without consulting the status file has
    # not established that anyone approved it, and defaulting to True would
    # make "forgot to pass it" indistinguishable from "a human approved it"
    # (agy P2). find_accepted_proposals always computes it.
    approval_ok: bool = False
    status_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.status_path = self.proposal_dir / ".status.yaml"


@dataclass
class ImplementResult:
    ref: ProposalRef
    pr_url: str | None
    error: str | None = None
    skipped_reason: str | None = None
    # Stable code (e.g. BLOCKED_APPROVAL_MISSING) when the proposal was
    # never attempted because it is permanently unrunnable as written --
    # distinct from both `error` (an attempt ran and failed) and a plain
    # `skipped_reason` (still counted separately by `_batch_outcome()`).
    # `skipped_reason` is still set alongside this so any consumer that
    # only reads the old channel keeps seeing a human-readable reason.
    blocked: str | None = None
    # True only when the blocked marker was freshly written or changed on
    # THIS tick (see `_mark_blocked`'s return value); False when an
    # already-recorded, unchanged marker was merely re-observed. Lets
    # `main()` avoid re-failing forever on a permanently blocked proposal.
    blocked_is_new: bool = False
    counts_toward_limit: bool = True
    # Set only on the admission gate's refusal arm (mctl-agents#410): the
    # `(code, issue_ref)` pair `main()` prints in `=== Stale source ===`.
    # Distinct from `blocked` -- this is a `needs-triage` write (the
    # proposal leaves the accepted queue), not a durable `accepted` park.
    stale_source: tuple[str, str] | None = None
    # Set only on the EXIT_VERIFICATION_BUDGET_EXHAUSTED path (mctl-agents#430):
    # the ledger `_run_implementer_agent`'s deadline guard populated, carried
    # here so `main()` can write its structured counts (not just `error`'s
    # reason text) to `--refusal-out`. `None` on every other path.
    budget_ledger: CommandBudgetLedger | None = None
    # True only on `implement_one`'s budget hand-back arm (mctl-agents#430):
    # the proposal was handed back to `accepted` with an incremented
    # `budget_handbacks` tally. Classified ahead of `error` by
    # `_batch_outcome` so the tick exits 0 and the downstream gitops commit
    # that makes the tally durable is never skipped -- the same reason
    # `stale_source` is excluded from `failed`. Without it the cap can never
    # advance and the paid retry loop it bounds stays unbounded (claude P2
    # on `4449024`).
    budget_handback: bool = False
    # True only when the cap's terminal `needs-triage` write actually landed
    # on THIS tick. Bucketed with the hand-back rather than with `failed` for
    # the same reason: that write is what ENDS the loop, and a red tick can
    # cost it the gitops commit. Unlike the hand-back there is no catch-up --
    # every later tick re-enters the same branch and loses the same write --
    # so a red tick here turns an unbounded green retry loop into an
    # unbounded red one at identical model cost (claude P2 on `8465c6e`).
    budget_terminal: bool = False


@dataclass(frozen=True)
class BatchOutcome:
    succeeded: int
    failed: int
    skipped: int
    # Trailing and defaulted so existing positional/keyword constructions
    # (e.g. BatchOutcome(succeeded=1, failed=1, skipped=1)) keep working.
    blocked: int = 0
    # Admission-gate refusals (mctl-agents#410 codex follow-up). Counted
    # separately from `failed` so a batch that also implemented a real
    # proposal on the same tick is not reported red for a refusal that
    # worked exactly as designed -- see the exit-code comment in `main()`.
    stale_source: int = 0
    # Verification-budget outcomes (mctl-agents#430): a hand-back, or the
    # cap's terminal write. Counted separately from `failed` for the same
    # reason as `stale_source` -- each arm's whole purpose is a durable
    # `.status.yaml` write, which a non-zero exit can cost us.
    verification_budget: int = 0


@dataclass(frozen=True)
class ExistingResult:
    """Outcome of the GitHub preflight for one deterministic result branch."""

    action: str
    pr_url: str | None = None
    head_sha: str | None = None
    reason: str | None = None


class GitHubPreflightError(RuntimeError):
    """GitHub could not be queried safely, so the model must not run."""


class ImplementerOperationTimeout(RuntimeError):
    """A bounded model or shell operation exceeded its wall-clock limit."""


class ImplementerFenced(RuntimeError):
    """An ExecutionClaim check answered CLAIM_FENCED right before a push.

    Not a failure of the proposal: the world this attempt's claim was pinned
    to has moved on (a handoff bumped the epoch, or the entity's version
    changed under it), so retrying against the SAME evidence cannot succeed.
    Mapped to EXIT_FENCED so the shepherd does not charge a review attempt for
    a race the executor did not cause (ADR-010 phase 2, #352).
    """


# The subset of `FREE_CLAIM_STATES` that means "nobody is here", as opposed to
# "somebody fenced this". `fenced` is deliberately absent: it is a free state
# for the purpose of acquiring, and the opposite of one for the purpose of
# deciding whether this attempt may still write the proposal's status.
_VACANT_CLAIM_STATES = frozenset({CLAIM_STATE_RELEASED, CLAIM_STATE_EXPIRED})


class ImplementerClaimRefused(RuntimeError):
    """A claim check refused this attempt where that refusal blocks.

    Three different answers arrive here, and the message says which:

    - CLAIM_HELD_BY_OTHER — a real, named executor holds the entity and this
      attempt lost the race;
    - CLAIM_UNKNOWN — the store could not be reached (or no token was
      configured) while `LIFECYCLE_OWNERSHIP_REQUIRED` is on, the default.
      Nobody holds anything; uncertainty is being failed closed. Reporting
      that as a competing holder sent the operator looking for an executor
      that does not exist (claude P2 on `31232dc`);
    - CLAIM_UNCLAIMED where a hold was expected — `claim_verdict_for` answers
      it for `released`, `expired` and `fenced`, so the common shape is this
      attempt's OWN lease running out mid-run — `ClaimClient.renew` runs only
      on the retake path, never as a heartbeat, so a lease that is too short
      for the run holding it is still never extended. Nobody else holds it
      either; naming a rival here
      hides the one thing an operator can act on, which is the lease
      (claude P2 on `d5e2a48`).

    Distinct from `ImplementerFenced`: nothing moved under this attempt.
    Distinct from a generic failure too, because the correct response is to
    stand down silently rather than to mark the proposal `needs-triage` — the
    entity is healthy, and triaging it is how two coordinating processes turn
    into one broken one.

    Only raised where the answer actually composes safety
    (`claim.blocks_mutation` True, i.e. `enforce` and above). Below that the
    claim is advisory and a rejected acquire returns None, exactly as before
    ADR-010 phase 2.

    The verdict is carried as an attribute, not only inside the message: the
    handler in `implement_one` has to tell the three apart to know whether the
    entity is held by someone (leave it alone), free (hand it back for an
    immediate retry) or unknown (fail closed and wait out the lease). Parsing
    that back out of prose would be the bug this class exists to avoid.

    `claim_state` carries the raw state under a CLAIM_UNCLAIMED verdict,
    because that verdict is NOT one situation. `FREE_CLAIM_STATES` folds
    `fenced` in beside `released` and `expired`, and `check` sends this
    attempt's own claim_id — so a claim fenced server-side by a newer executor
    comes back as our own fenced record and reads "free". Handing the proposal
    back on that erases the block the new holder just wrote (claude P2 on
    `af661d7`). Only `released` and `expired` mean nobody is there.
    """

    def __init__(
        self, message: str, *, verdict: str = CLAIM_UNKNOWN, claim_state: str | None = None
    ) -> None:
        super().__init__(message)
        self.verdict = verdict
        self.claim_state = claim_state

    @property
    def entity_is_free(self) -> bool:
        """True only where the answer PROVES nobody holds the entity.

        A missing state is not a proof: `claim_answer_from` leaves `claim`
        None whenever the store answered without a record, and an answer that
        named no claim cannot license a write over someone else's.
        """
        return self.verdict == CLAIM_UNCLAIMED and self.claim_state in _VACANT_CLAIM_STATES


class ImplementerOrphanedSubagent(OrphanedSubagentError):
    """The run ended while the delegated implementer sub-agent was still live.

    A harness failure, not a content failure: the prompt asks the agent to
    delegate to the `implementer` sub-agent, the CLI may launch that child
    asynchronously, and a run that returns before the child settles throws away
    whatever it produced. Mapped to EXIT_ORPHANED_SUBAGENT so the shepherd does
    not charge the proposal a review attempt for our own lost handoff.
    Subclasses the shared error so the helper can raise the generic type while
    the orchestrator keeps its greppable `Implementer*` naming.
    """


# ---------------------------------------------------------------------------
# ExecutionClaim wiring (ADR-010 phase 2, #352).
#
# The implementer's first lifecycle import of any kind: it acquires a claim
# on (devloop-proposal, "{service}/{slug}", implement) beside the existing
# `attempt` lease, and checks a claim immediately before every push this
# module performs. Below `enforce`, every check is advisory (logged, never
# blocking) — see `orchestrator.lifecycle.claim.blocks_mutation`.
# ---------------------------------------------------------------------------

# The 130-minute implement attempt lease, named rather than a bare literal so
# the yaml lease and the claim's `lease_seconds` read the same constant.
IMPLEMENT_ATTEMPT_LEASE = timedelta(minutes=130)


def _claim_lease_seconds(env_var: str, default: timedelta) -> int:
    """The lease for one claim: the computed default, or a LONGER override.

    The override can only lengthen. Every default here is computed from the
    run it has to outlive — 130 minutes matching the yaml `attempt` lease,
    or `_review_claim_lease_default()`'s floor widened by the implementer
    timeouts — and a shorter value expires the claim under its own attempt,
    which comes back from the push-site check as CLAIM_UNCLAIMED and stands a
    live run down. Documenting that hazard is what the previous version did,
    and a comment in `.env.example` does not survive being copied into a
    values file (claude P3 on `b362b5e`, from agy's P2 one round earlier).
    A deliberately shorter lease is not a tunable this module offers, and for
    IMPLEMENT there is no downward route at all: `IMPLEMENT_ATTEMPT_LEASE` is
    the 130 minutes the yaml `attempt` lease is stamped for, a constant no
    timeout feeds. Only REVIEW's default moves, and it moves by shortening the
    run — the implementer timeouts it is derived from (claude P3 on
    `f4d0dec`).
    """
    raw = os.environ.get(env_var, "").strip()
    computed = int(default.total_seconds())
    if not raw:
        return computed
    try:
        asked = int(raw)
    except ValueError:
        return computed
    if asked < computed:
        print(
            f"[lifecycle] {env_var}={asked} is shorter than the computed lease "
            f"{computed}s and would expire under the run it guards; using {computed}s",
            flush=True,
        )
        return computed
    return asked


# One MERGE_POLL_INTERVAL (run_shepherd.py) — a review-remediation claim only
# needs to outlive one shepherd poll, not a full implement run. It is a FLOOR,
# not the whole answer: the lease also has to outlive the run that holds it,
# and nothing renews it mid-run — the one production call of `ClaimClient.renew`
# is the retake in `_acquire_claim`, which runs once, before the run starts. A
# claim that expires mid-run comes back from the push-site check as
# CLAIM_UNCLAIMED and stands the attempt down for no reason at all, so the
# default is sized from the two timeouts that actually bound the run rather
# than pinned to a literal that a raised IMPLEMENTER_TIMEOUT_SECONDS silently
# outgrows (claude P2 on `d5e2a48`).
REVIEW_CLAIM_LEASE_FLOOR = timedelta(minutes=30)


def _review_claim_lease_default() -> timedelta:
    """The review lease floor, widened to cover the run it has to outlive.

    `IMPLEMENTER_TIMEOUT_CEILING_SECONDS` (mctl-agents#423) bounds the WIDEST
    envelope any work class can select -- not `IMPLEMENTER_TIMEOUT_SECONDS`
    alone, which since #423 is only the review-class envelope. A
    CI-remediation or mixed follow-up can run up to the ceiling, and the
    lease has to outlive whichever class this attempt turns out to be before
    the class is even known (the claim is acquired before the bundle's work
    class is derived) -- so it is sized for the worst case unconditionally,
    the same way it was sized for the only case before #423 gave the
    envelope more than one value.
    The git work around the SDK run is bounded by
    `IMPLEMENTER_COMMAND_TIMEOUT_SECONDS` per command, of which the clone
    before and the push after are the two that matter. The margin is
    deliberately the whole of both rather than a fraction, because the two
    errors are not symmetric in the way they look: expiring EARLY refuses a
    live attempt at its own push, and expiring LATE strands the entity for the
    rest of the lease whenever the holder cannot run its own `finally` — a
    crashed or evicted pod does not release, so the wait is the full remaining
    lease and not one shepherd poll (claude P3 on `c29195c`). A wider lease is
    still the right trade: the early error is certain and repeats, the late
    one needs a crash.
    """
    bound = timedelta(
        seconds=IMPLEMENTER_TIMEOUT_CEILING_SECONDS + 2 * IMPLEMENTER_COMMAND_TIMEOUT_SECONDS
    )
    return max(REVIEW_CLAIM_LEASE_FLOOR, bound)


def _resolve_attempt_id(service: str, slug: str, owner_epoch: int, attempt_ordinal: int) -> str:
    """Deterministic attempt identity: WORKFLOW_UID, or a derived fallback.

    Never `uuid.uuid4()` — a random id is non-deterministic in exactly the
    retried-pod case where determinism matters (ADR-010 §8): a pod that
    restarts and re-derives the SAME identity must be able to renew its own
    claim rather than be refused by it. `service` and `slug` are always
    present on a real `ProposalRef`, so this always succeeds; there is no
    reachable "cannot derive an identity" case for this caller.

    `HOSTNAME` is folded into the fallback because determinism and uniqueness
    pull in opposite directions here, and both matter. Without it, two pods
    starting on the same proposal in the same epoch derive the SAME executor
    id, so each one's `acquire` reads as the other RENEWING its own claim:
    the claim answers CLAIM_HELD_BY_ME to both, and the mechanism meant to
    stop concurrent implementers becomes the thing that permits them. In
    Kubernetes `HOSTNAME` is the pod name, so it is stable across a container
    restart within the same pod — the restart case determinism exists for
    (ADR-010 §8) still re-derives its own id and renews its own claim.

    Residual, stated rather than hidden: two processes on ONE host without
    `WORKFLOW_UID` still collide. That is not a shape anything deploys — the
    Temporal path always sets `WORKFLOW_UID` — and the fix for it is to set
    `WORKFLOW_UID`, not to make this id random.
    """
    workflow_uid = os.environ.get("WORKFLOW_UID", "").strip()
    if workflow_uid:
        return workflow_uid
    host = os.environ.get("HOSTNAME", "").strip()
    raw = f"{service}|{slug}|{owner_epoch}|{attempt_ordinal}|{host}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _ClaimContext:
    """Everything one push-site fencing check needs, bundled so a call site
    passes one argument instead of seven that must stay in lock-step."""

    client: ClaimClient
    claim_id: str
    entity: EntityRef
    phase: str
    owner_epoch: int
    entity_version: str
    executor: Executor
    attempt: str


def _acquire_claim(
    entity: EntityRef, phase: str, entity_version: str, attempt: str, *, lease_seconds: int, proposal_ref: str = ""
) -> _ClaimContext | None:
    """Acquire an execution claim: a context, None, or a refusal.

    Three outcomes, because the caller must act differently on each:

    - a `_ClaimContext` — granted, and every downstream push checks it;
    - None — no claim is in play: any answer at a rollout stage below
      `enforce`, where the claim is advisory, and CLAIM_UNKNOWN when the
      `LIFECYCLE_OWNERSHIP_REQUIRED` break-glass is explicitly off. NOT an
      error: the run proceeds on the pre-claim mechanisms, the yaml lease and
      `--force-with-lease`. Note that this is NOT the default path for
      CLAIM_UNKNOWN: `ownership_required()` defaults to True, so at `enforce`
      an unreachable store raises below rather than returning here;
    - `ImplementerFenced` / `ImplementerClaimRefused` — a definite "no" that
      the rollout stage says must stop a mutation. Raised rather than
      returned, because a returned None is indistinguishable from "no claim
      mechanism here" at every call site downstream.
    """
    client = ClaimClient()
    executor = Executor(type=OWNER_IMPLEMENTER, id=attempt)
    answer = client.acquire(
        entity, phase, 0, entity_version, executor, attempt,
        lease_seconds=lease_seconds, proposal_ref=proposal_ref,
    )
    if not answer.may_execute:
        # Three different answers hide behind one falsy `may_execute`, and
        # collapsing them is what made the first remediation of this defect
        # worse than the defect. Every downstream fencing check is guarded by
        # `if claim_context is not None`, so returning None does not DECLINE
        # the attempt — it DISABLES the check. At `enforce` that turned "wastes
        # an SDK call, then correctly blocks the push" into "wastes an SDK
        # call, then pushes on top of the holder".
        #
        # So the answer is split the same way `_check_claim_or_raise` splits
        # it, and by the same predicate, so the two cannot drift:
        # A fence is the sharpest answer the store gives, but `observe` still
        # RECORDS writes (`rollout.records_writes()` is True from observe up),
        # so a real 409 `fenced` does come back there — and halting the
        # attempt on it would be the observe stage composing safety, which is
        # exactly what it must not do (ADR-010 §12; agy P2 on `31232dc`). An
        # earlier revision raised unconditionally on the theory that a fence
        # was unreachable below enforce; that is true of `off` alone. Gated
        # here and in `_check_claim_or_raise` by the same predicate, so the
        # two sites still cannot answer one verdict two different ways.
        if answer.verdict == CLAIM_FENCED and rollout.new_answer_may_veto():
            raise ImplementerFenced(
                f"{FENCED_ERROR_PREFIX} claim for {entity.kind}:{entity.id}/{phase} "
                f"fenced at acquire: {answer.reason}"
            )
        if blocks_mutation(answer):
            # Two answers block here, and they are NOT the same event. A
            # CLAIM_UNKNOWN reaches this branch at `enforce` by default —
            # `ownership_required()` reads LIFECYCLE_OWNERSHIP_REQUIRED with a
            # default of true — so an expired token or a 503 from mctl-api
            # lands on a message that must not assert a competing executor
            # nobody can find (claude P2 on `31232dc`).
            if answer.verdict == CLAIM_UNKNOWN:
                raise ImplementerClaimRefused(
                    f"{CLAIM_REFUSED_ERROR_PREFIX} the claim store could not answer for "
                    f"{entity.kind}:{entity.id}/{phase}: {answer.reason or answer.verdict}. "
                    f"Nobody is known to hold it; uncertainty is failed closed because "
                    f"LIFECYCLE_OWNERSHIP_REQUIRED is on (set it to false to proceed anyway)",
                    verdict=CLAIM_UNKNOWN,
                )
            if answer.verdict == CLAIM_UNCLAIMED:
                # Naming the free state rather than a rival: this attempt never
                # held anything here, so the refusal is the store's own
                # (claude P3 on `c29195c`). The SHA stays in this comment and
                # out of the message — the message reaches the shepherd's log
                # and the batch summary, where a commit id from this branch is
                # noise to whoever is reading an incident (claude P3 on
                # `af661d7`).
                raise ImplementerClaimRefused(
                    f"{CLAIM_REFUSED_ERROR_PREFIX} the acquire for "
                    f"{entity.kind}:{entity.id}/{phase} was refused although the claim reads "
                    f"free (released, expired or fenced): {answer.reason or answer.verdict}. "
                    f"Nobody holds it; this attempt never held it either, so the refusal is "
                    f"the store's, not a lost race",
                    verdict=CLAIM_UNCLAIMED,
                    claim_state=answer.claim.state if answer.claim else None,
                )
            raise ImplementerClaimRefused(
                f"{CLAIM_REFUSED_ERROR_PREFIX} another executor holds the claim for "
                f"{entity.kind}:{entity.id}/{phase}: {answer.reason or answer.verdict}",
                verdict=CLAIM_HELD_BY_OTHER,
            )
        # Everything left is an answer at a rollout stage where the claim is
        # advisory by design — below `enforce`, or a CLAIM_UNKNOWN with the
        # ownership break-glass explicitly off. Decline the claim — never
        # adopt a claim_id that may name the winning executor's claim rather
        # than ours — and proceed exactly as this module did before claims
        # existed.
        return None
    claim_id = answer.claim.claim_id if answer.claim else ""
    if answer.retaken:
        # The one granted answer the store actually REFUSED: a 409 whose record
        # names this attempt, i.e. the orphan claim a killed predecessor of this
        # same (deterministic) identity never released. A refused acquire never
        # applied `lease_seconds`, so what is adopted here is the DEAD pod's
        # remaining `lease_until`, while `implement_one` stamps a fresh
        # full-length yaml lease two statements later. On the granted path the
        # two expire together by construction; this is the one path that
        # desynchronises them, in the direction `_review_claim_lease_default()`
        # exists to prevent — the claim expiring before the run it guards. Left
        # alone, an expiry mid-run answers CLAIM_UNCLAIMED to the shepherd's
        # freshness check, which matches none of its arms and falls through to
        # "not held" with the unexpired yaml lease never read: a second
        # implementer against a live one (claude P2 on `6794aad`).
        #
        # So the retake is completed rather than assumed, with the renew ADR-010
        # §8 and `_resolve_attempt_id`'s docstring both already name as the
        # point of the deterministic identity. A refused renew is the refusal
        # the 409 originally was, answered by the same rollout predicate as
        # every other refusal here so the stages cannot drift.
        # `claim_id` is non-empty here by construction, and deliberately not
        # re-checked: `retaken` is set only where `claim_verdict_for` read a
        # record `ExecutionClaim.from_payload` accepted, and that parse requires
        # a claim id. The guard this replaced could not run, and being the one
        # refusal in this function the rollout predicate did not gate, it said
        # the opposite of the paragraph above it (claude P3 on `0af3b38`).
        renewed = client.renew(
            claim_id, entity, phase, 0, entity_version, executor, attempt,
            lease_seconds=lease_seconds,
        )
        if not renewed.may_execute:
            if renewed.verdict == CLAIM_FENCED and rollout.new_answer_may_veto():
                raise ImplementerFenced(
                    f"{FENCED_ERROR_PREFIX} claim for {entity.kind}:{entity.id}/{phase} "
                    f"fenced while renewing this attempt's own claim: {renewed.reason}"
                )
            if blocks_mutation(renewed):
                raise ImplementerClaimRefused(
                    f"{CLAIM_REFUSED_ERROR_PREFIX} the claim for "
                    f"{entity.kind}:{entity.id}/{phase} names this attempt but its lease "
                    f"could not be extended: {renewed.reason or renewed.verdict}. The "
                    f"adopted lease is the previous pod's remainder, so continuing would "
                    f"run past a claim nobody is holding",
                    verdict=renewed.verdict,
                    claim_state=renewed.claim.state if renewed.claim else None,
                )
            # Advisory stage: decline the claim rather than carry one whose
            # lease this run cannot vouch for, exactly as the branch above does.
            return None
        if renewed.claim is not None and renewed.claim.claim_id:
            claim_id = renewed.claim.claim_id
    return _ClaimContext(
        client=client, claim_id=claim_id, entity=entity, phase=phase,
        owner_epoch=0, entity_version=entity_version, executor=executor, attempt=attempt,
    )


def _check_claim_or_raise(ctx: _ClaimContext, *, entity_version: str | None = None) -> None:
    """The early filter immediately before a mutation.

    NOT the authoritative check — that is the target system's own
    precondition (`--force-with-lease`, `--match-head-commit`). This exists so
    a fence is detected (and the attempt classified non-charging) before
    spending a git round trip on a push that would fail anyway.
    """
    version = ctx.entity_version if entity_version is None else entity_version
    answer = ctx.client.check(
        ctx.claim_id, ctx.entity, ctx.phase, ctx.owner_epoch, version, ctx.executor, ctx.attempt
    )
    if answer.verdict == CLAIM_FENCED and not rollout.new_answer_may_veto():
        # Below `enforce` the claim is advisory: observe records writes, so a
        # fence genuinely arrives here, and stopping the push on it would make
        # the observe stage decide. Logged instead — the divergence is the
        # whole product of that stage (agy P2 on `31232dc`).
        print(
            f"lifecycle: claim for {ctx.entity.kind}:{ctx.entity.id}/{ctx.phase} answered "
            f"fenced before push ({answer.reason}); advisory below enforce, pushing anyway"
        )
    elif answer.verdict == CLAIM_FENCED:
        raise ImplementerFenced(
            f"{FENCED_ERROR_PREFIX} claim for {ctx.entity.kind}:{ctx.entity.id}/{ctx.phase} "
            f"fenced before push: {answer.reason}"
        )
    if blocks_mutation(answer):
        # Not a fence: either the store could not answer while the ownership
        # break-glass is on, or someone else genuinely holds it. Same two
        # answers, same split and the same exception as `_acquire_claim`, so
        # one verdict cannot be reported two different ways depending on which
        # site observed it — and EXIT_CLAIM_REFUSED so the shepherd names it
        # instead of printing "subprocess failed transiently". Still
        # non-charging: this attempt did nothing wrong.
        if answer.verdict == CLAIM_UNKNOWN:
            raise ImplementerClaimRefused(
                f"{CLAIM_REFUSED_ERROR_PREFIX} the claim store could not answer for "
                f"{ctx.entity.kind}:{ctx.entity.id}/{ctx.phase} before the push: "
                f"{answer.reason or answer.verdict}. Nobody is known to hold it; "
                f"uncertainty is failed closed because LIFECYCLE_OWNERSHIP_REQUIRED is on",
                verdict=CLAIM_UNKNOWN,
            )
        if answer.verdict == CLAIM_UNCLAIMED:
            raise ImplementerClaimRefused(
                f"{CLAIM_REFUSED_ERROR_PREFIX} the claim this attempt held on "
                f"{ctx.entity.kind}:{ctx.entity.id}/{ctx.phase} is gone by the time of the "
                f"push: {answer.reason or answer.verdict}. Nobody else holds it — either this "
                f"attempt's own lease ran out (nothing renews it mid-run) or the store fenced "
                f"the claim and reports the free state that leaves behind",
                verdict=CLAIM_UNCLAIMED,
                claim_state=answer.claim.state if answer.claim else None,
            )
        raise ImplementerClaimRefused(
            f"{CLAIM_REFUSED_ERROR_PREFIX} another executor holds the claim for "
            f"{ctx.entity.kind}:{ctx.entity.id}/{ctx.phase}: {answer.reason or answer.verdict}",
            verdict=CLAIM_HELD_BY_OTHER,
        )


_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$")


def _parse_pr_url(url: str) -> tuple[str, int] | None:
    """Split a GitHub PR URL into (repo, number), or None if it does not match."""
    m = _PR_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _release_claim(ctx: _ClaimContext | None, *, reason: str) -> None:
    """Best-effort release. Must never fail the run it is cleaning up after."""
    if ctx is None:
        return
    try:
        ctx.client.release(
            ctx.claim_id, ctx.entity, ctx.phase, ctx.owner_epoch, ctx.executor, ctx.attempt, reason=reason[:200]
        )
    except Exception as exc:  # noqa: BLE001 — releasing a claim must never fail the run
        print(f"warn: could not release claim {ctx.claim_id!r}: {exc}")


# ---------------------------------------------------------------------------
# .status.yaml IO
# ---------------------------------------------------------------------------
def _load_status(path: Path) -> dict:
    """Parse .status.yaml. Missing file → {}; default status is `proposed`."""
    return load_status(path)


def _now_iso() -> str:
    """RFC 3339 UTC timestamp without microseconds."""
    return now_iso()


def update_status_yaml(
    ref: ProposalRef,
    new_status: str,
    actor: str = "mctl-agents[bot]",
    **fields: Any,
) -> None:
    """Write .status.yaml back to the gitops worktree.

    Every existing field is preserved unless the caller explicitly overrides
    it. Passing ``None`` removes a field. This prevents an `in-progress`
    transition or a failed retry from erasing an already-known PR URL.

    The file is keep-it-simple YAML — no comments preserved (we don't need
    ruamel for this). A trailing newline is added so `git diff` shows the
    last-line change cleanly.
    """
    update_status_file(ref.status_path, new_status, actor=actor, **fields)
    ref.status = new_status


def _status_attempt_id(ref: ProposalRef) -> str | None:
    """The attempt id `.status.yaml` currently names, or None."""
    held = _load_status(ref.status_path).get("attempt")
    return held.get("id") if isinstance(held, dict) else None


def _status_is_still_ours(ref: ProposalRef, attempt_id: str, *, doing: str) -> bool:
    """Whether this attempt may still write the proposal's status.

    THE invariant behind every status write an ending attempt makes, and it
    is deliberately one function rather than a guard per arm. Between this
    attempt's own `in-progress` write and the moment it finds out it lost the
    entity, a second executor can legitimately have taken the proposal and
    stamped its own `attempt` block. Any write that carries OUR attempt block
    — a hand-back to `accepted`, a `needs-triage` failure, anything — erases
    theirs, and `_attempt_is_fresh` (which reads only the yaml on that branch)
    then answers "not held" and lets a THIRD implementer start. Raised first
    against the `CLAIM_UNCLAIMED` hand-back (claude P2 on `af661d7`) and then
    against the 409 fence reaching `_mark_needs_triage`, which is ADR-010 §6's
    primary fence encoding (claude P2 on `0808376`); both are the same bug, so
    both now consult the same predicate.
    """
    held_id = _status_attempt_id(ref)
    if held_id == attempt_id:
        return True
    print(
        f"lifecycle: {ref.service}/{ref.slug}: not {doing} — `.status.yaml` names "
        f"attempt {held_id or '<none>'}, not this one ({attempt_id}); leaving the "
        f"current holder's status alone"
    )
    return False


def _hand_back_if_still_ours(
    ref: ProposalRef, attempt_id: str, *, budget_handbacks: int | None = None
) -> bool:
    """Restore `accepted` only while `.status.yaml` still names our attempt.

    Returns True when the hand-back was written. ``budget_handbacks``
    (mctl-agents#430) records how many times THIS proposal has been handed
    back for an exhausted verification budget, so the retry it enables stays
    bounded — see `IMPLEMENT_MAX_BUDGET_HANDBACKS`.
    """
    if not _status_is_still_ours(ref, attempt_id, doing="handing the proposal back"):
        return False
    fields: dict[str, Any] = {"attempt": None, "failure": None}
    if budget_handbacks is not None:
        fields["budget_handbacks"] = budget_handbacks
    update_status_yaml(ref, "accepted", **fields)
    return True


# The consecutive budget-exhausted attempt at which an implement run stops
# being handed back and becomes terminal. Read it exactly as
# `run_shepherd.MAX_HARNESS_FAILURES`, which it mirrors down to the
# comparison (`new >= MAX`): the Nth occurrence is the one that stops the
# loop, so N-1 hand-backs actually happen. Deliberately the same shape rather
# than the more obvious "N hand-backs allowed" -- two sibling caps that read
# alike but count differently is a worse trap than one slightly terse rule
# (agy P2 on `61595a0` read it the other way, which is the evidence that the
# wording, not the comparison, was what needed fixing).
#
# A cap is needed at all because the implement driver has no
# `review_attempts`/`harness_failures` budget of its own -- the sibling
# `except ImplementerOrphanedSubagent` arm stays terminal for exactly that
# reason -- so an unconditional hand-back would trade a wrong terminal state
# for an unbounded PAID retry loop (claude P2 on `624a433`).
IMPLEMENT_MAX_BUDGET_HANDBACKS = 3


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_accepted_proposals(
    state_dir: Path,
    service_filter: str | None = None,
    slug_filter: str | None = None,
    statuses: set[str] | None = None,
) -> list[ProposalRef]:
    """Glob agents-state and return all proposals with status == accepted.

    Filters are applied AFTER status filter — they narrow, never broaden.
    A directory without .status.yaml is treated as `proposed` (default per
    shared contract) and is therefore skipped here (Tier 2 only acts on
    `accepted`).

    When ``statuses`` is set it overrides the default filter entirely. The
    ``--review-feedback`` path uses this to look up proposals in
    ``{implemented, review-fixing}`` status, since the follow-up does NOT
    require the proposal to be in ``accepted`` (it is post-implementation).
    """
    if not state_dir.is_dir():
        raise SystemExit(f"State dir not found: {state_dir}")

    if statuses is not None:
        accepted_states = set(statuses)
    else:
        accepted_states = {"accepted"}

    refs: list[ProposalRef] = []
    for service_dir in sorted(state_dir.iterdir()):
        if not service_dir.is_dir() or service_dir.name.startswith("_"):
            continue  # skip _mentor/
        service = service_dir.name
        if service_filter and service != service_filter:
            continue

        proposals_dir = service_dir / "proposals"
        if not proposals_dir.is_dir():
            continue
        for proposal_dir in sorted(proposals_dir.iterdir()):
            if not proposal_dir.is_dir():
                continue
            slug = proposal_dir.name
            if slug_filter and slug != slug_filter:
                continue
            try:
                data = _load_status(proposal_dir / ".status.yaml")
            except Exception as e:  # noqa: BLE001 — skip one bad status file, keep scanning the rest
                print(f"warn: {service}/{slug}: failed to parse .status.yaml ({e}); skipping")
                continue
            status = data.get("status", "proposed")
            if not isinstance(status, str):
                # `status in accepted_states` raises TypeError on an
                # unhashable value, and this is outside the try above that
                # exists to skip one bad file -- so a single hand-edited
                # `status: [accepted]` anywhere under agents-state took the
                # whole scan down and no proposal ran (agy P2). Predates this
                # PR; fixed here because the same edit reaches the gate below.
                print(f"warn: {service}/{slug}: status is "
                      f"{type(status).__name__}, not a string; skipping")
                continue
            if status in accepted_states:
                refs.append(
                    ProposalRef(
                        service=service,
                        slug=slug,
                        proposal_dir=proposal_dir,
                        status=status,
                        approval_ok=human_approval_satisfied(data),
                    )
                )
    return refs


def build_adopted_ref(state_dir: Path, pr_url: str) -> ProposalRef:
    """Build a ``ProposalRef`` for a proposal-less adopted PR
    (mctlhq/mctl-agents#334): ``--adopted-pr <url>`` drives an EXISTING
    adoption record, never creates one — only the shepherd's own discovery
    pass (``orchestrator.pr_adoption.discover_adoptable``) adopts. A missing
    record is a hard exit; there is no fallback that invents one here.

    Returns an ordinary ``ProposalRef`` (this module's class, not a
    ``pr_adoption.PRRef`` — that class lives in a module this one must not
    import at module level, since ``pr_adoption`` imports ``run_shepherd``,
    not this file, and duck-typing across the two ``ProposalRef`` shapes is
    the established pattern here, see ``run_shepherd.reconcile_one``'s
    cross-module call into this module). ``proposal_dir`` is the record's
    own directory and ``status_path`` is overridden to point at
    ``.prref.yaml`` instead of the base class's ``.status.yaml``.
    """
    parsed = _parse_pr_url(pr_url)
    if parsed is None:
        print(f"--adopted-pr is not a GitHub PR URL: {pr_url!r}", file=sys.stderr)
        sys.exit(2)
    repo, number = parsed
    service = repo.split("/")[-1]
    proposal_dir = state_dir / service / "adopted-prs" / f"pr-{number}"
    status_path = proposal_dir / ".prref.yaml"
    if not status_path.exists():
        print(
            f"No adoption record found at {status_path}. The implementer "
            "never adopts a PR itself — only the shepherd's discovery pass "
            "does (mctlhq/mctl-agents#334).",
            file=sys.stderr,
        )
        sys.exit(2)
    data = load_status(status_path)
    ref = ProposalRef(
        service=service,
        slug=f"pr-{number}",
        proposal_dir=proposal_dir,
        status=str(data.get("status", "adopted")),
    )
    ref.status_path = status_path
    return ref


def _adopted_pr_is_fork(repo: str, number: int) -> bool:
    """Second, independent fork check (mctlhq/mctl-agents#334) — defence in
    depth. The shepherd's own discovery pass already refuses a fork PR; this
    repeats the check inside the process that will actually push, and
    immediately before the clone, so the one mutation this feature performs
    is never gated on the scheduler alone having gotten it right.
    """
    data = _github_json(["gh", "api", f"repos/{repo}/pulls/{number}"])
    if not isinstance(data, dict):
        raise GitHubPreflightError(f"unexpected response shape for {repo}#{number}")
    head = data.get("head") or {}
    head_repo = head.get("repo") or {}
    if bool(head_repo.get("fork")):
        return True
    base_repo = (data.get("base") or {}).get("repo") or {}
    head_owner = (head_repo.get("owner") or {}).get("login") or ""
    base_owner = (base_repo.get("owner") or {}).get("login") or ""
    return bool(head_owner) and head_owner != base_owner


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------
def _run(
    cmd: list[str],
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run a bounded subprocess with consistent logging."""
    effective_timeout = (
        IMPLEMENTER_COMMAND_TIMEOUT_SECONDS if timeout is None else timeout
    )
    refresh_github_token()
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    try:
        # run_capturing, not subprocess.run: on check=True a plain
        # CalledProcessError reaches Temporal as "returned non-zero exit
        # status 1" with the captured stderr stranded on the exception.
        #
        # command_span: a github.* / git.* span for GitHub reads, mutations,
        # commits and pushes (mctl-agents#195) — operation and target only,
        # never argv. Any other command gets no span.
        with tracing.command_span(cmd) as traced:
            result = run_capturing(cmd, cwd=cwd, check=check, timeout=effective_timeout)
            traced.exited(getattr(result, "returncode", None))
            return result
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired carries whatever the command printed before it hung.
        # Dropping it leaves an operator with "command exceeded 600s" and no
        # way to tell a wedged network call from an interactive prompt.
        raise ImplementerOperationTimeout(
            f"command exceeded {effective_timeout:g}s: {' '.join(cmd)} "
            f"{describe_output(exc.stdout, exc.stderr)}"
        ) from exc


def _github_json(cmd: list[str]) -> Any:
    """Run a GitHub CLI command and parse JSON, failing closed on any error."""
    proc = _run(cmd, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown GitHub CLI error").strip()
        raise GitHubPreflightError(detail)
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise GitHubPreflightError(f"invalid JSON from {' '.join(cmd[:3])}") from exc


def _gh_api_json(args: list[str]) -> Any:
    """`read_source_issue`'s `gh_api_json` adapter, routed through the
    bounded `_github_json` (mctl-agents#410 code review).

    `read_source_issue`'s own default reader calls `run_capturing` with
    `timeout=None` -- unbounded -- and skips `_run`'s token refresh and
    `$ gh api ...` log line. Passing this adapter instead keeps the
    admission gate's GitHub read on the same bounded, logged path as every
    other `gh` call in this module, `_superseding_pr_urls` included.
    """
    return _github_json(["gh", "api", *args])


def _clone_target(service: str, slug: str) -> Path:
    """gh repo clone the target sibling repo to a fresh tmp dir."""
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    target = Path(tempfile.gettempdir()) / f"impl-{service}-{slug}-{ts}"
    if target.exists():
        shutil.rmtree(target)
    # gh CLI honors GITHUB_TOKEN automatically.
    _run(["gh", "repo", "clone", f"mctlhq/{service}", str(target), "--", "--depth=10"])
    # Identify as the bot for commits made by the implementer agent (it runs
    # `git commit` itself — see implementer.md).
    _run(["git", "config", "user.name", "mctl-agents[bot]"], cwd=target)
    _run(["git", "config", "user.email", "mctl-agents[bot]@users.noreply.github.com"], cwd=target)
    return target


def _stage_implementer_agent(target: Path, service: str) -> None:
    """Copy the per-service implementer.md sub-agent into the cloned repo's
    .claude/agents/ so the SDK (cwd=target, setting_sources=project) sees it.

    The file is runtime scaffolding — it must NEVER end up in the PR. We
    register it in `.git/info/exclude` (a per-clone, untracked-by-design
    ignore list) so even a broad `git add -A` from the sub-agent leaves
    it untouched. We deliberately avoid editing the repo's `.gitignore`
    because that would itself be an out-of-scope change.

    The refusal marker (mctl-agents#360) is excluded for the same reason and
    one more: `_read_refusal_marker` honours it only while it is UNTRACKED, so
    a single `git add -A` that swept it into a follow-up commit would make
    every later refusal on that branch fall back to exit 42 and charge an
    attempt — the fix silently and permanently reverted for exactly the PR
    that needed it. The exclude entry is the defence; the untracked check is
    the backstop for the case where something committed it anyway.
    """
    src = AGENTS_DIR / service / ".claude" / "agents" / "implementer.md"
    if not src.exists():
        # Issue-driven proposals can target any mctlhq repo, including ones
        # without an agents/<svc>/ scaffold (the scaffold only exists for the
        # proactive rotation services). Fall back to the generic implementer
        # sub-agent so Tier 2 still works for those repos.
        generic = AGENTS_DIR / "_generic" / ".claude" / "agents" / "implementer.md"
        if generic.exists():
            print(f"info: no per-service implementer for '{service}'; using generic fallback.")
            src = generic
        else:
            raise SystemExit(
                f"Implementer agent template not found: {src} "
                f"(and no generic fallback at {generic})"
            )
    dst_dir = target / ".claude" / "agents"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / "implementer.md")

    # Local-only ignore: keeps PRs scoped to the proposal's intent, and keeps
    # the refusal marker honourable (see the docstring — a tracked marker is
    # ignored forever after).
    exclude_path = target / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    entries = (".claude/agents/implementer.md", REFUSAL_MARKER_FILENAME)
    existing = ""
    if exclude_path.exists():
        existing = exclude_path.read_text(encoding="utf-8")
    missing = [e for e in entries if e not in existing.splitlines()]
    if missing:
        with exclude_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for entry in missing:
                f.write(f"{entry}\n")


def _resolve_implementer_service_skills(target: Path, branch: str) -> ServiceSkillBundle:
    """mctlhq/mctl-agents#305 (R24): resolve the implementer's
    `ServiceSkillBundle` even though the implementer is still
    `agents.mctl.ai/v1alpha1` and has no `ExecutionPlan` — its envelope
    comes straight from `AgentManifest` (`tool_allow`, and
    `service_skills` for enablement/ceilings).

    `branch` is `feat/agents-<slug>` for a proposal run, or the adopted
    PR's own head branch (mctlhq/mctl-agents#334); either way
    `service_skills.pin_sha` resolves the merge-base with the default
    branch (R6) rather than HEAD — on a brand-new branch that is the same
    commit HEAD already is, and on a review-feedback or adopted branch it
    is NOT whatever a previous implementer run committed on top of it.

    Raises `ServiceSkillError` — the caller aborts before
    `_run_implementer_agent`, so before any commit, push, or PR (R22).

    Checks `service_skills.is_enabled` BEFORE calling `pin_sha` — a
    disabled policy (the default: no `spec.serviceSkills` block, or
    `MCTL_SERVICE_SKILLS=off`) must not run `git merge-base` at all (R17),
    not just skip the eventual `git ls-tree`.
    """
    implementer_manifest = load_agent_manifest(MANIFESTS_DIR / "implementer" / "agent.yaml")
    policy = implementer_manifest.service_skills
    pinned_sha = (
        service_skill_pin_sha(target, agent="implementer", branch=branch)
        if is_service_skills_enabled(policy)
        else ""
    )
    bundle = resolve_service_skill_bundle(
        agent="implementer",
        repo_dir=target,
        policy=policy,
        tool_allow=implementer_manifest.tool_allow,
        pinned_sha=pinned_sha,
    )
    if bundle.skills:
        print(
            f"[service_skills] implementer bundle: {len(bundle.skills)} skill(s) from "
            f"{bundle.resolved_from_sha[:8]}"
        )
    return bundle


def _adopted_pr_number(ref: ProposalRef) -> int | None:
    """The PR number for an adopted ref, recovered from `slug = "pr-<n>"`
    (mctlhq/mctl-agents#334). Kept a pure function of `ref.slug` rather than
    reading `.prref.yaml` here, so `_build_prompt` stays a pure formatter
    with no I/O of its own — same contract it already has.
    """
    if ref.slug.startswith("pr-"):
        try:
            return int(ref.slug[len("pr-"):])
        except ValueError:
            return None
    return None


def _build_prompt(
    ref: ProposalRef,
    review_feedback: dict | None = None,
    branch: str | None = None,
    adopted: bool = False,
    service_skills_block: str = "",
) -> str:
    """Prompt that delegates to the `implementer` sub-agent.

    The sub-agent is told (in its frontmatter and body) to read the spec
    files via $PROPOSAL_DIR, edit minimal files in cwd, run a brief sanity
    check, and `git commit` — but NOT push.

    When ``review_feedback`` is set the prompt is the follow-up variant:
    the agent is told the branch is already checked out, points to the
    existing PR, and addresses each codex finding from the bundle.

    ``branch`` overrides the deterministic `feat/agents-<slug>` branch name
    — the adopted-PR path (mctlhq/mctl-agents#334) passes the PR's own head
    branch, read from `.prref.yaml`, never a model-supplied value.
    ``adopted`` drops the proposal-spec sentence and commit trailer in
    favour of a plain PR reference, since an adoption record carries no
    requirements/design/tasks triplet to read.
    ``service_skills_block`` is ``""`` unless a non-empty
    ``ServiceSkillBundle`` was resolved for this run (mctlhq/mctl-agents#305)
    -- an empty string changes this function's output by zero bytes.
    """
    branch = branch or f"feat/agents-{ref.slug}"
    skills_section = f"\n{service_skills_block}\n" if service_skills_block else ""

    if review_feedback is not None:
        # Review-comment bodies and CI log excerpts are attacker-writable
        # (anyone who can comment on the PR / influence a build log). With a
        # <service_skills> authority block spliced into this same prompt,
        # that text must not be able to forge the block's delimiter tags --
        # same neutralizer the skill bodies themselves go through (R18).
        feedback_md = neutralize_service_skill_tags(_render_review_feedback(review_feedback))
        ci_only = _bundle_is_ci_only(review_feedback)
        # What the run is actually about. Every one of these was hardcoded to
        # the code-review framing; a CI-only bundle then read as a
        # self-contradiction (mctl-agents#411 review).
        work_items = "failing required checks" if ci_only else "codex findings"
        if ci_only:
            trigger_line = (
                "- A required CI check is FAILING on this PR. The code review "
                "is clean — there are no P1/P2 review findings to address. The "
                "failing-check evidence is below."
            )
            read_line = (
                "Read the failing-check evidence (below) and the relevant "
                "lines in the working tree."
            )
            apply_line = (
                "Apply the MINIMAL change that makes each failing check pass. "
                "Stay in scope —\n   do not refactor outside the touched files."
            )
            refusal_line = (
                "5. If a failing check is not something a code change can fix "
                "(an infrastructure\n   outage, a flake), is already fixed in "
                "the working tree, or must NOT be acted\n   on because of an "
                "explicit operator decision recorded on the PR, do not commit."
            )
        else:
            trigger_line = (
                "- Code review left P1/P2 findings on this PR — they are listed below."
            )
            read_line = (
                "Read the codex findings (below) and\n   the relevant lines in "
                "the working tree."
            )
            apply_line = (
                "Apply the MINIMAL change that resolves each finding. Stay in scope —\n"
                "   do not refactor outside the touched files."
            )
            refusal_line = (
                "5. If a finding is invalid, is already addressed, or must NOT "
                "be acted on\n   because of an explicit operator decision "
                "recorded on the PR, do not commit."
            )
        if adopted:
            number = _adopted_pr_number(ref)
            pr_url = f"https://github.com/mctlhq/{ref.service}/pull/{number}"
            subject = (
                f"fix(ci): fix failing required checks on mctlhq/{ref.service}#{number}"
                if ci_only
                else f"fix(review): address P1/P2 findings on mctlhq/{ref.service}#{number}"
            )
            context_spec_line = (
                "- `$PROPOSAL_DIR` (env var) holds an adoption record "
                "(`.prref.yaml`), NOT a requirements/design/tasks triplet — "
                "this PR has no proposal. Ground yourself in the evidence "
                "below and the diff on this branch."
            )
            commit_body_line = f"Body should reference the PR: `PR: {pr_url}`."
        else:
            context_spec_line = (
                "- Spec files live at `$PROPOSAL_DIR` (env var): "
                "requirements.md, design.md, tasks.md."
            )
            subject = (
                f"fix(ci): fix failing required checks on {ref.slug}"
                if ci_only
                else f"fix(agents): address P1/P2 codex findings on {ref.slug}"
            )
            commit_body_line = (
                "Body should reference the proposal: "
                f"`Proposal: platform-gitops/agents-state/{ref.service}/proposals/{ref.slug}/`."
            )
        # mctl-agents#423: a bundle carrying CI failures already has the
        # log evidence it will get — retrieved, bounded and paid for OUTSIDE
        # this run's own execution envelope. Nothing else can stop the agent
        # from fetching more itself (the CLI backgrounds a slow Bash command
        # past its own tool timeout rather than failing it), so the prompt
        # states the rule and the `_ci_log_guard_hook()` PreToolUse hook
        # (see options.py) is the actual enforcement.
        has_ci_failures = bool(review_feedback.get("ci_failures"))
        ci_log_ground_rule = (
            "- The log excerpt(s) under \"Log excerpt (bounded, ...)\" above are "
            "ALL the CI-log evidence you will get for this run — already "
            "retrieved and bounded before this run started. Do NOT run `gh run "
            "view --log`/`--log-failed`, `gh api .../logs`, or `curl`/`wget` a "
            "logs URL to fetch more; those commands are blocked. If the bounded "
            "excerpt is genuinely insufficient to decide on a code change, do "
            "not retry the fetch: write the refusal marker with "
            "`\"insufficient_evidence\": true` (see below) instead.\n"
            if has_ci_failures
            else ""
        )
        insufficient_evidence_note = (
            "\n   When the reason is specifically that the bounded CI-log "
            "evidence above cannot support a code decision (not merely that "
            "the check is fine or infrastructure-flaky), add "
            "`\"insufficient_evidence\": true` to the same marker:\n\n"
            "   {\"refused\": true, \"insufficient_evidence\": true, "
            "\"reason\": \"<what evidence is missing>\"}\n"
            if has_ci_failures
            else ""
        )
        return f"""\
Tier 2 implementer follow-up for proposal `{ref.service}/{ref.slug}`.

Context:
- Branch `{branch}` is already checked out on the existing PR.
{trigger_line}
{context_spec_line}

Workflow:
1. Use the `implementer` sub-agent. {read_line}
2. {apply_line}
3. Stage and commit on the SAME branch (`{branch}`). Conventional Commits
   subject: `{subject}`.
   {commit_body_line}
4. DO NOT push and DO NOT open a PR — the orchestrator will push to the
   existing branch after you finish. The PR auto-updates because the
   head ref does not change.
{refusal_line}
   Instead write the refusal marker file
   `{REFUSAL_MARKER_FILENAME}` in the root of the current working
   directory, with exactly this shape — one line, valid JSON:

   {{"refused": true, "reason": "<what you declined, and why>"}}

   In `reason`, give the evidence: quote the operator note, the code
   that already satisfies it, or the log line showing the failure is an
   infrastructure outage rather than a defect. Explain the same reasoning
   in your final message.
{insufficient_evidence_note}
   Write this file ONLY when you deliberately decided that changing nothing
   is the correct outcome. Never write it next to a commit, never as a
   progress note, and never with an empty or placeholder reason: the
   orchestrator reads it as your statement that this run was a considered
   no-op, and uses it to avoid spending one of this PR's bounded fix
   attempts on you. Do not commit the marker file itself — a committed
   marker is ignored.

{feedback_md}
{skills_section}
Ground rules:
- One commit per run is fine; multiple small commits are also fine.
- Stay strictly within scope — fixing the {work_items} only.
- Work ONLY inside the current working directory (the cloned target repo).
  NEVER create, edit, commit, or push files anywhere else — in particular
  the mounted gitops worktree under `/workdir`. If a finding implies a
  change in another repository, do NOT make it; describe it in your final
  message so a human can route it.
{ci_log_ground_rule}- Never defer work to "the background." This run is a single, one-shot
  turn — there is no later turn for you to resume into, no polling loop,
  and nothing will notify you when a backgrounded command finishes. Run
  every command synchronously and wait for its result before ending your
  turn. A slow build or test is fine — wait for it inline. Do NOT end your
  turn saying you will "keep working" or "report back once it's done":
  ending the turn ends the run, and anything not committed by then is lost.
- Every command you run is bounded automatically to what remains of this
  run's execution budget — you do not choose the bound and cannot widen it.
  Backgrounding (a trailing `&`), `nohup`, `setsid`, `disown`, and starting a
  background process to poll it in a loop are BLOCKED outright, not merely
  discouraged: the tool call is denied. If the remaining budget cannot fit
  another command, you will be told so; at that point, commit what is
  already proven correct and say so in your final message, or — if nothing
  is safe to commit — write the refusal marker with
  `{{"refused": true, "verification_budget_exhausted": true, "reason":
  "<what you could not verify>"}}`.
- No emoji in code or commit messages.
- English only.
"""

    return f"""\
Tier 2 implementer run for proposal `{ref.service}/{ref.slug}`.

Workflow:
1. Use the `implementer` sub-agent. Read spec files from `$PROPOSAL_DIR`
   (env var, set by the orchestrator): requirements.md, design.md, tasks.md.
   For mctl-docs proposals also read proposed-content.md if present.
2. Implement the MINIMAL change in the current working directory (a clean
   clone of mctlhq/{ref.service}). Follow that repo's CLAUDE.md conventions.
3. Stage and commit your changes on branch `{branch}` (already checked out)
   with a Conventional Commits subject like
   `feat(agents): {ref.slug}` (or fix:/chore: as appropriate). Body should
   reference the proposal: `Proposal: platform-gitops/agents-state/{ref.service}/proposals/{ref.slug}/`.
4. DO NOT push and DO NOT open a PR — the orchestrator will do that after
   you finish. Just commit.
5. If the proposal can't be safely implemented (missing context, scope too
   large, blocking dependency), STOP without committing and explain why.
{skills_section}
Ground rules:
- One commit per run is fine; multiple small commits are also fine.
- Stay strictly within the proposal's scope — no drive-by refactors.
- Work ONLY inside the current working directory (the cloned target repo).
  NEVER create, edit, commit, or push files anywhere else — in particular
  the mounted gitops worktree under `/workdir`. If the proposal asks for a
  change in another repository (e.g. alert rules or manifests in
  mctl-gitops), do NOT make it; describe it in your final message so a
  human can route it through a reviewed PR.
- Never defer work to "the background." This run is a single, one-shot
  turn — there is no later turn for you to resume into, no polling loop,
  and nothing will notify you when a backgrounded command finishes. Run
  every command synchronously and wait for its result before ending your
  turn. A slow build or test is fine — wait for it inline. Do NOT end your
  turn saying you will "keep working" or "report back once it's done":
  ending the turn ends the run, and anything not committed by then is lost.
- Every command you run is bounded automatically to what remains of this
  run's execution budget — you do not choose the bound and cannot widen it.
  Backgrounding (a trailing `&`), `nohup`, `setsid`, `disown`, and starting a
  background process to poll it in a loop are BLOCKED outright, not merely
  discouraged: the tool call is denied. If the remaining budget cannot fit
  another command, commit what is already proven correct and say so in your
  final message.
- No emoji in code or commit messages.
- English only.
"""


def _load_review_feedback(path: Path) -> dict:
    """Read the JSON bundle the shepherd writes via ``apply_followup``.

    Schema (loose — fields beyond the documented set are ignored):
        {
          "p1": bool,
          "p2": bool,
          "summaries": [
            {"file": "...", "line": 123, "severity": "P1", "body": "..."},
            ...
          ]
        }

    The shepherd's fallback path may also produce ``summaries`` as plain
    strings; we tolerate both shapes so a fallback bundle still works
    end-to-end.
    """
    if not path.exists():
        raise SystemExit(f"--review-feedback path not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"--review-feedback bundle must be a JSON object, got {type(data).__name__}")
    return data


def _render_ci_failures_section(ci_failures: list) -> str:
    """Render the ``## Failing required CI checks (fix each)`` section.

    Deterministic, sourced entirely from `bundle["ci_failures"]` — the
    shepherd builds these records straight from GitHub check-run data
    (mctlhq/mctl-agents#411) and never routes them through its summariser
    SDK, so nothing here is model-rewritten. Each record's fields are
    optional (a `StatusContext` has no job/step, some checks have no run
    URL, a pre-#423 bundle has no `log_*` keys at all), so every line is
    rendered defensively.

    `log_excerpt`/`log_status`/`log_truncated` (mctl-agents#423) render as a
    second, clearly bounded block: the CI-variant prompt ground rule tells
    the agent this is the whole of the evidence it will get and not to fetch
    more, so the heading has to say plainly that it IS bounded and whether
    it was truncated, rather than reading like an ordinary quoted excerpt.
    """
    records = [item for item in ci_failures if isinstance(item, dict)]
    if not records:
        # The header was emitted before any record was known to render, so a
        # list whose items are all non-dict produced a bare heading with
        # nothing under it — and, in a CI-only bundle, a whole prompt telling
        # the agent to fix checks it is never shown (review P3).
        return ""

    lines: list[str] = ["## Failing required CI checks (fix each)", ""]
    for i, item in enumerate(records, 1):
        check = item.get("check") or "(unknown check)"
        workflow = item.get("workflow")
        job = item.get("job")
        step = item.get("step")
        conclusion = item.get("conclusion") or "?"
        url = item.get("url")
        head = item.get("head_sha")
        excerpt = (item.get("excerpt") or "").strip()
        log_excerpt = (item.get("log_excerpt") or "").strip()
        log_status = item.get("log_status") or "skipped"
        log_truncated = bool(item.get("log_truncated"))
        loc_bits = [b for b in (workflow, job, step) if b]
        loc = " / ".join(loc_bits)
        header = f"### Check {i}: {check} [{conclusion}]"
        if loc:
            header += f" — {loc}"
        lines.append(header)
        if head:
            lines.append(f"Head SHA: {head}")
        if url:
            lines.append(f"Run: {url}")
        if excerpt:
            lines.append(excerpt)
        if log_excerpt:
            lines.append("")
            lines.append(f"Log excerpt (bounded, {log_status}):")
            if log_truncated:
                lines.append(
                    "(truncated — this is a head+tail slice of the log, not "
                    "the whole thing)"
                )
            lines.append(log_excerpt)
        elif log_status != "ok":
            # mctl-agents#423 review P2: a non-"ok" status with no excerpt
            # means retrieval was skipped, timed out, or came back
            # unavailable — say so instead of leaving the agent to guess why
            # there is no log evidence for this check.
            lines.append("")
            lines.append(f"Log excerpt: none ({log_status}).")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _bundle_is_ci_only(bundle: dict) -> bool:
    """True when the bundle carries failing-check evidence and NO review findings.

    mctl-agents#411 made this bundle shape reachable (an actionable required
    check failed while the review is clean). The prompt built around it has to
    know, because every sentence of the follow-up variant was written for the
    other shape: "Code review left P1/P2 findings on this PR", "Read the codex
    findings (below)", "if a finding is already addressed ... write the refusal
    marker", "fixing the codex findings only". Spliced above a bundle that says
    there are no findings, that plausibly produces a refusal (which charges
    `refusals`) or an empty commit (a deterministic content failure), either of
    which spends one of MAX_REVIEW_ATTEMPTS — so the check stays red, the same
    bundle is rebuilt next tick, and the proposal walks to review-stuck over a
    lint/mypy failure nobody ever asked the implementer to fix.
    """
    return not (bundle.get("summaries") or []) and bool(bundle.get("ci_failures") or [])


def _bundle_work_class(bundle: dict) -> str:
    """`"ci-remediation"`, `"mixed"` or `"review"` — the work class
    `implementer_envelope()` derives the execution envelope from
    (mctl-agents#423).

    Reuses `_bundle_is_ci_only`'s exact predicate for the CI-only case
    rather than restating it, so the two can never silently diverge: a
    bundle read the prompt one way and the envelope another would be worse
    than either one being wrong consistently. `"review"` (plain findings, or
    a bundle carrying neither — the pre-#411 shape and every pre-#423
    caller) is the unconditional default: `implementer_envelope("review")`
    always resolves to the base `IMPLEMENTER_TIMEOUT_SECONDS`, so this is
    the "nothing changes" branch.
    """
    if _bundle_is_ci_only(bundle):
        return "ci-remediation"
    if bundle.get("ci_failures"):
        return "mixed"
    return "review"


def _render_review_feedback(bundle: dict) -> str:
    """Format the JSON bundle as a Markdown section for the sub-agent."""
    summaries = bundle.get("summaries") or []
    ci_failures = bundle.get("ci_failures") or []
    has_p1 = bool(bundle.get("p1"))
    has_p2 = bool(bundle.get("p2"))

    lines: list[str] = ["## Code review findings (address each)"]
    if has_p1 and has_p2:
        lines.append("Severity mix: at least one P1 and one P2 — fix both.")
    elif has_p1:
        lines.append("Severity: at least one P1 — must be fixed.")
    elif has_p2:
        lines.append("Severity: P2 only — fix all of them.")
    lines.append("")

    if not summaries:
        # A CI-only bundle (mctlhq/mctl-agents#411: an actionable required
        # check failed with a clean review) still has something useful to
        # say — render the CI section below instead of the old dead end.
        if ci_failures:
            lines.append("(No code review findings in this bundle.)")
        else:
            lines.append("(No summaries in bundle — re-read the PR's code review on GitHub.)")
            return "\n".join(lines)
    else:
        for i, item in enumerate(summaries, 1):
            if isinstance(item, dict):
                severity = item.get("severity") or "?"
                file_ = item.get("file") or item.get("path") or "(top-level comment)"
                line = item.get("line")
                body = (item.get("body") or "").strip()
                loc = file_ + (f":{line}" if line else "")
                lines.append(f"### Finding {i} [{severity}] — {loc}")
                if body:
                    lines.append(body)
                lines.append("")
            else:
                # Fallback shape: plain string summary.
                lines.append(f"- {str(item).strip()}")

    rendered = "\n".join(lines).rstrip() + "\n"
    if ci_failures:
        rendered += "\n" + _render_ci_failures_section(ci_failures)
    return rendered


def _branch_exists_on_origin(repo_dir: Path, branch: str) -> bool:
    """True iff `git ls-remote --heads origin <branch>` returns a ref line."""
    proc = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir, check=False)
    return bool(proc.stdout.strip())


def _remote_head_sha(repo_dir: Path, branch: str) -> str | None:
    """The current head SHA of ``branch`` on origin, or None if it has none."""
    proc = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir, check=False)
    line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    return line.split()[0] if line else None


def _push_followup(
    repo_dir: Path, branch: str, expected_sha: str, *, claim_context: _ClaimContext | None = None
) -> None:
    """Push the follow-up commit to the existing branch (no `-u`).

    Fences on the claimed head SHA: `--force-with-lease=<branch>:<expected_sha>`
    is the AUTHORITATIVE check — the push itself fails if the remote moved,
    independently of the claim. `claim_context`, when given, is the EARLY
    filter checked immediately before this git call (ADR-010 phase 2, #352).
    """
    if claim_context is not None:
        _check_claim_or_raise(claim_context, entity_version=expected_sha)
    _run(
        ["git", "push", f"--force-with-lease={branch}:{expected_sha}", "origin", branch],
        cwd=repo_dir,
    )


async def _run_implementer_agent(
    repo_dir: Path,
    prompt: str,
    proposal_dir: Path,
    *,
    envelope_s: float | None = None,
    work_class: str = "review",
    budget_ledger: CommandBudgetLedger | None = None,
) -> None:
    """Run the implementer's Claude Code turn under one outer wall-clock bound.

    ``envelope_s``/``work_class`` (mctl-agents#423): the caller derives both
    from the bundle it is driving (`review_feedback_one` via
    `_bundle_work_class` + `implementer_envelope`) and passes them through so
    every failure message below names which budget expired, not just that
    "N seconds expired". ``envelope_s=None`` (the default -- every plain
    `implement_one` call, and every existing test that only passes the first
    three positional args) resolves to `IMPLEMENTER_TIMEOUT_SECONDS` read at
    CALL time, not at function-definition time, so monkeypatching that module
    attribute still works exactly as it did before this parameter existed.

    ``budget_ledger`` (mctl-agents#430): when supplied, every agent-issued
    Bash command is bounded to what remains of THIS run's envelope via
    `options._deadline_guard_hook` -- see `build_implementer_agent_options`.
    The absolute deadline is computed HERE, immediately before
    `anyio.fail_after(envelope_s)` below, on the SAME monotonic clock
    (`anyio.current_time()`) that call uses, so the guard's remaining-budget
    arithmetic and the outer bound agree on what "now" and "the deadline"
    mean. ``budget_ledger=None`` (every caller and test that predates this
    parameter) omits the guard entirely and reproduces today's behaviour
    byte-for-byte -- see `build_implementer_agent_options`'s docstring.
    """
    if envelope_s is None:
        envelope_s = IMPLEMENTER_TIMEOUT_SECONDS
    deadline_monotonic = anyio.current_time() + envelope_s
    # One-shot probe, not per-command: `shutil.which` is cheap but there is no
    # reason to pay it once per Bash call, and the fallback (skip wrapping,
    # keep the tool-input clamp and the detachment denials) is a property of
    # the whole run, not of any one command.
    timeout_available = shutil.which("timeout") is not None
    if not timeout_available:
        print(
            "warn: `timeout` binary not found on PATH; falling back to "
            "tool-input clamping and detachment denials only -- commands are "
            "no longer bounded at the OS level (mctl-agents#430)",
            file=sys.stderr,
        )
    options = build_implementer_agent_options(
        repo_dir, SERVICE_AGENT_MODEL, proposal_dir, work_class=work_class,
        deadline_monotonic=deadline_monotonic,
        budget_ledger=budget_ledger,
        timeout_available=timeout_available,
    )
    mcp_configured = bool(options.mcp_servers)
    # A budget bounds spend but not a stalled network/model stream. Keep one
    # proposal inside the workflow's larger deadline (incident e3649b04).
    # The fail_after wraps the mctl connectivity check too — a wedged
    # handshake must not silently eat into the caller's own timeout budget.
    ledger = LiveTaskLedger()
    # Set once every delegated child has been awaited to a terminal state. From
    # that point on the work is on disk, so an outer expiry during teardown must
    # not throw it away -- see the TimeoutError handler.
    drain_completed = False
    # Bound so mypy (and a human) can see the TimeoutError handler's
    # `ledger.live` guard makes referencing `client` there safe even though a
    # deadline that fires during `ClaudeSDKClient.__aenter__` never assigns it.
    client: Any = None
    try:
        with anyio.fail_after(envelope_s):
            async with (
                # Model/tool spans (mctl-agents#195): names, ids and usage
                # counters only — see orchestrator/tracing.AgentRunObserver.
                tracing.agent_run("implementer", getattr(options, "model", None)) as trace_run,
                ClaudeSDKClient(options=options) as client,
            ):
                if mcp_configured:
                    # fatal=False — see orchestrator/mcp_guard.py. The
                    # implementer applies an already-written proposal via
                    # Read/Write/Edit/Bash; mctl tools are supplementary.
                    await ensure_mctl_connected(client, fatal=False)
                await client.query(prompt)
                # receive_messages(), NOT receive_response(): the latter returns
                # at the first ResultMessage, and a ResultMessage ends one TURN,
                # not the RUN. The prompt asks the agent to delegate to the
                # `implementer` sub-agent; when the CLI launches that child
                # asynchronously the top-level session ends its turn with the
                # child still working, and returning here abandons its commit
                # and produces a false EXIT_NO_FOLLOWUP_COMMITS. mctl-agents#366.
                #
                # One generator for both phases: every receive_messages() call
                # returns a fresh generator over the same underlying stream, so
                # a second one would split messages with the first.
                # cast: receive_messages() is declared AsyncIterator but is an
                # async generator, so it does have aclose(). aclosing() is what
                # guarantees the generator is closed on the error paths below.
                stream = cast(
                    "AsyncGenerator[Any, None]", client.receive_messages()
                )
                async with aclosing(stream):
                    def _note(message: Any) -> None:
                        print(message)
                        trace_run.observe(message)

                    async for message in stream:
                        _note(message)
                        ledger.observe(message)
                        # Also stop on stream exhaustion (the `async for` ending
                        # on its own): that means the CLI exited.
                        if isinstance(message, ResultMessage):
                            break
                    if ledger.live:
                        print(
                            f"info: turn ended with {ledger.describe()}; "
                            f"awaiting terminal status"
                        )
                        try:
                            await drain_until_settled(
                                stream,
                                ledger,
                                timeout_s=IMPLEMENTER_DRAIN_TIMEOUT_SECONDS,
                                on_message=_note,
                            )
                        except OrphanedSubagentError as exc:
                            raise ImplementerOrphanedSubagent(
                                f"orphaned sub-agent: {exc}"
                            ) from exc
                        drain_completed = True
                    if not ledger.all_completed:
                        # Quiescent, so NOT an orphan: nothing is still mutating
                        # the worktree and _has_new_commits is the right
                        # adjudicator (no commit -> 42, genuinely deterministic).
                        # Logged so the distinction is visible in the Argo log.
                        print(f"warn: {ledger.describe()}")
    except TimeoutError as exc:
        # A live child when the outer bound fires is a harness loss WHEREVER we
        # were -- draining, or still in the first turn loop. The earlier
        # `draining and` conjunct was one narrower than the rule stated
        # everywhere else: a task that started and then burned the whole budget
        # without its turn ever emitting a ResultMessage never reached the
        # drain, so it exited 44 -- deterministic, charged, MAX_HARNESS_FAILURES
        # bypassed -- for a child that was demonstrably still running.
        if ledger.live:
            # mctl-agents#423: a shielded, bounded teardown before re-raising.
            # Without the shield, `client.disconnect()` here awaits inside the
            # scope `fail_after` just cancelled -- the first checkpoint inside
            # it raises immediately, the teardown is skipped, and whatever CLI
            # child the SDK spawned outlives this process. `move_on_after`
            # bounds the shield itself: a wedged disconnect must not turn a
            # harness failure into a hang.
            if client is not None:
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(IMPLEMENTER_TEARDOWN_GRACE_SECONDS):
                        # Resolved before disconnect() runs, not after: the
                        # belt-and-suspenders check below must still have it
                        # when the grace clamp cancels mid-`disconnect()` --
                        # the exact case it exists for. Only reached when the
                        # SDK exposes it -- a fake test client legitimately
                        # does not.
                        transport = getattr(client, "_transport", None)
                        process = getattr(transport, "_process", None)
                        try:
                            await client.disconnect()
                        except Exception as teardown_exc:  # noqa: BLE001 — best-effort teardown
                            print(
                                f"warn: shielded teardown disconnect failed "
                                f"({type(teardown_exc).__name__}: {teardown_exc})"
                            )
                        finally:
                            # Belt-and-suspenders: disconnect() above should
                            # have torn down the transport's CLI child
                            # already, but a disconnect that itself got cut
                            # short by the grace clamp (still bounded above,
                            # just possibly incomplete) must not leave that
                            # child running. This has to be a `finally`, not
                            # code after the `try` -- code after the `try`
                            # is skipped when `move_on_after` cancels
                            # mid-`disconnect()` (the cancellation is not an
                            # `Exception`, so it is not caught above, and it
                            # unwinds straight past anything that isn't a
                            # `finally`). `process.terminate()` itself has no
                            # `await`, so it still runs to completion here
                            # even while the scope is cancelled.
                            if process is not None and getattr(process, "returncode", None) is None:
                                try:
                                    process.terminate()
                                except ProcessLookupError:
                                    pass
            # The outer wall-clock bound, not the drain's own -- but the cause
            # is still a child we could not await, so it is charged to the
            # harness, not to the proposal.
            raise ImplementerOrphanedSubagent(
                f"orphaned sub-agent: outer timeout of {envelope_s:g}s "
                f"(work class: {work_class}) expired while awaiting "
                f"{ledger.describe()}"
            ) from exc
        if drain_completed:
            # Every child was awaited to a terminal state before this fired, so
            # whatever they produced is already in the worktree. What is left
            # running is teardown -- closing the SDK generator and the client,
            # which this branch by construction does with the CLI still mid-turn
            # and, after the grace clamp, on very little clock. Raising here
            # would exit 44: charged, MAX_HARNESS_FAILURES bypassed, and
            # `_has_new_commits`/`_push_followup` never reached, so the commit we
            # just spent the whole drain waiting for would go in the bin with the
            # tmp clone. Return instead and let the git check adjudicate.
            print(
                f"warn: outer bound of {envelope_s:g}s (work class: {work_class}) "
                f"expired after the sub-agent was awaited; proceeding on what is "
                f"already in the worktree"
            )
            return
        raise ImplementerOperationTimeout(
            f"operation exceeded {envelope_s:g}s (work class: {work_class}; "
            f"model stream, client construction, or mctl connectivity check)"
        ) from exc


# ---------------------------------------------------------------------------
# Review-feedback mode — drive an existing branch.
# ---------------------------------------------------------------------------
def _checkout_existing_branch(repo_dir: Path, branch: str) -> None:
    """Fetch and check out an existing remote branch.

    Assumes the caller already verified the branch exists on origin via
    ``_branch_exists_on_origin``. Uses an explicit refspec so the local
    branch is created tracking origin/<branch>.
    """
    _run(["git", "fetch", "origin", f"{branch}:{branch}"], cwd=repo_dir)
    _run(["git", "checkout", branch], cwd=repo_dir)


def review_feedback_one(
    ref: ProposalRef,
    bundle: dict,
    dry_run: bool = False,
    branch: str | None = None,
) -> ImplementResult:
    """Apply code review feedback as a follow-up commit on the existing PR.

    Pre-conditions (caller's responsibility):
    - ``feat/agents-<slug>`` (or ``branch``, when set) exists on origin (the
      shepherd only invokes this mode after observing an open PR).
    - ``ref`` has a ``pr`` URL set in `.status.yaml` (used for logging
      only — we do not re-open a PR).

    ``branch`` overrides the deterministic ``feat/agents-<slug>`` name — the
    adopted-PR path (mctlhq/mctl-agents#334) passes the PR's own head
    branch, read from ``.prref.yaml`` by the caller, never a model-supplied
    value. ``None`` (the default) reproduces today's behaviour exactly.

    On success: pushes a follow-up commit to the existing branch and
    returns an ``ImplementResult`` whose ``pr_url`` is the existing PR
    URL (read from `.status.yaml`). `.status.yaml` is intentionally NOT
    rewritten — the shepherd owns the status transitions for follow-ups.
    """
    if dry_run:
        print(f"[dry-run] would address review on {ref.service}/{ref.slug}")
        return ImplementResult(ref=ref, pr_url=None, skipped_reason="dry-run")

    target = None
    result: ImplementResult | None = None
    claim_ctx: _ClaimContext | None = None
    # Read in `finally` so the claim is released on every exit path, not just
    # the ones that remembered to call `_release_claim` — see codex P2 on
    # ADR-010 phase 2 (#352): a claim released on only one arm leaks on every
    # other one.
    release_reason = "attempt ended"
    # Released on every exit path but one: a CLAIM_UNKNOWN refusal, where the
    # store could not answer. `implement_one`'s arm states the rule — a
    # release we cannot confirm is how an outage frees a live hold — and this
    # function contradicted it by releasing unconditionally (claude P3 on
    # `af661d7`). Failing closed here costs the rest of the lease; releasing
    # into an outage costs a second executor on the same PR.
    #
    # The cost is real and is NOT only paid on an unreachable store.
    # CLAIM_UNKNOWN also covers three answers the store did give — a 2xx
    # carrying no claim record, a 404, and a claim state this image does not
    # recognise — and on those the skipped release strands the PR for the full
    # `_review_claim_lease_default()` window (at least 30 minutes, and longer
    # once IMPLEMENTER_TIMEOUT_SECONDS is raised) rather than for nothing
    # (claude P3 on `0808376`). Accepted deliberately: none of those three
    # answers says who holds the claim, so all three are the case this rule is
    # for. `LIFECYCLE_OWNERSHIP_REQUIRED=false` is the break-glass.
    release_claim = True
    # An explicit branch means the caller resolved a PRRef, i.e. the
    # adopted-PR path (mctlhq/mctl-agents#334) — captured BEFORE the
    # default-branch fallback below collapses the distinction.
    adopted = branch is not None
    branch = branch or f"feat/agents-{ref.slug}"
    try:
        # 1. Clone the sibling repo. The shepherd's bundle path holds the
        # findings; cloning fresh keeps the worktree clean (avoids
        # stepping on a previous wedged run).
        target = _clone_target(ref.service, ref.slug)

        # 2. The branch must exist on origin — the shepherd only triggers
        # follow-ups for PRs whose head ref already exists. If it does
        # not, fail loudly so the operator can investigate (a missing
        # branch implies the PR was closed/deleted between ticks).
        if not _branch_exists_on_origin(target, branch):
            return ImplementResult(
                ref=ref,
                pr_url=None,
                error=f"branch {branch} not found on origin; refusing to create it in review-feedback mode",
            )
        _checkout_existing_branch(target, branch)

        # 3. Stage the per-service implementer sub-agent (same as new-PR path).
        _stage_implementer_agent(target, ref.service)

        # 4. Capture HEAD BEFORE the SDK call. This is the only ref that
        # cleanly distinguishes a no-op follow-up from a successful one —
        # comparing against `origin/<branch>` is unreliable when the local
        # fetch is stale, and `origin/HEAD..HEAD` is always non-empty on
        # an existing PR branch (it includes the original implementer
        # commit). See codex P1 on PR #12.
        old_head = _capture_head_sha(target)

        # 4b. ExecutionClaim on (pull-request, review-remediation), pinned to
        # the head just captured (ADR-010 phase 2, #352). Acquired BEFORE the
        # SDK runs so a concurrent attempt on the same PR is refused before
        # spending the SDK call; `claim_ctx` stays None when the PR url is
        # not yet recorded or no claim could be granted, and the run
        # proceeds exactly as it did before this proposal — the claim is
        # advisory below `enforce`. Released unconditionally in `finally`.
        pre_status = _load_status(ref.status_path)
        parsed_pr = _parse_pr_url(str(pre_status.get("pr") or ""))
        # Unlike the implement phase (fixed ordinal — no per-epoch counter
        # exists yet, ADR-010 phase 2), review-remediation already persists
        # one in `.status.yaml`'s `review_attempts`, so the WORKFLOW_UID-less
        # fallback can and should use it to keep distinct attempts distinct.
        attempt_ordinal = int(pre_status.get("review_attempts", 0) or 0)
        attempt_id = _resolve_attempt_id(ref.service, ref.slug, owner_epoch=0, attempt_ordinal=attempt_ordinal)
        if parsed_pr is not None:
            repo, number = parsed_pr
            claim_ctx = _acquire_claim(
                EntityRef.for_pull_request(repo, number, old_head),
                PHASE_REVIEW_REMEDIATION,
                old_head,
                attempt_id,
                lease_seconds=_claim_lease_seconds(
                    "LIFECYCLE_CLAIM_LEASE_SECONDS_REVIEW", _review_claim_lease_default()
                ),
            )

        # 4c. Resolve this run's ServiceSkillBundle (mctlhq/mctl-agents#305,
        # R24) BEFORE the SDK call — a ServiceSkillError aborts here, before
        # any commit, push, or PR (R22).
        try:
            skill_bundle = _resolve_implementer_service_skills(target, branch)
        except ServiceSkillError as exc:
            release_reason = "service skill resolution failed"
            return ImplementResult(ref=ref, pr_url=None, error=f"service skill resolution failed: {exc}")

        # 5. Run the SDK with the bundle baked into the prompt. The execution
        # envelope is derived from the work class the bundle actually carries
        # (mctl-agents#423) -- a CI-remediation or mixed bundle gets a wider,
        # capped envelope than the review-only default, and its own guard
        # hook (see build_implementer_agent_options). `budget_ledger`
        # (mctl-agents#430) is populated by the deadline guard as the run
        # progresses -- created here, before the SDK call, so it is
        # available below regardless of how the run ends.
        work_class = _bundle_work_class(bundle)
        n_checks = len(bundle.get("ci_failures") or [])
        envelope_s = implementer_envelope(work_class, n_checks=n_checks)
        budget_ledger = CommandBudgetLedger()
        prompt = _build_prompt(
            ref,
            review_feedback=bundle,
            branch=branch,
            adopted=adopted,
            service_skills_block=skill_bundle.to_prompt_block(repo_slug=f"mctlhq/{ref.service}"),
        )
        anyio.run(
            functools.partial(
                _run_implementer_agent,
                envelope_s=envelope_s, work_class=work_class, budget_ledger=budget_ledger,
            ),
            target, prompt, ref.proposal_dir.resolve(),
        )

        # 6. Did the agent commit anything new (beyond the captured pre-SDK SHA)?
        if not _has_new_commits(target, base=old_head):
            # No commit is not automatically a failure: the agent may have
            # decided, on the evidence, that changing nothing is correct.
            # Only a valid marker separates the two (mctl-agents#360).
            refusal = _read_refusal_marker(target)
            if refusal:
                if refusal.verification_budget_exhausted:
                    # mctl-agents#430: the agent itself decided the remaining
                    # command budget could not fit another verification step
                    # -- the same outcome the ledger reports below, recorded
                    # deliberately rather than observed by the guard's denial.
                    release_reason = "no follow-up: verification budget exhausted"
                    return ImplementResult(
                        ref=ref,
                        pr_url=None,
                        error=f"{VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX} {refusal.reason}",
                        budget_ledger=budget_ledger,
                    )
                if refusal.insufficient_evidence:
                    # mctl-agents#423: the bounded CI-log evidence could not
                    # support a code decision -- distinct from an ordinary
                    # refusal (see EXIT_CI_EVIDENCE_INSUFFICIENT's comment).
                    release_reason = "no follow-up: ci evidence insufficient"
                    return ImplementResult(
                        ref=ref,
                        pr_url=None,
                        error=f"{CI_EVIDENCE_INSUFFICIENT_ERROR_PREFIX} {refusal.reason}",
                    )
                if budget_ledger.exhausted:
                    # The agent wrote a marker but left
                    # `verification_budget_exhausted` unset, while the
                    # ORCHESTRATOR's own ledger recorded the budget running
                    # out. Falling through to the generic refusal would map
                    # this to EXIT_DELIBERATE_NO_OP (47), which the shepherd
                    # charges to `review_attempts` as a decision on the
                    # merits -- charging the proposal for a fact about the
                    # runner because a model omitted an optional boolean
                    # (agy P2 on `624a433`). Structured orchestrator evidence
                    # outranks model prose, which is the whole reason the
                    # ledger exists; the agent's own reason still rides along.
                    release_reason = "no follow-up: verification budget exhausted"
                    return ImplementResult(
                        ref=ref,
                        pr_url=None,
                        error=(
                            f"{VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX} "
                            f"{refusal.reason} [{budget_ledger.describe()}]"
                        ),
                        budget_ledger=budget_ledger,
                    )
                release_reason = "no follow-up: refused"
                return ImplementResult(
                    ref=ref,
                    pr_url=None,
                    error=f"{REFUSAL_ERROR_PREFIX} {refusal.reason}",
                )
            if budget_ledger.exhausted:
                # mctl-agents#430: the ORCHESTRATOR's own ledger -- not model
                # prose -- observed the command budget run out with nothing
                # committed and no marker written. Distinct from a plain
                # EXIT_NO_FOLLOWUP_COMMITS: re-running with a bigger reserve
                # or a faster verification step may still make progress,
                # whereas a plain no-commit is deterministic.
                release_reason = "no follow-up: verification budget exhausted"
                return ImplementResult(
                    ref=ref,
                    pr_url=None,
                    error=f"{VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX} {budget_ledger.describe()}",
                    budget_ledger=budget_ledger,
                )
            release_reason = "no follow-up commits"
            return ImplementResult(
                ref=ref,
                pr_url=None,
                error="implementer produced no follow-up commits",
            )

        # 7. Push to the existing branch — no -u, no new PR. `old_head` is
        # both the `--force-with-lease` CAS and the claim's pinned version:
        # if the branch moved since step 4, the claim check catches it before
        # git runs, and the push's own lease catches it even if the claim
        # check could not (store unreachable, rollout below `enforce`).
        _push_followup(target, branch, old_head, claim_context=claim_ctx)

        # 8. Read the existing PR URL from `.status.yaml` for the result
        # surface; do NOT rewrite the status — that belongs to the shepherd.
        # Print the ledger summary even on success (mctl-agents#430): commits
        # present is EXIT_OK regardless of any clamping/truncation along the
        # way, but that truncation must still be visible in the Argo log.
        existing = _load_status(ref.status_path)
        pr_url = existing.get("pr")
        release_reason = "follow-up pushed"
        print(f"info: command budget ledger: {budget_ledger.describe()}")
        result = ImplementResult(ref=ref, pr_url=pr_url)
        return result

    except ImplementerFenced as e:
        # Not a failure of the proposal — see EXIT_FENCED. The claim is
        # already fenced server-side (or the classification decided this
        # attempt cannot proceed), but the release below is still a
        # best-effort no-op call, not a mutation, so it stays unconditional.
        release_reason = "fenced"
        result = ImplementResult(ref=ref, pr_url=None, error=str(e))
        return result
    except ImplementerClaimRefused as e:
        # A claim check refused this attempt on this PR. Not a failure of the
        # findings and not a fence: nothing moved, this attempt either lost
        # the race or could not reach the store under the ownership
        # break-glass. Mapped to EXIT_CLAIM_REFUSED, which the shepherd
        # classifies alongside a fence: non-charging, and printed as the claim
        # decision it is rather than as "subprocess failed transiently" — a
        # leaked lease otherwise repeats that misleading line every tick for
        # the full lease (claude P3 on `31232dc`).
        release_reason = "claim refused"
        release_claim = e.verdict != CLAIM_UNKNOWN
        result = ImplementResult(ref=ref, pr_url=None, error=str(e))
        return result
    except ImplementerOrphanedSubagent as e:
        # The message is already prefixed "orphaned sub-agent:" — that prefix is
        # what _review_feedback_exit_code() matches on. Deliberately do NOT try
        # to push whatever is in the worktree here: by construction the child may
        # still be writing, and racing its `git commit` (index.lock) or pushing a
        # half-finished change is worse than a free retry on the next tick.
        release_reason = "orphaned sub-agent"
        result = ImplementResult(ref=ref, pr_url=None, error=str(e))
        return result
    except ImplementerOperationTimeout as e:
        release_reason = "operation timed out"
        result = ImplementResult(
            ref=ref,
            pr_url=None,
            error=f"operation timed out: {e}",
        )
        return result
    except subprocess.CalledProcessError as e:
        release_reason = "shell step failed"
        msg = f"shell step failed: {' '.join(e.cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}"
        result = ImplementResult(ref=ref, pr_url=None, error=msg)
        return result
    except SystemExit as e:
        release_reason = "SystemExit"
        result = ImplementResult(ref=ref, pr_url=None, error=f"SystemExit: {e}")
        return result
    except Exception as e:  # pragma: no cover — defensive  # noqa: BLE001 — surfaces as a result, not a crash
        release_reason = "unexpected error"
        result = ImplementResult(ref=ref, pr_url=None, error=f"{type(e).__name__}: {e}")
        return result
    finally:
        # One `finally` rather than a release per arm: a claim released on
        # only one exit path leaks on every other one (codex P2, ADR-010
        # phase 2 / #352). The single exception is set above, where the store
        # itself could not be reached.
        if release_claim:
            _release_claim(claim_ctx, reason=release_reason)
        if target and target.exists() and result is not None and result.error is None:
            try:
                shutil.rmtree(target)
            except OSError:
                pass


def _has_new_commits(repo_dir: Path, base: str = "origin/HEAD") -> bool:
    """True iff the implementer actually committed something beyond base.

    Prefer to pass the *pre-SDK HEAD SHA* as ``base`` (captured by the
    caller right before the SDK invocation). That is the only ref that
    can reliably distinguish a no-op SDK run from a successful follow-up:

    - New-branch path (implement_one): a freshly-created branch sits at
      origin/HEAD, so the captured SHA equals origin/HEAD anyway.
    - Review-feedback path (review_feedback_one): the existing branch
      tip already has commits ahead of origin/HEAD AND ahead of
      origin/<branch> when the local fetch is stale. Comparing against
      either remote ref can wrongly report "new commits" when the SDK
      did nothing. The captured pre-SDK SHA cannot.
    """
    proc = _run(["git", "log", "--oneline", f"{base}..HEAD"], cwd=repo_dir, check=False)
    return bool(proc.stdout.strip())


def _capture_head_sha(repo_dir: Path) -> str:
    """Return the current HEAD SHA in ``repo_dir`` (no rev parsing here)."""
    proc = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# Pre-PR safety guard — chart MAJOR-version bump must acknowledge CRD migration
# ---------------------------------------------------------------------------
# Anchored at the start of the post-prefix YAML body — the helper
# below strips the diff `+`/`-` prefix and leading whitespace before
# applying this pattern. Negative lookahead `(?![.0-9])` rejects
# 4-component values like `1.2.3.4` instead of capturing `1.2.3` and
# silently dropping the trailing `.4`.
_TARGET_REVISION_RE = re.compile(
    r"""^targetRevision:\s*['"]?([0-9]+(?:\.[0-9]+){1,2})(?![.0-9])"""
)


def _diff_line_targets_revision(line: str) -> str | None:
    """Parse a diff `-`/`+` line that mutates a YAML
    ``targetRevision:`` field, return the version string. Return
    ``None`` for diff lines that look superficially similar but
    aren't real bumps:

    - YAML comments (`# targetRevision: 2.4.0`) — Codex P2 #1.
    - Substring keys (`nottargetRevision: 2.4.0`,
      `customTargetRevision: 2.4.0`) — Codex P2 #2.
    - 4-component pseudo-semvers (`targetRevision: 1.2.3.4`).
    - Unanchored substrings inside other YAML strings.

    Also returns ``None`` for the diff metadata lines themselves
    (`---`, `+++`, `@@`) by virtue of their prefix.
    """
    if not line or line[0] not in "+-":
        return None
    body = line[1:].lstrip()
    # Skip YAML comments — `_TARGET_REVISION_RE` is anchored at
    # `^targetRevision:` so this is belt-and-braces, but the explicit
    # check makes the intent obvious to a reader.
    if body.startswith("#"):
        return None
    m = _TARGET_REVISION_RE.match(body)
    if m is None:
        return None
    return m.group(1)


def _detect_chart_major_bumps(repo_dir: Path, base: str = "origin/HEAD") -> list[tuple[str, str, str]]:
    """Return (file, old_version, new_version) tuples for Helm chart
    `targetRevision` MAJOR-version bumps in the diff against ``base``.

    Matches lines like:
        - targetRevision: 0.10.7
        + targetRevision: 2.4.0

    Lines must be on adjacent diff hunks (same `targetRevision:` field
    edit). Pre-release suffixes (`-rc1`, `-beta.2`) are tolerated by
    extracting only the leading numeric prefix; the comparison is on
    integer MAJOR.

    Why: this is the long-tail timebomb from the 2026-05-01 ESO incident
    (external-secrets 0.10.x → 2.x). Helm doesn't refresh CRDs on
    `helm upgrade`, so a chart MAJOR bump can leave the cluster with
    a controller that requires CRDs the cluster doesn't serve. The
    proposal author must explicitly acknowledge the CRD migration
    plan; otherwise the implementer fails fast before pushing the PR.
    """
    # Pin the comparison to committed HEAD, NOT the working tree.
    # `git diff <base>` (no second arg) diffs base against the working
    # copy, which means a stray uncommitted scratch edit could fire
    # the guard against a PR that won't actually push that change —
    # or, conversely, an uncommitted revert could mask a real bump
    # that IS in HEAD. The bumps we want to gate on are exactly the
    # ones a `git push` would deliver, which is `<base>..HEAD`.
    proc = _run(["git", "diff", base, "HEAD", "--unified=0"], cwd=repo_dir, check=False)
    diff = proc.stdout

    bumps: list[tuple[str, str, str]] = []
    current_file: str | None = None
    # FIFO queue of `-` versions waiting for their matching `+`. A
    # single hunk like `-old1 -old2 +new1 +new2` must pair old1↔new1
    # and old2↔new2; with a scalar pending_old we'd lose old1 on the
    # second `-` and falsely pair old2↔new1, missing a real MAJOR
    # bump (Codex P1 on PR mctl-agents#14).
    pending_olds: list[str] = []

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):]
            # File boundary resets the queue — pairing `-` from one
            # file with `+` from another is never correct.
            pending_olds = []
            continue
        if line.startswith("--- "):
            # `--- a/<file>` is the partner of `+++ b/<file>` in the
            # diff header; just skip it. (We don't reset on `--- `
            # alone because that would clobber `pending_olds` between
            # the two header lines.)
            continue
        if line.startswith("@@"):
            # NB: do NOT reset pending_olds on hunk boundaries.
            # `git diff --unified=0` splits a single field-move-and-
            # bump into two hunks (delete at old line, add at new
            # line). Resetting here drops the old version before the
            # add is parsed, and a real MAJOR bump bypasses the guard
            # (Codex P1 on PR mctl-agents#14). The FIFO is per-file,
            # not per-hunk; a stray un-paired `-` would surface as a
            # false-positive bump on a later unrelated `+` in the
            # same file, which is the safer failure mode for a guard
            # (block-on-doubt).
            continue
        # Both `-` and `+` go through the anchored helper so a line
        # like `# targetRevision: 2.4.0` (comment) or
        # `someTargetRevision: 2.4.0` (substring key) does NOT register.
        if line.startswith("-") and not line.startswith("---"):
            ver = _diff_line_targets_revision(line)
            if ver:
                pending_olds.append(ver)
        elif line.startswith("+") and not line.startswith("+++"):
            ver = _diff_line_targets_revision(line)
            if ver and pending_olds and current_file is not None:
                old_ver = pending_olds.pop(0)
                if _is_major_bump(old_ver, ver):
                    bumps.append((current_file, old_ver, ver))
    return bumps


def _is_major_bump(old: str, new: str) -> bool:
    """True iff ``new`` has a strictly larger SemVer MAJOR than ``old``.

    Both strings are dotted SemVer (no leading `v`). Anything that
    doesn't parse cleanly returns False — defensive default that
    matches the existing `feedback_no_v_prefix` convention.
    """
    try:
        old_major = int(old.split(".")[0])
        new_major = int(new.split(".")[0])
    except (ValueError, IndexError):
        return False
    return new_major > old_major


def _proposal_acks_crd_migration(proposal_dir: Path) -> bool:
    """True if the proposal explicitly acknowledges that this change
    crosses a chart MAJOR boundary and therefore requires a CRD
    migration plan.

    Acknowledgement signal: any file whose name starts with
    ``crd-migration`` (case-insensitive) inside the proposal directory.
    Content is not inspected — the *existence* is the contract: the
    spec author saw the warning, named the file, and either described
    the migration in it or linked to the relevant doc / drill.

    Examples that count:
        proposals/eso-cve-patch/crd-migration.md
        proposals/eso-cve-patch/crd-migration-plan.md
        proposals/eso-cve-patch/CRD-MIGRATION.txt

    Empty files are fine — the file is a flag, not a doc.
    """
    if not proposal_dir.exists():
        return False
    for entry in proposal_dir.iterdir():
        if entry.is_file() and entry.name.lower().startswith("crd-migration"):
            return True
    return False


def _issue_closing_line(ref: ProposalRef) -> str:
    """Return a `Closes …` line for the PR body when the proposal was born
    from a GitHub issue, else an empty string.

    Issue-driven proposals carry a `source` block in .status.yaml:

        source:
          type: github_issue
          repo: mctlhq/mctl-telegram
          issue: 123

    Emitting `Closes <repo>#<N>` makes GitHub auto-close the originating
    issue when the PR merges. The fully-qualified `owner/repo#N` form works
    for same-repo references too, so we use it unconditionally — no need to
    special-case whether the issue repo equals the PR repo.
    """
    source = _load_status(ref.status_path).get("source")
    if not isinstance(source, dict) or source.get("type") != "github_issue":
        return ""
    repo = source.get("repo")
    issue = source.get("issue")
    if not repo or not issue:
        return ""
    return f"\n\nCloses {repo}#{issue}"


def _approval_blocked_message(ref: ProposalRef) -> tuple[str, str]:
    """Return ``(message, remedy)`` for a proposal blocked on missing approval.

    ``message`` explains WHY the gate refused (used verbatim as
    ``skipped_reason``). ``remedy`` names a route that actually applies to
    THIS proposal, built from the same ``source`` block ``_issue_closing_line``
    reads and ``workflow_id_for()`` (deliberately temporalio-free, so this
    module can import it).

    Never suggests hand-editing ``approval.approved_by`` -- that forges the
    exact record the gate exists to require.
    """
    message = (
        "requires_human_approval is set but no verified approval.approved_by "
        "is recorded, and the proposal is already accepted; no supported "
        "path can run it as written"
    )
    no_op_and_recovery = (
        "mctl-agents-approve only performs the proposed -> accepted flip "
        "and is a no-op on an already-accepted proposal; the supported "
        "recovery is to re-publish the proposal in 'proposed' status, where "
        "the flip actually records an approver. Never hand-edit "
        "approval.approved_by."
    )

    source = _load_status(ref.status_path).get("source")
    if isinstance(source, dict) and source.get("type") == "github_issue":
        repo = source.get("repo")
        issue = source.get("issue")
        if repo and issue:
            try:
                workflow_id = workflow_id_for(f"https://github.com/{repo}/issues/{issue}")
            except ValueError:
                workflow_id = None
            if workflow_id:
                remedy = (
                    f"{no_op_and_recovery} A DevLoopWorkflow {workflow_id} may "
                    f"exist for this issue; only if that execution is still "
                    f"running would signalling POST "
                    f"/api/v1/agents/dev-loop/{workflow_id}/approve do "
                    f"anything, and that signal is what is most likely to "
                    f"have produced this exact blocked state, so treat it as "
                    f"a secondary check, not the fix."
                )
                return message, remedy

    remedy = f"{no_op_and_recovery} No DevLoopWorkflow exists for this proposal."
    return message, remedy


def _pr_title_and_body(ref: ProposalRef) -> tuple[str, str]:
    title = f"feat(agents): {ref.slug}"
    body = (
        f"Implements accepted proposal `{ref.service}/{ref.slug}`.\n\n"
        f"Spec: https://github.com/mctlhq/mctl-gitops/tree/main/platform-gitops/"
        f"agents-state/{ref.service}/proposals/{ref.slug}/\n\n"
        f"Generated by mctl-agents Tier 2 implementer. Review carefully — "
        f"this PR was opened automatically and the spec may have gaps."
        f"{_issue_closing_line(ref)}"
    )
    return title, body


def _canonical_proposal_marker(ref: ProposalRef) -> str:
    return f"agents-state/{ref.service}/proposals/{ref.slug}/"


def _result_branch_is_missing(detail: str) -> bool:
    """Return true only for GitHub responses that prove the ref is absent."""
    return (
        "HTTP 404" in detail
        or "Not Found" in detail
        or (
            "HTTP 422" in detail
            and "No commit found for SHA:" in detail
        )
    )


def _open_pr_for_branch(ref: ProposalRef, branch: str) -> str:
    title, body = _pr_title_and_body(ref)
    # Pin the base repo with --repo: on a fork (e.g. mctl-openclaw, forked from
    # openclaw/openclaw) `gh pr create` otherwise defaults the base to the parent
    # repo and the non-interactive call fails, so the branch is pushed but no PR
    # is opened. --repo forces the PR into our repo against our own `main`.
    proc = _run(
        ["gh", "pr", "create", "--repo", f"mctlhq/{ref.service}",
         "--title", title, "--body", body, "--head", branch, "--base", "main"],
    )
    pr_url = proc.stdout.strip().splitlines()[-1]
    print(f"ok: PR opened: {pr_url}")
    return pr_url


def _preflight_existing_result(
    ref: ProposalRef,
    *,
    allow_pr_create: bool = True,
) -> ExistingResult:
    """Reconstruct durable proposal state from GitHub before using the model."""
    repo = f"mctlhq/{ref.service}"
    branch = f"feat/agents-{ref.slug}"
    prs = _github_json([
        "gh", "pr", "list", "--repo", repo, "--state", "all",
        "--head", branch, "--limit", "100",
        "--json", "number,url,state,mergedAt,headRefName,headRefOid,body",
    ])
    if not isinstance(prs, list):
        raise GitHubPreflightError("gh pr list returned a non-list response")

    exact = [pr for pr in prs if pr.get("headRefName") == branch]
    exact.sort(
        key=lambda pr: (
            pr.get("state") == "OPEN",
            bool(pr.get("mergedAt")) or pr.get("state") == "MERGED",
            int(pr.get("number") or 0),
        ),
        reverse=True,
    )
    if exact:
        pr = exact[0]
        if _canonical_proposal_marker(ref) not in (pr.get("body") or ""):
            return ExistingResult(
                action="needs-triage",
                pr_url=pr.get("url"),
                head_sha=pr.get("headRefOid"),
                reason="branch-collision",
            )
        common = {
            "pr_url": pr.get("url"),
            "head_sha": pr.get("headRefOid"),
        }
        if pr.get("state") == "OPEN":
            return ExistingResult(action="open", **common)
        if pr.get("mergedAt") or pr.get("state") == "MERGED":
            return ExistingResult(action="merged", **common)
        return ExistingResult(action="closed", reason="closed-unmerged", **common)

    encoded_branch = quote(branch, safe="")
    branch_proc = _run(
        ["gh", "api", f"repos/{repo}/commits/{encoded_branch}"],
        check=False,
    )
    if branch_proc.returncode != 0:
        detail = (branch_proc.stderr or branch_proc.stdout or "").strip()
        if _result_branch_is_missing(detail):
            return ExistingResult(action="none")
        raise GitHubPreflightError(detail or "failed to query result branch")
    try:
        branch_data = json.loads(branch_proc.stdout)
        head_sha = branch_data["sha"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GitHubPreflightError("invalid result-branch response") from exc

    comparison = _github_json([
        "gh", "api", f"repos/{repo}/compare/main...{head_sha}",
    ])
    ahead_by = int((comparison or {}).get("ahead_by") or 0)
    if ahead_by <= 0:
        return ExistingResult(
            action="needs-triage",
            head_sha=head_sha,
            reason="branch-has-no-commits",
        )
    if not allow_pr_create:
        return ExistingResult(
            action="branch-ready",
            head_sha=head_sha,
            reason="useful branch exists without PR",
        )

    # The previous attempt pushed useful commits and died before `gh pr create`.
    # Opening the PR is deterministic and avoids spending model quota again.
    try:
        pr_url = _open_pr_for_branch(ref, branch)
    except subprocess.CalledProcessError as exc:
        # A concurrent actor may have opened it between list and create.
        retry = _github_json([
            "gh", "pr", "list", "--repo", repo, "--state", "open",
            "--head", branch, "--limit", "10",
            "--json", "url,headRefName,headRefOid,body",
        ])
        matching = [
            pr for pr in retry
            if pr.get("headRefName") == branch
            and _canonical_proposal_marker(ref) in (pr.get("body") or "")
        ]
        if not matching:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise GitHubPreflightError(
                f"failed to open PR for existing result branch: {detail}"
            ) from exc
        pr_url = matching[0]["url"]
        head_sha = matching[0].get("headRefOid") or head_sha
    return ExistingResult(action="open", pr_url=pr_url, head_sha=head_sha)


def _github_projection(existing: ExistingResult) -> dict[str, Any]:
    state = {
        "open": "open",
        "merged": "merged",
        "closed": "closed",
        "needs-triage": "unknown",
    }.get(existing.action, "unknown")
    projection: dict[str, Any] = {
        "state": state,
        "observed_at": _now_iso(),
    }
    if existing.head_sha:
        projection["head_sha"] = existing.head_sha
    if existing.action == "needs-triage" and existing.reason:
        projection["blocking_reason"] = existing.reason
    return projection


def _triage_error(message: str, recorded: bool) -> str:
    """Annotate a failure message with what `_mark_needs_triage` actually did.

    The compare-and-swap can decline the write, and "recorded at a human gate"
    and "left alone because a second executor now owns the proposal" are two
    different states of the world. The caller turns this into
    `ImplementResult.error`, so the batch summary says which one happened
    instead of asserting a status write that never landed (claude P3 on
    `8ac2080`) — the same thing the hand-back already does for its skip reason.
    """
    if recorded:
        return message
    return f"{message} (not recorded: another attempt now holds the proposal)"


def _mark_needs_triage(
    ref: ProposalRef,
    *,
    code: str,
    stage: str,
    message: str,
    pr_url: str | None = None,
    attempt: dict[str, Any] | None = None,
    claim_context: _ClaimContext | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> bool:
    """Record a terminal failure on the proposal. True when it was written.

    False means a second executor now owns the proposal and its status was
    deliberately left alone — see `_status_is_still_ours`.
    """
    fields: dict[str, Any] = {
        "failure": {
            "code": code,
            "stage": stage,
            "message": message[:2000],
        },
        "notes": f"{stage}: {message[:500]}",
    }
    if extra_fields:
        fields.update(extra_fields)
    if pr_url:
        fields["pr"] = pr_url
    if attempt:
        # The same compare-and-swap the hand-back does, for the same reason:
        # this write carries OUR attempt block, so making it while somebody
        # else's run is live erases the block `_attempt_is_fresh` reads — and
        # parks the proposal at a human gate for a race that, on the fence
        # arm, the exception's own docstring calls "not a failure of the
        # proposal" (claude P2 on `0808376`). An attempt-less call (no attempt
        # was ever stamped) has nothing to compare and writes as before.
        attempt_id = attempt.get("id") or ""
        if not _status_is_still_ours(ref, attempt_id, doing=f"recording {code}"):
            # Still release: the claim names our own record, so letting go of
            # it frees nothing of theirs and leaks nothing of ours.
            _release_claim(claim_context, reason=f"{stage}: {code}")
            return False
        finished = dict(attempt)
        finished["finished_at"] = _now_iso()
        fields["attempt"] = finished
    update_status_yaml(ref, "needs-triage", **fields)
    # A terminal arm relinquishes the claim it was holding (ADR-010 phase 2,
    # #352) — best-effort, and never the reason this write fails.
    _release_claim(claim_context, reason=f"{stage}: {code}")
    return True


def _mark_blocked(
    ref: ProposalRef,
    *,
    code: str,
    message: str,
    remedy: str,
) -> bool:
    """Write (or refresh) the durable ``blocked`` marker exactly once.

    Reads the current file first: if ``code``, ``message`` and ``remedy``
    are all unchanged, returns ``False`` without writing, so a permanently
    blocked proposal produces exactly one gitops commit and not one per
    tick. The block carries no per-observation timestamp; ``since`` is set
    on first write and preserved after that. ``status`` stays ``accepted``.

    Returns ``True`` when it performed a write (a new marker, or a changed
    one), ``False`` on the no-op path -- callers use this to tell a
    freshly-detected block apart from one merely re-observed unchanged.
    """
    current = _load_status(ref.status_path).get("blocked")
    since = None
    if isinstance(current, dict):
        if (
            current.get("code") == code
            and current.get("message") == message
            and current.get("remedy") == remedy
        ):
            return False
        since = current.get("since")
    block = {
        "code": code,
        "since": since or _now_iso(),
        "message": message,
        "remedy": remedy,
    }
    update_status_yaml(ref, "accepted", blocked=block)
    return True


def _superseding_pr_urls(repo: str, number: int) -> list[str]:
    """Best-effort lookup of merged PRs GitHub already recorded as closing
    `repo#number` (mctl-agents#410, the honest version of "closed but
    superseded").

    Only called on the closed-as-completed arm -- there is nothing to
    supersede a `not_planned` issue with. Diagnostics only: any failure
    (network, JSON, unexpected shape) or an empty result returns `[]`, and
    the caller must never let that change the admission verdict -- this
    lookup never runs before the refusal is decided, only after, to build
    the message.
    """
    try:
        events = _github_json(["gh", "api", f"repos/{repo}/issues/{number}/timeline"])
    except Exception:  # noqa: BLE001 -- diagnostics only, never propagate
        return []
    if not isinstance(events, list):
        return []

    dated: list[tuple[str, str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        if kind == "cross-referenced":
            source_issue = (event.get("source") or {}).get("issue") or {}
            pr = source_issue.get("pull_request") or {}
            merged_at = pr.get("merged_at")
            url = source_issue.get("html_url")
            if merged_at and url:
                dated.append((merged_at, url))
        elif kind == "closed" and event.get("commit_id"):
            # A closing commit with no accompanying cross-reference event
            # still proves supersession -- look up the PR(s) that commit
            # belongs to. Best-effort per commit: one bad lookup must not
            # discard URLs already found from other events.
            try:
                prs = _github_json(
                    ["gh", "api", f"repos/{repo}/commits/{event['commit_id']}/pulls"]
                )
            except Exception as exc:  # noqa: BLE001 -- diagnostics only
                print(f"warn: could not look up PRs for commit {event['commit_id']}: {exc}")
                continue
            if isinstance(prs, list):
                for pr in prs:
                    if not isinstance(pr, dict):
                        continue
                    merged_at = pr.get("merged_at")
                    url = pr.get("html_url")
                    if merged_at and url:
                        dated.append((merged_at, url))

    if not dated:
        return []
    dated.sort(key=lambda item: item[0], reverse=True)
    seen: set[str] = set()
    urls: list[str] = []
    for _, url in dated:
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) == 3:
            break
    return urls


def _stale_source_message(ref: ProposalRef, verdict: SourceIssueVerdict) -> str:
    """Compose the admission-refusal message for a proposal's closed source
    issue (mctl-agents#410).

    Names the issue reference, close reason and close timestamp, and the
    one supported recovery -- reopening the issue, or re-publishing the
    proposal as 'proposed' -- rather than a re-check the implementer would
    never run on its own. Deterministic for a fixed verdict, and stays
    comfortably under the 2000-char clamp `_mark_needs_triage` applies to
    `failure.message`.
    """
    not_planned = verdict.state_reason == "not_planned"
    reason = "not planned" if not_planned else "completed"
    closed_at = f" (closed {verdict.closed_at})" if verdict.closed_at else ""
    message = (
        f"source issue {verdict.issue_ref} is closed as {reason}{closed_at}; "
        f"admission refused before any model attempt."
    )
    if not not_planned:
        # Nothing to supersede a not-planned issue with -- only look on the
        # completed arm.
        source = _load_status(ref.status_path).get("source") or {}
        repo = source.get("repo")
        number = source.get("issue")
        urls = _superseding_pr_urls(repo, number) if repo and number else []
        if urls:
            message += " Superseded by: " + ", ".join(urls) + "."
    message += (
        " Reopen the issue, or re-publish this proposal as 'proposed', to "
        "make it runnable again."
    )
    return message


def _push_and_open_pr(
    repo_dir: Path, ref: ProposalRef, *, claim_context: _ClaimContext | None = None
) -> str:
    branch = f"feat/agents-{ref.slug}"
    # One `git ls-remote`, not two: `_remote_head_sha` already answers None for
    # a branch origin does not have, and guarding it with
    # `_branch_exists_on_origin` only added a second remote call whose
    # transient failure would read as "no branch" and send us down the `push
    # -u` arm against a branch that exists (agy P3 on `f4d0dec`).
    expected_sha = _remote_head_sha(repo_dir, branch)
    if expected_sha:
        # A previous attempt already pushed and died before opening the PR —
        # a retried pod, exactly the case ADR-010 §8 names (mctl-agents#352).
        # REPLACE, not adopt: this attempt cloned fresh and recreated the
        # branch off the default branch, so the local branch does not contain
        # the dead attempt's commits and this push discards them. That is
        # intended — those commits are referenced by no PR, and the findings
        # they were meant to address are being implemented again here — but
        # `--force-with-lease` proves only that nobody MOVED the ref since the
        # read below, never that we contain it, so calling it an adoption read
        # as a guarantee it does not make (claude P3, carried from `b64b35b`).
        #
        # The lease is still the CAS that matters: it fails the push if a
        # third writer moves the branch in the gap between the read above and
        # the push below. A missing head is NOT expressible as a lease —
        # `--force-with-lease=<branch>:` means "must not already exist", the
        # negation of the precondition that selected this path — so an
        # unreadable head falls through to the plain `-u` push instead.
        if claim_context is not None:
            # Deliberately NOT `entity_version=expected_sha`. This claim is on
            # the PROPOSAL (`EntityRef.for_proposal`, acquired with
            # `entity_version=""`) — a proposal has no git head to pin at
            # implement time. Asserting this branch's SHA against it claims a
            # pin the claim never made, and by ADR-010 §6 the server fences on
            # the claim's own `entity_version` against the current head: a
            # version the claim does not hold is a conflict it must answer,
            # which would abort a perfectly good attempt as CLAIM_FENCED.
            # `--force-with-lease` below is the authoritative git CAS and is
            # unaffected either way (agy P2 on ddcdb0e).
            _check_claim_or_raise(claim_context)
        _run(
            ["git", "push", f"--force-with-lease={branch}:{expected_sha}", "origin", branch],
            cwd=repo_dir,
        )
    else:
        # Brand new branch — no remote ref exists (or its head could not be
        # read), so git has nothing to fence against. The CLAIM still does:
        # the model ran for a long time between
        # the acquire and here, and the claim may have been fenced or expired
        # in that window. ADR-010 requires the check immediately before EVERY
        # push this module performs, and "there is no remote ref yet" is a
        # statement about git, not about who is allowed to write.
        if claim_context is not None:
            _check_claim_or_raise(claim_context)
        _run(["git", "push", "-u", "origin", branch], cwd=repo_dir)
    return _open_pr_for_branch(ref, branch)


def implement_one(ref: ProposalRef, dry_run: bool = False) -> ImplementResult:
    """Implement a single accepted proposal. Returns ImplementResult."""
    if ref.status != "accepted":
        return ImplementResult(
            ref=ref,
            pr_url=None,
            skipped_reason=f"status is {ref.status}; only accepted proposals are runnable",
        )

    # `accepted` alone is not authorisation. A proposal whose control block
    # requires human approval must carry an approval naming someone; without
    # it, a status flip committed by anything -- or by anyone with write
    # access to agents-state -- is indistinguishable from a real approval
    # (gitops#986). This is a permanent deadlock, not a transient skip: no
    # supported path can ever run it as written (mctl-agents#349). Refuse
    # rather than run the model, and make the refusal loud and durable.
    if not ref.approval_ok:
        message, remedy = _approval_blocked_message(ref)
        blocked_is_new = False
        if not dry_run:
            blocked_is_new = _mark_blocked(
                ref,
                code=BLOCKED_APPROVAL_MISSING,
                message=message,
                remedy=remedy,
            )
        return ImplementResult(
            ref=ref,
            pr_url=None,
            blocked=BLOCKED_APPROVAL_MISSING,
            blocked_is_new=blocked_is_new,
            skipped_reason=message,
            counts_toward_limit=False,
        )

    if dry_run:
        print(f"[dry-run] would preflight then implement {ref.service}/{ref.slug}")
        return ImplementResult(ref=ref, pr_url=None, skipped_reason="dry-run")

    # Loaded once per proposal attempt (mctlhq/mctl-agents#196, ADR 011) and
    # reused wherever this run's identity is recorded, so the log line, the
    # MCP headers (orchestrator.options, loaded from the same
    # MCTL_EXECUTION_CONTEXT_FILE) and the `.status.yaml` execution: block
    # agree by construction whenever a control-plane-minted context exists.
    try:
        execution_context = load_from_environment(
            executor_type="implementer", workflow_type="implement", agent="implementer"
        )
    except ExecutionIdentityError as exc:
        # Mirrors orchestrator.options._execution_context_headers(): a
        # present-but-broken MCTL_EXECUTION_CONTEXT_FILE (unreadable,
        # truncated, or tamper-evidence failure) must not crash the attempt —
        # degrade to a locally-minted, explicitly unverified context instead.
        # load_from_environment() wraps every read/parse failure into
        # ExecutionIdentityError, so this narrow catch is complete — and an
        # ExecutionContextRequiredError (MCTL_REQUIRE_EXECUTION_CONTEXT set)
        # passes through and kills the attempt: require mode fails closed
        # and never mints a local identity (ADR 011).
        print(f"warn: MCTL_EXECUTION_CONTEXT_FILE is set but unreadable ({exc}); minting a local execution context.")
        execution_context = mint_local(executor_type="implementer", workflow_type="implement", agent="implementer")
    print(f"[identity] execution_context={json.dumps(execution_context.to_log_dict())}")

    try:
        existing = _preflight_existing_result(ref)
    except GitHubPreflightError as exc:
        return ImplementResult(
            ref=ref,
            pr_url=None,
            error=f"GitHub preflight failed closed: {exc}",
            counts_toward_limit=False,
        )

    if existing.action == "open":
        update_status_yaml(
            ref,
            "implemented",
            pr=existing.pr_url,
            github=_github_projection(existing),
            failure=None,
            blocked=None,
            notes=None,
            attempt=None,
        )
        return ImplementResult(ref=ref, pr_url=existing.pr_url)
    if existing.action == "merged":
        update_status_yaml(
            ref,
            "merged",
            pr=existing.pr_url,
            github=_github_projection(existing),
            merged_at=_now_iso(),
            failure=None,
            blocked=None,
            notes=None,
            attempt=None,
        )
        return ImplementResult(ref=ref, pr_url=existing.pr_url)
    if existing.action == "closed":
        update_status_yaml(
            ref,
            "rejected",
            pr=existing.pr_url,
            github=_github_projection(existing),
            notes="GitHub PR was closed without merging.",
            failure=None,
            blocked=None,
        )
        return ImplementResult(
            ref=ref,
            pr_url=existing.pr_url,
            skipped_reason="existing PR is closed without merge",
        )
    if existing.action == "needs-triage":
        _mark_needs_triage(
            ref,
            code=existing.reason or "existing-result-invalid",
            stage="preflight",
            message="Existing deterministic result cannot be adopted safely.",
            pr_url=existing.pr_url,
        )
        return ImplementResult(
            ref=ref,
            pr_url=existing.pr_url,
            error=existing.reason or "existing result needs triage",
        )

    # Admission gate (mctl-agents#410): only reachable on `existing.action
    # == "none"` -- no branch, no PR for this proposal at all -- because
    # every other preflight outcome above already returned. That ordering
    # matters: a proposal the implementer already carried to a `merged` PR
    # *has* a closed source issue (the PR body says `Closes <repo>#<N>`),
    # and gating before the preflight would relabel it stale on the very
    # tick that re-selects it.
    #
    # Spends nothing: no SDK auth, no ExecutionClaim acquire, no `attempt`
    # lease, no clone -- the whole point is to refuse before any of that,
    # not after.
    verdict = read_source_issue(
        _load_status(ref.status_path), stage="admission", gh_api_json=_gh_api_json
    )
    if verdict.linked and not verdict.known:
        # GitHub did not answer. Not evidence about the proposal -- the
        # same rule the shepherd's `linked and not known` guard follows.
        # Leave it `accepted` and untouched; do not charge the batch
        # budget for a GitHub blip.
        return ImplementResult(
            ref=ref,
            pr_url=None,
            skipped_reason="source issue unreadable; deferring",
            counts_toward_limit=False,
        )
    if verdict.failure:
        message = _stale_source_message(ref, verdict)
        recorded = _mark_needs_triage(
            ref,
            code=verdict.failure["code"],
            stage="admission",
            message=message,
        )
        return ImplementResult(
            ref=ref,
            pr_url=None,
            error=_triage_error(message, recorded),
            counts_toward_limit=False,
            stale_source=(verdict.failure["code"], verdict.issue_ref or "?"),
        )
    # `not verdict.linked` (no usable source block -- incident-responder
    # shape) or the issue is open: nothing to refuse, fall through to the
    # model exactly as today.

    try:
        ensure_auth_for_sdk()
    except (Exception, SystemExit) as exc:
        # Auth is workflow-global, not proposal-specific. Leave the proposal
        # accepted and abort the run so later ticks keep selecting this same
        # item instead of draining the entire queue into needs-triage.
        raise SystemExit(f"SDK authentication failed: {exc}") from exc

    # Never uuid.uuid4() (ADR-010 §8): a random id is non-deterministic in
    # exactly the retried-pod case where determinism matters.
    attempt_id = _resolve_attempt_id(ref.service, ref.slug, owner_epoch=0, attempt_ordinal=0)

    # ExecutionClaim beside the yaml lease, dual-written through `enforce`
    # (ADR-010 phase 2, #352). BEFORE the `in-progress` write, not after: a
    # refused acquire means another executor is working this proposal right
    # now, and the two writes this function would otherwise have already made
    # — the status flip and the 130-minute yaml lease — would stamp our
    # identity over theirs before we stood down.
    #
    # Below `enforce` this still cannot block: the claim is advisory there and
    # `_acquire_claim` returns None.
    #
    # The order has a known cost, accepted deliberately: between a granted
    # acquire and the `in-progress` write below, this process holds a claim
    # that no `.status.yaml` records. A SIGKILL in that window (eviction, OOM,
    # node drain) leaves an active claim with no `attempt` block, so
    # `_attempt_is_fresh` — which reads only the yaml — answers "not held",
    # the shepherd re-invokes, and every re-invocation is refused by the
    # orphan claim until its lease expires. Bounded by that lease and by a
    # window of two writes; the alternative ordering trades it for stamping
    # our identity over a live holder, which is unbounded and silent (claude
    # P3 on `31232dc`).
    try:
        claim_ctx = _acquire_claim(
            EntityRef.for_proposal(ref.service, ref.slug),
            PHASE_IMPLEMENT,
            "",
            attempt_id,
            lease_seconds=_claim_lease_seconds(
                "LIFECYCLE_CLAIM_LEASE_SECONDS_IMPLEMENT", IMPLEMENT_ATTEMPT_LEASE
            ),
            proposal_ref=f"{ref.service}/{ref.slug}",
        )
    except (ImplementerFenced, ImplementerClaimRefused) as exc:
        # A skip, not a failure, and explicitly NOT `_mark_needs_triage`: the
        # entity is healthy and held by someone who is running it. Leave the
        # proposal `accepted` and untouched so the holder's own run writes the
        # only status this proposal gets, and do not charge the batch budget
        # for a race this attempt did not cause.
        return ImplementResult(
            ref=ref,
            pr_url=None,
            skipped_reason=str(exc),
            counts_toward_limit=False,
        )

    started = datetime.now(UTC)
    attempt = {
        "id": attempt_id,
        "started_at": started.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "expires_at": (
            started + IMPLEMENT_ATTEMPT_LEASE
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    # Mark in-progress only after GitHub proves there is no prior result AND
    # the claim was not refused above.
    update_status_yaml(ref, "in-progress", attempt=attempt, failure=None, blocked=None)

    target = None
    result: ImplementResult | None = None
    try:
        # 2. Clone target sibling repo.
        target = _clone_target(ref.service, ref.slug)

        # 3. Branch.
        branch = f"feat/agents-{ref.slug}"
        _run(["git", "checkout", "-b", branch], cwd=target)

        # 4. Drop the implementer sub-agent into the clone's .claude/.
        _stage_implementer_agent(target, ref.service)

        # 4b. Resolve this run's ServiceSkillBundle (mctlhq/mctl-agents#305,
        # R24) BEFORE the SDK call — a ServiceSkillError aborts here, before
        # any commit, push, or PR (R22).
        try:
            skill_bundle = _resolve_implementer_service_skills(target, branch)
        except ServiceSkillError as exc:
            recorded = _mark_needs_triage(
                ref,
                code="service-skill-error",
                stage="service-skills",
                message=f"service skill resolution failed: {exc}",
                attempt=attempt,
                claim_context=claim_ctx,
            )
            return ImplementResult(
                ref=ref,
                pr_url=None,
                error=_triage_error(f"service skill resolution failed: {exc}", recorded),
            )

        # 5. Run the SDK with PROPOSAL_DIR pointing at the gitops worktree.
        # `budget_ledger` (mctl-agents#430): the same per-command deadline
        # guard `review_feedback_one` wires in -- the boundary this proposal
        # draws is generic over the driver, not review-remediation-only.
        prompt = _build_prompt(
            ref, service_skills_block=skill_bundle.to_prompt_block(repo_slug=f"mctlhq/{ref.service}")
        )
        budget_ledger = CommandBudgetLedger()
        anyio.run(
            functools.partial(_run_implementer_agent, budget_ledger=budget_ledger),
            target, prompt, ref.proposal_dir.resolve(),
        )
        print(f"info: command budget ledger: {budget_ledger.describe()}")

        # 6. Did the agent actually commit something?
        if not _has_new_commits(target):
            if budget_ledger.exhausted:
                prior_handbacks = int(
                    _load_status(ref.status_path).get("budget_handbacks", 0) or 0
                )
                # mctl-agents#430: the ORCHESTRATOR's own ledger -- not model
                # prose -- observed the per-command budget run out with
                # nothing committed. That is a fact about THIS RUN's envelope,
                # not about the proposal, so it must not be charged to the
                # proposal: `needs-triage` is terminal by contract (a retry
                # needs an operator-reviewed gitops change moving it back to
                # `accepted`), and parking a perfectly good proposal there
                # for a busy runner is exactly the misattribution this PR
                # removes on the review path and left in place here (claude
                # P2 on `630ac27`). Hand back instead: release the claim and
                # restore `accepted` under the same compare-and-swap the
                # claim-vanished arm uses, so the next attempt re-runs
                # against the current world.
                if prior_handbacks + 1 >= IMPLEMENT_MAX_BUDGET_HANDBACKS:
                    # The hand-back budget is spent. Blamelessness does not
                    # mean "retry forever at cost": record it terminally, but
                    # under its OWN code so the proposal's history still says
                    # "the runner ran out of budget", not "this proposal
                    # produces no commits".
                    exhausted_msg = (
                        "verification budget exhausted on "
                        f"{prior_handbacks + 1} consecutive attempts "
                        f"(limit {IMPLEMENT_MAX_BUDGET_HANDBACKS}): "
                        f"{budget_ledger.describe()}"
                    )
                    recorded = _mark_needs_triage(
                        ref,
                        code="verification-budget-exhausted",
                        stage="agent",
                        message=exhausted_msg,
                        attempt=attempt,
                        claim_context=claim_ctx,
                        # The operator gate out of `needs-triage` is a
                        # deliberate human decision to try again, so it must
                        # start from a clean budget. `_mark_needs_triage`
                        # preserves unrelated fields, so without this the next
                        # run to exhaust its budget would go terminal at once
                        # with zero hand-backs, printing "consecutive
                        # attempts" on what is really the first (claude P3 on
                        # `8465c6e`).
                        extra_fields={"budget_handbacks": None},
                    )
                    return ImplementResult(
                        ref=ref,
                        pr_url=None,
                        error=_triage_error(exhausted_msg, recorded),
                        budget_terminal=recorded,
                        budget_ledger=budget_ledger,
                    )
                message = (
                    "implementer produced no commits: "
                    f"{budget_ledger.describe()} "
                    f"(attempt {prior_handbacks + 1} of "
                    f"{IMPLEMENT_MAX_BUDGET_HANDBACKS})"
                )
                # Status first, claim second -- the order every sibling arm
                # uses (`_mark_needs_triage`, the `implemented` write). The
                # reverse frees mutual exclusion while `.status.yaml` still
                # names our live attempt, so a second executor can acquire
                # the claim inside that window and either lose its own write
                # to our hand-back or make our compare-and-swap decline
                # (agy P2 on `61595a0`).
                handed_back = _hand_back_if_still_ours(
                    ref, attempt_id, budget_handbacks=prior_handbacks + 1
                )
                _release_claim(claim_ctx, reason="agent: verification budget exhausted")
                if not handed_back:
                    # The CAS declined -- somebody else's attempt is in the
                    # file, so nothing was handed back and the next tick will
                    # not retry it. Two different outcomes must not read
                    # identically in the batch summary.
                    message = (
                        f"{message} (left as-is for the attempt that now holds it)"
                    )
                return ImplementResult(
                    ref=ref,
                    pr_url=None,
                    error=f"{VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX} {message}",
                    budget_handback=handed_back,
                    budget_ledger=budget_ledger,
                )
            recorded = _mark_needs_triage(
                ref,
                code="no-commits",
                stage="agent",
                message="implementer produced no commits",
                attempt=attempt,
                claim_context=claim_ctx,
            )
            return ImplementResult(
                ref=ref,
                pr_url=None,
                error=_triage_error("implementer produced no commits", recorded),
            )

        # 6b. Chart MAJOR-version guard — the long-tail timebomb from the
        # 2026-05-01 ESO incident (external-secrets 0.10.x → 2.x). Helm
        # doesn't refresh CRDs on upgrade, so a MAJOR bump can leave a
        # controller pinned to CRD versions the cluster doesn't serve.
        # Block the push unless the proposal explicitly opts in via a
        # `crd-migration*` marker file. The proposal author owns the
        # migration plan; the implementer just enforces that they've
        # written one down.
        bumps = _detect_chart_major_bumps(target)
        if bumps and not _proposal_acks_crd_migration(ref.proposal_dir):
            bump_summary = "; ".join(f"{f}: {old} → {new}" for f, old, new in bumps)
            msg = (
                "chart MAJOR-version bump detected without CRD-migration "
                f"acknowledgement: {bump_summary}. Add a 'crd-migration*' "
                "file (e.g. crd-migration-plan.md) to the proposal "
                f"directory '{ref.proposal_dir}' describing how the CRDs "
                "will be pre-staged or the migration sequenced. See "
                "feedback_eso_chart_2x_crd_lifecycle.md."
            )
            recorded = _mark_needs_triage(
                ref,
                code="chart-major-migration-required",
                stage="policy",
                message=msg,
                attempt=attempt,
                claim_context=claim_ctx,
            )
            return ImplementResult(ref=ref, pr_url=None, error=_triage_error(msg, recorded))

        # 7. Push + PR.
        pr_url = _push_and_open_pr(target, ref, claim_context=claim_ctx)

        # 8. Mark implemented.
        completed_attempt = dict(attempt)
        completed_attempt["finished_at"] = _now_iso()
        update_status_yaml(
            ref,
            "implemented",
            pr=pr_url,
            attempt=completed_attempt,
            failure=None,
            notes=None,
            # Read-only annotation, never approval/authorization (ADR 011):
            # overrides the investigator's own execution: block with THIS
            # attempt's identity, since a fresh implementer run produced
            # this transition.
            execution={
                "context_id": execution_context.context_id,
                "trace_id": execution_context.trace_id,
                "agent": execution_context.executor.agent or "implementer",
                "version": execution_context.executor.version,
            },
            # A run that got through clears the hand-back tally: the cap
            # bounds CONSECUTIVE budget-exhausted attempts, not the lifetime
            # of the proposal (mctl-agents#430).
            budget_handbacks=None,
        )
        _release_claim(claim_ctx, reason="implemented")
        result = ImplementResult(ref=ref, pr_url=pr_url)
        return result

    except ImplementerFenced as e:
        # Not a failure of the proposal: the world this attempt was pinned to
        # moved on. Left with a distinct triage code so a race is legible
        # instead of landing in the generic `unexpected-error` arm.
        msg = str(e)
        recorded = _mark_needs_triage(
            ref,
            code="fenced",
            stage="push",
            message=msg,
            attempt=attempt,
            claim_context=claim_ctx,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=_triage_error(msg, recorded))
        return result
    except ImplementerClaimRefused as e:
        # The push-site claim check refused this attempt: a skip, not a
        # failure, and explicitly NOT `_mark_needs_triage` — the exception's
        # own docstring forbids that outcome, and without this arm the refusal
        # fell through to the generic `except Exception` and was recorded as
        # `unexpected-error`/`agent`, i.e. a crash in the proposal's history
        # (claude + agy P2 on `d5e2a48`).
        #
        # It DOES charge the batch budget, unlike the acquire-site arm this
        # was first copied from. Every `counts_toward_limit=False` in this
        # module is a pre-SDK exit — approval blocked, preflight failed,
        # acquire refused — where nothing was spent. Here a full model pass
        # has already run, and `_implement_refs` charges on that flag alone:
        # leaving it False lets one mctl-api blip per proposal run the model
        # again down the whole accepted queue, which is the subscription-usage
        # multiplication `_max_proposals_error` exists to prevent
        # (incident-7eb12290, claude P2 on `c29195c`).
        #
        # What happens to `.status.yaml` depends on WHICH refusal this is;
        # the three are not one situation (claude P3 on `c29195c`):
        msg = str(e)
        if e.entity_is_free:
            # `released` or `expired`, and nothing else: nobody holds it, and
            # this attempt's own lease is the likeliest reason (nothing renews
            # it mid-run). Parking the proposal `in-progress` would cost the
            # rest of a 130-minute lease for a hold that does not exist, so
            # hand it back: release best-effort and restore `accepted` so the
            # next tick re-acquires and re-runs against the current world.
            # Nothing was pushed, so there is nothing to reconcile. The
            # restore is a compare-and-swap — see `_hand_back_if_still_ours`.
            _release_claim(claim_ctx, reason="claim vanished mid-run")
            if not _hand_back_if_still_ours(ref, attempt_id):
                # The CAS declined: somebody else's attempt is in the file, so
                # the proposal was NOT handed back and the next tick will not
                # retry it. Two different outcomes must not read identically
                # in the batch summary (claude P3 on `0808376`).
                msg = f"{msg} (left `in-progress` for the attempt that now holds it)"
        elif e.verdict == CLAIM_UNCLAIMED:
            # CLAIM_UNCLAIMED on a `fenced` record, or on no record at all.
            # `FREE_CLAIM_STATES` calls a fence free because acquiring over one
            # is legal; deciding this proposal's status on it is not. A fence
            # is somebody ELSE's write — the newer executor that fenced us is
            # running right now and owns the `attempt` block — and an answer
            # that named no claim proves nothing either way. Same handling as a
            # live holder: leave the status alone (claude P2 on `af661d7`).
            # The claim is still released: our claim_id names our own fenced
            # record, so the call frees nothing of theirs.
            _release_claim(claim_ctx, reason="claim fenced or unreadable mid-run")
        elif e.verdict == CLAIM_UNKNOWN:
            # The store could not answer. Fail closed: leave `in-progress`
            # and let the yaml lease expire on its own rather than hand the
            # entity to a second executor on the strength of an outage. The
            # claim is deliberately NOT released — a release we cannot
            # confirm is how an outage frees a live hold.
            pass
        else:
            # CLAIM_HELD_BY_OTHER: a real, named holder is running this now,
            # and their run writes the only status this proposal gets.
            # Rewriting it back to `accepted` from here would clobber their
            # `in-progress` and invite the second run this contract exists to
            # prevent.
            pass
        return ImplementResult(ref=ref, pr_url=None, skipped_reason=msg)
    except ImplementerOrphanedSubagent as e:
        # Batch mode has no review-attempt budget, so it needs no sentinel exit
        # code — but it does need its own triage code, otherwise this lands in
        # the generic `unexpected-error` arm below and a harness failure is
        # indistinguishable from a crash in the proposal's history.
        msg = str(e)
        recorded = _mark_needs_triage(
            ref,
            code="orphaned-subagent",
            stage="runtime",
            message=msg,
            attempt=attempt,
            claim_context=claim_ctx,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=_triage_error(msg, recorded))
        return result
    except ImplementerOperationTimeout as e:
        msg = f"operation timed out: {e}"
        recorded = _mark_needs_triage(
            ref,
            code="operation-timeout",
            stage="runtime",
            message=msg,
            attempt=attempt,
            claim_context=claim_ctx,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=_triage_error(msg, recorded))
        return result
    except subprocess.CalledProcessError as e:
        msg = f"shell step failed: {' '.join(e.cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}"
        recorded = _mark_needs_triage(
            ref,
            code="shell-failed",
            stage="shell",
            message=msg,
            attempt=attempt,
            claim_context=claim_ctx,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=_triage_error(msg, recorded))
        return result
    except SystemExit as e:
        # _stage_implementer_agent and a few other helpers raise SystemExit
        # for unrecoverable config errors (e.g. missing implementer.md for
        # a service that's in SERVICES but has no agents/<svc>/.claude/
        # tree yet). Catching SystemExit alongside Exception here keeps
        # one bad proposal from killing a multi-proposal run mid-pipeline.
        msg = f"SystemExit: {e}"
        recorded = _mark_needs_triage(
            ref,
            code="configuration-error",
            stage="agent",
            message=msg,
            attempt=attempt,
            claim_context=claim_ctx,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=_triage_error(msg, recorded))
        return result
    except Exception as e:  # pragma: no cover — defensive  # noqa: BLE001 — surfaces as a result, not a crash
        msg = f"{type(e).__name__}: {e}"
        recorded = _mark_needs_triage(
            ref,
            code="unexpected-error",
            stage="agent",
            message=msg,
            attempt=attempt,
            claim_context=claim_ctx,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=_triage_error(msg, recorded))
        return result
    finally:
        # Keep target dir for post-mortem on failure; clean only on success.
        if target and target.exists() and result is not None and result.pr_url is not None:
            try:
                shutil.rmtree(target)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _implement_refs(
    refs: list[ProposalRef],
    *,
    max_proposals: int,
    dry_run: bool,
) -> list[ImplementResult]:
    """Run a bounded batch without letting failed preflights starve the queue."""
    results: list[ImplementResult] = []
    handled = 0
    for ref in refs:
        label = f"{ref.service}/{ref.slug}"
        print(f"\n=== [{label}] Implementing ===")
        outcome = "aborted"
        try:
            result = implement_one(ref, dry_run=dry_run)
            if result.error:
                outcome = "failed"
            elif result.blocked:
                outcome = "blocked"
            elif result.skipped_reason:
                outcome = "skipped"
            elif result.pr_url:
                outcome = "ready"
            else:
                outcome = "failed"
        finally:
            # Workflow-global failures (for example SDK auth) deliberately
            # propagate out of ``implement_one``. Still close the attributable
            # progress span so operators can see which proposal aborted.
            print(f"=== [{label}] Finished: {outcome} ===")
        results.append(result)
        if result.counts_toward_limit:
            handled += 1
        if max_proposals and handled >= max_proposals:
            break
    return results


def _batch_outcome(results: list[ImplementResult]) -> BatchOutcome:
    """Classify every result; partial success must never mask a failure.

    Order matters: stale_source -> verification budget -> error -> blocked ->
    skipped_reason -> pr_url. A stale-source refusal also carries `error` (for readers of
    that older channel, and for the per-result "fail" summary line), so it
    must be classified before the `error` branch or a healthy admission
    refusal would count toward `failed` and force the whole batch red even
    when another proposal in the same tick succeeded (codex P2 follow-up on
    mctl-agents#410). A blocked result also carries a `skipped_reason` (for
    readers of that channel alone), so it must be classified before the
    `skipped_reason` branch or it would inflate the skip count.
    """
    succeeded = failed = skipped = blocked = stale_source = 0
    verification_budget = 0
    for result in results:
        if result.stale_source:
            stale_source += 1
        elif result.budget_handback or result.budget_terminal:
            verification_budget += 1
        elif result.error:
            failed += 1
        elif result.blocked:
            blocked += 1
        elif result.skipped_reason:
            skipped += 1
        elif result.pr_url:
            succeeded += 1
        else:
            failed += 1
    return BatchOutcome(
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        blocked=blocked,
        stale_source=stale_source,
        verification_budget=verification_budget,
    )


def _max_proposals_error(max_proposals: int, dry_run: bool) -> str | None:
    """Return a policy error for an unsafe executable batch size.

    Implementations are deliberately serial and limited to one proposal per
    run so a queue cannot multiply subscription usage. Unlimited discovery is
    still useful and safe in dry-run mode (incident-7eb12290).
    """
    if max_proposals < 0:
        return "--max-proposals must be zero or positive"
    if not dry_run and max_proposals != 1:
        return "--max-proposals must be 1 for executable runs (0 is dry-run only)"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Tier 2 implementer — open PRs from accepted proposals")
    ap.add_argument("--service", default="", help=f"Filter by service (one of: {', '.join(SERVICES)})")
    ap.add_argument("--slug", default="", help="Filter by proposal slug")
    ap.add_argument(
        "--max-proposals",
        type=int,
        default=int(os.getenv("IMPLEMENTER_MAX_PROPOSALS", "1")),
        help=(
            "Maximum accepted proposals per run (execution is fixed at 1; "
            "0 is allowed only with --dry-run)"
        ),
    )
    ap.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Path to platform-gitops/agents-state/ (defaults to STATE_DIR env)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Discover only; don't clone or run the SDK")
    ap.add_argument(
        "--review-feedback",
        default="",
        metavar="PATH",
        help=(
            "Path to a JSON bundle of codex P1/P2 findings written by the "
            "Tier 3 shepherd. When set, the implementer drives the EXISTING "
            "feat/agents-<slug> branch — fetches it, lets the sub-agent "
            "address findings, and pushes a follow-up commit (no new PR). "
            "Requires --service AND --slug."
        ),
    )
    ap.add_argument(
        "--refusal-out",
        default="",
        metavar="PATH",
        help=(
            "Where to write the refusal reason as JSON when a "
            "--review-feedback run ends in a deliberate no-op (exit "
            f"{EXIT_DELIBERATE_NO_OP}). The Tier 3 shepherd passes a temp "
            "path and reads the reason back into `.status.yaml` notes and "
            "its run summary (mctl-agents#360). The reason is also printed, "
            "so omitting this only costs the shepherd the structured copy."
        ),
    )
    ap.add_argument(
        "--adopted-pr",
        default="",
        metavar="URL",
        help=(
            "GitHub PR URL of a proposal-less, adopted PR (see "
            "orchestrator.pr_adoption, mctlhq/mctl-agents#334). Valid only "
            "together with --review-feedback; mutually exclusive with "
            "--slug. Drives the PR's OWN head branch instead of "
            "feat/agents-<slug>. Never adopts a PR itself — only the "
            "shepherd's discovery pass does; a missing adoption record "
            "exits 2."
        ),
    )
    args = ap.parse_args()

    if args.service and args.service not in SERVICES:
        print(f"Unknown service '{args.service}'. Available: {', '.join(SERVICES)}", file=sys.stderr)
        sys.exit(2)

    if args.adopted_pr and args.slug:
        print("--adopted-pr is mutually exclusive with --slug", file=sys.stderr)
        sys.exit(2)

    if args.adopted_pr and not args.review_feedback:
        print("--adopted-pr is only valid together with --review-feedback", file=sys.stderr)
        sys.exit(2)

    state_dir = Path(args.state_dir)

    # Review-feedback mode: drive the existing PR's branch with codex findings.
    if args.review_feedback:
        ensure_auth_for_sdk()
        bundle = _load_review_feedback(Path(args.review_feedback))

        if args.adopted_pr:
            if not args.service:
                print("--adopted-pr requires --service", file=sys.stderr)
                sys.exit(2)
            parsed_adopted = _parse_pr_url(args.adopted_pr)
            if parsed_adopted is None:
                print(f"--adopted-pr is not a GitHub PR URL: {args.adopted_pr!r}", file=sys.stderr)
                sys.exit(2)
            adopted_repo, adopted_number = parsed_adopted
            try:
                if _adopted_pr_is_fork(adopted_repo, adopted_number):
                    print(
                        f"--adopted-pr {args.adopted_pr} is a fork PR; "
                        "refusing to clone or push to it",
                        file=sys.stderr,
                    )
                    sys.exit(2)
            except GitHubPreflightError as exc:
                print(
                    f"--adopted-pr fork check failed closed: could not "
                    f"verify {adopted_repo}#{adopted_number} is not a fork "
                    f"({exc})",
                    file=sys.stderr,
                )
                sys.exit(2)
            ref = build_adopted_ref(state_dir, args.adopted_pr)
            record = load_status(ref.status_path)
            head_branch = record.get("head_branch") or ""
            if not head_branch:
                print(
                    f"adoption record at {ref.status_path} has no head_branch; refusing",
                    file=sys.stderr,
                )
                sys.exit(2)
            result = review_feedback_one(ref, bundle, dry_run=args.dry_run, branch=head_branch)
        else:
            if not (args.service and args.slug):
                print(
                    "--review-feedback requires --service AND --slug "
                    "(the shepherd always passes both), or --adopted-pr "
                    "instead of --slug.",
                    file=sys.stderr,
                )
                sys.exit(2)
            # In review-feedback mode the proposal is post-implementation —
            # status is `implemented` or `review-fixing`. Look it up under
            # those statuses rather than `accepted`.
            refs = find_accepted_proposals(
                state_dir,
                service_filter=args.service,
                slug_filter=args.slug,
                statuses={"implemented", "review-fixing"},
            )
            if not refs:
                print(
                    f"No proposal {args.service}/{args.slug} in implemented/review-fixing status; "
                    f"refusing to apply review feedback.",
                    file=sys.stderr,
                )
                sys.exit(1)
            result = review_feedback_one(refs[0], bundle, dry_run=args.dry_run)
        print("\n=== Review-feedback summary ===")
        if result.error:
            print(f"  fail {result.ref.service}/{result.ref.slug}: {result.error}")
            # Map deterministic content failures to sentinel exit codes so
            # the Tier 3 shepherd can stop retrying them forever (see
            # `_review_feedback_exit_code` docstring). Transient plumbing
            # errors continue to exit 1 so the shepherd's transient-failure
            # path is unchanged.
            code = _review_feedback_exit_code(result.error)
            if args.refusal_out:
                if code == EXIT_DELIBERATE_NO_OP:
                    _write_refusal_out(
                        Path(args.refusal_out),
                        result.error[len(REFUSAL_ERROR_PREFIX):].strip(),
                    )
                elif code == EXIT_CI_EVIDENCE_INSUFFICIENT:
                    # mctl-agents#423 review P2: `result.error` already carries
                    # the agent's reason (see the insufficient_evidence branch
                    # above), but this write used to be gated to
                    # EXIT_DELIBERATE_NO_OP only, so it never reached
                    # `--refusal-out` and the reason was silently dropped.
                    _write_refusal_out(
                        Path(args.refusal_out),
                        result.error[len(CI_EVIDENCE_INSUFFICIENT_ERROR_PREFIX):].strip(),
                    )
                elif code == EXIT_VERIFICATION_BUDGET_EXHAUSTED:
                    # mctl-agents#430: the structured ledger summary (clamp/
                    # deny counts, not just the reason text) so the shepherd
                    # -- and an operator reading the log -- can tell a busy
                    # runner apart from a reserve tuned too tight.
                    _write_verification_budget_exhausted_out(
                        Path(args.refusal_out),
                        result.error[len(VERIFICATION_BUDGET_EXHAUSTED_ERROR_PREFIX):].strip(),
                        result.budget_ledger,
                    )
            sys.exit(code)
        if result.skipped_reason:
            print(f"  skip {result.ref.service}/{result.ref.slug}: {result.skipped_reason}")
            return
        print(f"  ok   {result.ref.service}/{result.ref.slug} -> {result.pr_url or '(existing PR)'}")
        return

    if policy_error := _max_proposals_error(args.max_proposals, args.dry_run):
        print(policy_error, file=sys.stderr)
        sys.exit(2)

    refs = find_accepted_proposals(
        state_dir,
        service_filter=args.service or None,
        slug_filter=args.slug or None,
    )
    if not refs:
        print("info: no accepted proposals found.")
        return

    print(f"Found {len(refs)} accepted proposal(s):")
    for r in refs:
        print(f"  - {r.service}/{r.slug}")

    results = _implement_refs(
        refs,
        max_proposals=args.max_proposals,
        dry_run=args.dry_run,
    )

    print("\n=== Summary ===")
    for result in results:
        if result.budget_handback or result.budget_terminal:
            # Not a `fail` line: these arms are excluded from `outcome.failed`
            # below, and printing them as failures made a proposal retired
            # cleanly under the cap read one way per result and the opposite
            # way in `Totals:` (claude P3 on `68f3a05`).
            print(f"  budget {result.ref.service}/{result.ref.slug}: {result.error}")
        elif result.error:
            print(f"  fail {result.ref.service}/{result.ref.slug}: {result.error}")
        elif result.blocked:
            print(f"  blocked {result.ref.service}/{result.ref.slug}: {result.skipped_reason}")
        elif result.skipped_reason:
            print(f"  skip {result.ref.service}/{result.ref.slug}: {result.skipped_reason}")
        elif result.pr_url:
            print(f"  ok   {result.ref.service}/{result.ref.slug} -> {result.pr_url}")
        else:
            print(f"  fail {result.ref.service}/{result.ref.slug}: {result.error}")

    blocked_results = [r for r in results if r.blocked]
    if blocked_results:
        print("\n=== Blocked ===")
        for result in blocked_results:
            print(f"  {result.ref.service}/{result.ref.slug}: {result.blocked}")

    stale_sources = [(r, r.stale_source) for r in results if r.stale_source]
    if stale_sources:
        print("\n=== Stale source ===")
        for result, (stale_code, issue_ref) in stale_sources:
            print(f"  {result.ref.service}/{result.ref.slug}: {stale_code} {issue_ref}")

    budget_results = [r for r in results if r.budget_handback or r.budget_terminal]
    if budget_results:
        print("\n=== Verification budget ===")
        for result in budget_results:
            print(f"  {result.ref.service}/{result.ref.slug}: {result.error}")

    outcome = _batch_outcome(results)
    print(
        "Totals: "
        f"{outcome.succeeded} succeeded, "
        f"{outcome.failed} failed, "
        f"{outcome.skipped} skipped, "
        f"{outcome.blocked} blocked, "
        f"{outcome.stale_source} stale source, "
        f"{outcome.verification_budget} verification budget"
    )
    if outcome.failed:
        sys.exit(1)
    # A refusal-only batch's `needs-triage` write IS the retirement -- unlike
    # `blocked`'s idempotent diagnostic marker, there is no "same content
    # next tick" safety net if it never lands. The write only exists on disk
    # in this step; turning it into an actual gitops commit happens in the
    # downstream commit-and-push step. Per the EXIT_BLOCKED_ONLY note above,
    # the CWFT does not special-case any sentinel exit code today -- both its
    # `when` gates compare Argo step status strings, so ANY non-zero exit
    # here marks `implement` Failed and can skip that commit, leaving the
    # proposal `accepted` so the next tick re-selects it, re-reads GitHub,
    # and repeats forever -- precisely the loop this gate exists to end
    # (codex P2 follow-up on mctl-agents#410, PR #416). Exit 0 here so the
    # write is never put at risk; the `=== Stale source ===` section above
    # plus the committed `.status.yaml` already carry the signal for
    # operators, the same tradeoff already made for a mixed batch above.
    # `outcome.stale_source` is deliberately excluded from `outcome.failed`
    # above for the same reason. `outcome.verification_budget` is excluded on
    # exactly the same grounds (mctl-agents#430): that arm hands the proposal
    # back to `accepted` with an incremented `budget_handbacks`, and that
    # tally is the ONLY durable bound on the paid retry loop. If a non-zero
    # exit here marked `implement` Failed and skipped the commit step, every
    # tick would read `0` and the cap would never be reached -- reintroducing
    # precisely the unbounded loop the cap was added to close. The cap's own
    # terminal write is bucketed there too: it is the write that ENDS the
    # loop, and it has no catch-up path -- a dropped hand-back is recovered by
    # the next green tick, a dropped terminal write is re-attempted and
    # re-dropped forever, at full model cost every time.
    # A blocked-only run (no successful implementation to hand a durable
    # .status.yaml -> PR write off to the commit step) is a louder signal
    # than a plain skip -- see EXIT_BLOCKED_ONLY above. A run that also
    # succeeded exits 0 so real work is never reported red; --dry-run never
    # writes the marker and never changes today's exit-code behaviour. This
    # only fires on the tick that newly wrote or changed the marker
    # (`blocked_is_new`); a subsequent tick that finds the identical,
    # already-recorded block on disk returns 0 instead, so a permanently
    # blocked proposal produces exactly one failed Argo run, not one every
    # ~30 minutes forever. The durable `.status.yaml` marker and the
    # `=== Blocked ===` summary above still make the state visible on every
    # run regardless of exit code.
    if (
        outcome.blocked
        and not outcome.succeeded
        and not args.dry_run
        and any(r.blocked_is_new for r in results)
    ):
        sys.exit(EXIT_BLOCKED_ONLY)


def _traced_main() -> None:
    """`main()` under the pod's root span (mctl-agents#195).

    Parented on `TRACEPARENT` when the CWFT passes one, so this pod's spans
    join the Temporal DevLoop trace that submitted it. Inert — not even an
    SDK import — unless the standard `OTEL_*` endpoint variables are set."""
    tracing.init_tracing("mctl-agents-implementer")
    with tracing.pod_root_span("implementer.run", {tracing.AGENT_NAME: "implementer"}):
        main()


if __name__ == "__main__":
    _traced_main()
