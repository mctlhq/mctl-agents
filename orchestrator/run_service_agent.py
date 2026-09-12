"""Run the service-owner agent for one service.

Usage:
    python -m orchestrator.run_service_agent mctl-web
"""
import sys
import traceback
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, cast

import anyio
from claude_agent_sdk import ClaudeSDKClient, ResultMessage

from config.settings import AGENTS_DIR, SERVICE_AGENT_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.mcp_guard import ensure_mctl_connected
from orchestrator.options import (
    SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS,
    build_service_agent_options,
)
from orchestrator.subagent_wait import (
    LiveTaskLedger,
    OrphanedSubagentError,
    drain_until_settled,
)


class ServiceAgentOrphanedSubagent(OrphanedSubagentError):
    """The run ended while a delegated sub-agent was still live.

    A harness failure, not a content failure. The prompt below walks four
    steps named after the `.claude/agents/{researcher,analyst,spec-writer}.md`
    personas that `setting_sources=["project"]` loads from the agent's cwd, so
    the CLI can launch one of them asynchronously (`isAsync: True`) and end the
    top-level turn with the child still working. Returning at that first
    `ResultMessage` throws the child's inbox entry or proposal away silently.
    mctl-agents#366.

    Raised rather than returned so the loss is visible in the log — but it must
    never be allowed to reach the process exit code, which is why every
    entrypoint goes through `run_service_agent_tolerating_orphans` below.
    """


async def run_service_agent_tolerating_orphans(service: str) -> None:
    """Run one service agent, keeping an orphaned child from binning the parent's work.

    An orphan is, by construction, a PARTIAL success: the parent already wrote
    whatever it wrote — an inbox entry, a proposal triplet — to disk before the
    turn ended, and only the delegated child's contribution is lost. The
    workflow commits that directory in a later step, gated on this step's exit
    status, so exiting non-zero here would discard the parent's work too. That
    is strictly more loss than the pre-#366 behaviour it replaces, which at
    least exited 0 and got the partial committed — the exact inversion of what
    #366 is for.

    So: log loudly, exit 0, let the commit step run. Same trade and same
    rationale as `run_all._safe_run_service` ("the mentor reads whatever
    proposals/ landed on disk, so partial success still produces a useful
    digest"), which covers the RUN_MODE=full task group. This function covers
    the two paths that guard did NOT: `run_all._single_service`, reached by the
    declared `mctl-agents-single-service` operator operation, and `main()`
    below — the `python -m orchestrator.run_service_agent <svc>` invocation
    this module's own docstring documents. Both called the driver bare.

    Deliberately narrow — ONLY the orphan, not `except Exception`. Every other
    failure may mean zero real work happened, and swallowing those would report
    a false green to Argo for a run that produced nothing, which is the bug
    `_safe_run_incident_responder`'s McpNotConnectedError branch exists to
    prevent. The orphan is the one failure mode where "there is something on
    disk worth committing" is guaranteed rather than hoped for.
    """
    try:
        await run_service_agent(service)
    except ServiceAgentOrphanedSubagent as exc:
        print(f"warn: service-agent {service}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


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
        ledger = LiveTaskLedger()
        # receive_messages(), NOT receive_response(): the latter returns at the
        # first ResultMessage, and a ResultMessage ends one TURN, not the RUN.
        # See ServiceAgentOrphanedSubagent above and mctl-agents#366.
        #
        # One generator for both phases: every receive_messages() call returns
        # a fresh generator over the same underlying stream, so a second one
        # would split the messages with the first.
        # cast: receive_messages() is declared AsyncIterator but is an async
        # generator, so it does have aclose(); aclosing() guarantees it is
        # closed on the error path below.
        stream = cast("AsyncGenerator[Any, None]", client.receive_messages())
        async with aclosing(stream):
            async for message in stream:
                # Stream messages. Could be prettier-formatted; just print for now.
                print(message)
                ledger.observe(message)
                # Also stop on stream exhaustion (the `async for` ending on its
                # own): that means the CLI exited.
                if isinstance(message, ResultMessage):
                    break
            if ledger.live:
                print(
                    f"info: turn ended with {ledger.describe()}; "
                    f"awaiting terminal status"
                )
                try:
                    await drain_until_settled(
                        stream, ledger, timeout_s=SERVICE_AGENT_DRAIN_TIMEOUT_SECONDS
                    )
                except OrphanedSubagentError as exc:
                    raise ServiceAgentOrphanedSubagent(
                        f"orphaned sub-agent: {exc}"
                    ) from exc
            if not ledger.all_completed:
                # Quiescent, so NOT an orphan: nothing is still writing into
                # inbox/ or proposals/, and whatever landed on disk is what the
                # mentor will read. Logged so the distinction is visible.
                print(f"warn: {ledger.describe()}")


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
    anyio.run(run_service_agent_tolerating_orphans, service)


if __name__ == "__main__":
    main()
