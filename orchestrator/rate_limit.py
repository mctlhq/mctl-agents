"""Shared quota-exhaustion classifier for the Claude Agent SDK message stream.

`orchestrator.run_issue_investigator` already told a rejected OAuth/API-key
quota window (the CLI's terminal ``ResultMessage`` with ``is_error=True`` and
``api_error_status == 429``) apart from an ordinary agent/tooling failure.
`orchestrator.run_implementer` did not, and the two SHOULD NOT diverge: both
drivers watch the same stream shape for the same reason (mctl-agents#364).

This module is meant to be the one place that shape is inspected. Today only
`run_implementer` uses it: `run_issue_investigator` still keeps its own inline
429 check and its own `RateLimitExhaustedError`, so an `except` for one class
does not catch the other. Moving the investigator onto this module is a
follow-up (mctl-agents#455). Every check below is duck-typed --- ``getattr(message, ..., None)``
plus a ``type(message).__name__`` comparison, never an ``isinstance`` against
the SDK's own classes --- because this module MUST be importable at module
scope by ``orchestrator.run_issue_investigator``, which is itself imported by
the long-lived Temporal worker (``orchestrator/temporal/worker.py`` via the
issue poller). Importing ``claude_agent_sdk`` here would drag the whole agent
stack into that process, exactly what ADR-005/006 and
``tests/test_worker_isolation.py`` forbid (mctl-agents#149).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class RateLimitExhaustedError(RuntimeError):
    """The SDK's final ``ResultMessage`` reported an API-level rate/usage-limit
    rejection (``is_error`` True, ``api_error_status`` 429) rather than an
    agent/tooling failure.

    Carries an optional ``observation`` --- the durable, credential-free
    evidence (account label, limit type, reset time) gathered from any
    ``RateLimitEvent`` seen on the stream before the terminal frame --- so a
    caller can write it down without re-deriving it. ``observation`` is
    ``None`` when nothing called this constructor with one (for example
    ``run_issue_investigator``, which only needs the exception's existence,
    not its payload, to pick its OAuth-fallback retry).
    """

    def __init__(self, message: str, observation: RateLimitObservation | None = None) -> None:
        super().__init__(message)
        self.observation = observation


@dataclass(frozen=True)
class RateLimitObservation:
    """Durable, credential-free evidence of one rate-limit rejection.

    Every field here is safe to write into ``.status.yaml`` or print to a log:
    an ordinal account label, an SDK-supplied limit type/reset time, and a
    short summary. Never a token value, prefix or suffix.
    """

    account: str                          # "primary" / "secondary" / "1" / "2" / "unknown"
    rate_limit_type: str | None           # "seven_day", "five_hour", ... or None if unknown
    resets_at: str | None                 # RFC 3339 UTC, derived from the SDK's Unix ts
    resets_at_epoch: int | None           # the raw Unix ts, kept for exact comparison
    overage_disabled_reason: str | None   # e.g. "out_of_credits", or None
    detail: str                           # short, credential-free summary of what was observed


def _epoch_to_iso(epoch: int | None) -> str | None:
    """Unix timestamp -> RFC 3339 UTC, matching ``proposal_state.now_iso``'s
    formatting convention (no microseconds, ``Z`` suffix)."""
    if epoch is None:
        return None
    # Runs inside the 429 handler: an out-of-range value (e.g. a
    # millisecond-scaled `resets_at`) must degrade to "unknown reset", never
    # raise into the generic unexpected-error arm.
    try:
        return (
            datetime.fromtimestamp(epoch, tz=UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def is_rate_limit_result(message: object) -> bool:
    """Whether ``message`` is a terminal ``ResultMessage`` reporting a 429.

    Duck-typed on purpose (see the module docstring): a ``ResultMessage`` for
    any other failure (500, 529, ...) or a clean success answers ``False``, so
    today's behaviour for every other outcome is unchanged.
    """
    return (
        type(message).__name__ == "ResultMessage"
        and bool(getattr(message, "is_error", False))
        and getattr(message, "api_error_status", None) == 429
    )


def observe_rate_limit_event(message: object) -> Any | None:
    """Return a ``RateLimitEvent``'s ``rate_limit_info``, or ``None``.

    The event is enrichment only: an ``allowed``/``allowed_warning`` event
    fires on a perfectly healthy run, and a ``rejected`` event does not by
    itself end one. Callers filter on ``getattr(info, "status", None) ==
    "rejected"`` themselves; this only extracts the shape.
    """
    if type(message).__name__ != "RateLimitEvent":
        return None
    return getattr(message, "rate_limit_info", None)


def account_label() -> str:
    """Which OAuth/API-key account this process is running as, best-effort.

    Reads the optional, non-secret ``CLAUDE_OAUTH_ACCOUNT`` label first (the
    `_2`-suffix convention already used by ``.github/workflows/claude-review.yml``);
    falls back to deriving ``primary``/``secondary`` from
    ``orchestrator.auth.detect_auth().env_var``; returns ``"unknown"``
    otherwise. Never reads a token value.
    """
    explicit = os.getenv("CLAUDE_OAUTH_ACCOUNT", "").strip()
    if explicit:
        return explicit
    try:
        from orchestrator.auth import detect_auth

        env_var = detect_auth().env_var
    except Exception:  # noqa: BLE001 — no auth configured; fall through to unknown
        return "unknown"
    # `detect_auth()` names a variable on every path today, including
    # "(claude CLI session)"; stay total anyway, since this runs inside the
    # 429 handler and must never raise there.
    if not isinstance(env_var, str) or not env_var:
        return "unknown"
    if env_var.endswith("_SECONDARY"):
        return "secondary"
    if env_var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        return "primary"
    return "unknown"


def build_observation(info: object | None, *, detail: str) -> RateLimitObservation:
    """Build a ``RateLimitObservation`` from an optional ``RateLimitInfo``-shaped
    ``info`` (as returned by ``observe_rate_limit_event``). ``info`` may be
    ``None`` when the terminal 429 arrived with no preceding ``RateLimitEvent``
    on the stream (the SDK does not guarantee one) --- the observation is then
    account-only, with the limit-type/reset fields left ``None``.
    """
    resets_at_epoch = getattr(info, "resets_at", None) if info is not None else None
    if resets_at_epoch is not None:
        try:
            resets_at_epoch = int(resets_at_epoch)
        except (TypeError, ValueError):
            resets_at_epoch = None
    rate_limit_type = getattr(info, "rate_limit_type", None) if info is not None else None
    overage_disabled_reason = (
        getattr(info, "overage_disabled_reason", None) if info is not None else None
    )
    return RateLimitObservation(
        account=account_label(),
        rate_limit_type=rate_limit_type,
        resets_at=_epoch_to_iso(resets_at_epoch),
        resets_at_epoch=resets_at_epoch,
        overage_disabled_reason=overage_disabled_reason,
        detail=detail,
    )
