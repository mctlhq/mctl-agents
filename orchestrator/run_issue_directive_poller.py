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

One deliberate exception: an issue whose ONLY proposal directory is
terminal (`TERMINAL_STATUSES` — merged/rejected/review-stuck) never reaches
`_handle_directive` at all — `scan()` builds its `pending_by_issue` grouping
by walking `candidates`, the TERMINAL_STATUSES-excluded subset of
`all_refs`, and an issue with no non-terminal ref never has a candidate to
walk from in the first place (`all_refs` itself stays unfiltered, and
`_handle_directive`'s own `matches` is deliberately re-derived from it —
see the comment where `matches` is computed). The `_stale_directives` sweep
in `orchestrator.temporal.activities.discovery` applies the identical
TERMINAL_STATUSES filter to its own input refs, so the belt-and-braces
report is blind to the same issue too. This is a scope decision, not an
oversight: a directive on a fully closed-out issue has nothing left to
reopen through this path (claude P3 on head `f7a42ab`).

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
import time
from dataclasses import dataclass

import httpx

from orchestrator.directives import (
    VERBS,
    Directive,
    RawComment,
    ack_trailer,
    acked_comment_ids,
    bot_login_mismatch,
    fail_trailer,
    failed_attempt_counts,
    parse_comments,
)
from orchestrator.proposal_identity import (
    AmbiguousProposalError,
    ProposalCandidate,
    select_proposal_slug,
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

# Set True once this process has verified `directives.BOT_LOGINS` against
# the actually-authenticated `gh` login (see `_verify_bot_identity_once`).
# Checked once per process, not once per tick: this poller's process lives
# for many scan() calls inside one long-lived Temporal worker.
_bot_identity_checked = False

# A persistent dispatch failure (mctl-api down, broken auth) must not turn
# into an unbounded comment-spam loop: after this many failed attempts for
# the same comment id, the scan gives up — posts one final reply carrying
# the ack trailer (so no further tick retries it) instead of a fresh
# "will retry" comment every 15 minutes forever (codex review on #417).
MAX_DISPATCH_ATTEMPTS = 3

# A single transient `gh`/GitHub API blip while posting the retry-marker
# reply below must not by itself convert into permanently giving up on a
# maintainer's directive at the very first dispatch attempt — retry that
# one write a few times in-process before escalating (codex review on
# #417).
MARKER_POST_ATTEMPTS = 3
MARKER_POST_RETRY_DELAY_SECONDS = 1.0

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


class DispatchOutcomeAmbiguous(Exception):
    """Raised by `submit_investigate` when the POST to mctl-api may already
    have crossed the remote side-effect boundary (an Argo workflow may
    already be running) but this process cannot confirm either way — a 2xx
    response whose body does not parse to a workflow identity, or any
    `httpx.HTTPError` that occurs at or after the request reaches the
    server (a read/write failure, a dropped connection mid-response, a
    proxy error — not only `ReadTimeout`).

    Deliberately a distinct type from every other `submit_investigate`
    failure: those are safe to retry on the next tick (mctl-api definitely
    rejected the request with a 4xx/5xx, or a `ConnectError`/`ConnectTimeout`/
    `PoolTimeout` means the connection never reached it at all), while this
    one is NOT — blindly resubmitting could start a second, real, paid Argo
    run for the same directive. Mirrors the refusal `orchestrator.temporal.
    activities.argo`'s `submit_and_wait` already applies to the identical
    parse-failure shape (`argo.py`, "would duplicate the real SDK run").
    """


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

    `slug` and `requested_by` are both accepted for the caller's own
    logging/reply purposes only and are NOT sent as CWFT parameters (codex
    review on #417): the operation's only other caller, `orchestrator.
    temporal.workflows.dev_loop`'s `_run_cwft("mctl-agents-investigate",
    investigate_params)`, sends only `issue_url` (plus optional
    release-pinning fields this poller does not set) — `run_issue_
    investigator.main()` has no `--slug` flag at all and always re-derives
    the slug from `issue_url` via `resolve_slug()`, so a `slug` key here
    would be a parameter nothing on the other end reads (see design.md's
    open question, resolved against sending it). `requested_by` DOES have a
    `--requested-by` flag on the investigator's own CLI, but nothing in
    this repo declares `requested_by` as a parameter of the
    `mctl-agents-investigate` ClusterWorkflowTemplate (that manifest lives
    in mctl-gitops) or wires an Argo input through to that flag — sending it
    here would be a value mctl-api and/or the CWFT either drops silently or
    rejects outright, not a value that ever reaches `write_status_yaml`'s
    `request` block. Threading the requester through to `.status.yaml`
    needs a follow-up change to the CWFT manifest in mctl-gitops before this
    poller can send `requested_by` again.

    Raises `DispatchOutcomeAmbiguous` — never a plain `httpx`/parsing
    exception — for the cases where mctl-api may already have started the
    workflow but this call cannot confirm it, expressed positively rather
    than by enumeration (claude P2 on #421, carried from the previous
    round: only `ReadTimeout` was wrapped, leaving `RemoteProtocolError` —
    the everyday shape of an mctl-api pod restart or ingress drop mid
    response — and every other post-connect transport error to fall
    through to a plain retry): any `httpx.ConnectError`/`ConnectTimeout`/
    `PoolTimeout` means the connection never reached mctl-api at all, so a
    retry is genuinely safe and those propagate unchanged; every other
    `httpx.HTTPError` `client.post()` can raise happens at or after the
    request reached the server (a read/write failure, a dropped connection
    mid-response, a proxy error) and is therefore unconfirmable by
    construction. A 2xx response whose body does not parse to a workflow
    identity is the same ambiguous case for the same reason: mctl-api
    accepted the operation, only the reply describing it is malformed. A
    `raise_for_status()` failure (any 4xx/5xx) is left as a plain
    `httpx.HTTPStatusError` — mctl-api affirmatively rejected the request,
    so nothing could have started and the caller's normal retry is safe.
    """
    params = {"issue_url": issue_url}
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=_SUBMIT_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                f"/api/v1/operations/{INVESTIGATE_OPERATION}/execute",
                json=params,
                headers=auth_headers(),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise
        except httpx.HTTPError as exc:
            raise DispatchOutcomeAmbiguous(
                f"a transport error occurred waiting for mctl-api's response to "
                f"{INVESTIGATE_OPERATION} for {issue_url} ({type(exc).__name__}) — the request "
                "may already have reached the server and started"
            ) from exc
        response.raise_for_status()
        try:
            return response.json()["workflow"]["workflowName"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DispatchOutcomeAmbiguous(
                f"mctl-api accepted {INVESTIGATE_OPERATION} for {issue_url} (status "
                f"{response.status_code}) but its response body could not be parsed for the "
                f"workflow name: {exc!r}"
            ) from exc


def _post_reply(issue_url: str, body: str) -> None:
    _run(["gh", "issue", "comment", issue_url, "--body", body])


def _post_reply_with_retries(
    issue_url: str,
    body: str,
    attempts: int = MARKER_POST_ATTEMPTS,
    delay: float = MARKER_POST_RETRY_DELAY_SECONDS,
) -> None:
    """`_post_reply`, retried a few times before the caller treats the write
    as failed.

    Only used for the dispatch-failure retry-marker post: a single
    transient `gh`/GitHub API error there must not by itself escalate to
    the permanent give-up path (codex review on #417) — a handful of
    immediate in-process retries absorb a one-off blip without needing
    another poll tick, while staying bounded so a truly broken write path
    (dead token, outage) still reaches the give-up path instead of retrying
    forever.
    """
    last: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            _post_reply(issue_url, body)
            return
        except subprocess.CalledProcessError as e:
            last = e
            if attempt < attempts:
                time.sleep(delay)
    # attempts >= 1 guarantees the loop above always runs at least once, so
    # `last` is always set by the time we get here.
    assert last is not None  # noqa: S101
    raise last


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
    stale = sorted(f"{m.service}/{m.slug}" for m in matches if m.status in TERMINAL_STATUSES)
    # `resolve_slug` (orchestrator/run_issue_investigator.py) refuses this
    # exact case too — both sides now ask
    # `proposal_identity.select_proposal_slug`, so reaching this reply means
    # the choice was genuinely not forced: either every candidate is
    # `rejected`, or more than one is live. A human resolving the directories
    # is the only way forward, not one option among several. Naming it here
    # means the operator does not also have to go read `resolve_slug`'s own
    # error text to learn that (claude P2 on head `6c1aea8`).
    #
    # A `rejected` directory beside EXACTLY ONE live sibling no longer
    # reaches here: the resolver retires it and the directive dispatches
    # normally. With two or more live siblings it does still reach here
    # (`rejected` v1 + live v2 + live v3 — the resolver drops v1, two
    # survive, refusal), and `stale_note` below then names v1 as removable
    # when v2/v3 are the actual ambiguity. That imprecision predates the
    # resolver — it has the same shape for a `merged` sibling beside two
    # live ones — and is left as is rather than papered over here (claude
    # P3 on head `77107d0`).
    stale_note = ""
    if stale:
        stale_statuses = ", ".join(sorted({m.status for m in matches if m.status in TERMINAL_STATUSES}))
        verb = "is" if len(stale) == 1 else "are"
        stale_note = (
            f" {', '.join(stale)} {verb} already terminal ({stale_statuses}) and can "
            "likely be removed from gitops to resolve this."
        )
    # Unconditional, not only inside the `stale` branch above (claude P2 on
    # head `91ebe3e`): EVERY `_reply_ambiguous` outcome goes through
    # `_with_ack` below, so it is permanently acked regardless of whether
    # the ambiguity came from a stale terminal sibling or from two
    # ordinary live proposals (the slug-drift shape
    # `test_ambiguous_proposal_dirs_are_named_in_the_reply` covers).
    # Either way, an operator who removes the extra directory (exactly
    # what `resolve_slug`'s own error text asks for) ends up with one
    # match and gets silence unless told the fix does not retry itself —
    # the #395 shape this PR exists to remove.
    retry_note = (
        " This comment is already answered and will not be retried — comment "
        "`@MCTL reinvestigate` again once resolved."
    )
    return _with_ack(
        f"@{author} this issue has more than one proposal directory ({names}) — "
        f"refusing to guess which one to rewrite.{stale_note}{retry_note}",
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
    #
    # Only `type(error).__name__` goes into the reply, never `str(error)`
    # (claude P3 on #421): an `httpx.HTTPStatusError`/`ConnectError` message
    # can carry `MCTL_API_BASE_URL`'s host, port and route — internal
    # topology on a self-hosted deployment, published permanently to a
    # public, indexed GitHub comment. The full exception (type, message, and
    # for an `httpx.HTTPStatusError` the status code and response body) goes
    # to the poller's own log stream instead, via `_log_dispatch_error`,
    # called at every attempt below — not only the give-up one — since an
    # operator needs to tell a 401 from a 404 from a 503 well before the
    # retry budget runs out (claude P2 on #421).
    return (
        f"@{author} the re-investigation dispatch failed ({type(error).__name__}). This "
        f"will be retried on the next poll tick ({attempt}/{MAX_DISPATCH_ATTEMPTS})."
    )


def _reply_dispatch_gave_up(author: str, error: Exception, attempts: int) -> str:
    # Carries the ack trailer — this IS the give-up: no further tick may
    # retry a comment id that has already failed MAX_DISPATCH_ATTEMPTS
    # times, or the failure becomes an unbounded comment-spam loop.
    #
    # Same reasoning as `_reply_dispatch_failed` above: only the exception's
    # type name is public, never its message.
    plural = "time" if attempts == 1 else "times"
    return (
        f"@{author} the re-investigation dispatch failed ({type(error).__name__}) "
        f"{attempts} {plural} in a row. Giving up — this directive will not be retried "
        "automatically; an operator must check mctl-api and resubmit manually."
    )


def _reply_dispatch_marker_write_failed(author: str, error: Exception) -> str:
    # Distinct from `_reply_dispatch_gave_up` (claude P3 on #421): that
    # message says the dispatch itself failed N times, which is not true
    # here — dispatch attempt `attempt` failed once, same as any other, but
    # the RETRY MARKER for it could not be written, so `prior_failures`
    # would never advance and the next tick would retry the same attempt
    # forever without recording progress. Escalating to give-up is correct,
    # but the reply should say why: a GitHub write failure, not repeated
    # dispatch exhaustion.
    return (
        f"@{author} the re-investigation dispatch failed ({type(error).__name__}), and "
        "recording this attempt also failed — giving up now instead of retrying forever "
        "without a way to track progress. An operator must check mctl-api and GitHub, "
        "then resubmit manually."
    )


def _reply_dispatch_ambiguous(author: str, error: Exception) -> str:
    # Carries the ack trailer immediately, on the FIRST ambiguous outcome —
    # unlike `_reply_dispatch_failed`, there is no retry budget to spend
    # here: mctl-api may already have started the workflow, so automatically
    # resubmitting on a later tick (what a fail_trailer's retry-and-count
    # path would eventually do) risks a second, real, paid Argo run for the
    # same directive. Only the exception's type name is public, same
    # reasoning as the two replies above.
    return (
        f"@{author} the re-investigation dispatch outcome is unknown ({type(error).__name__}): "
        "mctl-api may already have accepted the request and started an Argo workflow, so this "
        "will NOT be retried automatically. An operator must check Argo for a workflow already "
        "running for this issue before resubmitting manually."
    )


def _log_dispatch_error(context: str, error: Exception) -> None:
    """Full, unsanitized diagnostic evidence for a dispatch failure, printed
    to the poller's own log stream — the only place it survives, since every
    public GitHub reply carries `type(error).__name__` only (see
    `_reply_dispatch_failed`'s docstring). Called on EVERY failed dispatch
    attempt, not only the terminal give-up one (claude P2 on #421): without
    this, the first two of three attempts left no record anywhere of
    whether mctl-api returned a 401, a 404 on a renamed operation, or a
    503 — the difference between broken auth, contract drift and a
    transient outage.
    """
    detail = repr(error)
    if isinstance(error, httpx.HTTPStatusError):
        detail = f"{detail} status={error.response.status_code} body={error.response.text[:500]!r}"
    print(f"FAIL: {context}: {detail}")


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
    "not-overwritable", "dispatched", "dispatch-failed",
    "dispatch-ambiguous", or "dry-run".

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
        # Deliberately UNFILTERED by status, unlike `scan()`'s own
        # TERMINAL_STATUSES-excluded `candidates`: this list is the input to
        # the shared resolver, which needs to see every directory claiming
        # the issue — including the terminal ones it is about to retire —
        # to decide whether the choice is forced.
        #
        # Matching `scan()`'s filter here instead was tried and reverted
        # (claude P2 on head `6c1aea8`), because it made the poller dispatch
        # a case `run_issue_investigator.resolve_slug` then refused: the
        # reply said "re-investigation started" (with the ack trailer, so it
        # is never retried) and the Argo workflow died with nothing posted
        # back to the issue. That divergence is now closed from the other
        # end — both this decision and `resolve_slug` route through
        # `proposal_identity.select_proposal_slug`, so the two agree on
        # every candidate set they both see.
        #
        # They do NOT always see the same set, and that gap is older than
        # the shared resolver: `resolve_slug` enumerates DIRECTORIES
        # (`existing_slugs` -> `iterdir`) and counts an absent or
        # unparseable `.status.yaml` as live, while `all_refs` comes from
        # `list_proposal_refs`, which enumerates `.status.yaml` BLOBS and
        # drops any it cannot parse (`gitops_state`, "has an unreadable
        # .status.yaml ...; skipping"). So a corrupt status file on one of
        # two directories is invisible here and live there — this side
        # dispatches, the investigator refuses, and the ack is permanent.
        # Pre-existing and unchanged by the resolver (the same single match
        # reached `len(matches) > 1` before), recorded here rather than
        # claimed away (claude P3 on head `77107d0`).
        matches = [r for r in all_refs if r.service == ref.service and _issue_number(r.slug) == number]
        resolved: str | None = None
        if matches:
            try:
                resolved = select_proposal_slug(
                    [ProposalCandidate(slug=m.slug, status=m.status) for m in matches]
                )
            except AmbiguousProposalError:
                resolved = None
        if not matches:
            outcome, body = "no-proposal", _reply_no_proposal(directive.author, directive.comment_id)
        elif resolved != ref.slug:
            # Either the resolver refused, or it named a directory other than
            # the one being walked. The second cannot happen today (`scan()`
            # only walks non-terminal refs, and a lone survivor of a
            # retire-the-rejected pass is that ref), but treating it as
            # ambiguous keeps this fail-closed if either filter ever moves.
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
        except DispatchOutcomeAmbiguous as e:
            # mctl-api may already have started the workflow — resubmitting
            # blindly on a later tick would duplicate a real, paid Argo run,
            # so this acks immediately (no retry budget spent) instead of
            # going through the fail_trailer/MAX_DISPATCH_ATTEMPTS path
            # (claude P2 on #421; mirrors argo.py's refusal to resubmit
            # after the identical parse-failure shape).
            _log_dispatch_error(
                f"ambiguous dispatch outcome for directive comment {directive.comment_id} ({issue_url})",
                e,
            )
            try:
                await asyncio.to_thread(
                    _post_reply_with_retries,
                    issue_url,
                    _with_ack(_reply_dispatch_ambiguous(directive.author, e), directive.comment_id),
                )
            except subprocess.CalledProcessError as post_e:
                # The exact residual `_reply_dispatched`'s handler below
                # guards against, on the branch this exception class exists
                # specifically to protect (claude P3 on #421): mctl-api may
                # already have started a workflow for this directive, but
                # if the ack write itself now also fails, the comment stays
                # unacked and the next tick silently re-enters `dispatch` —
                # exactly the blind resubmit `DispatchOutcomeAmbiguous` was
                # introduced to prevent. Loud and specific for the same
                # reason the dispatched-path handler is.
                print(
                    f"FAIL: directive comment {directive.comment_id} ({issue_url}) had an "
                    f"ambiguous dispatch outcome ({type(e).__name__}) and posting the ambiguous "
                    f"acknowledgement reply also failed after retries ({post_e.stderr or post_e}) — "
                    "this comment remains unacked and WILL be re-dispatched next tick unless an "
                    f"operator posts a comment containing `{ack_trailer(directive.comment_id)}` first."
                )
                raise
            return "dispatch-ambiguous"
        except Exception as e:  # noqa: BLE001 — surfaced as a per-directive failure, comment kept unacked for retry
            attempt = prior_failures + 1
            _log_dispatch_error(
                f"dispatch attempt {attempt}/{MAX_DISPATCH_ATTEMPTS} failed for directive comment "
                f"{directive.comment_id} ({issue_url})",
                e,
            )
            if attempt >= MAX_DISPATCH_ATTEMPTS:
                # Visible in the poller's own log stream, not only in the
                # buried GitHub comment: this is the permanent give-up path
                # (the reply below carries the ack trailer, so no later tick
                # retries this comment id) — an operator monitoring the
                # poller's logs must be able to see it happened without
                # having to notice the GitHub comment (codex review on #417).
                print(
                    f"FAIL: giving up on directive comment {directive.comment_id} "
                    f"({issue_url}) after {attempt} dispatch attempts — "
                    "acking; an operator must resubmit manually."
                )
                await asyncio.to_thread(
                    _post_reply,
                    issue_url,
                    _with_ack(_reply_dispatch_gave_up(directive.author, e, attempt), directive.comment_id),
                )
            else:
                try:
                    await asyncio.to_thread(
                        _post_reply_with_retries,
                        issue_url,
                        f"{_reply_dispatch_failed(directive.author, e, attempt)}\n\n"
                        f"{fail_trailer(directive.comment_id)}",
                    )
                except subprocess.CalledProcessError as post_e:
                    # Even after MARKER_POST_ATTEMPTS in-process retries, the
                    # retry-marker write itself failed — `prior_failures` is
                    # only ever recomputed from `fail_trailer` markers
                    # already posted (there is no other durable store; see
                    # the module docstring), so letting this exception
                    # propagate would leave `attempt` unchanged on the next
                    # tick and retry forever without ever recording progress
                    # toward the give-up bound. This is a distinct failure
                    # from ordinary dispatch exhaustion — a GitHub write
                    # failure, not `MAX_DISPATCH_ATTEMPTS` genuinely being
                    # reached (claude P3 on #421: the previous reply here
                    # reused `_reply_dispatch_gave_up`, whose wording claims
                    # the dispatch itself failed `attempt` times, which
                    # is not what happened). Escalate straight to give-up
                    # for this attempt instead of silently looping, but say
                    # why with `_reply_dispatch_marker_write_failed`.
                    print(
                        f"FAIL: could not post retry marker for directive comment "
                        f"{directive.comment_id} ({issue_url}) after dispatch attempt "
                        f"{attempt} ({post_e}) — escalating straight to give-up since "
                        "progress cannot be recorded without a working GitHub write."
                    )
                    await asyncio.to_thread(
                        _post_reply,
                        issue_url,
                        _with_ack(
                            _reply_dispatch_marker_write_failed(directive.author, e), directive.comment_id
                        ),
                    )
            return "dispatch-failed"
        try:
            # `_post_reply_with_retries`, not bare `_post_reply` (agy P2 on
            # #421): the ack trailer this reply carries is the ONLY record
            # that this comment was already dispatched — a transient
            # `gh`/GitHub write blip here, on a genuinely successful submit,
            # is otherwise indistinguishable on the next tick from "never
            # dispatched", and would resubmit into Argo a second time for
            # the same directive.
            await asyncio.to_thread(
                _post_reply_with_retries,
                issue_url,
                _reply_dispatched(directive.author, directive.comment_id, workflow_name, ref.service, ref.slug),
            )
        except subprocess.CalledProcessError as post_e:
            # Even MARKER_POST_ATTEMPTS in-process retries could not write
            # the ack — the workflow is already running in Argo, but this
            # comment remains unacked and WILL be resubmitted next tick
            # (there is no other durable store; see the module docstring).
            # Loud and specific so an operator can ack it manually before
            # that happens, rather than discovering a duplicate run.
            print(
                f"FAIL: directive comment {directive.comment_id} ({issue_url}) dispatched "
                f"successfully (Argo workflow {workflow_name!r} started) but posting the "
                f"acknowledgement reply failed after retries ({post_e.stderr or post_e}) — "
                "this comment remains unacked and WILL be resubmitted next tick unless an "
                f"operator posts a comment containing `{ack_trailer(directive.comment_id)}` first."
            )
            raise
        return "dispatched"

    await asyncio.to_thread(_post_reply, issue_url, body)
    return outcome


def _current_gh_login() -> str:
    """The GitHub login `gh`/`git` subprocess calls in this process actually
    authenticate writes as, per the App-installation token
    `orchestrator.github_token.refresh_github_token` keeps fresh.

    Deliberately GraphQL's `viewer { login }`, not `gh api user` (codex
    review on #417): REST `/user` requires a user-to-server OAuth token and
    returns a 403 for a GitHub App installation token — this process
    authenticates as the app's installation, never as a user, so `gh api
    user` cannot return this process's own identity at all. GraphQL's
    `viewer` field is the one GitHub-documented query that DOES resolve for
    an installation token, and it resolves to the same `<app-slug>[bot]`
    login GitHub renders on every comment this process posts (see
    `orchestrator.directives.BOT_LOGINS`'s docstring).
    """
    proc = _run([
        "gh", "api", "graphql",
        "-f", "query=query { viewer { login } }",
        "--jq", ".data.viewer.login",
    ])
    return proc.stdout.strip()


async def _verify_bot_identity_once() -> None:
    """Raise if `directives.BOT_LOGINS` has drifted from the actually
    authenticated login; no-op after the first successful check in this
    process (see `_bot_identity_checked`).

    A `_current_gh_login()` call that itself fails (network blip, rate
    limit) is NOT treated as a mismatch — it only means verification could
    not run this tick; it is retried on the next one, the same tolerance
    every other transient `gh` failure in this module gets. Only an actual
    login mismatch raises.
    """
    global _bot_identity_checked
    if _bot_identity_checked:
        return
    try:
        login = await asyncio.to_thread(_current_gh_login)
    except subprocess.CalledProcessError as e:
        print(f"WARN: could not verify bot identity ({e.stderr or e}); will retry next tick")
        return
    mismatch = bot_login_mismatch(login)
    if mismatch is not None:
        raise RuntimeError(f"BOT_LOGINS mismatch: {mismatch}")
    _bot_identity_checked = True


async def scan(dry_run: bool = False, max_directives: int = DEFAULT_MAX_DIRECTIVES) -> DirectiveScanResult:
    """Run one directive-scan tick. Returns dispatched/replied/deferred/failed
    counts. A `gh` failure reading one issue's comments is logged and the
    scan continues with the rest; the tick raises only on a global failure
    (the gitops proposal-set read itself, or a detected BOT_LOGINS identity
    mismatch).
    """
    if _scan_disabled():
        print("MCTL_DIRECTIVE_SCAN_ENABLED=false — directive scan disabled, skipping")
        return DirectiveScanResult()

    await _verify_bot_identity_once()

    all_refs = await list_proposal_refs()
    candidates = [r for r in all_refs if r.status not in TERMINAL_STATUSES]

    # Grouped by issue (not a flat list) so the per-tick cap below can be
    # applied round-robin across issues instead of by proposal order — see
    # the cap loop's comment for why a flat ordering starves every issue
    # after the first noisy one.
    #
    # Keyed by `issue_url`, NOT `(ref.service, ref.slug)`: the ambiguous
    # case (one issue backing multiple proposal directories, handled below
    # via `_handle_directive`'s own `matches` lookup) means several refs in
    # `candidates` can resolve to the same `issue_url`. Keying by proposal
    # made `read_issue_comments` run once per proposal sharing that issue
    # instead of once per issue, and put the same unacked directive comment
    # into multiple buckets — each processed independently by
    # `_handle_directive`, which posted a duplicate `_reply_ambiguous` reply
    # per proposal and charged the per-tick `--max-directives` budget once
    # per duplicate instead of once per actual dispatch decision (agy P2 /
    # claude P3 on #421). `matches` is re-derived from the issue number
    # inside `_handle_directive` regardless of which of the tied refs is
    # passed in, so any one representative ref for the issue is sufficient.
    pending_by_issue: dict[str, list[tuple[ProposalStateRef, str, Directive, int]]] = {}
    issue_order: list[str] = []
    seen_issue_urls: set[str] = set()
    failed = 0
    for ref in candidates:
        issue_url = issue_url_for(ref.service, ref.slug)
        if issue_url is None:
            continue
        if issue_url in seen_issue_urls:
            continue
        seen_issue_urls.add(issue_url)
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
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            # A malformed/unexpected `gh` payload (bad JSON, a missing key,
            # a shape `read_issue_comments` cannot parse) is a per-issue
            # failure, not a tick-ending crash — same tolerance as a
            # `CalledProcessError`, so one bad payload cannot take down
            # every other candidate's scan this tick (codex review on #417).
            print(f"WARN: malformed gh payload for {issue_url}: {e}")
            failed += 1
            continue

        acked = acked_comment_ids(comments)
        fail_counts = failed_attempt_counts(comments)
        for directive in parse_comments(comments):
            if not directive.comment_id or directive.comment_id in acked:
                continue
            if issue_url not in pending_by_issue:
                pending_by_issue[issue_url] = []
                issue_order.append(issue_url)
            pending_by_issue[issue_url].append(
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
        elif outcome in ("dispatch-failed", "dispatch-ambiguous"):
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
