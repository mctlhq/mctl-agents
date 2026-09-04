"""Shared durable-state helpers for proposal lifecycle controllers.

GitHub is authoritative for pull-request lifecycle.  ``.status.yaml`` is a
durable projection used for workflow coordination and operator audit.  Every
writer must therefore preserve fields it does not explicitly change.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


class _Unset:
    pass


UNSET = _Unset()


def now_iso() -> str:
    """Return an RFC 3339 UTC timestamp without microseconds."""
    return (
        datetime.now(UTC)
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


# Approvers that record no identity. "unknown" is the literal default in
# mctl-api's operations registry and in cwft-mctl-agents-approve.yaml, and the
# Temporal CLI's `approve` signals with no payload at all, which lands the same
# value. Treating it as an approval would leave the gate open through exactly
# the paths that record nothing.
_ANONYMOUS_APPROVERS = {"", "unknown", "none", "null"}


def human_approval_satisfied(data: dict[str, Any]) -> bool:
    """Whether a proposal's recorded approval meets its own requirement.

    ``control.requires_human_approval`` was written by the investigator,
    self-checked once at publish time, and then read by nothing: the
    implementer gated on ``status == accepted`` alone, so a proposal committed
    as ``accepted`` was indistinguishable from one a human approved
    (gitops#986).

    A proposal with no ``control`` block does NOT require approval. The
    incident-responder writes ``status: accepted`` with no control block at
    all, and defaulting to deny would strand that entire path.
    """
    control = data.get("control")
    if not isinstance(control, dict):
        return True
    if control.get("requires_human_approval") is not True:
        return True

    approval = data.get("approval")
    if not isinstance(approval, dict):
        return False
    approved_by = approval.get("approved_by")
    if not isinstance(approved_by, str):
        return False
    return approved_by.strip().lower() not in _ANONYMOUS_APPROVERS


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
