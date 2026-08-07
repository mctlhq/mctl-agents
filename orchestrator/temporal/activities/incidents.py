"""Activity: diagnose stuck analyzing incidents and write accepted proposals.
"""
from __future__ import annotations

from dataclasses import dataclass

from temporalio import activity

from orchestrator.run_incident_responder import run_incident_responder


@dataclass(frozen=True)
class IncidentResponderActivityResult:
    success: bool
    notes: str | None


@activity.defn
async def respond_incidents_activity() -> IncidentResponderActivityResult:
    try:
        await run_incident_responder()
        return IncidentResponderActivityResult(success=True, notes="Incident responder run finished successfully")
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning("respond_incidents_activity failed: %s", exc)
        return IncidentResponderActivityResult(success=False, notes=str(exc))
