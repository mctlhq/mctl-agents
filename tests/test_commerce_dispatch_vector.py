"""The dispatch authorization exactly as mctl-api puts it on the wire.

`tests/fixtures/commerce/dispatch_authorization_v1.json` was produced by
mctl-api's own signer (`internal/commerce/dispatch.go`, `DispatchSigner.sign`,
mctl-api 4.81.0) from a fixed test seed. Every other authorization in this
suite is built by this repository's Python, so a disagreement between the two
implementations about the wire shape could not show up in them. It did: the
parser required `schema_version` to be the encoding string
`mctl-commerce-dispatch/v1` while mctl-api sends the integer 1 (ADR 020: every
record carries `schema_version: 1`), so every live authorization was refused
and every paid call stuck in `dispatching` (mctlhq/mctl-agents#620 live proof,
2026-10-12).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from orchestrator import commerce as cm

VECTOR = json.loads((Path(__file__).parent / "fixtures/commerce/dispatch_authorization_v1.json").read_text())


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _keys() -> dict[str, bytes]:
    return {VECTOR["authorization"]["key_id"]: _b64url(VECTOR["public_key_b64url"])}


def test_mctl_api_wire_authorization_parses_and_verifies():
    auth = cm.DispatchAuthorization.from_dict(VECTOR["authorization"])
    assert auth.schema_version == 1
    cm.verify_dispatch_authorization(_keys(), auth)
    # The round trip reproduces the wire form, integer version included.
    assert auth.to_dict() == VECTOR["authorization"]


def test_python_signer_matches_mctl_api_byte_for_byte():
    """Ed25519 is deterministic: the same seed over the same signing message
    gives the same signature, so this pins the signing message too."""
    seed = base64.b64decode(VECTOR["seed_b64"])
    assert cm.public_key_for(seed) == _b64url(VECTOR["public_key_b64url"])
    unsigned = {k: v for k, v in VECTOR["authorization"].items() if k != "signature"}
    signed = cm.sign_dispatch_authorization(seed, cm.DispatchAuthorization(**unsigned))
    assert signed.signature == VECTOR["authorization"]["signature"]


@pytest.mark.parametrize("version", [cm.DISPATCH_AUTHORIZATION_VERSION, "1", True, 2, 0, None, 1.0])
def test_from_dict_refuses_any_schema_version_but_integer_one(version):
    wire = {**VECTOR["authorization"], "schema_version": version}
    with pytest.raises(cm.AuthorizationRejected, match="schema_version"):
        cm.DispatchAuthorization.from_dict(wire)


def test_from_dict_refuses_a_missing_schema_version():
    wire = {k: v for k, v in VECTOR["authorization"].items() if k != "schema_version"}
    with pytest.raises(cm.AuthorizationRejected, match="schema_version"):
        cm.DispatchAuthorization.from_dict(wire)
