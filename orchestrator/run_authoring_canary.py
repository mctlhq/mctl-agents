"""Inert authoring canary (mctlhq/mctl-agents#596, #470).

Inert by design: nothing imports, schedules or invokes this module, and it has
no CLI entry. It exists so one real agent addition travels through the governed
authoring path. tests/test_authoring_canary.py fails if that changes.
"""
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient

from config.settings import SERVICE_AGENT_MODEL
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_authoring_canary_options

PROMPT = "Reply with the single word OK. Do not use any tool."


async def run_authoring_canary(workdir: Path) -> None:
    ensure_auth_for_sdk()
    options = build_authoring_canary_options(workdir, SERVICE_AGENT_MODEL)
    async with ClaudeSDKClient(options=options) as client:
        await client.query(PROMPT)
        async for _ in client.receive_response():
            pass
