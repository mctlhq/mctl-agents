"""The credential/value screen shared by `orchestrator/tracing_sdk.py` and
`orchestrator/execution_evidence.py` (mctl-agents#520, #195).

Extracted from `tracing_sdk.py` verbatim, behaviour-preserving, so that a
stdlib-only module can apply the same credential-shape screen without
importing OpenTelemetry. This module imports `re` and nothing else.

Dropped, never masked: a masked value is a present attribute that reads
like data, an absent one is honest. That policy lives here once so it
cannot drift between the tracing guard and the evidence envelope.
"""
from __future__ import annotations

import re
from typing import Any

# An attribute value longer than this is a payload, not an identifier. It is
# DROPPED, not truncated: a truncated payload is still a payload.
MAX_ATTRIBUTE_CHARS = 256

# Denylist, applied ON TOP of an allowlist — so `mctl.issue.body` or
# `mctl.tool.arguments` is refused even though it is in the mctl namespace.
# Mirrors the Collector's patterns (mctl-docs telemetry-attributes.md,
# "Privacy") plus the payload-shaped suffixes this repo could plausibly
# produce.
_DENIED_KEY = re.compile(
    r"(?i)(?:authorization|cookie|api[-_]?key|secret|passw(?:or)?d|credential|private[-_]?key|session[-_]?key"
    r"|(?:^|[._])(?:prompts?|completions?|messages?|arguments?|args|argv|input|output|results?|body|content"
    r"|text|payload|stdout|stderr|command|cmd|diff|query|description|comment)$)"
)
_TOKEN_KEY = re.compile(r"(?i)token")
# Credential SHAPES, checked on every string value regardless of its key:
# GitHub tokens, fine-grained PATs, sk- keys (Anthropic/OpenAI), Vault
# tokens, JWTs, PEM private keys, bearer headers, basic-auth URLs.
_CREDENTIAL_VALUE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|hv[sbr]\.[A-Za-z0-9_-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:bearer\s+[A-Za-z0-9._~+/-]{12,})"
    r"|://[^/\s:@]+:[^/\s@]+@)"
)


def _scalar_allowed(value: Any) -> bool:
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return 0 < len(value) <= MAX_ATTRIBUTE_CHARS and not _CREDENTIAL_VALUE.search(value)
    return False


def value_allowed(value: Any) -> bool:
    if isinstance(value, list | tuple):
        return len(value) <= 32 and all(_scalar_allowed(v) for v in value)
    return _scalar_allowed(value)


def contains_credential(text: str) -> bool:
    """Whether `text` contains a credential-shaped substring.

    Used by callers, such as the evidence envelope, that need the same
    shape screen `tracing_sdk` applies but cannot import OpenTelemetry."""
    return isinstance(text, str) and bool(_CREDENTIAL_VALUE.search(text))


def safe_scalar(value: Any, *, max_chars: int) -> bool:
    """Whether `value` is a scalar safe to hash or export as-is: a bool,
    int or float, or a non-empty string within `max_chars` that does not
    contain a credential shape."""
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return 0 < len(value) <= max_chars and not _CREDENTIAL_VALUE.search(value)
    return False
