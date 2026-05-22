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
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import anyio
import yaml
from claude_agent_sdk import query

from config.settings import SERVICES, SHEPHERD_DIR, SHEPHERD_MODEL
from orchestrator import run_implementer
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import SHEPHERD_BUDGET_USD, build_shepherd_options


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
    pr_url: Optional[str] = None
    status_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.status_path = self.proposal_dir / ".status.yaml"


@dataclass
class CodexFinding:
    """A single P1 or P2 finding parsed out of a codex review comment.

    `commit_id` is the SHA the comment is anchored to (line-anchored
    review comments carry a `commit_id` field; top-level issue comments
    do not — those use `created_at > head_pushed_at` instead).
    """

    body: str
    path: Optional[str]
    line: Optional[int]
    commit_id: Optional[str]
    created_at: Optional[str]
    severity: str  # "P1" or "P2"


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
    merge_commit: Optional[str]
    close_comment_or_default: str
    head_sha: str
    head_pushed_at: Optional[str]   # ISO 8601, used to anchor codex signals
    merge_state_status: str         # mergeStateStatus from GraphQL
    checks_green: bool              # required checks all SUCCESS
    is_draft: bool


@dataclass
class ShepherdResult:
    ref: ProposalRef
    decision: str
    error: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# .status.yaml IO — a leaner re-implementation of run_implementer.py's
# helpers. The shepherd reads more fields (review_attempts, pr) and
# preserves them across writes.
# ---------------------------------------------------------------------------
def _load_status(path: Path) -> dict:
    """Parse .status.yaml. Missing file → {}; default status is `proposed`."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping, got {type(data).__name__}")
    return data


def _now_iso() -> str:
    """RFC 3339 UTC timestamp without microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    existing = _load_status(ref.status_path)
    payload = dict(existing)
    payload["status"] = new_status
    payload["updated_at"] = _now_iso()
    payload["updated_by"] = actor
    for k, v in fields.items():
        if v is None:
            payload.pop(k, None)
        else:
            payload[k] = v

    ref.status_path.parent.mkdir(parents=True, exist_ok=True)
    with ref.status_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
    ref.status = new_status
    if "review_attempts" in fields and fields["review_attempts"] is not None:
        ref.review_attempts = int(fields["review_attempts"])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_refs(
    state_dir: Path,
    service_filter: Optional[str] = None,
    slug_filter: Optional[str] = None,
) -> list[ProposalRef]:
    """Glob agents-state for proposals in SHEPHERD_INPUT_STATUSES.

    Both `implemented` and `review-fixing` are included per
    requirements.md L41-58. Proposals without a `pr:` URL are skipped —
    the shepherd has nothing to do until the implementer opens a PR.
    Services in SHEPHERD_SKIP_SERVICES are skipped entirely — they are
    owned by another PR lifecycle (e.g. pr-steward).
    """
    if not state_dir.is_dir():
        raise SystemExit(f"State dir not found: {state_dir}")

    refs: list[ProposalRef] = []
    for service_dir in sorted(state_dir.iterdir()):
        if not service_dir.is_dir() or service_dir.name.startswith("_"):
            continue
        service = service_dir.name
        if service in SHEPHERD_SKIP_SERVICES:
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
            except Exception as e:
                print(f"warn: {service}/{slug}: failed to parse .status.yaml ({e}); skipping")
                continue
            status = data.get("status", "proposed")
            if status not in SHEPHERD_INPUT_STATUSES:
                continue
            pr_url = data.get("pr")
            if not pr_url:
                # No PR yet — nothing for the shepherd to do.
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
def _run(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Thin wrapper over subprocess.run with consistent logging."""
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def _gh_api_json(args: list[str]) -> Any:
    """Run `gh api ...` and parse stdout as JSON. Empty stdout → None."""
    proc = _run(["gh", "api", *args])
    out = proc.stdout.strip()
    if not out:
        return None
    return json.loads(out)


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
    state_dir: Optional[Path] = None,
) -> Optional[PRSnapshot]:
    """Read the linked PR for a proposal.

    Returns None if `.status.yaml` has no `pr:` URL or the PR cannot be
    fetched. Crucially this does NOT filter to state=open — closed and
    merged PRs are returned too, so the next tick can flip the proposal
    to its terminal state (see requirements.md L41-58).
    """
    sd = state_dir or DEFAULT_STATE_DIR
    status_path = sd / service / "proposals" / slug / ".status.yaml"
    data = _load_status(status_path)
    pr_url = data.get("pr")
    if not pr_url:
        return None
    owner, repo, number = _parse_pr_url(pr_url)
    return _fetch_pr_snapshot(f"{owner}/{repo}", number)


def _fetch_pr_snapshot(repo: str, number: int) -> Optional[PRSnapshot]:
    """gh pr view + a small GraphQL probe to assemble a PRSnapshot.

    `gh pr view` already exposes most of what we need (state, merged,
    mergeStateStatus, headRefOid, statusCheckRollup). The head push
    timestamp is not on `gh pr view --json` so we read it from the PR
    timeline via `gh api`.
    """
    fields = (
        "number,headRefName,headRefOid,mergeStateStatus,state,merged,"
        "mergedAt,mergeCommit,closedAt,statusCheckRollup,isDraft,"
        "url,baseRepository"
    )
    try:
        view = _gh_api_json([
            "graphql", "-f",
            f"query=query($owner:String!,$repo:String!,$number:Int!){{repository(owner:$owner,name:$repo){{pullRequest(number:$number){{number state merged mergedAt mergeStateStatus isDraft headRefOid baseRefName mergeCommit{{oid}} timelineItems(itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT,PULL_REQUEST_COMMIT],last:50){{nodes{{__typename ... on PullRequestCommit{{commit{{oid committedDate}}}} ... on HeadRefForcePushedEvent{{createdAt afterCommit{{oid}}}}}}}} commits(last:1){{nodes{{commit{{oid committedDate pushedDate}}}}}} statusCheckRollup{{state}}}}}}}}",
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
    head_pushed_at: Optional[str] = None
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
    # hook-enabled repos) would stall here despite a clean codex review.
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
    )


def _iso_gt(a: Optional[str], b: Optional[str]) -> bool:
    """True iff a > b in ISO 8601 lexicographic order. Both must be set."""
    if not a or not b:
        return False
    return a > b


def _within_settle_window(
    head_pushed_at: Optional[str], now: datetime, settle_min: int
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
        pushed = pushed.replace(tzinfo=timezone.utc)
    if pushed > now:
        # A future-dated head (e.g. committedDate fallback on a commit authored
        # with a skewed clock) would make now - pushed negative, which is < the
        # window and would hold the PR forever. Treat it as outside the window.
        return False
    return now - pushed < timedelta(minutes=settle_min)


def _extract_severity(body: str) -> Optional[str]:
    """Find a `![P<n> Badge]` marker in a codex finding body. None if absent.

    Per tasks.md T2: codex prefixes findings with a Markdown badge image
    whose alt text is exactly `P1 Badge`, `P2 Badge`, etc. Match with a
    plain substring lookup; a regex isn't necessary.
    """
    for sev in ("P1", "P2", "P3"):
        if f"![{sev} Badge]" in body:
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
        print(f"warn: codex reviews fetch failed for {pr.repo}#{pr.number}: {e.stderr.strip()}")
        reviews = []
    for r in reviews:
        if (r.get("user") or {}).get("login") != REVIEW_BOT:
            continue
        commit_id = r.get("commit_id")
        if commit_id and commit_id == pr.head_sha:
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
        if (c.get("user") or {}).get("login") != REVIEW_BOT:
            continue
        commit_id = c.get("commit_id")
        if commit_id and commit_id == pr.head_sha:
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
    latest_trigger: Optional[dict] = None
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
    now: Optional[datetime] = None,
) -> tuple[str, Any]:
    """Return one of the five decisions per design.md L62-75.

    Pure: only depends on its arguments. `now` is injected so the settling
    window stays deterministic in tests; it defaults to the current UTC time.
    The 3-attempt cap on address-review loops lives in the OUTER state machine
    (process_one), NOT here, so this function stays trivially testable with
    hand-built fixtures.
    """
    if now is None:
        now = datetime.now(timezone.utc)
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
        parts.append(
            f"--- Finding {i} ({f.severity}) ---\n"
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
    state_dir: Optional[Path] = None,
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
        proc = subprocess.run(cmd, check=False, text=True)
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
        # `EXIT_BRANCH_MISSING_ON_ORIGIN` = 43). Anything else (1, 137,
        # 2, ...) is treated as transient — we cannot tell the kind from
        # the code alone.
        deterministic_codes = {
            run_implementer.EXIT_NO_FOLLOWUP_COMMITS,
            run_implementer.EXIT_BRANCH_MISSING_ON_ORIGIN,
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
def merge_pr(pr: PRSnapshot) -> tuple[bool, Optional[str]]:
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
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
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
    state_dir: Optional[Path] = None,
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

    # Dead-letter gate for `in-progress` proposals. Tier 2 owns the
    # proposal whenever status is `in-progress` AND the PR is OPEN or
    # MERGED — touching it would race the implementer's own
    # `update_status_yaml(ref, "implemented", pr=...)` call. Only the
    # closed-without-merge shape is a true dead-letter (Tier 2 finished
    # `gh pr create`, status flip dropped, operator then closed the
    # PR). decide() already returns flip-to-rejected for this case, so
    # we fall through after the guard.
    if ref.status == "in-progress" and not pr.closed_unmerged:
        print(
            f"info: {ref.service}/{ref.slug}: status=in-progress with "
            f"pr.state={pr.state} merged={pr.merged}; deferring to Tier 2"
        )
        return ShepherdResult(
            ref=ref,
            decision="wait",
            notes="in-progress with non-closed PR; Tier 2 owns",
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
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

    # Skip SDK auth init in dry-run mode: --dry-run is documented as
    # discovery-only and must work in environments without Claude
    # credentials (read-only ops checks, CI inventory runs).
    if not args.dry_run:
        ensure_auth_for_sdk()

    state_dir = Path(args.state_dir)
    refs = _discover_refs(
        state_dir,
        service_filter=args.service or None,
        slug_filter=args.slug or None,
    )

    if not refs:
        print("info: no implemented/review-fixing proposals with a PR found.")
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

    # Greppable summary — required by tasks.md task 2 DoD.
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


if __name__ == "__main__":
    main()
