"""Cited copy of the closed `mctl.events/v1` envelope shape (spike #757).

Source (read on the default branch, not imported):
  repo:   mctlhq/mctl-claude-remote
  path:   events/mctl_events/envelope.py
  SHA:    989bff3b8c0da53cfb109110dfadb022de142e40
Canonical schema:
  repo:   mctlhq/.github
  path:   events/schemas/event-envelope.v1.schema.json
  SHA:    92b1e28327b0d0b575b18007ab6935d45dff057d

Only the shape is copied: the closed top-level key set, the required keys, the
field patterns and the 4096-byte limit. If the producer changes, this copy is
stale; re-validate the fixtures against the producer's own tests.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

SPEC_VERSION = "mctl.events/v1"
FIELDS = frozenset({"specversion", "id", "type", "source", "occurred_at", "correlation_id", "subject"})
MAX_ENVELOPE_BYTES = 4096
MAX_SUBJECT_KEYS = 12
MAX_SUBJECT_VALUE = 256

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/@#-]{0,255}$")
_TYPE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")
_SOURCE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SUBJECT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_KIND = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
_SUBJECT_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@#+=-]*$")
_RFC3339 = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(\.\d+)?(Z|[+-]([01]\d|2[0-3]):[0-5]\d)$"
)


class EnvelopeRefused(ValueError):
    """The envelope does not match the closed mctl.events/v1 shape."""


def validate(doc: Any) -> dict[str, Any]:
    """Return `doc` unchanged if it is a valid envelope, else raise EnvelopeRefused."""
    if not isinstance(doc, dict):
        raise EnvelopeRefused("envelope must be an object")
    try:
        size = len(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise EnvelopeRefused("envelope is not serializable") from exc
    if size > MAX_ENVELOPE_BYTES:
        raise EnvelopeRefused(f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
    unknown = set(doc) - FIELDS
    if unknown:
        raise EnvelopeRefused(f"{len(unknown)} unknown envelope field(s)")
    missing = sorted(FIELDS - set(doc))
    if missing:
        raise EnvelopeRefused(f"missing envelope fields: {missing}")
    if doc["specversion"] != SPEC_VERSION:
        raise EnvelopeRefused("unsupported specversion")
    for name, pattern in (
        ("id", _ID),
        ("type", _TYPE),
        ("source", _SOURCE),
        ("correlation_id", _ID),
        ("occurred_at", _RFC3339),
    ):
        value = doc[name]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise EnvelopeRefused(f"invalid {name}")
    try:
        # The pattern admits impossible dates such as 02-30; the source parses too.
        datetime.fromisoformat(doc["occurred_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeRefused("invalid occurred_at") from exc
    subject = doc["subject"]
    if not isinstance(subject, dict) or "kind" not in subject:
        raise EnvelopeRefused("subject must be an object with a kind")
    if len(subject) > MAX_SUBJECT_KEYS:
        raise EnvelopeRefused("subject has too many keys")
    kind = subject["kind"]
    if not isinstance(kind, str) or len(kind) > 64 or not _KIND.fullmatch(kind):
        raise EnvelopeRefused("invalid subject.kind")
    for key, value in subject.items():
        if not isinstance(key, str) or not _SUBJECT_KEY.fullmatch(key):
            raise EnvelopeRefused("invalid subject key")
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_SUBJECT_VALUE:
            raise EnvelopeRefused(f"subject.{key} must be a bounded string")
        if not _SUBJECT_VALUE.fullmatch(value):
            raise EnvelopeRefused(f"subject.{key} must be an identifier, not free text")
    return doc
