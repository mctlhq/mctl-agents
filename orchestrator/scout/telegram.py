"""Telegram delivery: sendMessage only."""
from __future__ import annotations

from typing import Any

import httpx

METHOD = "sendMessage"
API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


def send_message(
    token: str,
    chat_id: str,
    text: str,
    markup: dict[str, Any] | None = None,
    reply_to: int | None = None,
    client: httpx.Client | None = None,
) -> int:
    """POST one message; returns its message_id. Errors never contain the token."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if markup:
        payload["reply_markup"] = markup
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    own = client is None
    http = client or httpx.Client(timeout=15.0)
    try:
        resp = http.post(f"{API}/bot{token}/{METHOD}", json=payload)
        data = resp.json() if resp.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise TelegramError(f"{METHOD} request failed: {type(exc).__name__}") from None
    finally:
        if own:
            http.close()
    if resp.status_code // 100 != 2 or not data.get("ok"):
        desc = str(data.get("description", ""))[:200].replace(token, "***")
        raise TelegramError(f"{METHOD} failed: HTTP {resp.status_code} {desc}")
    return int(data.get("result", {}).get("message_id", 0))
