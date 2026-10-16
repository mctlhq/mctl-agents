"""Blocking-only spec verification signal.

A SpecVerdict can say pass / fail / unknown and nothing else. It has no merge,
approve or authorize affordance: it is at most one more blocking input beside
CI and review for a future Shepherd argument. It never replaces them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orchestrator.sdd.apply import ApplyAbort, apply_change
from orchestrator.sdd.spec_model import GrammarError

OUTCOMES = ("pass", "fail", "unknown")


@dataclass(frozen=True)
class SpecVerdict:
    outcome: str
    findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {self.outcome!r}")


def verify_change(canonical: Mapping[str, str], deltas: Mapping[str, str], *, skip_specs: bool = False) -> SpecVerdict:
    """Dry-run the delta against canonical specs. No deltas and no skip -> unknown."""
    if not deltas:
        if skip_specs:
            return SpecVerdict("pass", ("skip_specs: no deltas expected",))
        return SpecVerdict("unknown", ("no spec deltas to verify",))
    try:
        result = apply_change(canonical, deltas)
    except (ApplyAbort, GrammarError) as exc:
        return SpecVerdict("fail", (str(exc),))
    return SpecVerdict("pass", tuple(result.warnings))
