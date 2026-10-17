"""Opt-in provider smoke check for the `cheap` model-policy profile.

Runs one tool-less, single-turn claude_agent_sdk query with the model the
`cheap` profile resolves to (honouring CLAUDE_CHEAP_MODEL, so the same script
proves the rollback). Needs real Claude credentials and spends a few cents.
Not collected by pytest and not run in CI.

    uv run python tools/model_smoke.py
    CLAUDE_CHEAP_MODEL=claude-haiku-4-5 uv run python tools/model_smoke.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.model_policy import DEFAULT_POLICY_PATH, ModelPolicy  # noqa: E402


def resolve_cheap_model() -> str:
    policy = ModelPolicy.load()
    probe = ModelPolicy(
        {
            "version": 1,
            "profiles": policy._profiles,
            "tasks": {"smoke": "cheap"},
        }
    )
    return probe.resolve("smoke").model


async def _run(model: str, prompt: str) -> int:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(model=model, max_turns=1, allowed_tools=[])
    result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    if result is None:
        print("FAIL: no ResultMessage received", file=sys.stderr)
        return 1
    usage_models = list((getattr(result, "model_usage", None) or {}).keys())
    print(f"requested={model}")
    print(f"reported_models={usage_models}")
    print(f"usage={result.usage}")
    print(f"duration_ms={result.duration_ms} cost_usd={result.total_cost_usd}")
    if result.is_error:
        print(f"FAIL: result is an error: {result.result}", file=sys.stderr)
        return 1
    if not any(m == model or m.startswith(model) for m in usage_models):
        print("FAIL: reported model does not match requested", file=sys.stderr)
        return 1
    print("OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--prompt", default="Reply with the single word: pong")
    args = parser.parse_args()
    print(f"policy={DEFAULT_POLICY_PATH}")
    return anyio.run(_run, resolve_cheap_model(), args.prompt)


if __name__ == "__main__":
    raise SystemExit(main())
