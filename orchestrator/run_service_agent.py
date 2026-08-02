"""Run the service-owner agent for one service.

Usage:
    python -m orchestrator.run_service_agent mctl-web
"""
import sys

import anyio
from claude_agent_sdk import ClaudeSDKClient

from config.settings import AGENTS_DIR, SERVICE_AGENT_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.mcp_guard import ensure_mctl_connected
from orchestrator.options import build_service_agent_options

PROMPT = """\
**Output language: English only. Write every artifact (inbox, proposals, summary report) in English. Do not switch languages even if context/ files contain non-English text.**

Autonomous daily run. **Do not ask the human anything — there is no human.** Work with what you have.

Use `context/` (read-only knowledge base — architecture, ADRs, current version) and `.claude/skills/` as your playbook.

Strict sequence (execute each step and create the corresponding files):

**Step 1 — researcher:**
- Create the file `inbox/{today's ISO date YYYY-MM-DD}.md`
- Sources:
  - GitHub releases of key deps (list in `context/architecture.md`, section "Dependencies for researcher") — via WebFetch
  - CVE / security advisories for the same deps — via WebSearch
  - If `mcp__mctl__*` tools are available, use them to check service status. **If they are not in your tool set, skip silently — do not ask the user, do not attempt to authenticate.**
- Record format strictly as in `.claude/agents/researcher.md`

**Step 2 — analyst:**
- Read the inbox file you just created
- Filter out irrelevant findings, pick a Top-3 with rationale (impact 1-5, effort 1-5)
- Append a `## Top-3 (for spec-writer)` section to the same inbox file (format in `.claude/agents/analyst.md`)
- Cross-check `context/decisions/` — do not propose anything already rejected

**Step 3 — spec-writer:**
- For each Top-3 item create `proposals/<slug>/` with three files: `requirements.md`, `design.md`, `tasks.md` (format in `.claude/agents/spec-writer.md`)
- EARS notation for acceptance criteria
- If a slug already exists, append `-v2`

**Step 4 — short final report** in a single message: what you found (count), what you dropped (count), what you wrote up (list of slugs).

**Important:**
- Never request interactive input from a human.
- Do not edit `context/` — it is read-only.
- If a step is technically impossible (e.g. no network for WebFetch), document that in the inbox file and continue with the next step.
- All written output must be in English."""


async def run_service_agent(service: str) -> None:
    service_dir = AGENTS_DIR / service
    if not service_dir.exists():
        raise SystemExit(f"Agent directory not found: {service_dir}")

    options = build_service_agent_options(service_dir, SERVICE_AGENT_MODEL)
    mcp_configured = bool(options.mcp_servers)
    print(f"\n=== Running agent {service} ({SERVICE_AGENT_MODEL}) ===\n")

    async with ClaudeSDKClient(options=options) as client:
        if mcp_configured:
            # fatal=False: mctl tools are a bonus here (service status
            # checks), not the whole job — the agent still does useful
            # WebSearch/WebFetch/proposal work without them. Warn loudly
            # instead so a real MCTL_TOKEN outage is visible in logs rather
            # than indistinguishable from "nothing to report" (see
            # orchestrator/mcp_guard.py).
            await ensure_mctl_connected(client, fatal=False)
        await client.query(PROMPT)
        async for message in client.receive_response():
            # Stream messages. Could be prettier-formatted; just print for now.
            print(message)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m orchestrator.run_service_agent <service>")
        print(f"Available: {', '.join(SERVICES)}")
        sys.exit(1)

    service = sys.argv[1]
    if service not in SERVICES:
        print(f"Unknown service {service}. Available: {', '.join(SERVICES)}")
        sys.exit(1)

    ensure_auth_for_sdk()
    anyio.run(run_service_agent, service)


if __name__ == "__main__":
    main()
