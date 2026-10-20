"""The mctl-api token stays out of agent tool subprocesses (mctlhq/mctl-agents#717).

A tool subprocess (the SDK Bash tool and anything it starts) is a child of the
CLI the SDK spawns, so it sees exactly the environment the SDK hands that CLI.
These tests spawn the CLI through the SDK's own transport, with a stub CLI that
starts a shell the way the Bash tool would and records what the shell sees. The
orchestrator process keeps the token: the policy checkpoint's approval store,
the work-context client and the mctl MCP headers read it in-process.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import anyio
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from orchestrator import options

TOKEN = "mctl-admin-token-717"
OPUS = "claude-opus-4-1"


def _stub_cli(tmp_path: Path, out: Path) -> Path:
    """A stand-in for the CLI: one shell child, as the Bash tool would start,
    writes the token it can see (or `unset`) and a control variable."""
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/bin/sh\n"
        f"sh -c 'printf \"%s|%s\" \"${{MCTL_TOKEN-unset}}\" \"${{TOOL_ENV_CONTROL-unset}}\" > \"{out}\"'\n"
    )
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


def _seen_by_a_tool_subprocess(opts: ClaudeAgentOptions, tmp_path: Path) -> tuple[str, str]:
    out = tmp_path / "seen"
    opts.cli_path = _stub_cli(tmp_path, out)

    async def spawn() -> None:
        transport = SubprocessCLITransport(prompt="", options=opts)
        await transport.connect()
        try:
            assert transport._process is not None
            await transport._process.wait()
        finally:
            await transport.close()

    anyio.run(spawn)
    token, control = out.read_text().split("|")
    return token, control


@pytest.fixture
def pod_env(monkeypatch):
    monkeypatch.setenv("MCTL_TOKEN", TOKEN)
    monkeypatch.setenv("TOOL_ENV_CONTROL", "kept")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")


BUILDERS = {
    "service-agent": lambda d: options.build_service_agent_options(d, OPUS),
    "implementer": lambda d: options.build_implementer_agent_options(d, OPUS, d),
    "investigator": lambda d: options.build_issue_investigator_options(d, OPUS, d),
    "mentor": lambda d: options.build_mentor_options(d / "digest", OPUS),
    "shepherd": lambda d: options.build_shepherd_options(d, OPUS),
    "no env at all": lambda d: options._scrubbed(ClaudeAgentOptions()),
}


@pytest.mark.parametrize("build", BUILDERS.values(), ids=BUILDERS.keys())
def test_a_tool_subprocess_sees_no_mctl_token(pod_env, tmp_path, build):
    token, control = _seen_by_a_tool_subprocess(build(tmp_path), tmp_path)
    assert token == ""
    # The rest of the environment still reaches the tool: only the token is cut.
    assert control == "kept"
    # And the orchestrator process itself keeps it.
    assert os.environ["MCTL_TOKEN"] == TOKEN


def test_the_mctl_mcp_server_still_authenticates_without_the_env_var(pod_env, tmp_path):
    """The checkpointed MCP transport is how an agent reaches mctl-api: its
    bearer header is part of the server config, not read from the child's env."""
    opts = options.build_service_agent_options(tmp_path, OPUS)
    assert opts.env["MCTL_TOKEN"] == ""
    assert opts.mcp_servers["mctl"]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_without_the_scrub_the_token_reaches_the_tool(pod_env, tmp_path):
    """The stub sees what the SDK hands the CLI: proves the test above can fail."""
    token, _ = _seen_by_a_tool_subprocess(ClaudeAgentOptions(env={}), tmp_path)
    assert token == TOKEN
