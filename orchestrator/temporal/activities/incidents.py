"""Activity: incidents raised against one service in one time window.

Stage 6.4 of ADR-006 (#216) — the loop's last question: did the rollout
it just watched break anything? Deliberately NOT the global
IncidentLoopWorkflow, which is paused (#179), global, and cannot answer
"did *my* rollout break something".

Correlation is service + window only. Incidents carry no link to a
release, PR or commit, so anything stronger would be invention; #195/#196
(execution tracing/context) is where that belongs. The window is the
honest boundary: an incident for this service that fired after this
deploy is *plausibly* related, and the workflow reports it as exactly
that — an observation, never a verdict, and never a remediation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from temporalio import activity

from orchestrator.temporal.activities.proposals import (
    REQUEST_TIMEOUT_SECONDS,
    ProposalListingError,
)
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

# One rollout cannot plausibly produce hundreds of distinct incidents, and
# a runaway alert storm must not turn into an unbounded workflow payload.
INCIDENT_QUERY_LIMIT = 50


@dataclass(frozen=True)
class Incident:
    id: str
    title: str
    severity: str | None = None
    status: str | None = None
    started_at: str | None = None


@dataclass(frozen=True)
class IncidentQueryResult:
    incidents: list[Incident] = field(default_factory=list)


@activity.defn
async def list_service_incidents(service: str, since: str) -> IncidentQueryResult:
    """Incidents for ``service`` that started at/after ``since`` (RFC3339)."""
    params = {"service": service, "since": since, "limit": str(INCIDENT_QUERY_LIMIT)}
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get("/api/v1/incidents", params=params, headers=auth_headers())
        except httpx.RequestError as exc:
            raise ProposalListingError(f"listing incidents for {service} failed: {exc}") from exc
    if response.status_code != 200:
        raise ProposalListingError(
            f"listing incidents for {service} returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProposalListingError(f"non-JSON incidents payload for {service}") from exc
    if not isinstance(payload, dict):
        raise ProposalListingError(f"unexpected incidents payload type for {service}")

    items = payload.get("items")
    if not isinstance(items, list):
        # The endpoint normalises a null result to [], so a non-list here
        # means the shape changed rather than "nothing found" — say so
        # instead of silently reporting a clean rollout.
        raise ProposalListingError(f"unexpected incidents items type for {service}")

    incidents: list[Incident] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        incident_id = item.get("id") or item.get("fingerprint")
        if not isinstance(incident_id, str) or not incident_id:
            continue
        incidents.append(
            Incident(
                id=incident_id,
                title=str(item.get("title") or item.get("name") or ""),
                severity=item.get("severity") if isinstance(item.get("severity"), str) else None,
                status=item.get("status") if isinstance(item.get("status"), str) else None,
                started_at=item.get("started_at") if isinstance(item.get("started_at"), str) else None,
            )
        )
    activity.logger.info(
        "incidents service=%s since=%s found=%d", service, since, len(incidents)
    )
    return IncidentQueryResult(incidents=incidents)
