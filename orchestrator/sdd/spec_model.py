"""Capability spec and delta parsing/rendering.

Verified against OpenSpec v1.14.2 / f80d860 (mctl-agents#659). Each rule cites
the upstream function it ports; ``docs/spikes/659-openspec-grammar.md`` holds
the full comparison. Where MCTL deliberately differs, the difference is a
named entry in ``PROFILE`` and has its own test; anything not listed there is
meant to behave exactly as upstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Intentional MCTL deviations from upstream. Keys are cited by tests and docs.
PROFILE: dict[str, str] = {
    "no-retirement": (
        "retire_capabilities: true is refused; an emptied capability fails the "
        "rebuilt-spec check exactly as it does upstream without the marker"
    ),
    "validator-subset": (
        "only the validator rules that block archive are modelled (ERRORs), plus "
        "the SHALL/MUST WARNING; advisory warnings (Purpose length, text length, "
        "unapplied prose, proposal Why/What Changes) are not"
    ),
    "purpose-level": "a spec's Purpose is read from a '## Purpose' section only, not from any header level",
    "lf-output": "rebuilt specs are written with LF line endings; upstream keeps an existing CRLF file CRLF",
    "sync-mode": (
        "apply_change(mode='sync') mirrors upstream's agent workflow template "
        "(ADDED on an existing requirement acts as MODIFIED), not any CLI command"
    ),
}

# RB = src/core/parsers/requirement-blocks.ts, RT = requirement-text.ts,
# CF = code-fence.ts, SS = spec-structure.ts, SA = src/core/specs-apply.ts.
_REQUIREMENT_HEADER = re.compile(r"^###\s*Requirement:\s*(.+)\s*$", re.IGNORECASE)  # RB L38
_REQUIREMENTS_SECTION = re.compile(r"^##\s+Requirements\s*$", re.IGNORECASE)  # RB L47
_TOP_LEVEL = re.compile(r"^##\s+")
_SECTION = re.compile(r"^(##)\s+(.+)$")  # RB:splitTopLevelSections
_CLOSING_RUN = re.compile(r"[ \t]+#+[ \t]*$")
_FROM = re.compile(r"^\s*[-*+]?\s*FROM:\s*`?###\s*Requirement:\s*(.+?)`?\s*$")  # RB L561
_TO = re.compile(r"^\s*[-*+]?\s*TO:\s*`?###\s*Requirement:\s*(.+?)`?\s*$")  # RB L562
_REMOVED_BULLET = re.compile(r"^\s*[-*+]\s*`?###\s*Requirement:\s*(.+?)`?\s*$")  # RB L515
_SCENARIO_HEADER = re.compile(r"^####\s+")  # RT L31: ANY level-4 header
_SCENARIO_BODY_END = re.compile(r"^#{1,4}\s")
_HEADER_LINE = re.compile(r"^#{1,6}\s")
_METADATA_LINE = re.compile(r"^\*\*[^*]+\*\*:")
_SHALL_OR_MUST = re.compile(r"\b(SHALL|MUST)\b")
_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
# SS: the structure guard requires a space after ### (unlike RB).
_SS_REQUIREMENT = re.compile(r"^###\s+Requirement:\s*(.+)\s*$", re.IGNORECASE)
_SS_DELTA = re.compile(r"^##\s+(ADDED|MODIFIED|REMOVED|RENAMED)\s+Requirements\s*$", re.IGNORECASE)
_PURPOSE = re.compile(r"^##\s+Purpose\s*$", re.IGNORECASE)

DELTA_SECTIONS = ("added requirements", "modified requirements", "removed requirements", "renamed requirements")


class GrammarError(ValueError):
    """Raised for malformed spec or delta markdown."""


def normalize_text(text: str) -> str:
    """Strip a UTF-8 BOM and normalise CRLF/CR to LF (RB:normalizeLineEndings)."""
    return re.sub(r"\r\n?", "\n", text.removeprefix("﻿"))


def fence_mask(lines: list[str]) -> list[bool]:
    """True for every line inside a fenced block, fences included (CF:buildCodeFenceMask)."""
    mask = [False] * len(lines)
    active: tuple[str, int] | None = None
    for i, line in enumerate(lines):
        if active is None:
            m = _FENCE_OPEN.match(line)
            if m:
                active = (m.group(1)[0], len(m.group(1)))
                mask[i] = True
            continue
        mask[i] = True
        m = _FENCE_CLOSE.match(line)
        if m and m.group(1)[0] == active[0] and len(m.group(1)) >= active[1]:
            active = None
    return mask


def normalize_requirement_name(name: str) -> str:
    """Drop a CommonMark closing '#' run, then trim. Matching is case-sensitive."""
    return _CLOSING_RUN.sub("", name).strip()


def fold_requirement_name(name: str) -> str:
    """Lowercase + whitespace collapse; used only to detect near-miss typos."""
    return re.sub(r"\s+", " ", normalize_requirement_name(name).lower())


def normalize_block(raw: str) -> str:
    """Identity for 'identical content' checks (SA:normalizeBlockRaw)."""
    return normalize_text(raw).strip()


def scenario_name(header_line: str) -> str:
    text = _SCENARIO_HEADER.sub("", header_line, count=1)
    text = _CLOSING_RUN.sub("", text)
    return re.sub(r"^Scenario:\s*", "", text, flags=re.IGNORECASE).strip()


@dataclass(frozen=True)
class Requirement:
    name: str
    text: str  # the raw block: header line + body, trailing whitespace trimmed

    @property
    def body_lines(self) -> list[str]:
        return self.text.split("\n")[1:]

    @property
    def scenarios(self) -> list[str]:
        """Every non-fenced '#### ' header, in order, duplicates kept (RB:parseScenarioBlocks)."""
        lines = self.text.split("\n")
        mask = fence_mask(lines)
        return [scenario_name(ln) for i, ln in enumerate(lines) if not mask[i] and _SCENARIO_HEADER.match(ln)]

    @property
    def statement(self) -> str:
        """The requirement body before its first header (RT:extractRequirementBody)."""
        lines = self.body_lines
        mask = fence_mask(lines)
        captured: list[str] = []
        metadata: list[str] = []
        for i, line in enumerate(lines):
            if mask[i]:
                continue
            if _HEADER_LINE.match(line):
                break
            stripped = line.strip()
            if not stripped:
                continue
            (metadata if _METADATA_LINE.match(stripped) else captured).append(stripped)
        return "\n".join(captured or metadata)

    def scenario_count(self) -> int:
        """Scenarios whose body has content (RT:countScenarios)."""
        lines = self.body_lines
        mask = fence_mask(lines)
        count = 0
        for i, line in enumerate(lines):
            if mask[i] or not _SCENARIO_HEADER.match(line):
                continue
            end = i + 1
            while end < len(lines) and (mask[end] or not _SCENARIO_BODY_END.match(lines[end])):
                end += 1
            if "\n".join(lines[i + 1 : end]).strip():
                count += 1
        return count

    def renamed(self, new_name: str) -> Requirement:
        lines = self.text.split("\n")
        lines[0] = f"### Requirement: {new_name}"
        return Requirement(new_name, "\n".join(lines))


def has_shall_or_must(text: str) -> bool:
    return _SHALL_OR_MUST.search(text) is not None


def _blocks(lines: list[str], mask: list[bool]) -> tuple[list[str], list[Requirement]]:
    """(preamble lines, requirement blocks); a block ends at the next requirement or '## '."""

    def is_req(i: int) -> bool:
        return not mask[i] and _REQUIREMENT_HEADER.match(lines[i]) is not None

    i = 0
    preamble: list[str] = []
    while i < len(lines) and not is_req(i):
        preamble.append(lines[i])
        i += 1
    reqs: list[Requirement] = []
    while i < len(lines):
        if not is_req(i):
            i += 1
            continue
        m = _REQUIREMENT_HEADER.match(lines[i])
        if m is None:  # pragma: no cover - is_req matched this line
            raise GrammarError("malformed requirement block")
        buf = [lines[i]]
        i += 1
        while i < len(lines) and not is_req(i) and not (not mask[i] and _TOP_LEVEL.match(lines[i])):
            buf.append(lines[i])
            i += 1
        reqs.append(Requirement(normalize_requirement_name(m.group(1)), "\n".join(buf).rstrip()))
    return preamble, reqs


@dataclass
class Spec:
    """A main spec split around its '## Requirements' section (RB:extractRequirementsSection)."""

    before: str
    header_line: str
    preamble: str
    requirements: list[Requirement] = field(default_factory=list)
    after: str = "\n"


def parse_spec(text: str) -> Spec:
    lines = normalize_text(text).split("\n")
    mask = fence_mask(lines)
    start = next((i for i, ln in enumerate(lines) if not mask[i] and _REQUIREMENTS_SECTION.match(ln)), -1)
    if start == -1:
        before = "\n".join(lines).rstrip()
        return Spec(before + "\n\n" if before else "", "## Requirements", "", [], "\n")
    end = next(
        (i for i in range(start + 1, len(lines)) if not mask[i] and _TOP_LEVEL.match(lines[i])),
        len(lines),
    )
    preamble, reqs = _blocks(lines[start + 1 : end], mask[start + 1 : end])
    before = "\n".join(lines[:start])
    after = "\n".join(lines[end:])
    return Spec(
        before + "\n" if before.rstrip() else before,
        lines[start],
        "\n".join(preamble).rstrip(),
        reqs,
        after if after.startswith("\n") else "\n" + after,
    )


def has_requirements_section(text: str) -> bool:
    lines = normalize_text(text).split("\n")
    mask = fence_mask(lines)
    return any(not mask[i] and _REQUIREMENTS_SECTION.match(ln) for i, ln in enumerate(lines))


def _collapse_blank_runs(text: str) -> str:
    lines = text.split("\n")
    mask = fence_mask(lines)
    kept: list[str] = []
    blank = 0
    for i, line in enumerate(lines):
        if not mask[i] and line == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        kept.append(line)
    return "\n".join(kept)


def render_spec(spec: Spec) -> str:
    """Rebuild a spec the way SA:buildUpdatedSpec does."""
    body = "\n\n".join(
        ([spec.preamble.rstrip()] if spec.preamble.strip() else []) + [r.text for r in spec.requirements]
    ).rstrip()
    parts = [spec.before.rstrip(), spec.header_line, body, spec.after.strip()]
    return _collapse_blank_runs("\n\n".join(p for p in parts if p != "")).rstrip() + "\n"


def structure_issues(text: str) -> list[str]:
    """Main-spec structure errors that make archive refuse (SS:findMainSpecStructureIssues)."""
    lines = normalize_text(text).split("\n")
    mask = fence_mask(lines)
    lines = ["" if mask[i] else ln for i, ln in enumerate(lines)]
    start = next((i for i, ln in enumerate(lines) if _REQUIREMENTS_SECTION.match(ln)), -1)
    end = len(lines)
    if start != -1:
        end = next((i for i in range(start + 1, len(lines)) if _TOP_LEVEL.match(lines[i])), len(lines))
    issues: list[str] = []
    seen: dict[str, int] = {}
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if _SS_DELTA.match(line):
            issues.append(f"line {i + 1}: main spec contains delta header {line.strip()!r}")
            continue
        m = _SS_REQUIREMENT.match(line)
        if not m:
            continue
        if not (start != -1 and start < i < end):
            issues.append(f"line {i + 1}: {line.strip()!r} is outside the ## Requirements section")
            continue
        name = normalize_requirement_name(m.group(1))
        if name in seen:
            issues.append(f"line {i + 1}: {line.strip()!r} duplicates the requirement on line {seen[name]}")
        else:
            seen[name] = i + 1
    return issues


def _mask_html_comments(text: str) -> str:
    """Blank out <!-- ... --> spans (an unterminated one runs to EOF), keeping newlines."""

    def blank(s: str) -> str:
        return re.sub(r"[^\n]", " ", s)

    return re.sub(r"<!--[\s\S]*?(?:--!?>|$(?![\s\S]))", lambda m: blank(m.group(0)), text)


def purpose(text: str) -> str | None:
    """The body of '## Purpose', or None if absent or empty (SA:extractPurposeSection)."""
    normalized = normalize_text(text)
    lines = normalized.split("\n")
    masked = _mask_html_comments(normalized).split("\n")
    mask = fence_mask(masked)
    start = next((i for i, ln in enumerate(masked) if not mask[i] and _PURPOSE.match(ln)), -1)
    if start == -1:
        return None
    end = next(
        (i for i in range(start + 1, len(masked)) if not mask[i] and _TOP_LEVEL.match(masked[i])),
        len(masked),
    )
    prose = "\n".join(masked[i] for i in range(start + 1, end) if not mask[i]).strip()
    if not prose:
        return None
    return "\n".join(lines[start + 1 : end]).strip() or None


@dataclass
class Delta:
    added: list[Requirement] = field(default_factory=list)
    modified: list[Requirement] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)  # delta sections present, folded
    orphaned: list[str] = field(default_factory=list)  # requirements outside every delta section
    purpose: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed or self.renamed)


def parse_delta(text: str) -> Delta:
    """Read a delta spec (RB:parseDeltaSpec). Repeated sections merge; other '## ' sections are ignored."""
    lines = normalize_text(text).split("\n")
    mask = fence_mask(lines)
    heads = [(i, m.group(2).strip()) for i, ln in enumerate(lines) if not mask[i] and (m := _SECTION.match(ln))]
    d = Delta(purpose=purpose(text))
    for n, (i, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body, body_mask = lines[i + 1 : end], mask[i + 1 : end]
        folded = title.lower()
        if folded not in DELTA_SECTIONS:
            d.orphaned += [r.name for r in _blocks(body, body_mask)[1]]
            continue
        if folded not in d.sections:
            d.sections.append(folded)
        if folded == "added requirements":
            d.added += _blocks(body, body_mask)[1]
        elif folded == "modified requirements":
            d.modified += _blocks(body, body_mask)[1]
        elif folded == "removed requirements":
            for j, line in enumerate(body):
                if body_mask[j]:
                    continue
                m = _REQUIREMENT_HEADER.match(line) or _REMOVED_BULLET.match(line)
                if m:
                    d.removed.append(normalize_requirement_name(m.group(1)))
        else:
            d.renamed += _renamed_pairs(body, body_mask, i + 2)
    first = heads[0][0] if heads else len(lines)
    d.orphaned = [r.name for r in _blocks(lines[:first], mask[:first])[1]] + d.orphaned
    return d


def _renamed_pairs(body: list[str], mask: list[bool], first_line: int) -> list[tuple[str, str]]:
    """FROM/TO pairs within one RENAMED section; any unpaired line is an error (RB:parseRenamedPairs)."""
    pairs: list[tuple[str, str]] = []
    pending: tuple[str, int] | None = None
    for j, line in enumerate(body):
        if mask[j]:
            continue
        mf, mt = _FROM.match(line), _TO.match(line)
        if mf:
            if pending is not None:
                raise GrammarError(f"RENAMED FROM on line {pending[1]} has no matching TO")
            pending = (normalize_requirement_name(mf.group(1)), first_line + j)
        elif mt:
            if pending is None:
                raise GrammarError(f"RENAMED TO on line {first_line + j} has no matching FROM")
            pairs.append((pending[0], normalize_requirement_name(mt.group(1))))
            pending = None
    if pending is not None:
        raise GrammarError(f"RENAMED FROM on line {pending[1]} has no matching TO")
    return pairs
