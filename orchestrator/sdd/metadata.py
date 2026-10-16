"""``.openspec.yaml`` load/dump with MCTL execution-state exclusion.

Target: OpenSpec v1.14.2 / f80d860 (pinned facts from mctlhq/mctl-agents#655).
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

METADATA_FILENAME = ".openspec.yaml"
KNOWN_KEYS = (
    "schema",
    "created",
    "goal",
    "affected_areas",
    "initiative",
    "skip_specs",
    "retire_capabilities",
)
# Top-level .status.yaml keys: execution state that must never enter a spec artifact.
FORBIDDEN_EXECUTION_KEYS = frozenset(
    {
        "status",
        "approval",
        "control",
        "execution",
        "attempt",
        "pr",
        "github",
        "merged_at",
        "merge_commit",
        "context",
        "request",
    }
)
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class MetadataError(ValueError):
    """Raised when ``.openspec.yaml`` content is invalid."""


@dataclass
class ChangeMetadata:
    data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def skip_specs(self) -> bool:
        return bool(self.data.get("skip_specs", False))

    @property
    def extensions(self) -> dict[str, Any]:
        return {k: v for k, v in self.data.items() if k.startswith("x-")}


def _forbidden(key: str) -> str | None:
    if key in FORBIDDEN_EXECUTION_KEYS:
        return key
    if key.startswith("x-mctl-") and key[len("x-mctl-") :] in FORBIDDEN_EXECUTION_KEYS:
        return key
    return None


def _has_spec_files(change_dir: Path | None) -> bool:
    if change_dir is None:
        return False
    specs = change_dir / "specs"
    return specs.is_dir() and any(p.is_file() for p in specs.rglob("*"))


def validate_mapping(data: Mapping[str, Any], *, strict: bool = False, change_dir: Path | None = None) -> list[str]:
    """Validate; return warnings, raise MetadataError on any error."""
    warnings: list[str] = []
    for key in data:
        bad = _forbidden(str(key))
        if bad:
            raise MetadataError(f"key {bad!r} carries execution state and is forbidden")
    if not isinstance(data.get("schema"), str) or not data["schema"]:
        raise MetadataError("'schema' is required and must be a non-empty string")
    created = data.get("created")
    if created is not None and not (isinstance(created, str) and _DATE.match(created)):
        raise MetadataError("'created' must be a YYYY-MM-DD string")
    if "goal" in data and not isinstance(data["goal"], str):
        raise MetadataError("'goal' must be a string")
    areas = data.get("affected_areas")
    if areas is not None and not (isinstance(areas, list) and all(isinstance(a, str) for a in areas)):
        raise MetadataError("'affected_areas' must be a list of strings")
    retire = data.get("retire_capabilities")
    if retire is not None and not (isinstance(retire, list) and all(isinstance(a, str) for a in retire)):
        raise MetadataError("'retire_capabilities' must be a list of strings")
    if "skip_specs" in data and not isinstance(data["skip_specs"], bool):
        raise MetadataError("'skip_specs' must be a boolean")
    init = data.get("initiative")
    if init is not None:
        if not isinstance(init, dict):
            raise MetadataError("'initiative' must be a mapping")
        for k, v in init.items():
            if str(k).startswith("x-"):
                raise MetadataError(f"'x-' key {k!r} is not allowed inside 'initiative'")
            if k not in ("store", "id"):
                raise MetadataError(f"unknown key {k!r} inside 'initiative'")
            if not isinstance(v, str) or not _KEBAB.match(v):
                raise MetadataError(f"initiative.{k} must be kebab-case")
    if data.get("skip_specs") is True and _has_spec_files(change_dir):
        raise MetadataError("skip_specs is true but specs/ contains files")
    for key in data:
        if key in KNOWN_KEYS or str(key).startswith("x-"):
            continue
        msg = f"unknown top-level key {key!r}"
        if strict:
            raise MetadataError(msg)
        warnings.append(msg)
    return warnings


def load_change_metadata(change_dir: Path, *, strict: bool = False) -> ChangeMetadata | None:
    """Load ``<change_dir>/.openspec.yaml``; a missing file means no metadata."""
    path = Path(change_dir) / METADATA_FILENAME
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise MetadataError(".openspec.yaml must be a mapping")
    if isinstance(raw.get("created"), (_dt.date, _dt.datetime)):
        raw["created"] = raw["created"].strftime("%Y-%m-%d")
    warnings = validate_mapping(raw, strict=strict, change_dir=Path(change_dir))
    return ChangeMetadata(data=raw, warnings=warnings)


def dump_change_metadata(data: Mapping[str, Any], change_dir: Path, *, strict: bool = True) -> list[str]:
    """Validate then write; nothing is written when validation fails."""
    warnings = validate_mapping(data, strict=strict, change_dir=Path(change_dir))
    text = yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True)
    Path(change_dir).mkdir(parents=True, exist_ok=True)
    (Path(change_dir) / METADATA_FILENAME).write_text(text, encoding="utf-8")
    return warnings
