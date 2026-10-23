# ruff: noqa
"""EXPERIMENTAL comparison baseline for spike #746 (slice 3). Not shipped.

A stdio MCP server in front of one Streamable HTTP upstream, built on the
`mcp` SDK, with the same flags, allowlist, credential sources and refusal
shape as `mctl-runtime proxy`. It exists to be measured, not to be used.

Differences from the Rust proxy, on purpose and documented in the report:
- results are re-serialized by the SDK (pydantic), so key order and number
  text may differ from the upstream bytes;
- error mapping is minimal: any upstream failure becomes `provider-unavailable`;
  there is no timeout/401 parity;
- the upstream session is opened eagerly in the background, not lazily.

The token and the arguments are never logged.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

import anyio
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

DEFAULT_UPSTREAM = "https://api.mctl.ai/mcp"
DEFAULT_ALLOWLIST = "fixtures/allowlist.toml"
DEFAULT_TIMEOUT_MS = 10_000


def load_allowlist(path: str) -> list[str]:
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"allowlist {path}: {type(exc).__name__}") from None
    tools = data.get("tools")
    if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and t for t in tools):
        raise ValueError(f"allowlist {path}: `tools` must be a non-empty list of names")
    return list(tools)


def load_token() -> str | None:
    token_file = os.environ.get("MCTL_RUNTIME_TOKEN_FILE")
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    else:
        token = os.environ.get("MCTL_TOKEN", "").strip()
    return token or None


def refusal(reason: str, detail: str) -> types.CallToolResult:
    # Same shape as `refusal()` in src/bridge.rs.
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{reason}: {detail}")],
        isError=True,
        structuredContent={"reason_code": reason},
    )


class Upstream:
    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self.error: bool = False
        self.ready = anyio.Event()

    async def hold(self, url: str, token: str, timeout_s: float) -> None:
        """Keep the upstream session open for the life of the process."""
        try:
            async with streamablehttp_client(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout_s
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    with anyio.fail_after(timeout_s):
                        await session.initialize()
                    self.session = session
                    self.ready.set()
                    await anyio.sleep_forever()
        except Exception:
            self.error = True
        finally:
            self.session = None
            self.ready.set()

    async def get(self, timeout_s: float) -> ClientSession | None:
        with anyio.move_on_after(timeout_s):
            await self.ready.wait()
        return self.session


def build_server(allowlist: list[str], upstream: Upstream, has_token: bool, timeout_s: float) -> Server:
    server = Server("mctl-runtime-baseline")
    allowed = set(allowlist)

    # Raw request handlers: the decorator API consults a tool cache that can
    # trigger an upstream tools/list for an unlisted name, and it re-wraps
    # results. These return the SDK result objects as they are.
    async def on_list(_req: types.ListToolsRequest) -> types.ServerResult:
        session = await upstream.get(timeout_s) if has_token else None
        if session is None:
            return types.ServerResult(types.ListToolsResult(tools=[]))
        tools: list[types.Tool] = []
        cursor = None
        try:
            for _ in range(10):
                with anyio.fail_after(timeout_s):
                    page = await session.list_tools(cursor=cursor)
                tools.extend(t for t in page.tools if t.name in allowed)
                cursor = page.nextCursor
                if not cursor:
                    break
        except Exception:
            tools = []
        return types.ServerResult(types.ListToolsResult(tools=tools))

    async def on_call(req: types.CallToolRequest) -> types.ServerResult:
        name = req.params.name
        if name not in allowed:
            # Fail closed: no upstream I/O at all.
            return types.ServerResult(refusal("not-eligible", f"{name} is not allowlisted"))
        if not has_token:
            return types.ServerResult(refusal("credential-absent", "no credential configured"))
        session = await upstream.get(timeout_s)
        if session is None:
            return types.ServerResult(refusal("provider-unavailable", "upstream session unavailable"))
        try:
            with anyio.fail_after(timeout_s):
                result = await session.call_tool(name, req.params.arguments or {})
        except Exception as exc:
            return types.ServerResult(refusal("provider-unavailable", type(exc).__name__))
        return types.ServerResult(result)

    server.request_handlers[types.ListToolsRequest] = on_list
    server.request_handlers[types.CallToolRequest] = on_call
    return server


async def serve(args: argparse.Namespace, allowlist: list[str], token: str | None) -> None:
    timeout_s = args.timeout_ms / 1000.0
    upstream = Upstream()
    server = build_server(allowlist, upstream, token is not None, timeout_s)
    async with stdio_server() as (read, write):
        async with anyio.create_task_group() as tg:
            if token is not None:
                tg.start_soon(upstream.hold, args.upstream, token, timeout_s)
            await server.run(read, write, server.create_initialization_options())
            tg.cancel_scope.cancel()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="thin_adapter.py")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    args = parser.parse_args(argv)  # argparse exits 2 on a usage error
    if args.timeout_ms <= 0:
        print("thin_adapter: --timeout-ms must be a positive integer", file=sys.stderr)
        return 2
    try:
        allowlist = load_allowlist(args.allowlist)
    except ValueError as exc:
        print(f"thin_adapter: {exc}", file=sys.stderr)
        return 2
    anyio.run(serve, args, allowlist, load_token())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
