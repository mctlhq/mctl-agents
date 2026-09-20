"""Directive-comment scan — turns `@MCTL reinvestigate` into a real,
answered trigger (mctl-agents#417).

Before this module, a directive comment on an issue that owns a proposal
did nothing: no run was scheduled, no state changed, and no reply said so
(see design.md for the full #395 incident). This module is the fix's
trigger half — `orchestrator.directives` is the pure recognition rule,
`orchestrator.run_issue_investigator` is where a re-investigation already
worked, and this module is what connects a comment to that path.

Discovery is driven by the PROPOSAL SET, not by `gh search issues --match
comments`: search is index-lagged, and an indexing delay here would
reintroduce exactly the silence being fixed. For every non-terminal
proposal `gitops_state.list_proposal_refs()` already knows about, the
slug's `issue-<N>-` prefix plus the service name reconstruct the issue URL,
and one `gh issue view` per candidate yields its comments.

Dedup is an acknowledgement marker comment (`orchestrator.directives.
ack_trailer`), not a gitops write — the Temporal worker has no gitops
checkout and no deploy key (#179), so a `.status.yaml`-based marker would
have to queue behind the `mctl-gitops-main-writes` mutex through an
mctl-api operation on every tick, for something GitHub already knows.
Ordering matters: the ack is posted only AFTER a successful dispatch, so a
failed submit leaves the comment unacked and the next tick retries it.

Every non-dispatch outcome — unauthorized author, unrecognised verb, no
proposal directory, an ambiguous one, a non-overwritable status — is a
reply, never a silence. Only the recognised, authorized, unambiguous,
overwritable case submits anything.

This module never starts or signals a DevLoopWorkflow, and never touches
the `agents:intake` label: the comment path and the label path are
independent by construction (see tests/test_run_issue_directive_poller.py's
acceptance test).

Usage:
    python -m orchestrator.run_issue_directive_poller
    python -m orchestrator.run_issue_directive_poller --dry-run
    python -m orchestrator.run_issue_directive_poller --max-directives 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

import httpx

from orchestrator.directives import (
    VERBS,
    Directive,
    RawComment,
    ack_trailer,
    acked_comment_ids,
    fail_trailer,
    failed_attempt_counts,
    parse_comments,
)
from orchestrator.run_issue_investigator import _OVERWRITABLE_STATUSES, _run
from orchestrator.temporal.activities.gitops_state import ProposalStateRef, list_proposal_refs
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

# A tick dispatches at most this many directives. Unlike run_issue_poller's
# --max-issues (starting a workflow is cheap), a directive dispatch starts a
# real, paid SDK run — this cap is the thing standing between a mass-comment
# event and an uncapped fan-out. Directives beyond the cap keep their
# comment unacked and are picked up by a later tick. Matches the spirit of
# run_issue_poller.DEFAULT_MAX_ISSUES = 5.
DEFAULT_MAX_DIRECTIVES = 3

# A persistent dispatch failure (mctl-api down, broken auth) must not turn
# into an unbounded comment-spam loop: after this many failed attempts for
# the same comment id, the scan gives up — posts one final reply carrying
# the ack trailer (so no further tick retries it) instead of a fresh
# "will retry" comment every 15 minutes forever (codex review on #417).
MAX_DISPATCH_ATTEMPTS = 3

# Statuses a proposal never leaves — a directive on one of these is not
# worth even reading comments for. Everything else (including "proposed",
# the only status a reinvestigate directive can actually act on) is a
# candidate. Mirrors orchestrator.pr_adoption.TERMINAL_STATUSES; duplicated
# rather than imported so this module does not pull in run_shepherd's much
# heavier import graph for three literals.
TERMINAL_STATUSES = frozenset({"merged", "rejected", "review-stuck"})

# mctl-api operation name — maps 1:1 to cwft-mctl-agents-investigate.yaml,
# the same one DevLoopWorkflow submits for the label-driven path
# (orchestrator/temporal/workflows/dev_loop.py).
INVESTIGATE_OPERATION = "mctl-agents-investigate"

_SUBMIT_TIMEOUT_SECONDS = 30.0

_SLUG_ISSUE_RE = re.compile(r"^issue-(\d+)-")


def _scan_disabled() -> bool:
    """Fastest kill, no deploy: MCTL_DIRECTIVE_SCAN_ENABLED=false on the
    Temporal worker reverts the tick to label-only dispatch — nothing else
    in the system knows this scan existed."""
    return os.environ.get("MCTL_DIRECTIVE_SCAN_ENABLED", "true").strip().lower() in {
        "false", "0", "no", "off",
    }


def issue_url_for(service: str, slug: str) -> str | None:
    """The issue URL a proposal slug names, or None if the slug is not
    `issue-<N>-*` shaped (a non-issue-driven proposal, e.g. one written by
    the incident responder)."""
    match = _SLUG_ISSUE_RE.match(slug)
    if not match:
        return None
    return f"https://github.com/mctlhq/{service}/issues/{match.group(1)}"


def _issue_number(slug: str) -> str | None:
    match = _SLUG_ISSUE_RE.match(slug)
    return match.group(1) if match else None


def read_issue_comments(issue_url: str) -> list[RawComment]:
    """One `gh issue view` call, reduced to the `RawComment` shape
    `orchestrator.directives` reads. Raises `subprocess.CalledProcessError`
    on a `gh` failure — the caller logs it and continues with the rest, per
    the same per-issue tolerance `run_issue_poller.poll()` has.
    """
    proc = _run([
        "gh", "issue", "view",
        "--json", "number,url,state,comments",
        "--", issue_url,
    ])
    data = json.loads(proc.stdout)
    comments: list[RawComment] = []
    for c in data.get("comments") or []:
        comments.append(RawComment(
            id=str(c.get("id") or ""),
            author=((c.get("author") or {}).get("login")) or "",
            created_at=c.get("createdAt") or "",
            body=c.get("body") or "",
            author_association=str(c.get("authorAssociation") or ""),
        ))
    return comments


async def submit_investigate(issue_url: str, slug: str, requested_by: str) -> str:
    """POST the `mctl-agents-investigate` operation and return the Argo
    workflow name mctl-api hands back.

    Deliberately does not poll to completion the way
    `orchestrator.temporal.activities.argo.submit_and_wait` does: the scan
    runs synchronously inside a 15-minute tick and only needs the name to
    reply with — the investigation itself runs independently in Argo, the
    same as every other consumer of this operation.

    `slug` is accepted for the caller's own logging/reply purposes only and
    is NOT sent as a CWFT parameter (codex review on #417): the operation's
    only other caller, `orchestrator.temporal.workflows.dev_loop`'s
    `_run_cwft("mctl-agents-investigate", investigate_params)`, sends only
    `issue_url` (plus optional release-pinning fields this poller does not
    set) — `run_issue_investigator.main()` has no `--slug` flag at all and
    always re-derives the slug from `issue_url` via `resolve_slug()`, so a
    `slug` key here would be a parameter nothing on the other end reads
    (see design.md's open question, resolved against sending it).
    """
    params = {"issue_url": issue_url, "requested_by": requested_by}
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=_SUBMIT_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"/api/v1/operations/{INVESTIGATE_OPERATION}/execute",
            json=params,
            headers=auth_headers(),
        )
        response.raise_for_status()
        return response.json()["workflow"]["workflowName"]


def _post_reply(issue_url: str, body: str) -> None:
    _run(["gh", "issue", "comment", issue_url, "--body", body])


def _with_ack(body: str, comment_id: str) -> str:
    return f"{body}\n\n{ack_trailer(comment_id)}"


def _reply_unauthorized(author: str, comment_id: str) -> str:
    return _with_ack(
        f"@{author} this directive was not accepted: `@MCTL` directives only run for "
        "an author whose association with this repository is OWNER, MEMBER or "
        "COLLABORATOR.",
        comment_id,
    )


def _reply_unrecognised(author: str, comment_id: str) -> str:
    return _with_ack(
        f"@{author} `@MCTL` directive not recognised. Supported verb(s): "
        f"{', '.join(sorted(VERBS))}.",
        comment_id,
    )


def _reply_no_proposal(author: str, comment_id: str) -> str:
    return _with_ack(
        f"@{author} there is no proposal to rewrite for this issue yet. Add the "
        "`agents:intake` label to create one.",
        comment_id,
    )


def _reply_ambiguous(author: str, comment_id: str, matches: list[ProposalStateRef]) -> str:
    names = ", ".join(sorted(f"{m.service}/{m.slug}" for m in matches))
    return _with_ack(
        f"@{author} this issue has more than one proposal directory ({names}) — "
        "refusing to guess which one to rewrite.",
        comment_id,
    )


def _reply_not_overwritable(author: str, comment_id: str, status: str) -> str:
    return _with_ack(
        f"@{author} the proposal for this issue is at status `{status}`, which is "
        "not eligible for re-investigation — an implementer may already own it.",
        comment_id,
    )


def _reply_dispatched(author: str, comment_id: str, workflow_name: str, service: str, slug: str) -> str:
    return _with_ack(
        f"@{author} re-investigation started: Argo workflow `{workflow_name}` "
        f"({service}/{slug}).",
        comment_id,
    )


def _reply_dispatch_failed(author: str, error: Exception, attempt: int) -> str:
    # Deliberately NOT carrying the ack trailer — see the module docstring.
    # Carries a fail_trailer instead (appended by the caller) so the retry
    # budget below can be counted across ticks.
    return (
        f"@{author} the re-investigation dispatch failed ({error}). This will be "
        f"retried on the next poll tick ({attempt}/{MAX_DISPATCH_ATTEMPTS})."
    )


def _reply_dispatch_gave_up(author: str, error: Exception, attempts: int) -> str:
    # Carries the ack trailer — this IS the give-up: no further tick may
    # retry a comment id that has already failed MAX_DISPATCH_ATTEMPTS
    # times, or the failure becomes an unbounded comment-spam loop.
    return (
        f"@{author} the re-investigation dispatch failed ({error}) {attempts} times in a "
        "row. Giving up — this directive will not be retried automatically; an operator "
        "must check mctl-api and resubmit manually."
    )


@dataclass(frozen=True)
class DirectiveScanResult:
    dispatched: int = 0
    replied: int = 0
    deferred: int = 0
    failed: int = 0


async def _handle_directive(
    directive: Directive,
    *,
    issue_url: str,
    ref: ProposalStateRef,
    all_refs: list[ProposalStateRef],
    dry_run: bool,
    prior_failures: int = 0,
) -> str:
    """Run the decision table for one unacked directive. Returns one of:
    "unauthorized", "unrecognised", "no-proposal", "ambiguous",
    "not-overwritable", "dispatched", "dispatch-failed", or "dry-run".

    `prior_failures` is the number of previously recorded dispatch-failure
    attempts for this exact comment id (`orchestrator.directives.
    failed_attempt_counts`) — the bound that keeps a persistent dispatch
    failure from spamming a fresh "will retry" comment every tick forever.
    """
    if not directive.authorized:
        outcome, body = "unauthorized", _reply_unauthorized(directive.author, directive.comment_id)
    elif directive.verb is None:
        outcome, body = "unrecognised", _reply_unrecognised(directive.author, directive.comment_id)
    else:
        number = _issue_number(ref.slug)
        matches = [r for r in all_refs if r.service == ref.service and _issue_number(r.slug) == number]
        if not matches:
            outcome, body = "no-proposal", _reply_no_proposal(directive.author, directive.comment_id)
        elif len(matches) > 1:
            outcome, body = "ambiguous", _reply_ambiguous(directive.author, directive.comment_id, matches)
        elif ref.status not in _OVERWRITABLE_STATUSES:
            outcome, body = (
                "not-overwritable",
                _reply_not_overwritable(directive.author, directive.comment_id, ref.status),
            )
        else:
            outcome, body = "dispatch", ""

    if dry_run:
        print(f"[dry-run] {issue_url} comment {directive.comment_id}: would reply/act as '{outcome}'")
        return "dry-run"

    if outcome == "dispatch":
        try:
            workflow_name = await submit_investigate(issue_url, ref.slug, directive.author)
        except Exception as e:  # noqa: BLE001 — surfaced as a per-directive failure, comment kept unacked for retry
            attempt = prior_failures + 1
            if attempt >= MAX_DISPATCH_ATTEMPTS:
                _post_reply(
                    issue_url,
                    _with_ack(_reply_dispatch_gave_up(directive.author, e, attempt), directive.comment_id),
                )
            else:
                _post_reply(
                    issue_url,
                    f"{_reply_dispatch_failed(directive.author, e, attempt)}\n\n"
                    f"{fail_trailer(directive.comment_id)}",
                )
            return "dispatch-failed"
        _post_reply(
            issue_url,
            _reply_dispatched(directive.author, directive.comment_id, workflow_name, ref.service, ref.slug),
        )
        return "dispatched"

    _post_reply(issue_url, body)
    return outcome


async def scan(dry_run: bool = False, max_directives: int = DEFAULT_MAX_DIRECTIVES) -> DirectiveScanResult:
    """Run one directive-scan tick. Returns dispatched/replied/deferred/failed
    counts. A `gh` failure reading one issue's comments is logged and the
    scan continues with the rest; the tick raises only on a global failure
    (the gitops proposal-set read itself).
    """
    if _scan_disabled():
        print("MCTL_DIRECTIVE_SCAN_ENABLED=false — directive scan disabled, skipping")
        return DirectiveScanResult()

    all_refs = await list_proposal_refs()
    candidates = [r for r in all_refs if r.status not in TERMINAL_STATUSES]

    # Grouped by issue (not a flat list) so the per-tick cap below can be
    # applied round-robin across issues instead of by proposal order — see
    # the cap loop's comment for why a flat ordering starves every issue
    # after the first noisy one.
    pending_by_issue: dict[tuple[str, str], list[tuple[ProposalStateRef, str, Directive, int]]] = {}
    issue_order: list[tuple[str, str]] = []
    failed = 0
    for ref in candidates:
        issue_url = issue_url_for(ref.service, ref.slug)
        if issue_url is None:
            continue
        try:
            # Off the event loop: this scan can make one `gh issue view`
            # call per non-terminal proposal (tens of them at production
            # scale), and each is a blocking subprocess round-trip. Run
            # synchronously inside this coroutine it would freeze the
            # Temporal worker's asyncio event loop for the sum of all of
            # them, stalling every other activity/workflow task the same
            # worker process is scheduling (codex review on #417) — the
            # same `asyncio.to_thread` wrapping activities/discovery.py
            # already uses for the identical call.
            comments = await asyncio.to_thread(read_issue_comments, issue_url)
        except subprocess.CalledProcessError as e:
            print(f"WARN: could not read comments for {issue_url}: {e.stderr or e}")
            failed += 1
            continue

        acked = acked_comment_ids(comments)
        fail_counts = failed_attempt_counts(comments)
        key = (ref.service, ref.slug)
        for directive in parse_comments(comments):
            if not directive.comment_id or directive.comment_id in acked:
                continue
            if key not in pending_by_issue:
                pending_by_issue[key] = []
                issue_order.append(key)
            pending_by_issue[key].append(
                (ref, issue_url, directive, fail_counts.get(directive.comment_id, 0))
            )

    total_pending = sum(len(v) for v in pending_by_issue.values())

    if max_directives > 0 and total_pending > max_directives:
        # Round-robin across issues, one directive at a time, rather than
        # draining the first issue's queue before moving to the next: a
        # flat cap applied to a list ordered by proposal lets one issue
        # with many pending directives (a comment burst) consume the whole
        # tick's budget and starve every other issue's directive out of
        # every tick (codex review on #417).
        buckets = [pending_by_issue[key] for key in issue_order]
        actionable: list[tuple[ProposalStateRef, str, Directive, int]] = []
        while len(actionable) < max_directives and any(buckets):
            for bucket in buckets:
                if not bucket:
                    continue
                actionable.append(bucket.pop(0))
                if len(actionable) >= max_directives:
                    break
        deferred_count = total_pending - len(actionable)
        print(
            f"WARN: {total_pending} directive(s) found this tick — capping at "
            f"--max-directives={max_directives}; {deferred_count} deferred to a later tick."
        )
    else:
        actionable = [item for key in issue_order for item in pending_by_issue[key]]
        deferred_count = 0

    dispatched = 0
    replied = 0
    for ref, issue_url, directive, fail_count in actionable:
        try:
            outcome = await _handle_directive(
                directive, issue_url=issue_url, ref=ref, all_refs=all_refs, dry_run=dry_run,
                prior_failures=fail_count,
            )
        except subprocess.CalledProcessError as e:
            print(f"FAIL: could not reply on {issue_url}: {e.stderr or e}")
            failed += 1
            continue

        if dry_run:
            continue
        if outcome == "dispatched":
            dispatched += 1
            replied += 1
        elif outcome == "dispatch-failed":
            failed += 1
        else:
            replied += 1

    return DirectiveScanResult(
        dispatched=dispatched, replied=replied, deferred=deferred_count, failed=failed,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Directive-comment scan — dispatch `@MCTL reinvestigate` directives"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List directives that would be acted on; post no reply, submit nothing",
    )
    ap.add_argument(
        "--max-directives",
        type=int,
        default=DEFAULT_MAX_DIRECTIVES,
        help=(
            f"Max directives to act on per tick (default: {DEFAULT_MAX_DIRECTIVES}; "
            "0 disables the cap). Directives beyond the cap stay unacked for a later tick."
        ),
    )
    args = ap.parse_args()
    if args.max_directives < 0:
        ap.error("--max-directives must be >= 0 (use 0 to disable the cap)")

    result = asyncio.run(scan(dry_run=args.dry_run, max_directives=args.max_directives))

    print("\n=== Directive scan summary ===")
    print(
        f"dispatched={result.dispatched} replied={result.replied} "
        f"deferred={result.deferred} failed={result.failed}"
    )
    # Per-directive failures never fail the tick — same rule run_issue_poller
    # applies to per-issue failures. A comment that could not be read/acted
    # on this tick is retried on the next one.
    sys.exit(0)


if __name__ == "__main__":
    main()
