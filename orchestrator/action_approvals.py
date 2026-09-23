"""Durable, single-use action approvals backed by mctl-api (mctl-api#366,
mctlhq/mctl-agents#197/#198, docs/adr/014-policy-checkpoint.md).

mctl-api is the approval authority. An `ActionApprovalRequest` binds one
hashed side effect of one execution; a human decides it in mctl-api; the
requesting service spends it with one atomic consume. This module is the
mctl-agents side:

- `intent_hash()` reproduces mctl-api's `workitems.IntentHash` byte for
  byte, so the orchestrator recomputes the intent of the action it is about
  to perform and compares it with the one the human approved.
- `ActionApprovalClient` talks to the create / get / consume routes and
  classifies every answer into a typed status. Transport errors, 5xx, 408,
  429 and malformed answers are UNKNOWN; typed 4xx codes keep their meaning.
- `MctlApiApprovals` is the `policy_checkpoint.ApprovalLookup` backed by it.
  It answers GRANTED only after it has consumed, in the same call, an
  approved and unexpired receipt whose stored intent hash equals the freshly
  recomputed one. It never consumes anything else.

Stdlib-only, like `policy_checkpoint`, so every caller of the checkpoint can
load it. Enabled only by `MCTL_POLICY_APPROVALS=mctl-api`.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from orchestrator import policy_checkpoint as pc

DEFAULT_TIMEOUT_S = 10

ROUTES = {
    "create": "/api/v1/action-approvals",
    "get": "/api/v1/action-approvals/{id}",
    "consume": "/api/v1/action-approvals/{id}/consume",
}

ID_PREFIX = "aar_"
SCHEMA_VERSION = "actionapproval/v1"
INTENT_ENCODING_VERSION = "mctl-action-intent/v1"

#: How long a NEW request stays decidable. mctl-api caps it at 7 days; a
#: stored request keeps its own expiry whatever a retry sends.
TTL_ENV = "MCTL_POLICY_APPROVAL_TTL_S"
DEFAULT_TTL_S = 24 * 3600
MIN_TTL_S = 60
MAX_TTL_S = 7 * 24 * 3600 - 300

# Typed answers.
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
CONSUMED = "consumed"
#: This consume call spent the receipt. Distinct from CONSUMED (it was
#: already spent), which is a refusal.
SPENT = "spent"
MISMATCH = "mismatch"
NOT_FOUND = "not_found"
REFUSED = "refused"
UNKNOWN = "unknown"

_STATES = {"pending": PENDING, "approved": APPROVED, "denied": DENIED, "expired": EXPIRED, "consumed": CONSUMED}

# mctl-api's typed refusal codes (internal/api/handlers_action_approvals.go).
_CODES = {
    "approval_not_approved": PENDING,
    "approval_denied": DENIED,
    "approval_expired": EXPIRED,
    "approval_intent_mismatch": MISMATCH,
    "approval_consumed": CONSUMED,
    "approval_not_found": NOT_FOUND,
}

#: 4xx statuses that mean "try later", not "no".
_TRANSIENT_4XX = frozenset({408, 425, 429})


# ---------------------------------------------------------------------------
# The intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionIntent:
    """What an approval binds: the fields mctl-api's intent_hash covers, in
    its order."""

    execution_id: str
    action_kind: str
    target: str
    args_digest: str
    policy_rule_id: str
    policy_version: str
    artifact_hash: str = ""
    work_item_id: str = ""

    def fields(self) -> tuple[tuple[str, str], ...]:
        return (
            ("execution_id", self.execution_id),
            ("action_kind", self.action_kind),
            ("target", self.target),
            ("args_digest", self.args_digest),
            ("policy_rule_id", self.policy_rule_id),
            ("policy_version", self.policy_version),
            ("artifact_hash", self.artifact_hash),
            ("work_item_id", self.work_item_id),
        )


def intent_hash(intent: ActionIntent) -> str:
    """mctl-api's `IntentHash`: sha256 over `mctl-action-intent/v1\\n` then
    `<field>:<byte length>:<value>\\n` per field, as `sha256:<hex>`. The
    length is the UTF-8 byte length, as Go's `len` of a string."""
    out = bytearray(INTENT_ENCODING_VERSION.encode() + b"\n")
    for name, value in intent.fields():
        raw = value.encode("utf-8")
        out += name.encode() + b":" + str(len(raw)).encode() + b":" + raw + b"\n"
    return "sha256:" + hashlib.sha256(bytes(out)).hexdigest()


def intent_for(request: pc.ActionRequest, *, rule_id: str, policy_version: str) -> ActionIntent:
    """The intent of one checkpoint request.

    - `action_kind` is `<kind>:<operation>`, what the human reads.
    - `artifact_hash` is the checkpoint's own action digest, which also
      covers the actor. The approval therefore binds at least everything
      the pre-#366 digest binding did.
    - `work_item_id` stays empty in this slice: mctl-api refuses a request
      naming a work item it does not hold."""
    return ActionIntent(
        execution_id=request.execution_id,
        action_kind=f"{request.action_kind}:{request.operation}",
        target=request.target,
        args_digest=request.args_digest,
        policy_rule_id=rule_id,
        policy_version=policy_version,
        artifact_hash=request.action_digest(),
    )


def idempotency_key(intent: ActionIntent, attempt: int = 0) -> str:
    """Deterministic: the same intent (and attempt) is the same key, so a
    retry finds its request instead of opening a second one; a different
    intent is a different key. mctl-api answers a replayed key with the
    stored request whatever its state, so once that request is denied,
    expired or consumed the same intent needs a new `attempt` (a new human
    decision) to be asked again. This slice always uses attempt 0."""
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    return f"mctl-agents/intent/{intent_hash(intent).removeprefix('sha256:')}/{attempt}"


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    state: str
    intent_hash: str
    expires_at: str
    decided_by: str = ""


@dataclass(frozen=True)
class ApprovalAnswer:
    status: str
    record: ApprovalRecord | None = None
    code: str = ""
    reason: str = ""


class ApprovalStoreUnavailable(RuntimeError):
    """Raised by the transport only; every public method turns it into UNKNOWN."""


def _api_base() -> str:
    return os.environ.get("MCTL_API_BASE_URL", "https://api.mctl.ai").rstrip("/")


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """A 3xx is an error, never followed: a misconfigured base URL must not
    answer approval questions from somewhere else."""

    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirects)


def _record_of(payload: dict[str, Any]) -> ApprovalRecord | None:
    raw = payload.get("approval")
    if payload.get("schema_version") != SCHEMA_VERSION or not isinstance(raw, dict):
        return None
    rid, state, digest, expires = raw.get("id"), raw.get("state"), raw.get("intent_hash"), raw.get("expires_at")
    if not (isinstance(rid, str) and rid.startswith(ID_PREFIX) and len(rid) > len(ID_PREFIX)):
        return None
    if state not in _STATES or not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    if not isinstance(expires, str):
        return None
    decided_by = raw.get("decided_by")
    return ApprovalRecord(rid, state, digest, expires, decided_by if isinstance(decided_by, str) else "")


def _refusal(status: int, payload: dict[str, Any]) -> ApprovalAnswer:
    raw_code = payload.get("code")
    code = raw_code if isinstance(raw_code, str) else ""
    reason = f"HTTP {status} {code}".strip()
    if status >= 500 or status in _TRANSIENT_4XX or status < 400:
        return ApprovalAnswer(UNKNOWN, code=code, reason=reason)
    typed = _CODES.get(code)
    if typed is not None and ((status == 404) == (typed == NOT_FOUND)):
        return ApprovalAnswer(typed, code=code, reason=reason)
    return ApprovalAnswer(REFUSED, code=code, reason=reason)


class ActionApprovalClient:
    """Synchronous client for mctl-api's action-approval routes, with the
    same auth and transport rules as `work_context/client.py`: `MCTL_TOKEN`
    bearer, https only, no redirects, uncertainty as a value."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        self._base = (base_url or _api_base()).rstrip("/")
        self._token = token if token is not None else os.environ.get("MCTL_TOKEN", "").strip()
        self._timeout = timeout
        self._opener = _no_redirect_opener()

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        if not self._token:
            raise ApprovalStoreUnavailable("MCTL_TOKEN is not set")
        url = f"{self._base}{path}"
        if not url.startswith("https://"):
            raise ApprovalStoreUnavailable(f"refusing non-https MCTL_API_BASE_URL: {self._base}")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — scheme checked above
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                body = resp.read()
                status = resp.getcode() or 200
        except urllib.error.HTTPError as exc:
            try:
                parsed = json.loads(exc.read() or b"{}")
            except Exception:  # noqa: BLE001 — an unreadable error body is still that error
                parsed = {}
            return exc.code, parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            raise ApprovalStoreUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            parsed = json.loads(body or b"{}")
        except ValueError as exc:
            raise ApprovalStoreUnavailable(f"malformed response: {exc}") from exc
        return status, parsed if isinstance(parsed, dict) else {}

    def _read(self, method: str, path: str, payload: dict[str, Any] | None, ok: tuple[int, ...]) -> ApprovalAnswer:
        try:
            status, body = self._request(method, path, payload)
        except ApprovalStoreUnavailable as exc:
            return ApprovalAnswer(UNKNOWN, reason=str(exc))
        if status not in ok:
            return _refusal(status, body)
        record = _record_of(body)
        if record is None:
            return ApprovalAnswer(UNKNOWN, reason=f"HTTP {status} does not describe an action approval")
        return ApprovalAnswer(_STATES[record.state], record=record)

    def create(self, intent: ActionIntent, *, key: str, expires_at: datetime) -> ApprovalAnswer:
        """Create, or find by `key`, the request for `intent`. Sends the
        locally computed intent_hash, so mctl-api refuses (400
        `intent_hash_mismatch`) if the two encodings ever disagree."""
        optional = ("artifact_hash", "work_item_id")
        body: dict[str, Any] = {name: value for name, value in intent.fields() if value or name not in optional}
        body.update({
            "idempotency_key": key,
            "expires_at": expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "intent_hash": intent_hash(intent),
        })
        return self._read("POST", ROUTES["create"], body, ok=(200, 201))

    def get(self, approval_id: str) -> ApprovalAnswer:
        answer = self._read("GET", ROUTES["get"].format(id=quote(approval_id, safe="")), None, ok=(200,))
        if answer.record is not None and answer.record.id != approval_id:
            return ApprovalAnswer(UNKNOWN, reason=f"asked for {approval_id}, the store answered {answer.record.id}")
        return answer

    def consume(self, approval_id: str, presented_hash: str) -> ApprovalAnswer:
        """Spend `approval_id` for `presented_hash`. SPENT only when the
        store answers 200 with exactly that receipt, now consumed, for
        exactly that intent; anything else is not a spend."""
        answer = self._read("POST", ROUTES["consume"].format(id=quote(approval_id, safe="")),
                            {"intent_hash": presented_hash}, ok=(200,))
        rec = answer.record
        if rec is None:
            return answer
        if rec.id != approval_id or rec.state != "consumed" or rec.intent_hash != presented_hash:
            return ApprovalAnswer(UNKNOWN, record=rec, reason="consume answer does not describe this spend")
        return ApprovalAnswer(SPENT, record=rec)


# ---------------------------------------------------------------------------
# The ApprovalLookup
# ---------------------------------------------------------------------------

_OUTCOMES = {
    PENDING: pc.APPROVAL_PENDING,
    DENIED: pc.APPROVAL_DENIED,
    EXPIRED: pc.APPROVAL_EXPIRED,
    CONSUMED: pc.APPROVAL_CONSUMED,
    MISMATCH: pc.APPROVAL_MISMATCH,
    NOT_FOUND: pc.APPROVAL_REFUSED,
    REFUSED: pc.APPROVAL_REFUSED,
    UNKNOWN: pc.APPROVAL_UNKNOWN,
}


def _outcome(answer: ApprovalAnswer, ref: str = "") -> pc.ApprovalOutcome:
    status = _OUTCOMES.get(answer.status, pc.APPROVAL_UNKNOWN)
    return pc.ApprovalOutcome(status, approval_ref=ref, reason=answer.reason or answer.code)


def _ttl_s() -> int:
    try:
        ttl = int(os.environ.get(TTL_ENV, "") or DEFAULT_TTL_S)
    except ValueError:
        ttl = DEFAULT_TTL_S
    return min(max(ttl, MIN_TTL_S), MAX_TTL_S)


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class MctlApiApprovals:
    """`ApprovalLookup` backed by mctl-api's ActionApprovalRequest.

    `redeem` recomputes the intent of the action about to run and then:

    1. finds its receipt: the one named by `approval_ref`, or else the one
       its deterministic idempotency key names (created pending if none);
    2. refuses unless the receipt's stored intent hash equals the fresh one
       (MISMATCH, nothing consumed);
    3. refuses unless the receipt is approved and unexpired (PENDING,
       DENIED, EXPIRED, CONSUMED, nothing consumed);
    4. consumes it for the fresh hash, and answers GRANTED only when mctl-api
       confirms this call spent it. A refused or uncertain consume is a
       refusal, so the side effect does not run.
    """

    def __init__(
        self, client: ActionApprovalClient | None = None, *, now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client or ActionApprovalClient()
        self._now = now or (lambda: datetime.now(UTC))

    def redeem(
        self, request: pc.ActionRequest, *, rule_id: str, policy_version: str, approval_ref: str = "",
    ) -> pc.ApprovalOutcome:
        if not request.execution_id:
            # mctl-api binds an approval to one execution: without the sealed
            # #196 context there is nothing to bind it to.
            return pc.ApprovalOutcome(pc.APPROVAL_REFUSED, reason="no execution identity to bind an approval to")
        intent = intent_for(request, rule_id=rule_id, policy_version=policy_version)
        fresh = intent_hash(intent)
        if approval_ref:
            answer = self._client.get(approval_ref)
        else:
            answer = self._client.create(
                intent, key=idempotency_key(intent), expires_at=self._now() + timedelta(seconds=_ttl_s()),
            )
        rec = answer.record
        if rec is None:
            return _outcome(answer, approval_ref)
        if rec.intent_hash != fresh:
            return pc.ApprovalOutcome(
                pc.APPROVAL_MISMATCH, approval_ref=rec.id,
                reason=f"receipt is bound to {rec.intent_hash}, this action is {fresh}",
            )
        if answer.status != APPROVED:
            return _outcome(answer, rec.id)
        expires = _parse_time(rec.expires_at)
        if expires is None:
            return pc.ApprovalOutcome(pc.APPROVAL_UNKNOWN, approval_ref=rec.id, reason="unreadable expires_at")
        if expires <= self._now():
            return pc.ApprovalOutcome(pc.APPROVAL_EXPIRED, approval_ref=rec.id, reason=f"expired at {rec.expires_at}")
        spent = self._client.consume(rec.id, fresh)
        if spent.status != SPENT:
            return _outcome(spent, rec.id)
        return pc.ApprovalOutcome(pc.APPROVAL_GRANTED, approval_ref=rec.id,
                                  reason=f"approved by {rec.decided_by or 'a human'}")
