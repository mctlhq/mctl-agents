"""Tier 2 implementer — turns an `accepted` proposal into a real PR.

Pipeline per proposal:
    1. Find proposals/<slug>/.status.yaml with status: accepted
    2. Mark .status.yaml as `in-progress` (pre-commit so a crashed orchestrator
       won't be re-attempted on next run unless --force is passed).
    3. `gh repo clone mctlhq/<service>` to /tmp/impl-<service>-<slug>-<ts>/
    4. Create branch `feat/agents-<slug>` in the cloned repo.
    5. Run the implementer Claude sub-agent with cwd=cloned-repo and
       PROPOSAL_DIR pointing at the gitops proposal directory. The agent
       reads requirements.md / design.md / tasks.md, edits files, and
       commits — but does NOT push (orchestrator handles push + PR open).
    6. `git push -u origin <branch>` from the cloned repo.
    7. `gh pr create` with title/body referencing the proposal.
    8. Update .status.yaml → `implemented`, write the PR URL.

Auth:
    Uses GITHUB_TOKEN from env (Sub-plan B introduces this in the
    mctl-agents-secrets ExternalSecret as `github-token` Vault key, surfaced
    via `dataFrom: extract`). The PAT must have `repo` write scope on
    `mctlhq/*`. When sub-plan B has not yet landed, GITHUB_TOKEN is empty
    and `gh` CLI falls back to the device-flow login — the script will fail
    fast in that case (set --dry-run to verify spec parsing without auth).

Idempotency:
    A proposal whose .status.yaml is already `in-progress` is skipped on the
    next run unless --force is passed. The owner can manually flip the file
    back to `accepted` to retry.

Usage:
    python -m orchestrator.run_implementer
    python -m orchestrator.run_implementer --service mctl-web
    python -m orchestrator.run_implementer --service mctl-web --slug wrangler-cve-0933
    python -m orchestrator.run_implementer --slug wrangler-cve-0933 --force
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anyio
import yaml
from claude_agent_sdk import query

from config.settings import (
    AGENTS_DIR,
    SERVICE_AGENT_MODEL,
    SERVICES,
)
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_implementer_agent_options


# ---------------------------------------------------------------------------
# State directory resolution.
# In the cluster, the orchestrator container has /workdir/mctl-gitops/
# mounted (see entrypoint.sh + cwft-mctl-agents-implement.yaml). Locally, the
# user can override via STATE_DIR env or pass --state-dir.
# ---------------------------------------------------------------------------
DEFAULT_STATE_DIR = Path(
    os.getenv(
        "STATE_DIR",
        "/Users/dmitriimashkov/PycharmProjects/mctlhq/mctl-gitops/platform-gitops/agents-state",
    )
)


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


# ---------------------------------------------------------------------------
# .status.yaml IO
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


def update_status_yaml(
    ref: ProposalRef,
    new_status: str,
    pr: Optional[str] = None,
    notes: Optional[str] = None,
    actor: str = "mctl-agents[bot]",
) -> None:
    """Write .status.yaml back to the gitops worktree.

    The file is keep-it-simple YAML — no comments preserved (we don't need
    ruamel for this). A trailing newline is added so `git diff` shows the
    last-line change cleanly.
    """
    payload = {
        "status": new_status,
        "updated_at": _now_iso(),
        "updated_by": actor,
    }
    if pr is not None:
        payload["pr"] = pr
    if notes is not None:
        payload["notes"] = notes

    ref.status_path.parent.mkdir(parents=True, exist_ok=True)
    with ref.status_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
    ref.status = new_status


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def find_accepted_proposals(
    state_dir: Path,
    service_filter: Optional[str] = None,
    slug_filter: Optional[str] = None,
) -> list[ProposalRef]:
    """Glob agents-state and return all proposals with status == accepted.

    Filters are applied AFTER status filter — they narrow, never broaden.
    A directory without .status.yaml is treated as `proposed` (default per
    shared contract) and is therefore skipped here (Tier 2 only acts on
    `accepted`).
    """
    if not state_dir.is_dir():
        raise SystemExit(f"State dir not found: {state_dir}")

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
            if status == "accepted":
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
def _run(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Thin wrapper over subprocess.run with consistent logging."""
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


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
    """
    src = AGENTS_DIR / service / ".claude" / "agents" / "implementer.md"
    if not src.exists():
        raise SystemExit(f"Implementer agent template not found: {src}")
    dst_dir = target / ".claude" / "agents"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / "implementer.md")


def _build_prompt(ref: ProposalRef) -> str:
    """Prompt that delegates to the `implementer` sub-agent.

    The sub-agent is told (in its frontmatter and body) to read the spec
    files via $PROPOSAL_DIR, edit minimal files in cwd, run a brief sanity
    check, and `git commit` — but NOT push.
    """
    branch = f"feat/agents-{ref.slug}"
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
- No emoji in code or commit messages.
- English only.
"""


async def _run_implementer_agent(repo_dir: Path, prompt: str) -> None:
    options = build_implementer_agent_options(repo_dir, SERVICE_AGENT_MODEL)
    async for message in query(prompt=prompt, options=options):
        print(message)


def _has_new_commits(repo_dir: Path) -> bool:
    """True iff the implementer actually committed something on the branch."""
    proc = _run(["git", "log", "--oneline", "origin/HEAD..HEAD"], cwd=repo_dir, check=False)
    return bool(proc.stdout.strip())


def _push_and_open_pr(repo_dir: Path, ref: ProposalRef) -> str:
    branch = f"feat/agents-{ref.slug}"
    _run(["git", "push", "-u", "origin", branch], cwd=repo_dir)

    title = f"feat(agents): {ref.slug}"
    body = (
        f"Implements accepted proposal `{ref.service}/{ref.slug}`.\n\n"
        f"Spec: https://github.com/mctlhq/mctl-gitops/tree/main/platform-gitops/"
        f"agents-state/{ref.service}/proposals/{ref.slug}/\n\n"
        f"Generated by mctl-agents Tier 2 implementer. Review carefully — "
        f"this PR was opened automatically and the spec may have gaps."
    )
    proc = _run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch, "--base", "main"],
        cwd=repo_dir,
    )
    pr_url = proc.stdout.strip().splitlines()[-1]
    print(f"✓ PR opened: {pr_url}")
    return pr_url


def implement_one(ref: ProposalRef, force: bool = False, dry_run: bool = False) -> ImplementResult:
    """Implement a single accepted proposal. Returns ImplementResult."""
    if ref.status == "in-progress" and not force:
        return ImplementResult(
            ref=ref,
            pr_url=None,
            skipped_reason="already in-progress (a previous attempt may have died); pass --force to retry",
        )

    if dry_run:
        print(f"[dry-run] would implement {ref.service}/{ref.slug}")
        return ImplementResult(ref=ref, pr_url=None, skipped_reason="dry-run")

    # 1. Mark in-progress BEFORE doing real work — lets a parallel runner skip.
    update_status_yaml(ref, "in-progress")

    target = None
    try:
        # 2. Clone target sibling repo.
        target = _clone_target(ref.service, ref.slug)

        # 3. Branch.
        branch = f"feat/agents-{ref.slug}"
        _run(["git", "checkout", "-b", branch], cwd=target)

        # 4. Drop the implementer sub-agent into the clone's .claude/.
        _stage_implementer_agent(target, ref.service)

        # 5. Run the SDK with PROPOSAL_DIR pointing at the gitops worktree.
        os.environ["PROPOSAL_DIR"] = str(ref.proposal_dir.resolve())
        prompt = _build_prompt(ref)
        anyio.run(_run_implementer_agent, target, prompt)

        # 6. Did the agent actually commit something?
        if not _has_new_commits(target):
            update_status_yaml(
                ref,
                "accepted",
                notes="implementer produced no commits; reverted to accepted",
            )
            return ImplementResult(
                ref=ref,
                pr_url=None,
                error="implementer produced no commits",
            )

        # 7. Push + PR.
        pr_url = _push_and_open_pr(target, ref)

        # 8. Mark implemented.
        update_status_yaml(ref, "implemented", pr=pr_url)
        return ImplementResult(ref=ref, pr_url=pr_url)

    except subprocess.CalledProcessError as e:
        msg = f"shell step failed: {' '.join(e.cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}"
        # Leave .status.yaml as in-progress so operator notices the wedge.
        return ImplementResult(ref=ref, pr_url=None, error=msg)
    except Exception as e:  # pragma: no cover — defensive
        return ImplementResult(ref=ref, pr_url=None, error=f"{type(e).__name__}: {e}")
    finally:
        # Keep target dir for post-mortem on failure; clean only on success.
        if target and target.exists():
            try:
                shutil.rmtree(target)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Tier 2 implementer — open PRs from accepted proposals")
    ap.add_argument("--service", default="", help=f"Filter by service (one of: {', '.join(SERVICES)})")
    ap.add_argument("--slug", default="", help="Filter by proposal slug")
    ap.add_argument("--force", action="store_true", help="Re-run a proposal stuck in `in-progress`")
    ap.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Path to platform-gitops/agents-state/ (defaults to STATE_DIR env)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Discover only; don't clone or run the SDK")
    args = ap.parse_args()

    if args.service and args.service not in SERVICES:
        print(f"Unknown service '{args.service}'. Available: {', '.join(SERVICES)}", file=sys.stderr)
        sys.exit(2)

    ensure_auth_for_sdk()

    state_dir = Path(args.state_dir)
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

    results: list[ImplementResult] = []
    for ref in refs:
        print(f"\n=== Implementing {ref.service}/{ref.slug} ===")
        results.append(implement_one(ref, force=args.force, dry_run=args.dry_run))

    print("\n=== Summary ===")
    fail = 0
    for r in results:
        if r.pr_url:
            print(f"  ✓ {r.ref.service}/{r.ref.slug} → {r.pr_url}")
        elif r.skipped_reason:
            print(f"  · {r.ref.service}/{r.ref.slug} skipped: {r.skipped_reason}")
        else:
            fail += 1
            print(f"  ✗ {r.ref.service}/{r.ref.slug} failed: {r.error}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
