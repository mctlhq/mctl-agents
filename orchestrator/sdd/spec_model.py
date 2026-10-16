"""Capability spec and delta parsing/rendering.

Target: OpenSpec v1.14.2 / f80d860. The markdown grammar below is ASSUMED, not
verified against f80d860 (the pinned reference does not cover it); it is
isolated here so a verified grammar can replace it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

GRAMMAR_VERIFIED = False

_REQ = re.compile(r"^### Requirement:\s*(.+?)\s*$")
_SCEN = re.compile(r"^#### Scenario:\s*(.+?)\s*$")
_OP = re.compile(r"^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements\s*$")
_FROM = re.compile(r"^\s*-?\s*FROM:\s*`?### Requirement:\s*(.+?)`?\s*$")
_TO = re.compile(r"^\s*-?\s*TO:\s*`?### Requirement:\s*(.+?)`?\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


class GrammarError(ValueError):
    """Raised for malformed spec or delta markdown."""


def normalize_name(name: str) -> str:
    """Casefold + whitespace collapse, used for near-miss detection."""
    return " ".join(name.split()).casefold()


def normalize_content(text: str) -> str:
    return "\n".join(" ".join(ln.split()) for ln in text.strip().splitlines() if ln.strip())


@dataclass(frozen=True)
class Requirement:
    name: str
    text: str  # header line + body, no trailing blank lines

    @property
    def scenarios(self) -> list[str]:
        out = []
        for ln in self.text.splitlines():
            m = _SCEN.match(ln)
            if m:
                out.append(normalize_name(m.group(1)))
        return out

    def renamed(self, new_name: str) -> Requirement:
        lines = self.text.splitlines()
        lines[0] = f"### Requirement: {new_name}"
        return Requirement(new_name, "\n".join(lines))


@dataclass
class Spec:
    preamble: str
    requirements: list[Requirement] = field(default_factory=list)

    def duplicates(self) -> list[str]:
        seen: set[str] = set()
        dup: list[str] = []
        for r in self.requirements:
            k = normalize_name(r.name)
            if k in seen:
                dup.append(r.name)
            seen.add(k)
        return dup


def _split_blocks(lines: list[str]) -> tuple[list[str], list[Requirement]]:
    pre: list[str] = []
    blocks: list[list[str]] = []
    in_fence = False
    for ln in lines:
        if _FENCE.match(ln):
            in_fence = not in_fence
        if not in_fence and _REQ.match(ln):
            blocks.append([ln])
        elif blocks:
            blocks[-1].append(ln)
        else:
            pre.append(ln)
    reqs = []
    for b in blocks:
        m = _REQ.match(b[0])
        if m is None:  # pragma: no cover - block always starts with a header
            raise GrammarError("malformed requirement block")
        reqs.append(Requirement(m.group(1), "\n".join(b).rstrip()))
    return pre, reqs


def parse_spec(text: str) -> Spec:
    pre, reqs = _split_blocks(text.splitlines())
    return Spec("\n".join(pre).rstrip(), reqs)


def render_spec(spec: Spec) -> str:
    parts = [spec.preamble.rstrip()] if spec.preamble.strip() else []
    parts += [r.text for r in spec.requirements]
    return "\n\n".join(parts) + "\n"


@dataclass
class Delta:
    added: list[Requirement] = field(default_factory=list)
    modified: list[Requirement] = field(default_factory=list)
    removed: list[Requirement] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)


def parse_delta(text: str) -> Delta:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for ln in text.splitlines():
        if _FENCE.match(ln):
            in_fence = not in_fence
        m = None if in_fence else _OP.match(ln)
        if m:
            current = m.group(1)
            if current in sections:
                raise GrammarError(f"duplicate '## {current} Requirements' section")
            sections[current] = []
        elif not in_fence and ln.startswith("## "):
            raise GrammarError(f"unknown delta section: {ln!r}")
        elif current is not None:
            sections[current].append(ln)
    d = Delta()
    for op, lines in sections.items():
        if op == "RENAMED":
            pending: str | None = None
            for ln in lines:
                mf, mt = _FROM.match(ln), _TO.match(ln)
                if mf:
                    if pending is not None:
                        raise GrammarError("RENAMED FROM without TO")
                    pending = mf.group(1)
                elif mt:
                    if pending is None:
                        raise GrammarError("RENAMED TO without FROM")
                    d.renamed.append((pending, mt.group(1)))
                    pending = None
            if pending is not None:
                raise GrammarError("RENAMED FROM without TO")
            continue
        _, reqs = _split_blocks(lines)
        {"ADDED": d.added, "MODIFIED": d.modified, "REMOVED": d.removed}[op].extend(reqs)
    return d
