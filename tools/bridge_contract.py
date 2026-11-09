"""Reference validator for the mctl bridge v1alpha2 contract (docs/contracts/bridge/v1alpha2/).

Every check takes RAW BYTES, never a parsed document, so that parser differentials
(duplicate keys, lone surrogates, 1.0 for an integer, ...) are rejected before any
schema sees the value. Order: precheck, integer tokens, schema, body cap, payload
cap, digest recompute, approval basket rules, display-code cross-checks.

Needs jsonschema (dev dependency group). Run `uv sync`.

CLI:
    python tools/bridge_contract.py validate (read-command|read-result|approval-basket|write-command) FILE
    python tools/bridge_contract.py digest FILE        # JCS digest of the whole JSON value
    python tools/bridge_contract.py fingerprint FILE   # basket_fingerprint of an approval basket
    python tools/bridge_contract.py sums [--check]     # print / verify SHA256SUMS
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]
from jsonschema import validators
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import jcs  # noqa: E402

CONTRACT_DIR = ROOT / "docs" / "contracts" / "bridge" / "v1alpha2"
V1_DIR = ROOT / "docs" / "spikes" / "1043"

MAX_BODY_BYTES = 16384
V2_SCHEMA_VERSION = "bridge.mctl.ai/v1alpha2"
MAX_PAYLOAD_BYTES = 12288

SCHEMA_FILES = {
    "read-command": "read-command.schema.json",
    "read-result": "read-result.schema.json",
    "approval-basket": "approval-basket.schema.json",
    "write-command": "command-place-order-fake.schema.json",
    "claim-response": "claim-response.schema.json",
}


class ContractError(ValueError):
    """A rejected document. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Any:
    """`pattern` with the cross-language semantics of the contract.

    jsonschema's default is re.search, and Python `$` also matches before a trailing
    newline. The contract requires a full match over ASCII, which Go RE2 does natively.
    """
    if validator.is_type(instance, "string") and not re.fullmatch(pattern, instance, re.ASCII):
        yield jsonschema.ValidationError(f"{instance!r} does not match {pattern!r}")


_Validator = validators.extend(jsonschema.Draft202012Validator, {"pattern": _pattern})


def load_schema(name: str) -> dict[str, Any]:
    loaded = json.loads((CONTRACT_DIR / SCHEMA_FILES[name]).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"{name} schema is not an object")
    return loaded


def registry() -> Registry:
    """All v1alpha2 schemas plus the v1alpha1 command, keyed by `$id`."""
    reg: Registry = Registry()
    docs = [load_schema(n) for n in SCHEMA_FILES]
    docs.append(json.loads((V1_DIR / "command.schema.json").read_text(encoding="utf-8")))
    for doc in docs:
        reg = reg.with_resource(doc["$id"], Resource.from_contents(doc))
    return reg


def validator(name: str) -> Any:
    schema = load_schema(name)
    _Validator.check_schema(schema)
    return _Validator(schema, registry=registry(), format_checker=_Validator.FORMAT_CHECKER)


def _validate_schema(name: str, doc: Any) -> None:
    errors = sorted(validator(name).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    if errors:
        first = errors[0]
        where = "/".join(map(str, first.absolute_path)) or "<root>"
        raise ContractError("schema_invalid", f"{where}: {first.message}")


def _parse(raw: bytes) -> Any:
    """precheck, integer tokens, then parse. Returns the parsed object."""
    try:
        numbers = jcs.precheck(raw)
    except jcs.JCSError as exc:
        raise ContractError(exc.code, exc.message) from exc
    for path, token in numbers:
        if not jcs.INTEGER_TOKEN.fullmatch(token):
            where = "/".join(map(str, path))
            raise ContractError("non_integer_token", f"{where}: {token!r} is not a JSON integer token")
    return json.loads(raw.decode("utf-8"))


def _check_basket_rules(basket: dict[str, Any]) -> None:
    lines = basket["lines"]
    codes = [line["item_code"] for line in lines]
    if len(set(codes)) != len(codes):
        raise ContractError("duplicate_item", "item_code repeats in lines")
    if codes != sorted(codes):
        raise ContractError("unsorted_lines", "lines must be sorted ascending by item_code")
    subtotal = sum(line["quantity"] * line["unit_price_minor"] for line in lines)
    if basket["subtotal_minor"] != subtotal:
        raise ContractError("subtotal_mismatch", f"subtotal_minor is {basket['subtotal_minor']}, expected {subtotal}")
    total = subtotal + basket["delivery_fee_minor"] + basket["service_fee_minor"] - basket["discount_minor"]
    if basket["total_minor"] != total:
        raise ContractError("total_mismatch", f"total_minor is {basket['total_minor']}, expected {total}")


def basket_fingerprint(basket: Any) -> str:
    """sha256:<hex> of JCS(approval_basket). The single M1 basket fingerprint."""
    return jcs.digest(basket)


def check_approval_basket(raw: bytes) -> str:
    """Validate an approval basket from raw bytes. Returns its basket_fingerprint."""
    doc = _parse(raw)
    _validate_schema("approval-basket", doc)
    _check_basket_rules(doc)
    return basket_fingerprint(doc)


def check_read_command(raw: bytes) -> dict[str, Any]:
    doc = _parse(raw)
    _validate_schema("read-command", doc)
    if len(raw) > MAX_BODY_BYTES:
        raise ContractError("payload_too_large", f"body is {len(raw)} bytes, cap {MAX_BODY_BYTES}")
    if not isinstance(doc, dict):
        raise ContractError("not_object", "the root must be an object")
    return doc


def check_write_command(raw: bytes) -> dict[str, Any]:
    doc = _parse(raw)
    _validate_schema("write-command", doc)
    _check_basket_rules(doc["payload"])
    return doc


def check_claim_response(raw: bytes) -> dict[str, Any]:
    doc = _parse(raw)
    _validate_schema("claim-response", doc)
    for command in doc.get("commands", []):
        if command.get("schema_version") == V2_SCHEMA_VERSION and command.get("kind") == "place_order_fake":
            _check_basket_rules(command["payload"])
    return doc


def check_read_result(raw: bytes) -> dict[str, Any]:
    doc = _parse(raw)
    _validate_schema("read-result", doc)
    if len(raw) > MAX_BODY_BYTES:
        raise ContractError("payload_too_large", f"body is {len(raw)} bytes, cap {MAX_BODY_BYTES}")
    payload = doc.get("payload")
    if payload is None:
        return doc
    canonical = jcs.canonicalize(payload)
    if len(canonical) > MAX_PAYLOAD_BYTES:
        raise ContractError("payload_too_large", f"JCS(payload) is {len(canonical)} bytes, cap {MAX_PAYLOAD_BYTES}")
    expected = "sha256:" + hashlib.sha256(canonical).hexdigest()
    if doc["payload_digest"] != expected:
        raise ContractError("digest_mismatch", f"payload_digest is {doc['payload_digest']}, expected {expected}")
    basket = payload.get("approval_basket")
    if basket is not None:
        _check_basket_rules(basket)
    line_codes = {line["item_code"] for line in basket["lines"]} if basket else set()
    for entry in payload.get("display", []):
        if entry["item_code"] not in line_codes:
            raise ContractError("display_not_in_basket", f"display item_code {entry['item_code']} is not a basket line")
    unavailable = payload.get("unavailable_items", [])
    if len(set(unavailable)) != len(unavailable):
        raise ContractError("duplicate_item", "unavailable_items repeats an item_code")
    if set(unavailable) & line_codes:
        raise ContractError("unavailable_overlaps_lines", "unavailable_items must be disjoint from the basket lines")
    return doc


# ------------------------------------------------------------------------------ SHA256SUMS


def sums_text() -> str:
    """SHA256SUMS content: every published file under the contract dir except SHA256SUMS."""
    lines = []
    for path in sorted(p for p in CONTRACT_DIR.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        rel = path.relative_to(CONTRACT_DIR).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------ CLI

_CHECKS = {
    "read-command": check_read_command,
    "read-result": check_read_result,
    "approval-basket": check_approval_basket,
    "write-command": check_write_command,
    "claim-response": check_claim_response,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_val = sub.add_parser("validate")
    p_val.add_argument("kind", choices=sorted(_CHECKS))
    p_val.add_argument("file", type=Path)
    p_dig = sub.add_parser("digest")
    p_dig.add_argument("file", type=Path)
    p_fp = sub.add_parser("fingerprint")
    p_fp.add_argument("file", type=Path)
    p_sums = sub.add_parser("sums")
    p_sums.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "validate":
            _CHECKS[args.kind](args.file.read_bytes())
            print("ok")
        elif args.cmd == "digest":
            print(jcs.digest(_parse(args.file.read_bytes())))
        elif args.cmd == "fingerprint":
            print(check_approval_basket(args.file.read_bytes()))
        else:
            text = sums_text()
            if args.check:
                if (CONTRACT_DIR / "SHA256SUMS").read_text(encoding="utf-8") != text:
                    raise ContractError("sums_mismatch", "SHA256SUMS does not match the files")
                print("ok")
            else:
                sys.stdout.write(text)
    except (ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
