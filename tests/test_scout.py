"""Offline tests for the scout (mctlhq/mctl-agents#797)."""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from orchestrator import run_scout
from orchestrator.options import build_scout_options
from orchestrator.scout import dedupe, feeds, render, scoring, telegram

NOW = datetime(2026, 10, 26, 6, 40, tzinfo=UTC)
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>A &amp; B</title>
<link>https://x.test/a?utm_source=z</link><description>&lt;p&gt;hello&lt;/p&gt;</description>
<pubDate>{date}</pubDate></item></channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>T</title>
<link rel="alternate" href="https://x.test/t"/><summary>S</summary>
<updated>2026-10-26T05:00:00Z</updated></entry></feed>"""


def _client(handler):
    return feeds.make_client(httpx.MockTransport(handler))


def _fetch(handler):
    with _client(handler) as c:
        return feeds.fetch_source(c, {"name": "s", "url": "https://x.test/f"})


def test_failures_are_not_ok_zero():
    def boom(request):
        raise httpx.ConnectTimeout("t")
    assert _fetch(boom).status == "failed"
    assert _fetch(lambda r: httpx.Response(503)).status == "failed"
    assert _fetch(lambda r: httpx.Response(200, text="<html></html>")).status == "malformed"
    assert _fetch(lambda r: httpx.Response(200, text="not xml")).status == "malformed"


def test_doctype_entity_and_size():
    assert _fetch(lambda r: httpx.Response(200, text="<!DOCTYPE x><rss/>")).status == "malformed"
    assert _fetch(lambda r: httpx.Response(200, text='<!ENTITY a "b"><rss/>')).status == "malformed"
    res = _fetch(lambda r: httpx.Response(200, content=b"x" * (feeds.MAX_BYTES + 10)))
    assert (res.status, res.reason) == ("failed", "size")


def test_parse_rss_atom_and_disabled():
    r = _fetch(lambda req: httpx.Response(200, text=RSS.format(date="Mon, 26 Oct 2026 05:00:00 GMT")))
    assert r.status == "ok" and r.items[0].title == "A & B" and r.items[0].summary == "hello"
    assert r.items[0].published == datetime(2026, 10, 26, 5, tzinfo=UTC)
    a = _fetch(lambda req: httpx.Response(200, text=ATOM))
    assert a.items[0].link == "https://x.test/t" and a.items[0].published is not None
    bad = feeds.parse_feed(ATOM.replace("https://x.test/t", "javascript:alert(1)").encode(), "s")
    assert bad[0].link == ""

    def never(request):
        raise AssertionError("disabled source fetched")
    with _client(never) as c:
        d = feeds.fetch_source(c, {"name": "d", "url": "https://x.test", "enabled": False, "note": "n"})
    assert d.status == "disabled"


def test_window():
    def it(h):
        return feeds.Item("t", "l", "", None if h is None else NOW - timedelta(hours=h), "s")
    kept, undated = feeds.in_window([it(25), it(27), it(None)], NOW, 26)
    assert len(kept) == 1 and undated == 1


def test_normalise_url():
    a = dedupe.normalise_url("HTTPS://Example.com/p/?utm_source=a&fbclid=1&k=v#frag")
    assert a == "https://example.com/p?k=v"
    assert dedupe.normalise_url("https://example.com/p?ref=x&mc_cid=2") == "https://example.com/p"


def test_scout_issue_urls(monkeypatch):
    class P:
        stdout = json.dumps({"body": "Source: https://Ex.com/a/?utm_x=1\nQ"}) + "\n"
    monkeypatch.setattr(dedupe, "refresh_github_token", lambda: None)
    monkeypatch.setattr(dedupe, "run_capturing", lambda *a, **k: P())
    assert dedupe.scout_issue_urls(90) == {"https://ex.com/a"}

    def fail(*a, **k):
        raise OSError("x")
    monkeypatch.setattr(dedupe, "run_capturing", fail)
    assert dedupe.scout_issue_urls(90) is None


class _Msg:
    def __init__(self, out, cost):
        self.structured_output = out
        self.total_cost_usd = cost


_Msg.__name__ = "ResultMessage"


class StubClient:
    def __init__(self, responder, seen):
        self.responder, self.seen = responder, seen

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_response(self):
        yield self.responder(self.prompt)


def _factory(responder, seen):
    @asynccontextmanager
    async def f(options):
        seen.append(options)
        yield StubClient(responder, seen)
    return f


def test_options_are_tool_free():
    o = build_scout_options(Path("/x"), "m", {"type": "object"})
    assert o.tools == [] and o.allowed_tools == [] and o.mcp_servers == {}
    assert o.setting_sources == [] and o.output_format is not None


def _item(link="https://x.test/a", weight=1.0, pub=None):
    return feeds.Item("T", link, "S", pub or NOW, "src", weight)


def _row(i, **kw):
    base = {"id": i, "impact": 3, "novelty": 3, "urgency": 1, "cost": 2, "repo": "mctl-api", "related": [],
            "question": "q?", "outcomes": "o", "pitch_ru": "p", "why_en": "w"}
    return {**base, **kw}


def test_validate_row():
    repos = ["mctl-api", ".github"]
    sent = {"a0"}
    assert scoring.validate_row(_row("zz"), sent, repos) is None
    assert scoring.validate_row(_row("a0", impact=7), sent, repos) is None
    assert scoring.validate_row(_row("a0", repo="evil"), sent, repos)["repo"] == ".github"
    v = scoring.validate_row(_row("a0", related=["mctlhq/mctl-api#4", "bad", ".github#41"]), sent, repos)
    assert v["related"] == ["mctlhq/mctl-api#4", ".github#41"]


def test_passes_options_and_budget():
    seen: list = []
    items = {"a0": _item(), "a1": _item("https://x.test/b")}

    def responder(prompt):
        if "<platform>" in prompt:
            return _Msg({"items": [_row("a0"), _row("a1", impact=9), _row("ghost")]}, 0.2)
        return _Msg({"items": [{"id": "a0", "relevant": True, "why": "w"},
                               {"id": "a1", "relevant": True, "why": "w"}]}, 0.2)
    f = _factory(responder, seen)
    budget = scoring.Budget(1.0)
    rel, _ = asyncio.run(scoring.relevance_pass(f, Path("/x"), "m", "P", items, budget))
    good, st = asyncio.run(scoring.scoring_pass(f, Path("/x"), "m", "P", "plat", [], rel, ["mctl-api"], budget))
    assert set(good) == {"a0"} and st.dropped == 1
    assert all(o.tools == [] and o.mcp_servers == {} and o.output_format for o in seen)
    # budget exhaustion
    spent = scoring.Budget(1.0, 1.0)
    _, st2 = asyncio.run(scoring.relevance_pass(f, Path("/x"), "m", "P", items, spent))
    assert st2.budget_skipped == 2


def test_rank_and_adds_to():
    items = {"a0": _item(weight=1.0), "a1": _item("https://x.test/b", weight=2.0)}
    scored = {"a0": scoring.validate_row(_row("a0", related=[".github#1"], novelty=1), {"a0"}, ["mctl-api"]),
              "a1": scoring.validate_row(_row("a1"), {"a1"}, ["mctl-api"])}
    w = {"impact": 1.0, "novelty": 1.0, "urgency": 1.0, "cost": 1.0}
    ranked = scoring.rank(scored, items, w, 10)
    assert ranked[0].id == "a1" and ranked[0].score == 18.0
    assert ranked[1].adds_to is True


def _scored(**kw):
    base = dict(id="a0", title="T", link="https://x.test/a", source="s", repo="mctl-api", score=5.0,
                question="q?", outcomes="o", pitch_ru="p", why_en="w", related=[])
    return render.Scored(**{**base, **kw})


def test_draft_url_short_and_long():
    url, full = render.draft_url(_scored(), "run1")
    sp = urlsplit(url)
    assert sp.path == "/mctlhq/mctl-api/issues/new" and full is None
    q = parse_qs(sp.query)
    assert q["labels"] == ["spike:scout"] and "Lifecycle: proposed" in q["body"][0] and "run1" in q["body"][0]
    url2, full2 = render.draft_url(_scored(why_en="w" * 190, question="q" * 240, outcomes="o" * 60,
                                           link="https://x.test/" + "a" * 1500), "run1")
    assert len(url2) <= render.MAX_BUTTON_URL_CHARS and full2 and "Lifecycle: proposed" in full2


def test_render_escapes_and_caps():
    ok = feeds.SourceResult("s", "ok", items=[])
    items = [_scored(id=f"a{i}", title="<a href=x>&</a>" + "t" * 200, pitch_ru="p" * 160) for i in range(10)]
    text, markup, _ = render.render_digest(items, [ok], render.Counts(), "r")
    assert len(text) <= 4096 and "<a href=x>" not in text and "&lt;a href=x&gt;" in text
    assert len(markup["inline_keyboard"]) >= 1


def test_overflow_drafts_only_for_kept_items():
    ok = feeds.SourceResult("s", "ok", items=[])
    items = [_scored(id=f"a{i}", pitch_ru="p" * 400) for i in range(9)]
    items.append(_scored(id="a9", link="https://x.test/" + "a" * 1500, why_en="w" * 190, question="q" * 240))
    _text, markup, overflow = render.render_digest(items, [ok], render.Counts(), "r")
    assert len(markup["inline_keyboard"]) < 10
    assert overflow == []


def test_render_variants():
    bad = [feeds.SourceResult("s", "failed", "timeout")]
    text, _m, _ = render.render_digest([], bad, render.Counts(), "r")
    assert "all sources failed" in text and "timeout" in text and "nothing today" not in text
    ok = [feeds.SourceResult("s", "ok", items=[])]
    text, _, _ = render.render_digest([], ok, render.Counts(), "r")
    assert "no relevant items were found" in text


def test_send_message():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert telegram.send_message("TOK", "42", "hi", {"inline_keyboard": []}, client=c) == 7
    assert calls[0].url.path.endswith("/sendMessage")
    assert json.loads(calls[0].content)["chat_id"] == "42"
    bad = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"ok": False, "description": "bad TOK"})))
    with pytest.raises(telegram.TelegramError) as ei:
        telegram.send_message("TOK", "42", "hi", client=bad)
    assert "TOK" not in str(ei.value)


FORBIDDEN = ("getUpdates", "deleteWebhook", "setWebhook", "gh issue", "gh pr", "-X POST", "-X PATCH", "git push")


def test_source_scan():
    root = Path(run_scout.__file__).parent
    files = [root / "run_scout.py", *sorted((root / "scout").glob("*.py"))]
    for f in files:
        src = f.read_text()
        for bad in FORBIDDEN:
            assert bad not in src, (f, bad)
        assert "POST" not in re.sub(r"\bhttp\.post|\.post\(", "", src) or f.name == "telegram.py"


def _env_feed(monkeypatch, tmp_path):
    monkeypatch.setattr(dedupe, "scout_issue_urls", lambda d: set())
    cfg = tmp_path / "s.yaml"
    cfg.write_text("window_hours: 26\ndedupe_days: 90\nranking: {top_n: 10, weights: {impact: 1, novelty: 1, "
                   "urgency: 1, cost: 1}}\nsources:\n  - {name: s, url: 'https://x.test/f'}\n")
    return cfg


def test_run_secrets_and_dry_run(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SCOUT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SCOUT_TELEGRAM_CHAT_ID", raising=False)
    cfg = _env_feed(monkeypatch, tmp_path)

    def no_fetch(request):
        raise AssertionError("fetched before secret check")
    assert asyncio.run(run_scout.run_scout(False, None, cfg, http_transport=httpx.MockTransport(no_fetch))) == 1
    date = NOW.strftime("%a, %d %b %Y %H:%M:%S GMT")
    tr = httpx.MockTransport(lambda r: httpx.Response(200, text=RSS.format(date=date)))

    def responder(prompt):
        if "<platform>" in prompt:
            return _Msg({"items": [_row("a0")]}, 0.1)
        return _Msg({"items": [{"id": "a0", "relevant": True, "why": "w"}]}, 0.1)
    monkeypatch.setattr(run_scout.backlog_mod, "collect_backlog", lambda: [])
    seen: list = []
    rc = asyncio.run(run_scout.run_scout(True, None, cfg, client_factory=_factory(responder, seen),
                                         http_transport=tr, now=NOW))
    out = capsys.readouterr().out
    assert rc == 0 and "fetched 1" in out and "https://github.com/mctlhq/mctl-api/issues/new" in out


def test_run_all_failed_exits_1(monkeypatch, tmp_path):
    cfg = _env_feed(monkeypatch, tmp_path)
    monkeypatch.setenv("SCOUT_TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("SCOUT_TELEGRAM_CHAT_ID", "1")
    sent = []
    monkeypatch.setattr(run_scout, "send_message", lambda *a, **k: sent.append(a[2]) or 1)
    tr = httpx.MockTransport(lambda r: httpx.Response(503))
    rc = asyncio.run(run_scout.run_scout(False, None, cfg, client_factory=_factory(None, []),
                                         http_transport=tr, now=NOW))
    assert rc == 1 and "all sources failed" in sent[0]


def test_session_failure_still_renders_digest(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SCOUT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SCOUT_TELEGRAM_CHAT_ID", raising=False)
    cfg = _env_feed(monkeypatch, tmp_path)
    date = NOW.strftime("%a, %d %b %Y %H:%M:%S GMT")
    tr = httpx.MockTransport(lambda r: httpx.Response(200, text=RSS.format(date=date)))

    @asynccontextmanager
    async def boom(options):
        raise RuntimeError("cli died")
        yield  # pragma: no cover

    monkeypatch.setattr(run_scout.backlog_mod, "collect_backlog", lambda: [])
    rc = asyncio.run(run_scout.run_scout(True, None, cfg, client_factory=boom, http_transport=tr, now=NOW))
    cap = capsys.readouterr()
    assert rc == 0 and "RuntimeError" in cap.err and "fetched 1" in cap.out
