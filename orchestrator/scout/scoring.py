"""Relevance and scoring passes (tool-free SDK sessions) plus ranking."""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

from orchestrator.options import build_scout_options
from orchestrator.scout.feeds import Item
from orchestrator.scout.render import Scored

RELEVANCE_BATCH = 60
SCORING_BATCH = 15
FALLBACK_REPO = ".github"

RELEVANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "relevant": {"type": "boolean"},
            "why": {"type": "string", "maxLength": 200},
        },
        "required": ["id", "relevant", "why"],
    }}},
    "required": ["items"],
}


def scoring_schema(repos: list[str]) -> dict[str, Any]:
    score = {"type": "integer", "minimum": 0, "maximum": 5}
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "impact": score, "novelty": score, "urgency": score, "cost": score,
                "repo": {"type": "string", "enum": repos},
                "related": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "question": {"type": "string", "maxLength": 240},
                "outcomes": {"type": "string", "maxLength": 60},
                "pitch_ru": {"type": "string", "maxLength": 160},
                "why_en": {"type": "string", "maxLength": 200},
            },
            "required": ["id", "impact", "novelty", "urgency", "cost", "repo", "related",
                         "question", "outcomes", "pitch_ru", "why_en"],
        }}},
        "required": ["items"],
    }


ClientFactory = Callable[[ClaudeAgentOptions], AbstractAsyncContextManager[Any]]

_RELATED_RE = re.compile(r"^(mctlhq/)?[\w.-]+#\d+$")


@dataclass
class Budget:
    limit: float
    spent: float = 0.0

    @property
    def remaining(self) -> float:
        return self.limit - self.spent

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


@dataclass
class PassResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0
    budget_skipped: int = 0


async def _session(
    factory: ClientFactory,
    agent_dir: Path,
    model: str,
    schema: dict[str, Any],
    prompt: str,
    budget: Budget,
) -> list[dict[str, Any]] | None:
    """One structured-output session. Returns items list or None on failure."""
    options = build_scout_options(agent_dir, model, schema)
    options.max_budget_usd = min(options.max_budget_usd or budget.remaining, budget.remaining)
    try:
        async with factory(options) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage) or type(msg).__name__ == "ResultMessage":
                    budget.spent += float(getattr(msg, "total_cost_usd", 0) or 0)
                    out = getattr(msg, "structured_output", None)
                    if isinstance(out, dict) and isinstance(out.get("items"), list):
                        return [r for r in out["items"] if isinstance(r, dict)]
                    return None
    except Exception as exc:  # noqa: BLE001 - one failed batch must not abort the digest run
        print(f"scout: session failed: {type(exc).__name__}", file=sys.stderr)
        return None
    return None


def _wrap(prompt: str, payload: Any, extra: str = "") -> str:
    return f"{prompt}\n\n{extra}<untrusted_articles>\n{json.dumps(payload, ensure_ascii=False)}\n</untrusted_articles>"


async def relevance_pass(
    factory: ClientFactory, agent_dir: Path, model: str, prompt: str,
    items: dict[str, Item], budget: Budget,
) -> tuple[dict[str, Item], PassResult]:
    """Return (relevant items by id, stats)."""
    ids = list(items)
    result = PassResult()
    relevant: dict[str, Item] = {}
    for i in range(0, len(ids), RELEVANCE_BATCH):
        batch = ids[i:i + RELEVANCE_BATCH]
        if budget.exhausted:
            result.budget_skipped += len(batch)
            continue
        payload = [{"id": k, "title": items[k].title, "source": items[k].source, "summary": items[k].summary}
                   for k in batch]
        rows = await _session(factory, agent_dir, model, RELEVANCE_SCHEMA, _wrap(prompt, payload), budget)
        seen: set[str] = set()
        for r in rows or []:
            rid = r.get("id")
            if rid in batch and rid not in seen and isinstance(r.get("relevant"), bool):
                seen.add(str(rid))
                if r["relevant"]:
                    relevant[str(rid)] = items[str(rid)]
        result.dropped += len(batch) - len(seen)
    return relevant, result


def _int05(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 5:
        return None
    return v


def validate_row(row: dict[str, Any], sent: set[str], repos: list[str]) -> dict[str, Any] | None:
    """Re-validate one scoring row in Python; None means drop as unscored."""
    if row.get("id") not in sent:
        return None
    nums = {k: _int05(row.get(k)) for k in ("impact", "novelty", "urgency", "cost")}
    if any(v is None for v in nums.values()):
        return None
    texts = {}
    for k, lim in (("question", 240), ("outcomes", 60), ("pitch_ru", 160), ("why_en", 200)):
        v = row.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        texts[k] = v.strip()[:lim]
    repo = row.get("repo")
    if repo not in repos:
        repo = FALLBACK_REPO
    related = [r for r in (row.get("related") or []) if isinstance(r, str) and _RELATED_RE.match(r)][:3]
    return {"id": row["id"], **nums, **texts, "repo": repo, "related": related}


async def scoring_pass(
    factory: ClientFactory, agent_dir: Path, model: str, prompt: str, platform: str,
    backlog: list[dict[str, str]], items: dict[str, Item], repos: list[str], budget: Budget,
) -> tuple[dict[str, dict[str, Any]], PassResult]:
    ids = list(items)
    result = PassResult()
    good: dict[str, dict[str, Any]] = {}
    schema = scoring_schema(repos)
    extra = f"<platform>\n{platform}\n</platform>\n<backlog>\n{json.dumps(backlog)}\n</backlog>\n"
    for i in range(0, len(ids), SCORING_BATCH):
        batch = ids[i:i + SCORING_BATCH]
        if budget.exhausted:
            result.budget_skipped += len(batch)
            continue
        payload = [{"id": k, "title": items[k].title, "source": items[k].source, "summary": items[k].summary}
                   for k in batch]
        rows = await _session(factory, agent_dir, model, schema, _wrap(prompt, payload, extra), budget)
        ok = 0
        for r in rows or []:
            v = validate_row(r, set(batch), repos)
            if v and v["id"] not in good:
                good[v["id"]] = v
                ok += 1
        result.dropped += len(batch) - ok
    return good, result


def rank(
    scored: dict[str, dict[str, Any]], items: dict[str, Item],
    weights: dict[str, float], top_n: int,
) -> list[Scored]:
    out: list[tuple[tuple[float, int, float], Scored]] = []
    for k, v in scored.items():
        it = items[k]
        total = sum(weights[d] * v[d] for d in ("impact", "novelty", "urgency", "cost")) * it.weight
        ts = it.published.timestamp() if it.published else 0.0
        out.append(((total, v["urgency"], ts), Scored(
            id=k, title=it.title, link=it.link, source=it.source, repo=v["repo"], score=total,
            question=v["question"], outcomes=v["outcomes"], pitch_ru=v["pitch_ru"], why_en=v["why_en"],
            related=v["related"], adds_to=bool(v["related"]) and v["novelty"] <= 1, urgency=v["urgency"],
        )))
    out.sort(key=lambda t: t[0], reverse=True)
    return [s for _, s in out[:top_n]]
