"""Run every service agent in parallel, then the mentor.

Usage:
    python -m orchestrator.run_all                # mode=full (default)
    RUN_MODE=mentor-only python -m orchestrator.run_all
    RUN_MODE=single-service RUN_SERVICE=mctl-api python -m orchestrator.run_all
    RUN_MODE=incident-responder python -m orchestrator.run_all

Modes:
    full                — all service agents in parallel, then the mentor (default)
    mentor-only         — mentor only, reads existing proposals/ from state
    single-service      — one agent (name in RUN_SERVICE), no mentor
    incident-responder  — diagnose TypeGeneric analyzing incidents, write accepted proposals
"""
import os
import sys
import traceback
import anyio

from config.settings import ROTATING_SERVICES, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.run_service_agent import run_service_agent
from orchestrator.run_mentor import run_mentor
from orchestrator.run_incident_responder import McpNotConnectedError, run_incident_responder

# Distinct from run_issue_poller.py's RATE_LIMIT_EXIT_CODE=3 — different
# workflow template, no collision risk; Argo's assert-attempt only checks
# whether the step status is Succeeded, not the specific exit code.
MCP_NOT_CONNECTED_EXIT_CODE = 4


async def _safe_run_service(service: str) -> None:
    """Run one service agent and swallow any failure.

    Without this, anyio.create_task_group is fail-fast: if a single agent
    raises (e.g. error_max_budget_usd, network blip, SDK error), the whole
    TaskGroup tears down and every other agent is cancelled — even those
    that already finished writing proposals. The commit-and-push step in
    the workflow then never runs and money is spent for no output.

    We deliberately log + drop. The mentor that runs afterwards reads
    whatever proposals/ landed on disk, so partial success still produces
    a useful digest.
    """
    try:
        await run_service_agent(service)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — intentional broad catch
        # SystemExit is raised by run_service_agent when the agent
        # directory is missing (run_service_agent.py:55). It inherits
        # from BaseException, not Exception, so it would otherwise
        # bypass this handler and the TaskGroup would still tear down.
        # We deliberately keep KeyboardInterrupt / CancelledError on the
        # BaseException side — those should propagate.
        print(
            f"⚠️  service-agent {service} failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)


async def _safe_run_incident_responder() -> None:
    """Run the incident responder and swallow any *transient* failure.

    Unlike the per-service agents in `_full`, this mode has no task group to
    protect it — a raised exception (e.g. error_max_budget_usd, a flaky
    mctl MCP call, an SDK error) propagates straight out of main() and the
    process exits non-zero. Argo then marks the incident-responder job
    Failed, which raises a workflow_failed incident about the incident
    responder itself with no useful logs (see incident
    argo-mctl-agents-incidents-1785053700-1785053856 / proposal b892d8c7).

    The responder is best-effort diagnostics: any proposals it already
    wrote to state_dir before failing are still useful, and a transient
    failure here should not surface as a platform incident. We log + drop,
    matching _safe_run_service's rationale — EXCEPT for
    McpNotConnectedError, which means zero real work happened and must not
    be reported as a false green (see that exception's docstring).
    """
    try:
        await run_incident_responder()
    except McpNotConnectedError as exc:
        # Not a transient/best-effort failure — the pipeline did zero real
        # work, so unlike everything else here this must NOT be swallowed.
        # Exit non-zero so Argo's assert-attempt gate (which only checks
        # step status) correctly reports failure instead of a false green,
        # and run-fallback gets a chance to retry on the account-2 token.
        print(f"❌ incident-responder: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(MCP_NOT_CONNECTED_EXIT_CODE)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — intentional broad catch
        print(
            f"⚠️  incident-responder failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)


async def _full() -> None:
    async with anyio.create_task_group() as tg:
        for service in ROTATING_SERVICES:
            tg.start_soon(_safe_run_service, service)
    await run_mentor()


async def _mentor_only() -> None:
    print("=== mode=mentor-only — skipping service agents ===")
    await run_mentor()


async def _single_service(service: str) -> None:
    if service not in SERVICES:
        print(
            f"ERROR: unknown service '{service}'. "
            f"Valid: {', '.join(SERVICES)}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"=== mode=single-service — only {service}, no mentor ===")
    await run_service_agent(service)


async def main() -> None:
    mode = os.getenv("RUN_MODE", "full").strip()
    service = os.getenv("RUN_SERVICE", "").strip()

    if mode == "full":
        await _full()
    elif mode == "mentor-only":
        await _mentor_only()
    elif mode == "single-service":
        await _single_service(service)
    elif mode == "incident-responder":
        print("=== mode=incident-responder — diagnosing TypeGeneric analyzing incidents ===")
        await _safe_run_incident_responder()
    else:
        print(
            f"ERROR: unknown RUN_MODE '{mode}'. "
            f"Valid: full, mentor-only, single-service, incident-responder",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    ensure_auth_for_sdk()
    anyio.run(main)
