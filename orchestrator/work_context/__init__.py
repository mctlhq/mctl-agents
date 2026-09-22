"""Client-side `WorkItem` contract (mctl-api#227): who is asking to resume
what, on which surface, as whom.

See `docs/adr/011-work-item-resume-contract.md`. mctl-api owns the durable
record; this package is a tolerant client mirror plus a staged rollout
switch, exactly like `orchestrator/lifecycle/` is for ownership. It owns no
state and performs no I/O of its own beyond `client.py`'s HTTP calls.
"""

from orchestrator.work_context.contract import (  # noqa: F401
    ACTOR_KINDS,
    SURFACE_KINDS,
    TERMINAL_WORK_ITEM_STATES,
    WORK_ITEM_ABSENT,
    WORK_ITEM_CONFLICT,
    WORK_ITEM_FOUND,
    WORK_ITEM_STATES,
    WORK_ITEM_UNKNOWN,
    ActorRef,
    CanonicalState,
    ExecutionRef,
    SurfaceRef,
    WorkItem,
    WorkItemAnswer,
    WorkItemRef,
    execution_id_for,
    reconstruct_canonical_state,
    work_context_params,
    work_item_verdict_for,
)
