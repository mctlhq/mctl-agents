"""Sweep: record verifications for landed security fixes (mctl-agents#809).

One-shot CLI, scheduled later by a CronWorkflow in mctl-gitops. Each run:

1. Lists `awaiting_verification` occurrences (cap 50), reads each in full, and
   asks `verification.plan` for at most one write per attempt.
2. Fallback for a missed DevLoop hook: lists `fixing` occurrences whose
   attempt's DevLoop has finished with a merged PR and binds the same
   `merge_commit` (the idempotency key makes a double bind a replay).

It prints one JSON line per occurrence. A 503 prints one `unknown` line and
exits 0; a 403 on a write stops all writes and exits non-zero, and so does
`MCTL_RUN_TOKEN_MODE=enforce` (before any API call): a run token is confined
to its own work item and cannot sweep many.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime

from orchestrator import run_token
from orchestrator.security_findings import contract, verification
from orchestrator.security_findings.client import (
    OK,
    UNKNOWN,
    SecurityFindingsClient,
    idempotency_key,
)

SWEEP_CAP = 50
_DEV_LOOP_ID_RE = re.compile(r"^dev-loop-[A-Za-z0-9_.-]+?-([A-Za-z0-9_.-]+)-(\d+)$")

#: `workflow_id -> (pr_url, merge_commit)` of a finished DevLoop whose PR merged, else None.
MergedLookup = Callable[[SecurityFindingsClient, str], "tuple[str, str] | None"]


def _emit(**fields: object) -> None:
    print(json.dumps(fields, sort_keys=True), flush=True)


def default_merged_lookup(client: SecurityFindingsClient, workflow_id: str) -> tuple[str, str] | None:
    m = _DEV_LOOP_ID_RE.match(workflow_id)
    if not m:
        return None
    repo, number = m.group(1), m.group(2)
    loop = client.get_dev_loop(workflow_id)
    if loop.verdict != "exists" or loop.status == "Running":
        return None
    from orchestrator.temporal.activities.pr_state import get_pr_state
    from orchestrator.temporal.activities.proposals import find_proposal_slug

    async def go() -> tuple[str, str] | None:
        slug = await find_proposal_slug(repo, number)
        if not slug:
            return None
        pr = await get_pr_state(repo, slug)
        if pr.merged and pr.merge_commit and pr.pr_url:
            return pr.pr_url, pr.merge_commit
        return None

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 — an unreadable PR is "not known to be merged"
        print(f"warn: merged-PR lookup failed for {workflow_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def sweep(
    client: SecurityFindingsClient,
    verifiers: list[verification.Verifier],
    *,
    now: datetime | None = None,
    merged_lookup: MergedLookup = default_merged_lookup,
) -> None:
    now = now or datetime.now(UTC)
    _bind_missed(client, merged_lookup)
    listing = client.list_occurrences(state=contract.STATE_AWAITING, max_items=SWEEP_CAP)
    if listing.verdict != OK:
        _emit(verdict=UNKNOWN, reason=listing.reason)
        return
    for summary in listing.occurrences:
        if client.forbidden_route:
            break
        read = client.get_occurrence(summary.id)
        if read.verdict != OK or read.occurrence is None:
            _emit(occurrence_id=summary.id, verdict=read.verdict, code=read.code)
            continue
        occ = read.occurrence
        attempt = occ.active_attempt
        if occ.state != contract.STATE_AWAITING or attempt is None:
            _emit(occurrence_id=occ.id, skipped="not_awaiting_with_active_attempt")
            continue

        def identity(finding_id: str = occ.finding_id) -> str:
            answer = client.get_finding_identity(finding_id)
            return answer.identity_key if answer.verdict == OK else ""

        decision = verification.plan(verification.Context(occ, attempt, identity, now), verifiers)
        if decision.write is None:
            _emit(occurrence_id=occ.id, attempt_id=attempt.id, skipped=decision.reason)
            continue
        w = decision.write
        answer = client.record_verification(
            attempt.id,
            mechanism=w.mechanism,
            result=w.result,
            revision=w.revision,
            artifact_digest=w.artifact_digest,
            evidence_refs=[{k: v for k, v in (("kind", e.kind), ("ref", e.ref), ("digest", e.digest)) if v}
                           for e in w.evidence_refs],
            expected_state_version=occ.state_version,
            idempotency_key=idempotency_key(
                "svr", attempt.id, w.mechanism, w.result, w.revision or w.artifact_digest, decision.window
            ),
            occurrence_id=occ.id,
        )
        # A 422 verification_not_bound is final: never retried with another revision.
        _emit(
            occurrence_id=occ.id, attempt_id=attempt.id, mechanism=w.mechanism, result=w.result,
            verdict=answer.verdict, code=answer.code, transition=answer.transition, state=answer.state,
            replayed=answer.replayed, reason=decision.reason,
        )


def _bind_missed(client: SecurityFindingsClient, merged_lookup: MergedLookup) -> None:
    listing = client.list_occurrences(state=contract.STATE_FIXING, max_items=SWEEP_CAP)
    if listing.verdict != OK:
        _emit(verdict=UNKNOWN, reason=f"fixing listing: {listing.reason}")
        return
    for summary in listing.occurrences:
        if client.forbidden_route:
            return
        read = client.get_occurrence(summary.id)
        occ = read.occurrence
        attempt = occ.active_attempt if occ else None
        if read.verdict != OK or occ is None or attempt is None or occ.state != contract.STATE_FIXING:
            continue
        if not attempt.devloop_workflow_id:
            continue
        merged = merged_lookup(client, attempt.devloop_workflow_id)
        if merged is None:
            continue
        pr_url, merge_commit = merged
        answer = client.record_fix(
            attempt.id,
            landed_revision=merge_commit,
            landed_ref=pr_url,
            expected_state_version=occ.state_version,
            idempotency_key=idempotency_key("sra-fix", attempt.id, merge_commit),
            occurrence_id=occ.id,
        )
        _emit(occurrence_id=occ.id, attempt_id=attempt.id, bind=answer.verdict, code=answer.code,
              transition=answer.transition, replayed=answer.replayed)


def default_verifiers() -> list[verification.Verifier]:
    return [
        verification.RegressionTestVerifier(mapping=verification.load_regression_map()),
        verification.AssuranceReplayVerifier(),
        verification.ScannerRerunVerifier(),
    ]


def main() -> int:
    try:
        enforce = run_token.mode() == run_token.MODE_ENFORCE
    except run_token.RunTokenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if enforce:
        print(
            "error: run_security_verifier cannot run under MCTL_RUN_TOKEN_MODE=enforce: "
            "a run token is confined to its own work item and cannot sweep occurrences of many",
            file=sys.stderr,
        )
        return 2
    client = SecurityFindingsClient()
    sweep(client, default_verifiers())
    if client.forbidden_route:
        print(f"error: 403 forbidden on {client.forbidden_route}: the token lacks security:remediate", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
