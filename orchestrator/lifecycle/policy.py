"""Who SHOULD own a service's PRs, and who decides its merges.

This is deliberately separate from `client.py`, which answers who DOES own a
particular entity right now. The distinction is the one the current codebase
does not draw: `SHEPHERD_SKIP_SERVICES` is a statement of policy that has been
read as a statement of state, so "this repo belongs to another lifecycle" and
"another actor is currently driving this PR" are answered by the same list.

Policy survives the migration. State moves to the ownership record. The policy
answer is recorded as `policy_ref` on every acquire, so "why does this actor
own it" is answerable from the row instead of by re-deriving the environment
that produced it.

The functions here are thin wrappers over `run_shepherd`'s existing constants
rather than a second copy of them. A second copy would drift, and the whole
point of this package is to stop having two answers to one question.
"""
from __future__ import annotations

from orchestrator.lifecycle.contract import (
    OWNER_HUMAN_CODEOWNER,
    OWNER_PR_STEWARD,
    OWNER_SHEPHERD,
)


def default_owner_for(service: str) -> str:
    """The actor that should own review-remediation for ``service``.

    A service the shepherd skips entirely is owned by another PR lifecycle —
    today that is `mctl-claude-remote`'s pr-steward. A fix-only service is
    owned by the shepherd for the FIX stage; merge authority is a separate
    question answered by `merge_authority_for`, and conflating the two is what
    left steward-owned repositories with no path from a review finding back to
    code (mctlhq/mctl-agents#292).
    """
    from orchestrator import run_shepherd

    mode = run_shepherd._service_mode(service)
    if mode == run_shepherd.SKIP:
        return OWNER_PR_STEWARD
    return OWNER_SHEPHERD


def merge_authority_for(service: str) -> str:
    """Who may merge ``service``'s agent PRs.

    Independent of `default_owner_for` on purpose. Ownership says who is
    responsible for advancing the entity; this says who is permitted to perform
    one specific consequential action. An owner that inherited merge authority
    by being an owner is precisely the misreading mctlhq/mctl-agents#344
    records a reviewer making about `merge_owner`.

    This is descriptive routing, not an authorization grant: GitHub branch
    protection and CODEOWNERS remain authoritative, and are re-evaluated at the
    action boundary regardless of what this returns.
    """
    from orchestrator import run_shepherd

    # Delegated, not re-derived. run_shepherd._merge_owner_for is the existing
    # answer and this module's whole purpose is to stop there being two of
    # them; a second copy of the NEVER_MERGE_SERVICES rule here would drift
    # from the one merge_pr actually enforces.
    #
    # The one thing added on top is the FULL case, which that function does not
    # express because it is only ever called on a deferred merge: a service the
    # shepherd merges itself is owned by the shepherd, not handed anywhere.
    if run_shepherd._service_mode(service) == run_shepherd.FULL:
        return OWNER_SHEPHERD
    owner = run_shepherd._merge_owner_for(service)
    return OWNER_HUMAN_CODEOWNER if owner == "human-codeowner" else OWNER_PR_STEWARD


def policy_ref_for(service: str) -> str:
    """A short, auditable statement of which policy produced the answer.

    Stored on the ownership record so an operator can see the reason without
    reconstructing the environment the deciding process ran in — which today is
    a CWFT env var in another repository.
    """
    from orchestrator import run_shepherd

    return f"service-mode:{service}={run_shepherd._service_mode(service)}"
