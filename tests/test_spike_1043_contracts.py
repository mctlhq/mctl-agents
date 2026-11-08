"""Offline contract checks for the spike 1043 bridge schemas and memo.

jsonschema is a required (dev group) dependency: a missing import FAILS the
suite, it never skips. Run `uv sync` to install it.
"""

import base64
import json
import re
from pathlib import Path

try:
    import jsonschema
except ImportError as exc:  # pragma: no cover - failure path
    raise RuntimeError(
        "jsonschema is required for tests/test_spike_1043_contracts.py; "
        "install it with `uv sync` (dev dependency group)"
    ) from exc

import pytest

SPIKE = Path(__file__).resolve().parent.parent / "docs" / "spikes"
DIR = SPIKE / "1043"
FIX = DIR / "fixtures"
MEMO = SPIKE / "1043-cross-namespace-dispatch.md"
OUTCOMES = {
    "NEXUS_VERIFIED",
    "API_BRIDGE_SELECTED",
    "BLOCKED_NEEDS_SDK_OR_SERVER_UPGRADE",
    "DEFER",
}

# (fixture, schema, valid, expected failing keyword/path substring)
CASES = [
    ("command.valid.json", "command", True, None),
    ("result.valid.json", "result", True, None),
    ("command.extra-field.json", "command", False, "glovo_session_cookie"),
    ("command.mutation-missing-approval.json", "command", False, "approval_ref"),
    ("command.ttl-too-long.json", "command", False, "ttl_seconds"),
    ("command.bad-ref-pattern.json", "command", False, "tenant_ref"),
    ("command.string-too-long.json", "command", False, "note"),
    ("result.extra-field.json", "result", False, "customer_email"),
]


def _schema(name):
    schema = json.loads((DIR / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)


@pytest.mark.parametrize(("fixture", "schema", "valid", "reason"), CASES)
def test_fixture(fixture, schema, valid, reason):
    doc = json.loads((FIX / fixture).read_text())
    errors = list(_schema(schema).iter_errors(doc))
    if valid:
        assert errors == []
    else:
        assert errors, f"{fixture} unexpectedly validated"
        assert any(reason in str(e.message) + "/".join(map(str, e.absolute_path)) for e in errors), [
            e.message for e in errors
        ]


def test_case_counts_nonzero():
    # Derive expectations from fixtures, never from shared mutable test state.
    assert sum(valid for _, _, valid, _ in CASES) == 2
    assert sum(not valid for _, _, valid, _ in CASES) == 6


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("outcome", "paid"),
        ("provider_effect", "charged"),
        ("result_ref", "not-a-result-id"),
        ("error_code", "secret_exposed"),
    ],
)
def test_result_rejects_invalid_fields(field, invalid_value):
    # Negative result-schema checks are order-independent and need no fixture mutations.
    result = json.loads((FIX / "result.valid.json").read_text())
    result[field] = invalid_value
    assert list(_schema("result").iter_errors(result)), field


def test_memo_status_line_is_one_allowed_outcome():
    first = MEMO.read_text().splitlines()[0]
    assert first.startswith("STATUS: ")
    assert first.removeprefix("STATUS: ") in OUTCOMES


def test_no_credential_shaped_values():
    texts = [p.read_text() for p in FIX.glob("*.json")] + [MEMO.read_text()]
    jwt = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
    for text in texts:
        assert "MCTL_TOKEN=" not in text
        assert "Bearer " not in text
        assert not jwt.search(text)


SCOPE = re.compile(r"^temporal-system:(reader|writer|admin)$")


def _fake_jwt(scope):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return ".".join([enc({"alg": "none"}), enc({"permissions": [scope]}), ""])


def _scopes(token):
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["permissions"]


def test_valid_and_invalid_system_scope():
    assert all(SCOPE.match(s) for s in _scopes(_fake_jwt("temporal-system:admin")))
    # The pre-amendment spelling is invalid and must be rejected.
    assert not any(SCOPE.match(s) for s in _scopes(_fake_jwt("system:admin")))


def test_memo_uses_correct_system_scope_spelling():
    text = MEMO.read_text()
    assert "temporal-system:admin" in text
    assert not re.search(r"(?<!temporal-)system:admin", text)
