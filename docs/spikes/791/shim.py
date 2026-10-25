"""Loopback adapter for mctlhq/mctl-agents#791: Anthropic Messages -> OpenAI Chat Completions.

Used only if the snapshot's `POST /v1/messages` probe fails. A pure codec: no
retries, no model logic, no tool execution. Anything it cannot translate raises
`ShimUnsupported` (HTTP 501, recorded by the caller); nothing is dropped
silently. It binds to loopback only and requires a per-run token. Logs carry
request ids and sizes, never content. Nothing under `orchestrator/` imports it.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOOPBACK = "127.0.0.1"
#: Request fields with a defined translation.
_MAPPED = {"model", "system", "messages", "tools", "max_tokens", "stream", "temperature", "top_p",
           "stop_sequences", "tool_choice"}
#: Request fields with no OpenAI equivalent; reported in `notes`, never silently lost.
_NOTED = {"metadata", "thinking", "top_k"}
_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use"}


class ShimUnsupported(ValueError):
    """The request or response uses something the codec does not translate."""


def _text_of(content: Any, where: str) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, Mapping) or block.get("type") != "text":
            kind = block.get("type") if isinstance(block, Mapping) else type(block).__name__
            raise ShimUnsupported(f"shim_unsupported: {where} block {kind}")
        parts.append(str(block.get("text", "")))
    return "".join(parts)


def messages_to_chat(req: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Translate a Messages request. Returns (chat request, noted-but-dropped fields)."""
    unknown = sorted(set(req) - _MAPPED - _NOTED)
    if unknown:
        raise ShimUnsupported(f"shim_unsupported: request field(s) {unknown}")
    chat: dict[str, Any] = {"model": req.get("model", ""), "messages": []}
    if req.get("system"):
        chat["messages"].append({"role": "system", "content": _text_of(req["system"], "system")})
    for msg in req.get("messages", []):
        role, content = msg.get("role"), msg.get("content")
        if isinstance(content, str):
            chat["messages"].append({"role": role, "content": content})
            continue
        text: list[str] = []
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for block in content or []:
            kind = block.get("type") if isinstance(block, Mapping) else None
            if kind == "text":
                text.append(str(block.get("text", "")))
            elif kind == "tool_use" and role == "assistant":
                calls.append({"id": block["id"], "type": "function", "function": {
                    "name": block["name"], "arguments": json.dumps(block.get("input", {}))}})
            elif kind == "tool_result" and role == "user":
                body = _text_of(block.get("content", ""), "tool_result")
                if block.get("is_error"):
                    body = "[tool error] " + body
                results.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": body})
            else:
                raise ShimUnsupported(f"shim_unsupported: {role} content block {kind!r}")
        chat["messages"].extend(results)  # tool results must directly follow the tool calls
        if text or calls:
            entry: dict[str, Any] = {"role": role, "content": "".join(text) or None}
            if calls:
                entry["tool_calls"] = calls
            chat["messages"].append(entry)
    if req.get("tools"):
        chat["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema", {"type": "object"})}} for t in req["tools"]]
    if "tool_choice" in req:
        tc = req["tool_choice"]
        kind = tc.get("type") if isinstance(tc, Mapping) else None
        if kind == "auto":
            chat["tool_choice"] = "auto"
        elif kind == "any":
            chat["tool_choice"] = "required"
        elif kind == "tool":
            chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        else:
            raise ShimUnsupported(f"shim_unsupported: tool_choice {tc!r}")
    for src, dst in (("max_tokens", "max_tokens"), ("temperature", "temperature"),
                     ("top_p", "top_p"), ("stop_sequences", "stop")):
        if src in req:
            chat[dst] = req[src]
    return chat, sorted(_NOTED.intersection(req))


def chat_to_message(resp: Mapping[str, Any], model: str) -> dict[str, Any]:
    """Translate a (non-streamed) Chat Completions response into a Messages response."""
    choices = resp.get("choices") or []
    if len(choices) != 1:
        raise ShimUnsupported(f"shim_unsupported: {len(choices)} choices")
    choice = choices[0]
    msg = choice.get("message") or {}
    blocks: list[dict[str, Any]] = []
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError as exc:
            raise ShimUnsupported("shim_unsupported: malformed tool_call arguments") from exc
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args})
    finish = choice.get("finish_reason")
    if finish not in _STOP:
        raise ShimUnsupported(f"shim_unsupported: finish_reason {finish!r}")
    stop_reason = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else _STOP[finish]
    usage = resp.get("usage") or {}
    msg_id = resp.get("id") or f"msg_{uuid.uuid4().hex[:12]}"
    return {"id": msg_id, "type": "message", "role": "assistant",
            "model": resp.get("model") or model, "content": blocks, "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)}}


def message_to_sse(message: Mapping[str, Any]) -> str:
    """Frame a complete Messages response as the SSE stream the CLI expects."""
    def ev(name: str, data: Mapping[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"

    usage = message["usage"]
    out = [ev("message_start", {"message": {**message, "content": [], "stop_reason": None,
                                            "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 0}}})]
    for i, block in enumerate(message["content"]):
        if block["type"] == "text":
            out.append(ev("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}}))
            out.append(ev("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": block["text"]}}))
        else:
            out.append(ev("content_block_start", {"index": i, "content_block": {
                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}))
            out.append(ev("content_block_delta", {"index": i, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        out.append(ev("content_block_stop", {"index": i}))
    out.append(ev("message_delta", {"delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                                    "usage": {"output_tokens": usage["output_tokens"]}}))
    out.append(ev("message_stop", {}))
    return "".join(out)


#: (chat request) -> chat response. The real one POSTs to Hetzner; tests inject a fake.
Upstream = Callable[[dict[str, Any]], dict[str, Any]]


class ShimServer:
    def __init__(self, upstream: Upstream, *, host: str = LOOPBACK, port: int = 0, token: str | None = None) -> None:
        if host != LOOPBACK:
            raise ValueError(f"shim binds to {LOOPBACK} only, not {host!r}")
        self.token = token or secrets.token_urlsafe(32)
        self.unsupported: list[str] = []
        shim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # request ids and sizes only
                print(f"shim: {self.command} {self.path.split('?')[0]}", file=sys.stderr)

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                auth = self.headers.get("authorization", "").removeprefix("Bearer ")
                presented = self.headers.get("x-api-key") or auth
                if not hmac.compare_digest(presented.encode(), shim.token.encode()):
                    return self._send(401, b'{"error":"bad token"}')
                if self.path.split("?")[0] != "/v1/messages":
                    return self._send(404, b'{"error":"not found"}')
                try:
                    req = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                    chat, _ = messages_to_chat(req)
                    chat["stream"] = False
                    message = chat_to_message(upstream(chat), str(req.get("model", "")))
                except ShimUnsupported as exc:
                    shim.unsupported.append(str(exc))
                    err = {"type": "error", "error": {"type": "shim_unsupported", "message": str(exc)}}
                    return self._send(501, json.dumps(err).encode())
                except Exception as exc:  # noqa: BLE001 — surfaced as a provider failure, never retried
                    err = {"type": "error", "error": {"type": "upstream_error", "message": type(exc).__name__}}
                    return self._send(502, json.dumps(err).encode())
                if req.get("stream"):
                    return self._send(200, message_to_sse(message).encode(), "text/event-stream")
                self._send(200, json.dumps(message).encode())

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self._httpd.server_address[1]}"

    def __enter__(self) -> ShimServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
