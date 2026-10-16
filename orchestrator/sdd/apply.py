"""Pure delta application and tmp-dir archive, following the pinned archive rules.

Target: OpenSpec v1.14.2 / f80d860. Order: RENAMED -> REMOVED -> MODIFIED -> ADDED,
with cross-section pre-validation. MCTL choice: mirror ``archive`` (deterministic
abort), NOT ``sync``'s implicit ADDED -> MODIFIED, unless ``mode="sync"`` is
requested explicitly (kept only to demonstrate the divergence).
"""

from __future__ import annotations

import datetime as _dt
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.sdd.spec_model import (
    Delta,
    Requirement,
    Spec,
    normalize_content,
    normalize_name,
    parse_delta,
    parse_spec,
    render_spec,
)


class ApplyAbort(Exception):
    """A delta operation aborted; no output is produced."""


@dataclass
class ApplyResult:
    specs: dict[str, str]
    warnings: list[str] = field(default_factory=list)


def _find(reqs: list[Requirement], name: str, what: str) -> int | None:
    """Exact match index, None if absent; a case/whitespace near-miss aborts."""
    for i, r in enumerate(reqs):
        if r.name == name:
            return i
    key = normalize_name(name)
    for r in reqs:
        if normalize_name(r.name) == key:
            raise ApplyAbort(f"{what}: {name!r} is a near-miss of existing {r.name!r}")
    return None


def _apply_one(cap: str, canonical: str | None, delta: Delta, mode: str, warnings: list[str]) -> str:
    if canonical is None:
        if delta.modified or delta.renamed:
            raise ApplyAbort(f"{cap}: new capability allows only ADDED (got MODIFIED/RENAMED)")
        spec = Spec(f"# {cap} Specification\n\n## Requirements")
    else:
        spec = parse_spec(canonical)
        dup = spec.duplicates()
        if dup:
            raise ApplyAbort(f"{cap}: duplicate requirement headers in main spec: {dup}")
    for label, group in (
        ("ADDED", delta.added),
        ("MODIFIED", delta.modified),
        ("REMOVED", delta.removed),
    ):
        names = [normalize_name(r.name) for r in group]
        if len(names) != len(set(names)):
            raise ApplyAbort(f"{cap}: duplicate header within {label}")
    removed = {normalize_name(r.name) for r in delta.removed}
    added = {normalize_name(r.name) for r in delta.added}
    for frm, to in delta.renamed:
        if normalize_name(frm) in removed:
            raise ApplyAbort(f"{cap}: RENAMED FROM {frm!r} is also REMOVED")
        if normalize_name(to) in added:
            raise ApplyAbort(f"{cap}: RENAMED TO {to!r} collides with ADDED")
    reqs = list(spec.requirements)

    for frm, to in delta.renamed:
        i = _find(reqs, frm, "RENAMED")
        if i is None:
            if _find(reqs, to, "RENAMED") is None:
                raise ApplyAbort(f"{cap}: RENAMED {frm!r} -> {to!r}: both missing")
            continue  # source missing, target present: no-op
        if _find(reqs, to, "RENAMED") is not None:
            raise ApplyAbort(f"{cap}: RENAMED TO {to!r} already exists")
        reqs[i] = reqs[i].renamed(to)

    for r in delta.removed:
        i = _find(reqs, r.name, "REMOVED")
        if i is None:
            warnings.append(f"{cap}: REMOVED {r.name!r} not found; skipped")
        else:
            del reqs[i]

    def modify(r: Requirement) -> None:
        i = _find(reqs, r.name, "MODIFIED")
        if i is None:
            raise ApplyAbort(f"{cap}: MODIFIED {r.name!r} not found")
        if normalize_content(reqs[i].text) == normalize_content(r.text):
            return
        missing = set(reqs[i].scenarios) - set(r.scenarios)
        if missing:
            raise ApplyAbort(f"{cap}: MODIFIED {r.name!r} drops scenarios {sorted(missing)}")
        reqs[i] = r

    for r in delta.modified:
        modify(r)

    for r in delta.added:
        i = _find(reqs, r.name, "ADDED")
        if i is None:
            reqs.append(r)
        elif normalize_content(reqs[i].text) == normalize_content(r.text):
            continue
        elif mode == "sync":
            modify(r)
        else:
            raise ApplyAbort(f"{cap}: ADDED {r.name!r} exists with different content")

    return render_spec(Spec(spec.preamble, reqs))


def apply_change(canonical: Mapping[str, str], deltas: Mapping[str, str], *, mode: str = "archive") -> ApplyResult:
    """Apply per-capability delta files to canonical specs; all-or-nothing, pure."""
    if mode not in ("archive", "sync"):
        raise ValueError(f"unknown mode {mode!r}")
    out = dict(canonical)
    warnings: list[str] = []
    for cap in sorted(deltas):
        out[cap] = _apply_one(cap, canonical.get(cap), parse_delta(deltas[cap]), mode, warnings)
    return ApplyResult(out, warnings)


def _collect_specs(base: Path, *, strict: bool) -> dict[str, str]:
    """Map capability (path under base, posix) to spec.md text, at any depth."""
    out: dict[str, str] = {}
    for p in sorted(base.rglob("spec.md")):
        cap = p.parent.relative_to(base).as_posix()
        if cap == ".":
            if strict:
                raise ApplyAbort(f"spec.md directly under {base.name}/ has no capability directory")
            continue
        out[cap] = p.read_text(encoding="utf-8")
    return out


def archive_change(root: Path, change: str, *, today: _dt.date | None = None) -> Path:
    """Apply ``root/changes/<change>`` to ``root/specs`` and move it to the archive.

    Test/tmp-dir use only. Fails if the archive target exists or any op aborts,
    leaving canonical specs and the change untouched.
    """
    root = Path(root)
    change_dir = root / "changes" / change
    stamp = (today or _dt.date.today()).isoformat()
    target = root / "changes" / "archive" / f"{stamp}-{change}"
    if not change_dir.is_dir():
        raise ApplyAbort(f"change not found: {change}")
    if target.exists():
        raise ApplyAbort(f"archive target already exists: {target.name}")
    deltas = _collect_specs(change_dir / "specs", strict=True)
    canonical = _collect_specs(root / "specs", strict=False)
    result = apply_change(canonical, deltas)  # raises before any write
    for cap in deltas:
        dest = root / "specs" / cap / "spec.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(result.specs[cap], encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(change_dir), str(target))
    return target
