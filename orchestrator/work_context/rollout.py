"""Which answer decides: the issue URL, or the `WorkItem` store.

Mirrors `orchestrator/lifecycle/rollout.py`'s four-stage ladder and its
reasoning for keeping the switches separate:

=================================================  ==========================================
``workflow.patched("work-context-resume")``        History compatibility. Permanent, never a
                                                    rollout control. Read in
                                                    ``dev_loop.py`` and nowhere else.
``WORK_CONTEXT_ROLLOUT_MODE``                       The rollout control. Read in this module
                                                    and nowhere else.
``WORK_CONTEXT_REQUIRED``                           Break-glass INSIDE enforce/only. Read
                                                    through ``blocks_on_unknown()`` and
                                                    nowhere else.
=================================================  ==========================================

Default ``off``: nothing in this proposal calls the work-item store, and the
investigator behaves byte-for-byte as it does today, until an operator moves
this env var — which is what lets the change merge ahead of its two cross-repo
prerequisites (mctlhq/mctl-gitops#1279, mctlhq/mctl-api#335).
"""
from __future__ import annotations

import os

#: Nothing is written and nothing is read. The issue URL remains the only
#: source of task state; the new CLI flags parse and validate but never
#: reach the store.
OFF = "off"

#: The `WorkItem` is resolved, the `WorkContextRef` is sealed and the
#: reconstructed canonical state is logged — but the issue URL still decides.
#: This is the only stage that produces evidence for later stages.
OBSERVE = "observe"

#: The reconstructed canonical state may VETO a run (e.g. a work item in a
#: terminal state) but may never LICENSE one the issue path would refuse.
ENFORCE = "enforce"

#: The `WorkItem` is the sole source of canonical task state; `--issue-url`
#: becomes optional and is resolved from the work item instead.
ONLY = "only"

_ORDER = (OFF, OBSERVE, ENFORCE, ONLY)

ENV_VAR = "WORK_CONTEXT_ROLLOUT_MODE"

#: Break-glass INSIDE enforce/only: set false to restore fail-open behaviour
#: (an unreachable work-item store no longer blocks a mutating step) during
#: an mctl-api outage, without changing the mode.
REQUIRED_ENV_VAR = "WORK_CONTEXT_REQUIRED"


def mode() -> str:
    """The configured stage, defaulting to OFF.

    An unrecognised value answers OFF and warns rather than raising — a typo
    in a gitops env map must not crash the investigator, and refusing to act
    is the direction that changes nothing.
    """
    raw = os.environ.get(ENV_VAR, OFF).strip().lower()
    if raw in _ORDER:
        return raw
    if raw:
        print(
            f"warn: work_context: unrecognised {ENV_VAR}={raw!r}; falling back to {OFF!r} "
            f"(expected one of {', '.join(_ORDER)})",
            flush=True,
        )
    return OFF


def at_least(stage: str) -> bool:
    """Is the configured mode at or past `stage`?"""
    if stage not in _ORDER:
        raise ValueError(f"unknown rollout stage: {stage!r}")
    return _ORDER.index(mode()) >= _ORDER.index(stage)


def computes_new_answer() -> bool:
    """Should the work-item store be consulted at all?"""
    return at_least(OBSERVE)


def new_answer_may_veto() -> bool:
    """May the reconstructed canonical state STOP a run the issue path would
    have allowed?"""
    return at_least(ENFORCE)


def new_answer_decides() -> bool:
    """Is the work item the only source of canonical task state consulted?"""
    return at_least(ONLY)


def work_context_required() -> bool:
    return os.environ.get(REQUIRED_ENV_VAR, "true").strip().lower() not in {"false", "no", "0", "off"}


def blocks_on_unknown() -> bool:
    """Does an unreachable work-item store block a mutating step?

    Two conditions, the mode is the outer one: below ENFORCE the new answer
    does not decide anything, so an UNKNOWN cannot block whatever
    WORK_CONTEXT_REQUIRED is set to.
    """
    return new_answer_may_veto() and work_context_required()
