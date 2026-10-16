"""Triplet <-> OpenSpec-style change package conversion (spike, one-way safe).

Target: OpenSpec v1.14.2 / f80d860. Only planning content is converted. The
``.status.yaml`` ``source`` block maps to ``x-mctl-*`` keys; every other
``.status.yaml`` field stays MCTL execution state and is never copied.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from orchestrator.sdd.metadata import validate_mapping
from orchestrator.sdd.spec_model import parse_delta

TRIPLET = ("requirements.md", "design.md", "tasks.md")
SCHEMA_NAME = "mctl-devloop"
_AC_PREFIX = "## Acceptance criteria"
_FENCE = re.compile(r"^\s*(```|~~~)")
_EARS = re.compile(r"^(WHEN|IF|WHILE)\s+(.*?)\s+(?:THEN\s+)?THE SYSTEM SHALL\s+(.*)$", re.DOTALL)
_GROUP = re.compile(r"^_Group: (.*)_$")


def _split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """(raw text before first '## ', [(heading_line, raw_chunk_incl_heading)])."""
    head: list[str] = []
    chunks: list[list[str]] = []
    in_fence = False
    for ln in text.splitlines(keepends=True):
        if _FENCE.match(ln):
            in_fence = not in_fence
        if not in_fence and ln.startswith("## "):
            chunks.append([ln])
        elif chunks:
            chunks[-1].append(ln)
        else:
            head.append(ln)
    return "".join(head), [(c[0].rstrip("\n"), "".join(c)) for c in chunks]


def _parse_ac(chunk: str) -> list[tuple[str | None, list[str]]]:
    items: list[tuple[str | None, list[str]]] = []
    group: str | None = None
    for ln in chunk.splitlines()[1:]:
        if not ln.strip():
            continue
        if ln.startswith("- "):
            items.append((group, [ln[2:]]))  # a label covers every bullet until the next label
        elif ln[0] in " \t" and items:
            items[-1][1].append(ln)
        else:
            group = ln.strip()
    return items


def _requirement_block(n: int, group: str | None, lines: list[str]) -> str:
    first = lines[0]
    words = re.sub(r"[`*#:]", "", first).split()
    name = f"AC-{n:02d} " + " ".join(words[:8])
    out = [f"### Requirement: {name}"]
    if group:
        out.append(f"_Group: {group}_")
    out += lines
    m = _EARS.match(" ".join(x.strip() for x in lines))
    scen = [f"#### Scenario: AC-{n:02d}"]
    if m:
        scen += [f"- **WHEN** {m.group(2)}", f"- **THEN** {m.group(3)}"]
    else:
        scen += [f"- **THEN** {first}"]
    return "\n".join(out) + "\n\n" + "\n".join(scen)


class ConversionError(ValueError):
    """Malformed input that conversion refuses to drop silently."""


@dataclass
class ChangePackage:
    capability: str
    files: dict[str, str]


def triplet_to_change(
    triplet: Mapping[str, str],
    source_info: Mapping[str, Any],
    *,
    capability: str,
    created: str,
) -> ChangePackage:
    req = triplet["requirements.md"]
    head, sections = _split_sections(req)
    title = head.splitlines()[0].lstrip("# ").strip() if head.strip() else ""
    prop = head
    ac_chunk = ""
    ac_heading = ""
    after = ""
    prev = ""
    for heading, chunk in sections:
        if heading.lower().startswith(_AC_PREFIX.lower()) and not ac_chunk:
            ac_chunk, ac_heading, after = chunk, heading, prev
        else:
            prop += chunk
            prev = heading
    items = _parse_ac(ac_chunk)
    if ac_chunk.strip() and not items:
        raise ConversionError("acceptance criteria section has no '- ' bullet items; refusing to drop it silently")
    blocks = [_requirement_block(i, g, ls) for i, (g, ls) in enumerate(items, 1)]
    spec = "## ADDED Requirements\n\n" + "\n\n".join(blocks) + "\n" if blocks else ""
    src = (source_info.get("source") or {})
    meta: dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "created": created,
        "goal": title,
        "affected_areas": [source_info["service"]],
        "x-mctl-issue": src.get("url"),
        "x-mctl-repo": src.get("repo"),
        "x-mctl-issue-number": src.get("issue"),
        "x-mctl-service": source_info["service"],
        "x-mctl-slug": source_info["slug"],
        "x-mctl-acceptance-heading": ac_heading,
        "x-mctl-acceptance-after": after,
    }
    meta = {k: v for k, v in meta.items() if v is not None}
    validate_mapping(meta, strict=True)
    files = {
        ".openspec.yaml": yaml.safe_dump(meta, sort_keys=False, allow_unicode=True),
        "proposal.md": prop,
        "design.md": triplet["design.md"],
        "tasks.md": triplet["tasks.md"],
    }
    if spec:
        files[f"specs/{capability}/spec.md"] = spec
    return ChangePackage(capability, files)


def change_to_triplet(pkg: ChangePackage) -> tuple[dict[str, str], dict[str, Any]]:
    meta = yaml.safe_load(pkg.files[".openspec.yaml"])
    head, sections = _split_sections(pkg.files["proposal.md"])
    spec = pkg.files.get(f"specs/{pkg.capability}/spec.md", "")
    out_lines: list[str] = []
    prev_group: str | None = None
    for r in parse_delta(spec).added if spec else []:
        body = r.text.split("\n\n#### Scenario:")[0].splitlines()[1:]
        group = None
        if body and (m := _GROUP.match(body[0])):
            group, body = m.group(1), body[1:]
        if group and group != prev_group:
            if out_lines:
                out_lines.append("")
            out_lines.append(group)
            out_lines.append("")
        out_lines.append("- " + "\n".join(body))
        prev_group = group
    ac = ""
    if out_lines:
        ac = meta.get("x-mctl-acceptance-heading", "## Acceptance criteria (EARS)") + "\n\n"
        ac += "\n".join(out_lines) + "\n"
    after = meta.get("x-mctl-acceptance-after", "")
    text = head
    placed = not ac
    if not placed and not after:
        text, placed = text.rstrip("\n") + "\n\n" + ac + "\n", True
    for heading, chunk in sections:
        text += chunk
        if not placed and heading == after:
            text = text.rstrip("\n") + "\n\n" + ac
            text += "\n" if heading != sections[-1][0] else ""
            placed = True
    if not placed:
        text = text.rstrip("\n") + "\n\n" + ac
    source = {
        "service": meta.get("x-mctl-service"),
        "slug": meta.get("x-mctl-slug"),
        "source": {
            "type": "github_issue",
            "repo": meta.get("x-mctl-repo"),
            "issue": meta.get("x-mctl-issue-number"),
            "url": meta.get("x-mctl-issue"),
        },
    }
    triplet = {
        "requirements.md": text,
        # A `trivial` package (skip_specs) has no design.md; the triplet keeps an empty one.
        "design.md": pkg.files.get("design.md", ""),
        "tasks.md": pkg.files["tasks.md"],
    }
    return triplet, source


@dataclass(frozen=True)
class LossEntry:
    file: str
    line: int
    kind: str  # heading | bullet | checkbox | source
    text: str
    target: str | None  # package file holding it; None = unmapped (loss)


_BULLET = re.compile(r"^\s*(?:- |\d+\.\s)")
_TARGETS = {
    "requirements.md": (".openspec.yaml", "proposal.md"),
    "design.md": ("design.md",),
    "tasks.md": ("tasks.md",),
}


def loss_report(triplet: Mapping[str, str], source_info: Mapping[str, Any], pkg: ChangePackage) -> list[LossEntry]:
    """Locate every heading/bullet/checkbox/source value in the package."""
    out: list[LossEntry] = []
    spec_files = tuple(k for k in pkg.files if k.startswith("specs/"))
    for name in TRIPLET:
        search = _TARGETS[name] + (spec_files if name == "requirements.md" else ())
        for n, ln in enumerate(triplet[name].splitlines(), 1):
            if not ln.strip():
                continue
            if ln.startswith("#"):
                kind, needle = "heading", ln.strip()
            elif _BULLET.match(ln):
                kind = "checkbox" if "- [" in ln[:6] else "bullet"
                needle = _BULLET.sub("", ln, count=1).strip()
            else:
                continue
            target = next((f for f in search if needle in pkg.files[f]), None)
            out.append(LossEntry(name, n, kind, ln.strip(), target))
    flat = {
        "source.url": (source_info.get("source") or {}).get("url"),
        "source.repo": (source_info.get("source") or {}).get("repo"),
        "source.issue": (source_info.get("source") or {}).get("issue"),
        "service": source_info.get("service"),
        "slug": source_info.get("slug"),
    }
    for key, val in flat.items():
        if val is None:
            continue
        hit = str(val) in pkg.files[".openspec.yaml"]
        out.append(LossEntry(".status.yaml", 0, "source", f"{key}={val}", ".openspec.yaml" if hit else None))
    return out
