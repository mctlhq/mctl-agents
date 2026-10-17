"""Opt-in provider smoke check and benchmark for the `cheap` model-policy profile.

Runs tool-less, single-turn claude_agent_sdk queries with the model the `cheap`
profile resolves to (honouring CLAUDE_CHEAP_MODEL, so the same script proves the
rollback). Needs real Claude credentials and spends a few cents. Not collected
by pytest and not run in CI.

    uv run python tools/model_smoke.py
    CLAUDE_CHEAP_MODEL=claude-haiku-4-5 uv run python tools/model_smoke.py
    uv run python tools/model_smoke.py --eval
    CLAUDE_CHEAP_MODEL=claude-haiku-4-5 uv run python tools/model_smoke.py --eval

`--eval` runs two fixed mechanical workloads (commit-type classification and
URL extraction, ten inputs each, JSON output) and prints one summary row per
workload. The inputs are constants in this file, so every model is measured on
identical inputs. It is evidence for mechanical tasks only, not for judgement
tasks such as review or root-cause analysis.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.model_policy import ModelPolicy


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


_ISOLATION_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


async def _ask(model: str, prompt: str) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    # `tools=[]` passes `--tools ""` and removes every tool; `allowed_tools=[]`
    # would only empty the auto-approve list. The rest keeps an operator's own
    # Claude Code setup (settings, MCP servers, claude.ai connectors, memory)
    # out of the prompt, so the measurement matches a bare worker container.
    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        tools=[],
        setting_sources=[],
        strict_mcp_config=True,
        env=_ISOLATION_ENV,
    )
    result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result = message
    return result


def _model_matches(result: Any, model: str) -> tuple[bool, list[str]]:
    reported = list((getattr(result, "model_usage", None) or {}).keys())
    # Every reported model must be the requested one: a second model (the CLI's
    # background small-fast model) would leak into the measured tokens and cost.
    return bool(reported) and all(m == model or m.startswith(model) for m in reported), reported


async def _smoke(model: str, prompt: str) -> int:
    result = await _ask(model, prompt)
    if result is None:
        print("FAIL: no ResultMessage received", file=sys.stderr)
        return 1
    matches, reported = _model_matches(result, model)
    print(f"requested={model}")
    print(f"reported_models={reported}")
    print(f"usage={result.usage}")
    print(f"duration_ms={result.duration_ms} cost_usd={result.total_cost_usd}")
    print(f"result={result.result!r}")
    if result.is_error:
        print(f"FAIL: result is an error: {result.result}", file=sys.stderr)
        return 1
    if not matches:
        print("FAIL: reported models do not all match the requested one", file=sys.stderr)
        return 1
    print("OK")
    return 0


# --- evaluation workloads ----------------------------------------------------

_COMMIT_TYPES = ("feat", "fix", "docs", "test", "ci", "refactor", "chore")

_COMMIT_PROMPT = (
    "Classify the change described below with exactly one conventional-commit "
    f"type from this list: {', '.join(_COMMIT_TYPES)}.\n"
    'Answer with JSON only, in the form {{"type": "<type>"}}, and nothing else.\n\n'
    "Change: {text}"
)

_COMMITS: tuple[tuple[str, str], ...] = (
    ("Add a --dry-run flag to the deploy command so users can preview changes", "feat"),
    ("Stop the scheduler from crashing when the cron expression has trailing spaces", "fix"),
    ("Explain the rollback procedure in the README", "docs"),
    ("Add unit tests for the retry backoff calculation", "test"),
    ("Run the GitHub Actions lint job on pull requests as well as pushes", "ci"),
    ("Split the 600-line handler module into three files without changing behaviour", "refactor"),
    ("Bump the pinned pre-commit hook versions", "chore"),
    ("Return 404 instead of 500 when the requested tenant does not exist", "fix"),
    ("Support exporting reports as CSV in addition to PDF", "feat"),
    ("Correct a typo in the API reference page for the /health endpoint", "docs"),
)

_URL_PROMPT = (
    "Extract every http or https URL from the text below, in order of appearance, "
    "without trailing punctuation.\n"
    'Answer with JSON only, in the form {{"urls": ["..."]}}, and nothing else. '
    'Use {{"urls": []}} when there are none.\n\n'
    "Text: {text}"
)

_URL_TEXTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("See https://example.com/docs for details.", ("https://example.com/docs",)),
    ("No links in this sentence at all.", ()),
    (
        "Mirror: http://mirror.example.org/pkg.tar.gz, primary: https://cdn.example.net/pkg.tar.gz.",
        ("http://mirror.example.org/pkg.tar.gz", "https://cdn.example.net/pkg.tar.gz"),
    ),
    ("The dashboard (https://grafana.example.io/d/abc?orgId=1) shows it.", ("https://grafana.example.io/d/abc?orgId=1",)),
    ("Email ops@example.com, not a URL; ftp://files.example.com is not http either.", ()),
    ("Issue https://github.com/acme/repo/issues/42 and PR https://github.com/acme/repo/pull/43!", (
        "https://github.com/acme/repo/issues/42",
        "https://github.com/acme/repo/pull/43",
    )),
    ("Local: http://localhost:8080/health", ("http://localhost:8080/health",)),
    ("Read <https://example.com/a#section-2> first.", ("https://example.com/a#section-2",)),
    ("example.com without a scheme does not count.", ()),
    (
        "Three: https://a.example.com, https://b.example.com/x; and https://c.example.com/y?z=1.",
        ("https://a.example.com", "https://b.example.com/x", "https://c.example.com/y?z=1"),
    ),
)


@dataclass
class _Row:
    workload: str
    model: str
    correct: int = 0
    malformed: int = 0
    errors: int = 0
    total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    durations_ms: list[int] = field(default_factory=list)


def _parse_json(text: str | None) -> Any:
    """Parse a reply as JSON; a single ```json fence is tolerated, nothing else."""
    body = (text or "").strip()
    if body.startswith("```") and body.endswith("```"):
        body = body.strip("`").removeprefix("json").strip()
    return json.loads(body)


async def _run_workload(
    model: str,
    name: str,
    items: tuple[tuple[str, Any], ...],
    prompt: str,
    check: Any,
) -> _Row:
    row = _Row(workload=name, model=model)
    for text, expected in items:
        row.total += 1
        result = await _ask(model, prompt.format(text=text))
        if result is None or result.is_error or not _model_matches(result, model)[0]:
            row.errors += 1
            continue
        usage = result.usage or {}
        row.input_tokens += (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
        )
        row.output_tokens += int(usage.get("output_tokens") or 0)
        row.cost_usd += float(result.total_cost_usd or 0.0)
        row.durations_ms.append(int(result.duration_ms))
        try:
            answer = _parse_json(result.result)
        except ValueError:
            row.malformed += 1
            continue
        if check(answer, expected):
            row.correct += 1
    return row


def _check_commit(answer: Any, expected: str) -> bool:
    return isinstance(answer, dict) and answer.get("type") == expected


def _check_urls(answer: Any, expected: tuple[str, ...]) -> bool:
    return isinstance(answer, dict) and tuple(answer.get("urls") or ()) == expected


async def _eval(model: str) -> int:
    rows = [
        await _run_workload(model, "commit-type", _COMMITS, _COMMIT_PROMPT, _check_commit),
        await _run_workload(model, "url-extract", _URL_TEXTS, _URL_PROMPT, _check_urls),
    ]
    print("| workload | model | correct | malformed JSON | errors | median latency ms "
          "| input tokens | output tokens | cost USD |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        median = int(statistics.median(r.durations_ms)) if r.durations_ms else 0
        print(
            f"| {r.workload} | {r.model} | {r.correct}/{r.total} | {r.malformed}/{r.total} "
            f"| {r.errors}/{r.total} | {median} | {r.input_tokens} | {r.output_tokens} "
            f"| {r.cost_usd:.4f} |"
        )
    return 1 if any(r.errors for r in rows) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--prompt", default="Reply with the single word: pong")
    parser.add_argument(
        "--eval",
        action="store_true",
        help="run the two fixed mechanical workloads instead of the single smoke query",
    )
    args = parser.parse_args()
    model = resolve_cheap_model()
    if args.eval:
        return anyio.run(_eval, model)
    return anyio.run(_smoke, model, args.prompt)


if __name__ == "__main__":
    raise SystemExit(main())
