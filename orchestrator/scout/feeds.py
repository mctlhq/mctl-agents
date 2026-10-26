"""Fetch and parse RSS 2.0 / Atom feeds and XML sitemaps with an explicit tri-state result."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
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
# Sitemap sources: pages fetched for a real title/description, newest first,
# only those modified within SITEMAP_RECENT_HOURS (the rest never reach the
# window anyway).
SITEMAP_MAX_PAGES = 5
SITEMAP_RECENT_HOURS = 48
PAGE_MAX_BYTES = 512 * 1024
SOURCE_TYPES = ("feed", "sitemap")

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
            kind = s.get("type", "feed")
            if kind not in SOURCE_TYPES:
                raise ValueError(f"source {s['name']}: type must be one of {SOURCE_TYPES}")
            if kind == "sitemap" and not str(s.get("path_prefix", "")).startswith("/"):
                raise ValueError(f"source {s['name']}: sitemap needs a path_prefix starting with /")
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


def _slug_title(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    words = slug.replace("_", "-").split("-")
    return " ".join(w for w in words if w).capitalize()


def parse_sitemap(body: bytes, source: str, base_url: str, path_prefix: str,
                  weight: float = 1.0) -> list[Item]:
    """Parse a <urlset> sitemap into undescribed items: one per <url> on the
    sitemap's own host whose path is strictly under path_prefix. lastmod is
    the page's last edit, not its publication, so an edited old page can
    re-enter the window; URL dedupe keeps it from being proposed twice."""
    _reject_dtd(body)
    try:
        root = ET.fromstring(body)  # noqa: S314 -- DOCTYPE/ENTITY rejected above
    except ET.ParseError as exc:
        raise ValueError(f"not well-formed XML: {exc}") from exc
    if _local(root.tag) != "urlset":
        raise ValueError(f"root <{_local(root.tag)}> is not a sitemap urlset")
    host = urlsplit(base_url).netloc.lower()
    prefix = path_prefix if path_prefix.endswith("/") else path_prefix + "/"
    items: list[Item] = []
    for u in root:
        if _local(u.tag) != "url":
            continue
        loc = _child_text(u, "loc")
        parts = urlsplit(loc)
        if (parts.scheme not in ("https", "http") or parts.netloc.lower() != host
                or not parts.path.startswith(prefix) or parts.path.rstrip("/") + "/" == prefix):
            continue
        items.append(Item(
            title=clean_text(_slug_title(loc), 300),
            link=loc,
            summary="",
            published=_parse_date(_child_text(u, "lastmod")),
            source=source,
            weight=weight,
        ))
    return items


class _PageMeta(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:description", "description") and key not in self.meta:
                self.meta[key] = a.get("content", "")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _page_meta(client: httpx.Client, url: str) -> tuple[str, str] | None:
    """(title, description) from a page's head, or None on any failure."""
    try:
        with client.stream("GET", url) as resp:
            if resp.status_code // 100 != 2:
                return None
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > PAGE_MAX_BYTES:
                    break
    except httpx.HTTPError:
        return None
    p = _PageMeta()
    try:
        p.feed(bytes(buf[:PAGE_MAX_BYTES]).decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 -- untrusted HTML, keep the slug title
        return None
    title = p.meta.get("og:title") or p.title
    desc = p.meta.get("og:description") or p.meta.get("description", "")
    return clean_text(title, 300), clean_text(desc)


def _describe_recent(client: httpx.Client, items: list[Item], now: datetime) -> list[Item]:
    cutoff = now - timedelta(hours=SITEMAP_RECENT_HOURS)
    ceiling = now + timedelta(hours=1)  # same tolerance as in_window
    recent = [i for i in items if i.published is not None and cutoff <= i.published <= ceiling]
    recent.sort(key=lambda i: i.published or cutoff, reverse=True)
    described: dict[str, Item] = {}
    for it in recent[:SITEMAP_MAX_PAGES]:
        meta = _page_meta(client, it.link)
        if meta:
            title, desc = meta
            described[it.link] = Item(title or it.title, it.link, desc, it.published, it.source, it.weight)
    return [described.get(i.link, i) for i in items]


def fetch_source(client: httpx.Client, src: dict[str, Any], now: datetime | None = None) -> SourceResult:
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
    weight = float(src.get("weight", 1.0))
    try:
        if src.get("type", "feed") == "sitemap":
            items = parse_sitemap(bytes(buf), name, str(src["url"]), str(src["path_prefix"]), weight)
        else:
            items = parse_feed(bytes(buf), name, weight)
    except ValueError as exc:
        return SourceResult(name, "malformed", str(exc)[:80])
    if src.get("type", "feed") == "sitemap":
        items = _describe_recent(client, items, now or datetime.now(UTC))
    if src.get("date_only"):
        items = [_end_of_day(i) for i in items]
    return SourceResult(name, "ok", items=items)


def _end_of_day(item: Item) -> Item:
    """Date-only feeds stamp a post 00:00 of its day whenever it went up, so a daily
    run would see it ~30h old and drop it. Dating it at the end of its day puts it in
    exactly one run: the first after that day ends."""
    if item.published is None:
        return item
    day = item.published.astimezone(UTC).date()
    return replace(item, published=datetime.combine(day, time.min, UTC) + timedelta(days=1))


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
