"""Generate the fixtures and vectors of the bridge v1alpha2 contract, and measure sizes.

    python tools/bridge_fixtures.py --write   # (re)write fixtures/ and vectors/
    python tools/bridge_fixtures.py --sizes   # print the measured worst-case sizes

Synthetic data only. The JCS expectations in jcs-digest.json are written by hand
(from RFC 8785 and the ES number rules), not computed by tools/jcs.py, so that the
vectors are an independent check of it. SHA256SUMS is not written here: run
`python tools/bridge_contract.py sums > docs/contracts/bridge/v1alpha2/SHA256SUMS`
after the README is final.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import jcs  # noqa: E402

CONTRACT_DIR = ROOT / "docs" / "contracts" / "bridge" / "v1alpha2"
V1_FIXTURES = ROOT / "docs" / "spikes" / "1043" / "fixtures"
V2 = "bridge.mctl.ai/v1alpha2"
CAP = 16384

CMD = "cmd_01HZZZZZZZZZZZZZZZZZZZZZZZ"
REQ = "rdq_01HYYYYYYYYYYYYYYYYYYYYYYY"
PHO = "pho_0123456789abcdef0123"
STORE = "pst_0123456789abcdef0123"


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def dump(doc: Any) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def compact(doc: Any) -> str:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n"


def write(rel: str, text: str) -> None:
    path = CONTRACT_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------------------ baskets


def make_basket(
    lines: list[tuple[str, int, int]],
    delivery: int = 199,
    service: int = 50,
    discount: int = 0,
    store: str = STORE,
) -> dict[str, Any]:
    subtotal = sum(q * p for _, q, p in lines)
    return {
        "v": "mctl-approval-basket/v1",
        "store_ref": store,
        "currency": "EUR",
        "lines": [{"item_code": c, "quantity": q, "unit_price_minor": p} for c, q, p in lines],
        "subtotal_minor": subtotal,
        "delivery_fee_minor": delivery,
        "service_fee_minor": service,
        "discount_minor": discount,
        "total_minor": subtotal + delivery + service - discount,
    }


MINIMAL_LINES = [("FAKE-1", 1, 500)]
SMALL_LINES = [("FAKE-BREAD", 2, 150), ("FAKE-MILK", 1, 120)]


def lines_30() -> list[tuple[str, int, int]]:
    return [(f"FAKE-ITEM-{i:02d}", 1 + i % 3, 100 + 7 * i) for i in range(30)]


def display_for(basket: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"item_code": ln["item_code"], "item_name": "Fake " + ln["item_code"][5:].title()} for ln in basket["lines"]
    ]


# ------------------------------------------------------------------------------ commands


def read_command(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": V2,
        "command_id": CMD,
        "request_id": REQ,
        "kind": kind,
        "attempt": 1,
        "tenant_ref": "tnr_0123456789abcdef0123",
        "principal_ref": "prr_0123456789abcdef0123",
        "connection_ref": "gcn_0123456789abcdef0123",
        "install_revision_ref": "irv_0123456789abcdef0123",
        "issuer": "mctl-api",
        "audience": "provider:glovo",
        "nonce": "AAAAAAAAAAAAAAAAAAAAAA",
        "issued_at": "2026-11-09T10:00:00Z",
        "expires_at": "2026-11-09T10:05:00Z",
        "ttl_seconds": 300,
        "lease_token": "lt_0123456789abcdef0123",
        "params": params,
    }


def v1_command() -> dict[str, Any]:
    loaded = json.loads((V1_FIXTURES / "command.valid.json").read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("v1alpha1 command fixture is not an object")
    return loaded


def write_command(payload: dict[str, Any]) -> dict[str, Any]:
    doc = v1_command()
    doc["schema_version"] = V2
    doc["kind"] = "place_order_fake"
    doc["payload"] = payload
    return doc


# ------------------------------------------------------------------------------ results


def result(kind: str, payload: dict[str, Any] | None, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": V2,
        "command_id": CMD,
        "request_id": REQ,
        "kind": kind,
        "lease_token": "lt_0123456789abcdef0123",
        "attempt": 1,
        "worker_identity": "provider:glovo",
        "outcome": "succeeded",
        "provider_effect": "none",
        "error_code": "none",
        "completed_at": "2026-11-09T10:01:00Z",
    }
    if payload is not None:
        doc["payload"] = payload
        doc["payload_digest"] = jcs.digest(payload)
    doc.update(over)
    return doc


def history_payload(orders: int = 2, lines: int = 2) -> dict[str, Any]:
    return {
        "orders": [
            {
                "history_order_ref": f"pho_{n:020d}",
                "placed_on": f"2026-11-{1 + n:02d}",
                "status": ("delivered", "cancelled", "in_progress")[n % 3],
                "total_minor": 1500 + n,
                "currency": "EUR",
                "line_count": lines,
                "lines": [{"item_code": f"FAKE-{k}", "quantity": 1 + k} for k in range(lines)],
            }
            for n in range(orders)
        ],
        "next_cursor": "c2VlZC1jdXJzb3I",
    }


def preview_payload(basket: dict[str, Any], unavailable: list[str]) -> dict[str, Any]:
    return {
        "empty": False,
        "display": display_for(basket),
        "unavailable_items": unavailable,
        "approval_basket": basket,
    }


def basket_payload(basket: dict[str, Any]) -> dict[str, Any]:
    return {
        "empty": False,
        "store_name": "Fake Store (Test)",
        "display": display_for(basket),
        "approval_basket": basket,
    }


def max_history() -> dict[str, Any]:
    payload = history_payload(20, 5)
    payload["next_cursor"] = "c" * 64
    for n, order in enumerate(payload["orders"]):
        order["history_order_ref"] = "pho_" + f"{n:040d}"
        order["status"] = "in_progress"
        order["total_minor"] = 10000000
        order["line_count"] = 100
        order["lines"] = [{"item_code": "FAKE-" + f"{k:02d}" + "A" * 30, "quantity": 100} for k in range(5)]
    return payload


def max_basket() -> dict[str, Any]:
    lines = [("FAKE-" + f"{i:02d}" + "A" * 30, 100, 1000000) for i in range(30)]
    return make_basket(lines, 10000000, 10000000, 10000000, "pst_" + "S" * 40)


def max_display(basket: dict[str, Any]) -> list[dict[str, str]]:
    return [{"item_code": ln["item_code"], "item_name": "N" * 60} for ln in basket["lines"]]


def max_preview() -> dict[str, Any]:
    basket = max_basket()
    unavailable = ["FAKE-" + f"{i:02d}" + "U" * 30 for i in range(30)]
    return {"empty": False, "display": max_display(basket), "unavailable_items": unavailable, "approval_basket": basket}


def max_basket_read() -> dict[str, Any]:
    basket = max_basket()
    return {"empty": False, "store_name": "S" * 60, "display": max_display(basket), "approval_basket": basket}


def padded(doc: dict[str, Any], size: int) -> str:
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    n = len(text.encode("utf-8"))
    if n > size:
        raise ValueError(f"document is {n} bytes, larger than {size}")
    return text + " " * (size - n)


# ------------------------------------------------------------------------------ vectors


def jcs_vectors() -> list[dict[str, str]]:
    """(name, category, input_text, hand-written canonical text)."""
    rows = [
        ("literals", "literals", "[true,false,null]", "[true,false,null]"),
        ("bool-in-object", "literals", '{"a":true}', '{"a":true}'),
        ("empty-object", "empty", "{}", "{}"),
        ("empty-array", "empty", "[]", "[]"),
        (
            "nested",
            "nested",
            '{"z":[{"b":2,"a":1},[3,2,1]],"y":{"d":{},"c":[]}}',
            '{"y":{"c":[],"d":{}},"z":[{"a":1,"b":2},[3,2,1]]}',
        ),
        (
            "key-order-bmp",
            "utf16-key-order",
            '{"b":1,"a":2,"B":3,"\\u00e9":4,"\\ufb33":5}',
            '{"B":3,"a":2,"b":1,"é":4,"\ufb33":5}',
        ),
        (
            "key-order-supplementary",
            "utf16-key-order",
            '{"\\ufb33":1,"\\ud83d\\ude00":2}',
            '{"😀":2,"\ufb33":1}',
        ),
        ("key-order-numeric-strings", "utf16-key-order", '{"10":1,"9":2,"1":3}', '{"1":3,"10":1,"9":2}'),
        (
            "control-escapes",
            "control-escapes",
            '{"s":"\\u0000\\u0001\\u001f\\b\\t\\n\\f\\r"}',
            '{"s":"\\u0000\\u0001\\u001f\\b\\t\\n\\f\\r"}',
        ),
        ("del-unescaped", "del", '{"s":"\\u007f"}', '{"s":"\x7f"}'),
        ("quote-backslash", "string-escapes", '{"s":"\\"\\\\"}', '{"s":"\\"\\\\"}'),
        ("solidus-unescaped", "string-escapes", '{"s":"\\/"}', '{"s":"/"}'),
        ("raw-non-ascii", "string-escapes", '{"s":"\\u00e9\\u20ac\\ud83d\\ude00"}', '{"s":"é€😀"}'),
        ("number-1e21", "es-numbers", "[1e21]", "[1e+21]"),
        ("number-1e20", "es-numbers", "[1e20]", "[100000000000000000000]"),
        ("number-1e-7", "es-numbers", "[1e-7]", "[1e-7]"),
        ("number-1e-6", "es-numbers", "[0.000001]", "[0.000001]"),
        ("number-4.50", "es-numbers", "[4.50]", "[4.5]"),
        ("number-large-float", "es-numbers", "[1.2345678901234568e20]", "[123456789012345680000]"),
        ("negative-zero-int", "negative-zero", "[-0]", "[0]"),
        ("negative-zero-float", "negative-zero", "[-0.0]", "[0]"),
        (
            "integers",
            "integers",
            "[0,-1,9007199254740991,-9007199254740991]",
            "[0,-1,9007199254740991,-9007199254740991]",
        ),
        (
            "rfc8785-3.2.2-sample",
            "rfc8785-3.2.2",
            '{"numbers":[333333333.33333329,1E30,4.50,2e-3,0.000000000000000000000000001],'
            '"string":"\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/","literals":[null,true,false]}',
            '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}',
        ),
        (
            "rfc8785-3.2.3-sorting",
            "rfc8785-3.2.3",
            '{"\\u20ac":"Euro Sign","\\r":"Carriage Return","\\ufb33":"Hebrew Letter Dalet With Dagesh",'
            '"1":"One","\\ud83d\\ude00":"Emoji: Grinning Face","\\u0080":"Control",'
            '"\\u00f6":"Latin Small Letter O With Diaeresis"}',
            '{"\\r":"Carriage Return","1":"One","\x80":"Control","ö":"Latin Small Letter O With Diaeresis",'
            '"€":"Euro Sign","😀":"Emoji: Grinning Face","\ufb33":"Hebrew Letter Dalet With Dagesh"}',
        ),
    ]
    return [{"name": n, "category": c, "input_text": i, "canonical": o, "digest": sha(o)} for n, c, i, o in rows]


def must_reject_entries() -> list[dict[str, Any]]:
    basket = make_basket(SMALL_LINES)
    doc_text = json.dumps(result("read_basket", basket_payload(basket)), indent=2, ensure_ascii=False)
    both = ["read-result", "read-command"]
    only_result = ["read-result"]

    def sub(old: str, new: str) -> str:
        if old not in doc_text:
            raise ValueError(f"missing {old!r}")
        return doc_text.replace(old, new, 1)

    return [
        {"name": "duplicate-key-top-level", "reason": "duplicate_key", "applies": both, "raw": '{"a":1,"a":2}'},
        {"name": "duplicate-key-nested", "reason": "duplicate_key", "applies": both, "raw": '{"a":{"b":1,"b":2}}'},
        {
            "name": "escaped-duplicate-key-top-level",
            "reason": "duplicate_key",
            "applies": both,
            "raw": '{"a":1,"\\u0061":2}',
        },
        {
            "name": "escaped-duplicate-quantity-in-basket-line",
            "reason": "duplicate_key",
            "applies": only_result,
            "raw": sub('"quantity": 2,', '"quantity": 2, "\\u0071uantity": 3,'),
        },
        {"name": "escape-in-object-key", "reason": "escaped_key", "applies": both, "raw": '{"\\u0061":1}'},
        {"name": "lone-high-surrogate", "reason": "lone_surrogate", "applies": both, "raw": '{"a":"\\ud800"}'},
        {"name": "lone-low-surrogate", "reason": "lone_surrogate", "applies": both, "raw": '{"a":"\\udc00"}'},
        {
            "name": "high-surrogate-then-non-surrogate",
            "reason": "lone_surrogate",
            "applies": both,
            "raw": '{"a":"\\ud800\\u0041"}',
        },
        {"name": "nan", "reason": "non_finite", "applies": both, "raw": '{"a":NaN}'},
        {"name": "infinity", "reason": "non_finite", "applies": both, "raw": '{"a":Infinity}'},
        {"name": "negative-infinity", "reason": "non_finite", "applies": both, "raw": '{"a":-Infinity}'},
        {"name": "exponent-overflow-1e400", "reason": "number_range", "applies": both, "raw": '{"a":1e400}'},
        {"name": "integer-2-pow-53-plus-1", "reason": "number_range", "applies": both, "raw": '{"a":9007199254740993}'},
        {
            "name": "integer-minus-2-pow-53-minus-1",
            "reason": "number_range",
            "applies": both,
            "raw": '{"a":-9007199254740993}',
        },
        {
            "name": "quantity-1.0",
            "reason": "non_integer_token",
            "applies": only_result,
            "raw": sub('"quantity": 2,', '"quantity": 1.0,'),
        },
        {
            "name": "quantity-1e2",
            "reason": "non_integer_token",
            "applies": only_result,
            "raw": sub('"quantity": 2,', '"quantity": 1e2,'),
        },
        {
            "name": "quantity-true",
            "reason": "schema_invalid",
            "applies": only_result,
            "raw": sub('"quantity": 2,', '"quantity": true,'),
        },
        {"name": "utf8-bom", "reason": "bom", "applies": both, "raw": "\ufeff{}"},
        {"name": "trailing-bytes", "reason": "trailing_bytes", "applies": both, "raw": "{} x"},
        {"name": "two-root-objects", "reason": "trailing_bytes", "applies": both, "raw": "{}{}"},
        {"name": "array-root", "reason": "not_object", "applies": both, "raw": "[]"},
        {"name": "raw-control-char-in-string", "reason": "control_char", "applies": both, "raw": '{"a":"x\ty"}'},
        {"name": "nesting-too-deep", "reason": "too_deep", "applies": both, "raw": '{"a":' + "[" * 40 + "]" * 40 + "}"},
    ]


def pattern_parity() -> dict[str, Any]:
    def case(cid: str, schema: str, pointer: str, accept: list[str], reject: list[str]) -> dict[str, Any]:
        return {"id": cid, "schema": schema, "pointer": pointer, "accept": accept, "reject": reject}

    rr = "read-result.schema.json"
    digits = ["٢٠٢٦-١١-٠٩", "２０２６-１１-０９"]
    return {
        "description": (
            "Strings that every consumer must accept or reject for an anchored schema pattern. "
            "Python and Go RE2 must agree: no trailing newline, no non-ASCII digits, ASCII only."
        ),
        "cases": [
            case(
                "placed_on",
                rr,
                "/$defs/placed_on/pattern",
                ["2026-11-09"],
                ["2026-11-09\n", "2026-11-09\r", *digits, "2026-11-9"],
            ),
            case(
                "issued_at",
                "read-command.schema.json",
                "/properties/issued_at/pattern",
                ["2026-11-09T10:00:00Z", "2026-11-09T10:00:00.123456789Z"],
                [
                    "2026-11-09T10:00:00Z\n",
                    "2026-11-09T10:00:00Z\r",
                    "２０２６-11-09T10:00:00Z",
                    "2026-11-09 10:00:00Z",
                    "2026-11-09T10:00:00+00:00",
                ],
            ),
            case(
                "completed_at",
                rr,
                "/properties/completed_at/pattern",
                ["2026-11-09T10:01:00Z"],
                ["2026-11-09T10:01:00Z\n", "2026-11-09 10:01:00"],
            ),
            case(
                "item_code",
                "approval-basket.schema.json",
                "/$defs/item_code/pattern",
                ["FAKE-1", "FAKE-A-B-9"],
                ["FAKE-1\n", "FAKE-1\r", "fake-1", "FAKE-a", "FAKE-É", "FAKE-", "FAKE-" + "A" * 33],
            ),
            case(
                "item_name",
                rr,
                "/$defs/display_name/pattern",
                ["Fake Bread (Test), 1.5"],
                ["Fake Bread\n", "Fake Bread\r", "Café", "", "A" * 61, "Fake\tBread"],
            ),
            case(
                "store_ref",
                "approval-basket.schema.json",
                "/properties/store_ref/pattern",
                [STORE],
                [STORE + "\n", STORE + "\r", "pst_short", "pst_0123456789abcdef012É"],
            ),
        ],
    }


# ------------------------------------------------------------------------------ files


def build() -> dict[str, str]:
    out: dict[str, str] = {}

    def put(name: str, doc: Any) -> None:
        out[name] = dump(doc)

    # read commands
    hist = read_command("read_history", {"limit": 20, "cursor": "c2VlZC1jdXJzb3I"})
    prev = read_command("read_preview", {"history_order_ref": PHO})
    bask = read_command("read_basket", {})
    put("fixtures/read-command.history.valid.json", hist)
    put("fixtures/read-command.preview.valid.json", prev)
    put("fixtures/read-command.basket.valid.json", bask)
    put("fixtures/read-command.with-order-ref.json", {**bask, "order_ref": "ordr_0123456789abcdef0123"})
    put("fixtures/read-command.with-execution-id.json", {**bask, "execution_id": "we_0123456789abcdef0123"})
    put("fixtures/read-command.with-workflow-id.json", {**bask, "workflow_id": "wf_0123456789abcdef0123"})
    put("fixtures/read-command.kind-prepare.json", {**bask, "kind": "prepare"})
    put("fixtures/read-command.params-wrong-kind.json", {**hist, "params": {"history_order_ref": PHO}})

    # results
    small = make_basket(SMALL_LINES)
    r_hist = result("read_history", history_payload())
    r_prev = result("read_preview", preview_payload(small, ["FAKE-GONE"]))
    r_bask = result("read_basket", basket_payload(small))
    put("fixtures/read-result.history.valid.json", r_hist)
    put("fixtures/read-result.preview.valid.json", r_prev)
    put("fixtures/read-result.basket.valid.json", r_bask)
    put(
        "fixtures/read-result.failed.valid.json",
        result(
            "read_preview",
            None,
            outcome="failed_terminal",
            error_code="provider_rejected",
            error_detail="unknown history_order_ref",
        ),
    )
    failed_with_payload = result(
        "read_preview", r_prev["payload"], outcome="failed_terminal", error_code="provider_rejected"
    )
    put("fixtures/read-result.failed-with-payload.json", failed_with_payload)

    # missing one field per kind
    h_missing = copy.deepcopy(history_payload())
    del h_missing["orders"][0]["total_minor"]
    p_missing = copy.deepcopy(r_prev["payload"])
    del p_missing["unavailable_items"]
    b_missing = copy.deepcopy(r_bask["payload"])
    del b_missing["empty"]
    put("fixtures/read-result.history.missing-field.json", result("read_history", h_missing))
    put("fixtures/read-result.preview.missing-field.json", result("read_preview", p_missing))
    put("fixtures/read-result.basket.missing-field.json", result("read_basket", b_missing))

    put("fixtures/read-result.with-binding-echo.json", {**r_bask, "tenant_ref": "tnr_0123456789abcdef0123"})
    put("fixtures/read-result.extra-field.json", {**r_bask, "customer_email": "x"})
    out["fixtures/read-result.quantity-float.json"] = dump(r_bask).replace('"quantity": 2,', '"quantity": 1.0,', 1)
    put("fixtures/read-result.digest-mismatch.json", {**r_bask, "payload_digest": "sha256:" + "0" * 64})
    put("fixtures/read-result.bad-date-time.json", {**r_bask, "completed_at": "2026-11-09 10:01:00"})

    # empty reads
    put("fixtures/read-result.basket.empty.valid.json", result("read_basket", {"empty": True, "display": []}))
    put(
        "fixtures/read-result.preview.empty.valid.json",
        result("read_preview", {"empty": True, "display": [], "unavailable_items": []}),
    )
    put(
        "fixtures/read-result.preview.all-unavailable.valid.json",
        result("read_preview", {"empty": True, "display": [], "unavailable_items": ["FAKE-GONE", "FAKE-OUT"]}),
    )
    bad_empty = {"empty": True, "display": [], "unavailable_items": [], "approval_basket": small}
    put("fixtures/read-result.empty-with-basket.json", result("read_preview", bad_empty))
    nonempty = {"empty": False, "display": [], "unavailable_items": []}
    put("fixtures/read-result.nonempty-without-basket.json", result("read_preview", nonempty))

    # maximal fixtures and the cap
    r_max_hist = result("read_history", max_history())
    out["fixtures/read-result.history.max.valid.json"] = compact(result("read_history", max_history()))
    out["fixtures/read-result.preview.max.valid.json"] = compact(result("read_preview", max_preview()))
    out["fixtures/read-result.basket.max.valid.json"] = compact(result("read_basket", max_basket_read()))
    out["fixtures/read-result.at-cap.json"] = padded(r_max_hist, CAP)
    out["fixtures/read-result.over-cap.json"] = padded(r_max_hist, CAP + 1)

    # claim responses
    v1 = v1_command()
    put(
        "fixtures/claim-response.v1alpha1-only.valid.json",
        {"schema_version": "bridge.mctl.ai/v1alpha1", "commands": [v1]},
    )
    wc = write_command(make_basket(MINIMAL_LINES))
    put(
        "fixtures/claim-response.v1alpha2.valid.json",
        {"schema_version": "bridge.mctl.ai/claim/v1alpha2", "commands": [v1, hist, wc]},
    )
    put(
        "fixtures/claim-response.extra-field.json",
        {"schema_version": "bridge.mctl.ai/v1alpha1", "commands": [v1], "next_poll": 5},
    )
    put("fixtures/claim-response.too-many.json", {"schema_version": "bridge.mctl.ai/v1alpha1", "commands": [v1] * 11})

    # approval baskets
    put("fixtures/approval-basket.valid.json", small)
    unsorted_ = copy.deepcopy(small)
    unsorted_["lines"].reverse()
    put("fixtures/approval-basket.unsorted.json", unsorted_)
    dup = make_basket([("FAKE-MILK", 1, 120), ("FAKE-MILK", 1, 120)])
    put("fixtures/approval-basket.duplicate-item.json", dup)
    bad_total = {**small, "total_minor": small["total_minor"] + 1}
    put("fixtures/approval-basket.total-mismatch.json", bad_total)
    disp = copy.deepcopy(small)
    disp["lines"][0]["item_name"] = "Fake Bread"
    put("fixtures/approval-basket.display-field.json", disp)

    # write command
    basket1 = make_basket(MINIMAL_LINES)
    put("fixtures/command-place-order-fake.valid.json", write_command(basket1))
    put("fixtures/command-place-order-fake.v1alpha1-payload.json", {**write_command(basket1), "payload": v1["payload"]})
    put(
        "fixtures/command-place-order-fake.wrapped-payload.json",
        {**write_command(basket1), "payload": {"approval_basket": basket1}},
    )
    missing = write_command(basket1)
    del missing["approval_ref"]
    put("fixtures/command-place-order-fake.missing-field.json", missing)
    put("fixtures/command-place-order-fake.extra-field.json", {**write_command(basket1), "note": "x"})
    put(
        "fixtures/command-place-order-fake.install-revision-ref.json",
        {**write_command(basket1), "install_revision_ref": "irv_0123456789abcdef0123"},
    )

    # vectors
    basket_min = make_basket(MINIMAL_LINES)
    permuted = dict(reversed(list(basket_min.items())))
    permuted["lines"] = [dict(reversed(list(ln.items()))) for ln in basket_min["lines"]]
    price_up = make_basket([("FAKE-1", 1, 501)])
    fee_up = make_basket(MINIMAL_LINES, delivery=200)

    def vec(name: str, doc: dict[str, Any], **rel: str) -> dict[str, Any]:
        canonical = jcs.canonicalize(doc).decode("utf-8")
        return {
            "name": name,
            "input_text": json.dumps(doc, separators=(",", ":")),
            "canonical": canonical,
            "fingerprint": sha(canonical),
            **rel,
        }

    def bad(name: str, reason: str, doc_text: str) -> dict[str, Any]:
        return {"name": name, "reason": reason, "input_text": doc_text}

    base_text = json.dumps(basket_min, separators=(",", ":"))
    out["vectors/basket-fingerprint.json"] = dump(
        {
            "description": "approval basket -> JCS canonical text -> basket_fingerprint = sha256:hex(SHA-256(JCS(basket))).",  # noqa: E501
            "vectors": [
                vec("minimal", basket_min),
                vec("thirty-lines", make_basket(lines_30())),
                vec("permuted-keys", permuted, same_fingerprint_as="minimal"),
                vec("unit-price-plus-one", price_up, different_from="minimal"),
                vec("delivery-fee-changed", fee_up, different_from="minimal"),
            ],
            "must_reject": [
                bad(
                    "unsorted-lines",
                    "unsorted_lines",
                    json.dumps(make_basket([("FAKE-B", 1, 100), ("FAKE-A", 1, 100)]), separators=(",", ":")),
                ),
                bad("duplicate-item-code", "duplicate_item", json.dumps(dup, separators=(",", ":"))),
                bad(
                    "wrong-subtotal",
                    "subtotal_mismatch",
                    json.dumps(
                        {**basket_min, "subtotal_minor": 501, "total_minor": 501 + 199 + 50}, separators=(",", ":")
                    ),
                ),
                bad(
                    "wrong-total",
                    "total_mismatch",
                    json.dumps({**basket_min, "total_minor": basket_min["total_minor"] - 1}, separators=(",", ":")),
                ),
                bad("extra-item-name", "schema_invalid", json.dumps(disp, separators=(",", ":"))),
                bad("quantity-1.0", "non_integer_token", base_text.replace('"quantity":1,', '"quantity":1.0,', 1)),
                bad("lowercase-item-code", "schema_invalid", base_text.replace("FAKE-1", "fake-1")),
                bad(
                    "non-ascii-item-code",
                    "schema_invalid",
                    json.dumps(basket_min, separators=(",", ":"), ensure_ascii=False).replace("FAKE-1", "FAKE-É"),
                ),
                bad(
                    "zero-lines",
                    "schema_invalid",
                    json.dumps(
                        {**basket_min, "lines": [], "subtotal_minor": 0, "total_minor": 249}, separators=(",", ":")
                    ),
                ),
            ],
        }
    )
    out["vectors/jcs-digest.json"] = dump(
        {
            "description": (
                "RFC 8785 vectors. `canonical` is written by hand from the RFC and the ECMAScript number rules, "
                "and `digest` is sha256 of its UTF-8 bytes. Every consumer reproduces them byte for byte."
            ),
            "vectors": jcs_vectors(),
        }
    )
    out["vectors/must-reject.json"] = (
        json.dumps(
            {
                "description": (
                    "Raw JSON text that every consumer, Go included, must reject before schema validation. "
                    "`applies` names the checks that must reject it; `reason` is the reference validator's code."
                ),
                "entries": must_reject_entries(),
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    )
    out["vectors/pattern-parity.json"] = dump(pattern_parity())
    return out


def sizes() -> list[tuple[str, int]]:
    basket = max_basket()
    rows = {
        "history, 20 orders x 5 lines": max_history(),
        "basket read, 30 lines": max_basket_read(),
        "preview, 30 lines and 30 unavailable": max_preview(),
        "approval basket alone, 30 lines": basket,
    }
    out = [(name, len(jcs.canonicalize(p))) for name, p in rows.items()]
    # Every envelope field at its maximum, including a payload_digest; the payload itself is excluded.
    envelope = result("read_history", {}, error_code="provider_unavailable", error_detail="x" * 128)
    envelope.pop("payload")
    envelope["lease_token"] = "lt_" + "A" * 64
    envelope["worker_identity"] = "provider:" + "a" * 32
    envelope["completed_at"] = "2026-11-09T10:01:00.123456789Z"
    envelope["attempt"] = 100
    out.append(
        (
            "result envelope without payload (every field at its maximum)",
            len(json.dumps(envelope, separators=(",", ":"))),
        )
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--sizes", action="store_true")
    args = parser.parse_args()
    if not (args.write or args.sizes):
        parser.error("give --write or --sizes")
    if args.write:
        for rel, text in build().items():
            write(rel, text)
    if args.sizes:
        for name, size in sizes():
            print(f"{size:>7,}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
