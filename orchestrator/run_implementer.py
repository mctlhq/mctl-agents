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

Usage:
    python -m orchestrator.run_implementer
    python -m orchestrator.run_implementer --service mctl-web
    python -m orchestrator.run_implementer --service mctl-web --slug wrangler-cve-0933
    python -m orchestrator.run_implementer --slug wrangler-cve-0933
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import anyio
from claude_agent_sdk import ClaudeSDKClient

from config.settings import (
    AGENTS_DIR,
    SERVICE_AGENT_MODEL,
    SERVICES,
)
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.github_token import refresh_github_token
from orchestrator.mcp_guard import ensure_mctl_connected
from orchestrator.options import (
    IMPLEMENTER_COMMAND_TIMEOUT_SECONDS,
    IMPLEMENTER_TIMEOUT_SECONDS,
    build_implementer_agent_options,
)
from orchestrator.proposal_state import load_status, now_iso, update_status_file


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
# tell deterministic *content* failures apart from transient subprocess /
# auth / network plumbing failures: only the former should consume one of
# the MAX_REVIEW_ATTEMPTS budget slots and eventually flip the proposal to
# `review-stuck`. Without a sentinel, the shepherd treats every non-zero
# exit as transient and retries forever — see codex P1 on PR #12.
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

    Everything else (non-timeout shell failures, SystemExit from missing
    config, unexpected exceptions) is left as ``EXIT_GENERIC_FAILURE`` and
    the shepherd treats it as transient.
    """
    if not error:
        return EXIT_OK
    if error == "implementer produced no follow-up commits":
        return EXIT_NO_FOLLOWUP_COMMITS
    if error.startswith("branch ") and "not found on origin" in error:
        return EXIT_BRANCH_MISSING_ON_ORIGIN
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
    status_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.status_path = self.proposal_dir / ".status.yaml"


@dataclass
class ImplementResult:
    ref: ProposalRef
    pr_url: Optional[str]
    error: Optional[str] = None
    skipped_reason: Optional[str] = None
    counts_toward_limit: bool = True


@dataclass(frozen=True)
class BatchOutcome:
    succeeded: int
    failed: int
    skipped: int


@dataclass(frozen=True)
class ExistingResult:
    """Outcome of the GitHub preflight for one deterministic result branch."""

    action: str
    pr_url: Optional[str] = None
    head_sha: Optional[str] = None
    reason: Optional[str] = None


class GitHubPreflightError(RuntimeError):
    """GitHub could not be queried safely, so the model must not run."""


class ImplementerOperationTimeout(RuntimeError):
    """A bounded model or shell operation exceeded its wall-clock limit."""


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


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_accepted_proposals(
    state_dir: Path,
    service_filter: Optional[str] = None,
    slug_filter: Optional[str] = None,
    statuses: Optional[set[str]] = None,
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
            except Exception as e:
                print(f"⚠️  {service}/{slug}: failed to parse .status.yaml ({e}); skipping")
                continue
            status = data.get("status", "proposed")
            if status in accepted_states:
                refs.append(
                    ProposalRef(
                        service=service,
                        slug=slug,
                        proposal_dir=proposal_dir,
                        status=status,
                    )
                )
    return refs


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------
def _run(
    cmd: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run a bounded subprocess with consistent logging."""
    effective_timeout = (
        IMPLEMENTER_COMMAND_TIMEOUT_SECONDS if timeout is None else timeout
    )
    refresh_github_token()
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            text=True,
            capture_output=True,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ImplementerOperationTimeout(
            f"command exceeded {effective_timeout:g}s: {' '.join(cmd)}"
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


def _clone_target(service: str, slug: str) -> Path:
    """gh repo clone the target sibling repo to a fresh tmp dir."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
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
    """
    src = AGENTS_DIR / service / ".claude" / "agents" / "implementer.md"
    if not src.exists():
        # Issue-driven proposals can target any mctlhq repo, including ones
        # without an agents/<svc>/ scaffold (the scaffold only exists for the
        # proactive rotation services). Fall back to the generic implementer
        # sub-agent so Tier 2 still works for those repos.
        generic = AGENTS_DIR / "_generic" / ".claude" / "agents" / "implementer.md"
        if generic.exists():
            print(f"ℹ️  No per-service implementer for '{service}'; using generic fallback.")
            src = generic
        else:
            raise SystemExit(
                f"Implementer agent template not found: {src} "
                f"(and no generic fallback at {generic})"
            )
    dst_dir = target / ".claude" / "agents"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / "implementer.md")

    # Local-only ignore: keeps PRs scoped to the proposal's intent.
    exclude_path = target / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    entry = ".claude/agents/implementer.md"
    existing = ""
    if exclude_path.exists():
        existing = exclude_path.read_text(encoding="utf-8")
    if entry not in existing.splitlines():
        with exclude_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"{entry}\n")


def _build_prompt(ref: ProposalRef, review_feedback: Optional[dict] = None) -> str:
    """Prompt that delegates to the `implementer` sub-agent.

    The sub-agent is told (in its frontmatter and body) to read the spec
    files via $PROPOSAL_DIR, edit minimal files in cwd, run a brief sanity
    check, and `git commit` — but NOT push.

    When ``review_feedback`` is set the prompt is the follow-up variant:
    the agent is told the branch is already checked out, points to the
    existing PR, and addresses each codex finding from the bundle.
    """
    branch = f"feat/agents-{ref.slug}"

    if review_feedback is not None:
        feedback_md = _render_review_feedback(review_feedback)
        return f"""\
Tier 2 implementer follow-up for proposal `{ref.service}/{ref.slug}`.

Context:
- Branch `{branch}` is already checked out on the existing PR.
- Code review left P1/P2 findings on this PR — they are listed below.
- Spec files live at `$PROPOSAL_DIR` (env var): requirements.md, design.md, tasks.md.

Workflow:
1. Use the `implementer` sub-agent. Read the codex findings (below) and
   the relevant lines in the working tree.
2. Apply the MINIMAL change that resolves each finding. Stay in scope —
   do not refactor outside the touched files.
3. Stage and commit on the SAME branch (`{branch}`). Conventional Commits
   subject: `fix(agents): address P1/P2 codex findings on {ref.slug}`.
   Body should reference the proposal:
   `Proposal: platform-gitops/agents-state/{ref.service}/proposals/{ref.slug}/`.
4. DO NOT push and DO NOT open a PR — the orchestrator will push to the
   existing branch after you finish. The PR auto-updates because the
   head ref does not change.
5. If a finding is invalid or already addressed, STOP without committing
   and explain why in your final message.

{feedback_md}

Ground rules:
- One commit per run is fine; multiple small commits are also fine.
- Stay strictly within scope — fixing the codex findings only.
- Work ONLY inside the current working directory (the cloned target repo).
  NEVER create, edit, commit, or push files anywhere else — in particular
  the mounted gitops worktree under `/workdir`. If a finding implies a
  change in another repository, do NOT make it; describe it in your final
  message so a human can route it.
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

Ground rules:
- One commit per run is fine; multiple small commits are also fine.
- Stay strictly within the proposal's scope — no drive-by refactors.
- Work ONLY inside the current working directory (the cloned target repo).
  NEVER create, edit, commit, or push files anywhere else — in particular
  the mounted gitops worktree under `/workdir`. If the proposal asks for a
  change in another repository (e.g. alert rules or manifests in
  mctl-gitops), do NOT make it; describe it in your final message so a
  human can route it through a reviewed PR.
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


def _render_review_feedback(bundle: dict) -> str:
    """Format the JSON bundle as a Markdown section for the sub-agent."""
    summaries = bundle.get("summaries") or []
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
        lines.append("(No summaries in bundle — re-read the PR's code review on GitHub.)")
        return "\n".join(lines)

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
    return "\n".join(lines).rstrip() + "\n"


def _branch_exists_on_origin(repo_dir: Path, branch: str) -> bool:
    """True iff `git ls-remote --heads origin <branch>` returns a ref line."""
    proc = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=repo_dir, check=False)
    return bool(proc.stdout.strip())


def _push_followup(repo_dir: Path, branch: str) -> None:
    """Push the follow-up commit to the existing branch (no `-u`)."""
    _run(["git", "push", "origin", branch], cwd=repo_dir)


async def _run_implementer_agent(repo_dir: Path, prompt: str, proposal_dir: Path) -> None:
    options = build_implementer_agent_options(repo_dir, SERVICE_AGENT_MODEL, proposal_dir)
    mcp_configured = bool(options.mcp_servers)
    # A budget bounds spend but not a stalled network/model stream. Keep one
    # proposal inside the workflow's larger deadline (incident e3649b04).
    # The fail_after wraps the mctl connectivity check too — a wedged
    # handshake must not silently eat into the caller's own timeout budget.
    try:
        with anyio.fail_after(IMPLEMENTER_TIMEOUT_SECONDS):
            async with ClaudeSDKClient(options=options) as client:
                if mcp_configured:
                    # fatal=False — see orchestrator/mcp_guard.py. The
                    # implementer applies an already-written proposal via
                    # Read/Write/Edit/Bash; mctl tools are supplementary.
                    await ensure_mctl_connected(client, fatal=False)
                await client.query(prompt)
                async for message in client.receive_response():
                    print(message)
    except TimeoutError as exc:
        raise ImplementerOperationTimeout(
            f"model stream exceeded {IMPLEMENTER_TIMEOUT_SECONDS:g}s"
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
) -> ImplementResult:
    """Apply code review feedback as a follow-up commit on the existing PR.

    Pre-conditions (caller's responsibility):
    - ``feat/agents-<slug>`` exists on origin (the shepherd only invokes
      this mode after observing an open PR).
    - ``ref`` has a ``pr`` URL set in `.status.yaml` (used for logging
      only — we do not re-open a PR).

    On success: pushes a follow-up commit to the existing branch and
    returns an ``ImplementResult`` whose ``pr_url`` is the existing PR
    URL (read from `.status.yaml`). `.status.yaml` is intentionally NOT
    rewritten — the shepherd owns the status transitions for follow-ups.
    """
    if dry_run:
        print(f"[dry-run] would address review on {ref.service}/{ref.slug}")
        return ImplementResult(ref=ref, pr_url=None, skipped_reason="dry-run")

    target = None
    result: Optional[ImplementResult] = None
    branch = f"feat/agents-{ref.slug}"
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

        # 5. Run the SDK with the bundle baked into the prompt.
        prompt = _build_prompt(ref, review_feedback=bundle)
        anyio.run(_run_implementer_agent, target, prompt, ref.proposal_dir.resolve())

        # 6. Did the agent commit anything new (beyond the captured pre-SDK SHA)?
        if not _has_new_commits(target, base=old_head):
            return ImplementResult(
                ref=ref,
                pr_url=None,
                error="implementer produced no follow-up commits",
            )

        # 7. Push to the existing branch — no -u, no new PR.
        _push_followup(target, branch)

        # 8. Read the existing PR URL from `.status.yaml` for the result
        # surface; do NOT rewrite the status — that belongs to the shepherd.
        existing = _load_status(ref.status_path)
        pr_url = existing.get("pr")
        result = ImplementResult(ref=ref, pr_url=pr_url)
        return result

    except ImplementerOperationTimeout as e:
        result = ImplementResult(
            ref=ref,
            pr_url=None,
            error=f"operation timed out: {e}",
        )
        return result
    except subprocess.CalledProcessError as e:
        msg = f"shell step failed: {' '.join(e.cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}"
        result = ImplementResult(ref=ref, pr_url=None, error=msg)
        return result
    except SystemExit as e:
        result = ImplementResult(ref=ref, pr_url=None, error=f"SystemExit: {e}")
        return result
    except Exception as e:  # pragma: no cover — defensive
        result = ImplementResult(ref=ref, pr_url=None, error=f"{type(e).__name__}: {e}")
        return result
    finally:
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


def _diff_line_targets_revision(line: str) -> Optional[str]:
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
    current_file: Optional[str] = None
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
    print(f"✓ PR opened: {pr_url}")
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


def _mark_needs_triage(
    ref: ProposalRef,
    *,
    code: str,
    stage: str,
    message: str,
    pr_url: Optional[str] = None,
    attempt: Optional[dict[str, Any]] = None,
) -> None:
    fields: dict[str, Any] = {
        "failure": {
            "code": code,
            "stage": stage,
            "message": message[:2000],
        },
        "notes": f"{stage}: {message[:500]}",
    }
    if pr_url:
        fields["pr"] = pr_url
    if attempt:
        finished = dict(attempt)
        finished["finished_at"] = _now_iso()
        fields["attempt"] = finished
    update_status_yaml(ref, "needs-triage", **fields)


def _push_and_open_pr(repo_dir: Path, ref: ProposalRef) -> str:
    branch = f"feat/agents-{ref.slug}"
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

    if dry_run:
        print(f"[dry-run] would preflight then implement {ref.service}/{ref.slug}")
        return ImplementResult(ref=ref, pr_url=None, skipped_reason="dry-run")

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

    try:
        ensure_auth_for_sdk()
    except (Exception, SystemExit) as exc:
        # Auth is workflow-global, not proposal-specific. Leave the proposal
        # accepted and abort the run so later ticks keep selecting this same
        # item instead of draining the entire queue into needs-triage.
        raise SystemExit(f"SDK authentication failed: {exc}") from exc

    started = datetime.now(timezone.utc)
    attempt = {
        "id": os.getenv("WORKFLOW_UID") or str(uuid.uuid4()),
        "started_at": started.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "expires_at": (
            started + timedelta(minutes=130)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    # Mark in-progress only after GitHub proves there is no prior result.
    update_status_yaml(ref, "in-progress", attempt=attempt, failure=None)

    target = None
    result: Optional[ImplementResult] = None
    try:
        # 2. Clone target sibling repo.
        target = _clone_target(ref.service, ref.slug)

        # 3. Branch.
        branch = f"feat/agents-{ref.slug}"
        _run(["git", "checkout", "-b", branch], cwd=target)

        # 4. Drop the implementer sub-agent into the clone's .claude/.
        _stage_implementer_agent(target, ref.service)

        # 5. Run the SDK with PROPOSAL_DIR pointing at the gitops worktree.
        prompt = _build_prompt(ref)
        anyio.run(_run_implementer_agent, target, prompt, ref.proposal_dir.resolve())

        # 6. Did the agent actually commit something?
        if not _has_new_commits(target):
            _mark_needs_triage(
                ref,
                code="no-commits",
                stage="agent",
                message="implementer produced no commits",
                attempt=attempt,
            )
            return ImplementResult(
                ref=ref,
                pr_url=None,
                error="implementer produced no commits",
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
            _mark_needs_triage(
                ref,
                code="chart-major-migration-required",
                stage="policy",
                message=msg,
                attempt=attempt,
            )
            return ImplementResult(ref=ref, pr_url=None, error=msg)

        # 7. Push + PR.
        pr_url = _push_and_open_pr(target, ref)

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
        )
        result = ImplementResult(ref=ref, pr_url=pr_url)
        return result

    except ImplementerOperationTimeout as e:
        msg = f"operation timed out: {e}"
        _mark_needs_triage(
            ref,
            code="operation-timeout",
            stage="runtime",
            message=msg,
            attempt=attempt,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=msg)
        return result
    except subprocess.CalledProcessError as e:
        msg = f"shell step failed: {' '.join(e.cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}"
        _mark_needs_triage(
            ref,
            code="shell-failed",
            stage="shell",
            message=msg,
            attempt=attempt,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=msg)
        return result
    except SystemExit as e:
        # _stage_implementer_agent and a few other helpers raise SystemExit
        # for unrecoverable config errors (e.g. missing implementer.md for
        # a service that's in SERVICES but has no agents/<svc>/.claude/
        # tree yet). Catching SystemExit alongside Exception here keeps
        # one bad proposal from killing a multi-proposal run mid-pipeline.
        msg = f"SystemExit: {e}"
        _mark_needs_triage(
            ref,
            code="configuration-error",
            stage="agent",
            message=msg,
            attempt=attempt,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=msg)
        return result
    except Exception as e:  # pragma: no cover — defensive
        msg = f"{type(e).__name__}: {e}"
        _mark_needs_triage(
            ref,
            code="unexpected-error",
            stage="agent",
            message=msg,
            attempt=attempt,
        )
        result = ImplementResult(ref=ref, pr_url=None, error=msg)
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
    """Classify every result; partial success must never mask a failure."""
    succeeded = failed = skipped = 0
    for result in results:
        if result.error:
            failed += 1
        elif result.skipped_reason:
            skipped += 1
        elif result.pr_url:
            succeeded += 1
        else:
            failed += 1
    return BatchOutcome(succeeded=succeeded, failed=failed, skipped=skipped)


def _max_proposals_error(max_proposals: int, dry_run: bool) -> Optional[str]:
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
    args = ap.parse_args()

    if args.service and args.service not in SERVICES:
        print(f"Unknown service '{args.service}'. Available: {', '.join(SERVICES)}", file=sys.stderr)
        sys.exit(2)

    state_dir = Path(args.state_dir)

    # Review-feedback mode: drive the existing PR's branch with codex findings.
    if args.review_feedback:
        ensure_auth_for_sdk()
        if not (args.service and args.slug):
            print(
                "--review-feedback requires --service AND --slug "
                "(the shepherd always passes both).",
                file=sys.stderr,
            )
            sys.exit(2)
        bundle = _load_review_feedback(Path(args.review_feedback))
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
            sys.exit(_review_feedback_exit_code(result.error))
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
        print("ℹ️  No accepted proposals found.")
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
    for r in results:
        if r.error:
            print(f"  ✗ {r.ref.service}/{r.ref.slug} failed: {r.error}")
        elif r.skipped_reason:
            print(f"  · {r.ref.service}/{r.ref.slug} skipped: {r.skipped_reason}")
        elif r.pr_url:
            print(f"  ✓ {r.ref.service}/{r.ref.slug} → {r.pr_url}")
        else:
            print(f"  ✗ {r.ref.service}/{r.ref.slug} failed: {r.error}")
    outcome = _batch_outcome(results)
    print(
        "Totals: "
        f"{outcome.succeeded} succeeded, "
        f"{outcome.failed} failed, "
        f"{outcome.skipped} skipped"
    )
    if outcome.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
