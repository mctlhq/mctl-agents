"""Tier 3 PR shepherd — drive implementer-opened PRs to merge.

Pipeline per tick (every 5 minutes via CronWorkflow — added in a
follow-up gitops PR; this module is the orchestrator only):

    1. Glob agents-state/<svc>/proposals/<slug>/.status.yaml for any
       proposal whose `status` is in {implemented, review-fixing}. Both
       are non-terminal — filtering to `implemented` only would strand
       proposals that the previous tick moved to `review-fixing`.
    2. For each, read the linked PR (open, closed, OR merged — never
       filter to state=open, otherwise a human merge between ticks is
       never observed and the proposal sits in `implemented` forever).
    3. Run decide(pr, codex_review). One of: wait, address-review,
       merge, flip-to-merged, flip-to-rejected. `decide()` is a pure
       function. The 3-attempt cap on follow-up loops lives in the
       OUTER state machine (this module), not in decide().
    4. Apply the decision: merge_pr (with --match-head-commit),
       apply_followup (subprocess into run_implementer with
       --review-feedback), or update_status (terminal flip).
    5. Print `<service>/<slug>: <decision>` so the workflow log is
       greppable.

The Claude SDK is used for one specific decision: parsing review
findings into "merge-ready vs. needs-fix" and shaping the followup
prompt for the implementer when needed. The sub-agent prompt lives at
`agents/_shepherd/shepherd.md`. Everything else is deterministic
Python.

Env:
    STATE_DIR — path to platform-gitops/agents-state/ (default same as
        run_implementer.py — the workflow PVC mounts the gitops worktree
        at /workdir/mctl-gitops/...).
    SHEPHERD_BUDGET_USD — soft budget cap per tick (default 5.00).
    GITHUB_TOKEN — required for `gh api` and `gh pr merge` calls.

Usage:
    python -m orchestrator.run_shepherd
    python -m orchestrator.run_shepherd --service mctl-web
    python -m orchestrator.run_shepherd --service mctl-web --slug wrangler-cve-0933
    python -m orchestrator.run_shepherd --budget 2.00
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
from claude_agent_sdk import query

from config.settings import SERVICES, SHEPHERD_DIR, SHEPHERD_MODEL
from orchestrator import run_implementer
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.github_token import refresh_github_token
from orchestrator.options import SHEPHERD_BUDGET_USD, build_shepherd_options
from orchestrator.proc import run_capturing
from orchestrator.proposal_state import load_status, now_iso, update_status_file
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL

# ---------------------------------------------------------------------------
# State directory resolution — mirrors run_implementer.py.
# ---------------------------------------------------------------------------
DEFAULT_STATE_DIR = Path(
    os.getenv(
        "STATE_DIR",
        "/workdir/mctl-gitops/platform-gitops/agents-state",
    )
)

# Claude review bot login (the actor on review/comment/reaction events).
REVIEW_BOT = "claude[bot]"
# Codex Review connector — the org-wide "Auto review" bot. Its trigger is
# best-effort (does NOT fire on every push), so it never drives
# has_responded; when it HAS posted P1/P2 findings on the current head they
# gate the merge exactly like claude[bot]'s (org policy: 0 unaddressed
# P1/P2 from whichever bots actually reviewed). See #67.
CODEX_CONNECTOR_BOT = "chatgpt-codex-connector[bot]"
GATING_BOTS = (REVIEW_BOT, CODEX_CONNECTOR_BOT)

# Sweeper ownership (#213 / ADR-006 §6.1): a proposal whose DevLoopWorkflow
# is still Running drives its own review loop in-workflow; the cron sweep
# must not double-drive it. Liveness is read through mctl-api's describe
# endpoint (this pod deliberately holds no Temporal client). Any failure —
# missing token, endpoint absent (older mctl-api 404s the route), network
# error — means "not owned": the sweeper keeps today's behavior of driving
# everything, which is the safe default for a safety net.
# Same env var the Temporal activities read (orchestrator/temporal/mctl_client
# — imported for the constant only; that module pulls in nothing heavier
# than os). A deployment pointing MCTL_API_BASE_URL at a staging API must
# not have this one check silently talk to production instead.
MCTL_API_URL = MCTL_API_BASE_URL.rstrip("/")
DEV_LOOP_LIVENESS_TIMEOUT_S = 10

# The sweep tick runs every 5 minutes (see the module docstring), so the
# ownership pass must finish well inside that window no matter how many
# proposals are open or how badly mctl-api is degraded. Serially, N slow
# proposals cost N*10s and overlapping ticks would pile load onto an
# already-sick API. Two bounds together make the worst case independent
# of N: a small thread pool, and a hard wall-clock budget after which the
# remaining refs are kept unchecked (fail-open, same as any other failure).
def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """Opener that surfaces a 3xx as an HTTPError instead of following it."""

    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirects)


DEV_LOOP_LIVENESS_WORKERS = 8
DEV_LOOP_LIVENESS_BUDGET_S = 60


def _dev_loop_owns(service: str, slug: str) -> bool:
    """True iff a RUNNING DevLoopWorkflow drives this proposal (#213).

    The workflow id is derived exactly the way start.py derives it
    (dev-loop-mctlhq-{service}-{issue-number}); slugs without the
    issue-<N>- prefix (incident-*, pre-Temporal) never had a DevLoop.
    Every failure path returns False — see the MCTL_API_URL note above.
    """
    m = re.match(r"issue-(\d+)-", slug)
    if not m:
        return False
    token = os.environ.get("MCTL_TOKEN", "").strip()
    if not token:
        return False
    # `mctlhq` is hardcoded because a ProposalRef carries no repo owner —
    # unlike orphans.py, which derives one from `pr.repo`. Every proposal the
    # shepherd sweeps lives under this org today; a wrong owner would only
    # produce a 404, i.e. the fail-open "not owned" path.
    workflow_id = f"dev-loop-mctlhq-{service}-{m.group(1)}"
    url = f"{MCTL_API_URL}/api/v1/agents/dev-loop/{workflow_id}"
    if not url.startswith("https://"):
        # MCTL_API_URL is operator-provided env; refuse non-https schemes
        # (also satisfies ruff S310's audited-scheme requirement).
        print(f"warn: dev-loop liveness check skipped: non-https MCTL_API_URL {MCTL_API_URL}")
        return False
    try:
        # Constructed INSIDE the guard: Request.__init__ parses the url and
        # raises ValueError on a malformed one (an unmatched IPv6 bracket,
        # say). Outside, that escaped every except clause here, surfaced on
        # the future in _filter_dev_loop_owned, and aborted the whole sweep
        # instead of failing open for the one proposal.
        request = urllib.request.Request(  # noqa: S310 — scheme pinned to https above
            url, headers={"Authorization": f"Bearer {token}"}
        )
        # _NoRedirects, not the default opener: urllib replays the request
        # headers — MCTL_TOKEN included — at whatever a 3xx points to, so a
        # misconfigured or hostile redirect to http:// or another host would
        # leak the bearer token and defeat the https check above.
        with _no_redirect_opener().open(request, timeout=DEV_LOOP_LIVENESS_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        ValueError,
        OSError,
    ) as exc:
        # 404 = no such workflow (or an mctl-api predating the endpoint) —
        # genuinely not owned. Anything else is an infra failure; log it so
        # an operator can see the ownership check degraded, then sweep.
        if not (isinstance(exc, urllib.error.HTTPError) and exc.code == 404):
            print(f"warn: dev-loop liveness check failed for {workflow_id}: {exc}")
        return False
    if not isinstance(payload, dict) or payload.get("status") != "Running":
        return False
    # Running is not the same as ticking: an execution that started before
    # the shepherd-in-loop patch replays that branch as False and never
    # submits a tick, yet stays Running for up to the 14-day merge
    # deadline. Skipping it would leave its PR with no shepherd at all.
    # An mctl-api without the field answers None → swept, as before.
    return payload.get("shepherd_in_loop") is True


def _filter_dev_loop_owned(refs: list[ProposalRef]) -> list[ProposalRef]:
    """Drop refs a running DevLoop owns; used only in sweep mode (no
    explicit --slug — a targeted one-shot, including the DevLoop's own
    in-loop tick, must always process the slug it was given).

    Checks run concurrently and under a wall-clock budget so a degraded
    mctl-api cannot stretch one tick past the cron interval — see the
    DEV_LOOP_LIVENESS_* notes above. Anything not answered inside the
    budget is kept, which is the same fail-open outcome as an error.
    """
    if not refs:
        return refs
    owned: set[int] = set()
    workers = min(DEV_LOOP_LIVENESS_WORKERS, len(refs))
    # NOT `with ThreadPoolExecutor(...)`: __exit__ runs shutdown(wait=True,
    # cancel_futures=False), which blocks until every *queued* task has also
    # run — so the budget below would be advisory and the pass could still
    # cost ceil(N/workers)*DEV_LOOP_LIVENESS_TIMEOUT_S. Shutting down
    # explicitly with cancel_futures drops what has not started and does not
    # wait on what has; the in-flight calls carry their own socket timeout,
    # so the pool's threads retire on their own.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(_dev_loop_owns, ref.service, ref.slug): i
            for i, ref in enumerate(refs)
        }
        try:
            for future in as_completed(futures, timeout=DEV_LOOP_LIVENESS_BUDGET_S):
                if future.result():
                    owned.add(futures[future])
        except FuturesTimeoutError:
            # Budget spent. Whatever already answered still counts; the
            # rest stay unchecked and get swept.
            unanswered = sum(1 for f in futures if not f.done())
            print(
                f"warn: dev-loop ownership pass hit its "
                f"{DEV_LOOP_LIVENESS_BUDGET_S}s budget with {unanswered} "
                "proposal(s) unchecked — sweeping them"
            )
    finally:
        # Bounds this function; it cannot make an already-started worker
        # free. concurrent.futures.thread registers a process-wide
        # _python_exit atexit hook that joins every live pool thread, so a
        # hung call can still delay interpreter exit — but only by its own
        # DEV_LOOP_LIVENESS_TIMEOUT_S socket timeout, which is why that
        # timeout is 10s and not the budget. The tick's *work* stays inside
        # the budget either way; only the process's last breath waits.
        pool.shutdown(wait=False, cancel_futures=True)
    kept: list[ProposalRef] = []
    for i, ref in enumerate(refs):
        if i in owned:
            print(
                f"info: {ref.service}/{ref.slug} is driven by a running "
                "DevLoopWorkflow — skipping (sweeper mode, #213)"
            )
        else:
            kept.append(ref)
    return kept


COPILOT_BOT = "copilot-pull-request-reviewer[bot]"

# Statuses that the shepherd's discovery pass picks up. `implemented`
# and `review-fixing` are the steady-state inputs — see requirements.md
# L41-58 for why both must be in scope.
#
# `in-progress` is included narrowly to recover dead-letters: Tier 2
# sets `in-progress` BEFORE pushing a branch, then flips to
# `implemented` with `pr:` after `gh pr create`. If that second
# update_status_yaml is interrupted (pod evicted, network blip,
# `gh pr create` retry hiccup), the PR exists on GitHub but the
# proposal stays `in-progress` forever. The
# operator-closes-without-merging case (mctl-openclaw#15, 2026-04-30 —
# manually reconciled in mctl-gitops PR #97) is the canonical wedge.
# `process_one` gates on PR shape so we never race Tier 2 mid-flight:
# only `closed_unmerged` PRs are treated as dead-letters here, OPEN /
# MERGED PRs return `wait` and leave the proposal for Tier 2.
SHEPHERD_INPUT_STATUSES = {"implemented", "review-fixing", "in-progress"}
RECONCILE_INPUT_STATUSES = {
    "accepted",
    "in-progress",
    "implemented",
    "review-fixing",
    "review-stuck",
    "error",
    "needs-triage",
    "rejected",
}

# Outer-loop cap on consecutive address-review attempts before giving up
# and flipping to `review-stuck`. Lives here (NOT in decide()) so the
# pure function stays trivially testable. See design.md L122-142.
MAX_REVIEW_ATTEMPTS = 3

# mergeStateStatus values that are safe to merge per design.md L143-154.
# CLEAN = nothing in the way. HAS_HOOKS = pre-receive hooks (org-level
# branch protection, secret scanning) — GitHub still considers the PR
# mergeable. UNSTABLE = non-required CI failing but required ones pass.
MERGEABLE_STATES = {"CLEAN", "HAS_HOOKS", "UNSTABLE"}

# Settling window: do not merge a PR whose head was pushed within the last
# SHEPHERD_MERGE_SETTLE_MIN minutes, even when the review is clean and checks
# are green. This leaves a gap for a human (or a second-opinion reviewer such
# as codex) to push review fix-ups before the shepherd merges out from under
# active work — the failure mode that stranded fixes on mctl-telegram#115,
# which merged within minutes of a clean-but-shallow review. 0 disables the
# window. Anchored on head_pushed_at so the window resets on every new push.
def _settle_min_from_env() -> int:
    raw = os.environ.get("SHEPHERD_MERGE_SETTLE_MIN", "15")
    try:
        return int(raw)
    except ValueError:
        print(
            f"warn: SHEPHERD_MERGE_SETTLE_MIN={raw!r} is not an integer; "
            "defaulting to 15 minutes"
        )
        return 15


SHEPHERD_MERGE_SETTLE_MIN = _settle_min_from_env()


# Per-service opt-out. A repo whose name is listed here is owned by a
# different PR lifecycle (e.g. mctl-claude-remote's pr-steward) and the
# shepherd must NOT discover, fix, or merge its proposals — otherwise two
# actors push fixes to the same feat/agents-* branch and race on merge.
# Comma- or whitespace-separated; unset = empty set = today's behavior
# (zero blast radius for every other repo).
def _skip_services_from_env() -> frozenset[str]:
    raw = os.environ.get("SHEPHERD_SKIP_SERVICES", "")
    names = frozenset(s for s in raw.replace(",", " ").split() if s)
    unknown = names - set(SERVICES)
    if unknown:
        # A typo (e.g. `mctl-desig`) would silently skip nothing — warn so the
        # operator notices, mirroring the --service validation in main().
        print(
            "warn: SHEPHERD_SKIP_SERVICES contains names not in SERVICES "
            f"(typo?): {sorted(unknown)}"
        )
    return names


SHEPHERD_SKIP_SERVICES = _skip_services_from_env()


class FollowupSubprocessError(RuntimeError):
    """Raised when ``apply_followup`` cannot push a new commit.

    ``transient`` distinguishes plumbing failures (auth/network/branch
    protection — retry next tick, do NOT consume a review_attempts slot)
    from deterministic content failures (the implementer ran but produced
    no commits, or the PR branch was deleted on origin — same outcome
    every retry, so they MUST consume a slot and eventually flip the
    proposal to ``review-stuck`` for human triage).

    Default ``transient=True`` keeps backward compatibility with callers
    that raise without specifying — they are assumed safe-to-retry.
    Deterministic failures are surfaced via sentinel exit codes from
    ``run_implementer`` (see ``run_implementer._review_feedback_exit_code``).
    """

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


# ---------------------------------------------------------------------------
# Data classes. Plain old dataclasses, same shape as run_implementer.py.
# ---------------------------------------------------------------------------
@dataclass
class ProposalRef:
    """Lightweight handle to a proposal on disk."""

    service: str
    slug: str
    proposal_dir: Path
    status: str
    review_attempts: int = 0
    pr_url: str | None = None
    status_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.status_path = self.proposal_dir / ".status.yaml"


@dataclass
class CodexFinding:
    """A single P1 or P2 finding parsed out of a code review comment.

    `commit_id` is the SHA the comment is anchored to (line-anchored
    review comments carry a `commit_id` field; top-level issue comments
    do not — those use `created_at > head_pushed_at` instead).
    """

    body: str
    path: str | None
    line: int | None
    commit_id: str | None
    created_at: str | None
    severity: str  # "P1" or "P2"
    author: str | None = None  # bot login; None in fixtures predating #67


@dataclass
class CodexReview:
    """Aggregated codex signals on a PR, anchored to head_sha.

    `has_responded` matches design.md L86-99: a review by the claude
    review bot at head_sha, a line-anchored comment at head_sha, a
    "No P1/P2 findings" issue comment newer than head_pushed_at, OR a
    +1 reaction on a `@claude review` trigger comment newer than
    head_pushed_at.
    """

    has_responded: bool
    findings: list[CodexFinding]

    def findings_p1_p2(self, at: str) -> list[CodexFinding]:
        """Return findings anchored to the given head SHA only.

        Drops any finding whose commit_id is set but != at. Top-level
        issue comments without a commit_id are kept (they were already
        time-filtered against head_pushed_at upstream in
        read_codex_review).
        """
        out: list[CodexFinding] = []
        for f in self.findings:
            if f.commit_id is None or f.commit_id == at:
                out.append(f)
        return out


@dataclass
class CopilotReview:
    """Observed-only Copilot signal. Never gates a merge. Findings ride
    along to the per-tick operator log per design.md L100-108."""

    has_responded: bool
    findings_count: int


@dataclass
class PRSnapshot:
    """The slice of `gh pr view` JSON the shepherd actually uses."""

    number: int
    repo: str             # e.g. "mctlhq/mctl-web"
    state: str            # "OPEN", "CLOSED", "MERGED"
    merged: bool
    closed_unmerged: bool
    merge_commit: str | None
    close_comment_or_default: str
    head_sha: str
    head_pushed_at: str | None   # ISO 8601, used to anchor codex signals
    merge_state_status: str         # mergeStateStatus from GraphQL
    checks_green: bool              # required checks all SUCCESS
    is_draft: bool
    review_decision: str = ""       # APPROVED / CHANGES_REQUESTED / REVIEW_REQUIRED


@dataclass
class ShepherdResult:
    ref: ProposalRef
    decision: str
    error: str | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------
# .status.yaml IO — a leaner re-implementation of run_implementer.py's
# helpers. The shepherd reads more fields (review_attempts, pr) and
# preserves them across writes.
# ---------------------------------------------------------------------------
def _load_status(path: Path) -> dict:
    """Parse .status.yaml. Missing file → {}; default status is `proposed`."""
    return load_status(path)


def _now_iso() -> str:
    """RFC 3339 UTC timestamp without microseconds."""
    return now_iso()


def update_status(
    ref: ProposalRef,
    new_status: str,
    actor: str = "mctl-agents[bot]",
    **fields: Any,
) -> None:
    """Read-modify-write on `.status.yaml`.

    Existing fields (pr, notes, merged_at, merge_commit, review_attempts)
    are preserved unless explicitly overridden in `fields`. `status`,
    `updated_at`, and `updated_by` are always rewritten.
    """
    update_status_file(ref.status_path, new_status, actor=actor, **fields)
    ref.status = new_status
    if "review_attempts" in fields and fields["review_attempts"] is not None:
        ref.review_attempts = int(fields["review_attempts"])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_refs(
    state_dir: Path,
    service_filter: str | None = None,
    slug_filter: str | None = None,
    reconcile: bool = False,
    dry_run: bool = False,
) -> list[ProposalRef]:
    """Glob agents-state for proposals in SHEPHERD_INPUT_STATUSES.

    Both `implemented` and `review-fixing` are included per
    requirements.md L41-58. Proposals without a `pr:` URL are skipped —
    the shepherd has nothing to do until the implementer opens a PR.

    Normal mode skips services in SHEPHERD_SKIP_SERVICES entirely (they are
    owned by another PR lifecycle, e.g. pr-steward). Reconcile mode covers all
    services because GitHub-to-YAML projection is independent of which actor
    owns the active review/fix/merge loop.
    """
    if not state_dir.is_dir():
        raise SystemExit(f"State dir not found: {state_dir}")

    refs: list[ProposalRef] = []
    for service_dir in sorted(state_dir.iterdir()):
        if not service_dir.is_dir() or service_dir.name.startswith("_"):
            continue
        service = service_dir.name
        is_skipped = service in SHEPHERD_SKIP_SERVICES
        if not reconcile and is_skipped:
            # Owned by another PR lifecycle (e.g. pr-steward). Leave it alone,
            # but log it (only for real targets with a proposals/ dir) so an
            # operator isn't confused about why its proposals never appear.
            if (service_dir / "proposals").is_dir():
                print(
                    f"shepherd: skipping {service} "
                    "(SHEPHERD_SKIP_SERVICES; owned by another PR lifecycle)"
                )
            continue
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
            accepted_statuses = (
                RECONCILE_INPUT_STATUSES if reconcile else SHEPHERD_INPUT_STATUSES
            )
            if status not in accepted_statuses:
                continue
            pr_url = data.get("pr")
            if not pr_url and not reconcile:
                pr_url = _find_pr_url_by_branch(service, slug)
                if pr_url:
                    if not dry_run:
                        update_status_file(
                            proposal_dir / ".status.yaml",
                            "implemented" if status == "in-progress" else status,
                            pr=pr_url,
                        )
                    if status == "in-progress":
                        status = "implemented"
            if not pr_url and not reconcile:
                continue
            refs.append(
                ProposalRef(
                    service=service,
                    slug=slug,
                    proposal_dir=proposal_dir,
                    status=status,
                    review_attempts=int(data.get("review_attempts", 0) or 0),
                    pr_url=pr_url,
                )
            )
    return refs


# ---------------------------------------------------------------------------
# gh CLI helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Thin wrapper over subprocess.run with consistent logging.

    See run_issue_investigator._run — same reason for run_capturing: the
    shepherd's git/gh failures surface as Argo "exit code 128" with no cause
    attached unless stderr rides along on the exception.
    """
    refresh_github_token()
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    return run_capturing(cmd, cwd=cwd, check=check)


def _gh_api_json(args: list[str]) -> Any:
    """Run `gh api ...` and parse stdout as JSON. Empty stdout → None."""
    proc = _run(["gh", "api", *args])
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)


def _find_pr_url_by_branch(service: str, slug: str) -> str | None:
    """Find the canonical implementer PR even when YAML lost its URL."""
    branch = f"feat/agents-{slug}"
    proc = _run([
        "gh", "pr", "list",
        "--repo", f"mctlhq/{service}",
        "--state", "all",
        "--head", branch,
        "--limit", "100",
        "--json", "number,url,state,mergedAt,headRefName,body",
    ], check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        print(f"warn: GitHub PR discovery failed for {service}/{slug}: {detail}")
        return None
    try:
        prs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        print(f"warn: invalid GitHub PR discovery JSON for {service}/{slug}")
        return None
    marker = f"agents-state/{service}/proposals/{slug}/"
    exact = [
        pr for pr in prs
        if pr.get("headRefName") == branch and marker in (pr.get("body") or "")
    ]
    exact.sort(
        key=lambda pr: (
            pr.get("state") == "OPEN",
            bool(pr.get("mergedAt")) or pr.get("state") == "MERGED",
            int(pr.get("number") or 0),
        ),
        reverse=True,
    )
    return exact[0].get("url") if exact else None


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a GitHub PR URL into (owner, repo, number).

    Accepts both API-style and web-style URLs:
      https://github.com/mctlhq/mctl-web/pull/42
      https://api.github.com/repos/mctlhq/mctl-web/pulls/42
    """
    parts = pr_url.rstrip("/").split("/")
    try:
        # last segment is the number, the segment two-back is the owner
        # for /pull/<n>, repo is between them. Walk by index from the right.
        number = int(parts[-1])
        # /pull/<n> or /pulls/<n>
        # parts[-3] = repo, parts[-4] = owner (works for both URL shapes)
        repo = parts[-3]
        owner = parts[-4]
        return owner, repo, number
    except (ValueError, IndexError) as e:
        raise ValueError(f"Cannot parse PR URL: {pr_url!r}") from e


# ---------------------------------------------------------------------------
# PR + review readers
# ---------------------------------------------------------------------------
def find_pr_for_proposal(
    service: str,
    slug: str,
    state_dir: Path | None = None,
) -> PRSnapshot | None:
    """Read the linked PR for a proposal.

    If `.status.yaml` has no `pr:` URL, discover it from the deterministic
    implementer branch. Crucially this does NOT filter to state=open — closed
    and merged PRs are returned too.
    """
    sd = state_dir or DEFAULT_STATE_DIR
    status_path = sd / service / "proposals" / slug / ".status.yaml"
    data = _load_status(status_path)
    pr_url = data.get("pr") or _find_pr_url_by_branch(service, slug)
    if not pr_url:
        return None
    owner, repo, number = _parse_pr_url(pr_url)
    return _fetch_pr_snapshot(f"{owner}/{repo}", number)


def _fetch_pr_snapshot(repo: str, number: int) -> PRSnapshot | None:
    """gh pr view + a small GraphQL probe to assemble a PRSnapshot.

    `gh pr view` already exposes most of what we need (state, merged,
    mergeStateStatus, headRefOid, statusCheckRollup). The head push
    timestamp is not on `gh pr view --json` so we read it from the PR
    timeline via `gh api`.
    """
    try:
        view = _gh_api_json([
            "graphql", "-f",
            "query=query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$number){number state merged mergedAt mergeStateStatus reviewDecision isDraft headRefOid baseRefName mergeCommit{oid} timelineItems(itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT,PULL_REQUEST_COMMIT],last:50){nodes{__typename ... on PullRequestCommit{commit{oid committedDate}} ... on HeadRefForcePushedEvent{createdAt afterCommit{oid}}}} commits(last:1){nodes{commit{oid committedDate pushedDate}}} statusCheckRollup{state}}}}",  # noqa: E501 — single-line GraphQL query, not the kind of prose the line-length limit is meant to keep readable
            "-F", f"owner={repo.split('/')[0]}",
            "-F", f"repo={repo.split('/')[1]}",
            "-F", f"number={number}",
        ])
    except subprocess.CalledProcessError as e:
        print(f"warn: gh graphql failed for {repo}#{number}: {e.stderr.strip()}")
        return None

    if not view:
        return None
    pr = view.get("data", {}).get("repository", {}).get("pullRequest")
    if not pr:
        return None

    state = (pr.get("state") or "").upper()
    merged = bool(pr.get("merged"))
    head_sha = pr.get("headRefOid") or ""
    merge_commit_obj = pr.get("mergeCommit") or {}
    merge_commit = merge_commit_obj.get("oid")
    rollup = pr.get("statusCheckRollup") or {}
    rollup_state = (rollup.get("state") or "").upper()
    merge_state_status = (pr.get("mergeStateStatus") or "").upper()

    # head_pushed_at: prefer the latest commit's pushedDate; fall back to
    # committedDate; fall back to the latest force-push event's
    # createdAt. None if nothing matches — read_codex_review then
    # treats every issue-comment signal as predating the push (i.e. it
    # cannot rely on stale "no major issues" comments).
    head_pushed_at: str | None = None
    commits_nodes = (pr.get("commits") or {}).get("nodes") or []
    if commits_nodes:
        c = (commits_nodes[0] or {}).get("commit") or {}
        head_pushed_at = c.get("pushedDate") or c.get("committedDate")
    if not head_pushed_at:
        for node in (pr.get("timelineItems") or {}).get("nodes") or []:
            if node.get("__typename") == "HeadRefForcePushedEvent":
                head_pushed_at = node.get("createdAt")

    closed_unmerged = state == "CLOSED" and not merged
    close_comment_or_default = (
        "PR was closed without merging."
        if closed_unmerged
        else ""
    )

    # GitHub's statusCheckRollup.state aggregates *all* checks (required
    # and not). When merge_state_status is UNSTABLE, GitHub has already
    # confirmed required checks pass (only non-required ones are red);
    # when HAS_HOOKS, required checks pass and the only blocker is the
    # pre-receive hook. Both are mergeable per design.md L143-154, so we
    # also treat them as "checks green" even when the raw rollup is not
    # SUCCESS. Otherwise PRs with non-required CI failures (or in
    # hook-enabled repos) would stall here despite a clean code review.
    #
    # Third branch: the rollup is absent/empty when the repo has no CI
    # configured at all (e.g. mctl-gitops where merges have CI=SKIPPED
    # with an empty rollup). In that case GitHub still classifies the PR
    # as CLEAN — there is literally nothing to wait for. Without this
    # branch decide() would return ("wait", None) forever and the
    # shepherd would never progress past the `if not pr.checks_green`
    # gate.
    checks_green = (
        rollup_state == "SUCCESS"
        or merge_state_status in {"UNSTABLE", "HAS_HOOKS"}
        or (not rollup_state and merge_state_status == "CLEAN")
    )

    return PRSnapshot(
        number=int(pr.get("number") or number),
        repo=repo,
        state=state,
        merged=merged,
        closed_unmerged=closed_unmerged,
        merge_commit=merge_commit,
        close_comment_or_default=close_comment_or_default,
        head_sha=head_sha,
        head_pushed_at=head_pushed_at,
        merge_state_status=merge_state_status,
        checks_green=checks_green,
        is_draft=bool(pr.get("isDraft")),
        review_decision=(pr.get("reviewDecision") or "").upper(),
    )


def _iso_gt(a: str | None, b: str | None) -> bool:
    """True iff a > b in ISO 8601 lexicographic order. Both must be set."""
    if not a or not b:
        return False
    return a > b


def _within_settle_window(
    head_pushed_at: str | None, now: datetime, settle_min: int
) -> bool:
    """True iff head_pushed_at is less than settle_min minutes before now.

    Unknown/unparseable timestamps and a non-positive settle_min return False
    (no constraint), so the merge gate keeps its prior behavior when the window
    is disabled or the push time cannot be determined.
    """
    if not head_pushed_at or settle_min <= 0:
        return False
    try:
        pushed = datetime.fromisoformat(head_pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if pushed.tzinfo is None:
        pushed = pushed.replace(tzinfo=UTC)
    if pushed > now:
        # A future-dated head (e.g. committedDate fallback on a commit authored
        # with a skewed clock) would make now - pushed negative, which is < the
        # window and would hold the PR forever. Treat it as outside the window.
        return False
    return now - pushed < timedelta(minutes=settle_min)


def _extract_severity(body: str) -> str | None:
    """Find the severity marker in a review comment body.

    Supports the observed formats:
    - Codex badge format:   ``![P2 Badge](...)`` anywhere in body (legacy)
    - Claude review format: ``**P2 —`` anywhere in body (bold prefix), or
                            ``P2 —`` / ``P2 -`` / ``P2:`` at the start of
                            any line. The colon variant appeared 2026-08-28
                            (mctl-portal#88: every inline finding was
                            ``P1: ...`` and the shepherd parsed 0 findings,
                            waiting forever on a changes-requested PR).
    """
    for sev in ("P1", "P2", "P3"):
        if f"![{sev} Badge]" in body:
            return sev
        # Claude bold prefix — appears anywhere in the body (inline comment
        # bodies start with it; top-level review bodies embed it mid-text).
        if f"**{sev} —" in body or f"**{sev} -" in body or f"**{sev}:" in body:
            return sev
        # Bare prefix — matches at the start of the body or any line.
        for mark in (f"{sev} —", f"{sev} -", f"{sev}:"):
            if body.startswith(mark) or f"\n{mark}" in body:
                return sev
    return None


def read_codex_review(pr: PRSnapshot) -> CodexReview:
    """Build a CodexReview anchored to pr.head_sha.

    Implements the four signal rules in design.md L86-99:
      1. A review by the claude review bot at commit_id == pr.head_sha (any state).
      2. A line-anchored review comment by the bot at commit_id == head_sha.
      3. A top-level issue comment by the bot containing "No P1/P2 findings"
         with created_at > pr.head_pushed_at.
      4. A +1 reaction by the bot on the most recent `@claude review`
         trigger comment whose created_at > pr.head_pushed_at.

    findings_p1_p2(at=head_sha) drops any finding whose commit_id is
    set but != head_sha so a P1 from an earlier commit that the
    follow-up already fixed cannot make the loop spin forever.

    Findings are collected from BOTH gating bots (claude[bot] and
    chatgpt-codex-connector[bot], see GATING_BOTS / #67); has_responded is
    driven by claude[bot] alone — the connector's trigger is best-effort
    and a PR must never wait on it.
    """
    has_responded = False
    findings: list[CodexFinding] = []

    # 1. Reviews — `gh api repos/<owner>/<repo>/pulls/<n>/reviews`.
    try:
        reviews = _gh_api_json([
            f"repos/{pr.repo}/pulls/{pr.number}/reviews",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"warn: code reviews fetch failed for {pr.repo}#{pr.number}: {e.stderr.strip()}")
        reviews = []
    for r in reviews:
        login = (r.get("user") or {}).get("login")
        if login not in GATING_BOTS:
            continue
        commit_id = r.get("commit_id")
        # Only the primary reviewer flips has_responded — the connector is
        # best-effort and must not be waited on (see GATING_BOTS note).
        if login == REVIEW_BOT and commit_id and commit_id == pr.head_sha:
            has_responded = True
        # Top-level review body can carry findings too.
        body = r.get("body") or ""
        sev = _extract_severity(body)
        if sev in ("P1", "P2"):
            findings.append(CodexFinding(
                body=body,
                path=None,
                line=None,
                commit_id=commit_id,
                created_at=r.get("submitted_at"),
                severity=sev,
                author=login,
            ))

    # 2. Line-anchored review comments — `gh api repos/.../pulls/<n>/comments`.
    try:
        review_comments = _gh_api_json([
            f"repos/{pr.repo}/pulls/{pr.number}/comments",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"warn: codex pull comments fetch failed: {e.stderr.strip()}")
        review_comments = []
    for c in review_comments:
        login = (c.get("user") or {}).get("login")
        if login not in GATING_BOTS:
            continue
        commit_id = c.get("commit_id")
        if login == REVIEW_BOT and commit_id and commit_id == pr.head_sha:
            has_responded = True
        body = c.get("body") or ""
        sev = _extract_severity(body)
        if sev in ("P1", "P2"):
            findings.append(CodexFinding(
                body=body,
                path=c.get("path"),
                line=c.get("line") or c.get("original_line"),
                commit_id=commit_id,
                created_at=c.get("created_at"),
                severity=sev,
                author=login,
            ))

    # 3. Issue comments — top-level `No P1/P2 findings` newer
    #    than head_pushed_at — and (4) +1 reactions on a `@claude review`
    #    trigger newer than head_pushed_at. `gh api repos/.../issues/<n>/comments`.
    try:
        issue_comments = _gh_api_json([
            f"repos/{pr.repo}/issues/{pr.number}/comments",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"warn: codex issue comments fetch failed: {e.stderr.strip()}")
        issue_comments = []

    # Walk newest-first to find the most recent `@claude review` trigger.
    latest_trigger: dict | None = None
    for c in sorted(issue_comments, key=lambda x: x.get("created_at") or "", reverse=True):
        body = (c.get("body") or "").strip()
        if "@claude review" in body.lower():
            latest_trigger = c
            break

    for c in issue_comments:
        login = (c.get("user") or {}).get("login")
        body = c.get("body") or ""
        if login == REVIEW_BOT:
            created_at = c.get("created_at")
            if "No P1/P2 findings" in body and _iso_gt(created_at, pr.head_pushed_at):
                has_responded = True
            sev = _extract_severity(body)
            if sev in ("P1", "P2") and _iso_gt(created_at, pr.head_pushed_at):
                # Top-level issue comment — no commit_id; time-anchor only.
                # A finding posted as an issue comment is itself proof the bot
                # responded; set the flag so decide() routes to address-review
                # instead of spinning in wait when no separate +1 reaction or
                # "no major issues" sibling comment exists.
                has_responded = True
                findings.append(CodexFinding(
                    body=body,
                    path=None,
                    line=None,
                    commit_id=None,
                    created_at=created_at,
                    severity=sev,
                    author=login,
                ))
        elif login == CODEX_CONNECTOR_BOT:
            # Connector findings gate, but its presence/absence never
            # drives has_responded — it does not review every push, and a
            # PR must not wait on a reviewer that may never come.
            created_at = c.get("created_at")
            sev = _extract_severity(body)
            if sev in ("P1", "P2") and _iso_gt(created_at, pr.head_pushed_at):
                findings.append(CodexFinding(
                    body=body,
                    path=None,
                    line=None,
                    commit_id=None,
                    created_at=created_at,
                    severity=sev,
                    author=login,
                ))

    # +1 reaction by claude review bot on the latest @claude review trigger.
    if latest_trigger and _iso_gt(latest_trigger.get("created_at"), pr.head_pushed_at):
        comment_id = latest_trigger.get("id")
        try:
            reactions = _gh_api_json([
                f"repos/{pr.repo}/issues/comments/{comment_id}/reactions",
                "--paginate",
            ]) or []
        except subprocess.CalledProcessError as e:
            print(f"warn: codex reactions fetch failed: {e.stderr.strip()}")
            reactions = []
        for r in reactions:
            if (r.get("user") or {}).get("login") == REVIEW_BOT and r.get("content") == "+1":
                has_responded = True
                break

    return CodexReview(has_responded=has_responded, findings=findings)


def read_copilot_review(pr: PRSnapshot) -> CopilotReview:
    """Observed-only Copilot signal. Never gates a merge.

    Per design.md L100-108: claude review is the only gating signal. Copilot's
    findings (if any) are surfaced in the per-tick operator log so a
    human can spot interesting commentary; they do not block.
    """
    has_responded = False
    count = 0
    try:
        reviews = _gh_api_json([
            f"repos/{pr.repo}/pulls/{pr.number}/reviews",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError as e:
        print(f"warn: copilot reviews fetch failed: {e.stderr.strip()}")
        reviews = []
    for r in reviews:
        if (r.get("user") or {}).get("login") != COPILOT_BOT:
            continue
        has_responded = True
    try:
        comments = _gh_api_json([
            f"repos/{pr.repo}/pulls/{pr.number}/comments",
            "--paginate",
        ]) or []
    except subprocess.CalledProcessError:
        comments = []
    for c in comments:
        if (c.get("user") or {}).get("login") == COPILOT_BOT:
            has_responded = True
            count += 1
    return CopilotReview(has_responded=has_responded, findings_count=count)


# ---------------------------------------------------------------------------
# Decision logic — the pure function. No I/O, no globals.
# ---------------------------------------------------------------------------
def decide(
    pr: PRSnapshot,
    codex_review: CodexReview,
    now: datetime | None = None,
) -> tuple[str, Any]:
    """Return one of the five decisions per design.md L62-75.

    Pure: only depends on its arguments. `now` is injected so the settling
    window stays deterministic in tests; it defaults to the current UTC time.
    The 3-attempt cap on address-review loops lives in the OUTER state machine
    (process_one), NOT here, so this function stays trivially testable with
    hand-built fixtures.
    """
    if now is None:
        now = datetime.now(UTC)
    if pr.merged:
        return ("flip-to-merged", pr.merge_commit)
    if pr.closed_unmerged:
        return ("flip-to-rejected", pr.close_comment_or_default)
    if pr.is_draft:
        return ("wait", None)
    if not codex_review.has_responded:
        # Codex still parsing the PR head.
        return ("wait", None)
    findings = codex_review.findings_p1_p2(at=pr.head_sha)
    if findings:
        return ("address-review", findings)
    if pr.merge_state_status not in MERGEABLE_STATES:
        # BLOCKED, BEHIND, DIRTY, UNKNOWN, DRAFT — retry next tick.
        return ("wait", None)
    if not pr.checks_green:
        return ("wait", None)
    if _within_settle_window(pr.head_pushed_at, now, SHEPHERD_MERGE_SETTLE_MIN):
        # Clean + green, but the head was pushed within the settling window —
        # hold one tick so a human or second-opinion reviewer can land fix-ups
        # before we merge. See SHEPHERD_MERGE_SETTLE_MIN.
        return ("wait", None)
    return ("merge", None)


# ---------------------------------------------------------------------------
# Sub-agent invocation — let shepherd.md format the bundle into JSON.
# ---------------------------------------------------------------------------
async def _format_bundle_via_sdk(findings: list[CodexFinding]) -> dict:
    """Call the shepherd sub-agent to turn raw findings into the
    `{p1, p2, summaries}` bundle.

    Returns a dict with the parsed JSON, or a deterministic fallback if
    the SDK is unavailable / response unparseable. The fallback path
    keeps the shepherd functional even when the Claude budget is
    exhausted — the implementer just gets a less polished prompt.
    """
    raw = _serialise_findings(findings)
    prompt = (
        "You are about to receive a list of P1/P2 review findings on a PR.\n"
        "Use the `shepherd` sub-agent (defined in this project's "
        ".claude/agents/) to produce the final JSON.\n\n"
        "Findings:\n"
        f"{raw}\n\n"
        "Emit a single JSON object: {\"p1\": bool, \"p2\": bool, "
        "\"summaries\": [str, ...]}. Nothing else."
    )
    options = build_shepherd_options(SHEPHERD_DIR, SHEPHERD_MODEL)

    collected_text: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            # Capture any textual content the SDK streams. We do not
            # depend on a specific Message type here — any object with
            # a `content` attribute that yields text blocks works.
            text = _extract_text_from_message(message)
            if text:
                collected_text.append(text)
    except Exception as e:  # noqa: BLE001 — fall back deterministically
        print(f"warn: shepherd SDK call failed ({type(e).__name__}: {e}); using fallback bundle")
        return _fallback_bundle(findings)

    blob = "\n".join(collected_text).strip()
    parsed = _try_parse_json(blob)
    if parsed is None:
        print("warn: shepherd SDK returned non-JSON; using fallback bundle")
        return _fallback_bundle(findings)
    # Light validation — fall back if the structure is off.
    if not isinstance(parsed, dict) or "summaries" not in parsed:
        return _fallback_bundle(findings)
    parsed.setdefault("p1", any(f.severity == "P1" for f in findings))
    parsed.setdefault("p2", any(f.severity == "P2" for f in findings))
    return parsed


def _extract_text_from_message(message: Any) -> str:
    """Best-effort extractor for whatever the SDK streams.

    The Claude Agent SDK streams structured Message objects; we don't
    want to import its concrete classes here (keeps this module
    testable without the SDK installed). Fall through gracefully.
    """
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(message, "content", None)
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                out.append(t)
        if out:
            return "\n".join(out)
    if isinstance(message, str):
        return message
    return ""


def _try_parse_json(blob: str) -> Any:
    """Pull a JSON object out of the SDK response. Strips Markdown fences."""
    if not blob:
        return None
    candidate = blob.strip()
    if candidate.startswith("```"):
        # ```json ... ```  — drop the fence.
        lines = candidate.splitlines()
        # Drop the opening fence and (if present) the closing one.
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        candidate = "\n".join(lines).strip()
    # Find the first { and last } — the SDK occasionally wraps with prose.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        return None


def _serialise_findings(findings: list[CodexFinding]) -> str:
    """Compact human/SDK-readable rendering of the findings list."""
    parts: list[str] = []
    for i, f in enumerate(findings, 1):
        loc = ""
        if f.path:
            loc = f.path + (f":{f.line}" if f.line else "")
        who = f", {f.author}" if f.author else ""
        parts.append(
            f"--- Finding {i} ({f.severity}{who}) ---\n"
            f"Location: {loc or '(top-level comment)'}\n"
            f"Body:\n{f.body}\n"
        )
    return "\n".join(parts)


def _fallback_bundle(findings: list[CodexFinding]) -> dict:
    """Deterministic JSON if the SDK is unavailable.

    Summary per finding: severity + location + first non-empty line
    of the body. Good enough for the implementer to act on, and
    importantly leaves the shepherd functional when the budget is
    exhausted or the SDK errors out.
    """
    summaries: list[str] = []
    for f in findings:
        loc = ""
        if f.path:
            loc = f.path + (f":{f.line}" if f.line else "")
        first_line = ""
        for ln in (f.body or "").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("![") and not ln.startswith("#"):
                first_line = ln
                break
        summaries.append(
            f"[{f.severity}] {loc + ': ' if loc else ''}{first_line}".strip()
        )
    return {
        "p1": any(f.severity == "P1" for f in findings),
        "p2": any(f.severity == "P2" for f in findings),
        "summaries": summaries,
    }


# ---------------------------------------------------------------------------
# Address-review followup — subprocess into run_implementer.py with
# --review-feedback (Task 3, landed in this branch).
# ---------------------------------------------------------------------------
def apply_followup(
    service: str,
    slug: str,
    findings: list[CodexFinding],
    skip_subprocess: bool = False,
    state_dir: Path | None = None,
) -> dict:
    """Bundle findings + invoke the Tier 2 implementer with --review-feedback.

    Returns the bundle dict so callers (and tests) can inspect what the
    SDK produced. ``skip_subprocess`` is kept as a test-only escape hatch
    (the unit tests don't want to fork a real implementer) — production
    code paths leave it at the default ``False`` so the subprocess fires.

    ``state_dir`` is forwarded to the implementer subprocess as
    ``--state-dir <path>`` whenever it differs from the implementer's
    own default. Without this, an upstream shepherd run with a custom
    ``--state-dir`` would invoke the implementer with the env default
    and the implementer could not find the proposal in
    ``implemented/review-fixing`` — exiting non-zero and triggering an
    avoidable ``review-stuck`` flip.
    """
    bundle = anyio.run(_format_bundle_via_sdk, findings)

    if skip_subprocess:
        # Tests / dry-run: build the bundle but do not fork. The shepherd's
        # outer loop still flips the proposal to review-fixing; the next
        # tick will re-evaluate codex on the head SHA the implementer
        # would have moved.
        print(f"info: --review-feedback subprocess skipped (test/dry-run); bundle={bundle}")
        return bundle

    # Persist the bundle to a temp file because the implementer reads it
    # via `--review-feedback <path>` (see run_implementer.main).
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix=f"shepherd-{service}-{slug}-",
        delete=False, encoding="utf-8",
    ) as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
        bundle_path = fh.name

    cmd = [
        sys.executable, "-m", "orchestrator.run_implementer",
        "--service", service,
        "--slug", slug,
        "--review-feedback", bundle_path,
    ]
    # Forward --state-dir only when it differs from the implementer's
    # own DEFAULT_STATE_DIR. The implementer's argparse default is the
    # same env-driven path; appending it unconditionally would be noise.
    if state_dir is not None and Path(state_dir) != run_implementer.DEFAULT_STATE_DIR:
        cmd.extend(["--state-dir", str(state_dir)])
    print(f"$ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, check=False, text=True)  # noqa: S603 — cmd is list[str], built above
    finally:
        try:
            os.unlink(bundle_path)
        except FileNotFoundError:
            pass  # already gone — that's fine
    if proc.returncode != 0:
        # Surface as a typed exception so the outer state machine can
        # tell a transient subprocess failure (auth/network/branch
        # protection on the implementer's git push) apart from a
        # deterministic content failure (the agent ran but produced no
        # commits, or the PR branch was deleted on origin). The
        # MAX_REVIEW_ATTEMPTS cap exists for the latter — re-running an
        # auth-failed push is free, re-running an SDK call that just
        # produced nothing is not, and the only graceful exit for the
        # deterministic case is to flip to review-stuck once the budget
        # is spent.
        #
        # The implementer encodes the kind of failure via sentinel exit
        # codes (`run_implementer.EXIT_NO_FOLLOWUP_COMMITS` = 42,
        # `EXIT_BRANCH_MISSING_ON_ORIGIN` = 43,
        # `EXIT_OPERATION_TIMEOUT` = 44). Anything else (1, 137, 2, ...)
        # is treated as transient — we cannot tell the kind from the code
        # alone.
        deterministic_codes = {
            run_implementer.EXIT_NO_FOLLOWUP_COMMITS,
            run_implementer.EXIT_BRANCH_MISSING_ON_ORIGIN,
            run_implementer.EXIT_OPERATION_TIMEOUT,
        }
        is_transient = proc.returncode not in deterministic_codes
        raise FollowupSubprocessError(
            f"implementer follow-up exited non-zero "
            f"({proc.returncode}) for {service}/{slug}",
            transient=is_transient,
        )
    return bundle


# ---------------------------------------------------------------------------
# Re-trigger review after a successful followup push. Paired with
# apply_followup above — the review bot only re-reviews on explicit
# @-mention, so a bare new commit will not get it to look at the new
# head_sha. Without this, the shepherd stalls in `wait` forever after
# the first fix-up push.
# ---------------------------------------------------------------------------
def trigger_review(pr: PRSnapshot) -> None:
    """Post ``@claude review`` so the review bot re-reviews the new head.

    Best-effort: a failed comment post is logged, not raised. The tick
    still completes; the next tick will retry once a human posts the
    trigger manually. ``OSError`` is caught alongside
    ``CalledProcessError`` so a missing-`gh` binary in a stripped image
    cannot crash the tick after a real fix push — the exact failure
    mode this function is designed to prevent.
    """
    pr_ref = f"https://github.com/{pr.repo}/pull/{pr.number}"
    try:
        _run(["gh", "pr", "comment", pr_ref, "--body", "@claude review"])
        print(f"info: posted `@claude review` on {pr.repo}#{pr.number}")
    except (subprocess.CalledProcessError, OSError) as e:
        # OSError (e.g. FileNotFoundError when gh isn't on PATH) has no
        # .stderr attribute; fall back to str(e) via getattr.
        msg = (getattr(e, "stderr", None) or "").strip() or str(e)
        print(
            f"warn: failed to post `@claude review` on {pr.repo}#{pr.number} "
            f"({msg}); next tick stalls until trigger is posted manually"
        )


# ---------------------------------------------------------------------------
# Merge — gh pr merge with --match-head-commit per requirements.md L50-60.
# ---------------------------------------------------------------------------
def merge_pr(pr: PRSnapshot) -> tuple[bool, str | None]:
    """Invoke `gh pr merge --merge --delete-branch --match-head-commit <SHA>`.

    Returns (success, merge_commit_oid). On HEAD-SHA mismatch the gh
    CLI exits non-zero — callers SHALL treat that as transient `wait`
    so the next tick re-evaluates the new head (a push that landed
    between review and merge cannot smuggle unreviewed code through).
    """
    pr_ref = f"https://github.com/{pr.repo}/pull/{pr.number}"
    cmd = [
        "gh", "pr", "merge",
        "--merge",
        "--delete-branch",
        "--match-head-commit", pr.head_sha,
        pr_ref,
    ]
    # Bypasses _run() (this is the one gh call this module makes outside
    # that wrapper), so it needs its own refresh: merge_pr() typically fires
    # after a review/fix cycle long enough to have crossed the token's
    # ~60min TTL — the exact case refresh_github_token() exists to cover.
    refresh_github_token()
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)  # noqa: S603 — cmd is list[str]
    if proc.returncode != 0:
        # HEAD-SHA mismatch is the expected non-zero exit. Anything else
        # (auth, branch protection rejection) is also surfaced as wait —
        # the next tick re-fetches and re-decides.
        msg = (proc.stderr or proc.stdout or "").strip()
        print(f"warn: gh pr merge exited non-zero: {msg}")
        return (False, None)

    # Re-read the PR to get the merge commit oid for .status.yaml.
    snap = _fetch_pr_snapshot(pr.repo, pr.number)
    merge_commit = snap.merge_commit if snap else None
    return (True, merge_commit)


# ---------------------------------------------------------------------------
# Outer state machine — orchestrates one proposal per tick.
# ---------------------------------------------------------------------------
def process_one(
    ref: ProposalRef,
    skip_subprocess: bool = False,
    state_dir: Path | None = None,
) -> ShepherdResult:
    """Drive a single proposal one tick further.

    - Reads the linked PR (open/closed/merged — never filter to open).
    - Calls decide(); honours the 3-attempt cap on address-review loops
      per design.md L122-142.
    - Writes back .status.yaml.
    - Returns a ShepherdResult with the decision name (greppable in
      the workflow log).

    ``state_dir`` is forwarded to every helper that resolves proposals
    on disk. When ``None``, callers fall back to ``DEFAULT_STATE_DIR``
    (the env-driven default). The CLI always threads ``args.state_dir``
    so a non-default ``--state-dir`` is honoured end-to-end and we do
    not silently re-read from the env path.
    """
    pr = find_pr_for_proposal(ref.service, ref.slug, state_dir=state_dir)
    if pr is None:
        return ShepherdResult(
            ref=ref,
            decision="wait",
            error="could not fetch PR snapshot",
        )

    # A durable PR proves Tier 2 finished its expensive work. Heal a dropped
    # status write and continue on the same tick instead of waiting forever.
    if (
        ref.status == "in-progress"
        and not pr.closed_unmerged
        and not pr.merged
        and _attempt_is_fresh(ref)
    ):
        return ShepherdResult(
            ref=ref,
            decision="wait",
            notes="active implementation lease has not expired",
        )
    if ref.status == "in-progress" and not pr.closed_unmerged and not pr.merged:
        attempt = _load_status(ref.status_path).get("attempt")
        if isinstance(attempt, dict):
            attempt = dict(attempt)
            attempt.setdefault("finished_at", _now_iso())
        update_status(
            ref,
            "implemented",
            pr=f"https://github.com/{pr.repo}/pull/{pr.number}",
            failure=None,
            notes=None,
            attempt=attempt,
        )

    codex = read_codex_review(pr)
    copilot = read_copilot_review(pr)  # observed only — never gates
    decision, payload = decide(pr, codex)

    # Per-tick operator log line — Copilot's findings ride along here so
    # they are visible without gating the merge.
    print(
        f"info: pr={pr.repo}#{pr.number} head={pr.head_sha[:8]} "
        f"merge_state={pr.merge_state_status} checks_green={pr.checks_green} "
        f"codex_responded={codex.has_responded} codex_findings={len(codex.findings)} "
        f"connector_findings={sum(1 for f in codex.findings if f.author == CODEX_CONNECTOR_BOT)} "
        f"copilot_responded={copilot.has_responded} copilot_findings={copilot.findings_count} "
        f"-> {decision}"
    )

    if decision == "wait":
        return ShepherdResult(ref=ref, decision="wait")

    if decision == "flip-to-merged":
        update_status(
            ref,
            "merged",
            merged_at=_now_iso(),
            merge_commit=payload,
            review_attempts=None,  # clear it so terminal status is clean
        )
        return ShepherdResult(ref=ref, decision="flip-to-merged")

    if decision == "flip-to-rejected":
        update_status(
            ref,
            "rejected",
            notes=payload or "PR was closed without merging.",
            review_attempts=None,
        )
        return ShepherdResult(ref=ref, decision="flip-to-rejected")

    if decision == "address-review":
        # Outer-loop cap. design.md L122-142:
        #   tick 1: counter=0 -> call -> counter=1
        #   tick 2: counter=1 -> call -> counter=2
        #   tick 3: counter=2 -> call -> counter=3
        #   tick 4: counter=3 -> flip to review-stuck, NO call
        if ref.review_attempts >= MAX_REVIEW_ATTEMPTS:
            update_status(
                ref,
                "review-stuck",
                notes=(
                    f"Codex P1/P2 findings persisted across "
                    f"{MAX_REVIEW_ATTEMPTS} follow-up attempts; "
                    f"human triage required."
                ),
            )
            return ShepherdResult(ref=ref, decision="review-stuck")

        # Run the followup BEFORE incrementing the attempt counter or
        # flipping status. The MAX_REVIEW_ATTEMPTS cap is for codex
        # content fix attempts, not subprocess plumbing failures — a
        # transient auth/network/branch error in the implementer push
        # must NOT burn one of the three slots. On transient failure we
        # leave .status.yaml untouched so the next tick retries cleanly.
        #
        # Deterministic content failures (the implementer ran but
        # produced no commits, or the PR branch was deleted on origin)
        # DO consume a slot — re-running them yields the same outcome,
        # so the cap is the only thing that prevents the proposal from
        # spinning forever. When the cap is hit, flip to review-stuck
        # so a human can intervene.
        try:
            apply_followup(
                ref.service, ref.slug, payload,
                skip_subprocess=skip_subprocess,
                state_dir=state_dir,
            )
        except FollowupSubprocessError as e:
            if e.transient:
                print(
                    f"warn: {ref.service}/{ref.slug}: follow-up subprocess "
                    f"failed transiently ({e}); leaving "
                    f"review_attempts={ref.review_attempts} and "
                    f"status={ref.status} for retry next tick"
                )
                return ShepherdResult(
                    ref=ref,
                    decision="wait",
                    notes="follow-up subprocess failed; will retry next tick",
                )
            # Deterministic failure — count the attempt and surface the
            # proposal once the cap is exhausted.
            new_attempts = ref.review_attempts + 1
            print(
                f"error: {ref.service}/{ref.slug}: follow-up subprocess "
                f"failed deterministically ({e}); incrementing "
                f"review_attempts to {new_attempts}"
            )
            if new_attempts >= MAX_REVIEW_ATTEMPTS:
                update_status(
                    ref,
                    "review-stuck",
                    review_attempts=new_attempts,
                    notes=(
                        f"Follow-up failed deterministically "
                        f"{new_attempts} time(s) ({e}); human triage "
                        f"required."
                    ),
                )
                return ShepherdResult(
                    ref=ref,
                    decision="review-stuck",
                    notes="follow-up subprocess failed deterministically",
                )
            update_status(
                ref,
                "review-fixing",
                review_attempts=new_attempts,
            )
            update_status(ref, "implemented")
            return ShepherdResult(
                ref=ref,
                decision="wait",
                notes=(
                    "follow-up subprocess failed deterministically; "
                    "attempt counted, will retry next tick"
                ),
            )

        # Followup pushed successfully — re-trigger the review bot so it
        # re-reviews the new head SHA the implementer just pushed. The
        # bot only re-reviews on explicit @-mention; without this post
        # the loop stalls (read_codex_review returns has_responded=False
        # on the new head_sha, decide() returns wait, forever).
        trigger_review(pr)

        # Now it is fair to count the attempt. Flip to review-fixing so
        # the in-flight signal is on disk, then back to implemented so
        # the next tick re-evaluates codex against the new head SHA.
        update_status(
            ref,
            "review-fixing",
            review_attempts=ref.review_attempts + 1,
        )
        update_status(ref, "implemented")
        return ShepherdResult(ref=ref, decision="address-review")

    if decision == "merge":
        ok, merge_commit = merge_pr(pr)
        if not ok:
            # Transient: HEAD-SHA mismatch or branch-protection rejection.
            return ShepherdResult(
                ref=ref,
                decision="wait",
                notes="merge attempt non-zero; will retry next tick",
            )
        update_status(
            ref,
            "merged",
            merged_at=_now_iso(),
            merge_commit=merge_commit,
            review_attempts=None,
        )
        return ShepherdResult(ref=ref, decision="merge")

    # Defensive — decide() returned something unexpected.
    return ShepherdResult(
        ref=ref,
        decision=decision,
        error=f"unknown decision: {decision}",
    )


def _update_status_if_changed(
    ref: ProposalRef,
    new_status: str,
    *,
    dry_run: bool = False,
    **fields: Any,
) -> bool:
    """Avoid noisy GitOps commits when the durable projection is unchanged."""
    existing = _load_status(ref.status_path)
    if existing.get("status", "proposed") != new_status:
        if not dry_run:
            update_status(ref, new_status, **fields)
        return True
    for key, value in fields.items():
        if value is None:
            if key in existing:
                if not dry_run:
                    update_status(ref, new_status, **fields)
                return True
        elif existing.get(key) != value:
            if not dry_run:
                update_status(ref, new_status, **fields)
            return True
    return False


def _attempt_is_fresh(ref: ProposalRef) -> bool:
    attempt = _load_status(ref.status_path).get("attempt") or {}
    expires_at = attempt.get("expires_at") if isinstance(attempt, dict) else None
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return expires > datetime.now(UTC)


def _finished_attempt(ref: ProposalRef) -> dict[str, Any] | None:
    attempt = _load_status(ref.status_path).get("attempt")
    if not isinstance(attempt, dict):
        return None
    finished = dict(attempt)
    finished.setdefault("finished_at", _now_iso())
    return finished


def _github_projection_for_ref(
    ref: ProposalRef,
    *,
    state: str,
    head_sha: str,
    blocking_reason: str | None = None,
) -> dict[str, Any]:
    """Keep observed_at stable until a material GitHub field changes."""
    existing = _load_status(ref.status_path).get("github") or {}
    core: dict[str, Any] = {"state": state, "head_sha": head_sha}
    if blocking_reason:
        core["blocking_reason"] = blocking_reason
    if isinstance(existing, dict):
        existing_core = {
            key: value for key, value in existing.items() if key != "observed_at"
        }
        if existing_core == core and existing.get("observed_at"):
            return existing
    return {**core, "observed_at": _now_iso()}


def reconcile_one(
    ref: ProposalRef,
    state_dir: Path | None = None,
    dry_run: bool = False,
) -> ShepherdResult:
    """Project authoritative GitHub PR state back into ``.status.yaml``.

    This never reviews, fixes, or merges. It is safe for both shepherd-owned
    and steward-owned repositories.
    """
    status_data = _load_status(ref.status_path)
    existing_failure = status_data.get("failure")
    failure_code = (
        existing_failure.get("code")
        if isinstance(existing_failure, dict)
        else None
    )
    if ref.status == "needs-triage" and failure_code == "branch-collision":
        return ShepherdResult(
            ref=ref,
            decision="needs-triage",
            notes="branch collision requires explicit operator resolution",
        )
    discovered_url = ref.pr_url or _find_pr_url_by_branch(ref.service, ref.slug)
    pr = find_pr_for_proposal(ref.service, ref.slug, state_dir=state_dir)
    if pr is None:
        # A recorded/discovered URL that cannot be fetched is a transient
        # GitHub read failure, not proof that the PR disappeared.
        if discovered_url:
            return ShepherdResult(
                ref=ref,
                decision="wait",
                error="could not fetch recorded GitHub PR",
            )
        # A terminal human/GitHub decision is durable.  We still perform the
        # canonical branch/PR lookup above so a real PR can correct stale
        # projection, but the absence of a PR is not evidence that a rejected
        # or already-merged proposal should be reopened for triage.
        if ref.status in {"merged", "rejected"}:
            return ShepherdResult(
                ref=ref,
                decision="wait",
                notes=f"preserving terminal {ref.status} status without a PR",
            )
        try:
            recovered = run_implementer._preflight_existing_result(
                ref,  # type: ignore[arg-type]  # cross-module duck typing: this
                # module's ProposalRef is a superset of run_implementer's (see the
                # class docstring above — "same shape as run_implementer.py"), and
                # _preflight_existing_result only reads .service/.slug, which both
                # classes share. Nominally different classes, safe at runtime.
                allow_pr_create=not dry_run,
            )
        except run_implementer.GitHubPreflightError as exc:
            return ShepherdResult(
                ref=ref,
                decision="wait",
                error=f"GitHub preflight failed closed: {exc}",
            )
        if recovered.action == "open" and recovered.pr_url:
            _update_status_if_changed(
                ref,
                "implemented",
                dry_run=dry_run,
                pr=recovered.pr_url,
                failure=None,
                notes=None,
                attempt=_finished_attempt(ref),
            )
            owner, repo, number = _parse_pr_url(recovered.pr_url)
            pr = _fetch_pr_snapshot(f"{owner}/{repo}", number)
            if pr is None:
                return ShepherdResult(
                    ref=ref,
                    decision="wait",
                    error="opened/adopted PR but could not fetch its snapshot",
                )
            discovered_url = recovered.pr_url
        elif recovered.action == "branch-ready":
            return ShepherdResult(
                ref=ref,
                decision="would-open-pr",
                notes=recovered.reason,
            )
        elif recovered.action == "needs-triage":
            _update_status_if_changed(
                ref,
                "needs-triage",
                dry_run=dry_run,
                failure={
                    "code": recovered.reason or "existing-result-invalid",
                    "stage": "reconcile",
                    "message": "Existing deterministic result cannot be adopted safely.",
                },
                notes=f"reconcile: {recovered.reason or 'existing result invalid'}",
            )
            return ShepherdResult(
                ref,
                decision="needs-triage",
                notes=recovered.reason,
            )
        elif recovered.action in {"merged", "closed"} and recovered.pr_url:
            owner, repo, number = _parse_pr_url(recovered.pr_url)
            pr = _fetch_pr_snapshot(f"{owner}/{repo}", number)
            discovered_url = recovered.pr_url
            if pr is None:
                return ShepherdResult(
                    ref=ref,
                    decision="wait",
                    error="found terminal PR but could not fetch its snapshot",
                )
        else:
            pr = None
    if pr is None:
        if ref.status == "accepted":
            return ShepherdResult(ref=ref, decision="wait")
        if ref.status == "needs-triage" and existing_failure:
            return ShepherdResult(
                ref=ref,
                decision="needs-triage",
                notes=f"preserving existing failure: {failure_code or 'unknown'}",
            )
        if ref.status == "in-progress" and _attempt_is_fresh(ref):
            return ShepherdResult(
                ref=ref,
                decision="wait",
                notes="active implementation lease has not expired",
            )
        _update_status_if_changed(
            ref,
            "needs-triage",
            dry_run=dry_run,
            failure={
                "code": "missing-pr",
                "stage": "reconcile",
                "message": "No canonical PR exists for the deterministic result branch.",
            },
            notes="reconcile: no canonical GitHub PR found",
        )
        return ShepherdResult(
            ref=ref, decision="needs-triage", notes="missing canonical PR"
        )
    pr_url = discovered_url or f"https://github.com/{pr.repo}/pull/{pr.number}"
    github_state = "merged" if pr.merged else ("closed" if pr.closed_unmerged else "open")
    github = _github_projection_for_ref(
        ref,
        state=github_state,
        head_sha=pr.head_sha,
    )
    if pr.merged:
        existing_status = _load_status(ref.status_path)
        _update_status_if_changed(
            ref,
            "merged",
            dry_run=dry_run,
            pr=pr_url,
            github=github,
            merged_at=existing_status.get("merged_at") or _now_iso(),
            merge_commit=pr.merge_commit,
            review_attempts=None,
            failure=None,
        )
        return ShepherdResult(ref=ref, decision="flip-to-merged")
    if pr.closed_unmerged:
        _update_status_if_changed(
            ref,
            "rejected",
            dry_run=dry_run,
            pr=pr_url,
            github=github,
            notes=pr.close_comment_or_default or "PR was closed without merging.",
            review_attempts=None,
            failure=None,
        )
        return ShepherdResult(ref=ref, decision="flip-to-rejected")
    if ref.status == "needs-triage" and failure_code == "merge-conflict":
        prior_github = status_data.get("github") or {}
        prior_head = (
            prior_github.get("head_sha")
            if isinstance(prior_github, dict)
            else None
        )
        # GitHub can transiently return UNKNOWN while recomputing
        # mergeability. Never let that weak observation erase a previously
        # confirmed conflict. Reopen the proposal only after a new head SHA
        # is explicitly reported in one of GitHub's mergeable states.
        if (
            pr.merge_state_status != "DIRTY"
            and (
                pr.merge_state_status not in MERGEABLE_STATES
                or not prior_head
                or prior_head == pr.head_sha
            )
        ):
            return ShepherdResult(
                ref=ref,
                decision="needs-triage",
                notes=(
                    "preserving confirmed merge conflict until a changed "
                    "head SHA is explicitly mergeable"
                ),
            )
    if pr.merge_state_status == "DIRTY":
        github = _github_projection_for_ref(
            ref,
            state="open",
            head_sha=pr.head_sha,
            blocking_reason="conflict",
        )
        _update_status_if_changed(
            ref,
            "needs-triage",
            dry_run=dry_run,
            pr=pr_url,
            github=github,
            failure={
                "code": "merge-conflict",
                "stage": "reconcile",
                "message": "GitHub reports a merge conflict with the base branch.",
            },
            notes="reconcile: open PR has a merge conflict",
        )
        return ShepherdResult(ref=ref, decision="needs-triage", notes="merge conflict")

    target_status = ref.status
    if ref.status in {
        "accepted",
        "in-progress",
        "error",
        "needs-triage",
        "rejected",
    }:
        target_status = "implemented"
    if ref.status == "review-stuck":
        prior_github = status_data.get("github") or {}
        prior_head = (
            prior_github.get("head_sha")
            if isinstance(prior_github, dict)
            else None
        )
        if pr.review_decision == "APPROVED" or (
            prior_head and prior_head != pr.head_sha
        ):
            target_status = "implemented"
    repair_fields: dict[str, Any] = {
        "pr": pr_url,
        "github": github,
    }
    if target_status == "implemented" and ref.status != "implemented":
        repair_fields["failure"] = None
        repair_fields["notes"] = None
        repair_fields["attempt"] = _finished_attempt(ref)
        if ref.status == "review-stuck":
            repair_fields["review_attempts"] = None
    changed = _update_status_if_changed(
        ref,
        target_status,
        dry_run=dry_run,
        **repair_fields,
    )
    return ShepherdResult(
        ref=ref,
        decision="repair-open-pr" if changed else "wait",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_summary(results: list[ShepherdResult]) -> None:
    """Greppable per-tick summary; exit non-zero if any proposal errored."""
    print("\n=== Shepherd summary ===")
    fail = 0
    for r in results:
        line = f"{r.ref.service}/{r.ref.slug}: {r.decision}"
        if r.error:
            line += f"  ERROR: {r.error}"
            fail += 1
        if r.notes:
            line += f"  ({r.notes})"
        print(line)
    if fail:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Tier 3 PR shepherd — drive implementer-opened PRs to merge",
    )
    ap.add_argument(
        "--service", default="",
        help=f"Filter by service (one of: {', '.join(SERVICES)})",
    )
    ap.add_argument(
        "--slug", default="",
        help="Filter by proposal slug",
    )
    ap.add_argument(
        "--budget", type=float, default=None,
        help=(
            "Per-tick soft budget cap in USD "
            "(default $SHEPHERD_BUDGET_USD or 5.00). When the cap is "
            "crossed the shepherd exits cleanly with a warning."
        ),
    )
    ap.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Path to platform-gitops/agents-state/ (defaults to STATE_DIR env)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Discover only; do not call the SDK or merge anything",
    )
    ap.add_argument(
        "--reconcile", action="store_true",
        help=(
            "GitHub-first status projection for every service: discover lost "
            "PR links and repair open/merged/closed/expired states. Never "
            "reads reviews, applies fixes, merges, or calls the SDK."
        ),
    )
    args = ap.parse_args()

    if args.service and args.service not in SERVICES:
        print(
            f"Unknown service '{args.service}'. Available: {', '.join(SERVICES)}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Resolve effective budget.
    budget = args.budget if args.budget is not None else SHEPHERD_BUDGET_USD
    if budget <= 0:
        print(f"warn: SHEPHERD_BUDGET_USD={budget} is non-positive; nothing will run")

    # Skip SDK auth init in dry-run AND reconcile modes: both are read-only
    # (reconcile only reads PR state + writes terminal status) and must work
    # in environments without Claude credentials.
    if not args.dry_run and not args.reconcile:
        ensure_auth_for_sdk()

    state_dir = Path(args.state_dir)
    refs = _discover_refs(
        state_dir,
        service_filter=args.service or None,
        slug_filter=args.slug or None,
        reconcile=args.reconcile,
        dry_run=args.dry_run,
    )

    # Sweep mode only (#213): a targeted --slug run — including the
    # DevLoop's own in-loop shepherd tick — always processes its slug;
    # reconcile stays unfiltered too (read-only idempotent projection,
    # and terminal states must be projected even for owned slugs).
    if not args.slug and not args.reconcile:
        refs = _filter_dev_loop_owned(refs)

    if not refs:
        if args.reconcile:
            print("info: no non-terminal proposals to reconcile.")
        else:
            print("info: no implemented/review-fixing proposals with a PR found.")
        return

    # Reconcile mode: GitHub projection, no budget or SDK. Dry-run executes the
    # full decision pass but suppresses YAML writes and PR creation.
    if args.reconcile:
        print(f"Reconcile pass: {len(refs)} proposal(s):")
        for r in refs:
            print(f"  - {r.service}/{r.slug} [{r.status}]")
        reconcile_results: list[ShepherdResult] = []
        for ref in refs:
            reconcile_results.append(
                reconcile_one(
                    ref,
                    state_dir=state_dir,
                    dry_run=args.dry_run,
                )
            )
        _print_summary(reconcile_results)
        return

    print(f"Found {len(refs)} proposal(s) to evaluate (budget cap ${budget:.2f}):")
    for r in refs:
        print(f"  - {r.service}/{r.slug} [{r.status}] attempts={r.review_attempts}")

    results: list[ShepherdResult] = []
    spent_estimate = 0.0
    # Per-call estimate is impossible to bound exactly without SDK
    # cost reporting; we use a conservative upper bound to keep the
    # cap meaningful. The SDK itself enforces max_budget_usd per
    # query (see build_shepherd_options) — this loop-level guard is
    # belt-and-braces for a runaway tick with many proposals.
    per_call_estimate = SHEPHERD_BUDGET_USD

    for ref in refs:
        if spent_estimate >= budget:
            print(
                f"warn: SHEPHERD_BUDGET_USD cap ${budget:.2f} reached "
                f"before processing {ref.service}/{ref.slug}; exiting cleanly"
            )
            break
        if args.dry_run:
            print(f"[dry-run] would process {ref.service}/{ref.slug}")
            results.append(ShepherdResult(ref=ref, decision="dry-run"))
            continue
        # Production mode: apply_followup forks the implementer with
        # --review-feedback. Tests pass skip_subprocess=True via
        # process_one() to avoid forking real shells. ``state_dir`` is
        # threaded through so a non-default ``--state-dir`` is honoured
        # by every helper (find_pr_for_proposal, apply_followup, ...)
        # rather than silently falling back to the env-driven default.
        result = process_one(ref, state_dir=state_dir)
        results.append(result)
        if result.decision == "address-review":
            spent_estimate += per_call_estimate

    _print_summary(results)


if __name__ == "__main__":
    main()
