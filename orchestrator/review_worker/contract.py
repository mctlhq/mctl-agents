"""Pure contract of the review-worker producer (mctlhq/mctl-agents#986).

No I/O. This module decides three things and nothing else:

- how an mctl-api answer is read (`classify`): a receipt is ``stored`` only
  for a 201 whose body says ``mode=worker``, ``assurance=independent`` and
  the submitted ``request_id``. A disabled route (501) and an answer that was
  not observed (5xx, transport, bad JSON, 401/408/425/429) are their own
  states: neither is ever ``stored``, and neither is "no receipt";
- whether the attached reviewer execution is independent of the authoring
  one (`check_independent`), before a token is minted for it;
- the exact worker submit body (`worker_body`): seven keys, no identity.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orchestrator import review_receipts as rr

AGENT_NAME = "review-worker"
SCOPE = "review"
PROFILE = "independent-review"
TOKEN_TTL_SECONDS = 900

STATE_STORED = "stored"
STATE_REFUSED = "refused"
STATE_DISABLED = "disabled"
STATE_UNOBSERVED = "unobserved"
STATES = (STATE_STORED, STATE_REFUSED, STATE_DISABLED, STATE_UNOBSERVED)

CODE_INGEST_DISABLED = "review_worker_ingest_disabled"
CODE_AUTHORING_UNRESOLVED = "review_authoring_unresolved"
CODE_NOT_INDEPENDENT = "review_reviewer_not_independent"
CODE_INELIGIBLE = "review_reviewer_ineligible"
CODE_EXECUTION_UNPROVEN = "review_execution_unproven"
CODE_NONCE_CONSUMED = "review_nonce_consumed"
CODE_REQUEST_CONFLICT = "review_request_conflict"
#: Our own codes (not mctl-api's) for an answer that is not what was asked.
CODE_RESPONSE_UNVERIFIED = "review_response_unverified"
CODE_TOKEN_UNVERIFIED = "review_token_unverified"  # noqa: S105 - a refusal code
CODE_RECEIPT_UNOBSERVED = "review_receipt_unobserved"
CODE_SUBJECT_MISMATCH = "review_subject_digest_mismatch"
CODE_VERDICT_MALFORMED = "review_verdict_malformed"

#: Every refusal mctl-api documents for the review routes (#897, #917) and
#: the Tier A codes, with the status mctl-api answers (0 = not fixed). Used
#: for documentation and tests; an unknown 4xx code is still ``refused``.
KNOWN_REFUSALS: Mapping[str, int] = {
    "review_nonce_consumed": 409,
    "review_nonce_mismatch": 403,
    "review_reviewer_ineligible": 403,
    "review_reviewer_not_independent": 409,
    "review_run_token_required": 403,
    "review_request_mode_manual": 409,
    "review_request_closed": 409,
    "review_request_not_found": 404,
    "review_binding_mismatch": 409,
    "review_execution_unproven": 403,
    "review_authoring_unresolved": 0,
    "review_authoring_unproven": 422,
    "review_request_conflict": 409,
    "review_not_permitted": 403,
    "agent_unknown": 400,
    rr.CODE_PASS_WITH_UNOBSERVED: 422,
    rr.CODE_UNOBSERVED_OUTCOME: 422,
    rr.CODE_RUBRIC_MISMATCH: 422,
    rr.CODE_MALFORMED: 400,
    rr.CODE_UNKNOWN_KEY: 400,
    rr.CODE_BAD_SUBJECT: 422,
    rr.CODE_BAD_IDENTITY: 422,
}

#: Typed codes that say the store gave no usable answer: never a refusal.
UNOBSERVED_CODES = frozenset({
    "review_store_unavailable",
    "review_provenance_unavailable",
    "review_principal_unavailable",
    "review_evidence_unavailable",
    "agent_run_tokens_unavailable",
    "execution_provenance_unavailable",
    "work_items_unavailable",
})

#: 4xx statuses that say nothing about the receipt.
_UNCERTAIN_4XX = frozenset({401, 408, 425, 429})
_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_RECEIPT_ID_RE = re.compile(r"rc_[0-9a-f]{16}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class Outcome:
    """The end state of a step or of the run. Only ``stored`` carries a
    receipt; ``disabled`` and ``unobserved`` are distinct from ``refused``."""

    state: str
    code: str = ""
    http_status: int = 0
    receipt_id: str = ""
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        if (self.state == STATE_STORED) != bool(self.receipt_id and self.content_hash):
            raise ValueError("exactly a stored outcome carries a receipt id and its content hash")

    @property
    def stored(self) -> bool:
        return self.state == STATE_STORED

    @property
    def label(self) -> str:
        """The outcome code the log line and the exit name."""
        return self.code or self.state


class Stop(Exception):
    """A step ended the run before a receipt could exist."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(f"{outcome.state} {outcome.code} (HTTP {outcome.http_status})")
        self.outcome = outcome


def refused(code: str, status: int = 0) -> Outcome:
    return Outcome(STATE_REFUSED, code, status)


def unobserved(code: str, status: int = 0) -> Outcome:
    return Outcome(STATE_UNOBSERVED, code, status)


def code_of(payload: Any) -> str:
    """mctl-api's typed ``code``, or "" (free text is never kept)."""
    code = payload.get("code") if isinstance(payload, Mapping) else None
    return code if isinstance(code, str) and _CODE_RE.fullmatch(code) else ""


def classify_failure(status: int, payload: Any) -> Outcome:
    """A non-success answer. ``status`` 0 is a transport error or a body that
    could not be read."""
    code = code_of(payload)
    if status == 501 and code == CODE_INGEST_DISABLED:
        return Outcome(STATE_DISABLED, CODE_INGEST_DISABLED, status)
    if status <= 0 or status >= 500 or status < 400 or status in _UNCERTAIN_4XX or code in UNOBSERVED_CODES:
        return unobserved(code or (f"http_{status}" if status else "transport_error"), status)
    return refused(code or f"http_{status}", status)


def classify(status: int, payload: Any, *, request_id: str) -> Outcome:
    """Read the answer to ``POST /api/v1/review-receipts``."""
    if status == 201:
        body = payload if isinstance(payload, Mapping) else {}
        receipt = body.get("receipt")
        receipt = receipt if isinstance(receipt, Mapping) else {}
        receipt_id, content_hash = body.get("receipt_id"), receipt.get("content_hash")
        if (
            body.get("mode") == "worker"
            and body.get("assurance") == "independent"
            and request_id
            and body.get("request_id") == request_id
            and isinstance(receipt_id, str)
            and _RECEIPT_ID_RE.fullmatch(receipt_id)
            and isinstance(content_hash, str)
            and _DIGEST_RE.fullmatch(content_hash)
        ):
            return Outcome(STATE_STORED, "", status, receipt_id, content_hash)
        # A 2xx that does not describe this worker receipt proves nothing.
        return unobserved(CODE_RESPONSE_UNVERIFIED, status)
    return classify_failure(status, payload)


@dataclass(frozen=True)
class Provenance:
    """What mctl-api recorded for one execution."""

    execution_id: str
    attempt_id: str
    lineage_root: str
    attached_direct_service: bool


def check_independent(request: rr.ReviewRequest, reviewer: Provenance) -> str | None:
    """The refusal code when ``reviewer`` may not review ``request``, else
    None. Mirrors mctl-api's order (eligibility, then independence) so that a
    mis-attached execution fails here, before a token is minted and a nonce
    is spent. Unprovable facts are not independent."""
    if reviewer.attached_direct_service is not True:
        return CODE_INELIGIBLE
    if not reviewer.execution_id or not reviewer.attempt_id or not reviewer.lineage_root:
        return CODE_EXECUTION_UNPROVEN
    if reviewer.execution_id == request.authoring_execution_id:
        return CODE_NOT_INDEPENDENT
    if reviewer.attempt_id in request.authoring_attempt_ids:
        return CODE_NOT_INDEPENDENT
    if reviewer.lineage_root == request.authoring_lineage_root:
        return CODE_NOT_INDEPENDENT
    return None


WORKER_BODY_KEYS = ("request_id", "subject", "nonce", "outcome", "criteria", "findings_ref", "evidence_ref")


def worker_body(request: rr.ReviewRequest, draft: rr.ReviewReceipt) -> dict[str, Any]:
    """The worker submit body: exactly ``WORKER_BODY_KEYS``. No identity
    field is sent; mctl-api fills the reviewer's from the run token."""
    if draft.request_id != request.request_id or draft.nonce != request.nonce:
        raise ValueError("the draft receipt answers a different request")
    return {
        "request_id": request.request_id,
        "subject": request.subject.to_dict(),
        "nonce": request.nonce,
        "outcome": draft.outcome,
        "criteria": [{"id": c.id, "status": c.status} for c in draft.criteria],
        "findings_ref": draft.findings_ref.to_dict(),
        "evidence_ref": draft.evidence_ref,
    }
