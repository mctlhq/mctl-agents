"""Capability commerce, the mctl-agents side (ADR 020,
docs/adr/020-capability-commerce-contract.md; mctlhq/mctl-agents#619, C4).

mctl-api owns every piece of commerce state (ADR 020 sec. 14). This module
holds no state of its own. It is:

- the byte-exact digests both implementations must agree on: `quote_digest`
  (sec. 5), `terms_binding` (sec. 8) and the dispatch authorization's signing
  message (sec. 9), each over the length-prefixed encoding mctl-api already
  uses for approval intents;
- `verify_dispatch_authorization`, which a paid provider's ingress runs
  before it does anything (sec. 9, requirement 1);
- `CommerceClient`, the HTTP client for mctl-api's commerce routes, with the
  same transport rules as `orchestrator/action_approvals.py`: `MCTL_TOKEN`
  bearer, https only, no redirects, and uncertainty returned as a value,
  never guessed into an answer.

Stdlib-only at import. `cryptography` is imported inside the two functions
that sign or verify, so loading this module never requires it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeGuard
from urllib.parse import quote, urlencode

QUOTE_ENCODING_VERSION = "mctl-commerce-quote/v1"
TERMS_ENCODING_VERSION = "mctl-commerce-terms/v1"
DISPATCH_AUTHORIZATION_VERSION = "mctl-commerce-dispatch/v1"
#: The authorization's wire `schema_version` (ADR 020: every record carries
#: the integer `schema_version: 1`). Distinct from
#: DISPATCH_AUTHORIZATION_VERSION, which names the signed encoding and is
#: never on the wire. mctl-api sends the integer (internal/commerce/dispatch.go
#: `SchemaVersion int`); reading it as the encoding string refused every live
#: authorization (mctlhq/mctl-agents#620 live proof, 2026-10-12).
DISPATCH_AUTHORIZATION_SCHEMA_VERSION = 1
DISPATCH_SIGNATURE_ALGORITHM = "ed25519"
#: ADR 020 sec. 9: `not_found` is definitive only this long after the
#: dispatch deadline.
MAX_CLOCK_SKEW_S = 60

#: The argument a paid call carries into the policy checkpoint so the
#: approval binds the price (ADR 020 sec. 8). A caller can never supply it.
TERMS_BINDING_ARGUMENT = "commerce_terms_binding"

#: ISO 4217 minor-unit exponents, for DISPLAY only (ADR 020 sec. 2): money
#: stays an integer count of minor units everywhere else.
MINOR_UNIT_EXPONENTS: Mapping[str, int] = {
    "EUR": 2, "USD": 2, "GBP": 2, "CHF": 2, "CAD": 2, "AUD": 2, "PLN": 2, "CZK": 2, "SEK": 2, "NOK": 2,
    "DKK": 2, "UAH": 2, "JPY": 0, "KRW": 0, "HUF": 2, "BHD": 3, "KWD": 3,
}


def price_display(currency: str, amount_minor: int) -> str:
    """`"EUR 0.12"`. Display only. An unlisted currency is shown in minor
    units (`"XYZ 12 minor"`) rather than guessing an exponent."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0:
        raise ValueError("amount_minor must be a non-negative integer")
    exponent = MINOR_UNIT_EXPONENTS.get(currency)
    if exponent is None:
        return f"{currency} {amount_minor} minor"
    if exponent == 0:
        return f"{currency} {amount_minor}"
    whole, frac = divmod(amount_minor, 10 ** exponent)
    return f"{currency} {whole}.{frac:0{exponent}d}"


IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_HASHED_TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: The dispatch authorization's signed fields, in signing order.
DISPATCH_SIGNED_FIELDS = (
    "key_id", "invocation_id", "provider_operation_key", "provider_id", "capability_id",
    "input_schema_hash", "args_digest", "tenant_id", "dispatch_deadline",
)


class CommerceError(ValueError):
    """A commerce document that cannot be used as it stands."""


class AuthorizationRejected(CommerceError):
    """A dispatch authorization that does not verify. The provider refuses
    the call, and nothing is recorded or executed."""


# ---------------------------------------------------------------------------
# Encodings
# ---------------------------------------------------------------------------


def length_prefixed(version: str, fields: tuple[tuple[str, str], ...]) -> bytes:
    """`<version>\\n` then `<field>:<byte length>:<value>\\n` per field: no
    escaping anywhere, so any client reproduces it byte for byte."""
    out = bytearray(version.encode() + b"\n")
    for name, value in fields:
        raw = value.encode()
        out += name.encode() + b":" + str(len(raw)).encode() + b":" + raw + b"\n"
    return bytes(out)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def hashed_time(value: datetime | str) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ`: UTC, whole seconds, a `Z`. Never
    `isoformat()`, which writes `+00:00` (ADR 020 sec. 5). A string is
    accepted only when it is already in exactly this form."""
    if isinstance(value, str):
        if not _HASHED_TIME_PATTERN.match(value):
            raise CommerceError(f"{value!r} is not a YYYY-MM-DDTHH:MM:SSZ time")
        return value
    if value.tzinfo is None:
        raise CommerceError("a hashed time must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime:
    """An RFC 3339 time from mctl-api. Anything else is a malformed answer."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CommerceError(f"unreadable time {value!r}") from exc
    if parsed.tzinfo is None:
        raise CommerceError(f"time {value!r} has no offset")
    return parsed.astimezone(UTC)


def _int_text(value: Any, *, where: str) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CommerceError(f"{where} must be a non-negative integer")
    return str(value)


def _money(doc: Mapping[str, Any], *, where: str) -> tuple[str, int]:
    price = doc.get("price")
    if not isinstance(price, Mapping):
        raise CommerceError(f"{where}.price is missing")
    currency, amount = price.get("currency"), price.get("amount_minor")
    if not isinstance(currency, str):
        raise CommerceError(f"{where}.price.currency is missing")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise CommerceError(f"{where}.price.amount_minor must be a non-negative integer")
    _int_text(amount, where=f"{where}.price.amount_minor")
    return currency, amount


def _text(doc: Mapping[str, Any], name: str, *, where: str) -> str:
    value = doc.get(name, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CommerceError(f"{where}.{name} must be a string")
    return value


def quote_digest(quote_doc: Mapping[str, Any]) -> str:
    """ADR 020 sec. 5, reproduced from a quote as mctl-api serializes it.
    The caller compares it with the quote's own `quote_digest` before it
    accepts anything."""
    w = "quote"
    currency, amount = _money(quote_doc, where=w)
    return _digest(length_prefixed(QUOTE_ENCODING_VERSION, (
        ("quote_id", _text(quote_doc, "quote_id", where=w)),
        ("terms_id", _text(quote_doc, "terms_id", where=w)),
        ("terms_version", _int_text(quote_doc.get("terms_version"), where=f"{w}.terms_version")),
        ("capability_id", _text(quote_doc, "capability_id", where=w)),
        ("input_schema_hash", _text(quote_doc, "input_schema_hash", where=w)),
        ("tenant_id", _text(quote_doc, "tenant_id", where=w)),
        ("payer_account_id", _text(quote_doc, "payer_account_id", where=w)),
        ("principal_id", _text(quote_doc, "principal_id", where=w)),
        ("subject_principal_id", _text(quote_doc, "subject_principal_id", where=w)),
        ("delegation_grant_id", _text(quote_doc, "delegation_grant_id", where=w)),
        ("execution_id", _text(quote_doc, "execution_id", where=w)),
        ("args_digest", _text(quote_doc, "args_digest", where=w)),
        ("price_mode", _text(quote_doc, "price_mode", where=w)),
        ("currency", currency),
        ("amount_minor", str(amount)),
        ("funds_mode", _text(quote_doc, "funds_mode", where=w)),
        ("expires_at", hashed_time(parse_time(_text(quote_doc, "expires_at", where=w)))),
    )))


def payer_account_for(tenant_id: str) -> str:
    """The milestone's one payer per tenant (ADR 020 sec. 3), the same
    derivation mctl-api's `payerAccountFor` uses."""
    return "acct_tenant_" + tenant_id


def terms_binding(terms_doc: Mapping[str, Any], *, tenant_id: str, payer_account_id: str) -> str:
    """ADR 020 sec. 8: the price an approval covers. No quote id and no
    expiry, so an approval that waits for days still names this price."""
    w = "terms"
    currency, amount = _money(terms_doc, where=w)
    return _digest(length_prefixed(TERMS_ENCODING_VERSION, (
        ("terms_id", _text(terms_doc, "terms_id", where=w)),
        ("terms_version", _int_text(terms_doc.get("terms_version"), where=f"{w}.terms_version")),
        ("capability_id", _text(terms_doc, "capability_id", where=w)),
        ("input_schema_hash", _text(terms_doc, "input_schema_hash", where=w)),
        ("tenant_id", tenant_id),
        ("payer_account_id", payer_account_id),
        ("price_mode", _text(terms_doc, "price_mode", where=w)),
        ("currency", currency),
        ("amount_minor", str(amount)),
        ("funds_mode", _text(terms_doc, "funds_mode", where=w)),
    )))


def provider_operation_key(invocation_id: str) -> str:
    return "mctl-op/v1:" + invocation_id


def args_digest(arguments: Mapping[str, Any]) -> str:
    """ADR 017's `canonical_json(arguments)` digest, the value a quote binds
    and `InvocationRecord.arguments_hash` already carries."""
    from orchestrator.context_snapshot import canonical_json, hash_bytes

    return hash_bytes(canonical_json(dict(arguments)))


def gateway_idempotency_key(
    *, execution_id: str, capability_id: str, arguments_digest: str, client_key: str = "",
) -> str:
    """The key the gateway accepts an invocation under. Deterministic, so a
    restarted or concurrent retry of the same call in the same execution
    finds the invocation the first attempt accepted instead of opening a
    second one (ADR 020 sec. 6). `client_key` lets a caller ask for a
    deliberate second call with identical arguments."""
    raw = length_prefixed("mctl-gateway-invocation/v1", (
        ("execution_id", execution_id),
        ("capability_id", capability_id),
        ("args_digest", arguments_digest),
        ("client_key", client_key),
    ))
    return "mctl-gw.v1:" + hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Dispatch authorization (ADR 020 sec. 9)
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


def _unb64url(text: str) -> bytes:
    if not isinstance(text, str) or not _BASE64URL.fullmatch(text):
        raise AuthorizationRejected("signature is not base64url without padding")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise AuthorizationRejected("signature is not base64url") from exc


@dataclass(frozen=True)
class DispatchAuthorization:
    """The instruction mctl-api signs for exactly one paid call. Carries no
    arguments, results, prices or payment proofs."""

    key_id: str
    invocation_id: str
    provider_operation_key: str
    provider_id: str
    capability_id: str
    input_schema_hash: str
    args_digest: str
    tenant_id: str
    dispatch_deadline: str
    signature: str = ""
    schema_version: int = DISPATCH_AUTHORIZATION_SCHEMA_VERSION

    def signing_message(self) -> bytes:
        return length_prefixed(
            DISPATCH_AUTHORIZATION_VERSION, tuple((name, getattr(self, name)) for name in DISPATCH_SIGNED_FIELDS),
        )

    def deadline(self) -> datetime:
        return parse_time(hashed_time(self.dispatch_deadline))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **{name: getattr(self, name) for name in DISPATCH_SIGNED_FIELDS},
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, data: Any) -> DispatchAuthorization:
        if not isinstance(data, Mapping):
            raise AuthorizationRejected("authorization must be an object")
        strings = {"signature", *DISPATCH_SIGNED_FIELDS}
        unknown = set(data) - strings - {"schema_version"}
        if unknown:
            raise AuthorizationRejected(f"authorization carries unknown fields {sorted(unknown)!r}")
        version = data.get("schema_version")
        # bool is an int subclass: `true` must not pass as version 1.
        if not isinstance(version, int) or isinstance(version, bool):
            raise AuthorizationRejected(f"authorization.schema_version must be an integer, got {version!r}")
        if version != DISPATCH_AUTHORIZATION_SCHEMA_VERSION:
            raise AuthorizationRejected(f"unknown authorization schema_version {version!r}")
        values: dict[str, Any] = {"schema_version": version}
        for name in strings:
            value = data.get(name)
            if not isinstance(value, str) or not value:
                raise AuthorizationRejected(f"authorization.{name} is missing")
            values[name] = value
        return cls(**values)


def sign_dispatch_authorization(seed: bytes, authorization: DispatchAuthorization) -> DispatchAuthorization:
    """Sign as mctl-api does. Used by tests and by a local pilot's harness;
    production signing happens only in mctl-api."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(seed)
    unsigned = DispatchAuthorization(**{**authorization.to_dict(), "signature": "x"})
    return DispatchAuthorization(**{**unsigned.to_dict(), "signature": _b64url(key.sign(unsigned.signing_message()))})


def public_key_for(seed: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def verify_dispatch_authorization(
    public_keys: Mapping[str, bytes], authorization: DispatchAuthorization,
) -> None:
    """Raise `AuthorizationRejected` unless `authorization` is a well-formed
    v1 authorization signed by one of `public_keys` (pinned by key id).
    Binding to the actual call (provider, tool, schema, arguments, deadline)
    is the ingress's job; this only proves mctl-api said exactly this."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if (not isinstance(authorization.schema_version, int) or isinstance(authorization.schema_version, bool)
            or authorization.schema_version != DISPATCH_AUTHORIZATION_SCHEMA_VERSION):
        raise AuthorizationRejected(f"unknown authorization version {authorization.schema_version!r}")
    if authorization.provider_operation_key != provider_operation_key(authorization.invocation_id):
        raise AuthorizationRejected("provider_operation_key does not derive from invocation_id")
    try:
        hashed_time(authorization.dispatch_deadline)
    except CommerceError as exc:
        raise AuthorizationRejected(str(exc)) from exc
    pub = public_keys.get(authorization.key_id)
    if pub is None:
        raise AuthorizationRejected(f"unknown signing key {authorization.key_id!r}")
    signature = _unb64url(authorization.signature)
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(signature, authorization.signing_message())
    except (InvalidSignature, ValueError) as exc:
        raise AuthorizationRejected("signature does not verify") from exc


# ---------------------------------------------------------------------------
# Provider outcomes (ADR 020 sec. 9, requirement 5)
# ---------------------------------------------------------------------------

SUCCEEDED = "succeeded"
FAILED = "failed"
IN_PROGRESS = "in_progress"
NOT_FOUND = "not_found"
OUTCOME_STATUSES = frozenset({SUCCEEDED, FAILED, IN_PROGRESS, NOT_FOUND})


class DispatchRefused(Exception):
    """A paid provider's ingress refused the dispatch BEFORE accepting it:
    nothing was recorded and nothing ran, so the reservation may be
    released. Only this exception type means that; any other failure of a
    paid call is ambiguous and goes to the outcome lookup."""


class OutcomeUnavailable(Exception):
    """A paid provider could not answer for an operation. Never `not_found`."""


@dataclass(frozen=True)
class ProviderOutcome:
    """`outcome(key)`'s answer. A lookup that fails raises
    `OutcomeUnavailable` instead; it never answers `not_found`."""

    status: str
    is_error: bool = False
    text: str = ""

    def __post_init__(self) -> None:
        if self.status not in OUTCOME_STATUSES:
            raise ValueError(f"unknown outcome status {self.status!r}")


# ---------------------------------------------------------------------------
# CommerceClient — mctl-api's commerce routes
# ---------------------------------------------------------------------------

OK = "ok"
#: mctl-api answered with a typed refusal; `code` says which.
REFUSED = "refused"
#: The answer could not be observed: transport failure, 5xx, 408/429, 401,
#: an untyped 403, or a malformed body. Never a refusal, never an absence.
UNKNOWN = "unknown"

_TRANSIENT_4XX = frozenset({408, 425, 429})


@dataclass(frozen=True)
class CommerceAnswer:
    status: str
    http_status: int = 0
    code: str = ""
    body: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


class CommerceUnavailable(RuntimeError):
    """Raised by the transport only; every public method turns it into UNKNOWN."""


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirects)


def _classify(status: int, payload: Mapping[str, Any]) -> CommerceAnswer:
    raw = payload.get("code")
    code = raw if isinstance(raw, str) else ""
    reason = f"HTTP {status} {code}".strip()
    if status >= 500 or status in _TRANSIENT_4XX or status < 400:
        return CommerceAnswer(UNKNOWN, status, code, reason=reason)
    if status == 401 or (status == 403 and not code):
        # This process cannot talk to the ledger: an infrastructure fault,
        # not the ledger's answer about this call.
        return CommerceAnswer(UNKNOWN, status, code, reason=reason)
    if status == 404:
        return CommerceAnswer(REFUSED, status, "not-found", reason=reason)
    return CommerceAnswer(REFUSED, status, code or f"http-{status}", reason=reason)


# ---------------------------------------------------------------------------
# Tenant reads (ADR 020 sec. 12; mctlhq/mctl-agents#620)
# ---------------------------------------------------------------------------

READ_SCHEMA_VERSION = 1

_RECEIPT_TEXT_FIELDS = (
    "invocation_id", "capability_id", "terms_id", "quote_id", "quote_digest", "price_mode", "funds_mode",
    "provider_id", "execution_outcome", "charge_status", "payment_status", "settlement_status", "phase",
    "reason_code", "created_at", "updated_at", "approval_id", "execution_id",
)
# mctl-api's `policy_decision` object (mctlhq/mctl-api#569): a reference to the
# decision, never the policy text or the approver's free-form note.
_POLICY_DECISION_FIELDS = ("status", "approval_id", "policy_rule_id", "policy_version")
_POLICY_DECISION_STATUSES = frozenset({"none", "recorded", "missing", "unavailable"})
# mctl-api `commerce.Balance` field names (integer minor units).
_BUDGET_FIELDS = ("available_minor", "reserved_minor", "committed_minor", "pending_liability_minor")
_RECEIPT_MONEY_FIELDS = ("price",)


def _read_int(value: Any, *, where: str) -> int:
    """An integer count of minor units or of anything else. A float (even
    `12.0`), a bool, a string and a negative are all malformed: ADR 020
    invariant 2 forbids binary floating point for money."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CommerceError(f"{where} must be a non-negative integer")
    return value


def _read_schema(doc: Mapping[str, Any], *, where: str) -> None:
    if doc.get("schema_version") != READ_SCHEMA_VERSION:
        raise CommerceError(f"{where} has unsupported schema_version {doc.get('schema_version')!r}")


def _is_currency(value: Any) -> TypeGuard[str]:
    """The same ISO 4217 shape `CommerceSummary` requires of a sealed
    descriptor: three ASCII capital letters."""
    return isinstance(value, str) and len(value) == 3 and value.isascii() and value.isupper() and value.isalpha()


def _read_money(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CommerceError(f"{where} must be an object")
    currency = value.get("currency")
    if not _is_currency(currency):
        raise CommerceError(f"{where}.currency must be an ISO 4217 code")
    return {"currency": currency, "amount_minor": _read_int(value.get("amount_minor"), where=f"{where}.amount_minor")}


def _read_policy_decision(value: Any) -> dict[str, str] | None:
    """The receipt's policy summary, or None when it cannot be trusted as
    one: a status this client does not know, or a malformed sub-field, drops
    the sub-object instead of making the whole (otherwise valid) receipt
    UNKNOWN. mctl-api may add a status before this client learns it."""
    if not isinstance(value, Mapping):
        return None
    out: dict[str, str] = {}
    for name in _POLICY_DECISION_FIELDS:
        field = value.get(name)
        if field is None:
            continue
        if not isinstance(field, str):
            return None
        out[name] = field
    if out.get("status") not in _POLICY_DECISION_STATUSES:
        return None
    return out


@dataclass(frozen=True)
class BudgetView:
    """A tenant's simulated budget, integer minor units only. Field names are
    mctl-api's `commerce.Balance` wire names, so a body and its view agree."""

    currency: str
    funds_mode: str
    available_minor: int
    reserved_minor: int
    committed_minor: int
    pending_liability_minor: int

    @classmethod
    def from_dict(cls, data: Any) -> BudgetView:
        if not isinstance(data, Mapping):
            raise CommerceError("budget must be an object")
        _read_schema(data, where="budget")
        currency, funds_mode = data.get("currency"), data.get("funds_mode")
        if not _is_currency(currency):
            raise CommerceError("budget.currency must be an ISO 4217 code")
        if not isinstance(funds_mode, str) or not funds_mode:
            raise CommerceError("budget.funds_mode is missing")
        return cls(
            currency=currency, funds_mode=funds_mode,
            **{name: _read_int(data.get(name), where=f"budget.{name}")
               for name in _BUDGET_FIELDS},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": READ_SCHEMA_VERSION, "currency": self.currency, "funds_mode": self.funds_mode,
            **{name: getattr(self, name) for name in _BUDGET_FIELDS},
        }


@dataclass(frozen=True)
class ReceiptView:
    """The ADR 020 sec. 12 receipt. A whitelist: arguments, results, payment
    proofs and `external_ref` contents are never copied, whatever mctl-api
    sends, so no caller of this client can surface them."""

    fields: Mapping[str, Any]

    @classmethod
    def from_dict(cls, data: Any) -> ReceiptView:
        if not isinstance(data, Mapping):
            raise CommerceError("receipt must be an object")
        _read_schema(data, where="receipt")
        out: dict[str, Any] = {"schema_version": READ_SCHEMA_VERSION}
        for name in _RECEIPT_TEXT_FIELDS:
            value = data.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise CommerceError(f"receipt.{name} must be a string")
            out[name] = value
        for name in ("invocation_id", "phase"):
            if not out.get(name):
                raise CommerceError(f"receipt.{name} is missing")
        if data.get("terms_version") is not None:
            out["terms_version"] = _read_int(data.get("terms_version"), where="receipt.terms_version")
        if data.get("usage_quantity") is not None:
            out["usage_quantity"] = _read_int(data.get("usage_quantity"), where="receipt.usage_quantity")
        policy_decision = _read_policy_decision(data.get("policy_decision"))
        if policy_decision is not None:
            out["policy_decision"] = policy_decision
        for name in _RECEIPT_MONEY_FIELDS:
            if data.get(name) is not None:
                out[name] = _read_money(data.get(name), where=f"receipt.{name}")
        return cls(out)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.fields)


def _not_found() -> CommerceAnswer:
    """The one answer for a foreign tenant's resource and a missing one
    (ADR 020 invariant 17): identical in every field."""
    return CommerceAnswer(REFUSED, 404, "not-found", reason="HTTP 404 not-found")


def _parsed(answer: CommerceAnswer, parse: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> CommerceAnswer:
    if answer.status == REFUSED and answer.http_status == 404:
        return _not_found()
    if not answer.ok:
        return answer
    try:
        return CommerceAnswer(OK, answer.http_status, body=parse(answer.body))
    except CommerceError as exc:
        # A body this client cannot read is an unobserved answer, never a
        # zero balance and never an empty list.
        return CommerceAnswer(UNKNOWN, answer.http_status, reason=f"unreadable response: {exc}")


class CommerceClient:
    """Synchronous client for `/api/v1/commerce`. Every method returns a
    `CommerceAnswer`; none raises on a remote failure."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = 10) -> None:
        self._base = (base_url or os.environ.get("MCTL_API_BASE_URL", "https://api.mctl.ai")).rstrip("/")
        self._token = token if token is not None else os.environ.get("MCTL_TOKEN", "").strip()
        self._timeout = timeout
        self._opener = _no_redirect_opener()

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
        if not self._token:
            raise CommerceUnavailable("MCTL_TOKEN is not set")
        url = f"{self._base}{path}"
        if not url.startswith("https://"):
            raise CommerceUnavailable(f"refusing non-https MCTL_API_BASE_URL: {self._base}")
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
            raise CommerceUnavailable(f"{type(exc).__name__}: {exc}") from exc
        try:
            parsed = json.loads(body or b"{}")
        except ValueError as exc:
            raise CommerceUnavailable(f"malformed response: {exc}") from exc
        if not isinstance(parsed, dict):
            raise CommerceUnavailable("response is not a JSON object")
        return status, parsed

    def call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> CommerceAnswer:
        try:
            status, body = self._request(method, path, payload)
        except CommerceUnavailable as exc:
            return CommerceAnswer(UNKNOWN, reason=str(exc))
        if 200 <= status < 300:
            return CommerceAnswer(OK, status, body=body)
        return _classify(status, body)

    # -- routes ----------------------------------------------------------------

    @staticmethod
    def _tenant(tenant_id: str, rest: str = "") -> str:
        return f"/api/v1/commerce/tenants/{quote(tenant_id, safe='')}{rest}"

    @staticmethod
    def _invocation(tenant_id: str, invocation_id: str, rest: str = "") -> str:
        return CommerceClient._tenant(tenant_id, f"/invocations/{quote(invocation_id, safe='')}{rest}")

    def current_terms(self, capability_id: str, input_schema_hash: str) -> CommerceAnswer:
        query = urlencode({"capability_id": capability_id, "input_schema_hash": input_schema_hash, "current": "true"})
        return self.call("GET", f"/api/v1/commerce/terms?{query}")

    def precheck(self, tenant_id: str, *, currency: str, amount_minor: int) -> CommerceAnswer:
        return self.call("POST", self._tenant(tenant_id, "/budget/precheck"),
                         {"amount": {"currency": currency, "amount_minor": amount_minor}})

    def mint_quote(
        self, tenant_id: str, *, capability_id: str, input_schema_hash: str, args_digest: str, execution_id: str,
    ) -> CommerceAnswer:
        return self.call("POST", self._tenant(tenant_id, "/quotes"), {
            "capability_id": capability_id, "input_schema_hash": input_schema_hash,
            "args_digest": args_digest, "execution_id": execution_id,
        })

    def accept(
        self, tenant_id: str, *, quote_doc: Mapping[str, Any], idempotency_key: str, approval_id: str,
        terms_binding_value: str,
    ) -> CommerceAnswer:
        body: dict[str, Any] = {
            "quote_id": quote_doc.get("quote_id"), "quote_digest": quote_doc.get("quote_digest"),
            "idempotency_key": idempotency_key, "args_digest": quote_doc.get("args_digest"),
            "execution_id": quote_doc.get("execution_id"), "terms_binding": terms_binding_value,
        }
        if approval_id:
            body["approval_id"] = approval_id
        return self.call("POST", self._tenant(tenant_id, "/invocations"), body)

    def find_invocation(self, tenant_id: str, idempotency_key: str) -> CommerceAnswer:
        query = urlencode({"idempotency_key": idempotency_key})
        return self.call("GET", self._tenant(tenant_id, f"/invocations?{query}"))

    def get_invocation(self, tenant_id: str, invocation_id: str) -> CommerceAnswer:
        return self.call("GET", self._invocation(tenant_id, invocation_id))

    def reserve(self, tenant_id: str, invocation_id: str) -> CommerceAnswer:
        return self.call("POST", self._invocation(tenant_id, invocation_id, "/reserve"), {})

    def dispatch(self, tenant_id: str, invocation_id: str) -> CommerceAnswer:
        return self.call("POST", self._invocation(tenant_id, invocation_id, "/dispatch"), {})

    def transition(
        self, tenant_id: str, invocation_id: str, *, expected_phase: str, to_phase: str, reason_code: str = "",
        note: str = "",
    ) -> CommerceAnswer:
        body = {"expected_phase": expected_phase, "to_phase": to_phase}
        if reason_code:
            body["reason_code"] = reason_code
        if note:
            body["note"] = note
        return self.call("POST", self._invocation(tenant_id, invocation_id, "/transitions"), body)

    def finalize(self, tenant_id: str, invocation_id: str, *, execution_outcome: str) -> CommerceAnswer:
        return self.call("POST", self._invocation(tenant_id, invocation_id, "/finalize"),
                         {"execution_outcome": execution_outcome})

    def release(self, tenant_id: str, invocation_id: str, *, reason_code: str) -> CommerceAnswer:
        return self.call("POST", self._invocation(tenant_id, invocation_id, "/release"), {"reason_code": reason_code})

    def dispatch_keys(self) -> CommerceAnswer:
        return self.call("GET", "/api/v1/commerce/dispatch-keys")

    # -- tenant reads (read-only; sanitized to the ADR 020 sec. 12 fields) ----

    def get_budget(self, tenant_id: str) -> CommerceAnswer:
        """`body` is `BudgetView.to_dict()`. Unreachable is UNKNOWN, never a
        zero balance."""
        return _parsed(self.call("GET", self._tenant(tenant_id, "/budget")),
                       lambda body: BudgetView.from_dict(body).to_dict())

    def get_receipt(self, tenant_id: str, invocation_id: str) -> CommerceAnswer:
        """`body` is `ReceiptView.to_dict()`. A foreign tenant's invocation
        and a missing one answer identically."""
        return _parsed(self.call("GET", self._invocation(tenant_id, invocation_id, "/receipt")),
                       lambda body: ReceiptView.from_dict(body).to_dict())

    def list_invocations(
        self, tenant_id: str, *, capability_id: str | None = None, phase: str | None = None,
        since: str | None = None, limit: int = 50,
    ) -> CommerceAnswer:
        """`body` is `{"invocations": [receipt rows]}`. Unreachable is
        UNKNOWN, never an empty list."""
        query = {k: v for k, v in (("capability_id", capability_id), ("phase", phase), ("since", since)) if v}
        query["limit"] = str(max(1, min(int(limit), 200)))

        def parse(body: Mapping[str, Any]) -> Mapping[str, Any]:
            if not isinstance(body, Mapping):
                raise CommerceError("invocations response must be an object")
            rows = body.get("invocations")
            if not isinstance(rows, list):
                raise CommerceError("invocations must be a list")
            return {"invocations": [ReceiptView.from_dict(row).to_dict() for row in rows]}

        return _parsed(self.call("GET", self._tenant(tenant_id, f"/invocations?{urlencode(query)}")), parse)
