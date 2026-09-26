#!/usr/bin/env python3
"""Benchmark harness for mctlhq/mctl-agents#242 slice 4 (task 13, design.md
sec. 4): measures the eager-vs-discovery capability-loading tradeoff this
proposal claims. The measurement itself is an operator step (part C); its
results are in `docs/benchmarks/capability-discovery.md`.

Two independent commands:

    uv run python tools/capability_bench.py schema-bytes
    uv run python tools/capability_bench.py compare EAGER.json DISCOVERY.json

- `schema-bytes` connects to the mctl-api provider ONCE (live: needs
  `MCTL_TOKEN`, real network, and pulls in `claude_agent_sdk`/`mcp`), lists
  its tools, and reports eager's first-turn tool-schema bytes (every
  advertised tool — the profile's `mcp__mctl__*` allow-list is the whole
  wildcard) against discovery's first-turn bytes (the three static gateway
  tool schemas from `orchestrator.capability_gateway.CapabilityGateway.
  sdk_tools()`).
- `compare` takes two already-captured run transcripts — JSON arrays of
  `ResultMessage`-shaped objects (field names matching the SDK's own:
  `session_id`, `model_usage`, `uuid`, `num_turns`, `duration_api_ms`,
  `is_error`), one recorded running the issue-investigator in `eager` mode
  and one in `discovery` mode, against the SAME fixed issue and target SHA
  — and renders the markdown table this proposal's benchmark doc expects:
  input, output, cache-read and cache-creation tokens, turns and wall
  clock, with the sample size (1 run per mode, for this pilot) stated
  explicitly. Token math reuses `orchestrator.usage_ledger.UsageRecorder`'s
  own per-message planning, committing its baseline between messages so
  cumulative `model_usage` becomes per-message deltas (no delivery, no
  network — the recorder is unconditionally constructed with no token,
  which disables delivery by construction). USD cost is NOT computed here: the price catalog is
  server-side (mctl-api), not duplicated in this repository.

Every function performing I/O (`_live_mctl_tools`, the two `_cmd_*`
functions) is a thin wrapper around a pure function
(`schema_bytes_report`/`compare_report`/`render_markdown_table`) this file's
own unit test (`tests/test_capability_bench.py`, T14) exercises directly,
from recorded fixture data, with no model call and no network.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.context_snapshot import canonical_json  # noqa: E402
from orchestrator.usage_ledger import UsageRecorder  # noqa: E402

# ---------------------------------------------------------------------------
# schema-bytes — pure computation
# ---------------------------------------------------------------------------


def _schema_bytes(input_schema: Mapping[str, Any] | None) -> int:
    """Serialized byte size of one JSON input schema — the same
    canonical-JSON convention `orchestrator/capability_gateway.py`'s
    `_discover` already uses for `CapabilityDescriptor.input_schema_bytes`,
    so the two numbers this module reports are directly comparable to that
    one."""
    return len(canonical_json(dict(input_schema or {})))


def eager_first_turn_bytes(mctl_tools: Sequence[Any]) -> int:
    """Sum of every mctl-api tool's input-schema bytes — what
    `mcp_servers=mctl_mcp_config(always_load=True)` plus the profile's
    `mcp__mctl__*` allow-list puts in the model's first turn today.
    `mctl_tools` is whatever a provider's `list_tools()` returned (real
    `mcp.types.Tool`s in production, a plain object with `.inputSchema` in
    tests) — only that one attribute is read."""
    return sum(_schema_bytes(getattr(t, "inputSchema", None)) for t in mctl_tools)


def discovery_first_turn_bytes(gateway_tools: Sequence[Any]) -> int:
    """Sum of the three gateway tools' schema bytes
    (`CapabilityGateway.sdk_tools()`'s `capability_search`/`_describe`/
    `_invoke`, each an `SdkMcpTool` with an `.input_schema` attribute) —
    what discovery mode puts in the model's first turn instead."""
    return sum(_schema_bytes(getattr(t, "input_schema", None)) for t in gateway_tools)


def schema_bytes_report(mctl_tools: Sequence[Any], gateway_tools: Sequence[Any]) -> dict[str, Any]:
    """Pure: the `schema-bytes` command's whole computation, given
    already-fetched tool lists. Separated from live I/O so this is exactly
    what T14 exercises with no network and no model call."""
    eager = eager_first_turn_bytes(mctl_tools)
    discovery = discovery_first_turn_bytes(gateway_tools)
    return {
        "eager_tool_count": len(mctl_tools),
        "eager_schema_bytes": eager,
        "discovery_tool_count": len(gateway_tools),
        "discovery_schema_bytes": discovery,
        "saved_bytes": eager - discovery,
    }


def _live_mctl_tools() -> list[Any]:
    """Live-connect to the mctl-api provider ONCE and list its tools.
    Operator-run only: needs `MCTL_TOKEN` and a real network round trip, and
    pulls in `claude_agent_sdk`/`mcp` — the same RUNTIME half `orchestrator/
    capability_gateway.py`'s own module docstring keeps out of the worker's
    import graph. Deferred import for the same reason; this function is
    never called by anything this repo's test suite exercises."""
    import anyio

    from config.settings import MCTL_MCP_URL
    from orchestrator.capability_gateway import _default_remote_headers

    async def _list() -> list[Any]:
        import mcp
        from mcp.client.streamable_http import streamablehttp_client

        headers = _default_remote_headers()
        async with streamablehttp_client(MCTL_MCP_URL, headers=headers) as (read_stream, write_stream, _get_id):
            async with mcp.ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return list(result.tools)

    return anyio.run(_list)


def _gateway_tools() -> list[Any]:
    """The three static gateway tools. No sealed `CapabilitySet` is needed:
    `CapabilityGateway.sdk_tools()` only reads `self.capability_set` inside
    the closures it returns, never while building the tool list itself (see
    that method's own docstring) — so a placeholder with an empty
    `.capabilities` tuple is enough to ask for the schemas alone."""
    from orchestrator.capability import AbsentPolicyCheckpoint
    from orchestrator.capability_gateway import CapabilityGateway

    gateway = CapabilityGateway(
        capability_set=cast("Any", SimpleNamespace(capabilities=())),
        checkpoint=AbsentPolicyCheckpoint(),
    )
    return gateway.sdk_tools()


# ---------------------------------------------------------------------------
# compare — pure computation
# ---------------------------------------------------------------------------

#: (usage_ledger field, markdown column label). Order is the table's row
#: order. `num_turns`/`duration_api_ms` come straight off the ResultMessage
#: (`usage_ledger.py`'s `common` dict), not the per-model usage breakdown, so
#: this module takes them once per session rather than once per model_key —
#: `_totals_for_transcript` below folds that distinction in.
_TOKEN_FIELDS = (
    ("input_tokens", "Input tokens"),
    ("output_tokens", "Output tokens"),
    ("cache_read_tokens", "Cache-read tokens"),
    ("cache_write_tokens", "Cache-creation tokens"),
)
_PER_MESSAGE_FIELDS = (
    ("num_turns", "Turns"),
    ("duration_api_ms", "Wall clock (API, ms)"),
)
_ALL_FIELDS = _TOKEN_FIELDS + _PER_MESSAGE_FIELDS


def _totals_for_transcript(messages: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Total every usage field `UsageRecorder` extracts from one captured
    run's `ResultMessage`s. Pure: the recorder is constructed with
    `token=""`, which `UsageRecorder.enabled` reports `False` for, and it is
    never asked to deliver, so no network call happens here.

    `model_usage` is cumulative per `(session_id, model_key)`. The recorder
    turns it into per-message deltas only against a committed baseline, and
    `records_for` never commits one, so each message is planned and then
    committed here exactly as `UsageRecorder._record` does minus delivery;
    summing the resulting deltas gives the session's real totals.

    `num_turns`/`duration_api_ms` sit on the ResultMessage itself and are
    cumulative per session too, so each session contributes its largest
    reported value once, and sessions are summed.
    """
    recorder = UsageRecorder("issue-investigator", token="")
    totals: dict[str, int] = {field: 0 for field, _ in _ALL_FIELDS}
    per_session: dict[str, dict[str, int]] = {}
    for raw in messages:
        message = SimpleNamespace(**raw)
        planned = recorder._plan(message)
        recorder._commit(planned)
        records = [record for _, _, _, record in planned]
        if records:
            session = per_session.setdefault(str(records[0].get("session_id", "")), {})
            for field, _ in _PER_MESSAGE_FIELDS:
                value = records[0].get(field)
                if isinstance(value, int):
                    session[field] = max(session.get(field, 0), value)
        for record in records:
            for field, _ in _TOKEN_FIELDS:
                value = record.get(field)
                if isinstance(value, int):
                    totals[field] += value
    for session in per_session.values():
        for field, value in session.items():
            totals[field] += value
    return totals


def compare_report(
    eager_messages: Sequence[Mapping[str, Any]], discovery_messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Pure: the `compare` command's whole computation, given two
    already-loaded transcripts (lists of `ResultMessage`-shaped dicts).
    `sample_size` is always 1 for this pilot — one fixed issue and target
    SHA, one run per mode (requirements.md's benchmark acceptance
    criterion: "the sample size is stated")."""
    return {
        "sample_size": 1,
        "eager": _totals_for_transcript(eager_messages),
        "discovery": _totals_for_transcript(discovery_messages),
    }


def render_markdown_table(report: Mapping[str, Any]) -> str:
    lines = ["| Metric | Eager | Discovery |", "| --- | --- | --- |"]
    for field, label in _ALL_FIELDS:
        lines.append(f"| {label} | {report['eager'][field]} | {report['discovery'][field]} |")
    lines.append("")
    lines.append(f"Sample size: {report['sample_size']} run per mode.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_schema_bytes(args: argparse.Namespace) -> int:
    report = schema_bytes_report(_live_mctl_tools(), _gateway_tools())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _load_transcript(path: str) -> list[dict[str, Any]]:
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, list) or not all(isinstance(item, dict) for item in loaded):
        raise SystemExit(f"{path}: expected a JSON array of ResultMessage objects")
    return loaded


def _cmd_compare(args: argparse.Namespace) -> int:
    eager_messages = _load_transcript(args.eager)
    discovery_messages = _load_transcript(args.discovery)
    report = compare_report(eager_messages, discovery_messages)
    print(render_markdown_table(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema_bytes_parser = subparsers.add_parser(
        "schema-bytes",
        help="live: compare eager vs discovery first-turn tool-schema bytes",
    )
    schema_bytes_parser.set_defaults(func=_cmd_schema_bytes)

    compare_parser = subparsers.add_parser(
        "compare", help="render the eager-vs-discovery token/turn/wall-clock markdown table",
    )
    compare_parser.add_argument("eager", help="path to a captured eager-mode transcript (JSON array)")
    compare_parser.add_argument("discovery", help="path to a captured discovery-mode transcript (JSON array)")
    compare_parser.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
