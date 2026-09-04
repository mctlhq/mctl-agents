"""Shared durable-state helpers for proposal lifecycle controllers.

GitHub is authoritative for pull-request lifecycle.  ``.status.yaml`` is a
durable projection used for workflow coordination and operator audit.  Every
writer must therefore preserve fields it does not explicitly change.
"""
from __future__ import annotations

import os
import secrets
import stat
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

# Spellings that waive the approval requirement. Deliberately a denylist: the
# gate defaults to ON, so a value nobody anticipated is refused rather than
# read as consent.
_FALSEY = {"false", "no", "0", "off", ""}


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
    if control is None:
        return True
    if not isinstance(control, dict):
        # A control block that is present but not a mapping is corrupt, not
        # absent. "Absent means not required" is a statement about proposals
        # that never asked for approval; a malformed one asked for something
        # unreadable, so it fails closed (agy P2).
        return False

    # Only a value that recognisably says "no" waives the gate. Everything
    # else — including anything unrecognised — requires an approval.
    #
    # Two ways to get this wrong, both of which fail OPEN and both of which a
    # reviewer caught in turn (agy P1, twice):
    #
    #   `required is not True`      → `requires_human_approval: "true"` waives
    #                                 the gate, because YAML leaves a quoted
    #                                 scalar a string.
    #   `str(required) not in {…}`  → a list, a dict or a typo waives it,
    #                                 because an allowlist of truthy spellings
    #                                 treats everything unlisted as falsey.
    #
    # So the test is against the FALSEY spellings, and the default is to
    # require approval. An absent key still means "never asked".
    required = control.get("requires_human_approval")
    if required is None or required is False:
        return True
    if isinstance(required, str) and required.strip().lower() in _FALSEY:
        return True
    if isinstance(required, int) and not isinstance(required, bool) and required == 0:
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

    _write_status_atomic(path, payload)
    return payload


def _write_status_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Serialise, then rename over the target. Never truncate it in place.

    `open(path, "w")` truncates to zero bytes before a single byte of the
    new content is written, and that window is not merely lost work here.
    A `.status.yaml` left empty or half-written makes `load_status` return
    `{}` or raise, and an empty mapping has no `control` block — which
    `human_approval_satisfied` reads as "this proposal never asked for
    approval" and waives the gate. So the one failure mode that erases the
    requirement is the same one that lets it be skipped (agy P1 on
    gitops#986).

    The dump happens to a string first, so a serialisation error leaves the
    file untouched instead of emptied. rename(2) within a directory is
    atomic, so a concurrent reader sees the old file or the new one and
    never a partial one.

    This closes the truncation, partial-read and crash-corruption windows.
    It does NOT make the surrounding read-modify-write exclusive: two
    writers that both load before either replaces still lose one update.
    Closing that needs a lock, and the obvious place for one — a sidecar
    beside the file — is inside the gitops worktree, where the investigate
    CWFT's `git add ':(glob)…/proposals/*/**'` would stage it. Tracked in
    mctlhq/mctl-agents#307 rather than improvised here.

    Mirrors `run_issue_investigator.write_status_yaml`, which was given the
    same treatment for the same reason; not shared with it because that one
    also carries staging-directory permission handling this path does not.
    """
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)

    # A rewrite must not silently change who can read the file: several
    # components that are not this process read .status.yaml.
    try:
        mode: int | None = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None

    # O_CREAT|O_EXCL with 0o666 rather than tempfile.mkstemp, which forces
    # 0600 and would carry that onto the real file through os.replace. This
    # way the kernel applies the process umask, which is what an ordinary
    # open() would have produced.
    tmp_path = path.with_name(f".status.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            if mode is not None:
                # On the DESCRIPTOR: a path-based chmod re-resolves a name
                # in a directory the agent can write.
                os.fchmod(stream.fileno(), mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
