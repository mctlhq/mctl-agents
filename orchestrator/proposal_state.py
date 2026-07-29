"""Shared durable-state helpers for proposal lifecycle controllers.

GitHub is authoritative for pull-request lifecycle.  ``.status.yaml`` is a
durable projection used for workflow coordination and operator audit.  Every
writer must therefore preserve fields it does not explicitly change.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class _Unset:
    pass


UNSET = _Unset()


def now_iso() -> str:
    """Return an RFC 3339 UTC timestamp without microseconds."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_status(path: Path) -> dict[str, Any]:
    """Parse a proposal status file, returning an empty mapping if absent."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping, got {type(data).__name__}")
    return data


def update_status_file(
    path: Path,
    new_status: str,
    *,
    actor: str = "mctl-agents[bot]",
    **fields: Any,
) -> dict[str, Any]:
    """Read, merge, and write one ``.status.yaml``.

    Existing fields are preserved by default.  Passing ``None`` explicitly
    removes a field; omitting it leaves the on-disk value untouched.  ``UNSET``
    is accepted for callers that build keyword arguments programmatically.
    """
    payload = dict(load_status(path))
    payload["status"] = new_status
    payload["updated_at"] = now_iso()
    payload["updated_by"] = actor
    for key, value in fields.items():
        if value is UNSET:
            continue
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(payload, stream, sort_keys=False, allow_unicode=True)
    return payload
