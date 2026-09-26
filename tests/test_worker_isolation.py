"""The long-lived Temporal worker must not carry the agent stack (#149).

ADR-005/006 draw one hard line: Temporal owns durable orchestration, and
anything that drives an LLM, clones repositories or spends credentials
runs in a short-lived Argo sandbox. `mctl-agents-worker` is the process
on the orchestration side of that line — it runs for days, holds the
Temporal connection, and has a 256Mi limit it has already been
OOMKilled against once (agents#179).

Nothing enforced the line, and it had quietly moved: `discover_and_project`
and `detect_orphans` reuse read-only helpers that happen to live in
`run_shepherd`, and `poll_issues_activity` reuses URL helpers from
`run_issue_investigator` — so importing the worker pulled in
`claude_agent_sdk`, `run_implementer`, `mcp_guard` and a module-level
`load_dotenv()`, none of which the worker has any business holding.

Import is not invocation, so this was not yet a live incident. It was
one refactor away from becoming one, and the acceptance criterion in
#149 is about the worker process, not about intent.

A subprocess, not an in-process import: pytest has already imported half
the codebase by the time this runs, so `sys.modules` in THIS process
proves nothing about what the worker alone would load.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Modules that mean "an agent runs here". claude_agent_sdk is the SDK
# itself; the two run_* modules are the coding agents that drive it and
# that carry their own subprocess/credential machinery. orchestrator.auth
# has a module-level load_dotenv() and reads credential env vars directly
# (see agents#364, orchestrator/rate_limit.py's account_label(), which
# imports it lazily specifically to keep it out of this list) -- it must
# never be reachable from the worker's import graph at module scope.
FORBIDDEN_IN_WORKER = ("claude_agent_sdk", "orchestrator.run_implementer", "orchestrator.auth")


def _modules_imported_by(module: str) -> set[str]:
    """Every module name loaded by importing `module` in a fresh process."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}, sys; print(chr(10).join(sorted(sys.modules)))"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"importing {module} failed:\n{result.stderr[-2000:]}"
    return set(result.stdout.split("\n"))


def test_the_worker_does_not_import_the_agent_stack():
    """Importing the worker must not load the SDK or the coding agents.

    If this fails, look at what the newly-added activity imports at module
    level: the fix is a deferred import at the agent's own call site, not
    an exception here. The worker's import graph IS the boundary — an
    activity that can reach the SDK without a deferred import is one
    `await` away from running an agent in the orchestration process.
    """
    loaded = _modules_imported_by("orchestrator.temporal.worker")

    leaked = sorted(name for name in FORBIDDEN_IN_WORKER if name in loaded)
    assert not leaked, (
        f"orchestrator.temporal.worker pulls in {leaked} at import time. "
        "Defer that import to the function that actually runs the agent "
        "(see run_shepherd._normalise_findings / run_issue_investigator._run_agent)."
    )


def test_the_guard_can_actually_see_the_sdk():
    """Control: the probe must be able to detect the SDK when it IS there.

    Without this, deleting the SDK from the environment — or a typo in the
    module name above — would turn the real test into a silent pass.

    orchestrator.run_implementer also imports orchestrator.auth at module
    scope (`from orchestrator.auth import ensure_auth_for_sdk`), so the same
    probe doubles as the control for that entry in FORBIDDEN_IN_WORKER: it
    proves the guard can actually see orchestrator.auth too, not just the
    SDK, before either matters to `test_the_worker_does_not_import_the_agent_stack`.
    """
    loaded = _modules_imported_by("orchestrator.run_implementer")

    assert "claude_agent_sdk" in loaded
    assert "orchestrator.auth" in loaded


def test_capability_module_is_importable_by_the_worker():
    """`orchestrator/capability.py` (mctlhq/mctl-agents#242, ADR 017) is
    stdlib-only by design — the contract module, not the gateway — so the
    worker must be able to import it exactly like `orchestrator/
    context_snapshot.py`, its closest sibling. Regression coverage for T12
    (tasks.md, slice 2): once a caller wires this module in, it must not
    silently pull in claude_agent_sdk/mcp along the way."""
    loaded = _modules_imported_by("orchestrator.capability")

    assert "claude_agent_sdk" not in loaded
    assert "mcp" not in loaded
    assert "yaml" not in loaded  # load_consequence_table() defers its own import


def test_capability_gateway_is_never_imported_by_the_worker():
    """`orchestrator/capability_gateway.py` (mctlhq/mctl-agents#242 slice 2)
    imports `claude_agent_sdk` and `mcp` lazily inside its own functions, so
    merely importing IT stays free of both — but the module itself must
    never be reachable from the worker's import graph at module scope
    (T12, tasks.md). No caller wires it in this slice; this guards the day
    one does."""
    loaded = _modules_imported_by("orchestrator.temporal.worker")

    assert "orchestrator.capability_gateway" not in loaded


def test_the_guard_can_actually_see_capability_gateway():
    """Control for the assertion above: the probe must be able to detect
    `orchestrator.capability_gateway` when it IS present, and confirm that
    module alone (not `orchestrator.capability`) is what pulls in the SDK
    and `mcp`."""
    loaded = _modules_imported_by("orchestrator.capability_gateway")

    assert "orchestrator.capability_gateway" in loaded
    # capability_gateway's own top-level imports are stdlib + orchestrator.*
    # only (claude_agent_sdk/mcp are deferred inside its functions) — this
    # mirrors capability.py's own stdlib-only import graph one level up.
    assert "claude_agent_sdk" not in loaded
    assert "mcp" not in loaded


def test_subagent_wait_is_importable_by_the_worker():
    """The sub-agent-await helper must not drag the SDK in at import time.

    Not merely a nice property: the worker imports `orchestrator.run_shepherd`
    and `orchestrator.run_issue_investigator` at module scope (see
    temporal/activities/discovery.py and orphans.py, and the issue poller), and
    those are exactly the two drivers most likely to need this helper next —
    shepherd is the last delegating driver. A module-scope `claude_agent_sdk`
    import in `subagent_wait` would force each of them into a bespoke workaround
    (a local exception that cannot subclass the shared one, plus a driver-local
    timeout constant) rather than just using it.

    Stated as a positive assertion because the property is invisible otherwise:
    `FORBIDDEN_IN_WORKER` only catches the SDK once some worker-imported module
    already pulls it in, which is one step too late to explain why.
    """
    loaded = _modules_imported_by("orchestrator.subagent_wait")

    assert "claude_agent_sdk" not in loaded, (
        "orchestrator.subagent_wait imports the agent SDK at module scope. "
        "Keep those imports inside LiveTaskLedger.observe/_settle so every "
        "worker-imported driver can use this helper directly."
    )
