"""Start a RemediationAttempt for one SecurityOccurrence.

The operator files a tracking issue and runs
`python -m orchestrator.security_findings start`. This module gets or creates
the issue's WorkItem and asks mctl-api to start an attempt bound to it and to
the deterministic DevLoop id of that issue. mctl-api decides the transition;
the outcome here is only a report of its answer.

Outcomes: `started`, `replayed`, `already_active(attempt_id)`,
`refused(code)`, `unknown`.

The start guard runs before any write: a DevLoop that already exists for the
issue's workflow id cannot be restarted once past implement
(mctlhq/mctl-agents#748), so an attempt bound to it would never get a landed
revision. Existence is `GET /api/v1/agents/dev-loop/{id}`: 404 continues,
200 refuses `devloop_exists`, anything else is `unknown` and also refuses.
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
from orchestrator.temporal.issue_ref import workflow_id_for
from orchestrator.work_context import creation

STARTED = "started"
REPLAYED = "replayed"
ALREADY_ACTIVE = "already_active"


@dataclass(frozen=True)
class StartOutcome:
    outcome: str
    code: str = ""
    reason: str = ""
    occurrence_id: str = ""
    attempt_id: str = ""
    work_item_id: str = ""
    workflow_id: str = ""
    devloop_status: str = ""

    def to_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}

    @property
    def exit_code(self) -> int:
        return {STARTED: 0, REPLAYED: 0, ALREADY_ACTIVE: 0, REFUSED: 1}.get(self.outcome, 2)


def start(
    client: SecurityFindingsClient,
    work_items: object,
    *,
    occurrence_id: str,
    issue_url: str,
    tenant: str,
) -> StartOutcome:
    """`work_items` is a `WorkItemClient` (duck-typed: `create_work_item`)."""
    try:
        workflow_id = workflow_id_for(issue_url)
    except ValueError as exc:
        return StartOutcome(REFUSED, code="invalid_issue_url", reason=str(exc), occurrence_id=occurrence_id)

    # 0. Start guard, before any write.
    loop = client.get_dev_loop(workflow_id)
    if loop.verdict == "exists":
        return StartOutcome(
            REFUSED, code="devloop_exists", reason=f"{workflow_id} is {loop.status or 'present'}",
            occurrence_id=occurrence_id, workflow_id=workflow_id, devloop_status=loop.status,
        )
    if loop.verdict != "absent":
        return StartOutcome(
            UNKNOWN, reason=f"dev-loop existence unreadable: {loop.reason}",
            occurrence_id=occurrence_id, workflow_id=workflow_id,
        )

    # 1. The occurrence.
    read = client.get_occurrence(occurrence_id)
    if read.verdict != OK or read.occurrence is None:
        return StartOutcome(read.verdict, code=read.code, reason=read.reason, occurrence_id=occurrence_id,
                            workflow_id=workflow_id)
    occ = read.occurrence
    active = occ.active_attempt
    # An `unverifiable` occurrence may still name its last attempt as active:
    # starting from it supersedes that attempt server-side, so it is startable.
    if active is not None and occ.state not in contract.START_STATES:
        return StartOutcome(ALREADY_ACTIVE, occurrence_id=occ.id, attempt_id=active.id, workflow_id=workflow_id)
    if occ.state not in contract.START_STATES:
        return StartOutcome(REFUSED, code="invalid_transition", reason=f"occurrence is {occ.state}",
                            occurrence_id=occ.id, workflow_id=workflow_id)

    # 2. The WorkItem (idempotent create-or-resolve on the issue).
    created = work_items.create_work_item(  # type: ignore[attr-defined]
        tenant, issue_url, f"security-start-{occ.id}-{occ.state_version}"
    )
    if not created.usable:
        verdict = REFUSED if created.verdict == creation.REFUSED else UNKNOWN
        return StartOutcome(verdict, code=created.code if verdict == REFUSED else "", reason=created.reason,
                            occurrence_id=occ.id, workflow_id=workflow_id)

    # 3. The attempt.
    answer = client.start_remediation(
        occ.id,
        work_item_id=created.work_item_id,
        devloop_workflow_id=workflow_id,
        expected_state_version=occ.state_version,
        idempotency_key=idempotency_key("sra-start", occ.id, occ.state_version),
    )
    common = {"occurrence_id": occ.id, "work_item_id": created.work_item_id, "workflow_id": workflow_id}
    if answer.verdict == OK:
        return StartOutcome(REPLAYED if answer.replayed else STARTED, attempt_id=answer.attempt_id, **common)
    if answer.verdict == REFUSED and answer.code == "remediation_attempt_active":
        again = client.get_occurrence(occ.id)
        current = again.occurrence.active_attempt if again.occurrence else None
        if current is None:
            return StartOutcome(UNKNOWN, reason="active attempt could not be read back", **common)
        return StartOutcome(ALREADY_ACTIVE, attempt_id=current.id, **common)
    return StartOutcome(answer.verdict, code=answer.code, reason=answer.reason, **common)
