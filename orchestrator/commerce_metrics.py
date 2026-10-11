"""Commerce metrics (ADR 020 sec. 13; mctlhq/mctl-agents#620, C5).

Instruments:

- `mctl.commerce.invocations` (counter, `{invocation}`): attributes `phase`,
  `reason_code`, `price_mode`, `funds_mode`, `provider_id`;
- `mctl.commerce.invocation.duration` (histogram, `s`): attributes `phase`,
  `price_mode`, `provider_id`;
- `mctl.commerce.outcome_unknown` (counter, `{invocation}`): attribute
  `provider_id`.

The attribute set is closed and bounded. Every value comes from a closed
vocabulary; `provider_id` is limited to the admission list and anything else
collapses to `other`. Invocation, quote, approval, execution and tenant ids
are never attributes, and neither are amounts (ADR 012, ADR 020 sec. 13).

The ledger never depends on this module: every function here swallows its own
errors and does nothing when telemetry is off. Like `orchestrator/tracing.py`
it is stdlib-only at import and imports OpenTelemetry lazily, only once
tracing is configured, so it is safe in the long-lived Temporal worker.
"""
from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from typing import Any, Protocol

from orchestrator import tracing
from orchestrator.capability import FUNDS_MODES, PRICE_MODES, REASON_CODES

logger = logging.getLogger(__name__)

INVOCATIONS = "mctl.commerce.invocations"
DURATION = "mctl.commerce.invocation.duration"
OUTCOME_UNKNOWN = "mctl.commerce.outcome_unknown"

#: The only attribute keys any instrument may carry.
ALLOWED_ATTRIBUTES = frozenset({"phase", "reason_code", "price_mode", "funds_mode", "provider_id"})

#: ADR 020 sec. 11 phases, plus the empty phase of a call refused before an
#: invocation existed.
PHASES = frozenset({
    "accepted", "refused", "reserved", "dispatching", "dispatched", "outcome_unknown", "released", "finalized",
})
OTHER = "other"


class Backend(Protocol):
    def add(self, name: str, value: int, attributes: Mapping[str, str]) -> None: ...

    def record(self, name: str, value: float, attributes: Mapping[str, str]) -> None: ...


def _closed(value: Any, vocabulary: Collection[str]) -> str:
    return value if isinstance(value, str) and value in vocabulary else (OTHER if value else "")


def attributes_for(
    *, admitted_providers: Collection[str], phase: Any = "", reason_code: Any = "", price_mode: Any = "",
    funds_mode: Any = "", provider_id: Any = "",
) -> dict[str, str]:
    """The bounded attribute set for one data point. Pure."""
    return {
        "phase": _closed(phase, PHASES),
        "reason_code": _closed(reason_code, REASON_CODES),
        "price_mode": _closed(price_mode, PRICE_MODES),
        "funds_mode": _closed(funds_mode, FUNDS_MODES),
        "provider_id": provider_id if isinstance(provider_id, str) and provider_id in admitted_providers else OTHER,
    }


class _OtelBackend:
    def __init__(self) -> None:
        from opentelemetry import metrics

        meter = metrics.get_meter("mctl.commerce")
        self._counters = {
            INVOCATIONS: meter.create_counter(INVOCATIONS, unit="{invocation}",
                                              description="Paid capability invocations by terminal phase"),
            OUTCOME_UNKNOWN: meter.create_counter(OUTCOME_UNKNOWN, unit="{invocation}",
                                                  description="Paid invocations left with an unknown outcome"),
        }
        self._histogram = meter.create_histogram(DURATION, unit="s",
                                                 description="Wall time of one paid capability_invoke")

    def add(self, name: str, value: int, attributes: Mapping[str, str]) -> None:
        self._counters[name].add(value, dict(attributes))

    def record(self, name: str, value: float, attributes: Mapping[str, str]) -> None:
        self._histogram.record(value, dict(attributes))


_backend: Backend | None = None
_resolved = False


def set_backend(backend: Backend | None) -> None:
    """Install `backend` (tests, or an operator harness). None restores the
    default: OpenTelemetry when tracing is on, otherwise nothing."""
    global _backend, _resolved
    _backend, _resolved = backend, backend is not None


def _current() -> Backend | None:
    global _backend, _resolved
    if _resolved:
        return _backend
    if not tracing.enabled():
        return None  # not cached: tracing may be initialised later
    try:
        _backend = _OtelBackend()
    except Exception as exc:  # noqa: BLE001 — metrics are optional
        logger.warning("commerce metrics: OpenTelemetry metrics unavailable (%s)", type(exc).__name__)
        _backend = None
    _resolved = True
    return _backend


def record_invocation(
    *, admitted_providers: Collection[str], phase: Any, reason_code: Any, price_mode: Any, funds_mode: Any,
    provider_id: Any, duration_s: float,
) -> None:
    backend = _current()
    if backend is None:
        return
    try:
        attrs = attributes_for(admitted_providers=admitted_providers, phase=phase, reason_code=reason_code,
                               price_mode=price_mode, funds_mode=funds_mode, provider_id=provider_id)
        backend.add(INVOCATIONS, 1, attrs)
        backend.record(DURATION, max(0.0, float(duration_s)), {
            k: attrs[k] for k in ("phase", "price_mode", "provider_id")})
    except Exception as exc:  # noqa: BLE001 — a metric never fails a charge
        logger.debug("commerce metrics: dropped a data point (%s)", type(exc).__name__)


def record_outcome_unknown(*, admitted_providers: Collection[str], provider_id: Any) -> None:
    backend = _current()
    if backend is None:
        return
    try:
        attrs = attributes_for(admitted_providers=admitted_providers, provider_id=provider_id)
        backend.add(OUTCOME_UNKNOWN, 1, {"provider_id": attrs["provider_id"]})
    except Exception as exc:  # noqa: BLE001
        logger.debug("commerce metrics: dropped a data point (%s)", type(exc).__name__)
