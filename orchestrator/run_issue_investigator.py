"""Issue-investigator — turns a GitHub issue into an `accepted`-pending proposal.

This is the issue-driven entry point into the mctl-agents pipeline. Where the
proactive researcher/analyst/spec-writer rotation decides *itself* what to
improve, the investigator takes a human-filed feature request and converts it
into the same proposal triplet the rest of the pipeline already understands.

Pipeline for one issue:
    1. Parse --issue-url into owner / repo / number. Owner must be `mctlhq`.
    2. `gh issue view` the issue (title, body, state).
    3. Derive a deterministic slug `issue-<N>-<kebab-title>` and locate the
       proposal dir agents-state/<service>/proposals/<slug>/.
    4. Idempotency guard: if a .status.yaml already exists and is past
       `proposed` (accepted / in-progress / implemented / merged / ...),
       skip — an implementation is already in flight and must not be
       clobbered. A missing file or `proposed` status is overwritable.
    5. `gh repo clone mctlhq/<service>` (read-only) so the agent can ground
       the design in real code.
    6. Run the Claude Agent SDK: the agent reads the issue (passed in the
       prompt) + the cloned code and writes requirements.md / design.md /
       tasks.md into $PROPOSAL_DIR.
    7. Write .status.yaml — `status: proposed` plus a `source` block linking
       the proposal back to the GitHub issue, and a `control` block marking
       it as requiring human approval.
    8. Comment on the issue with a link to the proposal.

The proposal stops at `status: proposed`. A human reviews the spec and flips
.status.yaml to `accepted`, after which the existing Tier 2 implementer opens
a PR (which carries `Closes <repo>#<N>` thanks to the `source` block).

Auth:
    GITHUB_TOKEN from env (gh CLI honors it) — needs `repo` read on the
    target repo plus `issues: write` for the closing comment.

Usage:
    python -m orchestrator.run_issue_investigator \\
        --issue-url https://github.com/mctlhq/mctl-telegram/issues/123
    python -m orchestrator.run_issue_investigator --issue-url <url> --dry-run
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anyio
import yaml
from claude_agent_sdk import query

from config.settings import SERVICE_AGENT_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_issue_investigator_options


DEFAULT_STATE_DIR = Path(
    os.getenv(
        "STATE_DIR",
        "/workdir/mctl-gitops/platform-gitops/agents-state",
    )
)
INVESTIGATOR_MODEL = os.getenv("ISSUE_INVESTIGATOR_MODEL", SERVICE_AGENT_MODEL)

# A proposal whose .status.yaml is missing or still `proposed` can be
# (re-)investigated. Anything past that means the implementer/shepherd has
# taken ownership — re-running the investigator would clobber in-flight work.
_OVERWRITABLE_STATUSES = {"proposed"}

# https://github.com/<owner>/<repo>/issues/<n>
_ISSUE_URL_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/(\d+)/?$"
)


@dataclass
class IssueRef:
    owner: str
    repo: str          # repo name only, e.g. "mctl-telegram"
    number: int
    url: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class IssueData:
    ref: IssueRef
    title: str
    body: str
    state: str         # "OPEN" / "CLOSED"


def _now_iso() -> str:
    """RFC 3339 UTC timestamp without microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Thin wrapper over subprocess.run with consistent logging."""
    print(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def parse_issue_url(url: str) -> IssueRef:
    """Parse a GitHub issue URL into an IssueRef. Raises SystemExit on a
    malformed URL or a non-`mctlhq` owner (the pipeline only touches
    repos under the mctlhq org)."""
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        raise SystemExit(
            f"Not a GitHub issue URL: {url!r}\n"
            f"Expected: https://github.com/<owner>/<repo>/issues/<number>"
        )
    owner, repo, number = m.group(1), m.group(2), int(m.group(3))
    if owner != "mctlhq":
        raise SystemExit(
            f"Issue owner {owner!r} is not 'mctlhq'; the investigator only "
            f"handles repos under the mctlhq org."
        )
    return IssueRef(owner=owner, repo=repo, number=number, url=url.strip())


def slugify(text: str, max_len: int = 40) -> str:
    """Lowercase kebab-case slug fragment from free text. Empty input
    collapses to 'untitled' so the slug is never bare `issue-<N>-`."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "untitled"


def build_slug(issue_number: int, title: str) -> str:
    """Deterministic proposal slug: `issue-<N>-<kebab-title>`.

    Deterministic on (number, title) so a re-run for the same issue targets
    the same proposal directory instead of spawning a `-v2` duplicate.
    """
    return f"issue-{issue_number}-{slugify(title)}"


def gh_issue_view(url: str) -> IssueData:
    """Fetch issue title / body / state via `gh issue view --json`."""
    proc = _run([
        "gh", "issue", "view", url,
        "--json", "number,title,body,state,url",
    ])
    data = json.loads(proc.stdout)
    ref = parse_issue_url(data["url"])
    return IssueData(
        ref=ref,
        title=data.get("title") or "",
        body=data.get("body") or "",
        state=data.get("state") or "",
    )


def _load_status(path: Path) -> dict:
    """Parse .status.yaml. Missing file → {}."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping, got {type(data).__name__}")
    return data


def _clone_repo(full_repo: str, slug: str) -> Path:
    """Read-only `gh repo clone` of the target repo to a fresh tmp dir."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = Path(tempfile.gettempdir()) / f"investigate-{slug}-{ts}"
    if target.exists():
        shutil.rmtree(target)
    # Shallow — the investigator only reads the current tree, never history.
    _run(["gh", "repo", "clone", full_repo, str(target), "--", "--depth=1"])
    return target


def write_status_yaml(proposal_dir: Path, issue: IssueData) -> Path:
    """Write the initial .status.yaml for an issue-driven proposal.

    Status starts at `proposed`. The `source` block links the proposal back
    to the originating GitHub issue — the Tier 2 implementer reads it to add
    `Closes <repo>#<N>` to the PR, and `update_status_yaml` preserves it
    through every later transition.
    """
    payload = {
        "status": "proposed",
        "updated_at": _now_iso(),
        "updated_by": "mctl-agents[bot]",
        "source": {
            "type": "github_issue",
            "repo": issue.ref.full_repo,
            "issue": issue.ref.number,
            "url": issue.ref.url,
        },
        "control": {
            "requires_human_approval": True,
        },
    }
    proposal_dir.mkdir(parents=True, exist_ok=True)
    status_path = proposal_dir / ".status.yaml"
    with status_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
    return status_path


def _gitops_tree_url(service: str, slug: str) -> str:
    return (
        "https://github.com/mctlhq/mctl-gitops/tree/main/platform-gitops/"
        f"agents-state/{service}/proposals/{slug}/"
    )


def post_proposal_comment(issue_url: str, service: str, slug: str) -> None:
    """Comment on the issue with a link to the freshly written proposal."""
    body = (
        "mctl-agents issue-investigator has analyzed this issue and created "
        "a proposal:\n\n"
        f"{_gitops_tree_url(service, slug)}\n\n"
        "Status: `proposed` — pending human approval. Review `requirements.md`, "
        "`design.md` and `tasks.md`, then flip the proposal's `.status.yaml` "
        "to `accepted` to let the Tier 2 implementer open a PR."
    )
    _run(["gh", "issue", "comment", issue_url, "--body", body])


def _build_prompt(issue: IssueData, service: str, slug: str) -> str:
    """Prompt for the investigator SDK agent.

    The agent's cwd is a read-only clone of the target repo; it writes the
    proposal triplet into $PROPOSAL_DIR. It does NOT write .status.yaml —
    the Python wrapper owns that (deterministic `source` block).
    """
    return f"""\
**Output language: English only. Write every file in English.**
**No human is present. Do not ask for input. Work with what you have.**

You are the mctl-agents **issue-investigator**. Turn a GitHub issue into a
spec-driven proposal that the Tier 2 implementer can later build.

## The issue

- Repo: `{issue.ref.full_repo}`
- Issue: #{issue.ref.number} — {issue.title}
- URL: {issue.ref.url}
- State: {issue.state}

Issue body:
---
{issue.body}
---

## Your working context

- Your current working directory is a fresh, read-only clone of
  `{issue.ref.full_repo}`. Explore it with Glob / Grep / Read / Bash to
  understand the architecture, stack, and conventions BEFORE you write the
  design. Ground every design decision in code you actually read.
- Read the repo's `CLAUDE.md` (cwd root, if present) for conventions.
- `$PROPOSAL_DIR` (env var) is where you write the proposal files.

## What to produce

Write exactly three files into `$PROPOSAL_DIR`:

### 1. requirements.md (EARS notation)
```
# <Proposal title>

## Context
1-2 paragraphs: what the issue asks for, why it matters.

## User stories
- AS a <role> I WANT <capability> SO THAT <value>

## Acceptance criteria (EARS)
- WHEN <trigger> THE SYSTEM SHALL <response>
- WHILE <state> THE SYSTEM SHALL <invariant>
- IF <condition> THEN THE SYSTEM SHALL <response>

## Out of scope
- what is explicitly NOT part of this proposal

## Open questions
- Anything the issue left ambiguous. If the issue is fully specified,
  write "None." Do NOT block on open questions — record them and proceed
  with the most reasonable interpretation.
```

### 2. design.md
```
# Design: {slug}

## Current state
How the relevant part of `{service}` works today — cite real files/paths
you read in the clone.

## Proposed solution
Architectural description: what changes, where, and why this way.

## Alternatives
2-3 options considered and why they were dropped.

## Platform impact
- Migrations, backward compatibility, resource impact, risks + mitigations.
```

### 3. tasks.md
```
# Tasks: {slug}

- [ ] 1. <task> — DoD: <what "done" means>
- [ ] 2. <task> (depends on 1) — DoD: ...

## Tests
- [ ] T1. <test>

## Rollback
How to roll back if this goes sideways.
```

## Rules

- All three files must agree on the same intent — no contradictions.
- Be concrete: reference real files and symbols from the clone.
- A vague issue still gets a complete proposal — capture the ambiguity in
  `## Open questions`, never stop to ask.
- Do NOT write `.status.yaml` — the orchestrator writes it.
- Do NOT edit the cloned repo — it is read-only scratch.
- No emoji. English only.

## Final message

3-5 lines: the proposal title, the three files you wrote, and anything the
human reviewer should look at carefully (especially open questions).
"""


async def _run_agent(repo_dir: Path, prompt: str, proposal_dir: Path) -> None:
    options = build_issue_investigator_options(repo_dir, INVESTIGATOR_MODEL, proposal_dir)
    async for message in query(prompt=prompt, options=options):
        print(message)


@dataclass
class InvestigateResult:
    service: str
    slug: str
    proposal_dir: Path
    skipped_reason: Optional[str] = None
    error: Optional[str] = None


def investigate(
    issue_url: str,
    state_dir: Path = DEFAULT_STATE_DIR,
    dry_run: bool = False,
) -> InvestigateResult:
    """Investigate one GitHub issue and write a `proposed` proposal."""
    if not state_dir.is_dir():
        raise SystemExit(f"State dir not found: {state_dir}")

    issue = gh_issue_view(issue_url)
    service = issue.ref.repo
    if service not in SERVICES:
        raise SystemExit(
            f"Repo '{service}' is not a known service. Add it to "
            f"config/settings.py SERVICES (NON_ROTATING_SERVICES if it has "
            f"no agents/<svc>/ scaffold) before investigating its issues. "
            f"Known: {', '.join(SERVICES)}"
        )

    slug = build_slug(issue.ref.number, issue.title)
    proposal_dir = state_dir / service / "proposals" / slug
    status_path = proposal_dir / ".status.yaml"

    # Idempotency guard — never clobber a proposal an implementer owns.
    existing = _load_status(status_path)
    existing_status = existing.get("status")
    if existing and existing_status not in _OVERWRITABLE_STATUSES:
        reason = (
            f"proposal {service}/{slug} already at status "
            f"'{existing_status}' — refusing to overwrite in-flight work"
        )
        print(f"⚠️  {reason}")
        return InvestigateResult(service, slug, proposal_dir, skipped_reason=reason)

    if issue.state == "CLOSED":
        print(f"⚠️  Issue {issue.ref.full_repo}#{issue.ref.number} is CLOSED — investigating anyway.")

    if dry_run:
        print(
            f"[dry-run] would investigate {issue.ref.full_repo}#{issue.ref.number}\n"
            f"          service={service} slug={slug}\n"
            f"          proposal_dir={proposal_dir}"
        )
        return InvestigateResult(service, slug, proposal_dir, skipped_reason="dry-run")

    clone = None
    try:
        # 1. Read-only clone so the agent can ground the design in real code.
        clone = _clone_repo(issue.ref.full_repo, slug)

        # 2. Pre-create the proposal dir (add_dirs needs it to exist).
        proposal_dir.mkdir(parents=True, exist_ok=True)

        # 3. Run the SDK agent — writes the requirements/design/tasks triplet.
        prompt = _build_prompt(issue, service, slug)
        anyio.run(_run_agent, clone, prompt, proposal_dir.resolve())

        # 4. Verify the agent produced the triplet.
        missing = [
            name for name in ("requirements.md", "design.md", "tasks.md")
            if not (proposal_dir / name).is_file()
        ]
        if missing:
            return InvestigateResult(
                service, slug, proposal_dir,
                error=f"agent did not write: {', '.join(missing)}",
            )

        # 5. Write .status.yaml (status: proposed + source block).
        write_status_yaml(proposal_dir, issue)

        # 6. Link the proposal back to the issue.
        post_proposal_comment(issue.ref.url, service, slug)

        return InvestigateResult(service, slug, proposal_dir)

    except subprocess.CalledProcessError as e:
        msg = f"shell step failed: {' '.join(e.cmd)}\nstdout: {e.stdout}\nstderr: {e.stderr}"
        return InvestigateResult(service, slug, proposal_dir, error=msg)
    except SystemExit as e:
        return InvestigateResult(service, slug, proposal_dir, error=f"SystemExit: {e}")
    except Exception as e:  # pragma: no cover — defensive
        return InvestigateResult(service, slug, proposal_dir, error=f"{type(e).__name__}: {e}")
    finally:
        # Keep the clone on failure for post-mortem; drop it on success.
        if clone and clone.exists():
            try:
                shutil.rmtree(clone)
            except OSError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Issue-investigator — turn a GitHub issue into a proposal"
    )
    ap.add_argument(
        "--issue-url",
        required=True,
        help="GitHub issue URL, e.g. https://github.com/mctlhq/mctl-telegram/issues/123",
    )
    ap.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Path to platform-gitops/agents-state/ (defaults to STATE_DIR env)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve issue + slug only; don't clone, run the SDK, or comment",
    )
    args = ap.parse_args()

    ensure_auth_for_sdk()

    result = investigate(
        issue_url=args.issue_url,
        state_dir=Path(args.state_dir),
        dry_run=args.dry_run,
    )

    print("\n=== Investigate summary ===")
    if result.error:
        print(f"  ✗ {result.service}/{result.slug} failed: {result.error}")
        sys.exit(1)
    if result.skipped_reason:
        print(f"  · {result.service}/{result.slug} skipped: {result.skipped_reason}")
        return
    print(f"  ✓ {result.service}/{result.slug} → {result.proposal_dir}")
    print(f"    {_gitops_tree_url(result.service, result.slug)}")


if __name__ == "__main__":
    main()
