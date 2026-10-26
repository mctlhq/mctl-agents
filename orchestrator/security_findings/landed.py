"""Bind a DevLoop's landed merge commit to the active attempt that owns it.

Shared by the `security_record_landed` Temporal activity and the sweep's
fallback for a missed hook. Binding a fix moves the occurrence to
`awaiting_verification` and nothing more: this module never records a
verification, so a merge alone is never "fixed".

`GET /occurrences` filters by `state` but not by `devloop_workflow_id`, and
the documented `repository` filter format (normalised `host/owner/repo`) is
not something this client should guess at. So the listing is by state only
and the repository is matched here, on the normalised suffix, before each
candidate is read in full and matched on `devloop_workflow_id` exactly.
"""
from __future__ import annotations

from dataclasses import dataclass

from orchestrator.security_findings import contract
from orchestrator.security_findings.client import (
    OK,
    REFUSED,
    UNKNOWN,
    SecurityFindingsClient,
    idempotency_key,
)


@dataclass(frozen=True)
class LandedOutcome:
    occurrence_id: str
    outcome: str  # bound | replayed | refused:<code> | unknown
    attempt_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"occurrence_id": self.occurrence_id, "attempt_id": self.attempt_id, "outcome": self.outcome}


def repository_matches(subject_repository: str, target_repo: str) -> bool:
    """`github.com/mctlhq/svc` (or `mctlhq/svc`, or bare `svc`) against the
    DevLoop's target repo name. An unreadable subject repository is kept as a
    candidate: the exact `devloop_workflow_id` match decides."""
    if not subject_repository:
        return True
    have = subject_repository.lower().removesuffix(".git").rstrip("/")
    want = target_repo.lower().rstrip("/")
    return have == want or have.endswith("/" + want)


def bind_landed(
    client: SecurityFindingsClient, *, workflow_id: str, target_repo: str, pr_url: str, merge_commit: str
) -> list[LandedOutcome]:
    """Zero matches (the normal case for a non-security DevLoop) costs one list call."""
    if not merge_commit or not workflow_id:
        return []
    listing = client.list_occurrences(state=contract.STATE_FIXING)
    if listing.verdict != OK:
        return [LandedOutcome("", UNKNOWN)]
    out: list[LandedOutcome] = []
    for summary in listing.occurrences:
        if summary.state != contract.STATE_FIXING or not repository_matches(summary.repository, target_repo):
            continue
        read = client.get_occurrence(summary.id)
        if read.verdict != OK or read.occurrence is None:
            out.append(LandedOutcome(summary.id, UNKNOWN))
            continue
        occ = read.occurrence
        attempt = occ.active_attempt
        if occ.state != contract.STATE_FIXING or attempt is None or attempt.devloop_workflow_id != workflow_id:
            continue
        answer = client.record_fix(
            attempt.id,
            landed_revision=merge_commit,
            landed_ref=pr_url,
            expected_state_version=occ.state_version,
            idempotency_key=idempotency_key("sra-fix", attempt.id, merge_commit),
            occurrence_id=occ.id,
        )
        if answer.verdict == OK:
            out.append(LandedOutcome(occ.id, "replayed" if answer.replayed else "bound", attempt.id))
        elif answer.verdict == REFUSED:
            out.append(LandedOutcome(occ.id, f"refused:{answer.code}", attempt.id))
            if answer.code == "forbidden":
                break
        else:
            out.append(LandedOutcome(occ.id, UNKNOWN, attempt.id))
    return out
