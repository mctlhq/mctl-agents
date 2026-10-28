"""Count Claude Code safety-classifier interruptions in an SDK stream
(mctlhq/mctl-agents#849).

When Claude Code's client-side safety classifier cuts an assistant turn, the
CLI injects a notice into the conversation ("Your response above was stopped
by a safety classifier -- this is not a tool or API error ..."). It is not a
`refusal` stop reason and not an error: the session goes on and the final
ResultMessage looks like an ordinary success, while tool calls from the
withheld part silently did not run. Spike #796 saw it in 7 of 14 sessions on
a stripped native binary.

This module only makes it visible. It does not retry, rephrase or block.

What it reads. The notice can arrive as assistant text, as a plain user
message, or inside a tool result, so `observe` looks at a message's string
content, every block's `text`, and a ToolResultBlock's `content` (a string or
a list of `{"type": "text", "text": ...}` items). It keeps a count and nothing
else: no text is stored, logged or emitted.

"Could not observe" is not zero. `result()` returns the count only when the
stream was seen whole: at least one ResultMessage arrived and the run ended
without an exception. Otherwise it returns UNOBSERVED, because a stream that
broke off may have carried a notice nobody read. This is the contract the
spike harness (`tools/rea_spike.py`, #841) measured with.

Duck-typed on the SDK's class names, like `tracing.AgentRunObserver`, so this
module never imports `claude_agent_sdk`.
"""
from __future__ import annotations

from typing import Any

#: The stable part of the notice; the surrounding wording and dashes vary.
NOTICE = "stopped by a safety classifier"
UNOBSERVED = "unobserved"


def _texts(message: Any) -> list[str]:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
        if type(block).__name__ != "ToolResultBlock":
            continue
        result = getattr(block, "content", None)
        if isinstance(result, str):
            out.append(result)
        elif isinstance(result, list):
            out.extend(
                item["text"] for item in result
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
    return out


class InterruptionCounter:
    """Counts classifier notices over one SDK session's messages."""

    def __init__(self) -> None:
        self._count = 0
        self._results = 0
        self._failed = False

    def observe(self, message: Any) -> None:
        """Never raises: counting must not break the stream it reads."""
        try:
            if type(message).__name__ == "ResultMessage":
                self._results += 1
                return
            self._count += sum(text.count(NOTICE) for text in _texts(message))
        except Exception:  # noqa: BLE001 — a message we could not read is an incomplete observation
            self._failed = True

    def result(self, error: BaseException | None = None) -> int | str:
        """The count, or UNOBSERVED when the stream was not seen whole."""
        if error is not None or self._failed or self._results == 0:
            return UNOBSERVED
        return self._count
