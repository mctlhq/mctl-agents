"""Incident responder — diagnoses TypeGeneric `analyzing` incidents, writes accepted proposals.

For each TypeGeneric incident stuck in `analyzing` for longer than MIN_AGE_MINUTES,
the responder runs a Claude sub-agent that:
  1. Lists analyzing incidents via mctl MCP tools.
  2. Skips incidents younger than MIN_AGE_MINUTES (they may still self-resolve).
  3. For each qualifying incident, fetches logs and writes a diagnosis proposal to
     agents-state/{target_service}/proposals/incident-{id[:8]}/ with status: accepted.
  4. Resolves the mctl incident with "diagnosis delegated to implementer".

The implementer's find_accepted_proposals() picks up the proposal on its next run
and opens a PR. The shepherd then drives it through to merge.

Usage:
    python -m orchestrator.run_incident_responder
    MIN_AGE_MINUTES=60 python -m orchestrator.run_incident_responder
"""
from __future__ import annotations

import os
import anyio
from pathlib import Path

from config.settings import AGENTS_DIR, SERVICE_AGENT_MODEL
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_incident_responder_options


DEFAULT_STATE_DIR = Path(
    os.getenv(
        "STATE_DIR",
        "/workdir/mctl-gitops/platform-gitops/agents-state",
    )
)
MIN_AGE_MINUTES = int(os.getenv("MIN_AGE_MINUTES", "30"))
RESPONDER_MODEL = os.getenv("INCIDENT_RESPONDER_MODEL", SERVICE_AGENT_MODEL)
RESPONDER_BUDGET_USD = float(os.getenv("INCIDENT_RESPONDER_BUDGET_USD", "2.00"))


def _build_prompt(state_dir: Path, min_age_minutes: int) -> str:
    return f"""\
**Output language: English only.**
**No human present. Do not ask for input. Work with what you have.**

You are the mctl incident responder. Diagnose TypeGeneric incidents stuck in
`analyzing` status and convert them into accepted implementer proposals.

## Steps

**Step 1 — discover**
Call `mctl_list_incidents` with `status=analyzing`.
If the tool is unavailable or returns zero results, print "no analyzing incidents" and stop.

**Step 2 — filter**
Keep only incidents that match ALL of:
- Type or alert name contains "Generic" (case-insensitive), OR the incident has no
  skill match (any incident still analyzing after a long time qualifies).
- `created_at` is older than {min_age_minutes} minutes.
  Use Bash to compute: `python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())"`.
  Compare against each incident's `created_at` field.
- Incident is not already resolved or merged.

Limit: process at most 5 incidents per run.

**Step 3 — diagnose and write**
For each qualifying incident:

a. Call `mctl_get_incident` with the incident ID to get full details.
b. Determine tenant and service from the incident fields.
c. Call `mctl_get_service_logs` with the tenant and service name; read the last ~50 lines.
d. Diagnose the root cause and a concrete fix.
e. Determine target_service:
   - Config/resource/rule change → `mctl-gitops`
   - Go code change → `mctl-agent`
   - Orchestrator change → `mctl-agents`
   - Default: `mctl-gitops`
f. Build slug: `incident-` + first 8 characters of the incident ID.
g. Write the proposal files (see CLAUDE.md for format) to:
   `{state_dir}/{{target_service}}/proposals/{{slug}}/`
   Write in this order: requirements.md → design.md → tasks.md → .status.yaml
h. Call `mctl_resolve_incident` with the incident ID and reason:
   `"diagnosis delegated to implementer — proposal: {{target_service}}/proposals/{{slug}}"`

**Step 4 — report**
Print a summary table:
  incident_id | slug | target_service | action_taken

## Constraints
- Write only inside `{state_dir}/`.
- If you cannot diagnose with confidence, still write the proposal with
  `## Confidence: LOW` in design.md. A low-confidence proposal is better
  than none — the implementer can verify before applying.
- No emoji in proposal files.
- English only in all output.
"""


async def run_incident_responder(state_dir: Path = DEFAULT_STATE_DIR) -> None:
    agent_dir = AGENTS_DIR / "_incident-responder"
    if not agent_dir.exists():
        raise SystemExit(f"Incident responder agent dir not found: {agent_dir}")

    from claude_agent_sdk import query

    prompt = _build_prompt(state_dir, MIN_AGE_MINUTES)
    options = build_incident_responder_options(
        agent_dir=agent_dir,
        model=RESPONDER_MODEL,
        state_dir=state_dir,
    )

    print(f"\n=== Running incident responder ({RESPONDER_MODEL}, state_dir={state_dir}) ===\n")
    async for message in query(prompt=prompt, options=options):
        print(message)


if __name__ == "__main__":
    ensure_auth_for_sdk()
    anyio.run(run_incident_responder)
