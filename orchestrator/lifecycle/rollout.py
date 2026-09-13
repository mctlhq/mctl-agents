"""Which answer decides: the old mechanism, or the ownership store.

ADR-010 §12 describes the migration as four stages, and until now those stages
existed only in the ADR. The writers shipped behind
``workflow.patched("lifecycle-ownership")``, which is a HISTORY marker — it
answers "may this execution add these commands to its history", never "should
this deployment trust the new answer". Using it as a rollout control would mean
the only way to change the rollout is to deploy a new workflow patch.

Three switches exist in this area and each has exactly one job:

===========================================  ==========================================
``workflow.patched("lifecycle-ownership")``  History compatibility. Permanent, never
                                             removed, never a rollout control. Read in
                                             ``dev_loop.py`` and nowhere else.
``LIFECYCLE_ROLLOUT_MODE``                   The rollout control. Read in this module
                                             and nowhere else.
``LIFECYCLE_OWNERSHIP_REQUIRED``             Break-glass INSIDE enforce/only. Read
                                             through ``blocks_on_unknown()`` and
                                             nowhere else.
===========================================  ==========================================

Kept out of ``policy.py`` deliberately: policy answers "who SHOULD own this"
as a pure function of the service, and both the bootstrap and the shepherd call
it. Putting an environment read behind ``default_owner_for`` would make the
answer depend on where it was asked.
"""
from __future__ import annotations

import os

from orchestrator.lifecycle.client import ownership_required

#: Nothing is written and nothing is read. Break-glass, not a steady state:
#: the DevLoop still schedules its ownership activity on the usual cadence, so
#: the cost is the activity, not the write.
OFF = "off"

#: The new answer is computed and recorded alongside the old one, and the OLD
#: one still decides. This is the only stage that produces evidence: a stage
#: that writes without comparing measures elapsed time, not agreement.
OBSERVE = "observe"

#: The new answer may VETO the old one — it can stop an action the old
#: mechanism would have permitted — but cannot license one the old mechanism
#: forbids. The conservative direction, and the first stage where UNKNOWN has
#: consequences.
ENFORCE = "enforce"

#: The new answer decides outright. The old mechanism is no longer consulted.
ONLY = "only"

_ORDER = (OFF, OBSERVE, ENFORCE, ONLY)

ENV_VAR = "LIFECYCLE_ROLLOUT_MODE"


def mode() -> str:
    """The configured stage, defaulting to OFF.

    An unrecognised value answers OFF and warns rather than raising. A typo in
    a gitops env map must not crash the shepherd, and of the two ways to be
    wrong, refusing to act is the one that changes nothing — the same reasoning
    that makes UNKNOWN block rather than permit further down.
    """
    raw = os.environ.get(ENV_VAR, OFF).strip().lower()
    if raw in _ORDER:
        return raw
    if raw:
        print(
            f"lifecycle: unrecognised {ENV_VAR}={raw!r}; falling back to {OFF!r} "
            f"(expected one of {', '.join(_ORDER)})",
            flush=True,
        )
    return OFF


def at_least(stage: str) -> bool:
    """Is the configured mode at or past `stage`?"""
    if stage not in _ORDER:
        raise ValueError(f"unknown rollout stage: {stage!r}")
    return _ORDER.index(mode()) >= _ORDER.index(stage)


def records_writes() -> bool:
    """Should ownership be written to the store at all?"""
    return at_least(OBSERVE)


def computes_new_answer() -> bool:
    """Should the store be consulted and the two answers compared?

    True from OBSERVE up, including the stages where the new answer also
    decides: the comparison is what makes a divergence at enforce legible
    afterwards, and switching it off at the moment it starts mattering would
    leave the most consequential stage the least observable.
    """
    return at_least(OBSERVE)


def new_answer_may_veto() -> bool:
    """May the store STOP an action the old mechanism would have allowed?"""
    return at_least(ENFORCE)


def new_answer_decides() -> bool:
    """Is the store the only answer consulted?"""
    return at_least(ONLY)


def blocks_on_unknown() -> bool:
    """Does an unreachable store block a mutation?

    Two conditions, and the mode is the outer one. Below ENFORCE the new answer
    does not decide anything, so an UNKNOWN cannot block whatever
    ``LIFECYCLE_OWNERSHIP_REQUIRED`` is set to — which is why that variable is
    read only from here. It is the break-glass INSIDE enforce, not a second,
    competing rollout switch.
    """
    return new_answer_may_veto() and ownership_required()
