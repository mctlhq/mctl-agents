"""Pure delta application and tmp-dir archive, following OpenSpec's archive.

Verified against OpenSpec v1.14.2 / f80d860 (mctl-agents#659): a port of
SA:buildUpdatedSpec (src/core/specs-apply.ts) with archive's validation
before and after it (src/core/archive.ts). Order: RENAMED -> REMOVED ->
MODIFIED -> ADDED, after cross-section pre-validation. Any abort leaves the
input untouched. ``mode="sync"`` is ``PROFILE["sync-mode"]``, not CLI code.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from orchestrator.sdd.metadata import MetadataError, validate_mapping
from orchestrator.sdd.spec_model import (
    PROFILE,
    Delta,
    Requirement,
    Spec,
    fold_requirement_name,
    normalize_block,
    parse_delta,
    parse_spec,
    purpose,
    render_spec,
    structure_issues,
)
from orchestrator.sdd.validate import errors, validate_delta, validate_spec

PURPOSE_PLACEHOLDER = "TBD - created by archiving change {change}. Update Purpose after archive."
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}-")


class ApplyAbort(Exception):
    """A delta operation aborted; no output is produced."""


@dataclass
class ApplyResult:
    specs: dict[str, str]
    warnings: list[str] = field(default_factory=list)


def spec_skeleton(cap: str, change: str, delta_purpose: str | None = None) -> str:
    """SA:buildSpecSkeleton: a delta's own Purpose beats the placeholder."""
    body = (delta_purpose or "").strip() or PURPOSE_PLACEHOLDER.format(change=change)
    return f"# {cap} Specification\n\n## Purpose\n{body}\n\n## Requirements\n"


def _prevalidate(cap: str, delta: Delta) -> None:
    """SA L260-362: duplicates per section and cross-section conflicts."""
    for label, names in (
        ("ADDED", [r.name for r in delta.added]),
        ("MODIFIED", [r.name for r in delta.modified]),
        ("REMOVED", delta.removed),
        ("RENAMED FROM", [f for f, _ in delta.renamed]),
        ("RENAMED TO", [t for _, t in delta.renamed]),
    ):
        seen: set[str] = set()
        for n in names:
            if n in seen:
                raise ApplyAbort(f"{cap}: duplicate requirement in {label}: {n!r}")
            seen.add(n)
    added = {r.name for r in delta.added}
    modified = {r.name for r in delta.modified}
    removed = set(delta.removed)
    for frm, to in delta.renamed:
        if any(fold_requirement_name(r) == fold_requirement_name(frm) for r in removed):
            raise ApplyAbort(f"{cap}: RENAMED FROM {frm!r} is also REMOVED")
        if frm in modified:
            raise ApplyAbort(f"{cap}: MODIFIED must reference the new header {to!r}, not {frm!r}")
        if to in added:
            raise ApplyAbort(f"{cap}: RENAMED TO {to!r} collides with ADDED")
    for a, b, both in (
        ("MODIFIED", "REMOVED", modified & removed),
        ("MODIFIED", "ADDED", modified & added),
        ("ADDED", "REMOVED", added & removed),
    ):
        if both:
            raise ApplyAbort(f"{cap}: requirement {sorted(both)[0]!r} is in both {a} and {b}")
    if delta.is_empty:
        raise ApplyAbort(f"{cap}: delta has no operations")


def _missing_scenarios(current: Requirement, incoming: Requirement) -> list[str]:
    """Multiplicity-aware scenario loss (RB:diffScenarioNames)."""
    remaining: dict[str, int] = {}
    for name in incoming.scenarios:
        remaining[name] = remaining.get(name, 0) + 1
    missing = []
    for name in current.scenarios:
        if remaining.get(name, 0) > 0:
            remaining[name] -= 1
        else:
            missing.append(name)
    return missing


def _apply_one(cap: str, canonical: str | None, delta: Delta, mode: str, change: str, warnings: list[str]) -> str:
    _prevalidate(cap, delta)
    is_new = canonical is None
    if canonical is None:
        if delta.modified or delta.renamed:
            raise ApplyAbort(f"{cap}: new capability allows only ADDED (got MODIFIED/RENAMED)")
        if delta.removed:
            warnings.append(f"{cap}: {len(delta.removed)} REMOVED requirement(s) ignored for new spec")
        canonical = spec_skeleton(cap, change, delta.purpose)
        if delta.purpose and ("<!--" in canonical or structure_issues(canonical) or purpose(canonical) is None):
            # SA:readableOverview: a carried Purpose that would hide or break
            # the spec's own headers is replaced by the placeholder.
            canonical = spec_skeleton(cap, change)
            warnings.append(f"{cap}: delta Purpose ignored (it would leave the new spec unreadable)")
    elif delta.purpose and purpose(canonical) not in (None, delta.purpose):
        warnings.append(f"{cap}: delta Purpose ignored; the spec already has one")
    issues = structure_issues(canonical)
    if issues:
        raise ApplyAbort(f"{cap}: target spec is structurally invalid: {'; '.join(issues)}")

    spec = parse_spec(canonical)
    blocks: dict[str, Requirement] = {r.name: r for r in spec.requirements}
    order = [r.name for r in spec.requirements]

    def near_miss(name: str, *, other_than: str | None = None) -> str | None:
        key = fold_requirement_name(name)
        return next((k for k in blocks if k != other_than and fold_requirement_name(k) == key), None)

    for frm, to in delta.renamed:
        if frm not in blocks:
            if to in blocks:
                if (k := near_miss(frm, other_than=to)) is not None:
                    raise ApplyAbort(f"{cap}: RENAMED source {frm!r} not found, but {k!r} exists")
                continue  # already renamed
            raise ApplyAbort(f"{cap}: RENAMED source {frm!r} not found")
        if to in blocks:
            raise ApplyAbort(f"{cap}: RENAMED TO {to!r} already exists")
        if (k := near_miss(to, other_than=frm)) is not None:
            raise ApplyAbort(f"{cap}: RENAMED TO {to!r} differs from existing {k!r} only in case or spacing")
        blocks[to] = blocks.pop(frm).renamed(to)
        order[order.index(frm)] = to

    for name in delta.removed:
        if name in blocks:
            del blocks[name]
        elif not is_new:
            if (k := near_miss(name)) is not None:
                raise ApplyAbort(f"{cap}: REMOVED {name!r} not found, but {k!r} exists")
            warnings.append(f"{cap}: REMOVED {name!r} not found in the current spec; treating it as already removed")

    def modify(r: Requirement) -> None:
        current = blocks.get(r.name)
        if current is None:
            raise ApplyAbort(f"{cap}: MODIFIED {r.name!r} not found")
        missing = _missing_scenarios(current, r)
        if missing:
            raise ApplyAbort(f"{cap}: MODIFIED {r.name!r} drops scenarios {missing}")
        blocks[r.name] = r

    for r in delta.modified:
        modify(r)

    for r in delta.added:
        existing = blocks.get(r.name)
        if existing is not None:
            if normalize_block(existing.text) == normalize_block(r.text):
                continue
            if mode == "sync":
                modify(r)
                continue
            raise ApplyAbort(f"{cap}: ADDED {r.name!r} exists with different content")
        if (k := near_miss(r.name)) is not None:
            raise ApplyAbort(f"{cap}: ADDED {r.name!r} differs from existing {k!r} only in case or spacing")
        blocks[r.name] = r

    kept = [blocks[k] for k in order if k in blocks]
    kept += [b for k, b in blocks.items() if k not in order]
    return render_spec(Spec(spec.before, spec.header_line, spec.preamble, kept, spec.after))


def apply_change(
    canonical: Mapping[str, str],
    deltas: Mapping[str, str],
    *,
    mode: str = "archive",
    change: str = "change",
) -> ApplyResult:
    """Apply per-capability delta files to canonical specs; all-or-nothing, pure.

    Validates each delta first and each rebuilt spec after, as archive does:
    any ERROR aborts, WARNINGs are returned.
    """
    if mode not in ("archive", "sync"):
        raise ValueError(f"unknown mode {mode!r}")
    out = dict(canonical)
    warnings: list[str] = []
    parsed = {cap: parse_delta(deltas[cap]) for cap in sorted(deltas)}
    for cap, delta in parsed.items():
        issues = validate_delta(cap, delta)
        if errors(issues):
            raise ApplyAbort("; ".join(errors(issues)))
        warnings += [i.message for i in issues if i.level == "WARNING"]
    for cap, delta in parsed.items():
        out[cap] = _apply_one(cap, canonical.get(cap), delta, mode, change, warnings)
        issues = validate_spec(cap, out[cap])
        if errors(issues):
            raise ApplyAbort(f"rebuilt spec would be invalid: {'; '.join(errors(issues))}")
        warnings += [i.message for i in issues if i.level == "WARNING"]
    return ApplyResult(out, warnings)


def _collect_specs(base: Path, *, strict: bool) -> dict[str, str]:
    """Map capability (path under base, posix) to spec.md text, at any depth."""
    out: dict[str, str] = {}
    for p in sorted(base.rglob("spec.md")):
        rel = p.parent.relative_to(base)
        if any(part.startswith(".") for part in rel.parts):
            continue  # upstream spec discovery skips hidden entries
        cap = rel.as_posix()
        if cap == ".":
            if strict:
                raise ApplyAbort(f"spec.md directly under {base.name}/ has no capability directory")
            continue
        out[cap] = p.read_text(encoding="utf-8")
    return out


def _check_metadata(change_dir: Path) -> None:
    meta_path = change_dir / ".openspec.yaml"
    if not meta_path.exists():
        return
    try:
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise MetadataError(".openspec.yaml must be a mapping")
        validate_mapping(meta)
    except (yaml.YAMLError, MetadataError) as exc:
        raise ApplyAbort(f"invalid .openspec.yaml: {exc}") from exc
    if meta.get("retire_capabilities") is True:
        raise ApplyAbort(f"retire_capabilities is not supported: {PROFILE['no-retirement']}")


def archive_target(change: str, today: _dt.date) -> str:
    """AR: a name that already carries a YYYY-MM-DD- prefix is not prefixed again."""
    return change if _DATED.match(change) else f"{today.isoformat()}-{change}"


def archive_change(root: Path, change: str, *, today: _dt.date | None = None) -> Path:
    """Apply ``root/changes/<change>`` to ``root/specs`` and move it to the archive.

    Test/tmp-dir use only. Fails if the archive target exists or any op aborts,
    leaving canonical specs and the change untouched.
    """
    root = Path(root)
    change_dir = root / "changes" / change
    target = root / "changes" / "archive" / archive_target(change, today or _dt.date.today())
    if not change_dir.is_dir():
        raise ApplyAbort(f"change not found: {change}")
    if target.exists():
        raise ApplyAbort(f"archive target already exists: {target.name}")
    _check_metadata(change_dir)
    deltas = _collect_specs(change_dir / "specs", strict=True)
    canonical = _collect_specs(root / "specs", strict=False)
    result = apply_change(canonical, deltas, change=change)  # raises before any write
    for cap in deltas:
        dest = root / "specs" / cap / "spec.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(result.specs[cap], encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(change_dir), str(target))
    return target
