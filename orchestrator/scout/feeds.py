"""Fetch and parse RSS 2.0 / Atom feeds with an explicit tri-state result."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.parsers import expat

import httpx
import yaml

USER_AGENT = "mctl-agents-scout/1 (+https://github.com/mctlhq/mctl-agents)"
TIMEOUT_SECONDS = 15.0
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
SUMMARY_CHARS = 600

Status = Literal["ok", "failed", "malformed", "disabled"]


@dataclass(frozen=True)
class Item:
    title: str
    link: str
    summary: str
    published: datetime | None
    source: str
    weight: float = 1.0


@dataclass
class SourceResult:
    name: str
    status: Status
    reason: str = ""
    items: list[Item] = field(default_factory=list)

    def label(self) -> str:
        if self.status == "ok":
            return f"ok({len(self.items)})"
        if self.status == "disabled":
            return f"disabled ({self.reason})" if self.reason else "disabled"
        return f"{self.status}({self.reason})" if self.reason else self.status


@dataclass(frozen=True)
class Config:
    sources: list[dict[str, Any]]
    top_n: int
    weights: dict[str, float]
    window_hours: int
    dedupe_days: int


class ConfigError(ValueError):
    pass


def load_sources(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text())
        sources = raw["sources"]
        ranking = raw["ranking"]
        weights = {k: float(ranking["weights"][k]) for k in ("impact", "novelty", "urgency", "cost")}
        top_n = int(ranking["top_n"])
        window = int(raw["window_hours"])
        dedupe = int(raw["dedupe_days"])
        if not isinstance(sources, list):
            raise TypeError("sources must be a list")
        for s in sources:
            if not s["name"] or not s["url"]:
                raise ValueError("source needs name and url")
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid scout config {path}: {exc}") from exc
    if top_n < 1 or window < 1 or dedupe < 1:
        raise ConfigError("top_n, window_hours and dedupe_days must be positive")
    return Config(sources, top_n, weights, window, dedupe)


_TAG_RE = re.compile(r"<[^>]*>")


def clean_text(value: str | None, limit: int = SUMMARY_CHARS) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    return " ".join(text.split())[:limit]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, *names: str) -> str:
    for child in el:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()  # type: ignore[union-attr]
    return ""


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for child in entry:
        if _local(child.tag) == "link":
            href = child.get("href", "")
            if child.get("rel", "alternate") == "alternate" and href:
                return href
            fallback = fallback or href
    return fallback


def _strip_feed_tracking(link: str) -> str:
    """Drop Medium's per-feed `source=rss...` tag so the Source URL (and dedupe key)
    is the article itself, not which feed it came through."""
    parts = urlsplit(link)
    if "source=rss" not in parts.query:
        return link
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not (k == "source" and v.startswith("rss"))]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _refuse_dtd(*_args: object) -> None:
    raise ValueError("DOCTYPE/ENTITY not allowed")


def _reject_dtd(body: bytes) -> None:
    """Refuse a document that declares a DTD or an entity.

    Expat's declaration handlers fire only on real declarations, so markup
    quoted inside CDATA (GitHub's feeds embed `<!DOCTYPE html>` in
    content:encoded) is not mistaken for one, as a byte search would. The
    handler raises before any declared entity can be expanded.
    """
    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = _refuse_dtd
    p.EntityDeclHandler = _refuse_dtd
    try:
        p.Parse(body, True)
    except expat.ExpatError as exc:
        raise ValueError(f"not well-formed XML: {exc}") from exc


def parse_feed(body: bytes, source: str, weight: float = 1.0) -> list[Item]:
    """Parse RSS 2.0 or Atom. Raises ValueError (malformed) on anything else."""
    _reject_dtd(body)
    try:
        root = ET.fromstring(body)  # noqa: S314 -- DOCTYPE/ENTITY rejected above
    except ET.ParseError as exc:
        raise ValueError(f"not well-formed XML: {exc}") from exc
    kind = _local(root.tag)
    if kind == "rss":
        entries = [e for ch in root for e in ch if _local(ch.tag) == "channel" and _local(e.tag) == "item"]
    elif kind == "feed":
        entries = [e for e in root if _local(e.tag) == "entry"]
    else:
        raise ValueError(f"root <{kind}> is neither rss nor feed")
    items: list[Item] = []
    for e in entries:
        link = _atom_link(e) if kind == "feed" else _child_text(e, "link")
        # Telegram rejects the whole HTML message over one non-http(s) href;
        # an empty link is skipped by run_scout.
        if not link.lower().startswith(("https://", "http://")):
            link = ""
        link = _strip_feed_tracking(link)
        date = _parse_date(_child_text(e, "pubDate", "published", "updated", "date"))
        items.append(Item(
            title=clean_text(_child_text(e, "title"), 300),
            link=link,
            summary=clean_text(_child_text(e, "description", "summary", "content", "encoded")),
            published=date,
            source=source,
            weight=weight,
        ))
    return items


def fetch_source(client: httpx.Client, src: dict[str, Any]) -> SourceResult:
    name = str(src["name"])
    if src.get("enabled", True) is False:
        return SourceResult(name, "disabled", str(src.get("note", "")))
    try:
        with client.stream("GET", str(src["url"])) as resp:
            if resp.status_code // 100 != 2:
                return SourceResult(name, "failed", f"HTTP {resp.status_code}")
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BYTES:
                    return SourceResult(name, "failed", "size")
    except httpx.TimeoutException:
        return SourceResult(name, "failed", "timeout")
    except httpx.HTTPError as exc:
        return SourceResult(name, "failed", type(exc).__name__)
    try:
        items = parse_feed(bytes(buf), name, float(src.get("weight", 1.0)))
    except ValueError as exc:
        return SourceResult(name, "malformed", str(exc)[:80])
    return SourceResult(name, "ok", items=items)


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        transport=transport,
    )


def in_window(
    items: list[Item], now: datetime, hours: int, undated_hook: Callable[[], None] | None = None
) -> tuple[list[Item], int]:
    """Keep items within [now-hours, now+1h]. Returns (kept, undated_count)."""
    lo, hi = now - timedelta(hours=hours), now + timedelta(hours=1)
    kept: list[Item] = []
    undated = 0
    for it in items:
        if it.published is None:
            undated += 1
        elif lo <= it.published <= hi:
            kept.append(it)
    return kept, undated
