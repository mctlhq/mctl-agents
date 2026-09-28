"""Which strategy decides: the env var, or the committed
`ContextStrategyBinding` (mctlhq/mctl-agents#472 Slice B, ADR 019 sec. 4).

Mirrors `orchestrator/work_context/rollout.py` and
`orchestrator/lifecycle/rollout.py`'s four-stage ladder and their reasoning
for keeping the switches separate. Three switches exist in this area and
each has exactly one job:

=================================================  ==========================================
``ISSUE_INVESTIGATOR_CONTEXT_MODE``                 Does assembly happen at all
                                                     (``off``/``shadow``/``on``). Read in
                                                     ``context_assembly.assemble_investigator_
                                                     context`` and nowhere else. Unrelated to
                                                     this ladder: at ``off`` no collector runs
                                                     and this module is never consulted,
                                                     whatever it is set to.
``CONTEXT_RELEASE_ROLLOUT_MODE``                    Which strategy decides. The rollout
                                                     control. Read in this module and
                                                     nowhere else.
``CONTEXT_RELEASE_REQUIRED``                        Break-glass INSIDE enforce/only. Read
                                                     through ``blocks_on_unknown()`` and
                                                     nowhere else.
=================================================  ==========================================

Default ``off``: no file under ``config/context-strategies/`` is opened,
neither ``orchestrator.context_release`` nor ``yaml`` is imported, and
`AssemblyConfig.from_env()` decides exactly as it does today — every
snapshot keeps its exact bytes and `snapshot_id`.

``OBSERVE_ENVIRONMENT`` is a constant, not `execution.environment`:
``observe`` always consults the ``shadow`` binding, the one candidate slot
ADR 019 sec. 2 allows with ``evidence.kind: none``, whatever
``AGENT_ENVIRONMENT`` holds. No manifest sets ``AGENT_ENVIRONMENT`` today, so
the production investigator's `execution.environment` is `"production"`,
which has no committed binding; relabelling it `"shadow"` to let it observe
would change the sealed `execution.environment` and every authoritative
`snapshot_id` it has ever produced. Operators must NEVER change
``AGENT_ENVIRONMENT`` to enable ``observe`` — set this module's own env var
instead. ``enforce``/``only`` consult `execution.environment` itself, so a
missing `production` binding is the fail-closed default at those stages
until a `production` binding exists (mctlhq/mctl-agents#528).

The closed-vocabulary reason codes a resolution can carry
(`orchestrator/context_assembly.py`'s `RELEASE_REASON_*`) are: the env var
decided (`off`/`observe`), the binding decided (`enforce`/`only`), the
binding could not be resolved and `observe` skipped it harmlessly, the
binding could not be resolved and `enforce`/`only` fell back to the default
strategy, or the `observe` shadow pass itself failed and was discarded.
"""
from __future__ import annotations

import os

#: The env var alone decides (`ISSUE_INVESTIGATOR_CONTEXT_STRATEGY`, via
#: `AssemblyConfig.strategy`). No binding is loaded; no catalog file is
#: opened; `context_release` is never imported.
OFF = "off"

#: The `shadow` binding is resolved and logged, and the bound strategy runs
#: as a second, non-authoritative `run_pipeline` pass over the same
#: candidate list — but the env var still decides what the model reads.
#: This is the only stage that produces comparison evidence for a later
#: promotion.
OBSERVE = "observe"

#: The resolved binding (for `execution.environment`) selects the
#: authoritative strategy. An unresolvable binding blocks when
#: `blocks_on_unknown()` holds, otherwise falls back to
#: `deterministic-fixed-order`.
ENFORCE = "enforce"

#: As `enforce`, and a set `ISSUE_INVESTIGATOR_CONTEXT_STRATEGY` is a hard
#: error: the env var can never silently shadow the binding.
ONLY = "only"

_ORDER = (OFF, OBSERVE, ENFORCE, ONLY)

ENV_VAR = "CONTEXT_RELEASE_ROLLOUT_MODE"

#: Break-glass INSIDE enforce/only: set false to make an unresolvable
#: binding non-blocking (falls back to the default strategy) during an
#: incident, without changing the rollout stage.
REQUIRED_ENV_VAR = "CONTEXT_RELEASE_REQUIRED"

#: The environment `observe` always resolves the binding for, regardless of
#: `AGENT_ENVIRONMENT`/`execution.environment`. See the module docstring.
OBSERVE_ENVIRONMENT = "shadow"


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
            f"warn: context_release: unrecognised {ENV_VAR}={raw!r}; falling back to {OFF!r} "
            f"(expected one of {', '.join(_ORDER)})",
            flush=True,
        )
    return OFF


def at_least(stage: str) -> bool:
    """Is the configured mode at or past `stage`?"""
    if stage not in _ORDER:
        raise ValueError(f"unknown rollout stage: {stage!r}")
    return _ORDER.index(mode()) >= _ORDER.index(stage)


def binding_is_observed() -> bool:
    """Should the binding be resolved and logged at all?"""
    return at_least(OBSERVE)


def binding_decides() -> bool:
    """Is the resolved binding the authoritative strategy?"""
    return at_least(ENFORCE)


def binding_is_sole_selector() -> bool:
    """Is a set `ISSUE_INVESTIGATOR_CONTEXT_STRATEGY` a hard error?"""
    return at_least(ONLY)


def context_release_required() -> bool:
    return os.environ.get(REQUIRED_ENV_VAR, "true").strip().lower() not in {"false", "no", "0", "off"}


def blocks_on_unknown() -> bool:
    """Does an unresolvable binding block a run?

    Two conditions, the mode is the outer one: below ENFORCE the binding
    does not decide anything, so an unresolved binding cannot block whatever
    CONTEXT_RELEASE_REQUIRED is set to.
    """
    return binding_decides() and context_release_required()
