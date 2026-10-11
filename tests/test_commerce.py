"""ADR 020 golden vectors and the CommerceClient's transport rules
(mctlhq/mctl-agents#619, C4).

The digests below are pinned identically in mctl-api
(internal/commerce/digest_test.go, dispatch_test.go), so the Python and Go
implementations cannot drift apart silently.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta, timezone

import pytest

from orchestrator import commerce as cm

GOLDEN_QUOTE_DIGEST = "sha256:1a7b37d7215a2d02d9160f0afe49310e803eb2515f3745f0afb77995f8069131"
GOLDEN_TERMS_BINDING = "sha256:edefb0ce2c0ce7b46abb82aab173268568657ee2ae9fd32af8e8eac54e5514e1"
GOLDEN_ARGS_DIGEST = "sha256:73fd22bf1a31b2661710f8e3413afb2705c876302e6cbf8a112aed3317130c8c"
SCHEMA_HASH = "sha256:" + "ab" * 32

GOLDEN_DISPATCH_SEED = b"\x07" * 32
GOLDEN_DISPATCH_PUBLIC_KEY = "6kpsY-KcUgq-9VB7Ey7F-ZVHdq6-vnuSQh7qaRRG0iw"
GOLDEN_DISPATCH_MESSAGE_DIGEST = "sha256:e613ea26797144af17eb98f8206279a1c764bcf16e72a3463a47e5366df89174"
GOLDEN_DISPATCH_SIGNATURE = (
    "QUdLuG4SYxJQUVeRJygWNAeh24OKUVb_mV9AJBl8e0Nql7BTHEdtdqRCeslo3x5yKP0pQcvemUh3AUfco-LtBQ"
)


def golden_terms() -> dict:
    return {
        "terms_id": "ctm_01JZ8PZ0000000000000000000", "terms_version": 3,
        "capability_id": "mctl://mcp-remote/pilot-provider/lookup_status", "input_schema_hash": SCHEMA_HASH,
        "price_mode": "per_successful_call", "price": {"currency": "EUR", "amount_minor": 12},
        "funds_mode": "simulated",
    }


def golden_quote() -> dict:
    return {
        **golden_terms(),
        "quote_id": "cq_01JZ8Q3K7W5V4X2Y1Z0A9B8C7D", "tenant_id": "acme", "payer_account_id": "acct_tenant_acme",
        "principal_id": "prn_01JZ8Q0000000000000000000A", "subject_principal_id": "prn_01JZ8Q0000000000000000000B",
        "delegation_grant_id": "", "execution_id": "we_01JZ8Q0000000000000000000C",
        "args_digest": GOLDEN_ARGS_DIGEST, "expires_at": "2026-10-11T12:05:00Z",
    }


def golden_authorization() -> cm.DispatchAuthorization:
    return cm.DispatchAuthorization(
        key_id="kid-test-1", invocation_id="inv_01JZ8Q0000000000000000000D",
        provider_operation_key="mctl-op/v1:inv_01JZ8Q0000000000000000000D", provider_id="pilot-provider",
        capability_id="mctl://mcp-local/pilot-provider/lookup_status", input_schema_hash=SCHEMA_HASH,
        args_digest=GOLDEN_ARGS_DIGEST, tenant_id="acme", dispatch_deadline="2026-10-11T12:06:00Z",
    )


# -- golden vectors ------------------------------------------------------------


def test_quote_digest_golden_vector():
    assert cm.quote_digest(golden_quote()) == GOLDEN_QUOTE_DIGEST


def test_quote_digest_accepts_an_offset_expiry_but_hashes_the_z_form():
    """mctl-api may serialize RFC 3339 with fractional seconds or an offset;
    the hash always covers the whole-second Z form."""
    for variant in ("2026-10-11T12:05:00.000Z", "2026-10-11T14:05:00+02:00", "2026-10-11T12:05:00+00:00"):
        assert cm.quote_digest({**golden_quote(), "expires_at": variant}) == GOLDEN_QUOTE_DIGEST


def test_terms_binding_golden_vector():
    assert cm.terms_binding(golden_terms(), tenant_id="acme",
                            payer_account_id=cm.payer_account_for("acme")) == GOLDEN_TERMS_BINDING


def test_args_digest_is_the_sha256_of_canonical_json():
    """The quote's args_digest is ADR 017's canonical_json digest, the same
    value InvocationRecord.arguments_hash already carries."""
    from orchestrator.context_snapshot import canonical_json

    sample = {"b": 1, "a": [1, 2]}
    assert cm.args_digest(sample) == "sha256:" + hashlib.sha256(canonical_json(sample)).hexdigest()
    assert cm.args_digest({"a": [1, 2], "b": 1}) == cm.args_digest(sample)


@pytest.mark.parametrize("field,value", [
    ("quote_id", "cq_other"), ("terms_id", "ctm_other"), ("terms_version", 4),
    ("capability_id", "mctl://mcp-remote/pilot-provider/other"), ("input_schema_hash", "sha256:" + "cd" * 32),
    ("tenant_id", "other"), ("payer_account_id", "acct_tenant_other"), ("principal_id", "prn_other"),
    ("subject_principal_id", "prn_01JZ8Q0000000000000000000A"), ("delegation_grant_id", "dg_1"),
    ("execution_id", "we_other"), ("args_digest", "sha256:" + "00" * 32), ("price_mode", "per_call"),
    ("price", {"currency": "USD", "amount_minor": 12}), ("price", {"currency": "EUR", "amount_minor": 13}),
    ("funds_mode", "external"), ("expires_at", "2026-10-11T12:05:01Z"),
])
def test_every_quote_field_is_bound(field, value):
    assert cm.quote_digest({**golden_quote(), field: value}) != GOLDEN_QUOTE_DIGEST


@pytest.mark.parametrize("field,value", [
    ("terms_id", "ctm_other"), ("terms_version", 4), ("capability_id", "mctl://x/y/z"),
    ("input_schema_hash", "sha256:" + "cd" * 32), ("price_mode", "per_call"),
    ("price", {"currency": "EUR", "amount_minor": 13}), ("price", {"currency": "USD", "amount_minor": 12}),
    ("funds_mode", "external"),
])
def test_every_terms_field_is_bound(field, value):
    assert cm.terms_binding({**golden_terms(), field: value}, tenant_id="acme",
                            payer_account_id="acct_tenant_acme") != GOLDEN_TERMS_BINDING


def test_terms_binding_binds_tenant_and_payer():
    other_tenant = cm.terms_binding(golden_terms(), tenant_id="other", payer_account_id="acct_tenant_acme")
    assert other_tenant != GOLDEN_TERMS_BINDING
    assert cm.terms_binding(golden_terms(), tenant_id="acme", payer_account_id="acct_x") != GOLDEN_TERMS_BINDING


def test_length_prefix_defeats_field_boundary_shifting():
    a = cm.length_prefixed("v", (("a", "x:1:y"), ("b", "")))
    b = cm.length_prefixed("v", (("a", "x"), ("b", "y")))
    assert a != b
    assert cm.length_prefixed("v", (("f", "é"),)) == "v\nf:2:é\n".encode()


@pytest.mark.parametrize("bad", [
    {"price": None}, {"price": {"currency": "EUR"}}, {"price": {"currency": "EUR", "amount_minor": -1}},
    {"price": {"currency": "EUR", "amount_minor": True}}, {"terms_version": "3"}, {"expires_at": "tomorrow"},
    {"expires_at": "2026-10-11T12:05:00"}, {"quote_id": 7},
])
def test_malformed_quote_is_an_error_not_a_digest(bad):
    with pytest.raises(cm.CommerceError):
        cm.quote_digest({**golden_quote(), **bad})


def test_hashed_time_never_writes_an_offset():
    t = datetime(2026, 10, 11, 14, 5, 0, 999_999, tzinfo=timezone(timedelta(hours=2)))
    assert cm.hashed_time(t) == "2026-10-11T12:05:00Z"
    with pytest.raises(cm.CommerceError):
        cm.hashed_time(datetime(2026, 10, 11))
    with pytest.raises(cm.CommerceError):
        cm.hashed_time("2026-10-11T12:05:00+00:00")


def test_gateway_idempotency_key_is_deterministic_and_fits_the_api_pattern():
    k = cm.gateway_idempotency_key(execution_id="we_1", capability_id="mctl://a/b/c", arguments_digest="sha256:x")
    assert k == cm.gateway_idempotency_key(execution_id="we_1", capability_id="mctl://a/b/c",
                                           arguments_digest="sha256:x")
    assert cm.IDEMPOTENCY_KEY_PATTERN.match(k)
    others = {
        cm.gateway_idempotency_key(execution_id="we_2", capability_id="mctl://a/b/c", arguments_digest="sha256:x"),
        cm.gateway_idempotency_key(execution_id="we_1", capability_id="mctl://a/b/d", arguments_digest="sha256:x"),
        cm.gateway_idempotency_key(execution_id="we_1", capability_id="mctl://a/b/c", arguments_digest="sha256:y"),
        cm.gateway_idempotency_key(execution_id="we_1", capability_id="mctl://a/b/c", arguments_digest="sha256:x",
                                   client_key="second"),
    }
    assert k not in others and len(others) == 4


# -- dispatch authorization ---------------------------------------------------------


def test_dispatch_authorization_golden_vector_matches_go():
    pub = cm.public_key_for(GOLDEN_DISPATCH_SEED)
    assert base64.urlsafe_b64encode(pub).rstrip(b"=").decode() == GOLDEN_DISPATCH_PUBLIC_KEY
    a = golden_authorization()
    assert "sha256:" + hashlib.sha256(a.signing_message()).hexdigest() == GOLDEN_DISPATCH_MESSAGE_DIGEST
    signed = cm.sign_dispatch_authorization(GOLDEN_DISPATCH_SEED, a)
    assert signed.signature == GOLDEN_DISPATCH_SIGNATURE
    cm.verify_dispatch_authorization({"kid-test-1": pub}, signed)


def test_go_signature_verifies_without_the_python_signer():
    pub = cm.public_key_for(GOLDEN_DISPATCH_SEED)
    cm.verify_dispatch_authorization(
        {"kid-test-1": pub}, dataclasses.replace(golden_authorization(), signature=GOLDEN_DISPATCH_SIGNATURE))


@pytest.mark.parametrize("field", cm.DISPATCH_SIGNED_FIELDS)
def test_tampering_any_signed_field_breaks_verification(field):
    pub = cm.public_key_for(GOLDEN_DISPATCH_SEED)
    signed = dataclasses.replace(golden_authorization(), signature=GOLDEN_DISPATCH_SIGNATURE)
    value = getattr(signed, field)
    tampered = dataclasses.replace(signed, **{field: value[:-1] + ("0" if value[-1] != "0" else "1")})
    if field == "invocation_id":
        # Keep the opkey consistent so the signature itself is what fails.
        opkey = cm.provider_operation_key(tampered.invocation_id)
        tampered = dataclasses.replace(tampered, provider_operation_key=opkey)
    keys = {"kid-test-1": pub, tampered.key_id: pub}
    with pytest.raises(cm.AuthorizationRejected):
        cm.verify_dispatch_authorization(keys, tampered)


def test_verification_refuses_unknown_key_padding_version_and_foreign_opkey():
    pub = cm.public_key_for(GOLDEN_DISPATCH_SEED)
    signed = dataclasses.replace(golden_authorization(), signature=GOLDEN_DISPATCH_SIGNATURE)
    with pytest.raises(cm.AuthorizationRejected, match="unknown signing key"):
        cm.verify_dispatch_authorization({"other": pub}, signed)
    with pytest.raises(cm.AuthorizationRejected, match="padding"):
        padded = dataclasses.replace(signed, signature=signed.signature + "==")
        cm.verify_dispatch_authorization({"kid-test-1": pub}, padded)
    with pytest.raises(cm.AuthorizationRejected, match="version"):
        cm.verify_dispatch_authorization({"kid-test-1": pub}, dataclasses.replace(signed, schema_version="v2"))
    with pytest.raises(cm.AuthorizationRejected, match="derive"):
        cm.verify_dispatch_authorization({"kid-test-1": pub},
                                         dataclasses.replace(signed, provider_operation_key="mctl-op/v1:inv_other"))
    other = cm.public_key_for(b"\x08" * 32)
    with pytest.raises(cm.AuthorizationRejected, match="does not verify"):
        cm.verify_dispatch_authorization({"kid-test-1": other}, signed)


def test_authorization_from_dict_is_strict():
    doc = dataclasses.replace(golden_authorization(), signature=GOLDEN_DISPATCH_SIGNATURE).to_dict()
    assert cm.DispatchAuthorization.from_dict(doc).signature == GOLDEN_DISPATCH_SIGNATURE
    with pytest.raises(cm.AuthorizationRejected, match="unknown fields"):
        cm.DispatchAuthorization.from_dict({**doc, "amount_minor": 12})
    for name in doc:
        with pytest.raises(cm.AuthorizationRejected):
            cm.DispatchAuthorization.from_dict({k: v for k, v in doc.items() if k != name})
    with pytest.raises(cm.AuthorizationRejected):
        cm.DispatchAuthorization.from_dict(["not", "a", "mapping"])


# -- CommerceClient transport --------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: bytes):
        self._status, self._body = status, body

    def read(self):
        return self._body

    def getcode(self):
        return self._status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _client(outcome, *, base="https://api.example"):
    c = cm.CommerceClient(base_url=base, token="t0k")  # noqa: S106 — a test double, not a credential
    c._opener = _Opener(outcome)
    return c


def _http_error(status: int, body: dict | bytes):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError("https://api.example/x", status, "err", {}, io.BytesIO(raw))


def test_client_ok_sends_bearer_and_json():
    c = _client(_Resp(201, b'{"quote_id": "cq_1"}'))
    ans = c.mint_quote("acme", capability_id="mctl://a/b/c", input_schema_hash="h", args_digest="d", execution_id="we")
    assert ans.ok and ans.body == {"quote_id": "cq_1"} and ans.http_status == 201
    req = c._opener.requests[0]
    assert req.get_header("Authorization") == "Bearer t0k"
    assert req.full_url == "https://api.example/api/v1/commerce/tenants/acme/quotes"
    assert json.loads(req.data) == {"capability_id": "mctl://a/b/c", "input_schema_hash": "h",
                                    "args_digest": "d", "execution_id": "we"}


def test_client_escapes_path_segments():
    c = _client(_Resp(200, b"{}"))
    c.get_invocation("a/b", "inv/../x")
    assert c._opener.requests[0].full_url.endswith("/tenants/a%2Fb/invocations/inv%2F..%2Fx")


@pytest.mark.parametrize("status,body,want_status,want_code", [
    (422, {"code": "budget-exhausted"}, cm.REFUSED, "budget-exhausted"),
    (409, {"code": "payment-unknown"}, cm.REFUSED, "payment-unknown"),
    (404, {"error": "no"}, cm.REFUSED, "not-found"),
    (403, {"code": "policy-denied"}, cm.REFUSED, "policy-denied"),
    (403, {}, cm.UNKNOWN, ""),
    (401, {"code": "x"}, cm.UNKNOWN, "x"),
    (429, {}, cm.UNKNOWN, ""),
    (408, {}, cm.UNKNOWN, ""),
    (500, {"code": "internal"}, cm.UNKNOWN, "internal"),
    (503, {"code": "dispatch-unavailable"}, cm.UNKNOWN, "dispatch-unavailable"),
    (302, {}, cm.UNKNOWN, ""),
])
def test_client_classifies_http_errors(status, body, want_status, want_code):
    ans = _client(_http_error(status, body)).reserve("acme", "inv_1")
    assert (ans.status, ans.code) == (want_status, want_code)


@pytest.mark.parametrize("outcome", [
    TimeoutError("slow"), ConnectionResetError("reset"), _Resp(200, b"not json"), _Resp(200, b"[1]"),
])
def test_client_transport_failures_are_unknown_never_refused(outcome):
    ans = _client(outcome).finalize("acme", "inv_1", execution_outcome="succeeded")
    assert ans.status == cm.UNKNOWN and not ans.ok


def test_client_refuses_http_base_and_missing_token():
    assert _client(_Resp(200, b"{}"), base="http://api.example").dispatch("acme", "inv_1").status == cm.UNKNOWN
    c = cm.CommerceClient(base_url="https://api.example", token="")
    assert c.dispatch("acme", "inv_1").status == cm.UNKNOWN


def test_client_does_not_follow_redirects():
    handler = [h for h in cm._no_redirect_opener().handlers if h.__class__.__name__ == "_NoRedirects"]
    assert handler and handler[0].redirect_request(None, None, 302, "", {}, "https://evil") is None


def test_parse_time_rejects_naive_and_garbage():
    assert cm.parse_time("2026-10-11T12:05:00Z") == datetime(2026, 10, 11, 12, 5, tzinfo=UTC)
    for bad in ("2026-10-11T12:05:00", "nope", None):
        with pytest.raises(cm.CommerceError):
            cm.parse_time(bad)  # type: ignore[arg-type]
