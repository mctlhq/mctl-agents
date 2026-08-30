"""Issue-poller — central scan that turns `agents:intake`-labelled issues
into Temporal DevLoopWorkflow runs.

This is the phase-5 cutover of the phase-2 issue-driven trigger: where
``run_issue_investigator`` used to handle ONE issue synchronously and this
poller used to call it in-process for every labelled issue, the poller's job
now shrinks to discovery + dispatch. The actual investigate/approve/
implement pipeline — including the account-2 OAuth rate-limit fallback —
lives inside ``DevLoopWorkflow`` and the Argo CWFTs it submits (see
``orchestrator/temporal/workflows/dev_loop.py``); this module never spends
SDK budget itself, only a cheap Temporal `start_workflow` RPC per issue.

Pipeline for one poll cycle:
    1. `gh search issues` — list open `mctlhq` issues labelled the configured
       label (default `agents:intake`).
    2. For each issue, in turn:
       a. Skip (label left in place) if the repo is not a known service —
          a misconfiguration the operator should see.
       b. Start (or attach to) that issue's DevLoopWorkflow. Temporal's own
          workflow-ID dedup (`dev-loop-{owner}-{repo}-{issue}`,
          ALLOW_DUPLICATE_FAILED_ONLY + USE_EXISTING) makes a repeated start
          against an already-RUNNING workflow a true no-op; a prior SUCCEEDED
          run for the same issue is treated as "already handled" (see
          ``WorkflowAlreadyStartedError`` handling below), not a failure. A
          prior FAILED run starts fresh, so a re-added label is the retry
          affordance after the cause is fixed.
       c. Remove the label so the next cycle does not re-scan an issue
          already handed off to the pipeline.
       d. On error, leave the label so the next cycle retries.

Per-issue failures never fail the whole poll: they are logged and the label
is kept for retry. The process exits non-zero only on a global failure —
broken `gh` auth (crashes before the per-issue loop even starts), or a
cycle with dispatchable issues where connecting to the Temporal frontend
itself fails (checked once up front, not per issue) — so the CronWorkflow
surfaces a genuine outage but stays green on the normal "one bad issue"
case.

A cycle dispatches at most ``--max-issues`` issues (default
``DEFAULT_MAX_ISSUES``) — starting a workflow is cheap, but this keeps a
mass-label event from fanning out into an uncapped run of Argo submissions in
one tick. Any issues beyond the cap keep their label and are picked up by a
later cycle. Pass ``--max-issues 0`` to disable the cap.

Auth:
    GITHUB_TOKEN from env (gh CLI honors it) — needs `repo` read on the
    target repos, plus `issues: write` for the label removal.
    TEMPORAL_ADDRESS / TEMPORAL_NAMESPACE from env — see
    orchestrator/temporal/start.py.

Usage:
    python -m orchestrator.run_issue_poller
    python -m orchestrator.run_issue_poller --label agents:intake --dry-run
    python -m orchestrator.run_issue_poller --max-issues 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass

from temporalio.exceptions import WorkflowAlreadyStartedError

from config.settings import SERVICES
from orchestrator.run_issue_investigator import IssueRef, _run, try_parse_issue_url
from orchestrator.temporal.start import connect, start_dev_loop_workflow, workflow_id_for

# `gh search` returns at most this many rows per call. A poll cycle with more
# labelled issues than this is well outside normal operation, but warn rather
# than silently drop the overflow.
_SEARCH_LIMIT = 100

# Issues carrying this label are picked up by the poller. An operator adds it
# to a feature-request issue to hand it to the pipeline; the poller removes it
# once the issue has been handed off to a DevLoopWorkflow.
DEFAULT_LABEL = "agents:intake"

# A cycle dispatches at most this many issues per tick. Issues beyond the cap
# keep their label and are handled by a later cycle. `--max-issues 0`
# disables the cap.
DEFAULT_MAX_ISSUES = 5


@dataclass
class PollResult:
    started: int
    failures: int


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


async def poll(
    label: str = DEFAULT_LABEL,
    dry_run: bool = False,
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> PollResult:
    """Run one poll cycle. Returns how many issues were started vs failed.

    At most ``max_issues`` *known-service* issues are dispatched per cycle
    (``max_issues <= 0`` disables the cap). Any beyond the cap keep their label
    and are picked up by a later cycle.
    """
    refs = search_labeled_issues(label)
    if not refs:
        print(f"No open issues labelled '{label}' — nothing to do.")
        return PollResult(started=0, failures=0)

    # The --max-issues cap bounds the number of Argo submissions a single
    # tick can fan out into. Apply it only to known-service issues: a
    # non-service issue never reaches start_dev_loop_workflow (it is
    # reported and its label kept for the operator), so capping the raw
    # search result would let a wall of mislabelled non-service issues starve
    # a valid one out of every cycle.
    service_refs = [r for r in refs if r.repo in SERVICES]
    other_refs = [r for r in refs if r.repo not in SERVICES]

    if max_issues > 0 and len(service_refs) > max_issues:
        # The dropped issues keep their label (they are never passed to
        # start_dev_loop_workflow / remove_label below), so the next cycle
        # picks them up.
        print(
            f"WARN: {len(service_refs)} dispatchable issue(s) labelled "
            f"'{label}' found — capping this cycle at --max-issues={max_issues}; "
            f"the remaining {len(service_refs) - max_issues} keep their label "
            f"for the next cycle."
        )
        service_refs = service_refs[:max_issues]

    # Non-service issues first so a misconfiguration surfaces at the top of
    # the log, then the capped dispatchable issues.
    refs = other_refs + service_refs
    print(f"Found {len(refs)} issue(s) labelled '{label}':")
    for ref in refs:
        print(f"  - {ref.full_repo}#{ref.number}")

    # Connect once per cycle, not once per issue — reused across every
    # start_dev_loop_workflow call below. Checked up front, before the
    # per-issue loop, so a Temporal-frontend outage is a genuine GLOBAL
    # failure (raises SystemExit, non-zero exit) rather than N identical
    # per-issue soft failures that would leave the CronWorkflow green.
    # Skipped entirely for dry-run (side-effect-free preview) and when there
    # is nothing dispatchable this cycle (no point requiring connectivity).
    client = None
    if not dry_run and service_refs:
        try:
            client = await connect()
        except Exception as e:
            raise SystemExit(f"Could not connect to Temporal — {e}") from e

    started = 0
    failures = 0
    for ref in refs:
        print(f"\n=== {ref.full_repo}#{ref.number} ===")

        known_service = ref.repo in SERVICES

        # dry-run is a side-effect-free preview: it neither starts a
        # workflow nor relabels.
        if dry_run:
            if known_service:
                wf_id = workflow_id_for(ref.url)
                print(f"[dry-run] would start/attach {wf_id} and remove '{label}'")
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
            handle = await start_dev_loop_workflow(ref.url, client=client)
        except WorkflowAlreadyStartedError:
            # A prior DevLoopWorkflow for this issue already ran to
            # SUCCESSFUL completion (ALLOW_DUPLICATE_FAILED_ONLY rejects
            # id-reuse only against a run that succeeded) — this issue has
            # been handled by the pipeline once; a re-added label is not a
            # new request. Not a failure: drop the label like any other
            # handled issue. A failed run does NOT land here: it restarts,
            # which is how an operator retries after fixing the cause.
            print(f"OK (already handled): {workflow_id_for(ref.url)} already ran to completion")
        except Exception as e:  # noqa: BLE001 — surfaced as a per-issue failure, label kept for retry
            print(f"FAIL: could not start workflow for {ref.url}: {e} — label kept for retry")
            failures += 1
            continue
        else:
            print(f"OK: started/attached {handle.id} (run_id={handle.result_run_id})")
            started += 1

        try:
            remove_label(ref.url, label)
            print(f"  removed '{label}' label")
        except subprocess.CalledProcessError as e:
            # The workflow IS started, but the label is still on the issue —
            # so the next cycle re-dispatches it (harmless no-op via
            # id_conflict_policy=USE_EXISTING while it's running, but noisy,
            # and once it succeeds ALLOW_DUPLICATE_FAILED_ONLY turns every
            # subsequent cycle into a logged failure). Count it as a failure so a broken
            # label-write permission surfaces instead of silently repeating.
            print(
                f"FAIL: workflow started but '{label}' label removal failed "
                f"({e.stderr or e}) — fix the token's label-write permission "
                f"or the next cycle repeats."
            )
            failures += 1

    return PollResult(started=started, failures=failures)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Issue-poller — start a DevLoopWorkflow for every `agents:intake`-labelled issue"
    )
    ap.add_argument(
        "--label",
        default=DEFAULT_LABEL,
        help=f"Issue label to scan for (default: {DEFAULT_LABEL})",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching issues only; don't start workflows or relabel",
    )
    ap.add_argument(
        "--max-issues",
        type=int,
        default=DEFAULT_MAX_ISSUES,
        help=(
            f"Max issues to dispatch per cycle (default: {DEFAULT_MAX_ISSUES}; "
            "0 disables the cap). Bounds how many Argo submissions a single "
            "tick can fan out into; capped-out issues keep their label and "
            "are handled by a later cycle."
        ),
    )
    args = ap.parse_args()
    if args.max_issues < 0:
        ap.error("--max-issues must be >= 0 (use 0 to disable the cap)")

    result = asyncio.run(poll(
        label=args.label,
        dry_run=args.dry_run,
        max_issues=args.max_issues,
    ))

    print("\n=== Poll summary ===")
    print(f"  {result.started} issue(s) started.")
    if result.failures:
        # Per-issue failures are expected and retryable (the label is kept).
        # Surface the count but exit 0 so the CronWorkflow does not flap on a
        # single malformed issue or a broken label-write permission on one
        # issue.
        print(f"  {result.failures} issue(s) failed — labels kept, will retry next cycle.")
    else:
        print("  all matching issues handled.")

    sys.exit(0)


if __name__ == "__main__":
    main()
