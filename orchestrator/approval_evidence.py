"""Execution authorization from the approval decision itself.

An accepted proposal's ``.status.yaml`` says who approved it and which content
digest, but a file in agents-state is only a claim: anything that can write
that tree can write an ``approval`` block. What a human actually decided lives
in mctl-api, as the DevLoop's ``aar_`` action approval: decided by a human
admin, bound to ``<service>/<slug>`` and to the proposal content digest, and
spent by the approve flip. The approve CWFT records that grant's id as
``approval.approval_ref``.

So nothing executes a proposal on the strength of the file. The implementer
reads the referenced grant back from mctl-api and requires all of:

- the grant exists, is a ``mctl-agents-approve`` grant, and the store answers
  for exactly that id;
- a human decided it (``github:`` / ``oidc:`` decider);
- it targets this ``<service>/<slug>``;
- its ``artifact_hash`` is the digest of the proposal files on disk now;
- it was spent by the approve flip (``consumed``), or already marked
  ``executed`` by an earlier attempt for this proposal.

A grant marked ``executed`` authorizes finishing that execution (opening the
PR for a result branch an earlier attempt pushed), never running the model
again: the caller passes ``allow_new_run=False`` on. Right before a PR is
opened the grant is marked ``executed`` in mctl-api, a single-use transition,
so one human decision yields one execution.

Uncertainty is never authorization and never a durable refusal: a store that
cannot be read answers ``transient`` and the attempt is retried later.

Stdlib-only, like ``action_approvals``, which it builds on.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orchestrator import action_approvals as aa

#: The grant kind the DevLoop asks for and the approve operation spends
#: (``orchestrator.temporal.activities.delegated_approval.APPROVE_ACTION_KIND``,
#: duplicated here because that module imports temporalio).
APPROVE_ACTION_KIND = "mctl-agents-approve"

#: mctl-api ``aar_`` ids, as the approve CWFT validates them.
GRANT_REF_RE = re.compile(r"aar_[A-Za-z0-9_-]{1,124}")

#: Decider namespaces that name a human (mctl-api ``approverName``).
HUMAN_DECIDER_PREFIXES = ("github:", "oidc:")

# Stable refusal codes, recorded in the ``blocked`` marker.
GRANT_MISSING = "approval-grant-missing"
GRANT_INVALID = "approval-grant-invalid"
GRANT_NOT_FOUND = "approval-grant-not-found"
GRANT_WRONG_KIND = "approval-grant-wrong-kind"
GRANT_NOT_HUMAN = "approval-grant-not-human"
GRANT_WRONG_TARGET = "approval-grant-wrong-target"
GRANT_CONTENT_MISMATCH = "approval-grant-content-mismatch"
GRANT_NOT_SPENT = "approval-grant-not-spent"
GRANT_REFUSED = "approval-grant-refused"
GRANT_ALREADY_EXECUTED = "approval-grant-already-executed"

GRANT_REFUSAL_CODES = frozenset({
    GRANT_MISSING,
    GRANT_INVALID,
    GRANT_NOT_FOUND,
    GRANT_WRONG_KIND,
    GRANT_NOT_HUMAN,
    GRANT_WRONG_TARGET,
    GRANT_CONTENT_MISMATCH,
    GRANT_NOT_SPENT,
    GRANT_REFUSED,
    GRANT_ALREADY_EXECUTED,
})


@dataclass(frozen=True)
class GrantVerdict:
    """The answer for one proposal. Exactly one of ``ok`` / ``code`` /
    ``transient`` describes it: ``ok`` authorizes, ``code`` is a durable
    refusal, ``transient`` means the store could not answer."""

    ok: bool
    grant_id: str = ""
    #: ``False`` when the grant was already marked ``executed``: the attempt
    #: may finish that execution but must not run the model again.
    allow_new_run: bool = False
    code: str = ""
    transient: bool = False
    detail: str = ""


def grant_target(service: str, slug: str) -> str:
    return f"{service}/{slug}"


def recorded_grant_ref(data: dict[str, Any]) -> str | None:
    """The ``approval.approval_ref`` a record carries, or ``None``."""
    approval = data.get("approval")
    if not isinstance(approval, dict):
        return None
    ref = approval.get("approval_ref")
    return ref.strip() if isinstance(ref, str) and ref.strip() else None


def _refuse(code: str, detail: str, grant_id: str = "") -> GrantVerdict:
    return GrantVerdict(ok=False, grant_id=grant_id, code=code, detail=detail)


def _transient(detail: str, grant_id: str = "") -> GrantVerdict:
    return GrantVerdict(ok=False, grant_id=grant_id, transient=True, detail=detail)


def verify_execution_grant(
    data: dict[str, Any],
    *,
    service: str,
    slug: str,
    current_digest: str,
    lookup: Callable[[str], aa.ApprovalAnswer],
) -> GrantVerdict:
    """Whether the human decision behind this record authorizes executing the
    proposal content that is on disk now (``current_digest``)."""
    ref = recorded_grant_ref(data)
    if ref is None:
        return _refuse(GRANT_MISSING, "the record names no approval grant (approval.approval_ref)")
    if not GRANT_REF_RE.fullmatch(ref):
        return _refuse(GRANT_INVALID, f"approval.approval_ref {ref!r} is not an aar_ id")
    if not current_digest.startswith("sha256:"):
        return _refuse(GRANT_CONTENT_MISMATCH, "the proposal content digest could not be computed", ref)

    answer = lookup(ref)
    if answer.status == aa.UNKNOWN:
        return _transient(f"approval grant {ref} could not be read: {answer.reason or 'unknown'}", ref)
    if answer.status == aa.NOT_FOUND:
        return _refuse(GRANT_NOT_FOUND, f"mctl-api holds no approval grant {ref} for this service", ref)
    rec = answer.record
    if rec is None:
        return _refuse(GRANT_REFUSED, f"approval grant {ref}: {answer.reason or answer.code or answer.status}", ref)
    if rec.id != ref:
        return _transient(f"asked for {ref}, the store answered {rec.id}", ref)
    if rec.action_kind != APPROVE_ACTION_KIND:
        return _refuse(GRANT_WRONG_KIND, f"approval grant {ref} is for {rec.action_kind or '<none>'}", ref)
    if not any(rec.decided_by.startswith(p) and len(rec.decided_by) > len(p) for p in HUMAN_DECIDER_PREFIXES):
        return _refuse(GRANT_NOT_HUMAN, f"approval grant {ref} has no human decider", ref)
    if rec.target != grant_target(service, slug):
        return _refuse(
            GRANT_WRONG_TARGET,
            f"approval grant {ref} targets {rec.target or '<none>'}, not {grant_target(service, slug)}",
            ref,
        )
    if rec.artifact_hash != current_digest:
        return _refuse(
            GRANT_CONTENT_MISMATCH,
            f"approval grant {ref} was decided for {rec.artifact_hash or '<none>'}; the files hash to {current_digest}",
            ref,
        )
    if rec.state == "consumed":
        return GrantVerdict(ok=True, grant_id=ref, allow_new_run=True)
    if rec.state == "executed":
        return GrantVerdict(ok=True, grant_id=ref, allow_new_run=False)
    # approved but never spent by the flip, pending, denied, expired: the
    # approve operation did not run on this decision.
    return _refuse(GRANT_NOT_SPENT, f"approval grant {ref} is {rec.state}, not spent by an approve flip", ref)


class GrantMarkError(RuntimeError):
    """The grant could not be marked executed; nothing may be opened."""


def mark_executed(
    grant_id: str,
    *,
    service: str,
    slug: str,
    artifact_hash: str,
    mark: Callable[[str, str, str], aa.ApprovalAnswer],
) -> None:
    """Mark ``grant_id`` executed for this proposal and content, or raise.

    A grant this proposal's earlier attempt already marked is accepted: it is
    the same decision finishing the same execution (the start gate refused
    a new model run on it). Any other answer, uncertainty included, raises."""
    answer = mark(grant_id, grant_target(service, slug), artifact_hash)
    if answer.status in (aa.EXECUTED_NOW, aa.EXECUTED):
        return
    raise GrantMarkError(
        f"approval grant {grant_id} could not be marked executed: "
        f"{answer.reason or answer.code or answer.status}"
    )
