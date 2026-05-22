"""Issue-poller — central scan that turns `agents:intake`-labelled issues
into mctl-agents proposals.

This is the Phase 2 trigger for the issue-driven entry point. Where
``run_issue_investigator`` handles ONE issue (manually, via the
``mctl_trigger_issue`` MCP tool), the poller scans every open issue under the
`mctlhq` org carrying the `agents:intake` label and runs the investigator
for each. It lets an operator start an investigation by *labelling* an issue
instead of calling an MCP tool — a single in-cluster CronWorkflow
(`mctl-agents-issue-poll`) does the scan.

Pipeline for one poll cycle:
    1. `gh search issues` — list open `mctlhq` issues labelled the configured
       label (default `agents:intake`).
    2. For each issue, in turn:
       a. Skip (label left in place) if the repo is not a known service —
          a misconfiguration the operator should see.
       b. Run ``investigate`` in-process. It is idempotent: a proposal
          already past `proposed` is left untouched.
       c. On success — or on a skip because an implementation is already
          in flight — remove the label so the next cycle does not
          re-investigate and re-spend the SDK budget.
       d. On error, leave the label so the next cycle retries.

Per-issue failures never fail the whole poll: they are logged and the label
is kept for retry. The process exits non-zero only on a global failure
(missing state dir, broken `gh` auth) so the CronWorkflow surfaces a genuine
outage but stays green on the normal "one bad issue" case.

A cycle investigates at most ``--max-issues`` issues (default
``DEFAULT_MAX_ISSUES``). Each investigation spends real SDK budget, so a
mass-label event would otherwise be an uncapped N-runs runaway; any issues
beyond the cap keep their label and are picked up by a later cycle. Pass
``--max-issues 0`` to disable the cap.

Auth:
    GITHUB_TOKEN from env (gh CLI honors it) — needs `repo` read on the
    target repos, plus `issues: write` for the proposal comment and the
    label removal.

Usage:
    python -m orchestrator.run_issue_poller
    python -m orchestrator.run_issue_poller --label agents:intake --dry-run
    python -m orchestrator.run_issue_poller --max-issues 3
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from config.settings import SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.run_issue_investigator import (
    DEFAULT_STATE_DIR,
    IssueRef,
    _run,
    investigate,
    try_parse_issue_url,
)

# `gh search` returns at most this many rows per call. A poll cycle with more
# labelled issues than this is well outside normal operation, but warn rather
# than silently drop the overflow.
_SEARCH_LIMIT = 100

# Issues carrying this label are picked up by the poller. An operator adds it
# to a feature-request issue to hand it to the pipeline; the poller removes it
# once the issue has been turned into a proposal.
DEFAULT_LABEL = "agents:intake"

# A cycle investigates at most this many issues. Each investigation spends
# real SDK budget, so without a cap a mass-label event would fan out into an
# uncapped run of paid investigations. Issues beyond the cap keep their label
# and are handled by a later cycle. `--max-issues 0` disables the cap.
DEFAULT_MAX_ISSUES = 5


def search_labeled_issues(label: str) -> list[IssueRef]:
    """Open `mctlhq` issues carrying ``label``.

    `gh search issues` can also surface pull requests (GitHub conflates the
    two in its search API); anything whose URL is not an `/issues/` URL — or
    is not under the `mctlhq` org — is dropped by ``parse_issue_url``.
    """
    proc = _run([
        "gh", "search", "issues",
        "--owner", "mctlhq",
        "--label", label,
        "--state", "open",
        "--limit", str(_SEARCH_LIMIT),
        "--json", "url",
    ])
    rows = json.loads(proc.stdout or "[]")
    if len(rows) >= _SEARCH_LIMIT:
        # The search is capped — any labelled issues beyond the cap are not
        # seen this cycle. They are picked up once the backlog drains below
        # the cap, but an operator should know the cap was hit.
        print(
            f"WARN: `gh search` returned the {_SEARCH_LIMIT}-row cap — there "
            f"may be more issues labelled '{label}' than handled this cycle."
        )
    refs: list[IssueRef] = []
    seen: set[str] = set()
    for row in rows:
        url = (row.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        ref = try_parse_issue_url(url)
        if ref is None:
            # A PR URL (/pull/…) or an issue outside the mctlhq org — not
            # ours to act on.
            continue
        refs.append(ref)
    return refs


def remove_label(issue_url: str, label: str) -> None:
    """Drop ``label`` from the issue so the next poll cycle skips it."""
    _run(["gh", "issue", "edit", issue_url, "--remove-label", label])


def poll(
    label: str = DEFAULT_LABEL,
    state_dir: Path = DEFAULT_STATE_DIR,
    dry_run: bool = False,
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> int:
    """Run one poll cycle. Returns the count of issues that failed.

    At most ``max_issues`` *known-service* issues are investigated per cycle
    (``max_issues <= 0`` disables the cap). Any beyond the cap keep their label
    and are picked up by a later cycle.
    """
    if not state_dir.is_dir():
        raise SystemExit(f"State dir not found: {state_dir}")

    refs = search_labeled_issues(label)
    if not refs:
        print(f"No open issues labelled '{label}' — nothing to do.")
        return 0

    # The --max-issues cap bounds the number of *paid* investigations per
    # cycle. Apply it only to known-service issues: a non-service issue costs
    # no SDK budget (it is reported and its label kept for the operator), so
    # capping the raw search result would let a wall of mislabelled
    # non-service issues starve a valid one out of every cycle.
    service_refs = [r for r in refs if r.repo in SERVICES]
    other_refs = [r for r in refs if r.repo not in SERVICES]

    if max_issues > 0 and len(service_refs) > max_issues:
        # The dropped issues keep their label (they are never passed to
        # investigate / remove_label below), so the next cycle picks them up.
        print(
            f"WARN: {len(service_refs)} investigable issue(s) labelled "
            f"'{label}' found — capping this cycle at --max-issues={max_issues}; "
            f"the remaining {len(service_refs) - max_issues} keep their label "
            f"for the next cycle."
        )
        service_refs = service_refs[:max_issues]

    # Non-service issues first so a misconfiguration surfaces at the top of
    # the log, then the capped investigable issues.
    refs = other_refs + service_refs
    print(f"Found {len(refs)} issue(s) labelled '{label}':")
    for ref in refs:
        print(f"  - {ref.full_repo}#{ref.number}")

    failures = 0
    for ref in refs:
        print(f"\n=== {ref.full_repo}#{ref.number} ===")

        known_service = ref.repo in SERVICES

        # dry-run is a side-effect-free preview: it neither investigates nor
        # relabels, and never increments the failure count (an unknown
        # service is reported but not counted — see the non-dry-run path).
        if dry_run:
            if known_service:
                print(f"[dry-run] would investigate {ref.url} and remove '{label}'")
            else:
                print(
                    f"[dry-run] would skip — {ref.repo} is not a known "
                    f"service (config/settings.py SERVICES)"
                )
            continue

        if not known_service:
            # A label on a repo the pipeline can't build. Leave the label so
            # the operator notices, rather than silently dropping the request.
            print(
                f"WARN: {ref.repo} is not a known service "
                f"(config/settings.py SERVICES) — skipping, label kept."
            )
            failures += 1
            continue

        try:
            result = investigate(ref.url, state_dir=state_dir)
        except SystemExit as e:
            # Pre-flight rejection from the investigator — treat as a
            # per-issue failure, keep the label.
            print(f"FAIL: investigate raised: {e}")
            failures += 1
            continue

        if result.error:
            print(
                f"FAIL: {result.service}/{result.slug}: {result.error} "
                f"— label kept for retry"
            )
            failures += 1
            continue

        # Success, or skipped because an implementation is already in flight.
        # Either way the issue has been handled — drop the label so the next
        # cycle does not re-run the (paid) investigator on it.
        if result.skipped_reason:
            print(f"OK (skipped): {result.service}/{result.slug} — {result.skipped_reason}")
        else:
            print(f"OK: {result.service}/{result.slug} — proposal written")
        try:
            remove_label(ref.url, label)
            print(f"  removed '{label}' label")
        except subprocess.CalledProcessError as e:
            # The proposal IS written, but the label is still on the issue —
            # so the next cycle re-investigates it (a `proposed` proposal is
            # overwritable) and re-spends the SDK budget. Count it as a
            # failure so a broken label-write permission surfaces instead of
            # silently burning budget every cycle.
            print(
                f"FAIL: {result.service}/{result.slug}: proposal written but "
                f"'{label}' label removal failed ({e.stderr or e}) — fix the "
                f"token's label-write permission or the next cycle repeats."
            )
            failures += 1

    return failures


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Issue-poller — investigate every `agents:intake`-labelled issue"
    )
    ap.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help=f"Issue label to scan for (default: {DEFAULT_LABEL})",
    )
    ap.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help="Path to platform-gitops/agents-state/ (defaults to STATE_DIR env)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching issues only; don't investigate or relabel",
    )
    ap.add_argument(
        "--max-issues",
        type=int,
        default=DEFAULT_MAX_ISSUES,
        help=(
            f"Max issues to investigate per cycle (default: {DEFAULT_MAX_ISSUES}; "
            "0 disables the cap). Each investigation spends SDK budget, so the "
            "cap bounds the cost of a mass-label event; capped-out issues keep "
            "their label and are handled by a later cycle."
        ),
    )
    args = ap.parse_args()
    if args.max_issues < 0:
        ap.error("--max-issues must be >= 0 (use 0 to disable the cap)")

    ensure_auth_for_sdk()

    failures = poll(
        label=args.label,
        state_dir=Path(args.state_dir),
        dry_run=args.dry_run,
        max_issues=args.max_issues,
    )

    print("\n=== Poll summary ===")
    if failures:
        # Per-issue failures are expected and retryable (the label is kept).
        # Surface the count but exit 0 so the CronWorkflow does not flap on a
        # single malformed issue. A global failure raised SystemExit earlier.
        print(f"  {failures} issue(s) failed — labels kept, will retry next cycle.")
    else:
        print("  all matching issues handled.")
    sys.exit(0)


if __name__ == "__main__":
    main()
