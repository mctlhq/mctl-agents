"""Run the service-owner agent for one service.

Usage:
    python -m orchestrator.run_service_agent mctl-web
"""
import sys
import anyio
from claude_agent_sdk import query

from config.settings import AGENTS_DIR, SERVICE_AGENT_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
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
    print(f"\n=== Running agent {service} ({SERVICE_AGENT_MODEL}) ===\n")

    async for message in query(prompt=PROMPT, options=options):
        # Stream messages. Could be prettier-formatted; just print for now.
        print(message)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python -m orchestrator.run_service_agent <service>")
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
