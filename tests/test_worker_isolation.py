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
# that carry their own subprocess/credential machinery.
FORBIDDEN_IN_WORKER = ("claude_agent_sdk", "orchestrator.run_implementer")


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
    """
    loaded = _modules_imported_by("orchestrator.run_implementer")

    assert "claude_agent_sdk" in loaded
