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

Invariant: a `LandedOutcome` carries an occurrence id only after that
occurrence was read and its active attempt proved to belong to `workflow_id`.
An unreadable candidate is logged to stderr and skipped (the verifier sweep
recovers it); only a failed listing yields the unscoped `LandedOutcome("")`.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from orchestrator.security_findings import contract
from orchestrator.security_findings.client import (
    OK,
    REFUSED,
    UNKNOWN,
    SecurityFindingsClient,
    idempotency_key,
)

SUPPORTED_OWNER = "mctlhq"


def mctlhq_repo_name(subject_repository: str) -> str:
    """`github.com/mctlhq/<repo>` or `mctlhq/<repo>` -> `<repo>`, else `""`."""
    have = subject_repository.strip().lower().rstrip("/").removesuffix(".git").rstrip("/")
    parts = have.split("/")
    if len(parts) == 3 and parts[0] == "github.com":
        parts = parts[1:]
    if len(parts) != 2 or parts[0] != SUPPORTED_OWNER:
        return ""
    repo = parts[1]
    if not repo or not all(c.isascii() and (c.isalnum() or c in "_.-") for c in repo):
        return ""
    return repo


def issue_number_for(workflow_id: str, repo: str) -> str:
    """Issue number of `dev-loop-mctlhq-<repo>-<N>`, verified by exact prefix; else `""`."""
    if not repo:
        return ""
    prefix = f"dev-loop-{SUPPORTED_OWNER}-{repo}-"
    if not workflow_id.startswith(prefix):
        return ""
    number = workflow_id[len(prefix):]
    return number if number and number.isascii() and number.isdigit() else ""


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
            print(
                f"warn: security landed binding skipped unread candidate {summary.id} "
                f"({read.verdict}/{read.code}) for {workflow_id}; the verifier sweep will recover it",
                file=sys.stderr,
            )
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
