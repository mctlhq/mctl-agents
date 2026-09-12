"""Lifecycle ownership: who is responsible for advancing an entity, and who
may act on it right now.

See `docs/adr/010-lifecycle-ownership-contract.md`. The durable record lives in
mctl-api (Postgres), because it must survive both pod death AND the absence of
any workflow — the zero-owner case is precisely when no DevLoopWorkflow exists,
so Temporal state cannot be its home.

This package is a client and a policy resolver. It owns no state.
"""

from orchestrator.lifecycle.contract import (  # noqa: F401
    KIND_DEVLOOP_PROPOSAL,
    KIND_PULL_REQUEST,
    OWNER_DEVLOOP_WORKFLOW,
    OWNER_HUMAN_CODEOWNER,
    OWNER_PR_STEWARD,
    OWNER_RECONCILER,
    OWNER_SHEPHERD,
    PHASE_IMPLEMENT,
    PHASE_REVIEW_REMEDIATION,
    STATE_ACTIVE,
    STATE_HANDING_OFF,
    STATE_RELEASED,
    STATE_TERMINAL,
    EntityRef,
    Owner,
    Ownership,
    OwnershipAnswer,
)
